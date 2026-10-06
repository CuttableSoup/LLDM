"""!
@file Inventory_Resolution.py
@brief Pure, DMCore-independent counterpart to DM_Inventory.py's own transfer_currency/
    transfer_item/place_new_item -- plain functions over an explicit entities dict rather than
    DMCore instance methods, mirroring Combat_Resolution.py/Social_Resolution.py's own "pure
    module, DMCore reaches in" shape. Built so Program_Interpreter.py's own transfer_item/
    transfer_currency ops -- deferred until a real authored program needed them; maneuvers.toml's
    own "sleight of hand" is that real caller -- can move currency/items with no DMCore instance
    in hand, the same
    reason Combat_Resolution.py/Social_Resolution.py exist at all.

    DM_Inventory.py's own transfer_currency/transfer_item/place_new_item keep their existing
    method names/signatures -- each becomes a thin wrapper forwarding self.entities/
    self.event_bus, so no caller anywhere else in the codebase changes at all.
"""

import re


def _settle(amount):
    """!
    @brief Rounds a balance after fractional prices have moved through it -- a setting may price
        in fractions (Pathfinder: 8 sp = 0.8 gp), and float subtraction alone would leave a purse
        holding 0.09999999999999998 instead of 0.1.
    @param amount The raw post-transfer balance.
    @return An int when whole, else the amount rounded to 4 places (a hundredth of a copper).
    """
    rounded = round(amount, 4)
    return int(rounded) if float(rounded).is_integer() else rounded


def format_currency(amount, denominations=()):
    """!
    @brief Spells a currency amount out the way the setting names its coins, for narration --
        the internal "currency" field is one number, and "the player pays 0.8 currency" both
        leaks that name into the prose and leaves the narrator guessing at coinage.
    @param amount The amount, in the setting's own value unit (ex: Pathfinder's gp).
    @param denominations The setting's [[currency.denomination]] entries (rules.toml), each
        {name, plural?, worth} -- worth in that same unit (ex: silver piece = 0.1). plural
        defaults to name + "s".
    @return Largest coin first, ex: 1.24 -> "1 gold piece, 2 silver pieces and 4 copper pieces";
            0.8 -> "8 silver pieces". With no denominations: "5 coins"/"0.8 coins".
    """
    coins = sorted(
        (d for d in denominations if d.get("name") and (d.get("worth") or 0) > 0),
        key=lambda d: d["worth"], reverse=True,
    )
    if not coins:
        amount = _settle(amount)
        return f"{amount} coin" if amount == 1 else f"{amount} coins"

    # Counted in whole units of the smallest coin, so float division never leaves 7.999 silver.
    smallest = coins[-1]["worth"]
    remaining = round(amount / smallest)
    parts = []
    for coin in coins:
        count, remaining = divmod(remaining, round(coin["worth"] / smallest))
        if count:
            label = coin["name"] if count == 1 else coin.get("plural") or coin["name"] + "s"
            parts.append(f"{count} {label}")
    if not parts:
        return f"0 {coins[-1].get('plural') or coins[-1]['name'] + 's'}"
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


# "5 gold", "8 sp", "20 silver pieces" -- a number, then a coin named by its first word or its
# abbreviation (the first letter plus "p"). See parse_currency_amount.
_AMOUNT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)?")


def parse_currency_amount(text, denominations=()):
    """!
    @brief The first sum of money a player's line names, in the setting's value unit -- what a
        bribe offers ("bribe him with 5 gold"). Mechanical, so money never rests on a model's
        reading.
    @param text The player's line.
    @param denominations The setting's [[currency.denomination]] entries (see format_currency).
    @return The amount (ex: "8 sp" -> 0.8), or None when no number is named. A bare number, or
            one followed by a word that names no coin ("5 coins"), is in the value unit.
    """
    coins = {}
    for coin in denominations or []:
        name = (coin.get("name") or "").lower()
        if not name or not coin.get("worth"):
            continue
        coins[name.split()[0]] = coin["worth"]
        coins[name.split()[0][0] + "p"] = coin["worth"]
    for match in _AMOUNT_PATTERN.finditer((text or "").lower()):
        number, word = float(match.group(1)), match.group(2) or ""
        worth = coins.get(word) or coins.get(word.rstrip("s")) or 1
        return _settle(number * worth)
    return None


def transfer_currency(entities, event_bus, from_name, to_name, amount=None):
    """!
    @brief Moves currency from one entity to another (ex: looting a chest's gold, or a
        successful "sleight of hand" theft).
    @param entities The live entities dict.
    @param event_bus The EventBus to publish a log_info line to.
    @param from_name The name of the entity currency is taken from.
    @param to_name The name of the entity currency is given to.
    @param amount How much to move; if None, moves all of from_name's currency.
    @return The amount actually transferred (0 if either entity is missing or there's none to move).
    """
    source = entities.get(from_name)
    destination = entities.get(to_name)
    if source is None or destination is None:
        return 0

    available = source.get("currency", 0)
    moved = available if amount is None else min(amount, available)
    if moved <= 0:
        return 0

    source["currency"] = _settle(available - moved)
    destination["currency"] = _settle(destination.get("currency", 0) + moved)
    event_bus.publish("log_info", f"{moved} currency moved from {from_name} to {to_name}.")
    return moved


def get_container_rejection_reason(entities, container_name, item_name):
    """!
    @brief Whether container_name's own optional container_allowed_supertypes/
        container_allowed_subtypes/container_capacity fields refuse item_name being added to
        its "inventory" -- the shared gate transfer_item/place_new_item both check before
        mutating anything, so every existing mover (give/take/trade, loot_entity, ADaM
        placement, a maneuver's own transfer op) respects it uniformly, not just player-typed
        commands (unlike get_max_bulk's own player-only carry cap, which is checked by
        DM_Inventory.py's own callers instead -- a container's own contents are a property of
        the container itself, not of whoever happens to be moving something into it).
    @param entities The live entities dict.
    @param container_name The entity item_name would be added to.
    @param item_name The item entity being added.
    @return "wrong_item_type" if container_name authors container_allowed_supertypes and/or
        container_allowed_subtypes and item_name's own supertype/subtype doesn't match every
        one of those that's set; "container_capacity_exceeded" if container_name authors
        container_capacity and adding item_name's own "bulk" to the sum of what it already
        holds would exceed it; None if container_name authors none of these fields (every
        entity shipped before this mechanic existed) or item_name passes every check it does
        author.
    """
    container = entities.get(container_name, {})
    item = entities.get(item_name, {})

    allowed_supertypes = container.get("container_allowed_supertypes")
    if allowed_supertypes and item.get("supertype") not in allowed_supertypes:
        return "wrong_item_type"
    allowed_subtypes = container.get("container_allowed_subtypes")
    if allowed_subtypes and item.get("subtype") not in allowed_subtypes:
        return "wrong_item_type"

    capacity = container.get("container_capacity")
    if capacity is not None:
        current_bulk = sum(
            entities.get(name, {}).get("bulk", 0) for name in container.get("inventory", [])
        )
        if current_bulk + item.get("bulk", 0) > capacity:
            return "container_capacity_exceeded"

    return None


def transfer_item(entities, event_bus, from_name, to_name, item_name):
    """!
    @brief Moves one occurrence of an item from one entity's inventory list to another's.
        Duplicates (ex: three "health potion" entries) represent quantity, so only one
        matching entry is removed per call.
    @param entities The live entities dict.
    @param event_bus The EventBus to publish a log_info line to.
    @param from_name The name of the entity the item is taken from.
    @param to_name The name of the entity the item is given to.
    @param item_name The name of the item to move.
    @return True if the item was present in from_name's inventory and moved, False otherwise
        (also False, with nothing moved, if to_name's own container_allowed_supertypes/
        container_allowed_subtypes/container_capacity refuses item_name -- see
        get_container_rejection_reason).
    """
    source = entities.get(from_name)
    destination = entities.get(to_name)
    if source is None or destination is None:
        return False

    source_inventory = source.get("inventory", [])
    if item_name not in source_inventory:
        return False

    reason = get_container_rejection_reason(entities, to_name, item_name)
    if reason:
        event_bus.publish("log_info", f"{item_name} can't be moved into {to_name} ({reason}).")
        return False

    source_inventory.remove(item_name)
    destination.setdefault("inventory", []).append(item_name)

    event_bus.publish("log_info", f"{item_name} moved from {from_name} to {to_name}.")
    return True


def destroy_equipped_item(entities, event_bus, entity_name, slot):
    """!
    @brief Destroys whatever entity_name currently has equipped in slot outright -- removed from
        both [entity.equipped] and "inventory" entirely, not just unslotted (compare
        DM_Inventory.py's own unequip_item, which only ever clears the slot mapping and leaves
        the item sitting in inventory). This is the Pathfinder Rust Monster / Sunder-a-weapon
        shape, deliberately simplified from Pathfinder's real two-hit item-HP model (an item's
        own max_hp is never read here) to a single destroy-or-nothing roll -- see
        Combat_Resolution.py's apply_destroy_equipped (the "chance" half of this) and
        maneuvers.toml's own "disarm".
    @param entities The live entities dict.
    @param event_bus The EventBus to publish a log_info line to.
    @param entity_name The entity whose gear is being destroyed.
    @param slot The equip slot to target (ex: "rhand").
    @return The destroyed item's own name, or None if entity_name had nothing equipped in slot.
    """
    entity = entities.get(entity_name)
    if entity is None:
        return None
    item_name = entity.get("equipped", {}).pop(slot, None)
    if item_name is None:
        return None
    inventory = entity.get("inventory", [])
    if item_name in inventory:
        inventory.remove(item_name)
    event_bus.publish("log_info", f"{entity_name}'s {item_name} is destroyed.")
    return item_name


def place_new_item(entities, destination_name, item_name):
    """!
    @brief Adds item_name to destination_name's inventory with no source entity -- transfer_item
        always needs a real "from" to remove the item from, but a freshly conjured ad hoc item
        never had one; it simply starts existing already in someone's possession.
    @param entities The live entities dict.
    @param destination_name The entity item_name should end up owned by.
    @param item_name The item entity's own name/entity_id.
    @return True if item_name was added; False if destination_name's own
        container_allowed_supertypes/container_allowed_subtypes/container_capacity refuses it
        (see get_container_rejection_reason) -- nothing is placed anywhere in that case, so an
        ad hoc item a caller doesn't check this return value for simply never comes into
        existence rather than leaking into an unreachable dict entry.
    """
    if get_container_rejection_reason(entities, destination_name, item_name):
        return False
    entities.setdefault(destination_name, {}).setdefault("inventory", []).append(item_name)
    return True
