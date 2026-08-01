"""!
@file NPC_Generation.py
@brief Pure, DMCore-independent NPC generation logic -- same "pure, entity-shape-agnostic"
    precedent Character_Creation.py/Challenge_Rating.py already set. Given a target challenge
    rating (see Challenge_Rating.py) and a catalog of archetype keywords (each naming a small
    set of real skills), asks the local LLM for a backstory + 1-2 keywords via OpenAI-style
    function calling, then mechanically assigns dice/pips to the resolved skills (plus HP) so
    the result's own calculate_challenge_rating lands near the target. DM_NpcGeneration.py is
    the DMCore-touching glue that calls this and bakes the result onto a live entity, the same
    split DM_CharacterCreation.py is to this module's own sibling.
"""

import json
import os
import random
import tomllib

from Challenge_Rating import calculate_challenge_rating, skill_rating
from LLM_Client import call_chat_completion as _real_call_chat_completion

DEFAULT_API_URL = "http://127.0.0.1:1234/v1/chat/completions"

# A named "key skill" landing at 0D would read as a design bug, not a deliberately weak NPC --
# 1D (rating 3) is the floor a fitted skill can ever land on.
MIN_KEY_SKILL_RATING = 3


def resolve_varied_value(value):
    """!
    @brief Resolves one entity_template field that may be authored as a plain value, a
        {min, max} range, or a weighted-choice list -- the shared "how varied is this field"
        vocabulary templates.toml uses across hint/cr_multiplier/currency/qualities/
        attitudes, so DM_NpcGeneration.py doesn't need separate resolution logic per field.
        Applying this to every leaf individually (not the whole [entity_template.attitudes]
        default array at once, for instance) is what lets a template mix fixed and varied
        entries freely (ex: templates.toml's generated_stranger: trust/confidence stay a
        flat 0 while disposition/intimacy vary) -- see _resolve_attitudes in
        DM_NpcGeneration.py for how the six-axis array itself is walked.
    @param value One of:
        - A plain scalar (int/float/str/bool) -- returned unchanged.
        - {"min": low, "max": high} -- a uniform random pick in that range. Both ints picks
          an int (random.randint, inclusive); either being a float picks a float
          (random.uniform).
        - A list of single-key {"choice": weight} tables (ex: templates.toml's own
          `race = [{"human"=60}, {"elf"=20}, ...]`) -- a weighted random pick of the *key*
          (not the weight), via random.choices. Weights are relative, not required to sum to
          100 or 1 -- random.choices normalizes them internally.
    @return The resolved value -- a plain scalar either way.
    """
    if isinstance(value, dict) and "min" in value and "max" in value:
        low, high = value["min"], value["max"]
        if isinstance(low, float) or isinstance(high, float):
            return random.uniform(low, high)
        return random.randint(low, high)

    if isinstance(value, list) and value and all(isinstance(entry, dict) for entry in value):
        weighted = {}
        for entry in value:
            weighted.update(entry)
        return random.choices(list(weighted.keys()), weights=list(weighted.values()), k=1)[0]

    return value


def load_npc_keywords(rules_dir=os.path.join("Rules", "Fantasy")):
    """!
    @brief Scans every *.toml directly under rules_dir for "npc_keyword" entries -- the same
        generic per-file scan Character_Creation.py's load_character_creation_data uses for
        "skill"/"race", duplicated here so this stays importable/callable with no DMCore/
        live rules table in the picture.
    @param rules_dir Path to the rules directory, relative to this file's own location.
    @return {keyword_name: [skill_name, ...]}.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    full_dir = os.path.join(base_dir, rules_dir)

    keywords = {}
    if not os.path.exists(full_dir):
        return keywords

    for filename in os.listdir(full_dir):
        if not filename.endswith(".toml"):
            continue
        filepath = os.path.join(full_dir, filename)
        try:
            with open(filepath, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            continue
        for entry in data.get("npc_keyword", []):
            name = entry.get("name")
            if name:
                keywords[name] = list(entry.get("skills", []))

    return keywords


def fit_skills_to_cr(key_skills, target_cr, hp_share=0.3, damage_dice=0, damage_pips=0):
    """!
    @brief Deterministically distributes a challenge-rating "budget" across key_skills (plus
        HP) so the result's own calculate_challenge_rating lands on target_cr exactly (modulo
        the same integer rounding calculate_challenge_rating itself already does). Variance/
        randomness is the caller's job (rolled into target_cr before this runs, and by which
        keywords/key_skills were even chosen) -- this function itself is deterministic so it
        stays directly testable.
    @param key_skills An ordered list of skill names (ex: the union of 1-2 keywords' own
        skill lists) -- duplicates are fine (deduped, order-preserving); only the first 3
        (by calculate_challenge_rating's own top_n=3) actually affect the resulting CR, same
        as any other entity's own trained skills.
    @param target_cr The challenge rating to fit toward (already variance-rolled).
    @param hp_share The fraction of target_cr's budget spent on HP (default 0.3, matching the
        rough proportion hand-authored creatures.toml/characters.toml entries already show).
    @param damage_dice/damage_pips The entity's own best damage-dealing weapon/ability, if
        already known (ex: a hand-authored weapon on the same template) -- 0/0 (the default)
        if none, in which case the full remaining budget goes to skills instead. Not resolved
        automatically by this function; a caller with a real weapon must pass its dice/pips in
        directly (see NPC generation's own known "generally match" simplification for a
        generate=true template that also hand-supplies a weapon).
    @return (skills_dict, max_hp) -- skills_dict is {skill_name: {"dice", "pips"}}, one entry
        per unique name in key_skills (an empty list yields an empty skills_dict and max_hp
        derived from hp_share alone).
    """
    # target_cr arrives as a float once a caller has rolled variance into it
    # (target_cr * random.uniform(...), see generate_npc_stats) -- rounded to an int up
    # front so every downstream value (hp_units, remaining, and therefore dice/pips) stays
    # integer arithmetic throughout, not floats leaking into a {"dice", "pips"} skill entry.
    target_cr = round(target_cr)
    hp_units = round(target_cr * hp_share)
    max_hp = hp_units * 3
    remaining = target_cr - hp_units - skill_rating(damage_dice, damage_pips)

    unique_skills = list(dict.fromkeys(key_skills))  # dedupe, preserve first-seen order
    skills_dict = {}
    if unique_skills:
        primary_rating = max(remaining, MIN_KEY_SKILL_RATING)
        flavor_rating = max(primary_rating // 2, MIN_KEY_SKILL_RATING)
        for index, name in enumerate(unique_skills):
            rating = primary_rating if index < 3 else flavor_rating
            skills_dict[name] = {"dice": rating // 3, "pips": rating % 3}

    return skills_dict, max_hp


def _build_tool_schema(npc_keywords):
    """!
    @brief The OpenAI-style "tools" payload for generate_npc_stats' own tool call: a single
        "describe_npc" function whose "keywords" field is enum-constrained to the real
        catalog (see load_npc_keywords) -- constraining the LLM to a fixed vocabulary instead
        of free text is what makes this reliable with small local models (verified live
        against LM Studio during design).
    @param npc_keywords {keyword_name: [skill_name, ...]}, from load_npc_keywords.
    @return The "tools" list for call_chat_completion.
    """
    return [{
        "type": "function",
        "function": {
            "name": "describe_npc",
            "description": "Report the generated NPC's name, one-sentence backstory, and 1-2 archetype keywords.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "backstory": {"type": "string"},
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(npc_keywords)},
                        "minItems": 1,
                        "maxItems": 2,
                    },
                },
                "required": ["name", "backstory", "keywords"],
            },
        },
    }]


def _describe_qualities(qualities):
    """!
    @brief Renders an entity's own already-resolved gender/race/age (see
        DM_NpcGeneration.py's _resolve_generated_qualities, which must run *before*
        generate_npc_stats -- these have to be concrete values by the time this is called,
        not a {min, max}/weighted-choice table) into a short clause fed into the LLM prompt,
        so the invented name/backstory actually matches -- ex: a resolved gender = "male"
        shouldn't come back paired with a name the model would only ever pick for a woman.
        Deliberately scoped to just these three keys (not every arbitrary
        [entity_template.qualities] leaf a template might declare) -- they're the ones
        templates.toml's own varied fields actually vary today; a template's other
        descriptive qualities (body/eye/hair/...) stay flavor the LLM never needs for naming.
    @param qualities The entity's own already-resolved "qualities" dict, or None/{}.
    @return A sentence fragment (ex: "They are a male halfling, about 37 years old."), or ""
        if qualities has none of gender/race/age to describe.
    """
    if not qualities:
        return ""
    descriptor = " ".join(str(value) for value in (qualities.get("gender"), qualities.get("race")) if value)
    age = qualities.get("age")

    sentence = f"They are a {descriptor}" if descriptor else "They are"
    if age is None:
        return f"{sentence}." if descriptor else ""
    return f"{sentence}, about {age} years old." if descriptor else f"They are about {age} years old."


def _fallback_npc_stats(npc_keywords, target_cr, hp_share):
    """!
    @brief The offline/failure path generate_npc_stats falls back to -- no network call at
        all, so it's instant and safe to use both when LM Studio is genuinely unreachable and
        when a save-game reload deliberately wants to skip generation (see
        DM_Persistence.py's load_game / DM_Rules.py's skip_llm_generation). Matches the rest
        of the app's "LM Studio is best-effort, never blocks core gameplay" posture (RagIndex
        returns [] until ready; generate_load_failed_response still narrates on failure).
    @param npc_keywords {keyword_name: [skill_name, ...]}, from load_npc_keywords.
    @param target_cr The already variance-rolled challenge rating to fit toward.
    @param hp_share Forwarded to fit_skills_to_cr.
    @return {"name", "description", "skills", "max_hp"}.
    """
    names = list(npc_keywords)
    chosen = random.sample(names, k=min(2, len(names))) if names else []
    key_skills = [skill for keyword in chosen for skill in npc_keywords.get(keyword, [])]
    skills, max_hp = fit_skills_to_cr(key_skills, target_cr, hp_share=hp_share)
    return {
        "name": "Unnamed Stranger",
        "description": "A figure whose story remains untold for now.",
        "skills": skills,
        "max_hp": max_hp,
    }


def generate_npc_stats(
    npc_keywords, target_cr, hint=None, qualities=None, variance=0.15, cr_multiplier=1.0,
    hp_share=0.3, call_chat_completion=None, api_url=DEFAULT_API_URL, skip_llm_generation=False,
):
    """!
    @brief The full NPC generation pipeline: ask the local LLM for a backstory + 1-2 archetype
        keywords via function calling, resolve those keywords to real skills, and fit that
        skill set (plus HP) to a randomly-varied target challenge rating.
    @param npc_keywords {keyword_name: [skill_name, ...]}, from load_npc_keywords -- passed in
        rather than reloaded here so a caller that's already loaded it once (or a test with a
        small fake catalog) doesn't pay/duplicate the file scan.
    @param target_cr The challenge rating to aim for, before variance/cr_multiplier.
    @param hint Optional flavor text (ex: "a suspicious traveling merchant") folded into the
        LLM prompt; a generic prompt is used if omitted.
    @param qualities The entity's own already-resolved qualities dict (gender/race/age --
        see _describe_qualities) -- must already be concrete values, not varied-value tables,
        so the caller (DM_NpcGeneration.py) has to resolve [entity_template.qualities]
        *before* calling this, not after. Folded into the same prompt as hint, so the
        invented name/backstory actually matches whatever race/gender was already rolled,
        instead of the two being decided independently and sometimes disagreeing.
    @param variance Fractional random spread applied to target_cr (default 0.15 = +/-15%) --
        this, plus which keywords the LLM happens to pick, is where "some random variance for
        uniqueness" actually comes from; fit_skills_to_cr itself stays fully deterministic.
    @param cr_multiplier Flat multiplier on target_cr, applied before variance (default 1.0) --
        lets a caller ask for a deliberately tougher/weaker NPC without touching target_cr
        itself (ex: a unique boss authored with cr_multiplier = 1.5).
    @param hp_share Forwarded to fit_skills_to_cr.
    @param call_chat_completion The LLM-calling callable to use -- None (the default) resolves
        to this module's own _real_call_chat_completion *at call time*, not at def time, so
        `unittest.mock.patch("NPC_Generation._real_call_chat_completion", fake)` reliably
        intercepts it even though nothing here explicitly passes one -- the
        dependency-injection seam tests use, since DMCore itself has no other one anywhere
        (see DM_NpcGeneration.py's own module docstring).
    @param api_url Forwarded to call_chat_completion.
    @param skip_llm_generation If true, skips the network call entirely and goes straight to
        the offline fallback path -- used when reloading a save (DM_Persistence.py), where
        whatever this call produces is about to be overwritten by the saved values anyway.
    @return {"name", "description", "skills", "max_hp"}. Falls back to _fallback_npc_stats on
        skip_llm_generation, an empty npc_keywords catalog, or any failure talking to the LLM
        (no tool_calls in the response, malformed JSON, network error, or timeout) -- this
        function itself never raises.
    """
    call_chat_completion = call_chat_completion or _real_call_chat_completion
    rolled_cr = target_cr * cr_multiplier * random.uniform(1 - variance, 1 + variance)

    if skip_llm_generation or not npc_keywords:
        return _fallback_npc_stats(npc_keywords, rolled_cr, hp_share)

    qualities_sentence = _describe_qualities(qualities)
    prompt = (
        f"Invent a tabletop RPG NPC{f' -- {hint}' if hint else ''}."
        f"{f' {qualities_sentence}' if qualities_sentence else ''} "
        f"Call describe_npc with a fitting name, a one-sentence backstory, and 1-2 keywords "
        f"that best capture what they're skilled at."
    )
    messages = [
        {"role": "system", "content": "You are helping design a non-player character for a tabletop RPG."},
        {"role": "user", "content": prompt},
    ]

    try:
        response = call_chat_completion(
            api_url, messages, tools=_build_tool_schema(npc_keywords), tool_choice="auto",
        )
        tool_calls = response["choices"][0]["message"]["tool_calls"]
        arguments = json.loads(tool_calls[0]["function"]["arguments"])
        name = arguments["name"]
        backstory = arguments["backstory"]
        chosen_keywords = [k for k in arguments["keywords"] if k in npc_keywords]
        if not chosen_keywords:
            raise ValueError("No recognized keywords in LLM response")
    except Exception:
        return _fallback_npc_stats(npc_keywords, rolled_cr, hp_share)

    key_skills = [skill for keyword in chosen_keywords for skill in npc_keywords.get(keyword, [])]
    skills, max_hp = fit_skills_to_cr(key_skills, rolled_cr, hp_share=hp_share)
    return {"name": name, "description": backstory, "skills": skills, "max_hp": max_hp}
