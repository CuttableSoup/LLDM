"""!
@file advance_retreat.py
@brief "advance"/"retreat" -- a free-standing intent (see CONTEXT.md's "Free-standing
    intent"). Repositions the whole scene at once via DM_Movement.py's own advance_or_retreat;
    unrelated to the scene target or the locked-container gate, unlike every item-named intent.
"""


def resolve_advance_retreat(core, data, resolved):
    """!
    @brief Resolves "advance"/"retreat" (see DM_Movement.py's advance_or_retreat) -- shifts the
        player's own band toward/away from current_target by up to their own speed, snapping
        party formation back into place. Always succeeds (there's no failure reason this can
        report) -- an empty "moved" list, when nothing else is present to react, is itself a
        valid outcome, not a denial.
    @param core The DMCore instance.
    @param data The item_interaction_detected payload ({intent, ...}).
    @param resolved The item_interaction_resolved publisher closure from
        DMCore._on_item_interaction_detected.
    """
    moved = core.advance_or_retreat(data.get("intent"))
    resolved(True, moved=moved)


def narrate_advance_retreat(llm_core, data):
    """!
    @brief Narrates "advance"/"retreat" (see resolve_advance_retreat). "moved" is
        advance_or_retreat's own {entity, before, after} list (DM_Movement.py) -- real
        band-gap numbers already earned by the player's own movement, never invented. Only the
        player's own band actually changes, so the effect on any two entities isn't necessarily
        the same direction -- retreating from current_target can close the gap to something
        else entirely, which is why this doesn't claim a uniform "moves away from everyone".
    @param llm_core The LLMCore instance -- unused; this intent narrates no ongoing scene
        grounding, unlike move/narrate_travel.
    @param data The "item_interaction_resolved" payload ({intent, moved, input}).
    @return The narration prompt.
    """
    intent = data.get("intent")
    moved = data.get("moved") or []
    if moved:
        movement_text = "; ".join(
            f"{entry['entity']} ({entry['before']} -> {entry['after']} bands away)" for entry in moved
        )
        verb = "advances" if intent == "advance" else "retreats"
        return (
            f"The player {verb}, changing how many bands away everyone present now is: "
            f"{movement_text}.\n"
            f"Narrate this brief repositioning in 1-2 sentences as the Game Master -- if "
            f"the numbers show the player got closer to one but farther from another, "
            f"that's real, not a mistake."
        )
    return (
        f"The player tries to {intent}, but there's no one else here for it to matter "
        f"against.\n"
        f"Narrate this in 1-2 sentences as the Game Master."
    )
