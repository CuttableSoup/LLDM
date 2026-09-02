# LLDM — Architecture

Part of the [LLDM](../CLAUDE.md) docs — the module map.

## Architecture

Six modules wired through `Event_Bus.py`, a synchronous pub/sub bus (`publish` calls every
subscriber immediately, over a snapshot of that event's subscriber list taken at the start of
the call — so a handler that subscribes a new callback for the event it's currently handling
doesn't have that callback invoked in the same `publish`, only the next one). `LLDM.py` boots
`NLPCore`, `LLMCore`, `GUICore` in that order at startup, but **not** `DMCore` — see "Booting
the game" for when and how it's actually constructed. The modules below live under `dm/`, `nlp/`,
`llm/`, `gui/`, `resolution/` (the pure, DMCore-independent layer `DM_Combat.py`/`DM_Status.py`/
`DM_Movement.py` wrap — see their own bullet, below), and `intents/` (see its own bullet) — only
`LLDM.py`, `paths.py`, `Event_Bus.py`, `CONTEXT.md`, and `Logger.py` stay at the repo root.
Elsewhere in this doc a module is still referred to by its bare filename; grep/glob for it
rather than assuming a particular path.

- **`DM_Core.py`** (`dm/`) — `DMCore`'s `__init__` plus its three event handlers
  (`_on_turn_detected`, `_on_item_interaction_detected`, `_on_dialogue_detected`) and their
  direct helpers. `_on_turn_detected` also calls `_on_item_interaction_detected` directly, once
  per item-kind clause in a mixed turn — see "Multiple actions". Composed from sibling mixin
  files, each owning one concern: `DM_Rules.py` (TOML/scenario/room loading), `DM_Combat.py`
  (dice rolling, opposed checks, damage, ability/behavior resolution), `DM_Status.py`
  (statuses/conditions, entity tests), `DM_Inventory.py` (currency/item transfer, plus
  equip/unequip/drop/use/container item-interaction intents), `DM_Social.py` (attitudes,
  character description), `DM_Movement.py` (bands, range, plus the actual room-transition/
  formation/advance-retreat mechanics every free-standing intent's own `intents/` module calls
  into — see its own bullet, below), `DM_Time.py` (the block clock, rest), `DM_Travel.py`
  (grid-based overworld travel), `DM_Persistence.py` (save/load), `DM_CharacterCreation.py`
  (baking a finished character-creation result onto the player entity), `DM_Dialogue.py`
  (resolving who's being addressed in free-form conversation, and the language-switch
  mechanic), `DM_Help.py` (the reserved "ADaM" out-of-character help persona), and
  `DM_Improvisation.py` (ad hoc entity creation/removal via LLM function calling). Python's MRO
  flattens every mixin method onto one `DMCore` instance, so call sites don't care which file
  defines a given method. `DM_Combat.py`/`DM_Status.py`/`DM_Movement.py`'s own dice/damage/
  condition methods (`resolve_action`, `calculate_damage`, `apply_condition`, `get_band`, ...)
  are themselves thin wrappers over `Combat_Resolution.py` — a pure, DMCore-independent module
  taking `entities`/`rules`/`skills`/`event_bus` explicitly instead of reading `self`, mirroring
  `Challenge_Rating.py`'s own existing shape (see "Status and conditions"). Every mixin method
  keeps its original signature, so this is invisible to every other caller in the codebase.
- **`intents/`** (repo root, sibling to `dm/`/`nlp/`/`llm/`/`resolution/`/`gui/`) — one module
  per free-standing intent (see `CONTEXT.md`'s "Free-standing intent": `advance`/`retreat`,
  `formation_behind`/`formation_abreast`, `speak_language`, `rest`, `move`, `travel`), each
  exporting a `resolve(core, data, resolved)` and a `narrate(llm_core, data) -> str`, plus
  `registry.py`'s own `HANDLERS` manifest mapping every one of those eight intent strings to its
  module's pair. `DM_Core.py`'s `_on_item_interaction_detected` and `LLM_Core.py`'s
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
  `travel`'s own `narrate()` also updates `llm_core.scenario_description`/`scenario_characters`
  on success, the same ongoing-narration-grounding refresh `generate_scene_intro` does for a
  brand-new scenario) — item-named intents (`examine`/`take`/`give`/`trade`/`use`/`equip`/
  `unequip`/`drop`/`open`/`close`) are out of scope for this registry, since they share real
  pre-condition logic (scene target resolution, the locked-container gate,
  `_run_interact_program`) ahead of their own dispatch in `DM_Core.py` that a per-intent split
  would only duplicate.
- **`NLP_Core.py`** (`nlp/`) — thin EventBus glue: subscribes to `user_input_submitted`/
  `rules_loaded`/`item_catalog_updated`, delegates to `Intent_Classification.py`'s
  `IntentClassifier`, and publishes whatever events come back. Also defines
  `SentenceTransformerMatcher`, the production `IntentMatcher` adapter — owns the loaded
  `sentence-transformers` (`all-MiniLM-L6-v2`) model and every precomputed skill/item/target
  embedding tensor, and is the one place in this file that still touches the EventBus for
  granular "mapped input to X" diagnostics, since encoding/scoring is where those facts become
  known. `NLPCore` itself owns no classification logic.
- **`Intent_Classification.py`** (`nlp/`) — pure, EventBus-independent: `IntentClassifier.classify()`
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
- **`LLM_Core.py`** (`llm/`) — posts to Ollama's OpenAI-compatible `/v1/chat/completions` on a
  background thread, with a rolling 100-message context window. Subscribes to nine narration
  triggers (see "Narration").
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
  up on the Party tab just for existing there. `DM_Combat.py`'s `get_party_challenge_rating`
  filters the same way (see "Challenge rating"). Notes is a free-typed scratchpad with its own
  save/load slice; Map is a free-form drawing canvas the engine never reads; Debug overwrites
  (not appends) the most recent LLM request/response on every `llm_debug_updated`.
- **`Textual_Core.py`** (`gui/`) — a parallel, headless-testable mirror of `GUI_Core`'s output, driven
  the same way via `user_input_submitted`. Not part of `LLDM.py`'s boot sequence; run standalone.
  Used by `test_unit.py` for pilot-driven UI tests.
- **`Logger.py`** — subscribes to `log_info`/`log_error`, prints with timestamps.

