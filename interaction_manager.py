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
    def __init__(self, attributes_data: List[Dict] = None):
        self.opposes_map: Dict[str, set] = {}
        self.spec_to_skill_map: Dict[str, str] = {}
        if attributes_data:
            self._build_opposes_map(attributes_data)
    def _build_opposes_map(self, attributes_data: List[Dict]):
        """Builds a map of attack_skill -> set of defense_skills."""
        for doc in attributes_data:
            if 'aptitude' not in doc: continue
            for attr_name, attr_data in doc['aptitude'].items():
                if not isinstance(attr_data, dict): continue
                for skill_name, skill_data in attr_data.items():
                    if skill_name in ['description', 'keywords', 'opposes']: continue
                    if isinstance(skill_data, dict):
                        if 'opposes' in skill_data:
                            for target in skill_data['opposes']:
                                if target not in self.opposes_map:
                                    self.opposes_map[target] = set()
                                self.opposes_map[target].add(skill_name)
                        for spec_name, spec_data in skill_data.items():
                            if spec_name in ['description', 'keywords', 'opposes']: continue
                            self.spec_to_skill_map[spec_name] = skill_name
                            if isinstance(spec_data, dict) and 'opposes' in spec_data:
                                for target in spec_data['opposes']:
                                    if target not in self.opposes_map:
                                        self.opposes_map[target] = set()
                                    self.opposes_map[target].add(spec_name)
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
        source_type = test_params.get('source', 'user')
        actor = user if source_type == 'user' else target
        stat_name = test_params.get('value')
        stat_val = 0
        if stat_name in actor.attribute:
            stat_val = actor.attribute[stat_name].base
        dice = stat_val // 3
        pips = stat_val % 3
        roll_total = self.roll_d6(dice, pips)
        difficulty_params = test_params.get('difficulty', {})
        difficulty_val = 0
        diff_type = difficulty_params.get('type', 'static')
        if diff_type == 'static':
            difficulty_val = difficulty_params.get('value', 0)
        elif diff_type == 'roll':
            diff_source = difficulty_params.get('source', 'target')
            diff_actor = target if diff_source == 'target' else user
            diff_stat = difficulty_params.get('value')
            if diff_stat == 'opposed':
                candidates = set()
                if stat_name in self.opposes_map:
                    candidates.update(self.opposes_map[stat_name])
                if stat_name in self.spec_to_skill_map:
                    parent_skill = self.spec_to_skill_map[stat_name]
                    if parent_skill in self.opposes_map:
                        candidates.update(self.opposes_map[parent_skill])
                best_skill = None
                best_val = -1
                for cand in candidates:
                    val = 0
                    if cand in diff_actor.attribute:
                        val = diff_actor.attribute[cand].base
                    if val > best_val:
                        best_val = val
                        best_skill = cand
                if best_skill:
                    diff_stat = best_skill
            diff_stat_val = 0
            if diff_stat in diff_actor.attribute:
                diff_stat_val = diff_actor.attribute[diff_stat].base
            d_dice = diff_stat_val // 3
            d_pips = diff_stat_val % 3
            difficulty_val = self.roll_d6(d_dice, d_pips)
        skill_msg = f"Skill: {stat_name}"
        if diff_type == 'roll':
            skill_msg += f" vs {diff_stat}"
        logger.info(f"Test: {actor.name} rolled {roll_total} (Stat: {stat_val} -> {dice}D+{pips}) vs Difficulty {difficulty_val} ({skill_msg})")
        return roll_total >= difficulty_val
    def execute_interaction(self, user: Entity, interaction: Interaction, targets: List[Entity], game_time: Any = None, game_entities: Dict[str, Entity] = None) -> Tuple[bool, str, str]:
        """
        Executes an interaction from a user to a list of targets.
        Returns:
            Tuple[bool, str, str]: (Success, Narrative Message, Log Message)
        """
        can_execute, reason = self.check_requirements(user, interaction, targets)
        if not can_execute:
            return False, f"You cannot do that: {reason}", f"{user.name} failed to {interaction.type}: {reason}"
        self._consume_costs(user, interaction)
        narrative_parts = []
        log_parts = []
        if interaction.user_effect:
            results = self.apply_effects(user, interaction.user_effect, [user], game_time, game_entities)
            narrative_parts.extend(results)
        if interaction.target_effect:
            results = self.apply_effects(user, interaction.target_effect, targets, game_time, game_entities)
            narrative_parts.extend(results)
        if interaction.self_effect:
            results = self.apply_effects(user, interaction.self_effect, [user], game_time, game_entities)
            narrative_parts.extend(results)
        action_desc = interaction.description if interaction.description else f"uses {interaction.type}"
        narrative = f"{user.name} {action_desc}. " + " ".join(narrative_parts)
        log = f"{user.name} executed {interaction.type}. " + " ".join(log_parts)
        return True, narrative, log
    def check_requirements(self, user: Entity, interaction: Interaction, targets: List[Entity]) -> Tuple[bool, str]:
        """Checks if the interaction can be performed."""
        if interaction.range > 0:
            for target in targets:
                if not hasattr(user, 'x') or not hasattr(user, 'y') or not hasattr(target, 'x') or not hasattr(target, 'y'):
                    continue
                dist = max(abs(user.x - target.x), abs(user.y - target.y))
                if dist > interaction.range:
                    return False, f"Target {target.name} is out of range ({dist} > {interaction.range})"
        for req in interaction.user_requirement:
            if not self._check_single_requirement(user, req, user):
                return False, f"User requirement failed: {req.type} {req.name if req.name else ''}"
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
            if req.name in ['cur_mp', 'cur_fp', 'cur_hp', 'cur_stamina']:
                if isinstance(req.relation, (int, float)):
                    current_val = getattr(target, req.name, 0)
                    if req.relation < 0:
                        cost = abs(req.relation)
                        if current_val < cost:
                            return False
                    else:
                        if current_val < req.relation:
                            return False
                return True
            if hasattr(target, req.name):
                val = getattr(target, req.name)
                if req.relation is not None:
                    return val == req.relation
                return bool(val)
        return True
    def _consume_costs(self, user: Entity, interaction: Interaction):
        """Deduct resources."""
        for req in interaction.user_requirement:
            if req.type == "property" and req.name in ['cur_mp', 'cur_fp', 'cur_hp']:
                if isinstance(req.relation, (int, float)) and req.relation < 0:
                    current_val = getattr(user, req.name, 0)
                    setattr(user, req.name, current_val + req.relation)
    def apply_effects(self, user: Entity, effects: List[Effect], targets: List[Entity], game_time: Any = None, game_entities: Dict[str, Entity] = None) -> List[str]:
        results = []
        for target in targets:
            for effect in effects:
                msg = self._apply_single_effect(user, effect, target, game_time, game_entities)
                if msg:
                    results.append(msg)
        return results
    def _apply_single_effect(self, user: Entity, effect: Effect, target: Entity, game_time: Any = None, game_entities: Dict[str, Entity] = None) -> str:
        value = 0
        if effect.magnitude:
            value = self.resolve_magnitude(effect.magnitude, user, target)
        if effect.parameters and 'resist' in effect.parameters:
            resist_params = effect.parameters['resist']
            resistance = 0
            skill_used = "None"
            res_source = resist_params.get('source', 'target')
            res_actor = target if res_source == 'target' else user
            res_type = resist_params.get('type', 'static')
            if res_type == 'static':
                resistance = resist_params.get('value', 0)
            elif res_type == 'roll':
                stat_name = resist_params.get('value')
                if stat_name == 'opposed':
                    all_defenses = set()
                    for defenses in self.opposes_map.values():
                        all_defenses.update(defenses)
                    best_skill = None
                    best_val = -1
                    for cand in all_defenses:
                        val = 0
                        if cand in res_actor.attribute:
                            val = res_actor.attribute[cand].base
                        if val > best_val:
                            best_val = val
                            best_skill = cand
                    if best_skill:
                        stat_name = best_skill
                    else:
                        stat_name = "dodge"
                skill_used = stat_name
                stat_val = 0
                if stat_name:
                    if stat_name in res_actor.attribute:
                        stat_val = res_actor.attribute[stat_name].base
                    elif stat_name in res_actor.attribute:
                        stat_val = res_actor.attribute[stat_name].base
                if stat_val > 0:
                    r_dice = stat_val // 3
                    r_pips = stat_val % 3
                    resistance = self.roll_d6(r_dice, r_pips)
            value = max(0, value - resistance)
            skill_log_msg = f"Skill: {skill_used}"
            logger.info(f"Resistance applied: {resistance} ({skill_log_msg}), {value} applied to {effect.apply}")
        if effect.entity and game_entities:
            status_name = effect.entity
            if status_name in game_entities:
                import copy
                status_entity = copy.deepcopy(game_entities[status_name])
                if game_time:
                    for dur in status_entity.duration:
                        dur.timestamp = game_time.total_seconds
                target.status.append(status_entity)
                return f"{target.name} is now affected by {status_entity.name}."
            else:
                return f"Error: Status entity '{status_name}' not found."
        if effect.name == "damage":
            target.cur_hp -= value
            return f"{target.name} takes {value} damage."
        elif effect.name == "heal":
            target.cur_hp += value
            if target.cur_hp > target.max_hp:
                target.cur_hp = target.max_hp
            return f"{target.name} heals for {value} HP."
        elif effect.apply:
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
                    setattr(target, stat, current_val + value)
                    return f"{target.name}'s {stat} increases by {value}."
        elif effect.inventory:
            op = effect.inventory.get('operation', 'add')
            items_data = effect.inventory.get('list', [])
            count = 0
            for item_dict in items_data:
                item_name = item_dict.get('item')
                quantity = item_dict.get('quantity', 1)
                if op == 'add':
                    found = False
                    for inv_item in target.inventory:
                        if inv_item.item == item_name:
                            inv_item.quantity += quantity
                            found = True
                            break
                    if not found:
                        from models import InventoryItem
                        new_item = InventoryItem(item=item_name, quantity=quantity)
                        target.inventory.append(new_item)
                    count += 1
                elif op == 'remove':
                    remaining_to_remove = quantity
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
        source_entity = None
        if magnitude.source == "user":
            source_entity = user
        elif magnitude.source == "target":
            source_entity = target
        elif magnitude.source == "self":
            source_entity = user
        if source_entity:
            lookup_key = magnitude.reference
            if magnitude.reference in ['skill', 'attribute', 'specialization'] and isinstance(magnitude.value, str):
                lookup_key = magnitude.value
            if lookup_key in source_entity.attribute:
                base_value = source_entity.attribute[lookup_key].base
            elif hasattr(source_entity, lookup_key):
                base_value = getattr(source_entity, lookup_key)
                if not isinstance(base_value, (int, float)):
                    base_value = 0
        if magnitude.value and isinstance(magnitude.value, (int, float)):
            if magnitude.type == "static":
                base_value = magnitude.value
            elif magnitude.type == "roll":
                pass
        if magnitude.type == "roll":
            dice = base_value // 3
            pips = base_value % 3
            base_value = self.roll_d6(dice, pips)
        total = base_value + magnitude.pre_mod
        return int(total)
    def process_triggers(self, entity: Entity, game_time: Any) -> List[str]:
        """
        Checks and executes triggers for an entity's statuses.
        Also handles expiration of statuses.
        """
        results = []
        for status in list(entity.status):
            expired = False
            for dur in status.duration:
                if dur.length != "*":
                    start_time = dur.timestamp
                    current_seconds = game_time.total_seconds
                    if (current_seconds - start_time) >= dur.length:
                        expired = True
                        break
            if expired:
                entity.status.remove(status)
                results.append(f"{status.name} has expired.")
                continue
            for trigger in status.trigger:
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
                    if trigger.self_effect:
                        msgs = self.apply_effects(entity, trigger.self_effect, [entity])
                        results.extend(msgs)
                    trigger.timestamp = current_seconds
        return results

