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
(see `docs/downtime.md`'s "Environments and the world map"), since both are the same underlying
weighted-choice mechanism.

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
pass, not just the single named point on the overworld grid `docs/downtime.md`'s own "Not yet
built" ad hoc destination generation idea produces. Runs deterministic post-generation
validation — every exit references a real room/band, every room is reachable from the start room — flagging breakage for the reviewer rather
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
