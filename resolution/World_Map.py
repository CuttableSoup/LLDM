"""!
@file World_Map.py
@brief The world map (see CONTEXT.md): where a grid point is -- which region, terrain, polity and
    road -- and what that means for the party's speed and whether a route is passable. Pure
    functions over the loaded rules (world_map.toml's [[region]]/[[road]], terrain.toml,
    environments.toml, polities.toml); read-only, so moving the party stays DM_Travel.py's job.

    "environment" (encounter tables), "terrain" (travel speed/passability) and "polity" (default
    language/narration) are three independent, all-optional fields on the same [[region]] table,
    and a [[road]] overrides terrain's speed wherever it reaches. Everything unauthored resolves
    to None/1.0/passable -- the "nothing there" default every point got before any of this existed.
    See docs/downtime.md's "Terrain, roads, and polities".
"""

import math


def region_at(rules, x, y):
    """!
    @brief The world_map.toml [[region]] containing grid point (x, y) -- first match wins (regions
        aren't expected to overlap, but nothing enforces it).
    @return The containing region's own table, or None if no authored region contains the point.
    """
    for region in rules.get("region", []):
        if (
            region.get("min_x", float("-inf")) <= x <= region.get("max_x", float("inf"))
            and region.get("min_y", float("-inf")) <= y <= region.get("max_y", float("inf"))
        ):
            return region
    return None


def environment_at(rules, x, y):
    """!
    @return The containing region's own "environment" name, or None -- the "no environment"
        default that's what "safe" looks like everywhere in this design (no watch, no encounter).
    """
    region = region_at(rules, x, y)
    return region.get("environment") if region else None


def terrain_at(rules, x, y):
    """!
    @return The containing region's own "terrain" name, or None -- which effective_speed_multiplier
        treats as speed_multiplier 1.0/passable, exactly the unmodified math.
    """
    region = region_at(rules, x, y)
    return region.get("terrain") if region else None


def polity_at(rules, x, y):
    """!@return The containing region's own "polity" name, or None."""
    region = region_at(rules, x, y)
    return region.get("polity") if region else None


def _find_by_name(rules, table, name):
    for entry in rules.get(table, []):
        if entry.get("name") == name:
            return entry
    return None


def find_environment(rules, name):
    """!@return The environments.toml [[environment]] entry named name (ex: "plains"), or None."""
    return _find_by_name(rules, "environment", name)


def find_terrain(rules, name):
    """!@return The terrain.toml [[terrain]] entry named name (ex: "coastal_forest"), or None."""
    return _find_by_name(rules, "terrain", name)


def find_polity(rules, name):
    """!@return The polities.toml [[polity]] entry named name (ex: "Varisia"), or None."""
    return _find_by_name(rules, "polity", name)


def point_to_segment_distance(x, y, sx, sy, ex, ey):
    """!
    @brief Standard clamped point-to-segment distance from (x, y) to the segment (sx, sy) ->
        (ex, ey) -- so a multi-segment road can reuse it once per leg.
    @return The perpendicular (or endpoint) distance, in grid units.
    """
    dx, dy = ex - sx, ey - sy
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        t = 0.0
    else:
        t = max(0.0, min(1.0, ((x - sx) * dx + (y - sy) * dy) / length_sq))
    nearest_x, nearest_y = sx + t * dx, sy + t * dy
    return math.hypot(x - nearest_x, y - nearest_y)


def road_points(road):
    """!
    @brief A road's own waypoints as a flat [(x, y), ...] list, at least 2 entries -- its own "path"
        (a list of >= 2 {x, y} tables) if authored, tracing a road that bends across as many legs
        as it names, else the single-segment "from"/"to" pair every road authored before "path"
        existed still uses. "path" wins if both are somehow present.
    @param road One world_map.toml [[road]] table.
    @return A list of (x, y) tuples, consecutive pairs of which are this road's own legs.
    """
    path = road.get("path")
    if path:
        return [(point.get("x", 0), point.get("y", 0)) for point in path]
    start, end = road.get("from", {}), road.get("to", {})
    return [(start.get("x", 0), start.get("y", 0)), (end.get("x", 0), end.get("y", 0))]


def road_multiplier(rules, x, y):
    """!
    @brief The best (highest) speed_multiplier among every [[road]] any of whose own legs passes
        within "width" grid units of (x, y). A road overrides terrain entirely where it applies
        (see effective_speed_multiplier): the whole point of a road is to counteract the ground
        it's built over.
    @return The matching road's own "speed_multiplier", or None if no road's "width" reaches the point.
    """
    best = None
    for road in rules.get("road", []):
        points = road_points(road)
        distance = min(
            point_to_segment_distance(x, y, sx, sy, ex, ey)
            for (sx, sy), (ex, ey) in zip(points, points[1:])
        )
        if distance <= road.get("width", 0):
            multiplier = road.get("speed_multiplier", 1.0)
            if best is None or multiplier > best:
                best = multiplier
    return best


def effective_speed_multiplier(rules, x, y):
    """!
    @brief The real speed multiplier a block of travel through (x, y) gets: a road's own multiplier
        if one reaches the point, else whichever region's own "terrain" names, else the plain,
        unmodified 1.0.
    @return A positive float multiplier against the party's own base travel speed.
    """
    multiplier = road_multiplier(rules, x, y)
    if multiplier is not None:
        return multiplier
    terrain_name = terrain_at(rules, x, y)
    terrain = find_terrain(rules, terrain_name) if terrain_name else None
    return terrain.get("speed_multiplier", 1.0) if terrain else 1.0


def terrain_blocks_travel(rules, x, y, party_tags):
    """!
    @brief Whether (x, y)'s own terrain is impassable to every traveling party member. A road never
        un-blocks impassable terrain (roads only ever appear in effective_speed_multiplier) --
        crossing genuinely impassable ground always needs the right conveyance, road or not.
    @param party_tags {entity_name: set-of-tags} -- each present party member's terrain tags.
    @return True if the point is impassable and no member's tags include its "requires_tag".
    """
    terrain_name = terrain_at(rules, x, y)
    terrain = find_terrain(rules, terrain_name) if terrain_name else None
    if not terrain or not terrain.get("impassable"):
        return False
    required = terrain.get("requires_tag")
    if not required:
        return False
    return not any(required in tags for tags in party_tags.values())


def route_is_passable(rules, origin_grid, destination_grid, party_tags):
    """!
    @brief Dry run over the whole straight-line path from origin_grid to destination_grid, checked
        once before a trip commits -- no pathfinding/backtracking exists, so a route is checked
        whole rather than discovering an impassable stretch mid-trip. Sampled at one point per
        grid unit, independent of party speed/terrain: fine enough to catch a narrow impassable
        strip without over-sampling a long trip.
    @param origin_grid {x, y} of the current location.
    @param destination_grid {x, y} of the named destination.
    @param party_tags {entity_name: set-of-tags}, as for terrain_blocks_travel.
    @return False if any sampled point is impassable terrain no member's tags satisfy; True
        otherwise (including a line crossing no mapped terrain at all).
    """
    dx = destination_grid["x"] - origin_grid["x"]
    dy = destination_grid["y"] - origin_grid["y"]
    distance = math.hypot(dx, dy)
    if distance == 0:
        return True
    steps = max(1, math.ceil(distance))
    for i in range(steps):
        t = (i + 0.5) / steps
        if terrain_blocks_travel(rules, origin_grid["x"] + dx * t, origin_grid["y"] + dy * t, party_tags):
            return False
    return True
