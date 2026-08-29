# Skill & Entity Effect Language — Design Doc

Status: revised design (2026-08-29) — supersedes the first implementation pass (2026-08-28),
whose code is being discarded in favor of the architecture described here. The interpreter
itself (`Program_Interpreter.py`, `Social_Resolution.py`, the op registry, the entity-lifecycle
attachment points) is unchanged and will be rebuilt as-is; what changed is *how a skill check's
own pass/fail effect gets attached* — see "Universal (untrained) abilities" for what changed and
why (short version: a flat, one-program-per-skill `[[skill_program]]` table couldn't express a
skill with more than one named use, ex: `trip`/`disarm`/`sunder` all rolled against `athletics`).
Not yet (re)implemented. Additive to the existing engine — nothing here changes behavior for a
skill/ability/entity that doesn't opt in. Module shape and rollout order below were settled via
an architecture-grilling pass against the actual codebase — see "Module shape" and "Rollout".

## Problem

Of the 36 skills in `Rules/Fantasy/skills.toml`, only 14 (`blades`, `axes`, `polearms`,
`brawling`, `missiles`, `arcane`, `finesse`, `acrobatics`, `reflexes`, `dodge`, `appraise`,
`medicine`, `observation`, `strength`) ever change game state on success or failure — damage via
`calculate_damage`/`apply_damage`, an `[entity.test]` outcome via `apply_test_outcome`, a craft
result via `place_new_item`, initiative ordering, or the `strength_damage` bonus formula. The
other 22 are fully reachable through `IntentClassifier`/`resolve_action`/`resolve_opposed_action`
but their `result["success"]` is only ever consumed by `LLMCore` for narration.

The same gap exists one level up, on entities themselves. An entity's only ways to react to
something happening to it are the fixed mechanisms already in the engine: `[[status]]`
(trigger + flat requirements + one condition to apply), `[entity.test]` (flat pass/fail table),
`[[entity.behavior]]` (choose one named ability/movement action), and `[[location.encounter]]`
(a location's own weighted roll on entry). Each is a closed, single-purpose shape. There's no way
today for, say, an item to do something specific when it's equipped, or a creature to do
something specific when it crosses a damage threshold, without writing a new Python branch —
exactly the scaling problem the skill-side gap has, just at the entity level instead.

## Goal

Let any skill check's pass/fail outcome, *and* any entity's own lifecycle moment (taking damage,
dying, being interacted with, ticking over a round, entering a scene), declare effects in data,
composed from one small closed set of primitives — without adding a new Python branch per skill
or per entity behavior. One interpreter, reused everywhere a `{condition} → {effect}` shape is
needed, is what actually makes this a "freeform data-driven engine" feature rather than one more
special case bolted onto the skill system.

## Non-goals

- **Not a textual language with a parser.** Every program is TOML data. This project
  deliberately never hands a small local LLM (Gemma via Ollama) free-form code to generate —
  `NPC_Generation.py`/`AdHoc_Generation.py` constrain every LLM output to an enum for exactly
  this reliability reason, and this design keeps that precedent (see "LLM generation").
- **Not a replacement for `[[status]]`, `[entity.test]`, `[[entity.behavior]]`, or
  `[[location.encounter]]`.** All four keep working exactly as they do today, and each stays the
  *preferred* tool for what it's already good at (see "Choosing a mechanism," below). This
  language is for the cases those flat, single-purpose shapes can't express — a conditional
  chained onto an effect, or an entity reacting to something none of the four existing
  mechanisms cover at all.
- **Not Turing-complete.** No loops, no variables, no user-defined functions beyond the named
  program catalog described below.

## Surface syntax

A program is an ordered list of **steps**. TOML's array-of-tables idiom *is* that list — no
`sequence`/`steps` wrapper needed:

```toml
[[entity]]
name = "intimidate"
skill = [ "intimidation" ]

[[entity.on_pass]]
do = "attitude"
entity = "target"
toward = "actor"
event = "intimidated"
magnitude = "actor.roll_margin"

[[entity.on_pass]]
if = "target.threat < -50"
then = { do = "condition", entity = "target", name = "shaken", duration = "scene" }
```

A single-step program skips the array and is just one inline table:

```toml
on_fail = { do = "attitude", entity = "target", toward = "actor",
            event = "failed_intimidation", magnitude = 0.3 }
```

**Step shapes.** A step is either an *action* (`do = "<op>"`, plus that op's own args) or a
*conditional* (`if = "<expr>"`, `then = <step or [steps]>`, `else = <step or [steps]>` optional).
`then`/`else` nest the same two shapes, so a branch can run more than one action without needing
a separate "sequence" keyword — nesting an array *is* sequencing.

**Condition expressions.** The common case is one comparison, written as a single string:
`"<role>.<field> <op> <literal>"` — ex: `"target.threat < -50"`,
`"target.has_condition:identified == false"`. `<op>` is one of `COMPARATORS`'s existing eight
(`>`, `<`, `>=`, `<=`, `==`, `!=`, `in`, `not_in`); `<field>` is anything
`Combat_Resolution.get_comparable_value` already resolves (`hp_per_remain`,
`has_condition:<name>`, `distance_to_target`, a plain entity attribute, ...) — every derived
field this project has already built works here for free, no new resolution code (see "Module
shape" for how `Program_Interpreter.py` reuses this directly rather than reimplementing it).

`entity_matches_requirements`'s flat list is AND-only; the one gap that motivates going beyond a
single string is boolean combination, so `if` also accepts a table instead of a string:
`{ all = ["expr", "expr", ...] }`, `{ any = [...] }`, `{ none = [...] }` — still just lists of
the same one-line comparison strings, no separate boolean-expression tree needed.

**Value references.** Anywhere a step expects a value (`magnitude`, `dice`, a literal), a bare
`"<role>.<field>"` string (no comparator) is resolved the same way — ex: `magnitude =
"actor.roll_margin"` above.

This compiles to the exact same small set of ops as a nested-dict tree would; the array-of-tables
and one-line comparison strings are purely surface sugar over `do`/`if`/`then`/`else`, kept
because TOML already has good idioms for "list of things" and "a labeled comparison," and reusing
them means an author never nests more than one level of curly braces by hand.

### Op reference

Every op resolves through a **pure** function — never through a `self`-reading DMCore mixin
method — so `Program_Interpreter.py` (see "Module shape") stays callable from both DMCore
mixins and other pure modules alike:

| `do` | args | resolves via | status |
|---|---|---|---|
| `condition` | `{entity, name, duration, dismiss?}` | `Combat_Resolution.apply_condition` | proven, to rebuild |
| `dismiss_condition` | `{entity, name}` | `Combat_Resolution.dismiss_condition` | proven, to rebuild |
| `attitude` | `{entity, toward, event, magnitude}` | `Social_Resolution.nudge_attitude_from_event` | proven, to rebuild |
| `damage` | `{entity, dice, pips, bonus, tags}` | `Combat_Resolution.calculate_damage` → `apply_damage` | proven, to rebuild |
| `heal` | `{entity, dice, pips, bonus}` | `Combat_Resolution.apply_healing` | proven, to rebuild |
| `transfer_item` | `{from, to, item}` | `Inventory_Resolution.transfer_item` | deferred |
| `transfer_currency` | `{from, to, amount?}` | `Inventory_Resolution.transfer_currency` | deferred |

"Status" tracks the actual rollout (see "Rollout"): `condition`/`dismiss_condition`/`attitude`/
`damage`/`heal` all worked correctly in the discarded first pass and are being rebuilt unchanged
— see this doc's own "Status" line up top, the op registry itself isn't what's being revised.
`transfer_item`/`transfer_currency` are still deferred — no authored program needs them yet, and
building them would mean extracting `Inventory_Resolution.py` (`DM_Inventory.py`'s
`transfer_item`/`transfer_currency`) ahead of a real caller, which "no op ahead of a caller"
rules out for now.

There is deliberately no `reveal` op. `apply_test_outcome`'s own `reveal` (`DM_Status.py`) is
already exactly `apply_condition(entity, "identified", duration="permanent")` — a program
expresses the same thing with the `condition` op already in the table above, so `reveal` would
be a second name for something `condition` already does.

`entity`/`toward`/`from`/`to` accept the reserved role tokens `actor`/`target`, resolved from
the evaluation context — the same reserved-placeholder convention `PLAYER_PLACEHOLDER` already
establishes for `"player"`.

## Evaluation context

Every program runs against `ctx = {actor, target, ...trigger-specific extras}`. Exactly two
roles exist, everywhere, on purpose — adding a third (`self`, `source`, ...) per trigger type
would mean the interpreter has to know which trigger it's running under just to resolve a
field reference. Instead, **which entity `target` means changes by attachment point, and that
mapping is fixed and documented per trigger** (table below) — the interpreter itself never
changes. `actor.<field>`/`target.<field>` resolve to `None` when that role doesn't apply to the
current trigger (ex: no `actor` on a round-upkeep tick), matching the existing convention where
derived fields already resolve to `None` under inapplicable conditions rather than erroring.

`actor`/`target` are always entity-name strings, never resolved entity dicts — matching every
existing pure function's own convention (an `entity_name: str` param plus a separate `entities`
dict) rather than a resolve-once-then-pass-a-dict shape.

## Module shape

`run_program(node, ctx, entities, rules, event_bus)` is a **pure** function in a new module,
`Program_Interpreter.py` — not a DMCore mixin. This is a deliberate correction from an earlier
draft of this doc, which proposed a `self`-reading mixin (`DM_SkillPrograms.py`): a `self`-based
interpreter can't be called from `Combat_Resolution.py`'s own `apply_damage`/`apply_healing` (see
"Attachment points" — `on_damage`/`on_heal` fire from inside those functions, which are
themselves pure and take no `self` at all), and it can only be unit-tested through a full
`DMCore`, the opposite of what "one interpreter, reused everywhere" is supposed to buy.

Pure modules calling other pure modules is already an established pattern here, not a novelty:
`Combat_Resolution.py` imports and calls `Challenge_Rating.skill_rating`; `NPC_Generation.py`
imports and calls `Challenge_Rating`'s own functions too; `AdHoc_Generation.py` imports and calls
`NPC_Generation.fit_skills_to_cr`. `Program_Interpreter.py` sits in that same graph: it's called
*from* `Combat_Resolution.py` (for `on_damage`/`on_heal`) exactly the way `Challenge_Rating.py`
already is, and it *calls into* `Combat_Resolution.py` itself for two things — the `condition`/
`dismiss_condition` ops, and reusing `entity_matches_requirements`/`get_comparable_value` (see
"Choosing a mechanism") to evaluate every `if` condition, rather than reimplementing
`COMPARATORS`/field-resolution a second time. A one-line condition string parses into the same
`{field, operator, value}` shape `entity_matches_requirements` already consumes; `{all=[...]}`/
`{any=[...]}`/`{none=[...]}` is boolean nesting *around* that existing AND-only primitive, not a
parallel implementation of it.

Op dispatch is a small dict registry (`OP_HANDLERS = {"condition": _op_condition, ...}`), not an
if/elif chain — adding an op later is one function plus one registry entry. Dispatch is strictly
on `do`/`if`, never on "whichever keys happen to be present" (unlike `apply_test_outcome`'s
existing dispatch, which is safe only because its outcome tables never nest — see the `reveal`
note above).

**Error handling is split by kind.** A structurally malformed step — an unknown `do` name, a
missing required arg — raises immediately: this is new, authored code, not legacy TOML, and it
matches `load_scenario_definition`'s own "fatal on purpose" precedent for an unknown scenario
name. A step whose *entity reference* doesn't resolve at evaluation time (ex: `entity = "target"`
with no target in this `ctx`) is a quiet no-op instead, matching the existing convention that a
derived field resolves to `None` under inapplicable conditions rather than erroring.

`Program_Interpreter.py` gets direct, bare-dict unit tests — no `EventBus`/`DMCore` — the same
seam `Combat_Resolution.py` was already built for but, in practice, is only ever exercised
through a full `DMCore` fixture today (`test_unit.py`'s combat/status test classes). The
interpreter's own tests are the one place in this feature where that pure seam actually has to
be entered directly, or the "pure and testable" argument for building it this way doesn't cash
out.

## Prerequisite: pure cores for attitude and transfer

`Combat_Resolution.py` already gives combat/status logic the pure-core treatment; `DM_Social.py`
and `DM_Inventory.py` never got it. `nudge_attitude_from_event` (`DM_Social.py`) and
`transfer_item`/`transfer_currency` (`DM_Inventory.py`) are still `self`-reading DMCore mixin
methods, each with real gating logic the `attitude`/`transfer_item`/`transfer_currency` ops need
to respect (a dead/tableless/unknown-event no-op for the former; a missing-entity/missing-item
no-op for the latter) — logic a pure `Program_Interpreter.py` can't reach unless it's pulled out
first.

Two new pure modules, `Social_Resolution.py` and `Inventory_Resolution.py`, extract exactly
those three methods (not a fuller pass over either mixin) into the same shape
`Combat_Resolution.py` already uses; `DM_Social.py`/`DM_Inventory.py`'s own methods become thin
wrappers in the same change, not a temporary duplicate left to drift. Each gets its own direct,
bare-dict tests, same commitment as `Program_Interpreter.py` itself.

Sequencing: `Social_Resolution.py` is needed before the `attitude` op ships (v1). `Inventory_Resolution.py`
can wait until `transfer_item`/`transfer_currency` are actually wired to a real attachment point —
matching the op table's own "no op ahead of a caller" rule above.

## Attachment points

| Attachment | Where | `actor` is | `target` is |
|---|---|---|---|
| `[entity.test].on_pass`/`.on_fail` | alongside the test's existing flat `pass`/`fail` | the checking entity | the entity carrying the test |
| ability `on_pass`/`on_fail` | on a weapon/spell/technique/talent/maneuver table — either *owned* (named in the checking entity's own `abilities` list) or *universal* (named in some `[[skill]]`'s own `abilities` list — see "Universal (untrained) abilities") | the checking entity | whoever it's checked against |
| `[entity.on_damage]` | on the entity itself | whoever dealt the damage (or absent, ex: a trap) | **the entity itself** |
| `[entity.on_death]` | on the entity itself | the killer, if known | **the entity itself** |
| `[entity.on_round_upkeep]` | on the entity itself | absent | **the entity itself** |
| `[entity.on_interact.<intent>]` | on the entity itself, one per intent (`examine`/`take`/`equip`/`use`/...) | the interacting entity (usually the player) | **the entity itself** |
| `[entity.on_enter]` | on the entity itself | absent | **the entity itself** |

There is deliberately no separate "bare skill, no ability" attachment point anymore — see
"Universal (untrained) abilities" for why a flat `[[skill_program]]` table (the first draft of
this row) was replaced by ordinary abilities that any entity may use without owning them. The
first two rows are skill-outcome attachment points; the last five are entity-lifecycle ones — an
entity declares them on its own template, and they run from the same places the engine already
visits that entity:

- `on_damage`/(`on_heal`, symmetrically) call `Program_Interpreter.run_program` directly from
  inside `Combat_Resolution.py`'s own `apply_damage`/`apply_healing`, right alongside their
  existing `evaluate_statuses` call — pure-to-pure, not lifted up to a DMCore wrapper (see
  "Module shape").
- `on_round_upkeep` is called from `DM_Status.py`'s `run_round_upkeep` wrapper, alongside its
  existing per-condition upkeep loop — this one *is* a `self`-reading DMCore mixin method
  already, so it just passes `self.entities`/`self.rules`/`self.event_bus` through explicitly.
- `on_interact.<intent>` concentrates at the single shared funnel every intent already resolves
  through — the `resolved(...)` closure inside `DM_Core.py`'s `_on_item_interaction_detected`,
  right before its one `item_interaction_resolved` publish call. One insertion point, not one per
  intent resolver. Runs *after* the intent's built-in resolution, *only on success* — a program
  that denies an interaction outright is out of scope for now. `ctx` stays `{actor, target}`; no
  extra outcome fields (item name, price, ...) are threaded through until a real authored program
  needs one by name.
- `on_enter` is called from `DM_Rules.py`'s `_enter_location`, alongside its existing
  `[[location.encounter]]` roll.
- `on_death` is **deferred**, not part of this rollout — see "Open questions."

## Universal (untrained) abilities

The first pass at closing the "22 dead skills" gap used a flat `[[skill_program]]` table in
`rules.toml`, keyed one-to-one by bare skill name (`next(p for p in ... if p.get("skill") ==
skill_name)` — the first entry sharing a skill name wins, silently, forever). That shape breaks
down the moment a skill has more than one *named* use. `trip`, `disarm`, and `sunder` are all
combat maneuvers rolled against `athletics` — but a tabletop character doesn't need special
training to attempt any of them; they're generally available options, not learned techniques.
One program per skill can't represent three independent maneuvers, and there's no way to make
one skill-wide `if`/`then` chain distinguish "the player tried to trip" from "the player tried to
sunder" — `ctx` only ever carries entity names, never *which situation* (which maneuver, which
object) the roll represents.

`[[skill_program]]` is removed as a standalone concept. In its place: `[[skill]]`'s own table
(`skills.toml`) gains an `abilities` field — a plain list of ability *names*. Each name resolves
to a standalone `[[entity]]` defined elsewhere (ex: a new `Rules/Fantasy/maneuvers.toml`,
mirroring `techniques.toml`'s existing shape for `cleave`) — the skill's own list never inlines
the ability's definition, it just marks membership. Naming an ability under a skill this way
means **any** entity may use it, no ownership check at all — the same "you don't have to be
trained to try" a real combat maneuver already gets at a tabletop.

```toml
# skills.toml
[[skill]]
name = "athletics"
...
untrained = [ "trip", "disarm", "sunder" ]
```

```toml
# maneuvers.toml -- a standalone catalog entry, same shape techniques.toml already uses for
# cleave; just not referenced from any particular entity's own "untrained" list.
[[entity]]
name = "trip"
supertype = "technique"
subtype = "maneuver"
description = "A leg sweep that knocks an opponent off their feet."
skill = [ "athletics" ]
range = 0
on_pass = { do = "condition", entity = "target", name = "prone", duration = "scene" }
```

This is additive, not a replacement for the existing *owned*-ability path (an entity's own
`abilities` list — `cleave`/`fireball`/an inline `punch`). An entity can still have its own
specific, trained ability for a skill, and owning one keeps taking precedence exactly as it does
today: `resolve_named_ability`'s existing entity-ownership check runs before this new
skill-listed fallback, so a character with their own bespoke version of a skill's effect isn't
overridden by the generic universal one.

A skill's own `abilities` list is purely a *membership* marker, not a routing rule — it says
nothing about which skill an ability actually rolls with. That's still the ability's own `skill`
field, exactly as `cleave`'s `skill = ["blades", "axes"]` already works today. The two are
independent: an ability can legally be listed under more than one skill's own `abilities` field
(ex: a maneuver usable via either `strength` or `blades`) while its own `skill` field
independently declares what it actually rolls against.

**Resolution.** `resolve_named_ability(entity_name, ability_name)` gains a fallback: if
`ability_name` isn't found in `entity_name`'s own `abilities` list, check whether it's listed
under any `[[skill]]`'s own `abilities` field — if so, resolve it via
`self.entities.get(ability_name)` regardless of who "owns" it. (Implementation detail: a flat set
of every name appearing in any skill's own `abilities` list, built once alongside `self.skills`
at load time, keeps this a cheap membership check rather than a per-turn scan over every loaded
skill.) This only ever succeeds on an exact name match — if NLP only matched the bare skill name
(the player's phrasing was too vague to name a specific maneuver), nothing here fires, and the
roll proceeds as an ordinary skill check with no attached effect. There's no principled way to
guess which of `trip`/`disarm`/`sunder` a vague "I try an athletics maneuver" meant, so this
deliberately never guesses.

**NLP embedding stops gating on `supertype` entirely.** The first pass only embedded
`technique`/`spell`-supertype entities as nameable actions — which also meant an *inline* ability
like `punch` (a one-off table nested inside `gladstone`'s own `abilities` list, never a top-level
`entities_data` entry at all) was never embedded, regardless of supertype. The replacement source
is the union of two scans, both resolved via `self.entities.get(name)`, neither checking
`supertype` at all:
1. Every name appearing in any entity's own `abilities` list (owned/trained — `cleave`,
   `fireball`, an inline `punch`).
2. Every name appearing in any `[[skill]]`'s own `abilities` list (universal — `trip`, `disarm`,
   `sunder`).

Each resolved ability contributes the same name/description/keyword phrases the embedding matcher
already builds for a skill or a technique/spell — no separate matching logic, still one flat
embedding space, so a player naming a maneuver directly (`"I trip him"`) can resolve straight to
it the same way naming `cleave` already does.

**The equipped-weapon/owned-ability lookup and the post-roll on_pass/on_fail lookup merge into
one method**, dropping the `"damage_value" in ability` gate the first pass's `find_attack_ability`
enforced — equipped weapon first, then the acting entity's own `abilities`, same priority as
today. This method deliberately does **not** also scan skill-listed universal abilities — that
ambiguity (multiple maneuvers, one skill) is exactly what `resolve_named_ability`'s own
exact-name-match fallback above exists to avoid guessing at. A universal ability only ever
becomes `named_ability` by being matched *by name*, never discovered as a side effect of "the
player rolled `athletics` and something matching happened to exist."

**`is_in_range` is unchanged.** `ability.get("range", 0)` — absent means melee, full stop, for
every ability regardless of whether it deals damage. No special-casing for a "this one's
non-physical" exception. A universal or owned ability with no explicit `range` (a hypothetical
conversational `charm`/`intimidate`) is melee-gated by default, same as a weapon — an author who
wants a skill's universal use reachable beyond melee has to say so explicitly (`range = <n>`),
never rely on an implicit "social checks are always reachable" carve-out. `trip`/`disarm`/
`sunder` want the melee default anyway, so `range = 0` above is written out for clarity rather
than left to the default to supply silently.

## Choosing a mechanism

Not everything belongs in a program. Keep using the flat, purpose-built mechanism when it
already fits:

- **One derived condition, no branching, no side effects beyond applying/dismissing it** →
  `[[status]]`. This is still the right tool for `stunned`/`wounded`/`severe` and anything like
  them.
- **Picking one action out of a priority list based on simple requirements** →
  `[[entity.behavior]]`. Still the right tool for combat AI.
- **A location-scoped random roll on entry** → `[[location.encounter]]`. Still the right tool
  for ambient population.
- **A flat difficulty check with one pass outcome and one fail outcome, no conditionals** →
  `[entity.test]`'s own `pass`/`fail` tables. Only reach for `on_pass`/`on_fail` alongside it
  when the outcome needs a branch the flat table can't express.
- **A skill's success should have a mechanical effect at all** → an ability, not a program of its
  own. If it's one universal effect anyone can attempt (no training implied), list it under the
  skill's own `abilities` field (see "Universal (untrained) abilities"); if it's a character's
  own specific, trained version, put it in that entity's own `abilities` list instead — either
  way, the effect itself is the ability's own `on_pass`/`on_fail`, this language's ordinary
  attachment point, just discovered by one of those two paths.
- **Anything else** — an item that does something specific when equipped, a creature that
  changes behavior after crossing a damage threshold — that's what this language is for.

## Worked examples

**Intimidation** — closing the "22 dead skills" gap, as a universal ability rather than a
skill-keyed program:

```toml
# skills.toml
[[skill]]
name = "intimidation"
...
abilities = [ "intimidate" ]
```

```toml
# maneuvers.toml
[[entity]]
name = "intimidate"
supertype = "technique"
subtype = "maneuver"
skill = [ "intimidation" ]

[[entity.on_pass]]
do = "attitude"
entity = "target"
toward = "actor"
event = "intimidated"
magnitude = "actor.roll_margin"

[[entity.on_pass]]
if = "target.threat < -50"
then = { do = "condition", entity = "target", name = "shaken", duration = "scene" }

on_fail = { do = "attitude", entity = "target", toward = "actor",
            event = "failed_intimidation", magnitude = 0.3 }
```

Step 1 always nudges the target's attitude, scaled by how well the roll succeeded. Step 2 reads
a *fresh* `threat` value — fresh because step 1 already ran and `target.threat` re-derives from
`get_attitude` live rather than caching — so a strong enough intimidation check both moves the
ongoing attitude number and, if that push crosses `-50`, applies an immediate `"shaken"`
condition for the scene. Any entity may attempt this (it's listed under the skill, not owned by
anyone) — a player saying "I intimidate him" resolves `named_ability` to `"intimidate"` directly
via NLP's own name/keyword match, or via `resolve_named_ability`'s skill-list fallback if only
the bare skill matched. Three things worth calling out honestly: `"intimidated"`/
`"failed_intimidation"` aren't real events yet (someone still authors matching
`[[attitude_event]]` deltas, same as `combat_hit`/`theft`/`favor`/`shared_enemy`); `roll_margin`
isn't already a 0..1 value the way `nudge_attitude_from_event`'s `magnitude` param expects — this
example assumes that gets normalized before landing (see "Open questions"); and `intimidate` has
no explicit `range`, so it defaults to melee-only (`is_in_range`'s unconditional `range = 0`
default) — intended here (a shouted threat from across the room doesn't land the same way), but
worth remembering as the default for *every* ability, not something intimidation opts into.

**Trip, disarm, sunder** — the case a flat `[[skill_program]]` couldn't express at all, and the
reason this design changed. All three are combat maneuvers rolled against `athletics`; none
require training:

```toml
# skills.toml
[[skill]]
name = "athletics"
...
abilities = [ "trip", "disarm", "sunder" ]
```

```toml
# maneuvers.toml
[[entity]]
name = "trip"
supertype = "technique"
subtype = "maneuver"
skill = [ "athletics" ]
range = 0
on_pass = { do = "condition", entity = "target", name = "prone", duration = "scene" }

[[entity]]
name = "disarm"
supertype = "technique"
subtype = "maneuver"
skill = [ "athletics" ]
range = 0
on_pass = { do = "condition", entity = "target", name = "disarmed", duration = "scene" }

[[entity]]
name = "sunder"
supertype = "technique"
subtype = "maneuver"
skill = [ "athletics" ]
range = 0
on_pass = { do = "condition", entity = "target", name = "sundered_weapon", duration = "permanent" }
```

"I trip him"/"I try to disarm him"/"I sunder his blade" each resolve NLP to a *different* one of
these three by name (their own distinct description/keywords, not just `athletics`'s), so each
gets its own independent `on_pass`. A vague "I use athletics on him" matches none of the three by
name and falls through to a plain, effect-less skill roll — no arbitrary pick between them. Note
`disarmed`/`sundered_weapon`/`prone` all need their own `[[condition]]` entries with real
modifiers (same as `shaken`/`enraged` elsewhere) to have actual teeth, not just narrated flavor.

**The cursed dagger, actually cursed** — an entity-level trigger. Today, `items.toml`'s cursed
dagger has a `"cursed"` tag that's flavor-only: per the existing `reveal`/tags convention, the
tag is read back by narration once identified, but nothing mechanical happens on equip. An
`on_interact` program is what would make it a real curse:

```toml
[entity.on_interact.equip]
if = "target.has_condition:identified == false"
then = { do = "condition", entity = "actor", name = "cursed", duration = "permanent" }
```

Here `target` is the dagger itself (the entity the program lives on); `actor` is whoever just
equipped it. Equipping an *unidentified* cursed dagger now actually curses the wearer — the
identify check (`items.toml`'s existing `[entity.test]`, `skill = ["arcane"]`) becomes something
worth doing before you put the thing on, not just flavor text.

**A troll's temper** — reacting to damage without a new Python branch:

```toml
# The troll already regenerates every round unless burned (rules.toml's "regenerating" upkeep).
# This adds: dropping under half HP without having been burned first makes it lash out harder.
[entity.on_damage]
if = { all = ["target.hp_per_remain < 0.5", "target.has_condition:enraged == false"] }
then = { do = "condition", entity = "target", name = "enraged", duration = "scene" }
```

`enraged` would need its own `[[condition]]` entry (a `modifier` bonus to damage/skill dice, say)
to have real teeth — same pattern as `"shaken"` above, and the same honest gap: this design lets
you *trigger* a condition from an arbitrary boolean combination of entity state; authoring what
that condition actually does is unchanged, ordinary `[[condition]]` work.

## LLM generation

Ad hoc/procedural content (`AdHoc_Generation.py`, `NPC_Generation.py`, and any future
scenario/encounter generator) must never ask an LLM to emit a raw program — a small local model
reliably authoring even the friendlier `do`/`if`/`then` surface is the same failure mode this
project has already designed around twice (`generate_npc_stats` constrains keyword choice to an
enum; `generate_ad_hoc_item`/`generate_ad_hoc_creature` constrain every field the same way).
Instead:

- Hand-authored TOML (`skills.toml`, `maneuvers.toml`/`techniques.toml`, entity templates,
  scenario files, `rules.toml`) writes programs directly — no LLM involved, no reliability
  concern. This covers every worked example above.
- Skill-outcome programs already live on named, hand-authored ability entities (universal ones
  in `maneuvers.toml`, owned ones on whichever entity template) — no separate catalog needed for
  those; an LLM tool call that wants to grant a generated NPC a combat maneuver picks an ability
  **by name** from that existing catalog (an enum exactly like `npc_keywords.toml`'s archetype
  choice), the same way it would pick any other real ability, never by generating one. Only the
  entity-lifecycle triggers (`on_damage`, `on_interact.<intent>`, ...) still want a dedicated
  curated catalog (`Rules/Fantasy/entity_programs.toml`) for the same reason, since those aren't
  ability entities at all — an ad hoc item/creature's `on_interact`/`on_damage` field, if ever
  exposed to generation, would be constrained to `program_ref = "<catalog name>"`, not a
  freeform table.

## Rollout

1. `Program_Interpreter.py`: the pure `do`/`if` engine plus the string-expression parser
   (`"<role>.<field> <op> <literal>"`), delegating structural condition evaluation to
   `Combat_Resolution.py`'s existing `entity_matches_requirements`/`get_comparable_value`. Ships
   with `condition`/`dismiss_condition`/`attitude` only. Direct, bare-dict unit tests — no
   EventBus/DMCore — the same pure-logic test seam `Intent_Classification.py` already uses, and
   the one this feature actually has to enter through (see "Module shape").
2. Extract `Social_Resolution.py` (`nudge_attitude_from_event`), needed for the `attitude` op
   above; rewrite `DM_Social.py`'s own method as a thin wrapper in the same change. Direct tests
   here too. (`Inventory_Resolution.py` waits until step 4b, below.)
3. Wire ability `on_pass`/`on_fail` first, using only `condition`/`dismiss_condition`/`attitude`
   — closes the "22 dead skills" gap with no changes to `apply_test_outcome`. This step now
   folds in what used to be its own separate step 3 (a `[[skill_program]]` table) and part of
   step 4 (ability tables), since both paths converge on the exact same attachment point:
   - Merge `find_attack_ability` and the post-roll ability re-derivation into one method, no
     `"damage_value"` gate (see "Universal (untrained) abilities").
   - Add `[[skill]]`'s own `abilities` field and `resolve_named_ability`'s skill-list fallback.
   - Stop gating NLP's ability embedding on `supertype`; embed the union of every entity's own
     `abilities` list and every skill's own `abilities` list instead.
   - Leave `is_in_range` untouched — `range` absent still means melee, unconditionally, for
     every ability.
4. Add `on_pass`/`on_fail` to `[entity.test]` tables too (the ability-table half of this is now
   folded into step 3 above). This is where `damage`/`heal` actually join the op registry (they
   have no caller before this step).
   - 4b. If/when a program needs `transfer_item`/`transfer_currency`, extract
     `Inventory_Resolution.py` the same way step 2 extracted `Social_Resolution.py`.
5. Add the entity-lifecycle attachment points one at a time, each wired from the single existing
   call site that already visits that entity at the right moment (see "Attachment points"):
   `on_damage`/`on_heal` (pure-to-pure, from inside `Combat_Resolution.py`), `on_round_upkeep`
   (from `DM_Status.py`'s wrapper), `on_interact.<intent>` (the one shared funnel in
   `_on_item_interaction_detected`), `on_enter` (from `_enter_location`). `on_death` is excluded
   from this rollout entirely — see "Open questions."
6. Author one real example per category in existing content: `intimidate` (universal, under
   `intimidation`) and `trip`/`disarm`/`sunder` (universal, under `athletics`, demonstrating more
   than one maneuver sharing a skill), the cursed dagger's `on_interact.equip`, and one
   `on_damage` example (the troll) — each doubling as a regression test for the interpreter
   against real data, not just fixtures.

## Open questions

- Should `magnitude` accept a value reference (`"actor.roll_margin"`) or only a flat 0..1
  constant? An expression is more powerful but means `nudge_attitude_from_event`'s contract
  changes from "a float computed in Python" to "a float evaluated from data, needing its own
  normalization" — worth confirming a real authored case needs this before generalizing it, and
  if so, defining the normalization (ex: `margin / some_max`) explicitly rather than passing a
  raw margin through.
- **`on_death` is deferred, out of this rollout.** Unlike the other four entity-lifecycle
  attachment points, there is no existing call site anywhere in the codebase that treats
  "HP crossed zero" as an edge-triggered moment — `hp_per_remain <= 0` is checked ad hoc,
  wherever a caller happens to care, and never as a single transition. It needs "fires exactly
  once" bookkeeping (a flag alongside `active_conditions`, or a dedicated check in `apply_damage`
  for the crossing itself) — not just "requirements currently hold," which `[[status]]`'s own
  sweep logic already assumes can be re-evaluated every call. This is its own design pass, not
  a fifth item on the list the other four attachment points share — grill it separately before
  adding it here.
- ~~`on_interact.<intent>` ordering~~ — settled: after the intent's built-in resolution, only on
  success, at the one shared funnel (see "Attachment points"). A program that needs to *deny* an
  interaction outright is still out of scope.
- **Keyword collision between universal abilities, and against unrelated catalog entries.** NLP
  embedding is one flat space — a poorly-chosen keyword on a new universal ability can lose to an
  unrelated entity that happens to share a literal word (the exact failure already found once,
  outside this design: a `"husbandry"`-flavored phrase losing to a spell literally named
  `"summon spectral wolf"` purely because both mention "wolf"). Worth a real live check with the
  actual NLP pipeline once `trip`/`disarm`/`sunder`/`intimidate` are authored, the same way that
  collision was actually found — not something to assume away from the TOML alone.
- **Does a skill's own `abilities` list need an author-facing lint** for a name that doesn't
  resolve to any real `[[entity]]` (a typo, ex: `abilities = ["trpi"]`)? `_instance_entities`'
  own "unknown entity" `log_error`-and-skip precedent is the natural fit; not designed in detail
  here.
- Save/load: for the ops that actually ship (`condition`/`dismiss_condition`/`attitude`, and
  later `damage`/`heal`/`transfer_item`/`transfer_currency`), a program never touches anything
  save/load doesn't already round-trip (conditions, attitude deltas, HP, currency, inventory) —
  no new persistence surface needed for any of this rollout. `on_death` firing exactly once is
  the one thing that will need a new persisted field, whenever it's designed.
