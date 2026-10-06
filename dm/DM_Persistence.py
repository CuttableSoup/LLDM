import os

import resolution.Combat_Resolution as Combat_Resolution
from dm.DM_Improvisation import catalog_entry
from dm.DM_Types import DMCoreProtocol
from persistence.slot import Persistable, SaveError, restore_all, snapshot_all
from resolution.Law_Resolution import LAW_INSTANCE_FIELDS

PART = "dm_state"


class WorldSlice(Persistable):
    """!
    @brief The save-slot keys describing the world itself -- setting/scenario, the player's
        identity, where the party is, every visited location's cache, the order scopes were
        instanced in, ground items, ad hoc entities, and each instance's mutable state. It's a
        diff from a fresh instantiation, not a raw dump of core.entities (which also holds every
        static template): restore() re-runs load_rules and the scenario's own load path, replays
        instancing, then overlays this on top, so a save doesn't freeze stale stats if templates
        are edited between sessions.

        Everything here has to happen in one ordered sequence (load_rules, rename, re-instance,
        re-enter the saved location, overlay), which is why this is one participant rather than
        several -- see restore(). Must be listed after every slice a re-instancing reads
        (removed entities, known locations, the clock) and before every slice that needs the
        finished scene (session focus).
    """

    def __init__(self, core):
        self.core = core

    # -- save ---------------------------------------------------------------------------------

    def snapshot(self):
        core = self.core
        return {
            "setting": core.setting,
            "scenario_key": core.scenario_key,
            "player_name": core.player_name,
            "player_template": core.player_template,
            "scenario_entities": core.scenario_entities,
            "current_location_key": core.current_location_key,
            "current_room_key": core.current_room_key,
            "location_runtime": core.location_runtime,
            "ground": self._ground_state(),
            "instances": {name: self._instance_state(name) for name in core._all_known_instance_names()},
            "ad_hoc_entities": core._collect_ad_hoc_entities(),
            # The exact chronological sequence every location/room scope was first instanced in.
            # restore replays it verbatim so a save's own disambiguated "wolf"/"wolf_2" names
            # line up even when the player interleaved visits across two locations (see
            # DM_Rules.py's _instance_entities).
            "entity_instancing_order": [list(entry) for entry in core.entity_instancing_order],
        }

    def _ground_state(self):
        """!
        @brief Items dropped since the scenario started, keyed per location_key as
            {"ground": [...], "rooms": {room_key: [...]}} -- mirrors the location/room branch
            _current_ground_items (DM_Inventory.py) reads, and walks every location, not just
            the active one.
        """
        ground_state = {}
        for location_key, location in self.core.locations.items():
            location_ground = {}
            if location.get("ground"):
                location_ground["ground"] = list(location["ground"])
            rooms_ground = {
                room_key: list(room["ground"]) for room_key, room in location.get("rooms", {}).items()
                if room.get("ground")
            }
            if rooms_ground:
                location_ground["rooms"] = rooms_ground
            if location_ground:
                ground_state[location_key] = location_ground
        return ground_state

    def _instance_state(self, name):
        core = self.core
        entity = core.entities.get(name, {})
        state = {
            "hp": Combat_Resolution.get_current_hp(core.world, name),
            "active_conditions": entity.get("active_conditions", {}),
            "currency": entity.get("currency", 0),
            # A party member's running XP total (_award_xp_for_defeat) -- accumulated runtime
            # state, not just a template's starting value.
            "exp": entity.get("exp", 0),
            "inventory": entity.get("inventory", []),
            "equipped": entity.get("equipped", {}),
            "band": Combat_Resolution.get_band(core.world, name),
            # Runtime attitude drift: dialogue sentiment (nudge_attitude) and action-driven
            # (nudge_attitude_from_event) accumulate separately -- see get_attitude.
            "attitude_deltas": entity.get("attitude_deltas", {}),
            "action_attitude_deltas": entity.get("action_attitude_deltas", {}),
            # Runtime fields, None until something sets them: the spoken language, a planted
            # prompt directive (ex: a cast "suggestion"), and what this entity is mounted/
            # hitched to. Without "mount", a reload would silently unmount the player.
            "current_language": entity.get("current_language"),
            "prompt_directive": entity.get("prompt_directive"),
            "mount": entity.get("mount"),
        }
        # Crime knowledge, assault marks and disguises (DM_Law.py) -- only when present.
        state.update({field: entity[field] for field in LAW_INSTANCE_FIELDS if field in entity})
        if entity.get("generated"):
            # No static template to re-derive these from -- decided once at generation time
            # (partly at random), so they have to round-trip or a reload would lose them or,
            # worse, hand the entity a different random race/attitude.
            state["generated"] = True
            state["skills"] = entity.get("skills", {})
            state["max_hp"] = entity.get("max_hp", 0)
            state["name"] = entity.get("name", name)
            state["description"] = entity.get("description", "")
            state["qualities"] = entity.get("qualities", {})
            state["attitudes"] = entity.get("attitudes", {})
        elif entity.get("edited"):
            # A hand-authored entity edited via ADaM: its description otherwise re-derives from
            # the template on reload, silently reverting the edit. "elif" -- a generated
            # entity's description already saves above.
            state["edited"] = True
            state["description"] = entity.get("description", "")
        if name == core.player_name:
            # Character creation can diverge the player's skills/qualities/languages/abilities
            # from the template's own baseline, and nothing else holds the difference.
            state["skills"] = entity.get("skills", {})
            state["qualities"] = entity.get("qualities", {})
            state["languages"] = entity.get("languages", [])
            state["abilities"] = entity.get("abilities", [])
        # A live polymorph has already overwritten FORM_OVERRIDE_FIELDS on this instance;
        # active_conditions saves the _form snapshot to revert it, but re-instancing resets
        # every non-ad-hoc entity to its template's base form, so without this a mid-polymorph
        # save would come back in base form with the condition still ticking. "any" -- whichever
        # condition most recently applied a form is the one in effect.
        if any(entry.get("_form") for entry in entity.get("active_conditions", {}).values()):
            state["form_override"] = {
                field: entity[field] for field in Combat_Resolution.FORM_OVERRIDE_FIELDS if field in entity
            }
        return state

    # -- load ---------------------------------------------------------------------------------

    def restore(self, data):
        """!
        @brief Rebuilds the world from this slice's keys, in the one order that works:

            1. load_rules (fresh templates) -- not optional: core.entities holds static templates
               and live instances under the same keys, so load_scenario alone would re-instance
               from whatever is live. It also re-seeds the player under its original template key,
               so the player is re-resolved and the saved rename replayed before anything looks
               them up.
            2. load_scenario_definition, then every scope the save says was visited is
               re-instanced (instancing replay) *before* load_scenario/_enter_location look at
               location_runtime, so their "already cached" check reuses it rather than paying a
               second LLM round trip for a generate=true template. Everything passes
               skip_llm_generation=True -- the overlay below restores the real saved identity.
            3. load_scenario, then jump to the saved location/room, under _restoring_save so
               encounter on_enter rolls don't fire again.
            4. ad hoc entities, scenario_entities order, ground, then the per-instance overlay.
        """
        core = self.core
        saved_player_name = data.get("player_name", core.player_name)
        core.scenario_key = data.get("scenario_key", core.scenario_key)
        core.setting = data.get("setting", core.setting)
        # This session's outgoing player identity, if load runs against an already-booted
        # DMCore whose player was renamed earlier (the in-app Load menu) -- captured before
        # load_rules, which is about to supersede it.
        previous_player_name = core.player_name
        core.load_rules(os.path.join("Rules", core.setting))
        # load_rules rebuilt every entity under its authored name, including the player's
        # original template (still is_player). A leftover renamed identity would be a second
        # is_player entity, ambiguous with the template _rename_player_entity needs. It's a
        # leftover exactly when it differs from the template key the session started from.
        if previous_player_name != core.player_template:
            core.entities.pop(previous_player_name, None)
        # Older saves predate player_template -- fall back to the saved name.
        saved_template = data.get("player_template", saved_player_name)
        core.player_name = core._resolve_player_name(saved_template)
        core.player_template = core.player_name
        core._rename_player_entity(saved_player_name)
        core.load_scenario_definition(core.scenario_key)
        # load_scenario_definition rebuilt core.locations purely from TOML, which has no notion
        # of a mid-journey ambush's ephemeral scratch scene -- reinject it (DM_Travel.py stashed
        # it in pending_downtime for exactly this) before the saved-location jump below, so a
        # saved current_location_key pointing at it resolves to a real location.
        pending_site = (core.pending_downtime or {}).get("encounter_site")
        if pending_site:
            core.locations[pending_site["key"]] = pending_site
        core.validate_loaded_data()

        saved_instancing_order = data.get("entity_instancing_order")
        if saved_instancing_order:
            self._replay_ordered_instancing(saved_instancing_order)
        else:
            self._replay_nested_instancing(data.get("location_runtime", {}))

        core._restoring_save = True
        try:
            core.load_scenario(skip_llm_generation=True)

            saved_location_key = data.get("current_location_key")
            saved_room_key = data.get("current_room_key")
            if saved_location_key and saved_location_key != core.current_location_key:
                core._enter_location(saved_location_key, arrival_room=saved_room_key, skip_llm_generation=True)
            elif saved_room_key and saved_room_key != core.current_room_key:
                core.enter_room(saved_room_key, skip_llm_generation=True)
        finally:
            core._restoring_save = False

        self._restore_ad_hoc_entities(data)
        self._restore_scenario_entity_order(data)
        self._restore_ground(data)
        for name, state in data.get("instances", {}).items():
            entity = core.entities.get(name)
            if entity is not None:
                self._overlay_instance(name, entity, state)

    def _restore_ad_hoc_entities(self, data):
        """!
        @brief Ad hoc entities have no template, so each comes back as a full dict replacement.
            item_catalog_updated is published once as a batch so NLPCore's item embeddings catch
            up -- a reload never republishes rules_loaded, so nothing else would register them.
        """
        core = self.core
        saved_ad_hoc_entities = data.get("ad_hoc_entities")
        if not saved_ad_hoc_entities:
            return
        for name, entity_dict in saved_ad_hoc_entities.items():
            core.entities[name] = entity_dict
        core.event_bus.publish("item_catalog_updated", {
            "entities": [catalog_entry(name, entity_dict) for name, entity_dict in saved_ad_hoc_entities.items()],
        })

    def _restore_scenario_entity_order(self, data):
        """!
        @brief Re-adds every saved scene participant the fresh re-instancing didn't reproduce
            (exactly the ad hoc ones: a conjured creature/container/trap, or a temporary summon),
            then puts the whole list back in saved order. Guarded on "name in core.entities" so a
            stale reference (scenario file changed, corrupt ad hoc entry) is dropped rather than
            left dangling. Order matters: first-match scans (_get_target_name's "open it" default,
            _literal_dialogue_target's tie-break) read it, and an improvised container/trap is
            inserted at the FRONT when made.
        """
        core = self.core
        saved_order = data.get("scenario_entities", [])
        for name in saved_order:
            if name not in core.scenario_entities and name in core.entities:
                core.scenario_entities.append(name)
        position = {name: index for index, name in enumerate(saved_order)}
        core.scenario_entities.sort(key=lambda name: position.get(name, len(position)))

    def _restore_ground(self, data):
        """!
        @brief Only for location/room keys that still exist -- a stale key from a since-edited
            scenario file is dropped rather than resurrected.
        """
        core = self.core
        for location_key, saved_ground in data.get("ground", {}).items():
            location = core.locations.get(location_key)
            if location is None:
                continue
            if saved_ground.get("ground"):
                location["ground"] = list(saved_ground["ground"])
            for room_key, items in saved_ground.get("rooms", {}).items():
                room = location.get("rooms", {}).get(room_key)
                if room is not None:
                    room["ground"] = list(items)

    def _overlay_instance(self, name, entity, state):
        """!
        @brief Writes one saved instance's mutable state onto its freshly re-instanced entity.
            A saved instance with no post-reload match is skipped by the caller (ex: the
            scenario file changed) rather than crashing.
        """
        core = self.core
        entity["hp"] = state.get("hp", entity.get("max_hp", 0))
        entity["active_conditions"] = state.get("active_conditions", {})
        entity["attitude_deltas"] = state.get("attitude_deltas", {})
        entity["action_attitude_deltas"] = state.get("action_attitude_deltas", {})
        entity["current_language"] = state.get("current_language")
        entity["prompt_directive"] = state.get("prompt_directive")
        entity["mount"] = state.get("mount")
        for field in LAW_INSTANCE_FIELDS:
            if field in state:
                entity[field] = state[field]
            else:
                entity.pop(field, None)
        entity["currency"] = state.get("currency", entity.get("currency", 0))
        entity["exp"] = state.get("exp", entity.get("exp", 0))
        entity["inventory"] = state.get("inventory", entity.get("inventory", []))
        entity["equipped"] = state.get("equipped", entity.get("equipped", {}))
        entity["band"] = state.get("band", entity.get("band", 1))
        if state.get("generated"):
            entity["generated"] = True
            entity["skills"] = state.get("skills", {})
            entity["max_hp"] = state.get("max_hp", entity.get("max_hp", 0))
            entity["name"] = state.get("name", entity.get("name", name))
            entity["description"] = state.get("description", entity.get("description", ""))
            entity["qualities"] = state.get("qualities", entity.get("qualities", {}))
            entity["attitudes"] = state.get("attitudes", entity.get("attitudes", {}))
        elif state.get("edited"):
            entity["edited"] = True
            entity["description"] = state.get("description", entity.get("description", ""))
        if name == core.player_name:
            # The entity was just re-instanced from the template's own skills/qualities/
            # languages, discarding whatever character creation built.
            entity["skills"] = state.get("skills", entity.get("skills", {}))
            entity["qualities"] = state.get("qualities", entity.get("qualities", {}))
            entity["languages"] = state.get("languages", entity.get("languages", []))
            if "abilities" in state:
                entity["abilities"] = state["abilities"]
        # A saved form_override is reapplied on top the same way active_conditions was. A field
        # absent from it was absent on the live entity too (the form template didn't define
        # it), so it's popped rather than left at whatever the base template provides.
        if "form_override" in state:
            form_override = state["form_override"]
            for field in Combat_Resolution.FORM_OVERRIDE_FIELDS:
                if field in form_override:
                    entity[field] = form_override[field]
                else:
                    entity.pop(field, None)

    def _replay_ordered_instancing(self, saved_instancing_order):
        """!
        @brief Re-instances every location/room scope in the exact chronological order the live
            playthrough first instanced them in, rather than grouping each location's rooms
            together -- this is what keeps entity_occurrence_counts-driven "wolf"/"wolf_2"
            disambiguation correct across interleaved visits. Calls
            _instance_location_persistent_names/_instance_entities directly rather than
            _enter_location/_populate_room: this is a bulk re-derivation of every visited scope,
            not a live move, and must not re-trigger _enter_location's party-formation/
            current_target/encounter side effects anywhere but the actual current location.
        @param saved_instancing_order ["location", location_key] or ["room", location_key,
            room_key] entries, in order.
        """
        core = self.core
        # Only _enter_location/_populate_room's own cache-miss branches append to this on the
        # live path, so set it directly from the save's recorded sequence.
        core.entity_instancing_order = [tuple(entry) for entry in saved_instancing_order]

        for entry in saved_instancing_order:
            kind = entry[0]
            location_key = entry[1]
            location = core.locations.get(location_key)
            if location is None:
                continue
            # Instanced as if standing there -- a template's default languages come from the
            # current location's polity. (Found by playtest: after a reload, Sandpoint's sheriff
            # and jailer had no languages, so neither could witness a crime.)
            core.current_location_key = location_key
            cache = core.location_runtime.setdefault(location_key, {})
            if kind == "location":
                if "persistent_names" not in cache:
                    cache["persistent_names"] = core._instance_location_persistent_names(
                        location, skip_llm_generation=True,
                    )
            else:
                room_key = entry[2]
                room = location.get("rooms", {}).get(room_key)
                visited_rooms = cache.setdefault("visited_rooms", {})
                if room and room_key not in visited_rooms:
                    visited_rooms[room_key] = core._instance_entities(
                        room.get("entities", []), party_pool=cache.get("persistent_names", []),
                        skip_llm_generation=True,
                    )

    def _replay_nested_instancing(self, saved_location_runtime):
        """!
        @brief Fallback replay for a save with no entity_instancing_order: each location the save
            names, then all of that location's visited rooms together. Correct as long as the
            player never interleaved visits across two locations. Rebuilds entity_instancing_order
            to match, so a *later* reload has a self-consistent order to use.
        """
        core = self.core
        for location_key, saved_cache in saved_location_runtime.items():
            location = core.locations.get(location_key)
            if location is None:
                continue
            core.current_location_key = location_key
            cache = core.location_runtime.setdefault(location_key, {})
            cache["persistent_names"] = core._instance_location_persistent_names(
                location, skip_llm_generation=True,
            )
            core.entity_instancing_order.append(("location", location_key))
            if location.get("rooms"):
                cache["visited_rooms"] = {}
                for room_key in saved_cache.get("visited_rooms", {}):
                    room = location["rooms"].get(room_key)
                    if room:
                        cache["visited_rooms"][room_key] = core._instance_entities(
                            room.get("entities", []), party_pool=cache["persistent_names"],
                            skip_llm_generation=True,
                        )
                        core.entity_instancing_order.append(("room", location_key, room_key))


class PersistenceMixin(DMCoreProtocol):
    """!
    @brief Save/load entry points for DMCore (a mixin -- only ever composed into DMCore). The
        state itself belongs to the slices in self.save_parts (DMCore.__init__ lists them in
        restore order); this just snapshots them into / restores them from one "dm_state" part
        of a save slot (see persistence/slot.py and docs/persistence.md). LLMCore and GUICore
        write their own sibling parts of the same slot independently.
    """

    def _save_slot_dir(self, slot_name):
        """!
        @return The directory holding this slot's parts -- the slot store's own answer, so
            there's one owner of slot paths across all three cores.
        """
        return self.slot_store.slot_dir(slot_name)

    def _all_known_instance_names(self):
        """!
        @brief Every instance name whose state a save has to persist, across *every* location
            the player has ever visited (self.location_runtime), not just the active one -- each
            location's persistent_names plus, for a room-based location, every visited room's
            instance list. Without the full walk, saving in one place and reloading would
            silently forget state left behind elsewhere (a disarmed trap, an NPC already
            talked to).
        @return A list of instance names, player included, deduplicated.
        """
        seen = []
        for cache in self.location_runtime.values():
            for name in cache.get("persistent_names", []):
                if name not in seen:
                    seen.append(name)
            for instance_names in cache.get("visited_rooms", {}).values():
                for name in instance_names:
                    if name not in seen:
                        seen.append(name)
        return seen

    def _collect_ad_hoc_entities(self):
        """!
        @brief Every ad hoc entity (entity["ad_hoc"] = True) currently *reachable* -- a live
            scenario_entities participant, on some ground list (any location, any room), or in a
            known instance's inventory/equipped -- for a save to persist in full, since there's no
            static template to re-derive it from. Reachability, not a scan of self.entities, is
            deliberate: remove_entity_from_scene never deletes an entity, just unreferences it
            everywhere, so an orphan naturally drops out of future saves.

            Excludes "recent_damage_tags" (a plain set, not JSON-serializable, and cleared every
            round anyway).
        @return {name: full_entity_dict, ...} for every reachable ad hoc entity.
        """
        names = set(self.scenario_entities)
        for location in self.locations.values():
            names.update(location.get("ground", []))
            for room in location.get("rooms", {}).values():
                names.update(room.get("ground", []))
        for instance_name in self._all_known_instance_names():
            entity = self.entities.get(instance_name, {})
            names.update(entity.get("inventory", []))
            names.update(entity.get("equipped", {}).values())

        return {
            name: {k: v for k, v in self.entities[name].items() if k != "recent_damage_tags"}
            for name in names
            if self.entities.get(name, {}).get("ad_hoc")
        }

    def save_game(self, slot_name):
        """!
        @brief Writes this core's part of a save slot (dm_state) -- every slice's snapshot,
            merged. LLMCore independently saves its own sibling part for the same slot.
        @param slot_name The save slot's name.
        """
        self.slot_store.write(slot_name, PART, snapshot_all(self.save_parts))
        self.event_bus.publish("log_info", f"Game saved to slot '{slot_name}'.")
        # Distinct from the log_info line above (Debug-tab-only) -- GUI/Textual subscribe to
        # this directly so a save gets a plain visible confirmation in the main history pane
        # without spending an LLM call narrating something as mundane as "you saved the game."
        self.event_bus.publish("game_saved", {"slot": slot_name})

    def load_game(self, slot_name):
        """!
        @brief Restores the dm_state part of a slot: the whole part is read and validated
            (missing, corrupt, wrong format version) before anything live is touched, then every
            slice restores in list order, then the resumed scene is republished.

            Publishes "game_loaded" on success -- deliberately not "scenario_loaded", so LLMCore
            restores its own saved state silently instead of narrating a brand-new opening scene
            on every resume. Publishes "game_load_failed" {"slot", "reason"} if the part can't be
            read, so the player gets feedback rather than the request silently doing nothing.
        @param slot_name The save slot's name to load.
        """
        try:
            data = self.slot_store.read(slot_name, PART)
        except SaveError as error:
            message = f"No save slot named '{slot_name}'." if error.reason == "not_found" else (
                f"Save slot '{slot_name}' can't be loaded ({error})."
            )
            self.event_bus.publish("log_error", message)
            self.event_bus.publish("game_load_failed", {"slot": slot_name, "reason": error.reason})
            return

        restore_all(self.save_parts, data)

        # _enter_location/enter_room already published a roster mid-load, but every restored ad
        # hoc entity and every generated entity's saved name/description landed *after* that --
        # so the roster published then described the freshly-re-instanced scene, not the saved
        # one. Republishing makes the resumed scene's cast reach the narrator (DM_Rules.py's
        # _publish_scene_roster); the dirty guard lets it through because the prose really changed.
        self._publish_scene_roster()
        self.law_enforcement.resume_after_load()

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
