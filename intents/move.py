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
    @brief Narrates "move" (see resolve_move). Reads the arrival from the payload and writes
        nothing: LLMCore refreshes the narrator's scene state from the same payload before this
        runs (see intents/registry.py's ARRIVAL_INTENTS).
    @param llm_core The narrator state, used only for its scene_length_instruction.
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
    description = data.get("room_description", "")
    characters = data.get("characters", [])
    characters_text = "\nCharacters present: " + " | ".join(characters) if characters else ""
    return (
        f"The player heads {data.get('direction', 'onward')}, arriving at: "
        f"\"{data.get('room_name', '')}\".\n"
        f"{description}{characters_text}\n"
        f"{llm_core.scene_length_instruction('arriving in this new area')}"
    )
