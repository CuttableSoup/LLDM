"""!
@file registry.py
@brief The free-standing intent registry (see CONTEXT.md's "Free-standing intent") -- the
    single manifest DM_Core.py's _on_item_interaction_detected and LLM_Core.py's
    generate_item_interaction_response both look up by intent string, rather than each keeping
    its own hand-synced if/elif ladder over the same eight intents. Adding a free-standing
    intent means adding one row here and one new sibling module -- never touching DM_Core.py or
    LLM_Core.py again. Item-named intents (examine/take/give/trade/use/equip/unequip/drop/open/
    close) are the other manifest, intents/item_named.py's ITEM_NAMED: they share real pre-condition
    logic (scene target resolution, the locked-container gate, _run_interact_program) that DM_Core.py
    keeps once, driven by each ItemIntent's own "gated"/"ground_aware" flags.
"""
from intents import advance_retreat, formation, hitch, lore_check, mount, rest, speak_language, travel
from intents.advance_retreat import narrate_advance_retreat, resolve_advance_retreat
from intents.formation import narrate_formation, resolve_formation
from intents.gesture import narrate_gesture, resolve_gesture
from intents.hitch import narrate_hitch, narrate_unhitch, resolve_hitch, resolve_unhitch
from intents.lore_check import narrate_lore_check, resolve_lore_check
from intents.mount import narrate_dismount, narrate_mount, resolve_dismount, resolve_mount
from intents.move import narrate_move, resolve_move
from intents.rest import narrate_rest, resolve_rest
from intents.speak_language import narrate_speak_language, resolve_speak_language
from intents.travel import narrate_travel, resolve_travel

# intent string -> (resolve(core, data, resolved), narrate(llm_core, data) -> str)
HANDLERS = {
    "advance": (resolve_advance_retreat, narrate_advance_retreat),
    "retreat": (resolve_advance_retreat, narrate_advance_retreat),
    "formation_behind": (resolve_formation, narrate_formation),
    "formation_abreast": (resolve_formation, narrate_formation),
    "speak_language": (resolve_speak_language, narrate_speak_language),
    "rest": (resolve_rest, narrate_rest),
    "move": (resolve_move, narrate_move),
    "travel": (resolve_travel, narrate_travel),
    "mount": (resolve_mount, narrate_mount),
    "dismount": (resolve_dismount, narrate_dismount),
    "hitch": (resolve_hitch, narrate_hitch),
    "unhitch": (resolve_unhitch, narrate_unhitch),
    "lore_check": (resolve_lore_check, narrate_lore_check),
    # Reached only by the adjudicator's "gesture" verdict (no IntentMatch, so not in MATCHES): a
    # turn-costing, diceless item-kind clause -- see intents/gesture.py.
    "gesture": (resolve_gesture, narrate_gesture),
}

# intent string -> IntentMatch, in the order the classifier gates them: lore_check ahead of the
# item-named intents, then the rest after them (formation ahead of advance -- "stand behind" is
# the more specific match -- and mount/hitch ahead of advance so "mount the horse" is never
# swallowed as one). nlp/Intent_Classification.py builds its keyword tables, semantic-router
# prototypes and exemption sets from this; adding a free-standing intent adds its IntentMatch to
# its own module's MATCH and the module to this tuple.
MATCHES = {}
for _module in (
    lore_check, formation, speak_language, rest, mount, hitch, advance_retreat, travel,
):
    MATCHES.update(_module.MATCH)

# Free-standing intents whose success puts the party somewhere new -- LLMCore refreshes the
# narrator's scene state from the resolved payload before narrating one (the single writer of
# that state; the intents' own narrate() only reads), so every later prompt in the new place stops
# citing the previous one's description.
ARRIVAL_INTENTS = frozenset({"move", "travel"})
