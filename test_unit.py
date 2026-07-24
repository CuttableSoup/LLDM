import asyncio
import json
import os
import shutil
import tempfile
import threading
import tkinter as tk
import unittest
from unittest.mock import patch

import numpy as np
import pytest
from sentence_transformers import SentenceTransformer

from DM_Core import DMCore
from Event_Bus import EventBus
from GUI_Core import GUICore
from LLM_Core import LLMCore
from LLM_Rag import RagIndex
from NLP_Core import NLPCore
from Textual_Core import TextualCore
from textual.widgets import Button, Input, RichLog, TabbedContent


class DMTestCase(unittest.TestCase):
    """Shared setUp for tests that just need a fresh DMCore over a real scenario.
    Subclasses set scenario_name to pick which one, and override setUp (calling
    super().setUp() first) to also capture events or otherwise extend the fixture."""
    scenario_name = "arena"

    def setUp(self):
        self.event_bus = EventBus()
        self.dm_core = DMCore(self.event_bus, scenario_name=self.scenario_name)

    def _capture(self, event_name):
        events = []
        self.event_bus.subscribe(event_name, events.append)
        return events

    def _capture_any(self, *event_names):
        events = []
        for name in event_names:
            self.event_bus.subscribe(name, events.append)
        return events


class LLMTestCase(unittest.TestCase):
    """Shared setUp for tests that just need a fresh LLMCore with RAG disabled."""

    def setUp(self):
        self.event_bus = EventBus()
        # rag_source_dir points at a real directory with no PDFs in it, so RagIndex's
        # background build returns immediately (see LLMCore.__init__'s docstring) instead of
        # every test here kicking off a real, potentially minutes-long index build against
        # whatever's actually in Settings/Fantasy/.
        self.llm_core = LLMCore(self.event_bus, rag_source_dir=os.path.join("Rules", "Fantasy"))


class TestGameBoot(unittest.TestCase):
    def test_boot_and_skill_identification(self):
        # 1. Initialize Event Bus
        event_bus = EventBus()

        # 2. Track action_detected events
        detected_actions = []
        def on_action_detected(data):
            detected_actions.append(data)
        event_bus.subscribe("action_detected", on_action_detected)

        # 3. Initialize NLPCore FIRST so it doesn't miss rules_loaded
        nlp_core = NLPCore(event_bus)

        # 4. Initialize DMCore (this triggers rules_loaded)
        dm_core = DMCore(event_bus)

        # Verify that skills were actually loaded into nlp_core
        self.assertGreater(len(nlp_core.skill_names), 0, "No skills loaded into NLPCore")

        # 5. Simulate user input
        test_input = "I attack with my sword"
        event_bus.publish("user_input_submitted", test_input)

        # 6. Verify skill identification
        self.assertGreater(len(detected_actions), 0, "No action detected event published")
        last_action = detected_actions[-1]
        self.assertEqual(last_action["skill"], "blades")
        self.assertGreater(last_action["score"], 0.5)
        print(f"Integration Test Success: '{test_input}' -> {last_action['skill']} ({last_action['score']:.4f})")


class TestNlpConfidenceThreshold(unittest.TestCase):
    # setUpClass (not setUp) so the slow sentence-transformers load only happens once for
    # every test method in this class, not once per method.
    @classmethod
    def setUpClass(cls):
        cls.event_bus = EventBus()
        cls.nlp_core = NLPCore(cls.event_bus)
        cls.dm_core = DMCore(cls.event_bus)

    def setUp(self):
        # cls.dm_core is shared across every test in this class (setUpClass, not setUp) to
        # avoid paying the slow model load repeatedly -- but several methods trigger *real*
        # combat against the scenario's wolf/wolf_2 (ex: test_clear_action_still_triggers_
        # above_threshold's "I attack with my sword"), so without a reset, HP damage
        # accumulated silently across nominally-independent tests in alphabetical execution
        # order. That's what made test_full_pipeline_naming_a_non_hostile_entity_does_not_
        # redirect_current_target occasionally flaky: two earlier tests' real combat rounds
        # could leave "wolf" already dead by the time it ran, flipping current_target to
        # "wolf_2" out from under an assertion that never expected combat history to matter.
        # Re-running the same load_rules/load_scenario_definition/load_scenario sequence
        # __init__ and load_game both use resets every mutable field (hp, active_conditions,
        # currency, inventory, round_number, current_target) back to a pristine "arena" load
        # before each test method, without re-paying for a new NLPCore/model load.
        self.dm_core.load_rules(os.path.join("Rules", "Fantasy"))
        self.dm_core.load_scenario_definition(self.dm_core.scenario_key)
        self.dm_core.load_scenario()

    def test_low_confidence_input_triggers_no_skill(self):
        # A greeting with no real skill/action content shouldn't be forced onto whatever
        # phrase happens to score highest (previously this mapped to "artistry" at ~0.32).
        detected_actions = []
        not_understood = []
        self.event_bus.subscribe("action_detected", detected_actions.append)
        self.event_bus.subscribe("action_not_understood", not_understood.append)

        self.event_bus.publish("user_input_submitted", "Hey there innkeeper")

        self.assertEqual(detected_actions, [])
        # Publishing this instead of just staying silent is what lets LLMCore give the
        # player some response rather than the app appearing to stall.
        self.assertEqual(len(not_understood), 1)
        self.assertIn("innkeeper", not_understood[0]["input"])

    def test_clear_action_still_triggers_above_threshold(self):
        detected_actions = []
        self.event_bus.subscribe("action_detected", detected_actions.append)

        self.event_bus.publish("user_input_submitted", "I attack with my sword")

        self.assertEqual(len(detected_actions), 1)
        self.assertEqual(detected_actions[0]["skill"], "blades")
        self.assertGreaterEqual(detected_actions[0]["score"], self.nlp_core.confidence_threshold)

    def test_keyword_fallback_rescues_a_below_threshold_literal_keyword_hit(self):
        # "bargain" isn't a keyword for anything, but "cost" is a literal keyword of
        # "appraise" (skills.toml) and the full sentence never clears confidence_threshold on
        # its own (~0.30 in practice) -- _match_by_keyword is what rescues this, gated on
        # appraise's own best embedding score (still ~0.30) clearing the much lower
        # keyword_fallback_floor rather than being accepted on keyword evidence alone.
        detected_actions = []
        self.event_bus.subscribe("action_detected", detected_actions.append)

        self.event_bus.publish("user_input_submitted", "I'll bargain with her over the cost of supper")

        self.assertEqual(len(detected_actions), 1)
        self.assertEqual(detected_actions[0]["skill"], "appraise")
        self.assertLess(detected_actions[0]["score"], self.nlp_core.confidence_threshold)
        self.assertGreaterEqual(detected_actions[0]["score"], self.nlp_core.keyword_fallback_floor)

    def test_alternate_phrasing_candidate_rescues_a_diluted_sentence(self):
        # The full sentence pools toward "harvest festival plans" and never clears
        # confidence_threshold, but _generate_match_candidates also tries the text truncated
        # at " regarding " ("talk"), which matches charisma's own bare "talk" keyword phrase
        # almost exactly -- this is the dilution gotcha (see NLP_Core.py's module notes)
        # actually getting fixed by a less-diluted candidate, not by the keyword fallback.
        detected_actions = []
        self.event_bus.subscribe("action_detected", detected_actions.append)

        self.event_bus.publish(
            "user_input_submitted",
            "I want to talk about something regarding the harvest festival plans",
        )

        self.assertEqual(len(detected_actions), 1)
        self.assertEqual(detected_actions[0]["skill"], "charisma")
        self.assertGreaterEqual(detected_actions[0]["score"], self.nlp_core.confidence_threshold)

    def test_detect_item_intent_examine_vs_take_vs_neither(self):
        self.assertEqual(self.nlp_core._detect_item_intent("examine the dagger"), "examine")
        self.assertEqual(self.nlp_core._detect_item_intent("take the gold"), "take")
        self.assertIsNone(self.nlp_core._detect_item_intent("attack with my sword"))

    def test_picking_a_lock_is_not_mistaken_for_taking_an_item(self):
        # "pick" alone would collide with "I pick the lock" (finesse) if it were a bare-word
        # match -- only the two-word "pick up" phrase should count as take-intent.
        self.assertIsNone(self.nlp_core._detect_item_intent("pick the lock"))
        self.assertEqual(self.nlp_core._detect_item_intent("pick up the dagger"), "take")

        detected_actions = []
        item_events = []
        self.event_bus.subscribe("action_detected", detected_actions.append)
        self.event_bus.subscribe("item_interaction_detected", item_events.append)

        self.event_bus.publish("user_input_submitted", "I pick the lock")

        self.assertEqual(item_events, [])
        self.assertEqual(len(detected_actions), 1)
        self.assertEqual(detected_actions[0]["skill"], "finesse")

    def test_full_pipeline_detects_a_known_item_by_name(self):
        # Item matching runs against every known "object"-supertype entity (globally, not
        # scoped to the active scenario) -- DMCore is what checks whether it's actually
        # present in the current target's inventory (see TestItemInteraction).
        item_events = []
        self.event_bus.subscribe("item_interaction_detected", item_events.append)

        self.event_bus.publish("user_input_submitted", "I examine the cursed dagger")

        self.assertEqual(len(item_events), 1)
        self.assertEqual(item_events[0]["intent"], "examine")
        self.assertEqual(item_events[0]["item_name"], "cursed dagger")

    def test_detect_save_load_intent_parses_slot_names(self):
        self.assertEqual(self.nlp_core._detect_save_load_intent("save as arena run 1"), ("save", "arena run 1"))
        self.assertEqual(
            self.nlp_core._detect_save_load_intent("save game as arena-run-1"), ("save", "arena-run-1")
        )
        self.assertEqual(self.nlp_core._detect_save_load_intent("save boss-fight"), ("save", "boss-fight"))
        self.assertEqual(self.nlp_core._detect_save_load_intent("load boss-fight"), ("load", "boss-fight"))
        self.assertEqual(
            self.nlp_core._detect_save_load_intent("load game as boss-fight"), ("load", "boss-fight")
        )

    def test_detect_save_load_intent_ignores_unrelated_input(self):
        self.assertEqual(self.nlp_core._detect_save_load_intent("i attack the wolf"), (None, None))
        # A recognized word alone with nothing following it is not a usable slot name.
        self.assertEqual(self.nlp_core._detect_save_load_intent("save"), (None, None))

    def test_full_pipeline_save_command_bypasses_skill_and_item_matching(self):
        save_events = []
        detected_actions = []
        item_events = []
        self.event_bus.subscribe("save_requested", save_events.append)
        self.event_bus.subscribe("action_detected", detected_actions.append)
        self.event_bus.subscribe("item_interaction_detected", item_events.append)

        try:
            self.event_bus.publish("user_input_submitted", "save as test-nlp-save-slot")

            self.assertEqual(save_events, [{"slot": "test-nlp-save-slot"}])
            self.assertEqual(detected_actions, [])
            self.assertEqual(item_events, [])
        finally:
            # This shared DMCore instance is also on the bus, so the publish above really did
            # write Saves/test-nlp-save-slot/ -- clean it up rather than leaving it behind.
            shutil.rmtree(self.dm_core._save_slot_dir("test-nlp-save-slot"), ignore_errors=True)

    def test_detect_item_intent_recognizes_give_trade_open_close(self):
        self.assertEqual(self.nlp_core._detect_item_intent("give the innkeeper a health potion"), "give")
        self.assertEqual(self.nlp_core._detect_item_intent("hand over the gold"), "give")
        self.assertEqual(self.nlp_core._detect_item_intent("trade the cursed dagger"), "trade")
        self.assertEqual(self.nlp_core._detect_item_intent("buy a health potion"), "trade")
        self.assertEqual(self.nlp_core._detect_item_intent("open the chest"), "open")
        self.assertEqual(self.nlp_core._detect_item_intent("close the chest"), "close")
        self.assertEqual(self.nlp_core._detect_item_intent("shut it"), "close")

    def test_trade_keywords_do_not_overlap_with_appraise(self):
        # skills.toml's "appraise" keywords: evaluation, commerce, investigation, value,
        # price, worth, cost, identify, examine -- none of those should ever get swallowed
        # by the trade intercept before appraise's own skill-matching gets a chance to run.
        appraise_keywords = ["evaluation", "commerce", "investigation", "value", "price",
                              "worth", "cost", "identify"]
        for keyword in appraise_keywords:
            self.assertIsNone(
                self.nlp_core._detect_item_intent(f"what's the {keyword} of this"),
                f"'{keyword}' should not trigger the trade intercept",
            )

    def test_close_combat_does_not_misfire_the_close_intent(self):
        # blades' own description is "Using swords and knives in close combat." -- a bare
        # "close " keyword would have swallowed this before skill matching ever ran.
        self.assertIsNone(self.nlp_core._detect_item_intent("I fight in close combat"))

    def test_detect_item_intent_recognizes_advance_and_retreat(self):
        self.assertEqual(self.nlp_core._detect_item_intent("I advance toward the wolf"), "advance")
        self.assertEqual(self.nlp_core._detect_item_intent("move closer"), "advance")
        self.assertEqual(self.nlp_core._detect_item_intent("I approach the wolf"), "advance")
        self.assertEqual(self.nlp_core._detect_item_intent("I retreat"), "retreat")
        self.assertEqual(self.nlp_core._detect_item_intent("back away slowly"), "retreat")
        self.assertEqual(self.nlp_core._detect_item_intent("fall back"), "retreat")

    def test_close_the_distance_still_resolves_to_close_not_advance(self):
        # A known, accepted ambiguity -- CLOSE_KEYWORDS' "close the " wins over this natural
        # phrasing (see ADVANCE_KEYWORDS' own module note). Documented here so a future
        # reader doesn't mistake it for an oversight.
        self.assertEqual(self.nlp_core._detect_item_intent("close the distance"), "close")

    def test_advance_retreat_keywords_do_not_misfire_on_unrelated_input(self):
        # None of ADVANCE_KEYWORDS/RETREAT_KEYWORDS should collide with ordinary skill phrasing.
        self.assertIsNone(self.nlp_core._detect_item_intent("I ask the innkeeper about the road"))
        self.assertIsNone(self.nlp_core._detect_item_intent("I attack the wolf with my sword"))

    def test_full_pipeline_open_bypasses_item_name_matching(self):
        # "open"/"close" act on the scene target directly -- map_to_item should never even
        # run for them, so item_name is always None regardless of what's actually present.
        item_events = []
        detected_actions = []
        self.event_bus.subscribe("item_interaction_detected", item_events.append)
        self.event_bus.subscribe("action_detected", detected_actions.append)

        self.event_bus.publish("user_input_submitted", "open the chest")

        self.assertEqual(len(item_events), 1)
        self.assertEqual(item_events[0]["intent"], "open")
        self.assertIsNone(item_events[0]["item_name"])
        self.assertEqual(detected_actions, [])

    def test_full_pipeline_advance_bypasses_item_name_matching(self):
        item_events = []
        self.event_bus.subscribe("item_interaction_detected", item_events.append)

        self.event_bus.publish("user_input_submitted", "I advance toward the wolf")

        self.assertEqual(len(item_events), 1)
        self.assertEqual(item_events[0]["intent"], "advance")
        self.assertIsNone(item_events[0]["item_name"])

    def test_map_to_target_matches_a_named_creature(self):
        # Global catalog match, same as map_to_item -- arena.toml's wolf_2/thane are both
        # loaded (this class's shared dm_core boots the "arena" scenario).
        target_name, score = self.nlp_core.map_to_target("attack wolf_2")
        self.assertEqual(target_name, "wolf_2")
        self.assertGreaterEqual(score, self.nlp_core.confidence_threshold)

    def test_map_to_target_excludes_the_player(self):
        # gladstone is is_player = true -- never a valid attack-target match, even though it's
        # a "creature" supertype entity like everything else in the target catalog.
        self.assertNotIn("gladstone", self.nlp_core.target_indices)

    def test_full_pipeline_attack_names_a_target_that_redirects_current_target(self):
        self.dm_core.current_target = "wolf"
        detected_actions = []
        self.event_bus.subscribe("action_detected", detected_actions.append)

        self.event_bus.publish("user_input_submitted", "I attack wolf_2")

        self.assertEqual(detected_actions[-1]["target"], "wolf_2")
        self.assertEqual(self.dm_core.current_target, "wolf_2")

    def test_full_pipeline_naming_a_non_hostile_entity_does_not_redirect_current_target(self):
        # thane is a confident semantic match (it's a known creature entity), but it isn't
        # hostile -- DMCore must reject the override rather than making an ally the target.
        self.dm_core.current_target = "wolf"
        detected_actions = []
        self.event_bus.subscribe("action_detected", detected_actions.append)

        self.event_bus.publish("user_input_submitted", "I attack thane")

        self.assertEqual(detected_actions[-1]["target"], "thane")
        self.assertEqual(self.dm_core.current_target, "wolf")


class TestClarificationResponse(LLMTestCase):
    def test_unmatched_input_queues_a_clarification_prompt_not_a_dice_roll(self):
        # _queue_narration appends to context_window synchronously before spawning the
        # background network fetch, so this is checkable without waiting on (or mocking) LM
        # Studio -- the point here is the prompt shape, not the LLM's actual reply.
        self.event_bus.publish("action_not_understood", {"input": "hey there innkeeper", "score": 0.32})

        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("hey there innkeeper", prompt)
        # No roll data (that's _describe_outcome's shape, used by the other narration paths).
        self.assertNotIn("Skill used:", prompt)
        self.assertNotIn("difficulty", prompt)

    def test_describe_outcome_includes_loot_so_the_llm_isnt_left_guessing(self):
        # Without this, the LLM has no idea what was actually gained and will happily invent
        # contents that don't match the real game state (observed: it narrated a "silver key
        # and leather-bound journal" for a chest that actually just held currency).
        result = {
            "input": "I pick the lock", "skill": "finesse", "roll": 18, "difficulty": 12,
            "success": True, "defender": "chest", "loot": {"currency": 20, "items": []},
        }
        description = self.llm_core._describe_outcome(result)
        self.assertIn("20 currency", description)

    def test_describe_outcome_omits_loot_text_when_nothing_gained(self):
        result = {
            "input": "I pick the lock", "skill": "finesse", "roll": 3, "difficulty": 12,
            "success": False, "defender": "chest",
        }
        description = self.llm_core._describe_outcome(result)
        self.assertNotIn("The player gains", description)

    def test_describe_outcome_uses_the_given_actor_and_skips_the_attempt_line_without_input(self):
        # A creature's own behavior-driven action (ex: a wolf's bite) has no free-text
        # "input" the way a player action does -- the leading "X attempts: ..." line
        # should be omitted entirely rather than printing an empty quoted string, and
        # the actor name should reflect who actually acted, not default to "the player".
        enemy_result = {
            "skill": "brawling", "roll": 9, "difficulty": 7, "success": True,
            "defender": "gladstone", "opposing_skill": "blades",
        }
        description = self.llm_core._describe_outcome(enemy_result, actor="wolf")
        self.assertNotIn("attempts", description)
        self.assertIn("Skill used: brawling", description)
        self.assertIn("opposed by gladstone's blades", description)

    def test_round_response_narrates_the_targets_counterattack(self):
        round_result = {
            "round": 1, "skill": "athletics", "roll": 10, "difficulty": 0, "success": True,
            "defender": "wolf", "input": "I vault past the wolf",
            "turns": [{
                "skill": "brawling", "roll": 9, "difficulty": 7, "success": True,
                "defender": "gladstone", "opposing_skill": "blades", "actor": "wolf",
                "damage": {"attacker": "wolf", "defender": "gladstone", "raw_damage": 5,
                           "reduction": 0, "net_damage": 5, "remaining_hp": 31},
            }],
        }
        self.event_bus.publish("round_resolved", round_result)
        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("wolf", prompt)
        self.assertIn("gladstone takes 5 damage", prompt)

    def test_examine_prompt_never_implies_a_transfer(self):
        self.event_bus.publish("item_interaction_resolved", {
            "intent": "examine", "item_name": "cursed dagger", "input": "I examine the cursed dagger",
            "found": True, "description": "A wickedly curved dagger etched with glowing runes.",
        })
        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("glowing runes", prompt)
        self.assertIn("nothing is taken", prompt)

    def test_take_prompt_reflects_currency_amount_not_the_literal_word_currency(self):
        self.event_bus.publish("item_interaction_resolved", {
            "intent": "take", "item_name": "currency", "input": "I take the gold",
            "found": True, "container": "chest", "amount": 20,
        })
        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("20 currency", prompt)

    def test_locked_container_prompt_explains_the_denial(self):
        self.event_bus.publish("item_interaction_resolved", {
            "intent": "take", "item_name": "cursed dagger", "input": "I take the dagger",
            "found": False, "reason": "locked", "container": "chest",
        })
        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("locked", prompt)
        self.assertIn("no roll involved", prompt)


class TestOpposedResolution(DMTestCase):
    def test_highest_value_opposing_skill_is_used(self):
        # blades opposes = ['dodge', 'blades', 'brawling', 'axes', 'polearms']
        # 'dodge' is listed first, but 'brawling' rates higher (5*3=15 vs 2*3=6),
        # so 'brawling' must be the one chosen and rolled.
        self.dm_core.entities["test_defender"] = {
            "name": "test_defender",
            "skills": {
                "dodge": {"dice": 2, "pips": 0},
                "brawling": {"dice": 5, "pips": 0},
            },
        }

        chosen = self.dm_core.get_opposing_skill("blades", "test_defender")
        self.assertEqual(chosen, "brawling")

        result = self.dm_core.resolve_opposed_action("gladstone", "blades", "test_defender")
        self.assertEqual(result["opposing_skill"], "brawling")
        self.assertEqual(result["defender"], "test_defender")
        # 5 dice + 0 pips can only roll between 5 and 30
        self.assertGreaterEqual(result["difficulty"], 5)
        self.assertLessEqual(result["difficulty"], 30)

    def test_pips_count_toward_the_rating(self):
        # 'dodge' has fewer dice (2) than 'brawling' (3), but +3 pips bumps its
        # rating past brawling's: dodge = 2*3+3=9, brawling = 3*3+0=9... so add one
        # more pip to make dodge strictly higher and confirm pips are honored.
        self.dm_core.entities["test_defender"] = {
            "name": "test_defender",
            "skills": {
                "dodge": {"dice": 2, "pips": 4},
                "brawling": {"dice": 3, "pips": 0},
            },
        }

        chosen = self.dm_core.get_opposing_skill("blades", "test_defender")
        self.assertEqual(chosen, "dodge")

    def test_wolf_scenario_defender_picks_dodge_over_brawling(self):
        # Regression check against the real creatures.toml data:
        # wolf dodge = 6*3=18, wolf brawling = 5*3=15, so dodge should win.
        chosen = self.dm_core.get_opposing_skill("blades", "wolf")
        self.assertEqual(chosen, "dodge")

    def test_no_matching_opposing_skill_defaults_to_zero(self):
        self.dm_core.entities["empty_defender"] = {"name": "empty_defender", "skills": {}}

        chosen = self.dm_core.get_opposing_skill("blades", "empty_defender")
        self.assertIsNone(chosen)

        result = self.dm_core.resolve_opposed_action("gladstone", "blades", "empty_defender")
        self.assertIsNone(result["opposing_skill"])
        self.assertEqual(result["difficulty"], 0)

    def test_no_difficulty_passed_defaults_to_zero(self):
        result = self.dm_core.resolve_action("gladstone", "blades")
        self.assertEqual(result["difficulty"], 0)


class TestDamageCalculation(DMTestCase):
    def test_bonus_resolves_flat_number(self):
        self.assertEqual(self.dm_core.resolve_bonus("gladstone", 5), 5)

    def test_bonus_resolves_from_rule_reference(self):
        # strength_damage rule: skill="strength", divisor=2. Gladstone's strength is 2 dice, so bonus = 2 // 2 = 1.
        self.assertEqual(self.dm_core.resolve_bonus("gladstone", "user.strength_damage"), 1)
        # Without the "user." prefix should resolve the same way.
        self.assertEqual(self.dm_core.resolve_bonus("gladstone", "strength_damage"), 1)

    def test_bonus_unknown_reference_defaults_to_zero(self):
        self.assertEqual(self.dm_core.resolve_bonus("gladstone", "user.made_up_rule"), 0)

    @patch("random.randint", return_value=4)
    def test_damage_value_rolls_dice_and_adds_bonus(self, mock_randint):
        # 2 dice @ 4 each + 1 pip + strength_damage bonus (1) = 10
        total = self.dm_core.resolve_damage_value(
            "gladstone", {"dice": 2, "pips": 1, "bonus": "user.strength_damage"}
        )
        self.assertEqual(total, 10)

    @patch("random.randint", return_value=4)
    def test_damage_value_resolves_weapon_reference_from_equipped_weapon(self, mock_randint):
        # techniques.toml's cleave uses "user.weapon.dice"/"user.weapon.pips"; gladstone's
        # equipped longsword (1 die, 2 pips) supplies both: 1 die @ 4 + 2 pips = 6.
        total = self.dm_core.resolve_damage_value(
            "gladstone", {"dice": "user.weapon.dice", "pips": "user.weapon.pips", "bonus": 0}
        )
        self.assertEqual(total, 6)

    def test_damage_value_weapon_reference_degrades_to_zero_without_equipped_weapon(self):
        # wolf has no equipped weapon at all, so the same indirection safely falls back to 0.
        total = self.dm_core.resolve_damage_value(
            "wolf", {"dice": "user.weapon.dice", "pips": "user.weapon.pips", "bonus": 0}
        )
        self.assertEqual(total, 0)

    @patch("random.randint", return_value=3)
    def test_damage_reduction_only_applies_to_matching_tags(self, mock_randint):
        # Gladstone wears chain mail: armor_value = 2 dice, tags = physical/piercing/bludgeoning/slashing.
        self.assertEqual(self.dm_core.get_damage_reduction("gladstone", ["bludgeoning"]), 6)
        self.assertEqual(self.dm_core.get_damage_reduction("gladstone", ["fire"]), 0)

    def test_get_current_hp_initializes_from_max_hp(self):
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), 36)

    def test_apply_damage_subtracts_and_floors_at_zero(self):
        self.dm_core.apply_damage("gladstone", 10)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), 26)
        self.dm_core.apply_damage("gladstone", 1000)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), 0)

    @patch("random.randint", return_value=2)
    def test_calculate_damage_applies_unresisted_damage_to_hp(self, mock_randint):
        # Fireball: 5 dice, no bonus, fire damage - gladstone's chain mail doesn't resist fire.
        fireball = {"damage_value": {"dice": 5, "pips": 0, "bonus": 0}, "damage_tags": ["fire"]}
        result = self.dm_core.calculate_damage("wolf", "gladstone", fireball)

        self.assertEqual(result["raw_damage"], 10)  # 5 dice @ 2 each
        self.assertEqual(result["reduction"], 0)
        self.assertEqual(result["net_damage"], 10)
        self.assertEqual(result["remaining_hp"], 26)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), 26)

    @patch("random.randint", return_value=3)
    def test_calculate_damage_reduced_by_matching_armor(self, mock_randint):
        # Punch: 0 dice + strength_damage bonus (1), bludgeoning - chain mail resists bludgeoning (2 dice @ 3 each = 6).
        punch = {"damage_value": {"dice": 0, "pips": 0, "bonus": "user.strength_damage"}, "damage_tags": ["bludgeoning"]}
        result = self.dm_core.calculate_damage("wolf", "gladstone", punch)

        self.assertEqual(result["raw_damage"], 1)
        self.assertEqual(result["reduction"], 6)
        self.assertEqual(result["net_damage"], 0)
        self.assertEqual(result["remaining_hp"], 36)

    def test_fire_elemental_is_immune_to_fire_tag(self):
        self.assertTrue(self.dm_core.is_immune_to("fire elemental", ["fire"]))
        self.assertFalse(self.dm_core.is_immune_to("fire elemental", ["slashing"]))
        # Immunity is a hard tag match, not a rolled amount -- an entity with no
        # immunity_tags at all (gladstone) is never immune to anything.
        self.assertFalse(self.dm_core.is_immune_to("gladstone", ["fire"]))

    @patch("random.randint", return_value=3)
    def test_innate_resistance_reduces_matching_tags_like_armor(self, mock_randint):
        # Fire elemental's resistance_value (2 dice) applies to physical tags, not fire.
        self.assertEqual(self.dm_core.get_damage_reduction("fire elemental", ["slashing"]), 6)
        self.assertEqual(self.dm_core.get_damage_reduction("fire elemental", ["fire"]), 0)

    @patch("random.randint", return_value=4)
    def test_calculate_damage_fireball_negated_by_matching_immunity(self, mock_randint):
        # Gladstone's fireball (5 dice fire) rolls 20 raw damage, but the fire elemental's
        # immunity_tags fully negate it -- net damage is 0 regardless of the roll.
        fireball = self.dm_core.find_attack_ability("gladstone", "arcane")
        result = self.dm_core.calculate_damage("gladstone", "fire elemental", fireball)

        self.assertEqual(result["raw_damage"], 20)
        self.assertEqual(result["reduction"], 20)
        self.assertEqual(result["net_damage"], 0)
        self.assertEqual(result["remaining_hp"], 30)

    @patch("random.randint", return_value=4)
    def test_calculate_damage_fireball_unresisted_against_non_immune_target(self, mock_randint):
        # The same fireball against a target with no fire immunity/resistance (wolf) hits normally.
        fireball = self.dm_core.find_attack_ability("gladstone", "arcane")
        result = self.dm_core.calculate_damage("gladstone", "wolf", fireball)

        self.assertEqual(result["raw_damage"], 20)
        self.assertEqual(result["reduction"], 0)
        self.assertEqual(result["net_damage"], 20)

    @patch("random.randint", return_value=3)
    def test_vulnerability_bonus_only_applies_to_matching_tags(self, mock_randint):
        # Fire elemental's vulnerability_value (2 dice) applies to water, not fire.
        self.assertEqual(self.dm_core.get_vulnerability_bonus("fire elemental", ["water"]), 6)
        self.assertEqual(self.dm_core.get_vulnerability_bonus("fire elemental", ["fire"]), 0)
        # No vulnerability_value/tags at all (gladstone) is never vulnerable to anything.
        self.assertEqual(self.dm_core.get_vulnerability_bonus("gladstone", ["water"]), 0)

    @patch("random.randint", return_value=4)
    def test_calculate_damage_splash_flow_exploits_water_vulnerability(self, mock_randint):
        # splash flow: 4 dice water, no bonus -> 16 raw. Fire elemental has no resistance to
        # water (only physical), so reduction is 0, and its vulnerability_value (2 dice) adds
        # 8 more on top -- net damage should exceed the raw roll, not just match it.
        splash_flow = self.dm_core.resolve_named_ability("gladstone", "splash flow")
        result = self.dm_core.calculate_damage("gladstone", "fire elemental", splash_flow)

        self.assertEqual(result["raw_damage"], 16)
        self.assertEqual(result["reduction"], 0)
        self.assertEqual(result["vulnerability_bonus"], 8)
        self.assertEqual(result["net_damage"], 24)
        self.assertEqual(result["remaining_hp"], 6)

    @patch("random.randint", return_value=4)
    def test_immunity_overrides_vulnerability_when_both_tags_present(self, mock_randint):
        # An attack tagged both "fire" (immune) and "water" (vulnerable) should still be fully
        # negated -- immunity is an absolute block that wins outright, not just a bigger number
        # in the same tug-of-war as resistance/vulnerability.
        hybrid_attack = {"damage_value": {"dice": 4, "pips": 0, "bonus": 0}, "damage_tags": ["fire", "water"]}
        result = self.dm_core.calculate_damage("gladstone", "fire elemental", hybrid_attack)

        self.assertEqual(result["vulnerability_bonus"], 0)
        self.assertEqual(result["net_damage"], 0)

    def test_cleave_is_reachable_via_either_listed_skill(self):
        # cleave's skill field is a list (["blades", "axes"]) -- gladstone's equipped longsword
        # already matches "blades" and wins there (see find_attack_ability's docstring), but
        # nothing equipped matches "axes", so cleave surfaces via ability_matches_skill's
        # list-membership check.
        cleave = self.dm_core.find_attack_ability("gladstone", "axes")
        assert cleave is not None
        self.assertEqual(cleave["name"], "cleave")

    @patch("random.randint", return_value=4)
    def test_calculate_damage_cleave_uses_equipped_weapons_dice_and_pips(self, mock_randint):
        # cleave's damage_value is "user.weapon.dice"/"user.weapon.pips", resolved from
        # gladstone's equipped longsword (1 die, 2 pips) plus strength_damage bonus (1):
        # 1 die @ 4 + 2 pips + 1 bonus = 7 raw damage, slashing.
        cleave = self.dm_core.find_attack_ability("gladstone", "axes")
        result = self.dm_core.calculate_damage("gladstone", "wolf", cleave)

        self.assertEqual(result["raw_damage"], 7)
        self.assertEqual(result["reduction"], 0)
        self.assertEqual(result["net_damage"], 7)


class TestCombatLoop(DMTestCase):
    def setUp(self):
        super().setUp()
        # These tests always face a scenario target, so combat narration ("round_resolved")
        # is what fires, not the no-combat "action_resolved" path.
        self.resolved = self._capture("round_resolved")

    def test_find_attack_ability_prefers_equipped_weapon(self):
        # Gladstone has a longsword equipped in rhand, which uses the "blades" skill.
        ability = self.dm_core.find_attack_ability("gladstone", "blades")
        assert ability is not None
        self.assertEqual(ability["name"], "longsword")

    def test_find_attack_ability_falls_back_to_innate_ability(self):
        # No equipped weapon uses "brawling", so the innate "punch" ability should be found instead.
        ability = self.dm_core.find_attack_ability("gladstone", "brawling")
        assert ability is not None
        self.assertEqual(ability["name"], "punch")

    def test_find_attack_ability_resolves_name_referenced_spell(self):
        # Gladstone's abilities table names "fireball" rather than inlining it; find_attack_ability
        # must resolve that reference to the shared spells.toml entity to find "arcane"/damage_value.
        ability = self.dm_core.find_attack_ability("gladstone", "arcane")
        assert ability is not None
        self.assertEqual(ability["name"], "fireball")
        self.assertIs(ability, self.dm_core.entities["fireball"])

    def test_splash_flow_is_reachable_by_name_alongside_fireball(self):
        # Both "fireball" and "splash flow" share the "arcane" skill, so find_attack_ability
        # (skill-first lookup) always returns fireball, the earlier-listed entry -- splash flow
        # is reached the same way a player naming "cleave" directly is, via resolve_named_ability.
        splash_flow = self.dm_core.resolve_named_ability("gladstone", "splash flow")
        assert splash_flow is not None
        self.assertIs(splash_flow, self.dm_core.entities["splash flow"])
        self.assertEqual(splash_flow["damage_tags"], ["water"])

    def test_resolve_ability_passes_through_inline_tables_and_looks_up_string_references(self):
        inline = {"name": "punch", "skill": "brawling"}
        self.assertIs(self.dm_core.resolve_ability(inline), inline)
        self.assertIs(self.dm_core.resolve_ability("fireball"), self.dm_core.entities["fireball"])
        self.assertIsNone(self.dm_core.resolve_ability("not_a_real_ability"))

    def test_find_attack_ability_returns_none_for_unmatched_skill(self):
        self.assertIsNone(self.dm_core.find_attack_ability("gladstone", "athletics"))

    def test_resolve_named_ability_finds_owned_ability_by_name(self):
        cleave = self.dm_core.resolve_named_ability("gladstone", "cleave")
        self.assertIsNotNone(cleave)
        self.assertIs(cleave, self.dm_core.entities["cleave"])

    def test_resolve_named_ability_returns_none_for_a_plain_skill_name(self):
        # "blades" is a skill gladstone has, not an ability of his -- must not match.
        self.assertIsNone(self.dm_core.resolve_named_ability("gladstone", "blades"))

    def test_resolve_named_ability_returns_none_for_an_ability_the_entity_lacks(self):
        # wolf has no "cleave" ability, only "bite".
        self.assertIsNone(self.dm_core.resolve_named_ability("wolf", "cleave"))

    def test_select_ability_skill_picks_best_rated_option_from_a_skill_list(self):
        # cleave's skill is ["blades", "axes"]; gladstone has "blades" (5 dice) and no "axes"
        # entry at all, so "blades" must be the one selected.
        cleave = self.dm_core.entities["cleave"]
        self.assertEqual(self.dm_core.select_ability_skill("gladstone", cleave), "blades")

    def test_select_ability_skill_passes_through_a_single_skill_string(self):
        fireball = self.dm_core.entities["fireball"]
        self.assertEqual(self.dm_core.select_ability_skill("gladstone", fireball), "arcane")

    def test_action_detected_with_named_ability_bypasses_find_attack_ability_priority(self):
        # find_attack_ability("gladstone", "blades") would normally return the equipped
        # longsword, never "cleave" (see test_find_attack_ability_prefers_equipped_weapon).
        # When NLPCore matches input directly to "cleave" by name, _on_action_detected must
        # use that exact ability instead -- proven here by asserting find_attack_ability is
        # never even called.
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self.dm_core.scenario = {"entities": [{"name": "practice_dummy", "band": 1}]}
        self.dm_core.load_scenario()

        with patch.object(self.dm_core, "find_attack_ability", wraps=self.dm_core.find_attack_ability) as spy:
            with patch("random.randint", return_value=3):
                self.dm_core._on_action_detected({"skill": "cleave", "input": "I cleave through them"})

        spy.assert_not_called()
        result = self.resolved[-1]
        self.assertTrue(result["success"])
        self.assertEqual(result["skill"], "blades")  # cleave resolved to gladstone's best listed skill
        self.assertIn("damage", result)
        self.assertGreater(result["damage"]["net_damage"], 0)

    def test_missed_attack_does_not_apply_damage(self):
        # wolf's dodge (6 dice) will always beat gladstone's blades (2 dice) at this fixed roll.
        with patch("random.randint", return_value=1):
            self.dm_core._on_action_detected({"skill": "blades", "input": "I attack with my sword"})

        result = self.resolved[-1]
        self.assertFalse(result["success"])
        self.assertNotIn("damage", result)
        self.assertEqual(result["round"], 1)
        self.assertEqual(self.dm_core.get_current_hp("wolf"), 16)

    def test_successful_attack_applies_damage_to_the_target(self):
        # Give the player an opponent with no matching opposing skill, so the attack auto-succeeds (difficulty 0).
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self.dm_core.scenario = {"entities": [{"name": "practice_dummy", "band": 1}]}
        self.dm_core.load_scenario()

        with patch("random.randint", return_value=3):
            self.dm_core._on_action_detected({"skill": "blades", "input": "I attack with my sword"})

        result = self.resolved[-1]
        self.assertTrue(result["success"])
        self.assertIn("damage", result)
        self.assertEqual(result["damage"]["defender"], "practice_dummy")
        self.assertGreater(result["damage"]["net_damage"], 0)
        self.assertEqual(
            self.dm_core.get_current_hp("practice_dummy"),
            20 - result["damage"]["net_damage"],
        )

    def test_non_attack_skill_never_applies_damage(self):
        # "athletics" has no equipped weapon or innate ability tied to it, so no damage should occur even on a hit.
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self.dm_core.scenario = {"entities": [{"name": "practice_dummy", "band": 1}]}
        self.dm_core.load_scenario()

        self.dm_core._on_action_detected({"skill": "athletics", "input": "I climb the wall"})

        result = self.resolved[-1]
        self.assertNotIn("damage", result)

    def test_no_target_narrates_via_action_resolved_not_round_resolved(self):
        # An empty scenario has no target, so this is a non-combat skill use: it should
        # narrate immediately via "action_resolved", not get batched as a combat round.
        action_events = []
        self.event_bus.subscribe("action_resolved", action_events.append)
        self.dm_core.scenario = {"entities": [{"name": "gladstone", "band": 1}]}
        self.dm_core.load_scenario()

        self.dm_core._on_action_detected({"skill": "athletics", "input": "I climb the wall"})

        self.assertEqual(len(action_events), 1)
        self.assertEqual(self.resolved, [])
        self.assertNotIn("round", action_events[0])

    def test_scenario_load_publishes_scenario_loaded(self):
        # DMCore.__init__ (in setUp) should have already published a one-time scene-intro event.
        scenario_events = []
        event_bus = EventBus()
        event_bus.subscribe("scenario_loaded", scenario_events.append)
        DMCore(event_bus)

        self.assertEqual(len(scenario_events), 1)
        self.assertEqual(scenario_events[0]["name"], "The Arena")

    def test_missing_scenario_raises_instead_of_starting_with_no_data(self):
        # A scenario name with no matching file under Rules/Fantasy/scenarios/ must fail
        # loudly -- silently continuing with an empty self.scenario used to let the LLM
        # narrate an opening scene with no name/description (it would happily hallucinate
        # one, ex: a "featureless gray void", with no indication anything had gone wrong).
        with self.assertRaises(FileNotFoundError):
            DMCore(EventBus(), scenario_name="does_not_exist")


class TestMovementAndRange(DMTestCase):
    def setUp(self):
        super().setUp()  # arena: bands=4, enclosed=true, everyone starts band 1
        self.resolved = self._capture_any("round_resolved", "action_resolved")

    def test_every_entity_including_player_has_an_objective_band(self):
        # Deliberately not a "player is always 0" special case anymore -- see DM_Movement.py's
        # module note on why an earlier anchor-relative version was rejected.
        self.dm_core.entities["gladstone"]["band"] = 3
        self.assertEqual(self.dm_core.get_band("gladstone"), 3)

    def test_get_band_defaults_to_one_when_unset(self):
        del self.dm_core.entities["wolf"]["band"]
        self.assertEqual(self.dm_core.get_band("wolf"), 1)

    def test_get_distance_between_computes_the_gap(self):
        self.dm_core.entities["gladstone"]["band"] = 2
        self.dm_core.entities["wolf"]["band"] = 4
        self.dm_core.entities["wolf_2"]["band"] = 1
        self.assertEqual(self.dm_core.get_distance_between("gladstone", "wolf"), 2)
        self.assertEqual(self.dm_core.get_distance_between("wolf", "wolf_2"), 3)

    # --- move_entity: floor, and enclosed-vs-open ceiling ----------------------------------

    def test_move_entity_clamps_at_band_one_floor(self):
        self.dm_core.entities["wolf"]["band"] = 2
        self.assertEqual(self.dm_core.move_entity("wolf", -5), 1)

    def test_move_entity_applies_to_the_player_too(self):
        # No more "no-op for the player" special case -- the player is a normal movable
        # entity like everyone else now.
        self.dm_core.entities["gladstone"]["band"] = 2
        self.assertEqual(self.dm_core.move_entity("gladstone", 1), 3)

    def test_move_entity_caps_at_the_rooms_band_count_when_enclosed(self):
        self.assertTrue(self.dm_core.scenario.get("enclosed"))
        self.assertEqual(self.dm_core.scenario.get("bands"), 4)
        self.dm_core.entities["wolf"]["band"] = 4
        self.assertEqual(self.dm_core.move_entity("wolf", 5), 4)  # hit the wall, stayed put

    def test_move_entity_is_unbounded_when_not_enclosed(self):
        field = DMCore(EventBus(), scenario_name="field")  # bands=6, enclosed=false
        field.entities["wolf"]["band"] = 6
        self.assertEqual(field.move_entity("wolf", 20), 26)  # no ceiling at all -- can flee

    # --- advance_or_retreat: direction is toward/away from current_target ------------------

    def test_advance_moves_the_player_toward_current_target(self):
        self.assertEqual(self.dm_core.current_target, "wolf")
        self.dm_core.entities["gladstone"]["band"] = 1
        self.dm_core.entities["wolf"]["band"] = 4

        self.dm_core.advance_or_retreat("advance")

        self.assertEqual(self.dm_core.get_band("gladstone"), 2)  # moved one band toward wolf

    def test_retreat_moves_the_player_away_from_current_target(self):
        self.dm_core.entities["gladstone"]["band"] = 2
        self.dm_core.entities["wolf"]["band"] = 4

        self.dm_core.advance_or_retreat("retreat")

        self.assertEqual(self.dm_core.get_band("gladstone"), 1)  # moved away from wolf

    def test_retreating_from_one_enemy_can_close_the_gap_to_another(self):
        # The headline reason objective bands replaced the earlier player-anchored model:
        # current_target (wolf) is "ahead" of the player, wolf_2 is "behind" -- retreating
        # from wolf necessarily moves toward wolf_2, since both share the same line and only
        # the player's own band actually moves.
        self.dm_core.entities["gladstone"]["band"] = 3
        self.dm_core.entities["wolf"]["band"] = 4  # current_target, ahead
        self.dm_core.entities["wolf_2"]["band"] = 1  # behind

        moved = self.dm_core.advance_or_retreat("retreat")

        self.assertEqual(self.dm_core.get_band("gladstone"), 2)
        wolf_entry = next(e for e in moved if e["entity"] == "wolf")
        wolf_2_entry = next(e for e in moved if e["entity"] == "wolf_2")
        self.assertEqual(wolf_entry, {"entity": "wolf", "before": 1, "after": 2})  # farther
        self.assertEqual(wolf_2_entry, {"entity": "wolf_2", "before": 2, "after": 1})  # closer

    def test_advance_retreat_is_a_noop_with_no_current_target(self):
        self.dm_core.current_target = None
        self.assertEqual(self.dm_core.advance_or_retreat("advance"), [])

    def test_advance_skips_dead_entities_from_the_moved_report(self):
        self.dm_core.entities["gladstone"]["band"] = 3
        self.dm_core.entities["wolf"]["band"] = 4
        self.dm_core.entities["wolf_2"]["hp"] = 0
        self.dm_core.entities["wolf_2"]["band"] = 1

        moved = self.dm_core.advance_or_retreat("advance")

        self.assertNotIn("wolf_2", {entry["entity"] for entry in moved})

    # --- move_toward_or_away: the creature/ally counterpart to advance_or_retreat ----------

    def test_move_toward_or_away_advances_the_entity_toward_its_opponent(self):
        self.dm_core.entities["wolf"]["band"] = 1
        self.dm_core.entities["gladstone"]["band"] = 4

        result = self.dm_core.move_toward_or_away("wolf", "gladstone", "advance")

        self.assertEqual(self.dm_core.get_band("wolf"), 2)
        self.assertEqual(result, {"opponent": "gladstone", "before": 3, "after": 2})

    def test_move_toward_or_away_retreats_the_entity_away_from_its_opponent(self):
        self.dm_core.entities["wolf"]["band"] = 2
        self.dm_core.entities["gladstone"]["band"] = 4

        result = self.dm_core.move_toward_or_away("wolf", "gladstone", "retreat")

        self.assertEqual(self.dm_core.get_band("wolf"), 1)
        self.assertEqual(result, {"opponent": "gladstone", "before": 2, "after": 3})

    def test_move_toward_or_away_only_moves_the_acting_entity(self):
        # Unlike advance_or_retreat (always the player), this can be called for any entity --
        # only entity_name's own band should change, wolf_2 stays put.
        self.dm_core.entities["wolf"]["band"] = 1
        self.dm_core.entities["wolf_2"]["band"] = 1
        self.dm_core.entities["gladstone"]["band"] = 4

        self.dm_core.move_toward_or_away("wolf", "gladstone", "advance")

        self.assertEqual(self.dm_core.get_band("wolf"), 2)
        self.assertEqual(self.dm_core.get_band("wolf_2"), 1)

    def test_move_toward_or_away_returns_none_for_an_unknown_entity(self):
        self.assertIsNone(self.dm_core.move_toward_or_away("not_a_real_entity", "gladstone", "advance"))
        self.assertIsNone(self.dm_core.move_toward_or_away("wolf", "not_a_real_entity", "advance"))

    # --- is_in_range -------------------------------------------------------------------

    def test_is_in_range_is_always_true_for_non_attack_actions(self):
        self.assertTrue(self.dm_core.is_in_range("gladstone", "wolf", None))

    def test_innate_ability_with_no_range_data_is_also_melee_only(self):
        bite = self.dm_core.resolve_named_ability("wolf", "bite")
        self.dm_core.entities["wolf"]["band"] = 3
        self.assertFalse(self.dm_core.is_in_range("wolf", "gladstone", bite))

    def test_weapon_and_spell_range_thresholds(self):
        # (item, defender band, expected) -- gladstone stays at band 1 throughout, so
        # defender band doubles as the gap between them. Covers melee (longsword has no
        # "range" field, defaulting to 0), a reach weapon (spear, range=1), a ranged
        # weapon (long bow, range=6), and a spell (fireball, range=5), each right at and
        # one band past its own limit.
        cases = [
            ("longsword", 1, True), ("longsword", 2, False),
            ("spear", 1, True), ("spear", 2, True), ("spear", 3, False),
            ("long bow", 7, True), ("long bow", 8, False),
            ("fireball", 6, True), ("fireball", 7, False),
        ]
        for item_name, band, expected in cases:
            with self.subTest(item=item_name, band=band):
                ability = self.dm_core.entities[item_name]
                self.dm_core.entities["wolf"]["band"] = band
                self.assertEqual(self.dm_core.is_in_range("gladstone", "wolf", ability), expected)

    # --- integration through _on_action_detected / resolve_behavior_action ---------------

    def test_out_of_range_attack_is_denied_without_a_roll(self):
        self.dm_core.entities["wolf"]["band"] = 3  # longsword needs gap 0

        self.dm_core._on_action_detected({"skill": "blades", "input": "I attack with my sword"})

        result = self.resolved[-1]
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "out_of_range")
        self.assertIsNone(result["roll"])
        self.assertNotIn("damage", result)

    def test_in_range_ranged_attack_still_resolves_and_can_hit(self):
        self.dm_core.entities["gladstone"]["equipped"]["rhand"] = "long bow"
        self.dm_core.entities["wolf"]["band"] = 4  # gap 3, well within long bow's range=6

        with patch("random.randint", return_value=6):
            self.dm_core._on_action_detected({"skill": "missiles", "input": "I fire an arrow"})

        result = self.resolved[-1]
        self.assertNotEqual(result.get("reason"), "out_of_range")
        self.assertIn("roll", result)

    def test_resolve_behavior_action_advances_when_its_attack_is_out_of_range(self):
        # Closing the distance is the fallback for *any* out-of-reach attack behavior, no
        # TOML authoring required -- unlike fleeing (below), which is an explicit opt-in.
        self.dm_core.entities["wolf"]["band"] = 3  # bite needs gap 0 with gladstone at band 1

        result = self.dm_core.resolve_behavior_action("wolf", "gladstone")

        self.assertEqual(result, {"movement": "advance", "opponent": "gladstone", "before": 2, "after": 1})
        self.assertEqual(self.dm_core.get_band("wolf"), 2)

    def test_resolve_behavior_action_retreats_once_badly_hurt(self):
        # creatures.toml's wolf: hp_per_remain under 0.40 matches its own explicit "retreat"
        # behavior entry, checked ahead of "bite" -- self-preservation wins even though the
        # wolf is already in range and could otherwise attack.
        self.dm_core.apply_damage("wolf", 12)  # 4/16 = 25%, under the 0.40 cutoff
        self.dm_core.entities["wolf"]["band"] = 1
        self.dm_core.entities["gladstone"]["band"] = 1

        result = self.dm_core.resolve_behavior_action("wolf", "gladstone")

        self.assertEqual(result["movement"], "retreat")
        self.assertEqual(self.dm_core.get_band("wolf"), 2)

    def test_resolve_behavior_action_returns_none_when_a_deliberate_move_has_no_valid_opponent(self):
        self.dm_core.entities["fleeing_dummy"] = {
            "name": "fleeing_dummy", "max_hp": 20, "hp": 5, "skills": {},
            "behavior": [{"requirements": [], "action": "retreat"}],
        }
        self.assertIsNone(self.dm_core.resolve_behavior_action("fleeing_dummy", "not_a_real_entity"))

    # --- _on_item_interaction_detected("advance"/"retreat") -------------------------------

    def test_item_interaction_advance_moves_the_player_and_publishes_moved(self):
        item_events = []
        self.event_bus.subscribe("item_interaction_resolved", item_events.append)
        self.dm_core.entities["gladstone"]["band"] = 1
        self.dm_core.entities["wolf"]["band"] = 4

        self.dm_core._on_item_interaction_detected({
            "intent": "advance", "item_name": None, "input": "I advance", "score": None,
        })

        result = item_events[-1]
        self.assertTrue(result["found"])
        self.assertIsNone(result["item_name"])
        self.assertEqual(self.dm_core.get_band("gladstone"), 2)
        wolf_entry = next(e for e in result["moved"] if e["entity"] == "wolf")
        self.assertEqual(wolf_entry, {"entity": "wolf", "before": 3, "after": 2})

    def test_item_interaction_advance_is_not_blocked_by_a_locked_container(self):
        # Unlike take/give/trade, movement never routes through the is_locked gate -- a
        # locked chest in the scene must never stop the player from repositioning.
        dungeon = DMCore(EventBus(), scenario_name="dungeon")
        item_events = []
        dungeon.event_bus.subscribe("item_interaction_resolved", item_events.append)
        self.assertTrue(dungeon.is_locked("chest"))

        dungeon._on_item_interaction_detected({
            "intent": "retreat", "item_name": None, "input": "I back away", "score": None,
        })

        self.assertTrue(item_events[-1]["found"])

    # --- save/load persists band ------------------------------------------------------------

    def test_advance_or_retreat_is_saved_and_restored(self):
        self.dm_core.entities["gladstone"]["band"] = 1
        self.dm_core.entities["wolf"]["band"] = 4
        self.dm_core.advance_or_retreat("advance")
        band_before = self.dm_core.get_band("gladstone")

        self.dm_core.save_game("test_movement_save")
        self.dm_core.entities["gladstone"]["band"] = 1  # mutate away from the saved value
        self.dm_core.load_game("test_movement_save")

        self.assertEqual(self.dm_core.get_band("gladstone"), band_before)

        import shutil
        shutil.rmtree(self.dm_core._save_slot_dir("test_movement_save"), ignore_errors=True)


class TestEntityBehavior(DMTestCase):
    def setUp(self):
        super().setUp()
        self.resolved = self._capture("round_resolved")

    def test_choose_behavior_matches_while_the_entity_is_alive(self):
        # creatures.toml's wolf: a single behavior, "always bite while hp_per_remain >= 0.01".
        behavior = self.dm_core.choose_behavior("wolf")
        assert behavior is not None
        self.assertEqual(behavior["action"], "bite")

    def test_choose_behavior_returns_none_once_effectively_dead(self):
        # Reusing entity_matches_requirements means this needs no death-specific check of its
        # own -- once hp_per_remain drops below 0.01, the requirement simply stops matching.
        self.dm_core.apply_damage("wolf", self.dm_core.get_current_hp("wolf"))
        self.assertEqual(self.dm_core.get_current_hp("wolf"), 0)
        self.assertIsNone(self.dm_core.choose_behavior("wolf"))

    def test_choose_behavior_returns_none_for_an_entity_with_no_behavior_data(self):
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self.assertIsNone(self.dm_core.choose_behavior("practice_dummy"))

    def test_resolve_behavior_action_returns_none_without_a_matching_behavior(self):
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self.assertIsNone(self.dm_core.resolve_behavior_action("practice_dummy", "gladstone"))

    def test_resolve_behavior_action_returns_none_for_an_action_the_entity_doesnt_own(self):
        # A behavior's "action" is looked up the same ownership-gated way as a player naming
        # a technique directly (resolve_named_ability) -- a typo'd or missing ability name
        # must fail safe, not raise or silently roll against nothing.
        self.dm_core.entities["confused_dummy"] = {
            "name": "confused_dummy", "max_hp": 20, "skills": {},
            "behavior": [{"requirements": [], "action": "does_not_exist"}],
        }
        self.assertIsNone(self.dm_core.resolve_behavior_action("confused_dummy", "gladstone"))

    def test_choose_behavior_can_pick_between_a_ranged_and_melee_option_by_distance(self):
        # "distance_to_target" isn't used by any shipped creature yet (see get_comparable_value),
        # but is available for exactly this: a hypothetical archer-brawler choosing its bow
        # while the gap is still open, falling to its fists once the target closes in --
        # opponent_name has to be passed through choose_behavior for this to resolve at all.
        self.dm_core.entities["archer_dummy"] = {
            "name": "archer_dummy", "max_hp": 20, "skills": {},
            "behavior": [
                {
                    "requirements": [{"field": "distance_to_target", "operator": ">", "value": 0}],
                    "action": "shoot",
                },
                {"requirements": [], "action": "punch"},
            ],
        }
        self.dm_core.entities["archer_dummy"]["band"] = 4
        self.dm_core.entities["gladstone"]["band"] = 1

        behavior = self.dm_core.choose_behavior("archer_dummy", "gladstone")
        self.assertEqual(behavior["action"], "shoot")

        self.dm_core.entities["archer_dummy"]["band"] = 1
        behavior = self.dm_core.choose_behavior("archer_dummy", "gladstone")
        self.assertEqual(behavior["action"], "punch")

    def test_choose_behavior_without_an_opponent_never_matches_a_distance_requirement(self):
        self.dm_core.entities["archer_dummy"] = {
            "name": "archer_dummy", "max_hp": 20, "skills": {},
            "behavior": [
                {
                    "requirements": [{"field": "distance_to_target", "operator": ">", "value": 0}],
                    "action": "shoot",
                },
            ],
        }
        self.assertIsNone(self.dm_core.choose_behavior("archer_dummy"))

    def test_resolve_behavior_action_strikes_back_and_applies_damage(self):
        # An unarmored, skill-less target so the wolf's bite always lands and nothing
        # reduces the raw damage -- isolates resolve_behavior_action from armor/opposed-skill
        # specifics, which are already covered by TestDamageCalculation/TestOpposedResolution.
        self.dm_core.entities["target_dummy"] = {"name": "target_dummy", "max_hp": 20, "skills": {}}

        with patch("random.randint", return_value=4):
            result = self.dm_core.resolve_behavior_action("wolf", "target_dummy")

        assert result is not None
        self.assertTrue(result["success"])
        self.assertEqual(result["skill"], "brawling")
        self.assertIn("damage", result)
        self.assertGreater(result["damage"]["net_damage"], 0)
        self.assertEqual(
            self.dm_core.get_current_hp("target_dummy"),
            20 - result["damage"]["net_damage"],
        )

    def test_combat_round_includes_the_targets_counterattack(self):
        # The default "arena" scenario's first wolf is hostile, so its own behavior should
        # fire in the same round as the player's action -- proving combat is now mutual
        # rather than only ever the player rolling to hit.
        with patch("random.randint", return_value=4):
            self.dm_core._on_action_detected({"skill": "athletics", "input": "I vault over the rubble"})

        result = self.resolved[-1]
        self.assertIn("turns", result)
        turns_by_actor = {turn["actor"]: turn for turn in result["turns"]}
        self.assertIn("wolf", turns_by_actor)
        self.assertEqual(turns_by_actor["wolf"]["skill"], "brawling")
        self.assertEqual(turns_by_actor["wolf"]["defender"], "gladstone")

    def test_target_without_behavior_data_does_not_counterattack(self):
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self.dm_core.scenario = {"entities": [{"name": "practice_dummy", "band": 1}]}
        self.dm_core.load_scenario()

        self.dm_core._on_action_detected({"skill": "athletics", "input": "I test my footing"})

        self.assertNotIn("turns", self.resolved[-1])

    def test_ally_acts_alongside_enemy_in_the_same_round(self):
        # arena.toml's thane has positive disposition toward gladstone (not hostile) but its
        # own [[entity.behavior]] too -- it should attack current_target (the wolf) the same
        # round the wolf attacks the player, proving allies pull their weight alongside enemies.
        with patch("random.randint", return_value=4):
            self.dm_core._on_action_detected({"skill": "athletics", "input": "I brace myself"})

        turns_by_actor = {turn["actor"]: turn for turn in self.resolved[-1]["turns"]}
        self.assertIn("thane", turns_by_actor)
        self.assertEqual(turns_by_actor["thane"]["defender"], "wolf")
        self.assertIn("wolf", turns_by_actor)
        self.assertEqual(turns_by_actor["wolf"]["defender"], "gladstone")

    def test_roll_initiative_pools_dodge_and_untrained_observation(self):
        # wolf has dodge 6D/0 pips and no observation skill at all -- rules.toml's [[initiative]]
        # still pools it in at the same untrained 1D/0 pips resolve_action defaults missing
        # skills to, for a combined 7D pool.
        with patch("random.randint", return_value=4):
            initiative = self.dm_core.roll_initiative("wolf")
        self.assertEqual(initiative, 28)

    def test_round_resolved_attaches_initiative_to_player_and_every_turn(self):
        with patch("random.randint", return_value=4):
            self.dm_core._on_action_detected({"skill": "athletics", "input": "I brace myself"})

        result = self.resolved[-1]
        self.assertIn("initiative", result)
        for turn in result["turns"]:
            self.assertIn("initiative", turn)

    def test_turns_are_ordered_by_initiative_descending(self):
        # arena.toml's default scene has two wolves plus thane. Both wolves share dodge (6D) +
        # untrained observation (1D) = 7D, outrolling thane's dodge (3D) + untrained observation
        # (1D) = 4D once every die lands the same (patched to 4) -- so both wolves (tied,
        # stable-sorted in their original declaration order) sort ahead of thane in "turns".
        with patch("random.randint", return_value=4):
            self.dm_core._on_action_detected({"skill": "athletics", "input": "I brace myself"})

        turns = self.resolved[-1]["turns"]
        self.assertEqual([turn["actor"] for turn in turns], ["wolf", "wolf_2", "thane"])
        self.assertEqual(turns[0]["initiative"], 28)
        self.assertEqual(turns[1]["initiative"], 28)
        self.assertGreater(turns[0]["initiative"], turns[2]["initiative"])

    def test_current_target_advances_to_next_hostile_when_current_dies(self):
        self.dm_core.apply_damage("wolf", 999)
        with patch("random.randint", return_value=1):
            self.dm_core._on_action_detected({"skill": "athletics", "input": "I reposition"})
        self.assertEqual(self.dm_core.current_target, "wolf_2")

    def test_current_target_falls_back_to_a_living_ally_once_every_enemy_is_dead(self):
        self.dm_core.apply_damage("wolf", 999)
        self.dm_core.apply_damage("wolf_2", 999)
        with patch("random.randint", return_value=1):
            self.dm_core._on_action_detected({"skill": "athletics", "input": "I catch my breath"})
        # Neither wolf is hostile-and-alive anymore, so current_target falls back to the first
        # living non-player entity (thane) instead of staying pinned on a corpse -- and since
        # thane isn't hostile, the *next* action resolves as action_resolved, not
        # round_resolved, meaning combat has actually ended.
        self.assertEqual(self.dm_core.current_target, "thane")

    def test_choose_combat_target_returns_none_when_nothing_is_alive(self):
        self.dm_core.apply_damage("wolf", 999)
        self.dm_core.apply_damage("wolf_2", 999)
        self.dm_core.apply_damage("thane", 999)
        self.assertIsNone(self.dm_core._choose_combat_target())

    def test_explicit_target_override_redirects_current_target(self):
        self.dm_core._on_action_detected({
            "skill": "athletics", "input": "I focus on the second wolf", "target": "wolf_2",
        })
        self.assertEqual(self.dm_core.current_target, "wolf_2")

    def test_explicit_target_override_ignored_if_not_hostile(self):
        # thane is a live scene entity, just not a hostile one -- naming it should not make it
        # the player's combat target.
        self.dm_core._on_action_detected({"skill": "athletics", "input": "...", "target": "thane"})
        self.assertEqual(self.dm_core.current_target, "wolf")

    def test_explicit_target_override_ignored_if_dead(self):
        self.dm_core.apply_damage("wolf_2", 999)
        self.dm_core._on_action_detected({"skill": "athletics", "input": "...", "target": "wolf_2"})
        self.assertEqual(self.dm_core.current_target, "wolf")

    def test_explicit_target_override_ignored_if_not_in_scene(self):
        self.dm_core._on_action_detected({"skill": "athletics", "input": "...", "target": "fire elemental"})
        self.assertEqual(self.dm_core.current_target, "wolf")


class TestBandit(DMTestCase):
    def setUp(self):
        super().setUp()
        self.dm_core.scenario = {
            "bands": 8, "enclosed": False,
            "entities": [{"name": "gladstone", "band": 1}, {"name": "bandit", "band": 5}],
        }
        self.dm_core.load_scenario()

    def test_bow_and_sword_resolve_as_real_item_entities(self):
        # Not [[entity.abilities]] inline duplicates -- "short bow"/"rusty shortsword" resolve
        # straight to their own items.toml entities, range/damage_value/damage_tags included.
        bow = self.dm_core.resolve_named_ability("bandit", "short bow")
        sword = self.dm_core.resolve_named_ability("bandit", "rusty shortsword")
        self.assertEqual(bow["range"], 4)
        self.assertEqual(bow["skill"], "missiles")
        self.assertNotIn("range", sword)  # melee, same as any other unlisted-range weapon
        self.assertEqual(sword["skill"], "blades")

    def test_favors_the_bow_at_a_distance(self):
        # Starting gap is 4 -- exactly the short bow's own range, so it's both "not adjacent"
        # (distance_to_target > 0, the behavior's own requirement) and actually reachable.
        behavior = self.dm_core.choose_behavior("bandit", "gladstone")
        self.assertEqual(behavior["action"], "short bow")

        turn = self.dm_core.resolve_behavior_action("bandit", "gladstone")
        self.assertEqual(turn["skill"], "missiles")
        self.assertNotIn("movement", turn)

    def test_switches_to_the_sword_once_adjacent(self):
        self.dm_core.entities["bandit"]["band"] = 1  # same band as gladstone -- gap 0

        behavior = self.dm_core.choose_behavior("bandit", "gladstone")
        self.assertEqual(behavior["action"], "rusty shortsword")

        turn = self.dm_core.resolve_behavior_action("bandit", "gladstone")
        self.assertEqual(turn["skill"], "blades")
        self.assertNotIn("movement", turn)

    def test_closes_distance_instead_of_shooting_past_the_bows_own_range(self):
        # distance_to_target > 0 alone still picks "short bow" (it doesn't know the bow's own
        # range, just that there's a gap) -- is_in_range is what actually catches this, falling
        # back to the implicit "advance" resolve_behavior_action already provides for any
        # out-of-reach attack, no bandit-specific TOML needed for this part.
        self.dm_core.entities["bandit"]["band"] = 6  # gap 5, past the short bow's range = 4

        turn = self.dm_core.resolve_behavior_action("bandit", "gladstone")

        self.assertEqual(turn, {"movement": "advance", "opponent": "gladstone", "before": 5, "after": 4})

    def test_flees_once_badly_hurt_instead_of_drawing_the_sword(self):
        self.dm_core.entities["bandit"]["band"] = 1  # adjacent -- sword would otherwise fire
        self.dm_core.apply_damage("bandit", 15)  # 3/18 ~= 17%, under the 0.40 self-preservation cutoff

        turn = self.dm_core.resolve_behavior_action("bandit", "gladstone")

        self.assertEqual(turn["movement"], "retreat")

    def test_field_scenario_seats_the_bandit_at_short_bow_range(self):
        # field.toml's own starting band (5) is chosen to already sit at gap 4 from gladstone
        # (band 1) -- the short bow's own range -- so the very first round of a real playthrough
        # already demonstrates the ranged-over-melee choice, not just this test's own setUp.
        field = DMCore(EventBus(), scenario_name="field")
        self.assertIn("bandit", field.scenario_entities)
        self.assertEqual(field.get_distance_between("gladstone", "bandit"), 4)


class TestStatusEvaluation(DMTestCase):
    def test_hp_per_remain_requirement_matches_current_percentage(self):
        # gladstone: max_hp 36. At 18 hp (50%) the "wounded" status (0.40-0.59) should match.
        self.dm_core.apply_damage("gladstone", 18)
        matched_names = [s["name"] for s in self.dm_core.get_applicable_statuses("gladstone", "on_damage")]
        self.assertIn("wounded", matched_names)
        self.assertNotIn("severe", matched_names)

    def test_trigger_filters_out_non_matching_statuses(self):
        # Even at full HP (matches "bruised"'s 0.81-0.99 range), a different trigger name should match nothing.
        matched = self.dm_core.get_applicable_statuses("gladstone", "on_turn_start")
        self.assertEqual(matched, [])

    def test_not_in_requirement_blocks_a_match(self):
        self.dm_core.rules["status"] = [{
            "name": "test_exclude",
            "trigger": "on_damage",
            "requirements": [{"field": "supertype", "operator": "not_in", "value": ["creature"]}],
        }]
        # gladstone is supertype "creature", so this status must not match.
        matched = self.dm_core.get_applicable_statuses("gladstone", "on_damage")
        self.assertEqual(matched, [])

    def test_in_requirement_requires_a_match(self):
        self.dm_core.rules["status"] = [{
            "name": "test_include",
            "trigger": "on_damage",
            "requirements": [{"field": "supertype", "operator": "in", "value": ["undead"]}],
        }]
        # gladstone is supertype "creature", not "undead", so this status must not match.
        matched = self.dm_core.get_applicable_statuses("gladstone", "on_damage")
        self.assertEqual(matched, [])

        self.dm_core.entities["gladstone"]["supertype"] = "undead"
        matched = self.dm_core.get_applicable_statuses("gladstone", "on_damage")
        self.assertEqual([s["name"] for s in matched], ["test_include"])

    def test_distance_to_target_resolves_the_band_gap_to_the_given_opponent(self):
        self.dm_core.entities["gladstone"]["band"] = 1
        self.dm_core.entities["wolf"]["band"] = 4
        self.assertEqual(self.dm_core.get_comparable_value("wolf", "distance_to_target", "gladstone"), 3)

    def test_distance_to_target_is_none_without_an_opponent(self):
        # A status's own requirements never pass opponent_name (see get_applicable_statuses),
        # so a requirement that names "distance_to_target" there can never accidentally match.
        self.assertIsNone(self.dm_core.get_comparable_value("wolf", "distance_to_target"))

    def test_distance_to_target_requirement_is_forwarded_through_entity_matches_requirements(self):
        self.dm_core.entities["gladstone"]["band"] = 1
        self.dm_core.entities["wolf"]["band"] = 1
        requirements = [{"field": "distance_to_target", "operator": ">", "value": 0}]
        self.assertFalse(self.dm_core.entity_matches_requirements("wolf", requirements, "gladstone"))
        self.dm_core.entities["wolf"]["band"] = 3
        self.assertTrue(self.dm_core.entity_matches_requirements("wolf", requirements, "gladstone"))

    def test_unknown_operator_never_matches(self):
        self.dm_core.rules["status"] = [{
            "name": "test_bad_operator",
            "trigger": "on_damage",
            "requirements": [{"field": "hp_per_remain", "operator": "~=", "value": 1}],
        }]
        matched = self.dm_core.get_applicable_statuses("gladstone", "on_damage")
        self.assertEqual(matched, [])

    def test_apply_damage_auto_applies_matching_condition(self):
        self.dm_core.apply_damage("gladstone", 18)  # -> 50% hp -> "wounded"
        self.assertIn("wounded", self.dm_core.entities["gladstone"]["active_conditions"])

    def test_apply_damage_does_not_apply_when_no_status_matches(self):
        # A single point of damage keeps gladstone above 0.99 hp_per_remain (the top of "bruised"'s
        # range is 0.99, and no status covers 1.0), so nothing should be applied yet. active_conditions
        # itself always exists now (seeded empty from the template's "conditions" at instancing time,
        # see load_scenario), so the check here is emptiness, not absence of the key.
        self.dm_core.apply_damage("gladstone", 0)
        self.assertEqual(self.dm_core.entities["gladstone"]["active_conditions"], {})

    def test_progressing_through_tiers_dismisses_the_stale_one(self):
        # Once a worse tier matches, the previous tier's requirements no longer hold, so
        # evaluate_statuses dismisses it instead of letting conditions accumulate.
        self.dm_core.apply_damage("gladstone", 18)  # 50% -> wounded
        self.dm_core.apply_damage("gladstone", 13)  # ~14% -> incapacitated
        active = self.dm_core.entities["gladstone"]["active_conditions"]
        self.assertNotIn("wounded", active)
        self.assertIn("incapacitated", active)

    def test_healing_back_above_a_tier_dismisses_its_condition(self):
        self.dm_core.apply_damage("gladstone", 18)  # 50% -> wounded
        self.assertIn("wounded", self.dm_core.entities["gladstone"]["active_conditions"])
        self.dm_core.apply_healing("gladstone", 999)  # back to full -> no tier matches
        self.assertNotIn("wounded", self.dm_core.entities["gladstone"]["active_conditions"])

    def test_dead_condition_is_not_auto_dismissed_by_healing(self):
        # "dead"'s apply block sets dismiss = "resurrection", so simple healing must not
        # revive it via the same automatic sweep that clears "wounded"/"stunned"/etc.
        self.dm_core.apply_damage("gladstone", 36)  # 0% -> dead
        self.assertIn("dead", self.dm_core.entities["gladstone"]["active_conditions"])
        self.dm_core.apply_healing("gladstone", 999)
        self.assertIn("dead", self.dm_core.entities["gladstone"]["active_conditions"])


class TestScenarioLoading(DMTestCase):
    def test_duplicate_entities_get_unique_instance_names(self):
        # arena.toml lists gladstone once, wolf twice, and thane (an ally) once.
        self.assertEqual(self.dm_core.scenario_entities, ["gladstone", "wolf", "wolf_2", "thane"])
        self.assertIn("wolf", self.dm_core.entities)
        self.assertIn("wolf_2", self.dm_core.entities)

    def test_current_target_defaults_to_the_first_hostile_entity_skipping_allies(self):
        # thane (non-hostile, an ally) is listed after both wolves in arena.toml, but even if
        # it weren't, current_target must never default to an ally -- it's chosen by hostility,
        # not by list position.
        self.assertEqual(self.dm_core.current_target, "wolf")

    def test_current_target_skips_an_ally_even_when_listed_first(self):
        self.dm_core.scenario = {"entities": [
            {"name": "thane", "band": 1}, {"name": "wolf", "band": 1},
        ]}
        self.dm_core.load_scenario()
        self.assertEqual(self.dm_core.current_target, "wolf")

    def test_duplicate_instances_are_independent(self):
        self.dm_core.apply_damage("wolf", 10)
        self.assertEqual(self.dm_core.get_current_hp("wolf"), 6)
        self.assertEqual(self.dm_core.get_current_hp("wolf_2"), 16)

    def test_instances_carry_their_own_entity_id(self):
        # entity_id lives on the instance itself, so it's still identifiable
        # once the dict is passed around independently of its self.entities key.
        self.assertEqual(self.dm_core.entities["gladstone"]["entity_id"], "gladstone")
        self.assertEqual(self.dm_core.entities["wolf"]["entity_id"], "wolf")
        self.assertEqual(self.dm_core.entities["wolf_2"]["entity_id"], "wolf_2")

    def test_instances_carry_their_scenario_band(self):
        self.dm_core.scenario = {"entities": [
            {"name": "wolf", "band": 2},
            {"name": "wolf", "band": 5},
        ]}
        self.dm_core.load_scenario()
        self.assertEqual(self.dm_core.entities["wolf"]["band"], 2)
        self.assertEqual(self.dm_core.entities["wolf_2"]["band"], 5)

    def test_unknown_entity_in_scenario_is_skipped_not_crashed(self):
        self.dm_core.scenario = {"entities": [{"name": "griffin", "band": 1}]}
        self.dm_core.load_scenario()
        self.assertEqual(self.dm_core.scenario_entities, [])

    def test_get_target_name_skips_the_player(self):
        target = self.dm_core._get_target_name()
        self.assertEqual(target, "wolf")

    def test_reloading_scenario_resets_instances(self):
        self.dm_core.scenario = {"entities": [{"name": "wolf", "band": 1}]}
        self.dm_core.load_scenario()
        self.assertEqual(self.dm_core.scenario_entities, ["wolf"])


class TestLockedChest(DMTestCase):
    # Rules/Fantasy/scenarios/dungeon.toml puts the player alone with a locked chest
    # (items.toml's "chest": [entity.test] {difficulty=12, skill=["finesse"]}, starting
    # condition "locked").
    scenario_name = "dungeon"

    def setUp(self):
        super().setUp()
        self.action_events = self._capture("action_resolved")
        self.round_events = self._capture("round_resolved")

    def test_chest_starts_locked(self):
        # Seeded from the template's [entity.conditions.locked] at instancing time (load_scenario).
        self.assertTrue(self.dm_core.is_locked("chest"))
        self.assertIn("locked", self.dm_core.entities["chest"]["active_conditions"])

    def test_chest_is_never_hostile_despite_no_attitude_data(self):
        # Objects opt out of combat routing regardless of the neutral-disposition default
        # that would otherwise mark a no-attitude-data entity as hostile (ex: wolf).
        self.assertFalse(self.dm_core.is_hostile("chest", "gladstone"))

    def test_forcing_it_with_strength_falls_through_to_the_opposed_fortitude_path(self):
        # "strength" isn't in the chest's [entity.test] skill list, so this isn't a lock pick
        # attempt at all -- it falls through to the *normal* resolve_opposed_action path, which
        # finds the chest's own "fortitude" (5 dice, since strength's `opposes` includes
        # "fortitude") and uses that as the difficulty. No special-casing needed for this at all.
        with patch("random.randint", return_value=1):  # gladstone: 2 dice @ 1 = 2 vs chest: 5 @ 1 = 5
            self.dm_core._on_action_detected({"skill": "strength", "input": "I try to force the chest"})

        self.assertTrue(self.dm_core.is_locked("chest"))  # forcing it isn't what removes "locked"
        self.assertEqual(self.round_events, [])
        result = self.action_events[-1]
        self.assertFalse(result["success"])
        self.assertEqual(result["defender"], "chest")
        self.assertEqual(result["opposing_skill"], "fortitude")
        self.assertEqual(result["difficulty"], 5)

    def test_failed_pick_leaves_it_locked_and_applies_jammed_on_fail(self):
        with patch("random.randint", return_value=1):  # 3 dice @ 1 = 3, well under test difficulty 12
            self.dm_core._on_action_detected({"skill": "finesse", "input": "I pick the lock"})

        self.assertTrue(self.dm_core.is_locked("chest"))
        self.assertEqual(self.round_events, [])
        result = self.action_events[-1]
        self.assertFalse(result["success"])
        self.assertEqual(result["defender"], "chest")
        self.assertIsNone(result["opposing_skill"])
        self.assertEqual(result["difficulty"], 12)
        # [entity.test.fail] applies the permanent "jammed" condition.
        self.assertIn("jammed", self.dm_core.entities["chest"]["active_conditions"])

    def test_successful_pick_dismisses_the_locked_condition_without_forcing_loot(self):
        # Opening the chest must NOT auto-transfer its contents -- a player should be able to
        # examine what's inside (ex: a cursed weapon) before ever deciding to take it. See
        # TestItemInteraction for the separate examine/take mechanism.
        starting_currency = self.dm_core.entities["gladstone"]["currency"]
        with patch("random.randint", return_value=6):  # 3 dice @ 6 = 18, clears test difficulty 12
            self.dm_core._on_action_detected({"skill": "finesse", "input": "I pick the lock"})

        self.assertFalse(self.dm_core.is_locked("chest"))
        self.assertNotIn("jammed", self.dm_core.entities["chest"]["active_conditions"])
        result = self.action_events[-1]
        self.assertTrue(result["success"])
        self.assertEqual(result["defender"], "chest")
        self.assertNotIn("loot", result)

        self.assertEqual(self.dm_core.entities["chest"]["currency"], 20)
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], starting_currency)
        self.assertIn("cursed dagger", self.dm_core.entities["chest"]["inventory"])

    def test_picking_an_already_open_chest_does_not_retrigger_the_test_or_reloot(self):
        # requires_condition="locked" means the test is only attemptable while locked -- once
        # dismissed, a repeat "finesse" attempt must fall through to the normal opposed path
        # (difficulty 0, since the chest has no observation/reflexes for finesse to oppose),
        # not silently re-run [entity.test] and re-loot an already-empty chest.
        with patch("random.randint", return_value=6):
            self.dm_core._on_action_detected({"skill": "finesse", "input": "I pick the lock"})
        currency_after_first_pick = self.dm_core.entities["gladstone"]["currency"]

        self.dm_core._on_action_detected({"skill": "finesse", "input": "I pick the lock again"})

        result = self.action_events[-1]
        self.assertEqual(result["difficulty"], 0)
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], currency_after_first_pick)

    def test_jammed_permanently_blocks_further_pick_attempts(self):
        # blocks_if_condition="jammed" means once jammed (applied by test.fail), the test can
        # never be attempted again via finesse -- even a roll that would clear difficulty 12
        # must fall through to the normal opposed path instead (difficulty 0), leaving it locked.
        with patch("random.randint", return_value=1):  # fails, applies "jammed"
            self.dm_core._on_action_detected({"skill": "finesse", "input": "I fumble the lock"})
        self.assertIn("jammed", self.dm_core.entities["chest"]["active_conditions"])

        with patch("random.randint", return_value=6):  # would clear difficulty 12 if it ran
            self.dm_core._on_action_detected({"skill": "finesse", "input": "I try again"})

        self.assertTrue(self.dm_core.is_locked("chest"))
        result = self.action_events[-1]
        self.assertEqual(result["difficulty"], 0)

    def test_dismiss_condition_is_a_noop_for_a_condition_not_present(self):
        self.assertFalse(self.dm_core.dismiss_condition("chest", "not_a_real_condition"))
        self.assertTrue(self.dm_core.is_locked("chest"))  # unaffected

    def test_apply_test_outcome_is_a_noop_for_empty_outcome(self):
        self.dm_core.apply_test_outcome("chest", "")
        self.dm_core.apply_test_outcome("chest", None)
        self.assertEqual(
            self.dm_core.entities["chest"]["active_conditions"],
            {"locked": {"duration": "permanent"}, "closed": {"duration": "permanent"}},
        )

    def test_apply_test_outcome_reveal_key_applies_identified(self):
        self.assertFalse(self.dm_core.is_identified("chest"))
        self.dm_core.apply_test_outcome("chest", {"reveal": True})
        self.assertTrue(self.dm_core.is_identified("chest"))

    def test_apply_test_outcome_without_reveal_key_leaves_it_unidentified(self):
        self.dm_core.apply_test_outcome("chest", {"dismiss_condition": "locked"})
        self.assertFalse(self.dm_core.is_identified("chest"))


class TestItemInteraction(DMTestCase):
    # dungeon.toml's chest carries a "cursed dagger" plus currency=20, for exercising
    # examine (read-only) vs take (transfers) without any dice roll involved.
    scenario_name = "dungeon"

    def setUp(self):
        super().setUp()
        self.resolved = self._capture("item_interaction_resolved")

    def test_examine_and_take_are_blocked_while_the_container_is_locked(self):
        self.dm_core._on_item_interaction_detected({
            "intent": "examine", "item_name": "cursed dagger", "input": "I examine the cursed dagger",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "locked")
        self.assertNotIn("cursed dagger", self.dm_core.entities["gladstone"]["inventory"])

    def _unlock_the_chest(self):
        self.dm_core.roll_dice = lambda dice, pips: 99
        self.dm_core._on_action_detected({"skill": "finesse", "input": "I pick the lock"})

    def _open_the_chest(self):
        # Unlocking and opening are independent conditions -- picking the lock only dismisses
        # "locked"; reaching the chest's *contents* also requires "closed" to be dismissed.
        self.dm_core._on_item_interaction_detected({
            "intent": "open", "item_name": None, "input": "I open the chest",
        })

    def test_examine_and_take_are_blocked_while_closed_but_unlocked(self):
        self._unlock_the_chest()
        self.dm_core._on_item_interaction_detected({
            "intent": "examine", "item_name": "cursed dagger", "input": "I examine the cursed dagger",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "closed")

    def test_examine_describes_an_item_without_transferring_it(self):
        self._unlock_the_chest()
        self._open_the_chest()
        self.dm_core._on_item_interaction_detected({
            "intent": "examine", "item_name": "cursed dagger", "input": "I examine the cursed dagger",
        })
        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertIn("runes", result["description"])
        self.assertNotIn("cursed dagger", self.dm_core.entities["gladstone"]["inventory"])
        self.assertIn("cursed dagger", self.dm_core.entities["chest"]["inventory"])
        # Never leaked before a real check earns it -- see TestItemTargetedSkillCheck for the
        # arcane check that's actually supposed to reveal this.
        self.assertEqual(result["revealed"], [])
        self.assertNotIn("cursed", result["description"])

    def test_examine_surfaces_revealed_tags_once_identified(self):
        self._unlock_the_chest()
        self._open_the_chest()
        self.dm_core.apply_condition("cursed dagger", "identified", duration="permanent", dismiss="")

        self.dm_core._on_item_interaction_detected({
            "intent": "examine", "item_name": "cursed dagger", "input": "I examine the cursed dagger",
        })

        result = self.resolved[-1]
        self.assertEqual(result["revealed"], ["cursed"])

    def test_open_reveals_real_contents_without_mechanical_data(self):
        # This is what actually fixes the chest hallucinating invented treasure -- LLMCore
        # now has the real inventory to narrate from instead of nothing at all. Built from
        # describe_character (flavor description only) -- exactly its output and nothing
        # more, so items.toml's "tags" field (["cursed"]) is never separately appended, even
        # though the item's own *name* happens to contain the word "cursed" regardless.
        self._unlock_the_chest()
        self.dm_core._on_item_interaction_detected({
            "intent": "open", "item_name": None, "input": "I open the chest",
        })
        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["contents"], [self.dm_core.describe_character("cursed dagger")])
        self.assertNotIn("tags", result["contents"][0])

    def test_open_an_empty_container_reports_empty_contents(self):
        self._unlock_the_chest()
        self.dm_core.entities["chest"]["inventory"] = []
        self.dm_core._on_item_interaction_detected({
            "intent": "open", "item_name": None, "input": "I open the chest",
        })
        self.assertEqual(self.resolved[-1]["contents"], [])

    def test_take_transfers_the_item(self):
        self._unlock_the_chest()
        self._open_the_chest()
        self.dm_core._on_item_interaction_detected({
            "intent": "take", "item_name": "cursed dagger", "input": "I take the cursed dagger",
        })
        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertIn("cursed dagger", self.dm_core.entities["gladstone"]["inventory"])
        self.assertNotIn("cursed dagger", self.dm_core.entities["chest"]["inventory"])

    def test_currency_examine_and_take_use_transfer_currency_not_transfer_item(self):
        self._unlock_the_chest()
        self._open_the_chest()
        self.dm_core._on_item_interaction_detected({
            "intent": "examine", "item_name": "currency", "input": "I check the gold",
        })
        examine_result = self.resolved[-1]
        self.assertTrue(examine_result["found"])
        self.assertIn("20", examine_result["description"])
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 100)  # unchanged

        self.dm_core._on_item_interaction_detected({
            "intent": "take", "item_name": "currency", "input": "I take the gold",
        })
        take_result = self.resolved[-1]
        self.assertTrue(take_result["found"])
        self.assertEqual(take_result["amount"], 20)
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 120)
        self.assertEqual(self.dm_core.entities["chest"]["currency"], 0)

    def test_item_not_present_is_reported_not_present(self):
        self._unlock_the_chest()
        self._open_the_chest()
        self.dm_core._on_item_interaction_detected({
            "intent": "take", "item_name": "longsword", "input": "I take the longsword",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_present")

    def test_examining_the_container_itself_describes_it(self):
        self._unlock_the_chest()
        self.dm_core._on_item_interaction_detected({
            "intent": "examine", "item_name": "chest", "input": "I examine the chest",
        })
        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertIn("iron-bound", result["description"])

    def test_taking_the_container_itself_is_not_takeable(self):
        self._unlock_the_chest()
        self.dm_core._on_item_interaction_detected({
            "intent": "take", "item_name": "chest", "input": "I take the whole chest",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_takeable")


class TestItemTargetedSkillCheck(DMTestCase):
    scenario_name = "dungeon"

    def setUp(self):
        super().setUp()
        self.action_events = self._capture("action_resolved")
        self.round_events = self._capture("round_resolved")
        self.dm_core.dismiss_condition("chest", "locked")
        self.dm_core.dismiss_condition("chest", "closed")

    def _check_the_dagger(self, roll_result):
        self.dm_core.roll_dice = lambda dice, pips: roll_result
        self.dm_core._on_action_detected({
            "skill": "arcane", "target": "cursed dagger", "input": "I check the dagger for curses",
        })

    def test_unreachable_item_is_not_a_test_target(self):
        # Still inside a locked, closed chest -- nothing to detect requirements the item's
        # own test would otherwise accept.
        self.dm_core.entities["chest"]["active_conditions"] = {"locked": {"duration": "permanent"}}
        self.assertIsNone(self.dm_core._resolve_item_test_target("cursed dagger", "arcane"))

    def test_item_in_an_open_container_is_reachable(self):
        self.assertEqual(self.dm_core._resolve_item_test_target("cursed dagger", "arcane"), "cursed dagger")

    def test_item_already_in_player_inventory_is_reachable(self):
        self.dm_core.transfer_item("chest", "gladstone", "cursed dagger")
        self.assertEqual(self.dm_core._resolve_item_test_target("cursed dagger", "arcane"), "cursed dagger")

    def test_wrong_skill_does_not_match_the_items_test(self):
        # "blades" isn't in the dagger's test.skill (["arcane"]) -- not a test target at all,
        # same as any other skill against an entity whose test doesn't list it.
        self.assertIsNone(self.dm_core._resolve_item_test_target("cursed dagger", "blades"))

    def test_successful_check_reveals_tags_and_marks_identified(self):
        self._check_the_dagger(roll_result=8)  # clears the dagger's own test difficulty (8)

        self.assertEqual(self.round_events, [])  # inspecting an item is never combat
        result = self.action_events[-1]
        self.assertTrue(result["success"])
        self.assertEqual(result["defender"], "cursed dagger")
        self.assertIsNone(result["opposing_skill"])
        self.assertEqual(result["revealed"], ["cursed"])
        self.assertTrue(self.dm_core.is_identified("cursed dagger"))

    def test_failed_check_reveals_nothing(self):
        self._check_the_dagger(roll_result=1)  # well under the dagger's own test difficulty (8)

        result = self.action_events[-1]
        self.assertFalse(result["success"])
        self.assertNotIn("revealed", result)
        self.assertFalse(self.dm_core.is_identified("cursed dagger"))

    def test_identified_item_blocks_a_repeat_check(self):
        # blocks_if_condition="identified" -- once known, re-checking it is pointless and
        # falls through to whatever an ordinary "arcane" action against current_target would
        # do instead, exactly like the chest's own "jammed"/"locked" gating.
        self._check_the_dagger(roll_result=8)
        self.assertTrue(self.dm_core.is_identified("cursed dagger"))

        self.assertIsNone(self.dm_core._resolve_item_test_target("cursed dagger", "arcane"))

    def test_item_test_never_advances_round_number_or_touches_current_target(self):
        starting_target = self.dm_core.current_target
        starting_round = self.dm_core.round_number

        self._check_the_dagger(roll_result=8)

        self.assertEqual(self.dm_core.round_number, starting_round)
        self.assertEqual(self.dm_core.current_target, starting_target)


class TestHealthPotionIdentify(DMTestCase):
    def setUp(self):
        super().setUp()
        self.action_events = self._capture("action_resolved")

    def _check_the_potion(self, skill_name, roll_result):
        self.dm_core.roll_dice = lambda dice, pips: roll_result
        self.dm_core._on_action_detected({
            "skill": skill_name, "target": "health potion", "input": "I appraise the health potion",
        })

    def test_starts_unidentified_with_no_hint_in_its_description(self):
        self.assertFalse(self.dm_core.is_identified("health potion"))
        self.assertNotIn("healing", self.dm_core.entities["health potion"]["description"].lower())

    def test_successful_check_reveals_healing_and_marks_identified(self):
        self._check_the_potion("appraise", roll_result=4)  # clears difficulty 4
        result = self.action_events[-1]
        self.assertTrue(result["success"])
        self.assertEqual(result["revealed"], ["healing"])
        self.assertTrue(self.dm_core.is_identified("health potion"))

    def test_failed_check_reveals_nothing(self):
        self._check_the_potion("appraise", roll_result=1)  # well under difficulty 4
        result = self.action_events[-1]
        self.assertFalse(result["success"])
        self.assertNotIn("revealed", result)
        self.assertFalse(self.dm_core.is_identified("health potion"))

    def test_medicine_also_qualifies_as_a_valid_identify_skill(self):
        self._check_the_potion("medicine", roll_result=4)
        self.assertTrue(self.dm_core.is_identified("health potion"))

    def test_identifying_one_potion_identifies_every_copy_at_once(self):
        # Fungible items share one template dict (see this class's own docstring) -- there's
        # no per-instance "which copy" to distinguish.
        self.assertEqual(self.dm_core.entities["gladstone"]["inventory"].count("health potion"), 3)
        self._check_the_potion("appraise", roll_result=4)
        self.assertTrue(self.dm_core.is_identified("health potion"))

    def test_identified_potion_blocks_a_repeat_check(self):
        self._check_the_potion("appraise", roll_result=4)
        self.assertTrue(self.dm_core.is_identified("health potion"))

        self.assertIsNone(self.dm_core._resolve_item_test_target("health potion", "appraise"))


class TestOpenClose(DMTestCase):
    scenario_name = "dungeon"

    def setUp(self):
        super().setUp()
        self.resolved = self._capture("item_interaction_resolved")

    def _unlock_the_chest(self):
        self.dm_core.roll_dice = lambda dice, pips: 99
        self.dm_core._on_action_detected({"skill": "finesse", "input": "I pick the lock"})

    def _open(self):
        self.dm_core._on_item_interaction_detected({
            "intent": "open", "item_name": None, "input": "I open the chest",
        })

    def _close(self):
        self.dm_core._on_item_interaction_detected({
            "intent": "close", "item_name": None, "input": "I close the chest",
        })

    def test_chest_starts_closed(self):
        self.assertTrue(self.dm_core.is_closed("chest"))

    def test_open_is_blocked_while_locked(self):
        self._open()
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "locked")
        self.assertTrue(self.dm_core.is_closed("chest"))

    def test_open_dismisses_closed_once_unlocked(self):
        self._unlock_the_chest()
        self._open()
        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["container"], "chest")
        self.assertFalse(self.dm_core.is_closed("chest"))

    def test_opening_an_already_open_chest_fails_safe(self):
        self._unlock_the_chest()
        self._open()
        self._open()
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "already_open")

    def test_close_reapplies_the_condition_and_reblocks_contents(self):
        self._unlock_the_chest()
        self._open()
        self._close()
        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertTrue(self.dm_core.is_closed("chest"))

        self.dm_core._on_item_interaction_detected({
            "intent": "examine", "item_name": "cursed dagger", "input": "I examine the cursed dagger",
        })
        self.assertEqual(self.resolved[-1]["reason"], "closed")

    def test_closing_an_already_closed_chest_fails_safe(self):
        self._unlock_the_chest()
        self._close()
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "already_closed")

    def test_open_close_on_a_non_container_fails_safe(self):
        # innkeeper (tavern.toml) is subtype "humanoid", not "container" -- opening/closing
        # a person must fail safely rather than silently applying a nonsensical condition.
        self.dm_core.scenario = {"entities": [{"name": "gladstone", "band": 1}, {"name": "innkeeper", "band": 1}]}
        self.dm_core.load_scenario()

        self._open()

        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_openable")

    def test_open_close_with_no_target_fails_safe(self):
        self.dm_core.scenario = {"entities": [{"name": "gladstone", "band": 1}]}
        self.dm_core.load_scenario()

        self._open()

        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_openable")


class TestGiveAndTrade(DMTestCase):
    # tavern.toml's innkeeper -- a living recipient, unlike the dungeon's chest, so give
    # actually has somewhere sensible to go.
    scenario_name = "tavern"

    def setUp(self):
        super().setUp()
        self.resolved = self._capture("item_interaction_resolved")

    def test_give_moves_an_item_from_the_player_to_the_target(self):
        self.dm_core._on_item_interaction_detected({
            "intent": "give", "item_name": "health potion", "input": "I give the innkeeper a health potion",
        })
        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["container"], "innkeeper")
        self.assertIn("health potion", self.dm_core.entities["innkeeper"]["inventory"])
        self.assertEqual(self.dm_core.entities["gladstone"]["inventory"].count("health potion"), 2)

    def test_give_currency_moves_it_from_the_player_to_the_target(self):
        self.dm_core._on_item_interaction_detected({
            "intent": "give", "item_name": "currency", "input": "I give her some gold",
        })
        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 0)
        self.assertEqual(self.dm_core.entities["innkeeper"]["currency"], 140)

    def test_give_an_item_the_player_does_not_have_reports_not_present(self):
        self.dm_core._on_item_interaction_detected({
            "intent": "give", "item_name": "long bow", "input": "I give her my longbow",
        })
        # gladstone never had a "long bow" at all -- not in inventory or equipped.
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_present")

    def test_give_with_no_target_reports_no_recipient(self):
        self.dm_core.scenario = {"entities": [{"name": "gladstone", "band": 1}]}
        self.dm_core.load_scenario()

        self.dm_core._on_item_interaction_detected({
            "intent": "give", "item_name": "health potion", "input": "I give away a health potion",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "no_recipient")

    def test_trade_charges_the_items_toml_value_and_moves_it_to_the_player(self):
        # dungeon.toml's chest holds "cursed dagger" (value = 5); tavern's innkeeper has
        # neither, so build an ad-hoc scenario reusing the chest as a "shop" for this test.
        self.dm_core.scenario = {"entities": [{"name": "gladstone", "band": 1}, {"name": "chest", "band": 1}]}
        self.dm_core.load_scenario()
        self.dm_core.dismiss_condition("chest", "locked")
        self.dm_core.dismiss_condition("chest", "closed")
        starting_currency = self.dm_core.entities["gladstone"]["currency"]

        self.dm_core._on_item_interaction_detected({
            "intent": "trade", "item_name": "cursed dagger", "input": "I buy the cursed dagger",
        })

        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["price"], 5)
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], starting_currency - 5)
        self.assertEqual(self.dm_core.entities["chest"]["currency"], 25)
        self.assertIn("cursed dagger", self.dm_core.entities["gladstone"]["inventory"])
        self.assertNotIn("cursed dagger", self.dm_core.entities["chest"]["inventory"])

    def test_trade_denied_outright_when_the_player_cant_afford_it(self):
        self.dm_core.scenario = {"entities": [{"name": "gladstone", "band": 1}, {"name": "chest", "band": 1}]}
        self.dm_core.load_scenario()
        self.dm_core.dismiss_condition("chest", "locked")
        self.dm_core.dismiss_condition("chest", "closed")
        self.dm_core.entities["gladstone"]["currency"] = 2  # less than the dagger's value of 5

        self.dm_core._on_item_interaction_detected({
            "intent": "trade", "item_name": "cursed dagger", "input": "I buy the cursed dagger",
        })

        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "cant_afford")
        self.assertEqual(result["price"], 5)
        # Nothing partially transferred -- currency and inventory are both untouched.
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 2)
        self.assertIn("cursed dagger", self.dm_core.entities["chest"]["inventory"])

    def test_trading_for_currency_itself_is_rejected(self):
        self.dm_core._on_item_interaction_detected({
            "intent": "trade", "item_name": "currency", "input": "I trade for some gold",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_takeable")


class TestUseItem(DMTestCase):
    def setUp(self):
        super().setUp()
        self.resolved = self._capture("item_interaction_resolved")

    def _use(self, item_name="health potion", roll_result=6):
        self.dm_core.roll_dice = lambda dice, pips: roll_result
        self.dm_core._on_item_interaction_detected({
            "intent": "use", "item_name": item_name, "input": "I drink the health potion",
        })
        return self.resolved[-1]

    def test_using_heals_and_consumes_exactly_one(self):
        self.dm_core.apply_damage("gladstone", 20)  # 36 -> 16
        starting_count = self.dm_core.entities["gladstone"]["inventory"].count("health potion")

        # roll_dice is stubbed to return roll_result directly (same convention
        # TestItemTargetedSkillCheck's own _check_the_dagger mock already uses), so this is
        # the healing roll's total, not per-die.
        result = self._use(roll_result=6)

        self.assertTrue(result["found"])
        self.assertEqual(result["healed"], 6)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), 22)
        self.assertEqual(result["remaining_hp"], 22)
        self.assertEqual(
            self.dm_core.entities["gladstone"]["inventory"].count("health potion"),
            starting_count - 1,
        )

    def test_using_a_single_use_item_replaces_it_with_its_replace_with(self):
        # gladstone starts with three health potions -- using one should leave exactly two
        # behind, plus one new glass vial, not wipe every potion out.
        starting_count = self.dm_core.entities["gladstone"]["inventory"].count("health potion")

        result = self._use()

        self.assertEqual(result["charges_left"], 0)
        self.assertEqual(result["replaced_with"], "glass vial")
        self.assertEqual(
            self.dm_core.entities["gladstone"]["inventory"].count("health potion"),
            starting_count - 1,
        )
        self.assertIn("glass vial", self.dm_core.entities["gladstone"]["inventory"])

    def test_apply_healing_clamps_at_max_hp_directly(self):
        self.dm_core.apply_damage("gladstone", 1)  # 36 -> 35
        result = self.dm_core.apply_healing("gladstone", 999)
        self.assertEqual(result, 36)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), 36)

    def test_using_identifies_the_item_even_if_never_checked_first(self):
        self.assertFalse(self.dm_core.is_identified("health potion"))
        self._use()
        self.assertTrue(self.dm_core.is_identified("health potion"))

    def test_using_something_not_marked_usable_is_rejected(self):
        result = self._use(item_name="longsword")
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_usable")
        self.assertIn("longsword", self.dm_core.entities["gladstone"]["inventory"])

    def test_using_something_not_actually_carried_is_rejected(self):
        # "cursed dagger" is a real entity, just not in gladstone's own inventory in this scenario.
        result = self._use(item_name="cursed dagger")
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_present")

    def test_using_never_touches_a_scene_target(self):
        # Unlike take/give/trade, use must never reach into current_target's inventory --
        # there's no health potion anywhere near the wolf/thane in this scenario at all, so a
        # match here can only mean it incorrectly looked at the player's own stock regardless
        # of who source_name would normally be.
        starting_target = self.dm_core.current_target
        self._use()
        self.assertEqual(self.dm_core.current_target, starting_target)
        self.assertNotIn("health potion", self.dm_core.entities.get(starting_target, {}).get("inventory", []))

    def test_multi_charge_item_survives_repeated_uses_until_charges_run_out(self):
        # No real multi-charge item exists yet (see the "charges" field's own module note --
        # this is meant to generalize to a future wand), so this exercises _consume_charge
        # directly against a synthetic one rather than waiting for real content to add it.
        self.dm_core.entities["test wand"] = {
            "name": "test wand", "supertype": "object", "subtype": "wand",
            "usable": True, "charges": 2,
        }
        self.dm_core.entities["gladstone"]["inventory"].append("test wand")

        result = self._use(item_name="test wand")
        self.assertEqual(result["charges_left"], 1)
        self.assertIn("test wand", self.dm_core.entities["gladstone"]["inventory"])

        result = self._use(item_name="test wand")
        self.assertEqual(result["charges_left"], 0)
        self.assertNotIn("test wand", self.dm_core.entities["gladstone"]["inventory"])

    def test_using_an_item_with_no_healing_stat_still_succeeds_with_no_effect(self):
        self.dm_core.entities["test wand"] = {
            "name": "test wand", "supertype": "object", "subtype": "wand",
            "usable": True, "charges": 1,
        }
        self.dm_core.entities["gladstone"]["inventory"].append("test wand")

        result = self._use(item_name="test wand")

        self.assertTrue(result["found"])
        self.assertEqual(result["healed"], 0)


class TestInventoryTransfer(DMTestCase):
    scenario_name = "dungeon"

    def test_transfer_currency_moves_the_full_amount_by_default(self):
        moved = self.dm_core.transfer_currency("chest", "gladstone")
        self.assertEqual(moved, 20)
        self.assertEqual(self.dm_core.entities["chest"]["currency"], 0)
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 120)

    def test_transfer_currency_moves_only_the_requested_amount(self):
        moved = self.dm_core.transfer_currency("chest", "gladstone", amount=5)
        self.assertEqual(moved, 5)
        self.assertEqual(self.dm_core.entities["chest"]["currency"], 15)
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 105)

    def test_transfer_currency_clamps_to_whats_available(self):
        moved = self.dm_core.transfer_currency("chest", "gladstone", amount=1000)
        self.assertEqual(moved, 20)
        self.assertEqual(self.dm_core.entities["chest"]["currency"], 0)

    def test_transfer_currency_is_a_noop_for_a_missing_entity(self):
        moved = self.dm_core.transfer_currency("chest", "does_not_exist")
        self.assertEqual(moved, 0)
        self.assertEqual(self.dm_core.entities["chest"]["currency"], 20)  # unchanged

    def test_transfer_item_moves_one_matching_entry(self):
        # gladstone carries three "health potion" entries -- only one should move per call.
        moved = self.dm_core.transfer_item("gladstone", "chest", "health potion")
        self.assertTrue(moved)
        self.assertEqual(self.dm_core.entities["gladstone"]["inventory"].count("health potion"), 2)
        self.assertIn("health potion", self.dm_core.entities["chest"]["inventory"])

    def test_transfer_item_returns_false_when_item_not_present(self):
        moved = self.dm_core.transfer_item("chest", "gladstone", "longsword")
        self.assertFalse(moved)

    def test_loot_entity_moves_currency_and_every_inventory_item(self):
        # Give the chest some items too, not just currency, to exercise the full sweep.
        self.dm_core.entities["chest"]["inventory"] = ["health potion", "health potion"]

        self.dm_core.loot_entity("chest", "gladstone")

        self.assertEqual(self.dm_core.entities["chest"]["currency"], 0)
        self.assertEqual(self.dm_core.entities["chest"].get("inventory"), [])
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 120)
        self.assertEqual(self.dm_core.entities["gladstone"]["inventory"].count("health potion"), 5)


class TestNpcDialogue(DMTestCase):
    # Rules/Fantasy/scenarios/tavern.toml puts the player with a friendly NPC
    # (npcs.toml's innkeeper) instead of the default "arena" combat scenario.
    scenario_name = "tavern"

    def setUp(self):
        super().setUp()
        self.action_events = self._capture("action_resolved")
        self.round_events = self._capture("round_resolved")

    def test_innkeeper_is_not_hostile_toward_the_player(self):
        self.assertFalse(self.dm_core.is_hostile("innkeeper", "gladstone"))

    def test_wolf_is_still_hostile_by_default(self):
        # Wolves carry no explicit attitudes data, so the neutral (0) default should still
        # count as hostile/combat-ready -- this is a regression guard for is_hostile's threshold.
        self.assertTrue(self.dm_core.is_hostile("wolf", "gladstone"))

    def test_talking_to_the_innkeeper_narrates_immediately_as_dialogue(self):
        self.dm_core._on_action_detected({
            "skill": "charisma",
            "input": "I ask the innkeeper if she's heard any news from the road",
        })

        self.assertEqual(len(self.action_events), 1)
        self.assertEqual(self.round_events, [])
        result = self.action_events[0]
        self.assertEqual(result["defender"], "innkeeper")
        self.assertNotIn("round", result)
        self.assertNotIn("damage", result)

    def test_attacking_the_innkeeper_still_resolves_but_does_not_batch_into_a_round(self):
        # Hostility (not weapon possession) gates round batching, so even an attack against a
        # non-hostile NPC narrates immediately rather than waiting for "the round" to end.
        self.dm_core._on_action_detected({
            "skill": "blades",
            "input": "I draw my sword on the innkeeper",
        })

        self.assertEqual(len(self.action_events), 1)
        self.assertEqual(self.round_events, [])

    def test_fighting_a_hostile_target_still_batches_into_round_resolved(self):
        # Sanity check the branch didn't regress combat routing for an actually hostile target.
        self.dm_core.scenario = {
            "entities": [
                { "name": "gladstone", "band": 1 },
                { "name": "wolf", "band": 1 },
            ],
        }
        self.dm_core.load_scenario()

        self.dm_core._on_action_detected({"skill": "blades", "input": "I attack the wolf"})

        self.assertEqual(len(self.round_events), 1)
        self.assertEqual(self.action_events, [])
        self.assertEqual(self.round_events[0]["round"], 1)

    def test_describe_character_includes_descriptive_flavor_fields(self):
        description = self.dm_core.describe_character("innkeeper")
        self.assertIn("stout woman with flour-dusted sleeves", description)
        self.assertIn("Lost her husband to a bandit raid", description)
        self.assertIn("Welcome to the Rusty Tankard", description)

    def test_describe_character_returns_empty_for_pure_mechanics_entity(self):
        # wolf has skills but no description/qualities/memories/quotes -- nothing to narrate.
        self.assertEqual(self.dm_core.describe_character("wolf"), "")

    def test_scenario_loaded_includes_character_roster(self):
        scenario_events = []
        self.event_bus.subscribe("scenario_loaded", scenario_events.append)
        DMCore(self.event_bus, scenario_name="tavern")

        characters = scenario_events[-1]["characters"]
        self.assertTrue(any("innkeeper" in c for c in characters))
        self.assertTrue(any("gladstone" in c for c in characters))

    def test_action_resolved_includes_defender_details_for_a_described_npc(self):
        self.dm_core._on_action_detected({
            "skill": "charisma",
            "input": "I ask the innkeeper about her husband",
        })

        result = self.action_events[0]
        self.assertIn("Lost her husband to a bandit raid", result["defender_details"])


class TestAttitudePhrases(DMTestCase):
    def _tier_name(self, value):
        tier = self.dm_core.get_attitude_tier(value)
        assert tier is not None
        return tier["name"]

    def test_get_attitude_tier_selects_the_right_band(self):
        self.assertEqual(self._tier_name(-150), "hostile")
        self.assertEqual(self._tier_name(-99), "unfriendly")
        self.assertEqual(self._tier_name(-40), "wary")
        self.assertEqual(self._tier_name(0), "neutral")
        self.assertEqual(self._tier_name(40), "warm")
        self.assertEqual(self._tier_name(99), "friendly")
        self.assertEqual(self._tier_name(150), "devoted")

    def test_get_attitude_tier_boundary_values_resolve_to_the_earlier_declared_tier(self):
        # -100 sits on both "hostile" and "unfriendly"'s edge; declaration order (hostile
        # first) breaks the tie, same convention as choose_behavior's first-match-wins.
        self.assertEqual(self._tier_name(-100), "hostile")
        self.assertEqual(self._tier_name(-60), "unfriendly")
        self.assertEqual(self._tier_name(100), "friendly")

    def test_get_attitude_tier_clamps_values_beyond_the_nominal_range(self):
        self.assertEqual(self._tier_name(-500), "hostile")
        self.assertEqual(self._tier_name(500), "devoted")

    def test_get_attitude_tier_returns_none_without_attitude_tier_data(self):
        self.dm_core.rules["attitude_tier"] = []
        self.assertIsNone(self.dm_core.get_attitude_tier(0))

    def test_describe_attitude_mixes_tiers_per_axis(self):
        # gladstone's undead override: disposition/trust/respect/obligation/intimacy = -100
        # (hostile), confidence = 100 -- a genuine mix of extremes in one attitude array.
        self.dm_core.entities["zombie"] = {"name": "zombie", "supertype": "undead"}

        description = self.dm_core.describe_attitude("gladstone", "zombie")

        self.assertIn("Attitude toward zombie:", description)
        self.assertIn("wants them gone, one way or another", description)  # disposition: hostile
        self.assertIn("treats them as an active threat", description)  # trust: hostile
        self.assertIn("feels bold and confident around them", description)  # confidence: friendly (100 boundary)
        self.assertIn("is repulsed by them", description)  # intimacy: hostile

    def test_describe_attitude_at_the_top_of_the_nominal_range(self):
        # gladstone's name override for "anne": all six axes at exactly 100 -- the shared
        # boundary between "friendly" (60..100) and "devoted" (100..150), which resolves to
        # "friendly" since it's declared first (same convention as the hostile/unfriendly case).
        description = self.dm_core.describe_attitude("gladstone", "anne")
        self.assertIn("is genuinely friendly toward them", description)
        self.assertIn("trusts them readily, taking them at their word", description)

    def test_describe_character_surfaces_attitude_for_a_pure_mechanics_entity(self):
        # wolf has no description/qualities/memories/quotes -- describe_character("wolf") with
        # no toward_name still returns "" (regression guard), but passing toward_name gives it
        # something to say after all: its (default-neutral) attitude toward the player.
        self.assertEqual(self.dm_core.describe_character("wolf"), "")

        description = self.dm_core.describe_character("wolf", toward_name="gladstone")
        self.assertIn("wolf -", description)
        self.assertIn("Attitude toward gladstone:", description)
        self.assertIn("feels nothing in particular toward them", description)

    def test_describe_character_skips_attitude_toward_self(self):
        description = self.dm_core.describe_character("wolf", toward_name="wolf")
        self.assertEqual(description, "")

    def test_scenario_roster_now_includes_previously_silent_entities(self):
        # Before attitude phrasing existed, wolf contributed nothing to the roster (its
        # describe_character was ""); now every scenario entity has at least an attitude line.
        scenario_events = []
        self.event_bus.subscribe("scenario_loaded", scenario_events.append)
        DMCore(self.event_bus)  # default "arena" scenario: gladstone, wolf, wolf_2

        characters = scenario_events[-1]["characters"]
        self.assertTrue(any("wolf -" in c and "Attitude toward gladstone" in c for c in characters))

    def test_defender_details_includes_attitude_during_combat(self):
        round_events = []
        self.event_bus.subscribe("round_resolved", round_events.append)

        self.dm_core._on_action_detected({"skill": "blades", "input": "I attack the wolf"})

        self.assertIn("Attitude toward gladstone:", round_events[0]["defender_details"])


class TestSaveLoad(DMTestCase):
    def setUp(self):
        super().setUp()
        self.slot_dirs = []

    def tearDown(self):
        for slot_dir in self.slot_dirs:
            shutil.rmtree(slot_dir, ignore_errors=True)

    def _track(self, slot_name):
        # Registers a slot for cleanup in tearDown and hands back its name, so tests can
        # write real files under Saves/ without leaving test artifacts behind afterward.
        self.slot_dirs.append(self.dm_core._save_slot_dir(slot_name))
        return slot_name

    def _read_dm_state(self, slot_name):
        with open(os.path.join(self.dm_core._save_slot_dir(slot_name), "dm_state.json")) as f:
            return json.load(f)

    def test_save_writes_a_diff_not_a_raw_entity_dump(self):
        # Only the fields anything actually mutates at runtime should be saved -- not a dump
        # of the whole template (ex: no "skills"/"equipped"/"max_hp" keys, which never change
        # post-instancing today).
        slot = self._track("test_save_writes_diff")
        self.dm_core.save_game(slot)
        data = self._read_dm_state(slot)

        self.assertEqual(data["scenario_key"], "arena")
        self.assertEqual(data["player_name"], "gladstone")
        self.assertEqual(data["scenario_entities"], self.dm_core.scenario_entities)
        gladstone_state = data["instances"]["gladstone"]
        self.assertEqual(
            set(gladstone_state.keys()), {"hp", "active_conditions", "currency", "inventory", "band"}
        )

    def test_save_captures_current_instance_state(self):
        slot = self._track("test_save_captures_state")
        self.dm_core.apply_damage("wolf", 10)
        self.dm_core.transfer_currency("gladstone", "wolf", 20)
        self.dm_core.save_game(slot)
        data = self._read_dm_state(slot)

        self.assertEqual(data["instances"]["wolf"]["hp"], 6)
        self.assertEqual(data["instances"]["gladstone"]["currency"], 80)
        self.assertEqual(data["instances"]["wolf"]["currency"], 20)

    def test_load_restores_saved_state_over_further_changes(self):
        slot = self._track("test_load_restores_state")
        self.dm_core.apply_damage("wolf", 10)  # wolf at 6/16
        self.dm_core.save_game(slot)

        self.dm_core.apply_damage("wolf", 6)  # wolf now at 0/16, diverged further from the save
        self.assertEqual(self.dm_core.get_current_hp("wolf"), 0)

        self.dm_core.load_game(slot)

        self.assertEqual(self.dm_core.get_current_hp("wolf"), 6)

    def test_load_restores_current_target_over_the_freshly_computed_default(self):
        # load_scenario() (called from within load_game) resets current_target to its default
        # (the first hostile-and-alive entity) before the saved value is overlaid on top --
        # this proves the saved value actually wins, so resuming a fight keeps targeting
        # whoever was actually being fought rather than snapping back to the default.
        slot = self._track("test_load_restores_current_target")
        self.dm_core.current_target = "wolf_2"
        self.dm_core.save_game(slot)

        self.dm_core.current_target = "wolf"
        self.dm_core.load_game(slot)

        self.assertEqual(self.dm_core.current_target, "wolf_2")

    def test_load_reinstantiates_from_current_templates_not_a_frozen_copy(self):
        # self.entities holds templates and live instances under the same keys -- a
        # single-occurrence instance like "wolf" overwrites self.entities["wolf"] the moment
        # it's first instanced (see CLAUDE.md's "Scenario instancing"). load_game must re-run
        # load_rules to get a genuinely fresh template, not just re-derive from whatever's
        # currently sitting in self.entities (which would be this same mutated instance).
        slot = self._track("test_load_reinstantiates_fresh")
        self.dm_core.save_game(slot)

        self.dm_core.entities["wolf"]["skills"]["brawling"]["dice"] = 999
        self.dm_core.load_game(slot)

        self.assertEqual(self.dm_core.entities["wolf"]["skills"]["brawling"]["dice"], 5)

    def test_load_publishes_game_loaded_not_scenario_loaded(self):
        slot = self._track("test_load_publishes_game_loaded")
        self.dm_core.save_game(slot)

        scenario_events = []
        game_loaded_events = []
        self.event_bus.subscribe("scenario_loaded", scenario_events.append)
        self.event_bus.subscribe("game_loaded", game_loaded_events.append)

        self.dm_core.load_game(slot)

        # Not re-published on load -- LLMCore would otherwise narrate a brand-new opening
        # scene every time a session resumes.
        self.assertEqual(scenario_events, [])
        self.assertEqual(len(game_loaded_events), 1)
        self.assertEqual(game_loaded_events[0]["slot"], slot)
        self.assertEqual(game_loaded_events[0]["name"], "The Arena")

    def test_load_missing_slot_fails_safe_and_publishes_game_load_failed(self):
        errors = []
        failures = []
        self.event_bus.subscribe("log_error", errors.append)
        self.event_bus.subscribe("game_load_failed", failures.append)

        self.dm_core.load_game("does_not_exist_slot")

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["reason"], "not_found")
        self.assertTrue(errors)
        self.assertEqual(self.dm_core.scenario_key, "arena")

    def test_save_requested_and_load_requested_events_are_wired(self):
        # Both NLPCore's text intercept and a GUI/Textual button publish these same events --
        # this is the one code path both triggers converge on.
        slot = self._track("test_events_wired")
        self.event_bus.publish("save_requested", {"slot": slot})
        self.assertTrue(os.path.exists(os.path.join(self.dm_core._save_slot_dir(slot), "dm_state.json")))

        self.dm_core.apply_damage("wolf", 5)
        self.event_bus.publish("load_requested", {"slot": slot})
        self.assertEqual(self.dm_core.get_current_hp("wolf"), 16)

    def test_save_requested_with_no_slot_is_ignored(self):
        warnings = []
        self.event_bus.subscribe("log_warning", warnings.append)

        self.event_bus.publish("save_requested", {})

        self.assertTrue(warnings)

    def test_slot_name_cannot_escape_the_saves_directory(self):
        saves_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Saves")
        slot_dir = self.dm_core._save_slot_dir("../../evil")
        self.assertEqual(os.path.dirname(slot_dir), saves_root)


class TestMultiRoomDungeon(DMTestCase):
    scenario_name = "crypt"

    def setUp(self):
        super().setUp()
        self.action_events = self._capture("action_resolved")
        self.item_events = self._capture("item_interaction_resolved")
        self.round_events = self._capture("round_resolved")

    def _move(self, direction):
        self.dm_core._on_item_interaction_detected(
            {"intent": "move", "item_name": None, "direction": direction, "input": f"go {direction}"}
        )
        return self.item_events[-1]

    def test_a_plain_scenario_has_no_rooms(self):
        # arena/tavern/field/dungeon have no [[room]] tables at all -- self.rooms must stay
        # empty for them, which is what load_scenario/enter_room branch on to know a scenario
        # is the plain, single-room shape rather than a dungeon's room graph.
        plain_dm = DMCore(EventBus(), scenario_name="dungeon")
        self.assertEqual(plain_dm.rooms, {})
        self.assertIsNone(plain_dm.current_room_key)

    def test_crypt_loads_its_room_graph_and_starts_in_the_entrance(self):
        self.assertEqual(
            set(self.dm_core.rooms.keys()),
            {
                "entrance", "hall_of_webs", "guard_chamber", "hidden_alcove",
                "collapsed_passage", "bone_gallery", "sanctum", "boss_chamber",
            },
        )
        self.assertEqual(self.dm_core.current_room_key, "entrance")
        # The player is never repeated in a room's own "entities" list (see
        # DM_Rules.py's _populate_room) -- only listed once, at [scenario].entities.
        self.assertEqual(self.dm_core.scenario_entities, ["gladstone", "dart trap"])
        # A trap is never hostile (same is_hostile short-circuit as any other "object"
        # supertype) -- with nothing hostile in the room, current_target falls back to it,
        # exactly the way the original dungeon.toml's chest already works.
        self.assertEqual(self.dm_core.current_target, "dart trap")

    def test_failed_disarm_damages_the_player_and_arms_blocks_further_attempts(self):
        starting_hp = self.dm_core.get_current_hp("gladstone")
        with patch("random.randint", return_value=1):  # finesse 3d1=3, well under difficulty 9
            self.dm_core._on_action_detected({"skill": "finesse", "input": "I try to disarm the trap"})

        result = self.action_events[-1]
        self.assertFalse(result["success"])
        # Trap's fail damage is 3d (patched to 1 each = 3 raw), reduced by chain mail's own
        # 2d "piercing" armor coverage (also patched to 1 each = 2) -- net 1, not 0, which is
        # exactly why the trap deals 3 dice and not 2 (see the items.toml comment).
        self.assertEqual(result["damage"]["net_damage"], 1)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), starting_hp - 1)
        self.assertIn("triggered", self.dm_core.entities["dart trap"]["active_conditions"])
        self.assertIn("armed", self.dm_core.entities["dart trap"]["active_conditions"])  # fail never dismisses it

        # blocks_if_condition="triggered" -- a repeat attempt must fall through to the normal
        # opposed path (difficulty 0, no HP loss) instead of rolling and re-damaging again.
        hp_after_first_hit = self.dm_core.get_current_hp("gladstone")
        with patch("random.randint", return_value=6):
            self.dm_core._on_action_detected({"skill": "finesse", "input": "I try again"})
        self.assertEqual(self.action_events[-1]["difficulty"], 0)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), hp_after_first_hit)

    def test_successful_disarm_dismisses_armed_with_no_damage(self):
        starting_hp = self.dm_core.get_current_hp("gladstone")
        with patch("random.randint", return_value=6):  # finesse 3d6=18, clears difficulty 9
            self.dm_core._on_action_detected({"skill": "finesse", "input": "I disarm the trap"})

        result = self.action_events[-1]
        self.assertTrue(result["success"])
        self.assertNotIn("damage", result)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), starting_hp)
        self.assertNotIn("armed", self.dm_core.entities["dart trap"]["active_conditions"])

    def test_move_denied_with_no_exit_from_a_plain_scenario(self):
        plain_dm = DMCore(self.event_bus, scenario_name="dungeon")
        self.item_events.clear()
        plain_dm._on_item_interaction_detected({"intent": "move", "item_name": None, "direction": "forward", "input": "go forward"})
        self.assertEqual(self.item_events[-1]["reason"], "no_exit")

    def test_back_denied_with_no_exit_from_the_entrance(self):
        # The entrance room declares no "back" exit at all.
        result = self._move("back")
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "no_exit")
        self.assertEqual(self.dm_core.current_room_key, "entrance")

    def test_forward_denied_from_the_wrong_band(self):
        # The entrance's own "forward" exit is only declared at band 2 -- the player starts
        # at band 1, so it isn't reachable yet (must advance toward the trap first).
        self.assertEqual(self.dm_core.get_band("gladstone"), 1)
        result = self._move("forward")
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "wrong_band")
        self.assertEqual(self.dm_core.current_room_key, "entrance")

    def test_forward_succeeds_once_the_player_reaches_the_exit_band(self):
        with patch("random.randint", return_value=6):
            self.dm_core._on_action_detected({"skill": "finesse", "input": "I disarm the trap"})
        self.dm_core.advance_or_retreat("advance")  # band 1 -> 2, toward the trap/exit
        self.assertEqual(self.dm_core.get_band("gladstone"), 2)

        result = self._move("forward")

        self.assertTrue(result["found"])
        self.assertEqual(result["room_name"], "The Hall of Webs")
        self.assertEqual(self.dm_core.current_room_key, "hall_of_webs")
        self.assertEqual(self.dm_core.scenario_entities, ["gladstone", "giant spider"])
        self.assertEqual(self.dm_core.current_target, "giant spider")
        self.assertEqual(self.dm_core.get_band("gladstone"), 1)  # this exit's own arrival_band

    def test_move_blocked_while_a_hostile_creature_is_still_alive(self):
        self.dm_core.enter_room("hall_of_webs")  # spider present, still alive
        self.item_events.clear()

        result = self._move("forward")

        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "blocked_by_enemies")
        self.assertEqual(self.dm_core.current_room_key, "hall_of_webs")

    def test_move_allowed_once_the_room_is_cleared(self):
        self.dm_core.enter_room("hall_of_webs")
        self.dm_core.apply_damage("giant spider", 999)  # kill it outright
        self.item_events.clear()

        result = self._move("forward")

        self.assertTrue(result["found"])
        self.assertEqual(self.dm_core.current_room_key, "guard_chamber")

    def test_branching_exit_leads_right_to_the_hidden_alcove_not_the_main_path(self):
        # guard_chamber declares two forward-ish exits at two different bands: "right" at
        # band 2 (a real branch, into hidden_alcove) and "forward" at band 3 (the main
        # path, into collapsed_passage) -- this is the case a single forward/back pair per
        # room could never express. Setting the band directly rather than via
        # advance_or_retreat -- guard_chamber's iron chest sits at band 1 (the same band the
        # player arrives at), so "advance" toward it (already gap 0) is correctly a no-op;
        # that's advance_or_retreat's own behavior (see TestMovementAndRange), not what this
        # test is about.
        self.dm_core.enter_room("guard_chamber")
        self.dm_core.entities["gladstone"]["band"] = 2

        result = self._move("right")

        self.assertTrue(result["found"])
        self.assertEqual(self.dm_core.current_room_key, "hidden_alcove")
        self.assertIn("dusty coffer", self.dm_core.scenario_entities)

    def test_branching_exit_forward_from_a_different_band_leads_to_the_main_path(self):
        self.dm_core.enter_room("guard_chamber")
        self.dm_core.entities["gladstone"]["band"] = 3

        result = self._move("forward")

        self.assertTrue(result["found"])
        self.assertEqual(self.dm_core.current_room_key, "collapsed_passage")

    def test_right_exit_not_available_from_band_3_in_guard_chamber(self):
        self.dm_core.enter_room("guard_chamber")
        self.dm_core.entities["gladstone"]["band"] = 3  # "right" only exists at band 2
        self.item_events.clear()

        result = self._move("right")

        self.assertEqual(result["reason"], "wrong_band")

    def test_player_state_carries_over_between_rooms(self):
        # Room transitions must never reset the player's own live instance the way a fresh
        # scenario load intentionally does (see DM_Rules.py's _populate_room) -- HP/currency/
        # inventory earned in one room must still be there in the next.
        self.dm_core.apply_damage("gladstone", 5)
        self.dm_core.entities["gladstone"]["currency"] += 50
        hp_before = self.dm_core.get_current_hp("gladstone")
        currency_before = self.dm_core.entities["gladstone"]["currency"]

        with patch("random.randint", return_value=6):
            self.dm_core._on_action_detected({"skill": "finesse", "input": "I disarm the trap"})
        self.dm_core.advance_or_retreat("advance")
        self._move("forward")

        self.assertEqual(self.dm_core.get_current_hp("gladstone"), hp_before)
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], currency_before)

    def test_revisited_room_keeps_its_state_instead_of_respawning(self):
        # Kill the spider, move on, then come back -- the same dead spider should still be
        # dead, not a freshly-instanced, full-HP one.
        self.dm_core.enter_room("hall_of_webs")
        self.dm_core.apply_damage("giant spider", 999)
        self._move("forward")  # -> guard_chamber

        self._move("back")  # -> back to hall_of_webs

        self.assertEqual(self.dm_core.current_room_key, "hall_of_webs")
        self.assertEqual(self.dm_core.get_current_hp("giant spider"), 0)
        # current_target re-falls-back past the dead spider since nothing else is hostile/alive.
        self.assertNotEqual(self.dm_core.current_target, "giant spider")

    def test_looted_chest_stays_looted_on_a_return_visit(self):
        self.dm_core.enter_room("hall_of_webs")
        self.dm_core.apply_damage("giant spider", 999)
        self._move("forward")  # -> guard_chamber (iron chest)

        with patch("random.randint", return_value=6):  # finesse 3d6=18, clears the chest's lock (10)
            self.dm_core._on_action_detected({"skill": "finesse", "input": "I pick the lock"})
        self.dm_core.transfer_item("iron chest", "gladstone", "health potion")
        self.assertNotIn("health potion", self.dm_core.entities["iron chest"]["inventory"])

        self._move("back")  # -> hall_of_webs
        self.dm_core.enter_room("guard_chamber")  # back again

        self.assertFalse(self.dm_core.is_locked("iron chest"))
        self.assertNotIn("health potion", self.dm_core.entities["iron chest"]["inventory"])

    def test_boss_chamber_has_no_forward_exit(self):
        for room_key in ("hall_of_webs", "guard_chamber", "collapsed_passage", "bone_gallery", "sanctum", "boss_chamber"):
            self.dm_core.enter_room(room_key)
        self.assertEqual(self.dm_core.current_room_key, "boss_chamber")
        self.item_events.clear()

        result = self._move("forward")

        self.assertEqual(result["reason"], "no_exit")

    def test_boss_chooses_its_first_behavior_entry_above_the_hp_cutoff(self):
        self.dm_core.enter_room("boss_chamber")
        # Above 40% of 50 HP -- the first [[entity.behavior]] entry should match.
        behavior = self.dm_core.choose_behavior("the bone warden")
        self.assertEqual(behavior["action"], "bone claw")

    def test_boss_escalates_to_its_second_behavior_entry_once_badly_hurt(self):
        self.dm_core.enter_room("boss_chamber")
        self.dm_core.apply_damage("the bone warden", 40)  # down to 10/50 = 20%, under the 0.4 cutoff

        behavior = self.dm_core.choose_behavior("the bone warden")

        self.assertEqual(behavior["action"], "grave chill")
        # "grave chill" is an inline [[entity.abilities]] entry on "the bone warden" itself
        # (no standalone spells.toml/techniques.toml entity to look up by name), so it has to
        # be resolved via resolve_named_ability, not a bare self.entities lookup.
        ability = self.dm_core.resolve_named_ability("the bone warden", "grave chill")
        self.assertIn("cold", ability.get("damage_tags", []))

    def test_boss_attack_deals_real_damage_when_it_hits(self):
        self.dm_core.enter_room("boss_chamber")
        with patch("random.randint", return_value=6):  # both attacker and defender roll max
            turn = self.dm_core.resolve_behavior_action("the bone warden", "gladstone")
        self.assertEqual(turn["skill"], "brawling")
        self.assertTrue(turn["success"])
        self.assertGreater(turn["damage"]["net_damage"], 0)
        self.assertEqual(turn["damage"]["defender"], "gladstone")


class TestMultiRoomSaveLoad(DMTestCase):
    scenario_name = "crypt"

    def setUp(self):
        super().setUp()
        self.slot_dirs = []

    def tearDown(self):
        for slot_dir in self.slot_dirs:
            shutil.rmtree(slot_dir, ignore_errors=True)

    def _track(self, slot_name):
        self.slot_dirs.append(self.dm_core._save_slot_dir(slot_name))
        return slot_name

    def test_save_load_resumes_in_the_room_it_was_saved_in(self):
        slot = self._track("test_crypt_resume_room")
        self.dm_core.enter_room("hall_of_webs")
        self.dm_core.apply_damage("giant spider", 5)
        self.dm_core.save_game(slot)

        fresh_bus = EventBus()
        fresh_dm = DMCore(fresh_bus, scenario_name="crypt")  # boots back at "entrance"
        fresh_dm.load_game(slot)

        self.assertEqual(fresh_dm.current_room_key, "hall_of_webs")
        self.assertEqual(fresh_dm.scenario_entities, ["gladstone", "giant spider"])
        self.assertEqual(fresh_dm.get_current_hp("giant spider"), 9)

    def test_save_load_preserves_an_earlier_cleared_room_too(self):
        # Not just the room the player is standing in -- a trap disarmed two rooms back must
        # still be disarmed after a resume, not reset to "armed" the way re-instancing purely
        # from the starting room's own fresh templates would leave it.
        slot = self._track("test_crypt_resume_earlier_room")
        with patch("random.randint", return_value=6):
            self.dm_core._on_action_detected({"skill": "finesse", "input": "I disarm the trap"})
        self.assertFalse(self.dm_core.entities["dart trap"].get("active_conditions", {}).get("armed"))
        self.dm_core.enter_room("hall_of_webs")
        self.dm_core.save_game(slot)

        fresh_dm = DMCore(EventBus(), scenario_name="crypt")
        fresh_dm.load_game(slot)

        self.assertEqual(fresh_dm.current_room_key, "hall_of_webs")
        self.assertNotIn("armed", fresh_dm.entities["dart trap"].get("active_conditions", {}))


class TestLLMSaveLoad(LLMTestCase):
    def setUp(self):
        super().setUp()
        self.slot_dirs = []

    def tearDown(self):
        for slot_dir in self.slot_dirs:
            shutil.rmtree(slot_dir, ignore_errors=True)

    def _track(self, slot_name):
        self.slot_dirs.append(self.llm_core._save_slot_dir(slot_name))
        return slot_name

    def test_save_writes_context_window_and_scenario_bookkeeping(self):
        slot = self._track("test_llm_save")
        self.llm_core.context_window = [{"role": "user", "content": "I attack the wolf"}]
        self.llm_core.scenario_name = "The Arena"
        self.llm_core.scenario_description = "A large arena."
        self.llm_core.scenario_characters = ["gladstone - A man"]

        self.llm_core.save_game(slot)

        with open(os.path.join(self.llm_core._save_slot_dir(slot), "llm_state.json")) as f:
            data = json.load(f)
        self.assertEqual(data["context_window"], self.llm_core.context_window)
        self.assertEqual(data["scenario_name"], "The Arena")

    def test_load_restores_context_window_silently_without_a_new_llm_call(self):
        # Resuming a session must not trigger a background fetch / new narration -- that's
        # what would make a resumed save reprint something like a fresh opening scene.
        slot = self._track("test_llm_load_silent")
        self.llm_core.context_window = [{"role": "assistant", "content": "The wolf snarls."}]
        self.llm_core.scenario_name = "The Arena"
        self.llm_core.save_game(slot)

        self.llm_core.context_window = []
        self.llm_core.scenario_name = ""

        with patch("threading.Thread") as mock_thread:
            self.llm_core.load_game(slot)
            mock_thread.assert_not_called()

        self.assertEqual(self.llm_core.context_window, [{"role": "assistant", "content": "The wolf snarls."}])
        self.assertEqual(self.llm_core.scenario_name, "The Arena")

    def test_load_missing_slot_logs_and_leaves_state_unchanged(self):
        errors = []
        self.event_bus.subscribe("log_error", errors.append)
        self.llm_core.context_window = [{"role": "user", "content": "untouched"}]

        self.llm_core.load_game("does_not_exist_slot")

        self.assertTrue(errors)
        self.assertEqual(self.llm_core.context_window, [{"role": "user", "content": "untouched"}])

    def test_game_load_failed_narrates_without_altering_state(self):
        self.llm_core.context_window = [{"role": "user", "content": "existing history"}]

        self.event_bus.publish("game_load_failed", {"slot": "boss-fight", "reason": "not_found"})

        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("boss-fight", prompt)
        self.assertIn("no such", prompt.lower())

    def test_save_requested_and_load_requested_events_are_wired(self):
        slot = self._track("test_llm_events_wired")
        self.llm_core.context_window = [{"role": "user", "content": "before save"}]
        self.event_bus.publish("save_requested", {"slot": slot})

        self.llm_core.context_window = [{"role": "user", "content": "after save, before load"}]
        self.event_bus.publish("load_requested", {"slot": slot})

        self.assertEqual(self.llm_core.context_window, [{"role": "user", "content": "before save"}])


class FakeRagIndex:
    """!
    @brief Duck-typed stand-in for LLM_Rag.RagIndex's query() method, so LLMCore-level tests
        (perform_rag formatting, _build_system_message wiring) don't need a real PDF/model --
        that mechanism is covered on its own by TestRagIndex below.
    """

    def __init__(self, matches):
        self.matches = matches

    def query(self, text, top_k=None, confidence_threshold=None):
        return self.matches


class TestLlmPerformRag(LLMTestCase):
    def test_perform_rag_returns_empty_string_with_no_matches(self):
        self.llm_core.rag_index = FakeRagIndex([])
        self.assertEqual(self.llm_core.perform_rag("what is Brevoy"), "")

    def test_perform_rag_formats_matches_with_source_and_page(self):
        self.llm_core.rag_index = FakeRagIndex([
            ({"source": "Inner Sea World Guide", "page": 23, "text": "Brevoy is a nation of two rival houses."}, 0.57),
            ({"source": "Inner Sea World Guide", "page": 24, "text": "House Orlovsky and House Surtova vie for the throne."}, 0.48),
        ])
        context = self.llm_core.perform_rag("what is Brevoy")
        self.assertIn("Brevoy is a nation of two rival houses.", context)
        self.assertIn("(Inner Sea World Guide p.23)", context)
        self.assertIn("House Orlovsky and House Surtova vie for the throne.", context)

    def test_build_system_message_includes_rag_context_when_present(self):
        self.llm_core.rag_index = FakeRagIndex([
            ({"source": "Inner Sea World Guide", "page": 23, "text": "Brevoy is a nation of two rival houses."}, 0.57),
        ])
        system_message = self.llm_core._build_system_message("The player asks about Brevoy.")
        self.assertIn("Reference lore from the campaign sourcebook", system_message)
        self.assertIn("Brevoy is a nation of two rival houses.", system_message)

    def test_build_system_message_omits_rag_section_with_no_matches(self):
        self.llm_core.rag_index = FakeRagIndex([])
        system_message = self.llm_core._build_system_message("I attack the wolf.")
        self.assertNotIn("Reference lore", system_message)

    def test_queue_narration_never_persists_rag_context_into_context_window(self):
        # Retrieved fresh into the per-request system message each time (see
        # _build_system_message), not stored in context_window -- otherwise every future turn
        # would replay every past turn's lore excerpts too, ballooning the rolling window.
        self.llm_core.rag_index = FakeRagIndex([
            ({"source": "Inner Sea World Guide", "page": 23, "text": "Brevoy is a nation of two rival houses."}, 0.57),
        ])
        self.llm_core._queue_narration("The player asks about Brevoy.")
        stored_prompt = self.llm_core.context_window[-1]["content"]
        self.assertNotIn("Brevoy is a nation of two rival houses", stored_prompt)
        self.assertEqual(stored_prompt, "The player asks about Brevoy.")


class TestRagIndex(unittest.TestCase):
    """!
    @brief Tests LLM_Rag.RagIndex's own mechanics directly -- chunking, caching, and
        nearest-neighbor query ranking -- independent of any real PDF (see
        test_chunking_and_caching_use_no_model_or_pdf) or a real SentenceTransformer model
        where one's actually needed (see setUpClass), never the real, gitignored Settings/
        sourcebook itself: that file may not exist on every machine this suite runs on, and
        even when it does, processing it fully takes minutes (see CLAUDE.md).
    """

    @classmethod
    def setUpClass(cls):
        # Paying SentenceTransformer's ~15-20s load once for the whole class, the same
        # setUpClass pattern TestNlpConfidenceThreshold/TestGameBoot already use.
        cls.index = RagIndex.__new__(RagIndex)
        cls.index.event_bus = EventBus()
        cls.index.model = SentenceTransformer("all-MiniLM-L6-v2")

    def setUp(self):
        # Fresh per test -- these are cheap, in-memory attributes, not the shared model.
        self.index = self.__class__.index
        self.index.top_k = 3
        self.index.confidence_threshold = 0.3
        self.index.chunks = []
        self.index.chunk_embeddings = None
        self.index.ready = False

    def test_chunk_page_text_splits_long_text_into_word_bounded_chunks(self):
        sentence = "The dragon flies over the mountain peak. "
        long_text = sentence * 40  # ~320 words, well past MAX_CHUNK_WORDS (180)
        chunks = self.index._chunk_page_text(long_text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.split()), 180)

    def test_chunk_page_text_drops_fragments_below_the_minimum(self):
        self.assertEqual(self.index._chunk_page_text("Vigil. Castle Firrine."), [])

    def test_chunk_page_text_returns_nothing_for_empty_input(self):
        self.assertEqual(self.index._chunk_page_text(""), [])
        self.assertEqual(self.index._chunk_page_text("   \n  "), [])

    def test_extract_chunks_tags_each_chunk_with_its_source_and_page(self):
        class FakePage:
            def __init__(self, text):
                self._text = text

            def extract_text(self):
                return self._text

        class FakeReader:
            def __init__(self, path):
                filler = "word " * 60
                self.pages = [FakePage(f"Page one lore. {filler}"), FakePage(f"Page two lore. {filler}")]

        with patch("LLM_Rag.PdfReader", FakeReader):
            chunks = self.index._extract_chunks(["scratch/fake_book.pdf"])

        self.assertTrue(all(chunk["source"] == "fake_book" for chunk in chunks))
        self.assertEqual({chunk["page"] for chunk in chunks}, {1, 2})

    def test_cache_round_trips_chunks_and_embeddings(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.index.cache_dir = tmp_dir
            chunks = [{"source": "book", "page": 1, "text": "Some lore text."}]
            embeddings = np.array([[0.1, 0.2, 0.3]], dtype=np.float32)
            self.index._save_cache("fakekey", chunks, embeddings)

            loaded_chunks, loaded_embeddings = self.index._load_cache("fakekey")
            self.assertEqual(loaded_chunks, chunks)
            np.testing.assert_array_almost_equal(loaded_embeddings, embeddings)

    def test_load_cache_returns_none_when_nothing_cached(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.index.cache_dir = tmp_dir
            chunks, embeddings = self.index._load_cache("no-such-key")
            self.assertIsNone(chunks)
            self.assertIsNone(embeddings)

    def test_cache_key_changes_when_a_source_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "book.pdf")
            with open(path, "wb") as f:
                f.write(b"original content")
            key_before = self.index._cache_key([path])

            with open(path, "wb") as f:
                f.write(b"edited content, different size")
            key_after = self.index._cache_key([path])

            self.assertNotEqual(key_before, key_after)

    def test_query_returns_nothing_before_the_index_is_ready(self):
        self.index.ready = False
        self.assertEqual(self.index.query("anything"), [])

    def test_query_ranks_the_closest_chunk_first_and_respects_the_threshold(self):
        chunks = [
            {"source": "book", "page": 1, "text": "Brevoy is a cold northern nation of two rival houses."},
            {"source": "book", "page": 2, "text": "The chef seasons the soup with fresh basil and garlic."},
            {"source": "book", "page": 3, "text": "House Orlovsky and House Surtova both claim Brevoy's throne."},
        ]
        embeddings = self.index.model.encode([c["text"] for c in chunks], convert_to_numpy=True)
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        self.index.chunks = chunks
        self.index.chunk_embeddings = embeddings / norms
        self.index.ready = True

        results = self.index.query("Tell me about the rival houses of Brevoy", top_k=2)

        self.assertGreater(len(results), 0)
        self.assertLessEqual(len(results), 2)
        top_chunk, top_score = results[0]
        self.assertIn(top_chunk["page"], (1, 3))
        for _chunk, score in results:
            self.assertGreaterEqual(score, self.index.confidence_threshold)

    def test_query_returns_nothing_for_an_unrelated_query_above_threshold(self):
        chunks = [
            {"source": "book", "page": 1, "text": "Brevoy is a cold northern nation of two rival houses."},
        ]
        embeddings = self.index.model.encode([c["text"] for c in chunks], convert_to_numpy=True)
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        self.index.chunks = chunks
        self.index.chunk_embeddings = embeddings / norms
        self.index.ready = True

        results = self.index.query("How do I bake a chocolate cake", confidence_threshold=0.6)
        self.assertEqual(results, [])


class TestGUICore(unittest.TestCase):
    """GUI_Core.py's Tkinter surface, exercised directly (no mainloop) -- see Textual_Core.py's
    own tests below for the headless-testable mirror this class doesn't duplicate."""

    def setUp(self):
        self.event_bus = EventBus()
        self.gui = GUICore(self.event_bus)
        self.gui.root.withdraw()  # keep the real window off-screen during tests
        self.slot_dirs = []

    def tearDown(self):
        self.gui.root.destroy()
        for slot_dir in self.slot_dirs:
            shutil.rmtree(slot_dir, ignore_errors=True)

    def _track(self, slot_name):
        self.slot_dirs.append(self.gui._save_slot_dir(slot_name))
        return slot_name

    def test_init_builds_the_expected_tabs_and_subscribes_to_every_event(self):
        tab_texts = [self.gui.notebook.tab(t, "text") for t in self.gui.notebook.tabs()]
        self.assertEqual(tab_texts, ["Party", "Notes", "Map"])
        for event_name in ("llm_response_ready", "rules_loaded", "party_status_changed",
                           "game_saved", "game_loaded", "game_load_failed",
                           "save_requested", "load_requested"):
            self.assertIn(event_name, self.event_bus.subscribers)

    def test_display_llm_response_appends_to_history(self):
        self.event_bus.publish("llm_response_ready", "The wolf snarls.")
        self.assertIn("The wolf snarls.", self.gui.history_text.get("1.0", tk.END))

    def test_submit_input_publishes_echoes_and_clears_the_entry(self):
        submitted = []
        self.event_bus.subscribe("user_input_submitted", submitted.append)
        self.gui.input_entry.insert(0, "attack the wolf")

        self.gui.submit_input()

        self.assertEqual(submitted, ["attack the wolf"])
        self.assertIn("> attack the wolf", self.gui.history_text.get("1.0", tk.END))
        self.assertEqual(self.gui.input_entry.get(), "")

    def test_submit_input_ignores_blank_input(self):
        submitted = []
        self.event_bus.subscribe("user_input_submitted", submitted.append)
        self.gui.input_entry.insert(0, "   ")

        self.gui.submit_input()

        self.assertEqual(submitted, [])

    def test_display_party_status_renders_equipment_abilities_inventory_conditions(self):
        self.event_bus.publish("rules_loaded", {"entities": {
            "gladstone": {
                "is_player": True, "name": "Gladstone", "hp": 30, "max_hp": 36,
                "equipped": {"rhand": "longsword"}, "abilities": ["cleave"],
                "inventory": ["torch", "torch"], "active_conditions": {"wounded": {}},
            },
            "thane": {"is_party": True, "name": "Thane", "hp": 10, "max_hp": 10},
            "wolf": {"name": "wolf", "hp": 10, "max_hp": 10},  # neither player nor party
        }})

        members = self.gui.party_tree.get_children()
        labels = [self.gui.party_tree.item(m, "text") for m in members]
        self.assertEqual(labels, ["Gladstone (HP: 30/36)", "Thane (HP: 10/10)"])

        groups = self.gui.party_tree.get_children(members[0])
        group_texts = [self.gui.party_tree.item(g, "text") for g in groups]
        self.assertEqual(group_texts, ["Equipment", "Abilities", "Inventory", "Conditions"])

        equipment, abilities, inventory, conditions = groups

        def child_texts(node):
            return [self.gui.party_tree.item(c, "text") for c in self.gui.party_tree.get_children(node)]

        self.assertEqual(child_texts(equipment), ["rhand: longsword"])
        self.assertEqual(child_texts(abilities), ["cleave"])
        self.assertEqual(child_texts(inventory), ["torch x2"])
        self.assertEqual(child_texts(conditions), ["wounded"])

    def test_display_party_status_shows_none_placeholders_for_empty_groups(self):
        self.event_bus.publish("rules_loaded", {"entities": {
            "gladstone": {"is_player": True, "name": "Gladstone", "hp": 36, "max_hp": 36},
        }})
        member = self.gui.party_tree.get_children()[0]
        for group in self.gui.party_tree.get_children(member):
            children = self.gui.party_tree.get_children(group)
            self.assertEqual([self.gui.party_tree.item(c, "text") for c in children], ["(none)"])

    def test_display_party_status_redraws_instead_of_appending_on_repeat_events(self):
        # "party_status_changed" fires after every action -- the tree must be rebuilt each
        # time, not grow a duplicate node per event (see DM_Core.py's _publish_party_status).
        payload = {"entities": {"gladstone": {"is_player": True, "name": "Gladstone", "hp": 36, "max_hp": 36}}}
        self.event_bus.publish("rules_loaded", payload)
        self.event_bus.publish("party_status_changed", payload)
        self.assertEqual(len(self.gui.party_tree.get_children()), 1)

    def test_save_load_status_events_append_to_history(self):
        self.event_bus.publish("game_saved", {"slot": "run1"})
        self.event_bus.publish("game_loaded", {"slot": "run1"})
        self.event_bus.publish("game_load_failed", {"slot": "missing"})

        history = self.gui.history_text.get("1.0", tk.END)
        self.assertIn("Game saved as 'run1'", history)
        self.assertIn("Game loaded from 'run1'", history)
        self.assertIn("No save named 'missing' found", history)

    def test_map_drawing_creates_a_line_in_the_chosen_color_and_clear_removes_it(self):
        self.gui.set_map_pen_color("red")

        Event = type("Event", (), {})
        start, move, end = Event(), Event(), Event()
        start.x, start.y = 10, 10
        move.x, move.y = 20, 20
        end.x, end.y = 20, 20
        self.gui._on_map_draw_start(start)
        self.gui._on_map_draw_move(move)
        self.gui._on_map_draw_end(end)

        lines = self.gui.map_canvas.find_all()
        self.assertEqual(len(lines), 1)
        self.assertEqual(self.gui.map_canvas.itemcget(lines[0], "fill"), "red")
        self.assertIsNone(self.gui._map_last_point)

        self.gui.clear_map()
        self.assertEqual(self.gui.map_canvas.find_all(), ())

    def test_save_game_then_load_game_round_trips_the_notes_tab(self):
        slot = self._track("test_gui_notes_roundtrip")
        self.gui.notes_text.delete("1.0", tk.END)
        self.gui.notes_text.insert(tk.END, "remember the side passage")
        self.gui.save_game(slot)

        self.gui.notes_text.delete("1.0", tk.END)
        self.gui.load_game(slot)

        self.assertEqual(self.gui.notes_text.get("1.0", "end-1c"), "remember the side passage")

    def test_load_game_missing_slot_logs_and_leaves_notes_untouched(self):
        self.gui.display_notes("still here")
        errors = []
        self.event_bus.subscribe("log_error", errors.append)

        self.gui.load_game("no_such_slot_at_all")

        self.assertEqual(self.gui.notes_text.get("1.0", "end-1c"), "still here")
        self.assertTrue(errors)

    def test_save_requested_and_load_requested_events_are_wired(self):
        # The same events NLPCore's save/load text intercept publishes -- GUICore must react
        # to them regardless of whether the File menu or a typed command triggered them.
        slot = self._track("test_gui_event_wiring")
        self.gui.notes_text.delete("1.0", tk.END)
        self.gui.notes_text.insert(tk.END, "wired via events")

        self.event_bus.publish("save_requested", {"slot": slot})
        self.gui.notes_text.delete("1.0", tk.END)
        self.event_bus.publish("load_requested", {"slot": slot})

        self.assertEqual(self.gui.notes_text.get("1.0", "end-1c"), "wired via events")

    def test_save_requested_with_no_slot_is_ignored(self):
        slots_before = self.gui._list_save_slots()

        self.gui._on_save_requested({})
        self.gui._on_load_requested({})

        self.assertEqual(self.gui._list_save_slots(), slots_before)

    @patch("GUI_Core.simpledialog.askstring", return_value="  my slot  ")
    def test_request_save_publishes_save_requested_with_the_trimmed_typed_name(self, mock_askstring):
        self._track("my slot")
        save_events = []
        self.event_bus.subscribe("save_requested", save_events.append)

        self.gui.request_save()

        self.assertEqual(save_events, [{"slot": "my slot"}])

    @patch("GUI_Core.simpledialog.askstring", return_value=None)
    def test_request_save_does_nothing_when_the_popup_is_cancelled(self, mock_askstring):
        save_events = []
        self.event_bus.subscribe("save_requested", save_events.append)

        self.gui.request_save()

        self.assertEqual(save_events, [])

    @patch.object(GUICore, "_list_save_slots", return_value=[])
    def test_request_load_with_no_saved_games_shows_a_message_instead_of_a_list(self, mock_slots):
        self.gui.request_load()

        popup = next(w for w in self.gui.root.winfo_children() if isinstance(w, tk.Toplevel))
        labels = [w for w in popup.winfo_children() if isinstance(w, tk.Label)]
        self.assertEqual(labels[0].cget("text"), "No saved games found.")
        popup.destroy()

    @patch.object(GUICore, "_list_save_slots", return_value=["run1", "run2"])
    def test_request_load_lists_slots_and_publishes_load_requested_for_the_selection(self, mock_slots):
        load_events = []
        self.event_bus.subscribe("load_requested", load_events.append)

        self.gui.request_load()

        picker = next(w for w in self.gui.root.winfo_children() if isinstance(w, tk.Toplevel))
        listbox = next(w for w in picker.winfo_children() if isinstance(w, tk.Listbox))
        self.assertEqual(listbox.get(0, tk.END), ("run1", "run2"))

        listbox.selection_clear(0, tk.END)
        listbox.selection_set(1)
        button_row = next(w for w in picker.winfo_children() if isinstance(w, tk.Frame))
        load_button = next(
            w for w in button_row.winfo_children()
            if isinstance(w, tk.Button) and w.cget("text") == "Load"
        )

        load_button.invoke()

        self.assertEqual(load_events, [{"slot": "run2"}])
        self.assertFalse(picker.winfo_exists())


def lines_of(app, widget_id):
    return [str(line) for line in app.query_one(f"#{widget_id}", RichLog).lines]


@pytest.mark.asyncio
async def test_user_input_and_llm_response_mirror_into_history():
    event_bus = EventBus()
    app = TextualCore(event_bus)

    async with app.run_test() as pilot:
        await pilot.pause()
        event_bus.publish("user_input_submitted", "I attack the wolf")
        event_bus.publish("llm_response_ready", "The wolf dodges your blow.")
        await pilot.pause()

        history = lines_of(app, "history")
        assert any("> I attack the wolf" in line for line in history)
        assert any("The wolf dodges your blow." in line for line in history)


@pytest.mark.asyncio
async def test_events_published_before_mount_are_buffered_then_flushed():
    event_bus = EventBus()
    app = TextualCore(event_bus)

    # Mirrors DMCore publishing rules_loaded synchronously during __init__, which in
    # LLDM.py happens before the GUI's event loop (Tkinter's or Textual's) is running.
    event_bus.publish("rules_loaded", {
        "skills": {"blades": {"name": "blades"}},
        "entities": {"gladstone": {"name": "gladstone"}},
    })

    async with app.run_test() as pilot:
        await pilot.pause()
        debug_log = lines_of(app, "debug_log")
        assert any("blades" in line for line in debug_log)
        assert any("gladstone" in line for line in debug_log)


@pytest.mark.asyncio
async def test_background_thread_publish_is_thread_safe():
    # LLMCore publishes llm_response_ready from a background fetch thread, not the app's
    # own thread, so this exercises call_safely's cross-thread path via call_from_thread.
    event_bus = EventBus()
    app = TextualCore(event_bus)

    async with app.run_test() as pilot:
        await pilot.pause()

        def from_background_thread():
            event_bus.publish("llm_response_ready", "Narration from a background thread.")

        thread = threading.Thread(target=from_background_thread)
        thread.start()
        await asyncio.to_thread(thread.join)
        await pilot.pause()

        assert any("Narration from a background thread." in line for line in lines_of(app, "history"))


@pytest.mark.asyncio
async def test_typing_and_pressing_enter_publishes_and_echoes_input():
    event_bus = EventBus()
    app = TextualCore(event_bus)
    received = []
    event_bus.subscribe("user_input_submitted", received.append)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#input_box")
        keys = ["space" if c == " " else c for c in "attack the wolf"]
        await pilot.press(*keys)
        await pilot.press("enter")
        await pilot.pause()

        assert received == ["attack the wolf"]
        assert any("> attack the wolf" in line for line in lines_of(app, "history"))
        # The input field clears after submitting, mirroring GUICore.submit_input.
        assert app.query_one("#input_box", Input).value == ""


@pytest.mark.asyncio
async def test_inactive_tab_content_requires_activation_to_read_lines():
    # Gotcha: a RichLog's .lines reflects width-wrapped content, and a widget inside a
    # non-active TabPane has no render width, so .lines reads empty until that tab is
    # switched to - even though the write already happened. Tests reading a background
    # tab's log must activate it first, as this test demonstrates.
    event_bus = EventBus()
    app = TextualCore(event_bus)

    async with app.run_test() as pilot:
        await pilot.pause()
        event_bus.publish("log_info", "a log line")
        await pilot.pause()

        assert lines_of(app, "event_log") == []

        app.query_one(TabbedContent).active = "event_log_tab"
        await pilot.pause()

        assert any("a log line" in line for line in lines_of(app, "event_log"))


@pytest.mark.asyncio
async def test_save_button_publishes_save_requested_with_slot_name():
    event_bus = EventBus()
    app = TextualCore(event_bus)
    received = []
    event_bus.subscribe("save_requested", received.append)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#slot_input")
        await pilot.press(*"arenarun1")
        # Focus + Enter rather than pilot.click("#save_button") -- the button's on-screen
        # offset can land right at the edge of the default test terminal size and raise
        # OutOfBounds, which focus-and-activate sidesteps entirely.
        app.query_one("#save_button", Button).focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert received == [{"slot": "arenarun1"}]


@pytest.mark.asyncio
async def test_load_button_publishes_load_requested_with_slot_name():
    event_bus = EventBus()
    app = TextualCore(event_bus)
    received = []
    event_bus.subscribe("load_requested", received.append)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#slot_input")
        await pilot.press(*"myslot")
        app.query_one("#load_button", Button).focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert received == [{"slot": "myslot"}]


@pytest.mark.asyncio
async def test_save_load_buttons_ignore_a_blank_slot_name():
    event_bus = EventBus()
    app = TextualCore(event_bus)
    save_events = []
    load_events = []
    event_bus.subscribe("save_requested", save_events.append)
    event_bus.subscribe("load_requested", load_events.append)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#save_button", Button).focus()
        await pilot.pause()
        await pilot.press("enter")
        app.query_one("#load_button", Button).focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert save_events == []
        assert load_events == []


@pytest.mark.asyncio
async def test_game_saved_loaded_and_load_failed_mirror_into_history():
    event_bus = EventBus()
    app = TextualCore(event_bus)

    async with app.run_test() as pilot:
        await pilot.pause()
        event_bus.publish("game_saved", {"slot": "arena-run-1"})
        event_bus.publish("game_loaded", {"slot": "arena-run-1"})
        event_bus.publish("game_load_failed", {"slot": "no-such-slot", "reason": "not_found"})
        await pilot.pause()

        history = lines_of(app, "history")
        assert any("Game saved as 'arena-run-1'" in line for line in history)
        assert any("Game loaded from 'arena-run-1'" in line for line in history)
        assert any("No save named 'no-such-slot' found" in line for line in history)


if __name__ == "__main__":
    unittest.main()
