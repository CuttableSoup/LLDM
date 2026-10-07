"""!
@file Ability_Effects.py
@brief Ability resolution -- what an actor's ability does once its roll has landed. One
    implementation for the player's own turn (DMCore._finish_rolled_outcome) and for every other
    entity's combat turn (Combat_Actions.resolve_behavior_action), so an NPC's spell does exactly
    what the same spell does in the player's hands: damage (single target or the AoE/multi-target
    pool the ability's own "targets" table describes, with save_for_half), summon, dispel, cure,
    teleport, spell materials, and the ability's own on_pass/on_fail program.

    Which target the ability points at is decided by the caller (the player's turn infers it from
    free text; an NPC's comes from its behavior list) -- this module takes a resolved target_name
    and does effects only. Everything that mutates a part of the game the combat graph can't reach
    (conjuring, banishing, moving, entering a location, spending materials, attitude drift) goes
    through ctx.hooks (CombatHooks, Combat_Actions.py); the rest reads and writes ctx directly.

    Each effect is data-gated: an entity only summons/dispels/cures/teleports if the ability it
    used authors that field, so an NPC gains nothing it wasn't already given. "teleport_to_location"
    is the one player-only effect -- it relocates the whole scene, which no NPC's own turn can do.
"""

import copy

import resolution.Combat_Actions as Combat_Actions
import resolution.Combat_Resolution as Combat_Resolution
from dm.DM_ActionOutcome import CureEffect, DamageEffect, DispelEffect, RolledOutcome, SummonEffect, TeleportEffect
from resolution.Combat_Resolution import matches_supertype_or_subtype, resolve_damage_value
from resolution.Program_Interpreter import run_program


def apply_ability_effects(
    ctx, actor_name, result, skill_name, named_ability, ability, target_name, via_test=False, input_text=None,
):
    """!
    @brief Everything that might apply once actor_name's roll has actually happened, in one call.
        Order matters: materials are spent before damage/summon effects are appended, mirroring a
        botched craft attempt's own "consume regardless of outcome" precedent.
    @param ctx The WorldContext, with ctx.hooks set.
    @param actor_name The entity using the ability.
    @param result The roll result, mutated in place with effects. Anything that isn't a
        RolledOutcome (a no-roll refusal) is left alone.
    @param skill_name The skill rolled, already resolved from any named ability.
    @param named_ability The resolved ability entity (technique/spell), or None.
    @param ability The attack ability already resolved for the roll, or None -- re-derived from
        named_ability or the actor's own equipment, so the test/no-target branches may leave it None.
    @param target_name The resolved target, or None (a self-only or untargeted ability).
    @param via_test True if the roll was a flat [entity.test] check (no damage, no program).
    @param input_text The actor's raw turn text, threaded into the ability's own program as ctx
        "input" -- read by ops that want the free-text content of the turn (ex: "suggestion").
    """
    if not isinstance(result, RolledOutcome):
        return
    _consume_materials(ctx, actor_name, named_ability)
    _apply_damage(ctx, actor_name, result, skill_name, named_ability, ability, target_name, via_test)
    _apply_summon(ctx, actor_name, result, named_ability)
    _apply_dispel(ctx, result, named_ability, target_name)
    _apply_cure(ctx, result, named_ability, target_name)
    _apply_teleport(ctx, actor_name, result, named_ability)
    _run_outcome_program(ctx, actor_name, result, skill_name, named_ability, ability, target_name, via_test, input_text)


def _consume_materials(ctx, actor_name, named_ability):
    """!
    @brief Spends a named ability's own "materials" once a real roll happened for it --
        unconditionally, success or failure alike (a fizzled cast still burns the reagent). The
        player's own cast is refused before the roll if it lacks them (DM_Core._resolve_roll); an
        NPC isn't gated, so it only spends what it actually carries.
    """
    if not named_ability or not named_ability.get("materials"):
        return
    inventory = list(ctx.entities.get(actor_name, {}).get("inventory", []))
    materials = []
    for material in named_ability["materials"]:
        quantity = material.get("quantity", 1)
        if inventory.count(material["item"]) < quantity and actor_name != ctx.player_name:
            continue
        materials.append(material)
        for _ in range(quantity):
            if material["item"] in inventory:
                inventory.remove(material["item"])
    if materials:
        ctx.hooks.consume_materials(actor_name, materials)


def _resolve_save_for_half(ctx, actor_name, ability, defender_name):
    """!
    @brief Rolls defender_name's own flat save against ability's "save_for_half" = {skill},
        checked at ability's own "difficulty" (default 10) -- the Pathfinder Reflex-half shape
        for an AoE-widened secondary target (never target_name itself, which already resolved
        through the ordinary opposed hit-or-miss roll). A failed save changes nothing (a deep copy
        of ability, full damage). A passed save halves it, UNLESS defender_name's own
        "negates_save_for_half" (a list of skill names) names this save's skill -- Pathfinder's
        Evasion, authored as data on the entity. The raw damage on a passed, non-negated save is
        rolled once here and folded into a per-target copy of ability whose "damage_value" becomes a
        flat {dice: 0, pips: 0, bonus: <halved>}, so calculate_damage still applies resistance/
        vulnerability/damage_bonus_vs on top without re-rolling. Never mutates the shared ability.
    @return A per-target copy of ability, or None if defender_name negates the save entirely (the
        caller skips that target).
    """
    spec = ability["save_for_half"]
    check = Combat_Resolution.resolve_action(ctx, defender_name, spec["skill"], difficulty=ability.get("difficulty", 10))
    if not check["success"]:
        return copy.deepcopy(ability)
    if spec["skill"] in ctx.entities.get(defender_name, {}).get("negates_save_for_half", []):
        return None
    raw_damage = resolve_damage_value(ctx, actor_name, ability.get("damage_value", {}))
    halved = copy.deepcopy(ability)
    halved["damage_value"] = {"dice": 0, "pips": 0, "bonus": raw_damage // 2}
    return halved


def _apply_damage(ctx, actor_name, result, skill_name, named_ability, ability, target_name, via_test):
    """!
    @brief Rolls and attaches ability damage if the roll succeeded and wasn't a flat [entity.test]
        check (a lockpick success must never also roll bonus weapon damage), and only if the
        resolved ability actually carries a "damage_value" -- a summoning spell is a real, matched
        ability but not an attack. Each real hit gets its own damage roll, DamageEffect and attitude
        nudge; "on_action" statuses (ex: Frightful Presence) fire once per turn that landed at
        least one such hit.
    """
    if not result.success or via_test:
        return
    if ability is None:
        ability = named_ability or Combat_Actions.find_attack_ability(ctx, actor_name, skill_name)
    if not ability or "damage_value" not in ability:
        return
    for defender_name in Combat_Actions.resolve_targets(ctx, actor_name, target_name, ability):
        if not defender_name:
            continue
        hit_ability = ability
        if defender_name != target_name and ability.get("save_for_half"):
            hit_ability = _resolve_save_for_half(ctx, actor_name, ability, defender_name)
            if hit_ability is None:
                continue
        damage = Combat_Actions.calculate_damage(ctx, actor_name, defender_name, hit_ability)
        result.effects.append(DamageEffect(
            defender=damage["defender"], net_damage=damage["net_damage"], remaining_hp=damage["remaining_hp"],
        ))
        ctx.hooks.nudge_combat_hit_attitude(defender_name, actor_name, damage.get("net_damage", 0))
    Combat_Actions.evaluate_proximity_statuses(ctx, actor_name, "on_action")


def _apply_summon(ctx, actor_name, result, named_ability):
    """!
    @brief Conjures a temporary ally at the caster's own band if the named ability authors a
        "summon" table ({"name"|"template", "duration"}) and the roll succeeded. Not "against"
        anyone, so it fires whether the roll was an auto-success or a contested one.
    """
    if not result.success or not named_ability:
        return
    summon_spec = named_ability.get("summon")
    if not summon_spec:
        return
    summoned_name = ctx.hooks.summon_creature(summon_spec, actor_name)
    if summoned_name:
        result.effects.append(SummonEffect(name=summoned_name))


def _apply_dispel(ctx, result, named_ability, target_name):
    """!
    @brief Banishes target_name outright if the named ability authors "dispel" (a {"supertypes",
        "subtypes"} filter, the shape damage_bonus_vs uses), the roll succeeded and the target's
        own supertype/subtype matches. A mismatched target simply isn't dispellable -- the "used
        on the wrong thing just wastes the action" shape of Pathfinder's Dispel Magic.
    """
    if not result.success or not named_ability or not target_name:
        return
    dispel_spec = named_ability.get("dispel")
    if not dispel_spec:
        return
    if not matches_supertype_or_subtype(ctx.entities.get(target_name, {}), dispel_spec):
        return
    ctx.hooks.remove_entity_from_scene(target_name)
    result.effects.append(DispelEffect(name=target_name))


def _apply_cure(ctx, result, named_ability, target_name):
    """!
    @brief Dismisses every one of target_name's active conditions matching the named ability's
        "cure" filter (against the [[condition]] catalog) if the roll succeeded. A target with
        nothing matching still gets an empty CureEffect rather than being silently skipped.
    """
    if not result.success or not named_ability or not target_name:
        return
    cure_spec = named_ability.get("cure")
    if not cure_spec:
        return
    cured = Combat_Resolution.dismiss_matching_conditions(ctx, target_name, cure_spec)
    result.effects.append(CureEffect(target=target_name, conditions=cured))


def _apply_teleport(ctx, actor_name, result, named_ability):
    """!
    @brief Relocates the actor if the named ability authors a teleport-shaped field and the roll
        succeeded. "teleport_to_band" (an int) jumps to that band within the current room -- the
        hook is handed the signed delta, reusing its floor/ceiling clamping (Dimension Door).
        "teleport_to_location" ({location, room, band}) jumps the whole party to another known
        location outright, with no travel time and working mid-combat (Teleport's appeal is
        escaping a losing fight) -- player-only, since it changes the scene.
    """
    if not result.success or not named_ability:
        return
    destination_band = named_ability.get("teleport_to_band")
    if destination_band is not None:
        new_band = ctx.hooks.move_entity(actor_name, destination_band - Combat_Resolution.get_band(ctx, actor_name))
        if new_band is not None:
            result.effects.append(TeleportEffect(entity=actor_name, band=new_band))
    destination = named_ability.get("teleport_to_location")
    if destination and actor_name == ctx.player_name:
        ctx.hooks.enter_location(destination["location"], destination.get("room"), destination.get("band", 1))
        result.effects.append(TeleportEffect(entity=actor_name, location=destination["location"]))


def _run_outcome_program(ctx, actor_name, result, skill_name, named_ability, ability, target_name, via_test, input_text):
    """!
    @brief Runs the resolved ability's own on_pass/on_fail program once a real ability-based roll
        has resolved -- a skill whose only mechanical effect is a condition/attitude nudge (ex:
        intimidate, trip/disarm/sunder) does something on a pass/fail without a new Python branch
        per skill. Never fires for a flat [entity.test] check (its own on_pass/on_fail lives in
        DM_Core._run_test_outcome_program) or for a roll with no resolvable ability.
        resolve_targets is [target_name] alone for an ability with no "targets" table, or the wider
        AoE/multi-target pool, run once per resolved target.
    """
    if via_test:
        return
    if ability is None:
        ability = named_ability or Combat_Actions.find_attack_ability(ctx, actor_name, skill_name)
    if not ability:
        return
    program = ability.get("on_pass" if result.success else "on_fail")
    if not program:
        return
    for program_target in Combat_Actions.resolve_targets(ctx, actor_name, target_name, ability):
        run_program(
            # "roll" -- this roll's own total, for an op that keeps it (ex: "disguise", whose
            # quality is what a witness's observation must beat; see DM_Law.py).
            program, {"actor": actor_name, "target": program_target, "input": input_text, "roll": result.roll},
            ctx.entities, ctx.rules, ctx.event_bus,
        )
