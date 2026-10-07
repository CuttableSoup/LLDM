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

**Item interaction outcome**:
The result of one item-interaction intent as narration receives it — the common fields (intent,
item name, the player's input and phrase, found/reason, who is present, quiet) plus whatever the
intent adds. Built in one place however the intent resolved: a direct turn, a resumed downtime,
or an improvised beat.
_Avoid_: resolved payload, interaction result.

**Structured decision**:
One LLM call constrained to a tool schema, answering with one accepted choice and its arguments,
a decline, or "unavailable" (the model never answered). Callers shape the result; none of them
parse tool calls or handle the transport themselves.
_Avoid_: tool call (that is the wire format), LLM query.

**Calendar**:
The block clock turned into a date — day, month, year — by the setting's `[[calendar_month]]`
table, or a bare "day N" when the setting authors none. Read-only: it never advances time.
_Avoid_: date system, clock (the block clock is the counter this reads, not this).

**World map**:
Where a grid point is — which region, terrain, polity and road — and what that means for the
party's speed and whether the route is passable. Read-only: moving the party is travel, not this.
_Avoid_: map data, geography.

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

**Action target**:
The entity a skill or ability action is aimed at, resolved from the NLP-matched name, the
player's wording (an ordinal, "the other one", a pronoun) and the conversation partner. Decides
whether the action is an assault and whether the reading is too weak to start a fight unasked.
Neighbours it is not: Scene target (the default for item intents) and Entity reference (what the
text literally names).
_Avoid_: victim (the implementation's own word; implies assault), current target (reserved for
the combat target).

**Ability resolution**:
What an actor's ability does once its roll has landed — damage, summon, dispel, cure, teleport,
spell materials and its own on_pass/on_fail program — as one implementation shared by the
player's turn and every other entity's combat turn. It takes an already-resolved target; deciding
who an ability points at stays with the caller.
_Avoid_: attack resolution (an ability need not attack), effect application.

**Entity reference**:
Which entities a line of player text literally names — by key, display name or alias, whole-word.
Exact matching only; fuzzy matching of an address phrase is the classifier's job.
_Avoid_: target resolution (that also picks defaults and fallbacks), name matching.

**Conveyance**:
The graph an entity's mount field draws (rider on horse, cart hitched to a team) and what is
asked of it: capacity sums across a team, speed is the slowest link, terrain passability is the
union.
_Avoid_: mount system, vehicle.
