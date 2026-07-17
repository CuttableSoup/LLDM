# LLDM — notes to self

An autonomous dungeon master: player types free-text actions, NLP maps them to a skill,
a simplified D6 (West End Games) engine rolls dice and resolves outcomes, and a local LLM
(currently Gemma via LM Studio at `http://127.0.0.1:1234`) narrates what happened. Everything
— skills, entities, items, spells, rules — is data-driven via TOML in `Rules/Fantasy/`.
Background/design intent lives in `notes.txt`; read that for the full RPG-system writeup
(range bands, attitudes, wound philosophy, extended goals). This file is the technical/architecture
picture plus gotchas discovered while building it.

## Architecture

Six modules wired through `Event_Bus.py`, a minimal synchronous pub/sub bus (`publish` calls
every subscriber immediately, in whatever thread called `publish`). `LLDM.py` boots them in
this order: `NLPCore`, `LLMCore`, `GUICore`, then `DMCore` last (it publishes `rules_loaded`
during `__init__`, so everything that needs it must already be subscribed).

- **`DM_Core.py`** — the rules engine; this is where almost all the real logic lives now, see
  "Combat loop" below. The `DMCore` class's implementation is composed from domain mixins in
  sibling files, each holding one concern: `DM_Rules.py` (TOML/scenario loading),
  `DM_Combat.py` (dice rolling, opposed checks, damage, ability/behavior resolution),
  `DM_Status.py` (the status/condition system and entity tests), `DM_Inventory.py`
  (currency/item transfer), `DM_Social.py` (attitudes and character description), and
  `DM_Persistence.py` (save/load). Because Python's MRO flattens every mixin method onto the
  one `DMCore` instance, every `dm_core.<method>(...)` call site elsewhere in the codebase (and
  in `test_unit.py`, which unit-tests most of these methods directly) is unaffected by which
  file actually defines a given method. `DM_Core.py` itself is reduced to `__init__` (boot
  wiring) plus the two real event handlers (`_on_action_detected`, `_on_item_interaction_detected`)
  and their direct helpers — the pieces that orchestrate calls across every mixin and don't
  belong to any single domain.
- **`NLP_Core.py`** — `sentence-transformers` (`all-MiniLM-L6-v2`) embeds each skill's
  name/description/keywords as separate phrases (not just one embedding per skill, to avoid
  dilution), then cosine-matches player input against all phrases. On `user_input_submitted`,
  publishes `action_detected` with the best-matching skill + score — but only if that score
  clears `self.confidence_threshold` (`0.5`); below it, `map_to_action` returns `(None, score)`,
  and `_on_user_input` publishes `action_not_understood` instead. It also separately matches
  free text against item names (`map_to_item`) for "examine"/"take" — see "Examining and
  taking items" below; that path runs *before* skill matching and, if it fires, skips it
  entirely for that input.
  **Don't let a low-confidence input fall through to complete silence** — with nothing
  subscribed to `action_not_understood`, the player types something, nothing ever happens, and
  it looks exactly like the app hung. `LLMCore` subscribing to it (below) is what closes that
  loop; if you ever add a *new* no-match branch here, wire its feedback the same way.
  **The 0.5 threshold rejects most naturally-phrased social input**, not just true
  non-actions — adding any topic clause to a sentence dilutes the whole-sentence embedding
  enough to drop it below 0.5 even when the phrasing is genuinely social. Two mitigations in
  `map_to_action`, both scoped to skill matching only (not `map_to_item`/`map_to_target`):
  `_generate_match_candidates` builds alternate, less-diluted phrasings of the same input
  (truncated at a topic-clause marker like `" about "`/`" regarding "`, or split on
  punctuation) and scores all of them against the skill-phrase bank, taking the best
  `(candidate, phrase)` pair rather than only ever trying the full sentence.
  `_match_by_keyword` is a second, independent fallback: if every candidate still misses
  `confidence_threshold`, a literal whole-word hit against the matched skill's own
  `skills.toml` `keywords` list can still rescue it — gated on that skill's own best embedding
  score clearing the much lower `keyword_fallback_floor` (`0.2`), so a coincidental keyword
  collision on an unrelated sentence doesn't get accepted on keyword evidence alone. **Not a
  full fix** — a phrasing with no literal keyword and no candidate that scores well on its own
  still falls through to `action_not_understood`. See `TestNlpConfidenceThreshold`'s
  `test_keyword_fallback_rescues_a_below_threshold_literal_keyword_hit` and
  `test_alternate_phrasing_candidate_rescues_a_diluted_sentence` for the mechanism in
  isolation.
- **`LLM_Core.py`** — posts to LM Studio's OpenAI-compatible `/v1/chat/completions` on a
  background thread, rolling 100-message context window. Subscribes to four narration
  triggers rather than raw input — see "Narration triggers" below.
- **`GUI_Core.py`** — Tkinter window: history pane + tabbed Party/Notes/Map/Debug panels.
  Only History (via `llm_response_ready`) and Debug (via `rules_loaded`) are actually fed
  data; Party/Notes/Map methods exist but nothing publishes to them yet.
- **`Textual_Core.py`** — a parallel, headless-testable mirror of GUI_Core's output (see
  "Textual mirror" below). Not part of the normal `LLDM.py` boot sequence; run standalone.
- **`Logger.py`** — subscribes to `log_info`/`log_error`, prints with timestamps.

## Combat/action loop

`user_input_submitted` → `NLPCore` → `action_detected {skill, score, input}` → `DMCore`
resolves it → either `round_resolved` (combat) or `action_resolved` (no combat) → `LLMCore`
narrates → `llm_response_ready` → GUI/Textual display it.

Inside `DMCore._on_action_detected` (see its own docstring in `DM_Core.py` for the full
parameter/target-redirect contract):
1. Resolves against `self.current_target` — the player's persisted combat target (see
   "Targeting and multi-actor combat rounds" below), not a freshly-recomputed value.
2. If there's a target, `resolve_opposed_action`: finds the defender's **highest-rated**
   matching skill from the attacker's skill's `opposes` list (rating = `dice*3 + pips`, since
   3 pips = 1 die per the D6 system), rolls it as the difficulty. No target/no matching skill
   → difficulty defaults to **0** (this default-to-zero convention is used everywhere a
   difficulty isn't supplied — see `resolve_action`'s signature).
3. On success, `find_attack_ability` looks for an equipped weapon or innate ability matching
   the skill and carrying `damage_value`, then `calculate_damage` rolls damage, resolves the
   `bonus` field (plain number or `"user.<rule>"` reference into `rules.toml`, e.g.
   `strength_damage`), subtracts armor `damage_reduction` (matching `damage_tags` against
   equipped items' `armor_tags`), and applies net damage to HP via `apply_damage`.
4. `apply_damage` also calls `evaluate_statuses(entity_name, "on_damage")` — see below.
5. **"Combat" means a target is present *and* `is_hostile(target_name, player_name)`** —
   not just target presence. `is_hostile` reads the first ("disposition") value of
   `get_attitude(entity_name, toward_name)`, which resolves an entity's `[entity.attitudes]`
   block: a `name` override (specific individual) beats a `supertype` override (broad
   category) beats `default`. **An entity with no `attitudes` table at all defaults to
   neutral (disposition 0), which still counts as hostile/combat-ready** — only a positive
   (Friendly-leaning) disposition opts an entity out of combat routing. This is why `wolf`
   (no attitudes data) and ad-hoc test entities like `practice_dummy` still batch into
   `round_resolved` without needing any attitude data added — only entities meant to be
   dialogue partners (ex: `npcs.toml`'s `innkeeper`, disposition `30`) need an explicit
   positive default. `scenario_entities` still never removes a dead entity (it stays in the
   list forever, at 0 HP), but `current_target` itself is never left pointing at one past the
   end of a round — see "Targeting and multi-actor combat rounds" below for how that's now
   handled. If in combat, `self.round_number` increments and the result publishes as
   `round_resolved` (narrated once, as a round summary). Otherwise (no target, or a
   non-hostile target like a tavern NPC) it publishes immediately as `action_resolved`
   (narrated per skill use) — this is also the path a *dialogue* skill check (ex: `charisma`)
   takes against a friendly NPC. Only one player action is resolved per call, so a "round" is
   one player action plus every other living scene entity's own turn (enemies attacking the
   player, allies attacking `current_target`) — see "Entity behavior (enemy turns)" and
   "Targeting and multi-actor combat rounds" below. The `round_resolved` payload carries the
   single player result plus `"turns"`, a list of every other participant's resolved action
   (each tagged with `"actor"`), covering allies and enemies alike in one round.

## Narration triggers

`LLMCore` subscribes to four events, each with its own prompt framing, sharing outcome-text
building via `_describe_outcome` and background-fetch plumbing via `_queue_narration`:
- `scenario_loaded` → `generate_scene_intro` — fires once, published by `DMCore.__init__`
  right after `load_scenario()`. Narrates the opening scene from the scenario's
  `name`/`description`/`characters`, and also stashes all three on
  `self.scenario_name`/`self.scenario_description`/`self.scenario_characters`. Not re-triggered
  by anything else today (no mid-session scenario reload path exists).
- `round_resolved` → `generate_round_response` — combat narration, once per round (see above).
- `action_resolved` → `generate_response` — non-combat narration, once per skill use.
- `action_not_understood` → `generate_clarification_response` — published by `NLPCore` when
  the player's input scores below `confidence_threshold`, i.e. no skill matched at all. Unlike
  the other three, there's no roll/damage to describe (`_describe_outcome` is never called
  here) — the prompt just tells the LLM what the player said didn't resolve to any action, and
  asks for a short in-character acknowledgment. This is the fix for player input silently going
  nowhere (see the `NLP_Core.py` bullet above) — without it, an unmatched input produced zero
  events past `NLPCore` and the app looked stalled.

**The scenario setting AND character roster are injected into the system message on every
request**, not just the one-shot intro — `_queue_narration` appends
`f" Setting: \"{scenario_name}\" - {scenario_description}"` and, if any exist,
`" Characters: " + " | ".join(scenario_characters)` to the system prompt. This is what keeps
combat/action narration grounded (ex: still mentioning the arena's crowd and dirt floor, or an
NPC's known backstory) turns after the intro message has scrolled out of the rolling
100-message `context_window`, rather than relying on that one message alone.

**Where the character roster comes from:** `DMCore.describe_character(entity_name, toward_name=None)`
(in `DM_Social.py`) builds a flavor-text line per entity out of purely descriptive TOML fields —
`description`, `qualities`, `memories`, `quotes` — deliberately excluding mechanical data
(skills/dice), since this is meant to tell the LLM *who* someone is, not how they roll.
Entities with none of those fields (ex: `wolf`, which is pure mechanics) return `""` when
called with no `toward_name` — but every call site actually passes `toward_name=player_name`
(see "Attitude phrases" below), which gives even a purely-mechanical entity something to say
(how it feels about the player), so in practice `wolf` does contribute a roster line today.
`DMCore.__init__` builds the `characters` list for every entity in `self.scenario_entities` and
includes it in the `scenario_loaded` payload; `_on_action_detected` separately attaches
`result["defender_details"] = describe_character(target_name, toward_name=player_name)` per
action, which `_describe_outcome` folds into the per-turn outcome text — belt-and-suspenders
against the persistent roster ever being stale (ex: an NPC added to the scene after the intro
fired, though nothing does that yet).

**Player is still effectively singular** — there's no party/character selection system yet —
but `self.player_name` is no longer a bare string literal. `_resolve_player_name()` (in
`DM_Rules.py`) scans loaded entity templates for the one with `is_player = true` (`characters.toml`'s
`gladstone`) and uses its name, raising `ValueError` if none is marked (same "fail loudly on
missing data" convention as `load_scenario_definition`'s missing-file check). This runs once in
`__init__`, right after `load_rules()` and before scenario instancing — not per-`load_scenario()`
call — so ad-hoc test scenarios that omit `gladstone` entirely (ex: `TestCombatLoop`'s
`practice_dummy`-only scenarios) still keep the `player_name` they booted with, unchanged from
the old hardcoded behavior. Swapping the active player character still means editing which
template has `is_player = true`, not a runtime selection UI.

## Attitude phrases

`get_attitude`'s six-value array (`disposition, trust, confidence, respect, obligation,
intimacy`, each -100..100 nominally) is exactly the kind of thing an LLM can't calibrate on its
own — `"38 disposition"` means nothing to it, but `"is warm and well-disposed toward them"`
does. `[[attitude_tier]]` (`rules.toml`) is a small band table, same general shape as
`[[range_modifier]]`: seven tiers (`hostile`, `unfriendly`, `wary`, `neutral`, `warm`,
`friendly`, `devoted`), each a `{name, minimum, maximum}` range plus one phrase per axis.
`DMCore.get_attitude_tier(value)` clamps the value to **[-150, 150]** first — headroom past the
nominal -100..100 range for whenever attitudes get modified at runtime (nothing does yet, but
the two outer tiers are already sized to absorb it: `hostile` is -150..-100, `devoted` is
100..150, both 50 wide, vs. 40 for the five tiers in between) — then returns the first tier
whose range contains it, in TOML declaration order. **A value sitting exactly on a shared
boundary resolves to whichever tier is declared first** — ex: -100 matches `hostile` (declared
before `unfriendly`), and 100 matches `friendly` (declared before `devoted`), not the tier you
might assume from the number alone. Same convention as `choose_behavior`'s first-match-wins.

`DMCore.describe_attitude(entity_name, toward_name)` calls `get_attitude` and looks up each of
the six values against its own axis's phrase in the matching tier, joining them into one
sentence (`"Attitude toward gladstone: is openly unfriendly toward them, is deeply suspicious
of their motives, ..."`). Because `get_attitude` already returns a sensible default
(`[0, 0, 0, 0, 0, 0]`, i.e. every axis lands in `neutral`) for any entity with no
`[entity.attitudes]` table at all, this works for every entity, not just ones with authored
attitude data — which is what lets `describe_character` fold it in as one more optional part,
alongside description/qualities/memories/quotes, whenever a `toward_name` is given (skipped if
`toward_name == entity_name`, since describing an entity's attitude toward itself is
meaningless). `creatures.toml`'s `fire elemental`/`wolf` have no `[entity.attitudes]` block and
never will need one just for this — they still surface a full six-axis "neutral" reading, which
is honest (no attitude data *is* neutral) rather than nothing at all.

## Scenario instancing

Scenarios live in `Rules/Fantasy/scenarios/*.toml` (`arena.toml`, `tavern.toml`, `dungeon.toml`,
each a single `[scenario]` table), separate from the flat `load_rules` scan of `Rules/Fantasy/*.toml`
that loads skills/entities/rules. They're kept in their own subdirectory specifically so
multiple scenarios can coexist as named files — the flat scan only keeps whichever
`[scenario]` table it reads last, so if a scenario file lived at the top level next to a
second one, one would silently overwrite the other.

`DMCore.__init__(event_bus, scenario_name="arena")` picks which one loads:
`load_scenario_definition(scenario_name)` reads `scenarios/{scenario_name}.toml` directly via
the module-level `scenario_file_path(scenario_name)` helper, and **raises `FileNotFoundError`
if the name doesn't match a file** — this is deliberately fatal rather than the
`load_rules`-style "log and continue" pattern used elsewhere, because silently continuing
with an empty `self.scenario` used to let `LLMCore` narrate an opening scene with no
name/description, which the LLM would happily hallucinate (ex: a "featureless gray void")
with zero indication anything had gone wrong. `load_scenario()` then turns the scenario's
entity list into independent instances: deep-copies each named template, tags it with its
scenario `band`, and disambiguates duplicates (`wolf`, `wolf_2`, ...) so two wolves don't
share one HP pool. Each instance also gets its own `entity_id` field matching its unique key,
so identity travels with the dict itself, not just the `self.entities` lookup key.

`LLDM.py` exposes this as a CLI arg: `python LLDM.py [scenario]`, default `arena` (ex:
`python LLDM.py tavern`). `main()` checks `scenario_file_path(args.scenario)` itself and exits
with a clean `Error: scenario '...' not found...` message *before* constructing any cores —
this is what actually avoids wasting `NLPCore`'s ~15-20s `sentence-transformers` load on a
typo'd scenario name, rather than relying on `DMCore`'s exception (which would still work,
just after that wait, and end in a raw traceback instead of a clean message).

If you reassign `self.scenario` at runtime (tests do this to inject fake targets), you must
call `load_scenario()` again — it's not re-derived automatically. Tests that don't care which
named scenario file backs it (ex: `TestScenarioLoading`'s ad-hoc single-wolf scenarios in
`test_unit.py`) still just assign a plain dict rather than adding a new file under `scenarios/`.

`_get_target_name()` still always returns the *first* non-player instance — that's now
deliberately reserved for non-combat interaction resolution (item interactions, entity tests
against objects like the chest), which never needs hostility-awareness. Combat targeting
itself moved to `self.current_target` (see "Targeting and multi-actor combat rounds"); no
band/range awareness yet.

**`self.entities` holds templates and live instances under the same keys, and instancing
overwrites the template slot.** For a single-occurrence entity (ex: `gladstone`, or the first
`wolf` in a scenario with only one), `instance_name == template_name`, so
`self.entities[instance_name] = instance` in `load_scenario()` replaces
`self.entities["wolf"]` — the pristine TOML-loaded template — with the live, mutable instance.
Only a *second* occurrence gets a distinct key (`wolf_2`) that doesn't collide with the
template. This matters beyond just this section: `load_game` (see "Saving and loading") has
to re-run `load_rules()` before re-instancing, precisely because calling `load_scenario()`
alone would silently instance from whatever's currently sitting in `self.entities["wolf"]` —
which, after the very first load, is this same live instance, not fresh disk data.

## Saving and loading

Two sibling JSON files per named save slot, `Saves/<slot_name>/dm_state.json` and
`Saves/<slot_name>/llm_state.json`, each written and read independently by `DMCore` and
`LLMCore` respectively. **This is deliberately not one combined file.** `LLMCore.context_window`
(the rolling narration transcript) is the only piece of cross-core data a save needs, and
`EventBus` is intentionally a pure fire-and-forget pub/sub bus with no request/response
mechanism (`publish` discards callback return values) — extending it just for this, or giving
`DMCore` a direct reference to `LLMCore`, would break the loose coupling the whole app is built
on (every core today only ever talks to any other core through events). So instead, each core
owns and persists its own slice under the same slot name, triggered by the same two events.

**Trigger:** `save_requested`/`load_requested` (`{"slot": slot_name}`), published from two
independent places that both funnel into the same handlers:
- `NLPCore._detect_save_load_intent` — a prefix-stripping intercept (`SAVE_PREFIXES`/
  `LOAD_PREFIXES` in `NLP_Core.py`), checked in `_on_user_input` *before* the examine/take
  intercept and before skill matching (a slot name could otherwise contain a word like "take"
  and misfire the item intercept). Unlike `map_to_item`'s embedding match, a slot name is
  arbitrary player-chosen text with no catalog to match against, so it's extracted by prefix
  stripping instead (`"save as arena-run-1"` → `("save", "arena-run-1")`). A prefix matching
  with nothing following it (ex: bare `"save"`) returns `(None, None)` and falls through to
  normal skill matching rather than saving to a blank name.
- `GUICore`/`TextualCore`'s slot-name field plus Save/Load buttons — `request_save`/
  `request_load` (Tkinter) and `on_button_pressed` (Textual) publish the exact same two events
  with the field's current text. Neither UI does anything save/load-specific beyond that; all
  the actual logic lives in `DMCore`/`LLMCore`'s handlers regardless of which trigger fired.

**What `DMCore.save_game` writes** is a diff from a fresh instantiation, not a raw dump of
`self.entities` (which also holds every static template — see the note above): `scenario_key`,
`player_name`, `round_number`, `scenario_entities`, and per-instance
`{hp, active_conditions, currency, inventory}` — the only fields anything in `DMCore`'s
implementation actually mutates at runtime (`apply_damage`, `apply_condition`/`dismiss_condition`,
`transfer_currency`, `transfer_item`). `equipped` and `band` are never saved because nothing
mutates them post-instancing today.

**What `DMCore.load_game` does:** re-runs `load_rules()` (fresh from every `Rules/Fantasy/*.toml`
file — see why above), then the same `load_scenario_definition`/`load_scenario` path `__init__`
uses, then overlays each saved instance's `hp`/`active_conditions`/`currency`/`inventory` on top
of the freshly-instanced entities. A saved instance name with no match after re-instancing (ex:
the scenario file's entity list changed since the save) is skipped rather than crashing. This
means a resumed save picks up *current* TOML stats, not whatever was true when the save was
made — buff a wolf's HP in TOML and an old save loading that wolf gets the buff for free.

Publishes **`game_loaded`** on success — deliberately not `scenario_loaded`, which `LLMCore`
treats as "narrate a brand-new opening scene." Reusing it would make every resume re-describe
the tavern as if you'd just walked in. Publishes **`game_load_failed`** (`{"slot", "reason"}`)
if the slot doesn't exist, so the player gets feedback rather than the request silently doing
nothing — the same rule `action_not_understood` already follows for unmatched input.

**What `LLMCore.save_game`/`load_game` do:** persist/restore `context_window` plus
`scenario_name`/`scenario_description`/`scenario_characters`, entirely independently of
`DMCore`'s own file. Loading is silent — no LLM call, no new narration queued — so resuming a
session doesn't reprint an opening-scene intro. `LLMCore` also subscribes to
`game_load_failed` (`generate_load_failed_response`) to narrate the failure in-character,
since `DMCore`'s own load attempt already failed before publishing that event — there's
nothing left to restore, just an acknowledgment to give the player.

**Slot names can't escape `Saves/`.** Both cores' `_save_slot_dir` run the player-given slot
name through `os.path.basename` first, so a slot literally named `"../../evil"` resolves to
`Saves/evil`, never outside `Saves/`. The two path helpers are deliberately duplicated (not
shared via an import) rather than coupling the two modules just for a three-line path
computation — same reasoning as the one-file-per-core split above.

## Entity behavior (enemy turns)

Combat used to be entirely one-sided: the player rolled against a target's defense skill,
but nothing ever rolled back. `[[entity.behavior]]` is a new per-entity table (creatures.toml's
`wolf` is the first to have one) that fixes this with the same data-driven pattern as
`[[status]]` rather than any hardcoded "monster AI":

```toml
[[entity.behavior]]
requirements = [
    { field = "hp_per_remain", operator = ">=", value = 0.01 },
]
action = "bite"
```

`DMCore.choose_behavior(entity_name)` and `resolve_behavior_action(entity_name, target_name)`
(both in `DM_Combat.py`) do the actual selection and resolution — see their docstrings for the
declaration-order matching, why a behavior names a specific `action` rather than a bare skill,
and why this reuses `resolve_named_ability`/`select_ability_skill` instead of
`find_attack_ability`. A future multi-behavior entity (ex: flee below some HP threshold,
otherwise attack) is just more list entries evaluated top-down — no new code, only data.

**Wired into `_on_action_detected`'s existing combat branch, not a separate turn loop.**
Right after a round's `round_number` increments, every other living scene entity (not just
`current_target`) gets `resolve_behavior_action(entity_name, opponent)` called on it, where
`opponent` is the player (if the entity is hostile) or `current_target` (if not — see
"Targeting and multi-actor combat rounds" below). Non-`None` results are collected into
`result["turns"]` on the same `round_resolved` payload — deliberately not one publish per
actor, so every existing "one `round_resolved` per player action" assumption (tests, narration
counting) still holds. An entity with no `behavior` data at all (ex: `practice_dummy`, most
ad-hoc test entities) just doesn't act, silently and without error — it's simply absent from
`turns`, which itself is absent entirely (not present-but-empty) when nobody else acted.
`LLMCore._describe_outcome` takes an optional `actor` param (see its docstring in
`LLM_Core.py`) so `generate_round_response` can narrate each `turns` entry without
misattributing it to the player.

## Initiative and turn order

`rules.toml`'s `[[initiative]]` is a list of terms (today, `dodge` and `observation`), the same
list-of-terms shape `[[status]]` already uses so a third contributing skill later is just
another entry, no code change. `DMCore.roll_initiative(entity_name)` (`DM_Combat.py`) pools
every named skill's dice/pips together and rolls the combined pool **once per round** — not the
`dice*3 + pips` static rating `get_opposing_skill`/`select_ability_skill` use to rank a fixed
pair of skills, since initiative is meant to vary round to round. A skill the entity lacks
defaults to the same untrained 1D/0 pips `resolve_action` already defaults to (ex: nothing in
`Rules/Fantasy` currently defines `observation` on a combatant, so every fight rolls it
untrained).

**Initiative only orders presentation, not resolution.** Every actor's action in a round is
still resolved independently against the state at the *start* of the round (see "Targeting and
multi-actor combat rounds" below — a round is a snapshot, not a sequential simulation where an
earlier actor's outcome could change a later actor's roll or target). `_on_action_detected`
rolls `self.roll_initiative(self.player_name)` into `result["initiative"]` and, per non-player
actor, into that actor's own entry in `result["turns"]`, then sorts `turns` by initiative
descending before publishing `round_resolved` — so both the narration order (`turns` is read in
list order by `LLMCore.generate_round_response`) and any future UI reflect who actually acted
first that round, closing the gap the old `# TODO: no initiative/turn order` comment described.
The player's own outcome is still narrated first in the prompt regardless of their rolled
initiative — restructuring that would mean deferring the player's own dice rolls until their
turn came up in the sorted order, which conflicts with the round being a fixed snapshot (see
above) and wasn't part of this change.

## Targeting and multi-actor combat rounds

`self.current_target` is the player's persisted combat target — distinct from
`_get_target_name()`, which stays reserved for non-combat interaction resolution (item
interactions, entity tests) and is untouched by any of this. `_choose_combat_target()` (in
`DM_Core.py`, alongside `_get_target_name()`) picks it — see its docstring for the exact
fallback order. `RulesMixin.load_scenario()` calls it at the end of every load — `__init__`,
`load_game`, and any ad-hoc test scenario reassignment — so `current_target` never drifts out
of sync with `scenario_entities`.

**Ally vs. enemy is derived, not a TOML field.** An entity is an enemy (attacks the player)
if `is_hostile(entity, player_name)`; otherwise, if it has its own `[[entity.behavior]]` data,
it's treated as an ally and attacks `current_target` instead. `creatures.toml`'s `wolf`/`fire
elemental` and `characters.toml`'s `gladstone` used to each carry an `[entity.allies]`/
`[entity.enemies]` table (`name = [...]`/`supertype = [...]`) that no code ever read — removed
as dead data. `characters.toml`'s `thane` (a mercenary fighting at gladstone's side in the
`arena` scenario) is the reference ally: positive `[entity.attitudes] default` disposition
toward everyone (so `is_hostile(thane, gladstone)` is false) plus its own `bite`-shaped
`[[entity.behavior]]`/`[[entity.abilities]]` pair is the entire recipe — no special-casing
anywhere in the resolution code.

**`current_target` only advances once, at the end of the round, if it died** — not interrupted
mid-round by an earlier actor's kill (ex: an ally finishing off the target before the round's
own narration is even built). Everyone who acts that round (player included) resolves against
whichever target was current at the *start* of the round.

**Explicit player-driven targeting** comes from `NLPCore.map_to_target(processed_text)` — see
its docstring in `NLP_Core.py` for the matching mechanism and the multi-instance-disambiguation
limitation. `_on_user_input` runs it *alongside* skill matching (not as a gate the way
item/save-load intent are): a matched skill's `action_detected` payload gets an extra
`"target"` field when `map_to_target` clears `confidence_threshold`, attached to the *same*
event rather than published separately. `_on_action_detected` only honors it if the name is
actually in `scenario_entities`, hostile toward the player, and alive — same "matching is
global, DMCore checks scene-relevance" division of labor `map_to_item` already established.
Naming a confidently-matched but non-hostile entity (ex: "I attack thane") is silently ignored
rather than making an ally the target.

## Status/condition system

Fully generalized, data-driven, in `rules.toml`'s `[[status]]` table (renamed from `[[wound]]`
— it's no longer just the HP-tier wound ladder, though that's still all that's defined today).
Each status has:
- `trigger` — a string naming when to evaluate it (only `"on_damage"` exists as a call site
  right now, wired into `apply_damage`). Adding a new trigger point later needs one new
  `evaluate_statuses(entity, "trigger_name")` call site in Python; no new status needs new code.
- `requirements` — a **list** of `{field, operator, value}` comparisons, ALL of which must
  hold (`COMPARATORS` dict in `DM_Status.py`: `>`, `<`, `>=`, `<=`, `==`, `!=`, `in`, `not_in`).
  `field` is either the derived `"hp_per_remain"` (current HP / max HP) or a direct entity
  attribute (`supertype`, `subtype`, etc.). This replaced an earlier, less general
  `{minimum, maximum}` / `include` / `exclude` shape — fully collapsed into one mechanism now.
- `apply` — `{condition, duration, dismiss}`. `condition` names an entry in `[[condition]]`.
  ("incapacitated"/"mortal" used to also carry a `test = {difficulty, skill, pass, fail}` field
  meant to roll fortitude/willpower to see if the entity falls unconscious or dies, but nothing
  in `DMCore`'s implementation ever read a status's `test` field — removed as dead data. Don't
  confuse this history with the unrelated, *actually wired up* `[entity.test]`, see "Entity
  tests" below, which only ever shared the field name and general shape, not any code path.)

`evaluate_statuses` finds every status matching a trigger whose requirements the entity
currently meets and calls `apply_condition`, which stores it in `entity["active_conditions"]`.

**`active_conditions` is now seeded, not just lazily created.** Each entity template can
declare starting conditions under `[entity.conditions]` (ex: `items.toml`'s `chest` has
`[entity.conditions.locked]`, `{duration, dismiss}` shaped exactly like what `apply_condition`
stores) — previously this table existed on every template (`characters.toml`, `npcs.toml`) but
was empty and unused. `load_scenario()` now copies it into a fresh per-instance
`active_conditions` dict when instancing (`instance["active_conditions"] =
dict(instance.get("conditions", {}))`), so **every instance has `active_conditions` present
from creation** (an empty `{}` if the template declared none) rather than only appearing the
first time `apply_condition` runs.

**`dismiss_condition(entity_name, condition_name)`** (see its docstring in `DM_Status.py`) is
the general-purpose removal primitive this enabled. First real use: a locked chest's `"locked"`
condition is dismissed via `apply_test_outcome` on a successful `[entity.test]` check (see
"Entity tests" below).

**Known gap (`# TODO` in `DM_Status.py`'s `evaluate_statuses`):** this only added a *manual*
removal primitive — nothing automatically re-evaluates and dismisses a status-driven condition
when its `requirements` stop holding. An entity taking sustained damage still accumulates every
wound tier it passed through (`wounded` + `incapacitated` both present) rather than replacing
the previous one; healing wouldn't clear `wounded` either. `dismiss_condition` would be the
right tool to build that with, it's just not wired to anything HP-related yet.

**Deferred (`# TODO` in `rules.toml` above the `[[condition]]` table):** the `enhance` ability
(a buff technique) was removed from `characters.toml` — "that's a later thing." We'd discussed
making its condition's `skills`/`modifier` variable per-use (like `damage_value` already is)
rather than fixed in `[[condition]]`, but didn't build it. Pick that up when `enhance` comes
back.

## Entity tests

A `[entity.test]` block is a general-purpose skill check *against an entity itself* — first
built for `items.toml`'s `chest`, but nothing here is lock-specific:
```toml
[entity.test]
difficulty = 12
skill = [ "finesse" ]
requires_condition = "locked"
blocks_if_condition = "jammed"
[entity.test.pass]
dismiss_condition = "locked"
[entity.test.fail]
condition = "jammed"
duration = "permanent"
dismiss = ""
```
In `_on_action_detected`: if the target has a `test` and **`is_test_available(target, test,
skill_name)`** says yes, it's resolved as a **flat difficulty check** (`resolve_action(player,
skill, test["difficulty"])`), matching D6/WEG's convention of static DCs for passive obstacles —
*not* run through `resolve_opposed_action`, since the target isn't rolling its own defense the
way a creature does. `is_test_available` checks three things: `skill_name` is in `test["skill"]`;
if `test["requires_condition"]` is set, that condition must currently be active (ex: `"locked"`
— once dismissed, the test stops being reachable at all); if `test["blocks_if_condition"]` is
set, that condition must *not* be active (ex: `"jammed"` — once applied by a failed attempt, no
further pick attempts can ever succeed via this test again). **Without this gating, an
already-opened chest would silently re-run the test (and re-loot, harmless only by accident
since there'd be nothing left) on every repeat attempt, and a `"jammed"` condition applied on
failure would have zero actual effect** — it was purely narrative flavor until this was added.

Whichever of `test["pass"]`/`test["fail"]` matches the outcome is handed to
**`apply_test_outcome(entity_name, outcome)`**, which dispatches purely on which keys are
present (no "action" enum): `dismiss_condition` removes that condition, `condition` applies a
new one (the exact `{condition, duration, dismiss}` shape `[[status]]`'s own `apply` block
already uses). A truthy `loot` key would hand everything on the target to
`self.player_name` via `loot_entity` in one shot (see "Inventory/currency transfer" below) —
**this primitive still exists and works, but the chest no longer uses it** (see "Examining and
taking items" below for why: opening a container must not force its contents into the player's
inventory before they can look at what's there). It remains available for a future entity where
instant auto-loot-on-success is actually the right call (ex: a mundane coin purse nobody would
plausibly want to inspect first) — that's a per-entity TOML choice, not a hardcoded policy.
`apply_test_outcome` returns `loot_entity`'s summary (or `None`) so a caller can attach it to
`result["loot"]` for narration if it does use `loot`.

**A skill *not* in `test["skill"]` isn't blocked — it just isn't a test at all,** and falls
through to the normal `resolve_opposed_action` path below it. This is what makes "try the chest,
get denied, then pick the lock" work with *zero* lock-specific code in that branch: `strength`
(forcing it) isn't in the chest's `test.skill` (only `["finesse"]`), so a strength attempt
resolves via the **ordinary opposed-skill mechanism** — `strength`'s `opposes` list includes
`"fortitude"`, and the chest already has `[entity.skills] fortitude = {dice=5, pips=0}` (originally
there for the item-breaking mechanic), so it's automatically used as the difficulty. No
`is_locked`-style special case was needed for that path at all; only the lock*pick* attempt
(`finesse`, matching `test.skill`) goes through `entity.test`.

**Objects are never treated as hostile**, regardless of attitude data: `is_hostile` short-circuits
to `False` whenever `entity["supertype"] == "object"`, *before* it would otherwise default a
no-attitude-data entity to hostile/combat-ready (the same default that makes `wolf` count as
hostile). Without this, a locked chest — which naturally has no `[entity.attitudes]` block —
would get batched into `round_resolved` combat narration instead of narrating immediately like
any other non-combat interaction.

**Gold/currency:** reuse the existing `currency` field (already on `gladstone`/`innkeeper`,
a plain integer) rather than inventing a new mechanism — `items.toml`'s `chest` has
`currency = 20`. Repeating an item name N times in `inventory` (ex: `inventory = ["gold",
"gold", ...]`) was the wrong shape for a *quantity* of a fungible thing; `currency` already
existed for exactly this.

## Item-targeted skill checks

`[entity.test]` (above) already let a skill check target the *scene* entity itself (ex: the
chest's own lock), reached via `self.current_target`. It could **not** reach something one
level *deeper* — an item sitting inside a container, or already carried in the player's own
inventory — because nothing indexed items as possible targets at all, and `current_target` only
ever names a top-level `scenario_entities` member. `items.toml`'s `cursed dagger` is the first
entity to need this: its own `[entity.test]` (`difficulty = 8`, `skill = ["arcane"]`,
`blocks_if_condition = "identified"`, `[entity.test.pass] reveal = true`) is what lets an arcane
check — not a passive `"examine"` — actually confirm it's cursed. The same mechanism is meant
for a future "search for traps" check on some other entity; nothing about it is dagger-specific.

`NLPCore.map_to_target` matches this into the same index as combat targets — see its docstring
in `NLP_Core.py` for how the two kinds of match stay undifferentiated at match time, and why
that's fine (`DMCore` is what decides whether a returned name is a combat redirect or a
reachable, testable item — the two are mutually exclusive outcomes for the same match, checked
in that order below).

**`DMCore._resolve_item_test_target`/`_resolve_item_test`** (see their docstrings in
`DM_Core.py`) do the reachability check and flat-difficulty resolution, tried *before*
combat-target redirection in `_on_action_detected` (inspecting an item is never an attack, so
it must never fall into `round_resolved`/damage/`current_target` machinery). Anything that
doesn't resolve (wrong skill, still locked away, not present anywhere reachable) falls through
to whatever the input would otherwise have resolved as — ex: `"blades"` against a hostile
target still routes to ordinary combat, unaffected. A successful resolution publishes straight
to `action_resolved`, never batching it into a round.

**`apply_test_outcome` gained a fourth dispatch key: a truthy `"reveal"`** applies the new,
permanent `"identified"` condition (`is_identified(entity_name)` mirrors `is_locked`/
`is_closed` exactly) — deliberately *not* a list of what to reveal. The **content** revealed
is read back off the entity's own data (its `"tags"` field) by whoever narrates the check,
once `is_identified` is true — single source of truth, no duplicate list to keep in sync with
the entity's real tags. `_resolve_item_test` attaches `result["revealed"] = <tags list>` only
when the check both succeeded and left the entity identified; `LLM_Core._describe_outcome`
renders that as `" The check reveals: cursed."` in the narration prompt — grounded in a roll
that actually happened, never handed to the LLM speculatively. `_on_item_interaction_detected`'s
`"examine"` branch was extended the same way: a plain look **never** includes `"revealed"`
unless `is_identified` is already true, so the curse only ever surfaces in narration *after* a
real arcane check earned it, not on a first glance.

**`blocks_if_condition = "identified"`** is what stops a repeat check from being pointless (or
from re-triggering `apply_test_outcome` a second time) — the exact same pattern the chest's own
`"jammed"` already used to permanently block a repeat lockpick. Once identified, the same skill
name against the same item just falls through to whatever an ordinary action would resolve to
instead (ex: `"arcane"` against `current_target` falls through to casting `fireball` at the
chest if that also happens to be gladstone's spell-casting skill — unrelated pre-existing
behavior, not a bug in this feature).

**Known gap (`# TODO` in `NLP_Core.py`'s `map_to_action`):** the obvious phrasing "I identify
the dagger" does **not** reach the arcane check at all. `map_to_target` correctly resolves
`"cursed dagger"` (0.72), but `map_to_action` resolves the skill to `"polearms"` (0.51) instead
of `"arcane"` — "identify" is a literal `appraise` keyword (skills.toml), yet the
whole-sentence embedding still favored an unrelated skill over either `appraise` or `arcane`.
Since `"polearms"` isn't in the dagger's `test.skill`, `_resolve_item_test_target` correctly
declines it (not a bug in this feature's own logic) and the turn falls through to a harmless
no-op action against `current_target` (the chest) — `is_identified` stays `False`. **The live
LLM response papers over this silently**: with no roll and no real result to narrate from, it
will invent a confident-sounding identification instead, contradicting the dagger's own
established description — the underlying mechanism *would* work if the skill had matched.
Same root cause as the `_match_by_keyword` gotcha above (a keyword-driven skill dominating an
unrelated whole-sentence embedding match) — not yet addressed for this specific phrasing.

## Inventory/currency transfer

Three general-purpose methods on `DMCore`, none lock/chest-specific:
- **`transfer_currency(from_name, to_name, amount=None)`** — moves currency between two
  entities' `currency` fields; `amount=None` moves all of it. Clamps to what's actually
  available (`min(amount, available)`) and no-ops (returns `0`) for a missing entity, rather
  than going negative or raising.
- **`transfer_item(from_name, to_name, item_name)`** — moves *one* matching entry out of
  `from_name`'s `inventory` list into `to_name`'s. Duplicates in `inventory` represent quantity
  (ex: three `"health potion"` entries), so this only ever removes one per call — looping is
  the caller's job. Returns `False` (no-op) if the item isn't present or either entity is missing.
- **`loot_entity(from_name, to_name)`** — sweeps *everything*: all currency plus every
  inventory item, one `transfer_item` call per entry. Returns `{currency, items}` describing
  what actually moved.

`_on_action_detected` still attaches a `loot_entity` result as `result["loot"]` when
`apply_test_outcome`'s `loot` key fires, and `LLM_Core._describe_outcome` turns that into
`" The player gains: 20 currency."` in the prompt — without this, the LLM has nothing real to
narrate a chest's contents from and will invent plausible-sounding treasure instead. This
machinery is what "Examining and taking items" (below) now calls directly instead, per-item,
rather than all-at-once via `loot`.

## Examining and taking items

**Opening a container must never force its contents into the player's inventory.** The chest's
`[entity.test.pass]` used to set `loot = true`, auto-transferring everything the instant the
lock was picked — this was reverted because a player has no chance to *examine* something (ex:
a cursed weapon) before deciding whether to take it if it's already silently in their pack.
Unlocking a container now only ever dismisses `"locked"`; the contents stay put until the player
deliberately opens it and then examines or takes something.

This is a distinct interaction path from the whole skill/dice system, because looking at,
taking, giving, trading, opening, or closing something already accessible doesn't warrant a
roll. Six intents share one pipeline:
- **`NLPCore._detect_item_intent(processed_text)`** checks for one of six verbs, *before* skill
  matching runs: `"examine"` (`examine`/`inspect`/`look at`/`check out`), `"take"` (`take `/
  `grab `/`pick up`/`loot `), `"give"` (`give `/`hand over`/`offer `), `"trade"` (`trade `/
  `buy `/`purchase `), `"open"` (`open the `/`open it`), `"close"` (`close the `/`close it`/
  `shut the `/`shut it`). **Phrases, not bare words, on purpose** — a bare `"pick"` would
  misfire on `"I pick the lock"` (the existing `finesse` lockpicking flow), so `"pick up"` (two
  words) is required instead; verified this doesn't regress lockpicking. Same reasoning forced
  `open`/`close` into `"...the "`/`"...it"` phrases rather than bare `"open "`/`"close "` —
  `blades`'s own description is `"Using swords and knives in close combat."`, and a bare
  `"close "` would have swallowed that input before skill matching ever ran. **`TRADE_KEYWORDS`
  deliberately avoids every word in `appraise`'s own keyword list** (`evaluation`, `commerce`,
  `investigation`, `value`, `price`, `worth`, `cost`, `identify`, `examine`), so a phrase like
  "what's this worth" still reaches `appraise` instead of being swallowed here.
- **`open`/`close` skip item-name matching entirely.** They act on the current scene target
  directly (`"open the chest"` opens *the* chest, not some named item inside it), so `NLPCore`
  publishes `item_interaction_detected` with `item_name: None` for them without ever calling
  `map_to_item`. The other four intents still go through **`NLPCore.map_to_item(processed_text)`**
  — the same embedding-match pattern as `map_to_action`, but against every `supertype == "object"`
  entity's name/description (built in `_on_rules_loaded` alongside the skill embeddings).
  **Currency is checked first as a fixed synonym list** (`gold`/`coin`/`currency`/`money`),
  returning the sentinel item name `"currency"` — it's a plain integer field on entities, not an
  object-supertype entity with a name to embed, so there's nothing to match it against
  semantically. If a verb is recognized but no item matches, the input falls through to normal
  skill matching (ex: in case the phrase legitimately meant something else).
- **`DMCore._on_item_interaction_detected`** resolves it with zero dice rolls. Shared gates
  first: a locked container denies everything (`reason: "locked"`); `open`/`close` are handled
  by `_resolve_open_close_intent` and return immediately (see below); `item_name` equal to the
  current target's own name means the player is addressing the target itself, not something
  inside it (`describe_character` for `"examine"`, `reason: "not_takeable"` for anything else —
  you can't pocket, give away, or sell the whole chest); a **closed** (but unlocked) container
  denies reaching its *contents* (`reason: "closed"`) while still allowing it to be examined or
  opened. Past those gates, **`"take"`/`"trade"` move an item from the target to the player;
  `"give"` moves one from the player to the target** — same `transfer_item`/`transfer_currency`
  primitives, just with source/destination swapped depending on intent. `"trade"` additionally
  charges the item's TOML `value` field as a price, charged via `transfer_currency` *before* the
  item moves — if the player can't afford it, the trade is denied outright (`reason:
  "cant_afford"`) rather than partially resolving. Publishes `item_interaction_resolved` either
  way (`found` plus `description`/`container`/`amount`/`price`/`reason` as applicable) so
  narration always has something to say, never silence.
- **`DMCore._resolve_open_close_intent`** is gated to `subtype == "container"` (ex: `chest`), so
  aiming `"open"`/`"close"` at a creature or a plain object with no openable nature fails safely
  (`reason: "not_openable"`) instead of silently applying a nonsensical condition to it. Toggles
  the same `"closed"` condition `is_closed` reads, seeded on `chest` via
  `[entity.conditions.closed]` — **independent of `"locked"`**: unlocking (picking the lock)
  only ever dismisses `"locked"`, so a freshly-picked chest is unlocked but still closed, and
  needs its own `"open"` action before its contents are reachable. Re-closing (`"close"`)
  re-applies the condition and re-blocks contents access; opening/closing an already-open/closed
  container fails safely (`reason: "already_open"`/`"already_closed"`) rather than re-toggling.
  **A successful `"open"` also attaches `contents`** — one `describe_character(item_name)`
  string per item in the container's `inventory` (its flavor description only, the same
  purely-descriptive field selection `describe_character` already uses for entities — never
  `tags`/`damage_value`/etc., so a cursed item's actual curse tag is never handed over here).
  Without this, `LLMCore` has nothing to narrate the opening from and will invent
  plausible-sounding treasure instead of what's actually inside. `"close"` never attaches
  `contents` — there's nothing new to reveal by shutting something.
- **`LLMCore.generate_item_interaction_response`** narrates it — explicitly telling the LLM
  `"nothing is taken, moved, or changed"` for a found `"examine"`, so it doesn't imply a transfer
  that didn't happen. Since `item_name` is `None` for `"open"`/`"close"`, the prompt-building
  falls back to `container` (the target's own name) wherever it would otherwise quote the item.

**Known gap, deliberately out of scope for now:** movement/positioning on a battle grid is
anticipated as the next interaction verb once band/range targeting exists (see "Open threads"),
but nothing here — not even a `move`/`go` keyword — attempts it yet.

## Tags vs. conditions

Two distinct mechanisms, easy to conflate since both gate on "does this entity have X":
- **Tags** are static, inherent classification data used purely for *matching* — they never
  change over an entity's lifetime unless its template does. `damage_tags`/`armor_tags` were
  the original example (an attack's damage type vs. worn armor's resistances); this was
  generalized so an entity can carry its own innate `resistance_value`/`resistance_tags`
  (rolled, partial reduction — same shape and same `get_damage_reduction` call as armor, just
  checked against the entity itself rather than its equipped items), `immunity_tags` (an
  absolute, unrolled block — `is_immune_to` short-circuits `calculate_damage` to zero net
  damage regardless of the roll, matching notes.txt's "poison damage tagged so undead are
  immune" example), and now `vulnerability_value`/`vulnerability_tags` — the mirror image of
  resistance: `get_vulnerability_bonus` rolls *extra* damage on a matching tag instead of
  reducing it, added to `raw_damage` before reduction in `calculate_damage`. Entity-innate
  only, same as resistance (no equipped-item vulnerability counterpart exists, unlike armor's
  `armor_value`/`armor_tags` reduction side). `creatures.toml`'s `fire elemental` now exercises
  all three at once: `immunity_tags = ["fire"]` (fire never gets through),
  `resistance_value`/`resistance_tags` covering physical damage types (reduced, not blocked),
  and `vulnerability_value`/`vulnerability_tags = ["water"]` (water hits harder). **Immunity
  wins outright over vulnerability if a single attack's `damage_tags` somehow matched both** —
  `calculate_damage` checks `is_immune_to` first and, if true, forces `vulnerability_bonus` to
  0 rather than letting the two fight it out; immunity is an absolute block, not just a bigger
  number in the same tug-of-war as resistance/vulnerability. `spells.toml`'s `splash flow`
  (single-target, `damage_tags = ["water"]`, on `gladstone`'s own ability list alongside
  `fireball`) is what actually exploits this — same "arcane" skill as fireball, so it's only
  reachable by name via `resolve_named_ability` (see "cleave" below), never via
  `find_attack_ability`'s skill-first lookup, which always resolves to whichever of the two is
  listed first (`fireball`).
- **Conditions** (`active_conditions`, `apply_condition`/`dismiss_condition`) are dynamic —
  gained and lost during play via triggers/tests (wound tiers, a chest's `"locked"`/`"jammed"`).
  Use a condition only for something that can plausibly change mid-scene (a spell granting
  temporary fire resistance); use a tag for something permanent to what the entity *is*. Don't
  seed a creature with an always-on, never-removed condition just to express innate immunity —
  that's what `immunity_tags`/`resistance_tags` are for.

**`abilities` is a flat list now, the same shape as `inventory`**, not a dict keyed by
ability name (that key was never actually read by any code — `find_attack_ability` only ever
iterated `.values()` — so it was pure decoration removed for simplicity). Each entry is
either a plain string, naming a shared catalog entity (`spells.toml`/`techniques.toml`)
resolved by the new `resolve_ability(ability)` via `self.entities[name]` (the same pattern
equipped items already used), or an inline table for a one-off innate ability with no shared
entity to point at (`wolf`'s `bite`, `fire elemental`'s `flame_touch`; both now declared as
plain `[[entity.abilities]]` array-of-tables entries rather than nested under a named key).
`characters.toml`'s `gladstone` mixes both in one array — `abilities = [ "fireball",
"cleave", { name = "punch", ... } ]` — since TOML 1.0 arrays can hold mixed element types.
**Gotcha:** a bare `key = [...]` array assignment only attaches to whichever table header is
currently open; `abilities` had to be placed *before* `[entity.equipped]` opens (among
gladstone's other flat keys like `inventory`), not after — placing it after silently nested
it inside `entity.equipped` instead of `entity` itself, with no parse error to catch it.
Referencing `"cleave"` this way is what makes `techniques.toml`'s `cleave` entity reachable via
`resolve_ability` at all.

**`techniques.toml`'s `cleave` exercises two things nothing else does:** a multi-skill
`skill = [ "blades", "axes" ]` list (either triggers it) and weapon-scaled damage
(`"user.weapon.dice"`/`"user.weapon.pips"` instead of a fixed amount). The mechanism is fully
documented at its own definitions, not restated here — see the docstrings of
`ability_matches_skill`, `resolve_weapon_reference`, and `resolve_damage_value` in
`DM_Combat.py`.

**Choosing a technique over a basic attack on the same skill is solved above `NLPCore`'s
matching, not inside `find_attack_ability`.** `NLPCore` embeds every
`supertype in ("technique", "spell")` entity into the same phrase space as skills, so naming
one directly (ex: "I cleave through them") can come back from `map_to_action` as `"cleave"`
instead of `"blades"` in the first place; `DMCore._on_action_detected` then routes a matched
ability name through `resolve_named_ability`/`select_ability_skill` instead of
`find_attack_ability` — see those two docstrings in `DM_Combat.py` for the ownership-gating and
skill-selection details. A bare skill match (ex: "blades" itself) is unaffected, since
`resolve_named_ability` only ever matches an actual ability name, never a skill name.

## Data/TOML conventions worth knowing

- `DM_Core.load_rules` only special-cases `skill` and `entity` top-level keys; everything else
  in any flat `Rules/Fantasy/*.toml` file lands generically in `self.rules[key]`. This is why
  adding `[[status]]`, `[strength_damage]`, etc. never required touching the loader. `scenario`
  used to be a third special case here, but scenarios moved to their own loader/subdirectory
  (see "Scenario instancing") so multiple named scenarios can coexist.
- `[entity.attitudes]` is `{default, name, supertype}`, where `name`/`supertype` are TOML
  arrays-of-one-key-tables (`[[entity.attitudes.name]]` then `anne = [...]` inside it), not
  plain tables — `tomllib` parses each into a *list* of single-key dicts (ex:
  `[{"anne": [100, 100, 100, 100, 100, 100]}]`), which is why `get_attitude` loops over the
  list checking `if toward_name in override` rather than doing a plain dict lookup.
  `characters.toml`'s `gladstone` and `npcs.toml`'s `innkeeper` are the two entities with real
  attitude data today; `creatures.toml`'s `wolf` deliberately has none (see the hostility note
  under "Combat/action loop" for why that's still fine).
- `damage_value = {dice, pips, bonus}` — `bonus` can be a flat number or `"user.<rule_name>"`,
  resolved via `resolve_bonus` against a same-named table in `self.rules` (e.g.
  `[strength_damage]` → `skill`, `divisor`). String `dice`/`pips` (like `techniques.toml`'s
  `"user.weapon.dice"`, meant to reference the attacker's equipped weapon) are **not**
  resolved — they degrade to 0 rather than crash. Nothing currently exercises that path.
- **`load_rules`'s blanket per-file exception handling means a malformed TOML file fails
  *quietly*** — a parse error (ex: a duplicate key in one inline table) doesn't crash, it just
  silently loads that file with less data than expected. If rule data seems to be missing,
  check for a parse error before assuming the logic is wrong.

## LLM integration

Endpoint is LM Studio's OpenAI-compatible API. Two gotchas specific to LM Studio:
- `/v1/models` lists models in its *catalog*, not what's currently loaded into memory — a
  chat completion can still 400 with `"No models loaded"` even though `/v1/models` shows one.
  If narration suddenly stops working, check whether the model actually needs reloading
  (`lms load <model>` or the UI) before assuming the code broke.
- The request payload has no explicit `"model"` field. That's fine as long as exactly one
  chat model is loaded (LM Studio infers it), but specifying it explicitly would let LM
  Studio's just-in-time loading auto-load the model on demand — discussed as a robustness
  improvement, not yet made.

## Textual mirror (headless testing)

`Textual_Core.py` subscribes to the same events `GUI_Core` displays and adds its own `Input`
widget that publishes `user_input_submitted` the same way `GUICore.submit_input` does. Built
specifically so the whole app can be driven and asserted on headlessly (`app.run_test()` /
`Pilot`) without Tkinter, a display, or a browser. Two non-obvious things if you touch this file:

1. **Don't name an attribute `self._ready`** — Textual's `App` base class has an internal
   `_ready()` coroutine used during shutdown; shadowing it with a bool crashes every run on
   exit with `TypeError: 'bool' object is not callable`. Use a different name
   (`_mirror_ready` here).
2. **Events can arrive before the app has mounted.** `DMCore` publishes `rules_loaded`
   synchronously during `__init__`, which can happen before Textual's `compose()` has even
   run (unlike Tkinter, where widgets exist synchronously at construction). `Textual_Core`
   buffers pre-mount writes and flushes them in `on_mount`.
3. **`RichLog.lines` only reflects width-wrapped content once its tab is active.** A widget
   inside a non-active `TabPane` has zero render width, so `.lines` reads empty even though
   `.write()` succeeded and didn't raise. Tests reading a background tab must activate it
   first (`tabbed_content.active = "tab_id"`, then `await pilot.pause()`).
4. Writes can come from a different thread (`LLMCore`'s background fetch calls
   `event_bus.publish` from inside a `threading.Thread`). `call_safely` wraps everything
   through `self.call_from_thread`, falling back to a direct call if that raises (covers both
   "called from a foreign thread" and "called before the app is running" cases with one path).
5. Pilot has no `.type()` in the installed Textual version (8.2.8) — build a key list instead
   (`["space" if c == " " else c for c in text]`) and pass it to `pilot.press(*keys)`.
6. If you ever write a diagnostic script that spawns a background thread and does a blocking
   `thread.join()` inside an `async def`, use `await asyncio.to_thread(thread.join)` — a bare
   `t.join()` freezes the whole event loop and deadlocks against `call_from_thread`, which is
   waiting for that same loop to run its scheduled callback. Cost about 10 minutes to
   diagnose; don't repeat it.

## Testing

Two files, split so `test_unit.py` never has a network dependency, not even a skippable one:
- **`test_unit.py`** — fast, offline `unittest.TestCase` classes covering dice/damage
  resolution, statuses, scenario loading, the locked chest and its `[entity.test]`/item-test
  machinery, examine/take/give/trade/open/close, inventory transfer, NPC dialogue, attitude
  phrases, save/load, and the Textual mirror's own `pytest.mark.asyncio` functions.
  `TestGameBoot` and `TestNlpConfidenceThreshold` load the real `sentence-transformers` model
  (~15-20s); the latter uses `setUpClass` so its many methods share one load. **Gotcha:**
  sharing `cls.dm_core` across every method in a `setUpClass`-based class also shares its
  *mutable* state — several methods trigger real, unseeded combat against the same
  `wolf`/`wolf_2`, so HP damage silently accumulated across nominally-independent tests in
  alphabetical order and occasionally left `wolf` dead by the time a later test assumed it was
  alive. Fixed with a `setUp` (not `setUpClass`) that re-runs `load_rules`/
  `load_scenario_definition`/`load_scenario` before each test method to reset every mutable
  field, without re-paying for a new model load. If you ever add `setUpClass`-shared state to
  a test class again, check whether any test method mutates it.
- **`test_integration.py`** — every test that needs a real, running LM Studio
  (`TestInnkeeperConversation` and its Textual counterpart, `TestArenaCombatConversation`,
  `TestChestSagaConversation`, `TestChestTradeConversation`, `TestSaveAndResumeConversation`).
  Each is gated on a private `_lm_studio_reachable()` check so they all skip together, not
  fail, when nothing's listening on `127.0.0.1:1234`. `python -m pytest -q` picks up both
  files; `python -m pytest -q test_unit.py` alone runs the fast, offline subset.

Lessons worth keeping in mind when touching either file:
- **Don't assert on exact live-LLM narrative content.** `TestInnkeeperConversation` used to
  assert the husband-loss question's response contained specific words and failed on a real
  run where the LLM conveyed her grief without ever using them. Assert structure (which events
  fired, non-empty/non-error) and read the printed transcript by hand when the test runs — a
  green checkmark alone isn't a reliable content signal.
- **Seeded randomness in one test can leak into another via the process-wide `random`
  module.** `TestChestSagaConversation` seeds `random` so its two rolls (lockpick, arcane
  check) land as passes instead of failing most of the time; its `tearDown` reseeds from OS
  entropy afterward specifically because an earlier version leaked the deterministic seed into
  an unrelated `test_unit.py` combat roll.
- **A Textual `pilot.click()` on a button near the test terminal's edge can raise
  `OutOfBounds`.** The Save/Load button tests focus the button and press Enter instead of
  clicking by CSS selector.
- **A comprehensive-looking suite is only useful if it stays committed** — several test files
  were found deleted from disk mid-session in an earlier version of this repo (no clear cause)
  and had to be restored from git history. Worth a `git status` gut-check if results seem to
  regress unexpectedly.
- **One live narration-grounding gap found via `TestChestTradeConversation` (`# TODO` in
  `LLM_Core.py`'s trade prompt):** the successful-purchase prompt reads oddly when the seller
  is an inanimate chest — a live run narrated the purchase as refused even though the transfer
  mechanically succeeded.

## Open threads / natural next steps

Tracked as `# TODO:` comments at their actual call sites, not as a list here — a prose list
like this drifts out of sync with the code the moment either one changes underneath it. Grep
the codebase for `TODO` to find every open item (auto status dismissal in `DM_Status.py`,
`enhance`'s deferred condition design in `rules.toml`, the unpublished GUI Party/Notes/Map tabs
in `GUI_Core.py`, movement/positioning in `NLP_Core.py`,
multi-instance target disambiguation and the item-identify keyword collision both in
`NLP_Core.py`, and the chest-as-shop trade narration gap in `LLM_Core.py`).
