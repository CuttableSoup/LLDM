from __future__ import annotations
from typing import List, Dict, Optional, Tuple

try:
    from classes import Entity, Skill
    from nlp_processor import ProcessedInput
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

def _get_skill(entity: Entity, skill_name: str) -> Optional[Skill]:
    if not entity:
        return None
    for attr_obj in entity.attribute.values():
        if skill_name in attr_obj.skill:
            return attr_obj.skill[skill_name]
    return None

def _get_attribute_base(entity: Entity, attr_name: str) -> int:
    if not entity:
        return 0
    attr_obj = entity.attribute.get(attr_name)
    return attr_obj.base if attr_obj else 0

def _get_skill_base(entity: Entity, skill_name: str) -> int:
    skill_obj = _get_skill(entity, skill_name)
    return skill_obj.base if skill_obj else 0

def process_player_actions(player: Entity, processed_input: ProcessedInput, game_entities: Dict[str, Entity]) -> List[Tuple[str, str]]:
    narrative_results: List[Tuple[str, str]] = []
    targets = processed_input.targets
    
    if not processed_input.actions:
        narrative_msg = f"You say, \"{processed_input.raw_text}\""
        history_msg = f"{player.name} says: \"{processed_input.raw_text}\""
        narrative_results.append((narrative_msg, history_msg))
        return narrative_results

    for action in processed_input.actions:
        intent_name = action.intent.name
        target_entities = targets 
        target = target_entities[0] if target_entities else None
        target_name = target.name if target else "something"
        
        narrative_msg = ""
        history_msg = ""
        
        if intent_name == "ATTACK":
            if target:
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
                if "open" in processed_input.raw_text.lower() and target.supertype == "object":
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
                narrative_msg = f"You attempt to use your {skill_name} skill{target_str}."
                history_msg = f"{player.name} uses {skill_name}{target_str}."
                narrative_results.append((narrative_msg, history_msg))
            else:
                narrative_msg = "You try to... do something, but are unsure what skill to use."
                history_msg = f"{player.name} is unsure what skill to use."
                narrative_results.append((narrative_msg, history_msg))

        
        elif intent_name == "DIALOGUE":
            if target and "intelligent" in target.status:
                narrative_msg = f"You try to talk to {target_name}."
                history_msg = f"{player.name} talks to {target_name}."
            else:
                narrative_msg = "You speak to the air."
                history_msg = f"{player.name} speaks to the air."
            narrative_results.append((narrative_msg, history_msg))

        elif intent_name == "CAST":
            spell_name = "your magic"
            narrative_msg = f"You begin casting {spell_name} at {target_name}."
            history_msg = f"{player.name} casts {spell_name} at {target_name}."
            narrative_results.append((narrative_msg, history_msg))
            
        elif intent_name == "GIVE":
            narrative_msg = f"You attempt to give {target_name} an item."
            history_msg = f"{player.name} gives {target_name} an item."
            narrative_results.append((narrative_msg, history_msg))

        elif intent_name == "TRADE":
            narrative_msg = f"You attempt to trade with {target_name}."
            history_msg = f"{player.name} trades with {target_name}."
            narrative_results.append((narrative_msg, history_msg))

        elif intent_name == "CHANGE_STANCE":
            stance = action.keyword
            narrative_msg = f"You {stance}."
            history_msg = f"{player.name} {stance}s."
            narrative_results.append((narrative_msg, history_msg))

        elif intent_name == "CRAFT":
            narrative_msg = "You open your crafting menu."
            history_msg = f"{player.name} attempts to craft."
            narrative_results.append((narrative_msg, history_msg))

        elif intent_name == "MEMORIZE":
            narrative_msg = "You study the object."
            history_msg = f"{player.name} studies {target_name}."
            narrative_results.append((narrative_msg, history_msg))

    if not narrative_results:
        narrative_msg = f"You ponder your actions: \"{processed_input.raw_text}\""
        history_msg = f"{player.name} ponders: \"{processed_input.raw_text}\""
        narrative_results.append((narrative_msg, history_msg))

    return narrative_results
