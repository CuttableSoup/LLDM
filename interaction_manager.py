"""
interaction_manager.py

This module handles the logic for entity interactions, including requirement checks
and effect application.
"""
from typing import List, Tuple, Dict, Any, Optional
import logging
import random

from models import Entity, Interaction, Effect, Requirement, Magnitude, DurationComponent

logger = logging.getLogger("InteractionManager")

class InteractionManager:
    def __init__(self):
        pass

    def roll_d6(self, dice: int, pips: int = 0) -> int:
        """Rolls N d6 and adds pips."""
        total = 0
        for _ in range(dice):
            total += random.randint(1, 6)
        return total + pips

    def resolve_test(self, user: Entity, test_params: Dict, target: Entity) -> bool:
        """
        Resolves a test requirement.
        
        test_params: {
            'source': 'user',
            'reference': 'skill', # or 'attribute', 'specialization'
            'value': 'longsword', # the specific stat name
            'difficulty': { ... } # difficulty definition
        }
        """
        # 1. Determine User's Roll
        source_type = test_params.get('source', 'user')
        actor = user if source_type == 'user' else target # simplistic
        
        stat_name = test_params.get('value')
        stat_val = 0
        
        # Look up in flattened attributes
        if stat_name in actor.attribute:
            stat_val = actor.attribute[stat_name].base
        
        # Calculate Dice and Pips
        dice = stat_val // 3
        pips = stat_val % 3
        
        roll_total = self.roll_d6(dice, pips)
        
        # 2. Determine Difficulty
        difficulty_params = test_params.get('difficulty', {})
        difficulty_val = 0
        
        diff_type = difficulty_params.get('type', 'static')
        
        if diff_type == 'static':
            difficulty_val = difficulty_params.get('value', 0)
        elif diff_type == 'roll':
            # Opposed roll
            diff_source = difficulty_params.get('source', 'target')
            diff_actor = target if diff_source == 'target' else user
            
            diff_stat = difficulty_params.get('value')
            diff_stat_val = 0
            if diff_stat in diff_actor.attribute:
                diff_stat_val = diff_actor.attribute[diff_stat].base
            
            d_dice = diff_stat_val // 3
            d_pips = diff_stat_val % 3
            difficulty_val = self.roll_d6(d_dice, d_pips)

        logger.info(f"Test: {actor.name} rolled {roll_total} (Stat: {stat_val} -> {dice}D+{pips}) vs Difficulty {difficulty_val}")
        
        return roll_total >= difficulty_val

    def execute_interaction(self, user: Entity, interaction: Interaction, targets: List[Entity], game_time: Any = None, game_entities: Dict[str, Entity] = None) -> Tuple[bool, str, str]:
        """
        Executes an interaction from a user to a list of targets.
        
        Returns:
            Tuple[bool, str, str]: (Success, Narrative Message, Log Message)
        """
        # 1. Check Requirements
        can_execute, reason = self.check_requirements(user, interaction, targets)
        if not can_execute:
            return False, f"You cannot do that: {reason}", f"{user.name} failed to {interaction.type}: {reason}"

        # 2. Pay Costs (part of requirements usually, but we might separate "check" from "consume")
        # For now, we assume requirements check includes resource availability, 
        # but we need a step to actually deduct MP/FP/Items.
        self._consume_costs(user, interaction)

        # 3. Apply Effects
        narrative_parts = []
        log_parts = []
        
        # User Effects
        if interaction.user_effect:
            results = self.apply_effects(user, interaction.user_effect, [user], game_time, game_entities)
            narrative_parts.extend(results)
            
        # Target Effects
        if interaction.target_effect:
            results = self.apply_effects(user, interaction.target_effect, targets, game_time, game_entities)
            narrative_parts.extend(results)
            
        # Self Effects (distinct from user_effect? usually same, but schema has both)
        if interaction.self_effect:
            results = self.apply_effects(user, interaction.self_effect, [user], game_time, game_entities)
            narrative_parts.extend(results)

        # Construct final messages
        action_desc = interaction.description if interaction.description else f"uses {interaction.type}"
        narrative = f"{user.name} {action_desc}. " + " ".join(narrative_parts)
        log = f"{user.name} executed {interaction.type}. " + " ".join(log_parts)
        
        return True, narrative, log

    def check_requirements(self, user: Entity, interaction: Interaction, targets: List[Entity]) -> Tuple[bool, str]:
        """Checks if the interaction can be performed."""
        
        # Range Check
        if interaction.range > 0:
            for target in targets:
                # Calculate distance (Chebyshev distance for grid)
                if not hasattr(user, 'x') or not hasattr(user, 'y') or not hasattr(target, 'x') or not hasattr(target, 'y'):
                    # If coordinates are missing, assume valid (or maybe invalid? defaulting to valid for abstract)
                    continue
                
                dist = max(abs(user.x - target.x), abs(user.y - target.y))
                if dist > interaction.range:
                    return False, f"Target {target.name} is out of range ({dist} > {interaction.range})"

        # User Requirements
        for req in interaction.user_requirement:
            if not self._check_single_requirement(user, req, user):
                return False, f"User requirement failed: {req.type} {req.name if req.name else ''}"

        # Target Requirements
        for target in targets:
            for req in interaction.target_requirement:
                if not self._check_single_requirement(user, req, target):
                    return False, f"Target requirement failed for {target.name}"

        return True, ""

    def _check_single_requirement(self, user: Entity, req: Requirement, target: Entity) -> bool:
        if req.type == "test":
            if req.test:
                 test_params = req.test.copy()
                 if req.difficulty:
                     test_params['difficulty'] = req.difficulty
                 
                 return self.resolve_test(user, test_params, target)
            return True
            
        elif req.type == "property":
            # Resource Checks and Costs
            if req.name in ['cur_mp', 'cur_fp', 'cur_hp', 'cur_stamina']:
                if isinstance(req.relation, (int, float)):
                    current_val = getattr(target, req.name, 0)
                    if req.relation < 0:
                        # Cost: Must have enough to pay
                        cost = abs(req.relation)
                        if current_val < cost:
                            return False
                    else:
                        # Prerequisite: Must have at least X
                        if current_val < req.relation:
                            return False
                return True
            
            # General Property Check (e.g. "is_undead": True)
            if hasattr(target, req.name):
                val = getattr(target, req.name)
                if req.relation is not None:
                    # Simple equality check for now
                    return val == req.relation
                return bool(val)
            
        return True

    def _consume_costs(self, user: Entity, interaction: Interaction):
        """Deduct resources."""
        for req in interaction.user_requirement:
            if req.type == "property" and req.name in ['cur_mp', 'cur_fp', 'cur_hp']:
                if isinstance(req.relation, (int, float)) and req.relation < 0:
                    current_val = getattr(user, req.name, 0)
                    # We assume check_requirements passed, so just deduct
                    setattr(user, req.name, current_val + req.relation) # relation is negative

    def apply_effects(self, user: Entity, effects: List[Effect], targets: List[Entity], game_time: Any = None, game_entities: Dict[str, Entity] = None) -> List[str]:
        results = []
        for target in targets:
            for effect in effects:
                msg = self._apply_single_effect(user, effect, target, game_time, game_entities)
                if msg:
                    results.append(msg)
        return results

    def _apply_single_effect(self, user: Entity, effect: Effect, target: Entity, game_time: Any = None, game_entities: Dict[str, Entity] = None) -> str:
        # Resolve Magnitude
        value = 0
        if effect.magnitude:
            value = self.resolve_magnitude(effect.magnitude, user, target)
        
        # Status Effect (Entity Application)
        if effect.entity and game_entities:
            # Look up the status entity
            status_name = effect.entity
            if status_name in game_entities:
                # Clone it (shallow copy usually enough if we don't mutate deep structure, but be careful)
                # We need a deep copy of the entity to track its own state (duration, etc)
                import copy
                status_entity = copy.deepcopy(game_entities[status_name])
                
                # Set Timestamp on duration components
                if game_time:
                    for dur in status_entity.duration:
                        dur.timestamp = game_time.total_seconds
                
                # Add to target status
                target.status.append(status_entity)
                return f"{target.name} is now affected by {status_entity.name}."
            else:
                return f"Error: Status entity '{status_name}' not found."

        # Apply Logic
        if effect.name == "damage":
            target.cur_hp -= value
            return f"{target.name} takes {value} damage."
        elif effect.name == "heal":
            target.cur_hp += value
            if target.cur_hp > target.max_hp:
                target.cur_hp = target.max_hp
            return f"{target.name} heals for {value} HP."
        elif effect.apply:
            # Example: damage_cur_hp
            if "damage_" in effect.apply:
                stat = effect.apply.replace("damage_", "")
                if hasattr(target, stat):
                    current_val = getattr(target, stat)
                    setattr(target, stat, current_val - value)
                    return f"{target.name}'s {stat} decreases by {value}."
            elif "restore_" in effect.apply:
                stat = effect.apply.replace("restore_", "")
                if hasattr(target, stat):
                    current_val = getattr(target, stat)
                    # TODO: Check max
                    setattr(target, stat, current_val + value)
                    return f"{target.name}'s {stat} increases by {value}."
        
        elif effect.inventory:
            # Inventory Operations
            # effect.inventory is a dict, e.g. {'operation': 'add', 'list': [...]}
            op = effect.inventory.get('operation', 'add')
            items_data = effect.inventory.get('list', [])
            
            # We need to convert dict items to InventoryItem if they aren't already
            # But wait, effect.inventory is a dict from the loader? 
            # The loader parses 'inventory' in Effect as a dict.
            # We need to handle the structure.
            
            # For now, let's assume items_data is a list of dicts describing items
            count = 0
            for item_dict in items_data:
                item_name = item_dict.get('item')
                quantity = item_dict.get('quantity', 1)
                
                if op == 'add':
                    # Check if item exists to stack?
                    found = False
                    for inv_item in target.inventory:
                        if inv_item.item == item_name:
                            inv_item.quantity += quantity
                            found = True
                            break
                    if not found:
                        # We need to import InventoryItem? It's in models.
                        # But we can't import inside method easily if not at top.
                        # It is imported at top.
                        from models import InventoryItem
                        new_item = InventoryItem(item=item_name, quantity=quantity)
                        target.inventory.append(new_item)
                    count += 1
                    
                elif op == 'remove':
                    # Remove items
                    remaining_to_remove = quantity
                    # Iterate backwards to safely remove
                    for i in range(len(target.inventory) - 1, -1, -1):
                        inv_item = target.inventory[i]
                        if inv_item.item == item_name:
                            if inv_item.quantity > remaining_to_remove:
                                inv_item.quantity -= remaining_to_remove
                                remaining_to_remove = 0
                                break
                            else:
                                remaining_to_remove -= inv_item.quantity
                                target.inventory.pop(i)
                                if remaining_to_remove <= 0:
                                    break
                    count += 1

            if count > 0:
                return f"{target.name}'s inventory was updated ({op})."
        
        return ""

    def resolve_magnitude(self, magnitude: Magnitude, user: Entity, target: Entity) -> int:
        base_value = 0
        
        # 1. Identify Source
        source_entity = None
        if magnitude.source == "user":
            source_entity = user
        elif magnitude.source == "target":
            source_entity = target
        elif magnitude.source == "self":
            source_entity = user
        
        # 2. Get Reference Value
        if source_entity and magnitude.reference:
            # Check attributes
            if magnitude.reference in source_entity.attribute:
                base_value = source_entity.attribute[magnitude.reference].base
            # Check skills (nested in attributes? or flattened?)
            # The loader flattens attributes, so "physique.strength" might be a key.
            elif magnitude.reference in source_entity.attribute: # Flattened keys
                 base_value = source_entity.attribute[magnitude.reference].base
            # Direct property
            elif hasattr(source_entity, magnitude.reference):
                base_value = getattr(source_entity, magnitude.reference)
                if not isinstance(base_value, (int, float)):
                    base_value = 0
        
        # 3. Apply Value (if static)
        if magnitude.value and isinstance(magnitude.value, (int, float)):
             if magnitude.type == "static":
                 base_value = magnitude.value
             elif magnitude.type == "roll":
                 # If magnitude is a roll based on a stat
                 # But here 'value' is usually the stat name if reference is skill/attribute
                 # If reference is 'none' and type is 'roll', maybe value is raw dice?
                 pass

        # If reference was valid, base_value is the stat total.
        # If type is roll, we roll it.
        if magnitude.type == "roll":
             dice = base_value // 3
             pips = base_value % 3
             base_value = self.roll_d6(dice, pips)
        
        # 4. Apply Pre-Mod
        total = base_value + magnitude.pre_mod
        
        return int(total)

    def process_triggers(self, entity: Entity, game_time: Any) -> List[str]:
        """
        Checks and executes triggers for an entity's statuses.
        Also handles expiration of statuses.
        """
        results = []
        
        # We need to iterate over a copy because we might remove statuses
        for status in list(entity.status):
            # 1. Check Expiration
            # Status entities might have a 'duration' component in their 'duration' list
            # or directly as a field if the schema allows. 
            # The schema says 'duration' is a list of DurationComponent.
            
            expired = False
            for dur in status.duration:
                if dur.length != "*": # '*' means indefinite
                    # Check if time passed > length
                    # We assume dur.timestamp is the start time (in seconds)
                    # If it's 0, we might need to set it when applying? 
                    # For now, let's assume it's set correctly.
                    
                    start_time = dur.timestamp
                    current_seconds = game_time.total_seconds
                    
                    # If timestamp is 0, it might mean "just created". 
                    # But if we don't set it, it will never expire properly if 0 is valid time.
                    # However, game starts at year 2000, so total_seconds is huge. 0 is definitely in the past.
                    # Wait, if timestamp is 0 (default), then (current - 0) > length is likely true immediately.
                    # So we MUST ensure timestamp is set when effect is applied.
                    
                    if (current_seconds - start_time) >= dur.length:
                        expired = True
                        break
            
            if expired:
                entity.status.remove(status)
                results.append(f"{status.name} has expired.")
                continue
            
            # 2. Check Triggers
            for trigger in status.trigger:
                # Check frequency
                # frequency: "round", "minute", "hour", "day"
                # We need to store last trigger time. 'timestamp' in Trigger.
                
                should_trigger = False
                current_seconds = game_time.total_seconds
                last_trigger = trigger.timestamp if trigger.timestamp is not None else 0
                
                interval = 0
                if trigger.frequency == "round":
                    interval = 6
                elif trigger.frequency == "minute":
                    interval = 60
                elif trigger.frequency == "hour":
                    interval = 3600
                elif trigger.frequency == "day":
                    interval = 86400
                
                if interval > 0:
                    if (current_seconds - last_trigger) >= interval:
                        should_trigger = True
                
                if should_trigger:
                    # Execute Effects
                    # Triggers have target/user/self effects.
                    # Who is target? Usually the entity having the status.
                    # Who is user? The status itself?
                    
                    # Apply Self Effects (to the entity holding the status)
                    if trigger.self_effect:
                        msgs = self.apply_effects(entity, trigger.self_effect, [entity])
                        results.extend(msgs)
                    
                    # Update timestamp
                    trigger.timestamp = current_seconds
                    
        return results
