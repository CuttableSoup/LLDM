# LLDM

An autonomous dungeon master: the player types free-text actions, NLP maps them to a skill,
a simplified D6 (West End Games) engine rolls dice and resolves outcomes, and a local LLM
(currently Gemma via LM Studio at `http://127.0.0.1:1234`) narrates what happened. Skills,
entities, items, spells, rules, and scenarios are all data-driven via TOML in `Rules/Fantasy/`.

## Architecture

Six modules wired through `Event_Bus.py`, a synchronous pub/sub bus (`publish` calls every
subscriber immediately, over a snapshot of that event's subscriber list taken at the start of
the call — not the live list — so a handler that itself subscribes a new callback for the
event it's currently handling doesn't have that callback also invoked within the same
`publish`; it only starts firing on the *next* one). `LLDM.py` boots `NLPCore`, `LLMCore`,
`GUICore` in that order at startup, but **not** `DMCore` — see "Booting the game" below for
when and how it's actually constructed.

- **`DM_Core.py`** — `DMCore`'s `__init__` plus its two event handlers
  (`_on_action_detected`, `_on_item_interaction_detected`) and their direct helpers: the
  orchestration that spans every domain mixin. The class itself is composed from sibling
  mixin files, each owning one concern: `DM_Rules.py` (TOML/scenario/room loading),
  `DM_Combat.py` (dice rolling, opposed checks, damage, ability/behavior resolution),
  `DM_Status.py` (statuses/conditions, entity tests), `DM_Inventory.py`
  (currency/item transfer), `DM_Social.py` (attitudes, character description),
  `DM_Movement.py` (bands, range), `DM_Persistence.py` (save/load), and
  `DM_CharacterCreation.py` (baking a finished character-creation result onto the player
  entity — see "Character creation" below). Python's MRO flattens every mixin method onto
  one `DMCore` instance, so `dm_core.<method>(...)` call sites don't care which file defines
  a given method.
- **`NLP_Core.py`** — `sentence-transformers` (`all-MiniLM-L6-v2`) embeds each skill's
  name/description/keywords as separate phrases, then cosine-matches player input against all
  of them. Also matches free text against item names/directions/save-load prefixes for
  non-skill intents (see "Items and movement as intents" below), checked before skill
  matching. Publishes `action_detected {skill, score, input, target?}` only when the score
  clears `confidence_threshold` (`0.5`); below it, publishes `action_not_understood` instead.
- **`LLM_Core.py`** — posts to LM Studio's OpenAI-compatible `/v1/chat/completions` on a
  background thread, with a rolling 100-message context window. Subscribes to six narration
  triggers (see "Narration" below).
- **`GUI_Core.py`** — Tkinter window: history pane + tabbed Party/Notes/Map/Debug panels, plus
  three dropdown menus on the window's menu bar: Character (Create... only), File (Save.../
  Load...), and Scenario (Load... only). Character -> Create... opens the race/point-buy
  dialog (`Character_Creation_GUI.py`) and publishes `character_created`, then (if a game
  hasn't already started — see `_on_game_started` below) stashes the result as
  `self._pending_character` and unlocks Scenario -> Load...; File -> Load... opens a
  slot-picker popup (listing every subdirectory of `Saves/`) and publishes `load_requested`
  directly, since a save already carries its own scenario — it lives under File, not
  Character, since it's a save-file operation rather than a character one. Scenario -> Load...
  is `DISABLED` until a character is pending; picking it opens a popup listing every real
  scenario (`list_available_scenarios`, `DM_Rules.py` — `character_test` excluded) and, on a
  selection, publishes `scenario_selected {"scenario_name", "character"}` paired with
  `self._pending_character`, then locks itself shut again. `_on_game_started` (subscribed to
  `rules_loaded`, which fires once per `DMCore` construction across every boot route) sets
  `self._game_started` and locks Scenario -> Load... shut for the rest of the session, so a
  later Create... can't reopen it once a game already exists. `GUICore` never constructs a
  `DMCore` itself — it only ever publishes; see "Booting the game" below for who's listening.
  History mirrors `llm_response_ready`; Party redraws on `rules_loaded`/`party_status_changed`
  as a `ttk.Treeview` (one node per `is_player`/`is_party` entity, expanding into Equipment/
  Skills/Abilities/Inventory/Conditions — Equipment lists every valid slot for the member's own
  supertype/subtype, filled or `(empty)`, via `get_equip_slots`'s same override precedence as
  `get_attitude`, see "Data/TOML conventions"). Membership is filtered through the payload's
  own `"scenario_entities"` list, not `is_player`/`is_party` alone — `self.entities` also still
  holds every *uninstanced* template from every loaded TOML file (ex: `characters.toml`'s
  `anne`, `is_party = true`, but not part of `arena.toml`'s own entities list), which must not
  show up on the Party tab just for existing on disk; `DM_Combat.py`'s
  `get_party_challenge_rating` filters the same way (see "Challenge rating"). Notes is a
  free-typed scratchpad with its own save/load slice. Map is a free-form drawing canvas the
  engine never reads. Debug overwrites (not appends) the most recent LLM request/response on
  every `llm_debug_updated`.
- **`Textual_Core.py`** — a parallel, headless-testable mirror of `GUI_Core`'s output, driven
  the same way via `user_input_submitted`. Not part of `LLDM.py`'s boot sequence; run standalone.
  Used by `test_unit.py` for pilot-driven UI tests.
- **`Logger.py`** — subscribes to `log_info`/`log_error`, prints with timestamps.

## Action resolution pipeline

`user_input_submitted` → `NLPCore` → `action_detected {skill, score, input, target?}` →
`DMCore` resolves it → `round_resolved` (combat) or `action_resolved` (no combat) → `LLMCore`
narrates → `llm_response_ready` → GUI/Textual display it. `_on_action_detected` and
`_on_item_interaction_detected` (see "Items and movement as intents" below) both also call
`_publish_party_status`, which re-publishes `party_status_changed {"entities": self.entities}`
so `GUICore`'s Party tab redraws after anything that could have changed a party member's own
HP/equipment/inventory/conditions.

Inside `DMCore._on_action_detected`:
1. Resolves the acting skill's ability (weapon/spell/technique/innate) via
   `resolve_named_ability`/`select_ability_skill` if the matched name is an ability, else
   `find_attack_ability` for a bare skill.
2. If the ability has a range and the target is out of it (`is_in_range`), the action fails
   immediately with `reason = "out_of_range"` — no roll happens.
3. Otherwise resolves against `self.current_target` (see "Combat" below), or against an
   item-level `[entity.test]` target one level deeper (a container's contents or something
   already in inventory — see "Entity tests"), or with no target at all (difficulty 0).
4. On a hit, `calculate_damage` rolls damage, resolves the `bonus` field (plain number or
   `"user.<rule>"` reference into `rules.toml`), applies armor/resistance reduction and
   vulnerability bonus, and `apply_damage` applies net damage to HP.
5. `apply_damage` also calls `evaluate_statuses(entity_name, "on_damage")` (see "Status and
   conditions").

## Combat

Combat is a target being present *and* `is_hostile(target_name, player_name)` — an entity
with no `[entity.attitudes]` table defaults to neutral disposition, which still counts as
hostile/combat-ready. An entity with `supertype == "object"` is never treated as hostile
regardless of attitude data. `is_hostile(entity, player_name)` distinguishes enemies (attack
the player) from allies (attack `self.current_target` instead, if they carry their own
`[[entity.behavior]]` data).

If in combat, `round_number` increments and the round publishes as `round_resolved` (one
narration per round). Otherwise it publishes as `action_resolved` (one narration per skill
use) — the path a dialogue check against a friendly NPC also takes.

A `round_resolved` payload carries the player's own resolved action plus `"turns"`: every
other living scene entity's own resolved action via `resolve_behavior_action`
(`DM_Combat.py`), driven by each entity's `[[entity.behavior]]` table — a declaration-order
list of `{requirements, action}` entries, matched top-down (requirements compared the same
way `[[status]]` requirements are — see "Status and conditions"). `turns` is sorted by
initiative: `roll_initiative(entity_name)` pools every skill named in `rules.toml`'s
`[[initiative]]` list, rolling once per round; an entity lacking a listed skill defaults to
untrained (0D/0 pips). Initiative only orders narration — every actor resolves independently
against state as of the start of the round, not sequentially. `current_target` only advances
(to the next living hostile entity, or the first living non-player entity if none is hostile)
once, at the end of the round, if it died.

A behavior entry's `action` is either an ability name or one of two reserved movement words,
`"advance"`/`"retreat"` (`MOVEMENT_ACTIONS`, `DM_Combat.py`), routed to `move_toward_or_away`
instead of an ability lookup. An explicit `"retreat"` entry is how a creature values its own
life — checked ahead of its attack entry, ex: `creatures.toml`'s wolf/giant spider/bandit flee
once `hp_per_remain` drops under 0.40 (the same cutoff `rules.toml`'s `"wounded"` tier bottoms
out at); an undead/construct entity has no such entry and fights on regardless. Separately,
`resolve_behavior_action` falls back to `"advance"` on its own whenever its chosen action
can't currently reach its target (`is_in_range`) — closing distance instead of standing idle.

## Challenge rating

`Challenge_Rating.py` is a pure, DMCore-independent module (same "pure, entity-shape-agnostic"
precedent `Character_Creation.py` sets) computing a single number for "how powerful is this
entity," built entirely from its own dice/pips — no separately-justified weighting constants.
`skill_rating(dice, pips)` is `dice * 3 + pips`, the shared "3 pips = 1 die" scale
`DM_Combat.py`'s `get_opposing_skill`/`select_ability_skill` already rated skills on before
this module extracted it into one place. `calculate_challenge_rating(skills, max_hp,
damage_dice=0, damage_pips=0, top_n=3)` sums three components on that same scale:
- **skill** — the average `skill_rating` of the entity's `top_n` (default 3) best-*trained*
  skills, not every skill it has. A flat sum would let a character trained broadly but
  shallowly across dozens of noncombat skills (ex: a full 2D-baseline skill table) outrank a
  boss creature authored with only 2-3 trained skills.
- **hp** — `max_hp // 3`, the same `/3` scale as pips-to-dice, so a flat stat with no dice of
  its own still lands in comparable units.
- **damage** — `skill_rating` of the entity's single best damage-dealing weapon/ability's own
  `dice`/`pips` — not its `bonus` field, which can be a `rules.toml` formula reference rather
  than a flat number, and isn't "dice and pips" in the first place.

`calculate_party_challenge_rating(member_ratings)` is a plain sum, not an average — a larger
party of individually modest ratings can still outrate one strong boss.

`DM_Combat.py`'s `get_challenge_rating(entity_name)`/`get_party_challenge_rating()` are the
DMCore-touching glue (the same split `DM_CharacterCreation.py` is to `Character_Creation.py`):
`_best_damage_dice_pips` finds the best `dice`/`pips` from every equipped item plus every
resolved ability with a `damage_value` (the same candidate pool `find_attack_ability` draws
from, just not filtered to one particular skill — nothing here is about to be rolled, so
there's no skill to disambiguate by), resolving `"user.weapon.<field>"` indirection
(`resolve_weapon_reference`) the same way a real attack would. `get_party_challenge_rating`
filters through `self.scenario_entities`, not a blind `is_player`/`is_party` scan of
`self.entities` — `self.entities` also still holds every *uninstanced* template from every
loaded TOML file (ex: `characters.toml`'s `anne`, `is_party = true`, but not part of
`arena.toml`'s own entities list), and those must not count just for existing on disk.

## Movement and range

Every scenario entity — the player included — has an objective, 1-indexed `band`: a position
on the current room's (or scenario's) band line, not a distance-from-player.
`get_distance_between(a, b)` is the absolute difference between two band numbers. The player
moves via `advance_or_retreat(direction)` (`DM_Movement.py`): shifts the player's own band by
up to their `speed` (default 1) toward or away from `current_target`. A creature/ally moves
the same way via `move_toward_or_away(entity_name, opponent_name, direction)`, just relative
to whichever opponent `resolve_behavior_action` resolved for it. Either way, only the one
entity that moved has its band changed (aside from party formation, below), but because gaps
are computed from both sides' bands, one move can change its distance to every other entity
at once — not always in the expected direction, since retreating from one opponent can carry
an entity toward something else. At a zero-gap tie, "advance" is a no-op; "retreat" prefers a
higher band number, falling back to a lower one only if higher is blocked.

`move_entity`'s floor is always band 1; its ceiling is the scene's own `bands` count,
enforced only when `enclosed` is true (the default). `enclosed = false` removes the ceiling
entirely — the mechanism for fleeing a scene: once the gap to every attacker's own `range` is
exceeded, nothing can reach the fleeing entity.

**Party formation.** Every `is_party` entity carries its own `follow_offset` (int, default 0),
read by `_apply_party_formation` to snap that entity's band to `player_band + follow_offset`.
`characters.toml`'s `thane` (`follow_offset = 0`) walks abreast; `anne` (`-1`) trails one band
behind to favor her ranged spellwork. This is a flat teleport, not a speed-limited move, and
only ever fires where the *player's* band changes (`advance_or_retreat`, `enter_room`) — never
from a creature/ally's own combat-turn movement, which stays free to drift out of formation
until the player's next move snaps it back. The player can override `follow_offset` in play:
"stay behind me"/"walk beside me" resolve to `item_interaction_detected` intents
`"formation_behind"`/`"formation_abreast"` (`DMCore._resolve_formation_intent`) — a party
member's own name either is or isn't literally present in the input (whole-word,
case-insensitive), so naming one addresses only them; naming none addresses the whole party.

`range` (int, in bands) lives on the weapon/spell/ability itself, absent/`0` meaning melee —
usable only in the target's own band. A reach weapon extends that by one band; a ranged
weapon/spell reaches however far its own data says, with no accuracy difference across that
range. `is_in_range` is `True` unconditionally when `ability` is `None` (a non-physical check).

## Character creation

Race/point-buy skill dice, applied to the player entity once, before any scenario loads.
`Character_Creation.py` is pure, UI- and DMCore-independent logic — its own
`load_character_creation_data(rules_dir)` re-scans `Rules/Fantasy/*.toml` for `[[skill]]`,
`[[race]]` (`races.toml`), and `rules.toml`'s `[character_creation]` table directly (its own
independent path resolution, same precedent `LLMCore._save_slot_dir` sets), since a character
has to be buildable *before* a `DMCore` exists to read that data off of. `[character_creation]`
holds `pool_dice` (15 — free dice to spend across skills) and `max_allocation_per_skill` (5).
Each race (`races.toml`) is its own complete, *absolute* `[race.skill_dice]` table, one entry
per skill (human included — no implicit "base_dice" default). `race_baseline_skills` reads a
skill's value off the race's table, floored at 0, falling back to `UNTRAINED_DICE` (0) if a
skill is missing from a race's own table. `elf`/`dwarf`/`half-orc`/`halfling` each raise four
skills to 3D and lower four others to 1D around the 2D baseline, netting even before any
allocation is spent. `validate_allocation` rejects an unknown skill, a negative entry,
anything over the per-skill cap, or a total that isn't *exactly* `pool_dice`;
`build_character_skills` is baseline + allocation for every skill.

`DM_CharacterCreation.py`'s `apply_character_creation(character)` — `character` being
`{"race", "allocation", "name"}` — is the one piece that touches `DMCore` state, called from
`DMCore.__init__` right after `_resolve_player_name()` and before any scenario loads.
`"allocation"`, if non-empty, is validated and overwrites `self.entities[self.player_name]
["skills"]` entirely and updates `qualities.race`; if absent/empty, the skill/race override is
skipped and the template's own hand-authored skills are left untouched (this is what lets
`LLDM.py`'s CLI quick-boot pass a bare `{"name": ...}` through this same method rather than
needing a separate rename-only path). `"name"`, if non-blank and different from the current
`player_name`, renames the player entity: `self.entities[self.player_name]` is popped and
re-inserted under the new key, and `self.player_name` repoints at it — so
`_instance_entities`' `"player"` placeholder (see "Scenarios and rooms") resolves to the new
name from the first scene onward. A name colliding with any other already-loaded entity is
rejected outright (`log_error`, not raised — the skill/race override still applies even when
the rename is rejected). Renaming doesn't touch any *other* entity's own
`[[entity.attitudes.name]]` override keyed to the old literal name — a renamed character just
falls back to that entity's `default` disposition, same as any name the override doesn't list.
`character=None` (every caller that omits it) is a complete no-op — `characters.toml`'s
`gladstone` stays exactly as-is; this system never retrofits existing NPCs/creatures.

`Character_Creation_GUI.py`'s `CharacterCreationDialog` (a modal `Toplevel`, same
`grab_set`/blocking-`wait_window` pattern `GUICore.request_load` uses) is the interactive
front end: an optional name field, a race dropdown, a per-skill allocation row, and a "dice
remaining" counter gating Create until it hits exactly zero. `self.result` is always
`{"race", "allocation", "name"}` once Create is pressed, or `None` if cancelled.
`GUICore.request_character_creation` runs this and, only when not cancelled, publishes
`"character_created"` — see "Booting the game" for what happens with the result.

## Booting the game

`LLDM.py`'s `main()` never constructs `DMCore` unconditionally — no scenario loads and
nothing is narrated until a player character *and* a chosen scenario exist, via whichever
route fires first:

1. **CLI quick-boot** — `python LLDM.py <scenario> [character_name]`. Giving `scenario` skips
   the Character menu entirely and constructs `DMCore` immediately; `character_name`, if also
   given, is passed as `{"name": character_name}` (a rename, skills untouched). Omitting
   `scenario` leaves the window open with nothing loaded, for routes 2/3 below.
2. **Character → Create... then Scenario → Load...** — a non-cancelled dialog result publishes
   `"character_created"` (see GUI_Core.py's own notes above), which `main()`'s
   `on_character_created` closure only logs a warning for (if `DMCore` already exists) — it
   doesn't construct anything. `GUICore` itself stashes the character and unlocks Scenario →
   Load...; picking a scenario from that popup publishes `"scenario_selected"
   {"scenario_name", "character"}`, which `main()`'s own `on_scenario_selected` closure reacts
   to (not `DMCore` — nothing exists yet to subscribe) by constructing
   `DMCore(scenario_name=..., character=...)`.
3. **File → Load...** — `GUICore.request_load` publishes `"load_requested"`. Before any
   `DMCore` exists, `main()`'s own `on_load_requested` closure handles this instead of
   `DMCore`'s usual `_on_load_requested`: it peeks the chosen slot's `dm_state.json` for its
   `"scenario_key"` (`LLDM._peek_saved_scenario_key` — a plain file read, no live `DMCore`
   needed), constructs `DMCore` against that scenario, then calls `dm_core.load_game(slot)` to
   overlay the rest of the saved state. This costs one throwaway `scenario_loaded` narration
   before `load_game`'s own `"game_loaded"` corrects it — the same double-narration cost as
   loading a save immediately after any ordinary new game, not a cost unique to this path.

`on_character_created`/`on_scenario_selected` no-op (the former with a logged warning) once
`DMCore` already exists — Create... (and, downstream of it, Scenario → Load...) only ever
starts the *first* game a session has; `GUICore`'s own `_on_game_started` (see above) is what
keeps Scenario → Load... from even being reachable again by that point. `on_load_requested`
no-ops silently instead, since File → Load... is meaningful at any time: every load after the
first is handled solely by `DMCore`'s own `_on_load_requested`, subscribed during its
`__init__` as always.

## Scenarios and rooms

`Rules/Fantasy/scenarios/*.toml` (`arena`, `tavern`, `field`, `dungeon`, `crypt`, plus
`character_test` — see "Testing") each hold one `[scenario]` table, kept in their own
subdirectory so multiple scenarios can coexist without the flat `load_rules` scan (which only
keeps the last `[scenario]` table it reads) overwriting one with another.

A scenario is either a **plain single room** (entities listed directly under `[scenario]`) or
a **multi-room dungeon** (`crypt`): one or more `[[room]]` tables, each with its own
`entities`/`bands`/`enclosed` plus `[[room.exit]]` sub-tables (`{band, direction, destination,
arrival_band}`), and `[scenario].start_room` naming the starting room. A room's own `entities`
list never includes the player — only room-local creatures/traps/chests; the player (and
anything meant to persist across the whole dungeon) is listed once at the scenario's top
level. `self.rooms` stays empty for a plain scenario, which is what lets
`load_scenario`/`enter_room` branch on room-graph vs. flat behavior without a separate flag.

**The player is referenced generically, never by a specific character's literal name.** Every
scenario/room's `entities` list names the player with the reserved sentinel `"player"`
(`DM_Rules.py`'s `PLAYER_PLACEHOLDER`), never a real template name like `"gladstone"`.
`_instance_entities` resolves it to `self.player_name` before the template lookup, so a
scenario keeps working regardless of which template is `is_player = true` or what a
freshly-created character was renamed to.

`DMCore.__init__(event_bus, scenario_name="arena")` picks which file loads via
`load_scenario_definition`, which raises `FileNotFoundError` for an unknown name (fatal on
purpose — an empty `self.scenario` would let the LLM hallucinate an opening scene with no real
content). `load_scenario()` deep-copies each named template into an independent instance,
tags it with its starting `band`, disambiguates duplicates (`wolf`, `wolf_2`, ...), and gives
each instance its own `entity_id`.

`enter_room(room_key, arrival_band)` — the only caller is
`DMCore._resolve_room_transition_intent`, gated on the current room declaring a matching exit
at the player's own band and on no living hostile remaining in the room. Moves only the
player's band; HP/inventory/currency/conditions carry over. A room visited before is restored
from `self.visited_rooms` rather than re-instanced, so a cleared trap, dead creature, or
looted chest stays that way on revisit.

**`self.entities` holds templates and live instances under the same keys** — instancing a
single-occurrence entity overwrites its template slot. `load_game` re-runs `load_rules()`
before re-instancing for this reason (see "Saving and loading").

## Status and conditions

`rules.toml`'s `[[status]]` table drives derived conditions. Each entry has:
- `trigger` — when to evaluate it; only `"on_damage"` is wired today, called from both
  `apply_damage` and `apply_healing` (see "Damage and healing" below).
- `requirements` — a list of `{field, operator, value}` comparisons (`COMPARATORS` in
  `DM_Status.py`: `>`, `<`, `>=`, `<=`, `==`, `!=`, `in`, `not_in`), ALL of which must hold.
  `field` is either derived (`"hp_per_remain"`) or a direct entity attribute.
- `apply` — `{condition, duration, dismiss}`, naming an entry in `[[condition]]`.

`entity_matches_requirements`/`get_comparable_value` are the shared engine behind both
`[[status]]`'s own requirements and `[[entity.behavior]]`'s; an optional `opponent_name` param
resolves the one opponent-relative derived field, `"distance_to_target"` (the band gap to
`opponent_name`) — used by a creature choosing *between* attack options by range, ex:
`creatures.toml`'s `bandit` favors its `short bow` while `distance_to_target > 0`, falling to
its `rusty shortsword` once that gap closes to 0.

`evaluate_statuses` finds every status matching a trigger whose requirements the entity
currently meets and calls `apply_condition`, storing it in `entity["active_conditions"]`
(seeded from the template's own `[entity.conditions]`). `dismiss_condition(entity_name,
condition_name)` is the general-purpose removal primitive.

`evaluate_statuses` also sweeps the *other* direction: after applying whatever matches now, it
dismisses any active condition (from the same trigger) whose requirements no longer hold —
ex: healing back above a "wounded" tier's hp_per_remain range dismisses "wounded" in the same
call. A condition is only eligible for this sweep if stored with a falsy `dismiss` — one
stored with a named mechanism (ex: `"dead"`'s `dismiss = "resurrection"`) is left alone, so
ordinary healing can't revive a dead entity through the same path that clears a wound tier.

## Damage and healing

`apply_damage` subtracts HP (floored at 0) and calls `evaluate_statuses(entity_name, "on_damage")`.
`apply_healing` adds HP (clamped at `max_hp`) and calls the same `evaluate_statuses("on_damage")` —
not to apply a *new* injury (healing only raises hp_per_remain, so no worse tier can newly
match) but so a wound tier's condition that no longer holds after the heal gets dismissed by
the stale-condition sweep above.

Nothing automatically re-evaluates a status-driven condition once its requirements stop
holding outside of `apply_damage`/`apply_healing`'s own calls.

## Entity tests

A `[entity.test]` block is a skill check against an entity itself (ex: `items.toml`'s `chest`
lock, `cursed dagger`'s curse-identification check; see `Rules/Fantasy/reference/
entity_schema.toml` for every field it and every other entity table can carry).
`is_test_available(target, test, skill_name)` gates it: `skill_name` must be in `test["skill"]`;
`requires_condition` (if set) must currently be active; `blocks_if_condition` (if set) must
not be. A skill not in `test["skill"]` isn't blocked — it just isn't a test, and falls through
to ordinary opposed-skill resolution instead.

A scene-level test (the target itself, via `self.current_target`) is resolved as a flat
difficulty check (`resolve_action`), not through `resolve_opposed_action`.
`_resolve_item_test_target`/`_resolve_item_test` handle the same mechanism one level deeper —
an item already in the player's inventory, or sitting in a reachable container — tried before
combat-target redirection so inspecting an item never becomes an attack.

`apply_test_outcome(entity_name, outcome)` dispatches on whichever keys are present in the
matched `pass`/`fail` table: `dismiss_condition` removes a condition, `condition` applies a
new one, `loot` transfers everything on the target via `loot_entity`, and `reveal` (truthy)
applies a permanent `"identified"` condition — the content it reveals is read back off the
entity's own `tags` field by whoever narrates it, not stored on the outcome itself.

## Inventory and currency

- **`transfer_currency(from_name, to_name, amount=None)`** — moves currency; `amount=None`
  moves all of it; clamps to what's available; no-ops on a missing entity.
- **`transfer_item(from_name, to_name, item_name)`** — moves one matching `inventory` entry;
  duplicates represent quantity, so callers loop for more than one.
- **`loot_entity(from_name, to_name)`** — sweeps all currency plus every inventory item.

## Items and movement as intents

Looking at, taking, giving, trading, opening, closing, using, equipping, dropping, moving
between rooms, and directing the party's own formation all bypass the skill/dice system
entirely — none of them warrant a roll. `NLPCore._detect_item_intent` recognizes phrase-level
keywords for thirteen intents before skill matching runs: `examine`, `equip`
(`equip`/`wear`/`wield`/`put on`), `unequip` (`unequip`/`take off` — deliberately not a
broader `remove`, which would collide with items.toml's own trap names and finesse's
`disarm`/`trap` keywords), `drop` (`drop`/`discard`/`put down`), `take`, `give`, `trade`,
`open`, `close`, `use` (currently `drink`/`quaff`), `formation_behind`/`formation_abreast`
(see "Party formation" above), and direction/movement phrases for `advance`/`retreat`/`move`.
`open`/`close`/`advance`/`retreat`/`formation_*`/`move` act on the current scene target, the
whole scene, or (for formation) whichever party member the input names, publishing
`item_interaction_detected` with `item_name: None`; every other intent runs through
`NLPCore.map_to_item`, an embedding match against every `supertype == "object"` entity's
name/description (currency is checked first as a fixed synonym list, returning the sentinel
`"currency"`).

`DMCore._on_item_interaction_detected` resolves with zero dice rolls:
- `"equip"`/`"unequip"`/`"drop"` are checked first, since none care about target_name/the
  locked gate below at all.
  - `_resolve_equip_intent` moves an item already in inventory into whichever
    `[entity.equipped]` slot its own `equip_slot` field resolves to for the player's
    supertype/subtype (`rules.toml`'s `[[equip_slot]]` via `get_equip_slots`). Denied
    `"not_present"`/`"not_equippable"`/`"cant_equip"` as appropriate. An item already sitting
    in the chosen slot is displaced (still in inventory) rather than refusing.
  - `_resolve_unequip_intent` only clears the slot mapping — denied `"not_equipped"` if it
    isn't equipped at all.
  - `_resolve_drop_intent` unequips if needed, then moves the item onto the current
    room/scene's own ground (`_current_ground_items`). **Known gap:** unlike
    `scenario_entities`, nothing in `"ground"` is saved/restored yet, so a drop since the last
    save doesn't survive a save/load round trip.
  - A later `"examine"`/`"take"` aimed at a ground item is resolved by `_resolve_ground_intent`
    before falling through to the ordinary target-based path below.
- A locked container denies everything (`reason: "locked"`).
- `item_name` equal to the current target's own name addresses the target itself, not
  something inside it.
- A closed (but unlocked) container denies reaching its contents (`reason: "closed"`) while
  still allowing examine/open.
- `"take"`/`"trade"` move an item to the player; `"give"` moves one to the target; `"trade"`
  additionally charges the item's TOML `value` (`reason: "cant_afford"` if unaffordable).
- `_resolve_open_close_intent` is gated to `subtype == "container"`; toggles `"closed"`,
  independent of `"locked"` — a picked lock still needs its own `"open"`. A successful open
  attaches `contents`: one flavor-description string per item inside.
- `_resolve_use_intent` activates/consumes an item, gated on a truthy `usable` field. The
  only effect implemented is healing (`healing = {dice, pips}`, rolled through
  `apply_healing`); using an item also applies a permanent `"identified"` condition.
  Consumption is charge-based (`_consume_charge`): no `charges` field means single-use; at
  zero charges the item is replaced by `replace_with` or simply removed.
- `_resolve_room_transition_intent` handles `"move"` (see "Scenarios and rooms").

Publishes `item_interaction_resolved` either way, with enough detail (`found`,
`reason`/`description`/`container`/`amount`/`price`/`contents`/`healed`/`charges_left`/
`replaced_with`/`slot`/`replaced` as applicable) for narration to explain a miss or a success.

## Social and attitudes

`get_attitude(entity, toward)` returns a six-value array (`disposition, trust, confidence,
respect, obligation, intimacy`, nominally -100..100; a `name` override beats `supertype` beats
`default`; no `[entity.attitudes]` table defaults to all-neutral). `get_attitude_tier(value)`
clamps to `[-150, 150]` and returns the first of seven `[[attitude_tier]]` bands whose range
contains it, in declaration order. `describe_attitude(entity, toward)` renders all six axes
as one sentence using each tier's own phrase per axis.

`describe_character(entity_name, toward_name=None)` builds a flavor-text roster line from
purely descriptive TOML fields (`description`, `qualities`, `memories`, `quotes`) plus, when
`toward_name` is given, the attitude sentence above — deliberately excluding mechanical data.
`DMCore.__init__` builds this roster into the `scenario_loaded` payload; `_on_action_detected`
also attaches a fresh `result["defender_details"]` per action.

`self.player_name` is resolved once in `__init__` via `_resolve_player_name()`, which scans
loaded templates for the one with `is_player = true` and raises `ValueError` if none is marked.

## Narration

`LLMCore` subscribes to narration-relevant events, sharing outcome-text building
(`_describe_outcome`) and background-fetch plumbing (`_queue_narration`):
- `scenario_loaded` → `generate_scene_intro` — once, from `DMCore.__init__`.
- `round_resolved` → `generate_round_response` — combat, once per round.
- `action_resolved` → `generate_response` — non-combat, once per skill use.
- `action_not_understood` → `generate_clarification_response` — acknowledges input that
  didn't resolve to any action.
- `item_interaction_resolved` → `generate_item_interaction_response` — covers examine/take/
  give/trade/open/close/use/equip/unequip/drop and room transitions.
- `game_load_failed` → `generate_load_failed_response`.

The scenario/room setting and character roster are re-injected into the system message on
every request, so narration stays grounded even after the intro scrolls out of the rolling
100-message `context_window`.

Every `_queue_narration` call's background fetch also publishes `llm_debug_updated
{"query", "response"}` alongside `llm_response_ready` — consumed only by `GUICore`'s Debug
tab, never stored in `context_window` itself.

## Saving and loading

Three sibling JSON files per slot — `Saves/<slot>/dm_state.json`, `llm_state.json`,
`gui_state.json` — written/read independently by `DMCore`, `LLMCore`, and `GUICore`. `EventBus`
has no request/response mechanism, so each core owns and persists its own slice.

**Trigger:** `save_requested`/`load_requested {"slot": slot_name}`, published by
`NLPCore._detect_save_load_intent`, by `GUICore`'s File → Save... / Character → Load... popups
(see "Booting the game" for the cold-start case), or by `Textual_Core`'s Save/Load buttons.

`DMCore.save_game` writes a diff from a fresh instantiation: `scenario_key`, `player_name`,
`round_number`, `current_room_key`, `scenario_entities`, and per-instance
`{hp, active_conditions, currency, inventory, band}`. `load_game` re-runs `load_rules()`, then
the same scenario-load path `__init__` uses, then overlays each saved instance's mutable
fields; a saved instance with no post-reload match is skipped. Publishes `game_loaded` on
success (not `scenario_loaded`, which would re-narrate an opening scene) or `game_load_failed
{"slot", "reason"}` on failure, then re-publishes `party_status_changed`.

`LLMCore.save_game`/`load_game` persist/restore `context_window` plus scenario name/
description/characters; loading is silent. `GUICore.save_game`/`load_game` persist/restore the
Notes tab's free text, same way.

Slot names are run through `os.path.basename` before use, so a slot can't escape `Saves/`.

## Tags vs. conditions

- **Tags** are static classification data, fixed for an entity's lifetime: `damage_tags`/
  `armor_tags`, `resistance_value`/`resistance_tags` (rolled, partial reduction via
  `get_damage_reduction`), `immunity_tags` (absolute — `is_immune_to` zeroes net damage
  regardless of roll), and `vulnerability_value`/`vulnerability_tags` (rolled, extra damage
  added before reduction). Immunity wins outright over vulnerability if both match.
- **Conditions** (`active_conditions`, `apply_condition`/`dismiss_condition`) are dynamic —
  gained/lost during play via triggers or tests. Use a condition for something that can
  plausibly change mid-scene; use a tag for something permanent to what the entity is.

`abilities` is a flat list, each entry either a plain string naming a shared catalog entity
(`spells.toml`/`techniques.toml`) or an inline table for a one-off innate ability. `techniques.
toml`'s `cleave` exercises a multi-skill `skill = [...]` list and weapon-scaled damage
(`"user.weapon.dice"`/`"user.weapon.pips"`); see `ability_matches_skill`,
`resolve_weapon_reference`, `resolve_damage_value` in `DM_Combat.py`. Naming a technique/spell
directly in input can resolve it via `map_to_action` before a bare skill would.

## Data/TOML conventions

- `Rules/Fantasy/reference/entity_schema.toml` catalogs every field the engine reads off an
  entity. Reference/documentation only, never loaded as game data (`load_rules` only
  `os.listdir()`s the top level of `Rules/Fantasy/`, one directory shallower).
- `load_rules` special-cases only `skill` and `entity` top-level keys; everything else in any
  flat `Rules/Fantasy/*.toml` file lands generically in `self.rules[key]`.
- `[entity.attitudes]` is `{default, name, supertype}`; `name`/`supertype` are TOML
  arrays-of-one-key-tables — `get_attitude` loops over the list checking `if toward_name in
  override`.
- `damage_value = {dice, pips, bonus}` — `bonus` is a flat number or `"user.<rule_name>"`,
  resolved via `resolve_bonus`. String `dice`/`pips` are not resolved and degrade to 0.
- `load_rules`'s per-file exception handling means a malformed TOML file fails quietly — a
  parse error loads that file with less data than expected, not a crash.

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

The RAG query is the player's own raw input, not the full instruction-padded narration prompt
— embedding the padded prompt dilutes similarity enough to miss lore a bare-input query would
find. `generate_scene_intro` passes the scenario name+description instead (no player input
exists yet); `generate_load_failed_response` falls back to its own full prompt.

`vectorize_pdf.py` is a standalone CLI that builds this same cache ahead of time:
`python vectorize_pdf.py [pdf_or_dir] [--query "..."]`, defaulting to `Settings/Fantasy/`.
Reuses `RagIndex` directly via `RagIndex.wait_until_ready()`.

## Textual mirror (headless testing)

`Textual_Core.py` subscribes to the same events `GUI_Core` displays and adds its own `Input`
widget publishing `user_input_submitted`, so the app can be driven and asserted on headlessly
(`app.run_test()`/`Pilot`) without Tkinter or a display.

Practical constraints when touching this file:
1. Don't name an attribute `self._ready` — Textual's `App` reserves that name internally.
2. Pre-mount events (`DMCore` publishes `rules_loaded` synchronously during `__init__`, which
   can precede `compose()`) are buffered and flushed in `on_mount`.
3. `RichLog.lines` only reflects content once its tab is active — activate it
   (`tabbed_content.active = "tab_id"`, then `await pilot.pause()`) before reading a
   background tab.
4. Writes can arrive from a foreign thread (`LLMCore`'s background fetch). `call_safely` wraps
   everything through `self.call_from_thread`, falling back to a direct call.
5. Pilot has no `.type()` in the installed Textual version (8.2.8) — build a key list
   (`["space" if c == " " else c for c in text]`) and pass it to `pilot.press(*keys)`.
6. Joining a background thread from an `async def` must go through
   `await asyncio.to_thread(thread.join)`, not a bare `t.join()`, or the event loop deadlocks.

## Testing

- **`test_unit.py`** — offline `unittest.TestCase` classes, kept deliberately lean: one
  representative test per genuinely distinct mechanism/branch, not one per edge case or per
  flavor variant of an already-covered code path (ex: one hidden-hazard notice-roll test
  stands in for both the dart trap and the scythe trap). `TestGameBoot` and
  `TestNlpConfidenceThreshold` load the real `sentence-transformers` model via `setUpClass`.
  Most other classes share fixture setup via `DMTestCase` (`scenario_name` class attribute,
  plus `_capture`/`_capture_any` helpers) and `LLMTestCase`. `TestCharacterCreationRename`
  covers the "player" placeholder and the rename path against
  `Rules/Fantasy/scenarios/character_test.toml` — a minimal scenario built solely for this,
  kept separate from the real gameplay scenarios.
- **`test_integration.py`** — every test needing a real, running LM Studio, gated on
  `_lm_studio_reachable()` so they skip together when nothing's listening on
  `127.0.0.1:1234`. `_LivePipelineTestCase`'s own optional `character` class attribute is
  forwarded straight into `DMCore`'s `character` param — used by
  `TestCreatedCharacterConversation` to check real narration/combat work end-to-end against a
  custom-named, custom-race character, not just `characters.toml`'s `gladstone`.

`python -m pytest -q` runs both files; `python -m pytest -q test_unit.py` runs the fast,
offline subset only.

## Known gaps

- `NLP_Core.py` — a keyword-driven skill match can still dominate an unrelated whole-sentence
  embedding match (ex: "identify the dagger" resolves to the wrong skill); no multi-instance
  disambiguation (ex: "the wounded wolf" vs. "the other wolf").
- Dropped items (`"ground"`) aren't saved/restored across a save/load round trip.

## Extended goals

Not yet started; no code or TODO markers exist for these:
- Characters are language-dependent — an entity's own comprehension of the language it's
  addressed in should gate dialogue/narration, not just its attitude data.
- Dialogue sentiment sways attitudes — the sentiment of what the player says, not just which
  skill check they made, should be able to move an entity's `[entity.attitudes]` axes.
- Actions sway attitudes by varying degrees — a resolved action (combat, theft, a favor)
  should nudge attitude axes proportionally, not just be gated by attitude that already exists.
- Random encounters, enemy generator — procedurally populate a scene/room with creatures
  instead of every encounter being scenario-authored.
- Scenario, quest, NPC, item, and location generators — procedurally author the TOML data
  itself rather than every scenario/entity being hand-written.
- A 'dungeon master' persona the LLM can speak directly to the player as.
- Tools that the LLM may call to directly interact with the scene.
