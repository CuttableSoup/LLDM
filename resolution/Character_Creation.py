"""!
@file Character_Creation.py
@brief Pure, UI-agnostic character-creation logic: race/skill data loaded independently of
    DMCore (so a character can be built *before* any DMCore/scenario exists -- mirrors
    LLMCore's own independently-computed save path, CLAUDE.md's "Saving and loading"), plus
    the point-buy math itself (baseline dice, allocation validation, final skills). Nothing
    here touches Tkinter or an EventBus; Character_Creation_GUI.py is the interactive dialog
    built on top of it, and DM_CharacterCreation.py is the DMCore mixin that bakes a finished
    result into the actual player entity.
"""

import os
import tomllib

from paths import PROJECT_ROOT

DEFAULT_CHARACTER_CREATION = {"pool_dice": 15, "max_allocation_per_skill": 5}
# What race_baseline_skills falls back to for a skill a race's own [race.skill_dice] table
# doesn't cover (every shipped race in races.toml lists all of them; this only matters for a
# malformed/incomplete race, or race=None -- ex: an unrecognized race name) -- 0D, the same
# "untrained" convention DM_Combat.py's own resolve_action/roll_initiative use for an entity's
# own missing skill. Kept as its own constant here (not imported from DM_Combat.py) since
# this whole module has to stay importable with no DMCore/DM_Combat.py in the picture at all
# (see this file's own module docstring).
UNTRAINED_DICE = 0


def load_character_creation_data(rules_dir=os.path.join("Rules", "Fantasy")):
    """!
    @brief Scans every *.toml directly under rules_dir for "skill"/"race"/"character_creation"
        keys -- the same generic per-file scan DM_Rules.py's load_rules does, duplicated here
        (not imported from DMCore) so this can run before any DMCore instance exists.
    @param rules_dir Path to the rules directory, relative to the project root.
    @return (skills, races, character_creation): skills is {name: skill_table}; races is a
            list of race tables; character_creation is DEFAULT_CHARACTER_CREATION overlaid
            with whatever rules.toml's own [character_creation] table declares.
    """
    full_dir = os.path.join(PROJECT_ROOT, rules_dir)

    skills = {}
    races = []
    character_creation = dict(DEFAULT_CHARACTER_CREATION)

    if not os.path.exists(full_dir):
        return skills, races, character_creation

    for filename in os.listdir(full_dir):
        if not filename.endswith(".toml"):
            continue
        filepath = os.path.join(full_dir, filename)
        try:
            with open(filepath, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            continue
        for skill in data.get("skill", []):
            skills[skill.get("name")] = skill
        races.extend(data.get("race", []))
        if "character_creation" in data:
            character_creation.update(data["character_creation"])

    return skills, races, character_creation


def get_race(races, race_name):
    """!
    @brief Finds a race table by name.
    @param races The list of race tables (see load_character_creation_data).
    @param race_name The race's own "name" field to match.
    @return The matching race table, or None.
    """
    for race in races:
        if race.get("name") == race_name:
            return race
    return None


def race_baseline_skills(skills, race):
    """!
    @brief Every skill's starting dice before point-buy allocation: race's own
        [race.skill_dice] value for that skill (an absolute dice count, not a delta -- ex:
        elf's own "arcane = 3" means literally 3D, full stop). Every race in races.toml,
        human included, lists every skill explicitly -- there is no separate "base_dice"
        constant anything falls back to by default. A skill genuinely absent from a race's
        own table (malformed/incomplete data, not a case races.toml's own shipped data relies
        on) falls back to UNTRAINED_DICE (0). Floored at 0 regardless of source -- a
        miskeyed/negative value in race data can't push a skill's dice pool negative (roll_dice
        would just clamp that to 0 anyway), but 0 itself is a legitimate, intentional baseline
        here, not something this floors back up to 1D.
    @param skills {name: skill_table}, from load_character_creation_data.
    @param race A race table (see get_race), or None (treated as declaring no skills at all --
        every skill falls back to UNTRAINED_DICE).
    @return {skill_name: baseline_dice_int}, one entry per skill.
    """
    values = (race or {}).get("skill_dice", {})
    return {name: max(0, values.get(name, UNTRAINED_DICE)) for name in skills}


def validate_allocation(skills, race, character_creation, allocation):
    """!
    @brief Checks a proposed point-buy allocation against the shared character-creation
        constants: every key must name a real skill, no single skill may receive more than
        max_allocation_per_skill, no entry may be negative, and the total spent must equal
        pool_dice exactly (no banking unspent dice, no overspending).
    @param skills {name: skill_table}, from load_character_creation_data.
    @param race A race table (see get_race), or None -- unused here directly, accepted only
        so callers can pass the same shape of arguments through to build_character_skills
        once validation passes.
    @param character_creation The point-buy constants table.
    @param allocation {skill_name: dice_int}, the player's proposed spend.
    @return (True, None) if valid, else (False, reason_string).
    """
    pool_dice = character_creation.get("pool_dice", DEFAULT_CHARACTER_CREATION["pool_dice"])
    max_per_skill = character_creation.get(
        "max_allocation_per_skill", DEFAULT_CHARACTER_CREATION["max_allocation_per_skill"]
    )

    unknown = [name for name in allocation if name not in skills]
    if unknown:
        return False, f"Unknown skill(s): {', '.join(sorted(unknown))}"

    for name, value in allocation.items():
        if value < 0:
            return False, f"\"{name}\" cannot have a negative allocation"
        if value > max_per_skill:
            return False, f"\"{name}\" exceeds the max allocation of {max_per_skill}D"

    total = sum(allocation.values())
    if total != pool_dice:
        return False, f"Allocated {total}D, but exactly {pool_dice}D must be spent"

    return True, None


def load_player_starting_exp(rules_dir=os.path.join("Rules", "Fantasy")):
    """!
    @brief Finds the is_player = true [[entity]] template's own "exp" field -- the starting XP
        balance a character-creation-time training spend (see spend_exp_on_skills) draws from,
        before any DMCore/live player instance exists to read it off of. Same "has to be
        readable before a DMCore exists" reasoning load_character_creation_data's own module
        docstring gives for re-scanning Rules/<setting>/*.toml directly rather than asking a
        DMCore -- a second, deliberately separate scan (not folded into that function's own
        return tuple) so its existing 3-value return shape never changes for callers that don't
        care about this.
    @param rules_dir Path to the rules directory, relative to the project root.
    @return The is_player template's own "exp" (0 if it doesn't author one), or 0 if no
            is_player entity is found in rules_dir at all.
    """
    full_dir = os.path.join(PROJECT_ROOT, rules_dir)
    if not os.path.exists(full_dir):
        return 0

    for filename in os.listdir(full_dir):
        if not filename.endswith(".toml"):
            continue
        filepath = os.path.join(full_dir, filename)
        try:
            with open(filepath, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            continue
        for entity in data.get("entity", []):
            if entity.get("is_player"):
                return entity.get("exp", 0)
    return 0


def spend_pip(dice, pips, exp):
    """!
    @brief Raises one skill by a single pip, spending XP equal to its own *current* dice count
        (not counting pips) -- rolling over into an additional die once pips reach 3, the same
        "3 pips = 1 die" scale skill_rating (Challenge_Rating.py) already uses, so a trained-up
        skill lands on exactly the same dice/pips shape a real skill entry (entity_schema.toml's
        [entity.skills]) would ever author by hand. Callers spend one pip at a time (see
        spend_exp_on_skills) rather than a single batch computation, since the cost itself rises
        as dice increase -- there's no closed-form shortcut for "raise this skill by N pips".
    @param dice The skill's current dice count.
    @param pips The skill's current pips (0, 1, or 2 -- 3 always rolls over immediately, so a
        skill never actually holds pips = 3 at rest).
    @param exp The XP balance to spend from.
    @return (new_dice, new_pips, remaining_exp), or None if exp is less than dice (the cost) --
            the "can't afford it" case, left entirely unchanged for the caller to report.
    """
    cost = dice
    if exp < cost:
        return None
    pips += 1
    if pips == 3:
        dice += 1
        pips = 0
    return dice, pips, exp - cost


def spend_exp_on_skills(skills_dict, exp, pip_spend):
    """!
    @brief Applies an ordered list of "raise this skill by one more pip" requests on top of
        skills_dict, spending from exp one pip at a time via spend_pip -- the training
        counterpart to validate_allocation/build_character_skills' own point-buy math, replayed
        server-side from scratch rather than trusting a client-submitted final {dice, pips} per
        skill (same "recompute, don't trust the submitted totals" precedent validate_allocation
        already sets). All-or-nothing: the first unaffordable or unknown-skill entry rejects the
        *entire* spend and returns the original, completely untouched skills_dict/exp -- no
        partial application, same shape validate_allocation's own pool_dice mismatch rejection
        already has for point-buy.
    @param skills_dict {skill_name: {"dice": int, "pips": int}} -- ex: build_character_skills'
        own output, or a live player entity's own "skills" table. Never mutated -- a shallow
        per-entry copy is built and returned instead.
    @param exp The XP balance to spend from (ex: load_player_starting_exp's own return, or a
        live player entity's own "exp" field).
    @param pip_spend A list of skill names, in order -- each entry is one more "raise this skill
        by a pip" request, so ["blades", "blades", "dodge"] means blades twice then dodge once,
        each purchase's own cost based on whatever that skill's dice count is by that point in
        the replay (including any earlier entries in this same list that already rolled it over
        to a higher die count).
    @return (new_skills_dict, remaining_exp, error_reason_or_None) -- error_reason is a string
            (an unknown skill name, or which skill/at what dice cost ran out of XP) if the whole
            spend was rejected, in which case new_skills_dict/remaining_exp are skills_dict/exp
            completely unchanged; None on success.
    """
    result = {name: dict(entry) for name, entry in skills_dict.items()}
    remaining = exp
    for name in pip_spend:
        if name not in result:
            return skills_dict, exp, f"Unknown skill: {name}"
        entry = result[name]
        spent = spend_pip(entry["dice"], entry["pips"], remaining)
        if spent is None:
            return skills_dict, exp, f"Not enough XP to raise \"{name}\" (needs {entry['dice']}, have {remaining})"
        entry["dice"], entry["pips"], remaining = spent
    return result, remaining, None


def build_character_skills(skills, race, allocation):
    """!
    @brief The final {dice, pips} per skill for a newly-created character: race_baseline_skills
        plus whatever the player allocated on top, for every skill (not just ones the player
        put dice into). Callers should validate_allocation first -- this doesn't itself check
        the allocation is legal, just adds it.
    @param skills {name: skill_table}, from load_character_creation_data.
    @param race A race table (see get_race), or None.
    @param allocation {skill_name: dice_int}, the player's own spend (already validated).
    @return {skill_name: {"dice": int, "pips": 0}}, one entry per skill.
    """
    baseline = race_baseline_skills(skills, race)
    return {
        name: {"dice": baseline[name] + allocation.get(name, 0), "pips": 0}
        for name in skills
    }
