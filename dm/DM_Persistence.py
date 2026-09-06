import json
import os

import resolution.Combat_Resolution as Combat_Resolution
from dm.DM_Types import DMCoreProtocol
from paths import PROJECT_ROOT


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
        safe_name = os.path.basename(slot_name.strip()) or "unnamed"
        return os.path.join(PROJECT_ROOT, "Saves", safe_name)

    def _all_known_instance_names(self):
        """!
        @brief Every instance name whose state save_game needs to persist, across *every*
            location the player has ever visited this playthrough (self.location_runtime --
            see DM_Rules.py's _enter_location), not just the one currently active -- each
            location's own "persistent_names" (its own entities list, ex: the player, and for
            a room-based location anything else meant to persist across its whole room graph,
            ex: crypt's "thane") plus, for a room-based location, the union of *every visited
            room's* own instance list within it. Without walking every location, saving in one
            place and reloading would silently forget the state of anything left behind in a
            location the player already passed through (an earlier room's trap already
            disarmed, an NPC already talked to) -- that location stays visited (so it isn't
            re-instanced fresh), but nothing would have restored its saved state either.
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
        @brief Every ad hoc entity (DM_Improvisation.py/DM_Summoning.py -- entity["ad_hoc"] =
            True) currently *reachable* -- a live self.scenario_entities participant (a
            conjured creature/container/trap, or a temporary summon -- see "Summoning" in
            CLAUDE.md), present in some ground list (across every location and, within a
            room-based one, every room), or in some known instance's own inventory/equipped
            mapping -- for save_game to persist in full (there's no static TOML template to
            re-derive an ad hoc entity's fields from on reload, unlike every other instance's
            own diff-based state). Reachability, not a scan of self.entities, is deliberate:
            remove_entity_from_scene never deletes an entity outright, just unreferences it
            everywhere (including self.scenario_entities), so an orphaned ad hoc entity
            naturally stops being collected here too -- no separate cleanup needed for it to
            fall out of future saves.

            Excludes "recent_damage_tags" (calculate_damage, DM_Combat.py) from the copied
            dict -- a plain Python set, not JSON-serializable, and deliberately ephemeral
            (cleared every round by run_round_upkeep) regardless; every other entity's own
            save path already excludes it too (it's not part of _instance_state's own
            whitelisted fields), this is just the one path that would otherwise carry it
            through via a raw dict copy.
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
        @brief Writes this core's mechanical state to Saves/<slot_name>/dm_state.json -- a
            diff from a fresh instantiation (round_number, current_block (the block clock --
            see DM_Time.py/docs/downtime.md, a fully separate axis from round_number),
            scenario_entities, and each
            instance's hp/active_conditions/currency/exp/inventory/equipped/band/
            attitude_deltas/action_attitude_deltas/current_language/prompt_directive/mount),
            not a raw dump
            of self.entities, which also holds every static template. Loading re-instantiates
            fresh from Rules/Fantasy TOML and overlays this diff on top, so a save doesn't
            freeze stale stats if templates are edited between sessions. LLMCore
            independently saves its own sibling file (context_window) for the same slot --
            see CLAUDE.md's "Saving and loading" section for why the two cores don't share
            one combined file. Also saves "current_location_key" and "location_runtime" (every
            visited location's own {persistent_names, visited_rooms} cache -- see DM_Rules.py's
            _enter_location) so a resumed save picks up in the same location/room, with every
            previously visited place's state intact (see _all_known_instance_names/load_game).

            Also saves "equipped" per instance -- unlike inventory (the item names), the
            [entity.equipped] slot mapping otherwise re-derives from the static template's own
            hand-authored mapping on reload, silently discarding whatever was actually equipped
            at save time for any entity that re-equipped differently mid-session.

            Also saves "ground" -- items dropped since the scenario started, which
            _current_ground_items (DM_Inventory.py) stores directly on the current room's own
            table (or the current location's own, for a freeform location), neither of which
            is otherwise part of any instance's own state. Keyed per location_key, each holding
            its own flat "ground" list and/or a "rooms" dict of room_key -> ground list --
            walks every location the same way _collect_ad_hoc_entities already does, not just
            the currently active one.

            A generated entity (DM_NpcGeneration.py -- entity["generated"] = True) also gets
            its own skills/max_hp/name/description/qualities/attitudes saved, since (unlike
            every other instance) it has no static TOML template to re-derive those from on
            load -- they were decided once, at generation time (some of them randomly, per
            entity_template's own varied fields -- see NPC_Generation.py's
            resolve_varied_value), and have to round-trip explicitly or a reload would either
            silently lose them or (worse) hand the entity a *different* random race/attitude
            than the one actually saved. Abilities/equipment aren't generated at all (still
            decided by whoever authors the entity_template, same as any hand-authored entity
            -- see CLAUDE.md's "NPC generation"), so there's nothing dynamic to save there.

            Also saves "ad_hoc_entities" (see _collect_ad_hoc_entities) -- every reachable
            ad hoc entity's own *complete* dict, not a diff, since (unlike a generated NPC,
            which still has a real entity_template to fall back on) an ad hoc entity has no
            static template at all to re-derive anything from on reload. And "removed_entities"
            -- every name ever forcibly removed via ImprovisationMixin.remove_entity_from_scene
            (DM_Improvisation.py), so a reload doesn't let a scenario/room's own static
            "entities" list respawn something the player (or ADaM, on their behalf) removed.
            And "known_locations" (DM_Travel.py, docs/downtime.md's "Travel") -- every gridded
            (or not) location key ever entered, so a reload doesn't forget which overworld
            destinations grid-based travel has already unlocked by name.

            Also saves "entity_instancing_order" (DM_Rules.py's self.entity_instancing_order) --
            the exact chronological sequence every location/room scope was first instanced in,
            live. load_game replays this verbatim rather than its own nested "each location,
            then all of that location's rooms" loop, so a save's own disambiguated "wolf"/
            "wolf_2" instance names still line up correctly even when the player interleaved
            visits across two different locations (see DM_Rules.py's _instance_entities' own
            docstring). Absent from a save written before this field existed -- load_game falls
            back to its previous replay order for those.
        @param slot_name The save slot's name (used as a directory name under Saves/).
        """
        slot_dir = self._save_slot_dir(slot_name)
        os.makedirs(slot_dir, exist_ok=True)
        instance_names = self._all_known_instance_names()

        def _instance_state(name):
            entity = self.entities.get(name, {})
            state = {
                "hp": self.get_current_hp(name),
                "active_conditions": entity.get("active_conditions", {}),
                "currency": entity.get("currency", 0),
                # A party member's own running XP total (_award_xp_for_defeat, DM_Combat.py --
                # see docs/combat.md's "Experience (XP)") -- genuinely accumulated runtime
                # state, same unconditional per-instance treatment currency gets, not just the
                # static starting value a hand-authored template's own "exp" field provides.
                "exp": entity.get("exp", 0),
                "inventory": entity.get("inventory", []),
                "equipped": entity.get("equipped", {}),
                "band": self.get_band(name),
                # Runtime dialogue-sentiment drift (DM_Social.py's nudge_attitude) -- genuinely
                # dynamic for every instance, hand-authored or not, so it's saved unconditionally
                # here rather than gated behind the generated/edited special cases below.
                "attitude_deltas": entity.get("attitude_deltas", {}),
                # Runtime action-driven drift (DM_Social.py's nudge_attitude_from_event -- combat/
                # theft/favor) -- tracked in its own accumulator, independent of attitude_deltas
                # above (see get_attitude), so it round-trips the same unconditional way.
                "action_attitude_deltas": entity.get("action_attitude_deltas", {}),
                # The player's own currently-spoken language (DM_Dialogue.py's
                # _resolve_language_intent) -- runtime state, absent (None) until the player
                # actually switches at least once, same unconditional per-instance treatment
                # attitude_deltas gets rather than a player-only special case.
                "current_language": entity.get("current_language"),
                # A planted directive (Social_Resolution.py's set_prompt_directive, ex: a
                # successfully-cast "suggestion") -- runtime state, absent (None) until something
                # actually plants one, same unconditional per-instance treatment current_language
                # gets.
                "prompt_directive": entity.get("prompt_directive"),
                # Who/what this entity is currently mounted on/hitched to (DM_Movement.py's
                # "mount"/"dismount"/"hitch"/"unhitch" -- see entity_schema.toml's own "mount"),
                # a string or list, absent (None) until something actually sets one -- same
                # unconditional per-instance treatment current_language/prompt_directive get.
                # Without this, reloading a save would silently unmount the player with no
                # explanation (see docs/downtime.md's "Mounts and conveyance").
                "mount": entity.get("mount"),
            }
            if entity.get("generated"):
                state["generated"] = True
                state["skills"] = entity.get("skills", {})
                state["max_hp"] = entity.get("max_hp", 0)
                state["name"] = entity.get("name", name)
                state["description"] = entity.get("description", "")
                state["qualities"] = entity.get("qualities", {})
                state["attitudes"] = entity.get("attitudes", {})
            elif entity.get("edited"):
                # A hand-authored entity edited via ADaM (DM_Improvisation.py's
                # _attempt_entity_edit) -- "description" doesn't otherwise round-trip for a
                # non-ad_hoc/non-generated instance (it just re-derives from the static
                # template on reload), so it has to be saved explicitly here or the edit would
                # silently revert on the next load. "elif", not "if" -- a generated entity's
                # own description already saves above; the two flags never need to stack.
                state["edited"] = True
                state["description"] = entity.get("description", "")
            # A live polymorph/shapeshift (Combat_Resolution.py's "form" condition field) has
            # already overwritten FORM_OVERRIDE_FIELDS on this instance; "active_conditions"
            # above saves the _form snapshot needed to revert it, but load_scenario/_enter_
            # location/enter_room re-instance every non-ad-hoc entity fresh from its own static
            # template first (see load_game, below) -- without this, a mid-polymorph save would
            # come back in base form with the condition still live, silently losing the
            # transformation until it happens to expire/dismiss. "any" rather than gating on a
            # single condition name -- whichever condition most recently applied a form is the
            # one currently in effect, and only its overridden fields are still live on the
            # entity regardless of which condition's own _form entry they came from.
            if any(entry.get("_form") for entry in entity.get("active_conditions", {}).values()):
                state["form_override"] = {
                    field: entity[field] for field in Combat_Resolution.FORM_OVERRIDE_FIELDS if field in entity
                }
            return state

        ground_state = {}
        for location_key, location in self.locations.items():
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

        data = {
            "version": 2,
            "setting": self.setting,
            "scenario_key": self.scenario_key,
            "player_name": self.player_name,
            "round_number": self.round_number,
            "current_block": self.current_block,
            "watch_rotation_index": self.watch_rotation_index,
            "pending_downtime": self.pending_downtime,
            "current_target": self.current_target,
            "scenario_entities": self.scenario_entities,
            "current_location_key": self.current_location_key,
            "current_room_key": self.current_room_key,
            "location_runtime": self.location_runtime,
            "ground": ground_state,
            "instances": {name: _instance_state(name) for name in instance_names},
            "ad_hoc_entities": self._collect_ad_hoc_entities(),
            "removed_entities": list(self.removed_entities),
            "known_locations": list(self.known_locations),
            "entity_instancing_order": [list(entry) for entry in self.entity_instancing_order],
        }
        with open(os.path.join(slot_dir, "dm_state.json"), "w") as f:
            json.dump(data, f, indent=2)
        self.event_bus.publish("log_info", f"Game saved to slot '{slot_name}'.")
        # Distinct from the log_info line above (Debug-tab-only) -- GUI/Textual subscribe to
        # this directly so a save gets a plain visible confirmation in the main history pane
        # without spending an LLM call narrating something as mundane as "you saved the game."
        self.event_bus.publish("game_saved", {"slot": slot_name})

    def _replay_ordered_instancing(self, saved_instancing_order):
        """!
        @brief load_game's preferred replay path -- re-instances every location/room scope in
            the exact chronological order the original live playthrough first instanced them in
            (DM_Rules.py's self.entity_instancing_order, round-tripped through the save as
            plain lists), rather than grouping every location's own rooms together the way
            self.location_runtime's own dict shape would otherwise suggest. This is what keeps
            DM_Rules.py's self.entity_occurrence_counts-driven "wolf"/"wolf_2" disambiguation
            correct even when the player interleaved visits across two different locations --
            see _instance_entities' own docstring. Deliberately calls
            _instance_location_persistent_names/_instance_entities directly, the same as
            _replay_nested_instancing below, rather than _enter_location/_populate_room -- this
            is a from-scratch bulk re-derivation of *every* visited scope, not a live move, and
            must not re-trigger _enter_location's own party-formation/current_target/random-
            encounter side effects for anywhere other than the player's actual current location.
        @param saved_instancing_order The save file's own "entity_instancing_order" list --
            ["location", location_key] or ["room", location_key, room_key] entries, in order.
            Guaranteed non-empty by the caller (an empty list means no save this recent has ever
            been written, which can't happen -- __init__ always enters a start location).
        """
        # _instance_location_persistent_names/_instance_entities don't append to
        # self.entity_instancing_order themselves (only _enter_location/_populate_room's own
        # cache-miss branches do, for the live path) -- set it directly from the save's own
        # recorded sequence up front, since that's exactly what it should end up holding.
        self.entity_instancing_order = [tuple(entry) for entry in saved_instancing_order]

        for entry in saved_instancing_order:
            kind = entry[0]
            location_key = entry[1]
            location = self.locations.get(location_key)
            if location is None:
                continue
            cache = self.location_runtime.setdefault(location_key, {})
            if kind == "location":
                if "persistent_names" not in cache:
                    cache["persistent_names"] = self._instance_location_persistent_names(
                        location, skip_llm_generation=True,
                    )
            else:
                room_key = entry[2]
                room = location.get("rooms", {}).get(room_key)
                visited_rooms = cache.setdefault("visited_rooms", {})
                if room and room_key not in visited_rooms:
                    visited_rooms[room_key] = self._instance_entities(
                        room.get("entities", []), party_pool=cache.get("persistent_names", []),
                        skip_llm_generation=True,
                    )

    def _replay_nested_instancing(self, saved_location_runtime):
        """!
        @brief load_game's fallback replay path, for a save written before self.entity_
            instancing_order existed (see _replay_ordered_instancing, above): re-instances each
            location the save's own "location_runtime" names, and, for a room-based location,
            all of that location's own visited rooms together, right after. Correct as long as
            the player never interleaved visits across two different locations -- the same
            limitation this whole mechanism carried before self.entity_instancing_order was
            introduced. Rebuilds self.entity_instancing_order to match this same replay order
            (rather than leaving it at load_scenario_definition's own empty reset) -- a save
            written before this field existed has no better history to fall back on, but at
            least carries a self-consistent order forward for _replay_ordered_instancing to use
            on any *later* reload.
        @param saved_location_runtime The save file's own "location_runtime" dict.
        """
        for location_key, saved_cache in saved_location_runtime.items():
            location = self.locations.get(location_key)
            if location is None:
                continue
            cache = self.location_runtime.setdefault(location_key, {})
            cache["persistent_names"] = self._instance_location_persistent_names(
                location, skip_llm_generation=True,
            )
            self.entity_instancing_order.append(("location", location_key))
            if location.get("rooms"):
                cache["visited_rooms"] = {}
                for room_key in saved_cache.get("visited_rooms", {}):
                    room = location["rooms"].get(room_key)
                    if room:
                        cache["visited_rooms"][room_key] = self._instance_entities(
                            room.get("entities", []), party_pool=cache["persistent_names"],
                            skip_llm_generation=True,
                        )
                        self.entity_instancing_order.append(("room", location_key, room_key))

    def load_game(self, slot_name):
        """!
        @brief Restores mechanical state from Saves/<slot_name>/dm_state.json: re-reads every
            Rules/Fantasy/*.toml template fresh via load_rules, then reloads the saved
            scenario via the same load_scenario_definition/load_scenario path __init__
            uses, then overlays each instance's saved hp/active_conditions/currency/
            inventory/equipped on top. A saved instance with no matching entity after
            re-instancing (ex: the scenario file changed) is skipped rather than crashing.

            "removed_entities" is restored *before* load_scenario_definition/load_scenario run
            (see below), since DM_Rules.py's _instance_entities consults self.removed_entities
            while re-instancing -- restoring it any later would let a removed hand-authored
            entity respawn on this very reload.

            Every location the save file's own "location_runtime" says was ever visited gets
            re-instanced fresh from templates *before* load_scenario() runs -- each location's
            own "entities" (its persistent_names) once, and, for a room-based location, each of
            its own visited rooms' entities once, mirroring exactly how a single room's own
            entities were already re-derived from the room's static list rather than trusting
            the saved instance names directly (same reasoning the load_rules call below already
            follows: re-instance from current TOML, not whatever's live in memory). load_rules
            (via load_scenario_definition, called just above) resets self.entity_occurrence_
            counts/self.entity_instancing_order to {}/[], and this loop below is the only thing
            that advances them before load_scenario() runs. The save's own "entity_instancing_
            order" (present on every save written by this version) drives the replay directly,
            in the exact chronological order those scopes were first instanced in live -- so the
            disambiguated "wolf"/"wolf_2" names this produces line up with the saved per-instance
            overlay below even if the player interleaved visits across two different locations,
            not just within one (see DM_Rules.py's _instance_entities' own docstring). A save
            written before that field existed has none; the loop falls back to its own previous
            "each location, then all of that location's own rooms" replay order for those, which
            stays exactly as correct as it always was outside that interleaved case. This
            populates self.location_runtime *before* load_scenario()/_enter_location ever look at
            it, so their own "already cached, don't re-instance" check finds it and reuses it
            instead of paying for a second real LLM round trip on a generate=true template. Then
            load_scenario() lands at the scenario's own start_location; if the save says the
            player was actually somewhere else, _enter_location (or, within the same location,
            the existing enter_room) jumps there -- arrival_band/room don't matter for either
            jump, since the "instances" overlay loop below restores the player's real saved
            band/room-state on top regardless.

            "ground" is restored right after, walking every location the save file remembers
            (only for location/room keys that still exist; a stale key from a since-edited
            scenario file is dropped rather than resurrecting a stale reference), mirroring
            exactly where save_game read it from and _current_ground_items (DM_Inventory.py)
            itself looks for it during play.

            "ad_hoc_entities" is restored alongside "ground" -- a full dict replacement per
            entity (there's no template to overlay onto), then "item_catalog_updated" is
            published once, as a batch, so NLPCore's own item_embeddings/item_indices catch up
            (a reload never republishes "rules_loaded", so nothing else would ever re-register
            them -- see NLP_Core.py's own _on_item_catalog_updated). Right after, every name in
            the save's own "scenario_entities" that isn't already present (having just been
            written into self.entities above, or restored to self.entities some other way) is
            appended back onto the live self.scenario_entities -- load_scenario()/_enter_location/
            enter_room only ever re-derive scenario_entities from *static* scenario/room data,
            so without this, any ad hoc entity that was a live scene participant (a conjured
            creature/container/trap, or a temporary summon) would vanish from the resumed scene
            even though its own full dict was just restored into self.entities a moment ago.

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

            Every re-instancing call below passes skip_llm_generation=True: a generate=true
            template (DM_NpcGeneration.py) would otherwise pay for a real LLM round trip here
            just to have its result immediately overwritten by the saved skills/max_hp/name/
            description a few lines down -- skip_llm_generation routes straight to the
            offline fallback path instead (instant, no network dependency), and the overlay
            loop below restores the entity's *actual* saved identity on top regardless.
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
        self.current_block = data.get("current_block", 0)
        self.watch_rotation_index = data.get("watch_rotation_index", 0)
        self.pending_downtime = data.get("pending_downtime")
        self.scenario_key = data.get("scenario_key", self.scenario_key)
        self.setting = data.get("setting", self.setting)
        # Must precede load_scenario_definition/load_scenario -- see this method's own
        # docstring for why _instance_entities needs this available before it ever runs.
        self.removed_entities = set(data.get("removed_entities", []))
        # Grid-based travel's own knowledge gate (see DM_Travel.py/docs/downtime.md's "Travel")
        # -- restored before load_scenario() runs below, same reasoning removed_entities just
        # above follows, though nothing here actually consults it until a later travel attempt;
        # load_scenario()'s own known_locations seeding re-unions harmlessly on top of this.
        self.known_locations = set(data.get("known_locations", []))
        self.load_rules(os.path.join("Rules", self.setting))
        self.load_scenario_definition(self.scenario_key)
        # load_scenario_definition just rebuilt self.locations purely from TOML, which has no
        # notion of a mid-journey ambush's own ephemeral scratch scene -- reinject it (see
        # DM_Travel.py's _enter_encounter_site, which stashed this same dict into
        # pending_downtime for exactly this) *before* the saved_location_key branch below runs,
        # so a saved current_location_key pointing at it resolves to a real location instead of
        # silently degrading to an empty {} one.
        pending_site = (self.pending_downtime or {}).get("encounter_site")
        if pending_site:
            self.locations[pending_site["key"]] = pending_site
        self.validate_loaded_data()

        saved_instancing_order = data.get("entity_instancing_order")
        if saved_instancing_order:
            self._replay_ordered_instancing(saved_instancing_order)
        else:
            self._replay_nested_instancing(data.get("location_runtime", {}))

        self.load_scenario(skip_llm_generation=True)

        saved_location_key = data.get("current_location_key")
        saved_room_key = data.get("current_room_key")
        if saved_location_key and saved_location_key != self.current_location_key:
            self._enter_location(saved_location_key, arrival_room=saved_room_key, skip_llm_generation=True)
        elif saved_room_key and saved_room_key != self.current_room_key:
            self.enter_room(saved_room_key, skip_llm_generation=True)

        saved_ad_hoc_entities = data.get("ad_hoc_entities")
        if saved_ad_hoc_entities:
            for name, entity_dict in saved_ad_hoc_entities.items():
                self.entities[name] = entity_dict
            self.event_bus.publish("item_catalog_updated", {
                "entities": [
                    {"name": name, "description": entity_dict.get("description", "")}
                    for name, entity_dict in saved_ad_hoc_entities.items()
                ],
            })

        # Re-adds every entity the save's own "scenario_entities" remembered as a live scene
        # participant that the fresh re-instancing above didn't already reproduce -- exactly
        # the ad hoc ones (a conjured creature/container/trap, or a temporary summon; see
        # "Summoning"/"Ad hoc entity creation and removal" in CLAUDE.md), since a hand-authored
        # entity's own presence already re-derives correctly from the static scenario/room
        # data load_scenario()/_enter_location/enter_room just walked above. Guarded on
        # "name in self.entities" so a stale reference (ex: the scenario file changed between
        # saves, or a name whose own ad_hoc_entities entry is missing/corrupt) is silently
        # dropped rather than adding a dangling scenario_entities entry nothing can resolve.
        # Appended in saved order, after whatever's already present -- exact position doesn't
        # matter (nothing reads scenario_entities order as meaningful; initiative decides
        # actual turn order every round regardless), only presence does.
        for name in data.get("scenario_entities", []):
            if name not in self.scenario_entities and name in self.entities:
                self.scenario_entities.append(name)

        for location_key, saved_ground in data.get("ground", {}).items():
            location = self.locations.get(location_key)
            if location is None:
                continue
            if saved_ground.get("ground"):
                location["ground"] = list(saved_ground["ground"])
            for room_key, items in saved_ground.get("rooms", {}).items():
                room = location.get("rooms", {}).get(room_key)
                if room is not None:
                    room["ground"] = list(items)

        for name, state in data.get("instances", {}).items():
            entity = self.entities.get(name)
            if entity is None:
                continue
            entity["hp"] = state.get("hp", entity.get("max_hp", 0))
            entity["active_conditions"] = state.get("active_conditions", {})
            entity["attitude_deltas"] = state.get("attitude_deltas", {})
            entity["action_attitude_deltas"] = state.get("action_attitude_deltas", {})
            entity["current_language"] = state.get("current_language")
            entity["prompt_directive"] = state.get("prompt_directive")
            entity["mount"] = state.get("mount")
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
            # Mirrors the save-side comment above -- entity has just been re-instanced fresh
            # from its own static template (base form), so a saved form_override has to be
            # reapplied on top the same way active_conditions was, or a mid-polymorph save
            # would load back in base form with the shapeshifting condition still ticking.
            # Absent from FORM_OVERRIDE_FIELDS means it was absent on the live entity too (the
            # form template didn't define it) -- popped again here rather than left at
            # whatever the freshly re-instanced base template happens to provide.
            if "form_override" in state:
                form_override = state["form_override"]
                for field in Combat_Resolution.FORM_OVERRIDE_FIELDS:
                    if field in form_override:
                        entity[field] = form_override[field]
                    else:
                        entity.pop(field, None)

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
