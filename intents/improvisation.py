"""!
@file improvisation.py
@brief The item-interaction verbs eligible for DM_Improvisation.py's ad hoc creation fallback,
    partitioned by whose inventory the created item has to land in. Plain constants with no
    DMCore or LLM dependency, so nlp/Intent_Classification.py and dm/DM_Improvisation.py can both
    import them without either picking up the other's coupling.
"""

# The item-interaction verbs eligible for the ad hoc creation fallback, partitioned by which
# entity's own inventory a created item actually needs to land in for DM_Core.py's ordinary,
# unchanged item-interaction dispatcher to resolve the *original* triggering intent correctly
# (see DM_Improvisation.py's own _on_improvisation_requested -- "give"/"equip"/"unequip"/"use"/
# "drop" always check the player's own inventory regardless of direction; "trade" is the one
# intent that checks the *current target's* inventory as its source, since buying something
# means the seller has to have it, not the buyer). Intent_Classification.py
# computes its own IMPROVISABLE_INTENTS as the union of these three, rather than hand-copying a
# fourth constant, so the two can't drift apart.
PLAYER_CENTRIC_INTENTS = frozenset({"give", "equip", "unequip", "use", "drop"})
GROUND_AWARE_INTENTS = frozenset({"examine", "take"})
TARGET_CENTRIC_INTENTS = frozenset({"trade"})
