# LLDM

An autonomous dungeon master: the player types free-text actions, NLP maps them to a skill,
a simplified D6 (West End Games) engine rolls dice and resolves outcomes, and a local LLM
(currently Gemma via LM Studio at `http://127.0.0.1:1234`) narrates what happened. Skills,
entities, items, spells, rules, and scenarios are all data-driven via TOML, organized into
"settings" — self-contained sibling directories under `Rules/` (`Rules/Fantasy/`,
`Rules/Zombie/`), each independently scanned by `load_rules`. None of the engine itself is
fantasy-specific — `DMCore(event_bus, scenario_name, setting="Fantasy")`'s own `setting` param
picks which one to boot from (`Rules/<setting>/scenarios/<scenario_name>.toml` and every
sibling `Rules/<setting>/*.toml`), and it round-trips through a save file (`dm_state.json`'s
own `"setting"` key) so a resumed save reloads from the same setting it was saved under.
`Rules/Fantasy/` is the original, deep setting; `Rules/Zombie/` is a bare-bones second one (a
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
  is what used to be `NLPCore._on_user_input`, returning `(processed_text, events)` — a list of
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
- **`LLM_Core.py`** — posts to LM Studio's OpenAI-compatible `/v1/chat/completions` on a
  background thread, with a rolling 100-message context window. Subscribes to eight narration
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
round, not sequentially. `current_target` only advances (to the next living hostile entity, or
the first living non-player entity if none is hostile) once, at the end of the round, if it
died.

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
substrings never splits), after save/load and inter-room movement have had their whole-input
shot. Each clause is classified independently, in two passes:
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
fixed and varied entries freely (ex: `generated_stranger`'s `default` keeping trust/confidence
flat while disposition/intimacy vary). `hint`/`cr_multiplier`/`qualities` resolve before the LLM
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
matching the app's "LM Studio is best-effort, never blocks core gameplay" posture.

`LLM_Client.py`'s `call_chat_completion` is a small, synchronous, stateless POST — deliberately
not shared with `LLM_Core.py`'s async `fetch_from_llm` (`fetch_from_llm` must never raise; this
one must raise cleanly so `generate_npc_stats`'s fallback triggers). Its hard 20s `timeout`
matters because generation runs synchronously on whatever thread is instancing the scene
(always the GUI/main thread in practice) — a known v1 limitation: a scene with entity_template
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
the current room's (or scenario's) band line, not a distance-from-player.
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
(`advance_or_retreat`, `enter_room`) — never from a creature/ally's own combat-turn movement,
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

## Scenarios and rooms

`Rules/Fantasy/scenarios/*.toml` (`arena`, `tavern`, `field`, `dungeon`, `crypt`, plus
`character_test`/`scenario_entity_test` — see "Testing") each hold one `[scenario]` table, kept
in their own subdirectory so multiple scenarios can coexist without the flat `load_rules` scan
(which only keeps the last `[scenario]` table it reads) overwriting one with another.

A scenario is either a **plain single room** (entities listed directly under `[scenario]`) or a
**multi-room dungeon** (`crypt`): one or more `[[room]]` tables, each with its own
`entities`/`bands`/`enclosed` plus `[[room.exit]]` sub-tables (`{band, direction, destination,
arrival_band}`), and `[scenario].start_room` naming the starting room. A room's `entities` list
never includes the player — only room-local creatures/traps/chests; the player is listed once
at the scenario's top level. `self.rooms` stays empty for a plain scenario, which is what lets
`load_scenario`/`enter_room` branch on room-graph vs. flat behavior without a separate flag.

**The player is referenced generically.** Every scenario/room's `entities` list names the
player with the reserved sentinel `"player"` (`DM_Rules.py`'s `PLAYER_PLACEHOLDER`), never a
real template name. `_instance_entities` resolves it to `self.player_name` before the template
lookup, so a scenario keeps working regardless of which template is `is_player = true` or what
a freshly-created character was renamed to.

**A scenario file can define its own `[[entity]]` tables**, sibling to `[scenario]`/`[[room]]`,
scoped to this one scenario — letting a boss or one-off prop live in the same file as the
scenario referencing it. `load_scenario_definition` reads these into `self.entities` after
`load_rules` has run, so a scenario-local entity can reuse a shared name on purpose to override
it just for this scenario. `scenario_entity_test.toml` (excluded from
`list_available_scenarios`) exists solely to exercise this.

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
would let the LLM hallucinate an opening scene with no real content). `load_scenario()`
deep-copies each named template into an independent instance, tags it with its starting `band`,
disambiguates duplicates (`wolf`, `wolf_2`, ...), and gives each instance its own `entity_id`.

`enter_room(room_key, arrival_band)` — gated on the current room declaring a matching exit at
the player's band and on no living hostile remaining. Moves only the player's band;
HP/inventory/currency/conditions carry over. A room visited before is restored from
`self.visited_rooms` rather than re-instanced, so a cleared trap or looted chest stays that way.

**`self.entities` holds templates and live instances under the same keys** — instancing a
single-occurrence entity overwrites its template slot. `load_game` re-runs `load_rules()` before
re-instancing for this reason (see "Saving and loading").

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
resolves the one opponent-relative derived field, `"distance_to_target"` (the band gap to
`opponent_name`) — used by a creature choosing *between* attack options by range, ex:
`field.toml`'s `bandit` favors its `short bow` while `distance_to_target > 0`, falling to its
`rusty shortsword` once that gap closes to 0.

`evaluate_statuses` finds every status matching a trigger whose requirements the entity
currently meets and calls `apply_condition`, storing it in `entity["active_conditions"]`.
`dismiss_condition(entity_name, condition_name)` is the general-purpose removal primitive.

`evaluate_statuses` also sweeps the *other* direction: after applying whatever matches now, it
dismisses any active condition (from the same trigger) whose requirements no longer hold — ex:
healing back above a "wounded" tier's hp_per_remain range dismisses "wounded" in the same call.
A condition is only eligible for this sweep if stored with a falsy `dismiss` — one stored with a
named mechanism (ex: `"dead"`'s `dismiss = "resurrection"`) is left alone, so ordinary healing
can't revive a dead entity through the same path that clears a wound tier.

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
    own ground (`_current_ground_items`). **Known gap:** unlike `scenario_entities`, nothing in
    `"ground"` is saved/restored yet, so a drop since the last save doesn't survive a save/load
    round trip.
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

`get_attitude(entity, toward)` returns a six-value array (`disposition, trust, confidence,
respect, obligation, intimacy`, nominally -100..100; a `name` override beats `supertype` beats
`default`; no `[entity.attitudes]` table defaults to all-neutral). `get_attitude_tier(value)`
clamps to `[-150, 150]` and returns the first of seven `[[attitude_tier]]` bands whose range
contains it, in declaration order. `describe_attitude(entity, toward)` renders all six axes as
one sentence using each tier's own phrase per axis.

`describe_character(entity_name, toward_name=None)` builds a flavor-text roster line from purely
descriptive TOML fields (`description`, `qualities`, `memories`, `quotes`) plus, when
`toward_name` is given, the attitude sentence above — deliberately excluding mechanical data.
`DMCore.__init__` builds this roster into the `scenario_loaded` payload; `_on_turn_detected` also
attaches a fresh `result["defender_details"]` per action.

`self.player_name` is resolved once in `__init__` via `_resolve_player_name()`, which scans
loaded templates for the one with `is_player = true` and raises `ValueError` if none is marked.

## Dialogue

Directly addressing someone (`"talk to the innkeeper"`, `"ask the guard about the road"`) is a
third diceless channel: there's no item involved, the addressee is resolved from the scene
rather than looked up, and the result is a generated in-character reply, not a structured
mechanical outcome. Distinct from a *skill-based* social check (persuade/intimidate/deceive) —
those still roll dice via `resolve_opposed_action` and narrate in third person as the omniscient
GM; free-form talking never rolls anything and speaks as the addressed entity.
`Intent_Classification.py`'s `detect_dialogue_intent` recognizes `DIALOGUE_KEYWORDS` phrases (`"talk to"`/`"ask"`/
`"tell"`/`"greet"`/...), checked after item-interaction detection has had its shot (so
`"give the sword to Anne"` is never swallowed as dialogue) and before skill matching, publishing
`dialogue_detected {input, score}` with no further resolution.

`DMCore._on_dialogue_detected` delegates to `DM_Dialogue.py`'s `DialogueMixin`:
`_resolve_dialogue_target` searches the input for any present entity's name (whole-word,
excluding the player), falling back to `_get_target_name()`'s default scene target if none is
named. `_resolve_dialogue` gates on the target being present/alive (`reason: "not_present"`)
and not an inanimate `"object"` (`reason: "cant_talk"`) — but deliberately **not** on hostility:
addressing a hostile entity is allowed (shouting mid-fight), and the model is free to read that
as hostile/dismissive in character rather than being denied outright. A found target's
`persona`/`attitude` are attached for `LLMCore` to speak from. Publishes `dialogue_resolved
{target, input, found, present_entities, persona?, attitude?, reason?}` — no
`_publish_party_status()`, since dialogue never changes HP/equipment/inventory/conditions.

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
roster, and `exits` (`[]` for a flat scenario, else the room's exits resolved to friendly
names). No `_publish_party_status()` for the ordinary informational path — except when a removal
actually went through (see "Ad hoc entity creation and removal"), the one exception on purpose.

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
`player_name` excluded) as an enum constraint for `decide_entity_removal`. A real removal is
folded into `help_resolved` as `"removed"` and triggers `_publish_party_status()` — the one
exception to "ADaM never mutates state." `remove_entity_from_scene(name)` strips `name` from
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
silently retargets the player or flips the round-vs-action narration choice.

**Entity editing.** `_attempt_entity_edit` builds the same editable-name universe as removal and
asks `decide_entity_edit` to pick one and change it, or decline. Scope is narrow: a full
`new_description` rewrite plus `apply_condition`/`dismiss_condition` — never raw mechanical
fields like `skills`/`damage_value`. A description change tags `entity["edited"] = True` (see
"Persistence") and republishes `item_catalog_updated`; folded into `help_resolved` as `"edited"`
and triggers `_publish_party_status()`.

**Persistence.** An ad hoc entity has no static TOML template to re-derive from on reload, so
`DM_Persistence.py`'s `_collect_ad_hoc_entities` saves every *reachable* one's complete dict
under `"ad_hoc_entities"`; `load_game` restores each with a full dict replacement alongside
`"ground"`, then republishes `item_catalog_updated` once. `"removed_entities"` round-trips too,
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

## Narration

`LLMCore` subscribes to narration-relevant events, sharing outcome-text building
(`_describe_outcome`) and background-fetch plumbing (`_queue_narration`/`_fetch_and_publish`):
- `scenario_loaded` → `generate_scene_intro` — once, from `DMCore.__init__`.
- `round_resolved` → `generate_round_response` — combat, once per round.
- `action_resolved` → `generate_response` — non-combat, once per skill use.
- `action_not_understood` → `generate_clarification_response` — acknowledges input that didn't
  resolve to any action.
- `item_interaction_resolved` → `generate_item_interaction_response` — covers examine/take/give/
  trade/open/close/use/equip/unequip/drop and room transitions.
- `dialogue_resolved` → `generate_npc_dialogue` — a found target routes through
  `_queue_dialogue`; a denied one falls back to an ordinary `_queue_narration` explanation.
- `game_load_failed` → `generate_load_failed_response`.
- `help_resolved` → `generate_adam_response` — routes through `_queue_adam_response`, the one
  trigger here that never touches `context_window` at all.

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
`player_name`, `round_number`, `current_room_key`, `scenario_entities`, `ground`, and
per-instance `{hp, active_conditions, currency, inventory, equipped, band}`. `load_game` re-runs
`load_rules()`, then the same scenario-load path `__init__` uses, then overlays each saved
instance's mutable fields; a saved instance with no post-reload match is skipped. Publishes
`game_loaded` on success (not `scenario_loaded`, which would re-narrate an opening scene) or
`game_load_failed {"slot", "reason"}` on failure, then re-publishes `party_status_changed`.

`ground` (items dropped since the scenario started) round-trips too: a flat list for a
single-room scenario, or a dict keyed by `room_key` for a multi-room dungeon, mirroring the same
branch `_current_ground_items` (`DM_Inventory.py`) already makes.

`LLMCore.save_game`/`load_game` persist/restore `context_window` plus scenario name/description/
characters; loading is silent. `GUICore.save_game`/`load_game` persist/restore the Notes tab's
free text, same way.

Slot names are run through `os.path.basename` before use, so a slot can't escape `Saves/`.

## Tags vs. conditions

- **Tags** are static classification data, fixed for an entity's lifetime: `damage_tags`/
  `armor_tags`, `resistance_value`/`resistance_tags` (rolled, partial reduction via
  `get_damage_reduction`), `immunity_tags` (absolute — `is_immune_to` zeroes net damage
  regardless of roll), and `vulnerability_value`/`vulnerability_tags` (rolled, extra damage added
  before reduction). Immunity wins outright over vulnerability if both match.
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

Endpoint is LM Studio's OpenAI-compatible API. `/v1/models` lists the catalog, not what's
currently loaded — a chat completion can still 400 with `"No models loaded"` even when
`/v1/models` shows one. The request payload has no explicit `"model"` field, which only works
correctly when exactly one chat model is loaded.

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
- **`test_integration.py`** — every test needing a real, running LM Studio, gated on
  `_lm_studio_reachable()` so they skip together when nothing's listening on `127.0.0.1:1234`.
  `_LivePipelineTestCase`'s own optional `character` class attribute is forwarded straight into
  `DMCore`'s `character` param. `TestNpcGenerationLive` is a plain `unittest.TestCase` (no
  NLPCore/LLMCore) since NPC generation runs synchronously during `DMCore`'s own construction —
  a real tool-calling round trip. The pure fitting math and DMCore-side wiring both live in
  `test_unit.py` instead (patching `NPC_Generation._real_call_chat_completion` with a
  deterministic fake), so most of NPC generation stays covered by the fast offline suite — only
  the "does the currently-loaded model actually return a valid tool call" question needs a live
  LM Studio.

`python -m pytest -q` runs both files; `python -m pytest -q test_unit.py` runs the fast, offline
subset only.

## Known gaps

- `Intent_Classification.py` — a keyword-driven skill match can still dominate an unrelated
  whole-sentence embedding match (ex: "identify the dagger" resolves to the wrong skill); no
  multi-instance disambiguation (ex: "the wounded wolf" vs. "the other wolf"). One instance
  confirmed live by `test_unit.py`'s own keyword-collision invariant test: `DIALOGUE_KEYWORDS`'
  `"ask "` is a substring of a real skill's own `"mask"` keyword, so a sentence using "mask" as
  a whole word could still misfire as dialogue detection — not fixed, since changing keyword-
  matching behavior was out of scope for the refactor that found it.
- `DM_Rules.py`'s `_instance_entities` disambiguates duplicate names (`"wolf"`/`"wolf_2"`) via a
  counter scoped to one call's own `entity_entries` list, not against the live `self.entities`
  universe — deliberately, since it has to stay idempotent across repeated `load_scenario()`/
  `load_game()` calls over the *same* entity list (see that method's own comment). The accepted
  cost: two *different* rooms in the same multi-room dungeon that happen to declare the same
  creature name would silently collide — the second room's own instance would overwrite the
  first's live HP/conditions in `self.entities` rather than disambiguating to `"_2"`. Not
  currently reachable by any shipped scenario, and not fixed, since a real fix needs the engine
  to track which room a name came from, a bigger change than any deepening pass attempted so
  far — see `DM_Improvisation.py`'s own `_unique_entity_key`, a separate mechanism for ad hoc
  placement that checks `self.entities` directly and doesn't share this gap (or this fix).

## Extended goals

Not yet started, except where noted:
- Characters are language-dependent — an entity's own comprehension of the language it's
  addressed in should gate dialogue/narration, not just its attitude data.
- Dialogue sentiment sways attitudes — the sentiment of what the player says, not just which
  skill check they made, should be able to move an entity's `[entity.attitudes]` axes.
- Actions sway attitudes by varying degrees — a resolved action (combat, theft, a favor) should
  nudge attitude axes proportionally, not just be gated by attitude that already exists.
- Random encounters, enemy generator — procedurally populate a scene/room with creatures instead
  of every encounter being scenario-authored. **Partially started**: "NPC generation" fits a
  `generate = true` template's *stats* to a target CR at instancing time, but the template
  itself (attitudes/behavior/abilities/equipment, and whether/where it appears at all) is still
  hand-authored.
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
- A 'dungeon master' persona the LLM can speak directly to the player as.
- Tools that the LLM may call to directly interact with the scene.
