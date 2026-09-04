# LLDM — Downtime: The Block Clock, Rest, and Travel

Part of the [LLDM](../CLAUDE.md) docs — the coarse time-of-day clock underlying travel/rest, and
what's actually built of it so far. See "Not yet built" at the end for what's still open
(crafting's day-extension, training's reopening question, ad hoc destination generation, impassable
terrain) and the reasoning behind each — read it before extending anything here further.

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

Alongside that party-only fortitude heal, `apply_downtime_upkeep` (`DM_Status.py`) ticks
condition-driven upkeep (ex: `"regenerating"`'s own `upkeep_heal` — see `docs/combat.md`'s
"Status and conditions") for *every* living scene entity, not just the party — a creature's own
regeneration isn't a party privilege. Same one-aggregate-roll-scaled-by-blocks shape as the
fortitude heal, and the same `"recent_damage_tags"` gating `run_round_upkeep` already applies
mid-combat. This is what actually made `"regenerating"` mean something outside a fight at all —
previously the only tick was `run_round_upkeep`, called once per combat round and nowhere else.

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
paces to the *slowest* currently-present `is_player`/`is_party` member's own **effective** travel
speed (`_resolve_travel_speed`, `DM_Travel.py`) — a stat fully distinct from combat's own per-band
`speed` (`DM_Movement.py`), which never applies outside a room's own band line.

**Mounts and conveyance.** An entity can author `travel_speed` directly (`Rules/Fantasy/creatures.
toml`'s `horse`, `Rules/Zombie/items.toml`'s `car`) or defer to another entity via its own `mount`
field (a string, or a list for something with more than one provider) — `_resolve_travel_speed`
walks that reference recursively: a rider names their cart, the cart names its own team, so a
rider's effective speed resolves through the whole chain without needing to name the team
directly. Speed aggregates a multi-entry `mount` by **minimum** (paced to the slowest puller);
`DM_Rules.py`'s `get_carrying_capacity` mirrors this for load instead of speed, aggregating by
**sum** (every additional puller helps) — see `entity_schema.toml`'s own `mount` comment for why
the two use opposite operators. `get_current_bulk` folds a mounted rider's own weight (their flat
`bulk` field, plus their own carried gear if `[bulk]`'s `count_rider_gear` — default true — is set)
into whatever they're currently mounted on, so mounting is denied (`"bulk_exceeded"`) the same way
an over-full inventory is (`DM_Rules.py`'s `_would_exceed_mount_capacity`).

Reachable in play via the free-standing `"mount"`/`"dismount"` intents (`DM_Movement.py`'s
`_resolve_mount_intent`/`_resolve_dismount_intent`) — the player climbs onto a named,
currently-present, living, non-hostile entity, found by the same "search the raw input for a
known name" pattern formation uses; denied `"already_mounted"`/`"not_present"`/`"target_down"`/
`"target_hostile"`/`"not_a_mount"`/`"bulk_exceeded"` as appropriate. `"not_a_mount"`
(`_is_valid_conveyance`) is the eligibility gate that actually keeps this grounded: a target has
to author `travel_speed` directly, or already have a live `mount` chain of its own, or it's
denied even when present/alive/non-hostile — a friendly NPC with neither is not a mount just
because nothing else about it disqualifies it (mounting one would have no mechanical effect
either way, since anything without a real `travel_speed` already falls back to `[travel]`'s own
`default_speed`, but the fiction shouldn't imply climbing onto an ordinary person). Mounting
snaps the player's band to the mount's; `_sync_mount_bands` then keeps the pair together
afterward in both directions — the
player's own `advance`/`retreat` carries their mount along, and a mount's own behavior-driven
move (ex: the horse's own skittish `retreat` below 60% HP) carries its rider along too, no check
against being thrown. Losing a mount by any means (it dies, it's left behind) just silently
unwinds the relationship — `_resolve_travel_speed`/`get_carrying_capacity` both skip a stale
reference to something no longer present or alive, and dismounting/re-mounting carries no bespoke
penalty of its own; a deliberate choice against hardcoding a narrative consequence into the
actor/target-only trigger system (`resolution/Program_Interpreter.py`) that has no generic way to
reach "whoever currently has `mount` pointing at me."

**Persistence.** A mount is meant to survive both a save/load round-trip and an ordinary
location/room change mid-session — two originally-separate gaps, fixed independently:
`DM_Persistence.py`'s `save_game`/`load_game` round-trip `"mount"` as an ordinary per-instance
field now (same unconditional treatment `current_language`/`prompt_directive` already get), so
reloading no longer silently unmounts the player. Surviving an in-session location/room change is
a different problem, since `self.scenario_entities` gets rebuilt from scratch on every one of
those (`_populate_room`, `_enter_location`'s freeform branch) from whatever's *authored* in the
destination's own `"entities"` list — a dynamically-mounted horse was never part of that and would
otherwise just vanish the instant the player actually arrived anywhere new, even though grid
travel's own block/speed math (computed *before* `_enter_location` runs) already benefited from it
for that one leg. `DM_Rules.py`'s `_carry_mounts_into_scene` fixes this directly: called from both
`self.scenario_entities`-rebuilding sites, it walks the player's own live `"mount"` chain
(`_mount_chain` — like `_resolve_mount_targets`, but not filtered by presence/liveness, since it's
finding who to carry *into* the new scene, not resolving a stat off someone already confirmed to be
there) and appends anyone missing — no re-instancing needed, since a mount already has a live,
mutable copy in `self.entities` from whenever it was first mounted/hitched. `_sync_mount_bands` is
then also called from `_enter_location`/`enter_room` (on top of its existing `advance_or_retreat`/
`move_toward_or_away` call sites) once the player's own arrival band is finalized, so a carried-along
mount doesn't just appear in the new scene at some stale leftover band. Deliberately scoped to the
player alone (not every present `is_party` member) — "mount"/"dismount"/"hitch"/"unhitch" are
player-only intents today, so nothing else can actually have a `"mount"` field set through ordinary
play yet.

**Overload blocks movement, continuously.** `_would_exceed_mount_capacity` (checked once, at the
moment of mounting/hitching) isn't the whole story — gear picked up mid-ride, a second rider
mounting after the first, or a puller dying out of a team can all push a mount past capacity
*after* departure. `DM_Rules.py`'s `_is_mount_overloaded` (`get_current_bulk(mount) >
get_carrying_capacity(mount)`, always `False` for an uncapped mount) is re-checked on every actual
movement attempt, not just once: `DM_Movement.py`'s `advance_or_retreat` refuses to move at all
(returning `None`, a sentinel distinct from the legitimate empty-`moved`-list "no one else here"
case) while the player's own mount is overloaded, denied by `intents/advance_retreat.py` as reason
`"mount_overloaded"`; `DM_Travel.py`'s `_resolve_grid_travel_intent` runs the identical check ahead
of its own distance/block math, denied the same way. Both read `_resolve_mount_targets(player)[0]`
— the player's *immediate* mount (a horse, or a cart) — since `get_current_bulk`/
`get_carrying_capacity` are already fully recursive at that one node (a cart's own current load
already folds in its riders' riders; its own capacity already sums its whole pulling team), so
there's never a need to walk the chain again just to check for an overload somewhere in it.

**Hitching a vehicle.** A rider's own `mount` is always written by `"mount"`/`"dismount"`, but
nothing about *that* pair could ever populate a cart's own `mount` field — without a separate
mechanism, only a scenario/test author hand-writing one directly onto a cart's own data could ever
give it a team to defer to (a cart could only "mount" itself onto a horse, never the reverse). The
free-standing `"hitch"`/`"unhitch"` intents (`DM_Movement.py`'s `_resolve_hitch_intent`/
`_resolve_unhitch_intent`) are the player-facing way that gets built up during play instead:
`_named_present_entities_in_order` finds every currently-present entity named in the input, in
left-to-right reading order — for `"hitch"`, the first-named entity is the **puller**, the
second-named is the **vehicle** ("hitch the horse to the cart"), a fixed reading-order convention
rather than a guess based on either entity's own stats, so *direction* stays predictable regardless
of what either entity's data looks like. Whether the resulting pairing is actually *allowed* is a
separate question, gated on each entity's own data: the puller has to pass the exact same
`_is_valid_conveyance` eligibility check `"mount"` applies to its own target (`travel_speed`
directly, or an existing live `mount` chain), denied `"not_a_puller"` otherwise; the vehicle has to
have already been authored with a `"mount"` *key* at all — present, even if empty (`mount = ""` is
the shipped placeholder convention for a template meant to serve as a vehicle, ex: a cart with
nothing hitched to it yet) — denied `"not_a_vehicle"` if the key was never authored, so an ordinary
NPC nothing ever declared hitchable doesn't retroactively become one just because something got
hitched to it (closes the two-step version of the same gap `"not_a_mount"` closes for `"mount"`
directly: hitching a horse onto an arbitrary NPC, then mounting that NPC, would otherwise still
work). The puller's name is then promoted onto the vehicle's own `mount` (an authored empty string
→ a bare string; already a non-empty string or list → appended), the exact shape
`_resolve_mount_targets` already expects. Denied `"not_present"`/`"target_down"`/`"target_hostile"`/
`"not_a_puller"`/`"not_a_vehicle"`/`"already_hitched"` as appropriate — the liveness/hostility gates
apply to the puller only, since hitching up something actively trying to kill you makes no more
sense than climbing onto it. No bulk/capacity check of any kind: hitching only ever *adds* pulling
capacity, never something loading actual weight that would need gating the way mounting a rider
does. `"unhitch"` only needs the puller named — every present entity's own `mount` is searched for
a match, since there's normally only one vehicle to find it in.

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
caught off guard, not just whoever stood watch. Applied with `duration = "rounds", length = 1`
(see `docs/combat.md`'s "Status and conditions" for the full duration/length mechanism), so
`run_round_upkeep`'s own generic condition tick (`Combat_Resolution.tick_condition_durations`)
dismisses it the first round it's carried into, the one round of penalty a failed watch calls for.

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

**Crafting's day-extension.** Crafting already exists as an instant single-roll check (see
`docs/inventory-items.md`) — the downtime extension gives a recipe a `days_required` field, gating
completion on spending that many days, resolved as one roll at completion rather than a periodic
per-day roll (avoiding the swinginess a roll-every-day approach would add, the same reasoning the
one-aggregate-roll shape "Rest" above already follows). Scoped to locations with no active
environment for a first pass — crafting while camped somewhere dangerous is a real combination,
just not designed yet.

**Reopening character creation for training.** `spend_pip`/`spend_exp_on_skills`
(`Character_Creation.py`, see `docs/character-creation.md`'s "Training") already do the actual
mechanics — raising a skill by a pip costs XP equal to its own current dice count, rolling over
into an extra die at 3 pips — but the only door onto them is the character-creation screen, which
today only ever opens once, before a session's first game (see `docs/character-creation.md`'s
"Booting the game"). What's still genuinely undecided: **when** that screen (or an equivalent) can
reopen mid-game so accumulated post-chargen XP is actually spendable — a clock/downtime-adjacent
question in its own right — plus quest-driven XP (only defeating a hostile or surviving/disarming
a trap awards any today; see `docs/combat.md`'s "Experience (XP)").

**Ad hoc destination generation.** `known_locations` (see "Travel" above) gates travel on
knowledge, not a hand-authored connection — the natural extension is ADaM ad hoc-generating an
unknown destination on request, the same `tool_choice="auto"`/decline-biased shape
`docs/adam-improvisation.md`'s "Ad hoc entity creation and removal" already uses for items/
creatures. Not designed yet beyond that shape; other in-fiction ways to learn a location short of
ADaM generating one outright (dialogue, a sign) are equally still undesigned.

**Impassable terrain.** Terrain never blocks travel today — a straight line can cross an
unauthored gap, an ocean, or a mountain range exactly as easily as a plain, since a region (see
"Environments and the world map" above) only changes what gets rolled, not whether the trip is
possible. A real, explicitly deferred feature on top of this, not designed here.

**Build order.** Crafting's day-extension and training's reopening question are natural
fast-follows, independent of each other and of everything else in this document; ad hoc
destination generation is a fast-follow on top of `known_locations` specifically, independent of
watch/pausing entirely.
