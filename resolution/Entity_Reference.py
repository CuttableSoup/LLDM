"""!
@file Entity_Reference.py
@brief Entity reference -- which entities a line of player text literally names. One rule for
    "does this text name that entity", shared by dialogue, attacks, mount/hitch/formation and
    lore checks, so a change to alias semantics happens here and nowhere else.

    An entity is named by any of three phrases: its entities-dict *key*, its own "name" field,
    and each entry of its optional "aliases" list. The key alone became a gap the moment
    instanced crowds existed -- a background instance keyed "sandpoint_townsfolk_2" but shown to
    the player as "Fishmonger" was unaddressable by the only name the player ever sees. Matching
    is whole-word and case-insensitive against already-lowercased input.

    skip_modifier_aliases drops one kind of alias: a single word that is only a modifier inside
    a longer alias. A narrated spice merchant's aliases are "spice merchant", "spice" and
    "merchant"; "pin the merchant" must find him, but "kick the spice cart" must not assault him,
    so "spice" is skipped and "merchant" (the head word, last in the phrase) kept.

    Pure: no DMCore, WorldContext or event bus. Fuzzy and embedding matching of an address phrase
    stays in nlp/ -- this module is exact-word matching only.
"""

import re

WORD_BOUNDARY = r"\b%s\b"


def mentions(text, phrase):
    """!
    @brief Whole-word, case-insensitive search of text for phrase.
    @return The re.Match, or None (also None for an empty phrase).
    """
    if not phrase:
        return None
    return re.search(WORD_BOUNDARY % re.escape(phrase.lower()), text or "")


def mentions_any(text, phrases):
    """!@brief The first re.Match (in phrase order) of any phrase in text, or None."""
    for phrase in phrases:
        match = mentions(text, phrase)
        if match:
            return match
    return None


def entity_phrases(key, entity, skip_modifier_aliases=False):
    """!
    @brief Every phrase that names an entity: its key, its "name", and its aliases.
    @param skip_modifier_aliases Drop a single-word alias that is only a modifier in a longer one.
    """
    aliases = [alias.lower() for alias in entity.get("aliases", [])]
    if skip_modifier_aliases:
        modifiers = {word for alias in aliases if " " in alias for word in alias.split()[:-1]}
        aliases = [alias for alias in aliases if alias not in modifiers]
    return [key, entity.get("name", ""), *aliases]


def find_named(text, entities, keys, exclude=None, supertype=None, skip_modifier_aliases=False):
    """!
    @brief The entities among keys that text names, in keys' own order (declaration order breaks
        a tie the way every first-match-wins list in this codebase does).
    @param text The player's raw (already lowercased) input.
    @param entities The entities dict, key -> entity.
    @param keys The candidate keys, normally the scene's present entities.
    @param exclude A key never returned (normally the player).
    @param supertype If given, only entities of this supertype are candidates.
    @return A list of (key, first match position) pairs.
    """
    found = []
    for key in keys:
        entity = entities.get(key, {})
        if key == exclude or (supertype and entity.get("supertype") != supertype):
            continue
        match = None
        for phrase in entity_phrases(key, entity, skip_modifier_aliases):
            candidate = mentions(text, phrase)
            if candidate and (match is None or candidate.start() < match.start()):
                match = candidate
        if match:
            found.append((key, match.start()))
    return found


def first_named(text, entities, keys, **kwargs):
    """!@brief The first key in keys' own order that text names, or None. See find_named."""
    named = find_named(text, entities, keys, **kwargs)
    return named[0][0] if named else None


def named_in_reading_order(text, entities, keys, **kwargs):
    """!@brief Every key text names, ordered by where it first appears. See find_named."""
    return [key for key, _ in sorted(find_named(text, entities, keys, **kwargs), key=lambda pair: pair[1])]
