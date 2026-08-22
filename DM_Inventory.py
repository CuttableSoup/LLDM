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

    def place_new_item(self, destination_name, item_name):
        """!
        @brief Adds item_name to destination_name's inventory with no source entity --
            transfer_item always needs a real "from" to remove the item from, but a freshly
            conjured ad hoc item (DM_Improvisation.py, AdHoc_Generation.py) never had one; it
            simply starts existing already in someone's possession. Unlike transfer_item, this
            never fails on a missing destination -- setdefault creates the entity's own
            "inventory" list the first time it's needed, same as transfer_item's own
            destination.setdefault("inventory", []) already does for an existing entity.
        @param destination_name The entity item_name should end up owned by.
        @param item_name The item entity's own name/entity_id.
        """
        self.entities.setdefault(destination_name, {}).setdefault("inventory", []).append(item_name)

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
                _on_turn_detected, for narration) know what was gained without the LLM having
                to invent it.
        """
        currency_moved = self.transfer_currency(from_name, to_name)
        items_moved = []
        for item_name in list(self.entities.get(from_name, {}).get("inventory", [])):
            if self.transfer_item(from_name, to_name, item_name):
                items_moved.append(item_name)
        return {"currency": currency_moved, "items": items_moved}

    def _resolve_equip_intent(self, item_name, resolved):
        """!
        @brief Handles "equip" -- moving an item already in the player's own inventory into
            whichever [entity.equipped] slot it's actually valid for (see equip_item/
            DM_Rules.py's get_equip_slots). Deliberately never reaches a target's inventory
            (same "take it first" rule _resolve_use_intent already follows) -- gear has to be
            picked up before it can be worn.
        @param item_name NLPCore's best-guess item match (map_to_item), or None.
        @param resolved The item_interaction_resolved publisher closure from
            DMCore._on_item_interaction_detected.
        """
        player = self.entities.get(self.player_name, {})
        if not item_name or item_name not in player.get("inventory", []):
            resolved(False, reason="not_present")
            return
        if not self.entities.get(item_name, {}).get("equip_slot"):
            resolved(False, reason="not_equippable")
            return
        slot, previous = self.equip_item(self.player_name, item_name)
        if slot is None:
            # equip_item only returns None here when resolve_equip_slot found no candidate
            # slot valid for the player's own supertype/subtype -- the "not_equippable" case
            # above already ruled out "declares no equip_slot at all".
            resolved(False, reason="cant_equip")
            return
        resolved(True, slot=slot, replaced=previous)

    def _resolve_unequip_intent(self, item_name, resolved):
        """!
        @brief Handles "unequip" -- clearing whichever [entity.equipped] slot item_name
            currently occupies on the player (see unequip_item). The item stays in inventory
            either way (an equipped item is always also listed there -- see
            entity_schema.toml's own [entity.equipped] comment), so this never calls
            transfer_item.
        @param item_name NLPCore's best-guess item match (map_to_item), or None.
        @param resolved The item_interaction_resolved publisher closure from
            DMCore._on_item_interaction_detected.
        """
        slot = self.unequip_item(self.player_name, item_name) if item_name else None
        if slot is None:
            resolved(False, reason="not_equipped")
            return
        resolved(True, slot=slot)

    def _current_ground_items(self):
        """!
        @brief The mutable list of item names dropped in the current room/scene -- a room's
            own "ground" key when the current location has one active (persists across a
            revisit the same way a cleared trap does -- see DM_Rules.py's room-graph notes),
            or the current location's own "ground" key for a freeform location otherwise.
            Created empty on first use; never authored in TOML. Saved/restored by
            DM_Persistence.py's save_game/load_game (keyed per location, and per room_key
            within a room-based one), same shape this method itself branches on.
        @return The mutable ground list itself (not a copy) -- callers append/remove in place.
        """
        room = self._current_room()
        scope = room if room is not None else self.locations[self.current_location_key]
        return scope.setdefault("ground", [])

    def _resolve_drop_intent(self, item_name, resolved):
        """!
        @brief Handles "drop" -- moving an item out of the player's own inventory (clearing
            its own [entity.equipped] slot first, if it happened to be equipped) and onto the
            current room/scene's own ground (see _current_ground_items), where a later
            "take"/"examine" can reach it again -- unlike _resolve_use_intent's "use it up"
            consumption, dropping an item never destroys it.
        @param item_name NLPCore's best-guess item match (map_to_item), or None.
        @param resolved The item_interaction_resolved publisher closure from
            DMCore._on_item_interaction_detected.
        """
        player = self.entities.get(self.player_name, {})
        if not item_name or item_name not in player.get("inventory", []):
            resolved(False, reason="not_present")
            return
        self.unequip_item(self.player_name, item_name)
        player["inventory"].remove(item_name)
        self._current_ground_items().append(item_name)
        resolved(True)

    def _resolve_ground_intent(self, intent, item_name, resolved):
        """!
        @brief Handles "examine"/"take" once item_name is already confirmed to be sitting on
            the current room/scene's own ground (see _current_ground_items) -- checked ahead
            of target_name/the locked gate in DMCore._on_item_interaction_detected, since a
            dropped item has no container guarding it at all, unlike everything else
            "take"/"examine" can reach. "examine" only describes; "take" moves it into the
            player's own inventory the same way transfer_item would, just off the ground list
            instead of another entity's own inventory.
        @param intent "examine" or "take".
        @param item_name The item entity confirmed present in _current_ground_items().
        @param resolved The item_interaction_resolved publisher closure from
            DMCore._on_item_interaction_detected.
        """
        if intent == "examine":
            description = self.entities.get(item_name, {}).get("description", "")
            revealed = list(self.entities.get(item_name, {}).get("tags", [])) if self.is_identified(item_name) else []
            resolved(True, description=description, revealed=revealed)
            return
        self._current_ground_items().remove(item_name)
        self.entities.setdefault(self.player_name, {}).setdefault("inventory", []).append(item_name)
        resolved(True)

    def _consume_charge(self, item_name):
        """!
        @brief Decrements an item's own "charges" by one and returns what's left --
            _resolve_use_intent's only source of truth for whether this use was the item's
            last. An item with no "charges" field at all is single-use: treated as if this
            one use was already its only charge, so it always returns 0 (every potion today,
            since none declare "charges"). An item that does declare one (ex: a future wand)
            survives repeated uses until the count actually reaches zero.
        @param item_name The entity being used.
        @return The remaining charge count (0 or negative means "used up").
        """
        item = self.entities.get(item_name, {})
        if "charges" not in item:
            return 0
        item["charges"] -= 1
        return item["charges"]

    def _resolve_use_intent(self, item_name, resolved):
        """!
        @brief Handles "use" (today's only keywords are "drink"/"quaff", both potion-flavored
            -- see NLP_Core.py's USE_KEYWORDS) -- activating/consuming an item already in the
            player's own inventory. Deliberately never reaches a target's inventory the way
            "take" can -- you can't use something you haven't picked up yet; take/examine
            already exist for reaching a container's contents first. Gated on a truthy
            "usable" field (reason "not_usable" otherwise, ex: trying to use a sword) rather
            than any particular subtype, since this is meant to cover more than potions --
            a future wand (subtype "wand", not "potion") opts in the same way, just by
            carrying `usable = true` plus whatever effect fields it defines.

            Two effects are implemented: healing, read from the item's own "healing" {dice,
            pips} skill stat if present (ex: health potion) and rolled through apply_healing
            (DM_Status.py); and poison, read from a "poison" {dice, pips} skill stat the same
            way but rolled through the ordinary calculate_damage/apply_damage path instead
            (DM_Combat.py), tagged damage_tags = ["poison"] -- self-inflicted (attacker and
            defender are both the player), so a poison-resistant or poison-immune character
            correctly reduces or negates it exactly like a real attack would, and
            evaluate_statuses' own wound-tier conditions still apply. An item can carry either,
            both, or neither -- one with neither still "uses" successfully (consumes a charge,
            may still identify/replace itself below), it just has no numeric effect. Poison
            exists specifically so an ad hoc-conjured consumable (DM_Improvisation.py,
            AdHoc_Generation.py) isn't guaranteed free healing -- the LLM can mark one harmful
            instead, for balance.

            Using it also identifies it, whether or not it already was -- you now know
            exactly what it does, from experience, which is a strictly stronger kind of
            knowledge than a prior appraise/medicine check (see items.toml's health potion
            and its own [entity.test]).

            Consumption is charge-based (see _consume_charge): an item with no "charges"
            field at all is single-use, spent entirely on this one call (every potion
            today); one carrying a "charges" count only depletes by one per use and keeps
            working until it hits zero (a future wand's whole reason to have this field
            rather than being single-use like a potion). Either way, once charges reach
            zero, the item is removed from inventory and swapped for whatever its own
            "replace_with" names (ex: health potion -> glass vial, an empty husk) -- an item
            with no "replace_with" just vanishes, the same as before this field existed.
        @param item_name NLPCore's best-guess item match (map_to_item), or None.
        @param resolved The item_interaction_resolved publisher closure from
            DMCore._on_item_interaction_detected.
        """
        player = self.entities.get(self.player_name, {})
        if not item_name or item_name not in player.get("inventory", []):
            resolved(False, reason="not_present")
            return
        item = self.entities.get(item_name, {})
        if not item.get("usable"):
            resolved(False, reason="not_usable")
            return

        healing = item.get("skills", {}).get("healing")
        healed = 0
        remaining_hp = self.get_current_hp(self.player_name)
        if healing:
            healed = self.roll_dice(healing.get("dice", 0), healing.get("pips", 0))
            remaining_hp = self.apply_healing(self.player_name, healed)

        poison = item.get("skills", {}).get("poison")
        poisoned = 0
        if poison:
            damage_result = self.calculate_damage(
                self.player_name, self.player_name,
                {
                    "damage_value": {"dice": poison.get("dice", 0), "pips": poison.get("pips", 0), "bonus": 0},
                    "damage_tags": ["poison"],
                },
            )
            poisoned = damage_result["net_damage"]
            remaining_hp = damage_result["remaining_hp"]

        self.apply_condition(item_name, "identified", duration="permanent", dismiss="")

        charges_left = self._consume_charge(item_name)
        replaced_with = None
        if charges_left <= 0:
            player["inventory"].remove(item_name)
            replace_with = item.get("replace_with")
            if replace_with:
                if replace_with in self.entities:
                    player["inventory"].append(replace_with)
                    replaced_with = replace_with
                else:
                    self.event_bus.publish(
                        "log_error", f"{item_name}'s replace_with names unknown entity: {replace_with}"
                    )

        resolved(
            True, healed=healed, poisoned=poisoned, remaining_hp=remaining_hp,
            charges_left=max(charges_left, 0), replaced_with=replaced_with,
        )

    def _resolve_open_close_intent(self, intent, target_name, resolved):
        """!
        @brief Handles "open"/"close" against the current scene target directly -- these act on
            the container itself, not a named item inside it, so (unlike the other item
            intents) they never go through map_to_item at all. Gated to subtype ==
            "container" (ex: items.toml's chest) so aiming "open"/"close" at a creature or a
            plain object with no openable nature fails safely instead of silently applying a
            nonsensical condition to it.
        @param intent "open" or "close".
        @param target_name The current scene target's name, or None if there isn't one.
        @param resolved The item_interaction_resolved publisher closure from
            DMCore._on_item_interaction_detected.
        """
        if not target_name or self.entities.get(target_name, {}).get("subtype") != "container":
            resolved(False, reason="not_openable")
            return

        if intent == "open":
            if not self.is_closed(target_name):
                resolved(False, reason="already_open", container=target_name)
                return
            self.dismiss_condition(target_name, "closed")
            # Real contents, not a guess: each item's own describe_character() output (its
            # flavor description only -- the same purely-descriptive, no-mechanical-data
            # field selection describe_character already uses for entities) -- never its
            # "tags"/damage_value/etc., so a cursed item's actual curse tag is never handed
            # to the LLM here. Without this, "open the chest" had nothing to narrate from and
            # invented plausible-sounding treasure instead of the real contents.
            contents = [
                description for description in (
                    self.describe_character(item_name)
                    for item_name in self.entities.get(target_name, {}).get("inventory", [])
                )
                if description
            ]
            resolved(True, container=target_name, contents=contents)
            return

        if self.is_closed(target_name):
            resolved(False, reason="already_closed", container=target_name)
            return
        self.apply_condition(target_name, "closed", duration="permanent", dismiss="")
        resolved(True, container=target_name)

    def _resolve_transfer_intent(self, intent, item_name, target_name, resolved):
        """!
        @brief Handles "give"/"trade"/"examine"/"take" against a named item -- "take"/"trade"
            move an item from the target to the player, "give" moves one from the player to the
            target (same transfer_item/transfer_currency primitives, just with source/
            destination swapped), "examine" never moves anything. Called only once the locked
            gate, the ground-item short-circuit, and open/close have already had their shot (see
            DMCore._on_item_interaction_detected) -- item_name here always names something
            reachable only via a target (already in inventory, or inside/behind target_name).

            "examine"/"take" against an item already sitting in the player's own inventory (ex:
            DM_Improvisation.py placing an ad hoc item straight into inventory) resolve directly
            against the player -- recomputed here as already_owned, the same check the dispatcher
            makes for its own locked-gate purposes, so this resolver stays callable on its own
            with nothing but item_name/target_name and live entity state. "trade" additionally
            charges the item's TOML `value` as a price (denied outright if the player can't
            afford it, rather than a partial payment); trading for currency itself, or aiming
            "take"/"give"/"trade" at the target's own name rather than something inside it, is
            always "not_takeable" regardless of amount.
        @param intent "give", "trade", "examine", or "take".
        @param item_name NLPCore's best-guess item match (map_to_item), or the literal sentinel
            "currency".
        @param target_name The current scene target's name, or None if there isn't one.
        @param resolved The item_interaction_resolved publisher closure from
            DMCore._on_item_interaction_detected.
        """
        already_owned = intent in ("examine", "take") and item_name in self.entities.get(self.player_name, {}).get("inventory", [])

        if not already_owned and item_name == target_name:
            # Interacting with the container/creature itself, not something inside it -- there's
            # nothing to "take"/"give"/"trade" about the target as a whole.
            if intent == "examine":
                description = self.describe_character(target_name, toward_name=self.player_name) or ""
                resolved(True, description=description)
            else:
                resolved(False, reason="not_takeable")
            return

        if not already_owned and target_name and self.is_closed(target_name):
            # A closed (but unlocked) container can still be examined/opened from the outside
            # (handled by _resolve_open_close_intent) -- only reaching its *contents* is gated
            # here.
            resolved(False, reason="closed", container=target_name)
            return

        if intent == "give":
            if not target_name:
                resolved(False, reason="no_recipient")
                return
            source_name, destination_name = self.player_name, target_name
        elif already_owned:
            # Already in the player's own inventory (ex: an ad hoc item conjured straight into
            # inventory, DM_Improvisation.py) -- nothing to actually transfer, so source and
            # destination are the same rather than falling back to whatever the scene target
            # happens to be. Deliberately excludes "trade": buying an already-owned item is
            # nonsensical, and trade's own price-payment logic pays target_name directly,
            # independent of source_name/destination_name -- including it here would silently
            # charge the player currency against an unrelated scene target.
            source_name = destination_name = self.player_name
        else:
            source_name, destination_name = target_name, self.player_name

        if item_name == "currency":
            if intent == "trade":
                # Trading for currency itself is meaningless -- nothing to buy or sell here.
                # Checked before availability, since this is wrong regardless of the amount.
                resolved(False, reason="not_takeable")
                return
            # Currency is a plain "currency" integer field, not an inventory item -- handled
            # separately from transfer_item/source_inventory below.
            available = self.entities.get(source_name, {}).get("currency", 0) if source_name else 0
            if available <= 0:
                resolved(False, reason="not_present")
                return
            if intent == "examine":
                resolved(True, description=f"{available} currency", container=target_name)
            else:
                moved = self.transfer_currency(source_name, destination_name)
                resolved(True, container=target_name, amount=moved)
            return

        source_inventory = self.entities.get(source_name, {}).get("inventory", []) if source_name else []
        if item_name not in source_inventory:
            resolved(False, reason="not_present")
            return

        if intent == "examine":
            description = self.entities.get(item_name, {}).get("description", "")
            # A plain look never surfaces a hidden property (ex: the cursed dagger's curse) --
            # only once is_identified is true (a passed [entity.test], ex: an arcane check)
            # does examining it start including what that check actually revealed.
            revealed = list(self.entities.get(item_name, {}).get("tags", [])) if self.is_identified(item_name) else []
            # No real container involved when source_name/destination_name are both the player
            # (the "already in your own inventory" branch above) -- narration shouldn't claim
            # the item came from target_name when nothing was actually taken from it.
            container = target_name if source_name != destination_name else None
            resolved(True, description=description, container=container, revealed=revealed)
        elif intent == "trade":
            price = self.entities.get(item_name, {}).get("value", 0)
            buyer_currency = self.entities.get(self.player_name, {}).get("currency", 0)
            if buyer_currency < price:
                resolved(False, reason="cant_afford", price=price)
                return
            self.transfer_currency(self.player_name, target_name, price)
            self.transfer_item(source_name, destination_name, item_name)
            resolved(True, container=target_name, price=price)
        else:
            self.transfer_item(source_name, destination_name, item_name)
            container = target_name if source_name != destination_name else None
            resolved(True, container=container)
