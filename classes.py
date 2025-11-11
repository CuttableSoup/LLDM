"""
This module defines the core data classes used throughout the game.

It includes classes for game time, history events, entities (like players, creatures, items),
and game world components like rooms and environments. It also includes a `RulesetLoader`
for loading game data from YAML files and a `GameController` to manage the main game loop.
"""
from __future__ import annotations
from dataclasses import dataclass, field, fields
from typing import List, Dict, Any, Optional, Callable, Tuple
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML not found. Please install: pip install PyYAML")
    yaml = None

@dataclass
class GameTime:
    """Represents the in-game time."""
    year: int = 1
    month: int = 1
    day: int = 1
    hour: int = 0
    minute: int = 0
    second: int = 0

    def advance_time(self, seconds: int = 1):
        """Advances the game time by a specified number of seconds."""
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
        
        # Assuming 30 days in a month for simplicity.
        while self.day > 30:
            self.day -= 30
            self.month += 1
        
        while self.month > 12:
            self.month -= 12
            self.year += 1

    def get_time_string(self) -> str:
        """Returns a formatted string representation of the current time."""
        return f"Year {self.year}, Month {self.month}, Day {self.day}, Hour {self.hour:02d}:00"

    def copy(self) -> GameTime:
        """Creates a copy of the current GameTime object."""
        return GameTime(self.year, self.month, self.day, self.hour, self.minute, self.second)

@dataclass
class HistoryEvent:
    """Represents a single event that occurred in the game world."""
    timestamp: GameTime
    event_type: str
    description: str
    participants: List[str] = field(default_factory=list)

@dataclass
class EntityHistory:
    """Stores the history of events for a specific entity."""
    entity_name: str
    memory: List[HistoryEvent] = field(default_factory=list)

    def add_event(self, event: HistoryEvent):
        """Adds a new event to the entity's memory."""
        self.memory.append(event)

    def get_recent_history(self, count: int = 10) -> List[HistoryEvent]:
        """Returns a list of the most recent historical events."""
        return self.memory[-count:]

    def get_summary_for_llm(self) -> str:
        """Generates a concise summary of the entity's recent history for the LLM."""
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
    """Represents a skill with a base value and specializations."""
    base: int = 0
    specialization: Dict[str, int] = field(default_factory=dict)

@dataclass
class Attribute:
    """Represents an attribute that can have associated skills."""
    base: int = 0
    skill: Dict[str, Skill] = field(default_factory=dict)

@dataclass
class Quality:
    """Represents the physical qualities of an entity."""
    body: str = ""
    eye: str = ""
    gender: str = ""
    hair: str = ""
    height: int = 0
    skin: str = ""
    age: str = ""

@dataclass
class Cost:
    """Represents the cost to use a skill or item."""
    initial: List[Dict[str, Any]] = field(default_factory=list)
    ongoing: List[Dict[str, Any]] = field(default_factory=list)

@dataclass
class DurationComponent:
    """Represents the duration of an effect."""
    frequency: str = ""
    length: int = 0

@dataclass
class InventoryItem:
    """Represents an item in an entity's inventory."""
    item: str = ""
    quantity: int = 0
    equipped: bool = False
    inventory: List[InventoryItem] = field(default_factory=list)
    note: Optional[str] = None

@dataclass
class Entity:
    """A generic representation of any object or character in the game world."""
    name: str = ""
    supertype: str = ""  # e.g., creature, object, supernatural
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
    inventory_rules: List[Dict[str, Any]] = field(default_factory=list) # --- NEW ---
    supernatural: List[str] = field(default_factory=list)
    memory: List[str] = field(default_factory=list)
    quote: List[str] = field(default_factory=list)

def create_entity_from_dict(data: Dict[str, Any]) -> Entity:
    """
    Creates an Entity object from a dictionary.

    This function handles the nested structure of the entity data, converting
    dictionaries into their corresponding dataclass objects.

    Args:
        data: The dictionary containing the entity data.

    Returns:
        An Entity object.
    """
    data_copy = data.copy()

    # Recursively create nested dataclass objects.
    if 'quality' in data_copy:
        data_copy['quality'] = Quality(**data_copy['quality'])
        
    if 'cost' in data_copy:
        data_copy['cost'] = Cost(**data_copy['cost'])
        
    if 'duration' in data_copy:
        data_copy['duration'] = [DurationComponent(**comp) for comp in data_copy['duration']]
        
    final_attributes: Dict[str, Attribute] = {}
    
    # --- MODIFIED ---
    # This block now creates the flat attribute map (e.g., 'physique.blade')
    # required by the InteractionProcessor.
    if 'attribute' in data_copy:
        raw_attributes = data_copy['attribute']
        
        # This function will recursively add attributes
        def process_attr(attr_map: Dict, path_prefix=""):
            for key, value in attr_map.items():
                
                # 'choice' blocks are for requirements/apply, not base stats
                if key == 'choice':
                    continue
                    
                current_path = f"{path_prefix}{key}"
                
                # Check if it's a flat attribute (e.g., 'physique: 9' or 'physique.strength: 3')
                if isinstance(value, (int, float)):
                    final_attributes[current_path] = Attribute(base=value)
                
                # Check if it's a nested attribute block (e.g., 'physique: { base: 9, skill: ...}')
                elif isinstance(value, dict):
                    base_val = value.get('base', 0)
                    final_attributes[current_path] = Attribute(base=base_val)
                    
                    # Recurse for nested skills/specializations
                    # (e.g., skill: { blade: ... })
                    if 'skill' in value and isinstance(value['skill'], dict):
                        process_attr(value['skill'], path_prefix=f"{current_path}.")
                    if 'specialization' in value and isinstance(value['specialization'], dict):
                         process_attr(value['specialization'], path_prefix=f"{current_path}.")
        
        if isinstance(raw_attributes, dict):
            process_attr(raw_attributes)
        
        data_copy['attribute'] = final_attributes
    # --- END MODIFICATION ---

    # --- MODIFIED ---
    def _create_inventory(items_list: List[Dict]) -> List[InventoryItem]:
        """Helper function to recursively create inventory items."""
        output = []
        for item_data in items_list:
            # --- NEW LOGIC ---
            # Only process this as an item if it has an 'item' key
            # This skips the 'requirement' blocks
            if 'item' in item_data:
                nested_inv_data = item_data.pop('inventory', [])
                nested_inv = _create_inventory(nested_inv_data)
                output.append(InventoryItem(**item_data, inventory=nested_inv))
            # --- END NEW LOGIC ---
        return output

    if 'inventory' in data_copy:
        all_inventory_entries = data_copy.get('inventory', [])
        
        # Filter for actual items
        item_entries = [entry for entry in all_inventory_entries if 'item' in entry]
        data_copy['inventory'] = _create_inventory(item_entries)
        
        # Filter for requirements
        rule_entries = [entry['requirement'] for entry in all_inventory_entries if 'requirement' in entry]
        data_copy['inventory_rules'] = rule_entries
    # --- END MODIFICATION ---

    # Filter out any keys from the dictionary that are not fields in the Entity dataclass.
    entity_field_names = {f.name for f in fields(Entity)}
    filtered_data = {k: v for k, v in data_copy.items() if k in entity_field_names}
    
    # Set current HP/MP/FP to max if not specified.
    if 'max_hp' in filtered_data and 'cur_hp' not in filtered_data:
        filtered_data['cur_hp'] = filtered_data['max_hp']
    if 'max_mp' in filtered_data and 'cur_mp' not in filtered_data:
        filtered_data['cur_mp'] = filtered_data['max_mp']
    if 'max_fp' in filtered_data and 'cur_fp' not in filtered_data:
        filtered_data['cur_fp'] = filtered_data['max_fp']

    return Entity(**filtered_data)

@dataclass
class RoomLegendItem:
    """Represents an item in the legend of a room map."""
    char: str = ""  # The character symbol on the map.
    entity: str = ""  # The name of the entity this symbol represents.
    color: Optional[str] = None
    map_name: Optional[str] = None
    is_player: bool = False
    inventory: List[Dict[str, Any]] = field(default_factory=list)
    pattern: Optional[List[List[str]]] = None

@dataclass
class Room:
    """Represents a single room or area in the game world."""
    name: str = ""
    description: str = ""
    scale: int = 1
    layers: List[List[List[str]]] = field(default_factory=list)  # The map layout.
    legend: List[RoomLegendItem] = field(default_factory=list)

@dataclass
class Environment:
    """Represents the game environment, containing all the rooms."""
    rooms: List[Room] = field(default_factory=list)

@dataclass
class Scenario:
    """Represents a game scenario, including the environment."""
    scenario_name: str = ""
    environment: Environment = field(default_factory=Environment)


class RulesetLoader:
    """Loads game data from YAML files in a specified ruleset directory."""
    def __init__(self, ruleset_path: Path):
        if not yaml:
            raise ImportError("PyYAML is required to load rulesets.")
        self.ruleset_path = ruleset_path
        
        # Special storage for player/named characters
        self.characters: Dict[str, Entity] = {} 
        # Main dynamic storage, keyed by supertype
        self.entities_by_supertype: Dict[str, Dict[str, Entity]] = {}
        
        self.scenario: Optional[Scenario] = None
        self.attributes: List[Any] = []
        self.types: List[Any] = []
        
        print(f"RulesetLoader initialized for path: {self.ruleset_path}")

    # --- MODIFIED: True two-pass dynamic loading ---
    def load_all(self):
        """Loads all YAML files from the ruleset directory."""
        if not self.ruleset_path.is_dir():
            print(f"Error: Ruleset path not found: {self.ruleset_path}")
            return
            
        all_yaml_files = list(self.ruleset_path.glob("**/*.yaml"))
        schema_files_paths = set() # To track files we process in Pass 1

        # --- PASS 1: Load Schemas and Build Dynamic Maps ---
        print("--- RulesetLoader: Pass 1 (Schemas) ---")
        for yaml_file in all_yaml_files:
            docs = self._load_generic_yaml_all(yaml_file)
            if not docs:
                continue
                
            is_schema_file = False
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                
                # Identify schema files by their unique top-level keys
                if 'aptitude' in doc:
                    self.attributes.append(doc)
                    is_schema_file = True
                elif 'category' in doc:
                    self.types.append(doc)
                    is_schema_file = True
                elif 'map' in doc:
                    # This file contains scenario data
                    self._load_scenario_from_data(doc, yaml_file.name)
                    is_schema_file = True
            
            if is_schema_file:
                print(f"Identified schema data in: {yaml_file.name}")
                schema_files_paths.add(yaml_file)

        # Now, dynamically initialize storage based on found supertypes
        dynamic_supertypes = set()
        for doc in self.types: # self.types is a list of docs
            category = doc.get('category', {})
            if isinstance(category, dict):
                supertype = category.get('supertype')
                if supertype:
                    dynamic_supertypes.add(supertype)
        
        for supertype in dynamic_supertypes:
            self.entities_by_supertype[supertype] = {}
            
        print(f"Dynamically initialized storage for supertypes: {dynamic_supertypes}")

        # --- PASS 2: Load All Entities ---
        print("--- RulesetLoader: Pass 2 (Entities) ---")
        for yaml_file in all_yaml_files:
            # Skip the schema files we already processed
            if yaml_file in schema_files_paths:
                continue

            print(f"Processing entity file: {yaml_file.name}")
            entities_data = self._load_generic_yaml_all(yaml_file)
            
            for entity_data in entities_data:
                # This doc might be an entity
                if isinstance(entity_data, dict) and 'entity' in entity_data:
                    data = entity_data['entity']
                    
                    if 'name' not in data:
                        print(f"Warning: Skipping entity in {yaml_file.name} (missing 'name').")
                        continue
                    
                    entity_obj = create_entity_from_dict(data)
                    
                    # --- DYNAMIC SORTING LOGIC ---
                    # 1. Player/Character Check (Highest Priority)
                    if data.get("is_player", False):
                        self.characters[entity_obj.name] = entity_obj
                    
                    # 2. Supertype-Based Check (Data-driven)
                    elif entity_obj.supertype and entity_obj.supertype in self.entities_by_supertype:
                        self.entities_by_supertype[entity_obj.supertype][entity_obj.name] = entity_obj
                        
                    # 3. Log Unsorted
                    else:
                        print(f"Warning: Could not categorize entity '{entity_obj.name}' in {yaml_file.name}. "
                              f"Supertype '{entity_obj.supertype}' is not in the dynamic list from types data. "
                              "It will not be loaded into a category.")
                
                # Other docs without 'entity' (like templates.yaml) are skipped silently
    # --- END MODIFICATION ---

    def _load_generic_yaml_all(self, file_path: Path) -> List[Any]:
        """Loads all documents from a generic YAML file."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                # Filter out 'None' documents that result from '---'
                return [doc for doc in yaml.safe_load_all(f) if doc]
        except Exception as e:
            print(f"Error loading YAML file {file_path}: {e}")
            return []

    # --- MODIFIED: Renamed and logic updated to accept data ---
    def _load_scenario_from_data(self, data: Dict, file_name: str):
        """Loads a scenario from an already-loaded YAML document."""
        try:
            if not data:
                return

            map_data = data.get('map', {})
            if not map_data:
                print(f"Warning: 'map:' key not found in {file_name}. Skipping scenario load.")
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
            print(f"Successfully loaded scenario: {self.scenario.scenario_name} (from {file_name})")

        except Exception as e:
            print(f"Error loading scenario data from {file_name}: {e}")
    # --- END MODIFICATION ---

    def get_character(self, name: str) -> Optional[Entity]:
        """Retrieves a character entity by name."""
        return self.characters.get(name)

# Placeholder imports for GameController if modules are missing.
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
    """Manages the main game loop, player input, and NPC actions."""
    def __init__(self, loader: RulesetLoader, ruleset_path: Path, llm_manager: LLMManager):
        self.loader = loader
        self.nlp_processor = NLPProcessor(ruleset_path)
        self.llm_manager = llm_manager
        self.player_entity: Optional[Entity] = None
        self.game_time = GameTime(year=1, month=1, day=1, hour=8)
        self.game_entities: Dict[str, Entity] = {}
        self.entity_histories: Dict[str, EntityHistory] = {}
        self.current_room: Optional[Room] = None
        
        # --- Load entities from dynamic storage ---
        print("GameController: Loading all entities...")
        # Load all entities from the ruleset loader into one map
        self.game_entities.update(self.loader.characters)
        print(f"GameController: Loaded {len(self.loader.characters)} characters.")
        
        # Dynamically load from all discovered supertypes
        for supertype_name, entity_dict in self.loader.entities_by_supertype.items():
            print(f"GameController: Loading {len(entity_dict)} entities from supertype '{supertype_name}'...")
            self.game_entities.update(entity_dict)
        # --- END ---


        # Initialize histories for intelligent entities.
        for name, entity in self.game_entities.items():
            if any(status in entity.status for status in ["intelligent", "animalistic", "robotic"]):
                self.entity_histories[name] = EntityHistory(entity_name=name)
                print(f"GameController: Initialized history for intelligent entity: {name}")

        self.initiative_order: List[Entity] = []
        self.round_history: List[str] = []
        self.llm_chat_history: List[Dict[str, str]] = []

        # Callbacks for updating the GUI.
        self.update_narrative_callback: Callable[[str], None] = lambda text: None
        self.update_character_sheet_callback: Callable[[Entity], None] = lambda entity: None
        self.update_inventory_callback: Callable[[Entity], None] = lambda entity: None
        self.update_map_callback: Callable[[Optional[Room]], None] = lambda room: None

    def start_game(self, player: Entity):
        """Starts the game with the given player entity."""
        self.player_entity = player
        if player.name not in self.game_entities:
            self.game_entities[player.name] = player
            
        # Initialize history for the player if they are intelligent.
        if any(status in player.status for status in ["intelligent", "animalistic", "robotic"]):
            if player.name not in self.entity_histories:
                self.entity_histories[player.name] = EntityHistory(entity_name=player.name)
                print(f"GameController: Initialized history for player: {player.name}")
            
        # Load the initial room from the scenario.
        if self.loader.scenario and self.loader.scenario.environment.rooms:
            self.current_room = self.loader.scenario.environment.rooms[0]
            print(f"Loaded initial room: {self.current_room.name}")
        else:
            print("Warning: No scenario or rooms found in loader.")
        
        self.initiative_order = []
        
        # Populate the initiative order based on entities present in the current room.
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
                
                # Get entity from the master list
                entity_obj = self.game_entities.get(entity_name)
                
                if entity_obj:
                    if entity_obj not in self.initiative_order:
                        if not ("intelligent" in entity_obj.status or "animalistic" in entity_obj.status or "robotic" in entity_obj.status):
                            continue
                        self.initiative_order.append(entity_obj)
                        print(f"Added '{entity_name}' (char: '{char_code}') to initiative.")
                else:
                    print(f"Warning: Entity '{entity_name}' (char: '{char_code}') not found in any loader.")

        else:
            print("Warning: No room loaded, adding only player to initiative.")
            if self.player_entity:
                self.initiative_order = [self.player_entity]

        # Ensure the player is in the initiative order.
        if self.player_entity and self.player_entity not in self.initiative_order:
            print(f"Warning: Player '{self.player_entity.name}' not placed on map, adding to initiative.")
            self.initiative_order.append(self.player_entity)
            
        print(f"Starting game with {len(self.initiative_order)} entities in initiative.")
        
        # Create a starting event for the history.
        start_event = HistoryEvent(
            timestamp=self.game_time.copy(),
            event_type="world",
            description="The adventure begins.",
            participants=[e.name for e in self.initiative_order]
        )
        for history in self.entity_histories.values():
            history.add_event(start_event)
        
        # Update the GUI with the initial game state.
        self.update_narrative_callback(f"[{self.game_time.get_time_string()}] The adventure begins for {player.name}...")
        self.update_character_sheet_callback(self.player_entity)
        self.update_inventory_callback(self.player_entity)
        self.update_map_callback(self.current_room)
        
        print("GameController started.")


    def process_player_input(self, player_input: str):
        """Processes the player's text input."""
        if not self.player_entity or not self.nlp_processor:
            return

        print(f"Processing input: {player_input}")
        
        # Use the NLP processor to understand the player's intent.
        processed_action = self.nlp_processor.process_player_input(
            player_input, 
            self.game_entities
        )
        
        if not processed_action:
            self.update_narrative_callback("Error: Could not process input.")
            return
        
        # --- THIS IS THE MODIFIED PART ---
        # Process the identified actions.
        action_results = process_player_actions(
            self.player_entity,
            processed_action,
            self.game_entities,
            self.loader.attributes  # <-- Pass the loaded attributes
        )
        # --- END OF MODIFICATION ---

        targets_affected = processed_action.targets

        player_action_summary = ""
        
        # Update narrative and round history with the results of the player's actions.
        for narrative_msg, history_msg in action_results:
            self.update_narrative_callback(narrative_msg)
            self.round_history.append(history_msg)
            player_action_summary += history_msg + " "

        # Create a history event for the player's action.
        player_event = HistoryEvent(
            timestamp=self.game_time.copy(),
            event_type="player_action",
            description=player_action_summary.strip(),
            participants=[t.name for t in targets_affected]
        )
        
        # Add the event to the history of affected entities.
        for target_entity in targets_affected:
            if target_entity.name in self.entity_histories:
                self.entity_histories[target_entity.name].add_event(player_event)
        
        if self.player_entity.name in self.entity_histories:
            self.entity_histories[self.player_entity.name].add_event(player_event)

        # Add the player's action to the LLM chat history.
        self.llm_chat_history.append({"role": "user", "content": player_action_summary.strip()})
        
        # Update character sheets for affected entities.
        for target in targets_affected:
            self.update_character_sheet_callback(target)
        self.update_character_sheet_callback(self.player_entity)
        self.update_inventory_callback(self.player_entity)
        
        # Run NPC turns after the player's turn.
        self._run_npc_turns(player_action_summary)

    def _run_npc_turns(self, player_action_summary: str):
        """Runs the turns for all non-player characters (NPCs)."""
        print("Running NPC turns...")
        if not self.player_entity: return
        
        all_actions_taken = False
        
        game_state_context = self._get_current_game_state(self.player_entity)
        
        # Iterate through NPCs in the initiative order.
        for npc in self.initiative_order:
            if npc == self.player_entity:
                continue 

            # Only run turns for intelligent NPCs.
            if not ("intelligent" in npc.status or "animalistic" in npc.status or "robotic" in npc.status):
                continue
            
            npc_history_summary = ""
            if npc.name in self.entity_histories:
                npc_history_summary = self.entity_histories[npc.name].get_summary_for_llm()
            
            # Create a prompt for the LLM to generate the NPC's action.
            npc_prompt = (
                f"--- Your Context ---\n"
                f"You are {npc.name}. \n"
                f"{npc_history_summary}\n"
                f"--- Current Situation ---\n"
                f"You are in a room with: {game_state_context['actors_present']}. \n"
                f"The player, {self.player_entity.name}, just did this: '{player_action_summary}'. \n"
                f"What is your reaction or next action? Respond in character, briefly."
            )
            
            # Generate the NPC's response using the LLM.
            reaction_narrative = self.llm_manager.generate_response(
                prompt=npc_prompt,
                history=self.llm_chat_history 
            )
            
            # Create history events for the NPC's action.
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
            
            # Update the narrative with the NPC's action.
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
        
        # If any NPC took an action, generate a summary of the round.
        if all_actions_taken:
            action_log = "\n".join(self.round_history)
            
            # Create a prompt for the LLM to narrate a summary of the round.
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
            
            # Update the narrative with the summary.
            if summary.startswith("Error:"):
                self.update_narrative_callback(f"\n--- {summary} ---")
            else:
                self.update_narrative_callback(f"\n--- {summary} ---")
                
                self.llm_chat_history.append({"role": "assistant", "content": summary})
            
            self.round_history = []

    def _get_current_game_state(self, actor: Entity) -> Dict[str, Any]:
        """Gets the current game state from the perspective of a given actor."""
        
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
        """Processes any updates that should occur at the end of a round."""
        # This is a placeholder for future functionality, such as status effect updates.
        pass