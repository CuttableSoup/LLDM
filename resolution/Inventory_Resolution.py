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

    source["currency"] = available - moved
    destination["currency"] = destination.get("currency", 0) + moved
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
