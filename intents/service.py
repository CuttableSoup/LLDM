"""!
@file service.py
@brief "service" -- a priced service someone present offers (`[[entity.service]]`, docs/services.md),
    bought through the "trade" intent and resolved by DM_Services.py's own _buy_service from the
    improvisation seam, so it is registered here for narration only and has no IntentMatch.

    What a purchase may narrate is a choice the player makes (LLMCore.content_level, set by the
    CLI's --content or the GUI's Content menu): "fade" cuts away after the agreement, "explicit"
    narrates it plainly. Either way every character involved is an adult, and the narrator is told
    to stop rather than continue if that is ever in doubt -- an instruction in the prompt, because
    nothing in the data can know it.
"""

# What each level tells the narrator to do with a service tagged content = "sexual".
CONTENT_LEVELS = {
    "fade": (
        "Narrate the agreement and the payment, then cut away with a time skip or a closed door -- "
        "nothing that happens after is described."
    ),
    "explicit": (
        "Narrate it plainly and in full, without euphemism or cutting away, in keeping with the tone "
        "of the scene."
    ),
}
DEFAULT_CONTENT_LEVEL = "fade"
# Said at every level, to every sexual service.
ADULTS_ONLY = (
    "Every character involved is an adult who has agreed to this. If anyone in the scene is or might "
    "be a minor, do not narrate it at all -- cut away immediately."
)


def resolve_service(core, data, resolved):
    """!
    @brief Resolves "service" from a free-standing dispatch (nothing keyword-gates it today -- DM_Services.py
        publishes the result itself from the improvisation seam), by trying the whole input as a purchase.
    @param core The DMCore instance.
    @param data The item_interaction_detected payload ({input, phrase?, ...}).
    @param resolved The item_interaction_resolved publisher closure (unused: a purchase publishes its own).
    """
    if not core._try_service_purchase(data.get("phrase") or "", data.get("input") or ""):
        resolved(False, reason="no_such_service")


def narrate_service(llm_core, data):
    """!
    @brief Narrates "service" (see DM_Services.py's _buy_service). Price, provider and what changed
        are the engine's own results, never invented -- the prompt says so, since a narrator left
        to itself quotes a different figure than the one that was charged.
    @param llm_core The LLMCore instance, read for its content_level; None means "fade".
    @param data The "item_interaction_resolved" payload ({found, service, provider_label, price_text,
        content, joined, time?, healed?, reason?, input}).
    @return The narration prompt.
    """
    provider = data.get("provider_label") or data.get("provider") or "them"
    service = data.get("service") or "their services"
    price = data.get("price_text") or "the agreed price"
    if not data.get("found"):
        reasons = {
            "cant_afford": f"the player cannot pay {price}",
            "too_cold": f"{provider} will not deal with the player, feeling as they do toward them",
            "hostile": f"{provider} is hostile to the player and will not sell anything",
            "enemies_near": "there are enemies about, and this is no time for it",
            "already_joined": f"{provider} is already travelling with the player",
            "no_such_service": "nobody here offers that",
            "no_route": f"{provider} cannot take the player from here to there",
            "already_there": "the player is already there",
            "downtime_interrupted": "the player's journey is still unfinished",
        }
        return (
            f"The player tries to buy \"{service}\" from {provider} (input: \"{data.get('input', '')}\"), but "
            f"{reasons.get(data.get('reason'), 'it falls through')}. No roll was involved.\n"
            f"Narrate that in 1-2 sentences as the Game Master. State the price as {price} if it comes up; "
            f"never a different figure, and do not add anything the player did not do."
        )
    facts = [f"The player pays {provider} {price} for \"{service}\"."]
    if data.get("joined"):
        facts.append(f"{provider} now travels with the player's party.")
    if data.get("travelled"):
        facts.append(
            f"{provider} carries the player to {data.get('location_name') or 'their destination'} "
            f"({data.get('location_description') or 'no description'}), arriving after "
            f"{data.get('blocks_spent', 1)} block(s) on the road."
        )
    elif data.get("interrupted"):
        facts.append("The journey sets out, but is cut short on the road before it can arrive.")
    if data.get("time") and not data.get("travelled"):
        facts.append("Time passes.")
    if data.get("healed"):
        facts.append("The party is rested and recovers.")
    level = getattr(llm_core, "content_level", None) or DEFAULT_CONTENT_LEVEL
    tail = ""
    if data.get("content") == "sexual":
        tail = f"\n{CONTENT_LEVELS.get(level, CONTENT_LEVELS[DEFAULT_CONTENT_LEVEL])} {ADULTS_ONLY}"
    return (
        " ".join(facts)
        + f"\nNarrate this in 2-4 sentences as the Game Master. The price is exactly {price} -- never a different "
        f"figure. No roll was involved.{tail}"
    )
