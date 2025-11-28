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

    def execute_interaction(self, user: Entity, interaction: Interaction, targets: List[Entity]) -> Tuple[bool, str, str]:
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
            results = self.apply_effects(user, interaction.user_effect, [user])
            narrative_parts.extend(results)
            
        # Target Effects
        if interaction.target_effect:
            results = self.apply_effects(user, interaction.target_effect, targets)
            narrative_parts.extend(results)
            
        # Self Effects (distinct from user_effect? usually same, but schema has both)
        if interaction.self_effect:
            results = self.apply_effects(user, interaction.self_effect, [user])
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
            # TODO: Real distance check using map. For now, assume valid if targets exist.
            pass

        # User Requirements
        for req in interaction.user_requirement:
            if not self._check_single_requirement(user, req, user):
                return False, f"User requirement failed: {req.type}"

        # Target Requirements
        for target in targets:
            for req in interaction.target_requirement:
                if not self._check_single_requirement(user, req, target):
                    return False, f"Target requirement failed for {target.name}"

        return True, ""

    def _check_single_requirement(self, user: Entity, req: Requirement, target: Entity) -> bool:
        if req.type == "test":
            if req.test:
                 # We need to merge the difficulty into the test params if it's separate in the Requirement object
                 # The Requirement model has 'test' (dict) and 'difficulty' (int/dict)
                 test_params = req.test.copy()
                 if req.difficulty:
                     test_params['difficulty'] = req.difficulty
                 
                 return self.resolve_test(user, test_params, target)
            return True
        elif req.type == "cur_mp":
            # This is a bit tricky, the schema might define costs differently.
            # Assuming 'test' or specific fields for costs.
            # Checking if the requirement object has a resource check
            pass
            
        # Basic resource checks often come from 'cost' object in Entity, but Interaction has requirements.
        # Let's look at how requirements are structured in loader.py:
        # cur_mp/cur_fp might be in 'test' or as direct properties if the loader parses them that way.
        # The loader puts unknown keys into 'property' type requirements.
        
        if req.type == "property":
            # Example: {type: 'property', name: 'cur_mp', relation: -5} (meaning cost 5?)
            # Or {type: 'property', name: 'cur_mp', relation: {'min': 10}}
            pass
            
        return True

    def _consume_costs(self, user: Entity, interaction: Interaction):
        """Deduct resources."""
        # TODO: Implement cost deduction based on requirements or a specific cost field
        pass

    def apply_effects(self, user: Entity, effects: List[Effect], targets: List[Entity]) -> List[str]:
        results = []
        for target in targets:
            for effect in effects:
                msg = self._apply_single_effect(user, effect, target)
                if msg:
                    results.append(msg)
        return results

    def _apply_single_effect(self, user: Entity, effect: Effect, target: Entity) -> str:
        # Resolve Magnitude
        value = 0
        if effect.magnitude:
            value = self.resolve_magnitude(effect.magnitude, user, target)
        
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
