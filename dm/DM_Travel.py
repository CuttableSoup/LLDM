import math
import re

from dm.DM_Types import DMCoreProtocol


class TravelMixin(DMCoreProtocol):
    """!
    @brief Grid-based overworld travel (DMCore mixin -- only ever composed into DMCore, never
        instantiated on its own; relies on self.rules/self.entities/self.locations/
        self.scenario_entities/self.current_location_key/self.known_locations/self.event_bus/
        self.rooms/self.player_name/self._is_party_member/self.is_hostile/self.get_current_hp/
        self._enter_location/self._current_room/self._describe_scenario_characters/
        self.advance_blocks/self.is_daytime/self._resolve_one_encounter, set up by
        DMCore.__init__ or implemented by DM_Rules.py/DM_Encounters.py/DM_Time.py). See
        docs/downtime.md's "Travel" for the design this implements.

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

        Deliberate simplification, not yet built: every block's encounter roll (and the clock
        advance it rides on) happens in one uninterrupted burst right after _enter_location lands
        at the destination, not spread across an "in transit" scene of its own -- there's no such
        scene to hold an encountered creature if the player hasn't arrived anywhere yet, and
        pausing the clock mid-journey for a real fight (docs/downtime.md's own "Not yet built"
        note) is a separable future step. A creature this rolls up is simply added to the
        destination's own arrival scene, exactly like [[location.encounter]]'s own on_enter roll
        (which still fires separately, right after, unaffected). No night watch/surprise check
        either (docs/extended-goals.md's "Downtime") -- every block just rolls its table outright.
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

    def _resolve_grid_travel_intent(self, input_text, resolved):
        """!
        @brief Handles "travel" from a gridded current location -- see this class's own
            docstring for the full model. Denied (reason "no_exit") if input_text doesn't name
            a known, gridded destination, or (reason "blocked_by_enemies") under the exact same
            room-occupant gate DM_Movement.py's own exit-graph path already runs.
        @param input_text The raw (lowercased, prefix-stripped) player input.
        @param resolved The item_interaction_resolved publisher closure from
            DMCore._on_item_interaction_detected.
        """
        origin_grid = self.locations.get(self.current_location_key, {}).get("grid")
        destination_key = self._resolve_grid_destination(input_text)
        if destination_key is None:
            resolved(False, reason="no_exit")
            return

        if self.rooms:
            for entity_name in self.scenario_entities:
                if entity_name == self.player_name:
                    continue
                if self.is_hostile(entity_name, self.player_name) and self.get_current_hp(entity_name) > 0:
                    resolved(False, reason="blocked_by_enemies")
                    return

        destination_grid = self.locations[destination_key]["grid"]
        distance = math.hypot(destination_grid["x"] - origin_grid["x"], destination_grid["y"] - origin_grid["y"])
        speed = self._party_travel_speed()
        blocks = max(1, math.ceil(distance / speed)) if speed > 0 else 1

        self._enter_location(destination_key)

        for i in range(blocks):
            # Midpoint of this block's own leg of the straight line from origin to
            # destination -- true per-block sampling, not a coarser origin/destination split.
            t = (i + 0.5) / blocks
            point_x = origin_grid["x"] + (destination_grid["x"] - origin_grid["x"]) * t
            point_y = origin_grid["y"] + (destination_grid["y"] - origin_grid["y"]) * t
            environment_name = self.resolve_region_environment(point_x, point_y)
            if environment_name:
                environment = self._find_environment(environment_name)
                if environment:
                    table_key = "day_encounter" if self.is_daytime() else "night_encounter"
                    self._resolve_one_encounter({"encounter": environment.get(table_key, [])})
            self.advance_blocks(1)

        new_location = self.locations.get(self.current_location_key, {})
        new_room = self._current_room()
        resolved(
            True,
            location_name=new_location.get("name", ""),
            location_description=new_location.get("description", ""),
            room_name=new_room.get("name", "") if new_room else "",
            room_description=new_room.get("description", "") if new_room else "",
            characters=self._describe_scenario_characters(),
            blocks_spent=blocks,
            distance=round(distance, 1),
            time=self.get_time_state(),
        )
