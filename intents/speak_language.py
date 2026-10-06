"""!
@file speak_language.py
@brief "speak_language" -- a free-standing intent (see CONTEXT.md's "Free-standing intent").
    Switches the player's own active spoken language via DM_Dialogue.py's own
    _resolve_language_intent; unrelated to the scene target or the locked-container gate,
    unlike every item-named intent.
"""

from intents.match import IntentMatch

# -- What the classifier matches (see intents/registry.py's MATCHES) --

# Switching which of the player's own known languages is currently active (see DM_Dialogue.py's
# _current_language/_resolve_language_intent) -- like formation above, this acts on the player's
# own state rather than a named item, so no map_to_item lookup ever runs for it either; DMCore,
# not this module, is what figures out *which* language is named, by searching the raw input for
# one of the player's own "languages" (same "search input for a known name" pattern
# _resolve_formation_intent already uses for a party member's own name). Phrases, not a bare
# "speak ", since a bare word would collide with the linguistics skill's own "speak" keyword
# (skills.toml) -- same collision-avoidance reason DIALOGUE_KEYWORDS' own "speak to "/"speak
# with " already follow.
SPEAK_LANGUAGE_KEYWORDS = ("speak in ", "switch to speaking ", "start speaking ")
MATCH = {
    "speak_language": IntentMatch(keywords=SPEAK_LANGUAGE_KEYWORDS, exempt=True),
}


def resolve_speak_language(core, data, resolved):
    """!
    @brief Resolves "speak_language" (see DM_Dialogue.py's _resolve_language_intent) --
        switches which of the player's own known languages is currently active, matched
        whole-word/case-insensitive against the raw input. Denied (reason "unknown_language")
        if no known language is named.
    @param core The DMCore instance.
    @param data The item_interaction_detected payload ({input, ...}).
    @param resolved The item_interaction_resolved publisher closure from
        DMCore._on_item_interaction_detected.
    """
    core._resolve_language_intent(data.get("input"), resolved)


def narrate_speak_language(llm_core, data):
    """!
    @brief Narrates "speak_language" (see resolve_speak_language). "language" is
        DM_Dialogue.py's own real result -- whichever of the player's own known languages it
        actually matched in the raw input, never invented.
    @param llm_core The LLMCore instance -- unused; this intent narrates no ongoing scene
        grounding.
    @param data The "item_interaction_resolved" payload ({found, language?, input}).
    @return The narration prompt.
    """
    if not data.get("found"):
        return (
            f"The player tries to switch which language they're speaking "
            f"(input: \"{data.get('input', '')}\"), but the player doesn't actually know any "
            f"language matching that -- no roll involved.\n"
            f"Narrate a brief, in-character explanation in 1-2 sentences as the Game Master."
        )
    return (
        f"The player deliberately switches to speaking {data.get('language')} from now "
        f"on.\n"
        f"Narrate this brief moment in 1-2 sentences as the Game Master."
    )
