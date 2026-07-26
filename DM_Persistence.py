import json
import os

from DM_Types import DMCoreProtocol


class PersistenceMixin(DMCoreProtocol):
    """!
    @brief Save/load persistence to Saves/<slot_name>/dm_state.json (DMCore mixin -- only ever
        composed into DMCore, never instantiated on its own; relies on
        self.entities/self.event_bus/self.player_name/self.round_number/self.current_target/
        self.scenario_key/self.scenario_entities/self.scenario, set up by DMCore.__init__).
        load_game/save_game call back into RulesMixin's load_rules/load_scenario_definition/
        load_scenario and StatusMixin's get_current_hp, mirroring DMCore.__init__'s own
        bootstrap sequence. Inherits DMCoreProtocol purely so type checkers can resolve
        these shared attributes/cross-mixin methods -- see DM_Types.py.
    """

    def _save_slot_dir(self, slot_name):
        """!
        @brief Resolves a save slot name to its directory under Saves/. LLMCore computes this
            same path independently (it has no reference to DMCore, by design -- the two
            only ever communicate through events) and must be kept in sync with it, since
            both write sibling files into the same slot directory.
        @param slot_name The save slot's name, as given by the player.
        @return The absolute directory path for this slot. os.path.basename strips any
                path-separator components first, so a slot name can't escape Saves/ (ex: a
                slot literally named "../../etc" resolves to Saves/etc, not outside Saves/).
        """
        base_dir = os.path.dirname(os.path.abspath(__file__))
        safe_name = os.path.basename(slot_name.strip()) or "unnamed"
        return os.path.join(base_dir, "Saves", safe_name)

    def _all_known_instance_names(self):
        """!
        @brief Every instance name whose state save_game needs to persist -- just
            scenario_entities for a plain single-room scenario, but for a multi-room dungeon
            (see DM_Rules.py's room-graph notes) every persistent entity (the player, plus
            anything else declared at [scenario].entities, ex: crypt.toml's "thane" -- never
            themselves part of any room's own instance list, see DM_Rules.py's _populate_room)
            plus the union of *every visited room's* own instance list, not only the room the
            player happens to be standing in right now. Without the union, saving mid-dungeon
            and reloading would silently forget that an earlier room's trap was already
            disarmed or its creature already killed -- the room itself would still be marked
            visited (see visited_rooms) and so wouldn't be re-instanced fresh, but nothing
            would have restored its saved state either.
        @return A list of instance names, player included, deduplicated.
        """
        if not self.rooms:
            return list(self.scenario_entities)
        seen = list(self.persistent_entities)
        for instance_names in self.visited_rooms.values():
            for name in instance_names:
                if name not in seen:
                    seen.append(name)
        return seen

    def save_game(self, slot_name):
        """!
        @brief Writes this core's mechanical state to Saves/<slot_name>/dm_state.json -- a
            diff from a fresh instantiation (round_number, scenario_entities, and each
            instance's hp/active_conditions/currency/inventory/band), not a raw dump of
            self.entities, which also holds every static template. Loading re-instantiates
            fresh from Rules/Fantasy TOML and overlays this diff on top, so a save doesn't
            freeze stale stats if templates are edited between sessions. LLMCore
            independently saves its own sibling file (context_window) for the same slot --
            see CLAUDE.md's "Saving and loading" section for why the two cores don't share
            one combined file. For a multi-room dungeon, also saves current_room_key and
            visited_rooms so a resumed save picks up in the same room with every previously
            visited room's state intact (see _all_known_instance_names/load_game).
        @param slot_name The save slot's name (used as a directory name under Saves/).
        """
        slot_dir = self._save_slot_dir(slot_name)
        os.makedirs(slot_dir, exist_ok=True)
        instance_names = self._all_known_instance_names()
        data = {
            "version": 1,
            "scenario_key": self.scenario_key,
            "player_name": self.player_name,
            "round_number": self.round_number,
            "current_target": self.current_target,
            "scenario_entities": self.scenario_entities,
            "current_room_key": self.current_room_key,
            "visited_rooms": self.visited_rooms,
            "instances": {
                name: {
                    "hp": self.get_current_hp(name),
                    "active_conditions": self.entities.get(name, {}).get("active_conditions", {}),
                    "currency": self.entities.get(name, {}).get("currency", 0),
                    "inventory": self.entities.get(name, {}).get("inventory", []),
                    "band": self.get_band(name),
                }
                for name in instance_names
            },
        }
        with open(os.path.join(slot_dir, "dm_state.json"), "w") as f:
            json.dump(data, f, indent=2)
        self.event_bus.publish("log_info", f"Game saved to slot '{slot_name}'.")
        # Distinct from the log_info line above (Debug-tab-only) -- GUI/Textual subscribe to
        # this directly so a save gets a plain visible confirmation in the main history pane
        # without spending an LLM call narrating something as mundane as "you saved the game."
        self.event_bus.publish("game_saved", {"slot": slot_name})

    def load_game(self, slot_name):
        """!
        @brief Restores mechanical state from Saves/<slot_name>/dm_state.json: re-reads every
            Rules/Fantasy/*.toml template fresh via load_rules, then reloads the saved
            scenario via the same load_scenario_definition/load_scenario path __init__
            uses, then overlays each instance's saved hp/active_conditions/currency/
            inventory on top. A saved instance with no matching entity after re-instancing
            (ex: the scenario file changed) is skipped rather than crashing.

            The load_rules call is not optional: self.entities holds both static templates
            and live instances under the same keys (a single-occurrence instance like
            "wolf" *overwrites* self.entities["wolf"] the moment load_scenario first runs --
            see "Scenario instancing" in CLAUDE.md), so calling load_scenario() alone would
            re-instance from whatever's currently sitting in self.entities, which after the
            very first load is the *live, possibly-mutated instance*, not the pristine
            template. Re-running load_rules first is what actually makes a resumed save
            pick up current TOML stats rather than freezing stale in-memory ones.

            Publishes "game_loaded" on success -- deliberately not "scenario_loaded", so
            LLMCore restores its own saved state silently instead of narrating a brand-new
            opening scene on every resume. Publishes "game_load_failed" if the slot doesn't
            exist, so the player gets feedback rather than the request silently doing
            nothing (same rule action_not_understood already follows for unmatched input).

            For a multi-room dungeon, load_scenario() (below) only instances the *starting*
            room -- every other room the save file says was already visited gets
            re-instanced fresh from templates here too (same reasoning as the load_rules call
            above: re-instancing from current TOML, not whatever's live in memory), then
            enter_room moves into whichever room the player actually saved in. This has to
            happen before the "instances" overlay loop below, so saved hp/inventory/etc. has
            a real entity dict to land on for every visited room, not just the starting one.
        @param slot_name The save slot's name to load.
        """
        path = os.path.join(self._save_slot_dir(slot_name), "dm_state.json")
        if not os.path.exists(path):
            self.event_bus.publish("log_error", f"No save slot named '{slot_name}'.")
            self.event_bus.publish("game_load_failed", {"slot": slot_name, "reason": "not_found"})
            return

        with open(path, "r") as f:
            data = json.load(f)

        self.player_name = data.get("player_name", self.player_name)
        self.round_number = data.get("round_number", 0)
        self.scenario_key = data.get("scenario_key", self.scenario_key)
        self.load_rules(os.path.join("Rules", "Fantasy"))
        self.load_scenario_definition(self.scenario_key)
        self.load_scenario()

        if self.rooms:
            for room_key in data.get("visited_rooms", {}):
                if room_key == self.current_room_key:
                    continue  # load_scenario() (above) already instanced the starting room
                room = self.rooms.get(room_key)
                if room:
                    self.visited_rooms[room_key] = self._instance_entities(room.get("entities", []))
            saved_room_key = data.get("current_room_key")
            if saved_room_key and saved_room_key != self.current_room_key:
                # arrival_band doesn't matter here -- the "instances" overlay loop below
                # restores the player's real saved band on top regardless.
                self.enter_room(saved_room_key)

        for name, state in data.get("instances", {}).items():
            entity = self.entities.get(name)
            if entity is None:
                continue
            entity["hp"] = state.get("hp", entity.get("max_hp", 0))
            entity["active_conditions"] = state.get("active_conditions", {})
            entity["currency"] = state.get("currency", entity.get("currency", 0))
            entity["inventory"] = state.get("inventory", entity.get("inventory", []))
            entity["band"] = state.get("band", entity.get("band", 1))

        # load_scenario()/enter_room (above) already reset current_target to a freshly-
        # computed default -- overlay the saved value on top, same pattern as player_name, so
        # a resumed fight keeps targeting whoever it was actually fighting rather than
        # snapping back to the default.
        self.current_target = data.get("current_target", self.current_target)

        self.event_bus.publish("log_info", f"Game loaded from slot '{slot_name}'.")
        self.event_bus.publish("game_loaded", {
            "slot": slot_name,
            "name": self._current_scene_name(),
            "description": self._current_scene_description(),
            "characters": self._describe_scenario_characters(),
        })
        # Restores GUICore's Party tab to the resumed save's own state -- see
        # _publish_party_status (DM_Core.py) for why this isn't just "rules_loaded" again.
        self._publish_party_status()

    def _on_save_requested(self, data):
        """!
        @brief Event handler for a save request (from NLPCore's text intercept or a GUI/Textual
            button, both publishing the same event) -- a missing/blank slot name just logs
            a warning rather than saving to some default location unasked.
        @param data The "save_requested" payload ({"slot": slot_name}).
        """
        slot_name = data.get("slot")
        if not slot_name:
            self.event_bus.publish("log_warning", "save_requested with no slot name; ignored.")
            return
        self.save_game(slot_name)

    def _on_load_requested(self, data):
        """!
        @brief Event handler for a load request, mirroring _on_save_requested.
        @param data The "load_requested" payload ({"slot": slot_name}).
        """
        slot_name = data.get("slot")
        if not slot_name:
            self.event_bus.publish("log_warning", "load_requested with no slot name; ignored.")
            return
        self.load_game(slot_name)
