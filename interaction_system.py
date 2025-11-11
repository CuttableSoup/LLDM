# interaction_system.py
"""
This module is the core rules engine for processing entity interactions.

It takes a 'user' (actor), a 'target', and an 'interaction' (an entity 
like a weapon, spell, or item) and processes the logic defined in the 
interaction's YAML definition (e.g., requirements, costs, proficiency, apply).
"""
from __future__ import annotations
from typing import List, Dict, Optional, Any, Tuple
import re
import random

try:
    from classes import Entity, Skill, Attribute, Cost # <-- Import Cost
except ImportError:
    print("Warning: 'classes.py' not found. Using placeholder classes.")
    class Entity:
        name: str = "Player"
        attribute: Dict = {}
        cur_hp: int = 10
        cur_fp: int = 10
        cur_mp: int = 10
        max_hp: int = 10
        max_fp: int = 10
        max_mp: int = 10
    class Skill: pass
    class Attribute: pass
    class Cost:
        initial: List = []
        ongoing: List = []

class InteractionProcessor:
    """
    Processes a single interaction between a user, a target, 
    and an interaction-defining entity.
    """
    
    def __init__(self, user: Entity, target: Optional[Entity], 
                 interaction_entity: Entity, loader_attributes: List[Any]):
        """
        Initializes the processor for a single interaction.

        Args:
            user: The entity performing the action (e.g., player).
            target: The entity being acted upon (can be None).
            interaction_entity: The entity defining the action (e.g., a sword, a spell).
            loader_attributes: The raw list of docs from attributes.yaml.
        """
        self.user = user
        self.target = target
        self.interaction = interaction_entity
        
        # This will store the results, e.g., ('damage', target, 5)
        self.results: List[Dict[str, Any]] = []
        # This will store narrative messages
        self.narrative: List[str] = []
        
        # Parse the 'opposes' map from the attributes data
        self.opposes_map: Dict[str, List[str]] = self._parse_opposes_map(loader_attributes)

    def _parse_opposes_map(self, loader_attributes: List[Any]) -> Dict[str, List[str]]:
        """
        Parses the attributes data to build a map of which skills oppose what.
        e.g., {'blade': ['blade', 'axe', 'dodge', ...]}
        """
        opposes_map = {}
        try:
            for doc in loader_attributes:
                if 'aptitude' not in doc:
                    continue
                for attr_name, attr_data in doc.get('aptitude', {}).items():
                    if not isinstance(attr_data, dict):
                        continue
                    
                    # Loop through skills like 'blade', 'dodge'
                    for skill_name, skill_data in attr_data.items():
                        if not isinstance(skill_data, dict):
                            continue
                        
                        if 'opposes' in skill_data:
                            # The key is the skill that *can be opposed*
                            # The value is the list of skills *that can oppose it*
                            #
                            # We need to reverse this for our lookup.
                            # We want: "what skills can 'dodge' be used against?"
                            #
                            # Let's read the YAML again:
                            # physique.blade.opposes: [blade, axe, ...]
                            # dexterity.dodge.opposes: [blade, axe, ...]
                            #
                            # This means:
                            # An attack with 'blade' can be opposed by 'blade', 'axe', 'dodge'.
                            #
                            # This is the correct map:
                            # key = attacking skill
                            # value = list of defending skills
                            
                            opposing_skills_list = skill_data.get('opposes', [])
                            if opposing_skills_list:
                                opposes_map[skill_name] = opposing_skills_list

        except Exception as e:
            print(f"Error parsing attributes for opposes map: {e}")
            
        # Manually wire up the map based on attributes.yaml structure
        # The file shows 'opposes' *on the skill being used to oppose*.
        # So 'dodge' has an 'opposes' list, meaning 'dodge' *can oppose* those things.
        
        final_map: Dict[str, List[str]] = {}
        
        for doc in loader_attributes:
            if 'aptitude' not in doc: continue
            for attr_data in doc.get('aptitude', {}).values():
                if not isinstance(attr_data, dict): continue
                for skill_name, skill_data in attr_data.items():
                    if not isinstance(skill_data, dict): continue
                    
                    # 'skill_name' is the *defending* skill (e.g., 'dodge')
                    # 'opposed_list' is the *attacking* skills it can block (e.g., ['blade', 'axe'])
                    opposed_list = skill_data.get('opposes', [])
                    
                    for attacking_skill in opposed_list:
                        if attacking_skill not in final_map:
                            final_map[attacking_skill] = []
                        if skill_name not in final_map[attacking_skill]:
                            final_map[attacking_skill].append(skill_name)

        print(f"InteractionProcessor: Built opposes map: {final_map}")
        return final_map

    def _get_entity(self, keyword: str) -> Optional[Entity]:
        """Resolves 'user', 'target', or 'self' to the correct entity object."""
        if keyword == 'user':
            return self.user
        elif keyword == 'target':
            return self.target
        elif keyword == 'self':
            return self.interaction
        return None

    def _get_attribute_sum(self, entity: Entity, path_str: str) -> int:
        """
        Resolves an attribute path by summing its hierarchical parts,
        as per the game's logic.
        
        e.g., 'physique.blade.longsword' sums base values for:
        - 'physique'
        - 'physique.blade'
        - 'physique.blade.longsword'
        
        This automatically handles the fallback logic.
        """
        if not entity:
            return 0
            
        parts = path_str.split('.')
        total_sum = 0
        current_key = ""
        
        for part in parts:
            if not current_key:
                current_key = part
            else:
                current_key = f"{current_key}.{part}"
            
            # Get the Attribute object from the flat dictionary
            # (e.g., entity.attribute['physique.blade'])
            attr_obj = entity.attribute.get(current_key)
            if attr_obj:
                total_sum += attr_obj.base
                
        return total_sum

    def _resolve_path(self, entity: Entity, path: str) -> Any:
        """
        Resolves a dot-notation path to a value on an entity.
        
        Handles:
        1. Simple attributes: 'cur_hp', 'name', etc.
        2. Complex attributes: 'attribute.physique.strength'
        """
        if not entity:
            return 0
            
        parts = path.split('.')
        
        # Handle complex attribute summing
        if parts[0] == 'attribute':
            attribute_path = ".".join(parts[1:])
            return self._get_attribute_sum(entity, attribute_path)
            
        # Handle simple top-level attributes like 'cur_hp', 'name', 'supertype'
        try:
            val = getattr(entity, parts[0], None)
            if val is None:
                return 0
            
            # Handle simple nested gets like 'quality.eye'
            for part in parts[1:]:
                if isinstance(val, dict):
                    val = val.get(part)
                else:
                    val = getattr(val, part, None)
            
            return val
        except Exception:
            return 0

    def _set_value(self, entity: Entity, path: str, value: Any, operation: str = 'set'):
        """
        Sets a value on an entity based on a path.
        
        Supported paths:
        - 'damage_cur_hp'
        - 'heal_cur_hp'
        - 'cur_hp', 'cur_mp', 'cur_fp'
        - 'status.entity.add' (to add a status)
        """
        if not entity:
            return

        if path == 'damage_cur_hp':
            # Special case for 'damage'
            damage_amount = int(value)
            entity.cur_hp = max(0, entity.cur_hp - damage_amount)
            self.results.append({
                "type": "damage",
                "target": entity.name,
                "amount": damage_amount
            })
        elif path == 'heal_cur_hp':
            # Special case for 'heal'
            heal_amount = int(value)
            entity.cur_hp = min(entity.max_hp, entity.cur_hp + heal_amount)
            self.results.append({
                "type": "heal",
                "target": entity.name,
                "amount": heal_amount
            })
        elif path == 'cur_hp' or path == 'cur_fp' or path == 'cur_mp':
            value = int(value)
            max_val = 0
            
            if path == 'cur_hp':
                max_val = entity.max_hp
            elif path == 'cur_fp':
                max_val = entity.max_fp
            elif path == 'cur_mp':
                max_val = entity.max_mp

            current_val = getattr(entity, path, 0)
            
            if operation == 'add':
                setattr(entity, path, max(0, min(max_val, current_val + value)))
            elif operation == 'subtract':
                setattr(entity, path, max(0, min(max_val, current_val - value)))
            else: # set
                setattr(entity, path, max(0, min(max_val, value)))
        
        # TODO: Add logic for 'status.entity.add'
        # e.g., if path == 'status.entity.add': entity.status.append(value)
        
        else:
            print(f"Warning: _set_value does not yet support path: {path}")

    def _roll_dice(self, total_sum: int) -> int:
        """
        Rolls dice based on the "divide by 3, roll d6s, add remainder" rule.
        """
        if total_sum <= 0:
            return 0
            
        num_dice = total_sum // 3
        remainder = total_sum % 3
        
        roll_total = 0
        for _ in range(num_dice):
            roll_total += random.randint(1, 6)
            
        final_value = roll_total + remainder
        print(f"Roll: {num_dice}d6 ({roll_total}) + {remainder} (from sum {total_sum}) = {final_value}")
        return final_value

    def _evaluate_expression(self, expression: str) -> Any:
        """
        Evaluates a value string, resolving keywords and rolling dice.
        
        Examples:
        - "15" -> 15
        - "user:attribute.physique.strength" -> (resolves path)
        - "(user:attribute.intelligence.arcane/2).roll()" -> (resolves path)/2... then rolls
        """
        
        expression = str(expression) # Ensure it's a string
        
        # --- Handle dice rolling ---
        roll_suffix = ".roll()"
        is_roll = False
        if expression.endswith(roll_suffix):
            expression = expression[:-len(roll_suffix)]
            is_roll = True

        # --- Keyword/Path Replacement ---
        # Regex to find all instances of 'keyword:path'
        pattern = re.compile(r'(user|target|self):([a-zA-Z0-9_.]+)')
        
        def replacer(match):
            keyword = match.group(1)
            path = match.group(2)
            entity = self._get_entity(keyword)
            if not entity:
                return "0"
            value = self._resolve_path(entity, path)
            return str(value)
            
        resolved_str = pattern.sub(replacer, expression)
        
        # --- Evaluate the final expression ---
        try:
            # Saf-ish eval by removing builtins
            base_value = eval(resolved_str, {"__builtins__": {}}, {})
        except Exception as e:
            print(f"Error evaluating expression '{resolved_str}': {e}")
            base_value = 0
            
        if is_roll:
            # Pass the *sum* to the dice roller
            return self._roll_dice(int(base_value))
        else:
            return int(base_value)

    def _check_requirements(self, req_block: Any) -> bool:
        """
        Recursively checks if the user and target meet the requirements.
        
        Handles:
        - Simple key-value: 'user:attribute.physique: 3' (checks for >= 3)
        - 'or' blocks
        - 'and' blocks
        """
        if not req_block:
            return True # No requirements, always pass

        if isinstance(req_block, dict):
            # --- Handle 'or' logic ---
            if 'or' in req_block:
                for item in req_block['or']:
                    if self._check_requirements(item):
                        return True # One of the 'or' conditions passed
                self.narrative.append("You do not meet any of the required options.")
                return False # All 'or' conditions failed
            
            # --- Handle 'and' logic ---
            if 'and' in req_block:
                for item in req_block['and']:
                    if not self._check_requirements(item):
                        return False # One of the 'and' conditions failed
                return True # All 'and' conditions passed
            
            # --- Handle 'not' logic ---
            if 'not' in req_block:
                if self._check_requirements(req_block['not']):
                    self.narrative.append("You meet a condition you are not supposed to.")
                    return False # The 'not' condition was met, so fail
                return True # The 'not' condition was not met, so pass
                
            # --- Handle simple key-value checks ---
            # e.g., 'user:attribute.wisdom.miracle.conjuration: 3'
            for key, required_value in req_block.items():
                
                # Handle 'fail' blocks in actions, not here
                if key == 'fail' or key == 'pass':
                    continue

                # TODO: Handle complex 'check(user:status...)'
                if key.startswith('check('):
                    print(f"Warning: Skipping complex check requirement (not implemented): {key}")
                    continue
                    
                current_value = self._evaluate_expression(key)
                required_value = self._evaluate_expression(str(required_value))
                
                if current_value < required_value:
                    # Try to get a friendly name for the requirement
                    req_name = key.split(':')[-1].split('.')[-1]
                    self.narrative.append(
                        f"You do not meet the requirement: "
                        f"Need {req_name} of {required_value}, but you only have {current_value}."
                    )
                    return False
            
            return True # All key-value checks passed

        return True

    # --- MODIFIED FUNCTION ---
    def _apply_costs(self, cost_block: Cost) -> bool:
        """
        Applies the 'initial' costs to the user.
        Uses a two-pass system:
        1. Check if all resource costs can be paid.
        2. If so, deduct all resource costs.
        """
        # --- FIX 1: Check the object and its .initial attribute
        if not cost_block or not cost_block.initial:
            return True # No costs to apply

        resource_costs: List[Tuple[str, int]] = []
        
        # --- Pass 1: Check all costs ---
        # --- FIX 2: Iterate over cost_block.initial
        for cost_item in cost_block.initial:
            for key, value in cost_item.items():
                # We only care about resource costs (e.g., 'user:cur_fp: -3')
                # 'user:slot.hand: "points..."' is a narrative detail
                if 'cur_' in key:
                    cost_value = abs(int(value))
                    entity_key, path = key.split(':', 1)
                    entity = self._get_entity(entity_key)
                    
                    if not entity:
                        continue
                        
                    current_value = self._resolve_path(entity, path)
                    
                    if current_value < cost_value:
                        resource_name = path.split('.')[-1]
                        self.narrative.append(
                            f"You don't have enough {resource_name} "
                            f"(Need: {cost_value}, Have: {current_value})."
                        )
                        return False # Fail fast, can't pay
                    
                    resource_costs.append((key, int(value)))
        
        # --- Pass 2: Apply all costs ---
        for key, value in resource_costs:
            entity_key, path = key.split(':', 1)
            entity = self._get_entity(entity_key)
            self._set_value(entity, path, value, operation='add') # Add the negative value
            
        print(f"Applied costs: {resource_costs}")
        return True
    # --- END MODIFICATION ---

    def _run_proficiency_check(self, prof_block: Dict) -> bool:
        """
        Performs a proficiency check (skill roll).
        Returns True if the check passes or if no check is required.
        """
        if not prof_block:
            return True # No proficiency check required
            
        user_roll_str = ""
        diff_block = {}

        if 'choice' in prof_block:
            # --- Handle Choice Block ---
            # Find the best skill for the user to roll
            best_sum = -1
            best_roll_str = ""
            
            for roll_str in prof_block['choice'].keys():
                current_sum = self._evaluate_expression(roll_str.replace('.roll()', ''))
                if current_sum > best_sum:
                    best_sum = current_sum
                    best_roll_str = roll_str
            
            user_roll_str = best_roll_str
            diff_block = prof_block['choice'][user_roll_str]
            print(f"Proficiency: User chose best skill '{user_roll_str}' (Sum: {best_sum})")
            
        else:
            # --- Handle Single Roll Block ---
            for key, value in prof_block.items():
                if key.endswith('.roll()'):
                    user_roll_str = key
                    diff_block = value
                    break
        
        if not user_roll_str:
            print("Warning: Could not find a roll in proficiency block:", prof_block)
            return True # No valid check found

        # --- 1. Resolve User's Score ---
        user_sum_str = user_roll_str.replace('.roll()', '')
        user_base_sum = self._evaluate_expression(user_sum_str)
        user_final_score = self._roll_dice(user_base_sum)
        
        # --- 2. Resolve Difficulty Score ---
        difficulty_str = diff_block.get('difficulty', 0) # Default to 0 if missing
        difficulty_final_score = 0
        
        if 'target:opposed.roll()' in difficulty_str:
            # --- Handle Opposed Roll ---
            if not self.target:
                self.narrative.append("You attack, but there is no target to oppose you.")
                difficulty_final_score = 0 # No target, auto-hit?
            else:
                # Find the *attacker's* skill name (e.g., 'blade')
                attacker_skill_path_parts = user_sum_str.split('.')
                # Get the skill name (e.g., 'blade' from 'user:attribute.physique.blade.longsword')
                attacker_skill_name = attacker_skill_path_parts[2] if len(attacker_skill_path_parts) > 2 else 'brawling'
                
                # Get all skills that can defend against it
                valid_defense_skill_names = self.opposes_map.get(attacker_skill_name, [])
                
                best_defense_sum = -1
                best_defense_path = "nothing"
                
                # Check every attribute the defender has
                for attr_name, attr_obj in self.target.attribute.items():
                    # We only care about base attributes (e.g., 'physique'),
                    # not 'physique.strength'
                    if '.' in attr_name:
                        continue
                        
                    # This logic is for the old structure.
                    # New structure is flat, so we must iterate differently.
                    pass # See new logic below
                
                # --- New logic for flat attributes ---
                # Get all base attributes ('physique', 'dexterity', etc.)
                base_attributes = set(key.split('.')[0] for key in self.target.attribute.keys())
                
                for attr_name in base_attributes:
                    # Get all skills for this attribute (e.g., 'physique.blade', 'physique.strength')
                    skills_for_attr = [key for key in self.target.attribute.keys() if key.startswith(attr_name + '.') and len(key.split('.')) == 2]
                    
                    for skill_path in skills_for_attr:
                        skill_name = skill_path.split('.')[1] # e.g., 'blade'
                        
                        if skill_name in valid_defense_skill_names:
                            # This is a valid defense!
                            # Get the *full sum* for this skill, including specializations
                            # e.g., 'dexterity.dodge'
                            
                            # We must find the target's *best* specialization for this skill
                            best_spec_sum = -1
                            best_spec_path = skill_path
                            
                            spec_keys = [key for key in self.target.attribute.keys() if key.startswith(skill_path + '.') and len(key.split('.')) == 3]
                            
                            if not spec_keys:
                                # No specializations, just use the skill path
                                best_spec_sum = self._get_attribute_sum(self.target, skill_path)
                            else:
                                # Find the best specialization
                                for spec_path in spec_keys:
                                    spec_sum = self._get_attribute_sum(self.target, spec_path)
                                    if spec_sum > best_spec_sum:
                                        best_spec_sum = spec_sum
                                        best_spec_path = spec_path
                                        
                            # Now compare this skill's best sum to the overall best defense
                            if best_spec_sum > best_defense_sum:
                                best_defense_sum = best_spec_sum
                                best_defense_path = best_spec_path

                
                if best_defense_sum == -1:
                    # Target has no valid skills to oppose
                    self.narrative.append(f"{self.target.name} has no skill to defend!")
                    difficulty_final_score = 10 # Default static difficulty
                else:
                    # Roll the best defense
                    self.narrative.append(f"{self.target.name} defends with {best_defense_path} (Sum: {best_defense_sum})!")
                    difficulty_final_score = self._roll_dice(best_defense_sum)

        elif '.roll()' in difficulty_str:
            # --- Handle Standard Roll Difficulty ---
            diff_sum_str = difficulty_str.replace('.roll()', '')
            diff_base_sum = self._evaluate_expression(diff_sum_str)
            difficulty_final_score = self._roll_dice(diff_base_sum)
        
        else:
            # --- Handle Static Number Difficulty ---
            difficulty_final_score = self._evaluate_expression(difficulty_str)
            
        # --- 3. Compare and Return ---
        print(f"Proficiency Check: {user_roll_str} ({user_final_score}) vs Difficulty ({difficulty_final_score})")
        
        if user_final_score >= difficulty_final_score:
            pass_desc = diff_block.get('pass', {}).get('description')
            if pass_desc:
                self.narrative.append(pass_desc)
            return True
        else:
            fail_desc = diff_block.get('fail', {}).get('description')
            if fail_desc:
                self.narrative.append(fail_desc)
            else:
                self.narrative.append("You fail the check.") # Generic failure
            return False

    def _apply_effects(self, apply_block: Any):
        """
        Recursively applies the effects of the interaction (e.g., damage, status).
        """
        if not apply_block:
            return

        if isinstance(apply_block, dict):
            # Process a dictionary of effects
            # e.g., 'fire: { target:damage_cur_hp: ... }'
            for effect_type, effect_data in apply_block.items():
                if effect_type == 'choice':
                    # TODO: Handle 'choice' block (e.g., longsword)
                    # For now, just pick the first one
                    print("Warning: 'choice' in apply block not implemented. Picking first option.")
                    first_choice_key = list(effect_data.keys())[0]
                    self._apply_effects(effect_data[first_choice_key])
                
                elif effect_type == 'requirement':
                    # This is an 'apply' block with its own requirement
                    if self._check_requirements(effect_data):
                        # Requirements met, but what to apply?
                        # This assumes the *other* keys in this dict are the effects.
                        print("Warning: 'apply' block with 'requirement' is ambiguous. Applying sibling keys.")
                        temp_apply_block = apply_block.copy()
                        del temp_apply_block['requirement']
                        self._apply_effects(temp_apply_block)
                
                elif isinstance(effect_data, dict):
                    # e.g., 'target:damage_cur_hp: (user:physique.strength+6).roll()'
                    for key, value in effect_data.items():
                        entity_key, path = key.split(':', 1) # 'target', 'damage_cur_hp'
                        entity = self._get_entity(entity_key)
                        
                        final_value = self._evaluate_expression(str(value))
                        
                        self._set_value(entity, path, final_value)
                
        elif isinstance(apply_block, list):
            # Process a list of effects
            for item in apply_block:
                self._apply_effects(item)

    def process_interaction(self) -> Tuple[str, str]:
        """
        Runs the full interaction pipeline.
        
        Returns:
            A tuple of (narrative_message, history_message)
        """
        
        # --- 1. Check Requirements ---
        if not self._check_requirements(self.interaction.requirement):
            narrative = " ".join(self.narrative) or "You can't do that."
            history = f"{self.user.name} fails to use {self.interaction.name}."
            return (narrative, history)
            
        # --- 2. Apply Costs ---
        if not self._apply_costs(self.interaction.cost):
            narrative = " ".join(self.narrative) or "You can't afford the cost."
            history = f"{self.user.name} fails to pay for {self.interaction.name}."
            return (narrative, history)

        # --- 3. Run Proficiency Check ---
        passed_check = self._run_proficiency_check(self.interaction.proficiency)
        
        if not passed_check:
            narrative = " ".join(self.narrative) or "You failed the check."
            history = f"{self.user.name} fails to use {self.interaction.name} effectively."
            return (narrative, history)
            
        # --- 4. Apply Effects ---
        self._apply_effects(self.interaction.apply)
        
        # --- 5. Generate Narrative from Results ---
        # This is a simple narrative builder. You can make this much smarter.
        narrative = " ".join(self.narrative)
        history = ""
        
        if not self.results:
            narrative = narrative or f"You use {self.interaction.name}."
            history = f"{self.user.name} uses {self.interaction.name}."
            return (narrative, history)

        damage_done = 0
        heal_done = 0
        target_name = self.target.name if self.target else "something"
        
        for res in self.results:
            if res.get('type') == 'damage':
                damage_done += res['amount']
            if res.get('type') == 'heal':
                heal_done += res['amount']
        
        if damage_done > 0:
            narrative = (narrative or f"You strike {target_name} with your {self.interaction.name} for {damage_done} damage!").strip()
            history = f"{self.user.name} strikes {target_name} with {self.interaction.name} for {damage_done} damage."
        elif heal_done > 0:
            narrative = (narrative or f"You heal {target_name} with {self.interaction.name} for {heal_done} health!").strip()
            history = f"{self.user.name} heals {target_name} with {self.interaction.name} for {heal_done} health."
        else:
            narrative = (narrative or f"You use {self.interaction.name} on {target_name}.").strip()
            history = f"{self.user.name} uses {self.interaction.name} on {target_name}."

        return (narrative, history)