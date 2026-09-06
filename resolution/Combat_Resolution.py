"""!
@file Combat_Resolution.py
@brief Pure, DMCore-independent combat/status resolution -- dice rolls, damage, and the
    condition system, as plain functions over explicit entities/rules/skills/event_bus
    arguments rather than DMCore instance methods. Mirrors Challenge_Rating.py's own
    "pure module, DMCore reaches in" shape (see CLAUDE.md's "Challenge rating"): every
    function here is directly testable with a bare {} entities dict, no DMCore/EventBus
    subscription/scenario load required.

    DM_Combat.py's CombatMixin and DM_Status.py's StatusMixin keep their existing method
    names/signatures -- every one of these becomes a thin wrapper forwarding self.entities/
    self.rules/self.skills/self.event_bus, so no caller anywhere else in the codebase changes
    at all. DM_Movement.py's get_band/get_distance_between are the same shape, included here
    since get_comparable_value's own "distance_to_target" field and several functions below
    depend on them.

    Deliberately excludes anything that reaches into a sibling mixin beyond this graph
    (ex: apply_test_outcome's own loot_entity call, run_round_upkeep's own
    _expire_summon_if_due) -- those stay orchestration-level DMCore methods that call into
    this module's pure functions, the same "construct one layer up" precedent
    ActionOutcome's own producers follow (see DM_ActionOutcome.py).
"""

import random

# Defined before the Program_Interpreter import just below, on purpose: that module reads
# COMPARATORS at its own module level (to build its comparison regex), and the two modules
# already import each other (Program_Interpreter imports Combat_Resolution too) -- COMPARATORS
# has to already exist on this partially-initialized module by the time that happens, or the
# circular import fails with an AttributeError.
COMPARATORS = {
    ">": lambda actual, value: actual > value,
    "<": lambda actual, value: actual < value,
    ">=": lambda actual, value: actual >= value,
    "<=": lambda actual, value: actual <= value,
    "==": lambda actual, value: actual == value,
    "!=": lambda actual, value: actual != value,
    "in": lambda actual, value: actual in value,
    "not_in": lambda actual, value: actual not in value,
    "between": lambda actual, value: value[0] <= actual <= value[1],
}

import resolution.Program_Interpreter as Program_Interpreter
import resolution.Social_Resolution as Social_Resolution
from resolution.Challenge_Rating import skill_rating


def roll_dice(dice, pips):
    """!
    @brief Rolls the D6 dice pool and adds flat pips, per the D6 system.
    @param dice The number of six-sided dice to roll.
    @param pips The flat bonus added to the dice total.
    @return The total of the roll.
    """
    return sum(random.randint(1, 6) for _ in range(max(dice, 0))) + pips


def get_current_hp(entities, entity_name):
    """!
    @brief Gets an entity's current HP, initializing it from max_hp the first time it's needed.
    @param entities The live entities dict.
    @param entity_name The name of the entity.
    @return The entity's current HP.
    """
    entity = entities.get(entity_name, {})
    if "hp" not in entity:
        entity["hp"] = entity.get("max_hp", 0)
    return entity["hp"]


def get_band(entities, entity_name):
    """!
    @brief The entity's current band -- a 1-indexed position in the scenario's own bands,
        objective (not relative to the player or anything else).
    @param entities The live entities dict.
    @param entity_name The entity to check.
    @return The entity's "band" field (1 if unset).
    """
    return entities.get(entity_name, {}).get("band", 1)


def get_distance_between(entities, entity_a, entity_b):
    """!
    @brief The gap between two entities, in bands -- just their two band numbers subtracted.
    @param entities The live entities dict.
    @param entity_a The first entity's name.
    @param entity_b The second entity's name.
    @return The absolute band gap between them (0 if they're in the same band).
    """
    return abs(get_band(entities, entity_a) - get_band(entities, entity_b))


def get_active_conditions(entities, entity_name):
    """!
    @brief entity_name's own active_conditions dict -- the one place every other function/
        caller in this module reads it from, rather than each re-deriving
        entities.get(name, {}).get("active_conditions", {}) independently.
    @param entities The live entities dict.
    @param entity_name The entity to check.
    @return The entity's active_conditions dict ({} if it has none).
    """
    return entities.get(entity_name, {}).get("active_conditions", {})


def has_condition(entities, entity_name, condition_name):
    """!
    @brief Whether entity_name currently has condition_name active.
    @param entities The live entities dict.
    @param entity_name The entity to check.
    @param condition_name The condition name to look for.
    @return True if condition_name is in the entity's active_conditions.
    """
    return condition_name in get_active_conditions(entities, entity_name)


# The five denominations a condition's own "duration" may be authored as -- "rounds"/"rooms"/
# "blocks" are real, live countdowns (see tick_condition_durations, below, and its own callers:
# DM_Status.py's run_round_upkeep, DM_Time.py's advance_blocks, DM_Rules.py's enter_room);
# "days" is authoring-only sugar apply_condition itself converts to "blocks" (below) the moment
# it's applied, so it never actually exists as a stored value; "permanent" carries no length at
# all, cleared only by an explicit dismiss_condition call.
CONDITION_DURATIONS = ("rounds", "rooms", "blocks", "permanent")


def _apply_stat_drain(entities, rules, entity_name, condition_name):
    """!
    @brief Permanently removes dice/pips from entity_name's own base skill, per condition_name's
        own [[condition]] entry's optional "drain" = {skill, dice, pips} -- the Pathfinder
        "Energy Drained" shape (a permanent stat loss, distinct from [[condition]]'s ordinary
        "modifier", which is a roll-time-only penalty that evaporates the instant the condition
        is dismissed). Clamped so a drain can never push a skill below 0/0.
    @param entities The live entities dict.
    @param rules The loaded rules dict (may be None/{} -- no [[condition]] entry found means
        nothing to drain).
    @param entity_name The name of the entity losing the stat.
    @param condition_name The name of the condition being newly applied.
    @return {"skill", "dice", "pips"} describing the amount actually removed (for
        dismiss_condition to restore later), or None if this condition authors no "drain" at
        all, or the entity has no such skill to drain.
    """
    condition_def = next((c for c in (rules or {}).get("condition", []) if c.get("name") == condition_name), None)
    drain = condition_def.get("drain") if condition_def else None
    if not drain:
        return None
    skill_stats = entities.get(entity_name, {}).get("skills", {}).get(drain.get("skill"))
    if skill_stats is None:
        return None
    dice_amount = min(drain.get("dice", 0), skill_stats.get("dice", 0))
    pips_amount = min(drain.get("pips", 0), skill_stats.get("pips", 0))
    skill_stats["dice"] -= dice_amount
    skill_stats["pips"] -= pips_amount
    return {"skill": drain["skill"], "dice": dice_amount, "pips": pips_amount}


def _find_condition_def(rules, condition_name):
    """!@brief The [[condition]] entry named condition_name, or None."""
    return next((c for c in (rules or {}).get("condition", []) if c.get("name") == condition_name), None)


def _convert_periodic_phase(rules, phase):
    """!
    @brief Normalizes a periodic_test phase ({"unit", "length"}) the same way apply_condition
        normalizes an authored "days" duration -- "days" becomes "blocks", length scaled by
        rules.toml's own [time].blocks_per_day; "rounds"/"blocks" pass through unchanged.
    @param rules The loaded rules dict (may be None/{}).
    @param phase {"unit", "length"} -- an onset or interval entry off a [[condition]]'s own
        "periodic_test" table.
    @return (unit, length) with "days" already converted to "blocks".
    """
    unit, length = phase["unit"], phase["length"]
    if unit == "days":
        blocks_per_day = (rules or {}).get("time", {}).get("blocks_per_day", 3)
        unit, length = "blocks", length * blocks_per_day
    return unit, length


def _init_periodic_state(rules, condition_name):
    """!
    @brief Seeds a freshly-applied condition's own periodic_test countdown state -- the
        Pathfinder poison/disease "Frequency"/"Onset" shape (Rules/Fantasy/reference/
        pathfinder_mapping.toml's Poison/Dying-Stable-Disabled rows): a periodic self-save that
        starts after an optional onset delay, then repeats every "interval" until either it's
        dismissed some other way or "cure_after_successes" consecutive passes cure it outright.
    @param rules The loaded rules dict (may be None/{} -- no [[condition]] entry found means
        nothing to seed).
    @param condition_name The name of the condition being newly applied.
    @return {"remaining", "onset_passed", "successes", "drained"} state dict, or None if
        condition_name authors no "periodic_test" at all.
    """
    condition_def = _find_condition_def(rules, condition_name)
    periodic_test = condition_def.get("periodic_test") if condition_def else None
    if not periodic_test:
        return None
    onset = periodic_test.get("onset")
    if onset and onset.get("length", 0) > 0:
        _, remaining = _convert_periodic_phase(rules, onset)
        onset_passed = False
    else:
        _, remaining = _convert_periodic_phase(rules, periodic_test["interval"])
        onset_passed = True
    return {"remaining": remaining, "onset_passed": onset_passed, "successes": 0, "drained": {}}


def apply_condition(entities, event_bus, entity_name, condition_name, duration=None, length=None, dismiss=None, rules=None):
    """!
    @brief Marks a condition as active on an entity.
    @param entities The live entities dict.
    @param event_bus The EventBus to publish a log_info line to.
    @param entity_name The name of the entity gaining the condition.
    @param condition_name The name of the condition, as defined in the [[condition]] table.
    @param duration Which clock the condition counts down against -- one of
        CONDITION_DURATIONS, or the authoring-only "days" (converted here, if rules was passed,
        to "blocks" -- length * that setting's own [time].blocks_per_day, TimeMixin's own
        get_time_state default if a setting authors no [time] table at all); "permanent" for a
        duration with no live countdown at all, cleared only by an explicit dismiss_condition
        call (or, for a periodic_test-bearing condition, by tick_periodic_tests's own
        cure_after_successes check -- see _init_periodic_state).
    @param length How many of "duration"'s own unit remain (unused/ignored for "permanent").
    @param dismiss What removes the condition (ex: "healing", "resurrection").
    @param rules The loaded rules dict, needed to convert a "days" duration to "blocks", and to
        look up condition_name's own optional "drain"/"periodic_test" (see _apply_stat_drain/
        _init_periodic_state) -- every caller that can ever author "days"/"drain" (DM_Status.py's
        own apply_condition wrapper, Program_Interpreter.py's `condition` op, evaluate_statuses
        below) already has self.rules/rules in reach and passes it through; a caller that only
        ever applies a plain "permanent"/"rounds"/"rooms"/"blocks", non-draining condition (ex:
        DM_Travel.py's "surprised") can omit it.
    """
    if duration == "days":
        blocks_per_day = (rules or {}).get("time", {}).get("blocks_per_day", 3)
        duration, length = "blocks", length * blocks_per_day
    entity = entities.get(entity_name)
    if entity is None:
        return
    active_conditions = entity.setdefault("active_conditions", {})
    # Drains/periodic state only seed once per gain, not on every reapplication/refresh (ex: an
    # extended duration on an already-active condition must not double-drain the same skill, or
    # restart a disease's own onset/interval countdown from scratch) -- an already-active
    # condition carries its own already-computed state forward unchanged.
    if condition_name in active_conditions:
        drained = active_conditions[condition_name].get("_drained")
        periodic = active_conditions[condition_name].get("_periodic")
    else:
        drained = _apply_stat_drain(entities, rules, entity_name, condition_name)
        periodic = _init_periodic_state(rules, condition_name)
    entry = {"duration": duration, "length": length, "dismiss": dismiss}
    if drained:
        entry["_drained"] = drained
    if periodic:
        entry["_periodic"] = periodic
    active_conditions[condition_name] = entry
    event_bus.publish("log_info", f"{entity_name} gains condition '{condition_name}'.")


def tick_condition_durations(entities, event_bus, entity_name, unit, amount=1):
    """!
    @brief Decrements entity_name's own active_conditions entries whose "duration" matches unit
        by amount, dismissing any that reach 0 or below -- the one shared countdown every
        clock-backed duration ticks against (DM_Status.py's run_round_upkeep calls this with
        unit="rounds" once per combat round; DM_Time.py's advance_blocks with unit="blocks",
        amount=blocks elapsed; DM_Rules.py's enter_room with unit="rooms" once per room left).
        An entry with no "length" set (ex: "permanent", or a malformed apply site) is left
        alone -- there's nothing to count down. Iterates a snapshot of active_conditions'
        own keys, since dismissing one mutates the same dict mid-loop.
    @param entities The live entities dict.
    @param event_bus The EventBus, forwarded to dismiss_condition.
    @param entity_name The entity to tick.
    @param unit Which duration denomination just elapsed ("rounds"/"rooms"/"blocks").
    @param amount How many of that unit elapsed (blocks only -- rounds/rooms always tick by 1).
    """
    active_conditions = get_active_conditions(entities, entity_name)
    for condition_name, entry in list(active_conditions.items()):
        if entry.get("duration") != unit or entry.get("length") is None:
            continue
        entry["length"] -= amount
        if entry["length"] <= 0:
            dismiss_condition(entities, event_bus, entity_name, condition_name)


def _apply_periodic_drain(entities, entity_name, periodic_state, drain_specs):
    """!
    @brief Applies one failed periodic_test's own "on_fail.drain" list, accumulating the actual
        amount removed per skill onto periodic_state's own "drained" dict -- repeatable, unlike
        _apply_stat_drain's own one-shot "drain" field, since a disease/poison can fail its save
        more than once before being cured. dismiss_condition reads this dict back to restore
        every skill it ever touched, in total, the moment the condition finally clears. Clamped
        per application so a drain can never push a skill below 0/0, same as _apply_stat_drain.
    @param entities The live entities dict.
    @param entity_name The entity failing its save.
    @param periodic_state The condition instance's own "_periodic" dict, mutated in place.
    @param drain_specs A list of {"skill", "dice", "pips"} entries (periodic_test.on_fail.drain).
    """
    skills = entities.get(entity_name, {}).get("skills", {})
    for spec in drain_specs:
        skill_stats = skills.get(spec.get("skill"))
        if skill_stats is None:
            continue
        dice_amount = min(spec.get("dice", 0), skill_stats.get("dice", 0))
        pips_amount = min(spec.get("pips", 0), skill_stats.get("pips", 0))
        skill_stats["dice"] -= dice_amount
        skill_stats["pips"] -= pips_amount
        accumulated = periodic_state["drained"].setdefault(spec["skill"], {"dice": 0, "pips": 0})
        accumulated["dice"] += dice_amount
        accumulated["pips"] += pips_amount


def tick_periodic_tests(entities, rules, event_bus, entity_name, unit, amount=1):
    """!
    @brief Advances entity_name's own periodic_test countdowns by amount of unit ("rounds" or
        "blocks") -- the Pathfinder poison ("Frequency 1/round") / disease ("Frequency 1/day")
        shape (Rules/Fantasy/reference/pathfinder_mapping.toml's Poison/Dying-Stable-Disabled
        rows). Ticked from the exact same call sites tick_condition_durations already is
        (DM_Status.py's run_round_upkeep for "rounds", DM_Time.py's _tick_conditions_by_block for
        "blocks" -- deliberately not enter_room's "rooms" tick, a disease/poison's cadence isn't
        room-scoped), and over the same "every entity in self.entities, not just
        self.scenario_entities" scope _tick_conditions_by_block already uses for block-scale
        duration -- a disease keeps progressing on a party member who isn't even in the current
        scene.

        Each condition's own onset elapses once (its own countdown swapped for "interval"'s the
        moment it reaches 0, immediately rolling the first save), then a save (resolve_action, a
        flat difficulty check -- no dice_penalty, this is never the acting entity's own turn)
        repeats every "interval". A pass increments a stored consecutive-successes counter,
        auto-dismissing the condition outright once "cure_after_successes" is reached
        (Pathfinder's "2 consecutive saves" cure shape) -- a disease/poison with no explicit
        "cure"-shaped spell/dismiss trigger of its own still eventually clears on its own the
        honest way. A fail resets that counter to 0 and applies periodic_test's own
        "on_fail.drain" (see _apply_periodic_drain) and/or "on_fail.damage" (an ordinary rolled
        hit, via apply_damage -- a simple ongoing toxin with no ability-damage component at all).
    @param entities The live entities dict.
    @param rules The loaded rules dict.
    @param event_bus The EventBus, forwarded to apply_damage/dismiss_condition.
    @param entity_name The entity to tick.
    @param unit Which denomination just elapsed ("rounds"/"blocks") -- only conditions whose
        currently-active phase (onset if not yet passed, else interval) shares this unit (after
        "days" conversion) are advanced at all.
    @param amount How many of that unit elapsed.
    """
    active_conditions = get_active_conditions(entities, entity_name)
    for condition_name, entry in list(active_conditions.items()):
        periodic_state = entry.get("_periodic")
        if periodic_state is None:
            continue
        condition_def = _find_condition_def(rules, condition_name)
        periodic_test = condition_def.get("periodic_test") if condition_def else None
        if not periodic_test:
            continue
        active_phase = periodic_test["interval"] if periodic_state["onset_passed"] else (
            periodic_test.get("onset") or periodic_test["interval"]
        )
        phase_unit, _ = _convert_periodic_phase(rules, active_phase)
        if phase_unit != unit:
            continue
        periodic_state["remaining"] -= amount
        if periodic_state["remaining"] > 0:
            continue
        periodic_state["onset_passed"] = True
        _, periodic_state["remaining"] = _convert_periodic_phase(rules, periodic_test["interval"])
        roll = resolve_action(entities, rules, event_bus, entity_name, periodic_test["skill"], periodic_test.get("difficulty", 0))
        if roll["success"]:
            periodic_state["successes"] += 1
            if periodic_state["successes"] >= periodic_test.get("cure_after_successes", float("inf")):
                dismiss_condition(entities, event_bus, entity_name, condition_name)
        else:
            periodic_state["successes"] = 0
            on_fail = periodic_test.get("on_fail", {})
            if on_fail.get("drain"):
                _apply_periodic_drain(entities, entity_name, periodic_state, on_fail["drain"])
            damage_spec = on_fail.get("damage")
            if damage_spec:
                damage_total = roll_dice(damage_spec.get("dice", 0), damage_spec.get("pips", 0)) + damage_spec.get("bonus", 0)
                if damage_total > 0:
                    apply_damage(entities, rules, event_bus, entity_name, damage_total)


def dismiss_matching_conditions(entities, rules, event_bus, entity_name, spec):
    """!
    @brief Dismisses every one of entity_name's own active conditions whose [[condition]] entry
        matches spec's "supertypes"/"subtypes" filter (matches_supertype_or_subtype, reused
        unchanged against the condition catalog instead of the entity catalog -- it only ever
        reads dict.get("supertype")/dict.get("subtype"), so a [[condition]] entry authoring
        those two optional fields works identically to an [[entity]]'s own). The Pathfinder
        "remove disease"/"neutralize poison"/panacea shape: the caster doesn't need to name the
        specific affliction, just its kind -- {subtypes = ["disease"]} cures any active disease,
        {supertypes = ["affliction"]} a broader panacea also catching poison/curse. Mirrors
        DM_Core.py's _apply_dispel_if_hit exactly, just removing a condition instead of banishing
        an entity; a target carrying nothing matching simply has nothing cured, the same
        "used on the wrong thing just wastes it" shape dispel already has.
    @param entities The live entities dict.
    @param rules The loaded rules dict.
    @param event_bus The EventBus, forwarded to dismiss_condition.
    @param entity_name The name of the entity being cured.
    @param spec A table carrying "supertypes"/"subtypes" (both optional, each a list of
        strings) -- see matches_supertype_or_subtype.
    @return A list of the condition names actually dismissed (possibly empty).
    """
    cured = []
    for condition_name in list(get_active_conditions(entities, entity_name)):
        condition_def = _find_condition_def(rules, condition_name)
        if condition_def and matches_supertype_or_subtype(condition_def, spec):
            dismiss_condition(entities, event_bus, entity_name, condition_name)
            cured.append(condition_name)
    return cured


def tick_ability_cooldowns(entities, entity_name):
    """!
    @brief Decrements every one of entity_name's own active ability_cooldowns entries by one,
        dropping any that reach 0 -- the per-round counterpart to tick_condition_durations, for
        an ability's own cooldown_rounds (set when the ability is used -- see
        DM_Combat.py's resolve_behavior_action) rather than a [[condition]]'s duration/length.
        Called once per round from run_round_upkeep (DM_Status.py), same cadence as
        tick_condition_durations(unit="rounds").
    @param entities The live entities dict.
    @param entity_name The entity to tick.
    """
    cooldowns = entities.get(entity_name, {}).get("ability_cooldowns")
    if not cooldowns:
        return
    for ability_name in list(cooldowns):
        cooldowns[ability_name] -= 1
        if cooldowns[ability_name] <= 0:
            del cooldowns[ability_name]


def dismiss_condition(entities, event_bus, entity_name, condition_name):
    """!
    @brief Removes a condition from an entity, if it's currently active -- restoring any stat
        drain it applied (see _apply_stat_drain/apply_condition, and _apply_periodic_drain's own
        repeatable per-failure drain) by reading the exact amount(s) stashed on the condition's
        own active_conditions entry at apply/fail time, rather than re-deriving it from rules
        (which would need condition_name's own [[condition]] entry to still exist/be unchanged --
        reading back what was actually removed is exact regardless). No rules param needed here
        as a result.
    @param entities The live entities dict.
    @param event_bus The EventBus to publish a log_info line to.
    @param entity_name The name of the entity losing the condition.
    @param condition_name The name of the condition to remove.
    @return True if the condition was present and removed, False otherwise.
    """
    active_conditions = get_active_conditions(entities, entity_name)
    if condition_name not in active_conditions:
        return False
    entry = active_conditions.pop(condition_name)
    skills = entities.get(entity_name, {}).get("skills", {})
    drained = entry.get("_drained")
    if drained:
        skill_stats = skills.get(drained["skill"])
        if skill_stats is not None:
            skill_stats["dice"] = skill_stats.get("dice", 0) + drained.get("dice", 0)
            skill_stats["pips"] = skill_stats.get("pips", 0) + drained.get("pips", 0)
    periodic = entry.get("_periodic")
    if periodic:
        for skill_name, amount in periodic.get("drained", {}).items():
            skill_stats = skills.get(skill_name)
            if skill_stats is not None:
                skill_stats["dice"] = skill_stats.get("dice", 0) + amount.get("dice", 0)
                skill_stats["pips"] = skill_stats.get("pips", 0) + amount.get("pips", 0)
    event_bus.publish("log_info", f"{entity_name} loses condition '{condition_name}'.")
    return True


def get_concealment(entities, rules, entity_name):
    """!
    @brief The highest "miss_chance" (percent, 0-100) of any of entity_name's own
        active_conditions with a matching [[condition]] entry authoring one -- the Pathfinder
        "concealment"/Invisible shape: even a successful attack roll can still just miss
        outright. Takes the max across conditions rather than summing them (concealment
        doesn't stack additively either in Pathfinder), capped at 95 so nothing is ever
        completely unhittable by ordinary means.
    @param entities The live entities dict.
    @param rules The loaded rules dict.
    @param entity_name The name of the entity to check.
    @return The effective miss_chance, 0 if nothing applies.
    """
    active_conditions = get_active_conditions(entities, entity_name)
    condition_defs = {c.get("name"): c for c in rules.get("condition", [])}
    best = 0
    for condition_name in active_conditions:
        condition_def = condition_defs.get(condition_name)
        if not condition_def:
            continue
        miss_chance = condition_def.get("miss_chance", 0)
        if miss_chance > best:
            best = miss_chance
    return min(best, 95)


def get_override_target(entities, rules, entity_name):
    """!
    @brief The raw "override_target" value ("random", or a literal entity name) authored by
        any of entity_name's own active_conditions with a matching [[condition]] entry -- the
        Pathfinder Confused ("random")/Dominate (a literal name, authored by whatever spell
        applied the condition at cast time) shape: hijacking WHO an entity's turn is aimed at,
        not whether it can act (prevents_action) or which ability it picks (choose_behavior is
        untouched). If more than one active condition authors one, the last one found wins --
        same "no defined stacking order" precedent get_condition_modifier's own summing
        sidesteps by just adding everything together; a stacked, contradictory pair of forced-
        target effects isn't a case any shipped content produces.
    @param entities The live entities dict.
    @param rules The loaded rules dict.
    @param entity_name The name of the entity to check.
    @return "random", a literal entity name, or None if nothing overrides.
    """
    condition_defs = {c.get("name"): c for c in rules.get("condition", [])}
    override = None
    for condition_name in get_active_conditions(entities, entity_name):
        condition_def = condition_defs.get(condition_name)
        if condition_def and condition_def.get("override_target"):
            override = condition_def["override_target"]
    return override


def resolve_override_target(entities, rules, entity_name, candidates):
    """!
    @brief Resolves entity_name's own active override_target (get_override_target) to a real,
        currently-living target name.
    @param entities The live entities dict.
    @param rules The loaded rules dict.
    @param entity_name The name of the entity to check.
    @param candidates A caller-supplied pool of other currently-living scene entities to pick
        randomly from -- this pure module has no notion of "the scene" (self.scenario_entities
        lives on DMCore), so "random" can't derive its own pool.
    @return "random" resolves to a uniform random choice from candidates (None if candidates
        is empty); a literal name resolves to itself only if it currently names a real, living
        entity (None otherwise -- ex: the named target died since the effect was applied,
        treated the same as no override at all rather than erroring); None if nothing
        overrides in the first place.
    """
    override = get_override_target(entities, rules, entity_name)
    if override is None:
        return None
    if override == "random":
        return random.choice(candidates) if candidates else None
    if get_current_hp(entities, override) > 0:
        return override
    return None


def get_skill_group_members(rules, name_or_names):
    """!
    @brief Expands a skill/group name (or a list of them) through rules.toml's own
        [[skill_group]] table -- {name, skills} entries letting a cluster of skills be
        addressed by one shared name, standing in for the attribute layer this engine
        deliberately doesn't have (Pathfinder's Bull's Strength buffs every Strength-based
        skill/check at once; a "strength" skill_group is how the same shape is authored here,
        without building a real attribute stat). A name matching a defined group's own "name"
        expands to that group's own "skills" list; any other name (the common case -- most
        skills belong to no group at all) passes through unchanged as a single-element result,
        so this is purely additive over every existing "skill" field/reference that never
        mentions a group. The two current consumers: get_condition_modifier's own applies_to,
        and get_equipped_skill_bonus's own equipped_skill_bonus.skill.
    @param rules The loaded rules dict.
    @param name_or_names A single skill/group name, or a list of them.
    @return A flat list of real skill names (groups expanded; duplicates not deduplicated,
        since every caller only ever uses this for a membership test).
    """
    names = name_or_names if isinstance(name_or_names, list) else [name_or_names]
    group_defs = {g.get("name"): g.get("skills", []) for g in rules.get("skill_group", [])}
    expanded = []
    for name in names:
        expanded.extend(group_defs.get(name, [name]))
    return expanded


def get_condition_modifier(entities, rules, entity_name, skill_name=None):
    """!
    @brief Sums the {dice, pips, bonus} roll modifier of every one of entity_name's own
        active_conditions that has a matching entry in rules.toml's own [[condition]] table.
    @param entities The live entities dict.
    @param rules The loaded rules dict.
    @param entity_name The name of the entity to sum modifiers for.
    @param skill_name The skill being rolled, if known. A [[condition]] entry's own optional
        "applies_to" (a list of skill/skill_group names -- see get_skill_group_members)
        restricts its modifier to only those skills' rolls -- absent (every condition shipped
        before this field existed) still applies globally, unaffected. A scoped condition
        contributes nothing when skill_name is None (no skill context to check against), the
        same "can't match without a value" precedent distance_to_target/opponent_has_condition
        already follow with no opponent_name.
    @return A {"dice", "pips", "bonus"} dict, each defaulting to 0 if nothing applies.
    """
    active_conditions = get_active_conditions(entities, entity_name)
    condition_defs = {c.get("name"): c for c in rules.get("condition", [])}
    total = {"dice": 0, "pips": 0, "bonus": 0}
    for condition_name in active_conditions:
        condition_def = condition_defs.get(condition_name)
        if not condition_def:
            continue
        modifier = condition_def.get("modifier")
        if not modifier:
            continue
        applies_to = condition_def.get("applies_to")
        if applies_to and skill_name not in get_skill_group_members(rules, applies_to):
            continue
        total["dice"] += modifier.get("dice", 0)
        total["pips"] += modifier.get("pips", 0)
        total["bonus"] += modifier.get("bonus", 0)
    return total


def get_equipped_skill_bonus(entities, rules, entity_name, skill_name):
    """!
    @brief Sums the {dice, pips} bonus every one of entity_name's own equipped items
        contributes to skill_name, via each item's own optional "equipped_skill_bonus" =
        {skill, dice, pips} -- the Pathfinder "Ring/belt/wondrous stat bonus" shape (a passive
        skill-dice buff from a worn, non-weapon item). Distinct from armor_value/resistance_
        value (defense) and damage_value (offense) -- this is the first equipped-item field
        read for an *ordinary skill roll*, not just combat math. "skill" may name a single
        skill, a skill_group (see get_skill_group_members -- ex: a belt granting +1D to every
        Strength-based skill at once), or a list mixing either, the same "single name or list"
        convention an ability's own "skill" field already follows.
    @param entities The live entities dict.
    @param rules The loaded rules dict.
    @param entity_name The name of the entity to check.
    @param skill_name The skill being rolled, or None (matches nothing, same "can't match
        without a value" precedent get_condition_modifier's own applies_to follows).
    @return A {"dice", "pips"} dict, each defaulting to 0 if nothing applies.
    """
    total = {"dice": 0, "pips": 0}
    if skill_name is None:
        return total
    entity = entities.get(entity_name, {})
    for item_name in entity.get("equipped", {}).values():
        bonus = entities.get(item_name, {}).get("equipped_skill_bonus")
        if bonus and skill_name in get_skill_group_members(rules, bonus.get("skill")):
            total["dice"] += bonus.get("dice", 0)
            total["pips"] += bonus.get("pips", 0)
    return total


def get_comparable_value(entities, entity_name, field, opponent_name=None):
    """!
    @brief Resolves a requirement's field name to a comparable value for an entity.
    @param entities The live entities dict.
    @param entity_name The name of the entity to check.
    @param field The field name, either a derived value (ex: "hp_per_remain",
        "distance_to_target", "has_condition:<name>", "opponent_has_condition:<name>",
        "disposition"/"threat"/"familiarity") or an entity attribute (ex: "supertype").
    @param opponent_name The entity being acted against, if any.
    @return The resolved value, or None if it can't be determined.
    """
    if field == "hp_per_remain":
        entity = entities.get(entity_name, {})
        max_hp = entity.get("max_hp", 0)
        if max_hp <= 0:
            return None
        return get_current_hp(entities, entity_name) / max_hp
    if field == "distance_to_target":
        if opponent_name is None:
            return None
        return get_distance_between(entities, entity_name, opponent_name)
    if field.startswith("has_condition:"):
        condition_name = field[len("has_condition:"):]
        return has_condition(entities, entity_name, condition_name)
    if field.startswith("opponent_has_condition:"):
        if opponent_name is None:
            return None
        condition_name = field[len("opponent_has_condition:"):]
        return has_condition(entities, opponent_name, condition_name)
    if field.startswith("ability_ready:"):
        # True unless entity_name's own ability_cooldowns (set by an ability's own
        # cooldown_rounds, ticked down once per round by tick_ability_cooldowns) still has a
        # positive count for this ability name -- lets a behavior entry gate itself off while
        # its own high-value attack is recharging (ex: a breath weapon), falling through to a
        # weaker fallback entry in the meantime, the same way "has_condition:<name>" already
        # lets one gate off a paralyzed/warded creature's own attack entries.
        ability_name = field[len("ability_ready:"):]
        cooldowns = entities.get(entity_name, {}).get("ability_cooldowns", {})
        return cooldowns.get(ability_name, 0) <= 0
    if field in Social_Resolution.ATTITUDE_AXES:
        # entity_name's own attitude *toward* opponent_name -- ex: a program condition like
        # "target.threat < -50" reads how the checked entity feels about whoever it's being
        # checked against, re-derived live (not
        # cached) so an earlier step's own "attitude" op in the same program is already
        # reflected here. None with no opponent_name -- there's no one for this entity to have
        # an attitude toward in that case.
        if opponent_name is None:
            return None
        axis_index = Social_Resolution.ATTITUDE_AXES.index(field)
        return Social_Resolution.get_attitude(entities, entity_name, opponent_name)[axis_index]
    return entities.get(entity_name, {}).get(field)


def _requirement_matches(entities, event_bus, entity_name, requirement, opponent_name):
    """!
    @brief Evaluates one entry of a requirements list -- either a plain {field, operator,
        value} comparison, or a nested {"all"|"any"|"none": [...]} boolean combination of more
        such entries (recursive), the same shape Program_Interpreter.evaluate_condition already
        gives program `if`-steps.
    @param entities The live entities dict.
    @param event_bus The EventBus to publish a log_warning line to on an unknown operator.
    @param entity_name The name of the entity to check.
    @param requirement A {field, operator, value} comparison, or a boolean-combinator table.
    @param opponent_name The entity being acted against, if any.
    @return True if this entry is satisfied.
    """
    if "all" in requirement:
        return all(_requirement_matches(entities, event_bus, entity_name, sub, opponent_name) for sub in requirement["all"])
    if "any" in requirement:
        return any(_requirement_matches(entities, event_bus, entity_name, sub, opponent_name) for sub in requirement["any"])
    if "none" in requirement:
        return not any(_requirement_matches(entities, event_bus, entity_name, sub, opponent_name) for sub in requirement["none"])

    compare = COMPARATORS.get(requirement.get("operator"))
    if compare is None:
        event_bus.publish("log_warning", f"Unknown requirement operator: {requirement.get('operator')}")
        return False

    actual_value = get_comparable_value(entities, entity_name, requirement.get("field"), opponent_name)
    return actual_value is not None and compare(actual_value, requirement.get("value"))


def entity_matches_requirements(entities, event_bus, entity_name, requirements, opponent_name=None):
    """!
    @brief Checks whether an entity currently satisfies every comparison in a status's
        (or a behavior's, or an [entity.test]'s) requirements.
    @param entities The live entities dict.
    @param event_bus The EventBus to publish a log_warning line to on an unknown operator.
    @param entity_name The name of the entity to check.
    @param requirements A list of {field, operator, value} comparisons (each of which may
        instead be a nested {"all"|"any"|"none": [...]} boolean combination -- see
        _requirement_matches), all of which must hold.
    @param opponent_name The entity being acted against, if any.
    @return True if every entry is satisfied.
    """
    return all(
        _requirement_matches(entities, event_bus, entity_name, requirement, opponent_name)
        for requirement in requirements
    )


def get_applicable_statuses(entities, rules, event_bus, entity_name, trigger):
    """!
    @brief Finds every status definition for a given trigger whose requirements the entity
        currently meets.
    @param entities The live entities dict.
    @param rules The loaded rules dict.
    @param event_bus The EventBus, forwarded to entity_matches_requirements.
    @param entity_name The name of the entity to check.
    @param trigger The trigger name to filter statuses by (ex: "on_damage").
    @return A list of matching status definitions.
    """
    return [
        status for status in rules.get("status", [])
        if status.get("trigger") == trigger
        and entity_matches_requirements(entities, event_bus, entity_name, status.get("requirements", []))
    ]


def evaluate_statuses(entities, rules, event_bus, entity_name, trigger):
    """!
    @brief Applies every status matching the given trigger that the entity currently
        qualifies for, then dismisses any condition this same trigger's statuses previously
        applied whose requirements no longer hold.
    @param entities The live entities dict.
    @param rules The loaded rules dict.
    @param event_bus The EventBus, forwarded to apply_condition/dismiss_condition.
    @param entity_name The name of the entity to evaluate.
    @param trigger The trigger name to evaluate (ex: "on_damage").
    @return The list of status definitions that were applied.
    """
    matched_statuses = get_applicable_statuses(entities, rules, event_bus, entity_name, trigger)
    matched_conditions = set()
    for status in matched_statuses:
        apply_block = status.get("apply")
        if apply_block and apply_block.get("condition"):
            apply_condition(
                entities, event_bus, entity_name,
                apply_block["condition"],
                duration=apply_block.get("duration"),
                length=apply_block.get("length"),
                dismiss=apply_block.get("dismiss"),
                rules=rules,
            )
            matched_conditions.add(apply_block["condition"])

    active_conditions = get_active_conditions(entities, entity_name)
    for status in rules.get("status", []):
        if status.get("trigger") != trigger:
            continue
        apply_block = status.get("apply")
        condition_name = apply_block.get("condition") if apply_block else None
        if not condition_name or condition_name in matched_conditions:
            continue
        active_entry = active_conditions.get(condition_name)
        if active_entry is not None and not active_entry.get("dismiss"):
            dismiss_condition(entities, event_bus, entity_name, condition_name)

    return matched_statuses


def apply_damage(entities, rules, event_bus, entity_name, amount, actor_name=None):
    """!
    @brief Subtracts damage from an entity's current HP, floored at 0, evaluates on_damage
        statuses, and runs entity_name's own [entity.on_damage] program -- pure-to-pure, right
        alongside evaluate_statuses, never lifted up to a DMCore wrapper.
    @param entities The live entities dict.
    @param rules The loaded rules dict.
    @param event_bus The EventBus to publish a log_info line to.
    @param entity_name The name of the entity taking damage.
    @param amount The amount of damage to apply.
    @param actor_name The entity that dealt the damage, if known (ctx's own "actor" for
        on_damage) -- absent for damage with no real attacker (ex: a trap, per-round upkeep).
    @return The entity's remaining HP.
    """
    entity = entities.get(entity_name)
    if entity is None:
        return 0
    current_hp = get_current_hp(entities, entity_name)
    entity["hp"] = max(0, current_hp - amount)
    event_bus.publish("log_info", f"{entity_name} takes {amount} damage ({current_hp} -> {entity['hp']} HP).")
    evaluate_statuses(entities, rules, event_bus, entity_name, "on_damage")
    Program_Interpreter.run_program(
        entity.get("on_damage"), {"actor": actor_name, "target": entity_name}, entities, rules, event_bus,
    )
    return entity["hp"]


def apply_healing(entities, rules, event_bus, entity_name, amount, actor_name=None):
    """!
    @brief Adds HP to an entity, clamped at their own max_hp, and runs entity_name's own
        [entity.on_heal] program -- the symmetric counterpart to apply_damage's own on_damage.
    @param entities The live entities dict.
    @param rules The loaded rules dict.
    @param event_bus The EventBus to publish a log_info line to.
    @param entity_name The name of the entity being healed.
    @param amount The amount of HP to restore.
    @param actor_name The entity that healed this one, if known (ctx's own "actor" for on_heal).
    @return The entity's current HP after healing.
    """
    entity = entities.get(entity_name)
    if entity is None:
        return 0
    current_hp = get_current_hp(entities, entity_name)
    max_hp = entity.get("max_hp", current_hp)
    entity["hp"] = min(max_hp, current_hp + amount)
    event_bus.publish("log_info", f"{entity_name} heals {amount} HP ({current_hp} -> {entity['hp']} HP).")
    evaluate_statuses(entities, rules, event_bus, entity_name, "on_damage")
    Program_Interpreter.run_program(
        entity.get("on_heal"), {"actor": actor_name, "target": entity_name}, entities, rules, event_bus,
    )
    return entity["hp"]


def resolve_bonus(entities, rules, event_bus, attacker_name, bonus):
    """!
    @brief Resolves a damage_value's bonus field, which may be a flat number or a
        "user.<rule>" reference into a rules.toml formula.
    @param entities The live entities dict.
    @param rules The loaded rules dict.
    @param event_bus The EventBus to publish a log_warning line to on an unknown reference.
    @param attacker_name The name of the entity dealing damage.
    @param bonus The bonus field from a damage_value table.
    @return The resolved flat bonus amount.
    """
    if isinstance(bonus, (int, float)):
        return bonus
    if not isinstance(bonus, str):
        return 0

    rule_name = bonus.split(".")[-1]
    formula = rules.get(rule_name)
    if not formula:
        event_bus.publish("log_warning", f"Unknown damage bonus reference: {bonus}")
        return 0

    skill_stats = entities.get(attacker_name, {}).get("skills", {}).get(formula.get("skill"), {"dice": 0})
    return skill_stats.get("dice", 0) // formula.get("divisor", 1)


def get_equipped_weapon(entities, entity_name):
    """!
    @brief Finds the first of an entity's equipped items that deals damage.
    @param entities The live entities dict.
    @param entity_name The name of the entity to check.
    @return The equipped weapon's entity table, or None if nothing equipped has a damage_value.
    """
    entity = entities.get(entity_name, {})
    for item_name in entity.get("equipped", {}).values():
        item = entities.get(item_name)
        if item and "damage_value" in item:
            return item
    return None


def resolve_weapon_reference(entities, attacker_name, value, field):
    """!
    @brief Resolves a damage_value's dice/pips field when it's the "user.weapon.<field>"
        indirection.
    @param entities The live entities dict.
    @param attacker_name The name of the entity dealing damage.
    @param value The dice or pips field from a damage_value table.
    @param field Which field this is ("dice" or "pips"), matched against "user.weapon.<field>".
    @return value unchanged if it isn't that reference; otherwise the attacker's equipped
            weapon's matching field, or 0 if the attacker has no equipped weapon.
    """
    if value != f"user.weapon.{field}":
        return value
    weapon = get_equipped_weapon(entities, attacker_name)
    if weapon is None:
        return 0
    return weapon.get("damage_value", {}).get(field, 0)


def resolve_damage_value(entities, rules, event_bus, attacker_name, damage_value):
    """!
    @brief Rolls a damage_value's dice/pips and adds its resolved bonus.
    @param entities The live entities dict.
    @param rules The loaded rules dict.
    @param event_bus The EventBus to publish a log_warning line to.
    @param attacker_name The name of the entity dealing damage.
    @param damage_value A {dice, pips, bonus} table from an ability, weapon, or spell.
    @return The total rolled damage before any reduction.
    """
    dice = resolve_weapon_reference(entities, attacker_name, damage_value.get("dice", 0), "dice")
    pips = resolve_weapon_reference(entities, attacker_name, damage_value.get("pips", 0), "pips")
    if not isinstance(dice, (int, float)) or not isinstance(pips, (int, float)):
        event_bus.publish("log_warning", f"Unsupported damage dice/pips reference: {damage_value}")
        dice, pips = 0, 0

    bonus = resolve_bonus(entities, rules, event_bus, attacker_name, damage_value.get("bonus", 0))
    return roll_dice(int(dice), int(pips)) + bonus


def get_damage_reduction(entities, defender_name, damage_tags):
    """!
    @brief Sums the rolled reduction against the given damage tags: the defender's own
        innate resistance_value/resistance_tags plus the rolled armor value of any equipped
        items that resist the same tags.
    @param entities The live entities dict.
    @param defender_name The name of the entity taking damage.
    @param damage_tags The damage tags of the incoming attack (ex: ["fire"]).
    @return The total damage reduction.
    """
    defender = entities.get(defender_name, {})
    reduction = 0

    resistance_value = defender.get("resistance_value")
    resistance_tags = defender.get("resistance_tags", [])
    resistance_bypassed = any(tag in defender.get("resistance_bypass_tags", []) for tag in damage_tags)
    if resistance_value and not resistance_bypassed and any(tag in resistance_tags for tag in damage_tags):
        reduction += roll_dice(resistance_value.get("dice", 0), resistance_value.get("pips", 0))

    for item_name in defender.get("equipped", {}).values():
        item = entities.get(item_name, {})
        armor_value = item.get("armor_value")
        armor_tags = item.get("armor_tags", [])
        armor_bypassed = any(tag in item.get("armor_bypass_tags", []) for tag in damage_tags)
        if armor_value and not armor_bypassed and any(tag in armor_tags for tag in damage_tags):
            reduction += roll_dice(armor_value.get("dice", 0), armor_value.get("pips", 0))

    return reduction


def get_vulnerability_bonus(entities, defender_name, damage_tags):
    """!
    @brief Rolls the extra damage a defender's own vulnerability_value/vulnerability_tags
        adds on a matching hit.
    @param entities The live entities dict.
    @param defender_name The name of the entity taking damage.
    @param damage_tags The damage tags of the incoming attack (ex: ["water"]).
    @return The rolled bonus damage, or 0 if no tag matches.
    """
    defender = entities.get(defender_name, {})
    vulnerability_value = defender.get("vulnerability_value")
    vulnerability_tags = defender.get("vulnerability_tags", [])
    if vulnerability_value and any(tag in vulnerability_tags for tag in damage_tags):
        return roll_dice(vulnerability_value.get("dice", 0), vulnerability_value.get("pips", 0))
    return 0


def matches_supertype_or_subtype(entity, spec):
    """!
    @brief Whether entity's own supertype or subtype matches either list in spec -- the shared
        "what kind of thing is this" filter get_damage_bonus_vs (the Pathfinder "Holy"/"Bane"
        shape) and _apply_dispel_if_hit (DM_Core.py, the Pathfinder "Dispel Magic" shape) both
        key on, rather than each re-deriving the same OR-of-two-lists check independently.
    @param entity The entity dict to check.
    @param spec A table carrying "supertypes"/"subtypes" (both optional, each a list of
        strings; either or both absent defaults to an empty list).
    @return True if entity's own "supertype" is in spec's "supertypes", or its "subtype" is in
            spec's "subtypes" -- neither key present at all matches nothing, not everything.
    """
    return entity.get("supertype") in spec.get("supertypes", []) or entity.get("subtype") in spec.get("subtypes", [])


def get_damage_bonus_vs(entities, defender_name, ability):
    """!
    @brief Rolls an ability's own damage_bonus_vs bonus -- extra damage that only applies
        against a defender of a particular kind, matched by supertype/subtype rather than by
        damage_tags overlap (which only ever checks the defender's resistance/immunity/
        vulnerability, not what it *is*). This is the Pathfinder "Holy"/"Bane" shape (bonus
        damage vs. a creature type), which damage_tags alone can't express -- a "holy" damage
        tag would need every undead entity to also carry a matching vulnerability_tag, an
        indirect workaround rather than checking the defender's own supertype directly.
    @param entities The live entities dict.
    @param defender_name The name of the entity taking damage.
    @param ability A table optionally carrying "damage_bonus_vs" = {supertypes, subtypes,
        value = {dice, pips, bonus}}.
    @return The rolled bonus damage, or 0 if the defender's supertype/subtype doesn't match
        either list (both default to empty, so an ability authoring "damage_bonus_vs" with
        neither key matches nothing).
    """
    spec = ability.get("damage_bonus_vs")
    if not spec:
        return 0
    defender = entities.get(defender_name, {})
    if not matches_supertype_or_subtype(defender, spec):
        return 0
    value = spec.get("value", {})
    return roll_dice(value.get("dice", 0), value.get("pips", 0)) + value.get("bonus", 0)


def is_immune_to(entities, defender_name, damage_tags):
    """!
    @brief Whether an entity's immunity_tags fully negate an incoming attack's damage tags.
        "any" is a reserved wildcard: an entity authoring immunity_tags = ["any"] is immune to
        every damage_tags value that exists today or gets authored later (no need to enumerate
        every physical/energy type, or revisit this entity's own list when a new damage_tags
        value is invented elsewhere), and to a tagless attack too (an empty damage_tags would
        otherwise never match anything, ordinarily). It's also the only thing that can stop an
        attack's own damage_tags = ["any"] (true, unpreventable damage) -- get_damage_reduction/
        get_vulnerability_bonus never special-case "any" at all, since no real resistance_tags/
        armor_tags/vulnerability_tags list would ever legitimately contain the literal string
        "any", so an "any"-tagged hit already skips reduction/vulnerability for every defender
        without any further code, matching purely on ordinary tag membership.
    @param entities The live entities dict.
    @param defender_name The name of the entity taking damage.
    @param damage_tags The damage tags of the incoming attack (ex: ["fire"]).
    @return True if the defender's own immunity_tags author the "any" wildcard, or any damage
            tag matches the defender's immunity_tags.
    """
    immunity_tags = entities.get(defender_name, {}).get("immunity_tags", [])
    if "any" in immunity_tags:
        return True
    return any(tag in immunity_tags for tag in damage_tags)


def apply_on_hit_condition(entities, rules, event_bus, defender_name, ability, damage_tags):
    """!
    @brief Applies an ability's own on_hit_condition directly to whoever it just hit -- no
        [entity.test] detour needed. This is the Pathfinder "Wounding"/poison-on-hit shape
        (Rules/Fantasy/reference/pathfinder_mapping.toml's magic_item "Wounding" row and
        creature_ability "Poison" row both flagged this as the missing piece: the only way to
        apply a condition from an attack used to be via a target's own [entity.test], which a
        plain weapon/ability hit never has). Skipped entirely if the defender is immune to the
        ability's own damage_tags -- an attack a creature is fully immune to shouldn't also
        inflict a condition tied to that same damage.
    @param entities The live entities dict.
    @param rules The loaded rules dict, forwarded to apply_condition (for "days" duration
        conversion).
    @param event_bus The EventBus, forwarded to apply_condition.
    @param defender_name The name of the entity that was just hit.
    @param ability A table optionally carrying "on_hit_condition" = {condition, chance,
        duration, length, dismiss}. "chance" (1-100, default 100) is the percent chance the
        condition actually lands -- absent means it always does.
    @param damage_tags The hit's own damage_tags, checked against the defender's immunity_tags.
    """
    on_hit = ability.get("on_hit_condition")
    if not on_hit or is_immune_to(entities, defender_name, damage_tags):
        return
    if random.randint(1, 100) > on_hit.get("chance", 100):
        return
    apply_condition(
        entities, event_bus, defender_name, on_hit["condition"],
        duration=on_hit.get("duration"), length=on_hit.get("length"), dismiss=on_hit.get("dismiss"),
        rules=rules,
    )


def calculate_damage(entities, rules, event_bus, attacker_name, defender_name, ability):
    """!
    @brief Calculates and applies damage from an attacker's ability to a defender, including
        immunity, resistance/armor reduction, vulnerability, and a supertype/subtype-matched
        damage_bonus_vs. Also records ability's own damage_tags onto defender_name's own
        "recent_damage_tags", and applies the ability's own on_hit_condition (if any) directly
        to the defender.
    @param entities The live entities dict.
    @param rules The loaded rules dict.
    @param event_bus The EventBus to publish a log_info line to.
    @param attacker_name The name of the entity dealing damage.
    @param defender_name The name of the entity taking damage.
    @param ability A table with damage_value {dice, pips, bonus} and damage_tags.
    @return A dict describing the raw damage, reduction, vulnerability/vs-type bonus, net
        damage, and the defender's remaining HP.
    """
    damage_value = ability.get("damage_value", {"dice": 0, "pips": 0, "bonus": 0})
    damage_tags = ability.get("damage_tags", [])

    raw_damage = resolve_damage_value(entities, rules, event_bus, attacker_name, damage_value)
    if is_immune_to(entities, defender_name, damage_tags):
        reduction = raw_damage
        vulnerability_bonus = 0
        bonus_vs = 0
    else:
        reduction = get_damage_reduction(entities, defender_name, damage_tags)
        vulnerability_bonus = get_vulnerability_bonus(entities, defender_name, damage_tags)
        bonus_vs = get_damage_bonus_vs(entities, defender_name, ability)
    net_damage = max(0, raw_damage + vulnerability_bonus + bonus_vs - reduction)
    remaining_hp = apply_damage(entities, rules, event_bus, defender_name, net_damage, actor_name=attacker_name)
    apply_on_hit_condition(entities, rules, event_bus, defender_name, ability, damage_tags)

    defender = entities.get(defender_name)
    if defender is not None and damage_tags:
        defender.setdefault("recent_damage_tags", set()).update(damage_tags)

    event_bus.publish(
        "log_info",
        f"{attacker_name} deals {raw_damage} raw damage to {defender_name}"
        f"{f' (+{vulnerability_bonus} vulnerability)' if vulnerability_bonus else ''}"
        f"{f' (+{bonus_vs} vs. type)' if bonus_vs else ''}"
        f", reduced by {reduction} -> {net_damage} net damage."
    )
    return {
        "attacker": attacker_name,
        "defender": defender_name,
        "raw_damage": raw_damage,
        "reduction": reduction,
        "vulnerability_bonus": vulnerability_bonus,
        "bonus_vs": bonus_vs,
        "net_damage": net_damage,
        "remaining_hp": remaining_hp,
    }


def get_opposing_skill(entities, skills, skill_name, defender_name):
    """!
    @brief Finds the defender's best (highest-rated) skill among a skill's opposing skills.
    @param entities The live entities dict.
    @param skills The loaded skill catalog (self.skills -- distinct from rules).
    @param skill_name The attacker's skill.
    @param defender_name The name of the defending entity.
    @return The defender's highest-rated matching opposing skill name, or None.
    """
    opposes = skills.get(skill_name, {}).get("opposes", [])
    defender_skills = entities.get(defender_name, {}).get("skills", {})
    best_skill = None
    best_rating = None
    for opposing_skill in opposes:
        stats = defender_skills.get(opposing_skill)
        if stats is None:
            continue
        rating = skill_rating(stats.get("dice", 0), stats.get("pips", 0))
        if best_rating is None or rating > best_rating:
            best_rating = rating
            best_skill = opposing_skill
    return best_skill


def resolve_action(entities, rules, event_bus, entity_name, skill_name, difficulty=0, dice_penalty=0):
    """!
    @brief Resolves the outcome of an entity using a skill against a difficulty.
    @param entities The live entities dict.
    @param rules The loaded rules dict.
    @param event_bus The EventBus to publish a log_info line to.
    @param entity_name The name of the entity performing the action.
    @param skill_name The skill being used.
    @param difficulty The target number the roll must meet or beat.
    @param dice_penalty Whole dice subtracted from entity_name's own pool before rolling,
        floored at 0 dice.
    @return A dict describing the roll and whether it succeeded.
    """
    entity = entities.get(entity_name, {})
    skill_stats = entity.get("skills", {}).get(skill_name, {"dice": 0, "pips": 0})
    condition_modifier = get_condition_modifier(entities, rules, entity_name, skill_name)
    equip_bonus = get_equipped_skill_bonus(entities, rules, entity_name, skill_name)
    dice = max(0, skill_stats.get("dice", 0) - dice_penalty + condition_modifier["dice"] + equip_bonus["dice"])
    pips = skill_stats.get("pips", 0) + condition_modifier["pips"] + equip_bonus["pips"]
    roll = roll_dice(dice, pips) + condition_modifier["bonus"]
    success = roll >= difficulty
    event_bus.publish(
        "log_info",
        f"Resolved action: {entity_name} used {skill_name}, rolled {roll} vs difficulty {difficulty} -> {'success' if success else 'failure'}."
    )
    return {
        "entity": entity_name,
        "skill": skill_name,
        "roll": roll,
        "difficulty": difficulty,
        "success": success,
    }


def resolve_opposed_action(entities, rules, skills, event_bus, attacker_name, skill_name, defender_name, dice_penalty=0, ability=None):
    """!
    @brief Resolves a skill roll opposed by a defending entity's matching skill. Range is
        checked by the caller before this is reached at all.
    @param entities The live entities dict.
    @param rules The loaded rules dict.
    @param skills The loaded skill catalog.
    @param event_bus The EventBus to publish log lines to.
    @param attacker_name The name of the acting entity.
    @param skill_name The skill being used by the attacker.
    @param defender_name The name of the opposing entity.
    @param dice_penalty Forwarded to resolve_action for attacker_name's own roll only.
    @param ability The attacker's own resolved weapon/spell/technique, if any -- consulted only
        for its optional "ignores_concealment" flag (see below); every other field is unused
        here. None (the default, every pre-existing caller) never bypasses concealment.
    @return A dict describing the roll, the opposing skill used (if any), and the outcome. An
        otherwise-successful roll can still come back with "success": False and its own
        "concealed_miss": True if defender_name's own concealment (get_concealment) rolls a hit
        -- the Pathfinder Invisible/concealment shape (a successful attack roll can still just
        miss outright), unless "ability" authors "ignores_concealment" (ex: a ghost touch/
        seeking weapon).
    """
    opposing_skill = get_opposing_skill(entities, skills, skill_name, defender_name)
    if opposing_skill:
        defender_stats = entities[defender_name]["skills"][opposing_skill]
        defender_modifier = get_condition_modifier(entities, rules, defender_name, opposing_skill)
        defender_equip_bonus = get_equipped_skill_bonus(entities, rules, defender_name, opposing_skill)
        defender_dice = max(0, defender_stats.get("dice", 0) + defender_modifier["dice"] + defender_equip_bonus["dice"])
        defender_pips = defender_stats.get("pips", 0) + defender_modifier["pips"] + defender_equip_bonus["pips"]
        difficulty = roll_dice(defender_dice, defender_pips) + defender_modifier["bonus"]
    else:
        difficulty = 0

    result = resolve_action(entities, rules, event_bus, attacker_name, skill_name, difficulty, dice_penalty=dice_penalty)
    result["defender"] = defender_name
    result["opposing_skill"] = opposing_skill
    if result["success"] and not (ability and ability.get("ignores_concealment")):
        concealment = get_concealment(entities, rules, defender_name)
        if concealment and random.randint(1, 100) <= concealment:
            result["success"] = False
            result["concealed_miss"] = True
            event_bus.publish("log_info", f"{attacker_name}'s attack on {defender_name} misses outright -- concealed.")
    return result
