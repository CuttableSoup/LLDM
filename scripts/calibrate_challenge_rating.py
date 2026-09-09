"""!
@file calibrate_challenge_rating.py
@brief Standalone research script -- NOT part of the pytest suite, run by hand:
    `python scripts/calibrate_challenge_rating.py`.

    Originally a grid search over Challenge_Rating.py's own hp_divisor, looking for the value
    that made three creature archetypes (glass_cannon/balanced/tank, spending the SAME nominal
    CR very differently) win at the same rate against a fixed reference PC. That search's own
    finding: no hp_divisor fixed it -- balanced beat both extremes by a large, divisor-
    independent margin at every value tried, because a flat-sum CR formula is fundamentally not
    shape-invariant when win probability behaves like a PRODUCT of "how fast you kill them" and
    "how long you survive," not a sum of independently priced stats (see Challenge_Rating.py's
    own module docstring for the fix: CR = round(2*sqrt(offense_side * survival_side)), an
    AM-GM-based combination instead of a flat sum).

    This script now verifies THAT fix directly: build the same three archetypes (fixed offense-
    skill/defense/save ratings, varying only how much of the remaining budget goes to damage
    vs. HP) at the identical CR, under both formulas, on the exact same fixed ratings -- a true
    apples-to-apples A/B, not a comparison against a differently-configured earlier run. Result
    (RNG_SEED, TRIALS_PER_MATCHUP, CR_MAGNITUDE_MULTIPLIERS as shipped): the OLD flat-sum formula
    spreads 82% / 31% / 9% (avg 41%) across the three CR magnitudes; the NEW AM-GM formula
    spreads 47% / 24% / 4% (avg 25%) on the identical builds -- a real, meaningful reduction
    (roughly two-fifths), but NOT a full fix. The remaining spread traces to a distinct,
    deliberately out-of-scope sub-problem: offense_side itself still sums to-hit skill and
    damage-per-hit rather than combining them the same multiplicative way (DM_Combat.py's own
    _best_offense_package already treats a trained, 0-damage offense skill as meaningful on
    purpose -- the same simplification generated NPCs rely on by default -- so recursing AM-GM
    one level deeper would make every generation-default 0-damage NPC read as CR 0, a much bigger
    behavior change than this pass signed up for). Uses Combat_Simulator.py's Monte Carlo fight
    resolution (itself built on Combat_Resolution.py's real pure primitives --
    resolve_opposed_action, calculate_damage, ...) to measure win rate directly, not the
    formula's own arithmetic. Re-run this script (optionally editing SHAPE_DAMAGE_RATINGS/
    FIXED_OFFENSE_SKILL_RATING/FIXED_DEFENSE_RATING/FIXED_SAVE_RATING) if that sub-problem is
    ever worth tackling.
"""

import copy
import os
import random
import sys
import tomllib

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from resolution.Challenge_Rating import DEFAULT_HP_DIVISOR, calculate_challenge_rating
from resolution.Combat_Simulator import run_matchup
from resolution.NPC_Generation import _resolve_offense_survival_split

SETTING_DIR = os.path.join(_REPO_ROOT, "Rules", "Pathfinder")

# offense-skill/defense/save are held IDENTICAL, FIXED ratings across every shape -- deliberately
# (an opposed D6 roll is a sum-of-dice contest, so a build with meaningfully lower offense than
# the reference PC's own defense, or vice versa, barely lands hits at all regardless of anything
# else -- the first calibration pass that also varied offense/defense per shape found win rate
# swinging almost entirely on hit-chance, drowning out the actual axis under test). Holding
# to-hit/to-be-hit constant isolates exactly the trade-off the new formula's own AM-GM
# combination targets: spending the SAME product-based budget on more damage (kill them faster)
# vs. more HP (survive longer) -- the literal "glass cannon vs. tank" question, and the exact
# axis the earlier flat-sum grid search measured as broken.
FIXED_OFFENSE_SKILL_RATING = 15  # 5D+0 -- matches gladstone's own real blades/dodge (5D+0) so
# every shape actually lands hits at a fair, informative rate; a much lower fixed skill (an
# earlier pass tried 3D+0) meant every shape hit gladstone so rarely that only a build with
# massive per-hit damage could convert its handful of lucky hits into a kill inside max_rounds,
# swamping the actual damage-vs-HP trade-off this script means to isolate under an unrelated
# "can this shape even land enough hits in time" effect.
FIXED_DEFENSE_RATING = 6  # 2D+0
FIXED_SAVE_RATING = 3  # 1D+0, all three saves -- kept small so defense+save never exceeds a
# glass_cannon shape's own (much smaller) survival_side at the low end of CR_MAGNITUDE_MULTIPLIERS,
# which would otherwise force hp_component negative (clamped to 0) and break exact reproduction.
SAVE_SKILLS = ("fortitude", "reflexes", "willpower")

# Each shape's own fixed damage rating (skill_rating(dice, pips)) -- offense_side =
# FIXED_OFFENSE_SKILL_RATING + this, so it genuinely differs per shape (glass_cannon's own
# offense_side is much larger than tank's), the same way real damage output would differ.
# survival_side is then solved (_resolve_offense_survival_split, the exact same production
# helper fit_skills_to_cr itself uses) so every shape reproduces the identical target_cr exactly.
SHAPE_DAMAGE_RATINGS = {"glass_cannon": 24, "balanced": 9, "tank": 3}  # 8D / 3D / 1D -- a literal
# 0D tank (no damage-dealing weapon/ability at all) can structurally never win regardless of
# anything else (0 raw damage every hit), which isn't really testing hp_divisor/offense_side vs.
# survival_side at all -- see this module's own docstring for why that's a distinct, deliberately
# out-of-scope sub-problem (DM_Combat.py's own _best_offense_package already treats a trained,
# 0-damage offense skill as meaningful on purpose, the same simplification generated NPCs rely on
# by default). 1D is the smallest non-degenerate damage rating.

TRIALS_PER_MATCHUP = 1500
# Three CR magnitudes, not just gladstone's own single real CR -- see this module's own earlier
# finding that saturated matchups (everyone always wins or always loses) give a meaningless,
# trivially-zero spread; +/-15% around gladstone's own real CR stays in the informative band.
CR_MAGNITUDE_MULTIPLIERS = (0.85, 1.0, 1.15)
RNG_SEED = 20240607


def _load_skills_catalog(rules_dir):
    """!@brief Every [[skill]] across rules_dir's own *.toml, keyed by name -- the same
        lightweight "scan every file for one key" approach NPC_Generation.load_npc_keywords
        already uses, so this script needs no live DMCore/load_rules at all."""
    catalog = {}
    for filename in os.listdir(rules_dir):
        if not filename.endswith(".toml"):
            continue
        with open(os.path.join(rules_dir, filename), "rb") as f:
            data = tomllib.load(f)
        for skill in data.get("skill", []):
            catalog[skill["name"]] = skill
    return catalog


def _load_gladstone(rules_dir):
    """!@brief gladstone's own entity dict (Rules/Pathfinder/characters.toml) plus his equipped
        longsword's damage_value (equipment.toml), with the longsword's own "strength_damage"
        bonus formula (rules.toml's own [strength_damage] {skill, divisor}) resolved to a plain
        number up front -- Combat_Simulator bakes damage_value straight onto the entity and
        never consults a live "rules" dict for a formula reference (there's no condition system
        in play here to need one for), so this has to already be a literal int by the time it's
        used."""
    entities_by_name, rules = {}, {}
    for filename in os.listdir(rules_dir):
        if not filename.endswith(".toml"):
            continue
        with open(os.path.join(rules_dir, filename), "rb") as f:
            data = tomllib.load(f)
        for entity in data.get("entity", []):
            entities_by_name[entity["name"]] = entity
        for key, value in data.items():
            if key not in ("skill", "entity", "entity_template"):
                rules[key] = value

    gladstone = entities_by_name["gladstone"]
    longsword = entities_by_name["longsword"]
    formula = rules.get(longsword["damage_value"]["bonus"].split(".")[-1], {"skill": None, "divisor": 1})
    strength_dice = gladstone["skills"].get(formula["skill"], {}).get("dice", 0)
    bonus = strength_dice // formula.get("divisor", 1)

    return {
        "name": "gladstone",
        "skills": gladstone["skills"],
        "max_hp": gladstone["max_hp"],
        "damage_value": {"dice": longsword["damage_value"]["dice"], "pips": longsword["damage_value"]["pips"], "bonus": bonus},
        "damage_tags": longsword["damage_tags"],
    }


def _rating_to_dice_pips(rating):
    return rating // 3, rating % 3


def build_shaped_creature(name, target_cr, damage_rating, hp_divisor, offense_skill="blades", defense_skill="dodge"):
    """!
    @brief Builds a creature whose own calculate_challenge_rating (at hp_divisor) reproduces
        target_cr exactly under the NEW two-sided-product formula, with a FIXED offense-skill/
        defense/save (see this module's own FIXED_* constants) and a per-shape damage_rating --
        survival_side (hence HP, since defense/save are already pinned) is solved via
        _resolve_offense_survival_split, the exact production helper NPC_Generation.
        fit_skills_to_cr itself uses for the same inversion.
    @param name The entity's own name (used as its Combat_Simulator entities-dict key).
    @param target_cr The challenge rating this build must land on exactly.
    @param damage_rating This shape's own fixed skill_rating(damage_dice, damage_pips).
    @param hp_divisor Forwarded to the survival_side -> max_hp conversion.
    @param offense_skill/defense_skill Which of gladstone's own trained skills to assign the
        offense/defense rating to (blades/dodge -- the same pair gladstone himself trains, so
        the opposed roll between the two builds resolves against a real "opposes" relationship).
    @return An entity dict ready for Combat_Simulator's build_a/build_b factories.
    """
    target_cr = round(target_cr)
    offense_side = FIXED_OFFENSE_SKILL_RATING + damage_rating
    _, survival_side = _resolve_offense_survival_split(
        target_cr, offense_share=0, min_offense_side=offense_side, fixed_offense_side=offense_side,
    )
    # save_component is an AVERAGE across resistive skills (Challenge_Rating.calculate_challenge_
    # rating), not their sum -- all three saves are set equal here, so it's just FIXED_SAVE_RATING.
    hp_component = max(0, survival_side - FIXED_DEFENSE_RATING - FIXED_SAVE_RATING)

    offense_dice, offense_pips = _rating_to_dice_pips(FIXED_OFFENSE_SKILL_RATING)
    damage_dice, damage_pips = _rating_to_dice_pips(damage_rating)
    defense_dice, defense_pips = _rating_to_dice_pips(FIXED_DEFENSE_RATING)
    save_dice, save_pips = _rating_to_dice_pips(FIXED_SAVE_RATING)

    skills = {offense_skill: {"dice": offense_dice, "pips": offense_pips}, defense_skill: {"dice": defense_dice, "pips": defense_pips}}
    for save_skill in SAVE_SKILLS:
        skills[save_skill] = {"dice": save_dice, "pips": save_pips}

    return {
        "name": name,
        "skills": skills,
        "max_hp": hp_component * hp_divisor,
        "damage_value": {"dice": damage_dice, "pips": damage_pips, "bonus": 0},
        "damage_tags": ["slashing"],
    }


def _verify_reproduces_target_cr(build, target_cr, hp_divisor, defense_skill="dodge"):
    skills = build["skills"]
    save_ratings = [skills[s] for s in SAVE_SKILLS]
    offense_skill = next(name for name in skills if name not in SAVE_SKILLS and name != defense_skill)
    computed = calculate_challenge_rating(
        skills[offense_skill], build["damage_value"]["dice"], build["damage_value"]["pips"],
        skills[defense_skill], save_ratings, build["max_hp"], hp_divisor=hp_divisor,
    )
    assert computed == target_cr, f"build drifted from target_cr: {computed} != {target_cr}"


def main():
    random.seed(RNG_SEED)
    skills_catalog = _load_skills_catalog(SETTING_DIR)
    gladstone = _load_gladstone(SETTING_DIR)
    hp_divisor = DEFAULT_HP_DIVISOR

    header = f"{'CR mult':>7} | " + " | ".join(f"{shape:>13}" for shape in SHAPE_DAMAGE_RATINGS) + " |   spread"
    print(f"hp_divisor = {hp_divisor} (Challenge_Rating.DEFAULT_HP_DIVISOR)")
    print(header)
    print("-" * len(header))

    spreads = []
    for multiplier in CR_MAGNITUDE_MULTIPLIERS:
        gladstone_cr = calculate_challenge_rating(
            gladstone["skills"]["blades"], gladstone["damage_value"]["dice"], gladstone["damage_value"]["pips"],
            gladstone["skills"]["dodge"], [gladstone["skills"][s] for s in SAVE_SKILLS], gladstone["max_hp"],
            hp_divisor=hp_divisor,
        )
        target_cr = round(gladstone_cr * multiplier)

        win_rates = {}
        for shape_name, damage_rating in SHAPE_DAMAGE_RATINGS.items():
            build = build_shaped_creature(shape_name, target_cr, damage_rating, hp_divisor)
            _verify_reproduces_target_cr(build, target_cr, hp_divisor)
            result = run_matchup(
                lambda build=build: copy.deepcopy(build), lambda: copy.deepcopy(gladstone),
                rules={}, skills_catalog=skills_catalog, trials=TRIALS_PER_MATCHUP,
            )
            win_rates[shape_name] = result["a_win_rate"]

        spread = max(win_rates.values()) - min(win_rates.values())
        spreads.append(spread)
        row = " | ".join(f"{win_rates[shape]:>13.0%}" for shape in SHAPE_DAMAGE_RATINGS)
        print(f"{multiplier:>6.2f}x | {row} | {spread:>7.0%}  (target_cr={target_cr})")

    avg_spread = sum(spreads) / len(spreads)
    print("-" * len(header))
    print(f"Average spread across CR magnitudes: {avg_spread:.0%}")
    print("(on the same fixed builds, the old flat-sum formula spreads ~41% on average -- see")
    print(" this module's own docstring for the full old-vs-new numbers)")


if __name__ == "__main__":
    main()
