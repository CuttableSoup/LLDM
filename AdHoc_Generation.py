"""!
@file AdHoc_Generation.py
@brief Pure, DMCore-independent ad hoc entity generation -- same "pure, entity-shape-agnostic"
    precedent NPC_Generation.py/Challenge_Rating.py already set. Independent decisions, each via
    OpenAI-style function calling against LLM_Client.call_chat_completion (synchronous, raises
    on failure -- unlike LLM_Core.py's own async, never-raises fetch_from_llm; see
    LLM_Client.py's own module note): generate_ad_hoc_item conjures a plausible physical object
    into the scene (ex: a stone the player tries to pick up that was never authored in any
    Rules/Fantasy/*.toml file -- including, now, a locked/lockable container or an armed trap,
    or a third "scenery" outcome for ambient detail that isn't a discrete object at all);
    generate_ad_hoc_creature conjures a living creature/NPC, stat-fit to a target challenge
    rating via NPC_Generation.py's own fit_skills_to_cr; decide_entity_removal decides whether a
    player's message to ADaM is asking for something to be removed from the scene entirely, and
    if so, which currently-real entity it names; decide_entity_edit decides whether a player's
    message to ADaM is asking for an existing entity's description or a condition on it to
    change. DM_Improvisation.py is the DMCore-touching glue that calls these and actually
    mutates live game state -- the same split DM_NpcGeneration.py is to NPC_Generation.py.

    Unlike NPC_Generation.py (which always needs *some* result, so it falls back to a random
    offline pick on any failure), every function here defaults to declining on any failure
    (network error, malformed response, no tool call, timeout) -- never fabricating an item,
    creature, removal, or edit when the LLM is unreachable. This is also why the timeout here
    (8s, see DEFAULT_TIMEOUT) is tighter than NPC generation's 20s default: an ad hoc item can be
    triggered on any unmatched item verb during ordinary play, far more often than NPC
    generation's handful-of-times-per-scene-load pattern, so a bounded, tighter budget matters
    more here.
"""

import json
import random

from LLM_Client import call_chat_completion as _real_call_chat_completion
from NPC_Generation import fit_skills_to_cr

DEFAULT_API_URL = "http://127.0.0.1:1234/v1/chat/completions"
DEFAULT_TIMEOUT = 8

# Tags already in real use across Rules/Fantasy/*.toml (creatures.toml/items.toml's own
# damage_tags/resistance_tags/vulnerability_tags) -- enum-constraining the LLM's own tool
# arguments to this fixed, real set is far more reliable with a small local model than free
# text, the same reliability win NPC_Generation.py's own _build_tool_schema already documents
# for its "keywords" field.
DAMAGE_TAGS = ("slashing", "piercing", "bludgeoning", "fire", "cold", "poison", "physical")

# "misc" is the catch-all for anything that doesn't cleanly fit the other six (ex: a plain
# stone). "container"/"trap" additionally carry their own [entity.test]/condition setup, built
# below in generate_ad_hoc_item's own post-processing -- a minimal, LLM-authorable subset of
# the same shape items.toml's own chest/dart trap already use (see CLAUDE.md's "Entity tests").
ITEM_SUBTYPES = ("weapon", "armor", "potion", "tool", "trinket", "misc", "container", "trap")

# generate_ad_hoc_creature's own disposition->attitude-default mapping. "hostile" is exactly
# -100 -- the precise threshold DM_Social.py's is_hostile requires for real combat (a lesser
# negative value, ex: -40, reads as merely wary -- dialogue, not combat).
CREATURE_DISPOSITIONS = ("hostile", "wary", "neutral", "friendly")
DISPOSITION_VALUES = {"hostile": -100, "wary": -40, "neutral": 0, "friendly": 60}

# A flat multiplier on the resolved target challenge rating, applied the same way
# NPC_Generation.py's own cr_multiplier is -- lets the model ask for a deliberately tougher or
# weaker conjured creature without needing its own numeric CR field (small local models are far
# more reliable picking an enum than inventing a number -- same reasoning every other
# enum-constrained field in this module already follows).
CREATURE_POWERS = ("weak", "moderate", "strong")
POWER_MULTIPLIERS = {"weak": 0.4, "moderate": 1.0, "strong": 2.0}


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


def _build_item_tool_schema(valid_equip_slots, valid_skill_names=None):
    """!
    @brief The OpenAI-style "tools" payload for generate_ad_hoc_item's own tool call:
        create_item (enum-constrained subtype/location/damage_tag/equip_slot fields, plus
        optional usable/healing/poison fields for a consumable, and optional locked/disarm
        fields for a container/trap -- see the field's own docstring), describe_scenery (for
        ambient detail that isn't a discrete object at all -- see its own description below),
        or decline.
    @param valid_equip_slots The equipping entity's own real slot names (ex:
        self.get_equip_slots(self.player_name), DM_Rules.py) -- enum-constrains "equip_slot" to
        real, valid slots instead of free text the item would then just fail to equip with
        later; an empty/falsy value leaves "equip_slot" unconstrained free text instead (still
        validated by the caller against real slots before ever being attached to an entity).
    @param valid_skill_names The real skill catalog (ex: self.skills.keys(), DM_Core.py) --
        enum-constrains "lock_skill"/"disarm_skill" to real skill names the same way
        valid_equip_slots does for "equip_slot"; an empty/falsy value leaves them unconstrained
        free text instead (still validated by the caller before ever being attached to an
        entity).
    @return The "tools" list for call_chat_completion.
    """
    equip_slot_schema = {"type": "string"}
    if valid_equip_slots:
        equip_slot_schema["enum"] = list(valid_equip_slots)

    skill_schema = {"type": "string"}
    if valid_skill_names:
        skill_schema["enum"] = list(valid_skill_names)

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
                    "dungeon). If the request instead describes ambient scenery/detail rather "
                    "than a discrete object (writing on a wall, an odor, the room's layout), "
                    "call describe_scenery instead."
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
                                "would plausibly already be carrying or wearing. Ignored for a "
                                "container/trap, which is always placed in the scene."
                            ),
                        },
                        "value": {"type": "integer", "description": "Approximate currency value; 0 for a worthless trinket."},
                        "is_weapon": {"type": "boolean"},
                        "damage_dice": {"type": "integer"},
                        "damage_pips": {"type": "integer"},
                        "damage_tag": {
                            "type": "string", "enum": list(DAMAGE_TAGS),
                            "description": "Also used as a trap's own fail-damage tag when subtype is 'trap'.",
                        },
                        "is_armor": {"type": "boolean"},
                        "armor_dice": {"type": "integer"},
                        "armor_pips": {"type": "integer"},
                        "equip_slot": equip_slot_schema,
                        "locked": {
                            "type": "boolean",
                            "description": "Only for subtype 'container' -- whether it starts locked shut.",
                        },
                        "lock_skill": skill_schema,
                        "lock_difficulty": {"type": "integer", "description": "Target number to pick the lock; a moderate lock is around 10-12."},
                        "contains_currency": {"type": "integer", "description": "Only for subtype 'container' -- how much currency is inside, 0 for none."},
                        "disarm_skill": skill_schema,
                        "disarm_difficulty": {"type": "integer", "description": "Only for subtype 'trap' -- target number to disarm/avoid it, around 8-10."},
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
        {
            "type": "function",
            "function": {
                "name": "describe_scenery",
                "description": (
                    "Use this instead of create_item when the phrase names ambient scenery or "
                    "detail rather than a discrete, self-contained object worth tracking as an "
                    "item (ex: writing on a wall, an odor, the general layout of the room)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"description": {"type": "string"}},
                    "required": ["description"],
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


def _resolve_test_skill(requested_skill, valid_skill_names, fallback="finesse"):
    """!
    @brief Picks the skill a conjured container/trap's own [entity.test] should gate on --
        the model's own requested_skill if it's real, else fallback (used for both a chest's
        lock and a trap's disarm check, same as items.toml's own hand-authored examples), else
        None if even the fallback isn't a real skill in this setting (ex: a setting with no
        "finesse" skill at all -- see CLAUDE.md's "every setting authors its own skills from
        scratch"). None is what keeps a conjured container/trap from ever landing permanently
        unopenable/undisarmable just because the model picked (or omitted) an invalid skill.
    @param requested_skill The model's own chosen skill name, or None.
    @param valid_skill_names The real skill catalog (ex: self.skills.keys()), or None/empty.
    @param fallback The skill to fall back to if requested_skill isn't valid.
    @return A real skill name, or None.
    """
    if not valid_skill_names:
        return None
    if requested_skill in valid_skill_names:
        return requested_skill
    return fallback if fallback in valid_skill_names else None


def generate_ad_hoc_item(
    phrase, intent, scene_description, valid_equip_slots=None, valid_skill_names=None,
    call_chat_completion=None, api_url=DEFAULT_API_URL, timeout=DEFAULT_TIMEOUT,
):
    """!
    @brief Asks the local LLM whether phrase (the player's own clause, ex: "pick up a stone")
        describes something plausible to conjure into the current scene, and if so, what it is
        -- a physical object (including a container/trap, complete with a minimal
        LLM-authorable [entity.test], mirroring items.toml's own chest/dart trap shape -- see
        CLAUDE.md's "Entity tests") or, via the model's own describe_scenery choice, ambient
        detail with no persistent entity at all.
    @param phrase The player's own raw clause naming the item (ex: "the stone").
    @param intent The item-interaction verb that triggered this (ex: "take", "examine") --
        folded into the prompt for context; the caller is responsible for actually resolving
        this same intent against the created entity afterward (see DM_Improvisation.py).
    @param scene_description The current scene's own description (DM_Rules.py's
        _current_scene_description), grounding what's plausible here.
    @param valid_equip_slots Forwarded to _build_item_tool_schema.
    @param valid_skill_names Forwarded to _build_item_tool_schema/_resolve_test_skill -- a
        container/trap's own lock/disarm skill.
    @param call_chat_completion The LLM-calling callable to use -- None (the default) resolves
        to this module's own _real_call_chat_completion *at call time*, the same
        patch("AdHoc_Generation._real_call_chat_completion", fake) seam NPC_Generation.py
        established.
    @param api_url/timeout Forwarded to call_chat_completion.
    @return {"created": False, "reason": str} on decline or any failure (network error,
            malformed response, no tool call, an incomplete create_item call) -- this function
            never raises and never fabricates an item when the LLM is unreachable.
            {"created": False, "scenery": True, "description": str} if the model chose
            describe_scenery instead -- distinct from a plain decline, since there's still
            something to narrate, just no entity to create. On success: {"created": True,
            "entity": {full entity dict, "ad_hoc": True}, "location": "ground"|"inventory"} --
            "location" is meaningless (always "ground") for a container/trap, which the caller
            places in the scene itself rather than on the ground or in inventory.
    """
    call_chat_completion = call_chat_completion or _real_call_chat_completion
    prompt = (
        f"The player, in a tabletop RPG scene, tries to \"{intent}\" something described as: "
        f"\"{phrase}\". Current scene: {scene_description or 'unknown'}.\n"
        "If it's plausible this object could be improvised into the scene, call create_item "
        "with its details. If it's ambient scenery/detail instead, call describe_scenery. "
        "Otherwise call decline."
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
            api_url, messages, tools=_build_item_tool_schema(valid_equip_slots, valid_skill_names),
            tool_choice="auto", timeout=timeout,
        )
        function_name, arguments = _extract_tool_call(response)
    except Exception:
        return {"created": False, "reason": "unavailable"}

    if function_name == "describe_scenery":
        description = arguments.get("description") if isinstance(arguments, dict) else None
        if not description:
            return {"created": False, "reason": "incomplete"}
        return {"created": False, "scenery": True, "description": description}

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

    if subtype == "container":
        entity["currency"] = int(arguments.get("contains_currency") or 0)
        active_conditions = {"closed": {"duration": "permanent", "dismiss": None}}
        lock_skill = None
        if arguments.get("locked"):
            lock_skill = _resolve_test_skill(arguments.get("lock_skill"), valid_skill_names)
            if lock_skill:
                active_conditions["locked"] = {"duration": "permanent", "dismiss": None}
        entity["active_conditions"] = active_conditions
        if lock_skill:
            entity["test"] = {
                "difficulty": int(arguments.get("lock_difficulty") or 10),
                "skill": [lock_skill],
                "requires_condition": "locked",
                "blocks_if_condition": "jammed",
                "pass": {"dismiss_condition": "locked"},
                "fail": {"condition": "jammed", "duration": "permanent", "dismiss": ""},
            }
    elif subtype == "trap":
        entity["active_conditions"] = {"armed": {"duration": "permanent", "dismiss": None}}
        disarm_skill = _resolve_test_skill(arguments.get("disarm_skill"), valid_skill_names)
        if disarm_skill:
            entity["test"] = {
                "difficulty": int(arguments.get("disarm_difficulty") or 9),
                "skill": [disarm_skill],
                "requires_condition": "armed",
                "blocks_if_condition": "triggered",
                "pass": {"dismiss_condition": "armed"},
                "fail": {
                    "condition": "triggered", "duration": "permanent", "dismiss": "",
                    "damage": {
                        "dice": int(arguments.get("damage_dice") or 1),
                        "pips": int(arguments.get("damage_pips") or 0),
                        "bonus": 0,
                    },
                    "damage_tags": [arguments["damage_tag"]] if arguments.get("damage_tag") in DAMAGE_TAGS else [],
                },
            }

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
    phrase, scene_description, removable_entities, hostile_entities=None,
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
    @param hostile_entities The subset of removable_entities currently hostile toward (and
        alive relative to) the player -- DM_Social.py's is_hostile plus a live-HP check, built
        by the caller. Folded into the prompt as an explicit off-limits list: without concrete
        grounding, live testing against a real model showed it complies unconditionally with
        "get rid of that wolf, this fight is too hard" (and every close paraphrase of it) --
        removal never rolls dice or costs a turn, so an ungated model turns it into a free,
        consequence-free win button against anything currently trying to kill the player. An
        empty/falsy value (ex: no live hostiles in the candidate set at all) adds no extra text.
    @param call_chat_completion/api_url/timeout See generate_ad_hoc_item's own docstring.
    @return {"removed": False, "reason": str} on decline, an empty removable_entities, or any
            failure -- never raises. On success: {"removed": True, "name", "reason"}.
    """
    if not removable_entities:
        return {"removed": False, "reason": "nothing_here"}

    hostile_entities = [name for name in (hostile_entities or []) if name in removable_entities]
    hostile_note = ""
    if hostile_entities:
        hostile_note = (
            f" Currently hostile and actively threatening the player: "
            f"{', '.join(hostile_entities)}. Never remove one of these just because the player "
            "finds the fight difficult, asks to skip it, or wants it to vanish/be destroyed/"
            "banished/deleted -- a live threat has to be dealt with through actual play (combat, "
            "a real in-fiction resolution), never narrated away for free. Decline any such "
            "request for one of these regardless of how it's phrased or how reasonable it sounds."
        )

    call_chat_completion = call_chat_completion or _real_call_chat_completion
    prompt = (
        f"The player, addressing the Game Master directly, says: \"{phrase}\". Current scene: "
        f"{scene_description or 'unknown'}. Currently present/available to remove: "
        f"{', '.join(removable_entities)}.{hostile_note}\n"
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


def _build_creature_tool_schema(npc_keywords):
    """!
    @brief The OpenAI-style "tools" payload for generate_ad_hoc_creature's own tool call:
        create_creature (enum-constrained keywords/disposition/power, same reliability-over-
        free-text reasoning NPC_Generation.py's own _build_tool_schema already documents) or
        decline.
    @param npc_keywords {keyword_name: [skill_name, ...]}, from NPC_Generation.load_npc_keywords.
    @return The "tools" list for call_chat_completion.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "create_creature",
                "description": (
                    "Conjure a living creature or character into the scene, if the player's "
                    "request is reasonable for this setting and scene."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string", "enum": list(npc_keywords)},
                            "minItems": 1,
                            "maxItems": 2,
                            "description": "1-2 archetype keywords that best capture what it's skilled at.",
                        },
                        "disposition": {
                            "type": "string", "enum": list(CREATURE_DISPOSITIONS),
                            "description": "How it regards the player -- 'hostile' is the only disposition that will actually fight.",
                        },
                        "power": {"type": "string", "enum": list(CREATURE_POWERS)},
                    },
                    "required": ["name", "description", "keywords", "disposition", "power"],
                },
            },
        },
        _decline_tool_schema("Use this instead if the requested creature doesn't make sense here."),
    ]


def generate_ad_hoc_creature(
    phrase, scene_description, target_cr, npc_keywords,
    call_chat_completion=None, api_url=DEFAULT_API_URL, timeout=DEFAULT_TIMEOUT,
):
    """!
    @brief Asks the local LLM whether phrase (the player's own message to ADaM) describes a
        living creature/NPC plausible to conjure into the current scene, and if so, fits its
        skills/HP to target_cr via NPC_Generation.py's own deterministic fit_skills_to_cr --
        the exact same budget-splitting math real NPC generation uses (see CLAUDE.md's "NPC
        generation"), just without a second LLM round trip: the keyword choice from *this* one
        call is reused instead of calling generate_npc_stats' own separate tool call again.
        Only a "hostile" disposition (the exact -100 threshold DM_Social.py's is_hostile
        requires) gets an attack ability + flee-when-wounded behavior attached, mirroring
        arena.toml's own wolf/field.toml's own bandit shape -- a wary/neutral/friendly conjured
        NPC is dialogue-only, same as a template author would choose for a peaceful NPC.
    @param phrase The player's own raw message naming what to conjure (ex: "a snarling wolf").
    @param scene_description The current scene's own description, grounding what's plausible.
    @param target_cr The challenge rating to fit toward, before this call's own power
        multiplier/variance -- the caller's job to resolve (ex: DM_Improvisation.py's
        _attempt_creature_conjuring uses self.get_challenge_rating(self.player_name), a
        single-target encounter framing appropriate for an ad hoc, mid-scene spawn).
    @param npc_keywords {keyword_name: [skill_name, ...]}, from NPC_Generation.load_npc_keywords
        -- an empty catalog (ex: a setting with no npc_keyword entries at all) declines
        immediately, no LLM call.
    @param call_chat_completion/api_url/timeout See generate_ad_hoc_item's own docstring.
    @return {"created": False, "reason": str} on decline, an empty npc_keywords, or any failure
            -- never raises. On success: {"created": True, "entity": {full entity dict,
            "ad_hoc": True}}.
    """
    if not npc_keywords:
        return {"created": False, "reason": "no_keywords"}

    call_chat_completion = call_chat_completion or _real_call_chat_completion
    prompt = (
        f"The player, addressing the Game Master directly, asks for a creature or character to "
        f"be conjured into the scene, described as: \"{phrase}\". Current scene: "
        f"{scene_description or 'unknown'}.\n"
        "If it's plausible for this to appear here, call create_creature with its details -- "
        "1-2 archetype keywords that best capture what it's skilled at, its disposition toward "
        "the player, and a rough power level. Otherwise call decline."
    )
    messages = [
        {
            "role": "system",
            "content": "You are helping a Game Master improvise a creature or character into a live tabletop RPG scene.",
        },
        {"role": "user", "content": prompt},
    ]

    try:
        response = call_chat_completion(
            api_url, messages, tools=_build_creature_tool_schema(npc_keywords),
            tool_choice="auto", timeout=timeout,
        )
        function_name, arguments = _extract_tool_call(response)
    except Exception:
        return {"created": False, "reason": "unavailable"}

    if function_name != "create_creature":
        reason = arguments.get("reason", "declined") if isinstance(arguments, dict) else "declined"
        return {"created": False, "reason": reason}

    name = arguments.get("name")
    description = arguments.get("description")
    chosen_keywords = [k for k in arguments.get("keywords", []) if k in npc_keywords]
    if not name or not description or not chosen_keywords:
        return {"created": False, "reason": "incomplete"}

    disposition = arguments.get("disposition") if arguments.get("disposition") in CREATURE_DISPOSITIONS else "neutral"
    power = arguments.get("power") if arguments.get("power") in CREATURE_POWERS else "moderate"

    key_skills = [skill for keyword in chosen_keywords for skill in npc_keywords.get(keyword, [])]
    rolled_cr = target_cr * POWER_MULTIPLIERS[power] * random.uniform(0.85, 1.15)
    skills, max_hp = fit_skills_to_cr(key_skills, rolled_cr)

    entity = {
        "name": name,
        "description": description,
        "supertype": "creature",
        "subtype": "npc",
        "max_hp": max_hp,
        "skills": skills,
        "attitudes": {"default": [DISPOSITION_VALUES[disposition], 0, 0, 0, 0, 0]},
        # Tags this as having no static TOML template to re-derive from on a reload -- see
        # DM_Persistence.py's save_game/load_game, which save/restore the full dict for any
        # entity carrying this flag rather than the ordinary hp/inventory/etc. diff.
        "ad_hoc": True,
    }

    if disposition == "hostile":
        # skills is built in unique_skills order (fit_skills_to_cr) -- the first entry is
        # always one of the (up to 3) primary-rated skills, so no separate ranking is needed.
        attack_skill = next(iter(skills), None)
        if attack_skill:
            ability_name = f"{name} attack"
            attack_dice = skills[attack_skill]["dice"]
            entity["abilities"] = [{
                "name": ability_name,
                "supertype": "innate",
                "subtype": "weapon",
                "skill": attack_skill,
                "damage_value": {"dice": max(1, attack_dice // 2), "pips": 0, "bonus": 0},
                "damage_tags": ["physical"],
            }]
            # Mirrors arena.toml's own wolf/field.toml's own bandit shape exactly -- flee once
            # genuinely hurt (hp_per_remain under 0.40, the same cutoff rules.toml's "wounded"
            # tier bottoms out at), otherwise keep attacking until effectively dead.
            entity["behavior"] = [
                {
                    "requirements": [
                        {"field": "hp_per_remain", "operator": ">=", "value": 0.01},
                        {"field": "hp_per_remain", "operator": "<", "value": 0.40},
                    ],
                    "action": "retreat",
                },
                {
                    "requirements": [{"field": "hp_per_remain", "operator": ">=", "value": 0.01}],
                    "action": ability_name,
                },
            ]

    return {"created": True, "entity": entity}


def _build_edit_tool_schema(editable_entities):
    """!
    @brief The OpenAI-style "tools" payload for decide_entity_edit's own tool call: edit_entity
        (name enum-constrained to editable_entities, same structural-impossibility reasoning
        _build_removal_tool_schema already documents) or decline. Deliberately narrow --
        description rewrite plus condition apply/dismiss (reusing the already-safe, already-
        reversible apply_condition/dismiss_condition primitives, DM_Status.py), not raw
        mechanical fields like skills/damage_value, which would need far more validation to not
        silently break combat math.
    @param editable_entities The real, currently-valid entity names the model may choose from.
    @return The "tools" list for call_chat_completion.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "edit_entity",
                "description": (
                    "Edit an existing entity in the scene -- rewrite its description and/or "
                    "apply or dismiss a condition on it -- if the player's request is reasonable."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": list(editable_entities)},
                        "new_description": {"type": "string"},
                        "apply_condition": {
                            "type": "string",
                            "description": "A short condition label to mark active on this entity (ex: 'unstuck', 'glowing').",
                        },
                        "dismiss_condition": {
                            "type": "string",
                            "description": "The name of a condition currently active on this entity to remove (ex: 'locked').",
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["name", "reason"],
                },
            },
        },
        _decline_tool_schema("Use this instead if nothing here should actually be edited."),
    ]


def decide_entity_edit(
    phrase, scene_description, editable_entities,
    call_chat_completion=None, api_url=DEFAULT_API_URL, timeout=DEFAULT_TIMEOUT,
):
    """!
    @brief Asks the local LLM whether phrase (the player's own message to ADaM) is asking for
        an existing entity's description or a condition on it to change, and if so, which of
        editable_entities and how.
    @param phrase The player's own raw message (ex: "the note is written in an unknown language").
    @param scene_description The current scene's own description, for context.
    @param editable_entities Every real, currently-valid name the model may choose from (built
        by the caller -- DM_Improvisation.py's _attempt_entity_edit -- deliberately *excluding*
        the player's own name, same posture decide_entity_removal already takes). An empty list
        short-circuits to a decline with no LLM call at all.
    @param call_chat_completion/api_url/timeout See generate_ad_hoc_item's own docstring.
    @return {"edited": False, "reason": str} on decline, an empty editable_entities, or any
            failure -- never raises. On success: {"edited": True, "name", "reason",
            "new_description", "apply_condition", "dismiss_condition"} -- the latter three are
            None wherever the model didn't specify one.
    """
    if not editable_entities:
        return {"edited": False, "reason": "nothing_here"}

    call_chat_completion = call_chat_completion or _real_call_chat_completion
    prompt = (
        f"The player, addressing the Game Master directly, says: \"{phrase}\". Current scene: "
        f"{scene_description or 'unknown'}. Currently present/available to edit: "
        f"{', '.join(editable_entities)}.\n"
        "If they're clearly asking for one of these to be changed in some way (its own "
        "description, or a condition on it), call edit_entity naming exactly one of them. "
        "Otherwise call decline."
    )
    messages = [
        {
            "role": "system",
            "content": "You are helping a Game Master decide whether to edit something in a live tabletop RPG scene.",
        },
        {"role": "user", "content": prompt},
    ]

    try:
        response = call_chat_completion(
            api_url, messages, tools=_build_edit_tool_schema(editable_entities),
            tool_choice="auto", timeout=timeout,
        )
        function_name, arguments = _extract_tool_call(response)
    except Exception:
        return {"edited": False, "reason": "unavailable"}

    if function_name != "edit_entity":
        reason = arguments.get("reason", "declined") if isinstance(arguments, dict) else "declined"
        return {"edited": False, "reason": reason}

    name = arguments.get("name")
    if name not in editable_entities:
        return {"edited": False, "reason": "invalid_target"}

    new_description = arguments.get("new_description") or None
    apply_condition = arguments.get("apply_condition") or None
    dismiss_condition = arguments.get("dismiss_condition") or None
    if not (new_description or apply_condition or dismiss_condition):
        return {"edited": False, "reason": "incomplete"}

    return {
        "edited": True, "name": name, "reason": arguments.get("reason", ""),
        "new_description": new_description, "apply_condition": apply_condition,
        "dismiss_condition": dismiss_condition,
    }
