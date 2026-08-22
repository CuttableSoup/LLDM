import os

from AdHoc_Generation import (
    GROUND_AWARE_INTENTS, TARGET_CENTRIC_INTENTS, decide_entity_edit,
    decide_entity_removal, generate_ad_hoc_creature, generate_ad_hoc_item,
)
from DM_Types import DMCoreProtocol
from NPC_Generation import load_npc_keywords

# subtype values generate_ad_hoc_item may return that are placed as live, targetable scene
# participants (self.scenario_entities) rather than on the ground or in inventory -- a
# container/trap is opened/examined-as-itself the same way a hand-authored one is (see
# DM_Core.py's own _on_item_interaction_detected, which resolves "open"/"close" and a bare
# "examine <the target itself>" against self._get_target_name(), not a ground/inventory lookup).
# Unrelated to GROUND_AWARE_INTENTS/TARGET_CENTRIC_INTENTS (imported from AdHoc_Generation.py,
# above) -- Intent_Classification.py has no mirror of this one, so it stays local.
SCENE_PLACED_SUBTYPES = frozenset({"container", "trap"})


class ImprovisationMixin(DMCoreProtocol):
    """!
    @brief Ad hoc entity creation/removal/editing (DMCore mixin -- only ever composed into
        DMCore, never instantiated on its own; relies on self.entities/self.player_name/
        self.scenario_entities/self.persistent_entities/self.visited_rooms/self.rooms/
        self.scenario/self.removed_entities/self.current_target/self.setting/self.event_bus,
        set up by DMCore.__init__, plus RulesMixin's _current_scene_description/
        _place_new_entity/_unique_entity_key/_all_known_instance_names (PersistenceMixin),
        InventoryMixin's _current_ground_items/place_new_item,
        CombatMixin's get_equip_slots/get_challenge_rating, SocialMixin's is_hostile,
        MovementMixin's get_band, StatusMixin's apply_condition/dismiss_condition/get_current_hp,
        and DMCore's
        own _on_item_interaction_detected/_target_is_engaged. Inherits DMCoreProtocol purely
        so type checkers can resolve these shared attributes/cross-mixin methods -- see
        DM_Types.py. AdHoc_Generation.py is the pure, DMCore-independent LLM-calling half this
        mixin is glue for -- same split DM_NpcGeneration.py is to NPC_Generation.py.

        Two genuinely different risk profiles, not one symmetric mechanic -- each new
        capability slots into whichever of these two an item's own creation/removal already
        established:
        - **Automatic fallback**, no explicit invocation required (low risk): plain item
          creation, now extended to a container/trap (still generate_ad_hoc_item, just a
          subtype carrying its own [entity.test]) and to ambient scenery detail (a third
          "describe_scenery" outcome with no entity created at all) -- see
          _on_improvisation_requested.
        - **ADaM-gated**, behind explicitly addressing ADaM by name (higher risk -- can affect
          combat balance or mutate any existing entity, hand-authored included): entity removal
          (remove_entity_from_scene/_attempt_entity_removal), creature/NPC conjuring
          (_attempt_creature_conjuring -- a hostile one can fight, changing the scene's
          balance), and entity editing (_attempt_entity_edit -- can rewrite any entity's own
          description or apply/dismiss a condition on it). See DM_Help.py's own
          "removal_candidate"/"creature_candidate"/"edit_candidate" handling for how NLP_Core.py
          gates each of these behind a cheap local keyword pre-check.
    """

    def _on_improvisation_requested(self, data):
        """!
        @brief Event handler for "improvisation_requested" (NLP_Core.py's own last-resort
            fallback, published instead of "action_not_understood" when the whole turn would
            otherwise resolve to nothing at all). Asks generate_ad_hoc_item whether the named
            phrase is plausible; on decline/failure, publishes "action_not_understood" itself --
            exactly the outcome that would have happened without this feature, no new
            narration path needed. On a "scenery" outcome (the model's own describe_scenery
            choice -- ambient detail, not a discrete object), publishes a bespoke
            "item_interaction_resolved" with no entity created at all -- a pure flavor beat,
            nothing to persist, same treatment an ordinary "examine" description already gets.
            On an actual creation, places the new entity (via DM_Inventory.py's place_new_item
            for anything landing in an inventory -- see SCENE_PLACED_SUBTYPES above for the one
            placement that isn't) and resolves the *original* triggering intent against it,
            reusing the ordinary, otherwise-unchanged item-interaction pipeline uniformly for
            every placement, container/trap included -- DM_Core.py's own
            _on_item_interaction_detected resolves "examine"/"take" directly against the player
            whenever the item's already in their own inventory (its own docstring), which is
            what lets every placement branch below redispatch the same way instead of needing
            its own bespoke narration.

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
            valid_skill_names=self.skills.keys(),
        )
        if result.get("scenery"):
            self.event_bus.publish("item_interaction_resolved", {
                "intent": intent, "item_name": phrase, "input": input_text, "found": True,
                "description": result.get("description", ""),
                "present_entities": list(self.scenario_entities),
            })
            return
        if not result.get("created"):
            self.event_bus.publish("action_not_understood", {"input": input_text, "score": 0.0})
            return

        entity = result["entity"]
        name = self._unique_entity_key(entity["name"])
        entity["entity_id"] = name
        self.entities[name] = entity
        # Registers the new name/description into NLPCore's own item_embeddings/item_indices
        # (see NLP_Core.py's _on_item_catalog_updated) -- without this, a *later* reference to
        # the same item (ex: "drop the stone") would miss map_to_item again and either wrongly
        # re-trigger creation or dead-end, since NLPCore's embeddings are otherwise only ever
        # (re)built once, from "rules_loaded".
        self.event_bus.publish("item_catalog_updated", {
            "entities": [{"name": name, "description": entity.get("description", "")}],
        })

        # Decides where the newly-created entity physically lands; the actual narration/
        # mutation for the *original* triggering intent (equip/give/trade/take/...) is left to
        # the ordinary, unchanged item-interaction pipeline (DM_Core.py's own
        # _on_item_interaction_detected -- 180 lines of real currency/pricing/container-gating/
        # transfer logic this deliberately reuses rather than re-implementing narration from a
        # bespoke placement result) via one dispatch call below, shared by every branch,
        # including this one -- DM_Core.py's own source-resolution now recognizes an item
        # already sitting in the player's own inventory (see that method's own docstring), so
        # every placement path below can redispatch uniformly; none needs its own narration.
        if entity.get("subtype") in SCENE_PLACED_SUBTYPES:
            # A container/trap becomes a live, targetable scene participant, not a ground/
            # inventory item -- inserted at the front of scenario_entities so
            # self._get_target_name() ("the first non-party entity in scenario_entities order",
            # what "examine <the target itself>"/"open"/"close" all resolve against) picks it
            # immediately, even if some other non-party entity (ex: a hostile creature already
            # mid-fight) is also present. Also claims self.current_target (see
            # _claim_current_target_if_free) -- unlike _get_target_name(), a skill check against
            # the new entity's own [entity.test] (ex: picking its lock) resolves against
            # self.current_target, not _get_target_name(), so without this a freshly-conjured
            # container/trap could be examined/opened but never actually tested. band is set
            # explicitly here (unlike a plain ground/inventory item, which has no band of its
            # own at all) since this one becomes a real scenario_entities participant --
            # get_band/get_distance_between would otherwise silently default a bandless entity
            # to 1 regardless of where the player actually is, same as _attempt_creature_conjuring
            # already sets. _place_new_entity (DM_Rules.py) re-running entity_id/
            # self.entities[name] here is harmless -- both already hold these values from just
            # above -- but its setdefault on active_conditions is what preserves this entity's
            # own already-authored locked/closed or armed state (AdHoc_Generation.py) rather than
            # the unconditional overwrite _instance_entities uses for a fresh hand-authored
            # template.
            self._place_new_entity(name, entity, self.get_band(self.player_name))
            self.scenario_entities.insert(0, name)
            self._claim_current_target_if_free(name)
        elif intent in TARGET_CENTRIC_INTENTS:
            # "trade" checks the *current target's* own inventory as its source (buying means
            # the seller has to have it) -- the LLM's own "location" choice is meaningless here
            # (there's no "on the ground" or "in the player's pocket" for something a shopkeeper
            # is about to sell), so it's ignored; the item is stocked directly into the target's
            # own inventory instead, then the ordinary "trade" dispatch (DM_Core.py) charges the
            # entity's own "value" as a price and transfers it exactly like a real one.
            self.place_new_item(target_name, name)
        elif result["location"] == "ground" and intent in GROUND_AWARE_INTENTS:
            # The only two intents ("examine"/"take") the ordinary dispatcher actually checks
            # _current_ground_items() for -- everything else below lands in inventory instead.
            self._current_ground_items().append(name)
        else:
            # PLAYER_CENTRIC_INTENTS ("give"/"equip"/"unequip"/"use"/"drop", which resolve
            # against the player's own inventory regardless of source/destination direction --
            # DM_Inventory.py/DM_Core.py's own _on_item_interaction_detected dispatcher never
            # checks _current_ground_items() for any of these), or GROUND_AWARE_INTENTS with
            # location == "inventory" (conjured straight onto the player, ex: "check my pockets
            # for a match") -- both land in the player's own inventory the same way. A ground
            # placement for a player-centric intent just means "you spot it and immediately act
            # on it" collapses into one beat rather than a separate explicit "take" first.
            self.place_new_item(self.player_name, name)

        self._on_item_interaction_detected({"intent": intent, "item_name": name, "input": input_text})

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

        for location in self.locations.values():
            for scope in [location, *location.get("rooms", {}).values()]:
                ground = scope.get("ground")
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

    def _reachable_entity_names(self):
        """!
        @brief Every entity name the player could plausibly mean by pointing at "something
            here" -- every present scene entity, every ground item, every known instance's own
            inventory/equipped item -- the player's own name always excluded. Shared by both
            _attempt_entity_removal and _attempt_entity_edit, which otherwise ask the same "what
            exists to act on right now" question with different verbs (remove vs. edit); each
            still layers its own further narrowing on top (removal's own live-hostile subset,
            below, has no equivalent on the edit side).
        @return A fresh set (safe for the caller to mutate/narrow further).
        """
        reachable = set(self.scenario_entities)
        for location in self.locations.values():
            reachable.update(location.get("ground", []))
            for room in location.get("rooms", {}).values():
                reachable.update(room.get("ground", []))
        for instance_name in self._all_known_instance_names():
            entity = self.entities.get(instance_name, {})
            reachable.update(entity.get("inventory", []))
            reachable.update(entity.get("equipped", {}).values())
        reachable.discard(self.player_name)
        return reachable

    def _attempt_entity_removal(self, input_text):
        """!
        @brief Called from DM_Help.py's _on_help_detected when NLP_Core.py's own removal
            keyword gate flagged the player's message to ADaM as a plausible removal request --
            never automatic (see this class's own module docstring for why). Builds the real,
            current universe of removable names (_reachable_entity_names -- on top of the
            runtime guard remove_entity_from_scene itself also enforces), plus the live-hostile
            subset of it (is_hostile + a live-HP check), and asks
            decide_entity_removal to pick one, or decline -- see that function's own
            hostile_entities param for why this subset is computed and passed at all: without
            it, a real model complies with "get rid of that wolf, this fight is too hard"
            unconditionally, turning a free out-of-character request into a dice-free win
            against anything currently trying to kill the player.
        @param input_text The player's own raw message to ADaM.
        @return {"removed": False} on decline/nothing removable/failure. On success, the same
                {"removed": True, "name", "reason"} shape remove_entity_from_scene returns, with
                the LLM's own stated "reason" folded in.
        """
        removable = self._reachable_entity_names()

        if not removable:
            return {"removed": False}

        hostile = [
            candidate for candidate in removable
            if self.is_hostile(candidate, self.player_name) and self.get_current_hp(candidate) > 0
        ]
        decision = decide_entity_removal(
            input_text, self._current_scene_description(), sorted(removable), hostile_entities=hostile,
        )
        if not decision.get("removed"):
            return {"removed": False}

        outcome = self.remove_entity_from_scene(decision["name"])
        outcome["reason"] = decision.get("reason", "")
        return outcome

    def _claim_current_target_if_free(self, name):
        """!
        @brief Points self.current_target at name unless a fight is already genuinely engaged
            (a live, hostile-toward-the-player entity) -- shared by both container/trap
            placement and hostile creature conjuring below. Necessary because self.current_target
            is a different concept from self.scenario_entities membership/_get_target_name():
            _resolve_roll (DM_Core.py) resolves a scene-level [entity.test] check (ex: picking a
            lock, disarming a trap) against self.current_target specifically, not against
            whatever _get_target_name() would return -- so without this, a freshly-conjured
            container/trap could be examined/opened (which *do* go through _get_target_name())
            but never actually tested, and a freshly-conjured hostile creature couldn't be
            fought on the very next action. Deliberately never interrupts a fight already in
            progress -- conjuring a curiosity mid-combat shouldn't silently retarget the player
            away from what they're actually fighting (and, for a non-hostile creature, would
            also wrongly flip _on_turn_detected's own round-vs-action narration choice, since
            that decision reads self.current_target's own hostility).
        @param name The entity to claim as the current target, if nothing else already is.
        """
        if not self._target_is_engaged():
            self.current_target = name

    def _attempt_creature_conjuring(self, input_text):
        """!
        @brief Called from DM_Help.py's _on_help_detected when NLP_Core.py's own creature
            keyword gate flagged the player's message to ADaM as a plausible request to conjure
            a living creature/NPC -- never automatic (see this class's own module docstring for
            why). target_cr is the player's own current challenge rating (get_challenge_rating,
            CombatMixin) -- a single-target encounter framing appropriate for an ad hoc,
            mid-scene spawn, unlike real NPC generation's own party-pool resolution (see
            DM_NpcGeneration.py's _resolve_npc_target_cr), which isn't needed here since there's
            no entity_template's own target_cr field to resolve against. On success, disambiguates
            via RulesMixin's own _unique_entity_key and places the new entity via RulesMixin's
            own _place_new_entity (DM_Rules.py) -- the same entity_id/band/active_conditions
            primitive _instance_entities itself is built on -- then appends directly to
            self.scenario_entities, since _instance_entities' own occurrence-disambiguation/
            removed_entities-skip/NPC-generation/notice-roll wrapping around that primitive is
            only relevant at scenario/room load time (see CLAUDE.md's
            "Ad hoc entity creation and removal" -- the same reasoning generate_ad_hoc_item's own
            item placement already follows). A hostile creature also claims self.current_target (see
            _claim_current_target_if_free) -- so the very next player action can fight it
            without first having to explicitly retarget.
        @param input_text The player's own raw message to ADaM.
        @return {"created_creature": False} on decline/no keyword catalog/failure. On success,
                {"created_creature": True, "name"}.
        """
        npc_keywords = load_npc_keywords(os.path.join("Rules", self.setting))
        target_cr = self.get_challenge_rating(self.player_name)
        result = generate_ad_hoc_creature(
            input_text, self._current_scene_description(), target_cr, npc_keywords,
        )
        if not result.get("created"):
            return {"created_creature": False}

        entity = result["entity"]
        name = self._unique_entity_key(entity["name"])
        self._place_new_entity(name, entity, self.get_band(self.player_name))
        self.scenario_entities.append(name)
        self.event_bus.publish("item_catalog_updated", {
            "entities": [{"name": name, "description": entity.get("description", "")}],
        })

        if self.is_hostile(name, self.player_name):
            self._claim_current_target_if_free(name)

        return {"created_creature": True, "name": name}

    def _attempt_entity_edit(self, input_text):
        """!
        @brief Called from DM_Help.py's _on_help_detected when NLP_Core.py's own edit keyword
            gate flagged the player's message to ADaM as a plausible edit request -- never
            automatic (see this class's own module docstring for why: an unrestricted edit
            target is just as risky as an unrestricted removal target). Builds the same
            editable-name universe _attempt_entity_removal builds (_reachable_entity_names) and
            asks decide_entity_edit to pick one and how to change it, or decline.
        @param input_text The player's own raw message to ADaM.
        @return {"edited": False} on decline/nothing editable/failure/no actual change. On
                success, {"edited": True, "name", "reason"}.
        """
        editable = self._reachable_entity_names()

        if not editable:
            return {"edited": False}

        decision = decide_entity_edit(input_text, self._current_scene_description(), sorted(editable))
        if not decision.get("edited"):
            return {"edited": False}

        name = decision["name"]
        entity = self.entities.get(name)
        if entity is None:
            return {"edited": False}

        changed = False
        if decision.get("new_description"):
            entity["description"] = decision["new_description"]
            # No static TOML template carries a description edit -- tags this the same way
            # "generated"/"ad_hoc" mark their own extra saved fields, so DM_Persistence.py's
            # save_game knows to persist "description" explicitly instead of letting it
            # silently re-derive from (and revert to) the static template on reload.
            entity["edited"] = True
            changed = True
            self.event_bus.publish("item_catalog_updated", {
                "entities": [{"name": name, "description": entity.get("description", "")}],
            })
        if decision.get("apply_condition"):
            self.apply_condition(name, decision["apply_condition"], duration="permanent", dismiss="")
            changed = True
        if decision.get("dismiss_condition"):
            self.dismiss_condition(name, decision["dismiss_condition"])
            changed = True

        if not changed:
            return {"edited": False}

        return {"edited": True, "name": name, "reason": decision.get("reason", "")}
