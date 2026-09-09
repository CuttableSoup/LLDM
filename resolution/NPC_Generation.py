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
import math
import os
import random
import tomllib

from resolution.Challenge_Rating import DEFAULT_HP_DIVISOR, calculate_challenge_rating, skill_rating
from llm.LLM_Client import call_chat_completion as _real_call_chat_completion
from paths import PROJECT_ROOT

DEFAULT_API_URL = "http://127.0.0.1:11434/v1/chat/completions"

# A named "key skill" landing at 0D would read as a design bug, not a deliberately weak NPC --
# 1D (rating 3) is the floor a fitted skill can ever land on.
MIN_KEY_SKILL_RATING = 3


def resolve_varied_value(value):
    """!
    @brief Resolves one entity_template field that may be authored as a plain value, a
        {min, max} range, or a weighted-choice list -- the shared "how varied is this field"
        vocabulary an entity_template (ex: Rules/Fantasy/scenarios/debug.toml's own
        generated_stranger) uses across hint/cr_multiplier/currency/qualities/attitudes, so
        DM_NpcGeneration.py doesn't need separate resolution logic per field.
        Applying this to every leaf individually (not the whole [entity_template.attitudes]
        default array at once, for instance) is what lets a template mix fixed and varied
        entries freely (ex: generated_stranger's own trust/confidence stay a
        flat 0 while disposition/intimacy vary) -- see _resolve_attitudes in
        DM_NpcGeneration.py for how the six-axis array itself is walked.
    @param value One of:
        - A plain scalar (int/float/str/bool) -- returned unchanged.
        - {"min": low, "max": high} -- a uniform random pick in that range. Both ints picks
          an int (random.randint, inclusive); either being a float picks a float
          (random.uniform).
        - A list of single-key {"choice": weight} tables (ex: generated_stranger's own
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
    @param rules_dir Path to the rules directory, relative to the project root.
    @return {keyword_name: [skill_name, ...]}.
    """
    full_dir = os.path.join(PROJECT_ROOT, rules_dir)

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


def _resolve_offense_survival_split(target_cr, offense_share, min_offense_side, fixed_offense_side, search_radius=200):
    """!
    @brief Solves calculate_challenge_rating's own "CR = round(2*sqrt(offense_side *
        survival_side))" backwards: given target_cr, finds an (offense_side, survival_side)
        integer pair reproducing it exactly. Unlike the old flat-sum model (where any three
        numbers adding to target_cr trivially "round-tripped"), a product has no closed-form
        integer inverse in general, so this does an analytic estimate (continuous math) followed
        by a small local search for the exact integer hit -- the same "deterministic, land on
        target_cr exactly" contract fit_skills_to_cr already promised, just solved differently.
    @param target_cr The challenge rating to reproduce exactly (already a non-negative int).
    @param offense_share Where offense_side should sit as a fraction of the total "power" this
        entity spends (0-1, 0.5 = balanced) -- only consulted when fixed_offense_side is None;
        purely a starting point for the search, not a guarantee (the search may drift from it
        to actually land on target_cr, or to respect min_offense_side).
    @param min_offense_side offense_side is never chosen below this floor -- callers pass
        known_offense_rating (+ MIN_KEY_SKILL_RATING if a named offense skill will be assigned
        on top of it) so a "trained" offense skill never lands at a suspicious 0D.
    @param fixed_offense_side If not None, offense_side is pinned to exactly this value (no
        named offense skill to assign a rating to -- offense_side can only ever be whatever
        damage_dice/pips already supply) and only survival_side is searched.
    @param search_radius How far past the analytic estimate to search for an exact integer
        match, in either direction, before giving up and returning the closest candidate found.
    @return (offense_side, survival_side), both non-negative ints. Exact (reproduces target_cr
        precisely via calculate_challenge_rating) for every case this module's own tests cover;
        falls back to the nearest achievable pair if no exact integer solution exists within
        search_radius (an increasingly large offense_side/survival_side mismatch has real gaps
        in which integer CR values are reachable at all -- see this function's own module note).
    """
    if target_cr <= 0:
        return (max(min_offense_side, fixed_offense_side or 0), 0)

    power = (target_cr / 2) ** 2
    if fixed_offense_side is not None:
        offense_side = fixed_offense_side
    else:
        ratio = offense_share / max(1 - offense_share, 1e-9)
        offense_side = max(min_offense_side, round(math.sqrt(power * ratio)))
    if offense_side <= 0:
        return (offense_side, 0)

    survival_estimate = round(power / offense_side)
    best_survival = max(0, survival_estimate)
    for delta in range(search_radius + 1):
        for candidate in ({survival_estimate - delta, survival_estimate + delta} if delta else {survival_estimate}):
            if candidate < 0:
                continue
            if round(2 * math.sqrt(offense_side * candidate)) == target_cr:
                return (offense_side, candidate)
    return (offense_side, best_survival)


def fit_skills_to_cr(
    key_skills, target_cr, skills_catalog, hp_share=0.3, damage_dice=0, damage_pips=0,
    hp_divisor=DEFAULT_HP_DIVISOR, offense_share=0.5,
):
    """!
    @brief Deterministically distributes a challenge-rating "budget" across key_skills (plus
        HP) so the result's own calculate_challenge_rating lands on target_cr exactly (modulo
        the same integer rounding calculate_challenge_rating itself already does). Variance/
        randomness is the caller's job (rolled into target_cr before this runs, and by which
        keywords/key_skills were even chosen) -- this function itself is deterministic so it
        stays directly testable.

        Mirrors calculate_challenge_rating's own two-sided product (Challenge_Rating.py's
        module note): first solves for an (offense_side, survival_side) integer pair
        reproducing target_cr exactly (_resolve_offense_survival_split), then distributes each
        side the same way the old flat-sum version distributed its own "remaining" budget --
        offense_side (minus any already-known damage rating) goes to every named offense-role
        key_skill, tied at the same rating (only the single best-rated one is ever actually
        read forward, via DM_Combat.py's _best_offense_package/get_challenge_rating's own
        max-over-candidates, so tying keeps the result exact regardless of which one ends up
        "best"); survival_side splits into HP (via hp_share) and whatever's left for defense/
        resistive, resistive skills averaging against EVERY resistive-role skill the catalog
        defines, not just the ones named here (an un-named one reads as untrained/0, pulling the
        real average down, so the sum handed to the named ones has to already account for it).
        A key_skill with no combat_role at all (ex: "strength", "stealth") is flavor only -- it
        still gets a rating (so a generated NPC's sheet doesn't show a suspicious 0D the
        archetype named), but calculate_challenge_rating never reads it.

        **No named offense-role key_skill and no supplied damage_dice/pips at all** means
        offense_side is unavoidably 0 -- and since CR = round(2*sqrt(0 * survival_side)) is
        always 0 regardless of survival_side (see Challenge_Rating.py's own docstring), there is
        no target_cr split to solve for in that case: the entire budget is simply handed to
        HP/defense/resistive as if it were survival_side (still a sensible-looking stat sheet
        for a generated flavor NPC), and the resulting entity's own real CR will read 0, not
        target_cr. This is a deliberate consequence of the underlying formula, not a bug in this
        function -- see test_fit_skills_to_cr_round_trips_through_calculate_challenge_rating's
        own "no combat-relevant skill at all" case.
    @param key_skills An ordered list of skill names (ex: the union of 1-2 keywords' own
        skill lists) -- duplicates are fine (deduped, order-preserving).
    @param target_cr The challenge rating to fit toward (already variance-rolled).
    @param skills_catalog The setting's own {skill_name: {"combat_role", ...}} table (ex:
        DM_Combat.py's self.skills) -- pure data, no live DMCore needed, the same "just a
        dict" precedent load_character_creation_data's own rules_dir scan already sets for a
        DMCore-independent module.
    @param hp_share The fraction of survival_side's own budget spent on HP (default 0.3,
        matching the rough proportion hand-authored creatures.toml/characters.toml entries
        already show) -- the rest goes to defense/resistive. Only ever meaningful when a
        defense or resistive-role key_skill is actually named; otherwise the whole of
        survival_side becomes HP (nothing else to spend it on).
    @param damage_dice/damage_pips The entity's own best damage-dealing weapon/ability, if
        already known (ex: a hand-authored weapon on the same template) -- 0/0 (the default)
        if none, in which case offense_side comes entirely from whatever offense-role key_skill
        rating this function assigns. Not resolved automatically by this function; a caller with
        a real weapon must pass its dice/pips in directly (see NPC generation's own known
        "generally match" simplification for a generate=true template that also hand-supplies a
        weapon).
    @param hp_divisor Converts the HP share of survival_side back into raw max_hp -- must match
        whatever hp_divisor calculate_challenge_rating itself will be called with (Challenge_
        Rating.py's own DEFAULT_HP_DIVISOR, the default both sides always use today -- no
        per-setting override exists) or this function's own "round-trips exactly" claim breaks
        -- the exact arithmetic inverse of hp_component's own "max_hp // hp_divisor".
    @param offense_share Where offense_side should sit as a fraction of target_cr's own
        "power budget" (0-1, default 0.5 = balanced) -- the archetype knob replacing what
        hp_share alone used to control before offense/survival became a product rather than
        just more terms in the same sum. Only consulted when a named offense-role key_skill
        exists to actually receive the resulting rating; ignored (offense_side is pinned to
        whatever damage_dice/pips already supply) otherwise. See
        _resolve_offense_survival_split for the actual solve.
    @return (skills_dict, max_hp) -- skills_dict is {skill_name: {"dice", "pips"}}, one entry
        per unique name in key_skills (an empty list yields an empty skills_dict and max_hp
        derived from the whole target_cr budget).
    """
    # target_cr arrives as a float once a caller has rolled variance into it
    # (target_cr * random.uniform(...), see generate_npc_stats) -- rounded to an int up
    # front so every downstream value stays integer arithmetic throughout, not floats leaking
    # into a {"dice", "pips"} skill entry.
    target_cr = round(target_cr)
    known_offense_rating = skill_rating(damage_dice, damage_pips)

    unique_skills = list(dict.fromkeys(key_skills))  # dedupe, preserve first-seen order
    offense = [n for n in unique_skills if skills_catalog.get(n, {}).get("combat_role") == "offense"]
    defense = [n for n in unique_skills if skills_catalog.get(n, {}).get("combat_role") == "defense"]
    resistive = [n for n in unique_skills if skills_catalog.get(n, {}).get("combat_role") == "resistive"]
    flavor = [n for n in unique_skills if n not in offense and n not in defense and n not in resistive]
    resistive_total = sum(1 for skill in skills_catalog.values() if skill.get("combat_role") == "resistive")

    if offense:
        offense_side, survival_side = _resolve_offense_survival_split(
            target_cr, offense_share, known_offense_rating + MIN_KEY_SKILL_RATING, fixed_offense_side=None,
        )
    elif known_offense_rating > 0:
        offense_side, survival_side = _resolve_offense_survival_split(
            target_cr, offense_share, known_offense_rating, fixed_offense_side=known_offense_rating,
        )
    else:
        # No named offense-role key_skill AND no supplied damage at all -- offense_side is
        # unavoidably 0, so CR is unavoidably 0 too (see this function's own docstring). Nothing
        # to solve for; the whole budget becomes survival_side (HP/defense/resistive) instead.
        offense_side, survival_side = 0, target_cr

    # No defense/resistive key_skill named -- nothing else survival_side could go to, so all of
    # it becomes HP (the one component every entity always has, mirroring the old collapse).
    if defense or resistive:
        hp_units = round(survival_side * hp_share)
        remaining = survival_side - hp_units
    else:
        hp_units = survival_side
        remaining = 0
    max_hp = hp_units * hp_divisor

    buckets = {"defense": defense, "resistive": resistive}
    named_bucket_order = [name for name in ("defense", "resistive") if buckets[name]]
    bucket_budget = {"defense": 0, "resistive": 0}
    if named_bucket_order:
        share, leftover = divmod(remaining, len(named_bucket_order))
        for index, name in enumerate(named_bucket_order):
            bucket_budget[name] = share + (1 if index < leftover else 0)

    skills_dict = {}
    used_ratings = []
    if offense:
        rating = max(offense_side - known_offense_rating, MIN_KEY_SKILL_RATING)
        used_ratings.append(rating)
        for name in offense:
            skills_dict[name] = {"dice": rating // 3, "pips": rating % 3}

    if defense:
        rating = max(bucket_budget["defense"], MIN_KEY_SKILL_RATING)
        used_ratings.append(rating)
        for name in defense:
            skills_dict[name] = {"dice": rating // 3, "pips": rating % 3}

    if resistive:
        # sum(named ratings) + 0 * (unnamed saves) has to average to bucket_budget["resistive"]
        # across every resistive-role skill the catalog defines, not just len(resistive).
        target_sum = bucket_budget["resistive"] * max(resistive_total, len(resistive))
        share, leftover = divmod(target_sum, len(resistive))
        for index, name in enumerate(resistive):
            rating = max(share + (1 if index < leftover else 0), MIN_KEY_SKILL_RATING)
            used_ratings.append(rating)
            skills_dict[name] = {"dice": rating // 3, "pips": rating % 3}

    if flavor:
        # Cosmetic only -- calculate_challenge_rating never reads an untagged skill. Scaled off
        # the smallest combat rating actually used (so it reads as genuinely secondary), or a
        # flat floor if this archetype named no combat-relevant skill at all.
        flavor_rating = max(min(used_ratings) // 2, MIN_KEY_SKILL_RATING) if used_ratings else MIN_KEY_SKILL_RATING
        for name in flavor:
            skills_dict[name] = {"dice": flavor_rating // 3, "pips": flavor_rating % 3}

    return skills_dict, max_hp


def _build_tool_schema(npc_keywords):
    """!
    @brief The OpenAI-style "tools" payload for generate_npc_stats' own tool call: a single
        "describe_npc" function whose "keywords" field is enum-constrained to the real
        catalog (see load_npc_keywords) -- constraining the LLM to a fixed vocabulary instead
        of free text is what makes this reliable with small local models (verified live
        against Ollama during design).
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
        [entity_template.qualities] leaf a template might declare) -- they're the ones any
        shipped entity_template's own varied fields actually vary today; a template's other
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


def _fallback_npc_stats(npc_keywords, target_cr, hp_share, skills_catalog, hp_divisor=DEFAULT_HP_DIVISOR):
    """!
    @brief The offline/failure path generate_npc_stats falls back to -- no network call at
        all, so it's instant and safe to use both when Ollama is genuinely unreachable and
        when a save-game reload deliberately wants to skip generation (see
        DM_Persistence.py's load_game / DM_Rules.py's skip_llm_generation). Matches the rest
        of the app's "Ollama is best-effort, never blocks core gameplay" posture (RagIndex
        returns [] until ready; generate_load_failed_response still narrates on failure).
    @param npc_keywords {keyword_name: [skill_name, ...]}, from load_npc_keywords.
    @param target_cr The already variance-rolled challenge rating to fit toward.
    @param hp_share Forwarded to fit_skills_to_cr.
    @param skills_catalog Forwarded to fit_skills_to_cr.
    @param hp_divisor Forwarded to fit_skills_to_cr.
    @return {"name", "description", "skills", "max_hp"}.
    """
    names = list(npc_keywords)
    chosen = random.sample(names, k=min(2, len(names))) if names else []
    key_skills = [skill for keyword in chosen for skill in npc_keywords.get(keyword, [])]
    skills, max_hp = fit_skills_to_cr(key_skills, target_cr, skills_catalog, hp_share=hp_share, hp_divisor=hp_divisor)
    return {
        "name": "Unnamed Stranger",
        "description": "A figure whose story remains untold for now.",
        "skills": skills,
        "max_hp": max_hp,
    }


def generate_npc_stats(
    npc_keywords, target_cr, skills_catalog, hint=None, qualities=None, variance=0.15, cr_multiplier=1.0,
    hp_share=0.3, call_chat_completion=None, api_url=DEFAULT_API_URL, skip_llm_generation=False,
    hp_divisor=DEFAULT_HP_DIVISOR,
):
    """!
    @brief The full NPC generation pipeline: ask the local LLM for a backstory + 1-2 archetype
        keywords via function calling, resolve those keywords to real skills, and fit that
        skill set (plus HP) to a randomly-varied target challenge rating.
    @param npc_keywords {keyword_name: [skill_name, ...]}, from load_npc_keywords -- passed in
        rather than reloaded here so a caller that's already loaded it once (or a test with a
        small fake catalog) doesn't pay/duplicate the file scan.
    @param target_cr The challenge rating to aim for, before variance/cr_multiplier.
    @param skills_catalog Forwarded to fit_skills_to_cr -- the setting's own {skill_name:
        {"combat_role", ...}} table (ex: DM_Combat.py's self.skills).
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
        `unittest.mock.patch("resolution.NPC_Generation._real_call_chat_completion", fake)` reliably
        intercepts it even though nothing here explicitly passes one -- the
        dependency-injection seam tests use, since DMCore itself has no other one anywhere
        (see DM_NpcGeneration.py's own module docstring).
    @param api_url Forwarded to call_chat_completion.
    @param skip_llm_generation If true, skips the network call entirely and goes straight to
        the offline fallback path -- used when reloading a save (DM_Persistence.py), where
        whatever this call produces is about to be overwritten by the saved values anyway.
    @param hp_divisor Forwarded to fit_skills_to_cr/_fallback_npc_stats.
    @return {"name", "description", "skills", "max_hp"}. Falls back to _fallback_npc_stats on
        skip_llm_generation, an empty npc_keywords catalog, or any failure talking to the LLM
        (no tool_calls in the response, malformed JSON, network error, or timeout) -- this
        function itself never raises.
    """
    call_chat_completion = call_chat_completion or _real_call_chat_completion
    rolled_cr = target_cr * cr_multiplier * random.uniform(1 - variance, 1 + variance)

    if skip_llm_generation or not npc_keywords:
        return _fallback_npc_stats(npc_keywords, rolled_cr, hp_share, skills_catalog, hp_divisor=hp_divisor)

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
        return _fallback_npc_stats(npc_keywords, rolled_cr, hp_share, skills_catalog, hp_divisor=hp_divisor)

    key_skills = [skill for keyword in chosen_keywords for skill in npc_keywords.get(keyword, [])]
    skills, max_hp = fit_skills_to_cr(key_skills, rolled_cr, skills_catalog, hp_share=hp_share, hp_divisor=hp_divisor)
    return {"name": name, "description": backstory, "skills": skills, "max_hp": max_hp}
