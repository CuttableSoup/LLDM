# LLDM — Extended Goals

Part of the [LLDM](../CLAUDE.md) docs — not yet started, except where noted.

## Extended goals

Not yet started, except where noted. Each below was worked through as an actual design pass, not
just restated — several correct or extend what's currently shipped rather than describing a clean
gap.

**Random encounters, enemy generator** — procedurally populate a scene/room with creatures
instead of every encounter being scenario-authored. **Partially started**: `[[location.encounter]]`
(see "Random encounters") already rolls a weighted table of outcomes on entry, but the table
itself, and every entity/template it can resolve to, is still hand-authored — there's no
procedural encounter *design*, only randomized *selection* among authored options ("NPC
generation" separately fits a `generate = true` template's *stats* to a target CR at instancing
time, but the template itself — attitudes/behavior/abilities/equipment, whether/where it appears
at all — is still hand-authored too). The extension: a new reserved outcome kind in the weighted
table, alongside the existing real-entity-name/`"nothing"`/flavor-string trio, freely mixable with
hand-authored entries in the same table (an author can keep one specific hand-written boss and
leave the rest of the table open to generation) — matching exactly how `"nothing"` already
coexists with real names today. On a "generate" pick, reuses `_attempt_creature_conjuring`'s exact
machinery unchanged (`generate_ad_hoc_creature`, `fit_skills_to_cr`, the disposition/power enums),
decline path included — being automatic doesn't make a bad roll (a nonsensical CR/archetype
pairing) safe to force through. CR/archetype aren't left to bare `get_challenge_rating(player_name)`
math alone: a location/environment can optionally hint archetype/CR-multiplier via the same
`resolve_varied_value` shape `[[entity_template]]`'s own `hint`/`qualities` already use, so a swamp
skews toward low-CR beasts and a crypt toward undead rather than either rolling anything at any CR
by pure chance. This reserved kind works unchanged inside an environment's own day/night tables too
(see "Downtime"), since both are the same underlying weighted-choice mechanism.

**Scenario, quest, NPC, item, and location generators** — procedurally author the TOML data
itself rather than every scenario/entity being hand-written. Distinct in kind from every other
generation mechanism already designed elsewhere in this file: ad hoc items/creatures, NPC
stat-fitting, procedural encounters, and ad hoc destination generation are all *runtime* —
live during play, decline-biased, nothing reviewed before the player sees it. This bundle is
**offline authoring-assist** instead: a developer-run tool that drafts new TOML content —
an `entity_template`, an item catalog, a whole multi-room location — for a human to review, edit,
and commit like any other hand-authored `Rules/Fantasy/*.toml` file, never something a player
encounters unreviewed. Each tool is still built as a clean, typed, tool-call-shaped function from
the start (the same precedent `generate_ad_hoc_item`/`generate_ad_hoc_creature` already set for
being clean, LLM-callable units) so that exposing one to an LLM to operate at runtime later, if
ever wanted, is plumbing on top of an existing function rather than a rewrite — but the guardrail
posture for now is squarely "a human reviews it before it ships." Three of the five turned out to
be the real deliverables; the other two collapse or get explicitly deferred, below.

**NPC template generation** extends what "NPC generation" already does (fitting a `generate = true`
template's *stats* to a target CR at instancing time) to the rest of the template: its
`[entity.attitudes]`, `[[entity.behavior]]`, `abilities`, `equipped`, and `qualities.race` — all
still hand-authored today. Split into several focused calls (personality/attitudes, combat
behavior, loadout) rather than one combined one — not for the small-model reliability reasons the
runtime stat-fitting call is under (offline generation isn't fighting Ollama's tight timeout), but
because a human reviewer can regenerate just the behavior without discarding a personality they
liked. Behavior is authored free-form — no enum-constrained catalog of pre-built behavior
templates the way `npc_keywords.toml` constrains archetype choice, since that constraint exists
specifically to keep small, unsupervised, *runtime* models safe, and doesn't apply to a reviewed
offline tool — but still runs deterministic post-generation validation, checking that every
generated behavior entry's `action`/`requirements` actually resolve to a real skill/ability/field
and flagging anything that doesn't; free-form authoring is exactly the case most likely to
hallucinate a plausible-sounding but nonexistent name, and it's just as cheap to check as a
location's exit references (below). Equipment/abilities reference the real `items.toml`/
`spells.toml`/`techniques.toml` catalog by name only, never drafting new entries inline — keeps
this tool from overlapping with item generation, so reviewing an NPC template never also means
reviewing a surprise new weapon.

**Item catalog / loot-table generation** fills a shop's stock or a loot table ahead of time,
rather than reactively the way ad hoc item creation already does — the one item-shaped gap ad hoc
generation genuinely doesn't cover, since it only ever reacts to one specific player action on one
specific unmapped item. Two distinct modes: assembling from existing `items.toml` entries (the
common case), and, separately, drafting brand-new items when a theme genuinely calls for one nothing
in the catalog covers — kept as two separate calls so a new item draft gets reviewed on its own
rather than buried inside ordinary catalog assembly. Steered by a theme/tag hint plus a target
value budget, mirroring the archetype/CR-multiplier hint already designed for procedural
encounters — a value budget is the loot-table analog of challenge rating, budgeting treasure
richness the way CR budgets creature power.

**Location generation**, at its eventual full scope, generates a location's complete internal
structure — the whole `[[location.room]]` graph: multiple rooms, their exits/bands/connectivity,
each room's own `entities`/encounter table — a whole `crypt.toml`-shaped location in one authoring
pass, not just the single named point on the overworld grid "Downtime"'s own ad hoc destination
stub produces. Runs deterministic post-generation validation — every exit references a real
room/band, every room is reachable from the start room — flagging breakage for the reviewer rather
than shipping it silently: offline-and-reviewed means skipping the runtime-safety-critical
unsupervised guardrails, not skipping cheap programmatic sanity checks a human shouldn't have to
manually trace for. Each room is populated with concrete, named, already-existing entities/
templates chosen directly by the tool at authoring time — never the runtime "generate" reserved
encounter-kind (that stays a live, play-time-only mechanism, redundant here since the location is
already being authored as a fixed, reviewed artifact) and never a mid-pass invocation of the NPC or
item generators themselves (reference-only, consistent with equipment/abilities referencing the
real catalog above) — a human wanting a brand-new creature in a generated location runs NPC
generation first, reviews it, and only then generates the location referencing it.

**Scenario generation collapses to no dedicated tool.** It was originally framed as "compose the
other three," but once NPC/item/location generation exist, wiring their already-reviewed output
into a `[scenario]` table — `start_location`, entity placement, the scenario blurb — is just
ordinary manual TOML authoring, exactly like a human already does today referencing hand-authored
locations. Pure wiring with no generated content of its own isn't tedious enough to justify a
tool of its own; introducing one would be automation for a step that doesn't actually need it.

**Quest generation stays explicitly parked.** No quest/objective data model exists anywhere in
this codebase today — confirmed, not assumed. Unlike the other four, there's no existing scaffolding
to extend (ad hoc items/creatures, NPC stat-fitting, and now the three generators above all had
something real to react to); "generate a quest" can't be meaningfully scoped before "what a quest
even is as data" — tracking state, completion conditions, rewards — is designed as its own
prerequisite topic, independent of hand-authored vs. generated. Not attempted here.

**ADaM acting proactively, not just when addressed by name.** Today ADaM only reacts to an
explicit "adam ..." turn. Since ADaM already *is* the closest thing this game has to a "dungeon
master" persona — a prior version of this goal imagined a separate DM voice distinct from both
the narrator and ADaM, but that's simply ADaM itself, and "speaking directly to the player when
necessary" is exactly what proactive triggering already means — a fuller version of ADaM would
also initiate content unprompted (a complication, a random encounter, a pacing beat), not just
this reply-only channel. Deliberately not an always-eligible-to-act switch: an ambient "decide
whether to act this turn" hook would run on every turn, and live probing found the currently-loaded
model complies with an under-specified request readily unless explicitly told not to. If built,
favor a narrow, rate-limited trigger fired only at specific, already-instrumented moments (room
entry, N turns without incident) with its own decline-biased prompt. Not started.

**Tools that the LLM may call to directly interact with the scene.** **Partially started**:
ADaM's own ad hoc generation (see "Ad hoc entity creation and removal") already gives the LLM real
`tool_choice="auto"` function-calling access to create items/creatures and remove/edit entities —
but only via `LLM_Client.py`'s narrow, single-purpose, DMCore-triggered calls
(`generate_ad_hoc_item`/`generate_ad_hoc_creature`/`decide_entity_removal`/`decide_entity_edit`).
`LLM_Core.py`'s own narrating GM voice — the one actually speaking to the player on every ordinary
turn — never sends a `tools` payload at all and has no scene-mutation ability of its own; this goal
is really about giving *that* voice general tool access, not another bespoke gated call. Scoped to
start with the exact same four ADaM tools, just reachable on an ordinary turn instead of only when
ADaM is addressed — reusing already-built, already-decline-biased tool definitions is far lower
risk than inventing new tool surface at the same time as wiring up general access; a broader
toolset is a separable future step once "the narrator can call tools at all" is proven out. The
real guardrail question is what replaces "only runs when addressed" as the rate limiter, since a
narrator with tools on every ordinary turn has no such natural limit — the answer is the same
narrow/rate-limited/already-instrumented-moment philosophy above for proactive ADaM, cross-
referenced rather than re-derived, since the underlying risk (an under-specified request gets
readily complied with) is identical. Whatever tool call happens stays a pre-step in the same
resolve phase ADaM's own calls already happen in — the engine's strict resolve-then-narrate
ordering (see "Action resolution") is preserved rather than letting the text-generating call
itself mutate state mid-stream, which would make narration the one call in the entire engine that
both writes prose and mutates world state at once.

## Downtime: travel time, rest, crafting, training

Design for a coarse time-of-day clock underlying travel, rest, and (eventually) crafting/
training — inspired by Pathfinder's downtime rules. Fully grilled (see the design-tree interview
this section was built from); recorded here as the target shape, not a plan that was ever meant to
build in one pass — see "Suggested build order" at the end.

**Built**: the block clock primitive, a first basic slice of rest (heal-on-rest, no environment/
watch gate yet), and grid-based travel with environments/world map — see `docs/downtime.md` for
what's actually shipped and exactly where it diverges from the sketch below (day/night is read off
real elapsed hours against a `daylight_hours` tunable, not fixed block-index parity, since the
clock's own `[time]` rules.toml table exposes `daylight_hours` as a real, load-bearing value rather
than pure flavor). Still unbuilt: night watch/surprise, crafting's day-extension, and training's
reopening question.

**The block clock.** An 8-hour "block" (three per day) is the atomic time unit — fine enough for
meaningful travel/rest granularity, coarse enough that a multi-day journey doesn't mean
turn-by-turn play. A day is just three blocks, the unit a longer downtime task (crafting, training)
would consume instead of spending blocks directly. This clock is a new, persistent, top-level
`DMCore` field (round-tripped through `save_game`/`load_game` the same way `current_room_key`/
`location_runtime` already are — a downtime system that forgot elapsed time on reload would let a
save-scum trivially dodge a bad watch roll, undermining the whole mechanic) and a fully separate
axis from combat's own `round_number` — no shared upkeep hook with `run_round_upkeep`
(`DM_Status.py`), since one is tactical/per-turn and the other strategic/per-watch. Entering combat
mid-block, pausing the block clock, and auto-resuming travel toward the same destination once
combat resolves (mirroring how the existing hostile gate already blocks a *room* transition until a
fight clears rather than canceling the player's stated intent) is still unbuilt, now that travel is
real: today's shipped travel (`docs/downtime.md`) rolls every block's own encounter in one
uninterrupted burst right after arrival instead, a deliberate simplification recorded there, not
this design.

**Overworld locations get grid coordinates; the grid *defines* connectivity for them — built.**
See `docs/downtime.md`'s "Travel" for the shipped mechanics (`DM_Travel.py`). An optional
`grid = {x, y}` field applies only to `[[location]]` entries meant as overworld travel endpoints —
a dungeon's own internal `[[location.room]]` graph never needs one, the same way rooms already opt
into band positioning only when it matters. For this coordinate-bearing subset, the grid itself
replaces the authored `[[location.exit]]` graph: any two known locations are reachable directly,
with no exit to author — a genuinely different connectivity model from non-gridded locations,
which keep resolving through the existing exit graph completely unchanged.
**Travel is still gated, just on knowledge instead of a hand-authored connection**: naming a
destination only resolves if it's in the player's own `known_locations` (populated by having
visited it, or a scenario's own `[scenario].known_locations` seeding starting knowledge ahead of
time — `plains.toml`'s shipped worked example), denied the same way an unrecognized name is today
otherwise. **Extended goal on top of this, still not built**: ADaM ad hoc-generating an unknown
destination on request, the same `tool_choice="auto"`/decline-biased shape "Ad hoc entity creation
and removal" already uses for items/creatures — not designed yet, just the natural next step now
that `known_locations` is a real concept to insert into (dialogue/a sign as other in-fiction ways
to learn a location, short of ADaM generating one outright, are equally still undesigned).

**Distance and travel speed — built.** A new stat, distinct from combat's own band-movement
`speed` — authored as a `rules.toml` `[travel]` default (`default_speed`) with an optional
per-entity/per-race `travel_speed` override. A party paces to its *slowest* `is_party` member, not
just the player's own value (`_party_travel_speed`). Block count for a journey is Euclidean
(Pythagorean) distance between the two locations' `grid` coordinates, divided by the party's
travel speed, rounded up — no such thing as arriving a fraction of a block early, the same
"no fractional units" rounding rule HP/dice/bands already follow.

**Environments — built.** `Rules/Fantasy/environments.toml` — the same "shared catalog referenced
by key" shape `npc_keywords.toml` already is. Each named environment (`plains`, shipped, is the
only one authored so far) owns a day encounter table, a night encounter table, and its own watch
difficulty (authored already, not yet read by anything — see "Night watch and surprise," still
unbuilt, below). The tables themselves are the exact same weighted-choice/`resolve_varied_value`
machinery and three-way outcome (real `[[entity]]`/`[[entity_template]]` name / reserved
`"nothing"` / flavor-only string) `[[location.encounter]]` already uses, unchanged — "some travel
is uneventful" is just weighting `"nothing"` heavily, no new mechanism.

**Placing environments on the world — built.** `Rules/Fantasy/world_map.toml` — named *regions*
(a rectangle, naming one environment) rather than a per-tile dict, matching how every other table
in this codebase stays hand-sized rather than combinatorial. A coordinate resolves to whichever
region contains it (`resolve_region_environment`); a gap between authored regions defaults to "no
environment," which is what "safe" looks like everywhere in this design — there's deliberately no
separate safe flag anywhere (not on rest, not on crafting): absence of an environment already
means no watch check and no encounter roll, full stop. Terrain never *blocks* travel in this
design — a straight line can currently cross an unauthored gap, an ocean, or a mountain range
exactly as easily as a plain, since a region only changes what gets rolled, not whether the trip
is possible. Impassable terrain is a real, explicitly deferred feature on top of this, not
designed here.

**Per-block environment sampling — built.** True per-block sampling along the straight line
between origin and destination — one sample point per block (its own leg's midpoint), each
resolved against `world_map.toml` independently — not a coarser "first half is the origin's
environment, second half is the destination's" approximation. A journey crossing three regions
rolls from all three, proportional to how much of the line each covers; the coarser split was
considered and rejected once the region lookup's own "defaults to safe" fallback made it clear
every sample point always resolves to *something* — the two-bucket split would only have thrown
away information the map actually contains. `plains.toml`'s own shipped map is single-region, so
this hasn't yet been exercised by a real multi-region journey, but the sampling math itself doesn't
special-case the single-region case at all.

**Night watch and surprise.** On a night block, whichever `is_party` member is next up in a fixed
rotation (not player-chosen, not always the same member) rolls `observation` against that block's
*current* environment's own watch difficulty — the same flat-difficulty check `[entity.test]`
locks/traps already use, not a new check type; `observation` was picked over introducing a new
skill specifically because it's already the game's established notice/perception skill
(`[entity.notice]` on hidden traps, the `dodge + observation` initiative pool in `rules.toml`).
Success, or a non-hostile roll, changes nothing. Failure paired with a hostile encounter that
block tags the party with a new `surprised` `[[condition]]` entry (`rules.toml`) carrying a heavy
`modifier` and `duration = 1 round` — reusing the exact condition/modifier/duration machinery that
already makes `wounded`/`stunned` mechanically real (see "Status and conditions") rather than
inventing a turn-skip: initiative here only orders narration, so there's no "first turn" to deny
the way a true turn-order game would deny one. **A solo player never stands watch at all** — with
nobody to rotate to, any hostile night result always applies `surprised` unconditionally, no
`observation` roll attempted. (The alternative — resting suppresses the encounter roll entirely
for a solo traveler, the same as an absent environment — was rejected: it would make solo travel
strictly *safer* than traveling with a party, inverting the usual "safety in numbers" logic.)

**Rest — built, basic slice only.** Rest consumes one or more blocks and heals via the ordinary
`apply_healing` call (so the existing wound-tier condition dismiss-sweep applies for free), scaled
by `fortitude` — picked over `medicine` because this is the body's own recovery, not a caregiver
treating someone else's wound; `medicine` stays free for some other future check that actually
wants a caregiving/treatment framing. See `docs/downtime.md` for the shipped mechanics (the
`"rest"` free-text intent, one aggregate roll over the whole rest rather than per-block).
**Still unbuilt**: resting overnight anywhere with an active environment going through the same
per-block watch/encounter machinery travel now uses (environments exist as of "Travel," below) —
`rest` itself still never consults `resolve_region_environment`/an environment's own tables at
all, a deliberate scope cut when basic rest first shipped, not an oversight; the "absence of an
environment means safe" default (above) is what will actually exempt an inn or a cleared room once
this is wired up, not a bespoke flag.

**Crafting.** Already exists as an instant single-roll check (see "Inventory and items"). The
downtime extension: a recipe gains a `days_required`, and completion gates on spending that many
days, resolved as one roll at completion rather than a periodic per-day roll (avoids Pathfinder's
own swinginess) — scoped to locations with no active environment for a first pass, since crafting
while camped somewhere dangerous is a real combination but not designed yet.

**Training.** The core mechanics now exist — raising a skill by a pip costs XP equal to its own
current dice count, rolling over into an extra die at 3 pips (`spend_pip`/`spend_exp_on_skills`,
`Character_Creation.py`; see `docs/character-creation.md`'s "Training") — but only reachable from
the character-creation screen, which today only ever opens once, before the first game a session
has (see "Booting the game" above). What's still genuinely deferred: **when** that screen (or an
equivalent) can be reopened mid-game so accumulated post-chargen XP is actually spendable — a
clock/downtime-adjacent design question in its own right, not yet decided — and quest-driven XP
(only defeating a hostile entity, or surviving/disarming a trap, awards any today; see
`docs/combat.md`'s "Experience (XP)").

**Data conventions.** `environments.toml` and `world_map.toml` are now real sibling files alongside
the existing `Rules/Fantasy/*.toml` catalogs (see `docs/downtime.md`'s "Travel"); each environment
already authors a `watch_difficulty` field for the future watch formula, unread by anything yet.
Block length/blocks-per-day/daylight-hours and travel speed are folded into `rules.toml`'s own
`[time]`/`[travel]` tables (see `docs/downtime.md`) — mirroring exactly how `[[status]]`/
`[[condition]]`/`[[initiative]]` already centralize tunables there rather than living as Python
constants; the heal formula needed no table of its own beyond `[time]`, since it reads straight
off the resting entity's own `fortitude` skill.

**Suggested build order.** Clock primitive, basic rest, and grid/environment/distance travel are
all done (see `docs/downtime.md`). Night watch/surprise is next — built almost entirely from
reused machinery (flat-difficulty tests, condition/duration) plus the one genuinely new piece, the
watch formula itself, since every environment already authors its own `watch_difficulty`; folding
an environment/watch gate into the existing `rest` once watch exists is a natural fast-follow on
top of *that*. Crafting's day-extension and training's whole design are natural fast-follows too,
not blockers to building watch; `known_locations`-gated ad hoc destination generation is a
fast-follow on top of travel, independent of watch entirely.
