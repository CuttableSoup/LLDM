"""!
@file Conveyance.py
@brief Conveyance -- the graph an entity's "mount" field draws (a rider on a horse, a cart hitched
    to a team, a creature carrying its own load) and everything that is asked of it: how much a
    mount carries, how fast a rider travels, what terrain they can cross, and who gets carried
    along into a new scene. One module owns the graph walk and its cycle guard, so a malformed
    cyclic "mount" chain (never authored in shipped data) is handled once rather than in each
    question's own recursion.

    The three questions aggregate along the chain differently, on purpose (see entity_schema.toml's
    own "mount" comment): capacity *sums* across a team (a cart can bear whatever its hitched team
    can bear), speed takes the *minimum* (a rider defers to the slowest thing under them), and
    terrain passability takes the *union* (a rider can cross whatever anything they ride can).

    Pure over a WorldContext's entities, rules, scenario_entities and hit points -- no DMCore, no
    event bus. Changing the graph (mounting, hitching, snapping bands) stays with the movement
    mixin, which calls in here for the answers.
"""

import resolution.Combat_Resolution as Combat_Resolution


def mount_targets(ctx, entity_name):
    """!
    @brief entity_name's own currently-present, still-living "mount" entries -- a bare string or
        a list, normalized to a list. An absent "mount" field, or one naming something no longer
        in the scene or reduced to 0 HP, is silently dropped rather than raising: losing a mount,
        by any means, just unwinds the relationship with no error.
    @return A list of zero or more real, present, living entity names.
    """
    raw = ctx.entities.get(entity_name, {}).get("mount")
    if not raw:
        return []
    names = [raw] if isinstance(raw, str) else list(raw)
    return [name for name in names if name in ctx.scenario_entities and Combat_Resolution.get_current_hp(ctx, name) > 0]


def mount_chain(ctx, entity_name, _visited=None):
    """!
    @brief Every entity reachable by walking entity_name's own "mount" field forward -- whatever
        it defers to, and whatever *that* defers to. Unlike mount_targets, not filtered by scene
        presence or liveness: it decides who should be carried along into a new location, so a
        mount that is between scenes still has to be found by name.
    @return A set of entity names (never includes entity_name itself).
    """
    visited = _visited if _visited is not None else {entity_name}
    raw = ctx.entities.get(entity_name, {}).get("mount")
    names = [] if not raw else ([raw] if isinstance(raw, str) else list(raw))
    chain = set()
    for name in names:
        if name in visited:
            continue
        visited.add(name)
        chain.add(name)
        chain |= mount_chain(ctx, name, visited)
    return chain


def _walk(ctx, entity_name, resolve, cycle_value, _visited=frozenset()):
    """!
    @brief The one walk down entity_name's live mounts, guarding against a cycle.
    @param resolve resolve(name, mounts, recurse) -> value, where recurse(mount) resolves a mount
        the same way. How it combines the mounts' own values is each question's own aggregation.
    @param cycle_value What a revisited entity contributes.
    """
    if entity_name in _visited:
        return cycle_value
    visited = _visited | {entity_name}
    return resolve(
        entity_name, mount_targets(ctx, entity_name),
        lambda mount_name: _walk(ctx, mount_name, resolve, cycle_value, visited),
    )


def max_bulk(ctx, entity_name):
    """!
    @brief entity_name's own carrying capacity -- its authored "max_bulk" if it has one (ex:
        Rules/Zombie's "car", a flat number), else rules.toml's [bulk] table: min_bulk plus the
        entity's own "skill" dice times mod_multiplier. An authored field always wins over the
        formula -- an explicit number is a deliberate override.
    @return The max bulk, or None if the entity authors none and the setting authors no [bulk]
        table either -- callers treat None as "uncapped", never as zero.
    """
    entity = ctx.entities.get(entity_name, {})
    if "max_bulk" in entity:
        return entity["max_bulk"]
    formula = ctx.rules.get("bulk")
    if not formula:
        return None
    skill_stats = entity.get("skills", {}).get(formula.get("skill"), {})
    return formula.get("min_bulk", 0) + skill_stats.get("dice", 0) * formula.get("mod_multiplier", 1)


def carrying_capacity(ctx, entity_name):
    """!
    @brief entity_name's own real-time load-bearing capacity -- max_bulk directly if it has no
        live mount of its own (a leaf provider, ex: a horse), else the *sum* of every present
        mount's own capacity (a cart's capacity is whatever its hitched team can bear, never a
        number authored on the cart). A provider that resolves to None (uncapped) contributes 0.
    @return The summed capacity, or max_bulk's own None if there is no live mount and no capacity.
    """
    def resolve(name, mounts, recurse):
        if not mounts:
            return max_bulk(ctx, name)
        return sum(recurse(mount_name) or 0 for mount_name in mounts)
    return _walk(ctx, entity_name, resolve, 0)


def current_bulk(ctx, entity_name, _visited=None):
    """!
    @brief Sums the "bulk" of every item entity_name carries (its "inventory" -- an equipped item
        is always also listed there, so it is never double-counted), plus the load of every
        present entity whose own "mount" names entity_name: each rider's flat "bulk" (body
        weight), plus their own carried gear too if rules.toml's [bulk] opts in via
        "count_rider_gear" (default true). A rider on a rider is handled the same recursive way.
        Walks riders, not mounts, so it keeps its own cycle guard.
    @return The summed bulk, 0 if entity_name carries nothing or is unknown.
    """
    visited = _visited or set()
    if entity_name in visited:
        return 0
    visited = visited | {entity_name}

    entity = ctx.entities.get(entity_name, {})
    own_cargo = sum(ctx.entities.get(item_name, {}).get("bulk", 0) for item_name in entity.get("inventory", []))

    count_rider_gear = ctx.rules.get("bulk", {}).get("count_rider_gear", True)
    rider_load = 0
    for rider_name in ctx.scenario_entities:
        if rider_name == entity_name or entity_name not in mount_targets(ctx, rider_name):
            continue
        rider_load += ctx.entities.get(rider_name, {}).get("bulk", 0)
        if count_rider_gear:
            rider_load += current_bulk(ctx, rider_name, visited)
    return own_cargo + rider_load


def would_exceed_capacity(ctx, mount_name, rider_name):
    """!
    @brief Whether rider_name mounting (or loading cargo onto) mount_name would push its current
        load past its capacity -- previewing the number current_bulk(mount_name) would report the
        instant after "mount" is set. Always False for an uncapped mount.
    """
    capacity = carrying_capacity(ctx, mount_name)
    if capacity is None:
        return False
    added = ctx.entities.get(rider_name, {}).get("bulk", 0)
    if ctx.rules.get("bulk", {}).get("count_rider_gear", True):
        added += current_bulk(ctx, rider_name)
    return current_bulk(ctx, mount_name) + added > capacity


def is_overloaded(ctx, mount_name):
    """!
    @brief Whether mount_name is *currently* carrying more than it can bear. Unlike
        would_exceed_capacity (a one-time preview at mounting), this is re-checked whenever
        movement is attempted, so gear picked up mid-ride, a second rider, or a puller dying out
        of a team can ground an already-underway trip. Always False for an uncapped mount.
    """
    capacity = carrying_capacity(ctx, mount_name)
    return capacity is not None and current_bulk(ctx, mount_name) > capacity


def travel_speed(ctx, entity_name):
    """!
    @brief entity_name's own effective overland speed -- its own authored "travel_speed" if it
        has one (a leaf provider, ex: a horse, a car), else the *minimum* across every present
        mount it defers to (a rider on a cart on a horse walks that whole chain), else [travel]'s
        default_speed.
    @return A real number, never None.
    """
    default_speed = ctx.rules.get("travel", {"default_speed": 4}).get("default_speed", 4)

    def resolve(name, mounts, recurse):
        entity = ctx.entities.get(name, {})
        if "travel_speed" in entity:
            return entity["travel_speed"]
        if not mounts:
            return default_speed
        return min(recurse(mount_name) for mount_name in mounts)
    return _walk(ctx, entity_name, resolve, default_speed)


def terrain_tags(ctx, entity_name):
    """!
    @brief entity_name's own effective terrain passability -- its authored "terrain_tags" unioned
        with the tags of everything it rides, so a rider inherits whatever a boat or griffon
        under them can cross.
    @return A set of tags (ex: {"aquatic"}), empty if nothing in the chain authors any.
    """
    def resolve(name, mounts, recurse):
        tags = set(ctx.entities.get(name, {}).get("terrain_tags", []))
        for mount_name in mounts:
            tags |= recurse(mount_name)
        return tags
    return _walk(ctx, entity_name, resolve, set())


def is_conveyance(ctx, entity_name):
    """!
    @brief Whether entity_name is part of the overland-conveyance system at all: it authors its
        own "travel_speed" (a leaf provider, ex: a horse, a car) or it already has at least one
        live mount of its own (ex: a cart already hitched to a horse). Gates what can be mounted
        and what can pull a hitched cart -- nothing should let the fiction imply riding or hitching
        something that was never authored to work that way.
    """
    return "travel_speed" in ctx.entities.get(entity_name, {}) or bool(mount_targets(ctx, entity_name))
