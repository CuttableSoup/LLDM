"""!
@file advance_retreat.py
@brief "advance"/"retreat" -- a free-standing intent (see CONTEXT.md's "Free-standing
    intent"). Repositions the whole scene at once via DM_Movement.py's own advance_or_retreat;
    unrelated to the scene target or the locked-container gate, unlike every item-named intent.
"""

import re
from intents.match import IntentMatch

# -- What the classifier matches (see intents/registry.py's MATCHES) --

# Movement/positioning (see DM_Movement.py) -- like open/close, these act on the whole scene
# rather than a named item, so no map_to_item lookup ever runs for them either. Phrases, not
# bare "move ", since a bare word would swallow unrelated skill phrasing the same way a bare
# "close " would have (see the module note above) -- none of these collide with any
# skills.toml keyword list. Deliberately no "close the distance" here even though it's a
# natural phrasing -- CLOSE_KEYWORDS' "close the " is checked first (see item_intent_gates)
# and would swallow it as a "close" intent instead.
# "follow"/"go after" close on someone the same way -- there's no follow mechanic, so closing the
# distance is what the engine can do. Found by playtest: "i follow her at a respectful distance"
# was not understood. (A "<verb> toward" phrasing is TOWARD_PATTERN's, below.)
ADVANCE_KEYWORDS = (
    "advance", "move closer", "approach", "move toward", "move in", "step closer", "follow", "go after",
)
# "head/walk/proceed (carefully) toward X": travel when X is a real destination (checked in
# classify() before the item pass), else advance (detect_item_intent). Found by playtest: "i'll
# proceed carefully toward the wyrmwatch" was not understood; "head toward the docks" already
# reached travel through the semantic router and must keep doing so.
TOWARD_PATTERN = re.compile(
    r"\b(?:head|walk|proceed|go|make (?:my|our) way|run|hurry|stride|creep|edge)(?:s|es|ed|ing)?"
    r"(?:\s+\w+ly)?\s+towards?\b"
)
RETREAT_KEYWORDS = ("retreat", "back away", "back off", "fall back", "step back", "withdraw", "move away")
MATCH = {
    "advance": IntentMatch(keywords=ADVANCE_KEYWORDS, patterns=(TOWARD_PATTERN,), exempt=True),
    "retreat": IntentMatch(keywords=RETREAT_KEYWORDS, exempt=True),
}


def resolve_advance_retreat(core, data, resolved):
    """!
    @brief Resolves "advance"/"retreat" (see DM_Movement.py's advance_or_retreat) -- shifts the
        player's own band toward/away from current_target by up to their own speed, snapping
        party formation back into place. An empty "moved" list, when nothing else is present
        to react, is itself a valid outcome, not a denial -- but advance_or_retreat returns
        None instead (a distinct sentinel) when the player's own mount is currently overloaded,
        denied here as reason "mount_overloaded".
    @param core The DMCore instance.
    @param data The item_interaction_detected payload ({intent, ...}).
    @param resolved The item_interaction_resolved publisher closure from
        DMCore._on_item_interaction_detected.
    """
    moved = core.advance_or_retreat(data.get("intent"))
    if moved is None:
        resolved(False, reason="mount_overloaded")
        return
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
    @param data The "item_interaction_resolved" payload ({intent, reason?, moved?, input}).
    @return The narration prompt.
    """
    intent = data.get("intent")
    if data.get("reason") == "mount_overloaded":
        return (
            f"The player tries to {intent}, but whatever they're riding/hitched to is "
            f"carrying more than it can bear and refuses to budge -- no roll involved.\n"
            f"Narrate a brief, in-character explanation in 1-2 sentences as the Game Master."
        )
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
