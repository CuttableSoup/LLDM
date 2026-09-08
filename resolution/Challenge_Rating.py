"""!
@brief Pure "how powerful is this entity" math -- no DMCore/Tkinter/EventBus dependency, the
    same "pure, entity-shape-agnostic" precedent Character_Creation.py sets, since a challenge
    rating needs to be computable from plain skills/hp/damage numbers regardless of where they
    came from (a live DMCore entity today; a hypothetical encounter generator's own draft data
    tomorrow -- see CLAUDE.md's "Extended goals"). DM_Combat.py's get_challenge_rating/
    get_party_challenge_rating are the DMCore-touching glue that pulls those numbers off a
    live entity and calls into this module, the same split Character_Creation.py/
    DM_CharacterCreation.py already use for character creation.
"""

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


# Flat credits for immunity_tags -- a true immunity is a hard, unconditional block, not a
# rolled reduction like resistance_value, so it has no dice/pips of its own to convert with
# skill_rating -- these two constants are this module's own deliberately simple stand-in
# (the same "don't try to force an exact number" latitude reference/pathfinder_mapping.toml's
# own "Energy Resistance N"/"Vulnerability" rows already claim for their rolled equivalents,
# extended to immunity's all-or-nothing shape). IMMUNITY_PER_TAG_BONUS approximates one
# ordinary resistance_value die pool (2D+0p = 6) fully realized, per specific tag (ex: fire
# elemental's own immunity_tags = ["fire"]); IMMUNITY_ANY_BONUS is a much bigger flat jump for
# the reserved "any" wildcard (is_immune_to) -- immune to every damage_tags value that exists,
# a categorically different (and, among creatures rather than conjured hazard objects, rare)
# trait from resisting one specific damage type.
IMMUNITY_PER_TAG_BONUS = 6
IMMUNITY_ANY_BONUS = 30


def calculate_challenge_rating(
    offense_skill_rating, damage_dice, damage_pips, defense_skill_rating, save_ratings, max_hp,
    resistance_dice=0, resistance_pips=0, immunity_tags=None, vulnerability_dice=0, vulnerability_pips=0,
):
    """!
    @brief A single number describing how powerful an entity is, from its own dice/pips --
        summed from independently meaningful components, each on the same pip-unit scale
        skill_rating establishes. Every input here is already resolved by the caller (ex:
        DM_Combat.py's get_challenge_rating, which knows how to pull these off a live entity
        and its setting's own skills.toml) -- this function itself stays pure arithmetic:
          - offense: skill_rating(offense_skill_rating's own dice/pips) + skill_rating(damage_dice,
            damage_pips) -- the entity's single best attack (weapon or ability), its own
            to-hit skill and its own damage paired together rather than mixed independently
            from two different candidates (see DM_Combat.py's _best_offense_package).
          - defense: the entity's own rating in whichever skill(s) a setting's skills.toml
            tags combat_role = "defense" (Pathfinder/Fantasy/Zombie all use exactly one --
            dodge, the skill nearly every physical attack skill's own "opposes" list leads
            with). Counted at full value, not diluted into an average with unrelated skills.
          - save: the average rating across whichever skill(s) are tagged combat_role =
            "resistive" (fortitude/reflexes/willpower -- Pathfinder's own three saves) -- a
            distinct axis from offense/defense: how hard this entity is to lock down with a
            save-or-suck spell/condition, regardless of how it fares in a straight exchange.
          - hp: max_hp // SKILL_RATING_DIVISOR -- the same "/3" scale as pips-to-dice, so a
            flat stat (HP has no dice of its own) still lands in comparable units without
            needing a separately-justified weighting constant.
          - resistance/immunity/vulnerability: resistance_value's own rolled reduction adds
            directly (already dice/pips, the same scale as everything else); immunity_tags
            adds a flat credit per IMMUNITY_PER_TAG_BONUS/IMMUNITY_ANY_BONUS (see their own
            module comment); vulnerability_value SUBTRACTS -- a real exploitable weakness
            makes an entity easier to bring down, not harder, so it lowers CR rather than
            padding it the way a resistance would raise it.
    @param offense_skill_rating The dice/pips of the skill used by the entity's single best
        attack (weapon or ability) -- a {"dice", "pips"} table, or None/{} if it has no
        attack at all (a pure support entity is still ratable, at 0).
    @param damage_dice/damage_pips The dice/pips of that same best attack's own damage_value.
    @param defense_skill_rating The entity's own {"dice", "pips"} in its setting's combat_role
        = "defense" skill (None/{} if untrained or the setting authors no such skill at all).
    @param save_ratings A list of the entity's own {"dice", "pips"} tables, one per
        combat_role = "resistive" skill the setting authors (empty if none).
    @param max_hp The entity's max_hp.
    @param resistance_dice/resistance_pips The entity's own resistance_value dice/pips
        (0/0 if it has none at all -- not scoped to any particular resistance_tags match,
        the same "don't try to force an exact number" simplification the rolled reduction
        itself already accepts).
    @param immunity_tags The entity's own immunity_tags list (None/[] if it has none).
    @param vulnerability_dice/vulnerability_pips The entity's own vulnerability_value
        dice/pips (0/0 if it has none at all).
    @return The entity's challenge rating (an int).
    """
    def _rating(stats):
        return skill_rating((stats or {}).get("dice", 0), (stats or {}).get("pips", 0))

    offense_component = _rating(offense_skill_rating) + skill_rating(damage_dice, damage_pips)
    defense_component = _rating(defense_skill_rating)
    save_component = round(sum(_rating(stats) for stats in save_ratings) / len(save_ratings)) if save_ratings else 0
    hp_component = max_hp // SKILL_RATING_DIVISOR
    resistance_component = skill_rating(resistance_dice, resistance_pips)
    vulnerability_component = skill_rating(vulnerability_dice, vulnerability_pips)
    immunity_tags = immunity_tags or []
    if "any" in immunity_tags:
        immunity_component = IMMUNITY_ANY_BONUS
    else:
        immunity_component = IMMUNITY_PER_TAG_BONUS * len(immunity_tags)

    return (
        offense_component + defense_component + save_component + hp_component
        + resistance_component + immunity_component - vulnerability_component
    )


def calculate_party_challenge_rating(member_ratings):
    """!
    @brief A party's own challenge rating: the plain sum of every member's own
        calculate_challenge_rating -- total party strength, not a per-member average, so a
        larger party of individually modest ratings can still outrate a single strong boss.
    @param member_ratings Each party member's own challenge rating (int), already computed.
    @return The sum.
    """
    return sum(member_ratings)
