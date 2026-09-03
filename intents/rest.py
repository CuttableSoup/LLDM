"""!
@file rest.py
@brief "rest" -- a free-standing intent (see CONTEXT.md's "Free-standing intent"). Spends
    block-clock time via DM_Time.py's own rest, healing the party; unrelated to the scene
    target or the locked-container gate, unlike every item-named intent. See docs/downtime.md.
"""


def resolve_rest(core, data, resolved):
    """!
    @brief Resolves "rest" (see DM_Time.py's rest) -- NLPCore only recognizes that resting was
        requested at all; how long is decided here from the raw input itself, the same
        "DMCore resolves the specifics" split every other free-standing intent follows: a
        phrase naming night/dawn/morning spends a whole day's worth of blocks, anything else
        spends a single block.

        A hostile mid-block encounter can now pause the rest partway through (see
        docs/downtime.md's "Pausing for a fight") -- core.rest returns {"interrupted": True}
        either way that happens (a fresh pause this call, or an outright denial because a
        previously-paused downtime is still unresolved), and this simply publishes nothing
        this turn: the encounter's own existing "encounter_triggered" narration (or, for an
        outright denial, nothing new at all -- the player already knows why, from whatever
        made the earlier one pause) already covers it, and the eventual arrival/healing
        narration fires later, on its own, once DMCore._resume_pending_downtime completes it.
    @param core The DMCore instance.
    @param data The item_interaction_detected payload ({input, ...}).
    @param resolved The item_interaction_resolved publisher closure from
        DMCore._on_item_interaction_detected.
    """
    overnight_markers = ("night", "dawn", "morning")
    input_text = data.get("input")
    blocks_spent = (
        core.get_time_state()["blocks_per_day"]
        if any(marker in (input_text or "") for marker in overnight_markers)
        else 1
    )
    result = core.rest(blocks_spent)
    if result["interrupted"]:
        return
    resolved(True, healed=result["healed"], blocks_spent=result["blocks_spent"], time=result["time"])


def narrate_rest(llm_core, data):
    """!
    @brief Narrates "rest" (see resolve_rest). "healed"/"blocks_spent"/"time" are DMCore.rest's
        own real results (DM_Time.py), never invented. "healed" maps party member name ->
        {healed, remaining_hp}; an entity that rolled 0 (ex: no fortitude trained) still
        appears, so this doesn't claim recovery that didn't happen.
    @param llm_core The LLMCore instance -- unused; this intent narrates no ongoing scene
        grounding.
    @param data The "item_interaction_resolved" payload ({healed, blocks_spent, time}).
    @return The narration prompt.
    """
    healed = data.get("healed") or {}
    time_state = data.get("time") or {}
    if healed:
        healed_text = "; ".join(
            f"{name} recovers {info.get('healed', 0)} HP (now at {info.get('remaining_hp', 0)} HP)"
            for name, info in healed.items()
        )
    else:
        healed_text = "no one recovers any HP"
    time_of_day = "day" if time_state.get("is_day", True) else "night"
    return (
        f"The party rests for {data.get('blocks_spent', 1)} block(s): {healed_text}. "
        f"It's now {time_of_day} on day {time_state.get('day', 0)}.\n"
        f"Narrate this brief rest in 1-2 sentences as the Game Master -- no roll was "
        f"visible to the player, just its outcome."
    )
