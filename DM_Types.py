"""!
@file DM_Types.py
@brief Typing-only Protocol describing the state and cross-mixin methods that DMCore's
    domain mixins (DM_Combat.py, DM_Inventory.py, DM_Persistence.py, DM_Rules.py,
    DM_Social.py, DM_Status.py) rely on but don't themselves define -- each is actually set
    up by DMCore.__init__ (DM_Core.py) or implemented by exactly one other mixin. Every
    mixin inherits from DMCoreProtocol so type checkers (Pylance/pyright) can resolve
    dm_core.<attr> across the whole composed class without each mixin file re-declaring the
    same attribute independently, which is what triggers pyright's
    reportIncompatibleVariableOverride/reportIncompatibleMethodOverride -- two unrelated
    sibling mixins both declaring, say, `event_bus: EventBus` looks to pyright like one
    overriding the other with an incompatible type, even though the types match. A single
    shared declaration site fixes that structurally.

    This has zero runtime effect: Protocol subclasses impose no behavior, and none of these
    stub method bodies (`...`) ever actually run -- Python's MRO always finds the real
    implementation on whichever concrete mixin defines it first, before it would ever reach
    this shared base. See DM_Core.py's class docstring for how the mixins compose.
"""

from typing import Protocol

from Event_Bus import EventBus


class DMCoreProtocol(Protocol):
    event_bus: EventBus
    skills: dict
    entities: dict
    scenario: dict
    scenario_entities: list
    rules: dict
    round_number: int
    current_target: str | None
    scenario_key: str
    player_name: str

    # Cross-mixin methods -- each actually implemented by exactly one mixin (see that
    # mixin's own file for the real body); declared here once so every other mixin calling
    # across to it type-checks without re-declaring the signature itself.
    def loot_entity(self, from_name, to_name) -> dict: ...
    def apply_damage(self, entity_name, amount) -> int: ...
    def entity_matches_requirements(self, entity_name, requirements) -> bool: ...
    def describe_character(self, entity_name, toward_name=None) -> str: ...
    def _choose_combat_target(self) -> str | None: ...
    def load_rules(self, rules_dir) -> None: ...
    def load_scenario_definition(self, scenario_name) -> None: ...
    def load_scenario(self) -> None: ...
    def _describe_scenario_characters(self) -> list: ...
    def get_current_hp(self, entity_name) -> int: ...
    def get_band(self, entity_name) -> int: ...
    def get_distance_between(self, entity_a, entity_b) -> int: ...
    def is_in_range(self, attacker_name, defender_name, ability) -> bool: ...
