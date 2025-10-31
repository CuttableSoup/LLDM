from __future__ import annotations
from typing import List, Dict, Any, Optional, Tuple

try:
    from classes import Entity
    from nlp_processor import ProcessedInput, ActionComponent
except ImportError:
    print("Warning: 'classes.py' or 'nlp_processor.py' not found. Using placeholder classes.")
    class Entity:
        name: str = "Player"
    class ProcessedInput: pass
    class ActionComponent: pass

def process_player_actions(player: Entity, processed_input: ProcessedInput) -> List[Tuple[str, str]]:
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
        
        narrative_msg = ""
        history_msg = ""
        
        # This is the logic block moved from GUI.py
        if intent_name == "ATTACK":
            if target_entities:
                target = target_entities[0] # Simple: just attack the first target
                narrative_msg = f"You attack {target.name}!"
                history_msg = f"{player.name} attacks {target.name}."
            else:
                narrative_msg = "You swing your weapon at the air."
                history_msg = f"{player.name} swings their weapon at the air."
            narrative_results.append((narrative_msg, history_msg))

        elif intent_name == "MOVE":
            if target_entities:
                target = target_entities[0]
                narrative_msg = f"You move towards {target.name}."
                history_msg = f"{player.name} moves towards {target.name}."
            else:
                narrative_msg = "You move to a new position."
                history_msg = f"{player.name} moves to a new position."
            narrative_results.append((narrative_msg, history_msg))

        elif intent_name == "INTERACT":
            if target_entities:
                target = target_entities[0]
                # Check if it's an "open" action or "talk" action
                if "open" in processed_input.raw_text.lower() and target.supertype == "object":
                    narrative_msg = f"You attempt to open {target.name}."
                    history_msg = f"{player.name} attempts to open {target.name}."
                else:
                    narrative_msg = f"You interact with {target.name}."
                    history_msg = f"{player.name} interacts with {target.name}."
            else:
                narrative_msg = "You look around."
                history_msg = f"{player.name} looks around."
            narrative_results.append((narrative_msg, history_msg))
            
        elif intent_name == "USE_SKILL":
            skill_name = action.skill_name
            target = target_entities[0] if target_entities else None
            
            if skill_name:
                # You now have everything you need to make a skill check!
                # (Placeholder) self.process_skill_check(self.player_entity, skill_name, target)
                
                target_name = f" on {target.name}" if target else ""
                narrative_msg = f"You attempt to use your {skill_name} skill{target_name}."
                history_msg = f"{player.name} uses {skill_name}{target_name}."
                narrative_results.append((narrative_msg, history_msg))
            else:
                narrative_msg = "You try to... do something, but are unsure what skill to use."
                history_msg = f"{player.name} is unsure what skill to use."
                narrative_results.append((narrative_msg, history_msg))

    # Fallback for intents that were found but not handled in the logic above
    if not narrative_results:
        narrative_msg = f"You ponder your actions: \"{processed_input.raw_text}\""
        history_msg = f"{player.name} ponders: \"{processed_input.raw_text}\""
        narrative_results.append((narrative_msg, history_msg))

    return narrative_results