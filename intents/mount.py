"""!
@file mount.py
@brief "mount"/"dismount" -- a free-standing intent (see CONTEXT.md's "Free-standing
    intent"). Repositions the player onto a named, currently-present entity via
    DM_Movement.py's own _resolve_mount_intent/_resolve_dismount_intent; unrelated to the
    scene target or the locked-container gate, unlike every item-named intent.
"""


def resolve_mount(core, data, resolved):
    """!
    @brief Resolves "mount"/"ride" (see DM_Movement.py's _resolve_mount_intent).
    @param core The DMCore instance.
    @param data The item_interaction_detected payload ({input, ...}).
    @param resolved The item_interaction_resolved publisher closure from
        DMCore._on_item_interaction_detected.
    """
    core._resolve_mount_intent(data.get("input"), resolved)


def narrate_mount(llm_core, data):
    """!
    @brief Narrates "mount"/"ride" (see resolve_mount). "target" is the real entity name
        _resolve_mount_intent resolved, never invented.
    @param llm_core The LLMCore instance -- unused; this intent narrates no ongoing scene
        grounding.
    @param data The "item_interaction_resolved" payload ({found, reason?, target?, input}).
    @return The narration prompt.
    """
    if not data.get("found"):
        reason = data.get("reason")
        explanations = {
            "already_mounted": "they're already mounted on something else -- they'd need to dismount first",
            "not_present": "nothing here matches what they're trying to mount",
            "target_down": "what they're trying to mount is down, not something to climb onto",
            "target_hostile": "what they're trying to mount is hostile -- not something to just climb onto",
            "not_a_mount": "it's not something meant to be ridden at all",
            "bulk_exceeded": "it's already carrying more than it can bear -- there's no room for another rider",
        }
        explanation = explanations.get(reason, "it doesn't work")
        return (
            f"The player tries to mount something (input: \"{data.get('input', '')}\"), but "
            f"{explanation} -- no roll involved.\n"
            f"Narrate a brief, in-character explanation in 1-2 sentences as the Game Master."
        )
    return (
        f"The player climbs onto {data.get('target')}.\n"
        f"Narrate this brief action in 1-2 sentences as the Game Master -- no roll was "
        f"involved, it either succeeds cleanly or not at all."
    )


def resolve_dismount(core, data, resolved):
    """!
    @brief Resolves "dismount" (see DM_Movement.py's _resolve_dismount_intent).
    @param core The DMCore instance.
    @param data The item_interaction_detected payload ({input, ...}).
    @param resolved The item_interaction_resolved publisher closure from
        DMCore._on_item_interaction_detected.
    """
    core._resolve_dismount_intent(resolved)


def narrate_dismount(llm_core, data):
    """!
    @brief Narrates "dismount" (see resolve_dismount). "target" is whatever the player was
        actually mounted on, never invented.
    @param llm_core The LLMCore instance -- unused; this intent narrates no ongoing scene
        grounding.
    @param data The "item_interaction_resolved" payload ({found, reason?, target?, input}).
    @return The narration prompt.
    """
    if not data.get("found"):
        return (
            f"The player tries to dismount, but they aren't mounted on anything -- no roll "
            f"involved.\n"
            f"Narrate a brief, in-character explanation in 1-2 sentences as the Game Master."
        )
    return (
        f"The player dismounts from {data.get('target')}.\n"
        f"Narrate this brief action in 1-2 sentences as the Game Master -- no roll was "
        f"involved."
    )
