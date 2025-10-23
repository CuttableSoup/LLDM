from __future__ import annotations
from dataclasses import dataclass, field, fields
from typing import List, Dict, Any, Optional, Union
from pathlib import Path

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

    weight: float = 0.0
    """The entity's weight, typically in kg or lbs."""

    attribute: Dict[str, int] = field(default_factory=dict)
    """A dictionary of the entity's primary attributes and their values."""

    skill: Dict[str, Skill] = field(default_factory=dict)
    """A dictionary of the entity's skills."""

    quality: Quality = field(default_factory=Quality)
    """An object holding the entity's physical descriptors."""

    tag: List[str] = field(default_factory=list)
    """A list of arbitrary tags for game logic (e.g., ['Merchant', 'Undead'])."""

    tag_mod: Dict[str, str] = field(default_factory=dict)
    """
    A dictionary mapping tags to formulas that modify behavior.
    e.g., {'fire': 'strength-10'}
    """

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
        
    if 'skill' in data_copy:
        data_copy['skill'] = {name: Skill(**s_data) for name, s_data in data_copy['skill'].items()}

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

    return Entity(**filtered_data)