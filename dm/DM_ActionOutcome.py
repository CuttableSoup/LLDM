"""!
@file DM_ActionOutcome.py
@brief The typed result of one resolved player/creature action -- what DM_Core.py's
    _on_turn_detected (and DM_Crafting.py's _try_craft_action, DM_Combat.py's
    resolve_behavior_action) hand to LLM_Core.py's _describe_outcome for narration, replacing
    the untyped, ad hoc "result" dict every one of those producers used to bolt a new optional
    key onto (see CLAUDE.md's own "Action resolution pipeline" for where this sits).

    A true tagged union, not one dataclass with a pile of optional fields: exactly one
    ActionOutcome variant is created per resolved action, each carrying only the fields that
    variant actually needs. RolledOutcome (the only variant that can *succeed*) carries a
    list of Effect subtypes instead of a fixed set of optional attachments -- a new kind of
    on-hit consequence (this codebase has added four in five recent commits: loot, summon,
    craft, reveal) is a new Effect subtype the narrator's own formatter registry dispatches
    on, not a new field every existing ActionOutcome instance carries unused.

    LanguageBarrierOutcome is OutOfRangeOutcome's own shape reused for a different pre-roll
    gate -- a language_dependent ability/skill (DM_Combat.py's _ability_requires_language)
    against a target the player's own current_language isn't shared with, same "can't do it,
    don't roll" precedent (see DM_Core.py's _resolve_roll).

    Deliberately data-only -- no formatting logic lives here. LLM_Core.py's own
    _describe_outcome owns turning one of these into narration text (see CONTEXT.md's
    "ActionOutcome"/"Effect" entries for the vocabulary, kept independent of this module's
    own implementation).

    Scope: only the action_resolved/round_resolved pipeline (an ordinary skill/ability use,
    a craft attempt, or a creature's own behavior-driven turn). item_interaction_resolved
    (examine/take/give/equip/...) is a genuinely separate seam with its own producers
    (DM_Inventory.py/DM_Movement.py/DM_Dialogue.py) and consumer
    (generate_item_interaction_response) -- its own "reason" vocabulary is untouched by this.
"""

from dataclasses import dataclass, field


@dataclass
class DamageEffect:
    """!@brief Damage dealt by a landed hit -- from calculate_damage's own result dict."""
    defender: str
    net_damage: int
    remaining_hp: int


@dataclass
class LootEffect:
    """!@brief Currency/items gained -- from apply_test_outcome's own "loot" key."""
    currency: int
    items: list


@dataclass
class SummonEffect:
    """!@brief A temporary ally conjured by a successful summoning cast."""
    name: str


@dataclass
class CraftEffect:
    """!@brief The item finished by a successful craft roll."""
    item_name: str


@dataclass
class RevealEffect:
    """!@brief Tags revealed by a passed [entity.test] with a truthy "reveal" key."""
    tags: list


@dataclass
class DefenderDetailsEffect:
    """!@brief Defender flavor text (describe_character), attached belt-and-suspenders."""
    text: str


Effect = DamageEffect | LootEffect | SummonEffect | CraftEffect | RevealEffect | DefenderDetailsEffect


@dataclass
class RolledOutcome:
    """!@brief An ordinary roll actually happened -- the only variant that can succeed and
        carry Effects. Covers a plain skill/ability use, an item test, and a craft attempt
        alike (opposing_skill/defender are None for an untargeted or flat-difficulty roll)."""
    entity: str
    skill: str | None
    roll: int
    difficulty: int
    success: bool
    defender: str | None = None
    opposing_skill: str | None = None
    effects: list = field(default_factory=list)
    input: str | None = None


@dataclass
class OutOfRangeOutcome:
    """!@brief The target was too far away for this weapon/ability to reach -- no roll."""
    entity: str
    skill: str | None
    defender: str | None
    input: str | None = None


@dataclass
class LanguageBarrierOutcome:
    """!@brief A language_dependent ability/skill needs a shared language and the player's own
        current_language isn't one target_name knows -- no roll."""
    entity: str
    skill: str | None
    defender: str | None
    input: str | None = None


@dataclass
class MissingSpellMaterialsOutcome:
    """!@brief A named ability's own "materials" weren't fully present -- no roll."""
    entity: str
    skill: str | None
    input: str | None = None


@dataclass
class NotCraftableOutcome:
    """!@brief item_name has no [entity.craft] block at all -- no roll."""
    entity: str
    item_name: str
    input: str | None = None


@dataclass
class MissingStationOutcome:
    """!@brief item_name's own requires_station has no matching entity present -- no roll."""
    entity: str
    item_name: str
    station: str
    input: str | None = None


@dataclass
class MissingMaterialsOutcome:
    """!@brief The player doesn't have item_name's own required materials on hand -- no roll."""
    entity: str
    item_name: str
    materials: list
    input: str | None = None


@dataclass
class MovementOutcome:
    """!@brief A creature's own behavior-driven turn was a deliberate move (fleeing, or
        choosing to close distance) rather than an attack -- enemy-turn only; the player's
        own advance/retreat never reaches this pipeline at all (see CLAUDE.md's "Multiple
        actions")."""
    entity: str
    direction: str
    opponent: str | None
    before: int
    after: int


def rolled_outcome_from_roll(roll, effects=None, input_text=None):
    """!
    @brief Builds a RolledOutcome from resolve_action/resolve_opposed_action's own plain
        {entity, skill, roll, difficulty, success, defender?, opposing_skill?} return dict --
        those two stay untyped (DM_Rules.py's hidden-notice check uses the same raw dict for
        an unrelated bool check that has nothing to do with narration), so every narration-
        facing call site converts one layer up instead.
    @param roll The plain roll dict from resolve_action/resolve_opposed_action.
    @param effects An optional pre-built list of Effects (ex: a via_test roll's own loot/damage).
    @param input_text The player's raw turn input, if this is the player's own action.
    @return A RolledOutcome carrying the same roll/success data, typed.
    """
    return RolledOutcome(
        entity=roll["entity"],
        skill=roll["skill"],
        roll=roll["roll"],
        difficulty=roll["difficulty"],
        success=roll["success"],
        defender=roll.get("defender"),
        opposing_skill=roll.get("opposing_skill"),
        effects=effects if effects is not None else [],
        input=input_text,
    )


ActionOutcome = (
    RolledOutcome
    | OutOfRangeOutcome
    | LanguageBarrierOutcome
    | MissingSpellMaterialsOutcome
    | NotCraftableOutcome
    | MissingStationOutcome
    | MissingMaterialsOutcome
    | MovementOutcome
)
