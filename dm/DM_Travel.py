import math
import re

from dm.DM_Types import DMCoreProtocol

# A single, reused scratch location key for a mid-journey ambush -- never a freshly-minted
# key per pause (see _enter_encounter_site), so a long playthrough with many interrupted
# trips doesn't accumulate one throwaway self.locations entry per ambush.
ROAD_ENCOUNTER_KEY = "__road_encounter"


class TravelMixin(DMCoreProtocol):
    """!
    @brief Grid-based overworld travel (DMCore mixin -- only ever composed into DMCore, never
        instantiated on its own; relies on self.rules/self.entities/self.locations/
        self.scenario_entities/self.current_location_key/self.known_locations/self.event_bus/
        self.rooms/self.player_name/self.watch_rotation_index/self.pending_downtime/
        self._is_party_member/self.is_hostile/self.get_current_hp/self._enter_location/
        self._current_room/self._describe_scenario_characters/self.advance_blocks/
        self.is_daytime/self._resolve_one_encounter/self.resolve_action/
        self.apply_condition/self._any_hostile_present/self._resume_pending_downtime, set up
        by DMCore.__init__ or implemented by DM_Rules.py/DM_Encounters.py/DM_Time.py/
        DM_Combat.py/DM_Status.py/DM_Core.py). See docs/downtime.md's "Travel" for the design
        this implements.

        An optional "grid" field ({x, y}) on a [[location]] table opts it into this whole
        connectivity model, replacing its authored [[location.exit]]/"return_to" entirely (see
        DM_Movement.py's _resolve_travel_intent, which branches on the *current* location's own
        "grid" before ever consulting this mixin) -- a location with no "grid" field keeps
        resolving through the ordinary exit graph completely unchanged. For the gridded subset,
        any known location (self.known_locations -- populated on first visit, or seeded ahead of
        time by a scenario's own [scenario].known_locations, ex: a map the player starts with)
        is reachable directly by name/alias from any other gridded location, no exit to author.

        Distance is Euclidean between two locations' own "grid" coordinates; blocks (see
        DM_Time.py) are that distance divided by the party's own travel speed, rounded up -- no
        such thing as arriving a fraction of a block early, the same no-fractional-units
        rounding rule HP/dice/bands already follow. Each block spent samples whichever
        world_map.toml [[region]] contains the midpoint of that block's own leg of the journey
        (resolve_region_environment) and, if one matches, rolls that environment's own day or
        night encounter table (picked off self.is_daytime() at the start of that block) via the
        exact same _resolve_one_encounter [[location.encounter]] already uses -- true per-block
        sampling along the line, not a coarser "first half is the origin's environment" split, so
        a multi-region journey (not exercised by today's single-region shipped map, but already
        correct for one) rolls from every region proportional to how much of the line it covers.

        Arrival (_enter_location(destination_key)) no longer happens up front -- it's deferred
        until every block actually clears without interruption (_finish_pending_travel), so a
        mid-journey encounter is genuinely mid-journey, not narrated as already having arrived.
        A hostile block pauses the whole trip (self.pending_downtime, "kind": "travel") and
        moves the party into a small ephemeral scratch scene (_enter_encounter_site,
        ROAD_ENCOUNTER_KEY) to actually fight in -- see docs/downtime.md's "Pausing for a
        fight" for the full design (why this doesn't go through the ordinary _enter_location,
        how it survives save/load, and how the trip resumes automatically once the threat
        clears). [[location.encounter]]'s own on_enter roll still fires separately, unaffected,
        once real arrival finally happens.

        A night block (self.is_daytime() false) whose own encounter roll actually placed a
        hostile entity is followed by a night watch check (_roll_night_watch,
        docs/extended-goals.md's "Night watch and surprise") -- a day block, or a night block
        that rolled "nothing"/a friendly name/pure flavor, never rolls a watch at all, since
        there'd be nothing for a failed watch to matter against. This whole "one block against
        one environment" step is factored into _resolve_environment_block so DM_Time.py's own
        rest can reuse it verbatim against a single fixed point (_current_environment) instead
        of a line of travel -- the one place this mixin is itself relied on by another, rather
        than only ever the other way around.
    """

    def _travel_rules(self):
        """!
        @brief rules.toml's own [travel] table, defaulted the same "still works unauthored"
            shape every other optional rules.toml table falls back to (ex: [time]).
        @return {default_speed}.
        """
        return self.rules.get("travel", {"default_speed": 4})

    def _party_travel_speed(self):
        """!
        @brief The whole party's travel speed for grid distance/block math -- the slowest
            currently-present is_player/is_party member's own "travel_speed" field (an
            entity/race-level override), falling back to [travel]'s own default_speed for
            anyone who doesn't author one.
        @return The lowest travel speed among present party members (default_speed if somehow
            none are present at all).
        """
        default_speed = self._travel_rules().get("default_speed", 4)
        speeds = [
            self.entities.get(name, {}).get("travel_speed", default_speed)
            for name in self.scenario_entities
            if self._is_party_member(name)
        ]
        return min(speeds) if speeds else default_speed

    def resolve_region_environment(self, x, y):
        """!
        @brief Finds whichever world_map.toml [[region]] contains grid point (x, y) --
            first match wins (regions aren't expected to overlap, but nothing enforces it).
        @param x Grid x coordinate.
        @param y Grid y coordinate.
        @return The containing region's own "environment" name, or None if no authored region
            contains this point -- the "no environment" default that's what "safe" looks like
            everywhere in this design (no watch check, no encounter roll).
        """
        for region in self.rules.get("region", []):
            if (
                region.get("min_x", float("-inf")) <= x <= region.get("max_x", float("inf"))
                and region.get("min_y", float("-inf")) <= y <= region.get("max_y", float("inf"))
            ):
                return region.get("environment")
        return None

    def _find_environment(self, name):
        """!
        @brief Looks up one environments.toml [[environment]] entry by its own "name".
        @param name An environment name (ex: "plains"), as named by a [[region]]'s own
            "environment" field.
        @return The environment's own table, or None if no environment by that name is loaded.
        """
        for environment in self.rules.get("environment", []):
            if environment.get("name") == name:
                return environment
        return None

    def _current_environment(self):
        """!
        @brief The environment (if any) at the current location's own fixed grid point --
            rest's own "what environment am I resting inside of" lookup (DM_Time.py's rest),
            the single-point counterpart to _resolve_grid_travel_intent's own per-block *line*
            sampling: nothing moves during rest, so there's only ever one point to sample, not
            a leg of a journey. A location with no "grid" field at all (ex: any dungeon room,
            or an overworld location simply never placed on world_map.toml) resolves to None
            here, same as a gridded point that happens to fall in an unmapped gap -- both are
            the same "absence of an environment" default that's already what "safe" looks like
            everywhere else in this design (no watch check, no encounter roll).
        @return An environments.toml [[environment]] entry, or None.
        """
        grid = self.locations.get(self.current_location_key, {}).get("grid")
        if not grid:
            return None
        environment_name = self.resolve_region_environment(grid["x"], grid["y"])
        return self._find_environment(environment_name) if environment_name else None

    def _resolve_environment_block(self, environment):
        """!
        @brief Resolves one elapsed block's worth of exposure to a single environment -- the
            "roll this environment's own day/night encounter table off self.is_daytime() at
            this moment, then a night watch check if that roll turned out hostile" logic
            shared by grid travel's own per-block loop (_resolve_grid_travel_intent) and rest
            (DM_Time.py's rest), so the two don't each re-derive it independently and risk
            drifting apart.
        @param environment One environments.toml [[environment]] entry (ex: from
            _find_environment/_current_environment) -- never None; both callers only invoke
            this once they've already confirmed one actually applies.
        @return True if this block's own encounter roll turned out hostile (regardless of
            whether a night watch check followed, or how it went) -- callers that don't care
            can simply ignore it.
        """
        is_night = not self.is_daytime()
        table_key = "night_encounter" if is_night else "day_encounter"
        hostile = self._resolve_one_encounter({"encounter": environment.get(table_key, [])})
        if is_night and hostile:
            self._roll_night_watch(environment)
        return hostile

    def _resolve_grid_destination(self, input_text):
        """!
        @brief Finds a known, gridded [[location]] named in input_text -- the grid-model
            counterpart to DM_Movement.py's own _resolve_location_exit, searching every
            *other* gridded location's own "name"/"aliases" (not just this one location's own
            authored exit list, since a gridded location has none) whole-word/case-insensitive,
            same "search input for a known name" pattern used throughout this codebase.
        @param input_text The raw (lowercased, prefix-stripped) player input.
        @return The matched destination location's own key, or None if nothing known and
            gridded is named.
        """
        for key, location in self.locations.items():
            if key == self.current_location_key or "grid" not in location:
                continue
            if key not in self.known_locations:
                continue
            candidates = [location.get("name", "")] + list(location.get("aliases", []))
            for phrase in candidates:
                if phrase and re.search(rf"\b{re.escape(phrase.lower())}\b", input_text or ""):
                    return key
        return None

    def _roll_night_watch(self, environment):
        """!
        @brief Resolves whether a night block's own hostile encounter catches the party
            unprepared (docs/extended-goals.md's "Night watch and surprise") -- only ever
            called once a night block's own encounter roll has already placed a hostile
            entity, since "success, or a non-hostile roll, changes nothing" leaves nothing for
            a watch check to matter against otherwise.

            Whichever currently-present is_party member (player included -- on any given
            night, the watch could fall to either) is next up in self.watch_rotation_index's
            own fixed rotation rolls "observation" against environment's own
            "watch_difficulty"; the index only advances on a night a watch is actually rolled,
            not every night, so it still cycles through the same party across however many
            hostile nights occur, just not in lockstep with elapsed time. A party of one --
            nobody to rotate a watch to while the sole traveler sleeps -- always fails outright,
            no roll attempted (the alternative, treating solo rest as automatically safe, would
            make traveling alone strictly safer than with company, inverting the usual "safety
            in numbers" logic this design deliberately keeps).

            A failed (or skipped) watch applies "surprised" (rules.toml's own [[condition]]) to
            every present is_party member -- the whole party was caught off guard, not just
            whoever stood watch -- cleared after that fight's own first round of upkeep
            (_expire_surprised_if_due, DM_Status.py).
        @param environment The night block's own environments.toml entry, read for its
            "watch_difficulty".
        @return True if the party was surprised.
        """
        roster = [name for name in self.scenario_entities if self._is_party_member(name)]
        if len(roster) <= 1:
            surprised = True
        else:
            watcher = roster[self.watch_rotation_index % len(roster)]
            self.watch_rotation_index += 1
            result = self.resolve_action(watcher, "observation", environment.get("watch_difficulty", 0))
            surprised = not result["success"]
        if surprised:
            for name in roster:
                self.apply_condition(name, "surprised", duration="1 round", dismiss="")
        return surprised

    def _resolve_grid_travel_intent(self, input_text, resolved):
        """!
        @brief Handles "travel" from a gridded current location -- see this class's own
            docstring for the full model. Denied (reason "no_exit") if input_text doesn't name
            a known, gridded destination, or (reason "blocked_by_enemies") under the exact same
            room-occupant gate DM_Movement.py's own exit-graph path already runs. The
            "downtime_interrupted" denial/opportunistic-resume check lives one level up, in
            DM_Movement.py's own _resolve_travel_intent -- see that method's own docstring for
            why it has to run before the grid/non-grid branch decision, not here.
        @param input_text The raw (lowercased, prefix-stripped) player input.
        @param resolved The item_interaction_resolved publisher closure from
            DMCore._on_item_interaction_detected.
        """
        origin_grid = self.locations.get(self.current_location_key, {}).get("grid")
        destination_key = self._resolve_grid_destination(input_text)
        if destination_key is None:
            resolved(False, reason="no_exit")
            return

        if self.rooms and self._any_hostile_present():
            resolved(False, reason="blocked_by_enemies")
            return

        destination_grid = self.locations[destination_key]["grid"]
        distance = math.hypot(destination_grid["x"] - origin_grid["x"], destination_grid["y"] - origin_grid["y"])
        speed = self._party_travel_speed()
        blocks = max(1, math.ceil(distance / speed)) if speed > 0 else 1

        self.pending_downtime = {
            "kind": "travel", "destination_key": destination_key,
            "origin_grid": origin_grid, "destination_grid": destination_grid,
            "blocks_total": blocks, "blocks_done": 0, "distance": distance,
        }
        result = self._advance_pending_travel()
        if result["interrupted"]:
            return
        resolved(True, **{k: v for k, v in result.items() if k != "interrupted"})

    def _advance_pending_travel(self):
        """!
        @brief Runs (or resumes) self.pending_downtime's own per-block loop from wherever
            "blocks_done" left off -- the exact same midpoint-sampling/_resolve_environment_block
            math _resolve_grid_travel_intent always ran, just able to stop partway through and
            be called again later. A block whose own roll turns out hostile updates
            "blocks_done" and returns immediately ({"interrupted": True}) instead of
            continuing -- moving into the ephemeral encounter site first (_enter_encounter_site)
            if this is the first interruption of this trip (a second ambush during an
            already-paused trip is already standing there, nothing to re-enter). Running every
            remaining block clean hands off to _finish_pending_travel for real arrival.
        @return _finish_pending_travel's own result, or {"interrupted": True}.
        """
        pending = self.pending_downtime
        origin_grid = pending["origin_grid"]
        destination_grid = pending["destination_grid"]
        blocks_total = pending["blocks_total"]

        for i in range(pending["blocks_done"], blocks_total):
            # Midpoint of this block's own leg of the straight line from origin to
            # destination -- true per-block sampling, not a coarser origin/destination split.
            t = (i + 0.5) / blocks_total
            point_x = origin_grid["x"] + (destination_grid["x"] - origin_grid["x"]) * t
            point_y = origin_grid["y"] + (destination_grid["y"] - origin_grid["y"]) * t
            hostile = False
            environment_name = self.resolve_region_environment(point_x, point_y)
            if environment_name:
                environment = self._find_environment(environment_name)
                if environment:
                    hostile = self._resolve_environment_block(environment)
            self.advance_blocks(1)
            if hostile:
                pending["blocks_done"] = i + 1
                if self.current_location_key != ROAD_ENCOUNTER_KEY:
                    destination_name = self.locations.get(
                        pending["destination_key"], {},
                    ).get("name", "your destination")
                    self._enter_encounter_site(
                        f"Caught in the open, still short of {destination_name}.",
                    )
                return {"interrupted": True}

        return self._finish_pending_travel()

    def _finish_pending_travel(self):
        """!
        @brief Completes a self.pending_downtime travel once every block has cleared without
            interruption -- the real _enter_location(destination_key) arrival this class's own
            docstring describes, deferred until now instead of running up front. Clears
            self.pending_downtime first, so nothing downstream (ex: a status/on_enter program
            the arrival itself triggers) can observe a stale "still traveling" state.
        @return {"interrupted": False, ...} with exactly the fields
                intents/travel.py's narrate_travel expects (location_name/location_description/
                room_name/room_description/characters/blocks_spent/time), plus "distance" for
                the mechanics-only side of the original resolved() payload.
        """
        pending = self.pending_downtime
        self.pending_downtime = None
        self._enter_location(pending["destination_key"])
        new_location = self.locations.get(self.current_location_key, {})
        new_room = self._current_room()
        return {
            "interrupted": False,
            "location_name": new_location.get("name", ""),
            "location_description": new_location.get("description", ""),
            "room_name": new_room.get("name", "") if new_room else "",
            "room_description": new_room.get("description", "") if new_room else "",
            "characters": self._describe_scenario_characters(),
            "blocks_spent": pending["blocks_total"],
            "distance": round(pending["distance"], 1),
            "time": self.get_time_state(),
        }

    def _enter_encounter_site(self, description):
        """!
        @brief Moves the party into a small, ephemeral scratch scene for a mid-journey ambush
            -- ROAD_ENCOUNTER_KEY, a single fixed key reused by every such pause rather than a
            freshly-minted one each time. Deliberately does NOT go through the ordinary
            _enter_location: naming an already-live entity (an ally like crypt.toml's "thane")
            in a *new* location's own "entities" list would re-instance it as a fresh
            occurrence-disambiguated copy of its template (_instance_entities), silently
            orphaning the real live instance and its current hp/active_conditions -- exactly
            the bug _instance_location_persistent_names's own docstring already warns about for
            the player specifically. _enter_location's freeform branch would also reset
            scenario_entities to just [player_name] (list(self.persistent_entities)), losing
            both the ally and whatever _resolve_one_encounter just placed. This changes only
            "where we are" for narration/lookup purposes -- scenario_entities/
            persistent_entities are left completely untouched, so the party (and the
            encountered hostile) stay exactly who they already were.

            Also stashes this site's own dict into self.pending_downtime["encounter_site"] --
            self.locations gets fully rebuilt from TOML alone on a later load_game, so without
            this a saved-mid-ambush current_location_key would resolve to nothing on reload
            (see DM_Persistence.py's load_game, which reads this same key back out to
            reinject the site before it re-runs _enter_location on the saved location key).
        @param description Flavor text for this specific ambush (ex: naming the destination
            the party hasn't reached yet).
        """
        site = {
            "key": ROAD_ENCOUNTER_KEY, "name": "the road",
            "description": description, "entities": [],
        }
        self.locations[ROAD_ENCOUNTER_KEY] = site
        self.pending_downtime["encounter_site"] = site
        self.current_location_key = ROAD_ENCOUNTER_KEY
        self.known_locations.add(ROAD_ENCOUNTER_KEY)
        self.rooms = {}
        self.current_room_key = None
        self.visited_rooms = {}
        self.entities[self.player_name]["band"] = 1
