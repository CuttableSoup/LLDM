"""!
@file move.py
@brief "move" -- a free-standing intent (see CONTEXT.md's "Free-standing intent"). Takes a
    declared exit to a different room of the current location, via DM_Movement.py's own
    _resolve_room_transition_intent; unrelated to the scene target or the locked-container
    gate, unlike every item-named intent. See docs/movement-scenarios.md's own "Location-to-
    location travel" for the location-graph counterpart, narrated by intents/travel.py.
"""

_REASON_TEXT = {
    "no_exit": "there's no way through in that direction",
    "wrong_band": "the player isn't standing in the right spot to reach that way out",
    "blocked_by_enemies": "something hostile is still standing in the way",
}


def resolve_move(core, data, resolved):
    """!
    @brief Resolves "move" (see DM_Movement.py's _resolve_room_transition_intent) -- takes a
        declared [[room.exit]] usable from the player's current band. Denied (reason "no_exit")
        if the current room has none in that direction, (reason "wrong_band") if it does but
        not from here, or (reason "blocked_by_enemies") if a living hostile remains in the room.
    @param core The DMCore instance.
    @param data The item_interaction_detected payload ({direction, ...}).
    @param resolved The item_interaction_resolved publisher closure from
        DMCore._on_item_interaction_detected.
    """
    core._resolve_room_transition_intent(data.get("direction"), resolved)


def narrate_move(llm_core, data):
    """!
    @brief Narrates "move" (see resolve_move). On success, folds the new room's own name/
        description/characters into ongoing narration grounding (llm_core.scenario_description/
        scenario_characters) the same way generate_scene_intro does for a brand-new scenario --
        otherwise every later action prompt in the new room would keep citing the *previous*
        room's flavor text.
    @param llm_core The LLMCore instance -- its own scenario_description/scenario_characters
        are updated here on success, read by every later narration prompt until the next move.
    @param data The "item_interaction_resolved" payload ({found, reason?, direction, room_name?,
        room_description?, characters?, input}).
    @return The narration prompt.
    """
    if not data.get("found"):
        reason_text = _REASON_TEXT.get(
            data.get("reason"), "the player's attempt to move doesn't apply here",
        )
        return (
            f"The player tries to head {data.get('direction', 'onward')} "
            f"(input: \"{data.get('input', '')}\"), but {reason_text} -- no roll involved.\n"
            f"Narrate a brief, in-character explanation in 1-2 sentences as the Game Master."
        )
    llm_core.scenario_description = data.get("room_description", "")
    llm_core.scenario_characters = data.get("characters", [])
    characters_text = (
        "\nCharacters present: " + " | ".join(llm_core.scenario_characters)
        if llm_core.scenario_characters else ""
    )
    return (
        f"The player heads {data.get('direction', 'onward')}, arriving at: "
        f"\"{data.get('room_name', '')}\".\n"
        f"{llm_core.scenario_description}{characters_text}\n"
        f"Narrate arriving in this new area in 2-3 sentences as the Game Master."
    )
