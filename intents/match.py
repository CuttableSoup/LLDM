"""!
@file match.py
@brief IntentMatch -- how the classifier (nlp/Intent_Classification.py) recognizes one
    free-standing intent. Pure data: the keyword phrases, any extra regexes, the semantic-router
    prototypes, and the few flags that change how the rest of the pipeline treats a match. Each
    intents/<name>.py declares one per intent it owns; intents/registry.py's MATCHES lists them
    in gate order, and the classifier builds its tables from that, so adding a free-standing
    intent never means editing the classifier's own tables.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class IntentMatch:
    """!
    @param keywords Phrases the keyword gate matches (word-boundary aware -- see _phrase_matches).
    @param patterns Extra compiled regexes searched in addition to keywords.
    @param prototypes Phrases for the semantic router (authored post-process_input: lowercased,
        no leading filler -- see INTENT_PROTOTYPES's own note). Empty means the intent isn't
        routed semantically.
    @param exempt Free in the turn pipeline: published immediately as its own
        item_interaction_detected and never counted as one of the turn's actions (the West End
        Games exceptions for movement and directing the party).
    @param question_blocked Never taken from a question by the semantic router -- the intents
        that move the player or the clock.
    @param before_items Gated ahead of the item-named intents (only lore_check -- its long
        phrases would otherwise lose to a shorter item verb inside them).
    @param ignore Regexes whose matches are blanked out of the text before the keyword gate runs, for a
        keyword that has a second meaning ("rest" is also what a hand does on a shoulder).
    @param item_pass False for an intent the keyword gate in detect_item_intent doesn't handle
        (travel has its own gate, checked ahead of item detection).
    """

    keywords: tuple = ()
    patterns: tuple = ()
    ignore: tuple = ()
    prototypes: tuple = ()
    exempt: bool = False
    question_blocked: bool = False
    before_items: bool = False
    item_pass: bool = True
