"""!
@file Action_Target.py
@brief Action target resolution (see CONTEXT.md): who a skill or ability action is aimed at,
    from NLPCore's best-guess name for the clause, the player's own wording and the scene.
    resolve_action_target is the whole interface -- it reads the scene through ActionTargetScene
    and returns an ActionTarget verdict, changing nothing; DMCore._on_turn_detected applies it
    (current_target, the "assaulted" attitude, the law's assault mark, the weak-match question).

    What it decides, in order: an explicit name is honored if it is a live, present creature the
    action may fall on (any creature for an attack, only a hostile one otherwise); two live
    instances of one template ("wolf"/"wolf_2") are told apart by "the second", "the other", "the
    wounded" and "the healthy"; an attack that named nobody and has no fair current target goes
    to whoever its own words describe, else the one person a gendered pronoun can only mean,
    else the conversation partner -- and a victim found that way (inferred, not named) on a weak
    skill match is returned as confirm_first rather than attacked.

    Neighbours it is not: Entity_Reference (what the text literally names -- used here for the
    literal match) and DMCore._get_target_name/_choose_combat_target (the scene's default
    target when the player named nothing and nothing is being attacked).
"""

import re
from dataclasses import dataclass

from resolution.Entity_Reference import first_named

# Multi-instance targeting: NLPCore's own map_to_target (NLP_Core.py) picks one specific live
# instance name by raw text similarity to that instance's own registered name/description
# phrases -- given two identically-templated creatures ("wolf"/"wolf_2"), it has no way to prefer
# one over the other just because the player said "the second wolf"/"the other wolf"/"the wounded
# wolf", since none of those qualifier words are part of any registered phrase. These are
# deliberately small, literal keyword sets (matching Intent_Classification.py's own
# TRAVEL_KEYWORDS/DIALOGUE_KEYWORDS convention) rather than a general sentiment/adjective system.
TARGET_ORDINAL_KEYWORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
TARGET_OTHER_KEYWORDS = ("other", "another")
TARGET_WOUNDED_KEYWORDS = ("wounded", "hurt", "injured")
TARGET_HEALTHY_KEYWORDS = ("healthy", "unhurt", "uninjured", "unharmed")
# qualities.gender values a gendered pronoun can point at.
FEMALE_GENDERS = frozenset({"female", "woman", "girl", "f"})
MALE_GENDERS = frozenset({"male", "man", "boy", "m"})
# The same 0.40 hp_per_remain cutoff statuses.toml's own "wounded" status tier -- and debug.toml's
# wolf retreat behavior -- already use elsewhere in this codebase (see CLAUDE.md's "Combat"),
# reused here rather than inventing a second threshold. A candidate has to actually cross this
# line before "wounded"/"healthy" is honored -- calling a room full of undamaged creatures
# "wounded" shouldn't silently redirect to whichever one merely has the least HP among equals.
TARGET_WOUNDED_HP_CUTOFF = 0.40

# How sure NLPCore's skill match has to be before an attack may land on someone not already
# hostile whom the input never named -- the victim only inferred, by pronoun or as the
# conversation partner. Above the ordinary 0.5 bar, since starting a fight can't be taken back.
# Found by playtest: "use the fire for dramatic effect" (fireball, 0.57) and a keyword-fallback
# psionics hit (0.30) on a remark both fell through to the conversation partner, set a market
# burning and killed two bystanders. Naming the victim is intent enough: "trip silas" (0.645)
# still lands. Below this the player is asked first ("Attack Elara? (yes/no)") rather than
# refused -- found by playtest: refusing turned "Fight me!" (0.58) into a dead end, while "let's
# see what that knife is good for" (0.66) cleared the old 0.65 bar and killed a bystander.
ASSAULT_CONFIRM_SCORE = 0.8


class ActionTargetScene:
    """!
    @brief The port resolve_action_target reads the running game through -- read-only. DMCore
        supplies DMCoreActionTargetScene (dm/DM_ActionTarget.py); a test supplies a fake.

        Attributes (read): entities (key -> entity dict), scenario_entities (who is present),
        player_name, current_target (the persisted combat target, or None), partner_key (the
        conversation partner's entity key, or None).
    """

    def hp(self, key):
        """!@return The entity's current HP; 0 or less is dead."""
        raise NotImplementedError

    def hp_fraction(self, key):
        """!@return The entity's remaining share of its max HP (0.0-1.0), or None if it has none."""
        raise NotImplementedError

    def is_hostile(self, key):
        """!@return Whether the entity is hostile toward the player."""
        raise NotImplementedError

    def is_party_member(self, key):
        raise NotImplementedError

    def is_hidden(self, key):
        raise NotImplementedError


@dataclass(frozen=True)
class ActionTarget:
    """!
    @brief The verdict. Nothing has been applied when this is returned.
    @param target Who the action rolls against, or None for nobody (an attack with no fair target).
    @param current_target What DMCore.current_target becomes: the redirect's landing, or -- when
        confirm_first -- what it already was.
    @param assaulting The action falls on someone not hostile to the player, who has not been
        fought yet; the caller applies "assaulted" and the law's assault mark once it rolls.
    @param inferred The victim was only inferred (a pronoun, or the conversation partner), not named.
    @param confirm_first An inferred victim on too weak a skill match (ASSAULT_CONFIRM_SCORE):
        ask the player instead of attacking.
    @param no_opponent An attack with nobody to hit.
    @param incidental A non-attack that fell on a bystander only because it is the default
        target, though the player named nobody (they were speaking to someone else).
    """

    target: str | None
    current_target: str | None
    assaulting: bool = False
    inferred: bool = False
    confirm_first: bool = False
    no_opponent: bool = False
    incidental: bool = False


def resolve_action_target(scene, explicit_target, input_text, is_attack, score=1.0):
    """!
    @brief Resolves who one skill/ability clause is aimed at.
    @param scene An ActionTargetScene.
    @param explicit_target NLPCore's best-guess entity name for the clause (map_to_target), or None.
    @param input_text The player's raw turn input.
    @param is_attack The action is a damaging ability, or one authored as an act of aggression
        (entity_schema.toml's "assault"): anyone may be attacked, not only the already-hostile.
    @param score NLPCore's skill-match score for the clause.
    @return An ActionTarget.
    """
    text = input_text or ""
    current, assaulting = _redirect(scene, explicit_target, text, scene.current_target, allow_non_hostile=is_attack)
    target = current
    inferred = False
    if is_attack and not assaulting and _is_bystander(scene, target):
        # An attack NLPCore matched no name for, with no fight on: someone present it describes
        # ("pin the merchant's feet" -- a narrated person's occupation is an alias), else whoever
        # a pronoun can only mean, else whoever the player is talking to ("my turn to hit you!"),
        # else nobody -- never silently a non-hostile creature left over as the current target (a
        # chest or trap there stays fair game: smashing one is fine).
        target = None
        literal = _literal_attack_target(scene, text)
        for candidate in (literal, _pronoun_attack_target(scene, text), scene.partner_key):
            if not candidate:
                continue
            before = current
            current, assaulting = _redirect(scene, candidate, text, current, allow_non_hostile=True)
            if assaulting or current != before:
                target = current
                inferred = candidate != literal
                break
    confirm_first = assaulting and inferred and score < ASSAULT_CONFIRM_SCORE
    return ActionTarget(
        target=target,
        current_target=scene.current_target if confirm_first else current,
        assaulting=assaulting,
        inferred=inferred,
        confirm_first=confirm_first,
        no_opponent=is_attack and target is None,
        # Found by playtest: "casually reach out, tapping the heavy metal ring on his wrist", said
        # to the jailer, rolled polearms against the sheriff -- the default target -- and the
        # narrator, told "against Belor Hemlock", wrote a sword strike.
        incidental=not is_attack and explicit_target is None and bool(target) and _is_bystander(scene, target),
    )


def _redirect(scene, explicit_target, input_text, current, allow_non_hostile):
    """!
    @brief Honors an explicit, NLP-matched target as a combat redirect -- only if it names a live,
        in-scene creature that is hostile, or any living creature at all when allow_non_hostile
        (the action is an attack: anyone can be attacked). Naming a confidently-matched
        non-hostile entity for anything else (ex: a skill check near an ally) is silently ignored
        rather than making it the target. Two live instances of one template are told apart first
        (_resolve_instance_ambiguity), so a disambiguating word can redirect to a sibling before
        the hostile/alive checks run.
    @return (the combat target afterwards, whether it landed on a non-hostile creature).
    """
    explicit_target = _resolve_instance_ambiguity(scene, explicit_target, input_text, current)
    if not (
        explicit_target
        and explicit_target in scene.scenario_entities
        and explicit_target != scene.player_name
        and scene.hp(explicit_target) > 0
    ):
        return current, False
    if scene.is_hostile(explicit_target):
        return explicit_target, False
    if allow_non_hostile and scene.entities.get(explicit_target, {}).get("supertype") == "creature":
        return explicit_target, True
    return current, False


def _is_bystander(scene, key):
    """!
    @brief Whether an attack that named nobody has no fair target in key (normally the current
        target): there is none, or it's a creature not hostile to the player. An object (a chest,
        a trap) is still fair game, and so is anything already fighting.
    """
    if key is None:
        return True
    return scene.entities.get(key, {}).get("supertype") == "creature" and not scene.is_hostile(key)


def _instance_family(key):
    """!
    @brief Strips DM_Rules.py's own _unique_entity_key "_<N>" disambiguating suffix, if present,
        to recover the shared base name multiple live instances of one template were instanced
        under (ex: "wolf_2" -> "wolf"). A name with no numeric suffix returns unchanged.
    """
    match = re.match(r"^(.*)_(\d+)$", key)
    return match.group(1) if match else key


def _live_instances_sharing_family(scene, key):
    """!
    @brief Every living, in-scene entity sharing key's instance family, in stable creation order
        -- the bare base name first (if still alive), then "_2", "_3", ... A name with no live
        duplicates returns a single-element list.
    """
    family = _instance_family(key)

    def suffix(name):
        match = re.match(r"^.*_(\d+)$", name)
        return int(match.group(1)) if match else 1

    candidates = [
        name for name in scene.scenario_entities
        if _instance_family(name) == family and scene.hp(name) > 0
    ]
    candidates.sort(key=suffix)
    return candidates


def _resolve_instance_ambiguity(scene, explicit_target, input_text, current):
    """!
    @brief Re-checks input_text for a disambiguating word whenever explicit_target has one or more
        living same-family siblings still in the scene (ex: a second "wolf"). A single live
        instance (the overwhelmingly common case) short-circuits immediately. Checked in order --
        ordinal, then other/another (away from current), then wounded, then healthy -- and falls
        back to explicit_target unchanged if input_text carries none of them, or if a
        wounded/healthy claim doesn't match any candidate's real HP (TARGET_WOUNDED_HP_CUTOFF).
    @return explicit_target, or a same-family sibling instance name input_text actually pointed at.
    """
    if not explicit_target:
        return explicit_target
    candidates = _live_instances_sharing_family(scene, explicit_target)
    if len(candidates) <= 1:
        return explicit_target

    text = input_text.lower()

    for word, position in TARGET_ORDINAL_KEYWORDS.items():
        if position <= len(candidates) and re.search(rf"\b{word}\b", text):
            return candidates[position - 1]

    if any(re.search(rf"\b{word}\b", text) for word in TARGET_OTHER_KEYWORDS):
        others = [name for name in candidates if name != current]
        if others:
            return others[0]

    if any(re.search(rf"\b{word}\b", text) for word in TARGET_WOUNDED_KEYWORDS):
        wounded = [name for name in candidates if (scene.hp_fraction(name) or 1.0) < TARGET_WOUNDED_HP_CUTOFF]
        if wounded:
            return min(wounded, key=scene.hp_fraction)

    if any(re.search(rf"\b{word}\b", text) for word in TARGET_HEALTHY_KEYWORDS):
        healthy = [name for name in candidates if (scene.hp_fraction(name) or 0.0) >= TARGET_WOUNDED_HP_CUTOFF]
        if healthy:
            return max(healthy, key=scene.hp_fraction)

    return explicit_target


def _literal_attack_target(scene, input_text):
    """!
    @brief Who present an attack's own words point at -- Entity_Reference's whole-word key/name/
        alias scan, minus one kind of alias: a single word that is only a modifier inside a longer
        alias. A narrated spice merchant's aliases are "spice merchant", "spice" and "merchant";
        "pin the merchant" must find him, but "kick the spice cart" must not assault him, so
        "spice" is skipped and "merchant" (the head word) kept. Creatures only.
    @return An entity key, or None.
    """
    return first_named(
        input_text, scene.entities, scene.scenario_entities, exclude=scene.player_name,
        supertype="creature", skip_modifier_aliases=True,
    )


def _pronoun_attack_target(scene, input_text):
    """!
    @brief Who a gendered pronoun in an attack can only mean -- "kick her into the street" with one
        woman present. Found by playtest: with no name in the input and no conversation running,
        that kick met only air while the bread vendor stood right there. Only an unambiguous match
        counts; two women present and "her" means nobody.
    @return An entity key, or None.
    """
    words = set(re.findall(r"[a-z]+", input_text or ""))
    if words & {"her", "she", "hers"}:
        wanted = FEMALE_GENDERS
    elif words & {"him", "his", "he"}:
        wanted = MALE_GENDERS
    else:
        return None
    matches = [
        name for name in scene.scenario_entities
        if name != scene.player_name and not scene.is_party_member(name)
        and scene.entities.get(name, {}).get("supertype") == "creature"
        and scene.hp(name) > 0 and not scene.is_hidden(name)
        and str((scene.entities[name].get("qualities") or {}).get("gender", "")).lower() in wanted
    ]
    return matches[0] if len(matches) == 1 else None
