"""!
@file schemas.py
@brief The declared shape of the event payloads that carry the most structure across the bus,
    as TypedDicts -- so a producer and its consumers share one written contract instead of a
    docstring each. Only the contract is here; nothing in production checks it. tests/ runs a
    validating EventBus (tests/event_contract.py) that checks every payload published during the
    suite against SCHEMAS (events/validate.py), and a static test that every key a consumer
    reads is one a schema declares -- the two halves of "a key that exists on only one side".

    Fields are optional unless marked Required: most payloads are built by several producers
    that each fill what they know. An unknown key is a failure either way, which is the point.
    Adding a field means adding it here, in the same change that starts producing it.
"""

from typing import Any, Required, TypedDict


class ItemInteractionResolved(TypedDict, total=False):
    """!
    @brief A diceless intent's result, published only by DMCore._publish_item_interaction
        (resolution/Item_Outcome.py builds it) -- what Narration_Prompts'
        item_interaction and the free-standing intents' narrate() turn into prose.
        The first block is common to every intent; the rest are the per-intent extras (a free-
        standing intent's own module attaches its own -- see intents/<name>.py's resolve()).
    """

    # -- common
    intent: Required[str]
    found: bool
    item_name: str | None
    input: str
    present_entities: list
    quiet: bool
    phrase: str | None
    reason: str
    # -- item intents
    container: str | None
    contents: list
    description: str
    revealed: list
    replaced: str | None
    replaced_with: str | None
    slot: str
    amount: int
    amount_text: str
    price: int | float
    price_text: str
    healed: int | dict
    poisoned: int
    remaining_hp: int
    charges_left: int
    # -- movement / travel / rest
    direction: str
    room_name: str
    room_description: str
    location_name: str
    location_description: str
    characters: list
    moved: list
    distance: float
    blocks_spent: int
    time: dict
    polity: str | None
    # -- party / mounts / language / lore
    members: list
    stance: str
    language: str
    target: str
    skill: str
    puller: str
    vehicle: str


class DialogueResolved(TypedDict, total=False):
    """!@brief Who the player addressed and how (DMCore._on_dialogue_detected) -- what Narration_Prompts' npc_dialogue narrates."""

    found: Required[bool]
    input: str
    reason: str
    target: str | None
    target_label: str
    utterance: str | None
    speech_form: str
    persona: str
    attitude: str
    language_barrier: bool
    target_language: str
    nonsense_phrase: str | None
    present_entities: list


class ItemClause(TypedDict, total=False):
    """!@brief One item-interaction clause of a turn (Intent_Classification.py)."""

    kind: Required[str]
    intent: Required[str]
    item_name: str | None
    phrase: str | None


class ActionClause(TypedDict, total=False):
    """!@brief One skill/ability clause of a turn."""

    kind: Required[str]
    skill: Required[str]
    score: float
    target: str | None


class TurnDetected(TypedDict):
    """!@brief What NLPCore resolved the player's line to: always a list of clauses, even for one action."""

    clauses: list
    input: str


# event name -> schema. A plain type means a non-dict payload (llm_response_ready is the narration text).
SCHEMAS: dict[str, Any] = {
    "item_interaction_resolved": ItemInteractionResolved,
    "dialogue_resolved": DialogueResolved,
    "turn_detected": TurnDetected,
    "llm_response_ready": str,
}

# turn_detected's own clauses, by their "kind".
CLAUSE_SCHEMAS = {"item": ItemClause, "action": ActionClause}
