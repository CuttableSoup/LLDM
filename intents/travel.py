"""!
@file travel.py
@brief "travel" -- a free-standing intent (see CONTEXT.md's "Free-standing intent"). Takes a
    declared [[location.exit]] (or the current location's own "return_to"), or -- for a gridded
    current location -- a grid-based hop (DM_Travel.py), via DM_Movement.py's own
    _resolve_travel_intent; unrelated to the scene target or the locked-container gate, unlike
    every item-named intent. See docs/movement-scenarios.md and docs/downtime.md's own "Travel".
"""

_REASON_TEXT = {
    "no_exit": "there's no way through in that direction",
    "blocked_by_enemies": "something hostile is still standing in the way",
    "downtime_interrupted": "an unresolved threat from earlier is still keeping the party in place",
    "mount_overloaded": "whatever they're riding/hitched to is carrying more than it can bear and refuses to budge",
    "impassable_terrain": "the route crosses ground nothing in the party can actually get across",
}


def resolve_travel(core, data, resolved):
    """!
    @brief Resolves "travel" (see DM_Movement.py's _resolve_travel_intent) -- location-to-
        location travel, branching to grid-based travel first if the current location carries
        one. Denied (reason "no_exit") if no destination is named/known and no "return_to"
        applies, (reason "blocked_by_enemies") if a living hostile remains in the current
        room, (reason "downtime_interrupted") if a previously-paused trip/rest hasn't cleared
        yet (see docs/downtime.md's "Pausing for a fight"), (reason "mount_overloaded", a
        gridded destination only) if the player's own mount is currently overloaded (see
        docs/downtime.md's "Mounts and conveyance"), or (reason "impassable_terrain", a
        gridded destination only) if the straight-line route crosses terrain no currently-
        present party member can cross (see docs/downtime.md's "Terrain, roads, and polities").
    @param core The DMCore instance.
    @param data The item_interaction_detected payload ({input, ...}).
    @param resolved The item_interaction_resolved publisher closure from
        DMCore._on_item_interaction_detected.
    """
    core._resolve_travel_intent(data.get("input"), resolved)


def narrate_travel(llm_core, data):
    """!
    @brief Narrates "travel" (see resolve_travel). On success, folds the arrival room's own
        name/description/characters into ongoing narration grounding (llm_core.
        scenario_description/scenario_characters) when the new location has one active, else
        the location's own name/description -- same grounding-refresh reasoning as
        intents/move.py's own narrate_move. "blocks_spent"/"distance"/"time" are only ever
        present for a grid-based hop (DM_Travel.py) -- an ordinary exit-graph hop carries none
        of them, since it's instant. Any creature a travel block's own encounter table rolled up
        already narrates separately via its own "encounter_triggered", so this prompt only ever
        covers elapsed time, never invents what happened along the way. "polity" (a gridded
        destination only -- see docs/downtime.md's "Terrain, roads, and polities") is folded
        into the same journey sentence when present, never a separate fact the LLM has to
        invent on its own.
    @param llm_core The LLMCore instance -- its own scenario_description/scenario_characters
        are updated here on success, read by every later narration prompt until the next move/
        travel.
    @param data The "item_interaction_resolved" payload ({found, reason?, room_name?,
        room_description?, location_name?, location_description?, characters?, blocks_spent?,
        distance?, time?, polity?, input}).
    @return The narration prompt.
    """
    if not data.get("found"):
        reason_text = _REASON_TEXT.get(
            data.get("reason"), "the player's attempt to travel doesn't apply here",
        )
        return (
            f"The player tries to travel (input: \"{data.get('input', '')}\"), but "
            f"{reason_text} -- no roll involved.\n"
            f"Narrate a brief, in-character explanation in 1-2 sentences as the Game Master."
        )
    scene_name = data.get("room_name") or data.get("location_name", "")
    llm_core.scenario_description = data.get("room_description") or data.get("location_description", "")
    llm_core.scenario_characters = data.get("characters", [])
    characters_text = (
        "\nCharacters present: " + " | ".join(llm_core.scenario_characters)
        if llm_core.scenario_characters else ""
    )
    blocks_spent = data.get("blocks_spent")
    if blocks_spent:
        time_state = data.get("time") or {}
        time_of_day = "day" if time_state.get("is_day", True) else "night"
        date_label = time_state.get("date_label", f"day {time_state.get('day', 0)}")
        journey_text = (
            f" The journey took {blocks_spent} block(s) of travel time; it's now "
            f"{time_of_day}, {date_label}."
        )
    else:
        journey_text = ""
    polity = data.get("polity")
    polity_text = f" They've crossed into the borders of {polity}." if polity else ""
    return (
        f"The player travels to: \"{scene_name}\".{journey_text}{polity_text}\n"
        f"{llm_core.scenario_description}{characters_text}\n"
        f"Narrate arriving in this new place in 2-3 sentences as the Game Master."
    )
