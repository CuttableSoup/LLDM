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
    @param rules_dir Path to the rules directory, relative to this file's own location.
    @return (skills, races, character_creation): skills is {name: skill_table}; races is a
            list of race tables; character_creation is DEFAULT_CHARACTER_CREATION overlaid
            with whatever rules.toml's own [character_creation] table declares.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    full_dir = os.path.join(base_dir, rules_dir)

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
