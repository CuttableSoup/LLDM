from __future__ import annotations
from typing import List, Dict, Any, Optional, Tuple

try:
    from classes import Entity, Skill, Attribute
    from nlp_processor import ProcessedInput, ActionComponent
except ImportError:
    print("Warning: 'classes.py' or 'nlp_processor.py' not found. Using placeholder classes.")
    class Entity:
        name: str = "Player"
        attribute: Dict = {}
    class ProcessedInput:
        raw_text: str = ""
        actions: List = []
        targets: List = []
    class ActionComponent: pass
    class Skill: pass
    class Attribute: pass

# --- NEW: Helper functions for accessing new data structure ---

def _get_skill(entity: Entity, skill_name: str) -> Optional[Skill]:
    """Helper to find a skill in the new attribute structure."""
    if not entity:
        return None
    for attr_obj in entity.attribute.values():
        if skill_name in attr_obj.skill:
            return attr_obj.skill[skill_name]
    return None

def _get_attribute_base(entity: Entity, attr_name: str) -> int:
    """Helper to get the base value of an attribute."""
    if not entity:
        return 0
    attr_obj = entity.attribute.get(attr_name)
    return attr_obj.base if attr_obj else 0

def _get_skill_base(entity: Entity, skill_name: str) -> int:
    """Helper to get the base value of a skill."""
    skill_obj = _get_skill(entity, skill_name)
    return skill_obj.base if skill_obj else 0

# ---

def process_player_actions(player: Entity, processed_input: ProcessedInput, game_entities: Dict[str, Entity]) -> List[Tuple[str, str]]:
    """
    Processes a list of actions from the player and returns narrative results.

    Each result is a (narrative_message, history_message) tuple.
    - narrative_message: What the player sees.
    - history_message: What is saved for the round summary/NPC context.
    """
    narrative_results: List[Tuple[str, str]] = []
    targets = processed_input.targets
    
    # If no specific actions were classified, treat it as a "fallback" action.
    if not processed_input.actions:
        narrative_msg = f"You say, \"{processed_input.raw_text}\""
        history_msg = f"{player.name} says: \"{processed_input.raw_text}\""
        narrative_results.append((narrative_msg, history_msg))
        return narrative_results

    # Handle multiple actions, e.g., "move and attack"
    for action in processed_input.actions:
        intent_name = action.intent.name
        # Use all targets found in the sentence for each action.
        # (A more advanced system might link targets to specific actions)
        target_entities = targets 
        target = target_entities[0] if target_entities else None
        target_name = target.name if target else "something"
        
        narrative_msg = ""
        history_msg = ""
        
        # This is the logic block moved from GUI.py
        if intent_name == "ATTACK":
            if target:
                # --- GAME LOGIC (Placeholder) ---
                # 1. Get player's weapon and its governing skill (e.g., 'blade')
                # 2. Get player's skill value: _get_skill_base(player, 'blade')
                # 3. Get player's attribute: _get_attribute_base(player, 'physique')
                # 4. Perform attack roll vs. target's defense.
                # 5. On hit, calculate damage from weapon's `apply` field.
                # 6. Apply damage: target.cur_hp -= damage
                narrative_msg = f"You attack {target_name}!"
                history_msg = f"{player.name} attacks {target_name}."
            else:
                narrative_msg = "You swing your weapon at the air."
                history_msg = f"{player.name} swings their weapon at the air."
            narrative_results.append((narrative_msg, history_msg))

        elif intent_name == "MOVE":
            if target:
                narrative_msg = f"You move towards {target_name}."
                history_msg = f"{player.name} moves towards {target_name}."
            else:
                narrative_msg = "You move to a new position."
                history_msg = f"{player.name} moves to a new position."
            narrative_results.append((narrative_msg, history_msg))

        elif intent_name == "INTERACT":
            if target:
                # Check if it's an "open" action or "talk" action
                if "open" in processed_input.raw_text.lower() and target.supertype == "object":
                    # --- GAME LOGIC (Placeholder) ---
                    # 1. Check if target is 'locked' in its `status` list.
                    # 2. If locked, print "It's locked."
                    # 3. If not, check if it's a container.
                    # 4. If so, display contents.
                    narrative_msg = f"You attempt to open {target_name}."
                    history_msg = f"{player.name} attempts to open {target_name}."
                else:
                    narrative_msg = f"You interact with {target_name}."
                    history_msg = f"{player.name} interacts with {target_name}."
            else:
                narrative_msg = "You look around."
                history_msg = f"{player.name} looks around."
            narrative_results.append((narrative_msg, history_msg))
            
        elif intent_name == "USE_SKILL":
            skill_name = action.skill_name
            target_str = f" on {target_name}" if target else ""
            
            if skill_name:
                # --- GAME LOGIC (Placeholder) ---
                # 1. Get player's skill value: _get_skill_base(player, skill_name)
                # 2. Get target's `proficiency` block for this skill (if any).
                # 3. Get the `difficulty` from that block.
                # 4. Perform a skill check (e.g., d20 + skill_value vs difficulty)
                # 5. Return pass/fail narrative.
                narrative_msg = f"You attempt to use your {skill_name} skill{target_str}."
                history_msg = f"{player.name} uses {skill_name}{target_str}."
                narrative_results.append((narrative_msg, history_msg))
            else:
                narrative_msg = "You try to... do something, but are unsure what skill to use."
                history_msg = f"{player.name} is unsure what skill to use."
                narrative_results.append((narrative_msg, history_msg))

        # --- NEW: Skeleton Handlers ---
        
        elif intent_name == "DIALOGUE":
            if target and "intelligent" in target.status:
                narrative_msg = f"You try to talk to {target_name}."
                history_msg = f"{player.name} talks to {target_name}."
            else:
                narrative_msg = "You speak to the air."
                history_msg = f"{player.name} speaks to the air."
            narrative_results.append((narrative_msg, history_msg))

        elif intent_name == "CAST":
            spell_name = "your magic" # Placeholder: need to find *which* spell
            # --- GAME LOGIC (Placeholder) ---
            # 1. NER should be trained to find spell names (e.g. "fireball").
            # 2. Find the spell in player.supernatural list.
            # 3. Check player `cur_mp` vs. spell `cost`.
            # 4. Apply spell `effects` from `apply` block.
            narrative_msg = f"You begin casting {spell_name} at {target_name}."
            history_msg = f"{player.name} casts {spell_name} at {target_name}."
            narrative_results.append((narrative_msg, history_msg))
            
        elif intent_name == "GIVE":
            # This is complex: "give the potion to Kael"
            # Needs to find *what* item is being given.
            narrative_msg = f"You attempt to give {target_name} an item."
            history_msg = f"{player.name} gives {target_name} an item."
            narrative_results.append((narrative_msg, history_msg))

        elif intent_name == "TRADE":
            narrative_msg = f"You attempt to trade with {target_name}."
            history_msg = f"{player.name} trades with {target_name}."
            narrative_results.append((narrative_msg, history_msg))

        elif intent_name == "CHANGE_STANCE":
            # Keyword ("crouch", "stand", "lie down") is in action.keyword
            stance = action.keyword
            narrative_msg = f"You {stance}."
            history_msg = f"{player.name} {stance}s."
            narrative_results.append((narrative_msg, history_msg))

        elif intent_name == "CRAFT":
            narrative_msg = "You open your crafting menu." # (Placeholder)
            history_msg = f"{player.name} attempts to craft."
            narrative_results.append((narrative_msg, history_msg))

        elif intent_name == "MEMORIZE":
            narrative_msg = "You study the object." # (Placeholder)
            history_msg = f"{player.name} studies {target_name}."
            narrative_results.append((narrative_msg, history_msg))

    # Fallback for intents that were found but not handled in the logic above
    if not narrative_results:
        narrative_msg = f"You ponder your actions: \"{processed_input.raw_text}\""
        history_msg = f"{player.name} ponders: \"{processed_input.raw_text}\""
        narrative_results.append((narrative_msg, history_msg))

    return narrative_results