# LLDM — Combat, Challenge Rating, and Status

Part of the [LLDM](../CLAUDE.md) docs — rounds/turns, CR math, conditions, damage/healing, tags.

## Combat

Combat is a target being present *and* `is_hostile(target_name, player_name)`, which has two
distinct defaults, deliberately not collapsed into one: an entity with **no**
`[entity.attitudes]` table at all (ex: `debug.toml`'s wolf) is hostile unconditionally — a
monster that never bothered to author a disposition is still a monster. An entity that **does**
declare attitude data instead has to reach true hostility, `disposition <= -100`, to actually
fight — a merely wary/negative disposition is dialogue, not combat, which is what lets a
generated NPC's resolved disposition (see "NPC generation") land anywhere from wary to warm
without every non-friendly roll turning into a fight. An entity with `supertype == "object"` is
never treated as hostile regardless of attitude data. `is_hostile(entity, player_name)`
distinguishes enemies (attack the player) from allies (attack `self.current_target` instead, if
they carry their own `[[entity.behavior]]` data).

If in combat, `round_number` increments and the round publishes as `round_resolved` (one
narration per round). Otherwise it publishes as `action_resolved` (one narration per skill use)
— the path a dialogue check against a friendly NPC also takes.

A `round_resolved` payload carries the player's resolved actions (`"actions"`, a `list[
ActionOutcome]` — see "Multiple actions") plus `"turns"`: every other living scene entity's own
`{"actor", "initiative", "outcome"}` wrapper — `"outcome"` is that entity's own `ActionOutcome`
from `resolve_behavior_action` (`DM_Combat.py`, a `RolledOutcome` on an attack or a
`MovementOutcome` on a deliberate/fallback move), `"actor"`/`"initiative"` are this round's own
bookkeeping around it, not part of what actually happened, so they wrap the typed outcome rather
than living as fields on it. Driven by each entity's `[[entity.behavior]]` table — a
declaration-order list of `{requirements, action}` entries, matched top-down (requirements
compared the same way `[[status]]` requirements are). `turns` is sorted by initiative:
`roll_initiative(entity_name)` pools every skill named in `rules.toml`'s `[[initiative]]` list,
rolling once per round; an entity lacking a listed skill defaults to untrained. Initiative only
orders narration — every actor resolves independently against state as of the start of the
round, not sequentially. Once every turn has resolved, `run_round_upkeep` (see "Status and
conditions") applies any regeneration/fast-healing/bleed-style condition effects for the round.
`current_target` only advances (to the next living hostile entity, or the first living
non-player entity if none is hostile) once, at the very end of the round (after upkeep), if it
died.

**Naming one of several identically-named live instances.** `map_to_target` (`NLP_Core.py`)
matches by raw text similarity, so it always prefers the unsuffixed instance (`"wolf"` over
`"wolf_2"`, `DM_Rules.py`'s own `_unique_entity_key` suffixing) regardless of which one the
player meant — same-template instances share an identical description, and a player never
literally types the suffixed key. `_apply_target_redirect`'s own
`_resolve_named_instance_ambiguity` (`DM_Core.py`) corrects for this when the matched name has
living same-family siblings in the scene: it re-checks the turn's raw input for an ordinal
(`"the second wolf"`), `"other"`/`"another"`, or a wounded/healthy word (via `hp_per_remain`,
gated at the same `0.40` cutoff `rules.toml`'s `"wounded"` tier uses), falling back to the naive
match otherwise. A name with no live duplicates short-circuits immediately.

A behavior entry's `action` is either an ability name or one of two reserved movement words,
`"advance"`/`"retreat"` (`MOVEMENT_ACTIONS`, `DM_Combat.py`), routed to `move_toward_or_away`
instead of an ability lookup. An explicit `"retreat"` entry is how a creature values its own
life — checked ahead of its attack entry, ex: `debug.toml`'s wolf flees once `hp_per_remain`
drops under 0.40 (the same cutoff `rules.toml`'s `"wounded"` tier bottoms out at); an
undead/construct entity has no such entry and fights on regardless. Separately,
`resolve_behavior_action` falls back to `"advance"` on its own whenever its chosen action can't
currently reach its target (`is_in_range`) — closing distance instead of standing idle.


## Challenge rating

`Challenge_Rating.py` is a pure, DMCore-independent module computing a single number for "how
powerful is this entity," built entirely from its own dice/pips. `skill_rating(dice, pips)` is
`dice * 3 + pips`, the shared "3 pips = 1 die" scale. `calculate_challenge_rating(skills,
max_hp, damage_dice=0, damage_pips=0, top_n=3)` sums three components on that same scale:
- **skill** — the average `skill_rating` of the entity's `top_n` (default 3) best-*trained*
  skills, not every skill it has, so a character trained broadly but shallowly can't outrank a
  boss authored with only 2-3 trained skills.
- **hp** — `max_hp // 3`, the same `/3` scale as pips-to-dice.
- **damage** — `skill_rating` of the entity's single best damage-dealing weapon/ability's own
  `dice`/`pips` — not its `bonus` field, which can be a `rules.toml` formula reference rather
  than a flat number.

`calculate_party_challenge_rating(member_ratings)` is a plain sum, not an average — a larger
party of individually modest ratings can still outrate one strong boss.

`DM_Combat.py`'s `get_challenge_rating(entity_name)`/`get_party_challenge_rating()` are the
DMCore-touching glue: `_best_damage_dice_pips` finds the best `dice`/`pips` from every equipped
item plus every resolved ability with a `damage_value` (the same candidate pool
`find_attack_ability` draws from, just not filtered to one particular skill), resolving
`"user.weapon.<field>"` indirection (`resolve_weapon_reference`) the same way a real attack
would. `get_party_challenge_rating` filters through `self.scenario_entities`, not a blind
`is_player`/`is_party` scan of `self.entities` (see the same note on `GUI_Core.py`'s Party-tab
filtering, above).


## Status and conditions

`rules.toml`'s `[[status]]` table drives derived conditions. Each entry has:
- `trigger` — when to evaluate it; only `"on_damage"` is wired today, called from both
  `apply_damage` and `apply_healing` (see "Damage and healing").
- `requirements` — a list of entries, ALL of which must hold. Each entry is either a plain
  `{field, operator, value}` comparison (`COMPARATORS` in `Combat_Resolution.py`: `>`, `<`,
  `>=`, `<=`, `==`, `!=`, `in`, `not_in`, `between` — the last taking a `[low, high]` value,
  inclusive) or a nested `{"all"|"any"|"none": [...]}` boolean combination of more such entries
  (recursive) — the same shape `Program_Interpreter.evaluate_condition` gives program
  `if`-steps, both built from the same `COMPARATORS` table so a new operator only needs adding
  once. `field` is either derived (`"hp_per_remain"`) or a direct entity attribute.
  `entity_matches_requirements` is also what `is_test_available` (below) consults for an
  `[entity.test]`'s own optional `requirements` field, alongside its simpler
  `requires_condition`/`blocks_if_condition`.
- `apply` — `{condition, duration, length, dismiss}`, naming an entry in `[[condition]]`.

The actual roll/condition computation below (`resolve_action`, `get_condition_modifier`,
`apply_condition`, `evaluate_statuses`, ...) lives in `Combat_Resolution.py`, a pure module
taking `entities`/`rules`/`event_bus` explicitly rather than reading `self` — `DM_Status.py`'s
own methods are thin wrappers forwarding `self.entities`/`self.rules`/`self.event_bus`, kept
for every existing caller's sake (see "Architecture"). `get_active_conditions(entity_name)`
(a plain `{}`-defaulted dict) and `has_condition(entity_name, condition_name)` (a boolean
membership check) are the shared read accessors every other `active_conditions` check below —
`is_locked`/`is_closed`/`is_identified`/`is_hidden`/`is_test_available`'s own
`requires_condition`/`blocks_if_condition` gates, and the two derived requirement fields just
below — are built on, rather than each re-deriving
`entities.get(name, {}).get("active_conditions", {})` independently.

`entity_matches_requirements`/`get_comparable_value` are the shared engine behind `[[status]]`'s
own requirements, `[[entity.behavior]]`'s, and (as of the `requirements` field above)
`[entity.test]`'s; an optional `opponent_name` param
resolves the two opponent-relative derived fields, `"distance_to_target"` (the band gap to
`opponent_name`) — used by a creature choosing *between* attack options by range, ex:
`debug.toml`'s `bandit` favors its `short bow` while `distance_to_target > 0`, falling to its
`rusty shortsword` once that gap closes to 0 — and `"opponent_has_condition:<name>"` (below).
Two more derived fields resolve via `has_condition` rather than a numeric/positional value:
`"has_condition:<name>"` (a boolean presence check against the checking entity's own
`active_conditions`, ex: `{field = "has_condition:paralyzed", operator = "==", value = false}`
to gate a behavior entry off entirely while paralyzed, rather than letting it "act" at 0 dice —
see `get_condition_modifier`, which zeroes the roll but not the turn) and
`"opponent_has_condition:<name>"` (the same check against `opponent_name`'s own
`active_conditions` instead — lets a behavior react to its *target's* condition, ex: pressing an
attack while they're stunned, or favoring a fleeing/frightened target). Both resolve to `None`
(never matching) under the same conditions their positional cousins already do — a status's own
requirements never pass an `opponent_name`, so `"opponent_has_condition:<name>"` is only ever
meaningful inside `[[entity.behavior]]`.

`evaluate_statuses` finds every status matching a trigger whose requirements the entity
currently meets and calls `apply_condition`, storing it in the entity's own `active_conditions`.
`dismiss_condition(entity_name, condition_name)` is the general-purpose removal primitive.

**`duration`/`length` are a real, live countdown**, not flavor text. `duration` is one of
`Combat_Resolution.CONDITION_DURATIONS` — `"rounds"`, `"rooms"`, `"blocks"`, or `"permanent"`
(no countdown at all, cleared only by an explicit `dismiss_condition` call or a matching
`dismiss` trigger) — plus the authoring-only `"days"`, which `apply_condition` itself converts
to `"blocks"` the moment it's applied (`length * rules.toml`'s own `[time].blocks_per_day`, see
`docs/downtime.md`'s "The block clock") and so never exists as a stored value. `length` is how
many of that unit remain, decremented by `Combat_Resolution.tick_condition_durations` — called
once per living scene entity per combat round for `"rounds"` (`run_round_upkeep`, `DM_Status.py`
— ex: `"surprised"`, applied with `length = 1` by night watch, `docs/downtime.md`'s "Night watch
and surprise"), once per room left for `"rooms"` (`enter_room`, `DM_Rules.py` — scoped to a
same-dungeon room-to-room move only, not grid travel's own location-to-location one), and once
per block elapsed for `"blocks"` (`advance_blocks`, `DM_Time.py`, alongside its own unrelated
`prompt_directive` expiry). A condition applied with `duration = "permanent"` (or no `duration`
at all) needs no `length` and is left alone by every one of those ticks.

`evaluate_statuses` also sweeps the *other* direction: after applying whatever matches now, it
dismisses any active condition (from the same trigger) whose requirements no longer hold — ex:
healing back above a "wounded" tier's hp_per_remain range dismisses "wounded" in the same call.
A condition is only eligible for this sweep if stored with a falsy `dismiss` — one stored with a
named mechanism (ex: `"dead"`'s `dismiss = "resurrection"`) is left alone, so ordinary healing
can't revive a dead entity through the same path that clears a wound tier.

**A `[[condition]]` entry's own `modifier` (`{dice, pips, bonus}`) actually costs dice.**
`get_condition_modifier(entity_name)` (`DM_Status.py`) sums the `modifier` of every one of an
entity's `active_conditions` that has a matching `[[condition]]` entry — an active condition
with no such entry (ex: `"locked"`/`"closed"`/`"hidden"`, presence flags on non-creature
entities) contributes nothing. `resolve_action`/`resolve_opposed_action` (`DM_Combat.py`) fold
this into every roll: `dice` is reduced (floored at 0, same floor `dice_penalty` already uses)
by both `dice_penalty` *and* the acting entity's own condition dice penalty together, `pips` is
adjusted directly, and `bonus` is added to the final roll total after dice are rolled. In an
opposed roll, this applies independently to each side — the defender's own `active_conditions`
reduce *their* roll the same way, even though `dice_penalty` itself (the multi-action cost)
never touches the defender's side (see "Multiple actions"). This is what makes the wound
track's own conditions (`stunned`/`wounded`/`severe`/`incapacitated`/`mortal`, each with a
`[[condition]]` entry already authored in `rules.toml`) mechanically real rather than narration
flavor — a wounded character is measurably worse at everything, not just described as hurt.

**A `[[condition]]` entry's own `modifier` can be scoped to specific skills via `applies_to`**
(a list of skill names) — `get_condition_modifier` now takes an optional `skill_name` and skips
a condition's `modifier` entirely if it authors `applies_to` and `skill_name` isn't in it (no
`skill_name` at all never matches a scoped condition, same "can't match without a value"
precedent `distance_to_target`/`opponent_has_condition` already follow with no `opponent_name`).
`resolve_action`/`resolve_opposed_action` (`DM_Combat.py`) both already have the skill in scope
at their own call sites, so this costs no new plumbing beyond the one added parameter —
`resolve_opposed_action` passes the defender's own resolved `opposing_skill`, not the attacker's
`skill_name`, for the defender's side. A condition authoring no `applies_to` at all (every
condition shipped before this field existed) still applies globally, unchanged. This is what
lets Pathfinder's sight-only Dazzled or sound-only Deafened be authored honestly instead of as
an oversized blanket penalty (see `Rules/Fantasy/reference/pathfinder_mapping.toml`'s Pattern
C) — `rules.toml`'s own `"dazzled"` is the shipped example, `applies_to = ["observation"]`.

**`applies_to` (and `equipped_skill_bonus`'s own `skill`) can name a `[[skill_group]]` instead
of a literal skill**, expanded by `get_skill_group_members` (`Combat_Resolution.py`) — `rules.
toml`'s own `[[skill_group]]` table (`{name, skills}`) lets a cluster of skills be addressed by
one shared name, standing in for the attribute layer this engine deliberately doesn't have.
Pathfinder's Bull's Strength buffs every Strength-based skill/check at once, not one named
skill; authoring a condition's own `applies_to = ["strength"]` against `rules.toml`'s shipped
`"strength"` group (`["strength", "athletics", "blades", "axes", "brawling"]`) gets the same
shape without this engine needing a real ability score. A name matching no defined group is
still just a literal skill name, unchanged — this is purely additive over every existing
`applies_to`/`equipped_skill_bonus.skill` value, none of which name a group today.

**A `[[condition]]` entry can also carry `prevents_action = true`** — unlike `modifier` (a
penalized roll), this stops the entity from acting on its own turn at all.
`is_action_prevented(entity_name)` (`DM_Status.py`) is true if any of the entity's own
`active_conditions` has a matching `[[condition]]` entry authoring this flag.
`DM_Core.py`'s `_resolve_roll` checks it first, ahead of even a spell's own `materials` gate —
the player's own turn, if prevented, returns an `ActionPreventedOutcome` (no roll, same
"can't do it, don't roll" shape `OutOfRangeOutcome`/`LanguageBarrierOutcome` already use).
`DM_Combat.py`'s `resolve_behavior_action` checks it too, for a creature/ally's own turn —
treated exactly like "no `[[entity.behavior]]` entry currently matches" (`None`, no action this
round), rather than a new outcome type on that side. `rules.toml`'s `"pinned"`
(`maneuvers.toml`'s `"pin"`, only landable on an already-`"grappled"` target) is the first
(and, today, only) condition to author this, matching Pathfinder's real "pinned" — which stops
a character from acting at all, not just penalizing the roll — more honestly than the modifier
alone ever did; it keeps its own `-4` `modifier` too, in case a future mechanism (ex: an opposed
escape attempt) ever rolls *against* a pinned entity directly rather than just skipping their
turn. Note this is a different question from a creature's own `[[entity.behavior]]` choosing
*not* to act while a condition holds (ex: `{field = "has_condition:paralyzed", operator = "==",
value = false}` gating every entry off, mentioned above) — that's an authored opt-in per
creature, still lets the roll happen at all if some behavior entry didn't gate on it; `prevents_
action` is unconditional and covers the player's own turn too, which no behavior list ever does.

**A `[[condition]]` entry can also carry a per-round `upkeep_heal`/`upkeep_damage`
(`{dice, pips, bonus}`) instead of (or alongside) `modifier`** — a periodic effect rather than a
roll modifier, ex: `rules.toml`'s `"regenerating"` (`upkeep_heal = {dice = 2, pips = 0, bonus =
0}`). `run_round_upkeep()` (`DM_Status.py`) is the generic per-round hook: called once per
round from `_resolve_combat_round` (`DM_Core.py`), after every actor's own turn (including the
player's) has already resolved, it calls `apply_round_upkeep` for every living
(`hp_per_remain > 0`) entity in `self.scenario_entities`. `apply_round_upkeep` sums
`get_condition_upkeep`'s own heal/damage totals across every one of that entity's own
`active_conditions` with a matching `[[condition]]` entry, rolls each once, and applies the
result via the ordinary `apply_healing`/`apply_damage`. A condition's own
`upkeep_blocked_by_tags` (ex: `"regenerating"`'s own `["fire"]`) suppresses *both* its
heal and damage for that entity's tick entirely if the entity's own `"recent_damage_tags"` (a
plain `set`, populated by `calculate_damage` whenever it runs at all — DM_Combat.py — and never
persisted; not part of the whitelisted fields `DM_Persistence.py`'s `save_game` writes) overlaps
it — the Pathfinder "Regeneration 10 (fire)" shape: heals every round except one it's touched by
fire. `recent_damage_tags` is cleared at the end of every `apply_round_upkeep` call, so it only
ever reflects damage taken since the *previous* tick. `creatures.toml`'s `troll` is the shipped
example, seeding `"regenerating"` permanently via `[entity.conditions.regenerating]` (see
"Scenarios, locations, and rooms" for how a template's own `conditions` table becomes a live
instance's `active_conditions` at instancing time). `run_round_upkeep`/`apply_round_upkeep`
themselves stay scoped to actual combat rounds — but `DM_Status.py`'s `apply_downtime_upkeep`
(see `docs/downtime.md`'s "Rest") is the same `get_condition_upkeep` math applied outside one:
called from `_finish_pending_rest` once a rest actually completes, it rolls each entity's own
heal/damage totals *once*, dice/pips/bonus scaled by the whole rest's block count (not once per
block — the same "avoid swinginess from rolling repeatedly" reasoning rest's own fortitude
healing already follows), so a regenerating creature genuinely heals while camped, not just
mid-fight. It still checks `"recent_damage_tags"` the same way — a troll that took fire damage
right before making camp still doesn't regenerate through that rest.


## Pathfinder-mapping engine extensions

Five additive fields/triggers, added specifically to close `fit = "partial"`/`"none"` gaps in
`Rules/Fantasy/reference/pathfinder_mapping.toml` (a machine-readable Pathfinder 1e -> D6
mechanic-lookup table) without touching any existing behavior -- every one of these is
absent/inert unless a piece of content actually authors it.

- **`on_hit_condition`** (an ability field, `{condition, chance, duration, length, dismiss}`) --
  `apply_on_hit_condition` (`Combat_Resolution.py`, called from `calculate_damage`) applies a
  `[[condition]]` directly to whoever an ability just hit, no `[entity.test]` detour needed
  (the Pathfinder "Wounding"/poison-on-hit shape). `chance` (1-100, default 100) is the percent
  chance it actually lands. Skipped outright if the defender is immune to the ability's own
  `damage_tags`.
- **`damage_bonus_vs`** (an ability field, `{supertypes, subtypes, value = {dice, pips,
  bonus}}`) -- `get_damage_bonus_vs` rolls extra damage when the defender's own
  `supertype`/`subtype` matches either list, added into `calculate_damage` alongside
  `vulnerability_bonus`. The Pathfinder "Holy"/"Bane" shape (bonus vs. a creature *kind*),
  which plain `damage_tags` overlap can't express since that only ever checks the defender's
  resistance/immunity/vulnerability, not what it *is*.
- **`equipped_skill_bonus`** (an item field, `{skill, dice, pips}`) -- `get_equipped_skill_bonus`
  sums every equipped item's own matching bonus, folded into `resolve_action`/
  `resolve_opposed_action` alongside `get_condition_modifier`, for whichever entity is rolling
  (both sides of an opposed roll). The Pathfinder "Ring/belt/wondrous stat bonus" shape -- a
  passive skill-dice buff from a worn item with no weapon/armor role at all, not gated by slot.
- **`cooldown_rounds`** (an ability field) + the derived requirement field
  `"ability_ready:<name>"` -- using a behavior-driven ability that authors `cooldown_rounds`
  sets the acting entity's own `ability_cooldowns[name]` to that many rounds (`resolve_
  behavior_action`, `DM_Combat.py`, regardless of hit/miss), ticked back down to 0 (removed
  once it gets there) by `tick_ability_cooldowns`, called from `run_round_upkeep` alongside the
  existing condition-duration tick. A behavior list gates back off the same ability meanwhile
  via `{field = "ability_ready:<name>", operator = "==", value = true}` (`get_comparable_value`),
  falling through to a weaker fallback entry -- the Pathfinder "breath weapon usable once every
  1d4 rounds" shape, the same declaration-order gating `"has_condition:<name>"` already does
  for a paralyzed/warded creature.
- **The `[[status]]` `"on_action"` trigger** + `evaluate_proximity_statuses` (`DM_Status.py`) --
  distinct from every existing `[[status]]` trigger (`"on_damage"`, the only other one), which
  always self-applies its condition to whoever's HP just changed. An `"on_action"` status's own
  `requirements` are checked against the ACTING entity, but its `apply` block's condition lands
  on every *other* living entity within `apply.radius` bands (default 0 -- same band only) of
  the actor, filtered by `apply.side` (default `"enemies"`, same vocabulary `targets` uses).
  Fired once per turn that lands a real hit, from both `resolve_behavior_action` (an NPC's own
  turn) and `_apply_damage_if_hit` (`DM_Core.py`, the player's own turn) -- the Pathfinder "Fear
  aura/Frightful Presence" shape (fires off the attacker's own trait, not off damage taken).
  Honest simplification: fires on a landed hit, not on the bare attack attempt real Frightful
  Presence uses, since there's no untargeted "roar" action to hook instead; and runs no stale-
  condition dismissal sweep the way `evaluate_statuses` does for `"on_damage"`, since re-
  sweeping every nearby entity on each of the actor's turns would dismiss a still-fresh
  application from a different actor's own aura sharing the same condition name.
- **`miss_chance`** (a `[[condition]]` field, percent 0-100) -- `get_concealment` takes the
  highest `miss_chance` across a defender's own matching active conditions (capped at 95, not
  summed -- concealment doesn't stack additively), rolled in `resolve_opposed_action` right
  after an otherwise-successful roll: a hit that rolls under the defender's own concealment
  comes back `"success": False`, `"concealed_miss": True` instead. The Pathfinder Invisible/
  concealment shape -- deliberately *not* an `is_targetable` gate at the NLP layer (`map_to_
  target`), which would cross a seam that has no other reason to know about combat conditions.
  An attacking ability's own `ignores_concealment` (`entity_schema.toml`) bypasses this check
  entirely (a ghost touch/seeking weapon). Scoped to the ordinary opposed-roll path only -- a
  flat-`difficulty` spell or an `[entity.test]` check never consults it, matching how those
  paths already have no notion of "attack roll" the way an opposed check does.
- **`drain`** (a `[[condition]]` field, `{skill, dice, pips}`) -- `apply_condition` permanently
  subtracts from `entity["skills"][skill]`'s own base dice/pips the first time this condition
  is gained (floored at 0, and skipped on a reapplication/duration-refresh of an already-active
  instance, so it can never double-drain), stashing the *actual* amount removed (which may be
  less than authored, if clamped) on the condition's own `active_conditions` entry.
  `dismiss_condition` reads that stashed amount back and restores it -- no `rules` lookup
  needed at dismissal, since the exact removed amount already travels with the condition
  instance itself. Distinct from the ordinary `modifier` field: `modifier` is a roll-time-only
  penalty that evaporates the instant the condition is dismissed; `drain` mutates the base
  stat, restored only when the condition is explicitly removed (ex: a restoration-style spell's
  own `dismiss_condition` op) -- the Pathfinder "Energy Drained" shape (permanent stat loss).
- **`override_target`** (a `[[condition]]` field, `"random"` or a literal entity name) --
  `resolve_override_target` hijacks WHO an entity's turn is aimed at, not whether it can act
  (`prevents_action`) or which ability it picks (`choose_behavior` runs unaffected). Folded
  into `resolve_behavior_action` (`DM_Combat.py`), which swaps its own `target_name` *before*
  choosing a behavior, so a distance-based behavior choice (ex: a bandit favoring its bow at
  range) judges the real, overridden target rather than the original one. `"random"` picks
  uniformly from every other currently-living scene entity (the Pathfinder Confused shape); a
  literal name is used as-is if it currently names a real, living entity, else treated as no
  override at all (ex: the named target already died) -- the mechanical slice of Dominate
  (attack on the dominator's behalf), authored by whatever spell applies the condition naming
  its own chosen target. Deliberately NPC-only: `resolve_behavior_action` is the only call
  site, so a Confused/Dominated *player* -- which would mean scrambling free-text NLP input --
  is out of scope, the same call already made for real Dominate's "obey arbitrary commands"
  half (a narrative/ADaM question, not an engine primitive).
- **The `[[status]]` `"on_round"` trigger** -- reuses `evaluate_proximity_statuses` (the same
  function `"on_action"` already calls) unchanged, just fired once a round for every living
  scene entity from `run_round_upkeep` instead of only whoever just landed a hit. This is the
  whole mechanism behind a persistent terrain hazard (the Pathfinder "Persistent terrain/
  obstacle spells" shape -- Wall of Fire, Web, Grease): a `[[status]]` entry's `requirements`
  match the hazard entity itself (by `"name"`, the same field-comparison engine every other
  requirement already uses -- confirmed to survive same-name instance disambiguation, since
  `_instance_entities` never strips an instance's own authored `"name"` field, only the
  `self.entities`/`scenario_entities` *key* gets a `_2`/`_3` suffix), and its `apply` block
  (unchanged shape: `condition`, `radius`, `side`, `duration`, `length`) lands on whoever
  currently shares that entity's band. Authoring the applied condition with a short
  `duration = "rounds"`/`length = 1` is what makes it a *zone* rather than a one-time blast --
  it lapses on its own the moment an entity is no longer co-band, simply reapplied fresh each
  round they stay. No new field anywhere: the hazard is an ordinary `[[entity]]` (placed
  permanently in a room like any prop, or conjured mid-scene via the existing `summon`
  mechanism -- `_summon_creature` never cared what kind of entity it was placing), the real
  effect is an ordinary `[[condition]]`'s own `upkeep_damage`/`upkeep_heal`/`modifier`, and the
  only "connection" between the two is the same trigger/requirements matching every other
  status already uses. `rules.toml`'s own `"flame wall zone"` (matched to `spells.toml`'s
  `"flame wall"`, conjured by its own `"wall of fire"` spell via `summon`) is the shipped
  example.
- **`dispel`** (an ability field, `{supertypes, subtypes}`) -- `_apply_dispel_if_hit`
  (`DM_Core.py`, mirroring `_apply_summon_if_hit` exactly, just removing an entity instead of
  conjuring one) banishes `current_target` outright (`remove_entity_from_scene`) on a
  successful cast, but only if the target's own supertype/subtype actually matches either list
  -- the same `matches_supertype_or_subtype` OR-of-two-lists check `damage_bonus_vs` already
  uses (extracted as a shared helper, `Combat_Resolution.py`), reused unchanged rather than a
  second copy of the same matching rule. The shipped spell authors
  `{supertypes = ["spell"], subtypes = ["spell"]}`, covering both places `"spell"` shows up:
  every live spell-catalog entity's own `supertype`, and a conjured effect object's own
  `subtype` (ex: `spells.toml`'s `"flame wall"`, `supertype = "object"`) -- any future magical
  effect opts into being dispellable the same way, just by authoring `subtype = "spell"`, not
  narrowly scoped to one particular effect shape the way an earlier `subtypes = ["hazard"]`
  draft was. A mismatched target (ex: pointing `"dispel magic"` at an ordinary creature) simply
  does nothing, the Pathfinder "used on the wrong thing just wastes the action" shape, not a
  hard pre-roll gate. No `difficulty` authored on the shipped spell, so it resolves as an
  ordinary opposed roll against `current_target`'s own defenses -- a mindless effect with no
  skills of its own (ex: `"flame wall"`) offers essentially no resistance, while a warded
  creature's own skill makes it a real contest. Player-only, same scope every other cast-time
  effect (`summon`/`teleport_to_band`) already keeps. `spells.toml`'s own `"dispel magic"`
  (targeting `spells.toml`'s `"flame wall"` via its shared `subtype = "spell"`) is the shipped
  example.
- **`periodic_test`** (a `[[condition]]` field, `{skill, difficulty, onset, interval, on_fail,
  cure_after_successes}`) -- the Pathfinder poison ("Frequency 1/round")/disease ("Frequency
  1/day") shape: a recurring self-save that starts after an optional `onset` delay (`{unit,
  length}`, `unit` one of `"rounds"`/`"blocks"`/the authoring-only `"days"`, converted to
  `"blocks"` via `[time].blocks_per_day` the same way an ordinary condition `duration` already
  is), then repeats every `interval` (same `{unit, length}` shape) until either
  `cure_after_successes` consecutive passes cures it outright (Pathfinder's "2 consecutive
  saves" cure shape -- `dismiss_condition`, no explicit cure spell needed) or something else
  removes it first (ex: `cure`, below). `Combat_Resolution.tick_periodic_tests` -- ticked from
  the same call sites `tick_condition_durations` already is (`DM_Status.py`'s
  `run_round_upkeep` for `"rounds"`, `DM_Time.py`'s `_tick_conditions_by_block` for `"blocks"`,
  deliberately not `enter_room`'s `"rooms"` tick) -- decrements whichever phase (onset, then
  interval) is currently active, rolling a flat `resolve_action(skill, difficulty)` self-save
  the moment it reaches 0. A pass increments a stored consecutive-successes counter; a fail
  resets it to 0 and applies `on_fail.drain` (a list of `{skill, dice, pips}` entries -- a
  *repeatable* drain, stashed cumulatively per skill on the condition instance's own
  `active_conditions` entry and restored in full by `dismiss_condition`, distinct from the
  ordinary one-shot `drain` field above) and/or `on_fail.damage` (an ordinary rolled hit, via
  `apply_damage`). `rules.toml`'s own `"filth fever"` (applied by `creatures.toml`'s `"giant
  rat"` bite) is the shipped example.
- **`supertype`/`subtype` on a `[[condition]]` entry**, plus the ability field **`cure`**
  (`{supertypes, subtypes}`) -- `[[condition]]` entries are plain dicts the same as
  `[[entity]]` ones, and `matches_supertype_or_subtype` has no entity-specific logic at all (it
  only ever reads `dict.get("supertype")`/`dict.get("subtype")`), so it's reused unchanged
  against the condition catalog instead of the entity one -- no new matching code, just two new
  optional fields. `Combat_Resolution.dismiss_matching_conditions` walks an entity's own
  `active_conditions`, dismissing every one whose `[[condition]]` entry matches `cure`'s filter;
  `DM_Core.py`'s `_apply_cure_if_hit` (mirroring `_apply_dispel_if_hit` exactly, just removing a
  condition instead of banishing an entity) calls it on a successful cast against
  `current_target`, appending a `CureEffect` naming whatever was actually cured (possibly
  empty). The Pathfinder "Remove Disease"/"Neutralize Poison"/panacea shape -- the caster
  doesn't need to name the specific affliction, just its kind: `{subtypes = ["disease"]}` cures
  any active disease, `{supertypes = ["affliction"]}` a broader panacea also catching
  poison/curse, mirroring the way `dispel`'s own `{supertypes = ["spell"], subtypes =
  ["spell"]}` covers two different places `"spell"` shows up. A target with nothing matching
  simply has nothing cured, the same "used on the wrong thing just wastes it" shape `dispel`
  already has. `spells.toml`'s own `"cure disease"` (`{subtypes = ["disease"]}`, targeting
  `rules.toml`'s `"filth fever"` via its shared `subtype = "disease"`) is the shipped example.
- **The `[[status]]` `"on_arrival"` trigger** + `DM_Rules.py`'s own `_evaluate_arrival_statuses`
  (called from both `_enter_location` and `enter_room` -- the only two "a scene was just
  entered" moments this engine has) -- the Pathfinder "Glyph of Warding"/"Magic Mouth" shape.
  Reuses `evaluate_proximity_statuses` completely unchanged, just fired once per scene entity on
  arrival instead of once a combat round (`"on_round"`) or off a landed hit (`"on_action"`): an
  ordinary `[[entity]]` hazard, placed permanently like any other room prop (or conjured via
  `summon`, same as `"flame wall"`), whose own `[[status]]` entry matches it by `"name"` lands
  its `apply` condition on whoever just arrived. Distinct from the existing `[entity.on_enter]`
  program hook (`_run_on_enter_programs`, right alongside it) -- that always targets the entity
  itself (an item revealing its own tags once seen), never the party that just walked in; this
  is the other half, an effect aimed *at* the new arrivals. `evaluate_proximity_statuses` also
  gained an optional `self_dismiss` field (a condition name) -- dismissed on the matched hazard
  itself the moment this call actually lands its effect on at least one nearby entity (never if
  nobody was in range), what makes a glyph spend itself the first time it actually catches
  someone rather than re-triggering every visit the way a persistent `"on_round"` hazard
  (`"flame wall"`) is meant to. Pairs with a `has_condition:<name>` requirement against a hazard
  seeded with `[entity.conditions.<name>]` -- the same "seed a flag, dismiss it once solved"
  shape `items.toml`'s dart trap already uses via `[entity.test]`, just spent automatically here
  instead of by a deliberate disarm attempt.

  A genuine damage-dealing "Blast"-type glyph turns out to be expressible too, by reusing an
  ordinary `upkeep_damage`-bearing condition directly (ex: `"burning"`, the same one
  `"flame wall zone"` already applies) rather than needing a new field: `apply_round_upkeep`
  (which actually rolls `upkeep_damage`/`upkeep_heal`) is only ever *called* from an actual
  combat round (`run_round_upkeep`) or a completed rest (`apply_downtime_upkeep`) today, neither
  of which is guaranteed to happen soon -- or at all -- after simply walking into a room, so a
  freshly-applied `"burning"` would otherwise just sit inert. `_evaluate_arrival_statuses`
  closes that by resolving one implicit round of upkeep (`apply_round_upkeep` +
  `tick_condition_durations("rounds")`) immediately for whoever `evaluate_proximity_statuses`
  (now returning the set of newly-affected targets) just applied a condition to. Authoring the
  condition with `duration = "rounds"`/`length = 1` is what makes it a true instant blast --
  dealt and dismissed in that same call -- rather than a lingering burn; a longer length instead
  leaves it genuinely active afterward. A condition with no upkeep fields at all (ex:
  `"shaken"`) is unaffected -- `get_condition_upkeep` totals to zero for it, so this step is a
  harmless no-op. `items.toml`'s own `"warding glyph"` (the hazard, seeded with
  `[entity.conditions.armed]`) + `rules.toml`'s own `"warding glyph shock"`/`"warding glyph
  blast"` (`self_dismiss = "armed"` on both, applying `"shaken"` and `"burning"` respectively)
  is the shipped example -- one hazard demonstrating both the debuff and the blast shape.
- **`form`** (a `[[condition]]` field, an entity name) -- the Pathfinder Polymorph/Baleful
  Polymorph "form override" shape: `[[condition]]`'s own `modifier` is only ever a flat delta on
  an unchanged base, never a stat-block replacement, so nothing before this could swap an
  entity's skills/abilities/supertype/subtype mid-scene and revert it later at all.
  `_apply_form_override` (`Combat_Resolution.py`) snapshots a fixed set of fields
  (`FORM_OVERRIDE_FIELDS`: `name`, `description`, `supertype`, `subtype`, `skills`, `abilities`,
  `max_hp`, `medium`, `damage_value`, `damage_tags`, `tags`, `resistance_value`/
  `resistance_tags`/`resistance_bypass_tags`, `immunity_tags`, `vulnerability_value`/
  `vulnerability_tags`) off the target the first time the condition is gained (never on a
  refresh, the same guard `drain`/`periodic_test` already use), overwrites them with a deep copy
  of the same fields off the entity named by `form`, and stashes the exact original values
  (`None` standing in for "field was absent") as the condition instance's own `_form` entry.
  `dismiss_condition` restores that snapshot verbatim, last (after any `_drained`/`_periodic`
  restore already run) and wholesale -- no `rules` lookup needed at restore time, the same
  precedent `drain` already set. `form` looks the target up directly in the live `entities`
  dict (the same flat namespace `_instance_entities`'s own `"name"`-branch templates come from)
  rather than a separate pristine-template cache `Combat_Resolution.py` has no access to --
  naming a form that's already alive and mutated elsewhere in the scene copies that live
  instance's current stats rather than a pristine template's, the same edge case `_instance_
  entities`'s own docstring already documents for ordinary instancing, not a new risk this
  introduces. Deliberately untouched: `equipped`/`inventory`/`currency`/`attitudes`/`hp`/`band`/
  `active_conditions` all stay with the person, not the form -- a polymorphed fighter keeps
  their sword even though `get_equip_slots` is never re-checked against it (nothing re-audits
  already-equipped items against a new supertype/subtype, only a fresh equip attempt consults
  it), and current `hp` is left alone so a beefier form grants no free healing (`get_current_hp`
  already only ever seeds `hp` from `max_hp` if `hp` is absent). Two form-shaped conditions
  active on the same entity at once, or a `form` condition and a `drain` condition both touching
  `skills` at the same time, leaves restore order undefined -- not solved here, the same
  pre-existing gap `drain` already has for concurrent effects. Persistence: `DM_Persistence.py`'s
  `_instance_state` saves the currently-overridden `FORM_OVERRIDE_FIELDS` values (as
  `form_override`) whenever any active condition carries a `_form` snapshot, and `load_game`
  reapplies them on top of the freshly re-instanced base template -- without this, a save made
  mid-polymorph would reload in base form with the condition still ticking, silently losing the
  transformation until it happened to expire or be dismissed. `rules.toml`'s own `"polymorphed"`
  (`form = "coyote"`, `spells.toml`'s `"baleful polymorph"`, permanent) and `"wild shape"`
  (`form = "wild boar"`, `spells.toml`'s own `"wild shape"`, `duration = "rounds"`) are the
  shipped examples -- the same `subtype = "transmutation"` classification lets `spells.toml`'s
  `"break enchantment"` (`cure = {subtypes = ["transmutation"]}`) reverse either one without
  ever naming them directly, the same trick `"cure disease"` already uses against `"filth
  fever"`.


## Damage and healing

`apply_damage` subtracts HP (floored at 0) and calls `evaluate_statuses(entity_name,
"on_damage")`. `apply_healing` adds HP (clamped at `max_hp`) and calls the same
`evaluate_statuses("on_damage")` — not to apply a *new* injury but so a wound tier's condition
that no longer holds after the heal gets dismissed by the stale-condition sweep above.

Nothing automatically re-evaluates a status-driven condition once its requirements stop holding
outside of `apply_damage`/`apply_healing`'s own calls.


## Experience (XP)

`_award_xp_for_defeat` (`DM_Combat.py`) is one shared primitive with two call sites, each
deciding independently *when* an entity counts as neutralized rather than duplicating this
method's own math:
- `calculate_damage` captures `defender_name`'s HP before calling into
  `Combat_Resolution.calculate_damage`, and if that call brings it from positive down to
  exactly 0 *and* `is_hostile(defender_name, player_name)` is true, calls
  `_award_xp_for_defeat(defender_name)` before returning — a real kill, once, never a second hit
  against an already-dead corpse, and never for an ally/the player/an inanimate object (all
  `is_hostile` false by construction — see "Combat", above). Unconditional whenever it fires —
  no per-entity authoring needed.
- `apply_test_outcome` (`DM_Status.py`, see `docs/inventory-items.md`'s "Entity tests") calls it
  whenever the matched `[entity.test]` outcome carries a truthy `xp` key — ex: `items.toml`'s
  dart trap/scythe trap, whose own `[entity.test.pass]` pairs `xp = true` with
  `dismiss_condition = "armed"`, so surviving or disarming a trap is worth XP the same
  declarative way `loot`/`reveal`/`damage` already are, rather than a bespoke "if trap" branch
  anywhere. Deliberately opt-in, unlike a combat kill — most `[entity.test]`s (ex: a chest's
  lock) aren't "surviving a threat" at all, so this can't be inferred from `subtype == "trap"`
  or any other property; it has to be authored. Naturally single-fire too, with no extra
  bookkeeping: once `"armed"` is dismissed, `is_test_available`'s own `requires_condition` gate
  makes that same test permanently unavailable, so a disarmed trap can never re-award XP.

`_award_xp_for_defeat`'s base amount is the neutralized entity's own `exp` field if it authored one
at all — checked via plain key presence, so an authored `exp = 0` really does grant zero XP —
else its live `get_challenge_rating` (see "Challenge rating") — a poor stand-in for a trap
specifically, which has no skills/damage-dealing ability the usual way, so every shipped trap
authors an explicit `exp` instead. `exp` plays a second, dual role on
a player/party member instead: their own running XP total, only ever accumulated into, never
read as a custom-grant override (the same "one field, two sides of the same economy" shape
`currency` already has between a lootable entity's stash and a party member's own purse).

The base amount is multiplied by `rules.toml`'s own `[xp]` `xp_multiplier`, then either split
evenly across every entity in `self.scenario_entities` that's currently `is_player`/`is_party`
(`divide_between_party = true`, floor `//` division so an uneven split never grants fractional
XP) or credited to each one in full (`false` — Fantasy's own shipped default, mirroring how
`loot_entity`/`transfer_currency` already hand a defeated/looted entity's full currency to
whoever loots it rather than splitting it first). `[xp]` itself is a tuning table, not an opt-in
gate the way `[bulk]` is — a setting that authors no `[xp]` table at all still accumulates XP on
its party members' `exp` field, just at `xp_multiplier = 1`, `divide_between_party = false`.

Spending `exp` — training a skill up by a pip at a time — is `Character_Creation.py`'s
`spend_pip`/`spend_exp_on_skills`, wired into the character-creation screen today (see
`docs/character-creation.md`'s "Training"); quest-driven XP (as opposed to defeating an enemy),
and reopening that screen mid-game rather than only once at first boot, are both still open —
see `docs/downtime.md`'s "Not yet built".


## Tags vs. conditions

- **Tags** are static classification data, fixed for an entity's lifetime: `damage_tags`/
  `armor_tags`, `resistance_value`/`resistance_tags` (rolled, partial reduction via
  `get_damage_reduction`), `immunity_tags` (absolute — `is_immune_to` zeroes net damage
  regardless of roll), and `vulnerability_value`/`vulnerability_tags` (rolled, extra damage added
  before reduction). Immunity wins outright over vulnerability if both match. `resistance_tags`/
  `armor_tags` each have an optional bypass counterpart (`resistance_bypass_tags` on the
  entity itself, `armor_bypass_tags` on an equipped item) — if any of an incoming hit's
  `damage_tags` matches a bypass tag, that side's reduction is skipped for this hit entirely,
  even if `resistance_tags`/`armor_tags` would otherwise have matched (Pathfinder's "DR
  10/magic": mundane weapons reduced, magic weapons cut straight through). Absent/empty on
  every entity that doesn't author it, so this never changes existing behavior on its own.
  `language_dependent` (a plain bool, on an ability entry rather than an entity) plays the same
  fixed-classification role but for a different question — not what kind of damage landed, but
  whether the ability functions at all without a shared language (see "Dialogue"'s own
  "Language-dependent abilities and skill checks") — deliberately its own field rather than a
  value inside `damage_tags`, since that field only ever feeds the damage-reduction pipeline
  above and most language-dependent checks (ex: persuade) deal no damage to begin with.
- **Conditions** (`active_conditions`, `apply_condition`/`dismiss_condition`) are dynamic —
  gained/lost during play via triggers or tests. Use a condition for something that can plausibly
  change mid-scene; use a tag for something permanent to what the entity is.

`abilities` is a flat list, each entry either a plain string naming a shared catalog entity
(`spells.toml`/`techniques.toml`) or an inline table for a one-off innate ability.
`techniques.toml`'s `cleave` exercises a multi-skill `skill = [...]` list and weapon-scaled
damage (`"user.weapon.dice"`/`"user.weapon.pips"`); see `ability_matches_skill`,
`resolve_weapon_reference`, `resolve_damage_value` in `DM_Combat.py`. Naming a technique/spell
directly in input can resolve it via `map_to_action` before a bare skill would.


## Multi-target and area of effect

An ability's own `targets = {number, aoe, side}` (`entity_schema.toml`) widens who a
successful roll actually lands on, past the single `target_name` the roll was resolved
against — absent entirely (every ordinary weapon, most spells) means just that one entity,
unchanged. `resolve_targets` (`DM_Combat.py`) always puts `target_name` first, then — if
`targets` is authored — adds every other living scene entity within `aoe` bands of it
(`get_distance_between`, nearest-first; `aoe = 0`, the default, means only entities sharing
`target_name`'s own band), filtered by `side` (`"enemies"`, the default, and `"allies"` via
`is_hostile(candidate, attacker_name)` — relative to whoever is actually acting, not
hardcoded to the player; `"all"` skips the hostility check entirely for an indiscriminate
blast), and finally caps the combined list at `number` (default `1`, `0` = unlimited). One
mechanic covers three distinct shapes: `techniques.toml`'s `cleave` (`{number = 3, aoe = 0}`)
is discriminating multi-target — up to 3 other enemies sharing `target_name`'s band, no
radius; `spells.toml`'s `fireball` (`{aoe = 5, side = "all", number = 0}`) is an indiscriminate
blast — everyone within 5 bands, friend or foe, uncapped; a Pathfinder-style channeling that
only touches allies would author `{aoe = <radius>, side = "allies"}`.

**`side = "self"` is a fourth, short-circuiting case** — `resolve_targets` returns
`[attacker_name]` outright, before even looking at `target_name`/`aoe`/`number`. This is what
lets a personal ward/self-buff skip needing a named target at all (`_apply_damage_if_hit`'s
own outer gate no longer requires `target_name` either, for exactly this case — a `resolve_
targets` result of `[None]`, the ordinary untargeted-ability case, is simply skipped in the
loop) and guarantees it never spills onto an adjacent ally the way an ordinary
`{aoe = 0, side = "allies"}` still could if one happens to share `target_name`'s own band.

Both `_apply_damage_if_hit` and `_run_ability_outcome_program` (`DM_Core.py`) call
`resolve_targets` and loop over its result: each resolved defender gets its own
`calculate_damage`/`DamageEffect`/`combat_hit` attitude nudge, and the ability's own
`on_pass`/`on_fail` program (if any) runs once per resolved target rather than once against
`target_name` alone — so a discriminating area effect's `on_pass` (ex: an `apply_condition`
op) actually lands on every ally/enemy the blast caught.

