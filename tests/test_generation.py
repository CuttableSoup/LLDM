import json
import os
import shutil
import threading
import time
import unittest
from unittest.mock import patch
import resolution.Combat_Resolution as Combat_Resolution
from resolution.AdHoc_Generation import (
    NON_HOSTILE_DISPOSITIONS,
    _currency_amount,
    decide_entity_edit,
    decide_entity_removal,
    generate_ad_hoc_creature,
    generate_ad_hoc_item,
    generate_referenced_npc,
)
from resolution.Challenge_Rating import calculate_challenge_rating, skill_rating
from dm.DM_Core import DMCore, PERSON_TARGET_INTENTS
from tests.event_contract import ValidatingEventBus
from persistence.slot import MemorySlotStore
from llm.Narration_Prompts import NarratorState
from resolution.NPC_Generation import (
    _describe_qualities,
    fit_skills_to_cr,
    generate_npc_stats,
    load_npc_keywords,
    resolve_varied_value,
)
from resolution.World_Context import WorldContext
import resolution.Combat_Actions as Combat_Actions
from tests.support import (
    DMTestCase,
    script_llm,
    scripted_llm,
)
# Module-level hooks start the shared patches (tests/support.py) for every test in this file.
from tests.support import setUpModule, tearDownModule  # noqa: F401


# Minimal keyword catalog/skills catalog for generate_referenced_npc, which is DMCore-
# independent (same reasoning FAKE_SKILLS_CATALOG is declared separately for its siblings).
REFERENCED_NPC_KEYWORDS = {"merchant": ["appraise", "charisma"], "warrior": ["blades"]}


REFERENCED_NPC_SKILLS = {
    "appraise": {"name": "appraise"},
    "charisma": {"name": "charisma"},
    "blades": {"name": "blades", "combat_role": "offense"},
}


# A minimal skills_catalog fixture mirroring Rules/Fantasy/skills.toml's own real combat_role
# tags for the specific skill names these tests actually use -- fit_skills_to_cr/
# generate_npc_stats/generate_ad_hoc_creature are pure, DMCore-independent functions (no
# self.skills to read), so a plain dict fixture stands in the same way load_npc_keywords' own
# small fake catalogs already do elsewhere in this class. "strength"/"linguistics"/"stealth"
# are deliberately absent (no combat_role at all) -- flavor-only skills, same as the real file.
FAKE_SKILLS_CATALOG = {
    "blades": {"combat_role": "offense"},
    "axes": {"combat_role": "offense"},
    "athletics": {"combat_role": "offense"},
    "brawling": {"combat_role": "offense"},
    "arcane": {"combat_role": "offense"},
    "dodge": {"combat_role": "defense"},
    "fortitude": {"combat_role": "resistive"},
    "reflexes": {"combat_role": "resistive"},
    "willpower": {"combat_role": "resistive"},
}


class TestNpcGeneration(unittest.TestCase):
    """!
    @brief NPC_Generation.py's pure math/parsing -- no DMCore, no live LLM (see
        TestNpcGenerationDMCoreIntegration for the DMCore-side glue). generate_npc_stats'
        own seam (LLM_Client's scripted transport, see tests/support.py) is exercised directly here
        rather than through DMCore.
    """

    @staticmethod
    def _cr_from_skills(skills, max_hp, catalog=FAKE_SKILLS_CATALOG):
        """Mirrors Combat_Actions.py's get_challenge_rating, minus the resistance/immunity/
        vulnerability/equipped-weapon glue these pure skill-only fixtures never model -- the
        same "just a dict" resolution get_challenge_rating does against a live entity's own
        skills, applied here against a bare {skill_name: {"dice","pips"}} table instead."""
        def _best(role):
            candidates = [skills[n] for n in skills if catalog.get(n, {}).get("combat_role") == role]
            return max(candidates, key=lambda s: skill_rating(s["dice"], s["pips"]), default={})

        resistive_names = [n for n, s in catalog.items() if s.get("combat_role") == "resistive"]
        save_ratings = [skills.get(n, {}) for n in resistive_names]
        return calculate_challenge_rating(_best("offense"), 0, 0, _best("defense"), save_ratings, max_hp)

    def test_fit_skills_to_cr_round_trips_through_calculate_challenge_rating(self):
        # Every case naming a real offense-role key_skill should land *exactly* on target_cr --
        # fit_skills_to_cr is meant to be an exact inverse of calculate_challenge_rating's own
        # math, not just "close" (see _resolve_offense_survival_split, the helper actually
        # doing this inversion under the new two-sided-product formula).
        cases = [
            (20, ["blades", "dodge", "athletics"]),       # offense + defense, no saves named
            (41, ["arcane", "linguistics"]),               # one offense skill + one flavor skill
            (60, ["blades", "dodge", "athletics", "strength", "brawling"]),  # multiple offense
        ]
        for target_cr, key_skills in cases:
            skills, max_hp = fit_skills_to_cr(key_skills, target_cr, FAKE_SKILLS_CATALOG)
            self.assertEqual(
                self._cr_from_skills(skills, max_hp), target_cr,
                f"key_skills={key_skills} target_cr={target_cr}",
            )

    def test_fit_skills_to_cr_with_no_combat_relevant_skill_named_reads_as_zero_cr(self):
        # No offense/defense/resistive-role key_skill at all -- offense_side is unavoidably 0,
        # so calculate_challenge_rating reads 0 regardless of how much HP this civilian's own
        # stat sheet gets (see Challenge_Rating.py's own module docstring) -- a deliberate
        # behavior change from the old flat-sum formula, where this same case still promised an
        # exact target_cr back purely from HP. fit_skills_to_cr still sizes max_hp off target_cr
        # (a generated civilian's stat sheet still looks reasonable), just no longer claims a
        # real, nonzero threat rating for it.
        skills, max_hp = fit_skills_to_cr(["stealth"], target_cr=10, skills_catalog=FAKE_SKILLS_CATALOG)
        self.assertEqual(self._cr_from_skills(skills, max_hp), 0)
        self.assertGreater(max_hp, 0)

    def test_fit_skills_to_cr_never_produces_negative_or_zero_dice(self):
        skills, max_hp = fit_skills_to_cr(["blades", "dodge"], target_cr=1, skills_catalog=FAKE_SKILLS_CATALOG)
        self.assertGreaterEqual(max_hp, 0)
        for stats in skills.values():
            self.assertGreaterEqual(stats["dice"], 1)

    def test_fit_skills_to_cr_dedupes_and_only_combat_role_tagged_skills_affect_cr(self):
        skills, max_hp = fit_skills_to_cr(
            ["blades", "blades", "dodge", "athletics", "strength"], target_cr=30,
            skills_catalog=FAKE_SKILLS_CATALOG,
        )
        self.assertEqual(len(skills), 4)  # deduped from 5 to 4
        self.assertEqual(self._cr_from_skills(skills, max_hp), 30)

        # blades/athletics are both "offense" -- tied with each other (only the single
        # best-rated one is ever actually read, so they're set equal); dodge is "defense", a
        # separate additive component that needn't match them. strength has no combat_role at
        # all -- flavor only, and rated lower than every combat-relevant skill here.
        offense_ratings = [skill_rating(skills[n]["dice"], skills[n]["pips"]) for n in ("blades", "athletics")]
        defense_rating = skill_rating(skills["dodge"]["dice"], skills["dodge"]["pips"])
        flavor_rating = skill_rating(skills["strength"]["dice"], skills["strength"]["pips"])
        self.assertEqual(len(set(offense_ratings)), 1)  # blades and athletics tied
        self.assertLess(flavor_rating, offense_ratings[0])
        self.assertLess(flavor_rating, defense_rating)

    def test_fit_skills_to_cr_empty_key_skills_still_produces_hp(self):
        skills, max_hp = fit_skills_to_cr([], target_cr=30, skills_catalog=FAKE_SKILLS_CATALOG)
        self.assertEqual(skills, {})
        self.assertGreater(max_hp, 0)

    def test_resolve_varied_value_passes_a_plain_scalar_through_unchanged(self):
        self.assertEqual(resolve_varied_value(0.6), 0.6)
        self.assertEqual(resolve_varied_value(40), 40)
        self.assertEqual(resolve_varied_value("the innkeeper running the bar tonight"), "the innkeeper running the bar tonight")

    def test_resolve_varied_value_range_picks_int_or_float_by_the_bounds_own_type(self):
        for _ in range(20):
            value = resolve_varied_value({"min": 10, "max": 50})
            self.assertIsInstance(value, int)
            self.assertTrue(10 <= value <= 50)
        for _ in range(20):
            value = resolve_varied_value({"min": 0.8, "max": 1.2})
            self.assertIsInstance(value, float)
            self.assertTrue(0.8 <= value <= 1.2)

    def test_resolve_varied_value_weighted_list_only_ever_returns_one_of_the_keys(self):
        options = [{"halfling": 20}, {"dwarf": 20}, {"elf": 20}, {"human": 60}, {"half-orc": 20}]
        for _ in range(20):
            self.assertIn(resolve_varied_value(options), {"halfling", "dwarf", "elf", "human", "half-orc"})

    def test_describe_qualities_covers_every_combination_of_gender_race_age(self):
        self.assertEqual(_describe_qualities(None), "")
        self.assertEqual(_describe_qualities({}), "")
        self.assertEqual(_describe_qualities({"race": "dwarf"}), "They are a dwarf.")
        self.assertEqual(
            _describe_qualities({"gender": "female", "race": "elf"}), "They are a female elf.",
        )
        self.assertEqual(
            _describe_qualities({"gender": "male", "race": "halfling", "age": 37}),
            "They are a male halfling, about 37 years old.",
        )
        self.assertEqual(_describe_qualities({"age": 40}), "They are about 40 years old.")

    def test_resolve_varied_value_weighted_list_weights_neednt_sum_to_100(self):
        # Relative weights, not percentages -- random.choices normalizes internally, so a
        # heavily lopsided list (99 vs 1) should still overwhelmingly favor the heavy option.
        options = [{"rare": 1}, {"common": 99}]
        picks = [resolve_varied_value(options) for _ in range(200)]
        self.assertGreater(picks.count("common"), picks.count("rare"))

    def test_load_npc_keywords_reads_the_real_catalog(self):
        keywords = load_npc_keywords()
        self.assertIn("warrior", keywords)
        self.assertIn("blades", keywords["warrior"])

    def test_generate_npc_stats_uses_the_scripted_llm(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {"name": "describe_npc", "arguments": json.dumps({
                "name": "Test Name", "backstory": "A test backstory.", "keywords": ["warrior"],
            })}}]}}]}

        npc_keywords = {"warrior": ["blades", "axes", "athletics", "strength"]}
        script_llm(self, fake_call)
        result = generate_npc_stats(
            npc_keywords, target_cr=20, skills_catalog=FAKE_SKILLS_CATALOG, variance=0)

        self.assertEqual(result["name"], "Test Name")
        self.assertEqual(result["description"], "A test backstory.")
        self.assertEqual(set(result["skills"]), set(npc_keywords["warrior"]))

    def test_generate_npc_stats_falls_back_when_the_llm_raises(self):
        def failing_call(*args, **kwargs):
            raise ConnectionError("no Ollama")

        npc_keywords = {"warrior": ["blades", "axes", "athletics", "strength"]}
        script_llm(self, failing_call)
        result = generate_npc_stats(
            npc_keywords, target_cr=20, skills_catalog=FAKE_SKILLS_CATALOG)

        self.assertEqual(result["name"], "Unnamed Stranger")
        self.assertTrue(result["skills"])

    def test_generate_npc_stats_falls_back_when_llm_names_an_unrecognized_keyword(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {"name": "describe_npc", "arguments": json.dumps({
                "name": "X", "backstory": "Y", "keywords": ["not_a_real_keyword"],
            })}}]}}]}

        npc_keywords = {"warrior": ["blades"]}
        script_llm(self, fake_call)
        result = generate_npc_stats(
            npc_keywords, target_cr=20, skills_catalog=FAKE_SKILLS_CATALOG)

        self.assertEqual(result["name"], "Unnamed Stranger")

    def test_generate_npc_stats_skip_llm_generation_never_calls_the_injected_callable(self):
        def exploding_call(*args, **kwargs):
            raise AssertionError("should never be called when skip_llm_generation is True")

        npc_keywords = {"warrior": ["blades", "axes", "athletics", "strength"]}
        script_llm(self, exploding_call)
        result = generate_npc_stats(
            npc_keywords, target_cr=20, skills_catalog=FAKE_SKILLS_CATALOG,
            skip_llm_generation=True,
        )
        self.assertTrue(result["skills"])


class TestAdHocGeneration(unittest.TestCase):
    """!
    @brief AdHoc_Generation.py's pure LLM-calling logic -- no DMCore, no live LLM (see
        TestImprovisation for the DMCore-side glue that actually mutates game state).
        generate_ad_hoc_item/decide_entity_removal's own scripted LLM transport
        dependency-injection seam is exercised directly here, the same style
        TestNpcGeneration uses for generate_npc_stats.
    """

    def test_create_item_returns_a_full_entity_dict(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "create_item",
                "arguments": json.dumps({
                    "name": "stone", "description": "A smooth grey stone.",
                    "subtype": "misc", "location": "ground", "value": 0,
                }),
            }}]}}]}

        script_llm(self, fake_call)
        result = generate_ad_hoc_item("a stone", "take", "A dusty antechamber.")

        self.assertTrue(result["created"])
        self.assertEqual(result["location"], "ground")
        entity = result["entity"]
        self.assertEqual(entity["name"], "stone")
        self.assertEqual(entity["supertype"], "object")
        self.assertTrue(entity["ad_hoc"])
        self.assertNotIn("damage_value", entity)
        self.assertNotIn("armor_value", entity)

    def test_weapon_flags_attach_damage_value(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "create_item",
                "arguments": json.dumps({
                    "name": "rusty knife", "description": "A pitted old knife.",
                    "subtype": "weapon", "location": "ground", "is_weapon": True,
                    "damage_dice": 1, "damage_pips": 0, "damage_tag": "slashing",
                }),
            }}]}}]}

        script_llm(self, fake_call)
        result = generate_ad_hoc_item("a rusty knife", "take", "A rubbish heap.")

        entity = result["entity"]
        self.assertEqual(entity["damage_value"], {"dice": 1, "pips": 0, "bonus": 0})
        self.assertEqual(entity["damage_tags"], ["slashing"])

    def test_equip_slot_only_kept_when_actually_valid(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "create_item",
                "arguments": json.dumps({
                    "name": "iron ring", "description": "A plain band.",
                    "subtype": "trinket", "location": "ground", "equip_slot": "ring",
                }),
            }}]}}]}

        script_llm(self, fake_call)
        valid = generate_ad_hoc_item(
            "a ring", "take", "A dungeon.", valid_equip_slots=["ring", "neck"])
        self.assertEqual(valid["entity"]["equip_slot"], "ring")

        script_llm(self, fake_call)
        invalid = generate_ad_hoc_item(
            "a ring", "take", "A dungeon.", valid_equip_slots=["rhand"])
        self.assertNotIn("equip_slot", invalid["entity"])

    def test_usable_healing_item_carries_healing_skill_stat(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "create_item",
                "arguments": json.dumps({
                    "name": "murky tonic", "description": "A cloudy, herbal-smelling tonic.",
                    "subtype": "potion", "location": "ground",
                    "usable": True, "is_healing": True, "healing_dice": 2, "healing_pips": 1,
                }),
            }}]}}]}

        script_llm(self, fake_call)
        result = generate_ad_hoc_item("a tonic", "use", "A dungeon.")

        entity = result["entity"]
        self.assertTrue(entity["usable"])
        self.assertEqual(entity["skills"]["healing"], {"dice": 2, "pips": 1})
        self.assertNotIn("poison", entity["skills"])

    def test_usable_poisonous_item_carries_poison_skill_stat_instead(self):
        # For balance -- not every improvised consumable is a free heal.
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "create_item",
                "arguments": json.dumps({
                    "name": "unlabeled vial", "description": "A vial of something acrid.",
                    "subtype": "potion", "location": "ground",
                    "usable": True, "is_poisonous": True, "poison_dice": 1, "poison_pips": 2,
                }),
            }}]}}]}

        script_llm(self, fake_call)
        result = generate_ad_hoc_item("a strange vial", "use", "A dungeon.")

        entity = result["entity"]
        self.assertTrue(entity["usable"])
        self.assertEqual(entity["skills"]["poison"], {"dice": 1, "pips": 2})
        self.assertNotIn("healing", entity["skills"])

    def test_non_usable_item_carries_no_usable_flag_or_skills(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "create_item",
                "arguments": json.dumps({
                    "name": "stone", "description": "A smooth grey stone.",
                    "subtype": "misc", "location": "ground",
                }),
            }}]}}]}

        script_llm(self, fake_call)
        result = generate_ad_hoc_item("a stone", "take", "A dungeon.")

        self.assertNotIn("usable", result["entity"])
        self.assertNotIn("skills", result["entity"])

    def test_decline_reports_not_created(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "decline", "arguments": json.dumps({"reason": "not plausible here"}),
            }}]}}]}

        script_llm(self, fake_call)
        result = generate_ad_hoc_item("the moon", "take", "A dungeon.")
        self.assertFalse(result["created"])

    def test_generate_ad_hoc_item_never_fabricates_when_the_llm_raises(self):
        def failing_call(*args, **kwargs):
            raise ConnectionError("no Ollama")

        script_llm(self, failing_call)
        result = generate_ad_hoc_item("a stone", "take", "A dungeon.")
        self.assertFalse(result["created"])
        self.assertEqual(result["reason"], "unavailable")

    def _capture_item_prompt(self, **kwargs):
        prompts = []

        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            prompts.append(messages[-1]["content"])
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "create_item",
                "arguments": json.dumps({"name": "peppers", "description": "Smoked peppers.", "value": 0.04}),
            }}]}}]}

        script_llm(self, fake_call)
        result = generate_ad_hoc_item(**kwargs)
        return prompts[0], result

    def test_a_trade_prompt_carries_the_quoted_price_and_pricing_note(self):
        prompt, _ = self._capture_item_prompt(
            phrase="the smoked peppers", intent="trade", scene_description="A market.",
            recent_narration="Barnaby offers smoked peppers for four coppers.",
            pricing_note="value is in gold pieces.",
        )
        self.assertIn("four coppers", prompt)
        self.assertIn("quoted price", prompt)
        self.assertIn("value is in gold pieces.", prompt)

    def test_only_a_trade_prompt_carries_recent_narration(self):
        # Anything else ("take the peppers") isn't priced off what someone just said.
        prompt, _ = self._capture_item_prompt(
            phrase="the smoked peppers", intent="take", scene_description="A market.",
            recent_narration="Barnaby offers smoked peppers for four coppers.",
        )
        self.assertNotIn("four coppers", prompt)
        self.assertNotIn("Pricing:", prompt)

    def test_a_fractional_value_survives(self):
        _, result = self._capture_item_prompt(phrase="peppers", intent="trade", scene_description="A market.")
        self.assertEqual(result["entity"]["value"], 0.04)

    def test_currency_amounts_read_cleanly(self):
        self.assertEqual(_currency_amount(5), 5)
        self.assertIsInstance(_currency_amount(5.0), int)
        self.assertEqual(_currency_amount("0.8"), 0.8)
        for junk in (None, "", "lots", -3, float("nan"), float("inf")):
            self.assertEqual(_currency_amount(junk), 0, junk)

    def test_decide_entity_removal_picks_a_real_name(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "remove_entity",
                "arguments": json.dumps({"name": "torch", "reason": "player asked"}),
            }}]}}]}

        script_llm(self, fake_call)
        result = decide_entity_removal(
            "get rid of that torch", "A dim hallway.", ["torch", "wolf"])
        self.assertTrue(result["removed"])
        self.assertEqual(result["name"], "torch")

    def test_decide_entity_removal_rejects_a_name_outside_removable_entities(self):
        # The enum constraint itself should already prevent this in practice; this covers the
        # runtime double-check in case a model ever echoes back something off-list anyway.
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "remove_entity",
                "arguments": json.dumps({"name": "not_a_real_name", "reason": "why not"}),
            }}]}}]}

        script_llm(self, fake_call)
        result = decide_entity_removal(
            "remove it", "A dim hallway.", ["torch"])
        self.assertFalse(result["removed"])

    def test_decide_entity_removal_short_circuits_on_no_removable_entities(self):
        def exploding_call(*args, **kwargs):
            raise AssertionError("should never be called with nothing removable")

        script_llm(self, exploding_call)
        result = decide_entity_removal("remove something", "desc", [])
        self.assertFalse(result["removed"])

    def test_decide_entity_removal_never_fabricates_when_the_llm_raises(self):
        def failing_call(*args, **kwargs):
            raise TimeoutError("slow")

        script_llm(self, failing_call)
        result = decide_entity_removal("remove the torch", "desc", ["torch"])
        self.assertFalse(result["removed"])

    def test_describe_scenery_reports_no_entity_created(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "describe_scenery",
                "arguments": json.dumps({"description": "Faint claw marks score the stone wall."}),
            }}]}}]}

        script_llm(self, fake_call)
        result = generate_ad_hoc_item("the wall", "examine", "A dungeon.")

        self.assertFalse(result["created"])
        self.assertTrue(result["scenery"])
        self.assertEqual(result["description"], "Faint claw marks score the stone wall.")

    def test_locked_container_carries_active_conditions_and_test(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "create_item",
                "arguments": json.dumps({
                    "name": "old crate", "description": "A battered wooden crate.",
                    "subtype": "container", "location": "ground",
                    "locked": True, "lock_skill": "finesse", "lock_difficulty": 11,
                    "contains_currency": 15,
                }),
            }}]}}]}

        script_llm(self, fake_call)
        result = generate_ad_hoc_item(
            "a crate", "examine", "A storeroom.", valid_skill_names=["finesse", "blades"],
        )

        entity = result["entity"]
        self.assertEqual(entity["subtype"], "container")
        self.assertEqual(entity["currency"], 15)
        self.assertIn("locked", entity["active_conditions"])
        self.assertIn("closed", entity["active_conditions"])
        self.assertEqual(entity["test"]["skill"], ["finesse"])
        self.assertEqual(entity["test"]["difficulty"], 11)
        self.assertEqual(entity["test"]["requires_condition"], "locked")
        self.assertEqual(entity["test"]["pass"], {"dismiss_condition": "locked"})

    def test_locked_container_falls_back_to_finesse_when_model_omits_a_valid_lock_skill(self):
        # Never a permanently unopenable object -- see AdHoc_Generation._resolve_test_skill.
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "create_item",
                "arguments": json.dumps({
                    "name": "old crate", "description": "A battered wooden crate.",
                    "subtype": "container", "location": "ground", "locked": True,
                }),
            }}]}}]}

        script_llm(self, fake_call)
        result = generate_ad_hoc_item(
            "a crate", "examine", "A storeroom.", valid_skill_names=["finesse", "blades"],
        )

        entity = result["entity"]
        self.assertIn("locked", entity["active_conditions"])
        self.assertEqual(entity["test"]["skill"], ["finesse"])

    def test_unlocked_container_has_no_test_but_still_has_currency_and_closed_condition(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "create_item",
                "arguments": json.dumps({
                    "name": "old crate", "description": "A battered wooden crate.",
                    "subtype": "container", "location": "ground", "contains_currency": 3,
                }),
            }}]}}]}

        script_llm(self, fake_call)
        result = generate_ad_hoc_item("a crate", "examine", "A storeroom.")

        entity = result["entity"]
        self.assertEqual(entity["currency"], 3)
        self.assertNotIn("locked", entity["active_conditions"])
        self.assertIn("closed", entity["active_conditions"])
        self.assertNotIn("test", entity)

    def test_trap_carries_armed_condition_and_fail_damage(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "create_item",
                "arguments": json.dumps({
                    "name": "spike trap", "description": "A row of sharpened spikes.",
                    "subtype": "trap", "location": "ground",
                    "disarm_skill": "finesse", "disarm_difficulty": 9,
                    "damage_dice": 3, "damage_pips": 0, "damage_tag": "piercing",
                }),
            }}]}}]}

        script_llm(self, fake_call)
        result = generate_ad_hoc_item(
            "a trap", "examine", "A corridor.", valid_skill_names=["finesse"])

        entity = result["entity"]
        self.assertEqual(entity["subtype"], "trap")
        self.assertIn("armed", entity["active_conditions"])
        self.assertEqual(entity["test"]["requires_condition"], "armed")
        self.assertEqual(entity["test"]["fail"]["damage"], {"dice": 3, "pips": 0, "bonus": 0})
        self.assertEqual(entity["test"]["fail"]["damage_tags"], ["piercing"])

    def test_generate_ad_hoc_creature_fits_skills_and_attaches_attack_when_hostile(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "create_creature",
                "arguments": json.dumps({
                    "name": "cave rat", "description": "A mangy, oversized rat.",
                    "keywords": ["brute"], "disposition": "hostile", "power": "moderate",
                }),
            }}]}}]}
        npc_keywords = {"brute": ["strength", "brawling", "fortitude"]}

        script_llm(self, fake_call)
        result = generate_ad_hoc_creature(
            "a rat", "A dank cellar.", target_cr=20, npc_keywords=npc_keywords,
            skills_catalog=FAKE_SKILLS_CATALOG)

        self.assertTrue(result["created"])
        entity = result["entity"]
        self.assertEqual(entity["supertype"], "creature")
        self.assertTrue(entity["ad_hoc"])
        self.assertEqual(entity["attitudes"]["default"][0], -100)
        self.assertGreater(entity["max_hp"], 0)
        self.assertEqual(set(entity["skills"]), {"strength", "brawling", "fortitude"})
        self.assertEqual(len(entity["abilities"]), 1)
        self.assertIn(entity["abilities"][0]["skill"], entity["skills"])
        self.assertEqual(len(entity["behavior"]), 2)
        self.assertEqual(entity["behavior"][1]["action"], entity["abilities"][0]["name"])

    def test_generate_ad_hoc_creature_non_hostile_has_no_abilities_or_behavior(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "create_creature",
                "arguments": json.dumps({
                    "name": "lost pilgrim", "description": "A weary traveler.",
                    "keywords": ["scholar"], "disposition": "friendly", "power": "weak",
                }),
            }}]}}]}
        npc_keywords = {"scholar": ["knowledge", "willpower"]}

        script_llm(self, fake_call)
        result = generate_ad_hoc_creature(
            "a pilgrim", "A dusty road.", target_cr=20, npc_keywords=npc_keywords,
            skills_catalog=FAKE_SKILLS_CATALOG)

        entity = result["entity"]
        self.assertEqual(entity["attitudes"]["default"][0], 60)
        self.assertNotIn("abilities", entity)
        self.assertNotIn("behavior", entity)

    def test_generate_ad_hoc_creature_short_circuits_on_empty_keyword_catalog(self):
        def exploding_call(*args, **kwargs):
            raise AssertionError("should never be called with no npc_keywords catalog")

        script_llm(self, exploding_call)
        result = generate_ad_hoc_creature(
            "a rat", "desc", target_cr=20, npc_keywords={}, skills_catalog={})
        self.assertFalse(result["created"])
        self.assertEqual(result["reason"], "no_keywords")

    def test_decide_entity_edit_returns_new_description(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "edit_entity",
                "arguments": json.dumps({
                    "name": "torch", "new_description": "A torch, now guttering and nearly spent.",
                    "reason": "player asked",
                }),
            }}]}}]}

        script_llm(self, fake_call)
        result = decide_entity_edit("the torch is almost burned out", "desc", ["torch"])

        self.assertTrue(result["edited"])
        self.assertEqual(result["name"], "torch")
        self.assertEqual(result["new_description"], "A torch, now guttering and nearly spent.")
        self.assertIsNone(result["apply_condition"])

    def test_decide_entity_edit_rejects_a_name_outside_editable_entities(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "edit_entity",
                "arguments": json.dumps({"name": "not_a_real_name", "new_description": "x", "reason": "why not"}),
            }}]}}]}

        script_llm(self, fake_call)
        result = decide_entity_edit("change it", "desc", ["torch"])
        self.assertFalse(result["edited"])

    def test_decide_entity_edit_short_circuits_on_no_editable_entities(self):
        def exploding_call(*args, **kwargs):
            raise AssertionError("should never be called with nothing editable")

        script_llm(self, exploding_call)
        result = decide_entity_edit("change something", "desc", [])
        self.assertFalse(result["edited"])


class TestImprovisation(DMTestCase):
    """!
    @brief DM_Improvisation.py's ImprovisationMixin -- the DMCore-side glue for ad hoc entity
        creation/removal. AdHoc_Generation.py's own LLM transport is never exercised
        here (see TestAdHocGeneration) -- generate_ad_hoc_item/decide_entity_removal are
        patched directly so these tests cover only the glue's own state mutation/dispatch.
        scenario "arena" (DMTestCase's own default) declares "wolf" twice (disambiguating to
        "wolf"/"wolf_2") plus "thane" alongside the player, gladstone.
    """

    def setUp(self):
        super().setUp()
        self.item_events = self._capture("item_interaction_resolved")
        self.not_understood_events = self._capture("action_not_understood")
        self.catalog_events = self._capture("item_catalog_updated")

    def _fake_creation(self, entity_overrides=None, location="ground", created=True, reason=None):
        if not created:
            return {"created": False, "reason": reason or "declined"}
        entity = {
            "name": "stone", "supertype": "object", "subtype": "misc",
            "description": "A smooth grey stone.", "value": 0, "ad_hoc": True,
        }
        if entity_overrides:
            entity.update(entity_overrides)
        return {"created": True, "entity": entity, "location": location}

    def test_ground_placement_take_ends_up_in_inventory_off_the_ground(self):
        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value=self._fake_creation(location="ground")):
            self.dm_core._on_improvisation_requested({
                "intent": "take", "phrase": "a stone", "input": "pick up a stone",
            })

        self.assertIn("stone", self.dm_core.entities["gladstone"]["inventory"])
        self.assertNotIn("stone", self.dm_core._current_ground_items())
        self.assertEqual(self.catalog_events, [
            {"entities": [{"name": "stone", "description": "A smooth grey stone.", "targetable": False}]},
        ])

    def test_ground_placement_examine_describes_without_taking(self):
        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value=self._fake_creation(location="ground")):
            self.dm_core._on_improvisation_requested({
                "intent": "examine", "phrase": "a stone", "input": "examine the stone",
            })

        result = self.item_events[-1]
        self.assertTrue(result["found"])
        self.assertIn("stone", self.dm_core._current_ground_items())
        self.assertNotIn("stone", self.dm_core.entities["gladstone"]["inventory"])

    def test_player_centric_intent_lands_in_inventory_regardless_of_ground_location(self):
        # "equip" is player-centric -- the item goes straight into inventory and re-dispatches,
        # even though the fake LLM response chose "ground" (see DM_Improvisation.py's own
        # module docstring for why these two intent categories can't share one code path).
        entity = self._fake_creation(entity_overrides={"name": "iron ring", "equip_slot": "ring"}, location="ground")
        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value=entity):
            self.dm_core._on_improvisation_requested({
                "intent": "equip", "phrase": "a ring", "input": "equip the ring",
            })

        self.assertIn("iron ring", self.dm_core.entities["gladstone"]["inventory"])
        self.assertEqual(self.dm_core.entities["gladstone"]["equipped"].get("ring"), "iron ring")
        self.assertNotIn("iron ring", self.dm_core._current_ground_items())

    def test_a_conjured_poisonous_consumable_actually_poisons_on_use(self):
        # End-to-end: creation -> "use" is player-centric so it lands straight in inventory and
        # re-dispatches -> DM_Inventory.py's _resolve_use_intent rolls the poison damage for real.
        entity = self._fake_creation(entity_overrides={
            "name": "unlabeled vial", "subtype": "potion", "usable": True,
            "skills": {"poison": {"dice": 2, "pips": 0}},
        })
        self._stub_roll_dice(7)
        starting_hp = Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone")

        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value=entity):
            self.dm_core._on_improvisation_requested({
                "intent": "use", "phrase": "the strange vial", "input": "drink the strange vial",
            })

        result = self.item_events[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["poisoned"], 7)
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone"), starting_hp - 7)

    def test_inventory_placement_examine_resolves_through_the_ordinary_pipeline(self):
        # Placement (place_new_item) and narration both go through the same redispatch every
        # other branch uses -- DM_Core.py's own source-resolution recognizes the item is
        # already in gladstone's inventory (see _on_item_interaction_detected's docstring), so
        # this no longer needs its own bespoke publish. found=True here is itself the real
        # regression guard: a broken source-resolution would fall back to the scene target's
        # own (empty) inventory and report not_present instead.
        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value=self._fake_creation(location="inventory")):
            self.dm_core._on_improvisation_requested({
                "intent": "examine", "phrase": "my pockets", "input": "check my pockets",
            })

        result = self.item_events[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["item_name"], "stone")
        self.assertIsNone(result["container"])
        self.assertIn("stone", self.dm_core.entities["gladstone"]["inventory"])

    def test_decline_falls_back_to_action_not_understood(self):
        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value=self._fake_creation(created=False)):
            self.dm_core._on_improvisation_requested({
                "intent": "take", "phrase": "the moon", "input": "take the moon",
            })

        self.assertEqual(len(self.not_understood_events), 1)
        self.assertEqual(self.item_events, [])

    def test_a_decline_quotes_the_item_not_the_whole_clause(self):
        # Found by playtest: the notice read "grab the glimmering object without looking." isn't something...
        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value=self._fake_creation(created=False)) as generate:
            self.dm_core._on_improvisation_requested({
                "intent": "take", "phrase": "grab the glimmering object without looking.",
                "item_phrase": "glimmering object", "input": "grab the glimmering object without looking.",
            })

        self.assertEqual(self.not_understood_events[0]["phrase"], "glimmering object")
        self.assertEqual(generate.call_args.args[0], "grab the glimmering object without looking.")

    def test_remove_entity_from_scene_strips_presence_and_prevents_respawn(self):
        self.assertIn("wolf", self.dm_core.scenario_entities)

        outcome = self.dm_core.remove_entity_from_scene("wolf")

        self.assertTrue(outcome["removed"])
        self.assertNotIn("wolf", self.dm_core.scenario_entities)
        self.assertIn("wolf", self.dm_core.removed_entities)

        # Simulates a revisit/reload re-instancing the scenario's own static entities list --
        # "wolf" must not respawn just because debug.toml still declares it (unlike "wolf_2",
        # a separate instance never removed, which should still be there).
        self.dm_core.load_scenario()
        self.assertNotIn("wolf", self.dm_core.scenario_entities)
        self.assertIn("wolf_2", self.dm_core.scenario_entities)

    def test_remove_entity_from_scene_refuses_to_remove_the_player(self):
        outcome = self.dm_core.remove_entity_from_scene(self.dm_core.player_name)

        self.assertFalse(outcome["removed"])
        self.assertIn("gladstone", self.dm_core.scenario_entities)
        self.assertNotIn("gladstone", self.dm_core.removed_entities)

    def test_attempt_entity_removal_excludes_the_player_from_the_candidate_set(self):
        captured = {}

        def fake_decide(phrase, scene_description, removable_entities, **kwargs):
            captured["removable_entities"] = removable_entities
            return {"removed": False}

        with patch("dm.DM_Improvisation.decide_entity_removal", side_effect=fake_decide):
            self.dm_core._attempt_entity_removal("get rid of the wolf")

        self.assertIn("wolf", captured["removable_entities"])
        self.assertNotIn("gladstone", captured["removable_entities"])

    def test_attempt_entity_removal_flags_live_hostiles_for_the_prompt(self):
        # arena's own wolf/wolf_2 are hostile by default (no [entity.attitudes] at all -- see
        # CLAUDE.md's "Combat"); thane is a positive-disposition ally, never hostile. This is
        # what decide_entity_removal's own hostile_entities param leans on to refuse "get rid
        # of the wolf, this fight is too hard" -- see AdHoc_Generation.py's own module note.
        captured = {}

        def fake_decide(phrase, scene_description, removable_entities, hostile_entities=None, **kwargs):
            captured["hostile_entities"] = hostile_entities
            return {"removed": False}

        with patch("dm.DM_Improvisation.decide_entity_removal", side_effect=fake_decide):
            self.dm_core._attempt_entity_removal("get rid of the wolf, this fight is too hard")

        self.assertIn("wolf", captured["hostile_entities"])
        self.assertIn("wolf_2", captured["hostile_entities"])
        self.assertNotIn("thane", captured["hostile_entities"])

    def test_attempt_entity_removal_does_not_flag_a_dead_hostile_as_live(self):
        # A defeated creature is fair game for an ordinary removal request (ex: "get rid of the
        # wolf's carcass") -- only a *live* threat needs the hostile-entities guardrail.
        self.dm_core.entities["wolf"]["hp"] = 0
        captured = {}

        def fake_decide(phrase, scene_description, removable_entities, hostile_entities=None, **kwargs):
            captured["hostile_entities"] = hostile_entities
            return {"removed": False}

        with patch("dm.DM_Improvisation.decide_entity_removal", side_effect=fake_decide):
            self.dm_core._attempt_entity_removal("get rid of the dead wolf")

        self.assertNotIn("wolf", captured["hostile_entities"])
        self.assertIn("wolf_2", captured["hostile_entities"])

    def test_attempt_entity_removal_end_to_end_via_help_channel(self):
        help_events = self._capture("help_resolved")

        def fake_decide(phrase, scene_description, removable_entities, **kwargs):
            return {"removed": True, "name": "wolf", "reason": "player asked"}

        with patch("dm.DM_Improvisation.decide_entity_removal", side_effect=fake_decide):
            self.dm_core._on_help_detected({"input": "adam, get rid of the wolf", "removal_candidate": True})

        self.assertNotIn("wolf", self.dm_core.scenario_entities)
        self.assertEqual(help_events[-1]["removed"], {"removed": True, "name": "wolf", "reason": "player asked"})

    def test_scenery_result_publishes_flavor_with_no_entity_created(self):
        entities_before = set(self.dm_core.entities)
        fake_result = {"created": False, "scenery": True, "description": "Claw marks score the stone."}

        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value=fake_result):
            self.dm_core._on_improvisation_requested({
                "intent": "examine", "phrase": "the wall", "input": "examine the wall",
            })

        result = self.item_events[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["description"], "Claw marks score the stone.")
        self.assertEqual(set(self.dm_core.entities), entities_before)  # nothing created
        self.assertEqual(self.not_understood_events, [])

    def _fake_container_creation(self, locked=True):
        entity = {
            "name": "old crate", "supertype": "object", "subtype": "container",
            "description": "A battered wooden crate.", "value": 0, "currency": 15,
            "active_conditions": {"closed": {"duration": "permanent", "dismiss": None}},
            "ad_hoc": True,
        }
        if locked:
            entity["active_conditions"]["locked"] = {"duration": "permanent", "dismiss": None}
            entity["test"] = {
                "difficulty": 8, "skill": ["finesse"], "requires_condition": "locked",
                "blocks_if_condition": "jammed",
                "pass": {"dismiss_condition": "locked"},
                "fail": {"condition": "jammed", "duration": "permanent", "dismiss": ""},
            }
        return {"created": True, "entity": entity, "location": "ground"}

    def test_conjured_container_becomes_the_addressable_scene_target(self):
        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value=self._fake_container_creation(locked=False)):
            self.dm_core._on_improvisation_requested({
                "intent": "examine", "phrase": "a crate", "input": "examine the old crate",
            })

        self.assertEqual(self.dm_core.scenario_entities[0], "old crate")
        self.assertNotIn("old crate", self.dm_core._current_ground_items())
        result = self.item_events[-1]
        self.assertTrue(result["found"])

        # Now openable/lootable exactly like a hand-authored container -- current_target isn't
        # touched by container placement (only "open"/"close"/self-examine need
        # _get_target_name(), not self.current_target), so this exercises the ordinary,
        # unchanged _resolve_open_close_intent path end to end.
        self.dm_core._on_item_interaction_detected({"intent": "open", "item_name": None, "input": "open the crate"})
        opened = self.item_events[-1]
        self.assertTrue(opened["found"])
        self.assertEqual(opened["container"], "old crate")

    def test_conjured_locked_container_can_be_picked_then_opened(self):
        # No fight currently engaged -- the realistic case for discovering a container while
        # exploring -- so _claim_current_target_if_free actually claims it (see
        # test_attempt_creature_conjuring_does_not_steal_target_from_an_engaged_fight for the
        # opposite case).
        self.dm_core.current_target = None
        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value=self._fake_container_creation(locked=True)):
            self.dm_core._on_improvisation_requested({
                "intent": "examine", "phrase": "a crate", "input": "examine the old crate",
            })

        self.assertTrue(self.dm_core.is_locked("old crate"))
        self.assertEqual(self.dm_core.current_target, "old crate")

        self._stub_roll_dice(20)  # guarantee the lock pick succeeds
        self.dm_core._on_turn_detected({
            "clauses": [{"kind": "action", "skill": "finesse", "score": 1.0}],
            "input": "pick the lock",
        })

        self.assertFalse(self.dm_core.is_locked("old crate"))

    def test_conjured_trap_deals_damage_on_a_failed_disarm(self):
        entity = {
            "name": "spike trap", "supertype": "object", "subtype": "trap",
            "description": "A row of sharpened spikes.", "value": 0,
            "active_conditions": {"armed": {"duration": "permanent", "dismiss": None}},
            "test": {
                "difficulty": 20, "skill": ["finesse"], "requires_condition": "armed",
                "blocks_if_condition": "triggered",
                "pass": {"dismiss_condition": "armed"},
                "fail": {
                    "condition": "triggered", "duration": "permanent", "dismiss": "",
                    "damage": {"dice": 3, "pips": 0, "bonus": 0}, "damage_tags": ["piercing"],
                },
            },
            "ad_hoc": True,
        }
        fake_result = {"created": True, "entity": entity, "location": "ground"}
        starting_hp = Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone")
        self.dm_core.current_target = None  # no fight engaged -- see the container test's own note

        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value=fake_result):
            self.dm_core._on_improvisation_requested({
                "intent": "examine", "phrase": "a trap", "input": "examine the spike trap",
            })

        self.assertEqual(self.dm_core.scenario_entities[0], "spike trap")
        self.assertEqual(self.dm_core.current_target, "spike trap")

        # random.randint (not roll_dice) mocked -- same style TestLockedChest already uses --
        # so the trap's own damage roll (3 dice) still nets more than gladstone's chain mail
        # armor reduction (2 dice) rather than the two coincidentally cancelling out.
        with patch("random.randint", return_value=1):  # 3 dice @ 1 = 3, well under test difficulty 20
            self.dm_core._on_turn_detected({
                "clauses": [{"kind": "action", "skill": "finesse", "score": 1.0}],
                "input": "try to disarm it",
            })

        self.assertTrue(self.dm_core.entities["spike trap"]["active_conditions"].get("triggered"))
        self.assertLess(Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone"), starting_hp)

    def _fake_hostile_creature(self, name="cave rat"):
        entity = {
            "name": name, "description": "A mangy, oversized rat.",
            "supertype": "creature", "subtype": "npc", "max_hp": 9,
            "skills": {"brawling": {"dice": 2, "pips": 0}},
            "attitudes": {"default": [-100, 0, 0]},
            "abilities": [{
                "name": f"{name} attack", "supertype": "innate", "subtype": "weapon",
                "skill": "brawling", "damage_value": {"dice": 1, "pips": 0, "bonus": 0},
                "damage_tags": ["physical"],
            }],
            "behavior": [{"requirements": [{"field": "hp_per_remain", "operator": ">=", "value": 0.01}], "action": f"{name} attack"}],
            "ad_hoc": True,
        }
        return {"created": True, "entity": entity}

    def test_attempt_creature_conjuring_hostile_joins_scene_and_becomes_current_target(self):
        self.dm_core.current_target = None  # no fight already engaged (arena's own wolves aside)
        with patch("dm.DM_Improvisation.generate_ad_hoc_creature", return_value=self._fake_hostile_creature()):
            outcome = self.dm_core._attempt_creature_conjuring("summon a rat")

        self.assertTrue(outcome["created_creature"])
        self.assertIn("cave rat", self.dm_core.scenario_entities)
        self.assertTrue(self.dm_core.is_hostile("cave rat", self.dm_core.player_name))
        self.assertEqual(self.dm_core.current_target, "cave rat")
        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "cave rat"), Combat_Resolution.get_band(self.dm_core.world, "gladstone"))

    def test_attempt_creature_conjuring_does_not_steal_target_from_an_engaged_fight(self):
        self.dm_core.current_target = "wolf"  # already engaged with a live hostile

        with patch("dm.DM_Improvisation.generate_ad_hoc_creature", return_value=self._fake_hostile_creature()):
            self.dm_core._attempt_creature_conjuring("summon a rat")

        self.assertEqual(self.dm_core.current_target, "wolf")
        self.assertIn("cave rat", self.dm_core.scenario_entities)

    def test_attempt_creature_conjuring_declines_reports_false(self):
        with patch("dm.DM_Improvisation.generate_ad_hoc_creature", return_value={"created": False, "reason": "declined"}):
            outcome = self.dm_core._attempt_creature_conjuring("summon a dragon")

        self.assertFalse(outcome["created_creature"])

    def test_attempt_entity_edit_changes_description_and_tags_edited(self):
        def fake_decide(phrase, scene_description, editable_entities, **kwargs):
            return {
                "edited": True, "name": "wolf", "reason": "player asked",
                "new_description": "A scarred, one-eyed wolf.",
                "apply_condition": None, "dismiss_condition": None,
            }

        with patch("dm.DM_Improvisation.decide_entity_edit", side_effect=fake_decide):
            outcome = self.dm_core._attempt_entity_edit("the wolf has a scar over one eye")

        self.assertTrue(outcome["edited"])
        self.assertEqual(self.dm_core.entities["wolf"]["description"], "A scarred, one-eyed wolf.")
        self.assertTrue(self.dm_core.entities["wolf"]["edited"])

    def test_attempt_entity_edit_excludes_the_player_from_the_candidate_set(self):
        captured = {}

        def fake_decide(phrase, scene_description, editable_entities, **kwargs):
            captured["editable_entities"] = editable_entities
            return {"edited": False}

        with patch("dm.DM_Improvisation.decide_entity_edit", side_effect=fake_decide):
            self.dm_core._attempt_entity_edit("change the wolf")

        self.assertIn("wolf", captured["editable_entities"])
        self.assertNotIn("gladstone", captured["editable_entities"])

    def test_attempt_entity_edit_end_to_end_via_help_channel(self):
        help_events = self._capture("help_resolved")

        def fake_decide(phrase, scene_description, editable_entities, **kwargs):
            return {
                "edited": True, "name": "wolf", "reason": "player asked",
                "new_description": "A scarred, one-eyed wolf.", "apply_condition": None,
                "dismiss_condition": None,
            }

        with patch("dm.DM_Improvisation.decide_entity_edit", side_effect=fake_decide):
            self.dm_core._on_help_detected({"input": "adam, the wolf has a scar", "edit_candidate": True})

        self.assertEqual(self.dm_core.entities["wolf"]["description"], "A scarred, one-eyed wolf.")
        self.assertEqual(help_events[-1]["edited"]["name"], "wolf")

    def test_ad_hoc_item_name_colliding_with_a_live_entity_gets_disambiguated(self):
        # arena's own "wolf"/"wolf_2" are already live (see this class's own docstring) -- an
        # ad hoc item whose LLM-invented name collides with one must not silently overwrite it
        # (self.entities[name] = entity used to do exactly that before _unique_entity_key).
        entity = self._fake_creation(entity_overrides={"name": "wolf"}, location="ground")
        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value=entity):
            self.dm_core._on_improvisation_requested({
                "intent": "take", "phrase": "a wolf figurine", "input": "take the wolf figurine",
            })

        self.assertEqual(self.dm_core.entities["wolf"]["supertype"], "creature")  # untouched
        self.assertIn("wolf_3", self.dm_core.entities)  # wolf/wolf_2 already taken
        self.assertEqual(self.dm_core.entities["wolf_3"]["supertype"], "object")
        self.assertEqual(self.dm_core.entities["wolf_3"]["name"], "wolf")  # display text unchanged
        self.assertEqual(self.dm_core.entities["wolf_3"]["entity_id"], "wolf_3")
        self.assertIn("wolf_3", self.dm_core.entities["gladstone"]["inventory"])

    def test_ad_hoc_creature_name_colliding_with_the_player_does_not_clobber_them(self):
        with patch("dm.DM_Improvisation.generate_ad_hoc_creature", return_value=self._fake_hostile_creature(name="gladstone")):
            outcome = self.dm_core._attempt_creature_conjuring("summon my evil twin")

        self.assertTrue(outcome["created_creature"])
        self.assertEqual(outcome["name"], "gladstone_2")
        self.assertTrue(self.dm_core.entities["gladstone"]["is_player"])  # real player untouched
        self.assertIn("gladstone_2", self.dm_core.scenario_entities)
        self.assertEqual(self.dm_core.entities["gladstone_2"]["supertype"], "creature")

    def test_conjured_container_is_placed_at_the_players_current_band_not_band_1(self):
        self.dm_core.entities["gladstone"]["band"] = 3
        self.dm_core.current_target = None
        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value=self._fake_container_creation(locked=False)):
            self.dm_core._on_improvisation_requested({
                "intent": "examine", "phrase": "a crate", "input": "examine the old crate",
            })

        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "old crate"), 3)


class TestPlaceNewEntity(DMTestCase):
    """!
    @brief RulesMixin._place_new_entity (DM_Rules.py) -- the shared primitive
        _instance_entities and every band-bearing DM_Improvisation.py placement path (a
        conjured container/trap, a conjured creature) go through, instead of each hand-writing
        entity_id/band/active_conditions. Exercised directly here, with no scenario load or
        NLP pipeline involved.
    """

    def test_copies_active_conditions_from_a_templates_own_conditions_field(self):
        entity = {"name": "chest", "conditions": {"locked": {"duration": "permanent", "dismiss": None}}}
        result = self.dm_core._place_new_entity("chest", entity, band=2)

        self.assertIs(result, entity)
        self.assertEqual(entity["entity_id"], "chest")
        self.assertEqual(entity["band"], 2)
        self.assertEqual(entity["active_conditions"], {"locked": {"duration": "permanent", "dismiss": None}})
        self.assertIsNot(entity["active_conditions"], entity["conditions"])
        self.assertIs(self.dm_core.entities["chest"], entity)

    def test_preserves_active_conditions_already_authored_on_the_entity(self):
        entity = {"name": "trap", "active_conditions": {"armed": {"duration": "permanent", "dismiss": None}}}
        self.dm_core._place_new_entity("trap", entity, band=1)

        self.assertEqual(entity["active_conditions"], {"armed": {"duration": "permanent", "dismiss": None}})

    def test_defaults_active_conditions_to_empty_when_neither_field_is_present(self):
        entity = {"name": "goblin"}
        self.dm_core._place_new_entity("goblin", entity, band=1)

        self.assertEqual(entity["active_conditions"], {})


class TestNpcGenerationDMCoreIntegration(DMTestCase):
    """!
    @brief _instance_entities' entity_template branch (DM_Rules.py/DM_NpcGeneration.py)
        against debug.toml's own local "generated_stranger" entity_template --
        no live LLM, LLM_Client's transport is scripted with a deterministic
        fake so this stays part of the fast offline suite (see test_integration.py for a real
        end-to-end Ollama round trip).
    """
    scenario_name = "debug"
    start_location = "generation_grounds"

    def setUp(self):
        self.fake_call_log = []
        self.fake_call_prompts = []

        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            self.fake_call_log.append(1)
            self.fake_call_prompts.append(messages[-1]["content"])
            return {"choices": [{"message": {"tool_calls": [{"function": {"name": "describe_npc", "arguments": json.dumps({
                "name": f"Generated NPC {len(self.fake_call_log)}",
                "backstory": "A backstory from the fake LLM.",
                "keywords": ["warrior"],
            })}}]}}]}

        self._fake_call = fake_call
        with scripted_llm(fake_call):
            super().setUp()

        self.slot_dirs = []

    def tearDown(self):
        for slot_dir in self.slot_dirs:
            shutil.rmtree(slot_dir, ignore_errors=True)

    def _track(self, slot_name):
        self.slot_dirs.append(self.dm_core._save_slot_dir(slot_name))
        return slot_name

    def test_generate_true_template_gets_real_skills_name_and_generated_flag(self):
        entity = self.dm_core.entities["generated_stranger"]
        self.assertEqual(entity["name"], "Generated NPC 1")
        self.assertTrue(entity["generated"])
        self.assertEqual(set(entity["skills"]), {"blades", "axes", "athletics", "strength"})
        self.assertGreater(entity["max_hp"], 0)
        self.assertEqual(len(self.fake_call_log), 1)

    def test_qualities_are_resolved_before_and_fed_into_the_llm_prompt(self):
        # gender/race/age must already be concrete by the time the LLM is asked for a name --
        # otherwise the two are decided independently and can disagree (ex: an invented name
        # that reads as feminine paired with a separately-rolled gender = "male").
        entity = self.dm_core.entities["generated_stranger"]
        qualities = entity["qualities"]
        prompt = self.fake_call_prompts[0]
        self.assertIn(qualities["gender"], prompt)
        self.assertIn(qualities["race"], prompt)
        self.assertIn(str(qualities["age"]), prompt)

    def test_varied_currency_qualities_and_attitudes_all_resolve_to_concrete_values(self):
        # generated_stranger's own currency/qualities/attitudes mix fixed and varied fields --
        # every one of them should come out a plain scalar, never a leftover {"min", "max"}
        # dict or a weighted-choice list.
        entity = self.dm_core.entities["generated_stranger"]

        self.assertIsInstance(entity["currency"], int)
        self.assertTrue(10 <= entity["currency"] <= 50)

        qualities = entity["qualities"]
        self.assertIn(qualities["race"], {"halfling", "dwarf", "elf", "human", "half-orc"})
        self.assertIn(qualities["gender"], {"male", "female"})
        self.assertIsInstance(qualities["age"], int)
        self.assertTrue(18 <= qualities["age"] <= 50)

        default = entity["attitudes"]["default"]
        self.assertTrue(all(isinstance(axis, (int, float)) for axis in default))
        disposition, threat, familiarity = default
        self.assertTrue(-40 <= disposition <= 40)
        self.assertEqual(threat, 0)
        self.assertTrue(-40 <= familiarity <= 40)

    def test_player_attitude_token_is_substituted_with_the_live_player_name(self):
        # debug.toml's own generated_stranger authors this override toward the
        # literal token "player" -- it must resolve to whichever entity is actually
        # is_player = true (gladstone), not stay keyed to a string no live entity is ever named.
        name_overrides = self.dm_core.entities["generated_stranger"]["attitudes"]["name"]
        self.assertEqual(len(name_overrides), 1)
        override = name_overrides[0]
        self.assertIn(self.dm_core.player_name, override)
        self.assertNotIn("player", override)
        self.assertEqual(override[self.dm_core.player_name], [40, 0, 0])

    def test_generation_never_touches_abilities_equipped_or_inventory(self):
        # Combat/dialogue capability is decided separately, by whoever authors the
        # entity_template -- generation only ever fills in the stat block + flavor text (name/
        # description/skills/max_hp/currency/qualities/attitudes), never gear. generated_stranger
        # authors no [entity_template.equipped]/abilities/inventory of its own, so a generated
        # instance should end up with none either.
        entity = self.dm_core.entities["generated_stranger"]
        self.assertEqual(entity.get("equipped", {}), {})
        self.assertEqual(entity.get("abilities", []), [])
        self.assertEqual(entity.get("inventory", []), [])

    def test_target_cr_player_resolves_against_the_live_player(self):
        # generated_stranger's own target_cr = "player" -- generated with variance=0.15 (the
        # module default, since its own template doesn't override it), so it should
        # land in a generous but bounded band around gladstone's own real CR, not some
        # unrelated fixed number.
        npc_cr = Combat_Actions.get_challenge_rating(self.dm_core.world, "generated_stranger")
        player_cr = Combat_Actions.get_challenge_rating(self.dm_core.world, self.dm_core.player_name)
        self.assertLess(abs(npc_cr - player_cr), player_cr * 0.5)

    def test_describe_character_surfaces_the_generated_name_not_the_template_key(self):
        description = self.dm_core.describe_character("generated_stranger")
        self.assertTrue(description.startswith("Generated NPC 1"))
        self.assertNotIn("generated_stranger -", description)

    def test_save_then_load_restores_the_original_generation_not_a_new_one(self):
        original = self.dm_core.entities["generated_stranger"]
        original_name = original["name"]
        original_skills = dict(original["skills"])
        original_max_hp = original["max_hp"]
        # Also captured: the newer, randomly-varied fields (currency/qualities/attitudes) --
        # these have no static template to fall back to at all (unlike skills/max_hp/name/
        # description, which at least *look* like ordinary entity fields), so a reload that
        # regenerated fresh values for them instead of restoring the saved ones would be a
        # real, visible bug (a different race/attitude after every load).
        original_currency = original["currency"]
        original_qualities = dict(original["qualities"])
        original_attitudes = json.loads(json.dumps(original["attitudes"]))
        slot = self._track("npc_gen_test_slot")
        self.dm_core.save_game(slot)

        # A *different* fake generation, proving the overlay -- not this call -- is what the
        # reloaded entity actually reflects (skip_llm_generation routes load's own
        # re-instancing to the offline fallback path, so this is never even invoked -- see
        # the log length assertion below).
        def different_fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {"name": "describe_npc", "arguments": json.dumps({
                "name": "A Completely Different NPC", "backstory": "Different.", "keywords": ["scholar"],
            })}}]}}]}

        with scripted_llm(different_fake_call):
            self.dm_core.load_game(slot)

        reloaded = self.dm_core.entities["generated_stranger"]
        self.assertEqual(reloaded["name"], original_name)
        self.assertEqual(reloaded["skills"], original_skills)
        self.assertEqual(reloaded["max_hp"], original_max_hp)
        self.assertEqual(reloaded["currency"], original_currency)
        self.assertEqual(reloaded["qualities"], original_qualities)
        self.assertEqual(reloaded["attitudes"], original_attitudes)
        self.assertEqual(len(self.fake_call_log), 1)  # only the original setUp() generation

    def test_entity_templates_are_never_accidentally_referenced(self):
        # debug.toml's own "vault_specter_stub" is a real entity_template that
        # its own [scenario].entities deliberately never references (see
        # TestScenarioLocalEntities) -- unlike self.dm_core (this class's own
        # npc_generation_test fixture, which already has a *live*, generated
        # "generated_stranger" instance sitting in self.entities under that same key --
        # self.entities holds templates and live instances under the same keys, see CLAUDE.md's
        # "Scenarios and rooms"), self.entities here has no "vault_specter_stub" at all -- only
        # self.entity_templates does, proving the lookup itself is what's isolated, not just
        # that this particular scenario never happens to collide.
        dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="vault")
        self.assertIn("vault_specter_stub", dm.entity_templates)
        self.assertNotIn("vault_specter_stub", dm.entities)

        errors = []
        dm.event_bus.subscribe("log_error", errors.append)

        # A scenario entry naming an entity_template via "name" (the field real entities use)
        # must fail the same "unknown entity" way a real typo would, not silently resolve it.
        result = dm._instance_entities([{"name": "vault_specter_stub", "band": 1}])
        self.assertEqual(result, [])
        self.assertTrue(any("unknown entity" in e for e in errors))

        # Conversely, a real entity/creature template can't be pulled through "template"
        # either -- self.entity_templates has no "vault sentinel" entry to find.
        errors.clear()
        result = dm._instance_entities([{"template": "vault sentinel", "band": 1}])
        self.assertEqual(result, [])
        self.assertTrue(any("unknown entity template" in e for e in errors))


class TestNarratedPopulation(DMTestCase):
    """!
    @brief Narration-driven population -- the narrator populates a scene and the engine makes the
        people it mentions real (DM_Improvisation.py's _extract_scene_population/
        _apply_pending_population, AdHoc_Generation.py's extract_narrated_people). debug.toml's
        own town_square is the fixture: it opts in with population = "narrated".
    """
    start_location = "town_square"

    def _keyword(self):
        return next(iter(load_npc_keywords("Rules/Fantasy")))

    def _person(self, **overrides):
        person = {
            "name": "Marla Venn", "occupation": "fishmonger",
            "description": "A weather-beaten woman gutting the morning's catch.",
            "race": "human", "gender": "female", "age": 44,
            "memories": ["The catch has been poor this week."],
            "keywords": [self._keyword()], "disposition": "friendly", "power": "weak",
        }
        person.update(overrides)
        return person

    def _reply(self, people):
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "report_people", "arguments": json.dumps({"people": people}),
        }}]}}]}

    def _populate(self, people, text="Marla Venn gutting fish beside the crier."):
        with scripted_llm(return_value=self._reply(people)):
            queued = self.dm_core._extract_scene_population(text)
        self.dm_core._apply_pending_population()
        return queued

    def _crowd(self):
        return [n for n in self.dm_core.scenario_entities if self.dm_core.entities[n].get("source") == "narration"]

    def test_a_narrated_person_is_published_as_targetable(self):
        catalog = []
        self.dm_core.event_bus.subscribe("item_catalog_updated", catalog.append)
        self._populate([self._person()])
        [name] = self._crowd()
        self.assertIn({"name": name, "description": self.dm_core.entities[name].get("description", ""),
                       "targetable": True}, [entry for event in catalog for entry in event["entities"]])

    def test_a_narrated_person_keeps_their_voice(self):
        self._populate([self._person(voice="Brisk; haggles over every copper")])
        [name] = self._crowd()
        self.assertEqual(self.dm_core.entities[name]["voice"], "Brisk; haggles over every copper")

    def test_a_narrated_person_becomes_a_real_scene_entity(self):
        self.assertEqual(self._populate([self._person()]), 1)

        [name] = self._crowd()
        entity = self.dm_core.entities[name]
        self.assertEqual(entity["name"], "Marla Venn")
        self.assertEqual(entity["qualities"]["occupation"], "fishmonger")
        self.assertEqual(entity["memories"], ["The catch has been poor this week."])
        self.assertTrue(entity["background"] and entity["ad_hoc"])
        self.assertGreaterEqual(entity["max_hp"], 1)

    def test_a_narrated_person_is_never_able_to_fight(self):
        self._populate([self._person()])

        [name] = self._crowd()
        entity = self.dm_core.entities[name]
        self.assertFalse(entity.get("abilities") or entity.get("behavior"))
        self.assertFalse(self.dm_core.is_hostile(name, self.dm_core.player_name))

    def test_a_hostile_disposition_is_dropped_rather_than_defanged(self):
        self.assertEqual(self._populate([self._person(name="Bandit Kell", disposition="hostile")]), 0)
        self.assertEqual(self._crowd(), [])

    def test_the_occupation_addresses_a_person_who_has_their_own_name(self):
        self._populate([self._person()])

        [name] = self._crowd()
        self.assertEqual(self.dm_core._literal_dialogue_target('i approach the fishmonger. "any news?"'), name)
        self.assertEqual(self.dm_core._literal_dialogue_target("ask marla venn about the catch"), name)

    def test_the_engine_owns_the_mechanics_whatever_the_narrator_claims(self):
        # Fields outside the setting's freeform list are ignored, so "skills"/"max_hp" from the
        # model can never reach the entity.
        self._populate([self._person(skills={"brawling": {"dice": 9, "pips": 0}}, max_hp=999)])

        [name] = self._crowd()
        entity = self.dm_core.entities[name]
        self.assertNotEqual(entity["max_hp"], 999)
        self.assertNotIn("brawling", {k for k, v in entity["skills"].items() if v["dice"] == 9})

    def test_someone_the_game_already_knows_is_never_duplicated(self):
        self.assertEqual(self._populate([self._person(name="Town Crier")]), 1)

        self.assertEqual(self._crowd(), [])

    def test_the_scene_budget_caps_how_many_people_are_made(self):
        limit = self.dm_core._population_settings()["max"]
        people = [self._person(name=f"Person Number{i}", occupation=f"trade{i}") for i in range(limit + 3)]

        self._populate(people)

        self.assertLessEqual(len(self._crowd()), limit)

    def test_a_batch_is_dropped_if_the_player_left_the_scene(self):
        with scripted_llm(return_value=self._reply([self._person()])):
            self.dm_core._extract_scene_population("Marla Venn gutting fish.")
        self.dm_core.current_location_key = "debug_hub"
        self.dm_core._apply_pending_population()

        self.assertEqual(self._crowd(), [])

    def test_a_location_that_did_not_opt_in_is_never_read(self):
        self.dm_core._current_location()["population"] = "none"
        with scripted_llm() as never_called:
            self.dm_core._on_scene_narration_ready({"text": "A crowd mills about.", "label": "scenario_intro"})

        self.assertEqual(never_called.call_count, 0)

    def test_only_scene_setting_narrations_are_read(self):
        # NPC dialogue isn't a trigger: someone a speaker merely mentions isn't standing there.
        with scripted_llm() as never_called:
            self.dm_core._on_scene_narration_ready({"text": "Ask Marla, she knows.", "label": "dialogue:innkeeper"})

        self.assertEqual(never_called.call_count, 0)

    def test_people_introduced_mid_scene_are_read_too(self):
        # Found by playtest: a vendor the narrator introduced in a clarification and a stranger in
        # a skill result never became real, so fifty turns of fighting them targeted nothing.
        for label in ("clarification", "skill_response", "item_interaction:take"):
            with scripted_llm(return_value=self._reply([self._person()])) as called:
                self.dm_core._on_scene_narration_ready({"text": "Marla gutting fish.", "label": label})
            self.assertEqual(called.call_count, 1, label)
            self.dm_core._pending_population.clear()

    def test_queued_people_are_made_real_before_the_next_input_is_routed(self):
        # Found by playtest: the intro's people stayed unreal until some input happened to reach a
        # DMCore handler, so the first turns of talking to them were routed as nobody-here.
        with scripted_llm(return_value=self._reply([self._person()])):
            self.dm_core._extract_scene_population("Marla Venn gutting fish.")

        self.dm_core.event_bus.publish("player_input_received", "how's the catch?")

        self.assertEqual(len(self._crowd()), 1)

    def test_an_input_waits_for_an_extraction_still_running(self):
        def slow_reply(*_args, **_kwargs):
            time.sleep(0.5)
            return self._reply([self._person()])

        with scripted_llm(side_effect=slow_reply):
            worker = threading.Thread(target=self.dm_core._on_scene_narration_ready,
                                      args=({"text": "Marla gutting fish.", "label": "scenario_intro"},))
            worker.start()
            while self.dm_core._population_in_flight == 0 and worker.is_alive():
                time.sleep(0.01)
            self.dm_core.event_bus.publish("player_input_received", "how's the catch?")
            worker.join()

        self.assertEqual(len(self._crowd()), 1)

    def test_an_unreachable_extraction_model_is_logged_as_a_warning(self):
        warnings = []
        self.dm_core.event_bus.subscribe("log_warning", warnings.append)
        with scripted_llm(side_effect=ConnectionError):
            self.dm_core._extract_scene_population("Marla Venn gutting fish.")

        self.assertTrue(any("extraction model unavailable" in warning for warning in warnings))

    def test_extraction_runs_on_a_scene_setting_narration(self):
        with scripted_llm(return_value=self._reply([self._person()])):
            self.dm_core._on_scene_narration_ready({"text": "Marla gutting fish.", "label": "item_interaction:move"})
        self.dm_core._apply_pending_population()

        self.assertEqual(len(self._crowd()), 1)

    def test_no_one_is_made_while_a_hostile_is_present(self):
        self.dm_core.entities["angry wolf"] = {
            "name": "angry wolf", "supertype": "creature", "max_hp": 10, "hp": 10,
            "attitudes": {"default": [-100, 0, 0]}, "skills": {"brawling": {"dice": 2, "pips": 0}},
        }
        self.dm_core._place_new_entity("angry wolf", self.dm_core.entities["angry wolf"], 1)
        self.dm_core.scenario_entities.append("angry wolf")

        self.assertEqual(self._populate([self._person()]), 0)

    def test_a_populated_scene_survives_save_and_reload(self):
        self._populate([self._person()])
        [name] = self._crowd()
        before = dict(self.dm_core.entities[name])

        store = MemorySlotStore()
        self.dm_core.slot_store = store
        self.dm_core.save_game("crowd_slot")
        reloaded = DMCore(
            ValidatingEventBus(), scenario_name="debug", start_location="town_square", setting="Fantasy", slot_store=store,
        )
        reloaded.load_game("crowd_slot")

        self.assertIn(name, reloaded.scenario_entities)
        self.assertEqual(reloaded.entities[name]["name"], before["name"])
        self.assertEqual(reloaded.entities[name]["qualities"], before["qualities"])
        self.assertEqual(reloaded.entities[name]["attitudes"], before["attitudes"])

    def test_scene_setting_prose_is_longer_and_asks_for_people_only_where_opted_in(self):
        from types import SimpleNamespace
        core = SimpleNamespace(population=self.dm_core._population_prompt_settings())
        text = NarratorState.scene_length_instruction(core, "the opening scene")

        self.assertIn("5-6 sentences", text)
        self.assertIn("market stallholders", text)
        core = SimpleNamespace(population={"sentences": "2-3", "hint": ""})
        self.assertEqual(
            NarratorState.scene_length_instruction(core, "the opening scene"),
            "Narrate the opening scene in 2-3 sentences as the Game Master.",
        )

    def test_the_extraction_schema_excludes_hostility_and_engine_owned_fields(self):
        from resolution.AdHoc_Generation import build_people_tool_schema
        item = build_people_tool_schema({"trade": ["haggling"]})[0]["function"]["parameters"]["properties"]["people"]["items"]

        self.assertNotIn("hostile", item["properties"]["disposition"]["enum"])
        for engine_owned in ("skills", "max_hp", "abilities", "behavior"):
            self.assertNotIn(engine_owned, item["properties"])

    def test_a_setting_can_narrow_what_the_narrator_may_write(self):
        from resolution.AdHoc_Generation import build_people_tool_schema
        item = build_people_tool_schema({"trade": ["haggling"]}, ("name", "description"))[0]["function"]["parameters"]["properties"]["people"]["items"]

        self.assertNotIn("memories", item["properties"])
        self.assertIn("description", item["properties"])

    def test_validation_flags_a_narrated_location_in_a_setting_that_never_enabled_it(self):
        errors = self._capture("log_error")
        self.dm_core.rules["narration_population"] = {"enabled": False}
        self.dm_core.validate_loaded_data()

        self.assertTrue([e for e in errors if "town_square" in e and "no effect" in e])

    def test_validation_flags_a_bad_population_value(self):
        errors = self._capture("log_error")
        self.dm_core._current_location()["population"] = "crowded"
        self.dm_core.validate_loaded_data()

        self.assertTrue([e for e in errors if "population should be" in e])

    # -- containment: a narrated person is real but never picked on the player's behalf ----------

    def test_a_bystander_is_never_the_default_item_target(self):
        self._populate([self._person()])

        self.assertFalse(self.dm_core._is_background(self.dm_core._get_target_name()))

    def test_buying_and_giving_still_work_in_a_crowd_only_scene(self):
        self._populate([self._person()])
        for name in list(self.dm_core.scenario_entities):
            if name != self.dm_core.player_name and not self.dm_core._is_background(name):
                self.dm_core.scenario_entities.remove(name)

        for intent in PERSON_TARGET_INTENTS:
            with self.subTest(intent=intent):
                self.assertTrue(self.dm_core._is_background(self.dm_core._get_target_name(include_background=True)))
        self.assertIsNone(self.dm_core._get_target_name(), "a thing-addressing intent still sees nobody")

    def test_a_bystander_is_chosen_as_a_combat_target_only_once_nothing_else_qualifies(self):
        self._populate([self._person()])
        self.assertFalse(self.dm_core._is_background(self.dm_core._choose_combat_target()))

        for name in list(self.dm_core.scenario_entities):
            if name != self.dm_core.player_name and not self.dm_core._is_background(name):
                self.dm_core.scenario_entities.remove(name)

        self.assertTrue(self.dm_core._is_background(self.dm_core._choose_combat_target()))

    def test_the_dialogue_fallback_may_address_a_bystander(self):
        self._populate([self._person()])
        for name in list(self.dm_core.scenario_entities):
            if name != self.dm_core.player_name and not self.dm_core._is_background(name):
                self.dm_core.scenario_entities.remove(name)

        self.assertTrue(self.dm_core._is_background(self.dm_core._resolve_dialogue_target("ask about the weather")))

    def test_a_bystander_never_takes_a_combat_turn(self):
        self._populate([self._person()])
        self.dm_core.entities["angry wolf"] = {
            "name": "angry wolf", "supertype": "creature", "max_hp": 10, "hp": 10,
            "skills": {"brawling": {"dice": 2, "pips": 0}},
        }
        self.dm_core._place_new_entity("angry wolf", self.dm_core.entities["angry wolf"], 1)
        self.dm_core.scenario_entities.append("angry wolf")
        result = {"round": 1}
        self.dm_core._resolve_combat_round(result)

        actors = [turn["actor"] for turn in result.get("turns", [])]
        for name in self._crowd():
            self.assertNotIn(name, actors)


class TestSceneRoster(DMTestCase):
    """!
    @brief scene_roster_updated (DM_Rules.py's _publish_scene_roster) -- the event that keeps
        LLMCore.scenario_characters describing the scene the player is actually in. Before it,
        that attribute was written once at scenario load and fed every later narration's own
        " Characters: " line forever, so walking into a tavern narrated the market's cast.
    """
    start_location = "debug_hub"

    def test_entering_a_location_republishes_the_roster(self):
        published = self._capture("scene_roster_updated")
        self.dm_core._enter_location("town_square")

        self.assertTrue(published)
        self.assertTrue(any("crier" in line.lower() for line in published[-1]["characters"]))

    def test_an_unchanged_roster_does_not_republish(self):
        self.dm_core._enter_location("town_square")
        published = self._capture("scene_roster_updated")
        self.dm_core._publish_scene_roster()
        self.dm_core._publish_scene_roster()

        self.assertEqual(published, [])

    def test_the_roster_payload_carries_matchable_entity_phrases(self):
        published = self._capture("scene_roster_updated")
        self.dm_core._enter_location("town_square")
        entities = published[-1]["entities"]

        self.assertNotIn(self.dm_core.player_name, [entry["key"] for entry in entities])
        self.assertTrue(all(entry["name"] for entry in entities))
        self.assertIn("town crier", [entry["key"] for entry in entities])

    def test_removing_an_entity_republishes_the_roster(self):
        self.dm_core._enter_location("town_square")
        published = self._capture("scene_roster_updated")
        self.dm_core.remove_entity_from_scene("town crier")

        self.assertTrue(published)
        self.assertFalse(any("crier" in line.lower() for line in published[-1]["characters"]))

    def test_a_turn_backstops_a_roster_nothing_else_published(self):
        self.dm_core._enter_location("town_square")
        self.dm_core.scenario_entities.remove("town crier")
        published = self._capture("scene_roster_updated")
        self.dm_core._on_turn_detected({"clauses": [], "input": "wait"})
        self.dm_core._on_dialogue_detected({"input": "hello"})

        self.assertTrue(published)
        self.assertFalse(any("crier" in line.lower() for line in published[-1]["characters"]))


class TestReferencedNpcGeneration(unittest.TestCase):
    """!
    @brief AdHoc_Generation.py's generate_referenced_npc -- the pure, DMCore-independent half
        of promotion. Same injected-client stub pattern TestAdHocGeneration already uses.
    """

    def _fake_call(self, captured, **overrides):
        arguments = {
            "name": "Ferrin", "description": "A weathered trader behind a crate of silver fish.",
            "keywords": ["merchant"], "disposition": "neutral", "power": "weak",
        }
        # An empty override drops the field entirely, which is what the real model does to
        # "description" most of the time (see test_a_dropped_description_falls_back_to_the_
        # players_own_words).
        arguments.update(overrides)
        arguments = {key: value for key, value in arguments.items() if value != ""}

        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            captured["tools"] = tools
            captured["prompt"] = messages[1]["content"]
            return {"choices": [{"message": {"tool_calls": [
                {"function": {"name": "create_creature", "arguments": json.dumps(arguments)}},
            ]}}]}
        return fake_call

    def _generate(self, fake_call, phrase="the merchant", present=(), narration=()):
        script_llm(self, fake_call)
        return generate_referenced_npc(
            phrase, "A busy market square.", list(present), list(narration), 4,
            REFERENCED_NPC_KEYWORDS, REFERENCED_NPC_SKILLS)

    def test_it_fills_in_an_ordinary_bystander(self):
        result = self._generate(self._fake_call({}))

        self.assertTrue(result["created"])
        self.assertEqual(result["entity"]["name"], "Ferrin")
        self.assertTrue(result["entity"]["ad_hoc"])

    def test_a_materialized_bystander_keeps_its_voice(self):
        captured = {}
        result = self._generate(self._fake_call(captured, voice="Soft-spoken, trails off mid-sentence"))

        self.assertIn("voice", captured["tools"][0]["function"]["parameters"]["properties"])
        self.assertEqual(result["entity"]["voice"], "Soft-spoken, trails off mid-sentence")

    def test_the_schema_offers_no_hostile_disposition(self):
        captured = {}
        self._generate(self._fake_call(captured))
        disposition = captured["tools"][0]["function"]["parameters"]["properties"]["disposition"]

        self.assertEqual(disposition["enum"], list(NON_HOSTILE_DISPOSITIONS))
        self.assertNotIn("hostile", disposition["enum"])

    def test_a_materialized_bystander_cannot_fight(self):
        # Falls out of the narrowed enum: only "hostile" gets abilities/behavior, so this path
        # structurally cannot produce something that takes a combat turn.
        result = self._generate(self._fake_call({}))

        self.assertNotIn("abilities", result["entity"])
        self.assertNotIn("behavior", result["entity"])

    def test_the_prompt_carries_the_present_roster_and_recent_narration(self):
        captured = {}
        self._generate(
            self._fake_call(captured), present=["Garridan Viskalai"],
            narration=["A merchant argues loudly outside the tavern."],
        )

        self.assertIn("Garridan Viskalai", captured["prompt"])
        self.assertIn("argues loudly outside", captured["prompt"])
        self.assertIn("the merchant", captured["prompt"])

    def test_an_unreachable_model_declines_rather_than_raising(self):
        def failing_call(*args, **kwargs):
            raise ConnectionError("no Ollama")

        result = self._generate(failing_call)

        self.assertFalse(result["created"])
        self.assertEqual(result["reason"], "unavailable")

    def test_an_explicit_decline_is_reported_as_one(self):
        def declining_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [
                {"function": {"name": "decline", "arguments": json.dumps({"reason": "already here"})}},
            ]}}]}

        result = self._generate(declining_call)

        self.assertFalse(result["created"])
        self.assertEqual(result["reason"], "already here")

    def test_an_empty_address_phrase_never_reaches_the_network(self):
        def exploding_call(*args, **kwargs):
            raise AssertionError("should never be called with no address phrase")

        result = self._generate(exploding_call, phrase="")

        self.assertFalse(result["created"])
        self.assertEqual(result["reason"], "no_phrase")


    def test_a_dropped_description_falls_back_to_the_players_own_words(self):
        # Measured against the shipped gemma4, the model returns a good name and keywords but
        # omits "description" on roughly seven of every eight calls, despite the schema
        # requiring it -- prompt wording, per-property documentation and a narrower tool arity
        # were each tried and none of them moved it. So the field is recovered rather than
        # required: "the blacksmith" becomes "A blacksmith.", which invents nothing the player
        # did not already say. See test_integration.py's own TestReferencedNpcLive.
        result = self._generate(self._fake_call({}, description=""), phrase="blacksmith")

        self.assertTrue(result["created"])
        self.assertEqual(result["entity"]["description"], "A blacksmith.")

    def test_the_fallback_description_reads_as_plain_english(self):
        # Article by leading vowel, and a stray leading article dropped rather than doubled --
        # this string goes straight into describe_character's own persona line.
        self.assertEqual(
            self._generate(self._fake_call({}, description=""), phrase="old man")["entity"]["description"],
            "An old man.",
        )
        self.assertEqual(
            self._generate(self._fake_call({}, description=""), phrase="the merchant")["entity"]["description"],
            "A merchant.",
        )

    def test_a_bystander_is_never_created_already_dead(self):
        # fit_skills_to_cr returns 0 HP for a low enough target CR, and an entity with 0 HP
        # fails _resolve_dialogue's own aliveness gate the instant it is placed -- so the
        # player would materialize someone and be told in the same breath that nobody is
        # there. Floored where the tool call becomes an entity, so every creation path gets it.
        script_llm(self, self._fake_call({}))
        result = generate_referenced_npc(
            "blacksmith", "A market square.", [], [], 1, REFERENCED_NPC_KEYWORDS,
            REFERENCED_NPC_SKILLS)

        self.assertTrue(result["created"])
        self.assertGreaterEqual(result["entity"]["max_hp"], 1)

    def test_a_dropped_name_is_still_incomplete(self):
        # The fallback covers exactly one field. A creation with no name at all has nothing
        # honest to recover from -- inventing one is the thing this feature is built not to do.
        result = self._generate(self._fake_call({}, name=""))

        self.assertFalse(result["created"])
        self.assertEqual(result["reason"], "incomplete")

    def test_adam_conjuring_still_declines_on_a_dropped_description(self):
        # The fallback is deliberately scoped to promotion, which has the player's own noun
        # phrase to fall back on. ADaM's own conjuring has only a free-text request, so its
        # behavior here is unchanged.
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [
                {"function": {"name": "create_creature", "arguments": json.dumps({
                    "name": "Rat", "keywords": ["brute"], "disposition": "hostile", "power": "weak",
                })}},
            ]}}]}

        script_llm(self, fake_call)
        result = generate_ad_hoc_creature(
            "a rat", "A cellar.", 4, REFERENCED_NPC_KEYWORDS, REFERENCED_NPC_SKILLS,
        )

        self.assertFalse(result["created"])
        self.assertEqual(result["reason"], "incomplete")


class TestWorldContext(DMTestCase):
    """!
    @brief resolution/World_Context.py -- the bundle every Combat_Resolution function takes
        first. DMCore holds one and exposes entities/rules/skills as read-only views of it.
    """

    def test_a_function_runs_over_a_bare_context_with_no_dmcore(self):
        ctx = WorldContext({"orc": {"max_hp": 7, "band": 3}})

        self.assertEqual(Combat_Resolution.get_current_hp(ctx, "orc"), 7)
        self.assertEqual(Combat_Resolution.get_band(ctx, "orc"), 3)
        self.assertEqual(Combat_Resolution.get_distance_between(ctx, "orc", "orc"), 0)

    def test_fields_not_given_default_to_empty_so_a_missing_one_fails_at_first_use(self):
        ctx = WorldContext()
        self.assertEqual((ctx.entities, ctx.rules, ctx.skills), ({}, {}, {}))
        self.assertIsNone(ctx.event_bus)

    def test_dmcore_exposes_the_contexts_own_dicts_not_copies(self):
        world = self.dm_core.world
        self.assertIs(self.dm_core.entities, world.entities)
        self.assertIs(self.dm_core.rules, world.rules)
        self.assertIs(self.dm_core.skills, world.skills)
        self.assertIs(world.event_bus, self.event_bus)

    def test_nothing_can_rebind_the_state_a_context_holds(self):
        for name in ("entities", "rules", "skills"):
            with self.subTest(name=name):
                with self.assertRaises(AttributeError):
                    setattr(self.dm_core, name, {})

    def test_reloading_rules_fills_the_same_dicts_in_place(self):
        before = (self.dm_core.entities, self.dm_core.rules, self.dm_core.skills)
        self.dm_core.load_rules(os.path.join("Rules", "Fantasy"))

        self.assertIs(self.dm_core.entities, before[0])
        self.assertIs(self.dm_core.rules, before[1])
        self.assertIs(self.dm_core.skills, before[2])

    def test_a_condition_applies_through_the_context(self):
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "wounded", duration="permanent", dismiss="")
        self.assertTrue(Combat_Resolution.has_condition(self.dm_core.world, "gladstone", "wounded"))


if __name__ == "__main__":
    unittest.main()
