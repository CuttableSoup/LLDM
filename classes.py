from __future__ import annotations
from dataclasses import dataclass, field, fields
from typing import List, Dict, Any, Optional, Union
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML not found. Please install: pip install PyYAML")
    yaml = None

@dataclass
class Skill:
    """
    Holds data for a single skill, including its specializations.
    Updated to match character_schema.yaml (base value only).
    """
    base: int = 0
    """The base value (in pips) for the skill."""
    specialization: Dict[str, int] = field(default_factory=dict)
    """A dictionary of specializations under this skill, with their base values."""

# --- NEW: Attribute Dataclass ---
@dataclass
class Attribute:
    """
    Holds data for a single attribute, including its nested skills.
    """
    base: int = 0
    """The base value (in pips) for the attribute."""
    skill: Dict[str, Skill] = field(default_factory=dict)
    """A dictionary of skills governed by this attribute."""

@dataclass
class Quality:
    """
    Describes the physical qualities and appearance of the entity.
    """
    body: str = ""
    """Description of the entity's body type."""
    eye: str = ""
    """Description of the entity's eye color or appearance."""
    gender: str = ""
    """The entity's gender identity or presentation."""
    hair: str = ""
    """Description of the entity's hair color, style, or type."""
    height: int = 0
    """The entity's height, (e.g., in inches)."""
    skin: str = ""
    """Description of the entity's skin color or texture."""
    age: str = ""
    """Description of the entity's age using keywords [ infant, child, juvenile, adult, mature, venerable, ancient ]."""

@dataclass
class Cost:
    """
    Defines the initial (one-time) and ongoing (per-turn) costs
    for an ability, spell, or item.
    """
    initial: List[Dict[str, Any]] = field(default_factory=list)
    """
    List of one-time costs.
    e.g., [{'cur_hp': 10}, {'item': 'mana_potion'}]
    """
    ongoing: List[Dict[str, Any]] = field(default_factory=list)
    """
    List of ongoing costs.
    e.g., [{'frequency': 'turn', 'length': 1, 'cur_mp': 5}]
    """

@dataclass
class DurationComponent:
    """
    Defines a single component of an effect's duration.
    """
    frequency: str = ""
    """The unit of time (e.g., 'turn', 'minute', 'scene')."""
    length: int = 0
    """The number of frequency units the effect lasts."""

@dataclass
class InventoryItem:
    """
    Represents a single item in the entity's inventory.
    Can now contain other items (e.g., a backpack).
    """
    item: str = ""
    """The name or unique ID of the item."""
    quantity: int = 0
    """The number of this item the entity possesses."""
    equipped: bool = False
    """Whether the item is currently equipped (if equippable)."""
    inventory: List[InventoryItem] = field(default_factory=list)
    """A nested list of items contained within this item."""
    note: Optional[str] = None
    """An optional note or description for the item."""

@dataclass
class Entity:
    """
    A comprehensive dataclass to represent any entity in the game.
    Updated to match the structure of character_schema.yaml.
    """

    name: str = ""
    """The entity's proper name."""

    supertype: str = ""
    """The primary type of the entity (e.g., 'creature')."""

    type: str = ""
    """The primary type of the entity (e.g., 'humanoid')."""

    subtype: str = ""
    """The subtype of the entity (e.g., 'human')."""

    body: str = ""
    """A description of the entity's body or form (e.g., 'humanoid')."""

    max_hp: int = 0
    """Maximum Health Points."""
    cur_hp: int = 0
    """Current Health Points."""
    max_fp: int = 0
    """Maximum Fatigue/Stamina Points."""
    cur_fp: int = 0
    """Current Fatigue/Stamina Points."""
    max_mp: int = 0
    """Maximum Magic/Mana Points."""
    cur_mp: int = 0
    """Current Magic/Mana Points."""

    exp: int = 0
    """Experience points held by the entity."""
    
    size: str = ""
    """Size of the entity, described by keyword [ fine, diminutive, tiny, small, medium, large, huge, gargantuan, colossal ]."""

    weight: float = 0.0
    """The entity's weight, typically in kg or lbs."""

    # --- MODIFIED: To use new Attribute dataclass ---
    attribute: Dict[str, Attribute] = field(default_factory=dict)
    """A dictionary of the entity's primary attributes and their values."""

    # --- REMOVED: 'skill' field. It now lives inside 'attribute'. ---
    # skill: Dict[str, Skill] = field(default_factory=dict)

    quality: Quality = field(default_factory=Quality)
    """An object holding the entity's physical descriptors."""

    status: List[Any] = field(default_factory=list)
    """A list of status tags, modifiers, etc. (e.g., 'intelligent', {'fire': -10})"""

    ally: Dict[str, Any] = field(default_factory=dict)
    """
    A hierarchical dictionary defining allies.
    e.g., {'type': [{'creature': {'subtype': [{'human': ...}]}}]}
    """

    enemy: Dict[str, Any] = field(default_factory=dict)
    """
    A hierarchical dictionary defining enemies.
    e.g., {'type': [{'creature': {'subtype': [{'orc': ...}]}}]}
    """

    attitude: Dict[str, Any] = field(default_factory=dict)
    """
    A hierarchical dictionary defining attitudes towards other entities.
    Keys can be 'default' (a string) or 'type' (a list).
    Values are strings of 5 comma-separated values.
    """

    language: List[str] = field(default_factory=list)
    """A list of languages the entity can speak or understand."""

    target: List[str] = field(default_factory=list)
    """A list of valid target types for this entity (if it's an action/item)."""

    resist: Dict[str, Dict[str, str]] = field(default_factory=dict)
    """
    A dictionary of resistance formulas.
    e.g., {'magic': {'pass': 'formula', 'fail': 'formula'}}
    """

    range: int = 0
    """The effective range of the entity (if an item/ability) or its senses."""

    proficiency: Dict[str, Any] = field(default_factory=dict)
    """
    A list of skill proficiencies.
    e.g., ['skill_name', {'skill_name_2': 1}]
    """
    
    apply: Dict[str, Any] = field(default_factory=dict)
    """A dictionary of 'apply' effects (e.g., {'fire': {'damage(cur_hp)': 'formula'}})."""
    
    requirement: Dict[str, Any] = field(default_factory=dict)
    """A dictionary of requirements to use/equip (e.g., {'intelligence': {'base': 3}})."""

    cost: Cost = field(default_factory=Cost)
    """An object holding the initial and ongoing costs."""

    duration: List[DurationComponent] = field(default_factory=list)
    """A list of duration components for an effect."""

    value: int = 0
    """The base monetary value of the entity (if an item) or its bounty."""

    slot: Optional[str] = None
    """The equipment slot this entity occupies (if an item)."""

    inventory: List[InventoryItem] = field(default_factory=list)
    """A list of items carried by the entity."""

    supernatural: List[str] = field(default_factory=list)
    """A list of supernatural powers or abilities."""

    memory: List[str] = field(default_factory=list)
    """A list of key memories as simple strings."""

    quote: List[str] = field(default_factory=list)
    """A list of memorable quotes the entity might say."""

def create_entity_from_dict(data: Dict[str, Any]) -> Entity:
    """
    Factory function to create an Entity from a dictionary.
    
    This correctly handles nested dataclasses like Skill, Qualities,
    Cost, Duration, and recursive InventoryItems.
    """
    
    data_copy = data.copy()

    if 'quality' in data_copy:
        data_copy['quality'] = Quality(**data_copy['quality'])
        
    if 'cost' in data_copy:
        data_copy['cost'] = Cost(**data_copy['cost'])
        
    if 'duration' in data_copy:
        data_copy['duration'] = [DurationComponent(**comp) for comp in data_copy['duration']]
        
    
    # --- MODIFIED: To build the hierarchical Attribute->Skill structure ---
    final_attributes: Dict[str, Attribute] = {}
    
    if 'attribute' in data_copy:
        for attr_name, attr_data in data_copy['attribute'].items():
            
            new_attr = Attribute() # Create the new Attribute object
            
            if isinstance(attr_data, dict):
                new_attr.base = attr_data.get('base', 0)
                
                # Check for nested skills
                if 'skill' in attr_data:
                    for skill_name, skill_data in attr_data['skill'].items():
                        if isinstance(skill_data, dict):
                            new_attr.skill[skill_name] = Skill(**skill_data)
                        else:
                            # Handle simple "skill_name: 3"
                            new_attr.skill[skill_name] = Skill(base=skill_data)
            else:
                # Handle simple "attribute_name: 9"
                new_attr.base = attr_data
            
            final_attributes[attr_name] = new_attr
        
        data_copy['attribute'] = final_attributes

    # --- REMOVED: The old logic for a separate 'skill' dictionary ---
    # (The logic for 'skill:' at the top level is removed, as it
    # should all be nested under attributes per the YAML schema)
    # ---
    
    def _create_inventory(items_list: List[Dict]) -> List[InventoryItem]:
        """Helper to recursively build InventoryItem objects."""
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
    """Dataclass for items in the room's legend."""
    char: str = ""
    entity: str = ""
    color: Optional[str] = None
    map_name: Optional[str] = None # For map tooltips, etc.
    is_player: bool = False
    inventory: List[Dict[str, Any]] = field(default_factory=list)
    pattern: Optional[List[List[str]]] = None

@dataclass
class Room:
    """Dataclass to hold room data from rooms.yaml."""
    name: str = ""
    description: str = ""
    scale: int = 1
    layers: List[List[List[str]]] = field(default_factory=list)
    legend: List[RoomLegendItem] = field(default_factory=list)

    # --- REMOVED: The confusing '@property def map' ---
    # The GUI will now iterate over 'layers' directly.

@dataclass
class Environment:
    """Dataclass to hold the environment (list of rooms)."""
    rooms: List[Room] = field(default_factory=list)

@dataclass
class Scenario:
    """Dataclass to hold the entire scenario from rooms.yaml."""
    scenario_name: str = ""
    environment: Environment = field(default_factory=Environment)


class RulesetLoader:
    """
    Loads all .yaml files from a specified ruleset directory,
    handling the 'entity:' prefix.
    """
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
        """Loads all YAML files from the ruleset directory."""
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
        """Helper to load all documents from a YAML file."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return list(yaml.safe_load_all(f))
        except Exception as e:
            print(f"Error loading YAML file {file_path}: {e}")
            return []

    def _load_scenario(self, file_path: Path):
        """Helper to load and parse the scenario/rooms.yaml file."""
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
                        # This **item will now correctly parse
                        # color and map_name if they exist
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
        """Retrieves a loaded character by name."""
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
    # Define placeholder classes to prevent immediate crashes
    class NLPProcessor:
        def __init__(self, *args): pass
        def process_player_input(self, *args): return None
    class ProcessedInput: pass
    class LLMManager: pass
    def process_player_actions(*args) -> List[Tuple[str, str]]:
        return [("Error: 'action_processor.py' not found.", "Error")]


# --- NEW: GameController class added ---

class GameController:
    """
    Manages the game state, player input, and game loop logic.
    This class is separate from the GUI.
    """

    def __init__(self, loader: RulesetLoader, ruleset_path: Path, llm_manager: LLMManager):
        """
        Initializes the game controller.
        
        Args:
            loader: A pre-initialized RulesetLoader instance.
            ruleset_path: Path to the ruleset for the NLPProcessor.
            llm_manager: The manager for handling LLM API calls.
        """
        self.loader = loader
        """The data loader with all ruleset data."""
        
        # This processor is still used for *intent classification*
        self.nlp_processor = NLPProcessor(ruleset_path)
        """The NLP system for processing commands."""
        
        # This manager is used for *generative responses*
        self.llm_manager = llm_manager
        """The manager for handling LLM API calls."""
        
        self.player_entity: Optional[Entity] = None
        """The main player character entity."""
        
        self.game_entities: Dict[str, Entity] = {}
        """A dictionary of all entities in the scene, indexed by name."""
        
        self.current_room: Optional[Room] = None
        """The currently active room object."""
        
        # Load entities from the loader
        self.game_entities.update(self.loader.creatures)
        self.game_entities.update(self.loader.characters)

        # (Placeholder) List of all entities in the current encounter
        self.initiative_order: List[Entity] = []
        
        # (Placeholder) Game history for narrative summaries
        self.round_history: List[str] = []
        
        # This is the LLM's chat history
        self.llm_chat_history: List[Dict[str, str]] = []

        # Callbacks to update the GUI
        self.update_narrative_callback: Callable[[str], None] = lambda text: None
        self.update_character_sheet_callback: Callable[[Entity], None] = lambda entity: None
        self.update_inventory_callback: Callable[[Entity], None] = lambda entity: None
        self.update_map_callback: Callable[[Optional[Room]], None] = lambda room: None

    def start_game(self, player: Entity):
        """
        Initializes the game, loads the player, and starts the loop.
        
        Args:
            player: The pre-loaded player Entity object.
        """
        self.player_entity = player
        if player.name not in self.game_entities:
            self.game_entities[player.name] = player
            
        if self.loader.scenario and self.loader.scenario.environment.rooms:
            self.current_room = self.loader.scenario.environment.rooms[0]
            print(f"Loaded initial room: {self.current_room.name}")
        else:
            print("Warning: No scenario or rooms found in loader.")
        
        # Build the initiative order from the entities placed in the room
        self.initiative_order = []
        
        if self.current_room and self.current_room.layers:
            # 1. Create a quick lookup map from char -> entity_name
            legend_lookup: Dict[str, str] = {}
            if self.current_room.legend:
                for item in self.current_room.legend:
                    legend_lookup[item.char] = item.entity

            # 2. Find all unique entity characters on the map (all layers)
            placed_chars = set()
            for layer in self.current_room.layers: # <-- MODIFIED
                for y, row in enumerate(layer):
                    for x, char_code in enumerate(row):
                        # 'x' is empty/transparent, skip it
                        if char_code != 'x': # <-- SIMPLIFIED
                            placed_chars.add(char_code)
            
            # 3. Get the Entity object for each placed character
            print("--- Loading Entities for Initiative ---")
            for char_code in placed_chars:
                entity_name = legend_lookup.get(char_code)
                if not entity_name:
                    print(f"Warning: Character '{char_code}' on map but not in legend.")
                    continue
                
                # Check for creature/player
                entity_obj = self.game_entities.get(entity_name)
                
                if not entity_obj:
                    # Check for environment entity (dummy, chest, wall, etc.)
                    entity_obj = self.loader.environment_ents.get(entity_name)
                    if entity_obj and entity_name not in self.game_entities:
                        # Add to game_entities for tracking
                        self.game_entities[entity_name] = entity_obj
                
                if entity_obj:
                    if entity_obj not in self.initiative_order:
                        self.initiative_order.append(entity_obj)
                        print(f"Added '{entity_name}' (char: '{char_code}') to initiative.")
                else:
                    print(f"Warning: Entity '{entity_name}' (char: '{char_code}') not found in any loader.")

        else:
            # Fallback if no room is loaded
            print("Warning: No room loaded, adding only player to initiative.")
            if self.player_entity:
                self.initiative_order = [self.player_entity]

        # Ensure player is always in the list (if they weren't placed via 'P')
        if self.player_entity and self.player_entity not in self.initiative_order:
            print(f"Warning: Player '{self.player_entity.name}' not placed on map, adding to initiative.")
            self.initiative_order.append(self.player_entity)
            
        print(f"Starting game with {len(self.initiative_order)} entities in initiative.")
        
        # Manually update GUI on start
        self.update_narrative_callback(f"The adventure begins for {player.name}...")
        self.update_character_sheet_callback(self.player_entity)
        self.update_inventory_callback(self.player_entity)
        self.update_map_callback(self.current_room)
        
        print("GameController started.")


    def process_player_input(self, player_input: str):
        """
        Receives raw text input from the InputBar and processes it
        using the hybrid parser model.
        """
        if not self.player_entity or not self.nlp_processor:
            return

        print(f"Processing input: {player_input}")
        
        # 1. Run the NLP Pipeline (for intent classification)
        # We pass all game_entities as the "known_entities" for the NER step
        # processed_action is now a 'ProcessedInput' dataclass
        processed_action = self.nlp_processor.process_player_input(
            player_input, 
            self.game_entities
        )
        
        if not processed_action:
            self.update_narrative_callback("Error: Could not process input.")
            return
        
        # --- REFACTORED LOGIC ---

        # 2. Triage & Process Action
        # This function now lives in 'action_processor.py'
        # It returns a list of (narrative_message, history_message) tuples
        action_results = process_player_actions(
            self.player_entity,
            processed_action,
            self.game_entities # Pass game entities for action logic
        )

        # 3. Update GUI and History
        
        # --- FIX: Do not use set() on unhashable Entity objects ---
        # The target list from NLP is already unique.
        targets_affected = processed_action.targets
        # --- END FIX ---

        player_action_summary = ""
        
        for narrative_msg, history_msg in action_results:
            self.update_narrative_callback(narrative_msg)
            self.round_history.append(history_msg)
            player_action_summary += history_msg + " "

        # --- NEW: Add player's action to LLM history ---
        self.llm_chat_history.append({"role": "user", "content": player_action_summary.strip()})
        
        # 4. Update GUI
        # Update any targets that were affected
        for target in targets_affected: # Iterating over the list is fine
            self.update_character_sheet_callback(target)
        self.update_character_sheet_callback(self.player_entity)
        self.update_inventory_callback(self.player_entity)
        
        # --- END REFACTORED LOGIC ---
        
        # 5. Trigger NPC turns
        # Pass the plain text summary of what the player did
        self._run_npc_turns(player_action_summary)

    def _run_npc_turns(self, player_action_summary: str):
        """
        Runs the 'else' block of the loop for all non-player characters.
        
        Args:
            player_action_summary: A simple text string of what the player did.
        """
        print("Running NPC turns...")
        if not self.player_entity: return
        
        all_actions_taken = False
        
        # Get the current game state
        game_state_context = self._get_current_game_state(self.player_entity)
        
        for npc in self.initiative_order:
            if npc == self.player_entity:
                continue 

            if not ("intelligent" in npc.status or "animalistic" in npc.status or "robotic" in npc.status):
                continue
            
            # 1. (LLM) Generate NPC Response/Reaction to player's action
            #    We no longer use nlp_processor.generate_npc_response
            
            # --- NEW: Use LLMManager ---
            # Create a prompt for the NPC
            npc_prompt = (
                f"You are {npc.name}. "
                f"You are in a room with: {game_state_context['actors_present']}. "
                f"The player, {self.player_entity.name}, just did this: '{player_action_summary}'. "
                f"What is your reaction or next action? Respond in character, briefly."
            )
            
            # Generate the response
            # Note: We pass the *shared* chat history
            reaction_narrative = self.llm_manager.generate_response(
                prompt=npc_prompt,
                history=self.llm_chat_history 
            )
            # --- END NEW ---
            
            if reaction_narrative:
                # Add NPC response to GUI and history
                # Check if LLM returned an error message
                if reaction_narrative.startswith("Error:"):
                    formatted_narrative = reaction_narrative
                else:
                    formatted_narrative = f"{npc.name}: \"{reaction_narrative}\""
                
                self.update_narrative_callback(formatted_narrative)
                self.round_history.append(formatted_narrative)
                # Add to LLM history so it knows what was said
                self.llm_chat_history.append({"role": "assistant", "content": reaction_narrative})
            
            all_actions_taken = True
        
        # (Rest of method is unchanged from the original file)

        # 5. (Placeholder) Process round updates (e.g., poison, regeneration)
        self._process_round_updates()
        
        # 6. (Placeholder) Generate narrative summary
        if all_actions_taken:
            # (Placeholder) summary = self.llm.get_narrative_summary("\n".join(self.round_history))
            summary = "The round ends." # Placeholder
            self.update_narrative_callback(f"\n--- Round Summary ---\n{summary}")
            self.round_history = [] # Clear history for next round

    def _get_current_game_state(self, actor: Entity) -> Dict[str, Any]:
        """(Helper) Gathers all context for an LLM prompt."""
        
        actors_in_room = [e.name for e in self.initiative_order if e.name != actor.name]
        
        # Format attitudes
        attitudes_str = "none"
        if actor.attitude:
            try:
                import json
                attitudes_str = json.dumps(actor.attitude) # Simple serialization
            except ImportError:
                pass
        
        objects_in_room = []
        # (Placeholder) This needs to be populated from the room/legend
        # if self.current_room and self.current_room.objects:
        #     objects_in_room = [obj.name for obj in self.current_room.objects]

        return {
            "actors_present": ", ".join(actors_in_room) if actors_in_room else "none",
            "objects_present": ", ".join(objects_in_room) if objects_in_room else "none",
            "attitudes": attitudes_str,
            "game_history": "\n".join(self.round_history)
        }

    def _process_round_updates(self):
        """(Placeholder) Processes end-of-round effects like poison, regen, etc."""
        pass