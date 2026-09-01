# LLDM — Combat, Challenge Rating, and Status

Part of the [LLDM](../CLAUDE.md) docs — rounds/turns, CR math, conditions, damage/healing, tags.

## Combat

Combat is a target being present *and* `is_hostile(target_name, player_name)`, which has two
distinct defaults, deliberately not collapsed into one: an entity with **no**
`[entity.attitudes]` table at all (ex: `arena.toml`'s wolf) is hostile unconditionally — a
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
life — checked ahead of its attack entry, ex: `arena.toml`'s wolf flees once `hp_per_remain`
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
- `requirements` — a list of `{field, operator, value}` comparisons (`COMPARATORS` in
  `DM_Status.py`: `>`, `<`, `>=`, `<=`, `==`, `!=`, `in`, `not_in`), ALL of which must hold.
  `field` is either derived (`"hp_per_remain"`) or a direct entity attribute.
- `apply` — `{condition, duration, dismiss}`, naming an entry in `[[condition]]`.

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

`entity_matches_requirements`/`get_comparable_value` are the shared engine behind both
`[[status]]`'s own requirements and `[[entity.behavior]]`'s; an optional `opponent_name` param
resolves the two opponent-relative derived fields, `"distance_to_target"` (the band gap to
`opponent_name`) — used by a creature choosing *between* attack options by range, ex:
`field.toml`'s `bandit` favors its `short bow` while `distance_to_target > 0`, falling to its
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
instance's `active_conditions` at instancing time). This mechanism is scoped to actual combat
rounds only — there's no equivalent "per turn" concept outside one, so a regenerating creature
doesn't heal between scenes or during freeform (non-combat) play.


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
see `docs/extended-goals.md`.


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

