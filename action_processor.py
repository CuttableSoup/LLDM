"""
This module is responsible for processing player actions and generating narrative results.

It takes processed input from the NLP module and the current game state (player and entities)
and determines the outcome of the player's intended actions. It then returns a list of
narrative and history messages to be displayed to the player.
"""
from __future__ import annotations
from typing import List, Dict, Optional, Tuple, Any
import re
from interaction_system import InteractionProcessor

try:
    # Import necessary classes from other modules.
    from classes import Entity, Skill, InventoryItem
    from nlp_processor import ProcessedInput
except ImportError:
    # Provide placeholder classes if the primary modules are not found.
    # This allows for testing this module in isolation.
    print("Warning: 'classes.py' or 'nlp_processor.py' not found. Using placeholder classes.")
    class Entity:
        name: str = "Player"
        attribute: Dict = {}
        inventory: List = []
        supernatural: List[str] = []
        move: Dict[str, int] = {} # --- NEW ---
        passable: Dict[str, int] = {} # --- NEW ---
        requirement: Dict = {} # --- NEW ---
    class ProcessedInput:
        raw_text: str = ""
        actions: List = []
        targets: List = []
        interaction_entities: List = [] # Added for placeholder
    class ActionComponent: pass
    class Skill: pass
    class Attribute: pass
    class InventoryItem: pass

# --- NEW HELPER FUNCTIONS ---

def _create_mock_fist() -> Entity:
    """Creates a temporary entity for an unarmed strike."""
    fist_data = {
        'name': 'fist',
        'apply': {
            'bludgeoning': {
                'target:damage_cur_hp': 'user:attribute.physique.brawling+1'
            }
        },
        'proficiency': {
            'user:attribute.physique.brawling.roll()': {
                'difficulty': 'target:opposed.roll()'
            }
        },
        'requirement': {},
        'cost': {}
    }
    # This is a simplification; your create_entity_from_dict would be used here
    # We will just manually create a minimal Entity object
    fist = Entity(name="fist")
    fist.apply = fist_data['apply']
    fist.proficiency = fist_data['proficiency']
    fist.requirement = fist_data['requirement']
    try:
        from classes import Cost
        fist.cost = Cost()
    except ImportError:
        fist.cost = {}
        
    return fist


# --- ORIGINAL HELPER FUNCTIONS (UNUSED, BUT KEPT FOR REFERENCE) ---

def _get_skill(entity: Entity, skill_name: str) -> Optional[Skill]:
    """
    Retrieves a specific skill object from an entity.
    NOTE: This is the old way. The new way uses _get_attribute_sum
    in InteractionProcessor.
    """
    if not entity:
        return None
    # This logic is deprecated by the flat attribute structure
    return None

def _get_attribute_base(entity: Entity, attr_name: str) -> int:
    """
    Gets the base value of a specific attribute for an entity.
    NOTE: This is the old way. The new way uses _get_attribute_sum
    in InteractionProcessor.
    """
    if not entity:
        return 0
    attr_obj = entity.attribute.get(attr_name)
    return attr_obj.base if attr_obj else 0

def _get_skill_base(entity: Entity, skill_name: str) -> int:
    """
    Gets the base value of a specific skill for an entity.
    NOTE: This is the old way. The new way uses _get_attribute_sum
    in InteractionProcessor.
    """
    # This logic is deprecated
    return 0


# --- MAIN FUNCTION (HEAVILY MODIFIED) ---

def process_player_actions(player: Entity, processed_input: ProcessedInput, 
                           game_entities: Dict[str, Entity], 
                           loader_attributes: List[Any]) -> List[Tuple[str, str]]:
    """
    Processes the player's actions and generates narrative and history messages.
    Now uses InteractionProcessor to handle complex actions.
    
    Args:
        player: The player entity.
        processed_input: The processed input from the NLP module.
        game_entities: A dictionary of all entities in the game.
        loader_attributes: The raw data from attributes.yaml for parsing rules.
    """
    narrative_results: List[Tuple[str, str]] = []
    targets = processed_input.targets
    
    # If no specific actions are identified, treat it as speech.
    if not processed_input.actions:
        narrative_msg = f"You say, \"{processed_input.raw_text}\""
        history_msg = f"{player.name} says: \"{processed_input.raw_text}\""
        narrative_results.append((narrative_msg, history_msg))
        return narrative_results

    # Iterate through each identified action.
    for action in processed_input.actions:
        intent_name = action.intent.name
        
        # Get the primary target, if one exists
        target_entities = targets 
        target = target_entities[0] if target_entities else None
        target_name = target.name if target else "something"
        
        narrative_msg = ""
        history_msg = ""
        
        # --- MODIFIED: Handle the "ATTACK" intent ---
        if intent_name == "ATTACK":
            
            # 1. Find equipped weapon
            weapon_entity = None
            for item_ref in player.inventory:
                 if item_ref.equipped:
                     # Get the full entity from the master list
                     weapon_entity = game_entities.get(item_ref.item)
                     if weapon_entity and weapon_entity.supertype == 'object':
                         break # Found an equipped weapon
            
            if not weapon_entity:
                weapon_entity = _create_mock_fist() # Default to unarmed strike
            
            if target:
                # 2. Use InteractionProcessor
                print(f"Processing ATTACK with {weapon_entity.name} on {target_name}")
                processor = InteractionProcessor(player, target, weapon_entity, loader_attributes)
                narrative_msg, history_msg = processor.process_interaction()
                
            else:
                narrative_msg = "You swing your weapon at the air."
                history_msg = f"{player.name} swings their weapon at the air."
                
            narrative_results.append((narrative_msg, history_msg))

        # --- MODIFIED: Handle the "MOVE" intent ---
        elif intent_name == "MOVE":
            if not target:
                narrative_msg = "Move where?"
                history_msg = f"{player.name} wonders where to move."
                narrative_results.append((narrative_msg, history_msg))
                continue # Skip to the next action

            # 1. Check Requirements of the destination
            # We use the InteractionProcessor to check the target's 'requirement' block.
            # Context: User=player, Target=target(tile), Interaction=target(tile)
            # This allows 'user:attribute...' and 'target:attribute...' to resolve correctly.
            processor = InteractionProcessor(player, target, target, loader_attributes)
            
            # We call the private _check_requirements method directly to avoid
            # the side effects of process_interaction() (like costs/apply).
            if not processor._check_requirements(target.requirement):
                narrative_msg = " ".join(processor.narrative) or f"You cannot move to {target_name}."
                history_msg = f"{player.name} fails to move to {target_name}."
                narrative_results.append((narrative_msg, history_msg))
                continue
                
            # 2. Check Movement Cost vs. Speed
            # For now, we assume "land" movement. This can be expanded later.
            player_move_points = player.move.get("land", 0)
            
            # Default to 0 (impassable) if not specified, as per your request.
            move_cost = target.passable.get("land", 0) 
            
            if move_cost == 0:
                narrative_msg = f"You cannot pass through {target_name}."
                history_msg = f"{player.name} cannot pass through {target_name}."
            elif player_move_points < move_cost:
                narrative_msg = f"You don't have enough movement to move to {target_name} (Need: {move_cost}, Have: {player_move_points})."
                history_msg = f"{player.name} doesn't have enough movement to reach {target_name}."
            else:
                # Success!
                # TODO: Deduct movement points from the player for the turn.
                narrative_msg = f"You move to {target_name}."
                history_msg = f"{player.name} moves to {target_name}."

            narrative_results.append((narrative_msg, history_msg))

        # --- Handle the "INTERACT" intent (Unchanged) ---
        elif intent_name == "INTERACT":
            if target:
                # Simple "open" logic for containers
                if "open" in processed_input.raw_text.lower() and target.type == "container":
                    narrative_msg = f"You attempt to open {target_name}."
                    history_msg = f"{player.name} attempts to open {target_name}."
                    # TODO: This could also use InteractionProcessor
                    # if the "chest" entity has an 'open' proficiency/requirement
                else:
                    narrative_msg = f"You interact with {target_name}."
                    history_msg = f"{player.name} interacts with {target_name}."
            else:
                narrative_msg = "You look around."
                history_msg = f"{player.name} looks around."
            narrative_results.append((narrative_msg, history_msg))
            
        # --- Handle the "USE_SKILL" intent (Unchanged) ---
        elif intent_name == "USE_SKILL":
            # This is for generic skill checks, like "pick lock"
            # TODO: This could also be routed to InteractionProcessor
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

        # --- Handle the "DIALOGUE" intent (Unchanged) ---
        elif intent_name == "DIALOGUE":
            if target and "intelligent" in target.status:
                narrative_msg = f"You try to talk to {target_name}."
                history_msg = f"{player.name} talks to {target_name}."
            else:
                narrative_msg = "You speak to the air."
                history_msg = f"{player.name} speaks to the air."
            narrative_results.append((narrative_msg, history_msg))

        # --- MODIFIED: Handle the "CAST" intent ---
        elif intent_name == "CAST":
            # --- NEW LOGIC ---
            # The NLP processor already found the spell entity for us.
            if not processed_input.interaction_entities:
                narrative_msg = "You try to cast a spell, but aren't sure which one."
                history_msg = f"{player.name} fumbles a spell."
                narrative_results.append((narrative_msg, history_msg))
                continue
            
            # Just grab the first spell mentioned.
            spell_entity = processed_input.interaction_entities[0]
            
            if spell_entity:
                print(f"Processing CAST with {spell_entity.name} on {target_name}")
                processor = InteractionProcessor(player, target, spell_entity, loader_attributes)
                narrative_msg, history_msg = processor.process_interaction()
            # --- END NEW LOGIC ---
            else:
                # This case should not be hit if the logic above is correct
                narrative_msg = f"You try to cast a spell, but something went wrong."
                history_msg = f"{player.name} fumbles a spell."
                
            narrative_results.append((narrative_msg, history_msg))
            
        # --- Handle the "GIVE" intent ---
        elif intent_name == "GIVE":
            narrative_msg = f"You attempt to give {target_name} an item."
            history_msg = f"{player.name} gives {target_name} an item."
            narrative_results.append((narrative_msg, history_msg))

        # --- Handle the "TRADE" intent ---
        elif intent_name == "TRADE":
            narrative_msg = f"You attempt to trade with {target_name}."
            history_msg = f"{player.name} trades with {target_name}."
            narrative_results.append((narrative_msg, history_msg))

        # --- Handle the "CHANGE_STANCE" intent ---
        elif intent_name == "CHANGE_STANCE":
            stance = action.keyword
            narrative_msg = f"You {stance}."
            history_msg = f"{player.name} {stance}s."
            narrative_results.append((narrative_msg, history_msg))

        # --- Handle the "CRAFT" intent ---
        elif intent_name == "CRAFT":
            narrative_msg = "You open your crafting menu."
            history_msg = f"{player.name} attempts to craft."
            narrative_results.append((narrative_msg, history_msg))

        # --- Handle the "MEMORIZE" intent ---
        elif intent_name == "MEMORIZE":
            narrative_msg = "You study the object."
            history_msg = f"{player.name} studies {target_name}."
            narrative_results.append((narrative_msg, history_msg))

    # If no narrative results were generated, create a default pondering message.
    if not narrative_results:
        narrative_msg = f"You ponder your actions: \"{processed_input.raw_text}\""
        history_msg = f"{player.name} ponders: \"{processed_input.raw_text}\""
        narrative_results.append((narrative_msg, history_msg))

    return narrative_results