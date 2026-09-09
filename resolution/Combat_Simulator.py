"""!
@file Combat_Simulator.py
@brief Pure, DMCore-independent Monte Carlo combat simulation -- same "pure module, DMCore
    reaches in" shape Challenge_Rating.py/Combat_Resolution.py already set (see CLAUDE.md's
    "Challenge rating"). Runs real fights over Combat_Resolution.py's own pure primitives
    (resolve_opposed_action, calculate_damage, get_current_hp, ...) against a bare {entities,
    rules, skills_catalog} triple, no live EventBus subscription/scenario load required. This is
    what makes "is this creature appropriately dangerous" a number you can literally test (win
    rate over N simulated fights) rather than a formula you eyeball -- see
    scripts/calibrate_challenge_rating.py, which uses this module to calibrate
    Challenge_Rating.py's own hp_divisor by grid-searching for the value that makes win rate
    against a reference PC shape-invariant (the same nominal CR should mean the same danger
    regardless of whether that CR was spent on offense or HP).
"""

from Event_Bus import EventBus
from resolution.Challenge_Rating import skill_rating
from resolution.Combat_Resolution import calculate_damage, get_current_hp, resolve_opposed_action


def best_offense_skill(entity, skills_catalog):
    """!
    @brief The highest-rated combat_role == "offense" skill on a bare entity dict -- the
        simulator's own stand-in for DM_Combat.py's _best_offense_package/_skills_with_role,
        pure and scoped to exactly what a simulated fight needs (which named skill this
        combatant swings with), not the full equipped-weapon/ability candidate search a live
        scene entity's real attack resolution does.
    @param entity The combatant's own entity dict (must carry "skills").
    @param skills_catalog The setting's own {skill_name: {"combat_role", ...}} table.
    @return The winning skill's own name, or None if this entity trains no offense-tagged skill
        at all (a pure punching bag -- still simulatable, it simply never lands a hit).
    """
    entity_skills = entity.get("skills", {})
    offense_names = [
        name for name in entity_skills if skills_catalog.get(name, {}).get("combat_role") == "offense"
    ]
    if not offense_names:
        return None
    return max(
        offense_names,
        key=lambda name: skill_rating(entity_skills[name].get("dice", 0), entity_skills[name].get("pips", 0)),
    )


def _take_turn(entities, rules, skills_catalog, event_bus, attacker_name, defender_name):
    """!
    @brief One combatant's own turn against the other -- an opposed roll on its best offense
        skill, then calculate_damage on a hit. A no-op (never rolls) if attacker_name has no
        offense-tagged skill to swing with at all.
    @param entities The live entities dict (mutated: defender's own "hp").
    @param rules The loaded rules dict (may be {} -- a simulated fighter carries no
        active_conditions/equipped gear, so condition/equip-bonus lookups all resolve to
        nothing regardless of what rules itself contains).
    @param skills_catalog The setting's own skill catalog (for combat_role/opposes lookups).
    @param event_bus The EventBus to forward into resolve_opposed_action/calculate_damage.
    @param attacker_name The name of the entity acting this turn.
    @param defender_name The name of the entity being acted against.
    """
    skill_name = best_offense_skill(entities[attacker_name], skills_catalog)
    if skill_name is None:
        return
    ability = {
        "damage_value": entities[attacker_name].get("damage_value", {"dice": 0, "pips": 0, "bonus": 0}),
        "damage_tags": entities[attacker_name].get("damage_tags", []),
    }
    result = resolve_opposed_action(entities, rules, skills_catalog, event_bus, attacker_name, skill_name, defender_name, ability=ability)
    if result["success"]:
        calculate_damage(entities, rules, event_bus, attacker_name, defender_name, ability)


def simulate_fight(entities, rules, skills_catalog, name_a, name_b, event_bus, max_rounds=50):
    """!
    @brief Runs one simulated fight between name_a/name_b to completion, mutating entities in
        place (see run_matchup for the "fresh entities per trial" caller contract). Each round,
        both combatants act if still alive, in an order alternating by round parity (round 1:
        a then b; round 2: b then a; ...) so neither side gets a permanent first-mover edge
        across many trials.
    @param entities The live entities dict, already containing both combatants (each needs
        "skills"/"max_hp", plus its own "damage_value"/"damage_tags" for _take_turn).
    @param rules The loaded rules dict (may be {}).
    @param skills_catalog The setting's own skill catalog.
    @param name_a/name_b The two combatants' own entity names.
    @param event_bus The EventBus to forward into every roll/damage call.
    @param max_rounds The round cap before this fight is declared a timeout rather than a win --
        keeps a degenerate matchup (ex: neither side can land a hit) from looping forever.
    @return {"winner": name_a, name_b, or None (timeout), "rounds": int, "timeout": bool}.
    """
    for round_number in range(1, max_rounds + 1):
        order = (name_a, name_b) if round_number % 2 == 1 else (name_b, name_a)
        for actor, opponent in ((order[0], order[1]), (order[1], order[0])):
            if get_current_hp(entities, actor) <= 0 or get_current_hp(entities, opponent) <= 0:
                continue
            _take_turn(entities, rules, skills_catalog, event_bus, actor, opponent)

        a_alive = get_current_hp(entities, name_a) > 0
        b_alive = get_current_hp(entities, name_b) > 0
        if not a_alive or not b_alive:
            if a_alive:
                return {"winner": name_a, "rounds": round_number, "timeout": False}
            if b_alive:
                return {"winner": name_b, "rounds": round_number, "timeout": False}
            return {"winner": None, "rounds": round_number, "timeout": False}

    return {"winner": None, "rounds": max_rounds, "timeout": True}


def run_matchup(build_a, build_b, rules, skills_catalog, trials=500, max_rounds=50):
    """!
    @brief Runs `trials` independent simulate_fight calls and aggregates the outcome -- the
        actual Monte Carlo step: build_a/build_b are zero-arg factories (not shared entity
        dicts) specifically so one trial's mutated HP/active_conditions never leaks into the
        next.
    @param build_a/build_b Zero-arg callables, each returning a fresh {"name": ..., **entity}
        dict for that side (the returned dict's own "name" key is used as its entities-dict
        key and simulate_fight name).
    @param rules The loaded rules dict (may be {}).
    @param skills_catalog The setting's own skill catalog.
    @param trials How many independent fights to run.
    @param max_rounds Forwarded to simulate_fight.
    @return {"a_win_rate", "b_win_rate", "timeout_rate", "avg_rounds"}.
    """
    a_wins = b_wins = timeouts = 0
    total_rounds = 0
    for _ in range(trials):
        entity_a = build_a()
        entity_b = build_b()
        name_a, name_b = entity_a["name"], entity_b["name"]
        entities = {name_a: entity_a, name_b: entity_b}
        outcome = simulate_fight(entities, rules, skills_catalog, name_a, name_b, EventBus(), max_rounds=max_rounds)
        total_rounds += outcome["rounds"]
        if outcome["timeout"]:
            timeouts += 1
        elif outcome["winner"] == name_a:
            a_wins += 1
        elif outcome["winner"] == name_b:
            b_wins += 1

    return {
        "a_win_rate": a_wins / trials,
        "b_win_rate": b_wins / trials,
        "timeout_rate": timeouts / trials,
        "avg_rounds": total_rounds / trials,
    }


def _lowest_hp_living_target(entities, candidate_names):
    """!
    @brief Picks a focus-fire target: the living candidate with the lowest current HP (ties
        broken by candidate_names' own order) -- a simple, common tactical default (finish off
        the weakest target first) rather than every actor picking independently at random, which
        would dilute damage across the whole opposing side and rarely finish anyone off.
    @param entities The live entities dict.
    @param candidate_names The opposing side's own roster (dead members are skipped).
    @return The chosen target's own name, or None if every candidate is already dead.
    """
    living = [name for name in candidate_names if get_current_hp(entities, name) > 0]
    if not living:
        return None
    return min(living, key=lambda name: get_current_hp(entities, name))


def simulate_group_fight(entities, rules, skills_catalog, side_a_names, side_b_names, event_bus, max_rounds=50):
    """!
    @brief The group-vs-group counterpart to simulate_fight: every living member of both sides
        acts once per round (order alternates by round parity, side_a first on odd rounds, same
        "no permanent first-mover edge" reasoning simulate_fight already uses), each one
        targeting the opposing side's own lowest-HP living member (_lowest_hp_living_target --
        focus fire, not everyone picking independently). Ends the moment either side has no
        living members left, or max_rounds is reached (a timeout, not a win).
    @param entities The live entities dict, already containing every member of both sides.
    @param rules The loaded rules dict (may be {}).
    @param skills_catalog The setting's own skill catalog.
    @param side_a_names/side_b_names Each side's own roster of entity names.
    @param event_bus The EventBus to forward into every roll/damage call.
    @param max_rounds The round cap before this fight is declared a timeout.
    @return {"winner": "a", "b", or None (timeout), "rounds": int, "timeout": bool}.
    """
    def _alive(names):
        return [name for name in names if get_current_hp(entities, name) > 0]

    for round_number in range(1, max_rounds + 1):
        sides = (side_a_names, side_b_names) if round_number % 2 == 1 else (side_b_names, side_a_names)
        for acting_side, opposing_side in (sides, tuple(reversed(sides))):
            for actor in _alive(acting_side):
                if get_current_hp(entities, actor) <= 0:
                    continue  # may have died to an earlier actor's turn this same round
                target = _lowest_hp_living_target(entities, opposing_side)
                if target is None:
                    break
                _take_turn(entities, rules, skills_catalog, event_bus, actor, target)

        a_alive, b_alive = bool(_alive(side_a_names)), bool(_alive(side_b_names))
        if not a_alive or not b_alive:
            if a_alive:
                return {"winner": "a", "rounds": round_number, "timeout": False}
            if b_alive:
                return {"winner": "b", "rounds": round_number, "timeout": False}
            return {"winner": None, "rounds": round_number, "timeout": False}

    return {"winner": None, "rounds": max_rounds, "timeout": True}


def run_group_matchup(build_side_a, build_side_b, rules, skills_catalog, trials=500, max_rounds=50):
    """!
    @brief The group-vs-group counterpart to run_matchup: runs `trials` independent
        simulate_group_fight calls and aggregates the outcome.
    @param build_side_a/build_side_b Zero-arg callables, each returning a fresh list of
        {"name": ..., **entity} dicts for that side (fresh per trial, same "no cross-trial state
        leakage" contract run_matchup's own build_a/build_b already keep) -- every name across
        both sides combined must be unique (they all share one entities dict per trial).
    @param rules The loaded rules dict (may be {}).
    @param skills_catalog The setting's own skill catalog.
    @param trials How many independent fights to run.
    @param max_rounds Forwarded to simulate_group_fight.
    @return {"a_win_rate", "b_win_rate", "timeout_rate", "avg_rounds"}.
    """
    a_wins = b_wins = timeouts = 0
    total_rounds = 0
    for _ in range(trials):
        side_a = build_side_a()
        side_b = build_side_b()
        side_a_names = [entity["name"] for entity in side_a]
        side_b_names = [entity["name"] for entity in side_b]
        entities = {entity["name"]: entity for entity in (*side_a, *side_b)}
        outcome = simulate_group_fight(
            entities, rules, skills_catalog, side_a_names, side_b_names, EventBus(), max_rounds=max_rounds,
        )
        total_rounds += outcome["rounds"]
        if outcome["timeout"]:
            timeouts += 1
        elif outcome["winner"] == "a":
            a_wins += 1
        elif outcome["winner"] == "b":
            b_wins += 1

    return {
        "a_win_rate": a_wins / trials,
        "b_win_rate": b_wins / trials,
        "timeout_rate": timeouts / trials,
        "avg_rounds": total_rounds / trials,
    }
