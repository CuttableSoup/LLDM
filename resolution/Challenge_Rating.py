"""!
@brief Pure "how powerful is this entity" math -- no DMCore/Tkinter/EventBus dependency, the
    same "pure, entity-shape-agnostic" precedent Character_Creation.py sets, since a challenge
    rating needs to be computable from plain skills/hp/damage numbers regardless of where they
    came from (a live DMCore entity today; a hypothetical encounter generator's own draft data
    tomorrow -- see CLAUDE.md's "Extended goals"). DM_Combat.py's get_challenge_rating/
    get_party_challenge_rating are the DMCore-touching glue that pulls those numbers off a
    live entity and calls into this module, the same split Character_Creation.py/
    DM_CharacterCreation.py already use for character creation.

    calculate_challenge_rating combines two sides -- offense_side ("how fast can this entity
    kill you") and survival_side ("how long does it last") -- by twice their geometric mean
    rather than a flat sum, specifically because a Monte Carlo combat simulator (resolution/
    Combat_Simulator.py, driven by scripts/calibrate_challenge_rating.py) measured that a flat
    sum is NOT shape-invariant: three creatures built to the identical additive CR total, but
    spending it differently (offense/damage-heavy vs. HP-heavy vs. balanced), won at wildly
    different rates against a fixed reference PC -- a balanced build beat both extremes by a
    wide, tuning-constant-independent margin, because win probability in a race-to-zero-HP fight
    behaves like a product of "time to kill" and "time to survive," not a sum of independently
    priced stats. 2*sqrt(A*B) <= A+B always, with equality only at A == B (AM-GM) -- so a
    balanced build keeps its old additive-scale CR exactly, while a lopsided build's CR is
    pulled toward 0 unless it's genuinely stronger overall to compensate for the skew. See
    calculate_challenge_rating's own docstring for the exact split.
"""

import math

# Every 3 pips converts to a die (the same scale DM_Combat.py's get_opposing_skill/
# select_ability_skill already rate skills on) -- skill_rating is the one place that
# convention is spelled out, so nothing else needs to hardcode "* 3" separately.
SKILL_RATING_DIVISOR = 3


def skill_rating(dice, pips):
    """!
    @brief Converts a {dice, pips} pair onto a single comparable scale, in pip units. The
        shared building block behind get_opposing_skill/select_ability_skill's own skill
        comparisons (DM_Combat.py) and this module's own challenge rating -- one definition
        of "how good is a dice+pips rating" the whole engine agrees on.
    @param dice The number of dice.
    @param pips The flat pip bonus (0-2 in practice; not normalized here).
    @return dice * SKILL_RATING_DIVISOR + pips.
    """
    return dice * SKILL_RATING_DIVISOR + pips


# A separate, HP-specific conversion rate -- NOT the same as SKILL_RATING_DIVISOR, even though
# it used to just reuse that constant. Raw max_hp isn't a {dice, pips} pair with the same
# real-world ceiling a D6 pool has (skills cap around 5-8D in practice; max_hp can be hand-
# authored arbitrarily high), so pricing it on the same "/3" scale as pips-to-dice let a handful
# of extra HP swamp a whole extra die of training within survival_side. This only ever scales
# HP's own weight relative to defense/save/immunity -- the OTHER components it's summed with
# inside survival_side (still a flat sum among themselves; only the top-level offense_side/
# survival_side combination is multiplicative, see this module's own docstring). A first
# calibration pass tried fixing "HP drowns everything" purely by grid-searching this constant
# and found it couldn't: shape-invariance across offense/damage-heavy vs. HP-heavy builds turned
# out to need the multiplicative combination above regardless of what this divisor is set to.
# Left at the engine's original implicit value (3, matching SKILL_RATING_DIVISOR) -- re-run
# scripts/calibrate_challenge_rating.py against the new formula if this ever needs revisiting;
# the current script's argument is that this specific number stopped being the binding
# constraint once offense_side/survival_side combine multiplicatively instead of additively.
DEFAULT_HP_DIVISOR = 3


def calculate_challenge_rating(
    offense_skill_rating, damage_dice, damage_pips, defense_skill_rating, save_ratings, max_hp,
    hp_divisor=DEFAULT_HP_DIVISOR,
):
    """!
    @brief A single number describing how powerful an entity is, from its own dice/pips --
        two sides, each a sum of independently meaningful components on the same pip-unit scale
        skill_rating establishes, combined by twice their geometric mean rather than a flat sum
        (see this module's own docstring for why). Every input here is already resolved by the
        caller (ex: DM_Combat.py's get_challenge_rating, which knows how to pull these off a
        live entity and its setting's own skills.toml) -- this function itself stays pure
        arithmetic:
          - offense_side: skill_rating(offense_skill_rating's own dice/pips) + skill_rating(
            damage_dice, damage_pips) -- the entity's single best attack (weapon or ability),
            its own to-hit skill and its own damage paired together rather than mixed
            independently from two different candidates (see DM_Combat.py's
            _best_offense_package). "How fast this entity can kill you."
          - survival_side -- "how long this entity lasts" -- sums:
            - defense: the entity's own rating in whichever skill(s) a setting's skills.toml
              tags combat_role = "defense" (Pathfinder/Fantasy/Zombie all use exactly one --
              dodge, the skill nearly every physical attack skill's own "opposes" list leads
              with). Counted at full value, not diluted into an average with unrelated skills.
            - save: the average rating across whichever skill(s) are tagged combat_role =
              "resistive" (fortitude/reflexes/willpower -- Pathfinder's own three saves) -- a
              distinct axis from offense/defense: how hard this entity is to lock down with a
              save-or-suck spell/condition, regardless of how it fares in a straight exchange.
            - hp: max_hp // hp_divisor -- a flat stat (HP has no dice of its own) still needs
              converting into the same comparable pip units, but via its own divisor
              (DEFAULT_HP_DIVISOR), not SKILL_RATING_DIVISOR reused -- see that constant's own
              module comment.
          resistance_value/vulnerability_value/immunity_tags are deliberately NOT read here
          (dropped from an earlier version of this formula) -- all three are tag-scoped: they
          only ever matter against an incoming hit whose own damage_tags happens to match, which
          no particular encounter is guaranteed to bring. Baking any of them into every entity's
          own baseline rating overstated a defense (or, for vulnerability, a weakness) that may
          simply never come up.
          - CR = round(2 * sqrt(offense_side * survival_side)). An entity with offense_side == 0
            (no offense-tagged skill trained AND no damage-dealing weapon/ability at all) always
            computes CR = 0, regardless of survival_side -- a creature that can never deal damage
            poses no combat danger, by construction, no matter how much HP/defense it has. This
            is a deliberate consequence of the multiplicative combination, not an oversight --
            see the "Experience (XP)" doc section's existing caveat that a trap's own CR is
            already a poor stand-in for its real danger (no offense-dealing ability the usual
            way), which every shipped trap already works around with an explicit "exp" field;
            this formula change is consistent with, not contrary to, that existing caveat.
    @param offense_skill_rating The dice/pips of the skill used by the entity's single best
        attack (weapon or ability) -- a {"dice", "pips"} table, or None/{} if it has no
        attack at all (a pure support entity is still ratable, at 0 -- see offense_side == 0
        above).
    @param damage_dice/damage_pips The dice/pips of that same best attack's own damage_value.
    @param defense_skill_rating The entity's own {"dice", "pips"} in its setting's combat_role
        = "defense" skill (None/{} if untrained or the setting authors no such skill at all).
    @param save_ratings A list of the entity's own {"dice", "pips"} tables, one per
        combat_role = "resistive" skill the setting authors (empty if none).
    @param max_hp The entity's max_hp.
    @param hp_divisor Converts max_hp into CR "pips" within survival_side -- see
        DEFAULT_HP_DIVISOR's own module comment.
    @return The entity's challenge rating (an int).
    """
    def _rating(stats):
        return skill_rating((stats or {}).get("dice", 0), (stats or {}).get("pips", 0))

    offense_side = _rating(offense_skill_rating) + skill_rating(damage_dice, damage_pips)

    defense_component = _rating(defense_skill_rating)
    save_component = round(sum(_rating(stats) for stats in save_ratings) / len(save_ratings)) if save_ratings else 0
    hp_component = max_hp // hp_divisor

    survival_side = defense_component + save_component + hp_component

    return round(2 * math.sqrt(offense_side * survival_side))


def calculate_party_challenge_rating(member_ratings):
    """!
    @brief A party's own challenge rating: the plain sum of every member's own
        calculate_challenge_rating -- total party strength, not a per-member average, so a
        larger party of individually modest ratings can still outrate a single strong boss.
    @param member_ratings Each party member's own challenge rating (int), already computed.
    @return The sum.
    """
    return sum(member_ratings)
