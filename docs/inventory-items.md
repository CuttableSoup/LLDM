# LLDM — Entity Tests, Inventory, Items, and Crafting

Part of the [LLDM](../CLAUDE.md) docs — the diceless item pipeline plus the one item-adjacent check that does roll.

## Entity tests

A `[entity.test]` block is a skill check against an entity itself (ex: `items.toml`'s `chest`
lock; see `Rules/Fantasy/reference/entity_schema.toml` for every field it can carry).
`is_test_available(target, test, skill_name)` gates it: `skill_name` must be in `test["skill"]`;
`requires_condition` (if set) must currently be active; `blocks_if_condition` (if set) must not
be; an optional `requirements` list (if set) must be satisfied by `entity_matches_requirements`
— the same requirements engine `[[status]]`/`[[entity.behavior]]` use (see `docs/combat.md`'s
"Status and conditions"), for a test that needs to gate on more than one named condition's
presence/absence (an HP tier, an attribute, a `between` range, or a nested `all`/`any`/`none`
combination). All present fields must hold together; most tests only ever need the two simple
condition fields. A skill not in `test["skill"]` isn't blocked — it just isn't a test, and falls
through to ordinary opposed-skill resolution instead.

A scene-level test (the target itself, via `self.current_target`) is resolved as a flat
difficulty check (`resolve_action`), not through `resolve_opposed_action`.
`_resolve_item_test_target`/`_resolve_item_test` handle the same mechanism one level deeper — an
item already in the player's inventory, or sitting in a reachable container — tried before
combat-target redirection so inspecting an item never becomes an attack.

`apply_test_outcome(entity_name, outcome)` dispatches on whichever keys are present in the
matched `pass`/`fail` table: `dismiss_condition` removes a condition, `condition` applies a new
one, `loot` transfers everything on the target via `loot_entity`, `reveal` (truthy) applies a
permanent `"identified"` condition — the content it reveals is read back off the entity's own
`tags` field by whoever narrates it, not stored on the outcome itself — and `xp` (truthy) awards
XP via `_award_xp_for_defeat` (`DM_Combat.py`, see `docs/combat.md`'s "Experience (XP)") — the
same primitive a combat kill uses, just opt-in per test rather than automatic, since most
`[entity.test]`s (ex: the chest's own lock) aren't "surviving a threat." `items.toml`'s dart
trap/scythe trap are the shipped example, both pairing `xp = true` with their own
`dismiss_condition = "armed"`.


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


## Carry capacity (bulk)

`get_max_bulk(entity_name)` (`DM_Rules.py`) is an entity's own authored `max_bulk` field if it has
one (a flat override, no formula involved — ex: `Rules/Zombie/`'s `riley`/`car`), else `rules.toml`'s
`[bulk]` table — `min_bulk` plus that entity's own `skill` dice times `mod_multiplier` (Fantasy:
`min_bulk = 3`, `skill = "strength"`, `mod_multiplier = 2`, so a 2D strength character carries
`3 + 2*2 = 7`). An authored `max_bulk` always wins over the formula when both are available. A
setting that authors no `[bulk]` table and whose entity authors no `max_bulk` of its own either
(most of `Rules/Zombie/`) gets `None` back and the cap is never enforced for that entity.

`get_current_bulk(entity_name)` sums the `bulk` field of everything in an entity's own
`inventory` (an equipped item is always also listed there, so it's never double-counted), plus —
now that `bulk` also means something on a creature, not just an object — the load contributed by
any currently-present entity whose own `mount` field names entity_name (their own flat `bulk`,
their body weight, plus their own carried gear too if `rules.toml`'s `[bulk]` table opts in via
`count_rider_gear`, default true). See `docs/downtime.md`'s "Mounts and conveyance" for the full
mount/rider design and `DM_Rules.py`'s `get_carrying_capacity` (a *team*-aware capacity, summed
across whatever an entity like a cart itself defers to via its own `mount`, used instead of a
flat `get_max_bulk` wherever a mount's capacity is checked).

`DM_Inventory.py`'s `_bulk_would_be_exceeded(item_name)` checks whether adding that item's own
bulk to the player's current load would exceed `get_max_bulk` — `"take"`/`"trade"` (against a
target's inventory) and `"take"` off the ground are all denied `reason: "bulk_exceeded"` when it
would. Only ever checked against the player — no other entity's inventory is capacity-checked,
and `"give"` is never bulk-gated either way. Mounting an entity is checked separately
(`DM_Rules.py`'s `_would_exceed_mount_capacity`, `DM_Movement.py`'s `_resolve_mount_intent`)
against that entity's own `get_carrying_capacity`, not this player-only check.

`get_current_bulk` never recurses into an item's own nested `inventory` in the first place, so
whatever a container item holds already doesn't count against whoever's carrying it — a
container item's own `container_capacity` (optional) is what actually caps that instead:
`transfer_item`/`place_new_item` (`Inventory_Resolution.py`'s shared
`get_container_rejection_reason`) refuse to add an item that would push the sum of the
container's own contents' `bulk` past it. `items.toml`'s `bag of holding` is the shipped
example (the Pathfinder "Bag of Holding"/"Handy Haversack" shape). A container item can
separately author `container_allowed_supertypes`/`container_allowed_subtypes` (lists) to refuse
anything whose own `supertype`/`subtype` isn't on the list — `items.toml`'s `spellbook`
(`container_allowed_supertypes = ["spell"]`) is the shipped example. Both checks are absent/
unenforced on every container shipped before this mechanic existed.

Unlike `_bulk_would_be_exceeded` (checked only at the player-facing `"take"`/`"trade"` call
sites in `DM_Inventory.py`, leaving `loot_entity`/ADaM/a maneuver's own transfer op uncapped by
design), `get_container_rejection_reason` is checked **inside** `transfer_item`/`place_new_item`
themselves — every existing mover respects it uniformly, not just typed commands. `transfer_item`
returns `False` (nothing moved) on a refusal the same way it already does for "item not actually
present in the source"; `loot_entity` already treats that as "leave it with the source," so an
item a restricted container refuses during a loot sweep just stays behind rather than
vanishing. `place_new_item` gained the same `bool` return for the same reason — an ad hoc item
(`DM_Improvisation.py`) placed at a rejecting destination is never added anywhere else either
(no fallback to the ground or the player), so a caller that ignores the return value simply
never brings that item into existence, rather than leaking an orphaned entity into `self.entities`
with nothing referencing it. `_resolve_transfer_intent`'s own `"give"`/`"trade"`/`"take"`
branches surface the refusal as `reason: "wrong_item_type"`/`"container_capacity_exceeded"` —
for `"trade"` specifically, checked (and the price left unpaid) *before* `transfer_currency`
runs, so a doomed transfer never charges the player for nothing.


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
  in the scene never blocks examining something the player already possesses. Excludes the case
  where the current target *also* currently carries an item of this same shared-catalog name (ex:
  a second "health potion" in a chest, once the player already picked one up from an earlier
  container this same session) — there's still something real left to actually take from the
  target, so this never silently short-circuits into a self-transfer no-op that leaves the
  target's own copy behind untaken.
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


## Crafting

A third kind of item-adjacent check, alongside the diceless item interactions above and
`[entity.test]`-driven item tests (lock-picking, above): crafting genuinely rolls dice against a
real difficulty, so it's routed through the batched skill/ability side of `_on_turn_detected`'s
clause loop (`player_actions`, `dice_penalty`) rather than the diceless
`_on_item_interaction_detected` pipeline every other item verb uses — even though it's *detected*
exactly like one (a `"craft"` intent, matched via `detect_item_intent`'s own keyword ladder plus
the ordinary `map_to_item` lookup). Its "target" is the **crafted item's own name**, a pure
catalog entry that need never have been instanced anywhere — `map_to_item` already matches over
every `supertype == "object"` entity regardless of scene presence (see "Items and movement as
intents"), so `"craft a torch"` resolves the same way `"examine a torch"` would.

An item opts in via its own `[entity.craft]` block: `skill` (a list — like `[entity.test]`'s own,
any one qualifies, but since `"craft"`/`"brew"`/`"forge"` carry no skill of their own the way a
literal `"pick the lock with finesse"` does, the player's *best-trained* skill among the list is
auto-selected via `select_ability_skill`, the same "best-rated candidate" helper a multi-skill
ability like `cleave` already uses), `difficulty` (same `resolve_action` semantics as an item
test's own), `materials` (a list of `{item, quantity}`, checked against the player's own
inventory — duplicates represent quantity, same convention `transfer_item` already documents),
and an optional `requires_station` (a string naming a station tag; if set, some live
`self.scenario_entities` member's own top-level `provides_station` must match it, or the attempt
fails as `"missing_station"` with no roll — the first "is a tagged entity present in the scene"
check anywhere in this codebase; every existing `requires_condition`/`blocks_if_condition` gate
instead checks a condition on the acting/target entity's own state, never a third party's
presence). `provides_station` is deliberately its own field, not a repurposing of the existing
`tags` field — `tags` is documented as narration-only, never matched against anything else, and
overloading it here would break that invariant.

`DM_Crafting.py`'s `CraftingMixin._try_craft_action(item_name, dice_penalty)` is the resolver:
no `[entity.craft]` block at all fails as a `NotCraftableOutcome`; a missing station or missing
materials each fail as their own `ActionOutcome` variant (`MissingStationOutcome`/
`MissingMaterialsOutcome`), no roll attempted (mirroring `OutOfRangeOutcome`'s own no-roll
precedent) — none of these three gates cost anything. Past every gate, it calls
`resolve_action(player_name, skill_name, difficulty, dice_penalty=dice_penalty)` (never
`resolve_opposed_action` — nothing opposes a craft attempt) and **consumes every material
unconditionally, success or failure alike** — a botched attempt still spends the materials,
giving the difficulty number real stakes rather than a free retry. Only on success does it call
`place_new_item(player_name, item_name)` and append a `CraftEffect` to the resulting
`RolledOutcome`'s own `effects`. This is a plain `ActionOutcome` like any other, so it flows
through the ordinary `action_resolved`/`round_resolved` narration path unchanged;
`LLM_Core.py`'s `_describe_outcome` dispatches `CraftEffect` through its own formatter registry
(mirroring `SummonEffect`'s own) and gained three no-roll `ActionOutcome` branches
(`NotCraftableOutcome`/`MissingMaterialsOutcome`/`MissingStationOutcome`, mirroring the existing
`OutOfRangeOutcome` branch) — no new narration trigger event was needed. A craft attempt
deliberately never touches `self.current_target` and never sets `engaged_combat_target`, same
as an item test's own roll.

`Rules/Fantasy/scenarios/debug.toml`'s `blacksmith` location (already narrated as having "the
forge in the back") is the shipped worked example: a `forge` prop entity
(`provides_station = "forge"`, in the same `subtype = "prop"` style as `town_square`'s own
`market stall`) sits in its own `entities` list, and `items.toml`'s `iron dagger` is craftable
there from `iron ingot`/`leather strip` materials via either `strength` or `finesse`.

**Spell components reuse the same `materials` field and primitives.** A spell/technique/innate
ability (`entity_schema.toml`'s "Ability/weapon/spell/technique fields") can carry its own
`materials` (identical `{item, quantity}` shape to a craft recipe's), gating and consuming
through `DM_Crafting.py`'s `_has_materials`/`_consume_materials` directly rather than a parallel
mechanism. `DM_Core.py`'s `_resolve_roll` checks it first, ahead of the entity-test/opposed/
untargeted split every other roll path picks between — missing even one named material at its
required quantity fails the cast as a `MissingSpellMaterialsOutcome` with no roll attempted, the
same "gate failures cost nothing" precedent `OutOfRangeOutcome` already follows for a too-far
target (a distinct variant from craft's own `MissingMaterialsOutcome`, since a spell's own
failure has no `item_name` to report the way a craft attempt's does). Once a roll actually
happens, `_consume_spell_materials_if_rolled` spends
every material unconditionally, success or failure alike — a fizzled cast still burns the
reagent, mirroring a botched craft attempt's own cost. `spells.toml`'s `arc lance` (an
`iron filings`-consuming lightning bolt on gladstone's own `abilities` list, `iron filings`
pre-seeded in gladstone's starting inventory) is the shipped worked example.

