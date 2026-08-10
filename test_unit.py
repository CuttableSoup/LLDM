import asyncio
import json
import os
import shutil
import tempfile
import threading
import tkinter as tk
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from sentence_transformers import SentenceTransformer

from Character_Creation import (
    build_character_skills,
    get_race,
    load_character_creation_data,
    race_baseline_skills,
    validate_allocation,
)
from Character_Creation_GUI import CharacterCreationDialog
from Challenge_Rating import calculate_challenge_rating, calculate_party_challenge_rating, skill_rating
from DM_Core import DMCore
from DM_Rules import list_available_scenarios
from Event_Bus import EventBus
from GUI_Core import GUICore
import LLDM
from LLM_Core import LLMCore
from LLM_Rag import RagIndex
from NLP_Core import NLPCore
from NPC_Generation import (
    _describe_qualities,
    fit_skills_to_cr,
    generate_npc_stats,
    load_npc_keywords,
    resolve_varied_value,
)
from Textual_Core import TextualCore
from textual.widgets import Button, RichLog


def _new_tk_root_with_retry(attempts=3, delay=0.5):
    """!
    @brief Constructs a real tk.Tk() root, retrying on TclError -- creating a Tk() root is,
        on this environment, an occasionally-flaky operation independent of how many other
        Tk() roots have been created in this process (observed even with only one Tk() root
        created in an entire test run, so it isn't purely a cumulative-churn issue reducing
        Tk() creations elsewhere already helps with, just a residual one worth retrying
        directly). Every TestCase class that needs a real Tk() root for setUpClass should
        call this instead of tk.Tk() directly, so a single transient failure doesn't error
        out an entire test class at once.
    @param attempts How many times to try before giving up and letting the last TclError raise.
    @param delay Seconds to wait between attempts.
    @return A real, constructed tk.Tk() instance.
    """
    import time
    for attempt in range(attempts):
        try:
            return tk.Tk()
        except tk.TclError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


class TestEventBus(unittest.TestCase):
    """!
    @brief Event_Bus.py's publish/subscribe dispatch, including the one subtlety worth its
        own regression test: publish() must dispatch over a snapshot of the subscriber list,
        not the live one, so a handler that itself calls subscribe() for the same event_type
        it's currently handling (ex: LLDM.py's cold-start "load_requested" handler
        constructing a fresh DMCore, whose own __init__ subscribes _on_load_requested) doesn't
        also have that brand-new callback invoked within this same publish -- it should only
        ever fire starting from the *next* publish call.
    """

    def test_publish_calls_every_subscriber_with_the_message(self):
        bus = EventBus()
        received = []
        bus.subscribe("ping", received.append)
        bus.subscribe("ping", received.append)
        bus.publish("ping", "hello")
        self.assertEqual(received, ["hello", "hello"])


    def test_a_handler_subscribing_mid_dispatch_is_not_invoked_until_the_next_publish(self):
        bus = EventBus()
        calls = []

        def late_subscriber(message):
            calls.append(("late", message))

        def subscribes_another_handler(message):
            calls.append(("first", message))
            bus.subscribe("event", late_subscriber)

        bus.subscribe("event", subscribes_another_handler)
        bus.publish("event", 1)
        self.assertEqual(calls, [("first", 1)])  # late_subscriber must not fire yet

        bus.publish("event", 2)
        self.assertEqual(calls, [("first", 1), ("first", 2), ("late", 2)])


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
        last_action = detected_actions[-1]["actions"][0]
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
        action = detected_actions[0]["actions"][0]
        self.assertEqual(action["skill"], "blades")
        self.assertGreaterEqual(action["score"], self.nlp_core.confidence_threshold)

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
        action = detected_actions[0]["actions"][0]
        self.assertEqual(action["skill"], "appraise")
        self.assertLess(action["score"], self.nlp_core.confidence_threshold)
        self.assertGreaterEqual(action["score"], self.nlp_core.keyword_fallback_floor)


    def test_detect_item_intent_examine_vs_take_vs_neither(self):
        self.assertEqual(self.nlp_core._detect_item_intent("examine the dagger"), "examine")
        self.assertEqual(self.nlp_core._detect_item_intent("take the gold"), "take")
        self.assertIsNone(self.nlp_core._detect_item_intent("attack with my sword"))


    def test_detect_dialogue_intent_vs_item_and_skill_phrasing(self):
        self.assertTrue(self.nlp_core._detect_dialogue_intent("talk to the innkeeper"))
        self.assertTrue(self.nlp_core._detect_dialogue_intent("ask the guard about the road"))
        self.assertFalse(self.nlp_core._detect_dialogue_intent("take the gold"))
        self.assertFalse(self.nlp_core._detect_dialogue_intent("attack with my sword"))

    def test_dialogue_phrase_publishes_dialogue_detected_not_action_detected(self):
        # Full pipeline: a real "talk to" phrase should never reach map_to_action at all --
        # item-interaction detection is checked first and finds nothing here, so this is what
        # actually proves dialogue detection is wired into _on_user_input, not just callable
        # in isolation.
        dialogue_events = []
        action_events = []
        self.event_bus.subscribe("dialogue_detected", dialogue_events.append)
        self.event_bus.subscribe("action_detected", action_events.append)

        self.event_bus.publish("user_input_submitted", "I want to talk to the wolf")

        self.assertEqual(len(dialogue_events), 1)
        self.assertEqual(action_events, [])
        self.assertIn("wolf", dialogue_events[0]["input"])


    def test_split_action_clauses_on_and_then_and_punctuation(self):
        self.assertEqual(
            self.nlp_core._split_action_clauses("attack the orc and cast a ward"),
            ["attack the orc", "cast a ward"],
        )
        self.assertEqual(
            self.nlp_core._split_action_clauses("attack and then retreat"),
            ["attack", "retreat"],
        )
        self.assertEqual(
            self.nlp_core._split_action_clauses("attack with my sword"),
            ["attack with my sword"],
        )
        # \b-anchored -- "and"/"then" appearing inside another word must never split
        # (ex: "handle"/"sandbox" both literally contain the substring "and").
        self.assertEqual(
            self.nlp_core._split_action_clauses("handle the sandbox carefully"),
            ["handle the sandbox carefully"],
        )

    def test_multi_clause_input_publishes_multiple_actions(self):
        # Full pipeline: NLPCore's own conjunction-aware split, not just the pure method above.
        detected_actions = []
        self.event_bus.subscribe("action_detected", detected_actions.append)

        self.event_bus.publish("user_input_submitted", "I attack with my sword and pick the lock")

        self.assertEqual(len(detected_actions), 1)
        skills = [action["skill"] for action in detected_actions[0]["actions"]]
        self.assertEqual(skills, ["blades", "finesse"])

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


class TestFreeformDialogueNarration(LLMTestCase):
    """!
    @brief LLMCore's own side of DM_Dialogue.py's channel: generate_npc_dialogue, and the
        presence-tagging/filtering machinery every _queue_narration/_queue_dialogue call now
        threads through (see _filter_present_history). Exercised directly against
        "dialogue_resolved" payloads -- no DMCore involved -- the same "prompt shape, not the
        LLM's actual reply" scope TestClarificationResponse already keeps to.
    """

    def test_found_dialogue_queues_a_first_person_prompt_via_the_dialogue_path(self):
        self.event_bus.publish("dialogue_resolved", {
            "target": "innkeeper", "input": "have you heard anything from the road",
            "found": True, "persona": "innkeeper - A weary tavern keeper.",
            "attitude": "Attitude toward gladstone: is warm and well-disposed toward them.",
            "present_entities": ["gladstone", "innkeeper"],
        })

        entry = self.llm_core.context_window[-1]
        self.assertIn("have you heard anything from the road", entry["content"])
        self.assertEqual(entry["present"], ["gladstone", "innkeeper"])

    def test_not_found_dialogue_falls_back_to_ordinary_gm_narration(self):
        self.event_bus.publish("dialogue_resolved", {
            "target": None, "input": "hello?", "found": False, "reason": "no_one_here",
            "present_entities": ["gladstone"],
        })

        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("no one here to talk to", prompt)

    def test_filter_present_history_excludes_entries_the_entity_never_witnessed(self):
        self.llm_core.context_window = [
            {"role": "user", "content": "entrance room narration", "present": ["gladstone", "dart trap"]},
            {"role": "assistant", "content": "...", "present": ["gladstone", "dart trap"]},
            {"role": "user", "content": "hall of webs narration", "present": ["gladstone", "giant spider"]},
        ]

        spider_history = self.llm_core._filter_present_history("giant spider")
        trap_history = self.llm_core._filter_present_history("dart trap")

        self.assertEqual([e["content"] for e in spider_history], ["hall of webs narration"])
        self.assertEqual(
            [e["content"] for e in trap_history], ["entrance room narration", "..."],
        )

    def test_untagged_entries_are_excluded_from_every_filtered_view(self):
        # A clarification/load-failed prompt (no DMCore scenario_entities to tag it with --
        # see _queue_narration's own present_entities docstring) must never leak into a
        # specific NPC's own witnessed history just because it's untagged.
        self.llm_core.context_window = [{"role": "user", "content": "no one understood that"}]

        self.assertEqual(self.llm_core._filter_present_history("innkeeper"), [])

    def test_api_messages_strips_the_present_bookkeeping_tag(self):
        entries = [{"role": "user", "content": "hi", "present": ["gladstone"]}]
        self.assertEqual(self.llm_core._api_messages(entries), [{"role": "user", "content": "hi"}])


class TestMultiActionNarration(LLMTestCase):
    """!
    @brief _describe_player_actions -- the West End Games multi-action penalty's own narration
        side (see DM_Core.py's own _on_action_detected docstring). A single-action turn
        describes exactly like before this mechanic existed; a multi-action turn also names
        the shared penalty so the model's narration reads as one character splitting their
        attention, not several independent attacks.
    """

    def test_single_action_has_no_penalty_line(self):
        result = {"actions": [{"skill": "blades", "roll": 15, "difficulty": 10, "success": True}]}
        description = self.llm_core._describe_player_actions(result)
        self.assertNotIn("splitting their attention", description)
        self.assertIn("Skill used: blades", description)

    def test_two_actions_name_the_shared_penalty_and_describe_both(self):
        result = {"actions": [
            {"skill": "blades", "roll": 12, "difficulty": 10, "success": True},
            {"skill": "finesse", "roll": 9, "difficulty": 12, "success": False},
        ]}
        description = self.llm_core._describe_player_actions(result)
        self.assertIn("2 actions this turn", description)
        self.assertIn("-1D", description)
        self.assertIn("Skill used: blades", description)
        self.assertIn("Skill used: finesse", description)

    def test_three_actions_name_minus_2d(self):
        result = {"actions": [
            {"skill": "blades", "roll": 9, "difficulty": 10, "success": False},
            {"skill": "finesse", "roll": 9, "difficulty": 12, "success": False},
            {"skill": "charisma", "roll": 9, "difficulty": 10, "success": False},
        ]}
        description = self.llm_core._describe_player_actions(result)
        self.assertIn("3 actions this turn", description)
        self.assertIn("-2D", description)


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


class TestDamageCalculation(DMTestCase):
    def test_bonus_resolves_flat_number(self):
        self.assertEqual(self.dm_core.resolve_bonus("gladstone", 5), 5)


    @patch("random.randint", return_value=4)
    def test_damage_value_rolls_dice_and_adds_bonus(self, mock_randint):
        # 2 dice @ 4 each + 1 pip + strength_damage bonus (1) = 10
        total = self.dm_core.resolve_damage_value(
            "gladstone", {"dice": 2, "pips": 1, "bonus": "user.strength_damage"}
        )
        self.assertEqual(total, 10)


    def test_apply_damage_subtracts_and_floors_at_zero(self):
        self.dm_core.apply_damage("gladstone", 10)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), 26)
        self.dm_core.apply_damage("gladstone", 1000)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), 0)


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


    @patch("random.randint", return_value=4)
    def test_immunity_overrides_vulnerability_when_both_tags_present(self, mock_randint):
        # An attack tagged both "fire" (immune) and "water" (vulnerable) should still be fully
        # negated -- immunity is an absolute block that wins outright, not just a bigger number
        # in the same tug-of-war as resistance/vulnerability.
        hybrid_attack = {"damage_value": {"dice": 4, "pips": 0, "bonus": 0}, "damage_tags": ["fire", "water"]}
        result = self.dm_core.calculate_damage("gladstone", "fire elemental", hybrid_attack)

        self.assertEqual(result["vulnerability_bonus"], 0)
        self.assertEqual(result["net_damage"], 0)


class TestMultipleActions(DMTestCase):
    """!
    @brief The West End Games D6 "multiple actions" rule (see DM_Core.py's own
        _on_action_detected docstring): every action beyond the first attempted in one turn
        costs every one of that turn's actions a cumulative -1D, and however many actions the
        player attempts, exactly one round resolves -- never one round per action.
    """

    def test_resolve_action_dice_penalty_reduces_the_pool_not_the_pips(self):
        with patch("random.randint", return_value=3):
            full = self.dm_core.resolve_action("gladstone", "blades")  # 5D+0
            penalized = self.dm_core.resolve_action("gladstone", "blades", dice_penalty=2)  # 3D+0

        self.assertEqual(full["roll"], 15)
        self.assertEqual(penalized["roll"], 9)

    def test_resolve_action_dice_penalty_floors_at_zero_dice(self):
        with patch("random.randint", return_value=3):
            result = self.dm_core.resolve_action("gladstone", "charisma", dice_penalty=99)  # 2D+0

        self.assertEqual(result["roll"], 0)

    def test_resolve_opposed_action_penalty_never_touches_the_defenders_roll(self):
        self.dm_core.entities["test_defender"] = {
            "name": "test_defender", "skills": {"dodge": {"dice": 6, "pips": 0}},
        }
        with patch("random.randint", return_value=3):
            unpenalized = self.dm_core.resolve_opposed_action("gladstone", "blades", "test_defender")
            penalized = self.dm_core.resolve_opposed_action(
                "gladstone", "blades", "test_defender", dice_penalty=2,
            )

        # The defender's own dodge roll (6D @ 3 = 18) is identical either way -- only the
        # attacker's own roll (5D vs 3D @ 3 each) is reduced by the penalty.
        self.assertEqual(unpenalized["difficulty"], 18)
        self.assertEqual(penalized["difficulty"], 18)
        self.assertEqual(unpenalized["roll"], 15)
        self.assertEqual(penalized["roll"], 9)

    def test_two_actions_in_one_turn_each_roll_at_minus_1d_and_resolve_as_one_round(self):
        # A no-skills target auto-succeeds (difficulty 0) with no opposing roll to muddy the
        # numbers -- isolates the penalty itself, same "practice_dummy" pattern TestCombatLoop
        # already uses.
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self.dm_core.scenario = {"entities": [{"name": "gladstone", "band": 1}, {"name": "practice_dummy", "band": 1}]}
        self.dm_core.load_scenario()
        round_events = self._capture("round_resolved")
        starting_round = self.dm_core.round_number

        with patch("random.randint", return_value=3):
            self.dm_core._on_action_detected({
                "actions": [{"skill": "blades"}, {"skill": "blades"}],
                "input": "I attack the practice dummy and attack it again",
            })

        # Not two rounds -- one, no matter how many actions the player attempted this turn.
        self.assertEqual(len(round_events), 1)
        self.assertEqual(self.dm_core.round_number, starting_round + 1)
        actions = round_events[0]["actions"]
        self.assertEqual(len(actions), 2)
        # blades is 5D+0 -- at -1D (two actions this turn) each rolls 4D @ 3 = 12.
        self.assertEqual(actions[0]["roll"], 12)
        self.assertEqual(actions[1]["roll"], 12)

    def test_three_actions_apply_minus_2d(self):
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self.dm_core.scenario = {"entities": [{"name": "gladstone", "band": 1}, {"name": "practice_dummy", "band": 1}]}
        self.dm_core.load_scenario()
        round_events = self._capture("round_resolved")

        with patch("random.randint", return_value=3):
            self.dm_core._on_action_detected({
                "actions": [{"skill": "blades"}, {"skill": "blades"}, {"skill": "blades"}],
                "input": "I attack it, attack it again, and attack it once more",
            })

        # blades is 5D+0 -- at -2D (three actions this turn) each rolls 3D @ 3 = 9.
        for action in round_events[0]["actions"]:
            self.assertEqual(action["roll"], 9)

    def test_item_test_only_turn_never_triggers_a_round_even_if_current_target_is_hostile(self):
        # Regression: self.current_target ("wolf", hostile from scenario load) must not leak
        # into the round-trigger decision when every action this turn was actually an item
        # test, which never touches self.current_target at all (see _on_action_detected's own
        # "engaged_combat_target" note) -- an early version of this batching mistakenly
        # checked self.current_target's hostility unconditionally, turning "appraise a potion"
        # into a combat round just because a hostile wolf happened to already be the player's
        # standing target from scenario load.
        action_events = self._capture("action_resolved")
        round_events = self._capture("round_resolved")
        self.assertTrue(self.dm_core.is_hostile(self.dm_core.current_target, self.dm_core.player_name))

        self.dm_core.roll_dice = lambda dice, pips: 99
        self.dm_core._on_action_detected({
            "actions": [{"skill": "appraise", "target": "health potion"}],
            "input": "I appraise the health potion",
        })

        self.assertEqual(round_events, [])
        self.assertEqual(len(action_events), 1)

    def test_mixed_item_test_and_attack_batch_shares_the_penalty_and_still_one_round(self):
        # Diceless item-*interactions* (give/take/equip/...) never count toward this at all --
        # they're a wholly separate event/pipeline. An item *test* does roll dice, though, so
        # it shares the turn's penalty just like an opposed attack does.
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self.dm_core.scenario = {"entities": [{"name": "gladstone", "band": 1}, {"name": "practice_dummy", "band": 1}]}
        self.dm_core.load_scenario()
        round_events = self._capture("round_resolved")

        with patch("random.randint", return_value=3):
            self.dm_core._on_action_detected({
                "actions": [
                    {"skill": "appraise", "target": "health potion"},
                    {"skill": "blades"},
                ],
                "input": "I appraise the potion and attack the dummy",
            })

        self.assertEqual(len(round_events), 1)
        actions = round_events[0]["actions"]
        self.assertEqual(len(actions), 2)
        # appraise is 4D+0 -- at -1D (two actions this turn) rolls 3D @ 3 = 9, clearing the
        # health potion's own test difficulty (4).
        self.assertEqual(actions[0]["roll"], 9)
        self.assertTrue(actions[0]["success"])
        # blades is 5D+0 -- at -1D rolls 4D @ 3 = 12.
        self.assertEqual(actions[1]["roll"], 12)


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


    def test_select_ability_skill_picks_best_rated_option_from_a_skill_list(self):
        # cleave's skill is ["blades", "axes"]; gladstone has "blades" (5 dice) and no "axes"
        # entry at all, so "blades" must be the one selected.
        cleave = self.dm_core.entities["cleave"]
        self.assertEqual(self.dm_core.select_ability_skill("gladstone", cleave), "blades")


    def test_missed_attack_does_not_apply_damage(self):
        # wolf's dodge (6 dice) will always beat gladstone's blades (2 dice) at this fixed roll.
        with patch("random.randint", return_value=1):
            self.dm_core._on_action_detected({"actions": [{"skill": "blades"}], "input": "I attack with my sword"})

        result = self.resolved[-1]
        action = result["actions"][0]
        self.assertFalse(action["success"])
        self.assertNotIn("damage", action)
        self.assertEqual(result["round"], 1)
        self.assertEqual(self.dm_core.get_current_hp("wolf"), 16)

    def test_successful_attack_applies_damage_to_the_target(self):
        # Give the player an opponent with no matching opposing skill, so the attack auto-succeeds (difficulty 0).
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self.dm_core.scenario = {"entities": [{"name": "practice_dummy", "band": 1}]}
        self.dm_core.load_scenario()

        with patch("random.randint", return_value=3):
            self.dm_core._on_action_detected({"actions": [{"skill": "blades"}], "input": "I attack with my sword"})

        result = self.resolved[-1]
        action = result["actions"][0]
        self.assertTrue(action["success"])
        self.assertIn("damage", action)
        self.assertEqual(action["damage"]["defender"], "practice_dummy")
        self.assertGreater(action["damage"]["net_damage"], 0)
        self.assertEqual(
            self.dm_core.get_current_hp("practice_dummy"),
            20 - action["damage"]["net_damage"],
        )


class TestMovementAndRange(DMTestCase):
    def setUp(self):
        super().setUp()  # arena: bands=4, enclosed=true, everyone starts band 1
        self.resolved = self._capture_any("round_resolved", "action_resolved")


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


    # --- move_toward_or_away: the creature/ally counterpart to advance_or_retreat ----------


    # --- is_in_range -------------------------------------------------------------------


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

        self.dm_core._on_action_detected({"actions": [{"skill": "blades"}], "input": "I attack with my sword"})

        result = self.resolved[-1]
        action = result["actions"][0]
        self.assertFalse(action["success"])
        self.assertEqual(action["reason"], "out_of_range")
        self.assertIsNone(action["roll"])
        self.assertNotIn("damage", action)


    # --- _on_item_interaction_detected("advance"/"retreat") -------------------------------


    # --- save/load persists band ------------------------------------------------------------


class TestEntityBehavior(DMTestCase):
    def setUp(self):
        super().setUp()
        self.resolved = self._capture("round_resolved")

    def test_choose_behavior_matches_while_the_entity_is_alive(self):
        # creatures.toml's wolf: a single behavior, "always bite while hp_per_remain >= 0.01".
        behavior = self.dm_core.choose_behavior("wolf")
        assert behavior is not None
        self.assertEqual(behavior["action"], "bite")


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


    def test_roll_initiative_pools_dodge_and_untrained_observation(self):
        # wolf has dodge 6D/0 pips and no observation skill at all -- rules.toml's [[initiative]]
        # still pools it in at the same untrained 0D/0 pips resolve_action defaults missing
        # skills to, so the pool is just dodge's own 6D (observation contributes nothing).
        with patch("random.randint", return_value=4):
            initiative = self.dm_core.roll_initiative("wolf")
        self.assertEqual(initiative, 24)


    def test_current_target_advances_to_next_hostile_when_current_dies(self):
        self.dm_core.apply_damage("wolf", 999)
        with patch("random.randint", return_value=1):
            self.dm_core._on_action_detected({"actions": [{"skill": "athletics"}], "input": "I reposition"})
        self.assertEqual(self.dm_core.current_target, "wolf_2")


class TestBandit(DMTestCase):
    def setUp(self):
        super().setUp()
        self.dm_core.scenario = {
            "bands": 8, "enclosed": False,
            "entities": [{"name": "gladstone", "band": 1}, {"name": "bandit", "band": 5}],
        }
        self.dm_core.load_scenario()


    def test_favors_the_bow_at_a_distance(self):
        # Starting gap is 4 -- exactly the short bow's own range, so it's both "not adjacent"
        # (distance_to_target > 0, the behavior's own requirement) and actually reachable.
        behavior = self.dm_core.choose_behavior("bandit", "gladstone")
        self.assertEqual(behavior["action"], "short bow")

        turn = self.dm_core.resolve_behavior_action("bandit", "gladstone")
        self.assertEqual(turn["skill"], "missiles")
        self.assertNotIn("movement", turn)


class TestStatusEvaluation(DMTestCase):
    def test_hp_per_remain_requirement_matches_current_percentage(self):
        # gladstone: max_hp 36. At 18 hp (50%) the "wounded" status (0.40-0.59) should match.
        self.dm_core.apply_damage("gladstone", 18)
        matched_names = [s["name"] for s in self.dm_core.get_applicable_statuses("gladstone", "on_damage")]
        self.assertIn("wounded", matched_names)
        self.assertNotIn("severe", matched_names)


    def test_apply_damage_auto_applies_matching_condition(self):
        self.dm_core.apply_damage("gladstone", 18)  # -> 50% hp -> "wounded"
        self.assertIn("wounded", self.dm_core.entities["gladstone"]["active_conditions"])


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


class TestCharacterCreation(unittest.TestCase):
    """!
    @brief Character_Creation.py's pure race/point-buy logic -- no DMCore, no GUI, just the
        data + math (see Character_Creation.py's own module docstring for why it's
        independent of DMCore in the first place).
    """

    @classmethod
    def setUpClass(cls):
        cls.skills, cls.races, cls.character_creation = load_character_creation_data()


    def test_human_is_defined_as_2d_in_every_skill(self):
        # No implicit "base_dice" fallback -- human's own [race.skill_dice] table explicitly
        # lists every skill at 2D, same as any other race would list its own values.
        human = get_race(self.races, "human")
        self.assertEqual(set(human["skill_dice"].keys()), set(self.skills.keys()))
        baseline = race_baseline_skills(self.skills, human)
        self.assertTrue(all(dice == 2 for dice in baseline.values()))


    def test_validate_allocation_rejects_over_the_per_skill_cap(self):
        allocation = {"arcane": 6, "stealth": 9}
        ok, reason = validate_allocation(self.skills, None, self.character_creation, allocation)
        self.assertFalse(ok)
        self.assertIn("arcane", reason)


    def test_build_character_skills_adds_allocation_onto_baseline(self):
        allocation = {"arcane": 5, "stealth": 5, "observation": 5}
        skills = build_character_skills(self.skills, get_race(self.races, "elf"), allocation)
        self.assertEqual(skills["arcane"], {"dice": 8, "pips": 0})  # 3 baseline + 5 allocated
        self.assertEqual(skills["strength"], {"dice": 1, "pips": 0})  # untouched, elf's own override
        self.assertEqual(skills["blades"], {"dice": 2, "pips": 0})  # untouched, elf's own override


class TestChallengeRating(unittest.TestCase):
    """!
    @brief Challenge_Rating.py's pure "how powerful is this entity" math -- no DMCore, same
        independence Character_Creation.py's own TestCharacterCreation exercises above.
    """

    def test_skill_rating_converts_pips_to_the_shared_pip_scale(self):
        self.assertEqual(skill_rating(dice=5, pips=0), 15)
        self.assertEqual(skill_rating(dice=2, pips=2), 8)
        self.assertEqual(skill_rating(dice=0, pips=0), 0)

    def test_calculate_challenge_rating_averages_only_the_top_n_skills(self):
        # Six skills at 2D (rating 6) plus one standout at 5D (rating 15) -- top_n=3 should
        # average the standout with two of the 2D skills (15+6+6)/3=9, not get diluted by
        # every other 2D skill the entity also happens to have trained.
        skills = {f"skill_{i}": {"dice": 2, "pips": 0} for i in range(6)}
        skills["standout"] = {"dice": 5, "pips": 0}
        rating = calculate_challenge_rating(skills, max_hp=0, top_n=3)
        self.assertEqual(rating, 9)

    def test_calculate_challenge_rating_sums_skill_hp_and_damage_components(self):
        # gladstone's own numbers (characters.toml/items.toml/spells.toml): top-3 skill
        # ratings blades 5D=15, dodge 5D=15, then one of the 4D=12 skills; max_hp=36; best
        # damage is fireball's 5D=15 (beats the longsword's 1D+2=5 and cleave's weapon-scaled
        # 1D+2=5) -- skill (15+15+12)/3=14, hp 36//3=12, damage 15 -> 41.
        skills = {"blades": {"dice": 5, "pips": 0}, "dodge": {"dice": 5, "pips": 0},
                  "appraise": {"dice": 4, "pips": 0}}
        rating = calculate_challenge_rating(skills, max_hp=36, damage_dice=5, damage_pips=0)
        self.assertEqual(rating, 41)

    def test_calculate_challenge_rating_handles_an_entity_with_no_trained_skills(self):
        self.assertEqual(calculate_challenge_rating({}, max_hp=9, damage_dice=1, damage_pips=1), 7)

    def test_calculate_party_challenge_rating_is_the_sum_not_the_average(self):
        self.assertEqual(calculate_party_challenge_rating([41, 26, 21]), 88)
        self.assertEqual(calculate_party_challenge_rating([]), 0)


class TestChallengeRatingDMCoreIntegration(DMTestCase):
    """!
    @brief get_challenge_rating/get_party_challenge_rating (DM_Combat.py) against arena.toml's
        real gladstone/thane/wolf data -- confirms the DMCore-side glue (finding each entity's
        best damage-dealing weapon/ability, filtering the party by is_player/is_party) feeds
        Challenge_Rating.py's pure math the right numbers, not just that the math itself is
        right (TestChallengeRating already covers that in isolation).
    """

    def test_gladstone_rating_picks_fireball_over_his_own_longsword_as_the_best_damage(self):
        # skill (blades 5D=15, dodge 5D=15, one 4D skill=12) -> 14; hp 36//3=12; fireball's
        # 5D=15 beats the longsword's 1D+2=5 and cleave's weapon-scaled 1D+2=5 -- 14+12+15=41.
        self.assertEqual(self.dm_core.get_challenge_rating("gladstone"), 41)

    def test_thane_rating_uses_his_own_innate_shortsword_strike(self):
        # skill (three tied 4D skills=12 each) -> 12; hp 24//3=8; shortsword strike 2D=6.
        self.assertEqual(self.dm_core.get_challenge_rating("thane"), 26)

    def test_wolf_rating_uses_its_own_bite(self):
        # skill (dodge 6D=18, brawling 5D=15, one 2D skill=6) -> 13; hp 16//3=5; bite 1D=3.
        self.assertEqual(self.dm_core.get_challenge_rating("wolf"), 21)

    def test_unknown_entity_rates_zero(self):
        self.assertEqual(self.dm_core.get_challenge_rating("nobody"), 0)

    def test_party_rating_sums_gladstone_and_thane_but_not_the_wolves(self):
        self.assertEqual(self.dm_core.get_party_challenge_rating(), 41 + 26)


class TestNpcGeneration(unittest.TestCase):
    """!
    @brief NPC_Generation.py's pure math/parsing -- no DMCore, no live LLM (see
        TestNpcGenerationDMCoreIntegration for the DMCore-side glue). generate_npc_stats'
        own dependency-injection seam (call_chat_completion) is exercised directly here
        rather than through DMCore.
    """

    def test_fit_skills_to_cr_round_trips_through_calculate_challenge_rating(self):
        # Every case should land *exactly* on target_cr -- fit_skills_to_cr is meant to be an
        # exact inverse of calculate_challenge_rating's own math, not just "close".
        cases = [
            (20, ["blades", "dodge", "athletics"]),       # exactly 3 key skills
            (41, ["arcane", "linguistics"]),               # fewer than 3
            (10, ["stealth"]),                              # just 1
            (60, ["blades", "dodge", "athletics", "strength", "brawling"]),  # more than 3
        ]
        for target_cr, key_skills in cases:
            skills, max_hp = fit_skills_to_cr(key_skills, target_cr)
            self.assertEqual(
                calculate_challenge_rating(skills, max_hp), target_cr,
                f"key_skills={key_skills} target_cr={target_cr}",
            )

    def test_fit_skills_to_cr_never_produces_negative_or_zero_dice(self):
        skills, max_hp = fit_skills_to_cr(["blades", "dodge"], target_cr=1)
        self.assertGreaterEqual(max_hp, 0)
        for stats in skills.values():
            self.assertGreaterEqual(stats["dice"], 1)

    def test_fit_skills_to_cr_dedupes_and_only_the_first_three_affect_cr(self):
        skills, max_hp = fit_skills_to_cr(
            ["blades", "blades", "dodge", "athletics", "strength"], target_cr=30,
        )
        self.assertEqual(len(skills), 4)  # deduped from 5 to 4
        self.assertEqual(calculate_challenge_rating(skills, max_hp), 30)

        primary_ratings = [skill_rating(skills[n]["dice"], skills[n]["pips"]) for n in ("blades", "dodge", "athletics")]
        flavor_rating = skill_rating(skills["strength"]["dice"], skills["strength"]["pips"])
        self.assertEqual(len(set(primary_ratings)), 1)  # all three tied
        self.assertLess(flavor_rating, primary_ratings[0])  # the 4th is CR-free flavor only

    def test_fit_skills_to_cr_empty_key_skills_still_produces_hp(self):
        skills, max_hp = fit_skills_to_cr([], target_cr=30)
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

    def test_generate_npc_stats_uses_the_injected_call_chat_completion(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {"arguments": json.dumps({
                "name": "Test Name", "backstory": "A test backstory.", "keywords": ["warrior"],
            })}}]}}]}

        npc_keywords = {"warrior": ["blades", "axes", "athletics", "strength"]}
        result = generate_npc_stats(npc_keywords, target_cr=20, variance=0, call_chat_completion=fake_call)

        self.assertEqual(result["name"], "Test Name")
        self.assertEqual(result["description"], "A test backstory.")
        self.assertEqual(set(result["skills"]), set(npc_keywords["warrior"]))

    def test_generate_npc_stats_falls_back_when_call_chat_completion_raises(self):
        def failing_call(*args, **kwargs):
            raise ConnectionError("no LM Studio")

        npc_keywords = {"warrior": ["blades", "axes", "athletics", "strength"]}
        result = generate_npc_stats(npc_keywords, target_cr=20, call_chat_completion=failing_call)

        self.assertEqual(result["name"], "Unnamed Stranger")
        self.assertTrue(result["skills"])

    def test_generate_npc_stats_falls_back_when_llm_names_an_unrecognized_keyword(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {"arguments": json.dumps({
                "name": "X", "backstory": "Y", "keywords": ["not_a_real_keyword"],
            })}}]}}]}

        npc_keywords = {"warrior": ["blades"]}
        result = generate_npc_stats(npc_keywords, target_cr=20, call_chat_completion=fake_call)

        self.assertEqual(result["name"], "Unnamed Stranger")

    def test_generate_npc_stats_skip_llm_generation_never_calls_the_injected_callable(self):
        def exploding_call(*args, **kwargs):
            raise AssertionError("should never be called when skip_llm_generation is True")

        npc_keywords = {"warrior": ["blades", "axes", "athletics", "strength"]}
        result = generate_npc_stats(
            npc_keywords, target_cr=20, call_chat_completion=exploding_call, skip_llm_generation=True,
        )
        self.assertTrue(result["skills"])


class TestNpcGenerationDMCoreIntegration(DMTestCase):
    """!
    @brief _instance_entities' entity_template branch (DM_Rules.py/DM_NpcGeneration.py)
        against npc_generation_test.toml's own "generated_stranger" (templates.toml) -- no live
        LLM, NPC_Generation._real_call_chat_completion is patched with a deterministic fake
        so this stays part of the fast offline suite (see test_integration.py for a real
        end-to-end LM Studio round trip).
    """
    scenario_name = "npc_generation_test"

    def setUp(self):
        self.fake_call_log = []
        self.fake_call_prompts = []

        def fake_call(api_url, messages, tools=None, tool_choice=None):
            self.fake_call_log.append(1)
            self.fake_call_prompts.append(messages[-1]["content"])
            return {"choices": [{"message": {"tool_calls": [{"function": {"arguments": json.dumps({
                "name": f"Generated NPC {len(self.fake_call_log)}",
                "backstory": "A backstory from the fake LLM.",
                "keywords": ["warrior"],
            })}}]}}]}

        self._fake_call = fake_call
        with patch("NPC_Generation._real_call_chat_completion", new=fake_call):
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
        # generated_stranger's own currency/qualities/attitudes mix fixed and varied fields
        # (templates.toml) -- every one of them should come out a plain scalar, never a
        # leftover {"min", "max"} dict or a weighted-choice list.
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
        disposition, trust, confidence, respect, obligation, intimacy = default
        self.assertTrue(-40 <= disposition <= 40)
        self.assertEqual(trust, 0)
        self.assertEqual(confidence, 0)
        self.assertEqual(respect, 10)
        self.assertEqual(obligation, -20)
        self.assertTrue(-40 <= intimacy <= 40)

    def test_player_attitude_token_is_substituted_with_the_live_player_name(self):
        # templates.toml authors this override toward the literal token "player" -- it must
        # resolve to whichever entity is actually is_player = true (gladstone), not stay
        # keyed to a string no live entity is ever named.
        name_overrides = self.dm_core.entities["generated_stranger"]["attitudes"]["name"]
        self.assertEqual(len(name_overrides), 1)
        override = name_overrides[0]
        self.assertIn(self.dm_core.player_name, override)
        self.assertNotIn("player", override)
        self.assertEqual(override[self.dm_core.player_name], [40, 0, 0, 0, 0, 0])

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
        # module default, since npcs.toml's own template doesn't override it), so it should
        # land in a generous but bounded band around gladstone's own real CR, not some
        # unrelated fixed number.
        npc_cr = self.dm_core.get_challenge_rating("generated_stranger")
        player_cr = self.dm_core.get_challenge_rating(self.dm_core.player_name)
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
        def different_fake_call(api_url, messages, tools=None, tool_choice=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {"arguments": json.dumps({
                "name": "A Completely Different NPC", "backstory": "Different.", "keywords": ["scholar"],
            })}}]}}]}

        with patch("NPC_Generation._real_call_chat_completion", new=different_fake_call):
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
        # A fresh "arena" boot -- never references "generated_stranger" itself, so unlike
        # self.dm_core (this class's own npc_generation_test fixture, which already has a
        # *live*, generated "generated_stranger" instance sitting in self.entities under that
        # same key -- self.entities holds templates and live instances under the same keys,
        # see CLAUDE.md's "Scenarios and rooms"), self.entities here has no "generated_stranger"
        # at all -- only self.entity_templates does, proving the lookup itself is what's
        # isolated, not just that this particular scenario never happens to collide.
        with patch("NPC_Generation._real_call_chat_completion", new=self._fake_call):
            dm = DMCore(EventBus(), scenario_name="arena")
        self.assertIn("generated_stranger", dm.entity_templates)
        self.assertNotIn("generated_stranger", dm.entities)

        errors = []
        dm.event_bus.subscribe("log_error", errors.append)

        # A scenario entry naming an entity_template via "name" (the field real entities use)
        # must fail the same "unknown entity" way a real typo would, not silently resolve it.
        result = dm._instance_entities([{"name": "generated_stranger", "band": 1}])
        self.assertEqual(result, [])
        self.assertTrue(any("unknown entity" in e for e in errors))

        # Conversely, a real entity/creature template can't be pulled through "template"
        # either -- self.entity_templates has no "wolf" entry to find.
        errors.clear()
        result = dm._instance_entities([{"template": "wolf", "band": 1}])
        self.assertEqual(result, [])
        self.assertTrue(any("unknown entity template" in e for e in errors))


class TestCharacterCreationDMCoreIntegration(DMTestCase):

    def test_valid_character_creation_replaces_the_players_own_skills(self):
        character = {
            "race": "elf",
            "allocation": {"arcane": 5, "stealth": 5, "observation": 5},
        }
        dm = DMCore(EventBus(), scenario_name="arena", character=character)
        self.assertEqual(dm.entities["gladstone"]["skills"]["arcane"], {"dice": 8, "pips": 0})
        self.assertEqual(dm.entities["gladstone"]["skills"]["strength"], {"dice": 1, "pips": 0})
        # A skill the character sheet never touches still exists, at the elf's own baseline --
        # not still carrying gladstone's own hand-authored value from characters.toml.
        self.assertEqual(dm.entities["gladstone"]["skills"]["blades"], {"dice": 2, "pips": 0})
        self.assertEqual(dm.entities["gladstone"]["qualities"]["race"], "elf")


class TestCharacterCreationRename(unittest.TestCase):
    """!
    @brief apply_character_creation's own optional "name" override (Character_Creation_GUI.py's
        name field) plus the generic "player" scenario placeholder (DM_Rules.py's
        PLAYER_PLACEHOLDER/_instance_entities) that lets a renamed character actually appear
        in a scenario without the scenario itself needing to know that name. Uses
        Rules/Fantasy/scenarios/character_test.toml -- a minimal scenario built solely to
        exercise this mechanism, not one of the "real" gameplay scenarios (arena/tavern/
        field/dungeon/crypt), so neither can drift out of sync with the other.
    """


    def test_named_character_is_renamed_and_resolved_by_the_player_placeholder(self):
        character = {
            "race": "elf",
            "allocation": {"arcane": 5, "stealth": 5, "observation": 5},
            "name": "Aria",
        }
        dm = DMCore(EventBus(), scenario_name="character_test", character=character)

        self.assertEqual(dm.player_name, "Aria")
        self.assertNotIn("gladstone", dm.entities)  # re-keyed away, not left behind
        self.assertEqual(dm.entities["Aria"]["name"], "Aria")
        self.assertEqual(dm.entities["Aria"]["skills"]["arcane"], {"dice": 8, "pips": 0})
        # The scenario's own "player" placeholder followed the rename into the live instance.
        self.assertIn("Aria", dm.scenario_entities)
        self.assertNotIn("gladstone", dm.scenario_entities)
        self.assertNotIn("player", dm.scenario_entities)


    def test_renaming_to_an_existing_entitys_name_is_rejected_but_skills_still_apply(self):
        errors = []
        bus = EventBus()
        bus.subscribe("log_error", errors.append)
        character = {
            "race": "elf",
            "allocation": {"arcane": 5, "stealth": 5, "observation": 5},
            "name": "wolf",  # collides with creatures.toml's own "wolf" template
        }

        dm = DMCore(bus, scenario_name="character_test", character=character)

        self.assertEqual(dm.player_name, "gladstone")  # rename rejected
        self.assertEqual(dm.entities["gladstone"]["skills"]["arcane"], {"dice": 8, "pips": 0})
        self.assertEqual(dm.entities["wolf"]["supertype"], "creature")  # untouched, not clobbered
        self.assertTrue(any("rename rejected" in message for message in errors))

    def test_name_only_character_renames_without_touching_skills(self):
        # LLDM.py's CLI quick-boot path (a scenario + a bare character name, no interactive
        # point-buy) passes exactly this shape -- {"name": ...} with no "race"/"allocation" at
        # all -- so the skill/race override step must be skippable independently of the rename.
        dm = DMCore(EventBus(), scenario_name="character_test", character={"name": "Aria"})

        self.assertEqual(dm.player_name, "Aria")
        self.assertNotIn("gladstone", dm.entities)
        # Untouched -- characters.toml's own hand-authored value, not race_baseline_skills'.
        self.assertEqual(dm.entities["Aria"]["skills"]["blades"], {"dice": 5, "pips": 0})


class TestPeekSavedScenarioKey(unittest.TestCase):
    """!
    @brief LLDM.py's _peek_saved_scenario_key -- reads a save slot's own "scenario_key"
        without needing a live DMCore, so main()'s cold-start "Load..." handler (Character
        menu, no game active yet) knows which scenario to construct a brand new DMCore
        against before DMCore.load_game() itself has anything to run against.
    """

    def setUp(self):
        self.slot_dirs = []

    def tearDown(self):
        for slot_dir in self.slot_dirs:
            shutil.rmtree(slot_dir, ignore_errors=True)

    def _write_slot(self, slot_name, data):
        base_dir = os.path.dirname(os.path.abspath(LLDM.__file__))
        slot_dir = os.path.join(base_dir, "Saves", slot_name)
        self.slot_dirs.append(slot_dir)
        os.makedirs(slot_dir, exist_ok=True)
        with open(os.path.join(slot_dir, "dm_state.json"), "w") as f:
            json.dump(data, f)
        return slot_name

    def test_reads_the_slots_own_scenario_key(self):
        slot = self._write_slot("test_peek_scenario_key", {"scenario_key": "crypt"})
        self.assertEqual(LLDM._peek_saved_scenario_key(slot, "arena"), ("crypt", "Fantasy"))

    def test_reads_the_slots_own_setting(self):
        slot = self._write_slot(
            "test_peek_scenario_key_setting", {"scenario_key": "rooftop", "setting": "Zombie"},
        )
        self.assertEqual(LLDM._peek_saved_scenario_key(slot, "arena"), ("rooftop", "Zombie"))


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


    def test_failed_pick_leaves_it_locked_and_applies_jammed_on_fail(self):
        with patch("random.randint", return_value=1):  # 3 dice @ 1 = 3, well under test difficulty 12
            self.dm_core._on_action_detected({"actions": [{"skill": "finesse"}], "input": "I pick the lock"})

        self.assertTrue(self.dm_core.is_locked("chest"))
        self.assertEqual(self.round_events, [])
        result = self.action_events[-1]["actions"][0]
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
            self.dm_core._on_action_detected({"actions": [{"skill": "finesse"}], "input": "I pick the lock"})

        self.assertFalse(self.dm_core.is_locked("chest"))
        self.assertNotIn("jammed", self.dm_core.entities["chest"]["active_conditions"])
        result = self.action_events[-1]["actions"][0]
        self.assertTrue(result["success"])
        self.assertEqual(result["defender"], "chest")
        self.assertNotIn("loot", result)

        self.assertEqual(self.dm_core.entities["chest"]["currency"], 20)
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], starting_currency)
        self.assertIn("cursed dagger", self.dm_core.entities["chest"]["inventory"])


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
        self.dm_core._on_action_detected({"actions": [{"skill": "finesse"}], "input": "I pick the lock"})

    def _open_the_chest(self):
        # Unlocking and opening are independent conditions -- picking the lock only dismisses
        # "locked"; reaching the chest's *contents* also requires "closed" to be dismissed.
        self.dm_core._on_item_interaction_detected({
            "intent": "open", "item_name": None, "input": "I open the chest",
        })


    def test_examine_surfaces_revealed_tags_once_identified(self):
        self._unlock_the_chest()
        self._open_the_chest()
        self.dm_core.apply_condition("cursed dagger", "identified", duration="permanent", dismiss="")

        self.dm_core._on_item_interaction_detected({
            "intent": "examine", "item_name": "cursed dagger", "input": "I examine the cursed dagger",
        })

        result = self.resolved[-1]
        self.assertEqual(result["revealed"], ["cursed"])


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
            "actions": [{"skill": "arcane", "target": "cursed dagger"}],
            "input": "I check the dagger for curses",
        })


    def test_wrong_skill_does_not_match_the_items_test(self):
        # "blades" isn't in the dagger's test.skill (["arcane"]) -- not a test target at all,
        # same as any other skill against an entity whose test doesn't list it.
        self.assertIsNone(self.dm_core._resolve_item_test_target("cursed dagger", "blades"))

    def test_successful_check_reveals_tags_and_marks_identified(self):
        self._check_the_dagger(roll_result=8)  # clears the dagger's own test difficulty (8)

        self.assertEqual(self.round_events, [])  # inspecting an item is never combat
        result = self.action_events[-1]["actions"][0]
        self.assertTrue(result["success"])
        self.assertEqual(result["defender"], "cursed dagger")
        self.assertIsNone(result["opposing_skill"])
        self.assertEqual(result["revealed"], ["cursed"])
        self.assertTrue(self.dm_core.is_identified("cursed dagger"))


class TestHealthPotionIdentify(DMTestCase):
    def setUp(self):
        super().setUp()
        self.action_events = self._capture("action_resolved")

    def _check_the_potion(self, skill_name, roll_result):
        self.dm_core.roll_dice = lambda dice, pips: roll_result
        self.dm_core._on_action_detected({
            "actions": [{"skill": skill_name, "target": "health potion"}],
            "input": "I appraise the health potion",
        })


    def test_successful_check_reveals_healing_and_marks_identified(self):
        self._check_the_potion("appraise", roll_result=4)  # clears difficulty 4
        result = self.action_events[-1]["actions"][0]
        self.assertTrue(result["success"])
        self.assertEqual(result["revealed"], ["healing"])
        self.assertTrue(self.dm_core.is_identified("health potion"))


class TestOpenClose(DMTestCase):
    scenario_name = "dungeon"

    def setUp(self):
        super().setUp()
        self.resolved = self._capture("item_interaction_resolved")

    def _unlock_the_chest(self):
        self.dm_core.roll_dice = lambda dice, pips: 99
        self.dm_core._on_action_detected({"actions": [{"skill": "finesse"}], "input": "I pick the lock"})

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


class TestEquipUnequipDrop(DMTestCase):
    def setUp(self):
        super().setUp()
        self.resolved = self._capture("item_interaction_resolved")

    def _interact(self, intent, item_name):
        self.dm_core._on_item_interaction_detected({
            "intent": intent, "item_name": item_name, "input": f"I {intent} the {item_name}",
        })
        return self.resolved[-1]


    def test_equip_moves_item_into_its_declared_slot(self):
        self.dm_core.unequip_item("gladstone", "longsword")

        result = self._interact("equip", "longsword")

        self.assertTrue(result["found"])
        self.assertEqual(result["slot"], "rhand")
        self.assertIsNone(result["replaced"])
        self.assertEqual(self.dm_core.entities["gladstone"]["equipped"]["rhand"], "longsword")
        # Still in inventory too -- equipping never removes it from there.
        self.assertIn("longsword", self.dm_core.entities["gladstone"]["inventory"])

    def test_equip_displaces_whatever_was_already_in_that_slot(self):
        # gladstone's rhand already holds the longsword (characters.toml) -- equipping a
        # second rhand/lhand weapon should bump it, not refuse the action.
        self.dm_core.entities["gladstone"]["inventory"].append("rusty shortsword")

        result = self._interact("equip", "rusty shortsword")

        self.assertTrue(result["found"])
        self.assertEqual(result["slot"], "rhand")
        self.assertEqual(result["replaced"], "longsword")
        self.assertEqual(self.dm_core.entities["gladstone"]["equipped"]["rhand"], "rusty shortsword")
        self.assertIn("longsword", self.dm_core.entities["gladstone"]["inventory"])
        self.assertNotIn("longsword", self.dm_core.entities["gladstone"]["equipped"].values())


    def test_unequip_clears_the_slot_but_keeps_the_item_in_inventory(self):
        result = self._interact("unequip", "longsword")

        self.assertTrue(result["found"])
        self.assertEqual(result["slot"], "rhand")
        self.assertNotIn("rhand", self.dm_core.entities["gladstone"]["equipped"])
        self.assertIn("longsword", self.dm_core.entities["gladstone"]["inventory"])


class TestInventoryTransfer(DMTestCase):
    scenario_name = "dungeon"


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


    def test_talking_to_the_innkeeper_narrates_immediately_as_dialogue(self):
        self.dm_core._on_action_detected({
            "actions": [{"skill": "charisma"}],
            "input": "I ask the innkeeper if she's heard any news from the road",
        })

        self.assertEqual(len(self.action_events), 1)
        self.assertEqual(self.round_events, [])
        result = self.action_events[0]["actions"][0]
        self.assertEqual(result["defender"], "innkeeper")
        self.assertNotIn("round", self.action_events[0])
        self.assertNotIn("damage", result)


    def test_fighting_a_hostile_target_still_batches_into_round_resolved(self):
        # Sanity check the branch didn't regress combat routing for an actually hostile target.
        self.dm_core.scenario = {
            "entities": [
                { "name": "gladstone", "band": 1 },
                { "name": "wolf", "band": 1 },
            ],
        }
        self.dm_core.load_scenario()

        self.dm_core._on_action_detected({"actions": [{"skill": "blades"}], "input": "I attack the wolf"})

        self.assertEqual(len(self.round_events), 1)
        self.assertEqual(self.action_events, [])
        self.assertEqual(self.round_events[0]["round"], 1)


class TestFreeformDialogue(DMTestCase):
    """!
    @brief DM_Dialogue.py's DialogueMixin -- the new diceless "directly address someone"
        channel, distinct from TestNpcDialogue above (which is the pre-existing, still-valid
        charisma skill check path). scenario "tavern" puts the player with a friendly NPC
        (npcs.toml's innkeeper), same fixture TestNpcDialogue itself uses.
    """
    scenario_name = "tavern"

    def setUp(self):
        super().setUp()
        self.dialogue_events = self._capture("dialogue_resolved")

    def _talk(self, input_text):
        self.dm_core._on_dialogue_detected({"input": input_text})
        return self.dialogue_events[-1]

    def test_named_target_resolves_over_the_default(self):
        result = self._talk("i ask the innkeeper about the road")

        self.assertTrue(result["found"])
        self.assertEqual(result["target"], "innkeeper")
        self.assertIn("innkeeper", result["persona"])

    def test_hostile_target_is_still_addressable(self):
        # Unlike combat targeting, dialogue never gates on hostility -- addressing something
        # hostile (ex: shouting at a wolf mid-fight) is allowed. "wolf" is already loaded as a
        # template (creatures.toml, via load_rules) even though tavern.toml never instances
        # it -- just needs to be added to the live scene for this one check.
        self.dm_core.scenario_entities.append("wolf")
        self.assertTrue(self.dm_core.is_hostile("wolf", self.dm_core.player_name))

        result = self._talk("i talk to the wolf")

        self.assertTrue(result["found"])
        self.assertEqual(result["target"], "wolf")

    def test_absent_or_dead_target_is_denied(self):
        dead_result = self._talk("i talk to the innkeeper")
        self.dm_core.apply_damage("innkeeper", 9999)

        result = self._talk("i talk to the innkeeper")

        self.assertTrue(dead_result["found"])  # sanity: alive, this would have worked before
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_present")

    def test_object_entity_cannot_be_addressed(self):
        self.dm_core.entities["stone idol"] = {"name": "stone idol", "supertype": "object", "hp": 1}
        self.dm_core.scenario_entities.append("stone idol")

        result = self._talk("i talk to the stone idol")

        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "cant_talk")

    def test_no_addressee_at_all_is_denied(self):
        self.dm_core.scenario = {"entities": [{"name": "gladstone", "band": 1}]}
        self.dm_core.load_scenario()

        result = self._talk("hello? is anyone there")

        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "no_one_here")

    def test_every_dialogue_resolution_is_tagged_with_current_presence(self):
        result = self._talk("i talk to the innkeeper")
        self.assertEqual(set(result["present_entities"]), set(self.dm_core.scenario_entities))


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


class TestIsHostileThreshold(DMTestCase):
    """!
    @brief is_hostile's two distinct defaults (DM_Social.py): an entity with no
        [entity.attitudes] table at all (ex: creatures.toml's wolf/bandit) is hostile
        unconditionally, regardless of the -100 threshold below -- otherwise every existing
        hostile creature with no authored attitude data would stop fighting the moment the
        threshold tightened from "<= 0" to "<= -100".
    """

    def test_no_attitude_table_at_all_is_still_hostile(self):
        self.assertNotIn("attitudes", self.dm_core.entities["wolf"])
        self.assertTrue(self.dm_core.is_hostile("wolf", self.dm_core.player_name))

    def test_declared_attitude_data_requires_true_hostility_not_just_a_negative_disposition(self):
        self.dm_core.entities["wary_npc"] = {
            "supertype": "creature", "attitudes": {"default": [-40, 0, 0, 0, 0, 0]},
        }
        self.dm_core.entities["hostile_npc"] = {
            "supertype": "creature", "attitudes": {"default": [-100, 0, 0, 0, 0, 0]},
        }
        self.assertFalse(self.dm_core.is_hostile("wary_npc", self.dm_core.player_name))
        self.assertTrue(self.dm_core.is_hostile("hostile_npc", self.dm_core.player_name))

    def test_object_supertype_is_never_hostile_regardless_of_attitude_data(self):
        self.dm_core.entities["angry_chest"] = {
            "supertype": "object", "attitudes": {"default": [-100, 0, 0, 0, 0, 0]},
        }
        self.assertFalse(self.dm_core.is_hostile("angry_chest", self.dm_core.player_name))


class TestEquipSlots(DMTestCase):
    def test_get_equip_slots_prefers_subtype_match_over_supertype_only_entry(self):
        self.dm_core.rules["equip_slot"] = [
            {"supertype": "creature", "slots": ["default_slot"]},
            {"supertype": "creature", "subtype": "humanoid", "slots": ["rhand", "chest"]},
        ]
        self.assertEqual(self.dm_core.get_equip_slots("gladstone"), ["rhand", "chest"])


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


    def test_load_restores_saved_state_over_further_changes(self):
        slot = self._track("test_load_restores_state")
        self.dm_core.apply_damage("wolf", 10)  # wolf at 6/16
        self.dm_core.save_game(slot)

        self.dm_core.apply_damage("wolf", 6)  # wolf now at 0/16, diverged further from the save
        self.assertEqual(self.dm_core.get_current_hp("wolf"), 0)

        self.dm_core.load_game(slot)

        self.assertEqual(self.dm_core.get_current_hp("wolf"), 6)


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
        self.assertEqual(self.dm_core.scenario_entities, ["gladstone", "thane", "anne", "dart trap"])
        # A trap is never hostile (same is_hostile short-circuit as any other "object"
        # supertype) -- with nothing hostile in the room, current_target falls back to it,
        # exactly the way the original dungeon.toml's chest already works.
        self.assertEqual(self.dm_core.current_target, "dart trap")


    def test_party_formation_holds_after_advancing(self):
        # thane (follow_offset = 0) walks abreast; anne (follow_offset = -1) trails one band
        # behind -- both snap back into formation the moment the player's own band changes
        # (_apply_party_formation, DM_Movement.py), not just at scenario load.
        self.assertEqual(self.dm_core.get_band("gladstone"), 1)
        self.assertEqual(self.dm_core.get_band("thane"), 1)
        self.assertEqual(self.dm_core.get_band("anne"), 1)  # -1 clamped to the floor

        self.dm_core.advance_or_retreat("advance")  # entrance is 2 bands -- room to actually move

        self.assertEqual(self.dm_core.get_band("gladstone"), 2)
        self.assertEqual(self.dm_core.get_band("thane"), 2)  # walks abreast
        self.assertEqual(self.dm_core.get_band("anne"), 1)  # one band behind, no longer clamped

        self.dm_core.advance_or_retreat("retreat")

        self.assertEqual(self.dm_core.get_band("gladstone"), 1)
        self.assertEqual(self.dm_core.get_band("thane"), 1)
        self.assertEqual(self.dm_core.get_band("anne"), 1)


    def test_hidden_trap_fails_its_notice_roll_and_stays_out_of_the_roster(self):
        with patch("random.randint", return_value=1):  # observation 1D=1, under difficulty 4
            dm = DMCore(EventBus(), scenario_name="crypt")
        self.assertTrue(dm.is_hidden("dart trap"))
        roster_text = " ".join(dm._describe_scenario_characters())
        self.assertNotIn("dart trap", roster_text)


    def test_failed_disarm_damages_the_player_and_arms_blocks_further_attempts(self):
        starting_hp = self.dm_core.get_current_hp("gladstone")
        with patch("random.randint", return_value=1):  # finesse 3d1=3, well under difficulty 9
            self.dm_core._on_action_detected({"actions": [{"skill": "finesse"}], "input": "I try to disarm the trap"})

        result = self.action_events[-1]["actions"][0]
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
            self.dm_core._on_action_detected({"actions": [{"skill": "finesse"}], "input": "I try again"})
        self.assertEqual(self.action_events[-1]["actions"][0]["difficulty"], 0)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), hp_after_first_hit)


    def test_forward_succeeds_once_the_player_reaches_the_exit_band(self):
        with patch("random.randint", return_value=6):
            self.dm_core._on_action_detected({"actions": [{"skill": "finesse"}], "input": "I disarm the trap"})
        self.dm_core.advance_or_retreat("advance")  # band 1 -> 2, toward the trap/exit
        self.assertEqual(self.dm_core.get_band("gladstone"), 2)

        result = self._move("forward")

        self.assertTrue(result["found"])
        self.assertEqual(result["room_name"], "The Hall of Webs")
        self.assertEqual(self.dm_core.current_room_key, "hall_of_webs")
        self.assertEqual(self.dm_core.scenario_entities, ["gladstone", "thane", "anne", "giant spider"])
        self.assertEqual(self.dm_core.current_target, "giant spider")
        self.assertEqual(self.dm_core.get_band("gladstone"), 1)  # this exit's own arrival_band

    def test_move_blocked_while_a_hostile_creature_is_still_alive(self):
        self.dm_core.enter_room("hall_of_webs")  # spider present, still alive
        self.item_events.clear()

        result = self._move("forward")

        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "blocked_by_enemies")
        self.assertEqual(self.dm_core.current_room_key, "hall_of_webs")


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


class TestRoomLevelPresenceScoping(unittest.TestCase):
    """!
    @brief The actual payoff of room-level presence tagging: DMCore and LLMCore wired
        together over one real event bus (crypt.toml, same room graph TestMultiRoomDungeon
        exercises) -- an entity met only after a room transition has no access to what was
        narrated before it existed, while a party member who traveled through both rooms
        does. NLPCore is deliberately left out (dialogue/movement are triggered directly on
        dm_core, the same way TestMultiRoomDungeon's own _move helper does) -- this is about
        presence tagging flowing correctly between the two real cores, not NLP matching.
    """

    def setUp(self):
        self.event_bus = EventBus()
        # LLMCore must exist (and be subscribed) before DMCore's own __init__ publishes its
        # first "scenario_loaded" -- same ordering TestGameBoot already requires for NLPCore's
        # "rules_loaded" subscription, for the exact same reason.
        self.llm_core = LLMCore(self.event_bus, rag_source_dir=os.path.join("Rules", "Fantasy"))
        self.dm_core = DMCore(self.event_bus, scenario_name="crypt")

    def test_dialogue_history_is_scoped_to_who_was_actually_in_the_room(self):
        # Entrance room: gladstone/thane/anne/dart trap. This narration entry is tagged with
        # that roster -- "giant spider" was never present for it.
        entrance_entries = len(self.llm_core.context_window)
        self.assertGreater(entrance_entries, 0)

        self.dm_core.advance_or_retreat("advance")  # band 1 -> 2, the exit band
        self.dm_core._on_item_interaction_detected(
            {"intent": "move", "item_name": None, "direction": "forward", "input": "go forward"}
        )
        self.assertEqual(self.dm_core.current_room_key, "hall_of_webs")

        self.dm_core._on_dialogue_detected({"input": "i talk to the giant spider"})

        spider_history = self.llm_core._filter_present_history("giant spider")
        thane_history = self.llm_core._filter_present_history("thane")

        # The spider only ever witnessed what happened after the room transition -- none of
        # the entrance room's own narration/dialogue setup entries.
        self.assertEqual(len(spider_history), len(self.llm_core.context_window) - entrance_entries)
        for entry in spider_history:
            self.assertNotIn("dart trap", entry.get("present") or [])

        # thane persisted across both rooms (crypt.toml's own [scenario].entities), so his own
        # witnessed history spans the entrance narration *and* everything since.
        self.assertEqual(len(thane_history), len(self.llm_core.context_window))


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
        self.assertEqual(fresh_dm.scenario_entities, ["gladstone", "thane", "anne", "giant spider"])
        self.assertEqual(fresh_dm.get_current_hp("giant spider"), 9)


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


class TestLlmDebugEvent(LLMTestCase):
    """!
    @brief fetch_from_llm's own network path (LLM_Core.py's _queue_narration) never runs for
        real in this offline suite -- threading.Thread is patched so its target is captured
        and invoked directly/synchronously instead of on a real background thread, with
        urllib.request.urlopen mocked in place of a real LM Studio connection."""

    def _run_fetch(self, prompt, urlopen_result=None, urlopen_side_effect=None):
        with patch("threading.Thread") as mock_thread, \
             patch("urllib.request.urlopen", return_value=urlopen_result, side_effect=urlopen_side_effect):
            self.llm_core._queue_narration(prompt)
            mock_thread.call_args.kwargs["target"]()

    def test_successful_request_publishes_the_full_query_and_raw_response(self):
        debug_events = []
        self.event_bus.subscribe("llm_debug_updated", debug_events.append)
        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps(
            {"choices": [{"message": {"content": "The wolf snarls."}}]}
        ).encode("utf-8")

        self._run_fetch("The wolf attacks.", urlopen_result=fake_response)

        self.assertEqual(len(debug_events), 1)
        self.assertIn("[system]", debug_events[0]["query"])
        self.assertIn("The wolf attacks.", debug_events[0]["query"])
        self.assertEqual(debug_events[0]["response"], "The wolf snarls.")


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


class TestCharacterCreationDialog(unittest.TestCase):
    """!
    @brief Character_Creation_GUI.py's Tkinter dialog, exercised directly (no mainloop, no
        wait_window -- same "construct it, drive its widgets synchronously, no real modal
        block" pattern TestGUICore's own request_load tests already use). A small, hand-picked
        fixture (3 skills, a 3-dice pool, a 2-dice max per skill) rather than the real
        Rules/Fantasy data, so every expected number in these tests is easy to verify by hand.
    """

    # setUpClass (not setUp) so only one real Tk() root is ever created for this whole class --
    # TestGUICore already creates one Tk() per test across ~20 tests; stacking a Tk() root per
    # test here too pushed the total high enough to occasionally corrupt Tcl's own interpreter
    # state later in the same pytest process (an intermittent "invalid command name" /
    # tk-library TclError in an unrelated, later TestGUICore test -- a real, if rare,
    # environment fragility around creating many Tk() roots in one process, not a bug in this
    # dialog itself). Each test still gets its own fresh CharacterCreationDialog Toplevel,
    # destroyed at the end of every test method (or by the dialog's own Create/Cancel), just
    # not its own root.
    @classmethod
    def setUpClass(cls):
        cls.root = _new_tk_root_with_retry()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.skills = {"alpha": {}, "beta": {}, "gamma": {}}
        self.races = [
            {
                "name": "human", "description": "Baseline in everything.",
                "skill_dice": {"alpha": 2, "beta": 2, "gamma": 2},
            },
            {
                "name": "elf", "description": "Sharp senses, softer muscle.",
                # Absolute dice, not deltas -- and (unlike the old base_dice-fallback design)
                # every skill must be listed, "gamma" included, or it'd fall back to
                # UNTRAINED_DICE (0) instead of a deliberate value.
                "skill_dice": {"alpha": 3, "beta": 1, "gamma": 2},
            },
        ]
        self.character_creation = {"pool_dice": 3, "max_allocation_per_skill": 2}

    def _make_dialog(self):
        return CharacterCreationDialog(self.root, self.skills, self.races, self.character_creation)


    def test_create_sets_result_and_closes_the_dialog(self):
        dialog = self._make_dialog()
        dialog.allocation_vars["alpha"].set(2)
        dialog.allocation_vars["gamma"].set(1)

        dialog.create_button.invoke()

        self.assertEqual(
            dialog.result,
            {"race": "human", "allocation": {"alpha": 2, "gamma": 1}, "name": ""},
        )
        self.assertEqual(dialog.winfo_exists(), 0)

    def test_create_includes_a_trimmed_custom_name_when_entered(self):
        dialog = self._make_dialog()
        dialog.allocation_vars["alpha"].set(2)
        dialog.allocation_vars["gamma"].set(1)
        dialog.name_var.set("  Aria  ")

        dialog.create_button.invoke()

        self.assertEqual(dialog.result["name"], "Aria")


class TestGUICore(unittest.TestCase):
    """GUI_Core.py's Tkinter surface, exercised directly (no mainloop) -- see Textual_Core.py's
    own tests below for the headless-testable mirror this class doesn't duplicate."""

    # setUpClass (not setUp) so only one real Tk() root is ever created for this whole class,
    # the same fix TestCharacterCreationDialog uses -- creating ~20 real Tk() roots back to
    # back (one per test) was occasionally corrupting Tcl's own shared interpreter state later
    # in the same pytest process (an intermittent "invalid command name"/tk-library TclError in
    # a seemingly unrelated, later test -- a real environment fragility around Tk() churn, not
    # a bug in GUICore itself). Each test still gets its own fully independent GUICore instance
    # (GUI_Core.py's own `master` param makes its root a Toplevel of the shared root instead of
    # a brand new Tk()), destroyed at the end of every test, just not its own root interpreter.
    # _new_tk_root_with_retry (not a bare tk.Tk()) covers the residual case: the *one* Tk() root
    # this class does create can still occasionally fail the same way on its own, independent of
    # how much churn preceded it -- without a retry there, that single failure would error out
    # every test in the class at once instead of the pre-fix "one random test occasionally fails".
    @classmethod
    def setUpClass(cls):
        cls.shared_root = _new_tk_root_with_retry()
        cls.shared_root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.shared_root.destroy()

    def setUp(self):
        self.event_bus = EventBus()
        self.gui = GUICore(self.event_bus, master=self.shared_root)
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
        self.assertEqual(tab_texts, ["Party", "Notes", "Map", "Debug"])
        for event_name in ("llm_response_ready", "llm_debug_updated", "rules_loaded",
                           "party_status_changed", "game_saved", "game_loaded",
                           "game_load_failed", "save_requested", "load_requested"):
            self.assertIn(event_name, self.event_bus.subscribers)

    def test_menu_bar_layout_character_create_file_save_load_scenario_load(self):
        self.assertEqual(self.gui.menu_bar.entrycget(0, "label"), "Character")
        self.assertEqual(self.gui.menu_bar.entrycget(1, "label"), "File")
        self.assertEqual(self.gui.menu_bar.entrycget(2, "label"), "Scenario")

        self.assertEqual(self.gui.character_menu.index("end"), 0)
        self.assertEqual(self.gui.character_menu.entrycget(0, "label"), "Create...")

        self.assertEqual(self.gui.file_menu.index("end"), 1)
        self.assertEqual(self.gui.file_menu.entrycget(0, "label"), "Save...")
        self.assertEqual(self.gui.file_menu.entrycget(1, "label"), "Load...")

        self.assertEqual(self.gui.scenario_menu.index("end"), 0)
        self.assertEqual(self.gui.scenario_menu.entrycget(0, "label"), "Load...")
        self.assertEqual(str(self.gui.scenario_menu.entrycget(0, "state")), tk.DISABLED)

    @patch("GUI_Core.run_character_creation_dialog")
    @patch("GUI_Core.load_character_creation_data", return_value=({}, [], {}))
    def test_character_creation_unlocks_scenario_menu_and_load_publishes_scenario_selected(
        self, mock_load, mock_dialog,
    ):
        mock_dialog.return_value = {"race": "elf", "allocation": {"arcane": 5}, "name": "Aria"}
        self.gui.request_character_creation()

        self.assertEqual(str(self.gui.scenario_menu.entrycget(0, "state")), tk.NORMAL)

        events = []
        self.event_bus.subscribe("scenario_selected", events.append)

        self.gui.request_scenario_load()
        picker = next(w for w in self.gui.root.winfo_children() if isinstance(w, tk.Toplevel))
        listbox = next(w for w in picker.winfo_children() if isinstance(w, tk.Listbox))
        scenario_keys = [key for key, _name, _description in list_available_scenarios()]
        crypt_index = scenario_keys.index("crypt")
        listbox.selection_clear(0, tk.END)
        listbox.selection_set(crypt_index)
        button_row = next(w for w in picker.winfo_children() if isinstance(w, tk.Frame))
        load_button = next(
            w for w in button_row.winfo_children()
            if isinstance(w, tk.Button) and w.cget("text") == "Load"
        )

        load_button.invoke()

        self.assertEqual(events, [{
            "scenario_name": "crypt",
            "character": {"race": "elf", "allocation": {"arcane": 5}, "name": "Aria"},
        }])
        self.assertIsNone(self.gui._pending_character)
        self.assertEqual(str(self.gui.scenario_menu.entrycget(0, "state")), tk.DISABLED)
        self.assertFalse(picker.winfo_exists())

    def test_scenario_load_noops_when_no_character_is_pending(self):
        self.gui.request_scenario_load()
        self.assertEqual(
            [w for w in self.gui.root.winfo_children() if isinstance(w, tk.Toplevel)], [],
        )

    def test_rules_loaded_locks_the_scenario_menu_shut_for_the_rest_of_the_session(self):
        self.gui._pending_character = {"race": "human", "allocation": {}, "name": "Gladstone"}
        self.gui._set_scenario_menu_enabled(True)

        self.event_bus.publish("rules_loaded", {"entities": {}})

        self.assertIsNone(self.gui._pending_character)
        self.assertEqual(str(self.gui.scenario_menu.entrycget(0, "state")), tk.DISABLED)

        # A later Create... doesn't reopen it once a game has actually started.
        with patch("GUI_Core.load_character_creation_data", return_value=({}, [], {})), \
             patch("GUI_Core.run_character_creation_dialog", return_value={"race": "human", "allocation": {}, "name": "X"}):
            self.gui.request_character_creation()
        self.assertIsNone(self.gui._pending_character)
        self.assertEqual(str(self.gui.scenario_menu.entrycget(0, "state")), tk.DISABLED)


    def test_display_party_status_renders_equipment_skills_abilities_inventory_conditions(self):
        self.event_bus.publish("rules_loaded", {
            "scenario_entities": ["gladstone", "thane", "wolf"],
            "entities": {
                "gladstone": {
                    "is_player": True, "name": "Gladstone", "hp": 30, "max_hp": 36,
                    "equipped": {"rhand": "longsword"}, "abilities": ["cleave"],
                    "skills": {"blades": {"dice": 5, "pips": 0}, "athletics": {"dice": 2, "pips": 2}},
                    "inventory": ["torch", "torch"], "active_conditions": {"wounded": {}},
                },
                "thane": {"is_party": True, "name": "Thane", "hp": 10, "max_hp": 10},
                "wolf": {"name": "wolf", "hp": 10, "max_hp": 10},  # neither player nor party
                "anne": {"is_party": True, "name": "Anne", "hp": 8, "max_hp": 8},  # not in scenario_entities
            },
        })

        members = self.gui.party_tree.get_children()
        labels = [self.gui.party_tree.item(m, "text") for m in members]
        self.assertEqual(labels, ["Gladstone (HP: 30/36)", "Thane (HP: 10/10)"])

        groups = self.gui.party_tree.get_children(members[0])
        group_texts = [self.gui.party_tree.item(g, "text") for g in groups]
        self.assertEqual(group_texts, ["Equipment", "Skills", "Abilities", "Inventory", "Conditions"])

        equipment, skills, abilities, inventory, conditions = groups

        def child_texts(node):
            return [self.gui.party_tree.item(c, "text") for c in self.gui.party_tree.get_children(node)]

        self.assertEqual(child_texts(equipment), ["rhand: longsword"])
        self.assertEqual(child_texts(skills), ["blades: 5D", "athletics: 2D+2"])
        self.assertEqual(child_texts(abilities), ["cleave"])
        self.assertEqual(child_texts(inventory), ["torch x2"])
        self.assertEqual(child_texts(conditions), ["wounded"])


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

    @patch("GUI_Core.run_character_creation_dialog")
    @patch("GUI_Core.load_character_creation_data", return_value=({}, [], {}))
    def test_request_character_creation_publishes_character_created_with_the_dialogs_result(
        self, mock_load, mock_dialog,
    ):
        mock_dialog.return_value = {"race": "elf", "allocation": {"arcane": 5}, "name": "Aria"}
        events = []
        self.event_bus.subscribe("character_created", events.append)

        self.gui.request_character_creation()

        mock_load.assert_called_once()
        mock_dialog.assert_called_once_with(self.gui.root, {}, [], {})
        self.assertEqual(
            events, [{"character": {"race": "elf", "allocation": {"arcane": 5}, "name": "Aria"}}],
        )


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


if __name__ == "__main__":
    unittest.main()
