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
                for row in layer:
                    for char_code in row:
                        if char_code != 'x': 
                            placed_chars.add(char_code)
            
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
        
        self.update_narrative_callback(f"[{self.game_time.get_time_string()}] The adventure begins for {player.name}...")
        self.update_character_sheet_callback(self.player_entity)
        self.update_inventory_callback(self.player_entity)
        self.update_map_callback(self.current_room)

    def process_player_input(self, player_input: str):
        if not self.player_entity or not self.nlp_processor: 
            return
        
        processed_action = self.nlp_processor.process_player_input(player_input, self.game_entities)
        if not processed_action:
            self.update_narrative_callback("Error: Could not process input.")
            return
        
        action_results = process_player_actions(
            self.player_entity,
            processed_action,
            self.game_entities,
            self.loader.attributes
        )

        player_action_summary = ""
        for narrative_msg, history_msg in action_results:
            self.update_narrative_callback(narrative_msg)
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
                self.update_narrative_callback(fmt_narrative)
                self.round_history.append(fmt_narrative)
                self.llm_chat_history.append({"role": "assistant", "content": reaction_narrative})
                all_actions_taken = True
        
        if all_actions_taken:
            narrator_prompt = prompts['narrator_summary'].format(action_log="\n".join(self.round_history))
            summary = self.llm_manager.generate_response(prompt=narrator_prompt, history=self.llm_chat_history)
            self.update_narrative_callback(f"\n--- {summary} ---")
            self.llm_chat_history.append({"role": "assistant", "content": summary})
            self.round_history = []

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
        self.update_narrative_callback(f"\n--- ADaM: {answer} ---")

def process_player_actions(player: Entity, processed_input: ProcessedInput, game_entities: Dict[str, Entity], attributes: List[Dict]) -> List[Tuple[str, str]]:
    """
    Placeholder logic for processing actions. 
    Real logic should determine success/fail of actions based on attributes/skills.
    
    Returns:
        A list of tuples (narrative_message, history_log_message)
    """
    results = []
    # TODO: Implement full interaction logic using attributes and skills.
    for action in processed_input.actions:
         narrative = f"You attempt to {action.keyword}..."
         history = f"{player.name} tried to {action.keyword}."
         results.append((narrative, history))
    return results