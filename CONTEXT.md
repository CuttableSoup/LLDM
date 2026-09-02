# LLDM

An autonomous dungeon master: free-text player actions resolve through a data-driven D6 engine
and get narrated by a local LLM. See [CLAUDE.md](CLAUDE.md) for the module map.

## Language

**Item-interaction intent**:
A free-text action that resolves deterministically, with no dice roll — the "examine/take/
give/trade/use/equip/unequip/drop/open/close/advance/retreat/formation/speak_language/rest/
move/travel" family `DMCore._on_item_interaction_detected` dispatches. Splits into two groups:
the item-named intents (examine, take, give, trade, use, equip, unequip, drop, open, close),
which resolve against a named item or the current scene target; and free-standing intents,
below.
_Avoid_: item intent, diceless action.

**Free-standing intent**:
An item-interaction intent that acts on the scene, the party, or the block clock directly,
rather than a named item — advance, retreat, formation_behind, formation_abreast,
speak_language, rest, move, and travel. Resolved with no dependency on the scene target or the
locked-container gate, unlike every item-named intent.
_Avoid_: scene intent, movement intent (move/travel are only two of the eight).

**Scene target**:
The entity a non-free-standing item-interaction intent implicitly acts against when no item
name resolves it otherwise — the current combat target if one exists, else the first
non-player entity present. Gates access to a locked or closed container.
_Avoid_: current target (reserved for the combat target specifically), target_name (the
implementation's own parameter name).
