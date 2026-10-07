# LLDM — Architecture

Part of the [LLDM](../CLAUDE.md) docs — the module map.

## Architecture

Six modules wired through `Event_Bus.py`, a synchronous pub/sub bus (`publish` calls every
subscriber immediately, over a snapshot of that event's subscriber list taken at the start of
the call — so a handler that subscribes a new callback for the event it's currently handling
doesn't have that callback invoked in the same `publish`, only the next one). `LLDM.py` boots
`NLPCore`, `LLMCore`, `GUICore` in that order at startup, but **not** `DMCore` — see "Booting
the game" for when and how it's actually constructed. The modules below live under `dm/`, `nlp/`,
`llm/`, `gui/`, `resolution/` (the pure, DMCore-independent layer: `Combat_Resolution.py`, `Combat_Actions.py`,
`Law_Enforcement.py`, ... — see their own bullets, below), and `intents/` (see its own bullet) — only
`LLDM.py`, `paths.py`, `Event_Bus.py`, `CONTEXT.md`, and `Logger.py` stay at the repo root.
Elsewhere in this doc a module is still referred to by its bare filename; grep/glob for it
rather than assuming a particular path.

- **`DM_Core.py`** (`dm/`) — `DMCore`'s `__init__` plus its three event handlers
  (`_on_turn_detected`, `_on_item_interaction_detected`, `_on_dialogue_detected`) and their
  direct helpers. `_on_turn_detected` also calls `_on_item_interaction_detected` directly, once
  per item-kind clause in a mixed turn — see "Multiple actions". Composed from sibling mixin
  files, each owning one concern: `DM_Rules.py` (TOML/scenario/room loading), `DM_Combat.py`
  (just `DMCoreCombatHooks` — the logic itself is `resolution/Combat_Actions.py`), `DM_Status.py`
  (statuses/conditions, entity tests), `DM_Inventory.py` (currency/item transfer, plus
  equip/unequip/drop/use/container item-interaction intents), `DM_Social.py` (attitudes,
  character description), `DM_Movement.py` (bands, range, plus the actual room-transition/
  formation/advance-retreat mechanics every free-standing intent's own `intents/` module calls
  into — see its own bullet, below), `DM_Time.py` (the block clock, rest), `DM_Travel.py`
  (grid-based overworld travel), `DM_Persistence.py` (save/load entry points and the world's
  save slice — see `persistence/` below), `DM_CharacterCreation.py`
  (baking a finished character-creation result onto the player entity), `DM_Dialogue.py`
  (resolving who's being addressed in free-form conversation, and the language-switch
  mechanic), `DM_Help.py` (the reserved "ADaM" out-of-character help persona), and
  `DM_Improvisation.py` (ad hoc entity creation/removal via LLM function calling). Python's MRO
  flattens every mixin method onto one `DMCore` instance, so call sites don't care which file
  defines a given method. The dice/damage/condition primitives (`resolve_action`,
  `apply_condition`, `get_current_hp`, `get_band`, ...) are *not* DMCore methods: they live in
  `Combat_Resolution.py` — a pure, DMCore-independent module whose functions take a
  `WorldContext` (`resolution/World_Context.py`) first, mirroring `Challenge_Rating.py`'s own
  shape (see "Status and conditions"). `DMCore.world` is the one live context, and
  `DMCore.entities`/`rules`/`skills` are read-only properties over it, so nothing can rebind
  them out from under a function holding the context (they're filled in place). Call sites write
  `Combat_Resolution.get_current_hp(self.world, name)`; `WorldContext(entities)` is enough for a
  test or helper that only reads entities. What still lives on the mixins is orchestration that
  reaches into sibling mixins (`resolve_behavior_action`, `roll_initiative`, the lore check, ...).
- **`intents/`** (repo root, sibling to `dm/`/`nlp/`/`llm/`/`resolution/`/`gui/`) — one module
  per free-standing intent (see `CONTEXT.md`'s "Free-standing intent": `advance`/`retreat`,
  `formation_behind`/`formation_abreast`, `speak_language`, `rest`, `move`, `travel`), each
  exporting a `resolve(core, data, resolved)` and a `narrate(llm_core, data) -> str`, plus
  `registry.py`'s own `HANDLERS` manifest mapping every one of those eight intent strings to its
  module's pair. Each module also declares how the classifier recognizes its intent(s) as data — a
  `MATCH` of `match.py`'s `IntentMatch` (keyword phrases, extra regexes, semantic-router prototypes,
  and the `exempt`/`question_blocked`/`before_items` flags) — which `registry.py`'s `MATCHES`
  collects in gate order. `Intent_Classification.py` builds its keyword tables, `INTENT_PROTOTYPES`,
  `EXEMPT_ITEM_INTENTS` and `QUESTION_BLOCKED_ROUTES` from it, and `detect_item_intent` walks it, so
  a new free-standing intent never edits the classifier's own tables (the gate *order* is
  `MATCHES`' own, and the classifier's keyword-name constants like `REST_KEYWORDS` are views of it). `DM_Core.py`'s `_on_item_interaction_detected` and `LLM_Core.py`'s
  `generate_item_interaction_response` both look this registry up by intent string instead of
  keeping their own hand-synced if/elif ladder — adding a free-standing intent means adding one
  row here and one new sibling module, never touching either of those two files again. Neither
  `dm/` nor `llm/` imports from the other; both import downward from this neutral package
  instead, the same reason `resolution/` exists as its own layer under neither. `resolve()`
  itself is a thin wrapper calling into the mixin method that actually implements the mechanic
  (`DM_Movement.py`'s `advance_or_retreat`/`_resolve_formation_intent`/
  `_resolve_room_transition_intent`/`_resolve_travel_intent`, `DM_Dialogue.py`'s
  `_resolve_language_intent`, `DM_Time.py`'s `rest`) — those methods, and their own tests, are
  unchanged by this split. `narrate()` builds the LLM prompt from the same
  `item_interaction_resolved` payload, including that intent's own failure-reason text (`move`/
  `travel`'s own `narrate()` only *read* the payload; `LLMCore.generate_item_interaction_response`
  is the one writer of the narrator's scene state, refreshing it from the payload on a successful
  `ARRIVAL_INTENTS` intent before narrating, the same ongoing-narration-grounding refresh
  `generate_scene_intro` does for a brand-new scenario). Item-named intents
  (`examine`/`take`/`give`/`trade`/`use`/`equip`/`unequip`/`drop`/`open`/`close`) are the other
  manifest, `item_named.py`'s `ITEM_NAMED`: each `ItemIntent` declares its `resolve`, its
  success-only `narrate`, and the two pre-conditions that used to be branches in `DM_Core.py` —
  `gated` (the scene target's locked-container gate applies) and `ground_aware` (an item on the
  room's ground is reached ahead of any target). `DM_Core.py` keeps the shared pre-condition logic
  (scene target, `already_owned`, `_run_interact_program`) once and reads those flags; a denied
  attempt is still narrated by the shared failure text in `Narration_Prompts.item_interaction`.
  `improvisation.py` holds the three intent sets the ad hoc creation fallback partitions by.
- **`Ability_Effects.py`** (`resolution/`) — what an actor's ability does once its roll has
  landed, one implementation for the player's own turn (`DMCore._finish_rolled_outcome`) and every
  other entity's combat turn (`resolve_behavior_action`): damage (single or AoE, `save_for_half`),
  summon, dispel, cure, teleport, spell materials, and the ability's own `on_pass`/`on_fail`
  program. It takes a *resolved* target — the caller decides who (the player's turn infers it from
  text; an NPC's comes from its behavior list). Each effect is data-gated, so an NPC gains only what
  its ability authors; `teleport_to_location` stays player-only. What the combat graph can't reach
  (conjuring, banishing, moving, entering a location, spending materials) goes through `ctx.hooks`.
- **`Conveyance.py`** (`resolution/`) — the graph an entity's `mount` field draws and what is asked
  of it: `carrying_capacity` (sums across a team), `travel_speed` (minimum), `terrain_tags` (union),
  `current_bulk`, `max_bulk`, `would_exceed_capacity`/`is_overloaded`, `mount_targets`/`mount_chain`,
  `is_conveyance`. One walk with one cycle guard; pure over a `WorldContext`. Mounting, hitching and
  band-syncing stay in `DM_Movement.py`, which calls in here.
- **`Entity_Reference.py`** (`resolution/`) — which entities a line of text literally names: key,
  `name` or any alias, whole-word, with `skip_modifier_aliases` for "kick the spice cart" vs "pin the
  merchant". `first_named`/`find_named`/`named_in_reading_order` serve dialogue, attacks,
  mount/hitch/formation and lore checks. Exact matching only — fuzzy/embedding matching of an address
  phrase stays in `nlp/`.
- **`Data_Validation.py`** (`resolution/`) — `DataValidator(world, entity_templates,
  locations).validate()` returns `Problem` records for everything loaded; `DMCore.validate_loaded_data()`
  publishes each as a `log_error`. See `docs/data-conventions.md`.
- **`Combat_Actions.py`** (`resolution/`) — what a combatant does, as functions over a
  `WorldContext`: `calculate_damage` and its kill consequences (XP, murder check, `create_spawn`),
  `resolve_targets`, ability/behavior selection, `resolve_behavior_action`, challenge rating, XP, the
  lore check, and the status/range gates they rest on (`is_action_prevented`, `is_in_range`,
  `has_medium_access`, `evaluate_proximity_statuses`, `is_identified`). The few things it needs from
  sibling mixins — `is_hostile`, `note_kill`, attitude nudges, `move_toward_or_away`, `transfer_item` —
  come through `ctx.hooks` (`CombatHooks`; `DM_Combat.py`'s `DMCoreCombatHooks` is the DMCore adapter),
  plus the five `Ability_Effects.py` needs (`summon_creature`, `remove_entity_from_scene`,
  `consume_materials`, `move_entity`, `enter_location`). That is eleven: past about ten, convert the
  owning mixin instead of widening it.
  `World_Context.py` is the `WorldContext` itself: entities/rules/skills/event_bus plus
  `scenario_entities`/`player_name`/`universal_abilities`/`hooks`.
- **`events/`** (repo root) — the declared payloads of the events that carry the most structure,
  as `TypedDict`s (`schemas.py`: `item_interaction_resolved`, `dialogue_resolved`, `turn_detected` and
  its clauses, `llm_response_ready`), and `validate.py` to check a payload against one. Nothing in
  production checks them; `tests/event_contract.py` supplies a `ValidatingEventBus` (every test's
  bus) that fails the test at the publishing line on an unknown or missing key, plus a static check
  that every key a consumer reads is declared — a key on only one side of the bus is otherwise a
  silent `None`. A new payload field is added to `schemas.py` in the same change that produces it.
- **`Law_Enforcement.py`** (`resolution/`) — `LawEnforcement`: the confrontation/arrest flow and
  the law's saved state, DMCore-independent behind a `LawWorld` port; `DM_Enforcement.py` is
  `DMCore`'s side of it (the port adapter and the event wiring). See `docs/law.md`.
- **`persistence/`** (repo root, a neutral package like `intents/`) — `slot.py` is the save-slot
  seam: `FileSlotStore`/`MemorySlotStore` (where a slot lives, atomic writes, `format_version`),
  `SaveError`, and the `Persistable` contract `DMCore`'s save slices implement. `dm/`, `llm/` and
  `gui/` all import it downward; none imports another. See `docs/persistence.md`.
- **`NLP_Core.py`** (`nlp/`) — thin EventBus glue: subscribes to `user_input_submitted`/
  `rules_loaded`/`item_catalog_updated`/`location_exits_updated`, delegates to
  `Intent_Classification.py`'s `IntentClassifier`, and publishes whatever events come back. Also
  defines `SentenceTransformerMatcher`, the production `IntentMatcher` adapter — owns the loaded
  `sentence-transformers` (`all-MiniLM-L6-v2`) model and every precomputed skill/item/target/
  intent/destination embedding tensor, and is the one place in this file that still touches the
  EventBus for granular "mapped input to X" diagnostics, since encoding/scoring is where those
  facts become known. `NLPCore` itself owns no classification logic, and takes its `matcher` as an
  optional argument (a `FakeMatcher` in tests, so the confirmation/arrest answer protocol runs with no
  model load).
- **`Intent_Classification.py`** (`nlp/`) — pure, EventBus-independent: `IntentClassifier.classify()`
  returns `(processed_text, events, adjudication)` — `events` a list of
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
  publishes `action_not_understood` instead — unless the semantic intent router claims it first
  (see "Last-chance semantic routing" in `docs/action-resolution.md`). Two events here aren't
  input-driven: `IntentMatcher.register_item` incrementally *appends* a newly-created/reload-
  restored ad hoc entity's name/description on `item_catalog_updated`, and
  `IntentMatcher.set_destinations` wholesale *replaces* the current location's reachable-exit
  bank on `location_exits_updated` (`DM_Rules.py`'s `_enter_location`) — replace, not append,
  because the reachable set changes completely on every move, which is exactly why it carries a
  different verb from `register_item`.
- **`LLM_Core.py`** (`llm/`) — posts to Ollama's OpenAI-compatible `/v1/chat/completions` on a
  background thread, with a rolling 100-message context window. Subscribes to nine narration
  triggers (see "Narration"). It owns the transport only — the window, publish ordering,
  sourcebook retrieval, the request, the save slice. *What the narrator is told* lives in
  **`Narration_Prompts.py`** (`llm/`): one pure builder per trigger (`scene_intro`,
  `item_interaction`, `arrest`, ...) over a `NarratorState` and the event payload, returning a
  `Narration` (a prompt plus the system-message kind that frames it: standing GM, dialogue, ADaM,
  scene query), a `Notice` (out-of-character, no narration) or a `Skip`; plus the four system-message
  builders (which take already-retrieved lore, so nothing there calls out) and the outcome/effect
  formatters. Each `generate_*` on `LLMCore` is a one-liner into its builder, and one `_queue`
  replaces the old per-kind queue methods. The free-standing intents' `narrate()` receive the
  `NarratorState`, so they run without an `LLMCore` too.
- **`GUI_Core.py`** (`gui/`) — Tkinter window: history pane + tabbed Party/Notes/Map/Debug panels, plus
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
  up on the Party tab just for existing there. `Combat_Actions.py`'s `get_party_challenge_rating`
  filters the same way (see "Challenge rating"). Notes is a free-typed scratchpad with its own
  save/load slice; Map is a free-form drawing canvas the engine never reads; Debug overwrites
  (not appends) the most recent LLM request/response on every `llm_debug_updated`.
- **`Textual_Core.py`** (`gui/`) — a parallel, headless-testable mirror of `GUI_Core`'s output, driven
  the same way via `user_input_submitted`. Not part of `LLDM.py`'s boot sequence; run standalone.
  Used by `tests/test_gui.py` for pilot-driven UI tests.
- **`Logger.py`** — subscribes to `log_info`/`log_error`, prints with timestamps.

