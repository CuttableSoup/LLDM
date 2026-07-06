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

- **`DM_Core.py`** — the rules engine. Loads all TOML in `Rules/Fantasy/`, does dice rolling,
  opposed checks, damage, and the status/condition system. This is where almost all the real
  logic lives now; see "Combat loop" below.
- **`NLP_Core.py`** — `sentence-transformers` (`all-MiniLM-L6-v2`) embeds each skill's
  name/description/keywords as separate phrases (not just one embedding per skill, to avoid
  dilution), then cosine-matches player input against all phrases. On `user_input_submitted`,
  publishes `action_detected` with the best-matching skill + score — but only if that score
  clears `self.confidence_threshold` (`0.5`); below it, `map_to_action` returns `(None, score)`,
  and `_on_user_input` publishes `action_not_understood` instead (ex: "Hey there innkeeper"
  used to map to "artistry" at a 0.32 score and roll a real skill check against a greeting).
  It also separately matches free text against item names (`map_to_item`) for "examine"/"take"
  — see "Examining and taking items" below; that path runs *before* skill matching and, if it
  fires, skips it entirely for that input.
  **Don't let a low-confidence input fall through to complete silence** — with nothing
  subscribed to `action_not_understood`, the player types something, nothing ever happens, and
  it looks exactly like the app hung. `LLMCore` subscribing to it (below) is what closes that
  loop; if you ever add a *new* no-match branch here, wire its feedback the same way.
  **Gotcha found while building `TestInnkeeperConversation`:** the 0.5 threshold rejects
  almost any *naturally-phrased* social action, not just true non-actions — adding any topic
  ("...about the road", "...her husband") dilutes the sentence embedding enough to drop it
  below 0.5, even for genuinely-social phrasing. Only near-bare keyword phrasing (ex: "I try
  to charm her" → processed to "charm her" → ~0.60 on `charisma`) reliably clears it; "I
  persuade her to talk about her husband" scores ~0.36. In practice this means most
  conversational turns with an NPC currently go through the `action_not_understood`
  clarification path rather than genuine `charisma` action resolution — which still reads
  fine to the player (the persistent system-message character roster grounds either path
  equally), but it's worth knowing this is happening if `defender_details`/real skill checks
  ever seem to be "missing" during dialogue. Not fixed — no threshold/keyword-set tuning has
  been done here yet.
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

Inside `DMCore._on_action_detected`:
1. Picks a target via `_get_target_name()` — first non-player entry in `self.scenario_entities`.
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
   positive default. There's still no removal of dead entities from `scenario_entities`
   (a known gap noted below), so a hostile target already at 0 HP still counts as "in combat."
   If in combat, `self.round_number` increments and the result publishes as `round_resolved`
   (narrated once, as a round summary). Otherwise (no target, or a non-hostile target like a
   tavern NPC) it publishes immediately as `action_resolved` (narrated per skill use) — this
   is also the path a *dialogue* skill check (ex: `charisma`) takes against a friendly NPC.
   Only one player action is resolved per call today (no enemy turn loop exists), so a
   "round" is currently just one player action while a hostile target is present — the
   `round_resolved` payload carries a single result, not a list, ready to extend if/when
   multi-actor rounds are added.

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

**Where the character roster comes from:** `DMCore.describe_character(entity_name)` (in
`DM_Core.py`) builds a flavor-text line per entity out of purely descriptive TOML fields —
`description`, `qualities`, `memories`, `quotes` — deliberately excluding mechanical data
(skills/dice), since this is meant to tell the LLM *who* someone is, not how they roll.
Entities with none of those fields (ex: `wolf`, which is pure mechanics) return `""` and are
skipped. `DMCore.__init__` builds the `characters` list for every entity in
`self.scenario_entities` and includes it in the `scenario_loaded` payload; `_on_action_detected`
separately attaches `result["defender_details"] = describe_character(target_name)` per action,
which `_describe_outcome` folds into the per-turn outcome text — belt-and-suspenders against
the persistent roster ever being stale (ex: an NPC added to the scene after the intro fired,
though nothing does that yet). Verified live: asking the tavern's `innkeeper` about her husband
(her `memories` includes "Lost her husband to a bandit raid") produced narration referencing
that loss unprompted — the data actually reaches and shapes generation, not just cosmetic
plumbing.

**Player is hardcoded** as `self.player_name = "gladstone"` — there's no party/character
selection system yet.

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
`test_all.py`) still just assign a plain dict rather than adding a new file under `scenarios/`.

**Known gap:** `_get_target_name()` always returns the *first* non-player instance. No
targeting logic, no moving on when something dies, no band/range awareness yet.

## Status/condition system

Fully generalized, data-driven, in `rules.toml`'s `[[status]]` table (renamed from `[[wound]]`
— it's no longer just the HP-tier wound ladder, though that's still all that's defined today).
Each status has:
- `trigger` — a string naming when to evaluate it (only `"on_damage"` exists as a call site
  right now, wired into `apply_damage`). Adding a new trigger point later needs one new
  `evaluate_statuses(entity, "trigger_name")` call site in Python; no new status needs new code.
- `requirements` — a **list** of `{field, operator, value}` comparisons, ALL of which must
  hold (`COMPARATORS` dict in `DM_Core.py`: `>`, `<`, `>=`, `<=`, `==`, `!=`, `in`, `not_in`).
  `field` is either the derived `"hp_per_remain"` (current HP / max HP) or a direct entity
  attribute (`supertype`, `subtype`, etc.). This replaced an earlier, less general
  `{minimum, maximum}` / `include` / `exclude` shape — fully collapsed into one mechanism now.
- `apply` — `{condition, duration, dismiss}`. `condition` names an entry in `[[condition]]`.
- `test` — `{difficulty, skill, pass, fail}` (ex: "incapacitated"/"mortal" test
  fortitude/willpower to see if the entity falls unconscious or dies). **Still unused/dead
  data** — nothing in `DM_Core.py` reads a status's `test` field. Don't confuse this with the
  unrelated, *actually wired up* `[entity.test]` (see "Entity tests" below), which only shares
  the field name and general "difficulty/skill/pass/fail" shape by design, not any code path.

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

**`dismiss_condition(entity_name, condition_name)`** is the general-purpose removal primitive
this enabled — `del`s the entry from `active_conditions` if present, log-and-no-ops otherwise.
First real use: a locked chest's `"locked"` condition is dismissed via `apply_test_outcome` on
a successful `[entity.test]` check (see "Entity tests" below).

**Known gap still open (by design, not yet asked for):** this only added a *manual* removal
primitive — nothing automatically re-evaluates and dismisses a status-driven condition when its
`requirements` stop holding. An entity taking sustained damage still accumulates every wound
tier it passed through (`wounded` + `incapacitated` both present) rather than replacing the
previous one; healing wouldn't clear `wounded` either. `dismiss_condition` would be the right
tool to build that with, it's just not wired to anything HP-related yet.

**Deferred:** the `enhance` ability (a buff technique) was removed from `characters.toml` —
"that's a later thing." We'd discussed making its condition's `skills`/`modifier` variable
per-use (like `damage_value` already is) rather than fixed in `[[condition]]`, but didn't
build it. Pick that up when `enhance` comes back.

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
failure would have zero actual effect** — it was purely narrative flavor until this was added
(verified live: re-picking an unlocked/jammed chest now resolves at difficulty `0` via the
ordinary opposed path instead of re-running the `12`-difficulty test).

Whichever of `test["pass"]`/`test["fail"]` matches the outcome is handed to
**`apply_test_outcome(entity_name, outcome)`**, which dispatches purely on which keys are
present (no "action" enum): `dismiss_condition` removes that condition, `condition` applies a
new one (the exact `{condition, duration, dismiss}` shape `[[status]]`'s own `apply`/`test.fail`
blocks already use). A truthy `loot` key would hand everything on the target to
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
`" The player gains: 20 currency."` in the prompt (verified live: an earlier version of this
that omitted this had the LLM *inventing* contents — "a silver key and leather-bound journal" —
for a chest that only ever held currency). This machinery is what "Examining and taking items"
(below) now calls directly instead, per-item, rather than all-at-once via `loot`.

## Examining and taking items

**Opening a container must never force its contents into the player's inventory.** The chest's
`[entity.test.pass]` used to set `loot = true`, auto-transferring everything the instant the
lock was picked — this was reverted because a player has no chance to *examine* something (ex:
a cursed weapon) before deciding whether to take it if it's already silently in their pack.
Unlocking a container now only ever dismisses `"locked"`; the contents stay put until the player
deliberately examines or takes something.

This is a distinct interaction path from the whole skill/dice system, because looking at or
picking up something already accessible doesn't warrant a roll:
- **`NLPCore._detect_item_intent(processed_text)`** checks for an `"examine"` verb
  (`examine`/`inspect`/`look at`/`check out`) or a `"take"` verb (`take `/`grab `/`pick up`/
  `loot `) *before* skill matching runs. **Phrases, not bare words, on purpose** — a bare
  `"pick"` would misfire on `"I pick the lock"` (the existing `finesse` lockpicking flow), so
  `"pick up"` (two words) is required instead; verified this doesn't regress lockpicking.
- **`NLPCore.map_to_item(processed_text)`** — the same embedding-match pattern as
  `map_to_action`, but against every `supertype == "object"` entity's name/description (built in
  `_on_rules_loaded` alongside the skill embeddings). **Currency is checked first as a fixed
  synonym list** (`gold`/`coin`/`currency`/`money`), returning the sentinel item name
  `"currency"` — it's a plain integer field on entities, not an object-supertype entity with a
  name to embed, so there's nothing to match it against semantically.
- If both a verb and an item are found, NLPCore publishes **`item_interaction_detected`**
  (`{intent, item_name, input, score}`) *instead of* running skill matching for that input at
  all. A recognized verb with no item match still falls through to normal skill matching (ex: in
  case the phrase legitimately meant something else).
- **`DMCore._on_item_interaction_detected`** resolves it with zero dice rolls: locked container
  → denied (`reason: "locked"`); `item_name == "currency"` → `transfer_currency` (`"take"`) or
  just describes the amount (`"examine"`), via the same fixed sentinel; `item_name` equal to the
  current target's own name → `describe_character` for `"examine"`, `reason: "not_takeable"` for
  `"take"` (you can't pocket the whole chest); otherwise checked against the target's `inventory`
  list — `"take"` calls `transfer_item`, `"examine"` just reads `entities[item_name]["description"]`
  and changes nothing. Publishes `item_interaction_resolved` either way (`found` plus
  `description`/`container`/`amount`/`reason` as applicable) so narration always has something
  to say, never silence.
- **`LLMCore.generate_item_interaction_response`** narrates it — explicitly telling the LLM
  `"nothing is taken, moved, or changed"` for a found `"examine"`, so it doesn't imply a transfer
  that didn't happen. Verified live: examining `items.toml`'s `"cursed dagger"` (added specifically
  to exercise this) describes its glowing runes without adding it anywhere; a separate `"take"`
  afterward is what actually moves it, narrated as its own distinct moment.

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
- Real data bugs found and fixed along the way (in case similar ones lurk elsewhere):
  `creatures.toml`'s wolf had `[skills]`/`[abilities]` as top-level tables instead of
  `[entity.skills]`/`[entity.abilities]`, silently loading with zero skills. `characters.toml`
  equipped a `"shortsword"` that didn't exist in `items.toml` (only `"longsword"` did) — fixed
  independently by the user mid-session. A duplicate `duration` key in one TOML inline table
  made `rules.toml` fail to parse entirely (caught silently by `load_rules`'s per-file
  try/except, so nothing crashed — it just silently loaded with less data than expected).
  **Lesson:** `load_rules`'s blanket exception handling means a malformed TOML file fails
  *quietly*. If rule data seems to be missing, check for a parse error before assuming logic
  is wrong.

## LLM integration

Endpoint is LM Studio's OpenAI-compatible API. Verified live against `google/gemma-4-e4b`.
Two gotchas specific to LM Studio:
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

`textual` and `pytest-asyncio` are installed but not yet in a `requirements.txt` (there isn't
one yet, dependencies are just installed ad hoc: `sentence-transformers`, `numpy`, `textual`,
`pytest`, `pytest-asyncio`).

## Testing

One file, `test_all.py`, ~89 tests across thirteen `unittest.TestCase` classes plus the Textual
mirror's standalone `pytest.mark.asyncio` functions (all previously separate `test_*.py`
files, combined into one on request). All fast except `TestGameBoot` and
`TestNlpConfidenceThreshold` (both load the real `sentence-transformers` model, ~15-20s each
— `TestNlpConfidenceThreshold` uses `setUpClass` instead of `setUp` so its two test methods
share one load rather than paying that cost twice) and `TestInnkeeperConversation` (see below,
~20-30s and needs a running LM Studio). Run with `python -m pytest -q`.

- `TestGameBoot` — original integration smoke test (boot → skill match).
- `TestNlpConfidenceThreshold` — a low-confidence input (a plain greeting) triggers no
  `action_detected` at all (but does publish `action_not_understood`), while a clear action
  still triggers `action_detected` at/above `NLPCore.confidence_threshold`. Also covers
  `_detect_item_intent` (examine/take/neither), the `"pick the lock"` vs `"pick up X"`
  collision guard (a real `user_input_submitted` of `"I pick the lock"` still produces
  `action_detected` for `finesse`, never `item_interaction_detected`), and a full-pipeline
  check that a known item name (`"cursed dagger"`) is matched regardless of which scenario is
  currently active (matching is global; DMCore is what checks scene-relevance).
- `TestClarificationResponse` — `action_not_understood` queues a prompt describing what the
  player said with no roll/damage shape in it (checked via `llm_core.context_window`
  immediately after publish, since `_queue_narration` appends synchronously before spawning
  the network fetch thread — no need to wait on or mock LM Studio for this). Also covers
  `_describe_outcome` directly (a `result["loot"]` of `{"currency": 20, "items": []}` produces
  `"20 currency"` in the text, and no loot present omits the "gains" text entirely) and
  `generate_item_interaction_response`'s three prompt shapes (found-examine explicitly says
  nothing was taken; found-take on the `"currency"` sentinel names the amount, not the literal
  word "currency"; not-found explains the denial reason).
- `TestInnkeeperConversation` — the only test that hits a real, running LM Studio (guarded by
  a module-level `_lm_studio_reachable()` check via `@unittest.skipUnless`, so it *skips*
  rather than fails when nothing's listening on `127.0.0.1:1234`, keeping the rest of the
  suite network-independent). Boots the full `NLPCore`→`LLMCore`→`DMCore` pipeline against the
  `tavern` scenario and drives a real 3-turn conversation with the `innkeeper` via
  `user_input_submitted` (not by hand-constructing `action_detected` payloads), printing the
  transcript. One turn ("I try to charm her") is phrased to clear `confidence_threshold` and
  resolve as a genuine `charisma` action (asserted: exactly one `action_resolved`, never
  `round_resolved`, `defender_details` present); the other two are natural follow-ups that
  route through `action_not_understood` instead (see the `NLP_Core.py` gotcha above) — both
  are asserted non-empty/non-error. **Deliberately does not** assert on exact narrative content
  (ex: that the husband question literally says "bandit"/"husband") — an early version did,
  and it failed on a real run where the LLM conveyed her grief ("a deep, painful sadness...
  vague sigh") without ever using those words. Live LLM phrasing isn't a reliable regression
  signal; the printed transcript is how this actually gets verified (read it when the test
  runs, don't just trust the green checkmark).
- `TestOpposedResolution` — highest-rated opposing skill selection, pips counting toward the
  rating, no-match-defaults-to-zero.
- `TestDamageCalculation` — bonus resolution (flat/rule-reference/unknown), damage reduction
  by tag, HP floor at zero, two full `calculate_damage` runs.
- `TestCombatLoop` — `find_attack_ability` (equipped weapon > innate fallback > none),
  miss-does-nothing, hit-applies-damage, non-attack-skill-never-damages, no-target publishes
  `action_resolved` (not `round_resolved`), scenario load publishes `scenario_loaded` once, a
  missing scenario name raises `FileNotFoundError` instead of starting with no scenario data.
- `TestStatusEvaluation` — hp% requirement matching, trigger filtering, in/not_in,
  unknown-operator-fails-safe, auto-apply on damage, tier-accumulation (documented gap, not
  a bug).
- `TestScenarioLoading` — duplicate-name disambiguation, instance independence, `entity_id`,
  band assignment, unknown-entity-in-scenario doesn't crash.
- `TestLockedChest` — boots `DMCore(event_bus, scenario_name="dungeon")` (`items.toml`'s
  `chest`): starts locked (`is_locked` true, seeded from `[entity.conditions.locked]`), never
  hostile despite having no attitude data, `strength` (not in the chest's `test.skill`) falls
  through to the ordinary opposed path and gets resisted by its `fortitude` (5 dice) rather than
  the `[entity.test]` difficulty, a failed `finesse` pick leaves it locked *and* applies the
  permanent `"jammed"` condition (`test.fail`), a successful one dismisses `"locked"` **without**
  auto-transferring anything (`result` has no `"loot"` key, `chest`'s `currency`/`inventory`
  untouched — see "Examining and taking items"), a repeat pick attempt on an already-open chest
  falls through to the ordinary path (difficulty `0`) instead of re-running the test
  (`requires_condition`), a `"jammed"` chest permanently refuses further pick attempts the same
  way (`blocks_if_condition`), `dismiss_condition` no-ops (returns `False`) for a condition
  that isn't present, and `apply_test_outcome` no-ops for an empty/`None` outcome.
- `TestItemInteraction` — `DMCore._on_item_interaction_detected` against the dungeon's chest
  (holding `items.toml`'s `"cursed dagger"` plus `currency = 20`): blocked entirely while locked
  (`reason: "locked"`), `"examine"` returns the item's description without touching either
  entity's `inventory`, `"take"` actually calls `transfer_item`, the `"currency"` sentinel uses
  `transfer_currency`/an amount-only description instead of `transfer_item`, an absent item
  reports `reason: "not_present"`, examining the container itself (`item_name == target_name`)
  uses `describe_character`, and taking the container itself reports `reason: "not_takeable"`.
- `TestInventoryTransfer` — `transfer_currency` (moves all by default, clamps to what's
  available, no-ops for a missing entity), `transfer_item` (moves exactly one matching entry
  out of a duplicate-containing `inventory` list, returns `False` if absent), and `loot_entity`
  (sweeps currency + every inventory item in one call, returning a `{currency, items}` summary).
- `TestNpcDialogue` — boots `DMCore(event_bus, scenario_name="tavern")` (`npcs.toml`'s
  `innkeeper`): a non-hostile NPC's `is_hostile` reads false, talking to them narrates
  immediately via `action_resolved` (not batched into `round_resolved`) even for an attack
  skill, and a hostile target (`wolf`) still batches into `round_resolved` — a regression
  guard for the hostility-based branch. Also covers `describe_character` (includes
  description/qualities/memories/quotes, empty for a pure-mechanics entity like `wolf`),
  `scenario_loaded`'s `characters` roster, and `action_resolved`'s `defender_details`.
- The trailing `test_*` async functions (no class — plain `pytest.mark.asyncio`, mirroring
  the original `test_textual_core.py`) — see "Textual mirror" section above for what each
  case guards against; these encode real gotchas, not just feature coverage.

A comprehensive-looking test suite is only useful if it stays committed — several of these
were found deleted from disk mid-session in an earlier version of this repo (with no clear
cause) and had to be restored from git history or recreated from conversation history. Worth
a `git status` gut-check if test results seem to regress unexpectedly.

## Open threads / natural next steps

Roughly in the order they came up, none started yet:
- Automatic status dismissal (`dismiss_condition` exists and is manually wired for the locked
  chest, but nothing re-evaluates `[[status]]` requirements to auto-dismiss a condition whose
  trigger no longer holds — ex: healing back above a wound tier still leaves it applied).
- Real targeting (multiple live enemies, band/range awareness, switching target on kill).
- `enhance`'s variable condition design (skills/modifier parameterized per apply-site,
  mirroring how `damage_value` already works).
- GUI `Party Status`/`Notes`/`Map` tabs have display methods but nothing publishes to them.
- `techniques.toml`'s `"user.weapon.dice"`-style indirection (ability damage derived from
  whatever's currently equipped) isn't resolved by `resolve_damage_value` yet.
- No `requirements.txt` / dependency manifest.
