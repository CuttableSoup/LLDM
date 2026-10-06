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
speak_language, rest, move, travel, mount, dismount, hitch, unhitch, and lore_check. Resolved
with no dependency on the scene target or the locked-container gate, unlike every item-named
intent. lore_check is the one member that actually rolls dice — every other member is free
because it's diceless; it's exempted from the ordinary turn pipeline by deliberate design
instead, so recalling monster lore mid-fight never costs a turn.
_Avoid_: scene intent, movement intent (move/travel are only two of the thirteen).

**Legal record**:
One polity's standing file on one identity — the bounty, acclaim and charges that polity holds
against whoever the identity is. Only a crime committed inside that polity changes it, and it
never decays.
_Avoid_: rap sheet, criminal record, wanted status.

**Pending report**:
A crime witnessed by someone other than an enforcer, not yet filed against the offender's legal
record. It is filed when time next passes, provided a witness is still alive.
_Avoid_: queued crime, unfiled crime.

**Arrest confrontation**:
The open demand an enforcer has made of the player, awaiting one of the five replies (pay,
surrender, bribe, bluff, resist). It is announced only once the turn that provoked it has fully
resolved.
_Avoid_: pending arrest (the implementation's own name), arrest scene.

**Save slot**:
One named save, made of three independent parts — the game state, the narrator's memory, and
the Notes tab — each written and restored by whoever owns it.
_Avoid_: save file, save game.

**Scene target**:
The entity a non-free-standing item-interaction intent implicitly acts against when no item
name resolves it otherwise — the current combat target if one exists, else the first
non-player entity present. Gates access to a locked or closed container.
_Avoid_: current target (reserved for the combat target specifically), target_name (the
implementation's own parameter name).
