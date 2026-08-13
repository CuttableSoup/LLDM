from AdHoc_Generation import decide_entity_removal, generate_ad_hoc_item
from DM_Types import DMCoreProtocol

# The item-interaction verbs eligible for the ad hoc creation fallback (see
# _on_improvisation_requested), partitioned by which entity's own inventory the created item
# actually needs to land in for the ordinary, unchanged dispatcher to resolve the *original*
# triggering intent correctly (see DM_Core.py's _on_item_interaction_detected -- "give"/
# "equip"/"unequip"/"use"/"drop" always check the player's own inventory regardless of
# direction; "trade" is the one intent that checks the *current target's* inventory as its
# source, since buying something means the seller has to have it, not the buyer). NLP_Core.py's
# own IMPROVISABLE_INTENTS mirrors the union of all three sets exactly -- kept as separate
# constants (rather than importing one from the other) since NLP_Core.py must stay independent
# of DMCore/game state.
PLAYER_CENTRIC_INTENTS = frozenset({"give", "equip", "unequip", "use", "drop"})
GROUND_AWARE_INTENTS = frozenset({"examine", "take"})
TARGET_CENTRIC_INTENTS = frozenset({"trade"})


class ImprovisationMixin(DMCoreProtocol):
    """!
    @brief Ad hoc entity creation/removal (DMCore mixin -- only ever composed into DMCore,
        never instantiated on its own; relies on self.entities/self.player_name/
        self.scenario_entities/self.persistent_entities/self.visited_rooms/self.rooms/
        self.scenario/self.removed_entities/self.event_bus, set up by DMCore.__init__, plus
        RulesMixin's _current_scene_description/_all_known_instance_names (PersistenceMixin),
        InventoryMixin's _current_ground_items, CombatMixin's get_equip_slots, and DMCore's own
        _on_item_interaction_detected/_publish_party_status. Inherits DMCoreProtocol purely so
        type checkers can resolve these shared attributes/cross-mixin methods -- see
        DM_Types.py. AdHoc_Generation.py is the pure, DMCore-independent LLM-calling half this
        mixin is glue for -- same split DM_NpcGeneration.py is to NPC_Generation.py.

        Creation and removal are two genuinely different channels, triggered differently and
        with different risk profiles -- not two halves of one symmetric mechanic:
        - **Creation** (_on_improvisation_requested) is an automatic fallback during ordinary
          play, triggered by NLP_Core.py whenever an item-interaction verb names something that
          doesn't match any known item and nothing else in the input was understood either (see
          NLP_Core.py's own "improvisation_requested" note) -- conjuring a plausible prop is low
          risk, so no explicit invocation is required.
        - **Removal** (remove_entity_from_scene/_attempt_entity_removal) can target *any*
          entity, hand-authored included -- a much higher-risk operation -- so it's deliberately
          gated behind explicitly addressing ADaM by name (see DM_Help.py's own
          "removal_candidate" handling), never an automatic fallback.
    """

    def _on_improvisation_requested(self, data):
        """!
        @brief Event handler for "improvisation_requested" (NLP_Core.py's own last-resort
            fallback, published instead of "action_not_understood" when the whole turn would
            otherwise resolve to nothing at all). Asks generate_ad_hoc_item whether the named
            phrase is plausible; on decline/failure, publishes "action_not_understood" itself --
            exactly the outcome that would have happened without this feature, no new
            narration path needed. On success, places the new entity and resolves the
            *original* triggering intent against it, reusing the ordinary, otherwise-unchanged
            item-interaction pipeline wherever that pipeline actually supports the placement
            (see PLAYER_CENTRIC_INTENTS/GROUND_AWARE_INTENTS/TARGET_CENTRIC_INTENTS above for
            why the three-way split is necessary, not just tidy).

            "trade" short-circuits to a decline *before* ever asking the LLM if there's no
            current scene target at all (_get_target_name()) -- there's no one to buy from, the
            same "nothing here could plausibly be removed" short-circuit decide_entity_removal
            already applies on the removal side.
        @param data The "improvisation_requested" payload ({"intent", "phrase", "input"}).
        """
        intent = data.get("intent")
        phrase = data.get("phrase", "")
        input_text = data.get("input", "")

        target_name = self._get_target_name() if intent in TARGET_CENTRIC_INTENTS else None
        if intent in TARGET_CENTRIC_INTENTS and not target_name:
            self.event_bus.publish("action_not_understood", {"input": input_text, "score": 0.0})
            return

        result = generate_ad_hoc_item(
            phrase, intent, self._current_scene_description(),
            valid_equip_slots=self.get_equip_slots(self.player_name),
        )
        if not result.get("created"):
            self.event_bus.publish("action_not_understood", {"input": input_text, "score": 0.0})
            return

        entity = result["entity"]
        name = entity["name"]
        self.entities[name] = entity
        # Registers the new name/description into NLPCore's own item_embeddings/item_indices
        # (see NLP_Core.py's _on_item_catalog_updated) -- without this, a *later* reference to
        # the same item (ex: "drop the stone") would miss map_to_item again and either wrongly
        # re-trigger creation or dead-end, since NLPCore's embeddings are otherwise only ever
        # (re)built once, from "rules_loaded".
        self.event_bus.publish("item_catalog_updated", {
            "entities": [{"name": name, "description": entity.get("description", "")}],
        })

        if intent in TARGET_CENTRIC_INTENTS:
            # "trade" checks the *current target's* own inventory as its source (buying means
            # the seller has to have it) -- the LLM's own "location" choice is meaningless here
            # (there's no "on the ground" or "in the player's pocket" for something a shopkeeper
            # is about to sell), so it's ignored; the item is stocked directly into the target's
            # own inventory instead, then the ordinary "trade" dispatch (DM_Core.py) charges the
            # entity's own "value" as a price and transfers it exactly like a real one.
            self.entities.setdefault(target_name, {}).setdefault("inventory", []).append(name)
            self._on_item_interaction_detected({"intent": intent, "item_name": name, "input": input_text})
            return

        player_inventory = self.entities.setdefault(self.player_name, {}).setdefault("inventory", [])

        if intent in PLAYER_CENTRIC_INTENTS:
            # "give"/"equip"/"unequip"/"use"/"drop" all resolve against the *player's own*
            # inventory regardless of source/destination direction (DM_Inventory.py/DM_Core.py's
            # own _on_item_interaction_detected dispatcher never checks _current_ground_items()
            # for any of these) -- so the item lands directly in inventory either way, whether
            # the LLM's own "location" said "ground" or "inventory". A ground placement here
            # just means "you spot it and immediately act on it" collapses into one beat rather
            # than a separate explicit "take" first.
            player_inventory.append(name)
            self._on_item_interaction_detected({"intent": intent, "item_name": name, "input": input_text})
            return

        # intent in GROUND_AWARE_INTENTS ("examine"/"take") -- the only two intents the
        # ordinary dispatcher actually checks _current_ground_items() for.
        if result["location"] == "ground":
            self._current_ground_items().append(name)
            self._on_item_interaction_detected({"intent": intent, "item_name": name, "input": input_text})
            return

        # location == "inventory": conjured directly onto the player (ex: "check my pockets for
        # a match"). Re-dispatching "examine"/"take" here would incorrectly check the scene's
        # own default target's inventory instead of the player's (DM_Core.py's
        # _on_item_interaction_detected routes source_name = target_name for both), so this
        # publishes a bespoke success response directly instead of going through the dispatcher.
        player_inventory.append(name)
        self.event_bus.publish("item_interaction_resolved", {
            "intent": intent, "item_name": name, "input": input_text, "found": True,
            "description": entity.get("description", ""),
            "present_entities": list(self.scenario_entities),
        })
        self._publish_party_status()

    def remove_entity_from_scene(self, name):
        """!
        @brief The general-purpose removal primitive -- strips name from every list that
            currently makes it present/reachable, and records it so it can never respawn.
            Deliberately does *not* delete self.entities[name] outright -- leaves it orphaned/
            unreferenced, mirroring the existing precedent that a fully-consumed item
            (_consume_charge, DM_Inventory.py, charges hit 0, no replace_with) already just
            stops being referenced anywhere rather than being deleted from self.entities. This
            also means an orphaned entity self-cleans out of future saves for free (see
            DM_Persistence.py's own ad hoc entity collection, which is by *reachability*, not a
            scan of self.entities). Accepted consequence: removing a container also orphans (and
            so stops persisting) anything still listed in its own "inventory" -- intended, not a
            bug, given removal can target any entity including containers.
        @param name The entity to remove.
        @return {"removed": False, "reason": "cannot_remove_player"} if name is the player --
                a technical necessity (the engine assumes self.entities[self.player_name]
                exists everywhere), not a game-design restriction. Otherwise {"removed": True,
                "name": name}.
        """
        if name == self.player_name:
            return {"removed": False, "reason": "cannot_remove_player"}

        if name in self.scenario_entities:
            self.scenario_entities.remove(name)
        if name in self.persistent_entities:
            self.persistent_entities.remove(name)
        for instance_names in self.visited_rooms.values():
            if name in instance_names:
                instance_names.remove(name)

        if self.rooms:
            for room in self.rooms.values():
                ground = room.get("ground")
                if ground and name in ground:
                    ground.remove(name)
        else:
            ground = self.scenario.get("ground")
            if ground and name in ground:
                ground.remove(name)

        for instance_name in self._all_known_instance_names():
            entity = self.entities.get(instance_name, {})
            inventory = entity.get("inventory")
            if inventory and name in inventory:
                inventory.remove(name)
            equipped = entity.get("equipped")
            if equipped:
                for slot in [slot for slot, item in equipped.items() if item == name]:
                    del equipped[slot]

        # Prevents a scenario/room's own static "entities" list from re-instancing this name on
        # a later room revisit or a reload -- see DM_Rules.py's _instance_entities, the one
        # check point that consults this set.
        self.removed_entities.add(name)
        self.event_bus.publish("log_info", f"Removed '{name}' from the scene.")
        return {"removed": True, "name": name}

    def _attempt_entity_removal(self, input_text):
        """!
        @brief Called from DM_Help.py's _on_help_detected when NLP_Core.py's own removal
            keyword gate flagged the player's message to ADaM as a plausible removal request --
            never automatic (see this class's own module docstring for why). Builds the real,
            current universe of removable names (every present scene entity, every ground item,
            every known instance's own inventory/equipped item -- the player's own name always
            excluded, on top of the runtime guard remove_entity_from_scene itself also enforces)
            and asks decide_entity_removal to pick one, or decline.
        @param input_text The player's own raw message to ADaM.
        @return {"removed": False} on decline/nothing removable/failure. On success, the same
                {"removed": True, "name", "reason"} shape remove_entity_from_scene returns, with
                the LLM's own stated "reason" folded in.
        """
        removable = set(self.scenario_entities)
        if self.rooms:
            for room in self.rooms.values():
                removable.update(room.get("ground", []))
        else:
            removable.update(self.scenario.get("ground", []))
        for instance_name in self._all_known_instance_names():
            entity = self.entities.get(instance_name, {})
            removable.update(entity.get("inventory", []))
            removable.update(entity.get("equipped", {}).values())
        removable.discard(self.player_name)

        if not removable:
            return {"removed": False}

        decision = decide_entity_removal(input_text, self._current_scene_description(), sorted(removable))
        if not decision.get("removed"):
            return {"removed": False}

        outcome = self.remove_entity_from_scene(decision["name"])
        outcome["reason"] = decision.get("reason", "")
        return outcome
