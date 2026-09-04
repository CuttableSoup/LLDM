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
        self._is_party_member/self.is_hostile/self.get_current_hp/self._resolve_mount_targets/
        self._enter_location/
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

        Distance is Euclidean between two locations' own "grid" coordinates. Each block of travel
        covers the party's own base speed times whatever _effective_speed_multiplier applies at
        the party's current leading-edge position -- a [[road]] segment's own multiplier if one
        reaches that point, else whichever [[region]] contains it names as its own "terrain"
        (terrain.toml), else the plain, unmodified 1.0 every point got before either existed (see
        docs/downtime.md's "Terrain, roads, and polities"). blocks_total isn't a single fixed
        number computed up front the way it once was -- _advance_pending_travel's own while loop
        discovers it block by block as distance_covered climbs toward the total, since terrain
        along the way can slow (or a road speed up) how far each block actually reaches; a route
        with no authored terrain/road anywhere along it still reduces to exactly the old "distance
        / speed, rounded up" arithmetic. _route_is_passable is checked once, up front, before any
        of this runs at all -- a straight line crossing terrain the whole traveling party's own
        _resolve_conveyance_tags can't satisfy denies the entire attempt (reason
        "impassable_terrain"), no partial routing or pathfinding around it.

        Each block spent also samples whichever [[region]] contains the *midpoint* of that one
        block's own leg of the journey (resolve_region_environment) and, if one matches, rolls
        that environment's own day or night encounter table (picked off self.is_daytime() at the
        start of that block) via the exact same _resolve_one_encounter [[location.encounter]]
        already uses -- true per-block sampling along the line, not a coarser "first half is the
        origin's environment" split, so a multi-region journey rolls from every region
        proportional to how much of the line it covers. A [[region]]'s own "polity" (polities.toml)
        is unrelated to any of this block-by-block machinery -- it only ever seeds a freshly-
        instanced entity's default language (DM_Rules.py's _instance_entities) and names itself in
        arrival narration.

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

    def _resolve_travel_speed(self, entity_name, _visited=None):
        """!
        @brief entity_name's own effective overland speed -- its own authored "travel_speed"
            directly if it has one (a leaf provider, ex: creatures.toml's own "horse", a car),
            else the *minimum* _resolve_travel_speed across every currently-present entity
            named in its own "mount" field (see entity_schema.toml's own "mount" comment) --
            ex: a rider defers to their cart, which in turn defers to whichever horse(s)
            currently pull it, so a rider's own effective speed walks that whole chain rather
            than needing to name the team directly. Falls back to [travel]'s own default_speed
            if entity_name has neither a travel_speed nor a live mount to defer to.
            _visited guards against a malformed cyclic "mount" chain (never authored in
            shipped data).
        @param entity_name The entity to resolve.
        @param _visited Internal recursion guard; never pass explicitly.
        @return The resolved travel speed -- always a real number, never None.
        """
        default_speed = self._travel_rules().get("default_speed", 4)
        visited = _visited or set()
        if entity_name in visited:
            return default_speed
        visited = visited | {entity_name}

        entity = self.entities.get(entity_name, {})
        if "travel_speed" in entity:
            return entity["travel_speed"]
        mounts = self._resolve_mount_targets(entity_name)
        if not mounts:
            return default_speed
        return min(self._resolve_travel_speed(name, visited) for name in mounts)

    def _party_travel_speed(self):
        """!
        @brief The whole party's travel speed for grid distance/block math -- the slowest
            currently-present is_player/is_party member's own effective travel speed
            (_resolve_travel_speed -- their own "travel_speed" field, or whatever they're
            currently mounted on), falling back to [travel]'s own default_speed for anyone
            who has neither.
        @return The lowest travel speed among present party members (default_speed if somehow
            none are present at all).
        """
        default_speed = self._travel_rules().get("default_speed", 4)
        speeds = [
            self._resolve_travel_speed(name)
            for name in self.scenario_entities
            if self._is_party_member(name)
        ]
        return min(speeds) if speeds else default_speed

    def _resolve_region(self, x, y):
        """!
        @brief Finds whichever world_map.toml [[region]] contains grid point (x, y) --
            first match wins (regions aren't expected to overlap, but nothing enforces it).
            The one shared lookup resolve_region_environment/_resolve_region_terrain/
            _resolve_region_polity all read off of -- "environment" (encounter tables),
            "terrain" (travel speed/passability), and "polity" (default language/narration)
            are three independent, all-optional fields on the exact same [[region]] table, see
            docs/downtime.md's "Terrain, roads, and polities".
        @param x Grid x coordinate.
        @param y Grid y coordinate.
        @return The containing region's own table, or None if no authored region contains
            this point.
        """
        for region in self.rules.get("region", []):
            if (
                region.get("min_x", float("-inf")) <= x <= region.get("max_x", float("inf"))
                and region.get("min_y", float("-inf")) <= y <= region.get("max_y", float("inf"))
            ):
                return region
        return None

    def resolve_region_environment(self, x, y):
        """!
        @brief The region containing (x, y)'s own "environment" name.
        @param x Grid x coordinate.
        @param y Grid y coordinate.
        @return The containing region's own "environment" name, or None if no authored region
            contains this point -- the "no environment" default that's what "safe" looks like
            everywhere in this design (no watch check, no encounter roll).
        """
        region = self._resolve_region(x, y)
        return region.get("environment") if region else None

    def _resolve_region_terrain(self, x, y):
        """!
        @brief The region containing (x, y)'s own "terrain" name -- a region authoring no
            "terrain" field (every region shipped before this existed) resolves to None here,
            which _effective_speed_multiplier treats as speed_multiplier 1.0/passable, exactly
            today's unmodified math.
        @param x Grid x coordinate.
        @param y Grid y coordinate.
        @return The containing region's own "terrain" name, or None.
        """
        region = self._resolve_region(x, y)
        return region.get("terrain") if region else None

    def _resolve_region_polity(self, x, y):
        """!
        @brief The region containing (x, y)'s own "polity" name.
        @param x Grid x coordinate.
        @param y Grid y coordinate.
        @return The containing region's own "polity" name, or None.
        """
        region = self._resolve_region(x, y)
        return region.get("polity") if region else None

    def _current_polity_language(self):
        """!
        @brief The language a freshly-instanced entity should default to at the *current*
            location (DM_Rules.py's _instance_entities, called only once self.current_location_
            key already names the destination -- see _enter_location) -- looked up the same
            single-point way _current_environment already reads "what applies right here",
            just against polities.toml instead of environments.toml.
        @return polities.toml's own "language" for whichever polity (if any) contains the
            current location's own grid point, or None -- an ungridded location, a gridded
            point in an unmapped gap, or a polity that itself authors no "language" all resolve
            here the same way.
        """
        grid = self.locations.get(self.current_location_key, {}).get("grid")
        if not grid:
            return None
        polity_name = self._resolve_region_polity(grid["x"], grid["y"])
        if not polity_name:
            return None
        polity = self._find_polity(polity_name)
        return polity.get("language") if polity else None

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

    def _find_terrain(self, name):
        """!
        @brief Looks up one terrain.toml [[terrain]] entry by its own "name".
        @param name A terrain name (ex: "coastal_forest"), as named by a [[region]]'s own
            "terrain" field.
        @return The terrain's own table, or None if no terrain by that name is loaded.
        """
        for terrain in self.rules.get("terrain", []):
            if terrain.get("name") == name:
                return terrain
        return None

    def _find_polity(self, name):
        """!
        @brief Looks up one polities.toml [[polity]] entry by its own "name".
        @param name A polity name (ex: "Varisia"), as named by a [[region]]'s own "polity"
            field.
        @return The polity's own table, or None if no polity by that name is loaded.
        """
        for polity in self.rules.get("polity", []):
            if polity.get("name") == name:
                return polity
        return None

    def _resolve_conveyance_tags(self, entity_name, _visited=None):
        """!
        @brief entity_name's own effective terrain passability -- its own authored
            "terrain_tags" (a list, default []) unioned with the recursively-resolved tags of
            everything currently named in its own "mount" field (see entity_schema.toml's own
            "mount" comment), the exact same chain-walking shape _resolve_travel_speed already
            uses for speed: a rider inherits whatever a boat/griffon they're mounted on can
            cross, without needing to name it directly. _visited guards against a malformed
            cyclic "mount" chain (never authored in shipped data), same as
            _resolve_travel_speed's own guard.
        @param entity_name The entity to resolve.
        @param _visited Internal recursion guard; never pass explicitly.
        @return A set of terrain tags (ex: {"aquatic"}) -- never None, empty if this entity and
            everything in its mount chain has no terrain_tags of its own.
        """
        visited = _visited or set()
        if entity_name in visited:
            return set()
        visited = visited | {entity_name}

        entity = self.entities.get(entity_name, {})
        tags = set(entity.get("terrain_tags", []))
        for mount_name in self._resolve_mount_targets(entity_name):
            tags |= self._resolve_conveyance_tags(mount_name, visited)
        return tags

    def _party_conveyance_tags(self):
        """!
        @brief The whole party's combined terrain passability -- the union of every currently-
            present is_player/is_party member's own _resolve_conveyance_tags, mirroring
            _party_travel_speed's own "every present party member" scope (though union, not
            minimum: passability isn't paced to the slowest member the way speed is -- a route
            already denies outright the moment *any* present member can't cross it, checked
            per-member in _route_is_passable, not by first collapsing to one shared set here).
        @return {entity_name: set-of-tags} for every currently-present party member.
        """
        return {
            name: self._resolve_conveyance_tags(name)
            for name in self.scenario_entities
            if self._is_party_member(name)
        }

    def _resolve_road_multiplier(self, x, y):
        """!
        @brief The best (highest) speed_multiplier among every world_map.toml [[road]] whose
            own line segment ("from" to "to") passes within "width" grid units of (x, y) --
            standard clamped point-to-segment distance. A road overrides terrain entirely where
            it applies (see _effective_speed_multiplier) rather than compounding with it: the
            whole point of a road is to counteract whatever ground it's built over.
        @param x Grid x coordinate.
        @param y Grid y coordinate.
        @return The matching road's own "speed_multiplier", or None if no road's own "width"
            reaches this point.
        """
        best = None
        for road in self.rules.get("road", []):
            start, end = road.get("from", {}), road.get("to", {})
            sx, sy = start.get("x", 0), start.get("y", 0)
            ex, ey = end.get("x", 0), end.get("y", 0)
            dx, dy = ex - sx, ey - sy
            length_sq = dx * dx + dy * dy
            if length_sq == 0:
                t = 0.0
            else:
                t = max(0.0, min(1.0, ((x - sx) * dx + (y - sy) * dy) / length_sq))
            nearest_x, nearest_y = sx + t * dx, sy + t * dy
            distance = math.hypot(x - nearest_x, y - nearest_y)
            if distance <= road.get("width", 0):
                multiplier = road.get("speed_multiplier", 1.0)
                if best is None or multiplier > best:
                    best = multiplier
        return best

    def _effective_speed_multiplier(self, x, y):
        """!
        @brief The real speed multiplier a block of travel through (x, y) actually gets: a
            road's own multiplier if one reaches this point (_resolve_road_multiplier), else
            whichever region's own "terrain" names (_resolve_region_terrain/_find_terrain),
            else the plain, unmodified 1.0 every point got before either of these existed --
            the exact "nothing authored" default that keeps plains.toml's own math unchanged.
        @param x Grid x coordinate.
        @param y Grid y coordinate.
        @return A positive float multiplier against the party's own base travel speed.
        """
        road_multiplier = self._resolve_road_multiplier(x, y)
        if road_multiplier is not None:
            return road_multiplier
        terrain_name = self._resolve_region_terrain(x, y)
        terrain = self._find_terrain(terrain_name) if terrain_name else None
        return terrain.get("speed_multiplier", 1.0) if terrain else 1.0

    def _terrain_blocks_travel(self, x, y, party_tags):
        """!
        @brief Whether (x, y)'s own terrain is impassable to every currently-traveling party
            member -- used only by _route_is_passable's up-front dry run, never mid-trip (no
            pathfinding/backtracking exists, so a route is checked whole before a single block
            of it is actually spent). A road never un-blocks impassable terrain (roads only
            ever appear in _effective_speed_multiplier, not here) -- crossing genuinely
            impassable ground always needs the right conveyance, road or not.
        @param x Grid x coordinate.
        @param y Grid y coordinate.
        @param party_tags {entity_name: set-of-tags}, from _party_conveyance_tags.
        @return True if this point is impassable and no present party member's own resolved
            tags include its "requires_tag".
        """
        terrain_name = self._resolve_region_terrain(x, y)
        terrain = self._find_terrain(terrain_name) if terrain_name else None
        if not terrain or not terrain.get("impassable"):
            return False
        required = terrain.get("requires_tag")
        if not required:
            return False
        return not any(required in tags for tags in party_tags.values())

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
            (Combat_Resolution.tick_condition_durations, called from run_round_upkeep,
            DM_Status.py).
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
                self.apply_condition(name, "surprised", duration="rounds", length=1, dismiss="")
        return surprised

    def _route_is_passable(self, origin_grid, destination_grid):
        """!
        @brief Up-front dry run over the whole straight-line path from origin_grid to
            destination_grid, checked once before _resolve_grid_travel_intent commits to a
            self.pending_downtime at all -- no pathfinding/backtracking exists, so a route has
            to be checked whole rather than discovering an impassable stretch mid-trip with no
            way to resolve it. Sampled at a fixed one-point-per-grid-unit resolution,
            deliberately independent of party speed/terrain (which is exactly what this check
            exists to determine) -- fine enough to catch a narrow impassable strip crossing the
            line without over-sampling a long trip.
        @param origin_grid {x, y} of the current location.
        @param destination_grid {x, y} of the named destination.
        @return False if any sampled point along the line is impassable terrain
            (_terrain_blocks_travel) that no currently-present party member's own
            _resolve_conveyance_tags can satisfy; True otherwise (including the whole line
            crossing no mapped terrain at all).
        """
        dx = destination_grid["x"] - origin_grid["x"]
        dy = destination_grid["y"] - origin_grid["y"]
        distance = math.hypot(dx, dy)
        if distance == 0:
            return True
        party_tags = self._party_conveyance_tags()
        steps = max(1, math.ceil(distance))
        for i in range(steps):
            t = (i + 0.5) / steps
            x = origin_grid["x"] + dx * t
            y = origin_grid["y"] + dy * t
            if self._terrain_blocks_travel(x, y, party_tags):
                return False
        return True

    def _resolve_grid_travel_intent(self, input_text, resolved):
        """!
        @brief Handles "travel" from a gridded current location -- see this class's own
            docstring for the full model. Denied (reason "no_exit") if input_text doesn't name
            a known, gridded destination, (reason "blocked_by_enemies") under the exact same
            room-occupant gate DM_Movement.py's own exit-graph path already runs, (reason
            "mount_overloaded") if the player's own mount is currently carrying more than it
            can bear (_is_mount_overloaded, DM_Rules.py) -- checked fresh here, not just once
            at mount/hitch time, same reasoning DM_Movement.py's own advance_or_retreat
            applies to band movement -- or (reason "impassable_terrain") if the straight-line
            route crosses terrain no currently-present party member can cross
            (_route_is_passable). The "downtime_interrupted" denial/opportunistic-resume check
            lives one level up, in DM_Movement.py's own _resolve_travel_intent -- see that
            method's own docstring for why it has to run before the grid/non-grid branch
            decision, not here.
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

        mounts = self._resolve_mount_targets(self.player_name)
        if mounts and self._is_mount_overloaded(mounts[0]):
            resolved(False, reason="mount_overloaded")
            return

        destination_grid = self.locations[destination_key]["grid"]
        if not self._route_is_passable(origin_grid, destination_grid):
            resolved(False, reason="impassable_terrain")
            return

        distance = math.hypot(destination_grid["x"] - origin_grid["x"], destination_grid["y"] - origin_grid["y"])

        self.pending_downtime = {
            "kind": "travel", "destination_key": destination_key,
            "origin_grid": origin_grid, "destination_grid": destination_grid,
            "distance": distance, "distance_covered": 0.0, "blocks_done": 0,
        }
        result = self._advance_pending_travel()
        if result["interrupted"]:
            return
        resolved(True, **{k: v for k, v in result.items() if k != "interrupted"})

    def _advance_pending_travel(self):
        """!
        @brief Runs (or resumes) self.pending_downtime's own per-block loop from wherever
            "distance_covered" left off -- the exact same midpoint-sampling/
            _resolve_environment_block math _resolve_grid_travel_intent always ran, just able
            to stop partway through and be called again later. Unlike before this method
            existed, the number of blocks a trip takes is no longer fixed up front: each
            iteration covers the party's own base speed times whatever
            _effective_speed_multiplier applies at the party's *current* leading-edge position
            (clamped to a 0.1 floor so a stretch of slow terrain always eventually finishes --
            there's no backtracking/pathfinding to fall back on if it couldn't), so a road or a
            change in terrain partway along the line can make one trip's blocks_done come out
            differently than a plain distance/speed division would predict. A route with no
            authored terrain/road anywhere along it still reduces to exactly that plain division,
            one block at a time. A block whose own environment roll turns out hostile updates
            "blocks_done"/"distance_covered" and returns immediately ({"interrupted": True})
            instead of continuing -- moving into the ephemeral encounter site first
            (_enter_encounter_site) if this is the first interruption of this trip (a second
            ambush during an already-paused trip is already standing there, nothing to
            re-enter). Reaching the full distance clean hands off to _finish_pending_travel for
            real arrival.
        @return _finish_pending_travel's own result, or {"interrupted": True}.
        """
        pending = self.pending_downtime
        origin_grid = pending["origin_grid"]
        destination_grid = pending["destination_grid"]
        distance = pending["distance"]
        dx = destination_grid["x"] - origin_grid["x"]
        dy = destination_grid["y"] - origin_grid["y"]
        speed = self._party_travel_speed()

        while pending["distance_covered"] < distance:
            covered = pending["distance_covered"]
            t_start = covered / distance if distance > 0 else 1.0
            start_x = origin_grid["x"] + dx * t_start
            start_y = origin_grid["y"] + dy * t_start
            if speed > 0:
                multiplier = self._effective_speed_multiplier(start_x, start_y)
                step = min(speed * max(multiplier, 0.1), distance - covered)
            else:
                # Degenerate zero/negative speed -- same "just do it in one block" fallback
                # this method always had, from before terrain/roads existed.
                step = distance - covered
            new_covered = covered + step

            # Midpoint of this one block's own leg -- true per-block sampling, unchanged
            # semantics from before terrain/roads existed, just against a variable-length leg
            # instead of a fixed 1/blocks_total fraction.
            t_mid = ((covered + new_covered) / 2) / distance if distance > 0 else 1.0
            mid_x = origin_grid["x"] + dx * t_mid
            mid_y = origin_grid["y"] + dy * t_mid
            hostile = False
            environment_name = self.resolve_region_environment(mid_x, mid_y)
            if environment_name:
                environment = self._find_environment(environment_name)
                if environment:
                    hostile = self._resolve_environment_block(environment)

            self.advance_blocks(1)
            pending["distance_covered"] = new_covered
            pending["blocks_done"] += 1
            if hostile:
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
                room_name/room_description/characters/blocks_spent/time/polity), plus
                "distance" for the mechanics-only side of the original resolved() payload.
        """
        pending = self.pending_downtime
        self.pending_downtime = None
        self._enter_location(pending["destination_key"])
        new_location = self.locations.get(self.current_location_key, {})
        new_room = self._current_room()
        new_grid = new_location.get("grid")
        polity_name = self._resolve_region_polity(new_grid["x"], new_grid["y"]) if new_grid else None
        return {
            "interrupted": False,
            "location_name": new_location.get("name", ""),
            "location_description": new_location.get("description", ""),
            "room_name": new_room.get("name", "") if new_room else "",
            "room_description": new_room.get("description", "") if new_room else "",
            "characters": self._describe_scenario_characters(),
            "blocks_spent": pending["blocks_done"],
            "distance": round(pending["distance"], 1),
            "time": self.get_time_state(),
            "polity": polity_name,
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
