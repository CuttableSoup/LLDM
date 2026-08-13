"""!
@file AdHoc_Generation.py
@brief Pure, DMCore-independent ad hoc entity generation -- same "pure, entity-shape-agnostic"
    precedent NPC_Generation.py/Challenge_Rating.py already set. Two independent decisions, one
    per function, both via OpenAI-style function calling against LLM_Client.call_chat_completion
    (synchronous, raises on failure -- unlike LLM_Core.py's own async, never-raises
    fetch_from_llm; see LLM_Client.py's own module note): generate_ad_hoc_item conjures a
    plausible physical object into the scene (ex: a stone the player tries to pick up that was
    never authored in any Rules/Fantasy/*.toml file); decide_entity_removal decides whether a
    player's message to ADaM is asking for something to be removed from the scene entirely, and
    if so, which currently-real entity it names. DM_Improvisation.py is the DMCore-touching glue
    that calls these and actually mutates live game state -- the same split DM_NpcGeneration.py
    is to NPC_Generation.py.

    Unlike NPC_Generation.py (which always needs *some* result, so it falls back to a random
    offline pick on any failure), both functions here default to declining on any failure
    (network error, malformed response, no tool call, timeout) -- never fabricating an item or
    a removal when the LLM is unreachable. This is also why the timeout here (8s, see
    DEFAULT_TIMEOUT) is tighter than NPC generation's 20s default: an ad hoc item can be
    triggered on any unmatched item verb during ordinary play, far more often than NPC
    generation's handful-of-times-per-scene-load pattern, so a bounded, tighter budget matters
    more here.
"""

import json

from LLM_Client import call_chat_completion as _real_call_chat_completion

DEFAULT_API_URL = "http://127.0.0.1:1234/v1/chat/completions"
DEFAULT_TIMEOUT = 8

# Tags already in real use across Rules/Fantasy/*.toml (creatures.toml/items.toml's own
# damage_tags/resistance_tags/vulnerability_tags) -- enum-constraining the LLM's own tool
# arguments to this fixed, real set is far more reliable with a small local model than free
# text, the same reliability win NPC_Generation.py's own _build_tool_schema already documents
# for its "keywords" field.
DAMAGE_TAGS = ("slashing", "piercing", "bludgeoning", "fire", "cold", "poison", "physical")

# Deliberately excludes "container"/"trap" -- those need [entity.test]/locked-condition data
# this schema has no way to author, so an ad hoc "container"/"trap" would silently behave like
# an inert prop rather than a real one. "misc" is the catch-all for anything that doesn't
# cleanly fit the other five (ex: a plain stone).
ITEM_SUBTYPES = ("weapon", "armor", "potion", "tool", "trinket", "misc")


def _decline_tool_schema(description):
    """!
    @brief The shared "decline" function both tool schemas below offer alongside their own
        primary function -- the model's own escape hatch for an implausible request, letting
        tool_choice="auto" pick between "do it" and "don't" rather than forcing a create/remove
        call regardless of plausibility.
    @param description Function-specific guidance on when to pick this over the primary one.
    @return One OpenAI-style function-schema dict.
    """
    return {
        "type": "function",
        "function": {
            "name": "decline",
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    }


def _build_item_tool_schema(valid_equip_slots):
    """!
    @brief The OpenAI-style "tools" payload for generate_ad_hoc_item's own tool call:
        create_item (enum-constrained subtype/location/damage_tag/equip_slot fields, plus
        optional usable/healing/poison fields for a consumable -- see the field's own
        docstring) or decline.
    @param valid_equip_slots The equipping entity's own real slot names (ex:
        self.get_equip_slots(self.player_name), DM_Rules.py) -- enum-constrains "equip_slot" to
        real, valid slots instead of free text the item would then just fail to equip with
        later; an empty/falsy value leaves "equip_slot" unconstrained free text instead (still
        validated by the caller against real slots before ever being attached to an entity).
    @return The "tools" list for call_chat_completion.
    """
    equip_slot_schema = {"type": "string"}
    if valid_equip_slots:
        equip_slot_schema["enum"] = list(valid_equip_slots)

    return [
        {
            "type": "function",
            "function": {
                "name": "create_item",
                "description": (
                    "Conjure a physical object into the scene, if the player's request is "
                    "reasonable for this setting and scene. Keep it simple and grounded unless "
                    "the scene clearly calls for something more capable (a weapon, armor, a "
                    "magical trinket). For a usable/consumable item (a potion, a strange "
                    "mushroom, an unlabeled vial, ...), don't default to it always being "
                    "beneficial -- for balance, a plausible fraction of improvised consumables "
                    "should be marked poisonous instead of healing, especially for a vague or "
                    "risky-sounding request (ex: drinking an unidentified liquid found in a "
                    "dungeon)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "subtype": {"type": "string", "enum": list(ITEM_SUBTYPES)},
                        "location": {
                            "type": "string",
                            "enum": ["ground", "inventory"],
                            "description": (
                                "'ground' if it's a physical object the player would need to "
                                "pick up; 'inventory' only if it's something the character "
                                "would plausibly already be carrying or wearing."
                            ),
                        },
                        "value": {"type": "integer", "description": "Approximate currency value; 0 for a worthless trinket."},
                        "is_weapon": {"type": "boolean"},
                        "damage_dice": {"type": "integer"},
                        "damage_pips": {"type": "integer"},
                        "damage_tag": {"type": "string", "enum": list(DAMAGE_TAGS)},
                        "is_armor": {"type": "boolean"},
                        "armor_dice": {"type": "integer"},
                        "armor_pips": {"type": "integer"},
                        "equip_slot": equip_slot_schema,
                        "usable": {
                            "type": "boolean",
                            "description": "Whether this item can be used/drunk/activated at all (ex: a potion).",
                        },
                        "is_healing": {"type": "boolean"},
                        "healing_dice": {"type": "integer"},
                        "healing_pips": {"type": "integer"},
                        "is_poisonous": {
                            "type": "boolean",
                            "description": "Marks this usable item as harmful instead of beneficial when used.",
                        },
                        "poison_dice": {"type": "integer"},
                        "poison_pips": {"type": "integer"},
                    },
                    "required": ["name", "description", "subtype", "location"],
                },
            },
        },
        _decline_tool_schema("Use this instead if the requested object doesn't make sense here."),
    ]


def _build_removal_tool_schema(removable_entities):
    """!
    @brief The OpenAI-style "tools" payload for decide_entity_removal's own tool call:
        remove_entity (name enum-constrained to removable_entities) or decline. Constraining
        "name" to an enum of real, currently-present names is what makes it structurally
        impossible for the model to name something that doesn't exist (or, since the caller
        excludes it, the player themself) -- not just a runtime check after the fact.
    @param removable_entities The real, currently-valid entity names the model may choose from.
    @return The "tools" list for call_chat_completion.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "remove_entity",
                "description": "Remove something from the scene entirely, if the player's request is reasonable.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": list(removable_entities)},
                        "reason": {"type": "string"},
                    },
                    "required": ["name", "reason"],
                },
            },
        },
        _decline_tool_schema("Use this instead if nothing here should actually be removed."),
    ]


def _extract_tool_call(response):
    """!
    @brief Shared response-parsing for both functions below -- pulls the function name and
        parsed arguments out of a raw call_chat_completion response.
    @param response The parsed JSON response body from call_chat_completion.
    @return (function_name, arguments_dict).
    @raises Exception on any malformed/missing shape -- callers catch broadly, same convention
            NPC_Generation.py's own generate_npc_stats already follows.
    """
    tool_call = response["choices"][0]["message"]["tool_calls"][0]
    arguments = json.loads(tool_call["function"]["arguments"])
    return tool_call["function"]["name"], arguments


def generate_ad_hoc_item(
    phrase, intent, scene_description, valid_equip_slots=None,
    call_chat_completion=None, api_url=DEFAULT_API_URL, timeout=DEFAULT_TIMEOUT,
):
    """!
    @brief Asks the local LLM whether phrase (the player's own clause, ex: "pick up a stone")
        describes something plausible to conjure into the current scene, and if so, what it is.
    @param phrase The player's own raw clause naming the item (ex: "the stone").
    @param intent The item-interaction verb that triggered this (ex: "take", "examine") --
        folded into the prompt for context; the caller is responsible for actually resolving
        this same intent against the created entity afterward (see DM_Improvisation.py).
    @param scene_description The current scene's own description (DM_Rules.py's
        _current_scene_description), grounding what's plausible here.
    @param valid_equip_slots Forwarded to _build_item_tool_schema.
    @param call_chat_completion The LLM-calling callable to use -- None (the default) resolves
        to this module's own _real_call_chat_completion *at call time*, the same
        patch("AdHoc_Generation._real_call_chat_completion", fake) seam NPC_Generation.py
        established.
    @param api_url/timeout Forwarded to call_chat_completion.
    @return {"created": False, "reason": str} on decline or any failure (network error,
            malformed response, no tool call, an incomplete create_item call) -- this function
            never raises and never fabricates an item when the LLM is unreachable. On success:
            {"created": True, "entity": {full entity dict, "ad_hoc": True}, "location":
            "ground"|"inventory"}.
    """
    call_chat_completion = call_chat_completion or _real_call_chat_completion
    prompt = (
        f"The player, in a tabletop RPG scene, tries to \"{intent}\" something described as: "
        f"\"{phrase}\". Current scene: {scene_description or 'unknown'}.\n"
        "If it's plausible this object could be improvised into the scene, call create_item "
        "with its details. Otherwise call decline."
    )
    messages = [
        {
            "role": "system",
            "content": "You are helping a Game Master improvise physical objects into a live tabletop RPG scene.",
        },
        {"role": "user", "content": prompt},
    ]

    try:
        response = call_chat_completion(
            api_url, messages, tools=_build_item_tool_schema(valid_equip_slots),
            tool_choice="auto", timeout=timeout,
        )
        function_name, arguments = _extract_tool_call(response)
    except Exception:
        return {"created": False, "reason": "unavailable"}

    if function_name != "create_item":
        reason = arguments.get("reason", "declined") if isinstance(arguments, dict) else "declined"
        return {"created": False, "reason": reason}

    name = arguments.get("name")
    description = arguments.get("description")
    if not name or not description:
        return {"created": False, "reason": "incomplete"}

    subtype = arguments.get("subtype") if arguments.get("subtype") in ITEM_SUBTYPES else "misc"
    location = arguments.get("location") if arguments.get("location") in ("ground", "inventory") else "ground"

    entity = {
        "name": name,
        "supertype": "object",
        "subtype": subtype,
        "description": description,
        "value": int(arguments.get("value") or 0),
        # Tags this as having no static TOML template to re-derive from on a reload -- see
        # DM_Persistence.py's save_game/load_game, which save/restore the full dict for any
        # entity carrying this flag rather than the ordinary hp/inventory/etc. diff.
        "ad_hoc": True,
    }
    if arguments.get("is_weapon"):
        entity["damage_value"] = {
            "dice": int(arguments.get("damage_dice") or 0),
            "pips": int(arguments.get("damage_pips") or 0),
            "bonus": 0,
        }
        if arguments.get("damage_tag") in DAMAGE_TAGS:
            entity["damage_tags"] = [arguments["damage_tag"]]
    if arguments.get("is_armor"):
        entity["armor_value"] = {
            "dice": int(arguments.get("armor_dice") or 0),
            "pips": int(arguments.get("armor_pips") or 0),
        }
    equip_slot = arguments.get("equip_slot")
    if equip_slot and valid_equip_slots and equip_slot in valid_equip_slots:
        entity["equip_slot"] = equip_slot

    if arguments.get("usable"):
        entity["usable"] = True
        skills = {}
        if arguments.get("is_healing"):
            skills["healing"] = {
                "dice": int(arguments.get("healing_dice") or 0),
                "pips": int(arguments.get("healing_pips") or 0),
            }
        if arguments.get("is_poisonous"):
            skills["poison"] = {
                "dice": int(arguments.get("poison_dice") or 0),
                "pips": int(arguments.get("poison_pips") or 0),
            }
        if skills:
            entity["skills"] = skills

    return {"created": True, "entity": entity, "location": location}


def decide_entity_removal(
    phrase, scene_description, removable_entities,
    call_chat_completion=None, api_url=DEFAULT_API_URL, timeout=DEFAULT_TIMEOUT,
):
    """!
    @brief Asks the local LLM whether phrase (the player's own message to ADaM) is asking for
        something to be removed from the scene, and if so, which of removable_entities it names.
    @param phrase The player's own raw message (ex: "get rid of that torch").
    @param scene_description The current scene's own description, for context.
    @param removable_entities Every real, currently-valid name the model may choose from (built
        by the caller -- DM_Improvisation.py's _attempt_entity_removal -- deliberately
        *excluding* the player's own name, on top of the runtime guard
        remove_entity_from_scene itself also enforces). An empty list short-circuits to a
        decline with no LLM call at all -- nothing here could plausibly be removed.
    @param call_chat_completion/api_url/timeout See generate_ad_hoc_item's own docstring.
    @return {"removed": False, "reason": str} on decline, an empty removable_entities, or any
            failure -- never raises. On success: {"removed": True, "name", "reason"}.
    """
    if not removable_entities:
        return {"removed": False, "reason": "nothing_here"}

    call_chat_completion = call_chat_completion or _real_call_chat_completion
    prompt = (
        f"The player, addressing the Game Master directly, says: \"{phrase}\". Current scene: "
        f"{scene_description or 'unknown'}. Currently present/available to remove: "
        f"{', '.join(removable_entities)}.\n"
        "If they're clearly asking for one of these to be removed/destroyed/dismissed from the "
        "scene entirely, call remove_entity naming exactly one of them. Otherwise call decline."
    )
    messages = [
        {
            "role": "system",
            "content": "You are helping a Game Master decide whether to remove something from a live tabletop RPG scene.",
        },
        {"role": "user", "content": prompt},
    ]

    try:
        response = call_chat_completion(
            api_url, messages, tools=_build_removal_tool_schema(removable_entities),
            tool_choice="auto", timeout=timeout,
        )
        function_name, arguments = _extract_tool_call(response)
    except Exception:
        return {"removed": False, "reason": "unavailable"}

    if function_name != "remove_entity":
        reason = arguments.get("reason", "declined") if isinstance(arguments, dict) else "declined"
        return {"removed": False, "reason": reason}

    name = arguments.get("name")
    if name not in removable_entities:
        return {"removed": False, "reason": "invalid_target"}

    return {"removed": True, "name": name, "reason": arguments.get("reason", "")}
