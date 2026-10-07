"""!
@file Item_Outcome.py
@brief The item interaction outcome (see CONTEXT.md): the one payload narration receives for a
    diceless intent's result, however the intent resolved -- a direct turn
    (DMCore._on_item_interaction_detected), a resumed downtime (DMCore._resume_pending_downtime)
    or an improvised beat (DM_Improvisation.py). DMCore._publish_item_interaction is the only
    publisher; this builds what it publishes, so the common fields exist in the same shape on
    every path (events/schemas.py's ItemInteractionResolved documents them).
"""

# Amounts the narrator must be able to speak in the setting's own coins -- each gets a
# "<key>_text" twin, so LLMCore never has to know the setting's denominations.
CURRENCY_KEYS = ("price", "amount")


def build_item_interaction_outcome(
    intent, item_name, input_text, found, present_entities, quiet=False, phrase=None,
    format_currency=None, **extra,
):
    """!
    @brief Builds one item_interaction_resolved payload.
    @param intent The resolved intent (an item-named intent, a free-standing one, or the
        "travel"/"rest" of a resumed downtime).
    @param item_name The item the intent named, or None.
    @param input_text The player's own words ("" for a resumed downtime, which has none).
    @param found Whether the intent succeeded; a miss carries a "reason" in extra.
    @param present_entities Who is present now -- read fresh by the publisher, since a "move"
        has already changed the roster by the time this narration fires.
    @param quiet True when the clause shares its turn with real dialogue and narration should
        skip it (see IntentClassifier.classify).
    @param phrase The player's own words for the item, which a "not here" notice quotes.
    @param format_currency Callable spelling an amount in the setting's coins, or None.
    @param extra Whatever the intent adds (reason, container, price, direction, ...); wins over
        a common field of the same name.
    @return The payload dict.
    """
    extra = dict(extra)
    if format_currency:
        for key in CURRENCY_KEYS:
            if key in extra:
                extra[f"{key}_text"] = format_currency(extra[key])
    return {
        "intent": intent, "item_name": item_name, "input": input_text, "found": found,
        "present_entities": list(present_entities), "quiet": quiet, "phrase": phrase,
        **extra,
    }
