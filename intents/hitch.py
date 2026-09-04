"""!
@file hitch.py
@brief "hitch"/"unhitch" -- a free-standing intent (see CONTEXT.md's "Free-standing intent").
    Attaches one named, currently-present entity onto another's own "mount" field via
    DM_Movement.py's own _resolve_hitch_intent/_resolve_unhitch_intent -- the player-facing
    way a cart/wagon actually gets a horse (or team) hitched to it; unrelated to the scene
    target or the locked-container gate, unlike every item-named intent.
"""


def resolve_hitch(core, data, resolved):
    """!
    @brief Resolves "hitch" (see DM_Movement.py's _resolve_hitch_intent).
    @param core The DMCore instance.
    @param data The item_interaction_detected payload ({input, ...}).
    @param resolved The item_interaction_resolved publisher closure from
        DMCore._on_item_interaction_detected.
    """
    core._resolve_hitch_intent(data.get("input"), resolved)


def narrate_hitch(llm_core, data):
    """!
    @brief Narrates "hitch" (see resolve_hitch). "puller"/"vehicle" are the real entity names
        _resolve_hitch_intent resolved, never invented.
    @param llm_core The LLMCore instance -- unused; this intent narrates no ongoing scene
        grounding.
    @param data The "item_interaction_resolved" payload ({found, reason?, puller?, vehicle?,
        input}).
    @return The narration prompt.
    """
    if not data.get("found"):
        reason = data.get("reason")
        explanations = {
            "not_present": "there aren't two things here matching what they're trying to hitch together",
            "target_down": "one of them is down, not something to hitch up",
            "target_hostile": "what they're trying to hitch is hostile -- not something to just walk up to",
            "not_a_puller": "it's not something capable of pulling anything",
            "not_a_vehicle": "it's not something meant to be hitched to at all",
            "already_hitched": "it's already hitched that way",
        }
        explanation = explanations.get(reason, "it doesn't work")
        return (
            f"The player tries to hitch something up (input: \"{data.get('input', '')}\"), but "
            f"{explanation} -- no roll involved.\n"
            f"Narrate a brief, in-character explanation in 1-2 sentences as the Game Master."
        )
    return (
        f"The player hitches {data.get('puller')} to {data.get('vehicle')}.\n"
        f"Narrate this brief action in 1-2 sentences as the Game Master -- no roll was "
        f"involved, it either succeeds cleanly or not at all."
    )


def resolve_unhitch(core, data, resolved):
    """!
    @brief Resolves "unhitch" (see DM_Movement.py's _resolve_unhitch_intent).
    @param core The DMCore instance.
    @param data The item_interaction_detected payload ({input, ...}).
    @param resolved The item_interaction_resolved publisher closure from
        DMCore._on_item_interaction_detected.
    """
    core._resolve_unhitch_intent(data.get("input"), resolved)


def narrate_unhitch(llm_core, data):
    """!
    @brief Narrates "unhitch" (see resolve_unhitch). "puller"/"vehicle" are the real entity
        names _resolve_unhitch_intent resolved, never invented.
    @param llm_core The LLMCore instance -- unused; this intent narrates no ongoing scene
        grounding.
    @param data The "item_interaction_resolved" payload ({found, reason?, puller?, vehicle?,
        input}).
    @return The narration prompt.
    """
    if not data.get("found"):
        reason = data.get("reason")
        explanations = {
            "not_present": "nothing here matches what they're trying to unhitch",
            "not_hitched": "it isn't actually hitched to anything right now",
        }
        explanation = explanations.get(reason, "it doesn't work")
        return (
            f"The player tries to unhitch something (input: \"{data.get('input', '')}\"), but "
            f"{explanation} -- no roll involved.\n"
            f"Narrate a brief, in-character explanation in 1-2 sentences as the Game Master."
        )
    return (
        f"The player unhitches {data.get('puller')} from {data.get('vehicle')}.\n"
        f"Narrate this brief action in 1-2 sentences as the Game Master -- no roll was "
        f"involved."
    )
