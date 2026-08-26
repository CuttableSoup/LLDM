# LLDM

An autonomous dungeon master: the player types free-text actions, NLP maps them to a skill,
a simplified D6 (West End Games) engine rolls dice and resolves outcomes, and a local LLM
(currently Gemma via Ollama at `http://127.0.0.1:11434`) narrates what happened. Skills,
entities, items, spells, rules, and scenarios are all data-driven via TOML, organized into
"settings" — self-contained sibling directories under `Rules/` (`Rules/Fantasy/`,
`Rules/Zombie/`), each independently scanned by `load_rules`. None of the engine itself is
fantasy-specific — `DMCore(event_bus, scenario_name, setting="Fantasy")`'s own `setting` param
picks which one to boot from (`Rules/<setting>/scenarios/<scenario_name>.toml` and every
sibling `Rules/<setting>/*.toml`), and it round-trips through a save file (`dm_state.json`'s
own `"setting"` key) so a resumed save reloads from the same setting it was saved under.
`Rules/Fantasy/` is the deep, primary setting; `Rules/Zombie/` is a bare-bones second one (a
Left 4 Dead-inspired survival shooter) that proves the engine is setting-agnostic — see
`Rules/Zombie/scenarios/rooftop.toml` (`python LLDM.py rooftop --setting Zombie`). Every
setting authors its own skills/rules/races from scratch — nothing is shared or inherited
between settings, deliberately, so one setting's data can never leak into another's.
GUI-driven character creation (Character → Create...) and NPC generation are still wired to
`Rules/Fantasy/` only; a second setting is reachable only via CLI quick-boot (`--setting`) or a
save file that already carries its own `"setting"`.

## Architecture

Six modules wired through `Event_Bus.py`, a synchronous pub/sub bus (`publish` calls every
subscriber immediately, over a snapshot of that event's subscriber list taken at the start of
the call — so a handler that subscribes a new callback for the event it's currently handling
doesn't have that callback invoked in the same `publish`, only the next one). `LLDM.py` boots
`NLPCore`, `LLMCore`, `GUICore` in that order at startup, but **not** `DMCore` — see "Booting
the game" for when and how it's actually constructed.

- **`DM_Core.py`** — `DMCore`'s `__init__` plus its three event handlers
  (`_on_turn_detected`, `_on_item_interaction_detected`, `_on_dialogue_detected`) and their
  direct helpers. `_on_turn_detected` also calls `_on_item_interaction_detected` directly, once
  per item-kind clause in a mixed turn — see "Multiple actions". Composed from sibling mixin
  files, each owning one concern: `DM_Rules.py` (TOML/scenario/room loading), `DM_Combat.py`
  (dice rolling, opposed checks, damage, ability/behavior resolution), `DM_Status.py`
  (statuses/conditions, entity tests), `DM_Inventory.py` (currency/item transfer, plus
  equip/unequip/drop/use/container item-interaction intents), `DM_Social.py` (attitudes,
  character description), `DM_Movement.py` (bands, range, plus room-transition/formation
  intents), `DM_Persistence.py` (save/load), `DM_CharacterCreation.py` (baking a finished
  character-creation result onto the player entity), `DM_Dialogue.py` (resolving who's being
  addressed in free-form conversation), `DM_Help.py` (the reserved "ADaM" out-of-character help
  persona), and `DM_Improvisation.py` (ad hoc entity creation/removal via LLM function calling).
  Python's MRO flattens every mixin method onto one `DMCore` instance, so call sites don't care
  which file defines a given method.
- **`NLP_Core.py`** — thin EventBus glue: subscribes to `user_input_submitted`/
  `rules_loaded`/`item_catalog_updated`, delegates to `Intent_Classification.py`'s
  `IntentClassifier`, and publishes whatever events come back. Also defines
  `SentenceTransformerMatcher`, the production `IntentMatcher` adapter — owns the loaded
  `sentence-transformers` (`all-MiniLM-L6-v2`) model and every precomputed skill/item/target
  embedding tensor, and is the one place in this file that still touches the EventBus for
  granular "mapped input to X" diagnostics, since encoding/scoring is where those facts become
  known. `NLPCore` itself owns no classification logic.
- **`Intent_Classification.py`** — pure, EventBus-independent: `IntentClassifier.classify()`
  returns `(processed_text, events)` — a list of
  one or more `{"event", "payload"}` dicts for the glue layer to publish, rather than publishing
  anything itself (`AdHoc_Generation.py`/`DM_Improvisation.py` is the same pure/glue split).
  Depends on one seam, `IntentMatcher` (embedding-based skill/item/target matching —
  `SentenceTransformerMatcher` in production, a canned `FakeMatcher` in tests), for everything
  it can't resolve by keyword/regex alone. Matches free text against item names/directions/
  save-load prefixes (see "Items and movement as intents"), against `DIALOGUE_KEYWORDS` (see
  "Dialogue"), and against the reserved `ADAM_NAME_PATTERN` (see "ADaM") — gated by
  `REMOVAL_KEYWORDS`, a cheap local pre-check for whether an ADaM-addressed message is worth a
  synchronous ad hoc-removal LLM call (see "Ad hoc entity creation and removal"). An item-verb
  clause whose item name doesn't match anything is tracked and, if the whole turn would
  otherwise resolve to nothing, published as `improvisation_requested` instead of
  `action_not_understood`. Splits input into one or more clauses (`split_action_clauses`) and
  classifies each independently as an item interaction or a skill/ability action — merged into
  one `turn_detected {clauses: [{kind: "item", intent, item_name} | {kind: "action", skill,
  score, target?}, ...], input}` event, always a list, even for the ordinary single-clause input
  (see "Multiple actions" for the full classification order); if no clause resolves to anything,
  publishes `action_not_understood` instead. `IntentMatcher.register_item` incrementally
  re-registers a newly-created/reload-restored ad hoc entity's name/description on
  `item_catalog_updated`, the one event here that isn't input-driven.
- **`LLM_Core.py`** — posts to Ollama's OpenAI-compatible `/v1/chat/completions` on a
  background thread, with a rolling 100-message context window. Subscribes to nine narration
  triggers (see "Narration").
- **`GUI_Core.py`** — Tkinter window: history pane + tabbed Party/Notes/Map/Debug panels, plus
  three menus: Character (Create... only), File (Save.../Load...), Scenario (Load... only).
  Character → Create... opens the race/point-buy dialog (`Character_Creation_GUI.py`), publishes
  `character_created`, then (if a game hasn't already started) stashes the result as
  `self._pending_character` and unlocks Scenario → Load...; File → Load... opens a slot-picker
  (every subdirectory of `Saves/`) and publishes `load_requested` directly, since a save already
  carries its own scenario. Scenario → Load... is `DISABLED` until a character is pending;
  picking it lists every real scenario (`list_available_scenarios`, `DM_Rules.py` —
  `character_test` excluded) and publishes `scenario_selected {"scenario_name", "character"}`
  paired with `self._pending_character`, then locks itself shut again. `_on_game_started`
  (subscribed to `rules_loaded`) locks Scenario → Load... shut for the rest of the session.
  `GUICore` never constructs a `DMCore` itself — it only ever publishes; see "Booting the game"
  for who's listening. History mirrors `llm_response_ready`; Party redraws on
  `rules_loaded`/`party_status_changed` as a `ttk.Treeview` (one node per `is_player`/`is_party`
  entity, expanding into Equipment/Skills/Abilities/Inventory/Conditions — Equipment lists every
  valid slot for the member's supertype/subtype, filled or `(empty)`, via `get_equip_slots`'s
  same override precedence as `get_attitude`). Membership is filtered through the payload's own
  `"scenario_entities"` list, not `is_player`/`is_party` alone — `self.entities` can still hold
  an *uninstanced* `is_party` template that isn't part of the live scenario, which must not show
  up on the Party tab just for existing there. `DM_Combat.py`'s `get_party_challenge_rating`
  filters the same way (see "Challenge rating"). Notes is a free-typed scratchpad with its own
  save/load slice; Map is a free-form drawing canvas the engine never reads; Debug overwrites
  (not appends) the most recent LLM request/response on every `llm_debug_updated`.
- **`Textual_Core.py`** — a parallel, headless-testable mirror of `GUI_Core`'s output, driven
  the same way via `user_input_submitted`. Not part of `LLDM.py`'s boot sequence; run standalone.
  Used by `test_unit.py` for pilot-driven UI tests.
- **`Logger.py`** — subscribes to `log_info`/`log_error`, prints with timestamps.

## Action resolution pipeline

`user_input_submitted` → `NLPCore` → `turn_detected {clauses: [{kind: "item", intent,
item_name} | {kind: "action", skill, score, target?}, ...], input}` → `DMCore` resolves every
entry in `clauses` → `round_resolved` (combat) or `action_resolved` (no combat) → `LLMCore`
narrates → `llm_response_ready` → GUI/Textual display it. `clauses` is always a list, even for
the common single-clause input, and always mixes item-interaction and skill/ability entries
freely — see "Multiple actions" for how more than one entry changes resolution.
`_on_turn_detected` and `_on_item_interaction_detected` both also call `_publish_party_status`,
which re-publishes `party_status_changed {"entities": self.entities}` so `GUICore`'s Party tab
redraws after anything that could have changed a party member's HP/equipment/inventory/
conditions.

Inside `DMCore._on_turn_detected`, an item-kind entry is resolved immediately, in clause order,
via the ordinary `_on_item_interaction_detected` (narrating separately, right away); an
action-kind entry goes through:
1. Resolves the acting skill's ability (weapon/spell/technique/innate) via
   `resolve_named_ability`/`select_ability_skill` if the matched name is an ability, else
   `find_attack_ability` for a bare skill.
2. If the ability has a range and the target is out of it (`is_in_range`), the action fails
   immediately with `reason = "out_of_range"` — no roll happens.
3. Otherwise resolves against `self.current_target` (see "Combat"), or against an item-level
   `[entity.test]` target one level deeper (a container's contents or something already in
   inventory — see "Entity tests"), or with no target at all (difficulty 0). Every dice roll
   here is reduced by this turn's own `dice_penalty` (see "Multiple actions").
4. On a hit, `calculate_damage` rolls damage, resolves the `bonus` field (plain number or
   `"user.<rule>"` reference into `rules.toml`), applies armor/resistance reduction and
   vulnerability bonus, and `apply_damage` applies net damage to HP. Damage itself is never
   reduced by `dice_penalty` — only the skill/action roll that earned it.
5. `apply_damage` also calls `evaluate_statuses(entity_name, "on_damage")` (see "Status and
   conditions").

Once every action-kind entry has resolved, `DMCore` decides `round_resolved` vs.
`action_resolved` — and, for combat, runs every other scene entity's own turn — exactly once
for the whole batch, not once per entry (see "Multiple actions").

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

A `round_resolved` payload carries the player's resolved actions (`"actions"`, a list — see
"Multiple actions") plus `"turns"`: every other living scene entity's resolved action via
`resolve_behavior_action` (`DM_Combat.py`), driven by each entity's `[[entity.behavior]]` table
— a declaration-order list of `{requirements, action}` entries, matched top-down (requirements
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

## Multiple actions

The player may attempt more than one action in a single turn — the West End Games D6
"multiple actions" rule: every action beyond the first, movement and speech excepted, costs
every one of that turn's actions a cumulative -1D (two actions: -1D each; three: -2D each;
...). Movement (`advance`/`retreat`) and speech (see "Dialogue") never reach
`_on_turn_detected` at all — they're their own diceless event/pipelines — so they're free by
construction.

**Item interactions count too.** Drawing a weapon, picking something up, giving/trading/
opening/using an item are all "an action" in the same sense swinging a sword is — a diceless
item interaction (examine/equip/unequip/drop/take/give/trade/open/close/use — see "Items and
movement as intents") costs a turn slot exactly like a skill/ability entry does. It just never
receives `dice_penalty` itself, since it never rolled anything to begin with (an item *test*
that does roll, ex: picking a lock, both counts *and* gets penalized).

**Detection.** `Intent_Classification.py`'s `IntentClassifier.classify` splits input into
clauses once (`split_action_clauses`, on `ACTION_CLAUSE_PATTERN`: `--`, `?`, `,`, `;`, `:`, and
the standalone words `"and"`/`"then"`, `\b`-anchored so a word merely containing one of those
substrings never splits), after save/load, inter-room movement, and location-to-location travel
have all had their whole-input shot (in that order — see "Location-to-location travel"). Each
clause is classified independently, in two passes:
1. **Item-interaction pass.** `detect_item_intent` runs per clause. `EXEMPT_ITEM_INTENTS`
   (`advance`/`retreat`/`formation_behind`/`formation_abreast`) publish their own free-standing
   `item_interaction_detected` immediately and never join the shared turn (so `"attack the wolf
   and retreat"` still lets the retreat through). Everything else resolving as an item
   interaction joins the shared clause list as a `{"kind": "item", ...}` entry; a clause that
   doesn't is deferred to pass 2.
2. **Dialogue, then skill/ability matching.** Dialogue detection runs once against the whole
   input, only once pass 1 found nothing at all, so a genuine item verb naming an entity (ex:
   `"give the sword to Anne"`) is never swallowed as dialogue. Whatever pass 1 didn't claim is
   matched via `map_to_action`/`map_to_target` per clause, joining the list as a `{"kind":
   "action", ...}` entry — a clause missing `confidence_threshold` is simply dropped, not
   reported as `action_not_understood` on its own. A plain single-clause input always resolves
   to a list of exactly one entry. Same-skill-multiplier phrasing (ex: "attack it twice") is
   out of scope — only distinct clauses are detected as distinct actions.

**Resolution.** `dice_penalty = max(0, len(clauses) - 1)`, computed once per turn from the
combined item + action clause count and threaded through every dice-rolling action-kind entry:
`resolve_action`/`resolve_opposed_action` (`DM_Combat.py`) subtract whole dice (never pips) from
the *acting* entity's pool, floored at 0. For an opposed roll only the attacker's roll is
reduced — the defender's difficulty roll is computed before `dice_penalty` is applied.
`_on_turn_detected` loops every clause: item-kind entries resolve immediately via
`_on_item_interaction_detected`; action-kind entries resolve through the same phase helpers a
lone action always has, collecting into `player_actions`. The whole turn calls
`_resolve_combat_round` exactly once, after every action-kind entry resolves (tracked via
`engaged_combat_target`: an item-interaction/item-test-only turn must never trigger a round just
because `self.current_target` happens to already be hostile from an earlier turn). Item-kind
entries narrate separately (their own `item_interaction_resolved`, ahead of the batched
action-kind entries) rather than folding into one merged prompt.

`LLMCore._describe_player_actions` describes every entry in `"actions"`, preceded by a line
naming the shared penalty whenever there's more than one, so narration reads as one character
splitting their attention rather than several independent, equally-precise attacks.

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

## NPC generation

`[[entity_template]]` is a genuinely different top-level key from `[[entity]]`, loaded into its
own `self.entity_templates` dict, never `self.entities`. Every entity_template in
`Rules/Fantasy/` today is scenario-local (ex: `tavern_random.toml`'s own
`generated_innkeeper`/`generated_stranger`). Keeping generation stubs in a separate namespace
means a scenario/room entry names a real entity via `{ name = "wolf", band = 1 }` (looked up in
`self.entities`) but an entity_template via `{ template = "generated_stranger", band = 2 }`
(looked up in `self.entity_templates`) — `_instance_entities` (`DM_Rules.py`) checks which key
an entry has and resolves accordingly; a `name` that only exists as a template, or vice versa,
fails the same `log_error`-and-skip way a genuine typo would. An entity_template skips
authoring its own `[entity.skills]`/`max_hp`/`name` — those are decided the moment it's
instanced, via a local-LLM function call fit to a target challenge rating. Everything else
(`[entity.attitudes]`/`[[entity.behavior]]`/`abilities`/`equipped`/`is_party`/`qualities.race`)
is still hand-authored normally, deliberately varied per template so generated NPCs don't all
read as the same person wearing a different name tag. Referencing the same template twice gets
two independent instances (`generated_stranger`, `generated_stranger_2`), each with its own LLM
call.

**Varied fields.** An entity_template's scalar fields can be authored as a range or weighted
choice instead of a fixed value — `NPC_Generation.py`'s `resolve_varied_value(value)` is the
shared vocabulary: `{min, max}` (uniform random pick) or a list of single-key `{"choice" =
weight}` tables (`random.choices` pick of the key). Applied per-leaf, so a template can mix
fixed and varied entries freely (ex: `generated_stranger`'s `default` keeping `threat`
flat while disposition/familiarity vary). `hint`/`cr_multiplier`/`qualities` resolve before the LLM
call (they feed the prompt/target-CR math — gender/race/age have to be concrete before the LLM
invents a name); `currency`/attitudes resolve independently, any time after. An
`[[entity_template.attitudes.name]]` override can target the literal token `"player"` (same
reserved placeholder as `PLAYER_PLACEHOLDER`), substituted for `self.player_name` at instancing
time. `Rules/Fantasy/reference/template_schema.toml` doubles as a worked-examples file for this
vocabulary. Generation deliberately never touches `abilities`/`equipped`/`inventory` — whether
a generated NPC can fight or what it carries is still decided by whoever authors the template.

`Rules/Fantasy/npc_keywords.toml` is a small catalog of ~16 archetype keywords (`warrior`,
`trickster`, `scholar`, ...), each naming 3-4 real skills. `NPC_Generation.py` (pure) builds an
OpenAI-style `tools` payload constraining the LLM to 1-2 keyword *names* (an enum, not free
text — more reliable with small local models); `generate_npc_stats` resolves them to their
union of real skills before fitting. `fit_skills_to_cr(key_skills, target_cr, hp_share=0.3,
damage_dice=0, damage_pips=0)` is the deterministic inverse of `calculate_challenge_rating`:
`hp_units = round(target_cr * hp_share)`, `max_hp = hp_units * 3`; the remaining budget becomes
a single rating `R` (floored at 3) given identically to each of the first 3 key skills (any 4th+
gets a lower flavor-only rating). `generate_npc_stats` rolls `target_cr * cr_multiplier *
random.uniform(1-variance, 1+variance)` before fitting — that plus keyword choice is where
randomness comes from; `fit_skills_to_cr` itself stays deterministic and directly testable. On
any failure (no `tool_calls`, malformed JSON, network error, timeout, or
`skip_llm_generation=True`) it falls back to a random keyword pick with no network call —
matching the app's "Ollama is best-effort, never blocks core gameplay" posture.

`LLM_Client.py`'s `call_chat_completion` is a small, synchronous, stateless POST — deliberately
not shared with `LLM_Core.py`'s async `fetch_from_llm` (`fetch_from_llm` must never raise; this
one must raise cleanly so `generate_npc_stats`'s fallback triggers). Its hard 20s `timeout`
matters because generation runs synchronously on whatever thread is instancing the scene
(always the GUI/main thread in practice) — a known limitation: a scene with entity_template
entries visibly pauses ~5s per generated NPC on a fresh load. A two-phase "placeholder now,
patch later" redesign would fix this properly; out of scope for now.

`DM_NpcGeneration.py`'s `NpcGenerationMixin` runs from `_instance_entities` right after an
instance is stored into `self.entities` (the CR-fitting math reads back off
`self.entities[name]`). `_resolve_npc_target_cr` resolves a template's `target_cr` — a number,
or `"player"`/`"party"` (live CR, resolved fresh at instancing time). `"party"` can't call
`get_party_challenge_rating()` directly since `self.scenario_entities` isn't finalized mid-loop;
instead `_instance_entities` threads a `party_pool` param that combines with whoever's been
instanced earlier in the same loop. The result is tagged `entity["generated"] = True`.

**Save/load.** `skills`/`max_hp`/`name`/`description`/`qualities`/`attitudes` don't round-trip
for an ordinary entity (`save_game` only diffs `hp`/`active_conditions`/`currency`/`inventory`/
`equipped`/`band` — everything else re-derives from the static template) — broken for a
generated entity, which has no static template. `save_game` conditionally saves those six
fields too when `entity["generated"]` is true; `load_game` threads `skip_llm_generation=True`
through re-instancing so it takes the offline fallback path unconditionally, and the existing
overlay applies the real saved values on top.

`DM_Social.py`'s `describe_character` uses `entity.get("name", entity_name)`, not the raw dict
key, so a generated NPC's LLM-invented name is what's actually narrated.

## Movement and range

Every scenario entity — the player included — has an objective, 1-indexed `band`: a position on
the current room's own band line, not a distance-from-player. A freeform location (see
"Scenarios, locations, and rooms") has no band line of its own at all — everyone in it is
pinned to an implicit band 1, so advance/retreat there is always a no-op.
`get_distance_between(a, b)` is the absolute difference between two band numbers. The player
moves via `advance_or_retreat(direction)` (`DM_Movement.py`): shifts the player's band by up to
their `speed` (default 1) toward or away from `current_target`. A creature/ally moves the same
way via `move_toward_or_away(entity_name, opponent_name, direction)`, relative to whichever
opponent `resolve_behavior_action` resolved for it. Only the one entity that moved has its band
changed (aside from party formation, below), but because gaps are computed from both sides'
bands, one move can change its distance to every other entity at once — not always in the
expected direction, since retreating from one opponent can carry an entity toward something
else. At a zero-gap tie, "advance" is a no-op; "retreat" prefers a higher band number, falling
back to a lower one only if higher is blocked.

`move_entity`'s floor is always band 1; its ceiling is the scene's own `bands` count, enforced
only when `enclosed` is true (the default). `enclosed = false` removes the ceiling entirely —
the mechanism for fleeing a scene: once the gap to every attacker's own `range` is exceeded,
nothing can reach the fleeing entity.

**Party formation.** Every `is_party` entity carries its own `follow_offset` (int, default 0),
read by `_apply_party_formation` to snap that entity's band to `player_band + follow_offset`
(ex: `crypt.toml`'s `anne` trails one band behind to favor her ranged spellwork). This is a flat
teleport, not a speed-limited move, and only ever fires where the *player's* band changes
(`advance_or_retreat`, `enter_room`, `_enter_location`) — never from a creature/ally's own combat-turn movement,
which stays free to drift out of formation until the player's next move snaps it back. The
player can override `follow_offset` in play: "stay behind me"/"walk beside me" resolve to
`item_interaction_detected` intents `"formation_behind"`/`"formation_abreast"`
(`DMCore._resolve_formation_intent`) — a party member's name either is or isn't literally
present in the input (whole-word, case-insensitive), so naming one addresses only them; naming
none addresses the whole party.

`range` (int, in bands) lives on the weapon/spell/ability itself, absent/`0` meaning melee —
usable only in the target's own band. A reach weapon extends that by one band; a ranged
weapon/spell reaches however far its data says, with no accuracy difference across that range.
`is_in_range` is `True` unconditionally when `ability` is `None` (a non-physical check).

## Character creation

Race/point-buy skill dice, applied to the player entity once, before any scenario loads.
`Character_Creation.py` is pure, UI- and DMCore-independent logic — `load_character_creation_
data(rules_dir)` re-scans `Rules/Fantasy/*.toml` for `[[skill]]`, `[[race]]` (`races.toml`), and
`rules.toml`'s `[character_creation]` table directly, since a character has to be buildable
*before* a `DMCore` exists to read that data off of. `[character_creation]` holds `pool_dice`
(15 — free dice to spend across skills) and `max_allocation_per_skill` (5). Each race
(`races.toml`) is its own complete, *absolute* `[race.skill_dice]` table, one entry per skill
(human included — no implicit "base_dice" default). `race_baseline_skills` reads a skill's
value off the race's table, floored at 0, falling back to `UNTRAINED_DICE` (0) if missing.
`elf`/`dwarf`/`half-orc`/`halfling` each raise four skills to 3D and lower four others to 1D
around the 2D baseline, netting even before any allocation is spent. `validate_allocation`
rejects an unknown skill, a negative entry, anything over the per-skill cap, or a total that
isn't *exactly* `pool_dice`; `build_character_skills` is baseline + allocation for every skill.

`DM_CharacterCreation.py`'s `apply_character_creation(character)` — `character` being
`{"race", "allocation", "name"}` — is the one piece that touches `DMCore` state, called from
`DMCore.__init__` right after `_resolve_player_name()` and before any scenario loads.
`"allocation"`, if non-empty, is validated and overwrites `self.entities[self.player_name]
["skills"]` entirely and updates `qualities.race`; if absent, the override is skipped and the
template's own hand-authored skills are left untouched (this is what lets `LLDM.py`'s CLI
quick-boot pass a bare `{"name": ...}` through this same method rather than needing a separate
rename-only path). `"name"`, if non-blank and different from the current `player_name`, renames
the player entity: `self.entities[self.player_name]` is popped and re-inserted under the new
key, and `self.player_name` repoints at it. A name colliding with any other already-loaded
entity is rejected outright (`log_error`, not raised) — since this runs *before*
`load_scenario_definition`, a scenario file's own local entities don't exist yet, a known,
accepted gap. Renaming doesn't touch any other entity's `[[entity.attitudes.name]]` override
keyed to the old name. `character=None` (every caller that omits it) is a complete no-op.

`Character_Creation_GUI.py`'s `CharacterCreationDialog` (a modal `Toplevel`) is the interactive
front end: an optional name field, a race dropdown, a per-skill allocation row, and a "dice
remaining" counter gating Create until it hits exactly zero. `self.result` is always
`{"race", "allocation", "name"}` once Create is pressed, or `None` if cancelled.
`GUICore.request_character_creation` runs this and, only when not cancelled, publishes
`"character_created"`.

## Booting the game

`LLDM.py`'s `main()` never constructs `DMCore` unconditionally — no scenario loads and nothing
is narrated until a player character *and* a chosen scenario exist, via whichever route fires
first:

1. **CLI quick-boot** — `python LLDM.py <scenario> [character_name] [--setting SETTING]`. Giving
   `scenario` skips the Character menu entirely and constructs `DMCore` immediately;
   `character_name`, if also given, is passed as `{"name": character_name}` (a rename, skills
   untouched). `--setting` (default `"Fantasy"`) picks which `Rules/<setting>/` data pack
   `scenario` is resolved against. Omitting `scenario` leaves the window open for routes 2/3.
2. **Character → Create... then Scenario → Load...** — a non-cancelled dialog result publishes
   `"character_created"`, which `main()`'s `on_character_created` closure only logs a warning
   for (if `DMCore` already exists). `GUICore` stashes the character and unlocks Scenario →
   Load...; picking a scenario publishes `"scenario_selected" {"scenario_name", "character"}`,
   which `main()`'s `on_scenario_selected` closure reacts to by constructing
   `DMCore(scenario_name=..., character=...)`.
3. **File → Load...** — `GUICore.request_load` publishes `"load_requested"`. Before any `DMCore`
   exists, `main()`'s own `on_load_requested` closure handles this: it peeks the chosen slot's
   `dm_state.json` for its `"scenario_key"`/`"setting"` (`LLDM._peek_saved_scenario_key`, a plain
   file read, no live `DMCore` needed), constructs `DMCore` against that scenario/setting, then
   calls `dm_core.load_game(slot)` to overlay the rest of the saved state. This costs one
   throwaway `scenario_loaded` narration before `load_game`'s own `"game_loaded"` corrects it —
   the same double-narration cost as loading a save immediately after any ordinary new game.

`on_character_created`/`on_scenario_selected` no-op once `DMCore` already exists — Create...
only ever starts the *first* game a session has. `on_load_requested` no-ops silently instead,
since File → Load... is meaningful at any time: every load after the first is handled solely by
`DMCore`'s own `_on_load_requested`, subscribed during its `__init__` as always.

## Scenarios, locations, and rooms

`Rules/Fantasy/scenarios/*.toml` (`arena`, `tavern`, `field`, `dungeon`, `crypt`, `town`, plus
`character_test`/`scenario_entity_test`/`npc_generation_test` — see "Testing") each hold one
`[scenario]` table, kept in their own subdirectory so multiple scenarios can coexist without the
flat `load_rules` scan (which only keeps the last `[scenario]` table it reads) overwriting one
with another. Every scenario is `[scenario]` (just `name`/`description`/`start_location`) →
one or more `[[location]]` tables → optionally, per location, one or more `[[location.room]]`
tables — a location is a *superset* of a room, not a sister of it: `[[location.room]]`/
`[[location.room.exit]]` behave exactly like an ordinary room/exit, just nested one level
deeper. `Rules/Fantasy/reference/location_schema.toml`
is the field-by-field reference for the `[[location]]` shape.

**A location may declare `entities` directly, `[[location.room]]`, or both.** On a location with
no rooms at all (ex: `town.toml`'s `town_square`/`blacksmith`), `entities` is genuinely
freeform — no bands, every entry lands at an implicit band 1 (same default
`_instance_entities` already applies to any band-less entry) — fine for anywhere that never
needs real positioning. On a location that *does* have `[[location.room]]` (opted into only
when real positioning is needed — combat with meaningful advance/retreat/range, or a genuine
multi-room interior, ex: `crypt.toml`'s whole dungeon, wrapped in one location), `entities`
instead plays exactly the role a room's own `entities` doesn't: whoever persists across *every*
room in that location's own graph (ex: `crypt`'s `thane`/`anne`, following the player from room
to room) — still positioned via ordinary room bands, not freeform. A room's own `entities` list
never repeats them, only that room's local creatures/traps/chests.

**Rooms never float free at the scenario's top level.** `self.rooms`/`self.current_room_key`/
`self.visited_rooms` always describe whichever location is currently active — every method that
reads them (`_current_room`, `_populate_room`, `enter_room`, `_find_room_exit`,
`_resolve_room_transition_intent`, `_clamp_band`, `_current_ground_items`, ...) operates on
that location, re-pointed by `_enter_location` (`DM_Rules.py`) every time the active location
changes. `self.rooms` is `{}` (and `_current_room()` returns `None`) whenever the active
location is freeform.

**The player is referenced generically, and never needs to be named in any location's own
`entities` at all.** A scenario/room's `entities` list may still name the player with the
reserved sentinel `"player"` (`DM_Rules.py`'s `PLAYER_PLACEHOLDER`, resolved to
`self.player_name` before the template lookup) for a location visited exactly once — but
`_instance_location_persistent_names` guarantees the player is present in every location's own
`persistent_names` regardless, *without* re-instancing them: unlike `thane`/`anne`,
re-instancing the player via `_instance_entities` on every new location's first visit would
silently wipe `active_conditions` (any status effect gained mid-playthrough), since that
unconditionally overwrites from the template's static `conditions` field. `town.toml`'s own
locations never name the player at all, relying entirely on this guarantee.

**A scenario file can define its own `[[entity]]`/`[[entity_template]]` tables**, sibling to
`[scenario]`/`[[location]]`, scoped to this one scenario — letting a boss, one-off prop, or NPC-
generation stub live in the same file as the scenario referencing it. `load_scenario_definition`
reads these into `self.entities`/`self.entity_templates` after `load_rules` has run, so a
scenario-local entity can reuse a shared name on purpose to override it just for this scenario.
`scenario_entity_test.toml` (excluded from `list_available_scenarios`) exists solely to
exercise this.

**Every real gameplay scenario owns its own local copy of every npc/creature entity it
references** — playable standalone, without a shared creatures/characters file.
`characters.toml` keeps only `gladstone` (`_resolve_player_name` scans `self.entities` right
after `load_rules`, before any scenario loads, so the one template every boot needs resolvable
via `is_player = true` can never be scenario-local); `creatures.toml` keeps only `fire
elemental` (used directly by `test_unit.py`'s damage-reduction tests). An entity shared across
scenario files (ex: `wolf` between `arena.toml`/`field.toml`) is kept in sync by hand — no
single source of truth, the tradeoff self-containment makes on purpose. Items are out of scope
for this — a scenario's NPCs can still reference a shared item (ex: `field.toml`'s `bandit`
names `items.toml`'s `short bow`). One consequence: the rename collision check in
`apply_character_creation` runs *before* `load_scenario_definition`, so a chosen name colliding
with a scenario-local entity (ex: naming yourself "wolf" while playing `arena`) is **not**
caught the way colliding with `gladstone`/`fire elemental` still is —
`TestCharacterCreationRename`'s collision test picks `fire elemental` for exactly this reason.

`DMCore.__init__(event_bus, scenario_name="arena")` loads via `load_scenario_definition`, which
raises `FileNotFoundError` for an unknown name (fatal on purpose — an empty `self.scenario`
would let the LLM hallucinate an opening scene with no real content), then `load_scenario()` →
`_enter_location(self.scenario.get("start_location"))`. `_instance_entities` deep-copies each
named template into an independent instance, tags it with its starting `band`, disambiguates
duplicates (`wolf`, `wolf_2`, ...), and gives each instance its own `entity_id`.

`enter_room(room_key, arrival_band)` — the room-to-room move — is gated on the current room declaring a matching exit at the player's
band and on no living hostile remaining. Moves only the player's band; HP/inventory/currency/
conditions carry over. A room visited before is restored from `self.visited_rooms` rather than
re-instanced, so a cleared trap or looted chest stays that way.

**`_enter_location(location_key, arrival_room=None, arrival_band=1)`** (`DM_Rules.py`) is the
location-to-location counterpart: re-points `self.rooms`/`self.current_room_key`/
`self.visited_rooms`/`self.persistent_entities` at the new location via `self.location_runtime`
(`location_key -> {"persistent_names", "visited_rooms"}`), the same "instance once, restore
thereafter" cache `visited_rooms` itself already gives a single room, just one level up. A
location with rooms lands at `arrival_room` (or its own `start_room`) via the unchanged
`_populate_room`; a freeform location pins the player to band 1 (no real positioning exists to
place them at). Also resolves this location/room's own random encounter table on the way in
(see "Random encounters", below).

**Location-to-location travel** is reachable by naming where you want to go, not a fixed
direction word — `DM_Movement.py`'s `_resolve_location_exit` searches the current location's
own `[[location.exit]]` list for any destination whose `name` (or an `aliases` entry) appears
whole-word/case-insensitive in the input, the same "search input for a known name" pattern
`_resolve_dialogue_target`/`_resolve_formation_intent` already use for entity names — detected
by `Intent_Classification.py`'s `detect_travel_intent` (a `TRAVEL_KEYWORDS` phrase table plus a
`\bleave\b` word-boundary check, publishing a generic `"travel"` item-interaction intent with no
pre-parsed destination at all, unlike `"move"`'s own direction). `_resolve_travel_intent` falls
back to the current location's own `return_to` (a generic "leave"/"go outside" phrase) if no
destination is named, denied `reason="no_exit"` if that's also absent. **Hostile gate:** never
blocks a move taken from a location's own freeform space; always blocks one taken from inside a
`[[location.room]]` — the exact same `blocked_by_enemies` check `_resolve_room_transition_intent`
already runs for an ordinary room-to-room move, scoped to that one room's own occupants, whether
the destination is another room in the same location or a jump to a different location entirely.

**`self.entities` holds templates and live instances under the same keys** — instancing a
single-occurrence entity overwrites its template slot. `load_game` re-runs `load_rules()` before
re-instancing for this reason (see "Saving and loading").

## Random encounters

`[[location.encounter]]` (or `[[location.room.encounter]]`, same shape) is a weighted-choice
table resolved once, `on_enter`, every time its own location/room is entered —
`DM_Encounters.py`'s `EncounterMixin`, called from `_enter_location`. `trigger` is always
`"on_enter"` today (a repeating per-turn `"ambient"` roll is a deferred, undesigned extension).
`encounter` is the exact same `[ { "choice" = weight }, ... ]` shape `NPC_Generation.py`'s
`resolve_varied_value` already resolves for an `[[entity_template]]`'s own `hint`/
`qualities.race` (see "NPC generation") — reused directly, not a new probability mechanism, and
rolled fresh every visit rather than instanced-once-and-cached the way `visited_rooms` treats
ordinary entities. Each resolved key is handled the same way an ordinary `entities`-list entry
already would, tried in order: (1) a real `[[entity]]`/`[[entity_template]]` name — instanced
exactly like any other `{name = ...}`/`{template = ...}` entry (band defaults to the player's
own current band), joins `self.scenario_entities`, and claims `current_target` if hostile and
nothing's already engaged (`ImprovisationMixin._claim_current_target_if_free`) — friendly or
hostile is decided entirely by *that entity's own* `[entity.attitudes]`/`[[entity.behavior]]`
data, same as every other entity in the game, not by any field on the encounter itself; (2) the
reserved key `"nothing"` — a deliberate no-op, no entity, no narration; (3) otherwise — the
string itself is a flavor narration beat, no entity created (same shape ad hoc generation's own
`describe_scenery` already produces). Publishes `encounter_triggered` either way (skipped
entirely for a `"nothing"` result), narrated by `LLMCore.generate_encounter_response` — the one
narration trigger that's never a response to something the player did; it fires as a side effect
of simply arriving somewhere.

## Status and conditions

`rules.toml`'s `[[status]]` table drives derived conditions. Each entry has:
- `trigger` — when to evaluate it; only `"on_damage"` is wired today, called from both
  `apply_damage` and `apply_healing` (see "Damage and healing").
- `requirements` — a list of `{field, operator, value}` comparisons (`COMPARATORS` in
  `DM_Status.py`: `>`, `<`, `>=`, `<=`, `==`, `!=`, `in`, `not_in`), ALL of which must hold.
  `field` is either derived (`"hp_per_remain"`) or a direct entity attribute.
- `apply` — `{condition, duration, dismiss}`, naming an entry in `[[condition]]`.

`entity_matches_requirements`/`get_comparable_value` are the shared engine behind both
`[[status]]`'s own requirements and `[[entity.behavior]]`'s; an optional `opponent_name` param
resolves the two opponent-relative derived fields, `"distance_to_target"` (the band gap to
`opponent_name`) — used by a creature choosing *between* attack options by range, ex:
`field.toml`'s `bandit` favors its `short bow` while `distance_to_target > 0`, falling to its
`rusty shortsword` once that gap closes to 0 — and `"opponent_has_condition:<name>"` (below).
Two more derived fields read `active_conditions` directly rather than a numeric/positional
value: `"has_condition:<name>"` (a boolean presence check against the checking entity's own
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
currently meets and calls `apply_condition`, storing it in `entity["active_conditions"]`.
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

## Entity tests

A `[entity.test]` block is a skill check against an entity itself (ex: `items.toml`'s `chest`
lock; see `Rules/Fantasy/reference/entity_schema.toml` for every field it can carry).
`is_test_available(target, test, skill_name)` gates it: `skill_name` must be in `test["skill"]`;
`requires_condition` (if set) must currently be active; `blocks_if_condition` (if set) must not
be. A skill not in `test["skill"]` isn't blocked — it just isn't a test, and falls through to
ordinary opposed-skill resolution instead.

A scene-level test (the target itself, via `self.current_target`) is resolved as a flat
difficulty check (`resolve_action`), not through `resolve_opposed_action`.
`_resolve_item_test_target`/`_resolve_item_test` handle the same mechanism one level deeper — an
item already in the player's inventory, or sitting in a reachable container — tried before
combat-target redirection so inspecting an item never becomes an attack.

`apply_test_outcome(entity_name, outcome)` dispatches on whichever keys are present in the
matched `pass`/`fail` table: `dismiss_condition` removes a condition, `condition` applies a new
one, `loot` transfers everything on the target via `loot_entity`, and `reveal` (truthy) applies a
permanent `"identified"` condition — the content it reveals is read back off the entity's own
`tags` field by whoever narrates it, not stored on the outcome itself.

## Inventory and currency

- **`transfer_currency(from_name, to_name, amount=None)`** — moves currency; `amount=None`
  moves all of it; clamps to what's available; no-ops on a missing entity.
- **`transfer_item(from_name, to_name, item_name)`** — moves one matching `inventory` entry;
  duplicates represent quantity, so callers loop for more than one. Always needs a real
  `from_name` to remove the item from.
- **`place_new_item(destination_name, item_name)`** — adds an item to `destination_name`'s
  inventory with no source at all; the primitive `transfer_item` can't cover, for a freshly
  conjured ad hoc item that never existed anywhere before this moment (see "Ad hoc entity
  creation and removal" — `DM_Improvisation.py`'s own placement logic is this primitive's one
  caller today).
- **`loot_entity(from_name, to_name)`** — sweeps all currency plus every inventory item.

## Items and movement as intents

Looking at, taking, giving, trading, opening, closing, using, equipping, dropping, moving
between rooms, and directing the party's formation all bypass the *skill/dice* system entirely
— none of them warrant a roll (most still cost a turn action and share in the multi-action
penalty pool — see "Multiple actions"). `Intent_Classification.py`'s `detect_item_intent`
recognizes phrase-level keywords for thirteen intents, run per clause once save/load and
inter-room movement have had their own whole-input shot: `examine`, `equip`
(`equip`/`wear`/`wield`/`put on`), `unequip` (`unequip`/`take off` — deliberately not a broader
`remove`, which would collide with items.toml's own trap names and finesse's `disarm`/`trap`
keywords), `drop` (`drop`/`discard`/`put down`), `take`, `give`, `trade`, `open`, `close`, `use`
(currently `drink`/`quaff`), `formation_behind`/`formation_abreast`, and direction/movement
phrases for `advance`/`retreat`/`move`. `advance`/`retreat`/`formation_*` are
`EXEMPT_ITEM_INTENTS` — free, published as their own free-standing `item_interaction_detected`
and never joining the shared turn; `open`/`close` (`NO_ITEM_LOOKUP_INTENTS`) still cost a turn
slot but act on the current scene target directly rather than a named item; `move` is checked
separately, against the whole input, ahead of per-clause classification. Every other intent runs
through the `IntentMatcher` seam's own `map_to_item`, an embedding match against every
`supertype == "object"` entity's name/description (currency checked first as a fixed synonym
list, returning the sentinel `"currency"`), and — if it
resolves — joins the same shared per-turn clause list a skill/ability action does.

`DMCore._on_item_interaction_detected` resolves with zero dice rolls:
- `"equip"`/`"unequip"`/`"drop"` are checked first, since none care about target_name/the
  locked gate below at all.
  - `_resolve_equip_intent` moves an item already in inventory into whichever
    `[entity.equipped]` slot its own `equip_slot` field resolves to for the player's
    supertype/subtype (`rules.toml`'s `[[equip_slot]]` via `get_equip_slots`). Denied
    `"not_present"`/`"not_equippable"`/`"cant_equip"` as appropriate. An item already sitting in
    the chosen slot is displaced (still in inventory) rather than refusing.
  - `_resolve_unequip_intent` only clears the slot mapping — denied `"not_equipped"` if it isn't
    equipped at all.
  - `_resolve_drop_intent` unequips if needed, then moves the item onto the current room/scene's
    own ground (`_current_ground_items`) — this round-trips through save/load like everything
    else (see "Saving and loading").
  - A later `"examine"`/`"take"` aimed at a ground item is resolved by `_resolve_ground_intent`
    before falling through to the ordinary target-based path below.
- `"examine"`/`"take"` against an item already sitting in the player's own inventory (ex: an ad
  hoc item `DM_Improvisation.py` placed straight into inventory) resolve directly against the
  player, computed as its own `already_owned` flag right alongside the ground-item check above —
  checked *ahead of* the locked/closed-target gates and the item-is-target-itself check below,
  not just the source/destination resolution, so an unrelated locked/closed container elsewhere
  in the scene never blocks examining something the player already possesses.
- A locked container denies everything else (`reason: "locked"`).
- `item_name` equal to the current target's own name addresses the target itself, not something
  inside it.
- A closed (but unlocked) container denies reaching its contents (`reason: "closed"`) while still
  allowing examine/open.
- `"take"`/`"trade"` move an item to the player; `"give"` moves one to the target; `"trade"`
  additionally charges the item's TOML `value` (`reason: "cant_afford"` if unaffordable) —
  deliberately excluded from the `already_owned` short-circuit above, since buying an
  already-owned item is nonsensical and `"trade"`'s own price-payment always pays whatever the
  scene target happens to be, independent of source/destination.
- `_resolve_open_close_intent` is gated to `subtype == "container"`; toggles `"closed"`,
  independent of `"locked"` — a picked lock still needs its own `"open"`. A successful open
  attaches `contents`: one flavor-description string per item inside.
- `_resolve_use_intent` activates/consumes an item, gated on a truthy `usable` field: healing
  (`healing = {dice, pips}`, via `apply_healing`) and/or poison (`poison = {dice, pips}`, rolled
  through the ordinary `calculate_damage`/`apply_damage` path, self-inflicted, so a
  poison-resistant/immune character correctly reduces or negates it like a real attack). Using
  an item also applies a permanent `"identified"` condition regardless. Consumption is
  charge-based (`_consume_charge`): no `charges` field means single-use; at zero charges the item
  is replaced by `replace_with` or removed.
- `_resolve_room_transition_intent` handles `"move"` (see "Scenarios and rooms").

Publishes `item_interaction_resolved` either way, with enough detail (`found`,
`reason`/`description`/`container`/`amount`/`price`/`contents`/`healed`/`charges_left`/
`replaced_with`/`slot`/`replaced` as applicable) for narration to explain a miss or a success.

## Social and attitudes

`get_attitude(entity, toward)` returns a three-value array (`disposition, threat, familiarity`,
nominally -100..100; a `name` override beats `supertype` beats `default`; no
`[entity.attitudes]` table defaults to all-neutral). Collapsed from an original six
(`disposition, trust, confidence, respect, obligation, intimacy`) after NLI zero-shot testing
(see "Dialogue sentiment") found only three axes reliably separate from each other when read off
dialogue tone — `confidence`/`intimacy` were kept and renamed `threat`/`familiarity` (same
sign/semantics: `threat` positive = safe/confident, negative = threatened/afraid; `familiarity`
positive = close/fond, negative = distant/repulsed); `trust` never separated from disposition,
`respect` collapsed back into disposition under testing, and `obligation` turned out to be
structurally event-driven rather than tone-driven (a debt/favor is a fact about what happened,
not a quality of how something was said) — see "Extended goals" for the fuller testing writeup.
`get_attitude_tier(value)` clamps to `[-150, 150]` and returns the first of seven
`[[attitude_tier]]` bands whose range contains it, in declaration order.
`describe_attitude(entity, toward)` renders all three axes as one sentence using each tier's own
phrase per axis.

`describe_character(entity_name, toward_name=None)` builds a flavor-text roster line from purely
descriptive TOML fields (`description`, `qualities`, `memories`, `quotes`) plus, when
`toward_name` is given, the attitude sentence above — deliberately excluding mechanical data.
`DMCore.__init__` builds this roster into the `scenario_loaded` payload; `_on_turn_detected` also
attaches a fresh `result["defender_details"]` per action.

`self.player_name` is resolved once in `__init__` via `_resolve_player_name()`, which scans
loaded templates for the one with `is_player = true` and raises `ValueError` if none is marked.

**Action-driven attitude drift.** A resolved player action — landing a hit, stealing something,
giving something away — nudges the target's own three-axis attitude toward the player, the same
"a 0..1 confidence/severity signal scales a per-axis delta" shape dialogue sentiment already
uses (below), just driven by what happened rather than tone of voice, and moving more than one
axis at once. `rules.toml`'s `[[attitude_event]]` table holds each event's own *full-strength*
per-axis deltas (`combat_hit`, `theft`, `favor`, `shared_enemy` today) — applied at
`magnitude = 1.0` (ex: a killing blow, or the single most valuable item `items.toml` authors);
an ordinary occurrence scales down from there. Each event authors only `disposition`/`threat`/
`familiarity` deltas now — `shared_enemy` in particular lost its only two non-disposition deltas
(`trust`/`respect`) when those axes were dropped, so it's disposition-only today; `favor` lost
its single largest value (`obligation = 20`), leaving a comparatively modest `familiarity` bump
in its place — an accepted consequence of the axis collapse, not rebalanced to compensate.
`DM_Social.py`'s `nudge_attitude_from_event(entity_name, toward_name, event_name, magnitude)`
looks up the named event and writes the scaled deltas into their own `action_attitude_deltas`
accumulator (`get_attitude` sums it elementwise alongside `attitude_deltas`, same as before) — a
no-op for an unknown event, a falsy magnitude, an entity with no `[entity.attitudes]` table at
all, an inanimate object (`supertype == "object"`), or an entity with no HP left (a dead entity
isn't aware of anything happening to it or nearby anymore, whether that's the killing blow
itself, a theft, a gift, or a battlefield bond forming), mirroring `is_hostile`'s own "nothing to
nudge" precedent for a tableless creature.

Four call sites, each computing its own 0..1 magnitude from context: `DM_Core.py`'s
`_apply_damage_if_hit` fires `combat_hit` after a landed player hit, scaled by
`net_damage / defender max_hp` — a graze barely registers, a near-kill measurably scares the
defender (the `threat` axis) even while `disposition` stays pinned at `is_hostile`'s own
floor; only the player's own attacks trigger this (an entity's own combat-turn attack,
`resolve_behavior_action`, never does — there's no player-side attitude to move). The same
method's own `_nudge_shared_enemy_bonds` then fires `shared_enemy`, at that same magnitude,
toward every *other* living scene entity that already considers the struck target a real enemy
(`is_hostile(observer, target_name)`) — "bonds made on the battlefield," deliberately not
restricted to allies/party members, so even a merely-wary bystander can start warming to the
player for fighting something the bystander already hates. Safe to call unconditionally over
every scene entity: a tableless creature's own `is_hostile` returns `True` regardless of
`target_name` (see "Combat"), but `nudge_attitude_from_event`'s own "no `[entity.attitudes]`
table" gate silently no-ops for exactly that case, so a mindless hostile creature never actually
accumulates a bond it has no data to hold. `DM_Inventory.py`'s `_resolve_transfer_intent` fires
`theft` (a `"take"` that actually moved something) or `favor` (a `"give"`) once a real transfer
completes against a real, distinct, *conscious* target (the shared HP gate above is what makes
`theft` specifically require the victim to actually be aware it's happening, rather than looting
an unconscious or dead body counting as a felt violation) — for either an item (scaled by its
own TOML `value`) or currency (scaled by the amount moved) — against `SIGNIFICANT_VALUE` (25), a
reference scale keeping most shipped items in the 0..1 range without clipping. Deliberately
excludes `"trade"` (a fair, paid exchange, not a violation or a gift) and never fires for the
player's own "already owned" self-transfer no-op (see "Items and movement as intents").

`action_attitude_deltas` is capped independently of `attitude_deltas` — `ACTION_ATTITUDE_DRIFT_CAP`
(60) rather than `TALK_ATTITUDE_DRIFT_CAP` (40) — a real betrayal or a real act of generosity can
move an axis further than words alone, and the two accumulators are tracked separately
specifically so each can enforce its own ceiling rather than sharing one. Round-trips through
save/load the same unconditional way `attitude_deltas` already does (`DM_Persistence.py`).

## Dialogue

Directly addressing someone (`"talk to the innkeeper"`, `"ask the guard about the road"`) is a
third diceless channel: there's no item involved, the addressee is resolved from the scene
rather than looked up, and the result is a generated in-character reply, not a structured
mechanical outcome. Distinct from a *skill-based* social check (persuade/intimidate/deceive) —
those still roll dice via `resolve_opposed_action` and narrate in third person as the omniscient
GM; free-form talking never rolls anything and speaks as the addressed entity.
`Intent_Classification.py`'s `detect_dialogue_intent` recognizes `DIALOGUE_KEYWORDS` phrases (`"talk to"`/`"ask"`/
`"tell"`/`"greet"`/...), checked after item-interaction detection has had its shot (so
`"give the sword to Anne"` is never swallowed as dialogue) and before skill matching. Once
detected, `IntentClassifier.classify` also calls the matcher's own `classify_sentiment(processed)`
(see "Dialogue sentiment" below) and publishes `dialogue_detected {input, score, sentiment}` with
no further resolution.

`DMCore._on_dialogue_detected` delegates to `DM_Dialogue.py`'s `DialogueMixin`:
`_resolve_dialogue_target` searches the input for any present entity's name (whole-word,
excluding the player), falling back to `_get_target_name()`'s default scene target if none is
named. `_resolve_dialogue` gates on the target being present/alive (`reason: "not_present"`)
and not an inanimate `"object"` (`reason: "cant_talk"`) — but deliberately **not** on hostility:
addressing a hostile entity is allowed (shouting mid-fight), and the model is free to read that
as hostile/dismissive in character rather than being denied outright. A found target's
attitude (all three axes) is nudged by the classified sentiments (`nudge_attitude`, see below)
before `persona`/`attitude` are attached for `LLMCore` to speak from — so the same turn's own
reply already reflects it.
Publishes `dialogue_resolved {target, input, found, present_entities, persona?, attitude?,
reason?}` — no `_publish_party_status()`, since dialogue never changes HP/equipment/inventory/
conditions (attitude drift isn't surfaced on the Party tab either, so this still holds).

**Dialogue sentiment.** The tone of what the player says nudges the addressed entity's own
attitude toward them — all three axes at once, each classified independently and independently
scored. Classified locally (`NLP_Core.py`'s `SentenceTransformerMatcher.classify_sentiment`/
`classify_threat`/`classify_familiarity`, one call per axis, all backed by the same separate NLI
(natural-language-inference) model (`NLI_MODEL_NAME`, `facebook/bart-large-mnli`) rather than
this class's own embedding model, a lexicon-based analyzer, or a purpose-trained sentiment
classification head: reading tone/threat/closeness out of an utterance needs broad,
compositional coverage across however a player might phrase something (ex: "get out of my
sight" — clearly hostile, but with no single word a dictionary lookup would flag), which only a
model built for real language understanding reliably provides. Each is run via Hugging Face's
`"zero-shot-classification"` pipeline: entailment is scored between the input and each axis's own
three candidate labels (as a hypothesis built from that axis's own hypothesis template),
normalized to a softmax over the three mutually-exclusive labels per axis. `classify_sentiment`
uses `SENTIMENT_CANDIDATE_LABELS`/`SENTIMENT_HYPOTHESIS_TEMPLATE`;
`classify_threat`/`classify_familiarity` share one `DIALOGUE_HYPOTHESIS_TEMPLATE` with their own
`THREAT_CANDIDATE_LABELS`/`FAMILIARITY_CANDIDATE_LABELS`. None of these are the library's own
bare defaults (`["negative", "neutral", "positive"]` + `"This example is {}."`) — the bare
defaults misread plain informational dialogue (ex: "do you know where the blacksmith is") as
negative/positive at `sentiment_confidence_threshold`'s own floor; the richer per-label phrasing
(ex: `"negative in tone"`/`"neutral or informational"`/`"positive in tone"` for sentiment) plus a
dialogue-framed hypothesis template were tuned against held-out sets spanning hostile/warm/
informational/sarcastic/valence-crossed lines and resolved this without needing to raise the
confidence threshold at all — `threat`/`familiarity` were validated less exhaustively than
disposition (a smaller, though still adversarial, test battery), which is worth keeping in mind
if either axis's real-play behavior looks off. Each `classify_*` method returns `(label, score)`
— normalized back to plain `"negative"`/`"positive"`, and the winning label's own entailment
probability — gated at the shared `sentiment_confidence_threshold` (0.5, "meaningfully more
confident than the ~0.33 a 3-way coin-flip would give") and short-circuited to `(None, score)`
whenever the model's own winning label is the neutral one, covering purely informational
dialogue as well as genuinely neutral phrasing. Still local inference — no network call —
deliberately not an LLM call: dialogue is the single most frequent player action, so adding LLM
latency to every turn was rejected in favor of a fast, local classifier (in practice, ~0.2-0.5s
per axis on CPU — roughly 3x that per dialogue turn now that three axes are classified instead
of one, still well within budget). `DM_Social.py`'s `nudge_attitude(entity_name, toward_name,
sentiments)` takes `sentiments`, a `{axis_name: (label, score)}` dict (an axis missing from the
dict, or with a falsy label/score, contributes 0), and applies a capped drift into
`entity["attitude_deltas"][toward_name]` across all three axes at once whose *magnitude* on each
axis is that axis's own `score` — the classifier's own confidence, already 0..1 — times
`SENTIMENT_INTENSITY_SCALE` (currently `1`, i.e. unscaled; a single tunable knob shared across all
three axes rather than a hand-tuned delta table), not a flat per-sentiment amount: a line the
classifier read as more intensely negative/positive moves that axis further than a mildly-worded
one. Clamped to `±TALK_ATTITUDE_DRIFT_CAP` (40) per axis — a cap on *accumulated drift*, not on
the resolved value, so sustained same-direction talk can still push a base value already close to
`is_hostile`'s `-100` disposition threshold across it (an intentional emergent outcome: insult
someone long enough and they turn on you). `get_attitude` adds `attitude_deltas` elementwise on
top of whichever name/supertype/default array it resolves, so `is_hostile`/`describe_attitude`/
the GUI all see the drifted value transparently, with no other call site changes. An entity with
no `[entity.attitudes]` table at all (ex: `arena.toml`'s wolf) stays hostile unconditionally
regardless of drift, since `is_hostile` short-circuits on the table's absence before ever reading
a disposition value. `attitude_deltas` is genuinely dynamic runtime state, so it round-trips
through save/load in the ordinary per-instance diff (`DM_Persistence.py`) for *every* entity, not
just generated/ad-hoc ones.

**Language barriers.** Every entity's own `languages` list (an entity field,
`entity_schema.toml`, absent entirely defaulting to `["common"]` — same as every entity shipped
today) is what a `_resolve_dialogue` addressee actually understands. `DM_Dialogue.py`'s
`_detect_language_barrier(target_name)` compares `set(player_languages)` against
`target_name`'s own list; any overlap at all resolves as ordinary dialogue. No overlap resolves
`{"found": True, "language_barrier": True, "target_language", "nonsense_phrase"}` instead of the
ordinary persona/attitude reply — the target is still present and willing to react, just unable
to understand the words, so `nudge_attitude` is deliberately skipped (a sentiment classifier
reads the *meaning* of an utterance, which the target never received). `target_language` is the
first of the target's own unshared languages; `nonsense_phrase` is looked up by matching that
name against `races.toml`'s own `[[race]].language` field (`None` if no race claims it, ex: a
scenario-authored language with no matching race entry). `LLMCore.generate_npc_dialogue`
branches on `language_barrier` to `_build_language_barrier_prompt`, instructing the model to
reply only with invented gibberish styled after `nonsense_phrase` (explicitly told not to reuse
it verbatim) rather than answering what was actually asked — persona/attitude still ground *tone*
(a hostile speaker's gibberish should still read as hostile), just never the content.

Each race in `races.toml` authors its own `language` (`human` → `"common"`, `elf` → `"elvish"`,
`dwarf` → `"dwarvish"`, `half-orc` → `"orcish"`, `halfling` → `"halfling"`) plus a
`nonsense_phrase` example of what it sounds like (human has none — every shipped entity already
defaults to knowing `"common"`, so a human-to-human barrier never arises with today's data).
`DM_CharacterCreation.py`'s `apply_character_creation` appends the chosen race's own `language`
onto the player template's existing `languages` list (deduped) alongside the point-buy skill
override, so an elf player knows `["common", "elvish"]` while a human re-adding `"common"` is a
no-op. This is opt-in for scenario/entity authors: nothing changes for existing data until an
NPC's own `languages` list is deliberately narrowed (ex: `["elvish"]` alone, no `"common"`) or a
player picks a race whose language that NPC doesn't share either.

**Room-level presence.** Every DM-published narration event carries `present_entities`: a
snapshot of `self.scenario_entities` at publish time. `LLMCore` tags each `context_window`
entry with this snapshot and `generate_npc_dialogue` uses `_filter_present_history(target)` to
ground a specific NPC's reply only in what that NPC has witnessed, rather than the DM's own
always-full, omniscient window (which stays untouched — the player's point of view is
deliberately still everything). An entity instanced mid-dungeon, or left behind in a previous
room, simply has no access to entries tagged before/without it. The exchange itself is still
appended to the *shared* `context_window`, so it becomes part of what everyone present has now
witnessed — letting a second NPC later recall what was just said to the first.

## ADaM (out-of-character help)

`"ADaM"` (Artificial Dungeon and Master) is a fourth diceless channel — a reserved,
always-available, explicitly out-of-character persona the player can address for guidance
(skills/abilities, scene re-description, exits, command guidance), never an in-fiction one.
Unlike Dialogue, there's no addressee to resolve and no way to deny it — it always resolves, and
is unconditionally free: it never joins the shared multi-action turn economy.

`Intent_Classification.py`'s `ADAM_NAME_PATTERN` (`\badam\b`, case-insensitive) is checked as its own
whole-input, pre-clause-split reserved word — right after save/load detection, ahead of both
inter-room direction detection and the item-interaction pass — so it reaches the help channel
rather than being swallowed by `DIALOGUE_KEYWORDS` or an item verb first. Reserves the literal
name "adam" the same way `PLAYER_PLACEHOLDER` reserves "player" — a known, accepted tradeoff.
Publishes `help_detected {input}`.

`DM_Help.py`'s `HelpMixin._on_help_detected` gathers a fresh snapshot of live state every time
it fires (no memory of past exchanges — see below) and publishes `help_resolved`: the player's
skills/abilities/equipped/inventory, the current scene's name/description, the present-character
roster, and `exits` (the current room's own exits, if a room is active, followed by the current
location's own `[[location.exit]]` list, resolved to friendly destination names — `[]` if
neither exists, ex: a location with no rooms and no declared exits of its own). No
`_publish_party_status()` for the ordinary informational path — except when a removal, creature
conjuring, or edit actually goes through (see "Ad hoc entity creation and removal"), the three
exceptions on purpose.

**Deliberately excluded from `context_window`.** Every other narration trigger appends both
prompt and reply to `LLMCore`'s shared rolling window. ADaM's own `generate_adam_response`/
`_queue_adam_response` instead send a standalone `[system, user]` request to
`_fetch_and_publish(..., store_in_context=False)`, appending neither. Two reasons: (1) tone — a
meta/OOC exchange left in the shared window risks the GM later parroting mechanical facts
in-fiction; (2) budget — ADaM's dense payloads (full skill lists, exits) would otherwise crowd
the finite 100-message window. The cost: ADaM has no memory of its own past replies — each
invocation is a fresh request built from whatever `DM_Help.py` gathers off live state.

## Ad hoc entity creation and removal

ADaM's second capability: improvising the world itself, not just explaining it.
`AdHoc_Generation.py` is the pure LLM-calling half —
`generate_ad_hoc_item`/`generate_ad_hoc_creature`/`decide_entity_removal`/`decide_entity_edit`,
each an OpenAI-style tool call (`LLM_Client.py`, synchronous, raises on failure) offering a
primary function plus a shared `decline` escape hatch, `tool_choice="auto"`. Every function
defaults to declining on any failure — never fabricating an item/creature/removal/edit when the
LLM is unreachable — on a tighter 8s timeout than NPC generation's 20s (this can fire far more
often mid-session). `DM_Improvisation.py`'s `ImprovisationMixin` is the DMCore-touching glue.

**Two risk tiers.** (1) **Automatic fallback**, no ADaM address needed (low risk): plain item
creation, extended to a container/trap (a `generate_ad_hoc_item` subtype carrying its own
minimal `[entity.test]`) and to ambient scenery detail (`describe_scenery`, no entity created).
(2) **ADaM-gated**, behind explicitly naming ADaM (higher risk — can affect combat balance or
mutate any entity): removal (`removal_candidate`), creature conjuring (`creature_candidate`),
editing (`edit_candidate`) — each gated by its own cheap local keyword pre-check
(`Intent_Classification.py`'s `REMOVAL_KEYWORDS`/`CREATURE_KEYWORDS`/`EDIT_KEYWORDS`, attached to
`help_detected`'s payload only once `ADAM_NAME_PATTERN` matched), with the LLM's own
`tool_choice="auto"`/`decline` still the real arbiter.

**Creation.** `Intent_Classification.py` tracks any clause matching `IMPROVISABLE_INTENTS`
(`examine`/`take`/`give`/`equip`/`unequip`/`use`/`drop`/`trade`) whose `map_to_item` found
nothing. If the whole turn resolves to nothing at all, the first such candidate is published as
`improvisation_requested {intent, phrase, input}` instead of `action_not_understood`.
`ImprovisationMixin._on_improvisation_requested` calls `generate_ad_hoc_item`
(enum-constrained `subtype`/`equip_slot`/`lock_skill`/`disarm_skill`/`damage_tag`, built from
real in-use values for reliability with small local models). On decline/failure: publishes
`action_not_understood` — the same outcome as without this feature. On success: tags the entity
`ad_hoc = True` (drives its save/load treatment — see "Persistence"), stores it into
`self.entities`, publishes `item_catalog_updated`.

The same tool call offers `describe_scenery(description)` for ambient detail with no entity
created — a pure flavor beat published as `item_interaction_resolved {found: True,
description}`.

**Containers and traps.** `subtype = "container"`/`"trap"` carry a minimal LLM-authorable
version of `items.toml`'s hand-authored `chest`/`dart trap` shape: a locked container gets
`active_conditions = {"closed", "locked"}` plus a `[entity.test]` whose `pass` dismisses it and
`fail` applies `"jammed"`; a trap gets `active_conditions = {"armed"}` plus a `[entity.test]`
whose `fail` deals real damage — skipping `[entity.notice]`/`"hidden"` since it was only
conjured because the player already found it. `_resolve_test_skill` falls back to
`lock_skill`/`disarm_skill` → `"finesse"` → dropping the test block entirely, so a conjured
container/trap can never land permanently unopenable/undisarmable.

Unlike a plain item, a container/trap becomes a live, targetable scene participant
(`SCENE_PLACED_SUBTYPES`) — inserted at the *front* of `self.scenario_entities` (so
`_get_target_name()` picks it immediately) and claiming `self.current_target` via
`_claim_current_target_if_free` (needed since scene-level `[entity.test]` checks resolve against
`current_target`, not `_get_target_name()`), but never stealing the target from a fight already
engaged.

An item can opt into `"use"` (`usable = true`, `healing`/`poison` `{dice, pips}`) via
`is_healing`/`is_poisonous` flags — the tool schema explicitly tells the model a plausible
fraction of consumables should come back poisonous, not a guaranteed free heal.

**Placement.** The model's `location` choice (`"ground"`/`"inventory"`) is narrative judgment,
but which one actually works depends on the triggering intent. Every placement that lands in an
inventory goes through `DM_Inventory.py`'s `place_new_item` (see "Inventory and currency") and
then re-dispatches through the ordinary, unchanged `_on_item_interaction_detected` uniformly —
`PLAYER_CENTRIC_INTENTS` (`give`/`equip`/`unequip`/`use`/`drop`) always land in the player's own
inventory this way regardless of the model's own `location` choice; `GROUND_AWARE_INTENTS`
(`examine`/`take`) honor it — `"ground"` appends to `_current_ground_items()` instead and
re-dispatches the same way, `"inventory"` places into the player's own inventory exactly like a
player-centric intent does. `TARGET_CENTRIC_INTENTS` (`trade` alone) ignores `location` and
stocks the entity into the current scene target's own inventory via the same `place_new_item`
call — this is what lets a shopkeeper sell "most general goods" without every item being
pre-authored (`Rules/Fantasy/scenarios/shop.toml`); short-circuits to a decline if there's no
current scene target to sell from. `_on_item_interaction_detected` itself resolves "examine"/
"take" directly against the player whenever the item is already sitting in their own inventory
(checked ahead of the locked/closed-target gates, not just the source/destination resolution, so
an unrelated locked container elsewhere in the scene never blocks examining something the player
already possesses — see "Items and movement as intents") — which is what lets every one of these
placements redispatch through the same unchanged pipeline with no bespoke narration of its own.

**Removal.** `DM_Help.py`'s `_on_help_detected` calls `_attempt_entity_removal` first when
flagged — builds the real universe of removable names (every scene/ground/inventory entity,
`player_name` excluded) as an enum constraint for `decide_entity_removal`, plus a narrower
`hostile_entities` subset (`is_hostile` + a live-HP check) passed alongside it — without this,
a real model complies with "get rid of that wolf, this fight is too hard" unconditionally,
turning a free out-of-character request into a dice-free win; `decide_entity_removal`'s own
prompt is what actually leans on this subset to decline. A real removal is
folded into `help_resolved` as `"removed"` and triggers `_publish_party_status()` — one of the
three exceptions to "ADaM never mutates state" (creature conjuring and editing, below, are the
other two). `remove_entity_from_scene(name)` strips `name` from
every entity/ground/room list and records it in `self.removed_entities` so it can never respawn
(`DM_Rules.py`'s `_instance_entities` checks this set). Deliberately does **not**
`del self.entities[name]` — left orphaned/unreferenced, same precedent as a fully-consumed item,
which is also what makes it self-clean out of future saves (collection is by *reachability*).
Hard guard: refuses if `name == player_name`. Removing a container also orphans its own
inventory contents — intended.

**Creature/NPC conjuring.** `_attempt_creature_conjuring` resolves `target_cr =
get_challenge_rating(player_name)` (a single-target framing, not real NPC generation's
party-pool math) and calls `generate_ad_hoc_creature`: one tool call constrained to 1-2 real
`npc_keywords.toml` archetypes, a `disposition` enum (`hostile`/`wary`/`neutral`/`friendly` —
`hostile` maps to exactly `-100`, the real combat threshold), and a `power` enum
(`weak`/`moderate`/`strong`, a multiplier on `target_cr`). No second LLM round trip for stats —
the keyword choice is reused directly with `NPC_Generation.py`'s deterministic
`fit_skills_to_cr`. **Only `"hostile"`** gets an inline attack ability plus a
flee-under-0.4-hp_per_remain-then-attack behavior pair, mirroring `arena.toml`'s wolf; a
wary/neutral/friendly conjured NPC is dialogue-only. Placement mutates
`self.entities`/`self.scenario_entities` directly (since `_instance_entities` only runs at load
time) — `entity_id`/`band` (player's current band), `ad_hoc = True`. A hostile creature also
claims `self.current_target` unless a fight is already engaged, so conjuring mid-combat never
silently retargets the player or flips the round-vs-action narration choice. Folded into
`help_resolved` as `"created_creature"` and triggers `_publish_party_status()`, same as removal
and editing.

**Entity editing.** `_attempt_entity_edit` builds the same editable-name universe as removal and
asks `decide_entity_edit` to pick one and change it, or decline. Scope is narrow: a full
`new_description` rewrite plus `apply_condition`/`dismiss_condition` — never raw mechanical
fields like `skills`/`damage_value`. A description change tags `entity["edited"] = True` (see
"Persistence") and republishes `item_catalog_updated`; folded into `help_resolved` as `"edited"`
and triggers `_publish_party_status()`.

**Persistence.** An ad hoc entity has no static TOML template to re-derive from on reload, so
`DM_Persistence.py`'s `_collect_ad_hoc_entities` saves every *reachable* one's complete dict
under `"ad_hoc_entities"` — reachable meaning a live `self.scenario_entities` participant (a
conjured creature/container/trap, or a temporary summon — see "Summoning"), present in some
ground list, or sitting in some known instance's own inventory/equipped mapping.
`"recent_damage_tags"` (`calculate_damage`, `DM_Combat.py`) is stripped from the copied dict
first — a plain Python `set`, not JSON-serializable, and deliberately ephemeral regardless (see
"Status and conditions"'s own per-round upkeep note). `load_game` restores each entity with a
full dict replacement alongside `"ground"`, republishes `item_catalog_updated` once, then
re-appends every name the save's own `"scenario_entities"` remembered that a fresh
scenario/room re-instancing didn't already reproduce (exactly the ad hoc ones — a hand-authored
entity's own presence already re-derives correctly from static data) back onto the live
`self.scenario_entities`, guarded on the name actually resolving to something in `self.entities`
so a stale reference is dropped rather than left dangling. `"removed_entities"` round-trips too,
restored *before* scenario/room loading runs so a removed entity can't respawn. An *edited*
hand-authored entity still has a real template, so only `entity["edited"] = True` is needed to
make `save_game` include `"description"` in its diff and `load_game` restore it.

**NLPCore catch-up.** `SentenceTransformerMatcher`'s `item_embeddings`/`item_indices` are
otherwise only ever built once, from `"rules_loaded"` — an ad hoc entity created/restored after
that would be permanently unreachable to `map_to_item` without `item_catalog_updated`: `NLPCore`
forwards each entry in that event's payload to `IntentClassifier.register_item`, which delegates
to the matcher's own `register_item`, encoding the `{name, description}` pair and appending onto
the existing tensor/index list (`torch.cat`) rather than rebuilding from scratch. Every
capability above that introduces a name or description change republishes this event.

## Summoning

A spell/technique/innate ability's own `summon = {"name"|"template", "duration"}` field
(`entity_schema.toml`) opts a successful cast into conjuring a temporary ally, alongside (or
instead of) dealing damage — `spells.toml`'s `summon spectral wolf` (`creatures.toml`'s own
`spectral wolf`, on gladstone's `abilities` list) is the shipped example.
`DM_Core.py`'s `_apply_summon_if_hit`, called from `_on_turn_detected` right after
`_apply_damage_if_hit`, fires whenever the turn's own `named_ability` carries a `summon` table
and the roll succeeded — regardless of `target_name`/`via_test`, since a summon isn't "against"
anyone the way damage is: casting with no `current_target` at all resolves as an ordinary flat,
automatic `resolve_action` (difficulty 0); casting against a live hostile `current_target`
resolves as an ordinary opposed roll instead (the caster's own skill vs. whatever the target's
best matching `opposes` skill is — no bespoke "resist a summon" mechanic, just the same opposed
check any other ability already uses).

Unlike `DM_Improvisation.py`'s own ad hoc creature conjuring, a summon never invents anything —
`DM_Summoning.py`'s `_summon_creature` always instances a real, hand-authored `[[entity]]`/
`[[entity_template]]`, via the exact same `_instance_entities` primitive (`DM_Rules.py`) every
scenario/room's own static `entities` list is built on (reusing `_resolve_one_encounter`'s own
precedent, `DM_Encounters.py` — not `DM_Improvisation.py`'s bespoke `_unique_entity_key`/
`_place_new_entity` pairing, which exists specifically for an LLM-invented name with nothing
real to disambiguate against `self.entity_occurrence_counts` the ordinary way). The new instance
is placed at the caster's own current band, tagged `ad_hoc = True`, given a `summon_expires_in`
(whole combat rounds remaining), and appended to `self.scenario_entities` — but never claims
`self.current_target` (an ally never should, same as `_claim_current_target_if_free`'s own
hostile-only rule). Whether the summon actually fights *for* the caster is entirely down to its
own `[entity.attitudes]`/`[[entity.behavior]]` data, same as any other entity: `spectral wolf`'s
own flat-positive `default` attitude is what keeps it from reading as hostile-by-default (no
`[entity.attitudes]` table at all defaults to hostile — see "Combat"), and its own
`[[entity.behavior]]` entry is what makes it actually attack `self.current_target` each round.

`run_round_upkeep` (`DM_Status.py`) also counts down every living scene entity's own
`summon_expires_in` each combat round (`_expire_summon_if_due`, `DM_Summoning.py`), sharing the
same per-round cadence condition-driven upkeep already uses rather than a second pass over
`scenario_entities` — iterated as a snapshot (`list(self.scenario_entities)`), since an expiring
summon removes itself from that same live list mid-loop. A summon reaching 0 is removed from the
scene entirely via `remove_entity_from_scene` (`DM_Improvisation.py`), not a condition dismissed
— the creature itself is gone, not just one of its traits. There's no engine concept of a "round"
outside combat, so a summon cast with no fight underway doesn't start counting down until an
actual combat round happens.

**Save/load.** A summoned creature (`ad_hoc = True`) round-trips through save/load like any
other ad hoc entity — `_collect_ad_hoc_entities` treats live `scenario_entities` membership as
its own reachability source, and `load_game` re-appends a restored ad hoc entity's name back
onto `self.scenario_entities` (see "Saving and loading"'s own "Persistence" note), including
its own `summon_expires_in`, since the whole entity dict round-trips, not a whitelisted diff —
the same mechanism covers `DM_Improvisation.py`'s own ADaM-conjured creatures/containers/traps
too, not just summons.

**`_apply_damage_if_hit`'s own gating.** A resolved ability only attaches a `"damage"` entry to
the result if it actually carries a `damage_value` field (`"damage_value" in ability`, not just
a truthy `ability`) — a named ability with none (a summon, or any non-damaging spell) never
rolls through `calculate_damage`'s own `{"dice": 0, "pips": 0, "bonus": 0}` default and picks up
a spurious `"damage": {"net_damage": 0, ...}` entry just because it resolved against a target
that was present.

## Narration

`LLMCore` subscribes to narration-relevant events, sharing outcome-text building
(`_describe_outcome` — also the one place that turns a successful summon's own `"summoned"` key
into an actual narrated line, "Summoning" above) and background-fetch plumbing
(`_queue_narration`/`_fetch_and_publish`):
- `scenario_loaded` → `generate_scene_intro` — once, from `DMCore.__init__`.
- `round_resolved` → `generate_round_response` — combat, once per round.
- `action_resolved` → `generate_response` — non-combat, once per skill use.
- `action_not_understood` → `generate_clarification_response` — acknowledges input that didn't
  resolve to any action.
- `item_interaction_resolved` → `generate_item_interaction_response` — covers examine/take/give/
  trade/open/close/use/equip/unequip/drop, room transitions, and location-to-location travel.
- `dialogue_resolved` → `generate_npc_dialogue` — a found target routes through
  `_queue_dialogue`; a denied one falls back to an ordinary `_queue_narration` explanation.
- `game_load_failed` → `generate_load_failed_response`.
- `help_resolved` → `generate_adam_response` — routes through `_queue_adam_response`, the one
  trigger here that never touches `context_window` at all.
- `encounter_triggered` → `generate_encounter_response` — a location/room's own random
  encounter roll (see "Random encounters"), the one trigger here that's never a response to
  something the player did.

The scenario/room setting and character roster are re-injected into the system message on every
request, so narration stays grounded even after the intro scrolls out of the rolling 100-message
`context_window`. `generate_npc_dialogue`'s own system message (built by
`_build_dialogue_system_message`) is different in kind, not just content — it speaks as the
addressed entity, grounded in `persona`/`attitude` plus that entity's own presence-filtered
history, never the standing GM framing.

Every `_queue_narration`/`_queue_dialogue` call's background fetch also publishes
`llm_debug_updated {"query", "response"}` alongside `llm_response_ready` — consumed only by
`GUICore`'s Debug tab, never stored in `context_window` itself.

## Saving and loading

Three sibling JSON files per slot — `Saves/<slot>/dm_state.json`, `llm_state.json`,
`gui_state.json` — written/read independently by `DMCore`, `LLMCore`, and `GUICore`. `EventBus`
has no request/response mechanism, so each core owns and persists its own slice.

**Trigger:** `save_requested`/`load_requested {"slot": slot_name}`, published by
`Intent_Classification.py`'s `detect_save_load_intent` (via `IntentClassifier.classify`), by
`GUICore`'s File → Save... / Character → Load... popups
(see "Booting the game" for the cold-start case), or by `Textual_Core`'s Save/Load buttons.

`DMCore.save_game` writes a diff from a fresh instantiation: `setting`, `scenario_key`,
`player_name`, `round_number`, `current_location_key`, `current_room_key`, `location_runtime`
(every visited location's own `{persistent_names, visited_rooms}` cache — see "Scenarios,
locations, and rooms"), `scenario_entities`, `ground`, and per-instance `{hp, active_conditions,
currency, inventory, equipped, band}`. `load_game` re-runs `load_rules()`, then re-instances
every location the save file's own `location_runtime` says was ever visited (each location's own
`entities` once, each of its visited rooms' own entities once — mirroring exactly how a single
room's own instance list was already re-derived from the room's static entities rather than
trusted directly, so `_instance_entities`' own idempotent occurrence-counting reproduces the
identical instance names every time) *before* `load_scenario()`/`_enter_location` ever look at
`self.location_runtime`, so their own "already cached" check finds it and reuses it. Then jumps
to the saved `current_location_key`/`current_room_key` if they differ from the scenario's own
`start_location`. Finally overlays each saved instance's mutable fields; a saved instance with
no post-reload match is skipped. Publishes `game_loaded` on success (not `scenario_loaded`,
which would re-narrate an opening scene) or `game_load_failed {"slot", "reason"}` on failure,
then re-publishes `party_status_changed`.

`ground` (items dropped since the scenario started) round-trips too, keyed per location
(`{location_key: {"ground": [...], "rooms": {room_key: [...]}}}`), mirroring the same
location/room branch `_current_ground_items` (`DM_Inventory.py`) already makes.

`LLMCore.save_game`/`load_game` persist/restore `context_window` plus scenario name/description/
characters; loading is silent. `GUICore.save_game`/`load_game` persist/restore the Notes tab's
free text, same way.

Slot names are run through `os.path.basename` before use, so a slot can't escape `Saves/`.

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
- **Conditions** (`active_conditions`, `apply_condition`/`dismiss_condition`) are dynamic —
  gained/lost during play via triggers or tests. Use a condition for something that can plausibly
  change mid-scene; use a tag for something permanent to what the entity is.

`abilities` is a flat list, each entry either a plain string naming a shared catalog entity
(`spells.toml`/`techniques.toml`) or an inline table for a one-off innate ability.
`techniques.toml`'s `cleave` exercises a multi-skill `skill = [...]` list and weapon-scaled
damage (`"user.weapon.dice"`/`"user.weapon.pips"`); see `ability_matches_skill`,
`resolve_weapon_reference`, `resolve_damage_value` in `DM_Combat.py`. Naming a technique/spell
directly in input can resolve it via `map_to_action` before a bare skill would.

## Data/TOML conventions

- `Rules/Fantasy/reference/entity_schema.toml` catalogs every field the engine reads off an
  entity; the sibling `template_schema.toml` does the same for an `[[entity_template]]`'s own
  fields. Reference/documentation only, never loaded as game data (`load_rules` only
  `os.listdir()`s the top level of `Rules/Fantasy/`, one directory shallower).
- `load_rules` special-cases only `skill`, `entity`, and `entity_template` top-level keys;
  everything else in any flat `Rules/Fantasy/*.toml` file lands generically in `self.rules[key]`.
- `[entity.attitudes]` is `{default, name, supertype}`; `name`/`supertype` are TOML
  arrays-of-one-key-tables — `get_attitude` loops over the list checking `if toward_name in
  override`.
- `damage_value = {dice, pips, bonus}` — `bonus` is a flat number or `"user.<rule_name>"`,
  resolved via `resolve_bonus`. String `dice`/`pips` are not resolved and degrade to 0.
- `load_rules`'s per-file exception handling means a malformed TOML file fails quietly — a parse
  error loads that file with less data than expected, not a crash.

## LLM integration

Endpoint is Ollama's OpenAI-compatible API (`http://127.0.0.1:11434/v1/chat/completions`,
`ollama serve`'s default). Ollama can have several models pulled at once, so
every request payload carries an explicit `"model"` field — `LLM_Client.py`'s own
`DEFAULT_MODEL` ("gemma4") and `LLM_Core.py`'s own `self.model`, each independently, mirroring
the same intentional non-sharing `_save_slot_dir`'s own module note documents. `/v1/models`
lists every locally pulled model (Ollama's native `/api/tags` is the same catalog, non-OpenAI-
shaped); a chat completion against a model name that hasn't been pulled 404s rather than
falling back to whatever's loaded.

`Ollama_Launcher.py`'s `ensure_ollama_running` is a best-effort local server bootstrap, called
once from `LLDM.py`'s own `main()` on a background daemon thread, started right after `GUICore`
is constructed (before `NLPCore`/`LLMCore`/`DMCore`) — specifically so its window already exists
for the thread's own log callback to report progress into (see "Booting the game"): a fast no-op
if something's already listening at `127.0.0.1:11434`. Otherwise it resolves an `ollama.exe` to
run —
preferring a real system install (`shutil.which("ollama")`) over a vendored one, so installing
Ollama for real later transparently takes over from a downloaded copy — and if neither exists
at all, downloads Ollama's own official portable Windows build straight from its GitHub
releases (`ollama-windows-amd64.zip`, resolved via the stable `.../releases/latest/download/...`
URL, so this always tracks whatever's currently latest) into `vendor/ollama/`, a gitignored,
per-machine directory exactly like `Saves/` — never committed, never shipped in the repo. The
download is verified against Ollama's own published `sha256sum.txt` before extracting;
`os.walk`-based `_find_executable` locates `ollama.exe` inside the extracted tree without
assuming a particular zip layout. Windows-only by design (`ollama.exe`, the win_amd64 asset,
`CREATE_NO_WINDOW`) — matches this project's own current platform (win32).

Once an executable is resolved, `ensure_ollama_running` spawns `ollama serve` and returns
immediately — deliberately not blocking on the new *process* actually becoming ready, since
`NLPCore`'s own `sentence-transformers` model load (the very next boot step, ~15-20s) already
gives a freshly-spawned Ollama plenty of time to come up in the background. The one-time
*install* step, by contrast, blocks whatever called `ensure_ollama_running` — there's no "just
try again later" fallback for a binary that doesn't exist on disk yet, and this only ever runs
once per machine (every later launch finds the already-extracted executable first). Because a
fresh machine has to download the ~1.5GB Ollama binary plus, by default, a ~9.6GB model pull
(`gemma4`'s own `:latest`/E4B tag) before this call would otherwise return, `main()` runs the
entire `ensure_ollama_running` call on a background daemon thread rather than blocking its own
startup on it — see "Booting the game" for why nothing in the app actually needs it to have
finished before a game can start. Every failure mode (no network, a failed checksum, an
unwritable `vendor/`, the process failing to launch) just logs and lets the app continue exactly
as it already would with no Ollama available at all — the same best-effort posture every other
LLM integration point in this codebase already follows. `main()` registers an `atexit` cleanup
that terminates the spawned process, but only the one this call itself started (checked via a
`nonlocal` variable the background thread assigns once `ensure_ollama_running` returns — `None`
until then, so a shutdown racing the bootstrap simply has nothing yet to clean up) — a
pre-existing Ollama instance (started by hand, or by another app) is never touched.

A running server alone doesn't mean narration will work — a chat completion against a model
name that hasn't been pulled 404s (see this section's own opening paragraph), so
`ensure_ollama_running` also calls `_ensure_model_pulled` right after resolving/spawning a
server, whichever branch reached that point. Unlike the server spawn itself, this step *does*
wait (up to `ready_timeout`, default 15s) for the server to actually answer — there's no way to
know what's pulled, let alone pull something missing, without talking to it — then checks
`GET /api/tags` (Ollama's own native listing, not the OpenAI-compat one) and, if `model` (default
`DEFAULT_MODEL`, `"gemma4"` — kept in sync by hand with `LLM_Client.py`/`LLM_Core.py`'s own same-
named defaults, the same duplicated-not-shared convention as everything else in this module)
isn't listed, streams `POST /api/pull` and relays Ollama's own NDJSON progress lines through
`log`, throttled to roughly every 10% per phase so it reads as a progress bar rather than a
flood. `_model_already_pulled` treats a bare request name (`"gemma4"`) as matching its own
implicit `":latest"` tag, since `/api/tags` always reports one even when none was given at pull
time. Every failure here (server never comes up, network error mid-pull, an unknown model name)
is the same best-effort "log and give up" as everything else in this module — the app's own
existing "Could not connect to the local LLM"/404 handling is still the real fallback if a model
genuinely never gets pulled.

`ensure_ollama_running`'s own `log` callback, as wired by `LLDM.py`'s `main()`, reports status
two ways: `event_bus.publish("log_info", ...)` (`Logger.py`'s ordinary console mirror) and
`GUICore.display_system_status` (a `"[System] ..."` line in the History pane, the same prefix
convention `display_game_saved`/`display_game_loaded`/`display_game_load_failed` already use).
`GUICore.display_system_status` is why it's constructed first among the three event-subscribing cores in
`main()` (ahead of `NLPCore`/`LLMCore`) rather than last — the background bootstrap thread's own
closure over `gui_core` needs it to already exist the moment the thread starts, and starting the
thread this early lets the window reach `mainloop()` (see `gui_core.start()`) without
waiting on `NLPCore`'s own ~15-20s model load either. The bootstrap thread reports progress
while `mainloop()` is already running, so the running loop picks up each history-pane update on
its own, the same way `LLM_Core.py`'s own background narration fetches touch `GUICore` from a
foreign thread. This is safe because none of `GUICore`'s own subscriptions
(`llm_response_ready`, `rules_loaded`, ...) can fire this early regardless of thread timing —
nothing publishes them until `DMCore` exists, and `DMCore` isn't constructed until well after
this point (see "Booting the game"). One consequence worth naming: the player can open
Character → Create... and start a scenario while the Ollama bootstrap is still mid-download —
narration during that window degrades to "Could not connect to the local LLM"
(`LLM_Core.py`'s own existing best-effort path) until the bootstrap catches up.

## RAG / sourcebook grounding

`LLM_Rag.py`'s `RagIndex` indexes every `*.pdf` under `Settings/Fantasy/` (a gitignored
directory), building its index on a daemon background thread; `query()` returns `[]` until
`self.ready` is `True`. Chunks/embeddings are cached to
`Settings/Fantasy/.rag_cache/<hash>.{chunks.json,embeddings.npy}`, keyed by a hash of every
source PDF's path/size/mtime.

Chunking is sentence-bounded (`_chunk_page_text`, capped at `MAX_CHUNK_WORDS`=180, dropping
fragments under `MIN_CHUNK_WORDS`=40). Retrieval is per-request, appended to that request's
system message only — never stored in `context_window`. `perform_rag` returns no chunks below
`confidence_threshold` (`0.3`).

The RAG query is the player's own raw input, not the full instruction-padded narration prompt —
embedding the padded prompt dilutes similarity enough to miss lore a bare-input query would find.
`generate_scene_intro` passes the scenario name+description instead (no player input exists
yet); `generate_load_failed_response` falls back to its own full prompt.

`vectorize_pdf.py` is a standalone CLI that builds this same cache ahead of time: `python
vectorize_pdf.py [pdf_or_dir] [--query "..."]`, defaulting to `Settings/Fantasy/`. Reuses
`RagIndex` directly via `RagIndex.wait_until_ready()`.

## Textual mirror (headless testing)

`Textual_Core.py` subscribes to the same events `GUI_Core` displays and adds its own `Input`
widget publishing `user_input_submitted`, so the app can be driven and asserted on headlessly
(`app.run_test()`/`Pilot`) without Tkinter or a display.

Practical constraints when touching this file:
1. Don't name an attribute `self._ready` — Textual's `App` reserves that name internally.
2. Pre-mount events (`DMCore` publishes `rules_loaded` synchronously during `__init__`, which
   can precede `compose()`) are buffered and flushed in `on_mount`.
3. `RichLog.lines` only reflects content once its tab is active — activate it
   (`tabbed_content.active = "tab_id"`, then `await pilot.pause()`) before reading a background
   tab.
4. Writes can arrive from a foreign thread (`LLMCore`'s background fetch). `call_safely` wraps
   everything through `self.call_from_thread`, falling back to a direct call.
5. Pilot has no `.type()` in the installed Textual version (8.2.8) — build a key list (`["space"
   if c == " " else c for c in text]`) and pass it to `pilot.press(*keys)`.
6. Joining a background thread from an `async def` must go through `await
   asyncio.to_thread(thread.join)`, not a bare `t.join()`, or the event loop deadlocks.

## Testing

- **`test_unit.py`** — offline `unittest.TestCase` classes: one representative test per
  genuinely distinct mechanism/branch, not one per edge case or flavor variant of an
  already-covered code path. `TestGameBoot` and `TestNlpConfidenceThreshold` load the real
  `sentence-transformers` model via `setUpClass`, narrowed to what actually needs it
  (confidence-threshold/keyword-fallback scoring, real embedding registration).
  `TestIntentClassification` covers `Intent_Classification.py`'s gate/precedence order instead
  — `IntentClassifier` exercised directly against `FakeMatcher` (a canned `IntentMatcher`
  double defined alongside it), no model load, EventBus, or DMCore needed. Most other classes
  share fixture setup via `DMTestCase` (`scenario_name` class attribute, plus
  `_capture`/`_capture_any` helpers) and `LLMTestCase`.
- **`test_integration.py`** — every test needing a real, running Ollama, gated on
  `_ollama_reachable()` so they skip together when nothing's listening on `127.0.0.1:11434`.
  `_LivePipelineTestCase`'s own optional `character` class attribute is forwarded straight into
  `DMCore`'s `character` param. `TestNpcGenerationLive` is a plain `unittest.TestCase` (no
  NLPCore/LLMCore) since NPC generation runs synchronously during `DMCore`'s own construction —
  a real tool-calling round trip. The pure fitting math and DMCore-side wiring both live in
  `test_unit.py` instead (patching `NPC_Generation._real_call_chat_completion` with a
  deterministic fake), so most of NPC generation stays covered by the fast offline suite — only
  the "does the configured model actually return a valid tool call" question needs a live
  Ollama.

`python -m pytest -q` runs both files; `python -m pytest -q test_unit.py` runs the fast, offline
subset only.

## Extended goals

Not yet started, except where noted:
- Characters are language-dependent — an entity's own comprehension of the language it's
  addressed in should gate dialogue/narration, not just its attitude data. **Done for
  dialogue**: see "Dialogue"'s own "Language barriers" — `_detect_language_barrier` gates
  `_resolve_dialogue` on a shared `languages` entry, falling back to invented gibberish styled
  by `races.toml`'s own `nonsense_phrase` when nothing overlaps. Not extended to the omniscient
  third-person narrator voice, which by design still sees/relays everything regardless of what
  any one character understands (see "Room-level presence"'s own "the player's point of view is
  deliberately still everything").
- There's no mechanism yet for an *NPC's* own action (ex: an ally's combat hit) to sway anyone's
  attitude — only the player's own actions do (see "Action-driven attitude drift").
- Random encounters, enemy generator — procedurally populate a scene/room with creatures instead
  of every encounter being scenario-authored. **Partially started**: `[[location.encounter]]`
  (see "Random encounters") lets a location/room roll a weighted table of outcomes on entry —
  but the table itself, and every entity/template it can resolve to, is still hand-authored;
  there's no procedural encounter *design*, only randomized *selection* among authored options.
  "NPC generation" separately fits a `generate = true` template's *stats* to a target CR at
  instancing time, but the template itself (attitudes/behavior/abilities/equipment, and
  whether/where it appears at all) is still hand-authored too.
- Scenario, quest, NPC, item, and location generators — procedurally author the TOML data itself
  rather than every scenario/entity being hand-written. Same caveat as above.
- ADaM acting proactively, not just when addressed by name — today ADaM only reacts to an
  explicit "adam ..." turn; a fuller DM persona would also initiate content unprompted (a
  complication, a random encounter, a pacing beat). Deliberately not an always-eligible-to-act
  switch: an ambient "decide whether to act this turn" hook would run on every turn, and live
  probing found the currently-loaded model complies with an under-specified request readily
  unless explicitly told not to. If built, favor a narrow, rate-limited trigger fired only at
  specific, already-instrumented moments (room entry, N turns without incident) with its own
  decline-biased prompt. Not started.
- A 'dungeon master' persona the LLM can speak directly to the player as — distinct from both
  the omniscient third-person narrator voice and ADaM's own explicitly out-of-character one.
- Tools that the LLM may call to directly interact with the scene. **Partially started**: ADaM's
  own ad hoc generation (see "Ad hoc entity creation and removal") already gives the LLM real
  `tool_choice="auto"` function-calling access to create items/creatures and remove/edit
  entities — but only via `LLM_Client.py`'s narrow, single-purpose, DMCore-triggered calls
  (`generate_ad_hoc_item`/`generate_ad_hoc_creature`/`decide_entity_removal`/
  `decide_entity_edit`). `LLM_Core.py`'s own narrating GM voice — the one actually speaking to
  the player on every ordinary turn — never sends a `tools` payload at all and has no scene-
  mutation ability of its own; this goal is really about giving *that* voice general tool access,
  not another bespoke gated call.
