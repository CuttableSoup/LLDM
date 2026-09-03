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

import resolution.Program_Interpreter as Program_Interpreter
import resolution.Social_Resolution as Social_Resolution
from resolution.Challenge_Rating import skill_rating

COMPARATORS = {
    ">": lambda actual, value: actual > value,
    "<": lambda actual, value: actual < value,
    ">=": lambda actual, value: actual >= value,
    "<=": lambda actual, value: actual <= value,
    "==": lambda actual, value: actual == value,
    "!=": lambda actual, value: actual != value,
    "in": lambda actual, value: actual in value,
    "not_in": lambda actual, value: actual not in value,
}


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


def apply_condition(entities, event_bus, entity_name, condition_name, duration=None, dismiss=None):
    """!
    @brief Marks a condition as active on an entity.
    @param entities The live entities dict.
    @param event_bus The EventBus to publish a log_info line to.
    @param entity_name The name of the entity gaining the condition.
    @param condition_name The name of the condition, as defined in the [[condition]] table.
    @param duration How long the condition lasts (ex: "fleeting", "scene", "permanent").
    @param dismiss What removes the condition (ex: "healing", "resurrection").
    """
    entity = entities.get(entity_name)
    if entity is None:
        return
    active_conditions = entity.setdefault("active_conditions", {})
    active_conditions[condition_name] = {"duration": duration, "dismiss": dismiss}
    event_bus.publish("log_info", f"{entity_name} gains condition '{condition_name}'.")


def dismiss_condition(entities, event_bus, entity_name, condition_name):
    """!
    @brief Removes a condition from an entity, if it's currently active.
    @param entities The live entities dict.
    @param event_bus The EventBus to publish a log_info line to.
    @param entity_name The name of the entity losing the condition.
    @param condition_name The name of the condition to remove.
    @return True if the condition was present and removed, False otherwise.
    """
    active_conditions = get_active_conditions(entities, entity_name)
    if condition_name not in active_conditions:
        return False
    del active_conditions[condition_name]
    event_bus.publish("log_info", f"{entity_name} loses condition '{condition_name}'.")
    return True


def get_condition_modifier(entities, rules, entity_name):
    """!
    @brief Sums the {dice, pips, bonus} roll modifier of every one of entity_name's own
        active_conditions that has a matching entry in rules.toml's own [[condition]] table.
    @param entities The live entities dict.
    @param rules The loaded rules dict.
    @param entity_name The name of the entity to sum modifiers for.
    @return A {"dice", "pips", "bonus"} dict, each defaulting to 0 if nothing applies.
    """
    active_conditions = get_active_conditions(entities, entity_name)
    condition_defs = {c.get("name"): c.get("modifier", {}) for c in rules.get("condition", [])}
    total = {"dice": 0, "pips": 0, "bonus": 0}
    for condition_name in active_conditions:
        modifier = condition_defs.get(condition_name)
        if not modifier:
            continue
        total["dice"] += modifier.get("dice", 0)
        total["pips"] += modifier.get("pips", 0)
        total["bonus"] += modifier.get("bonus", 0)
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


def entity_matches_requirements(entities, event_bus, entity_name, requirements, opponent_name=None):
    """!
    @brief Checks whether an entity currently satisfies every comparison in a status's
        (or a behavior's) requirements.
    @param entities The live entities dict.
    @param event_bus The EventBus to publish a log_warning line to on an unknown operator.
    @param entity_name The name of the entity to check.
    @param requirements A list of {field, operator, value} comparisons, all of which must hold.
    @param opponent_name The entity being acted against, if any.
    @return True if every comparison is satisfied.
    """
    for comparison in requirements:
        compare = COMPARATORS.get(comparison.get("operator"))
        if compare is None:
            event_bus.publish("log_warning", f"Unknown requirement operator: {comparison.get('operator')}")
            return False

        actual_value = get_comparable_value(entities, entity_name, comparison.get("field"), opponent_name)
        if actual_value is None or not compare(actual_value, comparison.get("value")):
            return False

    return True


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
                dismiss=apply_block.get("dismiss"),
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


def is_immune_to(entities, defender_name, damage_tags):
    """!
    @brief Whether an entity's immunity_tags fully negate an incoming attack's damage tags.
    @param entities The live entities dict.
    @param defender_name The name of the entity taking damage.
    @param damage_tags The damage tags of the incoming attack (ex: ["fire"]).
    @return True if any damage tag matches the defender's immunity_tags.
    """
    immunity_tags = entities.get(defender_name, {}).get("immunity_tags", [])
    return any(tag in immunity_tags for tag in damage_tags)


def calculate_damage(entities, rules, event_bus, attacker_name, defender_name, ability):
    """!
    @brief Calculates and applies damage from an attacker's ability to a defender, including
        immunity, resistance/armor reduction, and vulnerability. Also records ability's own
        damage_tags onto defender_name's own "recent_damage_tags".
    @param entities The live entities dict.
    @param rules The loaded rules dict.
    @param event_bus The EventBus to publish a log_info line to.
    @param attacker_name The name of the entity dealing damage.
    @param defender_name The name of the entity taking damage.
    @param ability A table with damage_value {dice, pips, bonus} and damage_tags.
    @return A dict describing the raw damage, reduction, vulnerability bonus, net damage, and
        the defender's remaining HP.
    """
    damage_value = ability.get("damage_value", {"dice": 0, "pips": 0, "bonus": 0})
    damage_tags = ability.get("damage_tags", [])

    raw_damage = resolve_damage_value(entities, rules, event_bus, attacker_name, damage_value)
    if is_immune_to(entities, defender_name, damage_tags):
        reduction = raw_damage
        vulnerability_bonus = 0
    else:
        reduction = get_damage_reduction(entities, defender_name, damage_tags)
        vulnerability_bonus = get_vulnerability_bonus(entities, defender_name, damage_tags)
    net_damage = max(0, raw_damage + vulnerability_bonus - reduction)
    remaining_hp = apply_damage(entities, rules, event_bus, defender_name, net_damage, actor_name=attacker_name)

    defender = entities.get(defender_name)
    if defender is not None and damage_tags:
        defender.setdefault("recent_damage_tags", set()).update(damage_tags)

    event_bus.publish(
        "log_info",
        f"{attacker_name} deals {raw_damage} raw damage to {defender_name}"
        f"{f' (+{vulnerability_bonus} vulnerability)' if vulnerability_bonus else ''}"
        f", reduced by {reduction} -> {net_damage} net damage."
    )
    return {
        "attacker": attacker_name,
        "defender": defender_name,
        "raw_damage": raw_damage,
        "reduction": reduction,
        "vulnerability_bonus": vulnerability_bonus,
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
    condition_modifier = get_condition_modifier(entities, rules, entity_name)
    dice = max(0, skill_stats.get("dice", 0) - dice_penalty + condition_modifier["dice"])
    pips = skill_stats.get("pips", 0) + condition_modifier["pips"]
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


def resolve_opposed_action(entities, rules, skills, event_bus, attacker_name, skill_name, defender_name, dice_penalty=0):
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
    @return A dict describing the roll, the opposing skill used (if any), and the outcome.
    """
    opposing_skill = get_opposing_skill(entities, skills, skill_name, defender_name)
    if opposing_skill:
        defender_stats = entities[defender_name]["skills"][opposing_skill]
        defender_modifier = get_condition_modifier(entities, rules, defender_name)
        defender_dice = max(0, defender_stats.get("dice", 0) + defender_modifier["dice"])
        defender_pips = defender_stats.get("pips", 0) + defender_modifier["pips"]
        difficulty = roll_dice(defender_dice, defender_pips) + defender_modifier["bonus"]
    else:
        difficulty = 0

    result = resolve_action(entities, rules, event_bus, attacker_name, skill_name, difficulty, dice_penalty=dice_penalty)
    result["defender"] = defender_name
    result["opposing_skill"] = opposing_skill
    return result
