"""!
@file calibrate_party_challenge_rating.py
@brief Standalone research script -- NOT part of the pytest suite, run by hand:
    `python scripts/calibrate_party_challenge_rating.py`.

    Follow-up to calibrate_challenge_rating.py, using the same Combat_Simulator.py machinery
    (now via its group-fight functions, simulate_group_fight/run_group_matchup) to check whether
    `calculate_party_challenge_rating`'s own plain sum of member ratings actually predicts group
    encounter danger the way `Challenge_Rating.py`'s docstring implies ("a larger party of
    individually modest ratings can still outrate one strong boss"). A first exploratory run
    (gladstone + thane, party CR 95, vs. a scaling-up pack of wolves, CR 46 each) found the party
    doesn't become an underdog until the wolves' SUMMED CR is roughly 1.9x the party's own --
    nowhere near the "equal CR is an even fight" a plain sum implies. A second pass checked
    Lanchester's Square Law (aimed-fire/focus-fire attrition scales with headcount SQUARED, not
    linearly -- exactly what Combat_Simulator's own _lowest_hp_living_target does): it predicts
    N_crossover ~= party_count * sqrt(party_avg_cr / monster_cr), which gets the DIRECTION right
    (crossover N grows faster than the plain-sum's linear N ~= party_total_cr / monster_cr) but
    consistently UNDERESTIMATES the actual crossover, worse for weaker monsters (a monster too
    individually weak to do meaningful damage in time never wins regardless of count -- a floor
    effect no smooth power law captures on its own).

    This pass fits a general two-parameter power law instead of assuming Lanchester's specific
    exponent:
        N_crossover / party_count ~= A * (party_avg_cr / monster_cr) ^ p
    (Lanchester is the special case A=1, p=0.5; the plain-sum model is A=1, p=1.) A/p are fit by
    least squares in log space against every non-censored crossover this script measures, across
    several monster CRs and two party sizes -- if a consistent A/p exists across both, that's a
    usable rule of thumb; if the fit is poor (party size doesn't cleanly factor out, or weak-
    monster censoring dominates), that's worth knowing too and reported plainly either way.

    A follow-up manual probe found the mirror-image effect too: a SINGLE synthetic boss (built
    via the real NPC_Generation.fit_skills_to_cr, not a hand-rolled parallel budget split) needs
    only ~70% of the party's own summed CR to be an even fight against it, with a sharp
    (not gradual) transition around that point. run_boss_phase (Phase 2, below) puts that under
    the same rigor as the swarm pass: bisection (not a linear scan, since boss CR is a continuous
    free parameter) across the same party sizes, plus three boss build shapes (tanky/balanced/
    glass-cannon, via offense_share) to check whether the crossover multiplier is sensitive to
    the boss's own stat spread the same way Part 1's calibrate_challenge_rating.py already found
    shape sensitivity in the single-creature formula.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

import copy
import math

import numpy as np

from Event_Bus import EventBus
from dm.DM_Core import DMCore
from resolution.Combat_Simulator import run_group_matchup
from resolution.NPC_Generation import fit_skills_to_cr

TRIALS = 400
MAX_N = 20
BOSS_SKILLS = ["blades", "dodge", "fortitude", "reflexes", "willpower"]
BOSS_DAMAGE = (3, 0)  # fixed (dice, pips) -- see fit_skills_to_cr's own damage_dice/damage_pips;
# passing this into fit_skills_to_cr itself (not bolted on afterward) is what makes its returned
# "blades" rating already account for the weapon's own contribution to offense_side -- an
# earlier draft of this script forgot to and got a boss whose *nominal* CR didn't match what it
# was actually built for at all (blades scaling unboundedly while damage output stayed capped).


def _snapshot(dm, name, suffix=""):
    """!@brief A Combat_Simulator-ready entity dict off a live DMCore entity -- its own best
        offense package (skill + damage), full skills, and max_hp; damage_tags dropped (no
        resistance/armor on any debug.toml entity here to interact with anyway)."""
    entity = dm.entities[name]
    skill_stats, dice, pips = dm._best_offense_package(name)
    return {
        "name": f"{name}{suffix}",
        "skills": copy.deepcopy(entity["skills"]),
        "max_hp": entity["max_hp"],
        "damage_value": {"dice": dice, "pips": pips, "bonus": 0},
        "damage_tags": [],
    }


def _find_crossover(build_party, build_monster_group, rules, skills_catalog, max_n=MAX_N, trials=TRIALS):
    """!
    @brief Scans monster group size N = 1, 2, 3, ... until the party's own win rate drops below
        50%, then linearly interpolates between the two bracketing win rates for a continuous
        crossover estimate (rather than reporting only the bracketing integers) -- a group
        encounter's own real "how many monsters is too many" question is inherently continuous
        (half a monster is a meaningless idea narratively, but a useful one for fitting a curve
        against). A coarse, single-pass scan (not a bisection) -- group encounters are small-N
        in practice, so a linear scan is cheap enough and avoids any monotonicity assumption
        bisection would need.
    @return The interpolated crossover N (a float), or None if the party never drops below 50%
        win rate by max_n (the monster is too weak for this scan range to matter at all --
        "censored" in survival-analysis terms; excluded from this script's own curve fit, since
        it names a lower bound, not an actual crossover value).
    """
    previous_n, previous_rate = None, None
    for n in range(1, max_n + 1):
        def build_group(n=n):
            return [build_monster_group(index) for index in range(n)]

        result = run_group_matchup(build_party, build_group, rules, skills_catalog, trials=trials)
        rate = result["a_win_rate"]
        if rate < 0.5:
            if previous_rate is None:
                return float(n)  # already losing at N=1 -- no lower bracket to interpolate from
            # Linear interpolation between (previous_n, previous_rate) and (n, rate) for where
            # the line crosses exactly 0.5.
            span = previous_rate - rate
            fraction = (previous_rate - 0.5) / span if span else 0.5
            return previous_n + fraction * (n - previous_n)
        previous_n, previous_rate = n, rate
    return None


def _fit_power_law(data_points):
    """!
    @brief Least-squares fit of log(n_crossover / party_count) = log(A) + p * log(cr_ratio) --
        i.e. n_crossover/party_count = A * cr_ratio^p -- across every (cr_ratio, n_crossover,
        party_count) triple given. Ordinary linear regression in log space (numpy.polyfit),
        the standard way to fit a power law from data that plausibly spans an order of magnitude
        (as CR ratios here do).
    @param data_points A list of (cr_ratio, n_crossover, party_count) tuples.
    @return (A, p, r_squared).
    """
    x = np.log([ratio for ratio, _, _ in data_points])
    y = np.log([n / count for _, n, count in data_points])
    p, log_a = np.polyfit(x, y, 1)
    predicted_y = p * x + log_a
    residual_ss = np.sum((y - predicted_y) ** 2)
    total_ss = np.sum((y - np.mean(y)) ** 2)
    r_squared = 1 - residual_ss / total_ss if total_ss else float("nan")
    return math.exp(log_a), p, r_squared


def _build_boss(target_cr, skills_catalog, offense_share):
    """!
    @brief A single synthetic boss entity, built via the real production NPC_Generation.
        fit_skills_to_cr (not a hand-rolled parallel budget split) at exactly target_cr, with a
        fixed BOSS_DAMAGE weapon passed into fit_skills_to_cr itself (see this module's own
        BOSS_DAMAGE comment) so the boss's own real calculate_challenge_rating actually equals
        target_cr, not just its blades rating in isolation.
    @param target_cr The boss's own real challenge rating.
    @param skills_catalog The setting's own skill catalog.
    @param offense_share Forwarded to fit_skills_to_cr -- the boss's own build shape (0.5 =
        balanced; lower skews it toward survival_side/HP, higher toward offense_side/damage).
    @return A zero-arg factory returning [boss_entity] (a one-member "side" list, ready for
        run_group_matchup's own build_side_a/build_side_b contract).
    """
    damage_dice, damage_pips = BOSS_DAMAGE
    skills, max_hp = fit_skills_to_cr(
        BOSS_SKILLS, target_cr, skills_catalog, damage_dice=damage_dice, damage_pips=damage_pips,
        offense_share=offense_share,
    )

    def factory():
        return [{
            "name": "boss",
            "skills": skills,
            "max_hp": max_hp,
            "damage_value": {"dice": damage_dice, "pips": damage_pips, "bonus": 0},
            "damage_tags": [],
        }]
    return factory


def _find_boss_crossover_multiplier(
    build_party, party_cr_total, skills_catalog, rules, offense_share,
    low=0.2, high=1.3, iterations=8, trials=TRIALS,
):
    """!
    @brief Bisects for the boss-CR-as-a-fraction-of-party-CR multiplier where a single boss's
        own win rate against the party crosses 50% -- the mirror image of _find_crossover's own
        "how many weak monsters" question, this time "how strong does ONE opponent need to be."
        Bisection (not a linear scan) since the boss side has a continuous free parameter
        (target_cr, any non-negative number) rather than an integer headcount, and the observed
        transition is sharp enough (94% -> 6% win rate across roughly 0.6x-0.8x in an earlier
        manual probe) that a handful of bisection steps easily narrows it to within a percent or
        two of party_cr_total.
    @param build_party Zero-arg factory for the party's own side (Combat_Simulator shape).
    @param party_cr_total The party's own summed challenge rating (the 1.0x reference point).
    @param skills_catalog/rules Forwarded to run_group_matchup/_build_boss.
    @param offense_share The boss's own build shape, forwarded to _build_boss.
    @param low/high The initial bracket, as a multiplier of party_cr_total -- low is assumed to
        still favor the party (win rate >= 50%), high to favor the boss (< 50%); an assumption
        this function does not itself verify (see main()'s own sanity framing).
    @param iterations How many bisection steps to run -- each one halves the bracket width, so 8
        steps narrows an initial [0.2, 1.3] bracket to within about 0.004x.
    @param trials Forwarded to run_group_matchup at each bisection step.
    @return The multiplier at the final bracket's own midpoint.
    """
    for _ in range(iterations):
        mid = (low + high) / 2
        target_cr = max(1, round(party_cr_total * mid))
        boss_factory = _build_boss(target_cr, skills_catalog, offense_share)
        result = run_group_matchup(build_party, boss_factory, rules, skills_catalog, trials=trials)
        if result["a_win_rate"] >= 0.5:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def run_swarm_phase(dm, party_configs, skills_catalog, rules):
    """!
    @brief Phase 1: many-weak-monsters-vs-party -- scans group size N per (party, monster CR)
        combination for the win-rate-50% crossover, then fits a two-parameter power law against
        every non-censored result (see this module's own docstring for the shape/reasoning).
    """
    # A spread from "clearly weaker than any party member" to "clearly stronger," with extra
    # density in the cr_ratio ~1.5-2.5 band where the first pass's fitted power law diverged
    # sharply from actual data (goblin scout/horse/spectral wolf fill the 24-32 CR gap between
    # giant rat's 20 and coyote's 31) -- giant rat itself is kept as a known-censored
    # illustration (too weak to ever cross over at any party size).
    monster_names = [
        "giant rat", "goblin scout", "horse", "coyote", "spectral wolf", "giant spider",
        "skeleton warrior", "troll", "wolf", "fire elemental", "wraith", "the bone warden",
    ]

    print("=== Phase 1: swarm of weak monsters vs. party ===")
    print(f"{'party':>22} | {'monster':>16} | {'monster_cr':>10} | {'cr_ratio':>9} | {'crossover N':>12} | {'lanchester N':>13} | {'plain-sum N':>12}")
    print("-" * 106)

    data_points = []
    for party_label, (party_members, party_count) in party_configs.items():
        party_cr_total = sum(dm.get_challenge_rating(base_name) for base_name, _ in party_members)
        party_avg_cr = party_cr_total / party_count

        def build_party(members=party_members):
            return [_snapshot(dm, base_name, suffix=suffix) for base_name, suffix in members]

        for monster_name in monster_names:
            monster_cr = dm.get_challenge_rating(monster_name)
            if monster_cr <= 0:
                continue
            cr_ratio = party_avg_cr / monster_cr

            def build_monster(index, monster_name=monster_name):
                return _snapshot(dm, monster_name, suffix=f"_{index}")

            crossover = _find_crossover(build_party, build_monster, rules, skills_catalog)
            lanchester_n = party_count * math.sqrt(cr_ratio)
            plain_sum_n = party_count * cr_ratio

            crossover_label = f"{crossover:.1f}" if crossover is not None else f">{MAX_N}"
            print(
                f"{party_label:>22} | {monster_name:>16} | {monster_cr:>10} | {cr_ratio:>9.2f} | "
                f"{crossover_label:>12} | {lanchester_n:>13.1f} | {plain_sum_n:>12.1f}"
            )
            if crossover is not None:
                data_points.append((cr_ratio, crossover, party_count))

    print()
    if len(data_points) < 3:
        print("Too few non-censored crossovers to fit a curve.")
        return

    fitted_a, fitted_p, r_squared = _fit_power_law(data_points)
    print(f"Fitted power law: N_crossover / party_count = {fitted_a:.2f} * (party_avg_cr / monster_cr) ^ {fitted_p:.2f}")
    print(f"R^2 = {r_squared:.3f} across {len(data_points)} non-censored data points")
    print(f"(Lanchester's own square law: A=1.00, p=0.50; plain-sum model: A=1.00, p=1.00)")

    print()
    print("Fitted curve vs. actual, per data point:")
    for cr_ratio, actual_n, party_count in sorted(data_points):
        predicted = party_count * fitted_a * cr_ratio ** fitted_p
        print(f"  cr_ratio={cr_ratio:5.2f} party_count={party_count} actual_n={actual_n:5.1f} fitted_n={predicted:5.1f}")


def run_boss_phase(dm, party_configs, skills_catalog, rules):
    """!
    @brief Phase 2: one strong boss vs. party -- the mirror image of run_swarm_phase's own
        question. For each party config and boss build shape (offense_share -- 0.3 skews the
        boss toward survival_side/HP, "tanky"; 0.7 toward offense_side/damage, "glass cannon"),
        bisects for the boss CR (as a fraction of the party's own summed CR) where the party's
        win rate crosses 50%. A manual probe (gladstone+thane, balanced boss) found this
        crossover around 0.70x party CR with a sharp transition (94% -> 6% win rate between
        0.6x-0.8x) -- dramatically less than the "1.0x should be an even fight" a plain sum
        implies, the opposite-direction counterpart to run_swarm_phase's own "needs 1.5-2.5x"
        finding for many weak monsters.
    """
    print()
    print("=== Phase 2: single boss vs. party ===")
    print(f"{'party':>22} | {'boss offense_share':>19} | {'crossover (x party CR)':>23}")
    print("-" * 72)

    boss_shapes = {"tanky (0.3)": 0.3, "balanced (0.5)": 0.5, "glass cannon (0.7)": 0.7}
    results = []
    for party_label, (party_members, party_count) in party_configs.items():
        party_cr_total = sum(dm.get_challenge_rating(base_name) for base_name, _ in party_members)

        def build_party(members=party_members):
            return [_snapshot(dm, base_name, suffix=suffix) for base_name, suffix in members]

        for shape_label, offense_share in boss_shapes.items():
            crossover_multiplier = _find_boss_crossover_multiplier(
                build_party, party_cr_total, skills_catalog, rules, offense_share,
            )
            print(f"{party_label:>22} | {shape_label:>19} | {crossover_multiplier:>22.2f}x")
            results.append((party_label, party_count, shape_label, crossover_multiplier))

    print()
    by_shape = {}
    for _, _, shape_label, crossover_multiplier in results:
        by_shape.setdefault(shape_label, []).append(crossover_multiplier)
    print("Average crossover multiplier per boss shape (across all party sizes):")
    for shape_label, multipliers in by_shape.items():
        print(f"  {shape_label:>19}: {sum(multipliers) / len(multipliers):.2f}x (range {min(multipliers):.2f}x-{max(multipliers):.2f}x)")


def main():
    dm = DMCore(EventBus(), scenario_name="debug", start_location="arena_grounds")
    skills_catalog, rules = dm.skills, dm.rules

    # Each party member is (base_entity_name, name_suffix) -- debug.toml only ships gladstone/
    # thane, so the 3-person config reuses thane twice under distinct suffixes (a real entity's
    # real stats, just duplicated, the same way the monster side already reuses one creature
    # template N times) rather than fabricating a synthetic third character.
    party_configs = {
        "gladstone_solo": ([("gladstone", "")], 1),
        "gladstone_thane": ([("gladstone", ""), ("thane", "")], 2),
        "gladstone_thane_thane2": ([("gladstone", ""), ("thane", "_a"), ("thane", "_b")], 3),
    }

    run_swarm_phase(dm, party_configs, skills_catalog, rules)
    run_boss_phase(dm, party_configs, skills_catalog, rules)


if __name__ == "__main__":
    main()
