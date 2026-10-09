"""!
@file gesture.py
@brief "gesture" -- a wordless expressive act (a kiss, a bow, a dance), the one intent no keyword
    gate produces: IntentClassifier reaches it only through the adjudicator's "gesture" verdict
    (AdHoc_Generation.py's adjudicate_player_input), which also names its tone. Registered here
    like a free-standing intent so DMCore and LLMCore dispatch it with no branch of their own, but
    it is NOT exempt (it has no IntentMatch): it joins the turn as an item-kind clause, so it costs
    a turn slot and counts toward the multi-action penalty, while never rolling dice itself.
    Resolved by DM_Dialogue.py's _resolve_gesture_intent: only the target's attitude moves, through
    the setting's own [[attitude_event]] for the tone (rules.toml).
"""


def resolve_gesture(core, data, resolved):
    """!
    @brief Resolves "gesture" (see DM_Dialogue.py's _resolve_gesture_intent).
    @param core The DMCore instance.
    @param data The item_interaction_detected payload ({input, tone, ...}).
    @param resolved The item_interaction_resolved publisher closure from
        DMCore._on_item_interaction_detected.
    """
    core._resolve_gesture_intent(data.get("input"), data.get("tone"), resolved)


def narrate_gesture(llm_core, data):
    """!
    @brief Narrates "gesture" (see resolve_gesture). "target", "persona" and "attitude" are
        _resolve_gesture_intent's own real results, so the narrator describes the reaction of who
        is actually there, in keeping with how they actually feel -- never an invented one.
    @param llm_core The LLMCore instance -- unused; this intent narrates no ongoing scene
        grounding.
    @param data The "item_interaction_resolved" payload ({found, tone, target?, target_label?,
        persona?, attitude?, unwelcome?, reason?, input}).
    @return The narration prompt.
    """
    said = data.get("input", "")
    if not data.get("found"):
        who = data.get("target", "the person they meant")
        explanations = {
            "dead": f"{who} is dead and cannot react",
            "not_present": f"{who} is not here to receive it",
        }
        return (
            f"The player makes a wordless gesture (input: \"{said}\"), but "
            f"{explanations.get(data.get('reason'), 'there is no one to receive it')}.\n"
            f"Narrate the attempt in 1-2 sentences as the Game Master -- no roll was involved, "
            f"and no one reacts."
        )
    target = data.get("target")
    if not target:
        return (
            f"The player makes a wordless {data.get('tone')} gesture (input: \"{said}\") at no one "
            f"in particular -- no roll involved, nothing about the world changes.\n"
            f"Narrate it in 1-2 sentences as the Game Master. No one in the scene reacts unless "
            f"the scene already shows them watching."
        )
    welcome = (
        "It is unwelcome: they already feel too coldly toward the player for it to land well."
        if data.get("unwelcome") else "Let it land as their feelings toward the player would have it."
    )
    return (
        f"The player makes a wordless {data.get('tone')} gesture toward {data.get('target_label') or target} "
        f"(input: \"{said}\") -- no roll involved.\n"
        f"Who {data.get('target_label') or target} is: {data.get('persona')}\n"
        f"How they feel about the player now: {data.get('attitude')}\n{welcome}\n"
        f"Narrate the gesture and {data.get('target_label') or target}'s reaction in 2-3 sentences as "
        f"the Game Master. Describe their body language, with at most one short spoken line from "
        f"them. Do not speak for the player, and do not add injuries, items, or other people."
    )
