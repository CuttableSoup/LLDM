"""!
@file lore_check.py
@brief "lore_check" -- a free-standing intent (see CONTEXT.md's "Free-standing intent").
    Recalls a currently-present creature's own weaknesses/abilities via DM_Combat.py's own
    _resolve_lore_check_intent -- the Pathfinder Knowledge-skill shape (see
    docs/extended-goals.md's "Knowledge checks revealing monster lore"). Unrelated to the scene
    target or the locked-container gate, unlike every item-named intent. The one free-standing
    intent that actually rolls dice -- exempted from the ordinary turn pipeline by deliberate
    design (see Intent_Classification.py's own EXEMPT_ITEM_INTENTS comment), not because it's
    diceless the way every other member here is.
"""


def resolve_lore_check(core, data, resolved):
    """!
    @brief Resolves "lore_check" (see DM_Combat.py's _resolve_lore_check_intent).
    @param core The DMCore instance.
    @param data The item_interaction_detected payload ({input, ...}).
    @param resolved The item_interaction_resolved publisher closure from
        DMCore._on_item_interaction_detected.
    """
    core._resolve_lore_check_intent(data.get("input"), resolved)


def narrate_lore_check(llm_core, data):
    """!
    @brief Narrates "lore_check" (see resolve_lore_check). "target"/"skill"/"revealed" are
        _resolve_lore_check_intent's own real results, never invented -- "revealed" is the
        target's own resistance_tags/immunity_tags/vulnerability_tags/damage_tags, the same
        data already driving its combat math, not separately hand-authored lore text.
    @param llm_core The LLMCore instance -- unused; this intent narrates no ongoing scene
        grounding.
    @param data The "item_interaction_resolved" payload ({found, reason?, target?, skill?,
        revealed?, input}).
    @return The narration prompt.
    """
    if not data.get("found"):
        reason = data.get("reason")
        explanations = {
            "not_present": "nothing here matches what they're trying to recall anything about",
            "no_lore_available": (
                f"nothing comes to mind about {data.get('target', 'it')} in particular -- "
                f"it's not the kind of thing this training covers"
            ),
            "check_failed": f"they rack their brain about {data.get('target', 'it')}, but nothing useful surfaces",
        }
        explanation = explanations.get(reason, "they can't recall anything useful")
        return (
            f"The player tries to recall what they know about a creature "
            f"(input: \"{data.get('input', '')}\"), but {explanation}.\n"
            f"Narrate a brief, in-character moment of failed or unavailable recollection in 1-2 "
            f"sentences as the Game Master -- no new information is revealed."
        )
    revealed = data.get("revealed") or []
    target = data.get("target")
    if revealed:
        tag_text = ", ".join(revealed)
        recollection = f"they recall the following about {target}: {tag_text}"
    else:
        recollection = f"they rack their brain about {target}, but nothing noteworthy comes to mind"
    return (
        f"The player succeeds at recalling what they know about {target} using their "
        f"{data.get('skill')} training -- {recollection}.\n"
        f"Narrate this brief moment of recollection in 1-2 sentences as the Game Master, "
        f"in-character, translating any listed traits into a natural description of what the "
        f"character remembers (ex: a \"fire\" vulnerability becomes recalling it burns easily)."
    )
