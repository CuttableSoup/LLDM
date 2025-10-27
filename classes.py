from __future__ import annotations
from dataclasses import dataclass, field, fields
from typing import List, Dict, Any, Optional, Union
from pathlib import Path

# NEW IMPORTS
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

    weight: float = 0.0
    """The entity's weight, typically in kg or lbs."""

    attribute: Dict[str, int] = field(default_factory=dict)
    """A dictionary of the entity's primary attributes and their values."""

    skill: Dict[str, Skill] = field(default_factory=dict)
    """A dictionary of the entity's skills."""

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

    proficiency: List[Union[str, Dict[str, int]]] = field(default_factory=list)
    """
    A list of skill proficiencies.
    e.g., ['skill_name', {'skill_name_2': 1}]
    """

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
    
    # Make a copy to avoid modifying the original dict
    data_copy = data.copy()

    if 'quality' in data_copy:
        data_copy['quality'] = Quality(**data_copy['quality'])
        
    if 'cost' in data_copy:
        data_copy['cost'] = Cost(**data_copy['cost'])
        
    if 'duration' in data_copy:
        data_copy['duration'] = [DurationComponent(**comp) for comp in data_copy['duration']]
        
    # --- START MODIFICATION ---
    # Removed the 'if 'skill' in data_copy:' wrapper from this block.
    # This logic must run to parse the nested attribute/skill structure
    # from files like Valerius.yaml.
    
    # Handle new structure: {'physique': {'base': 9, 'skill': {...}}}
    # The Entity dataclass expects: {'physique': 9}
    # And skill: {'strength': {'base': 3, ...}}
    
    # This function seems to be designed for the *final* entity structure,
    # but the YAML structure for attributes/skills is nested.
    # Let's adjust for the YAML structure seen in Valerius.yaml
    
    final_attributes = {}
    final_skills = {}
    
    if 'attribute' in data_copy:
        for attr_name, attr_data in data_copy['attribute'].items():
            if isinstance(attr_data, dict):
                final_attributes[attr_name] = attr_data.get('base', 0)
                if 'skill' in attr_data:
                    for skill_name, skill_data in attr_data['skill'].items():
                        if isinstance(skill_data, dict):
                            final_skills[skill_name] = Skill(**skill_data)
                        else:
                            # Handle simple skill: val
                            final_skills[skill_name] = Skill(base=skill_data)
            else:
                # Handle simple attr: val
                final_attributes[attr_name] = attr_data
        
        data_copy['attribute'] = final_attributes
    
    if 'skill' in data_copy:
         for skill_name, skill_data in data_copy['skill'].items():
            if isinstance(skill_data, dict):
                final_skills[skill_name] = Skill(**skill_data)
            else:
                final_skills[skill_name] = Skill(base=skill_data)

    # Only overwrite skills if we found some in attributes
    if final_skills:
        data_copy['skill'] = final_skills
        
    # --- END MODIFICATION ---


    # Handle recursive inventory
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
    
    # Auto-populate cur_hp, cur_mp, cur_fp if they are 0
    if 'max_hp' in filtered_data and 'cur_hp' not in filtered_data:
        filtered_data['cur_hp'] = filtered_data['max_hp']
    if 'max_mp' in filtered_data and 'cur_mp' not in filtered_data:
        filtered_data['cur_mp'] = filtered_data['max_mp']
    if 'max_fp' in filtered_data and 'cur_fp' not in filtered_data:
        filtered_data['cur_fp'] = filtered_data['max_fp']

    return Entity(**filtered_data)


# --- NEW CLASSES ADDED BELOW ---

@dataclass
class RoomLegendItem:
    """Dataclass for items in the room's legend."""
    char: str = ""
    entity: str = ""
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

    @property
    def map(self) -> Optional[List[List[str]]]:
        """
        Returns the object/actor layer of the map.
        GUI.py's MapPanel expects this.
        In rooms.yaml, layer[0] is ground, layer[1] is objects/actors.
        """
        if len(self.layers) > 1:
            return self.layers[1] # Return the second layer
        elif self.layers:
            return self.layers[0] # Fallback to first layer
        return None

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
            
            # Special handling for rooms.yaml
            if yaml_file.name == "rooms.yaml":
                self._load_scenario(yaml_file)
                continue
            
            # Special handling for attributes.yaml and types.yaml
            if yaml_file.name == "attributes.yaml":
                self.attributes = self._load_generic_yaml_all(yaml_file)
                continue
            if yaml_file.name == "types.yaml":
                self.types = self._load_generic_yaml_all(yaml_file)
                continue

            # Load all other files as entities
            entities_data = self._load_generic_yaml_all(yaml_file)
            
            for entity_data in entities_data:
                if not isinstance(entity_data, dict) or 'entity' not in entity_data:
                    print(f"Warning: Skipping document in {yaml_file.name} (missing 'entity:' tag).")
                    continue
                
                # Extract data from under the 'entity:' key
                data = entity_data['entity']
                
                if 'name' not in data:
                    print(f"Warning: Skipping entity in {yaml_file.name} (missing 'name').")
                    continue
                
                entity_obj = create_entity_from_dict(data)
                
                # Sort entity into the correct dictionary
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
                    # Fallback for entities not in a sub-directory
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

            # --- START MODIFICATION ---
            # Get data from under the top-level 'map:' key
            map_data = data.get('map', {})
            if not map_data:
                print(f"Warning: 'map:' key not found in {file_path.name}. Skipping scenario load.")
                return

            # Manually parse the scenario data into dataclasses
            env_data = map_data.get('environment', {}) # Get environment from map_data
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
                scenario_name=map_data.get('name', 'Unnamed Scenario'), # Get name from map_data
                environment=parsed_env
            )
            # --- END MODIFICATION ---
            print(f"Successfully loaded scenario: {self.scenario.scenario_name}")

        except Exception as e:
            print(f"Error loading scenario file {file_path}: {e}")

    def get_character(self, name: str) -> Optional[Entity]:
        """Retrieves a loaded character by name."""
        return self.characters.get(name)
