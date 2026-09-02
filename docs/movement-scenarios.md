# LLDM — Movement, Scenarios, and Random Encounters

Part of the [LLDM](../CLAUDE.md) docs — bands/range, locations/rooms, encounter tables.

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

**Gridded locations skip this graph entirely.** An optional `grid = {x, y}` field on a
`[[location]]` — see `docs/downtime.md`'s "Travel" — replaces its own `[[location.exit]]`/
`return_to` graph with pure grid connectivity: any *known* location (`DMCore.known_locations`) is
reachable directly by name/alias, distance/time cost computed from grid coordinates and party
travel speed. `_resolve_travel_intent` branches on the *current* location's own `grid` field before
ever touching `_resolve_location_exit` below; a non-gridded location's exits are completely
unaffected either way.

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

