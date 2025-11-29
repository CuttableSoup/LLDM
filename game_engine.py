"""
game_engine.py

This module contains the GameController class, which acts as the central engine
for the game loop, coordinating between player input, NPC logic, and the GUI.
"""
from __future__ import annotations
import logging
from typing import List, Dict, Any, Optional, Callable, Tuple
from pathlib import Path

from models import Entity, Room, GameTime, EntityHistory, HistoryEvent
from loader import RulesetLoader
from interaction_manager import InteractionManager

# Placeholder imports for dependencies that might be missing in a basic run
try:
    from nlp_processor import NLPProcessor, ProcessedInput
    from llm_manager import LLMManager
    from prompts import prompts
except ImportError as e:
    logging.getLogger("GameEngine").error(f"Failed to import required modules: {e}")
    class NLPProcessor:
        def __init__(self, *args): pass
        def process_player_input(self, *args): return None
    class ProcessedInput: pass
    class LLMManager: pass
    prompts = {}

logger = logging.getLogger("GameEngine")

class GameController:
    def __init__(self, loader: RulesetLoader, ruleset_path: Path, llm_manager: LLMManager):
        self.loader = loader
        self.nlp_processor = NLPProcessor(ruleset_path)
        self.llm_manager = llm_manager
        self.interaction_manager = InteractionManager()
        
        self.player_entity: Optional[Entity] = None
        self.game_time = GameTime()
        self.game_time.set_time(year=2000, month=1, day=1, hour=8)
        self.game_entities: Dict[str, Entity] = {}
        self.entity_histories: Dict[str, EntityHistory] = {}
        self.current_room: Optional[Room] = None
        
        # Load all entities from the loader
        self.game_entities.update(self.loader.characters)
        for entity_dict in self.loader.entities_by_supertype.values():
            self.game_entities.update(entity_dict)

        # Initialize histories for intelligent entities
        for name, entity in self.game_entities.items():
            if any(status in entity.status for status in ["intelligent", "basic"]):
                self.entity_histories[name] = EntityHistory(entity_name=name)

        self.initiative_order: List[Entity] = []
        self.round_history: List[str] = []
        self.llm_chat_history: List[Dict[str, str]] = []
        
        # UI Callbacks (to be assigned by GUI)
        self.update_narrative_callback: Callable[[str], None] = lambda text: None
        self.update_character_sheet_callback: Callable[[Entity], None] = lambda entity: None
        self.update_inventory_callback: Callable[[Entity], None] = lambda entity: None
        self.update_map_callback: Callable[[Optional[Room]], None] = lambda room: None

        # Initialize Log
        self.log_file = Path("game_log.txt")
        self._log_to_file(f"\n\n--- Session Started: {self.game_time.get_time_string()} ---\n")

    def _log_to_file(self, text: str):
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception as e:
            logger.error(f"Failed to write to log file: {e}")

    def update_narrative(self, text: str):
        self._log_to_file(text)
        self.update_narrative_callback(text)

    def start_game(self, player: Entity):
        self.player_entity = player
        if player.name not in self.game_entities:
            self.game_entities[player.name] = player
            
        if any(status in player.status for status in ["intelligent", "basic"]):
            if player.name not in self.entity_histories:
                self.entity_histories[player.name] = EntityHistory(entity_name=player.name)
        
        # Load initial room
        if self.loader.scenario and self.loader.scenario.environment.rooms:
            self.current_room = self.loader.scenario.environment.rooms[0]
        
        self.initiative_order = []
        
        # Populate initiative based on room contents
        if self.current_room and self.current_room.layers:
            legend_lookup = {item.char: item.entity for item in self.current_room.legend}
            placed_chars = set()
            for layer in self.current_room.layers:
                for r_idx, row in enumerate(layer):
                    for c_idx, char_code in enumerate(row):
                        if char_code != 'x': 
                            placed_chars.add(char_code)
                            # Initialize position for entity
                            entity_name = legend_lookup.get(char_code)
                            entity_obj = self.game_entities.get(entity_name)
                            if entity_obj:
                                entity_obj.x = c_idx
                                entity_obj.y = r_idx
            
            for char_code in placed_chars:
                entity_name = legend_lookup.get(char_code)
                entity_obj = self.game_entities.get(entity_name)
                if entity_obj and entity_obj not in self.initiative_order:
                     # Only add intelligent/basic creatures to initiative
                     if any(s in entity_obj.status for s in ["intelligent", "basic"]):
                        self.initiative_order.append(entity_obj)
        else:
             if self.player_entity: 
                 self.initiative_order = [self.player_entity]

        if self.player_entity and self.player_entity not in self.initiative_order:
            self.initiative_order.append(self.player_entity)
            
        start_event = HistoryEvent(
            timestamp=self.game_time.copy(),
            event_type="world",
            description="The adventure begins.",
            participants=[e.name for e in self.initiative_order]
        )
        for history in self.entity_histories.values(): 
            history.add_event(start_event)
        
        self.update_narrative(f"[{self.game_time.get_time_string()}] The adventure begins for {player.name}...")
        self.update_character_sheet_callback(self.player_entity)
        self.update_inventory_callback(self.player_entity)
        self.update_map_callback(self.current_room)

    def process_player_input(self, player_input: str):
        if not self.player_entity or not self.nlp_processor: 
            return
        
        self._log_to_file(f"> {player_input}")

        processed_action = self.nlp_processor.process_player_input(player_input, self.game_entities)
        if not processed_action:
            self.update_narrative("Error: Could not process input.")
            return
        
        action_results = self.process_player_actions_logic(
            self.player_entity,
            processed_action,
            self.game_entities
        )

        player_action_summary = ""
        for narrative_msg, history_msg in action_results:
            self.update_narrative(narrative_msg)
            self.round_history.append(history_msg)
            player_action_summary += history_msg + " "

        player_event = HistoryEvent(
            timestamp=self.game_time.copy(),
            event_type="player_action",
            description=player_action_summary.strip(),
            participants=[t.name for t in processed_action.targets]
        )
        
        for target_entity in processed_action.targets:
            if target_entity.name in self.entity_histories:
                self.entity_histories[target_entity.name].add_event(player_event)
        if self.player_entity.name in self.entity_histories:
            self.entity_histories[self.player_entity.name].add_event(player_event)

        self.llm_chat_history.append({"role": "user", "content": player_action_summary.strip()})
        
        for target in processed_action.targets: 
            self.update_character_sheet_callback(target)
        self.update_character_sheet_callback(self.player_entity)
        self.update_inventory_callback(self.player_entity)
        
        self._run_npc_turns(player_action_summary)

    def _run_npc_turns(self, player_action_summary: str):
        if not self.player_entity: 
            return
        all_actions_taken = False
        game_state_context = self._get_current_game_state(self.player_entity)
        
        for npc in self.initiative_order:
            if npc == self.player_entity: 
                continue 
            if not any(s in npc.status for s in ["intelligent", "basic"]): 
                continue
            
            npc_history_summary = ""
            if npc.name in self.entity_histories:
                npc_history_summary = self.entity_histories[npc.name].get_summary_for_llm()
            
            npc_prompt = prompts['npc_action'].format(
                npc_name=npc.name,
                npc_history=npc_history_summary,
                actors_present=game_state_context['actors_present'],
                player_name=self.player_entity.name,
                player_action=player_action_summary
            )
            
            reaction_narrative = self.llm_manager.generate_response(prompt=npc_prompt, history=self.llm_chat_history)
            
            if reaction_narrative and not reaction_narrative.startswith("Error:"):
                dialogue_event = HistoryEvent(
                    timestamp=self.game_time.copy(),
                    event_type="dialogue_self",
                    description=f"You said: \"{reaction_narrative}\"",
                    participants=[self.player_entity.name]
                )
                if npc.name in self.entity_histories:
                    self.entity_histories[npc.name].add_event(dialogue_event)
                if self.player_entity.name in self.entity_histories:
                    player_event = HistoryEvent(
                        timestamp=self.game_time.copy(),
                        event_type="dialogue_npc",
                        description=f"{npc.name} said: \"{reaction_narrative}\"",
                        participants=[npc.name]
                    )
                    self.entity_histories[self.player_entity.name].add_event(player_event)
            
            if reaction_narrative:
                fmt_narrative = reaction_narrative if reaction_narrative.startswith("Error:") else f"{npc.name}: \"{reaction_narrative}\""
                self.update_narrative(fmt_narrative)
                self.round_history.append(fmt_narrative)
                self.llm_chat_history.append({"role": "assistant", "content": reaction_narrative})
                all_actions_taken = True
        
        if all_actions_taken:
            narrator_prompt = prompts['narrator_summary'].format(action_log="\n".join(self.round_history))
            summary = self.llm_manager.generate_response(prompt=narrator_prompt, history=self.llm_chat_history)
            self.update_narrative(f"\n--- {summary} ---")
            self.llm_chat_history.append({"role": "assistant", "content": summary})
            self.llm_chat_history.append({"role": "assistant", "content": summary})
            self.round_history = []
            
            self.advance_round()

    def advance_round(self):
        """Advances the game time by one round (6 seconds) and processes triggers."""
        self.game_time.advance_time(6)
        
        # Process triggers for all entities in initiative
        # (and maybe others in the room? For now, just initiative)
        for entity in self.initiative_order:
            msgs = self.interaction_manager.process_triggers(entity, self.game_time)
            for msg in msgs:
                self.update_narrative(msg)
                self.round_history.append(msg)

    def _get_current_game_state(self, actor: Entity) -> Dict[str, Any]:
        actors_in_room = [e.name for e in self.initiative_order if e.name != actor.name]
        return {
            "actors_present": ", ".join(actors_in_room) if actors_in_room else "none",
            "objects_present": "none",
            "attitudes": "none",
            "game_history": "\n".join(self.round_history)
        }

    def answer_player_question(self, question: str):
        if not self.player_entity: 
            return
        game_state = self._get_current_game_state(self.player_entity)
        prompt = prompts['adam_assistant'].format(question=question, game_state=game_state)
        answer = self.llm_manager.generate_response(prompt=prompt, history=self.llm_chat_history)
        self.update_narrative(f"\n--- ADaM: {answer} ---")

    def move_entity(self, entity: Entity, target_x: int, target_y: int) -> bool:
        """Moves an entity to a new position on the map."""
        if not self.current_room: return False
        
        # Check bounds (assuming square map for now or check layers)
        # Check collision (is 'x' or another entity there?)
        # For now, just update coords and assume valid for the demo
        
        # Update Map Layers (Visuals)
        # Find old char
        old_char = None
        for item in self.current_room.legend:
            if item.entity == entity.name:
                old_char = item.char
                break
        
        if not old_char: return False # Should not happen if entity is on map

        # Clear old pos
        # We need to know which layer the entity is on. 
        # For simplicity, we scan all layers and move the char.
        moved = False
        for layer in self.current_room.layers:
            # Check if target is valid in this layer (bounds)
            if target_y < len(layer) and target_x < len(layer[target_y]):
                 # Check if target is passable (not wall 'W')
                 # This is a very basic check.
                 target_code = layer[target_y][target_x]
                 if target_code == 'W': # Hardcoded wall check for now
                     return False
                 
                 # Clear old
                 if hasattr(entity, 'y') and hasattr(entity, 'x'):
                     if entity.y < len(layer) and entity.x < len(layer[entity.y]):
                         if layer[entity.y][entity.x] == old_char:
                             layer[entity.y][entity.x] = 'x' # Restore floor?
                 
                 # Set new
                 layer[target_y][target_x] = old_char
                 moved = True
        
        if moved:
            entity.x = target_x
            entity.y = target_y
            self.update_map_callback(self.current_room)
            return True
        
        return False

    def process_player_actions_logic(self, player: Entity, processed_input: ProcessedInput, game_entities: Dict[str, Entity]) -> List[Tuple[str, str]]:
        """
        Processes actions using the InteractionManager.
        """
        results = []
        
        for action in processed_input.actions:
            # Handle MOVE intent
            if action.intent.name == "MOVE":
                if not processed_input.targets:
                    results.append(("Move where?", "Player tried to move but didn't specify where."))
                    continue
                
                target = processed_input.targets[0]
                
                # Simple "move to adjacent" logic
                # 1. Get target position
                if not hasattr(target, 'x') or not hasattr(target, 'y'):
                     # If target isn't on map (maybe abstract?), we can't move to it physically
                     results.append((f"You can't see {target.name} here.", f"Player tried to move to {target.name} but it's not on the map."))
                     continue
                
                # 2. Calculate adjacent square
                # Simple approach: Move to same Y, X-1 (left) or X+1 (right) depending on relative pos
                dx = target.x - player.x
                dy = target.y - player.y
                
                new_x, new_y = player.x, player.y
                
                if abs(dx) > abs(dy):
                    # Move horizontally
                    new_x = player.x + (1 if dx > 0 else -1)
                    # Don't overlap target
                    if new_x == target.x and new_y == target.y:
                         new_x = player.x # Stay put if adjacent? Or stop 1 short.
                else:
                    # Move vertically
                    new_y = player.y + (1 if dy > 0 else -1)
                    if new_x == target.x and new_y == target.y:
                         new_y = player.y

                # Check if we are already adjacent
                if abs(player.x - target.x) <= 1 and abs(player.y - target.y) <= 1:
                     results.append((f"You are already close to {target.name}.", f"Player is already near {target.name}."))
                     continue

                # Execute Move
                if self.move_entity(player, new_x, new_y):
                    results.append((f"You move towards {target.name}.", f"{player.name} moved to ({new_x}, {new_y})."))
                else:
                    results.append(("Something blocks your way.", f"{player.name} failed to move."))
                
                continue

            # Find the interaction object in the player's list
            interaction_obj = None
            
            # 1. Search in interactions (active uses)
            for inter in player.interaction:
                if inter.type.lower() == action.keyword.lower():
                    interaction_obj = inter
                    break
            
            # 2. Search in abilities (inherent capabilities)
            if not interaction_obj:
                for abil in player.ability:
                    if abil.type.lower() == action.keyword.lower():
                        interaction_obj = abil
                        break
            
            # Fallback for basic attacks if not explicitly defined but requested?
            
            npc_prompt = prompts['npc_action'].format(
                npc_name=npc.name,
                npc_history=npc_history_summary,
                actors_present=game_state_context['actors_present'],
                player_name=self.player_entity.name,
                player_action=player_action_summary
            )
            
            reaction_narrative = self.llm_manager.generate_response(prompt=npc_prompt, history=self.llm_chat_history)
            
            if reaction_narrative and not reaction_narrative.startswith("Error:"):
                dialogue_event = HistoryEvent(
                    timestamp=self.game_time.copy(),
                    event_type="dialogue_self",
                    description=f"You said: \"{reaction_narrative}\"",
                    participants=[self.player_entity.name]
                )
                if npc.name in self.entity_histories:
                    self.entity_histories[npc.name].add_event(dialogue_event)
                if self.player_entity.name in self.entity_histories:
                    player_event = HistoryEvent(
                        timestamp=self.game_time.copy(),
                        event_type="dialogue_npc",
                        description=f"{npc.name} said: \"{reaction_narrative}\"",
                        participants=[npc.name]
                    )
                    self.entity_histories[self.player_entity.name].add_event(player_event)
            
            if reaction_narrative:
                fmt_narrative = reaction_narrative if reaction_narrative.startswith("Error:") else f"{npc.name}: \"{reaction_narrative}\""
                self.update_narrative(fmt_narrative)
                self.round_history.append(fmt_narrative)
                self.llm_chat_history.append({"role": "assistant", "content": reaction_narrative})
                all_actions_taken = True
        
        if all_actions_taken:
            narrator_prompt = prompts['narrator_summary'].format(action_log="\n".join(self.round_history))
            summary = self.llm_manager.generate_response(prompt=narrator_prompt, history=self.llm_chat_history)
            self.update_narrative(f"\n--- {summary} ---")
            self.llm_chat_history.append({"role": "assistant", "content": summary})
            self.llm_chat_history.append({"role": "assistant", "content": summary})
            self.round_history = []
            
            self.advance_round()

    def advance_round(self):
        """Advances the game time by one round (6 seconds) and processes triggers."""
        self.game_time.advance_time(6)
        
        # Process triggers for all entities in initiative
        # (and maybe others in the room? For now, just initiative)
        for entity in self.initiative_order:
            msgs = self.interaction_manager.process_triggers(entity, self.game_time)
            for msg in msgs:
                self.update_narrative(msg)
                self.round_history.append(msg)

    def _get_current_game_state(self, actor: Entity) -> Dict[str, Any]:
        actors_in_room = [e.name for e in self.initiative_order if e.name != actor.name]
        return {
            "actors_present": ", ".join(actors_in_room) if actors_in_room else "none",
            "objects_present": "none",
            "attitudes": "none",
            "game_history": "\n".join(self.round_history)
        }

    def answer_player_question(self, question: str):
        if not self.player_entity: 
            return
        game_state = self._get_current_game_state(self.player_entity)
        prompt = prompts['adam_assistant'].format(question=question, game_state=game_state)
        answer = self.llm_manager.generate_response(prompt=prompt, history=self.llm_chat_history)
        self.update_narrative(f"\n--- ADaM: {answer} ---")

    def move_entity(self, entity: Entity, target_x: int, target_y: int) -> bool:
        """Moves an entity to a new position on the map."""
        if not self.current_room: return False
        
        # Check bounds (assuming square map for now or check layers)
        # Check collision (is 'x' or another entity there?)
        # For now, just update coords and assume valid for the demo
        
        # Update Map Layers (Visuals)
        # Find old char
        old_char = None
        for item in self.current_room.legend:
            if item.entity == entity.name:
                old_char = item.char
                break
        
        if not old_char: return False # Should not happen if entity is on map

        # Clear old pos
        # We need to know which layer the entity is on. 
        # For simplicity, we scan all layers and move the char.
        moved = False
        for layer in self.current_room.layers:
            # Check if target is valid in this layer (bounds)
            if target_y < len(layer) and target_x < len(layer[target_y]):
                 # Check if target is passable (not wall 'W')
                 # This is a very basic check.
                 target_code = layer[target_y][target_x]
                 if target_code == 'W': # Hardcoded wall check for now
                     return False
                 
                 # Clear old
                 if hasattr(entity, 'y') and hasattr(entity, 'x'):
                     if entity.y < len(layer) and entity.x < len(layer[entity.y]):
                         if layer[entity.y][entity.x] == old_char:
                             layer[entity.y][entity.x] = 'x' # Restore floor?
                 
                 # Set new
                 layer[target_y][target_x] = old_char
                 moved = True
        
        if moved:
            entity.x = target_x
            entity.y = target_y
            self.update_map_callback(self.current_room)
            return True
        
        return False

    def process_player_actions_logic(self, player: Entity, processed_input: ProcessedInput, game_entities: Dict[str, Entity]) -> List[Tuple[str, str]]:
        """
        Processes actions using the InteractionManager.
        """
        results = []
        
        for action in processed_input.actions:
            # Handle MOVE intent
            if action.intent.name == "MOVE":
                if not processed_input.targets:
                    results.append(("Move where?", "Player tried to move but didn't specify where."))
                    continue
                
                target = processed_input.targets[0]
                
                # Simple "move to adjacent" logic
                # 1. Get target position
                if not hasattr(target, 'x') or not hasattr(target, 'y'):
                     # If target isn't on map (maybe abstract?), we can't move to it physically
                     results.append((f"You can't see {target.name} here.", f"Player tried to move to {target.name} but it's not on the map."))
                     continue
                
                # 2. Calculate adjacent square
                # Simple approach: Move to same Y, X-1 (left) or X+1 (right) depending on relative pos
                dx = target.x - player.x
                dy = target.y - player.y
                
                new_x, new_y = player.x, player.y
                
                if abs(dx) > abs(dy):
                    # Move horizontally
                    new_x = player.x + (1 if dx > 0 else -1)
                    # Don't overlap target
                    if new_x == target.x and new_y == target.y:
                         new_x = player.x # Stay put if adjacent? Or stop 1 short.
                else:
                    # Move vertically
                    new_y = player.y + (1 if dy > 0 else -1)
                    if new_x == target.x and new_y == target.y:
                         new_y = player.y

                # Check if we are already adjacent
                if abs(player.x - target.x) <= 1 and abs(player.y - target.y) <= 1:
                     results.append((f"You are already close to {target.name}.", f"Player is already near {target.name}."))
                     continue

                # Execute Move
                if self.move_entity(player, new_x, new_y):
                    results.append((f"You move towards {target.name}.", f"{player.name} moved to ({new_x}, {new_y})."))
                else:
                    results.append(("Something blocks your way.", f"{player.name} failed to move."))
                
                continue

            # Find the interaction object in the player's list
            interaction_obj = None
            
            # 1. Search in interactions (active uses)
            for inter in player.interaction:
                if inter.type.lower() == action.keyword.lower():
                    interaction_obj = inter
                    break
            
            # 2. Search in abilities (inherent capabilities)
            if not interaction_obj:
                for abil in player.ability:
                    if abil.type.lower() == action.keyword.lower():
                        interaction_obj = abil
                        break
            
            # Fallback for basic attacks if not explicitly defined but requested?
            # For now, if not found, we can't do it.
            if not interaction_obj:
                 results.append((f"You don't know how to {action.keyword}.", f"{player.name} tried to {action.keyword} but failed."))
                 continue

            # Execute
            success, narrative, log = self.interaction_manager.execute_interaction(player, interaction_obj, processed_input.targets, self.game_time, self.game_entities)
            results.append((narrative, log))
            
        return results