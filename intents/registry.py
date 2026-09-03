"""!
@file registry.py
@brief The free-standing intent registry (see CONTEXT.md's "Free-standing intent") -- the
    single manifest DM_Core.py's _on_item_interaction_detected and LLM_Core.py's
    generate_item_interaction_response both look up by intent string, rather than each keeping
    its own hand-synced if/elif ladder over the same eight intents. Adding a free-standing
    intent means adding one row here and one new sibling module -- never touching DM_Core.py or
    LLM_Core.py again. Item-named intents (examine/take/give/trade/use/equip/unequip/drop/open/
    close) are out of scope for this registry -- they share real pre-condition logic (scene
    target resolution, the locked-container gate, _run_interact_program) ahead of their own
    dispatch in DM_Core.py, which a per-intent split would only duplicate.
"""
from intents.advance_retreat import narrate_advance_retreat, resolve_advance_retreat
from intents.formation import narrate_formation, resolve_formation
from intents.hitch import narrate_hitch, narrate_unhitch, resolve_hitch, resolve_unhitch
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
}
