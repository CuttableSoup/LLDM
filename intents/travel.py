"""!
@file travel.py
@brief "travel" -- a free-standing intent (see CONTEXT.md's "Free-standing intent"). Takes a
    declared [[location.exit]] (or the current location's own "return_to"), or -- for a gridded
    current location -- a grid-based hop (DM_Travel.py), via DM_Movement.py's own
    _resolve_travel_intent; unrelated to the scene target or the locked-container gate, unlike
    every item-named intent. See docs/movement-scenarios.md and docs/downtime.md's own "Travel".
"""

import re
from intents.match import IntentMatch

# -- What the classifier matches (see intents/registry.py's MATCHES) --

# Location-to-location travel (see DM_Movement.py's _resolve_travel_intent) -- a different axis
# from DIRECTION_PHRASES below: a room's own exits are a fixed forward/back/left/right
# vocabulary, but a location's own exits are reachable by naming where you want to go, which
# this module has no catalog of (self.locations lives on DMCore, not here) -- so unlike
# detect_direction, this only recognizes that the input *smells like* a travel attempt at all;
# DMCore resolves *which* location it names from the raw input itself (same "search input for a
# known name" pattern _resolve_dialogue_target/_resolve_formation_intent already use). Checked
# ahead of item-interaction detection, same tier as DIRECTION_PHRASES. Deliberately no "travel
# to " here -- it would collide with skills.toml's own navigation keyword "travel" (see
# test_item_and_dialogue_keywords_never_collide_with_a_real_skill_keyword), so "go to "/"head
# to "/"walk to " cover the same phrasing without that risk.
TRAVEL_KEYWORDS = ("go to ", "head to ", "walk to ", "proceed to ", "enter the ", "go outside", "exit the")
# Checked separately from TRAVEL_KEYWORDS' own plain substring match -- a bare "leave" collides
# with axes' own "cleave" skill keyword the same way ADAM_NAME_PATTERN's "adam" would collide
# with plenty of ordinary words without \b-anchoring; word-boundary matching is what a short,
# common word like this needs, same precedent ADAM_NAME_PATTERN already sets.
LEAVE_PATTERN = re.compile(r"\bleave\b")
# Semantic-router phrases (nlp/Intent_Classification.py's INTENT_PROTOTYPES).
PROTOTYPES = {
        # No "step inside the inn" here, deliberately: "inn" sits close enough to "innkeeper" that
        # a plain greeting ("hey there innkeeper") scored 0.56 against it and routed as travel.
        # A prototype whose distinguishing noun is also a common NPC role word earns its whole
        # intent a false positive on every greeting aimed at that role -- "head into the tavern"
        # already covers entering a named building without that collision.

    "travel": (
        "head into the tavern", "go over to the market square", "walk to the blacksmith shop",
        "make my way to the temple", "step inside the guild hall", "leave here for the docks",
    ),
}
MATCH = {
    "travel": IntentMatch(keywords=TRAVEL_KEYWORDS, patterns=(LEAVE_PATTERN,), prototypes=PROTOTYPES["travel"], question_blocked=True, item_pass=False),
}


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
    @param data The item_interaction_detected payload ({input, destination?, ...}) --
        "destination" is NLPCore's own semantic match (Intent_Classification.py's
        _travel_event), absent/None whenever nothing was named confidently.
    @param resolved The item_interaction_resolved publisher closure from
        DMCore._on_item_interaction_detected.
    """
    core._resolve_travel_intent(
        data.get("input"), resolved, destination_key=data.get("destination"),
    )


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
    # Both when both exist, not just the room. A player who says "the tavern" and is narrated
    # arriving in "Common Room" alone has no way to tell a correct destination match from a
    # wrong one -- and semantic destination matching (NLP_Core.py's map_to_destination) makes
    # the player's own words and the arrival room's authored name differ routinely, where the
    # literal name scan alone mostly guaranteed they'd agree. Naming the location back is what
    # keeps a guessed destination checkable by the person who guessed at it.
    room_name = data.get("room_name")
    location_name = data.get("location_name", "")
    if room_name and location_name:
        scene_name = f"{location_name} ({room_name})"
    else:
        scene_name = room_name or location_name
    description = data.get("room_description") or data.get("location_description", "")
    characters = data.get("characters", [])
    characters_text = "\nCharacters present: " + " | ".join(characters) if characters else ""
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
        f"{description}{characters_text}\n"
        f"{llm_core.scene_length_instruction('arriving in this new place')}"
    )
