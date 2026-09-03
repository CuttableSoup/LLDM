# LLDM — Downtime: The Block Clock, Rest, and Travel

Part of the [LLDM](../CLAUDE.md) docs — the coarse time-of-day clock underlying travel/rest, and
what's actually built of it so far. See `docs/extended-goals.md`'s own "Downtime" section for the
full design (crafting's day-extension, training reopening, night watch/surprise) this is built
from; read it before extending anything here further.

## The block clock

`DMCore.current_block` (`DM_Time.py`) is a single monotonic counter of every 8-hour (by default)
"block" elapsed since the scenario started — a new, persistent, top-level `DMCore` field,
round-tripped through `save_game`/`load_game` the same way `round_number` already is (see
`docs/persistence.md`), and a fully separate axis from combat's own `round_number` (tactical/
per-turn vs. strategic/per-downtime-action). Nothing else about the clock is stored redundantly —
`get_time_state()` derives everything else fresh from `current_block` alone against `rules.toml`'s
own `[time]` table (`hours_per_day`, `daylight_hours`, `blocks_per_day` — 24/16/3 by default,
falling back to those same three values if a setting authors no `[time]` table at all, the same
"still works unauthored" pattern `[xp]` already follows): `day` (`current_block // blocks_per_day`),
`block_in_day`, `hour` (`block_in_day * hours_per_block`, where `hours_per_block = hours_per_day /
blocks_per_day`), and `is_day` (`hour < daylight_hours`) — day/night is read off actual elapsed
hours, not a fixed block-index parity, so a `daylight_hours` that doesn't evenly split
`blocks_per_day` still resolves sensibly (a block counts as daytime if it *starts* before
`daylight_hours`). `is_daytime()` is a one-line convenience over `get_time_state()["is_day"]`.
`advance_blocks(blocks)` is the only place `current_block` is ever mutated.

## Rest

`rest(blocks=1)` (`DM_Time.py`) first looks up the current location's own environment once
(`_current_environment`, `DM_Travel.py` — `None` for a location with no `grid` field, or one whose
grid point falls in an unmapped `world_map.toml` gap, both the same "absence of an environment"
default that's what "safe" looks like everywhere in this design), then advances the clock one block
at a time, rolling that environment's own day/night encounter table — and, on a night block whose
roll turns out hostile, a night watch check — via `_resolve_environment_block` for every block
spent, the exact machinery grid travel's own per-block loop already uses (see "Travel"/"Night watch
and surprise" below), just against one fixed point instead of a line of travel, since nothing moves
during rest. A location with no environment at all skips this entirely, same as before this
existed. A hostile block pauses the rest exactly like grid travel now does — see "Pausing for a
fight" below.

Once every block has elapsed, heals every living `is_player`/`is_party` member (`_is_party_member`)
via the ordinary `apply_healing` call, scaled by their own `fortitude` skill — the body's own
recovery, picked over `medicine` (a caregiver treating someone else's wound, which stays free for
some other future check that actually wants that framing). One aggregate roll per rester over the
whole rest, not one per block spent — `fortitude`'s own dice/pips scale directly with `blocks`
before the single roll happens, so a longer rest's variance still grows the way rolling more dice
actually would, the same "avoid swinginess from rolling repeatedly" reasoning crafting's own
`days_required` already follows (see `docs/inventory-items.md`) — and is unaffected by however many
of those blocks turned out hostile: resting through an ambush still heals exactly as much as an
uneventful rest of the same length would.

Reachable in play as the free-text `"rest"` intent (`Intent_Classification.py`'s `REST_KEYWORDS` —
`"rest"`, `"make camp"`, `"set up camp"`, `"sleep"`, `"camp for the night"`), one of
`EXEMPT_ITEM_INTENTS` alongside every other free-standing intent (see `CONTEXT.md`): diceless,
and never joins the shared multi-action turn (see `docs/action-resolution.md`'s "Multiple
actions"). NLPCore only recognizes that resting was requested at all; `intents/rest.py`'s own
`resolve_rest` (called by `DM_Core.py`'s `_on_item_interaction_detected`, via
`intents/registry.py`'s own `HANDLERS` manifest — see `docs/architecture.md`'s own `intents/`
bullet) decides *how long* from the raw input itself, the same "NLP flags the intent, the
handler resolves the specifics" split every other free-standing intent's own module follows — a
phrase naming `night`/`dawn`/`morning` spends a whole day's worth of blocks
(`get_time_state()["blocks_per_day"]`), anything else spends exactly one block.
`intents/rest.py`'s own `narrate_rest` narrates the real per-party-member healed amounts and the
resulting day/night state, never inventing either.

## Travel

An optional `grid = {x, y}` field on a `[[location]]` opts it into overworld travel (`DM_Travel.py`)
— and, for that coordinate-bearing subset only, *replaces* its authored `[[location.exit]]`/
`return_to` graph entirely: `DM_Movement.py`'s `_resolve_travel_intent` checks the *current*
location for a `grid` field first, branching to `_resolve_grid_travel_intent` before ever
consulting the exit graph if one is present. A location with no `grid` field keeps resolving
through the ordinary exit graph completely unchanged — the two connectivity models never mix on
the same location.

**Known locations.** From a gridded location, any *known* location is reachable directly by
naming it (its own `name`, or a location-level `aliases` list — distinct from an exit's own
per-connection `aliases`) — no exit to author. `DMCore.known_locations` (a persistent, top-level
set, round-tripped through save/load) gates this: `_enter_location` adds every location key it
ever enters, gridded or not, and a scenario may also seed starting knowledge ahead of any visit
via its own `[scenario].known_locations` (`plains.toml`'s worked example seeds both of its
locations at once — "some other in-fiction means," here a map the player starts with, since
neither could otherwise ever be named before being visited once).

**Distance and speed.** Blocks for a trip are Euclidean distance between the two locations' own
`grid` coordinates, divided by the party's travel speed, rounded up (`_resolve_grid_travel_intent`)
— no fractional blocks, the same rounding rule HP/dice/bands already follow. `_party_travel_speed`
paces to the *slowest* currently-present `is_player`/`is_party` member's own `travel_speed` field,
falling back to `rules.toml`'s `[travel]` table (`default_speed`, 4 by default) for whoever doesn't
author one — a stat fully distinct from combat's own per-band `speed` (`DM_Movement.py`), which
never applies outside a room's own band line.

**Environments and the world map.** `Rules/Fantasy/environments.toml` is a flat, shared catalog
(`[[environment]]`, referenced by name) — each entry owns a `day_encounter`/`night_encounter`
table, the exact same weighted-choice shape `[[location.encounter]]`'s own `encounter` field
already uses (a real `[[entity]]`/`[[entity_template]]` name, the reserved `"nothing"`, or a
flavor-only string), resolved through the identical `_resolve_one_encounter` — just once per block
of travel (or of rest — see "Rest" above) instead of once on location entry.
`Rules/Fantasy/world_map.toml` places environments on the grid as named rectangular `[[region]]`s
(`resolve_region_environment(x, y)` finds whichever region's bounds contain a point); a gap between
authored regions has no environment at all, which is what "safe" looks like everywhere in this
design — no watch check, no encounter roll. `_resolve_grid_travel_intent` samples the *midpoint* of
each block's own leg of the straight line between origin and destination — true per-block sampling,
not a coarser origin/destination split — so a journey crossing multiple regions (not exercised by
`plains.toml`'s own single-region shipped map, but already correct for one) would roll from each
proportional to how much of the line it covers. `_resolve_environment_block` factors the actual
"roll this environment's own day/night table, then a watch check if it's hostile at night" step out
of this per-block loop so rest (a single fixed point, not a line) can reuse it unchanged.

Shipped worked example: `Rules/Fantasy/scenarios/plains.toml`'s `trailhead` (grid `0,0`) and
`border_stones` (grid `4,0`), both inside `world_map.toml`'s `"the open plains"` region (naming the
`"plains"` environment, whose day table includes `environments.toml`'s two new shared
`creatures.toml` wildlife entries, `"wild boar"`/`"coyote"`) — exactly far enough apart, at the
default travel speed of 4, to cost exactly one block.

**Arrival is deferred, not up front.** `_enter_location(destination_key)` no longer runs before the
per-block loop — it's the very last step, run only once every block has cleared without a hostile
interruption (`_finish_pending_travel`). A hostile block instead pauses the whole trip — see
"Pausing for a fight" below.

## Night watch and surprise

Built for both travel and rest, off the same shared `_resolve_environment_block`. A night block
(`is_daytime()` false) whose own encounter roll (above) actually placed a hostile entity is
followed by `_roll_night_watch` (`DM_Travel.py`): whichever currently-present `is_party` member (player
included) is next up in `DMCore.watch_rotation_index`'s own fixed rotation — a persistent,
top-level counter, round-tripped through save/load like `current_block` — rolls `observation`
(the same skill already pooled into initiative) against that block's environment's own
`watch_difficulty` via the ordinary flat-difficulty `resolve_action`. The index only advances on a
night a watch is actually rolled, cycling through the party across however many hostile nights
occur rather than every elapsed night. A party of one skips the roll and is always caught —
nobody to rotate a watch to while the sole traveler sleeps, and treating solo rest as automatically
safe would invert the usual "safety in numbers" logic.

A failed (or skipped) watch applies `rules.toml`'s new `[[condition]]` `"surprised"` (a heavier
`-2` dice penalty than `"stunned"`'s `-1`) to every present `is_party` member — the whole party was
caught off guard, not just whoever stood watch. Nothing in this engine interprets a condition's own
`duration` field as a real countdown (see `docs/combat.md`'s "Status and conditions"), so
`"surprised"` uses the same bespoke-expiry shape `"summon_expires_in"` already does:
`_expire_surprised_if_due` (`DM_Status.py`) dismisses it the first time `run_round_upkeep` runs
after it was applied, giving it exactly the one round of penalty `"duration = 1 round"` calls for.

## Pausing for a fight

Built for both travel and rest. A hostile block (`_resolve_environment_block` returning `True`)
no longer just gets folded into an otherwise-uninterrupted burst — it pauses the whole trip/rest
via `DMCore.pending_downtime`, a new persistent, top-level field (`None` when nothing's paused,
round-tripped through save/load like `current_block`) holding `{"kind": "travel"|"rest",
"blocks_total", "blocks_done", ...kind-specific fields}`. `_advance_pending_travel`/
`_advance_pending_rest` (`DM_Travel.py`/`DM_Time.py`) run (or resume) the per-block loop starting
from `blocks_done` instead of 0; a fresh hostile block updates `blocks_done` and returns
immediately instead of continuing, while running every remaining block clean hands off to
`_finish_pending_travel`/`_finish_pending_rest` for the actual arrival/healing.

**The ephemeral encounter site (travel only).** Since arrival is now deferred (see "Travel" above),
a mid-journey ambush happens before the party has really gotten anywhere — `_enter_encounter_site`
(`DM_Travel.py`) moves them into a small scratch scene (`ROAD_ENCOUNTER_KEY`, a single key reused
by every such pause rather than a freshly-minted one) to actually fight in. This deliberately does
**not** go through the ordinary `_enter_location`: naming an already-live ally in a *new* location's
own `"entities"` list would re-instance it as a fresh, occurrence-disambiguated copy of its
template (`_instance_entities`), silently orphaning the real live instance and its current
hp/`active_conditions` — and `_enter_location`'s freeform branch would also reset
`scenario_entities` down to just the player. `_enter_encounter_site` instead touches only
`current_location_key`/`known_locations`/`rooms` — `scenario_entities`/`persistent_entities` are
left completely alone, so the party (and the creature `_resolve_one_encounter` just placed) stay
exactly who they already were. Rest never needs this — it already happens at a real location.

**Resuming.** `DMCore._any_hostile_present()` (the same `is_hostile`/`get_current_hp>0` loop the
`blocked_by_enemies` gates already ran, now factored out into one shared predicate) is checked at
the end of every combat round (`_resolve_combat_round`); once nothing hostile remains,
`_resume_pending_downtime()` continues the paused trip/rest automatically, publishing
`item_interaction_resolved` itself (with exactly the fields `intents/travel.py`'s `narrate_travel`
/ `intents/rest.py`'s `narrate_rest` already expect) since there's no original `resolved` closure
left to call on a turn that started out having nothing to do with travel or rest at all. A *new*
travel/rest attempt while one is already pending is denied (`reason: "downtime_interrupted"`) —
checked in `DM_Movement.py`'s `_resolve_travel_intent`, ahead of its own grid/non-grid branch
(the encounter site deliberately carries no `"grid"` field, so a travel attempt issued from it
would otherwise fall through to the *non*-grid path and never reach a check placed only inside
`_resolve_grid_travel_intent`) — opportunistically resuming first, in case the blocking hostile was
actually cleared some other way (ex: ADaM despawning it), so a stale `pending_downtime` can't
deadlock every future trip.

**Save/load.** `load_scenario_definition()` rebuilds `self.locations` purely from TOML on every
load, so a synthetic `ROAD_ENCOUNTER_KEY` entry wouldn't survive a reload on its own —
`_enter_encounter_site` also stashes its own minimal `{key, name, description}` shape into
`pending_downtime["encounter_site"]`, and `load_game` reinjects it into `self.locations` right
after `load_scenario_definition()` but before its own existing `_enter_location(saved_location_key,
...)` call, so a saved `current_location_key` pointing at the site resolves to a real location
instead of silently degrading to an empty `{}` one. This only closes half the gap, though: an
entity `_resolve_one_encounter` instances is now also flagged `entity["ad_hoc"] = True`
(`DM_Encounters.py`) so it round-trips through `_collect_ad_hoc_entities`
(`docs/persistence.md`) with its *live* hp/conditions intact — previously only its bare presence in
`scenario_entities` survived a reload, silently resetting it to pristine template stats, a
pre-existing gap that a multi-turn-spanning, savable pause now makes load-bearing rather than a
rare edge case.

## Not yet built

Crafting's `days_required` day-extension and reopening character creation mid-game for training
stay exactly as undesigned-in-code as `extended-goals.md`'s own "Downtime" section left them — read
that section before starting either; it was fully grilled as a design pass, not just sketched.
