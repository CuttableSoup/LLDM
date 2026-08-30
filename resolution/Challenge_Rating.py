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


def calculate_challenge_rating(skills, max_hp, damage_dice=0, damage_pips=0, top_n=3):
    """!
    @brief A single number describing how powerful an entity is, from its own dice/pips --
        summed from three independently meaningful components, each on the same pip-unit
        scale skill_rating establishes:
          - skill: the average skill_rating of the entity's top_n best-trained skills, not
            every skill it has -- an entity trained broadly but shallowly across dozens of
            noncombat skills (ex: a player character with a full skill table) shouldn't
            outrank one with only a couple of genuinely sharp skills (ex: a boss creature
            authored with just 2-3 trained skills); a flat sum would do exactly that.
          - hp: max_hp // SKILL_RATING_DIVISOR -- the same "/3" scale as pips-to-dice, so a
            flat stat (HP has no dice of its own) still lands in comparable units without
            needing a separately-justified weighting constant.
          - damage: skill_rating(damage_dice, damage_pips) of the entity's single best
            damage-dealing weapon/ability -- its own dice/pips only, not the "bonus" field
            (which can be a rules.toml formula reference rather than a flat number, and
            isn't "dice and pips" in the first place).
    @param skills The entity's own {skill_name: {"dice", "pips"}} table (entity["skills"]).
    @param max_hp The entity's max_hp.
    @param damage_dice/damage_pips The dice/pips of the entity's best damage-dealing
        weapon/ability, already resolved by the caller (ex: DM_Combat.py's
        get_challenge_rating, which knows how to find one on a live entity) -- default 0/0,
        so a pure support character with no attack of its own is still ratable.
    @param top_n How many of the entity's best-trained skills to average. Defaults to 3.
    @return The entity's challenge rating (an int).
    """
    ratings = sorted(
        (skill_rating(stats.get("dice", 0), stats.get("pips", 0)) for stats in skills.values()),
        reverse=True,
    )[:top_n]
    skill_component = round(sum(ratings) / len(ratings)) if ratings else 0
    hp_component = max_hp // SKILL_RATING_DIVISOR
    damage_component = skill_rating(damage_dice, damage_pips)
    return skill_component + hp_component + damage_component


def calculate_party_challenge_rating(member_ratings):
    """!
    @brief A party's own challenge rating: the plain sum of every member's own
        calculate_challenge_rating -- total party strength, not a per-member average, so a
        larger party of individually modest ratings can still outrate a single strong boss.
    @param member_ratings Each party member's own challenge rating (int), already computed.
    @return The sum.
    """
    return sum(member_ratings)
