# LLDM

An autonomous dungeon master: the player types free-text actions, NLP maps them to a skill,
a simplified D6 (West End Games) engine rolls dice and resolves outcomes, and a local LLM
(currently Gemma via LM Studio at `http://127.0.0.1:1234`) narrates what happened. Skills,
entities, items, spells, rules, and scenarios are all data-driven via TOML in `Rules/Fantasy/`.

## Architecture

Six modules wired through `Event_Bus.py`, a synchronous pub/sub bus (`publish` calls every
subscriber immediately, in whatever thread called `publish`). `LLDM.py` boots them in this
order: `NLPCore`, `LLMCore`, `GUICore`, then `DMCore` last (it publishes `rules_loaded` during
`__init__`, so everything that needs it must already be subscribed).

- **`DM_Core.py`** — `DMCore`'s `__init__` plus its two event handlers
  (`_on_action_detected`, `_on_item_interaction_detected`) and their direct helpers: the
  orchestration that spans every domain mixin. The class itself is composed from sibling
  mixin files, each owning one concern: `DM_Rules.py` (TOML/scenario/room loading),
  `DM_Combat.py` (dice rolling, opposed checks, damage, ability/behavior resolution),
  `DM_Status.py` (statuses/conditions, entity tests), `DM_Inventory.py`
  (currency/item transfer), `DM_Social.py` (attitudes, character description),
  `DM_Movement.py` (bands, range), and `DM_Persistence.py` (save/load). Python's MRO flattens
  every mixin method onto one `DMCore` instance, so `dm_core.<method>(...)` call sites
  (including `test_unit.py`) don't care which file defines a given method.
- **`NLP_Core.py`** — `sentence-transformers` (`all-MiniLM-L6-v2`) embeds each skill's
  name/description/keywords as separate phrases, then cosine-matches player input against all
  of them. Also matches free text against item names/directions/save-load prefixes for
  non-skill intents (see "Items and movement as intents" below), which are checked before
  skill matching. Publishes `action_detected {skill, score, input}` (with an optional
  `target` field) only when the score clears `confidence_threshold` (`0.5`); below it,
  publishes `action_not_understood` instead.
- **`LLM_Core.py`** — posts to LM Studio's OpenAI-compatible `/v1/chat/completions` on a
  background thread, with a rolling 100-message context window. Subscribes to six narration
  triggers (see "Narration" below).
- **`GUI_Core.py`** — Tkinter window: history pane + tabbed Party/Notes/Map/Debug panels, plus
  a dropdown File menu (Save.../Load...) on the window's menu bar in place of always-visible
  save controls. Save opens a popup asking for a slot name; Load opens a popup listing every
  existing slot (a subdirectory of `Saves/`) to pick from. History (via `llm_response_ready`)
  and Party are wired to real data; Notes is a free-typed scratchpad persisted through its own
  save/load slice (see "Saving and loading" below). The Party tab is a `ttk.Treeview`: one
  collapsible node per party member — an entity with `is_player = true` (the player) or
  `is_party = true` (an ally, ex: `characters.toml`'s `thane` — see `entity_schema.toml`'s
  `is_party`) — labeled with its current/max HP and each expanding into its own Equipment/
  Skills/Abilities/Inventory/Conditions groups. Equipment lists every slot valid for the
  member's own supertype/subtype (per rules.toml's `[[equip_slot]]` table, resolved the same
  override-precedence way `get_attitude` resolves name/supertype/default — see "Data/TOML
  conventions" below), filled or `(empty)`, not just the ones actually occupied — any
  equipped slot that isn't on that list at all (a data mismatch `_validate_equipped_slots`
  already logs at load time) is still shown, appended after. Skills lists each
  `[entity.skills]` entry in WEG dice notation (`"blades: 5D+2"`). It redraws on `rules_loaded`
  (boot/new game) and on `party_status_changed` (DMCore's cheap post-action re-publish of
  `self.entities`/`self.rules["equip_slot"]` — see "Action resolution pipeline" below — kept
  separate from `rules_loaded` since NLPCore also rebuilds its embeddings from that event,
  which would be far too expensive per action). The Map tab is a free-form drawing canvas
  (click-drag to sketch, a small color palette, a Clear button) for the player's own scratch
  map; the engine never writes to it. The Debug tab shows exactly the most recent LLM
  exchange — the full request text (system message plus entire `context_window` sent) and the
  raw response text, overwritten (not appended) on every `llm_debug_updated` (`LLM_Core.py`'s
  `fetch_from_llm`, fired alongside `llm_response_ready` on success, or with `"[ERROR] ..."`
  as the response on a failed request) — a debugging aid, no persistence, distinct from the
  history pane's already-narrated text.
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
list of `{requirements, action}` entries, matched top-down (`requirements` compared against
derived/entity fields the same way `[[status]]` requirements are — see "Status and
conditions"). `turns` is sorted by initiative: `roll_initiative(entity_name)` pools every
skill named in `rules.toml`'s `[[initiative]]` list and rolls once per round; an entity
lacking a listed skill defaults to untrained (1D/0 pips). Initiative only orders narration —
every actor resolves independently against state as of the start of the round, not
sequentially. `current_target` only advances (to the next living hostile entity, or the
first living non-player entity if none is hostile) once, at the end of the round, if it died.

A behavior entry's `action` is either an ability name or one of two reserved movement words,
`"advance"`/`"retreat"` (`MOVEMENT_ACTIONS`, `DM_Combat.py`), routed to `move_toward_or_away`
(`DM_Movement.py`) instead of an ability lookup. An explicit `"retreat"` entry is how a
creature values its own life — checked ahead of its attack entry in the same declaration-order
list, ex: `creatures.toml`'s wolf/giant spider/bandit flee once `hp_per_remain` drops under 0.40
(the same cutoff `rules.toml`'s own `"wounded"` tier bottoms out at) rather than fight to the death;
an undead/construct entity (skeleton warrior, the bone warden, a fire elemental) has no such
entry and fights on regardless of its own HP. Separately, and needing no TOML authorship at
all, `resolve_behavior_action` falls back to `"advance"` on its own whenever the behavior it
*did* choose names an attack that can't currently reach its target (`is_in_range` — DM_Movement.py) —
closing the distance instead of standing idle out of reach.

## Movement and range

Every scenario entity — the player included — has an objective, 1-indexed `band`: a position
on the current room's (or scenario's) band line, not a distance-from-player. `get_distance_between(a, b)`
is the absolute difference between two band numbers. The player moves via
`advance_or_retreat(direction)` (`DM_Movement.py`): shifts the player's own band by up to
their `speed` (default 1) toward or away from `current_target`. A creature/ally moves the
exact same way via `move_toward_or_away(entity_name, opponent_name, direction)` — the same
distance/tie-break math (`_resolve_move_delta`), just relative to whichever opponent
`resolve_behavior_action` resolved for it instead of always `current_target` (see "Combat").
Either way, only the one entity that moved has its band changed (aside from the player's own
party — see below), but because gaps are computed from both sides' bands, that single move can
change its distance to every other entity in the scene at once — and not always in the expected
direction, since retreating from one opponent can carry an entity toward something else on the
opposite side. At a zero-gap tie, "advance" is a no-op; "retreat" prefers a higher band number,
falling back to a lower one only if higher is already blocked.

`move_entity`'s floor is always band 1. Its ceiling is the current scene's own `bands` count,
enforced only when `enclosed` is true (the default when the field is absent). `enclosed = false`
removes the ceiling entirely — the mechanism for fleeing a scene: once the gap to every
attacker's own `range` is exceeded, nothing can reach the fleeing entity.

**Party formation.** Every `is_party` entity (ex: `characters.toml`'s `thane`/`anne`) carries
its own `follow_offset` (int, defaulting to 0), read by `_apply_party_formation`
(`DM_Movement.py`) to snap that entity's own band to `player_band + follow_offset` (clamped the
same way `move_entity` clamps anything else). `thane`'s `follow_offset = 0` walks abreast (a
melee fighter has no reason to hang back); `anne`'s `follow_offset = -1` trails one band behind,
favoring her own ranged `splash flow` over standing at the front line. This is a flat teleport,
not a speed-limited move — keeping formation is bookkeeping, not an action that costs a turn —
and only ever fires where the *player's* own band changes: `advance_or_retreat` (after the
player's own move) and `enter_room` (after the player's own `arrival_band` is set), never from a
creature/ally's own combat-turn movement (`move_toward_or_away`), which stays purely tactical
and is deliberately left free to drift out of formation until the player's next move snaps it
back. `follow_offset` lives directly on the entity instance, the same mutable field every other
per-instance stat lives on, which is what lets the player override it in play: "stay behind me"/
"walk beside me" (NLPCore's `FORMATION_BEHIND_KEYWORDS`/`FORMATION_ABREAST_KEYWORDS`) resolve to
`item_interaction_detected` intents `"formation_behind"`/`"formation_abreast"`, handled by
`DMCore._resolve_formation_intent` — unlike `map_to_item`/`map_to_target`'s embedding matches, a
party member's own name either is or isn't literally present in the input (a plain
whole-word, case-insensitive search against every currently-in-scene `is_party` member), so
naming one addresses only them; naming none addresses the whole party present. The new
`follow_offset` takes effect immediately via `_apply_party_formation`, not just on the next
move.

`range` (int, in bands) lives on the weapon/spell/ability itself, absent/`0` meaning melee —
usable only in the target's own band. A reach weapon (ex: `spear`, `range = 1`) extends that
by one band; a ranged weapon or spell (ex: `long bow`, `range = 6`) reaches however far its
own data says, with no accuracy difference across that range. `is_in_range(attacker, defender,
ability)` is `True` unconditionally when `ability` is `None` (a non-physical check, ex:
`charisma`).

## Scenarios and rooms

`Rules/Fantasy/scenarios/*.toml` (`arena`, `tavern`, `field`, `dungeon`, `crypt`) each hold one
`[scenario]` table, kept in their own subdirectory so multiple scenarios can coexist as named
files without the flat `load_rules` scan (which only keeps the last `[scenario]` table it
reads) silently overwriting one with another.

A scenario is either a **plain single room** (`arena`/`tavern`/`field`/`dungeon` — entities
listed directly under `[scenario]`) or a **multi-room dungeon** (`crypt`): one or more
`[[room]]` tables, each with its own `entities`/`bands`/`enclosed` plus `[[room.exit]]`
sub-tables (`{band, direction, destination, arrival_band}`), and `[scenario].start_room`
naming the starting room. A room's own `entities` list never includes the player — only
room-local creatures/traps/chests; the player (and anything meant to persist across the whole
dungeon) is listed once at the scenario's top level. `self.rooms` stays empty for a plain
scenario, which is what lets `load_scenario`/`enter_room` branch on room-graph vs. flat
behavior without a separate flag.

`DMCore.__init__(event_bus, scenario_name="arena")` picks which file loads via
`load_scenario_definition`, which raises `FileNotFoundError` for an unknown name (fatal on
purpose — an empty `self.scenario` would otherwise let the LLM narrate and hallucinate an
opening scene with no real content). `load_scenario()` deep-copies each named template into
an independent instance, tags it with its starting `band`, disambiguates duplicates (`wolf`,
`wolf_2`, ...), and gives each instance its own `entity_id`. `LLDM.py` exposes scenario
selection as `python LLDM.py [scenario]` (default `arena`); `main()` validates the name before
constructing any cores, so a typo fails fast instead of after `NLPCore`'s ~15-20s model load.

`enter_room(room_key, arrival_band)` (`DM_Rules.py`) — the only caller is
`DMCore._resolve_room_transition_intent`, gated on the current room declaring a matching exit
at the player's own band (`_find_room_exit`) and on no living hostile remaining in the room.
Moves only the player's band; HP/inventory/currency/conditions carry over. A room visited
before is restored from `self.visited_rooms` rather than re-instanced, so a cleared trap, a
dead creature, or a looted chest stays that way on revisit; a first-time room is instanced
fresh.

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

`entity_matches_requirements`/`get_comparable_value` (`DM_Status.py`) are the shared engine
behind both `[[status]]`'s own requirements and `[[entity.behavior]]`'s (see "Combat"); an
optional `opponent_name` param, forwarded from `choose_behavior`, resolves an
opponent-relative derived field — currently just `"distance_to_target"` (the band gap to
`opponent_name`, `None` — never matching — with no opponent given, which is always the case
for a status's own requirements). The implicit out-of-range fallback below already covers the
common single-attack case without it; this is for a creature choosing *between* more than one
attack option by range instead — `creatures.toml`'s `bandit` (in the `field` scenario) is the
shipped example: it favors its `short bow` while `distance_to_target > 0`, falling to its
`rusty shortsword` once that gap closes to 0, both named directly in its own `abilities` list
since they're real `items.toml` entities, not inline duplicates (see `resolve_ability`).

`evaluate_statuses` finds every status matching a trigger whose requirements the entity
currently meets and calls `apply_condition`, storing it in `entity["active_conditions"]`.
Every instance has `active_conditions` present from creation (seeded from the template's own
`[entity.conditions]`, empty if none declared). `dismiss_condition(entity_name, condition_name)`
is the general-purpose removal primitive.

`evaluate_statuses` also sweeps the *other* direction: after applying whatever matches now, it
walks every status definition sharing the same trigger and, for each one whose own `apply.condition`
is currently active on the entity but whose `requirements` no longer hold, dismisses it — ex: a
`gladstone` hurt from "wounded" (0.40-0.59 hp_per_remain) down into "incapacitated" (0.10-0.19) has
"wounded" dismissed in the same call, and one healed back above 0.59 has it dismissed too (see
"Damage and healing"). A condition is only eligible for this sweep if it was stored with a falsy
`dismiss` — one stored with a named mechanism (ex: `"dead"`'s `dismiss = "resurrection"`) is left
alone, so ordinary healing can't revive a dead entity through the same path that clears a wound tier.

## Damage and healing

`apply_damage` subtracts HP (floored at 0) and calls `evaluate_statuses(entity_name, "on_damage")`.
`apply_healing` adds HP (clamped at `max_hp`) and calls the same `evaluate_statuses("on_damage")` —
not to apply a *new* injury (healing only ever raises `hp_per_remain`, so no worse tier can newly
match) but so a wound tier's condition that no longer holds after the heal gets dismissed by
`evaluate_statuses`' own stale-condition sweep, above.

Nothing automatically re-evaluates and dismisses a status-driven condition once its
requirements stop holding (ex: a healed entity keeps a `wounded` condition applied earlier).

## Entity tests

A `[entity.test]` block is a skill check against an entity itself (ex: `items.toml`'s `chest`
lock, `cursed dagger`'s curse-identification check; see `Rules/Fantasy/reference/
entity_schema.toml` for every field it and every other entity table can carry).
`is_test_available(target, test, skill_name)` gates it: `skill_name` must be in `test["skill"]`;
`requires_condition` (if set) must currently be active; `blocks_if_condition` (if set) must
not be. A skill not in `test["skill"]` isn't blocked — it just isn't a test, and falls through
to ordinary opposed-skill resolution instead.

A scene-level test (the target itself, via `self.current_target`) is resolved as a flat
difficulty check (`resolve_action(player, skill, test["difficulty"])`), not through
`resolve_opposed_action`. `_resolve_item_test_target`/`_resolve_item_test` (`DM_Core.py`)
handle the same mechanism one level deeper — an item already in the player's inventory, or
sitting in a reachable (unlocked, open) container — tried before combat-target redirection so
inspecting an item never becomes an attack.

`apply_test_outcome(entity_name, outcome)` dispatches on whichever keys are present in the
matched `pass`/`fail` table: `dismiss_condition` removes a condition, `condition` applies a
new one, `loot` transfers everything on the target to the player via `loot_entity`, and
`reveal` (truthy) applies a permanent `"identified"` condition — the content it reveals is
read back off the entity's own `tags` field by whoever narrates it, not stored on the outcome
itself.

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
keywords (not bare words, to avoid misfiring on ordinary skill phrasing) for thirteen intents
before skill matching runs: `examine`, `equip` (`equip`/`wear`/`wield`/`put on`), `unequip`
(`unequip`/`take off` — deliberately not a broader `remove`, which would collide with
items.toml's own `"dart trap"`/`"scythe trap"` names and finesse's `disarm`/`trap` keywords),
`drop` (`drop`/`discard`/`put down`), `take`, `give`, `trade`, `open`, `close`, `use`
(currently `drink`/`quaff`), `formation_behind`/`formation_abreast` (see "Party formation" —
`stay behind`/`walk beside` and similar phrasings), and direction/movement phrases
(`DIRECTION_PHRASES`) for `advance`/`retreat`/`move`. `open`/`close`/`advance`/`retreat`/
`formation_behind`/`formation_abreast`/`move` act on the current scene target, the whole
scene, or (for formation) whichever party member the raw input names, publishing
`item_interaction_detected` with `item_name: None`; every other intent runs through
`NLPCore.map_to_item`, an embedding match against every `supertype == "object"` entity's
name/description (currency is checked first as a fixed synonym list — `gold`/`coin`/
`currency`/`money` — returning the sentinel `"currency"`).

`DMCore._on_item_interaction_detected` resolves with zero dice rolls:
- `"equip"`/`"unequip"`/`"drop"` are checked first, since none of them care about
  target_name/the locked gate below at all — same reasoning `"use"` already follows (gear has
  to already be in the player's own inventory).
  - `_resolve_equip_intent` (`InventoryMixin.equip_item`/`resolve_equip_slot`,
    `DM_Inventory.py`) moves an item already in the player's own inventory into whichever
    `[entity.equipped]` slot its own `equip_slot` field (items.toml — a single slot name, or a
    list of candidates) resolves to for the player's own supertype/subtype (`rules.toml`'s
    `[[equip_slot]]` table via `get_equip_slots`, `DM_Rules.py`). Denied `"not_present"` if
    it's not in inventory at all, `"not_equippable"` if it declares no `equip_slot`,
    `"cant_equip"` if none of its candidates are valid for the player. An item already sitting
    in the chosen slot is displaced (still in inventory, just unmapped) rather than refusing —
    the common "equip X" convention of implicitly swapping gear.
  - `_resolve_unequip_intent` (`InventoryMixin.unequip_item`) only clears the slot mapping —
    the item was always still in inventory either way — denied `"not_equipped"` if it isn't
    equipped at all.
  - `_resolve_drop_intent` unequips the item if needed, then moves it out of inventory onto
    the current room/scene's own ground (`_current_ground_items` — a `"ground"` list kept on
    the current room dict for a multi-room dungeon, or the flat scenario dict otherwise;
    created empty on first use, never authored in TOML). **Known gap:** unlike
    `scenario_entities`, nothing in `"ground"` is written to or restored from a save slot yet
    (see "Saving and loading" below), so a drop made since the last save doesn't survive a
    save/load round trip.
  - A later `"examine"`/`"take"` aimed at an item sitting in `_current_ground_items()` is
    resolved by `_resolve_ground_intent` — also checked ahead of target_name/the locked gate,
    since a dropped item has no container guarding it — before falling through to the
    ordinary target-based path below for everything else.
- A locked container denies everything (`reason: "locked"`).
- `item_name` equal to the current target's own name addresses the target itself, not
  something inside it.
- A closed (but unlocked) container denies reaching its contents (`reason: "closed"`) while
  still allowing examine/open.
- `"take"`/`"trade"` move an item to the player; `"give"` moves one to the target; `"trade"`
  additionally charges the item's TOML `value` (denied outright, `reason: "cant_afford"`, if
  the player can't pay).
- `_resolve_open_close_intent` is gated to `subtype == "container"`; toggles the `"closed"`
  condition, independent of `"locked"` — a picked lock still needs its own `"open"` before
  contents are reachable. A successful open attaches `contents`: one flavor-description string
  per item inside.
- `_resolve_use_intent` activates/consumes an item already in the player's own inventory,
  gated on a truthy `usable` field (`reason: "not_usable"` otherwise). The only effect
  implemented is healing, read from the item's own `healing = {dice, pips}` stat if present
  and rolled through `apply_healing`; using an item with no `healing` stat still succeeds with
  no numeric effect. Using an item also applies a permanent `"identified"` condition to it.
  Consumption is charge-based (`_consume_charge`): an item with no `charges` field is
  single-use; one that declares `charges` depletes by one per use. At zero charges the item
  is removed from inventory and replaced by whatever its `replace_with` names (ex: a drunk
  health potion becomes a `"glass vial"`), or simply removed if `replace_with` is absent.
- `_resolve_room_transition_intent` handles `"move"` (see "Scenarios and rooms").

Publishes `item_interaction_resolved` either way, with enough detail (`found`,
`reason`/`description`/`container`/`amount`/`price`/`contents`/`healed`/`charges_left`/
`replaced_with`/`slot`/`replaced` as applicable) for narration to explain a miss or a success.

## Social and attitudes

`get_attitude(entity, toward)` returns a six-value array (`disposition, trust, confidence,
respect, obligation, intimacy`, nominally -100..100; a `name` override beats `supertype` beats
`default`; no `[entity.attitudes]` table at all defaults to all-neutral). `get_attitude_tier(value)`
clamps to `[-150, 150]` and returns the first of seven `[[attitude_tier]]` bands (`rules.toml`)
whose range contains it, in declaration order (a value on a shared boundary resolves to
whichever tier is declared first). `describe_attitude(entity, toward)` renders all six axes as
one sentence using each tier's own phrase per axis.

`describe_character(entity_name, toward_name=None)` builds a flavor-text roster line from
purely descriptive TOML fields (`description`, `qualities`, `memories`, `quotes`) plus, when
`toward_name` is given, the attitude sentence above — deliberately excluding mechanical data
(skills/dice). `DMCore.__init__` builds this roster for every scenario entity into the
`scenario_loaded` payload; `_on_action_detected` also attaches a fresh
`result["defender_details"]` per action.

`self.player_name` is resolved once in `__init__` via `_resolve_player_name()`, which scans
loaded templates for the one with `is_player = true` (`characters.toml`'s `gladstone`) and
raises `ValueError` if none is marked.

## Narration

`LLMCore` subscribes to narration-relevant events, sharing outcome-text building
(`_describe_outcome`) and background-fetch plumbing (`_queue_narration`):
- `scenario_loaded` → `generate_scene_intro` — once, from `DMCore.__init__`.
- `round_resolved` → `generate_round_response` — combat, once per round.
- `action_resolved` → `generate_response` — non-combat, once per skill use.
- `action_not_understood` → `generate_clarification_response` — no roll to describe; just
  acknowledges the input didn't resolve to any action.
- `item_interaction_resolved` → `generate_item_interaction_response` — covers examine/take/
  give/trade/open/close/use/equip/unequip/drop, and room transitions (`intent == "move"`): a
  successful move overwrites `self.scenario_description`/`self.scenario_characters` with the
  new room's own data before building the prompt, the same way `generate_scene_intro` seeds
  them initially, so later prompts in the new room stop citing the previous room's flavor text.
- `game_load_failed` → `generate_load_failed_response`.

The scenario/room setting and character roster are re-injected into the system message on
every request (not just the opening one), so narration stays grounded even after the intro
scrolls out of the rolling 100-message `context_window`.

Every `_queue_narration` call's background fetch also publishes `llm_debug_updated
{"query", "response"}` alongside `llm_response_ready` — `"query"` is the exact outgoing
request text (system message plus the entire `context_window` sent, role-labeled), `"response"`
the raw text that came back (or `"[ERROR] ..."` on a failed request) — consumed only by
`GUICore`'s Debug tab (see "GUI_Core.py" above), never stored in `context_window` itself.

## Saving and loading

Three sibling JSON files per slot -- `Saves/<slot>/dm_state.json`, `Saves/<slot>/llm_state.json`,
`Saves/<slot>/gui_state.json` -- written/read independently by `DMCore`, `LLMCore`, and
`GUICore`. `EventBus` has no request/response mechanism, so each core owns and persists its
own slice rather than sharing one file.

**Trigger:** `save_requested`/`load_requested {"slot": slot_name}`, published either by
`NLPCore._detect_save_load_intent` (prefix-stripping on raw input, checked before item/skill
matching), by `GUICore`'s File menu (Save.../Load... popups -- see "GUI_Core.py" above), or by
`Textual_Core`'s slot-name field and Save/Load buttons.

`DMCore.save_game` writes a diff from a fresh instantiation: `scenario_key`, `player_name`,
`round_number`, `current_room_key`, `scenario_entities`, and per-instance
`{hp, active_conditions, currency, inventory, band}`. `load_game` re-runs `load_rules()` (fresh
TOML), then the same scenario-load path `__init__` uses, then overlays each saved instance's
mutable fields onto the freshly-instanced entities; a saved instance with no post-reload match
is skipped. Publishes `game_loaded` on success (not `scenario_loaded`, which would re-narrate
an opening scene) or `game_load_failed {"slot", "reason"}` on failure, then re-publishes
`party_status_changed` (see "Action resolution pipeline" below) so a resumed save's Party tab
isn't left showing the previous game's state.

`LLMCore.save_game`/`load_game` persist/restore `context_window` plus scenario name/
description/characters; loading is silent (no new narration queued).

`GUICore.save_game`/`load_game` persist/restore the Notes tab's free text; loading is silent,
same as `LLMCore`'s.

Slot names are run through `os.path.basename` before use, so a slot can't escape `Saves/`.

## Tags vs. conditions

- **Tags** are static classification data used purely for matching, fixed for an entity's
  lifetime: `damage_tags`/`armor_tags`, entity-innate `resistance_value`/`resistance_tags`
  (rolled, partial reduction via `get_damage_reduction`), `immunity_tags` (absolute — `is_immune_to`
  zeroes net damage regardless of roll), and `vulnerability_value`/`vulnerability_tags`
  (rolled, extra damage added before reduction). Immunity wins outright over vulnerability if
  a single attack's tags match both.
- **Conditions** (`active_conditions`, `apply_condition`/`dismiss_condition`) are dynamic —
  gained/lost during play via triggers or tests. Use a condition for something that can
  plausibly change mid-scene; use a tag for something permanent to what the entity is.

`abilities` is a flat list, each entry either a plain string naming a shared catalog entity
(`spells.toml`/`techniques.toml`, resolved via `resolve_ability`) or an inline table for a
one-off innate ability with no shared entity to point at. `techniques.toml`'s `cleave`
exercises a multi-skill `skill = [...]` list and weapon-scaled damage
(`"user.weapon.dice"`/`"user.weapon.pips"`); see `ability_matches_skill`,
`resolve_weapon_reference`, `resolve_damage_value` in `DM_Combat.py`. Naming a technique/spell
directly in input can resolve it via `map_to_action` before a bare skill would;
`_on_action_detected` then routes a matched ability name through
`resolve_named_ability`/`select_ability_skill` instead of `find_attack_ability`.

## Data/TOML conventions

- `Rules/Fantasy/reference/entity_schema.toml` is a single, heavily-commented `[[entity]]`
  cataloging every field the engine reads off an entity (or explicitly marks as unused/
  reserved) — identity, vitals, skills, inventory/equipped, weapon/spell/technique fields,
  defensive tags, abilities, attitudes, starting conditions, behavior, and `[entity.test]`.
  Reference/documentation only, never loaded as game data: `load_rules` only `os.listdir()`s
  the top level of `Rules/Fantasy/` (the same reason `Rules/Fantasy/scenarios/` is safe to
  hold its own `*.toml` files), and this file lives one directory deeper for the same reason.
- `load_rules` special-cases only `skill` and `entity` top-level keys; everything else in any
  flat `Rules/Fantasy/*.toml` file lands generically in `self.rules[key]`.
- `[entity.attitudes]` is `{default, name, supertype}`; `name`/`supertype` are TOML
  arrays-of-one-key-tables, parsed by `tomllib` as a list of single-key dicts — `get_attitude`
  loops over the list checking `if toward_name in override`.
- `damage_value = {dice, pips, bonus}` — `bonus` is a flat number or `"user.<rule_name>"`,
  resolved via `resolve_bonus` against a same-named `self.rules` table. String `dice`/`pips`
  are not resolved and degrade to 0.
- `load_rules`'s per-file exception handling means a malformed TOML file fails quietly — a
  parse error loads that file with less data than expected, not a crash.

## LLM integration

Endpoint is LM Studio's OpenAI-compatible API. `/v1/models` lists the catalog, not what's
currently loaded — a chat completion can still 400 with `"No models loaded"` even when
`/v1/models` shows one. The request payload has no explicit `"model"` field, which only works
correctly when exactly one chat model is loaded.

## RAG / sourcebook grounding

`LLM_Rag.py`'s `RagIndex` indexes every `*.pdf` under `Settings/Fantasy/` (a gitignored
directory). `RagIndex.__init__` builds its index on a daemon background thread; `query()`
returns `[]` until `self.ready` is `True`, so a narration request before the first build just
gets no lore that turn. Chunks and embeddings are cached to
`Settings/Fantasy/.rag_cache/<hash>.{chunks.json,embeddings.npy}`, keyed by a hash of every
source PDF's path/size/mtime.

Chunking is sentence-bounded: `_chunk_page_text` packs sentences into chunks capped at
`MAX_CHUNK_WORDS` (180); chunks under `MIN_CHUNK_WORDS` (40) are dropped. Retrieval is
per-request, computed fresh and appended to that request's system message only — never stored
in `context_window`. `perform_rag` returns no chunks below `confidence_threshold` (`0.3`).

The RAG query passed to `perform_rag` is the player's own raw input, not the full
instruction-padded narration prompt — embedding the padded prompt dilutes similarity enough to
miss lore a bare-input query would find. Every `_queue_narration` call site that has raw input
on hand passes it as `rag_query`; `generate_scene_intro` passes the scenario name+description
instead (no player input exists yet); `generate_load_failed_response` falls back to its own
full prompt.

`LLMCore.__init__` takes an optional `rag_source_dir` so tests can point it at a directory
with no PDFs (skips even the model load).

`vectorize_pdf.py` is a standalone CLI that builds this same cache ahead of time, outside the
app: `python vectorize_pdf.py [pdf_or_dir] [--query "..."]`, defaulting to `Settings/Fantasy/`.
Reuses `RagIndex` directly (no chunking/embedding logic duplicated) via
`RagIndex.wait_until_ready()`, which blocks on the same background thread `__init__` starts —
the one hook `RagIndex` itself has no other reason to expose, since `query()` already handles
"not ready yet" by returning no matches rather than blocking. A single `.pdf` path resolves to
its own parent directory (`RagIndex` indexes a whole directory, not one file in isolation), so
the cache this produces is exactly what a real run pointed at that directory would build.

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

- **`test_unit.py`** — offline `unittest.TestCase` classes covering dice/damage resolution,
  statuses, scenario/room loading, entity tests, item interactions (examine/take/give/trade/
  open/close/use/move), inventory transfer, NPC dialogue, attitude phrases, save/load,
  `GUICore` (below), and the Textual mirror. `TestGameBoot` and `TestNlpConfidenceThreshold`
  load the real `sentence-transformers` model via `setUpClass`; the latter uses `setUp` to
  re-run `load_rules`/`load_scenario_definition`/`load_scenario` before each test, since state
  shared across a `setUpClass`-based class is also shared mutable game state. Most other
  classes share their `DMCore`/`LLMCore` fixture setup via two base classes, `DMTestCase`
  (`scenario_name` class attribute, plus `_capture`/`_capture_any` helpers for subscribing a
  list-append per event) and `LLMTestCase` — subclasses override `setUp` (calling
  `super().setUp()` first) only when they need more than a bare fixture.
- **`TestGUICore`** — drives `GUICore` directly (no `mainloop`, real `Tk` root withdrawn in
  `setUp`) rather than duplicating `Textual_Core`'s own coverage: tab/event-subscription setup,
  input submission, the Party tab's tree rendering (per-member Equipment/Skills/Abilities/
  Inventory/Conditions groups, empty-group placeholders, every valid equip slot shown even
  unfilled, redraw-not-append on repeat events), the Map tab's freehand drawing/clear, the
  Debug tab overwriting (not appending) its Query/Response boxes on `llm_debug_updated`,
  save/load status narration, the Notes tab's save/load round trip, and the File menu's
  Save/Load popups (`simpledialog.askstring` mocked; the Load picker's listbox/button driven
  directly via `.invoke()`, with `_list_save_slots` mocked so the tests never depend on or
  touch whatever is actually saved under `Saves/`).
- **`test_integration.py`** — every test needing a real, running LM Studio
  (`TestInnkeeperConversation`, `TestRagGroundedNarration`, `TestArenaCombatConversation`,
  `TestChestSagaConversation`, `TestChestTradeConversation`, `TestCryptDungeonConversation`,
  `TestSaveAndResumeConversation`), gated on `_lm_studio_reachable()` so they skip together
  when nothing's listening on `127.0.0.1:1234`.

`python -m pytest -q` runs both files; `python -m pytest -q test_unit.py` runs the fast,
offline subset only.

## Known gaps

- `NLP_Core.py` — a keyword-driven skill match can still dominate an unrelated whole-sentence
  embedding match (ex: "identify the dagger" resolves to the wrong skill); no multi-instance
  disambiguation (ex: "the wounded wolf" vs. "the other wolf").

## Extended goals

Not yet started; no code or TODO markers exist for these:
- Characters are language-dependent — an entity's own comprehension of the language it's
  addressed in should gate dialogue/narration, not just its attitude data.
- Dialogue sentiment sways attitudes — the sentiment of what the player says, not just which
  skill check they made, should be able to move an entity's `[entity.attitudes]` axes.
- Actions sway attitudes by varying degrees — a resolved action (combat, theft, a favor)
  should nudge attitude axes by an amount proportional to the action itself, not just be
  gated by the attitude that already exists.
- Random encounters, enemy generator — procedurally populate a scene/room with creatures
  instead of every encounter being scenario-authored.
- Scenario, quest, NPC, item, and location generators — procedurally author the TOML data
  itself (or equivalent runtime structures) rather than every scenario/entity being
  hand-written.
- A 'dungeon master' persona the LLM can speak directly to the player as.
- Tools that the LLM may call to directly interact with the scene.