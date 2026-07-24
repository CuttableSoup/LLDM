from DM_Types import DMCoreProtocol


class InventoryMixin(DMCoreProtocol):
    """!
    @brief Inventory/currency transfer primitives (DMCore mixin -- only ever composed into
        DMCore, never instantiated on its own; relies on self.entities/self.event_bus,
        set up by DMCore.__init__). Inherits DMCoreProtocol purely so type checkers can
        resolve these shared attributes -- see DM_Types.py.
    """

    def transfer_currency(self, from_name, to_name, amount=None):
        """!
        @brief Moves currency from one entity to another (ex: looting a chest's gold).
        @param from_name The name of the entity currency is taken from.
        @param to_name The name of the entity currency is given to.
        @param amount How much to move; if None, moves all of from_name's currency.
        @return The amount actually transferred (0 if either entity is missing or there's none to move).
        """
        source = self.entities.get(from_name)
        destination = self.entities.get(to_name)
        if source is None or destination is None:
            return 0

        available = source.get("currency", 0)
        moved = available if amount is None else min(amount, available)
        if moved <= 0:
            return 0

        source["currency"] = available - moved
        destination["currency"] = destination.get("currency", 0) + moved
        self.event_bus.publish("log_info", f"{moved} currency moved from {from_name} to {to_name}.")
        return moved

    def transfer_item(self, from_name, to_name, item_name):
        """!
        @brief Moves one occurrence of an item from one entity's inventory list to another's.
            Duplicates (ex: three "health potion" entries) represent quantity, so only one
            matching entry is removed per call.
        @param from_name The name of the entity the item is taken from.
        @param to_name The name of the entity the item is given to.
        @param item_name The name of the item to move.
        @return True if the item was present in from_name's inventory and moved, False otherwise.
        """
        source = self.entities.get(from_name)
        destination = self.entities.get(to_name)
        if source is None or destination is None:
            return False

        source_inventory = source.get("inventory", [])
        if item_name not in source_inventory:
            return False

        source_inventory.remove(item_name)
        destination.setdefault("inventory", []).append(item_name)
        self.event_bus.publish("log_info", f"{item_name} moved from {from_name} to {to_name}.")
        return True

    def resolve_equip_slot(self, entity_name, item_name):
        """!
        @brief Picks which of item_name's own declared candidate slot(s) -- its "equip_slot"
            field, a single slot name or a list (ex: a one-handed weapon offering both
            "rhand" and "lhand") -- is actually valid for entity_name's own supertype/
            subtype (see DM_Rules.py's get_equip_slots). The first candidate on that list
            that's valid wins, same declaration-order first-match-wins convention every other
            list in this codebase (behavior, status, attitude tiers) already follows.
        @param entity_name The entity that would wear/wield item_name.
        @param item_name The item entity being equipped.
        @return The chosen slot name, or None if item_name declares no "equip_slot" at all,
                or none of its candidates are valid for entity_name.
        """
        candidates = self.entities.get(item_name, {}).get("equip_slot")
        if not candidates:
            return None
        if isinstance(candidates, str):
            candidates = [candidates]
        valid_slots = self.get_equip_slots(entity_name)
        for candidate in candidates:
            if candidate in valid_slots:
                return candidate
        return None

    def equip_item(self, entity_name, item_name):
        """!
        @brief Equips item_name -- already in entity_name's own inventory -- into whichever
            slot resolve_equip_slot picks. An item already sitting in that slot is simply
            displaced: it stays in inventory (equipped items are always also listed there --
            see entity_schema.toml's own [entity.equipped] comment), just no longer mapped to
            any slot -- the common "equip X" RPG convention of implicitly swapping out
            whatever was there rather than refusing the action.
        @param entity_name The entity equipping the item.
        @param item_name The item entity being equipped.
        @return (slot_name, previous_item_name_or_None) on success, or (None, None) if
                item_name isn't in entity_name's own inventory, or resolve_equip_slot found
                no valid slot for it at all.
        """
        entity = self.entities.get(entity_name)
        if entity is None or item_name not in entity.get("inventory", []):
            return None, None
        slot = self.resolve_equip_slot(entity_name, item_name)
        if slot is None:
            return None, None
        equipped = entity.setdefault("equipped", {})
        previous = equipped.get(slot)
        equipped[slot] = item_name
        self.event_bus.publish("log_info", f"{entity_name} equips {item_name} in {slot}.")
        return slot, previous

    def unequip_item(self, entity_name, item_name):
        """!
        @brief Removes item_name from whichever [entity.equipped] slot it currently occupies
            on entity_name, if any. The item stays in inventory either way -- this only ever
            touches the slot mapping, never transfer_item.
        @param entity_name The entity unequipping the item.
        @param item_name The item entity being unequipped.
        @return The slot name it was removed from, or None if item_name wasn't equipped on
                entity_name at all.
        """
        equipped = self.entities.get(entity_name, {}).get("equipped", {})
        for slot, equipped_item in list(equipped.items()):
            if equipped_item == item_name:
                del equipped[slot]
                self.event_bus.publish("log_info", f"{entity_name} unequips {item_name} from {slot}.")
                return slot
        return None

    def loot_entity(self, from_name, to_name):
        """!
        @brief Moves everything -- all currency and every inventory item -- from one entity to
            another. Ex: taking a chest's contents once it's open (see apply_test_outcome's
            "loot" key).
        @param from_name The name of the entity being looted (ex: a chest).
        @param to_name The name of the entity receiving the loot (ex: the player).
        @return A {currency, items} summary of what actually moved, so callers (ex:
                _on_action_detected, for narration) know what was gained without the LLM having
                to invent it.
        """
        currency_moved = self.transfer_currency(from_name, to_name)
        items_moved = []
        for item_name in list(self.entities.get(from_name, {}).get("inventory", [])):
            if self.transfer_item(from_name, to_name, item_name):
                items_moved.append(item_name)
        return {"currency": currency_moved, "items": items_moved}
