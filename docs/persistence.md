# LLDM — Saving and Loading

Part of the [LLDM](../CLAUDE.md) docs — the per-slot save/load contract.

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
currency, exp, inventory, equipped, band, attitude_deltas, action_attitude_deltas,
current_language, prompt_directive, mount}`. `load_game` re-runs `load_rules()`, then re-instances
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

