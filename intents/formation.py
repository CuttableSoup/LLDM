"""!
@file formation.py
@brief "formation_behind"/"formation_abreast" -- a free-standing intent (see CONTEXT.md's
    "Free-standing intent"). Directs the party via DM_Movement.py's own
    _resolve_formation_intent; unrelated to the scene target or the locked-container gate,
    unlike every item-named intent.
"""

from intents.match import IntentMatch

# -- What the classifier matches (see intents/registry.py's MATCHES) --

# Party positioning (see DM_Core._resolve_formation_intent / docs/movement-scenarios.md's
# "Party formation") --
# like advance/retreat above, these act on the scene (specifically, whichever party member is
# named, or the whole party if none is) rather than a named item, so no map_to_item lookup ever
# runs for them either; unlike advance/retreat, DMCore -- not this module -- is what figures out
# *who* is being addressed, by searching the raw input for a party member's own name. None of
# these phrases collide with ADVANCE/RETREAT_KEYWORDS' own substrings (ex: "fall back" is
# retreat, "fall in behind" is not "fall back").
FORMATION_BEHIND_KEYWORDS = (
    "stay behind", "get behind", "hang back", "keep behind", "fall in behind", "stand behind",
)
FORMATION_ABREAST_KEYWORDS = (
    "walk beside", "stay beside", "stay abreast", "walk with me", "walk alongside", "flank me",
    "stand beside", "walk abreast",
)
# Semantic-router phrases (nlp/Intent_Classification.py's INTENT_PROTOTYPES).
PROTOTYPES = {
    "formation_behind": (
        "stay behind me", "keep back and follow me", "fall in behind me",
    ),
    "formation_abreast": (
        "walk beside me", "stay at my side", "move up alongside me",
    ),
}
MATCH = {
    "formation_behind": IntentMatch(keywords=FORMATION_BEHIND_KEYWORDS, prototypes=PROTOTYPES["formation_behind"], exempt=True),
    "formation_abreast": IntentMatch(keywords=FORMATION_ABREAST_KEYWORDS, prototypes=PROTOTYPES["formation_abreast"], exempt=True),
}


def resolve_formation(core, data, resolved):
    """!
    @brief Resolves "formation_behind"/"formation_abreast" (see DM_Movement.py's
        _resolve_formation_intent) -- a player-issued party positioning command, addressing
        whichever party member(s) are named in the raw input, or every party member present if
        none is. Denied (reason "no_party") if no party member is present at all.
    @param core The DMCore instance.
    @param data The item_interaction_detected payload ({intent, input, ...}).
    @param resolved The item_interaction_resolved publisher closure from
        DMCore._on_item_interaction_detected.
    """
    core._resolve_formation_intent(data.get("intent"), data.get("input"), resolved)


def narrate_formation(llm_core, data):
    """!
    @brief Narrates "formation_behind"/"formation_abreast" (see resolve_formation). "members"/
        "stance" are DM_Movement.py's own real result -- whichever party member(s) it actually
        resolved (named in the input, or every party member present if none was), never
        invented.
    @param llm_core The LLMCore instance -- unused; this intent narrates no ongoing scene
        grounding.
    @param data The "item_interaction_resolved" payload ({found, reason?, members?, stance?,
        input}).
    @return The narration prompt.
    """
    if not data.get("found"):
        return (
            f"The player tries to direct the party (input: \"{data.get('input', '')}\"), but "
            f"there's no one from the player's own party here to direct -- no roll involved.\n"
            f"Narrate a brief, in-character explanation in 1-2 sentences as the Game Master."
        )
    members = data.get("members") or []
    members_text = " and ".join(members)
    stance_text = (
        "stay a band behind the player from now on" if data.get("stance") == "behind"
        else "walk abreast of the player from now on"
    )
    return (
        f"The player directs {members_text} to {stance_text}.\n"
        f"Narrate this brief bit of party direction in 1-2 sentences as the Game Master."
    )
