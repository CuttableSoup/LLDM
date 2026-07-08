class InventoryMixin:
    """!
    @brief Inventory/currency transfer primitives (DMCore mixin -- only ever composed into
           DMCore, never instantiated on its own; relies on self.entities/self.event_bus,
           set up by DMCore.__init__).
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
