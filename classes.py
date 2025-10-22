#!/usr/bin/env python3

"""
Data models for TTRPG entities for an AI Dungeon Master.

This file defines the main 'Entity' dataclass along with all
necessary helper dataclasses to represent a character, monster,
or object in the game world, based on the character_schema.yaml.

Identity
name: The entity's proper name (e.g., "Valerius").
supertype: The entity's main category (e.g., "creature").
type: The entity's category (e.g., "humanoid").
subtype: The entity's specific sub-category (e.g., "human").
body: The entity's physical form (e.g., "humanoid").

Core Vitals & Stats
max_hp / cur_hp: Maximum and current Health Points.
max_fp / cur_fp: Maximum and current Fatigue/Stamina Points.
max_mp / cur_mp: Maximum and current Magic/Mental Points.
exp: Experience points available to be spent.

Attributes & Skills
attributes: A dictionary of core stats (e.g., physique: 9, intelligence: 9).
skills: A dictionary of learned abilities (e.g., blades, spellcraft) which can contain specializations.

Physicality & Location
area: The entity's footprint (e.g., ['X']).
weight: The entity's weight.
qualities: A sub-object holding physical descriptors like eyes, hair, height, and gender.

Relationships & Social
allies: A hierarchical dictionary defining which other entities are considered friendly.
enemies: A hierarchical dictionary defining which other entities are considered hostile.
attitudes: A hierarchical dictionary defining the entity's 5-point emotional disposition towards others.
languages: A list of languages the entity speaks or understands.

Combat & Mechanics
tags: A list of temporary status effects or keywords (e.g., "poisoned", "burning").
tag_mod: A dictionary of formulas explaining how tags modify stats (e.g., fire: "strength-10").
resist: A dictionary of formulas for resisting different types of effects (e.g., "magic").
range: The effective range of the entity's senses or abilities.
target: A list of valid target types if this entity is an action or spell.
proficiency: A list of skill proficiencies to be used if this entity is an object.
cost: A sub-object defining the initial and ongoing costs (HP, MP, items) for an ability.
duration: A list defining how long an effect lasts (e.g., in turns or scenes).

Inventory & Equipment
inventory: A list of items the entity is carrying. Items can contain their own nested inventory (e.g., a backpack).
value: The value of the entity or item.
slot: The equipment slot an item occupies (e.g., "head", "main-hand").

Roleplay & Flavor
supernatural: A list of special powers, spells, or miracles (e.g., "fireball").
memories: A list of key background memories to inform roleplay.
quotes: A list of common phrases or quotes the entity might say.

"""

# Allows InventoryItem to contain a list of itself
from __future__ import annotations
from dataclasses import dataclass, field, fields
from typing import List, Dict, Any, Optional, Union
from pathlib import Path
import yaml


@dataclass
class Skill:
    """
    Holds data for a single skill, including its specializations.
    Updated to match character_schema.yaml (base value only).
    """
    base: int = 0
    """The base value (in pips) for the skill."""
    specializations: Dict[str, int] = field(default_factory=dict)
    """A dictionary of specializations under this skill, with their base values."""


@dataclass
class Qualities:
    """
    Describes the physical qualities and appearance of the entity.
    """
    body: str = ""
    """Description of the entity's body type."""
    eyes: str = ""
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
    e.g., [{'fequency': 'turn', 'length': 1, 'cur_mp': 5}]
    """


@dataclass
class DurationComponent:
    """
    Defines a single component of an effect's duration.
    """
    fequency: str = ""  # Note: Typo 'fequency' matches original schema
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

    area: List[str] = field(default_factory=list)
    """The current location/area(s) of the entity (e.g., ['TownSquare'])."""

    weight: float = 0.0
    """The entity's weight, typically in kg or lbs."""

    attributes: Dict[str, int] = field(default_factory=dict)
    """A dictionary of the entity's primary attributes and their values."""

    skills: Dict[str, Skill] = field(default_factory=dict)
    """A dictionary of the entity's skills."""

    qualities: Qualities = field(default_factory=Qualities)
    """An object holding the entity's physical descriptors."""

    tags: List[str] = field(default_factory=list)
    """A list of arbitrary tags for game logic (e.g., ['Merchant', 'Undead'])."""

    tag_mod: Dict[str, str] = field(default_factory=dict)
    """
    A dictionary mapping tags to formulas that modify behavior.
    e.g., {'fire': 'strength-10'}
    """

    allies: Dict[str, Any] = field(default_factory=dict)
    """
    A hierarchical dictionary defining allies.
    e.g., {'type': [{'creature': {'subtype': [{'human': ...}]}}]}
    """

    enemies: Dict[str, Any] = field(default_factory=dict)
    """
    A hierarchical dictionary defining enemies.
    e.g., {'type': [{'creature': {'subtype': [{'orc': ...}]}}]}
    """

    attitudes: Dict[str, Any] = field(default_factory=dict)
    """
    A hierarchical dictionary defining attitudes towards other entities.
    Keys can be 'default' (a string) or 'type' (a list).
    Values are strings of 5 comma-separated values.
    """

    languages: List[str] = field(default_factory=list)
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

    memories: List[str] = field(default_factory=list)
    """A list of key memories as simple strings."""

    quotes: List[str] = field(default_factory=list)
    """A list of memorable quotes the entity might say."""


# --- YAML Loading Utilities ---

def create_entity_from_dict(data: Dict[str, Any]) -> Entity:
    """
    Factory function to create an Entity from a dictionary.
    
    This correctly handles nested dataclasses like Skill, Qualities,
    Cost, Duration, and recursive InventoryItems.
    """
    
    # Make a copy to avoid modifying the original dict
    data_copy = data.copy()

    # --- Handle nested dataclasses ---

    if 'qualities' in data_copy:
        data_copy['qualities'] = Qualities(**data_copy['qualities'])
        
    if 'cost' in data_copy:
        data_copy['cost'] = Cost(**data_copy['cost'])
        
    if 'duration' in data_copy:
        data_copy['duration'] = [DurationComponent(**comp) for comp in data_copy['duration']]
        
    if 'skills' in data_copy:
        data_copy['skills'] = {name: Skill(**s_data) for name, s_data in data_copy['skills'].items()}

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

    # --- Filter keys ---
    # Only pass keys to Entity that it actually expects
    entity_field_names = {f.name for f in fields(Entity)}
    filtered_data = {k: v for k, v in data_copy.items() if k in entity_field_names}

    return Entity(**filtered_data)


class RulesetLoader:
    """
    Loads all game data from YAML files in a specified ruleset directory.
    """
    
    def __init__(self, base_path: Path):
        """
        Initializes the loader with the path to the ruleset.
        
        Args:
            base_path: A Path object pointing to the ruleset
                       (e.g., .../rulesets/medievalfantasy)
        """
        self.base_path = base_path
        if not self.base_path.is_dir():
            raise FileNotFoundError(f"Ruleset directory not found: {self.base_path}")
            
        # Dictionaries to hold loaded data
        self.attributes: List[Dict[str, Any]] = [] # Updated to list for multi-doc
        self.skills: Dict[str, Any] = {}
        self.slots: Dict[str, Any] = {}
        self.tags: List[Dict[str, Any]] = []
        self.types: List[Dict[str, Any]] = []
        
        self.items: Dict[str, Entity] = {}
        self.creatures: Dict[str, Entity] = {}
        self.supernatural: Dict[str, Entity] = {}
        self.characters: Dict[str, Entity] = {}
        
        self.locations: Dict[str, Any] = {}
        self.lorebooks: Dict[str, Any] = {}
        
        print(f"RulesetLoader initialized for: {self.base_path}")

    def _load_yaml(self, file_path: Path) -> Any:
        """
        Helper to load a single YAML file.
        Handles both single and multi-document YAML files.
        If multi-document, returns a list. If single, returns the object.
        """
        if not file_path.is_file():
            print(f"Warning: YAML file not found: {file_path}")
            return None
        try:
            with open(file_path, 'r') as f:
                # Read the whole file content
                file_content = f.read()
                
            # Use safe_load_all to get a generator
            documents = list(yaml.safe_load_all(file_content))
            
            if not documents:
                return None
            elif len(documents) == 1:
                return documents[0] # Return the single document
            else:
                return documents # Return the list of documents
                
        except yaml.YAMLError as e:
            print(f"Error parsing YAML file {file_path}: {e}")
            return None
        except Exception as e:
            print(f"Error reading file {file_path}: {e}")
            return None

    def _load_entities_from_file(self, file_name: str) -> Dict[str, Entity]:
        """
        Loads a single file (or multi-doc file) containing
        a list of entity definitions. (e.g., items.yaml)
        """
        data = self._load_yaml(self.base_path / file_name)
        if not data:
            return {}
            
        entities = {}

        # --- FIX ---
        # Ensure 'data' is always a list to simplify iteration
        if isinstance(data, dict):
            # A single file with one entity definition
            data_list = [data]
        elif isinstance(data, list):
            # A multi-document file, already a list
            data_list = data
        else:
            print(f"Warning: {file_name} loaded as unexpected type: {type(data)}")
            return {}

        for entity_data in data_list:
            if not isinstance(entity_data, dict):
                print(f"Warning: Skipping non-dictionary item in {file_name}")
                continue
            
            # The entity's name is *inside* the dictionary
            entity_name = entity_data.get('name')
            if not entity_name:
                print(f"Warning: Skipping entity with no 'name' in {file_name}")
                continue
                
            try:
                entities[entity_name] = create_entity_from_dict(entity_data)
            except Exception as e:
                print(f"Error creating entity '{entity_name}' from {file_name}: {e}")
        # --- END FIX ---
            
        return entities
        
    def _load_entities_from_dir(self, dir_name: str) -> Dict[str, Entity]:
        """Loads all YAML files in a directory as separate entities."""
        entity_dir = self.base_path / dir_name
        if not entity_dir.is_dir():
            print(f"Warning: Entity directory not found: {entity_dir}")
            return {}
            
        entities = {}
        for yaml_file in entity_dir.glob("*.yaml"):
            entity_name_from_file = yaml_file.stem  # 'Valerius.yaml' -> 'Valerius'
            entity_data = self._load_yaml(yaml_file)
            
            if not entity_data:
                continue
            
            # Handle if file is multi-doc (returns list) or single (returns dict)
            if isinstance(entity_data, list):
                print(f"Warning: {yaml_file} is multi-document. Only loading first doc.")
                if not entity_data:
                    continue
                entity_data = entity_data[0] # Take only the first document
                
            if not isinstance(entity_data, dict):
                 print(f"Warning: {yaml_file} did not load as a dictionary. Skipping.")
                 continue

            try:
                # Use entity name from file, but also pass data (which might have 'name')
                entities[entity_name_from_file] = create_entity_from_dict(entity_data)
            except Exception as e:
                print(f"Error creating entity '{entity_name_from_file}' from {yaml_file}: {e}")
        return entities

    def load_all(self):
        """
        Loads all YAML files from the ruleset directory.
        """
        print("--- Loading all ruleset data ---")
        
        # Simple file loads (Fixed to use self.base_path)
        self.attributes = self._load_yaml(self.base_path / "attributes.yaml") or []
        self.skills = self._load_yaml(self.base_path / "skills.yaml") or {}
        self.slots = self._load_yaml(self.base_path / "slots.yaml") or {}
        self.tags = self._load_yaml(self.base_path / "tags.yaml") or []
        self.types = self._load_yaml(self.base_path / "type.yaml") or []
        
        # Entity file loads (uses _load_entities_from_file)
        self.items = self._load_entities_from_file("items.yaml")
        self.creatures = self._load_entities_from_file("creatures.yaml")
        self.supernatural = self._load_entities_from_file("supernatural.yaml")
        
        # Entity directory loads (uses _load_entities_from_dir)
        self.characters = self._load_entities_from_dir("characters")
        self.locations = self._load_entities_from_dir("locations") # Note: May not be Entities
        self.lorebooks = self._load_entities_from_dir("lorebooks") # Note: May not be Entities
        
        print(f"Loaded {len(self.attributes)} attribute definitions (from multi-doc).")
        print(f"Loaded {len(self.skills)} skills.")
        print(f"Loaded {len(self.items)} items.")
        print(f"Loaded {len(self.creatures)} creatures.")
        print(f"Loaded {len(self.characters)} characters.")
        print("--- Loading complete ---")

    def get_character(self, name: str) -> Optional[Entity]:
        """Retrieves a loaded character by name."""
        return self.characters.get(name)