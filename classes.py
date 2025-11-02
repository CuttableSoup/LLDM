from __future__ import annotations
from dataclasses import dataclass, field, fields
from typing import List, Dict, Any, Optional
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML not found. Please install: pip install PyYAML")
    yaml = None

@dataclass
class GameTime:
    year: int = 1
    month: int = 1
    day: int = 1
    hour: int = 0
    minute: int = 0
    second: int = 0

    def advance_time(self, seconds: int = 1):
        self.second += seconds
        
        while self.second >= 60:
            self.second -= 60
            self.minute += 1
        
        while self.minute >= 60:
            self.minute -= 60
            self.hour += 1
        
        while self.hour >= 24:
            self.hour -= 24
            self.day += 1
        
        while self.day > 30:
            self.day -= 30
            self.month += 1
        
        while self.month > 12:
            self.month -= 12
            self.year += 1

    def get_time_string(self) -> str:
        return f"Year {self.year}, Month {self.month}, Day {self.day}, Hour {self.hour:02d}:00"

    def copy(self) -> GameTime:
        return GameTime(self.year, self.month, self.day, self.hour)

@dataclass
class HistoryEvent:
    timestamp: GameTime
    event_type: str
    description: str
    
    participants: List[str] = field(default_factory=list) 

@dataclass
class EntityHistory:
    entity_name: str
    memory: List[HistoryEvent] = field(default_factory=list)

    def add_event(self, event: HistoryEvent):
        self.memory.append(event)

    def get_recent_history(self, count: int = 10) -> List[HistoryEvent]:
        return self.memory[-count:]

    def get_summary_for_llm(self) -> str:
        summary_lines = [
            f"--- Key Memories for {self.entity_name} ---"
        ]
        
        recent_memory = self.get_recent_history(count=20)
        
        if not recent_memory:
            return f"--- {self.entity_name} has no significant memories. ---"
            
        for event in recent_memory:
            time_str = f"Y{event.timestamp.year}-M{event.timestamp.month}-D{event.timestamp.day}"
            summary_lines.append(f"[{time_str}] ({event.event_type}): {event.description}")
            
        return "\n".join(summary_lines)

@dataclass
class Skill:
    base: int = 0
    specialization: Dict[str, int] = field(default_factory=dict)

@dataclass
class Attribute:
    base: int = 0
    skill: Dict[str, Skill] = field(default_factory=dict)

@dataclass
class Quality:
    body: str = ""
    eye: str = ""
    gender: str = ""
    hair: str = ""
    height: int = 0
    skin: str = ""
    age: str = ""

@dataclass
class Cost:
    initial: List[Dict[str, Any]] = field(default_factory=list)
    ongoing: List[Dict[str, Any]] = field(default_factory=list)

@dataclass
class DurationComponent:
    frequency: str = ""
    length: int = 0

@dataclass
class InventoryItem:
    item: str = ""
    quantity: int = 0
    equipped: bool = False
    inventory: List[InventoryItem] = field(default_factory=list)
    note: Optional[str] = None

@dataclass
class Entity:
    name: str = ""
    supertype: str = ""
    type: str = ""
    subtype: str = ""
    body: str = ""
    max_hp: int = 0
    cur_hp: int = 0
    max_fp: int = 0
    cur_fp: int = 0
    max_mp: int = 0
    cur_mp: int = 0
    exp: int = 0
    size: str = ""
    weight: float = 0.0
    attribute: Dict[str, Attribute] = field(default_factory=dict)
    quality: Quality = field(default_factory=Quality)
    status: List[Any] = field(default_factory=list)
    ally: Dict[str, Any] = field(default_factory=dict)
    enemy: Dict[str, Any] = field(default_factory=dict)
    attitude: Dict[str, Any] = field(default_factory=dict)
    language: List[str] = field(default_factory=list)
    target: List[str] = field(default_factory=list)
    resist: Dict[str, Dict[str, str]] = field(default_factory=dict)
    range: int = 0
    proficiency: Dict[str, Any] = field(default_factory=dict)
    apply: Dict[str, Any] = field(default_factory=dict)
    requirement: Dict[str, Any] = field(default_factory=dict)
    cost: Cost = field(default_factory=Cost)
    duration: List[DurationComponent] = field(default_factory=list)
    value: int = 0
    slot: Optional[str] = None
    inventory: List[InventoryItem] = field(default_factory=list)
    supernatural: List[str] = field(default_factory=list)
    memory: List[str] = field(default_factory=list)
    quote: List[str] = field(default_factory=list)

def create_entity_from_dict(data: Dict[str, Any]) -> Entity:
    data_copy = data.copy()

    if 'quality' in data_copy:
        data_copy['quality'] = Quality(**data_copy['quality'])
        
    if 'cost' in data_copy:
        data_copy['cost'] = Cost(**data_copy['cost'])
        
    if 'duration' in data_copy:
        data_copy['duration'] = [DurationComponent(**comp) for comp in data_copy['duration']]
        
    final_attributes: Dict[str, Attribute] = {}
    
    if 'attribute' in data_copy:
        for attr_name, attr_data in data_copy['attribute'].items():
            
            new_attr = Attribute()
            
            if isinstance(attr_data, dict):
                new_attr.base = attr_data.get('base', 0)
                
                if 'skill' in attr_data:
                    for skill_name, skill_data in attr_data['skill'].items():
                        if isinstance(skill_data, dict):
                            new_attr.skill[skill_name] = Skill(**skill_data)
                        else:
                            new_attr.skill[skill_name] = Skill(base=skill_data)
            else:
                new_attr.base = attr_data
            
            final_attributes[attr_name] = new_attr
        
        data_copy['attribute'] = final_attributes

    def _create_inventory(items_list: List[Dict]) -> List[InventoryItem]:
        output = []
        for item_data in items_list:
            nested_inv_data = item_data.pop('inventory', [])
            nested_inv = _create_inventory(nested_inv_data)
            output.append(InventoryItem(**item_data, inventory=nested_inv))
        return output

    if 'inventory' in data_copy:
        data_copy['inventory'] = _create_inventory(data_copy['inventory'])

    entity_field_names = {f.name for f in fields(Entity)}
    filtered_data = {k: v for k, v in data_copy.items() if k in entity_field_names}
    
    if 'max_hp' in filtered_data and 'cur_hp' not in filtered_data:
        filtered_data['cur_hp'] = filtered_data['max_hp']
    if 'max_mp' in filtered_data and 'cur_mp' not in filtered_data:
        filtered_data['cur_mp'] = filtered_data['max_mp']
    if 'max_fp' in filtered_data and 'cur_fp' not in filtered_data:
        filtered_data['cur_fp'] = filtered_data['max_fp']

    return Entity(**filtered_data)

@dataclass
class RoomLegendItem:
    char: str = ""
    entity: str = ""
    color: Optional[str] = None
    map_name: Optional[str] = None
    is_player: bool = False
    inventory: List[Dict[str, Any]] = field(default_factory=list)
    pattern: Optional[List[List[str]]] = None

@dataclass
class Room:
    name: str = ""
    description: str = ""
    scale: int = 1
    layers: List[List[List[str]]] = field(default_factory=list)
    legend: List[RoomLegendItem] = field(default_factory=list)

@dataclass
class Environment:
    rooms: List[Room] = field(default_factory=list)

@dataclass
class Scenario:
    scenario_name: str = ""
    environment: Environment = field(default_factory=Environment)


class RulesetLoader:
    def __init__(self, ruleset_path: Path):
        if not yaml:
            raise ImportError("PyYAML is required to load rulesets.")
        self.ruleset_path = ruleset_path
        self.characters: Dict[str, Entity] = {}
        self.creatures: Dict[str, Entity] = {}
        self.items: Dict[str, Entity] = {}
        self.spells: Dict[str, Entity] = {}
        self.conditions: Dict[str, Entity] = {}
        self.environment_ents: Dict[str, Entity] = {}
        self.scenario: Optional[Scenario] = None
        
        self.attributes: List[Any] = []
        self.types: List[Any] = []
        print(f"RulesetLoader initialized for path: {self.ruleset_path}")

    def load_all(self):
        if not self.ruleset_path.is_dir():
            print(f"Error: Ruleset path not found: {self.ruleset_path}")
            return

        for yaml_file in self.ruleset_path.glob("**/*.yaml"):
            print(f"Processing file: {yaml_file.name}")
            
            if yaml_file.name == "rooms.yaml":
                self._load_scenario(yaml_file)
                continue
            
            if yaml_file.name == "attributes.yaml":
                self.attributes = self._load_generic_yaml_all(yaml_file)
                continue
            if yaml_file.name == "types.yaml":
                self.types = self._load_generic_yaml_all(yaml_file)
                continue

            entities_data = self._load_generic_yaml_all(yaml_file)
            
            for entity_data in entities_data:
                if not isinstance(entity_data, dict) or 'entity' not in entity_data:
                    print(f"Warning: Skipping document in {yaml_file.name} (missing 'entity:' tag).")
                    continue
                
                data = entity_data['entity']
                
                if 'name' not in data:
                    print(f"Warning: Skipping entity in {yaml_file.name} (missing 'name').")
                    continue
                
                entity_obj = create_entity_from_dict(data)
                
                parent_dir = yaml_file.parent.name
                
                if parent_dir == "characters":
                    self.characters[entity_obj.name] = entity_obj
                elif parent_dir == "creatures":
                    self.creatures[entity_obj.name] = entity_obj
                elif parent_dir == "items":
                    self.items[entity_obj.name] = entity_obj
                elif parent_dir == "spells":
                    self.spells[entity_obj.name] = entity_obj
                elif parent_dir == "conditions":
                    self.conditions[entity_obj.name] = entity_obj
                elif parent_dir == "medievalfantasy" and yaml_file.name == "environment.yaml":
                    self.environment_ents[entity_obj.name] = entity_obj
                else:
                    if entity_obj.supertype == "creature" and data.get("is_player", False):
                        self.characters[entity_obj.name] = entity_obj
                    elif entity_obj.supertype == "creature":
                        self.creatures[entity_obj.name] = entity_obj
                    elif entity_obj.supertype == "object":
                        self.items[entity_obj.name] = entity_obj
                    elif entity_obj.supertype == "supernatural":
                        self.spells[entity_obj.name] = entity_obj
                    elif entity_obj.supertype == "environment":
                        self.environment_ents[entity_obj.name] = entity_obj


    def _load_generic_yaml_all(self, file_path: Path) -> List[Any]:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return list(yaml.safe_load_all(f))
        except Exception as e:
            print(f"Error loading YAML file {file_path}: {e}")
            return []

    def _load_scenario(self, file_path: Path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            
            if not data:
                return

            map_data = data.get('map', {})
            if not map_data:
                print(f"Warning: 'map:' key not found in {file_path.name}. Skipping scenario load.")
                return

            env_data = map_data.get('environment', {})
            room_list_data = env_data.get('rooms', [])
            parsed_rooms = []
            
            for room_data in room_list_data:
                legend_list_data = room_data.get('legend', [])
                parsed_legend = []
                for item in legend_list_data:
                    if isinstance(item, dict):
                        parsed_legend.append(RoomLegendItem(**item))
                
                room_data['legend'] = parsed_legend
                parsed_rooms.append(Room(**room_data))
            
            parsed_env = Environment(rooms=parsed_rooms)
            self.scenario = Scenario(
                scenario_name=map_data.get('name', 'Unnamed Scenario'),
                environment=parsed_env
            )
            print(f"Successfully loaded scenario: {self.scenario.scenario_name}")

        except Exception as e:
            print(f"Error loading scenario file {file_path}: {e}")

    def get_character(self, name: str) -> Optional[Entity]:
        return self.characters.get(name)

from typing import List, Dict, Any, Optional, Callable, Tuple
from pathlib import Path

try:
    from nlp_processor import NLPProcessor, ProcessedInput
    from action_processor import process_player_actions
    from config_manager import ConfigManager
    from llm_manager import LLMManager, OLLAMA_MODELS
except ImportError as e:
    print(f"GameController (in classes.py) Error: Failed to import modules: {e}")
    class NLPProcessor:
        def __init__(self, *args): pass
        def process_player_input(self, *args): return None
    class ProcessedInput: pass
    class LLMManager: pass
    def process_player_actions(*args) -> List[Tuple[str, str]]:
        return [("Error: 'action_processor.py' not found.", "Error")]


class GameController:
    def __init__(self, loader: RulesetLoader, ruleset_path: Path, llm_manager: LLMManager):
        self.loader = loader
        self.nlp_processor = NLPProcessor(ruleset_path)
        self.llm_manager = llm_manager
        self.player_entity: Optional[Entity] = None
        self.game_time = GameTime(year=1, month=1, day=1, hour=8)
        self.game_entities: Dict[str, Entity] = {}
        self.entity_histories: Dict[str, EntityHistory] = {}
        self.current_room: Optional[Room] = None
        
        self.game_entities.update(self.loader.creatures)
        self.game_entities.update(self.loader.characters)

        for name, entity in self.game_entities.items():
            if any(status in entity.status for status in ["intelligent", "animalistic", "robotic"]):
                self.entity_histories[name] = EntityHistory(entity_name=name)
                print(f"GameController: Initialized history for intelligent entity: {name}")

        self.initiative_order: List[Entity] = []
        self.round_history: List[str] = []
        self.llm_chat_history: List[Dict[str, str]] = []

        self.update_narrative_callback: Callable[[str], None] = lambda text: None
        self.update_character_sheet_callback: Callable[[Entity], None] = lambda entity: None
        self.update_inventory_callback: Callable[[Entity], None] = lambda entity: None
        self.update_map_callback: Callable[[Optional[Room]], None] = lambda room: None

    def start_game(self, player: Entity):
        self.player_entity = player
        if player.name not in self.game_entities:
            self.game_entities[player.name] = player
            
        if any(status in player.status for status in ["intelligent", "animalistic", "robotic"]):
            if player.name not in self.entity_histories:
                self.entity_histories[player.name] = EntityHistory(entity_name=player.name)
                print(f"GameController: Initialized history for player: {player.name}")
            
        if self.loader.scenario and self.loader.scenario.environment.rooms:
            self.current_room = self.loader.scenario.environment.rooms[0]
            print(f"Loaded initial room: {self.current_room.name}")
        else:
            print("Warning: No scenario or rooms found in loader.")
        
        self.initiative_order = []
        
        if self.current_room and self.current_room.layers:
            legend_lookup: Dict[str, str] = {}
            if self.current_room.legend:
                for item in self.current_room.legend:
                    legend_lookup[item.char] = item.entity

            placed_chars = set()
            for layer in self.current_room.layers:
                for y, row in enumerate(layer):
                    for x, char_code in enumerate(row):
                        if char_code != 'x':
                            placed_chars.add(char_code)
            
            print("--- Loading Entities for Initiative ---")
            for char_code in placed_chars:
                entity_name = legend_lookup.get(char_code)
                if not entity_name:
                    print(f"Warning: Character '{char_code}' on map but not in legend.")
                    continue
                
                entity_obj = self.game_entities.get(entity_name)
                
                if not entity_obj:
                    entity_obj = self.loader.environment_ents.get(entity_name)
                    if entity_obj and entity_name not in self.game_entities:
                        self.game_entities[entity_name] = entity_obj
                
                if entity_obj:
                    if entity_obj not in self.initiative_order:
                        self.initiative_order.append(entity_obj)
                        print(f"Added '{entity_name}' (char: '{char_code}') to initiative.")
                else:
                    print(f"Warning: Entity '{entity_name}' (char: '{char_code}') not found in any loader.")

        else:
            print("Warning: No room loaded, adding only player to initiative.")
            if self.player_entity:
                self.initiative_order = [self.player_entity]

        if self.player_entity and self.player_entity not in self.initiative_order:
            print(f"Warning: Player '{self.player_entity.name}' not placed on map, adding to initiative.")
            self.initiative_order.append(self.player_entity)
            
        print(f"Starting game with {len(self.initiative_order)} entities in initiative.")
        
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
        
        print("GameController started.")


    def process_player_input(self, player_input: str):
        if not self.player_entity or not self.nlp_processor:
            return

        print(f"Processing input: {player_input}")
        
        processed_action = self.nlp_processor.process_player_input(
            player_input, 
            self.game_entities
        )
        
        if not processed_action:
            self.update_narrative_callback("Error: Could not process input.")
            return
        
        action_results = process_player_actions(
            self.player_entity,
            processed_action,
            self.game_entities
        )

        targets_affected = processed_action.targets

        player_action_summary = ""
        
        for narrative_msg, history_msg in action_results:
            self.update_narrative_callback(narrative_msg)
            self.round_history.append(history_msg)
            player_action_summary += history_msg + " "

        player_event = HistoryEvent(
            timestamp=self.game_time.copy(),
            event_type="player_action",
            description=player_action_summary.strip(),
            participants=[t.name for t in targets_affected]
        )
        
        for target_entity in targets_affected:
            if target_entity.name in self.entity_histories:
                self.entity_histories[target_entity.name].add_event(player_event)
        
        if self.player_entity.name in self.entity_histories:
            self.entity_histories[self.player_entity.name].add_event(player_event)


        self.llm_chat_history.append({"role": "user", "content": player_action_summary.strip()})
        
        for target in targets_affected:
            self.update_character_sheet_callback(target)
        self.update_character_sheet_callback(self.player_entity)
        self.update_inventory_callback(self.player_entity)
        
        self._run_npc_turns(player_action_summary)

    def _run_npc_turns(self, player_action_summary: str):
        print("Running NPC turns...")
        if not self.player_entity: return
        
        all_actions_taken = False
        
        game_state_context = self._get_current_game_state(self.player_entity)
        
        for npc in self.initiative_order:
            if npc == self.player_entity:
                continue 

            if not ("intelligent" in npc.status or "animalistic" in npc.status or "robotic" in npc.status):
                continue
            
            npc_history_summary = ""
            if npc.name in self.entity_histories:
                npc_history_summary = self.entity_histories[npc.name].get_summary_for_llm()
            
            npc_prompt = (
                f"--- Your Context ---\n"
                f"You are {npc.name}. \n"
                f"{npc_history_summary}\n"
                f"--- Current Situation ---\n"
                f"You are in a room with: {game_state_context['actors_present']}. \n"
                f"The player, {self.player_entity.name}, just did this: '{player_action_summary}'. \n"
                f"What is your reaction or next action? Respond in character, briefly."
            )
            
            reaction_narrative = self.llm_manager.generate_response(
                prompt=npc_prompt,
                history=self.llm_chat_history 
            )
            
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
                if reaction_narrative.startswith("Error:"):
                    formatted_narrative = reaction_narrative
                else:
                    formatted_narrative = f"{npc.name}: \"{reaction_narrative}\""
                
                self.update_narrative_callback(formatted_narrative)
                self.round_history.append(formatted_narrative)
                self.llm_chat_history.append({"role": "assistant", "content": reaction_narrative})
            
            all_actions_taken = True
        
        self._process_round_updates()
        
        if all_actions_taken:
            action_log = "\n".join(self.round_history)
            
            narrator_prompt = (
                f"You are the narrator. The following is a log of all actions and dialogue "
                f"that just occurred in a single round. Summarize these events into an "
                f"engaging, brief narrative summary for the player. Do not act as an NPC.\n"
                f"--- ACTION LOG --- "
                f"{action_log}\n"
                f"--- END LOG --- "
                f"Narrate the summary:"
            )
            
            summary = self.llm_manager.generate_response(
                prompt=narrator_prompt,
                history=self.llm_chat_history
            )
            
            if summary.startswith("Error:"):
                self.update_narrative_callback(f"\n--- {summary} ---")
            else:
                self.update_narrative_callback(f"\n--- {summary} ---")
                
                self.llm_chat_history.append({"role": "assistant", "content": summary})
            
            self.round_history = []

    def _get_current_game_state(self, actor: Entity) -> Dict[str, Any]:
        
        actors_in_room = [e.name for e in self.initiative_order if e.name != actor.name]
        
        attitudes_str = "none"
        if actor.attitude:
            try:
                import json
                attitudes_str = json.dumps(actor.attitude)
            except ImportError:
                pass
        
        objects_in_room = []

        return {
            "actors_present": ", ".join(actors_in_room) if actors_in_room else "none",
            "objects_present": ", ".join(objects_in_room) if objects_in_room else "none",
            "attitudes": attitudes_str,
            "game_history": "\n".join(self.round_history)
        }

    def _process_round_updates(self):
        pass