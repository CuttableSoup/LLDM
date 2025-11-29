"""
models.py

This module defines the core data classes used throughout the game, including entities,
rooms, interactions, and narrative history events.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Union

@dataclass
class GameTime:
    """Represents the in-game time. Year is stored separately to avoid overflow."""
    year: int = 2001
    total_seconds: int = 0  # Seconds since the beginning of the current year
    
    # Constants for time conversion
    SECONDS_PER_MINUTE = 60
    SECONDS_PER_HOUR = 3600
    SECONDS_PER_DAY = 86400
    SECONDS_PER_MONTH = 2592000  # 30 days
    SECONDS_PER_YEAR = 31104000  # 12 months

    def __init__(self, year: int = 1, month: int = 1, day: int = 1, hour: int = 0, minute: int = 0, second: int = 0, total_seconds: Optional[int] = None):
        self.year = year
        if total_seconds is not None:
            self.total_seconds = total_seconds
        else:
            self.total_seconds = (
                (month - 1) * self.SECONDS_PER_MONTH +
                (day - 1) * self.SECONDS_PER_DAY +
                hour * self.SECONDS_PER_HOUR +
                minute * self.SECONDS_PER_MINUTE +
                second
            )
        self._normalize()

    def _normalize(self):
        while self.total_seconds >= self.SECONDS_PER_YEAR:
            self.total_seconds -= self.SECONDS_PER_YEAR
            self.year += 1

    @property
    def month(self) -> int:
        return (self.total_seconds // self.SECONDS_PER_MONTH) + 1

    @property
    def day(self) -> int:
        return ((self.total_seconds % self.SECONDS_PER_MONTH) // self.SECONDS_PER_DAY) + 1

    @property
    def hour(self) -> int:
        return ((self.total_seconds % self.SECONDS_PER_DAY) // self.SECONDS_PER_HOUR)

    @property
    def minute(self) -> int:
        return ((self.total_seconds % self.SECONDS_PER_HOUR) // self.SECONDS_PER_MINUTE)

    @property
    def second(self) -> int:
        return self.total_seconds % self.SECONDS_PER_MINUTE

    def advance_time(self, seconds: int = 1):
        """Advances the game time by a specified number of seconds."""
        self.total_seconds += seconds
        self._normalize()

    def set_time(self, year: int = 1, month: int = 1, day: int = 1, hour: int = 0, minute: int = 0, second: int = 0):
        """Sets the game time to a specific date and time."""
        self.year = year
        self.total_seconds = (
            (month - 1) * self.SECONDS_PER_MONTH +
            (day - 1) * self.SECONDS_PER_DAY +
            hour * self.SECONDS_PER_HOUR +
            minute * self.SECONDS_PER_MINUTE +
            second
        )
        self._normalize()

    def get_time_string(self) -> str:
        return f"Year {self.year}, Month {self.month}, Day {self.day}, Hour {self.hour:02d}:00"

    def copy(self) -> GameTime:
        return GameTime(year=self.year, total_seconds=self.total_seconds)

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
        self.memory.append(event)

    def get_recent_history(self, count: int = 10) -> List[HistoryEvent]:
        return self.memory[-count:]

    def get_summary_for_llm(self) -> str:
        summary_lines = [f"--- Key Memories for {self.entity_name} ---"]
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
    height: str = ""
    skin: str = ""
    age: str = ""
    material: str = ""

@dataclass
class DurationComponent:
    frequency: str = ""
    length: Any = 0
    timestamp: int = 0

@dataclass
class Magnitude:
    """Represents the magnitude calculation for an effect."""
    source: str = "none"       # user, target, self, none
    reference: str = "none"    # skill, attribute, level, none
    value: Any = 0             # The specific stat name or raw value
    pre_mod: int = 0           # Static modifier added before calculation
    type: str = "static"       # static, roll, value

@dataclass
class Effect:
    """Represents an effect applied by an interaction."""
    name: str = "" 
    magnitude: Optional[Magnitude] = None 
    duration: Optional[DurationComponent] = None
    entity: Optional[str] = None 
    apply: Optional[str] = None 
    inventory: Optional[Dict[str, Any]] = None
    parameters: Dict[str, Any] = field(default_factory=dict)

@dataclass
class Requirement:
    """Represents a requirement for an interaction."""
    type: str = "test" 
    # For tests:
    test: Optional[Dict[str, Any]] = None
    difficulty: Optional[Union[int, Dict[str, Any]]] = None
    pass_effect: Optional[List[Effect]] = None 
    fail_effect: Optional[List[Effect]] = None 
    # For logic/other:
    sub_requirements: List['Requirement'] = field(default_factory=list)
    # For simple checks:
    name: Optional[str] = None
    relation: Optional[Any] = None

@dataclass
class Interaction:
    """Represents an active use or ability."""
    type: str = "" 
    description: str = ""
    target_effect: List[Effect] = field(default_factory=list)
    user_effect: List[Effect] = field(default_factory=list)
    self_effect: List[Effect] = field(default_factory=list)
    target_requirement: List[Requirement] = field(default_factory=list)
    user_requirement: List[Requirement] = field(default_factory=list)
    self_requirement: List[Requirement] = field(default_factory=list)
    range: int = 0

@dataclass
class Trigger:
    """Represents an automatic event."""
    frequency: str = ""
    length: Any = "*"
    timestamp: Optional[Any] = None
    target_effect: List[Effect] = field(default_factory=list)
    user_effect: List[Effect] = field(default_factory=list)
    self_effect: List[Effect] = field(default_factory=list)
    target_requirement: List[Requirement] = field(default_factory=list)
    user_requirement: List[Requirement] = field(default_factory=list)
    self_requirement: List[Requirement] = field(default_factory=list)

@dataclass
class InventoryItem:
    item: str = ""
    quantity: int = 0
    equipped: bool = False
    inventory: List[InventoryItem] = field(default_factory=list)
    note: Optional[str] = None

@dataclass
class Cost:
    mp: int = 0
    fp: int = 0
    hp: int = 0
    item: List[Dict[str, Any]] = field(default_factory=list)

@dataclass
class Entity:
    """A generic representation of any object or character in the game world."""
    name: str = ""
    supertype: str = "" 
    type: str = ""
    subtype: str = ""
    description: str = ""
    
    # Vitals
    max_hp: int = 0
    cur_hp: int = 0
    max_fp: int = 0
    cur_fp: int = 0
    max_mp: int = 0
    cur_mp: int = 0
    
    # Physicality & Stats
    exp: int = 0
    total_exp: int = 0
    size: str = ""
    weight: float = 0.0
    value: int = 0
    bulk: int = 0
    
    # Complex Fields
    attribute: Dict[str, Attribute] = field(default_factory=dict)
    quality: Quality = field(default_factory=Quality)
    status: List['Entity'] = field(default_factory=list)
    ally: List[Dict[str, Any]] = field(default_factory=list)
    enemy: List[Dict[str, Any]] = field(default_factory=list)
    attitude: List[Dict[str, Any]] = field(default_factory=list)
    language: List[str] = field(default_factory=list)
    target: List[str] = field(default_factory=list)
    proficiency: Dict[str, Any] = field(default_factory=dict)
    
    # Interactions & Abilities
    interaction: List[Interaction] = field(default_factory=list)
    ability: List[Interaction] = field(default_factory=list)
    trigger: List[Trigger] = field(default_factory=list)
    
    cost: Cost = field(default_factory=Cost)
    duration: List[DurationComponent] = field(default_factory=list)
    slot: List[str] = field(default_factory=list)
    
    # Inventory
    inventory: List[InventoryItem] = field(default_factory=list)
    inventory_rules: List[Dict[str, Any]] = field(default_factory=list) 
    
    # Movement & World
    x: int = 0
    y: int = 0
    move: Dict[str, int] = field(default_factory=dict)
    passable: Dict[str, int] = field(default_factory=dict)
    
    # Narrative
    memory: List[str] = field(default_factory=list)
    quote: List[str] = field(default_factory=list)
    
    # Other
    parameter: Dict[str, Any] = field(default_factory=dict)

# --- Map & Environment Classes ---

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
