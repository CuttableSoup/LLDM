# LLDM — Saving and Loading

Part of the [LLDM](../CLAUDE.md) docs — the per-slot save/load contract.

## Saving and loading

Three sibling JSON files per slot — `Saves/<slot>/dm_state.json`, `llm_state.json`,
`gui_state.json` — written/read independently by `DMCore`, `LLMCore`, and `GUICore`. `EventBus`
has no request/response mechanism, so each core owns and persists its own slice.

**The slot store** (`persistence/slot.py`) is the one place that knows where a slot lives and how a
part is written and read: `FileSlotStore` (production — `Saves/<slot>/<part>.json`, written
atomically via a temp file and `os.replace`) and `MemorySlotStore` (tests — same interface, no
filesystem). All three cores take an optional `slot_store=`; `LLDM.py` builds one `FileSlotStore`
and hands it to each. Every part is stamped with `format_version` (`FORMAT_VERSION`); a slot from
another version, a missing part, or invalid JSON raises `SaveError` (`not_found`, `corrupt`,
`unsupported_version`), which `DMCore.load_game` publishes as `game_load_failed {"slot",
"reason"}`. There are no migrations: an older slot simply won't load. `store.read` returns the
whole parsed part before anything live is touched, so a bad part never leaves a half-restored
`DMCore` (a failure *during* restore is still fail-fast, with no rollback).

**Participants.** `dm_state` is assembled by `DMCore.save_parts`, an ordered list of `Persistable`
slices (`snapshot()` → that slice's keys, `restore(data)`). Each slice lives next to the state it
owns, and `snapshot_all` rejects two slices claiming one key. The list order *is* the restore
order:

1. `ClockSlice` (`DM_Time.py`) — `round_number`, the block clock, `pending_downtime`.
2. `LawEnforcement` (`resolution/Law_Enforcement.py`, itself a `Persistable`) — `legal_records`,
   `pending_reports`, `pending_arrest`.
3. `RemovedEntitiesSlice` (`DM_Improvisation.py`) and `KnownLocationsSlice` (`DM_Travel.py`) —
   read by re-instancing, so they restore first.
4. `WorldSlice` (`DM_Persistence.py`) — everything about the world itself (below), one participant
   because `load_rules` → rename → instancing replay → saved-location jump → overlay is a single
   ordered sequence.
5. `SessionSlice` (`DM_Core.py`) — `current_target`, `recent_narration`, `conversation_partner`;
   after the world, because re-instancing resets the first and clears the last.

A new subsystem adds a slice and one entry to that list; it never edits `DM_Persistence.py`.

**Trigger:** `save_requested`/`load_requested {"slot": slot_name}`, published by
`Intent_Classification.py`'s `detect_save_load_intent` (via `IntentClassifier.classify`), by
`GUICore`'s File → Save... / Character → Load... popups
(see "Booting the game" for the cold-start case), or by `Textual_Core`'s Save/Load buttons.

`WorldSlice` writes a diff from a fresh instantiation: `setting`, `scenario_key`,
`player_name`, `round_number`, `current_location_key`, `current_room_key`, `location_runtime`
(every visited location's own `{persistent_names, visited_rooms}` cache — see "Scenarios,
locations, and rooms"), `scenario_entities`, `ground`, and per-instance `{hp, active_conditions,
currency, exp, inventory, equipped, band, attitude_deltas, action_attitude_deltas,
current_language, prompt_directive, mount}` — plus, for whichever instance is the player
specifically, `skills`/`qualities`/`languages` too (see below). `load_game` re-runs
`load_rules()` — which re-seeds the player entity back under its *original* template key (ex:
`"gladstone"`), undoing any character-creation rename the live session applied
(`DM_CharacterCreation.py`'s `apply_character_creation`, see `docs/character-creation.md`) — so
`self.player_name` is re-resolved fresh (`_resolve_player_name`) and `_rename_player_entity`
replays the save's own `player_name` on top of that, before `load_scenario_definition`/
`load_scenario` ever run; without this, every `self.entities[self.player_name]` lookup during the
reload (starting with `_enter_location`'s own arrival-band write) raises a bare `KeyError` on the
saved, renamed name. One subtlety in the resolve itself: if `load_game` runs against an
already-booted `DMCore` whose player was renamed earlier in the same session (ex: the in-app Load
menu, not a fresh process), that earlier renamed entity is still sitting in `self.entities` (a key
`load_rules()` never touches) and still carries its own `is_player = true` — left in place
alongside the freshly reloaded template's own `is_player = true` copy, `_resolve_player_name`'s
"the one `is_player` entity" lookup is ambiguous between the two, so `load_game` drops the stale
one first whenever some *other* key has already proven it's the real template (i.e., whenever
more than one `is_player` entity exists right after `load_rules()`).

Character creation (`apply_character_creation`) can freely diverge the player's own
`skills`/`qualities`/`languages` from the template's own hand-authored baseline (race/point-buy
allocation, plus whatever race language/starting gear chargen applied) — unlike an ordinary
hand-authored entity, none of that has any other source of truth to re-derive from on reload
(`load_scenario`'s own `_instance_entities` always deep-copies fresh from the *template's* own
hand-authored fields), so these three round-trip unconditionally for the player specifically, the
same way a `generated` NPC's own skills/qualities already do — without it, a customized
character's build would silently revert to the template's defaults on every reload, whether or
not the character was also renamed.

`load_game` then re-instances every location the save file's own `location_runtime` says was ever
visited (each location's own
`entities` once, each of its visited rooms' own entities once — mirroring exactly how a single
room's own instance list was already re-derived from the room's static entities rather than
trusted directly, so `_instance_entities`' own idempotent occurrence-counting reproduces the
identical instance names every time) *before* `load_scenario()`/`_enter_location` ever look at
`self.location_runtime`, so their own "already cached" check finds it and reuses it. Then jumps
to the saved `current_location_key`/`current_room_key` if they differ from the scenario's own
`start_location`. Both re-entries run with `_restoring_save` set, which makes
`_resolve_location_encounter` skip its `on_enter` rolls — before that, every reload could spawn and
narrate a fresh arrival encounter (a playtest's save → load → save drifted on it). Finally overlays each saved instance's mutable fields; a saved instance with
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


## What narrated and promoted NPCs need (nothing new)

Neither feature adds a save key of its own, by design:

- A **narrated** person and a **promoted** NPC both carry `ad_hoc: True` from their generator, so
  each is saved whole under `ad_hoc_entities` and re-added to `scenario_entities` on load, exactly
  like a conjured creature. They exist only because they were generated once, so nothing is
  re-derived from static TOML on reload. The per-scene budgets are counted off live `ad_hoc`/
  `source = "narration"` membership rather than a stored counter, so they survive a reload for
  free. A pending (extracted but not yet applied) batch is deliberately not saved: it is scene-
  scoped and only ever a few seconds old.

The one genuinely new key is `"recent_narration"` — the last few narration beats DMCore keeps as
grounding for promotion (`docs/adam-improvisation.md`). It's restored *after* the instance overlay
rather than in the prologue, since nothing during re-instancing reads it and the mid-load
narration `load_game` itself triggers would otherwise be the first thing appended to it. Absent
from an older save, which is harmless.

`"conversation_partner"` (`{"key", "idle_turns"}` or null — who the player is talking to, see
`docs/social-dialogue.md`'s "Conversation partner") is restored at the same point and for a
related reason: re-instancing re-enters the saved location, and `_enter_location` clears it.
`load_game` then republishes it (`conversation_partner_updated`) so NLPCore routes the next
unmarked line correctly. Absent from an older save: no conversation running.
