# LLDM — Character Creation and Booting

Part of the [LLDM](../CLAUDE.md) docs — race/point-buy, and the three ways a game starts.

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
`{"race", "allocation", "pip_spend", "name"}` — is the one piece that touches `DMCore` state,
called from `DMCore.__init__` right after `_resolve_player_name()` and before any scenario
loads. `"allocation"`, if non-empty, is validated and overwrites `self.entities[self.player_name]
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

**Training (spending XP on skills).** `Character_Creation.py`'s `spend_pip(dice, pips, exp)`
raises one skill by a single pip, costing XP equal to its own *current* dice count — pips roll
over into an extra die at 3 (`skill_rating`'s own "3 pips = 1 die" scale, `Challenge_Rating.py`),
so a trained-up skill lands on the same `{dice, pips}` shape any hand-authored one would.
`spend_exp_on_skills(skills_dict, exp, pip_spend)` replays an ordered list of skill names (each
entry = one more pip on that skill, its own cost based on wherever the replay has put that
skill's dice by then) on top of `skills_dict`, all-or-nothing — the first unaffordable or
unknown-skill entry rejects the whole spend and returns the original, untouched
`skills_dict`/`exp`. `apply_character_creation`'s own `"pip_spend"` — independent of
`"allocation"`/`"race"`, so it applies to the player template's own hand-authored skills just as
well as a freshly-built point-buy result — replays this fresh against `player["skills"]` and
`player["exp"]` (ex: `characters.toml`'s gladstone, `exp = 100`) the same "recompute, don't trust
the client" way `"allocation"` is validated; a rejected spend is logged and left entirely
unapplied, without aborting the rest of the method (training and renaming are unrelated).

`Character_Creation_GUI.py`'s `CharacterCreationDialog` (a modal `Toplevel`) is the interactive
front end: an optional name field, a race dropdown, a per-skill allocation row (baseline,
point-buy spend, running total, and a "Train" button spending from `player_exp`), a "dice
remaining"/"XP remaining" counter pair (only the dice counter gates Create — leftover XP just
carries over unspent), and Create/Cancel. `self.pip_spend` is the literal, ordered click log —
`_recompute_training` is the single source of truth, replaying it via `spend_exp_on_skills`
against whatever the current baseline+allocation is (never hand-tracked incrementally), so
switching race clears it (a purchase's own cost basis no longer means anything) and editing an
allocation re-prices every later entry live rather than preserving what was actually paid at
click time. `self.result` is always `{"race", "allocation", "pip_spend", "name"}` once Create is
pressed, or `None` if cancelled. `GUICore.request_character_creation` loads
`load_player_starting_exp(rules_dir)` (a second, separate `Rules/<setting>/*.toml` scan for the
`is_player` template's own `"exp"`) alongside `load_character_creation_data`, runs the dialog,
and — only when not cancelled — publishes `"character_created"`.


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

