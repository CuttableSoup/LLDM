"""!
@file Law_Resolution.py
@brief Pure, DMCore-independent law helpers -- which laws a crime breaks, how well-known someone
    is (acclaim), how hard they are to recognize, and how a witness report lands on a polity's
    record. DM_Law.py's LawMixin is the DMCore side: it decides who witnessed what and when a
    report is filed, and calls into these. See docs/law.md.

    A polity record is {"bounty", "acclaim", "crimes"} per offender identity (an entity name, or
    a disguise's alias). It only ever changes inside its own polity -- nothing here knows where
    the offender currently is; LawMixin is what refuses to touch a record from outside.
"""

import math

import resolution.Combat_Resolution as Combat_Resolution
import resolution.Social_Resolution as Social_Resolution
from resolution.Inventory_Resolution import _settle

# Every crime kind a [[polity.law]] may name -- DM_Validation.py rejects anything else.
CRIMES = ("theft", "assault", "murder", "banned_ability", "banned_presence", "resisting_arrest")

# A recognition band naming this instead of a difficulty tier means "no roll at all" -- a
# shambling corpse is recognized by anyone, trained or not (an untrained 0-dice roll would
# otherwise always fail).
AUTOMATIC = "automatic"


def law_matches(law, subject):
    """!
    @brief Whether law's own "match" table covers subject.
    @param law A [[polity.law]] entry.
    @param subject The entity dict the law is about (a cast spell, a present creature), or None
        for a crime with no subject (theft/assault/murder).
    @return True when the law has no "match" at all; otherwise True when subject's name is in
            "names", its supertype/subtype matches (Combat_Resolution.matches_supertype_or_subtype,
            the same shape lore_types/dispel use), or it carries one of "tags".
    """
    spec = law.get("match")
    if not spec:
        return True
    if subject is None:
        return False
    if subject.get("name") in spec.get("names", []):
        return True
    if set(spec.get("tags", [])) & set(subject.get("tags", [])):
        return True
    return Combat_Resolution.matches_supertype_or_subtype(subject, spec)


def matching_laws(laws, crime, subject=None):
    """!
    @brief The laws a crime of this kind breaks.
    @param laws The laws in force (merge_laws's output).
    @param crime One of CRIMES.
    @param subject See law_matches.
    @return Every law naming this crime whose "match" covers subject, in authored order.
    """
    return [law for law in laws if law.get("crime") == crime and law_matches(law, subject)]


def merge_laws(polity_laws, location_laws):
    """!
    @brief A location's own [[location.law]] entries layered over its polity's -- one with the
        same crime and "match" as a polity law replaces it (a temple fining theft more harshly),
        anything else is added (a noble quarter banning something the city at large allows).
    @param polity_laws The polity's own "law" list.
    @param location_laws The location's own "law" list.
    @return The laws in force here.
    """
    merged = list(polity_laws or [])
    for law in location_laws or []:
        key = (law.get("crime"), repr(law.get("match")))
        merged = [existing for existing in merged if (existing.get("crime"), repr(existing.get("match"))) != key]
        merged.append(law)
    return merged


def effective_acclaim(entity, record):
    """!
    @brief How someone is known here -- their own base acclaim plus this polity's.
    @param entity The entity dict (its own authored "acclaim", default 0).
    @param record That entity's record in the current polity, or None.
    @return (signed_total, magnitude). The sign says how they're known (negative: crimes,
            positive: deeds); magnitude is |base| + |polity|, which is what recognition reads, so
            a famous hero who turns thief here is *more* recognizable, not cancelled out to 0.
    """
    base = entity.get("acclaim", 0) or 0
    polity = (record or {}).get("acclaim", 0) or 0
    return base + polity, abs(base) + abs(polity)


def recognition_difficulty(magnitude, bands, tiers):
    """!
    @brief How hard it is to recognize someone of this acclaim magnitude.
    @param magnitude effective_acclaim's magnitude.
    @param bands rules.toml's [[law.recognition]] entries ({min_acclaim, tier}).
    @param tiers rules.toml's [[difficulty_tier]] entries.
    @return None if nobody could recognize them (magnitude 0, or below every band); 0 for an
            AUTOMATIC band (recognized without a roll); else the band's tier difficulty.
    """
    if magnitude <= 0:
        return None
    for band in sorted(bands or [], key=lambda b: b.get("min_acclaim", 0), reverse=True):
        if magnitude >= band.get("min_acclaim", 0):
            if band.get("tier") == AUTOMATIC:
                return 0
            tier = next((t for t in tiers or [] if t.get("name") == band.get("tier")), None)
            return int(tier.get("difficulty", 0)) if tier else None
    return None


def base_disposition(entities, entity_name, toward_name):
    """!
    @brief entity_name's *authored* disposition toward toward_name, ignoring drift from play --
        a bandit who was always hostile won't run to the watch, but a shopkeeper who turned
        hostile because the player just robbed them still will.
    @return The disposition, or None if entity_name authors no attitudes at all (a monster).
    """
    entity = entities.get(entity_name, {})
    if "attitudes" not in entity:
        return None
    undrifted = {key: value for key, value in entity.items() if key not in ("attitude_deltas", "action_attitude_deltas")}
    return Social_Resolution.get_attitude({**entities, entity_name: undrifted}, entity_name, toward_name)[0]


def is_capable_witness(entities, name, offender_name):
    """!
    @brief Whether name could ever report a crime by offender_name: an animate, living
        creature that speaks a language (a wolf or a zombie never calls the guard) and wasn't
        authored hostile toward the offender to begin with.
    """
    entity = entities.get(name, {})
    if entity.get("supertype") == "object" or not entity.get("languages"):
        return False
    if Combat_Resolution.get_current_hp(entities, name) <= 0:
        return False
    disposition = base_disposition(entities, name, offender_name)
    return disposition is not None and disposition > -100


def new_record():
    """!@brief An empty polity record for one offender identity."""
    return {"bounty": 0, "acclaim": 0, "crimes": []}


def file_report(records, polity, identity, law, crime_line):
    """!
    @brief Files one crime on identity's record in polity. A murder supersedes an assault on
        the same victim already filed: the assault's own fine/acclaim are taken back first, so a
        fight that ends in a death costs the murder penalty once, not murder plus assault.
    @param records DMCore.legal_records ({polity: {identity: record}}), mutated in place.
    @param polity The polity's name.
    @param identity Who it's filed against (an entity name or a disguise alias).
    @param law The [[polity.law]] broken.
    @param crime_line {"crime", "victim"?, "subject"?, "block"} -- stored with the fine/acclaim
        actually charged.
    @return The updated record.
    """
    record = records.setdefault(polity, {}).setdefault(identity, new_record())
    fine = law.get("fine", 0) or 0
    acclaim = law.get("acclaim", 0) or 0
    if crime_line.get("crime") == "murder" and crime_line.get("victim"):
        for earlier in record["crimes"]:
            if earlier.get("crime") == "assault" and earlier.get("victim") == crime_line["victim"] and not earlier.get("superseded"):
                earlier["superseded"] = True
                fine -= earlier.get("fine", 0)
                acclaim -= earlier.get("acclaim", 0)
    record["bounty"] = _settle(record["bounty"] + fine)
    record["acclaim"] += acclaim
    record["crimes"].append({**crime_line, "fine": law.get("fine", 0) or 0, "acclaim": law.get("acclaim", 0) or 0})
    return record


def jail_blocks(shortfall, blocks_per_unit):
    """!
    @brief How long a surrender that couldn't cover the bounty is served.
    @param shortfall The bounty left unpaid, in the setting's value unit.
    @param blocks_per_unit [law].jail_blocks_per_unit.
    @return Whole blocks -- at least 1 whenever anything is owed, 0 when nothing is.
    """
    if shortfall <= 0:
        return 0
    return max(1, math.ceil(round(shortfall * (blocks_per_unit or 0), 6)))


def bribe_modifier(offer, bounty, bands):
    """!
    @brief How an offer's size shifts a bribe's difficulty.
    @param offer What the player offered.
    @param bounty What the enforcer was demanding.
    @param bands [[law.bribe]] entries ({min_share, modifier}) -- the highest min_share the
        offer reaches (offer / bounty) wins.
    @return The difficulty modifier, or None when the offer is below every band (refused out of
            hand -- an insult, not a bribe).
    """
    share = offer / bounty if bounty > 0 else float("inf")
    for band in sorted(bands or [], key=lambda b: b.get("min_share", 0), reverse=True):
        if share >= band.get("min_share", 0):
            return band.get("modifier", 0)
    return None
