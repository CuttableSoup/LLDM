import asyncio
import json
import os
import shutil
import tempfile
import threading
import tkinter as tk
import tomllib
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
from AdHoc_Generation import (
    decide_entity_edit,
    decide_entity_removal,
    generate_ad_hoc_creature,
    generate_ad_hoc_item,
)
from Character_Creation_GUI import CharacterCreationDialog
from Challenge_Rating import calculate_challenge_rating, calculate_party_challenge_rating, skill_rating
from DM_Core import DMCore
from DM_Rules import list_available_scenarios
from Event_Bus import EventBus
from GUI_Core import GUICore
from Intent_Classification import (
    ADVANCE_KEYWORDS,
    CLOSE_KEYWORDS,
    DIALOGUE_KEYWORDS,
    DROP_KEYWORDS,
    EQUIP_KEYWORDS,
    EXAMINE_KEYWORDS,
    FORMATION_ABREAST_KEYWORDS,
    FORMATION_BEHIND_KEYWORDS,
    GIVE_KEYWORDS,
    OPEN_KEYWORDS,
    RETREAT_KEYWORDS,
    TAKE_KEYWORDS,
    TRADE_KEYWORDS,
    UNEQUIP_KEYWORDS,
    USE_KEYWORDS,
    IntentClassifier,
    detect_dialogue_intent,
    detect_help_intent,
    detect_item_intent,
    detect_save_load_intent,
    split_action_clauses,
)
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

        # 2. Track turn_detected events
        detected_actions = []
        def on_turn_detected(data):
            detected_actions.append(data)
        event_bus.subscribe("turn_detected", on_turn_detected)

        # 3. Initialize NLPCore FIRST so it doesn't miss rules_loaded
        nlp_core = NLPCore(event_bus)

        # 4. Initialize DMCore (this triggers rules_loaded)
        dm_core = DMCore(event_bus)

        # Verify that skills were actually loaded into the real SentenceTransformerMatcher
        self.assertGreater(len(nlp_core.matcher.skill_names), 0, "No skills loaded into NLPCore")

        # 5. Simulate user input
        test_input = "I attack with my sword"
        event_bus.publish("user_input_submitted", test_input)

        # 6. Verify skill identification
        self.assertGreater(len(detected_actions), 0, "No turn_detected event published")
        last_action = detected_actions[-1]["clauses"][0]
        self.assertEqual(last_action["skill"], "blades")
        self.assertGreater(last_action["score"], 0.5)
        print(f"Integration Test Success: '{test_input}' -> {last_action['skill']} ({last_action['score']:.4f})")


class TestNlpConfidenceThreshold(unittest.TestCase):
    """!
    @brief Covers behavior that genuinely needs the real SentenceTransformer model --
        confidence-threshold/keyword-fallback scoring and real embedding registration. Gate
        order and precedence (which used to also live here, reaching into NLPCore's own
        private methods) now live in TestIntentClassification, which exercises
        IntentClassifier directly with a FakeMatcher and needs no model load at all.
    """
    # setUpClass (not setUp) so the slow sentence-transformers load only happens once for
    # every test method in this class, not once per method.
    @classmethod
    def setUpClass(cls):
        cls.event_bus = EventBus()
        cls.nlp_core = NLPCore(cls.event_bus)
        cls.dm_core = DMCore(cls.event_bus)

    def setUp(self):
        # cls.dm_core is shared across every test in this class (setUpClass, not setUp) to
        # avoid paying the slow model load repeatedly. Re-running the same load_rules/
        # load_scenario_definition/load_scenario sequence __init__ and load_game both use
        # resets every mutable field back to a pristine "arena" load before each test method,
        # without re-paying for a new NLPCore/model load.
        self.dm_core.load_rules(os.path.join("Rules", "Fantasy"))
        self.dm_core.load_scenario_definition(self.dm_core.scenario_key)
        self.dm_core.load_scenario()

    def test_low_confidence_input_triggers_no_skill(self):
        # A greeting with no real skill/action content shouldn't be forced onto whatever
        # phrase happens to score highest (previously this mapped to "artistry" at ~0.32).
        detected_actions = []
        not_understood = []
        self.event_bus.subscribe("turn_detected", detected_actions.append)
        self.event_bus.subscribe("action_not_understood", not_understood.append)

        self.event_bus.publish("user_input_submitted", "Hey there innkeeper")

        self.assertEqual(detected_actions, [])
        # Publishing this instead of just staying silent is what lets LLMCore give the
        # player some response rather than the app appearing to stall.
        self.assertEqual(len(not_understood), 1)
        self.assertIn("innkeeper", not_understood[0]["input"])

    def test_clear_action_still_triggers_above_threshold(self):
        detected_actions = []
        self.event_bus.subscribe("turn_detected", detected_actions.append)

        self.event_bus.publish("user_input_submitted", "I attack with my sword")

        self.assertEqual(len(detected_actions), 1)
        action = detected_actions[0]["clauses"][0]
        self.assertEqual(action["skill"], "blades")
        self.assertGreaterEqual(action["score"], self.nlp_core.matcher.confidence_threshold)

    def test_keyword_fallback_rescues_a_below_threshold_literal_keyword_hit(self):
        # "bargain" isn't a keyword for anything, but "cost" is a literal keyword of
        # "appraise" (skills.toml) and the full sentence never clears confidence_threshold on
        # its own (~0.30 in practice) -- _match_by_keyword is what rescues this, gated on
        # appraise's own best embedding score (still ~0.30) clearing the much lower
        # keyword_fallback_floor rather than being accepted on keyword evidence alone.
        detected_actions = []
        self.event_bus.subscribe("turn_detected", detected_actions.append)

        self.event_bus.publish("user_input_submitted", "I'll bargain with her over the cost of supper")

        self.assertEqual(len(detected_actions), 1)
        action = detected_actions[0]["clauses"][0]
        self.assertEqual(action["skill"], "appraise")
        self.assertLess(action["score"], self.nlp_core.matcher.confidence_threshold)
        self.assertGreaterEqual(action["score"], self.nlp_core.matcher.keyword_fallback_floor)

    def test_item_catalog_updated_registers_a_new_item_matchable_afterward(self):
        # cls.nlp_core is shared across this whole class (setUpClass, not setUp) -- restore
        # its matcher's embeddings/indices afterward so registering a new item here can't leak
        # into any other test's own map_to_item/improvisation-fallback behavior.
        original_embeddings = self.nlp_core.matcher.item_embeddings
        original_indices = list(self.nlp_core.matcher.item_indices)
        try:
            item_name, _score = self.nlp_core.matcher.map_to_item("a glowing rubber chicken talisman")
            self.assertIsNone(item_name)

            self.event_bus.publish("item_catalog_updated", {
                "entities": [{
                    "name": "rubber chicken talisman",
                    "description": "A glowing rubber chicken talisman.",
                }],
            })

            item_name, _score = self.nlp_core.matcher.map_to_item("a glowing rubber chicken talisman")
            self.assertEqual(item_name, "rubber chicken talisman")
        finally:
            self.nlp_core.matcher.item_embeddings = original_embeddings
            self.nlp_core.matcher.item_indices = original_indices


class FakeMatcher:
    """!
    @brief Test-only IntentMatcher adapter -- returns pre-configured (name, score) tuples for
        exact clause-text lookups instead of running any real embedding model, so
        TestIntentClassification can exercise IntentClassifier's own gate/precedence order at
        full speed, with no SentenceTransformer load. Unmapped text always misses (None, 0.0),
        the same "confidently below threshold" shape SentenceTransformerMatcher returns for
        genuinely unmatched input. Real adapter: NLP_Core.py's SentenceTransformerMatcher --
        two adapters is what justifies IntentMatcher as a real seam rather than a hypothetical
        one authored just in case.
    """

    def __init__(self, actions=None, items=None, targets=None):
        self._actions = actions or {}
        self._items = items or {}
        self._targets = targets or {}

    def on_rules_loaded(self, data):
        pass

    def register_item(self, name, description):
        pass

    def map_to_action(self, processed_text):
        return self._actions.get(processed_text, (None, 0.0))

    def map_to_item(self, processed_text):
        return self._items.get(processed_text, (None, 0.0))

    def map_to_target(self, processed_text):
        return self._targets.get(processed_text, (None, 0.0))


class TestIntentClassification(unittest.TestCase):
    """!
    @brief Fast, offline coverage of Intent_Classification.py -- IntentClassifier.classify()
        exercised directly against a FakeMatcher, no EventBus/DMCore/SentenceTransformer
        needed at all. Covers exactly the precedence/gate-order question the pre-refactor
        NLPCore test suite never actually walked as an integrated sequence (ex:
        test_adam_wins_over_both_item_verb_and_dialogue, below) -- previously only individual
        mechanisms were covered in isolation. Pure gate functions (detect_item_intent,
        detect_dialogue_intent, detect_help_intent, detect_save_load_intent,
        split_action_clauses) are tested directly, with no classifier/matcher setup at all,
        since they need none.
    """

    def test_detect_item_intent_examine_vs_take_vs_neither(self):
        self.assertEqual(detect_item_intent("examine the dagger"), "examine")
        self.assertEqual(detect_item_intent("take the gold"), "take")
        self.assertIsNone(detect_item_intent("attack with my sword"))

    def test_detect_item_intent_unequip_wins_over_equip_substring(self):
        # "unequip " literally contains EQUIP_KEYWORDS' own "equip " as a substring -- this is
        # the one ordering dependency in item_intent detection most likely to regress silently
        # if the tuples were ever reordered.
        self.assertEqual(detect_item_intent("take off my armor"), "unequip")
        self.assertEqual(detect_item_intent("equip the armor"), "equip")

    def test_detect_item_intent_formation_wins_over_advance(self):
        self.assertEqual(detect_item_intent("stay behind me"), "formation_behind")
        self.assertEqual(detect_item_intent("walk beside me"), "formation_abreast")
        self.assertEqual(detect_item_intent("advance toward the wolf"), "advance")

    def test_detect_item_intent_close_requires_the_or_it_not_bare_close(self):
        # CLOSE_KEYWORDS requires "the"/"it" specifically so a bare "close " (as in "I fight in
        # close combat") can't misfire before skill matching gets a chance to run.
        self.assertEqual(detect_item_intent("close the chest"), "close")
        self.assertIsNone(detect_item_intent("i fight in close combat"))

    def test_detect_dialogue_intent_vs_item_and_skill_phrasing(self):
        self.assertTrue(detect_dialogue_intent("talk to the innkeeper"))
        self.assertTrue(detect_dialogue_intent("ask the guard about the road"))
        self.assertFalse(detect_dialogue_intent("take the gold"))
        self.assertFalse(detect_dialogue_intent("attack with my sword"))

    def test_detect_help_intent_matches_whole_word_adam_only(self):
        self.assertTrue(detect_help_intent("adam, what are my skills?"))
        self.assertTrue(detect_help_intent("ADaM help me"))
        # \b-anchored -- "adam" appearing inside another word must never match.
        self.assertFalse(detect_help_intent("this sword is adamantine"))
        self.assertFalse(detect_help_intent("attack the wolf"))

    def test_detect_save_load_intent_parses_slot_names(self):
        self.assertEqual(detect_save_load_intent("save as arena run 1"), ("save", "arena run 1"))
        self.assertEqual(detect_save_load_intent("save game as arena-run-1"), ("save", "arena-run-1"))
        self.assertEqual(detect_save_load_intent("save boss-fight"), ("save", "boss-fight"))
        self.assertEqual(detect_save_load_intent("load boss-fight"), ("load", "boss-fight"))
        self.assertEqual(detect_save_load_intent("load game as boss-fight"), ("load", "boss-fight"))

    def test_split_action_clauses_on_and_then_and_punctuation(self):
        self.assertEqual(
            split_action_clauses("attack the orc and cast a ward"),
            ["attack the orc", "cast a ward"],
        )
        self.assertEqual(split_action_clauses("attack and then retreat"), ["attack", "retreat"])
        self.assertEqual(split_action_clauses("attack with my sword"), ["attack with my sword"])
        # \b-anchored -- "and"/"then" appearing inside another word must never split
        # (ex: "handle"/"sandbox" both literally contain the substring "and").
        self.assertEqual(
            split_action_clauses("handle the sandbox carefully"),
            ["handle the sandbox carefully"],
        )

    def test_save_load_short_circuits_before_anything_else(self):
        classifier = IntentClassifier(FakeMatcher())
        _processed, events = classifier.classify("save as arena run 1")
        self.assertEqual(events, [{"event": "save_requested", "payload": {"slot": "arena run 1"}}])

    def test_multi_clause_input_publishes_multiple_actions(self):
        classifier = IntentClassifier(FakeMatcher(actions={
            "attack with my sword": ("blades", 0.9),
            "pick the lock": ("finesse", 0.8),
        }))
        _processed, events = classifier.classify("I attack with my sword and pick the lock")

        self.assertEqual(len(events), 1)
        skills = [clause["skill"] for clause in events[0]["payload"]["clauses"]]
        self.assertEqual(skills, ["blades", "finesse"])

    def test_mixed_item_and_action_clause_publishes_one_merged_turn(self):
        # The pipeline merge: an item-interaction clause and a skill/ability clause in one
        # input join the same turn_detected event, not two separate, uncoordinated ones.
        classifier = IntentClassifier(FakeMatcher(
            items={"take the longsword": ("longsword", 0.9)},
            actions={"attack the wolf": ("blades", 0.9)},
        ))
        _processed, events = classifier.classify("I take the longsword and attack the wolf")

        self.assertEqual(len(events), 1)
        clauses = events[0]["payload"]["clauses"]
        self.assertEqual(len(clauses), 2)
        self.assertEqual(clauses[0], {"kind": "item", "intent": "take", "item_name": "longsword"})
        self.assertEqual(clauses[1]["kind"], "action")
        self.assertEqual(clauses[1]["skill"], "blades")

    def test_exempt_clause_mixed_with_an_item_clause_still_publishes_separately(self):
        # "retreat" stays free (West End Games' own movement exception) and never joins the
        # shared turn, even when another clause in the same input is a genuine item action.
        classifier = IntentClassifier(FakeMatcher(items={"take the longsword": ("longsword", 0.9)}))
        _processed, events = classifier.classify("take the longsword and retreat")

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event"], "item_interaction_detected")
        self.assertEqual(events[0]["payload"]["intent"], "retreat")
        self.assertEqual(events[1]["event"], "turn_detected")
        self.assertEqual(
            events[1]["payload"]["clauses"], [{"kind": "item", "intent": "take", "item_name": "longsword"}],
        )

    def test_item_verb_still_takes_priority_over_dialogue(self):
        # A genuine item verb naming an entity is never swallowed as dialogue, even though
        # "to thane" would otherwise read as conversational address.
        classifier = IntentClassifier(FakeMatcher(items={"give the longsword to thane": ("longsword", 0.9)}))
        _processed, events = classifier.classify("give the longsword to thane")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "turn_detected")
        self.assertEqual(
            events[0]["payload"]["clauses"], [{"kind": "item", "intent": "give", "item_name": "longsword"}],
        )

    def test_dialogue_wins_once_item_pass_finds_nothing(self):
        classifier = IntentClassifier(FakeMatcher())
        _processed, events = classifier.classify("talk to the wolf")
        self.assertEqual(events, [{"event": "dialogue_detected", "payload": {"input": "talk to the wolf", "score": None}}])

    def test_adam_wins_over_both_item_verb_and_dialogue_in_the_same_input(self):
        # Checked ahead of both the item-interaction pass and DIALOGUE_KEYWORDS -- naming
        # "adam" anywhere in the input always reaches the help channel, never ordinary
        # dialogue or a real item turn, no matter what else the input contains.
        classifier = IntentClassifier(FakeMatcher(items={"the longsword to thane": ("longsword", 0.9)}))
        _processed, events = classifier.classify("talk to ADaM and give the longsword to thane")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "help_detected")
        self.assertIn("adam", events[0]["payload"]["input"])

    def test_removal_candidate_flag_only_set_when_removal_keywords_present(self):
        classifier = IntentClassifier(FakeMatcher())

        _processed, events = classifier.classify("adam, get rid of that torch")
        self.assertTrue(events[0]["payload"]["removal_candidate"])

        _processed, events = classifier.classify("adam, what are my skills")
        self.assertFalse(events[0]["payload"]["removal_candidate"])

    def test_creature_and_edit_candidate_flags_only_set_when_their_own_keywords_present(self):
        classifier = IntentClassifier(FakeMatcher())

        _processed, events = classifier.classify("adam, summon a wolf")
        self.assertTrue(events[0]["payload"]["creature_candidate"])
        self.assertFalse(events[0]["payload"]["edit_candidate"])
        self.assertFalse(events[0]["payload"]["removal_candidate"])

        _processed, events = classifier.classify("adam, change the torch's description")
        self.assertTrue(events[0]["payload"]["edit_candidate"])
        self.assertFalse(events[0]["payload"]["creature_candidate"])

        _processed, events = classifier.classify("adam, what are my skills")
        self.assertFalse(events[0]["payload"]["creature_candidate"])
        self.assertFalse(events[0]["payload"]["edit_candidate"])

    def test_unmatched_item_verb_triggers_improvisation_instead_of_action_not_understood(self):
        # FakeMatcher's map_to_item/map_to_action both miss (default) for this phrase -- the
        # whole turn would otherwise resolve to nothing at all, so the recognized-but-unmatched
        # "take" verb becomes DM_Improvisation.py's own last-resort candidate instead.
        classifier = IntentClassifier(FakeMatcher())
        _processed, events = classifier.classify("take the strange glowing talisman")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "improvisation_requested")
        self.assertEqual(events[0]["payload"]["intent"], "take")

    def test_matched_clause_elsewhere_in_input_still_wins_over_improvisation(self):
        # A compound input where one clause resolves normally still takes the ordinary path --
        # extending ad hoc creation into multi-clause turns is deliberately out of scope (see
        # CLAUDE.md's "Ad hoc entity creation and removal").
        classifier = IntentClassifier(FakeMatcher(actions={"attack the wolf": ("blades", 0.9)}))
        _processed, events = classifier.classify("take the strange glowing talisman and attack the wolf")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "turn_detected")

    def test_low_confidence_input_publishes_action_not_understood(self):
        classifier = IntentClassifier(FakeMatcher())
        _processed, events = classifier.classify("Hey there innkeeper")
        self.assertEqual(events[0]["event"], "action_not_understood")

    def test_item_and_dialogue_keywords_never_collide_with_a_real_skill_keyword(self):
        # Turns the prose scattered across Intent_Classification.py's own keyword-tuple
        # comments (ex: "TRADE_KEYWORDS deliberately avoids every word in skills.toml's
        # 'appraise' keywords list") into one executable invariant: no item/dialogue keyword
        # phrase this file declares should be findable inside a plain sentence that merely uses
        # a real skill's own keyword as a whole word -- otherwise a sentence clearly about that
        # skill could get silently swallowed by item/dialogue detection, which always runs
        # first. Skill keywords are checked space-padded (" {keyword} "), the minimal sentence
        # context a keyword could plausibly appear in, since the real detection code matches
        # each phrase (with whatever leading/trailing space it was authored with, ex:
        # "ask " or "close the ") against the whole processed sentence, not an isolated word.
        skills_path = os.path.join("Rules", "Fantasy", "skills.toml")
        with open(skills_path, "rb") as f:
            skills_data = tomllib.load(f)
        skill_keywords = set()
        for skill in skills_data.get("skill", []):
            skill_keywords.update(skill.get("keywords", []))

        keyword_tuples_by_name = {
            "EXAMINE_KEYWORDS": EXAMINE_KEYWORDS, "EQUIP_KEYWORDS": EQUIP_KEYWORDS,
            "UNEQUIP_KEYWORDS": UNEQUIP_KEYWORDS, "DROP_KEYWORDS": DROP_KEYWORDS,
            "TAKE_KEYWORDS": TAKE_KEYWORDS, "GIVE_KEYWORDS": GIVE_KEYWORDS,
            "TRADE_KEYWORDS": TRADE_KEYWORDS, "USE_KEYWORDS": USE_KEYWORDS,
            "OPEN_KEYWORDS": OPEN_KEYWORDS, "CLOSE_KEYWORDS": CLOSE_KEYWORDS,
            "ADVANCE_KEYWORDS": ADVANCE_KEYWORDS, "RETREAT_KEYWORDS": RETREAT_KEYWORDS,
            "FORMATION_BEHIND_KEYWORDS": FORMATION_BEHIND_KEYWORDS,
            "FORMATION_ABREAST_KEYWORDS": FORMATION_ABREAST_KEYWORDS,
            "DIALOGUE_KEYWORDS": DIALOGUE_KEYWORDS,
        }
        # Known, pre-existing exceptions -- not introduced by this refactor, and out of this
        # refactor's own scope to fix (a pure, zero-behavior-change extraction). "examine" is
        # deliberately both an EXAMINE_KEYWORDS phrase (item detection, checked first) and one
        # of appraise's own skills.toml keywords (CLAUDE.md's "Known gaps": "a keyword-driven
        # skill match can still dominate an unrelated whole-sentence embedding match"), so
        # "examine the dagger" always resolves as an item-examine rather than ever reaching
        # appraise -- accepted. "ask " colliding with "mask" (ex: a disguise/stealth skill's
        # own keyword) was NOT previously known or documented anywhere -- this test found it
        # live; flagged here rather than silently fixed, since changing DIALOGUE_KEYWORDS'
        # matching behavior is a real behavior change, out of scope for this pass.
        known_exceptions = {
            ("EXAMINE_KEYWORDS", "examine", "examine"),
            ("DIALOGUE_KEYWORDS", "ask ", "mask"),
        }

        for tuple_name, keyword_tuple in keyword_tuples_by_name.items():
            for phrase in keyword_tuple:
                for skill_keyword in skill_keywords:
                    if (tuple_name, phrase, skill_keyword) in known_exceptions:
                        continue
                    self.assertNotIn(
                        phrase, f" {skill_keyword} ",
                        f"{tuple_name}'s {phrase!r} is findable inside a sentence using skill "
                        f"keyword {skill_keyword!r}",
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
            self.dm_core._on_turn_detected({
                "clauses": [{"kind": "action", "skill": "blades"}, {"kind": "action", "skill": "blades"}],
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
            self.dm_core._on_turn_detected({
                "clauses": [
                    {"kind": "action", "skill": "blades"}, {"kind": "action", "skill": "blades"},
                    {"kind": "action", "skill": "blades"},
                ],
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
        self.dm_core._on_turn_detected({
            "clauses": [{"kind": "action", "skill": "appraise", "target": "health potion"}],
            "input": "I appraise the health potion",
        })

        self.assertEqual(round_events, [])
        self.assertEqual(len(action_events), 1)

    def test_mixed_item_test_and_attack_batch_shares_the_penalty_and_still_one_round(self):
        # An item *test* (ex: appraising a potion) rolls dice, so it shares the turn's penalty
        # just like an opposed attack does -- distinct from a diceless item *interaction*
        # (give/take/equip/...), covered by the tests below.
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self.dm_core.scenario = {"entities": [{"name": "gladstone", "band": 1}, {"name": "practice_dummy", "band": 1}]}
        self.dm_core.load_scenario()
        round_events = self._capture("round_resolved")

        with patch("random.randint", return_value=3):
            self.dm_core._on_turn_detected({
                "clauses": [
                    {"kind": "action", "skill": "appraise", "target": "health potion"},
                    {"kind": "action", "skill": "blades"},
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

    def test_item_interaction_clause_shares_the_penalty_but_never_rolls_itself(self):
        # Drawing a weapon, picking something up, giving/opening/using an item all cost the
        # same shared per-turn action economy a skill/ability action does in West End Games
        # D6 -- only movement and speech are actually free. An item-interaction clause resolves
        # via the ordinary, unchanged item-interaction pipeline (narrating separately, via its
        # own item_interaction_resolved) and never receives dice_penalty itself (it has
        # nothing to roll), but it still counts toward this turn's total N.
        item_events = self._capture("item_interaction_resolved")
        round_events = self._capture("round_resolved")

        with patch("random.randint", return_value=3):
            self.dm_core._on_turn_detected({
                "clauses": [
                    {"kind": "item", "intent": "drop", "item_name": "health potion"},
                    {"kind": "action", "skill": "blades"},
                ],
                "input": "I drop a health potion and attack the wolf",
            })

        self.assertEqual(len(item_events), 1)
        self.assertEqual(item_events[0]["intent"], "drop")
        self.assertTrue(item_events[0]["found"])
        self.assertIn("health potion", self.dm_core._current_ground_items())

        self.assertEqual(len(round_events), 1)
        actions = round_events[0]["actions"]
        self.assertEqual(len(actions), 1)
        # blades is 5D+0 -- at -1D (the drop counts as this turn's other action, even though
        # it never rolls) rolls 4D @ 3 = 12, not the unpenalized 5D @ 3 = 15 a lone attack
        # would get.
        self.assertEqual(actions[0]["roll"], 12)

    def test_item_only_turn_publishes_via_item_interaction_resolved_not_action_resolved(self):
        action_events = self._capture("action_resolved")
        round_events = self._capture("round_resolved")
        item_events = self._capture("item_interaction_resolved")

        self.dm_core._on_turn_detected({
            "clauses": [{"kind": "item", "intent": "drop", "item_name": "health potion"}],
            "input": "I drop a health potion",
        })

        self.assertEqual(len(item_events), 1)
        self.assertEqual(action_events, [])
        self.assertEqual(round_events, [])


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
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "blades"}], "input": "I attack with my sword"})

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
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "blades"}], "input": "I attack with my sword"})

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

        self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "blades"}], "input": "I attack with my sword"})

        result = self.resolved[-1]
        action = result["actions"][0]
        self.assertFalse(action["success"])
        self.assertEqual(action["reason"], "out_of_range")
        self.assertIsNone(action["roll"])
        self.assertNotIn("damage", action)


class TestEntityBehavior(DMTestCase):
    def setUp(self):
        super().setUp()
        self.resolved = self._capture("round_resolved")

    def test_choose_behavior_matches_while_the_entity_is_alive(self):
        # arena.toml's wolf: a single behavior, "always bite while hp_per_remain >= 0.01".
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
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "athletics"}], "input": "I reposition"})
        self.assertEqual(self.dm_core.current_target, "wolf_2")


class TestBandit(DMTestCase):
    # "bandit" is field.toml's own local entity now (creatures.toml no longer carries one --
    # see its own comment) -- booting "field" first is what makes it resolvable at all, before
    # setUp below overrides self.dm_core.scenario/re-runs load_scenario() with a custom band
    # layout.
    scenario_name = "field"

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


class TestAdHocGeneration(unittest.TestCase):
    """!
    @brief AdHoc_Generation.py's pure LLM-calling logic -- no DMCore, no live LLM (see
        TestImprovisation for the DMCore-side glue that actually mutates game state).
        generate_ad_hoc_item/decide_entity_removal's own call_chat_completion
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

        result = generate_ad_hoc_item("a stone", "take", "A dusty antechamber.", call_chat_completion=fake_call)

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

        result = generate_ad_hoc_item("a rusty knife", "take", "A rubbish heap.", call_chat_completion=fake_call)

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

        valid = generate_ad_hoc_item(
            "a ring", "take", "A dungeon.", valid_equip_slots=["ring", "neck"], call_chat_completion=fake_call,
        )
        self.assertEqual(valid["entity"]["equip_slot"], "ring")

        invalid = generate_ad_hoc_item(
            "a ring", "take", "A dungeon.", valid_equip_slots=["rhand"], call_chat_completion=fake_call,
        )
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

        result = generate_ad_hoc_item("a tonic", "use", "A dungeon.", call_chat_completion=fake_call)

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

        result = generate_ad_hoc_item("a strange vial", "use", "A dungeon.", call_chat_completion=fake_call)

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

        result = generate_ad_hoc_item("a stone", "take", "A dungeon.", call_chat_completion=fake_call)

        self.assertNotIn("usable", result["entity"])
        self.assertNotIn("skills", result["entity"])

    def test_decline_reports_not_created(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "decline", "arguments": json.dumps({"reason": "not plausible here"}),
            }}]}}]}

        result = generate_ad_hoc_item("the moon", "take", "A dungeon.", call_chat_completion=fake_call)
        self.assertFalse(result["created"])

    def test_generate_ad_hoc_item_never_fabricates_when_call_chat_completion_raises(self):
        def failing_call(*args, **kwargs):
            raise ConnectionError("no LM Studio")

        result = generate_ad_hoc_item("a stone", "take", "A dungeon.", call_chat_completion=failing_call)
        self.assertFalse(result["created"])
        self.assertEqual(result["reason"], "unavailable")

    def test_decide_entity_removal_picks_a_real_name(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "remove_entity",
                "arguments": json.dumps({"name": "torch", "reason": "player asked"}),
            }}]}}]}

        result = decide_entity_removal(
            "get rid of that torch", "A dim hallway.", ["torch", "wolf"], call_chat_completion=fake_call,
        )
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

        result = decide_entity_removal(
            "remove it", "A dim hallway.", ["torch"], call_chat_completion=fake_call,
        )
        self.assertFalse(result["removed"])

    def test_decide_entity_removal_short_circuits_on_no_removable_entities(self):
        def exploding_call(*args, **kwargs):
            raise AssertionError("should never be called with nothing removable")

        result = decide_entity_removal("remove something", "desc", [], call_chat_completion=exploding_call)
        self.assertFalse(result["removed"])

    def test_decide_entity_removal_never_fabricates_when_call_chat_completion_raises(self):
        def failing_call(*args, **kwargs):
            raise TimeoutError("slow")

        result = decide_entity_removal("remove the torch", "desc", ["torch"], call_chat_completion=failing_call)
        self.assertFalse(result["removed"])

    def test_describe_scenery_reports_no_entity_created(self):
        def fake_call(api_url, messages, tools=None, tool_choice=None, timeout=None):
            return {"choices": [{"message": {"tool_calls": [{"function": {
                "name": "describe_scenery",
                "arguments": json.dumps({"description": "Faint claw marks score the stone wall."}),
            }}]}}]}

        result = generate_ad_hoc_item("the wall", "examine", "A dungeon.", call_chat_completion=fake_call)

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

        result = generate_ad_hoc_item(
            "a crate", "examine", "A storeroom.", valid_skill_names=["finesse", "blades"],
            call_chat_completion=fake_call,
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

        result = generate_ad_hoc_item(
            "a crate", "examine", "A storeroom.", valid_skill_names=["finesse", "blades"],
            call_chat_completion=fake_call,
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

        result = generate_ad_hoc_item("a crate", "examine", "A storeroom.", call_chat_completion=fake_call)

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

        result = generate_ad_hoc_item(
            "a trap", "examine", "A corridor.", valid_skill_names=["finesse"], call_chat_completion=fake_call,
        )

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

        result = generate_ad_hoc_creature(
            "a rat", "A dank cellar.", target_cr=20, npc_keywords=npc_keywords, call_chat_completion=fake_call,
        )

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

        result = generate_ad_hoc_creature(
            "a pilgrim", "A dusty road.", target_cr=20, npc_keywords=npc_keywords, call_chat_completion=fake_call,
        )

        entity = result["entity"]
        self.assertEqual(entity["attitudes"]["default"][0], 60)
        self.assertNotIn("abilities", entity)
        self.assertNotIn("behavior", entity)

    def test_generate_ad_hoc_creature_short_circuits_on_empty_keyword_catalog(self):
        def exploding_call(*args, **kwargs):
            raise AssertionError("should never be called with no npc_keywords catalog")

        result = generate_ad_hoc_creature("a rat", "desc", target_cr=20, npc_keywords={}, call_chat_completion=exploding_call)
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

        result = decide_entity_edit("the torch is almost burned out", "desc", ["torch"], call_chat_completion=fake_call)

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

        result = decide_entity_edit("change it", "desc", ["torch"], call_chat_completion=fake_call)
        self.assertFalse(result["edited"])

    def test_decide_entity_edit_short_circuits_on_no_editable_entities(self):
        def exploding_call(*args, **kwargs):
            raise AssertionError("should never be called with nothing editable")

        result = decide_entity_edit("change something", "desc", [], call_chat_completion=exploding_call)
        self.assertFalse(result["edited"])


class TestImprovisation(DMTestCase):
    """!
    @brief DM_Improvisation.py's ImprovisationMixin -- the DMCore-side glue for ad hoc entity
        creation/removal. AdHoc_Generation.py's own call_chat_completion is never exercised
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
        with patch("DM_Improvisation.generate_ad_hoc_item", return_value=self._fake_creation(location="ground")):
            self.dm_core._on_improvisation_requested({
                "intent": "take", "phrase": "a stone", "input": "pick up a stone",
            })

        self.assertIn("stone", self.dm_core.entities["gladstone"]["inventory"])
        self.assertNotIn("stone", self.dm_core._current_ground_items())
        self.assertEqual(self.catalog_events, [
            {"entities": [{"name": "stone", "description": "A smooth grey stone."}]},
        ])

    def test_ground_placement_examine_describes_without_taking(self):
        with patch("DM_Improvisation.generate_ad_hoc_item", return_value=self._fake_creation(location="ground")):
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
        with patch("DM_Improvisation.generate_ad_hoc_item", return_value=entity):
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
        self.dm_core.roll_dice = lambda dice, pips: 7
        starting_hp = self.dm_core.get_current_hp("gladstone")

        with patch("DM_Improvisation.generate_ad_hoc_item", return_value=entity):
            self.dm_core._on_improvisation_requested({
                "intent": "use", "phrase": "the strange vial", "input": "drink the strange vial",
            })

        result = self.item_events[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["poisoned"], 7)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), starting_hp - 7)

    def test_inventory_placement_examine_resolves_through_the_ordinary_pipeline(self):
        # Placement (place_new_item) and narration both go through the same redispatch every
        # other branch uses -- DM_Core.py's own source-resolution recognizes the item is
        # already in gladstone's inventory (see _on_item_interaction_detected's docstring), so
        # this no longer needs its own bespoke publish. found=True here is itself the real
        # regression guard: a broken source-resolution would fall back to the scene target's
        # own (empty) inventory and report not_present instead.
        with patch("DM_Improvisation.generate_ad_hoc_item", return_value=self._fake_creation(location="inventory")):
            self.dm_core._on_improvisation_requested({
                "intent": "examine", "phrase": "my pockets", "input": "check my pockets",
            })

        result = self.item_events[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["item_name"], "stone")
        self.assertIsNone(result["container"])
        self.assertIn("stone", self.dm_core.entities["gladstone"]["inventory"])

    def test_decline_falls_back_to_action_not_understood(self):
        with patch("DM_Improvisation.generate_ad_hoc_item", return_value=self._fake_creation(created=False)):
            self.dm_core._on_improvisation_requested({
                "intent": "take", "phrase": "the moon", "input": "take the moon",
            })

        self.assertEqual(len(self.not_understood_events), 1)
        self.assertEqual(self.item_events, [])

    def test_remove_entity_from_scene_strips_presence_and_prevents_respawn(self):
        self.assertIn("wolf", self.dm_core.scenario_entities)

        outcome = self.dm_core.remove_entity_from_scene("wolf")

        self.assertTrue(outcome["removed"])
        self.assertNotIn("wolf", self.dm_core.scenario_entities)
        self.assertIn("wolf", self.dm_core.removed_entities)

        # Simulates a revisit/reload re-instancing the scenario's own static entities list --
        # "wolf" must not respawn just because arena.toml still declares it (unlike "wolf_2",
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

        with patch("DM_Improvisation.decide_entity_removal", side_effect=fake_decide):
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

        with patch("DM_Improvisation.decide_entity_removal", side_effect=fake_decide):
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

        with patch("DM_Improvisation.decide_entity_removal", side_effect=fake_decide):
            self.dm_core._attempt_entity_removal("get rid of the dead wolf")

        self.assertNotIn("wolf", captured["hostile_entities"])
        self.assertIn("wolf_2", captured["hostile_entities"])

    def test_attempt_entity_removal_end_to_end_via_help_channel(self):
        help_events = self._capture("help_resolved")

        def fake_decide(phrase, scene_description, removable_entities, **kwargs):
            return {"removed": True, "name": "wolf", "reason": "player asked"}

        with patch("DM_Improvisation.decide_entity_removal", side_effect=fake_decide):
            self.dm_core._on_help_detected({"input": "adam, get rid of the wolf", "removal_candidate": True})

        self.assertNotIn("wolf", self.dm_core.scenario_entities)
        self.assertEqual(help_events[-1]["removed"], {"removed": True, "name": "wolf", "reason": "player asked"})

    def test_scenery_result_publishes_flavor_with_no_entity_created(self):
        entities_before = set(self.dm_core.entities)
        fake_result = {"created": False, "scenery": True, "description": "Claw marks score the stone."}

        with patch("DM_Improvisation.generate_ad_hoc_item", return_value=fake_result):
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
        with patch("DM_Improvisation.generate_ad_hoc_item", return_value=self._fake_container_creation(locked=False)):
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
        with patch("DM_Improvisation.generate_ad_hoc_item", return_value=self._fake_container_creation(locked=True)):
            self.dm_core._on_improvisation_requested({
                "intent": "examine", "phrase": "a crate", "input": "examine the old crate",
            })

        self.assertTrue(self.dm_core.is_locked("old crate"))
        self.assertEqual(self.dm_core.current_target, "old crate")

        self.dm_core.roll_dice = lambda dice, pips: 20  # guarantee the lock pick succeeds
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
        starting_hp = self.dm_core.get_current_hp("gladstone")
        self.dm_core.current_target = None  # no fight engaged -- see the container test's own note

        with patch("DM_Improvisation.generate_ad_hoc_item", return_value=fake_result):
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
        self.assertLess(self.dm_core.get_current_hp("gladstone"), starting_hp)

    def _fake_hostile_creature(self, name="cave rat"):
        entity = {
            "name": name, "description": "A mangy, oversized rat.",
            "supertype": "creature", "subtype": "npc", "max_hp": 9,
            "skills": {"brawling": {"dice": 2, "pips": 0}},
            "attitudes": {"default": [-100, 0, 0, 0, 0, 0]},
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
        with patch("DM_Improvisation.generate_ad_hoc_creature", return_value=self._fake_hostile_creature()):
            outcome = self.dm_core._attempt_creature_conjuring("summon a rat")

        self.assertTrue(outcome["created_creature"])
        self.assertIn("cave rat", self.dm_core.scenario_entities)
        self.assertTrue(self.dm_core.is_hostile("cave rat", self.dm_core.player_name))
        self.assertEqual(self.dm_core.current_target, "cave rat")
        self.assertEqual(self.dm_core.get_band("cave rat"), self.dm_core.get_band("gladstone"))

    def test_attempt_creature_conjuring_does_not_steal_target_from_an_engaged_fight(self):
        self.dm_core.current_target = "wolf"  # already engaged with a live hostile

        with patch("DM_Improvisation.generate_ad_hoc_creature", return_value=self._fake_hostile_creature()):
            self.dm_core._attempt_creature_conjuring("summon a rat")

        self.assertEqual(self.dm_core.current_target, "wolf")
        self.assertIn("cave rat", self.dm_core.scenario_entities)

    def test_attempt_creature_conjuring_declines_reports_false(self):
        with patch("DM_Improvisation.generate_ad_hoc_creature", return_value={"created": False, "reason": "declined"}):
            outcome = self.dm_core._attempt_creature_conjuring("summon a dragon")

        self.assertFalse(outcome["created_creature"])

    def test_attempt_entity_edit_changes_description_and_tags_edited(self):
        def fake_decide(phrase, scene_description, editable_entities, **kwargs):
            return {
                "edited": True, "name": "wolf", "reason": "player asked",
                "new_description": "A scarred, one-eyed wolf.",
                "apply_condition": None, "dismiss_condition": None,
            }

        with patch("DM_Improvisation.decide_entity_edit", side_effect=fake_decide):
            outcome = self.dm_core._attempt_entity_edit("the wolf has a scar over one eye")

        self.assertTrue(outcome["edited"])
        self.assertEqual(self.dm_core.entities["wolf"]["description"], "A scarred, one-eyed wolf.")
        self.assertTrue(self.dm_core.entities["wolf"]["edited"])

    def test_attempt_entity_edit_excludes_the_player_from_the_candidate_set(self):
        captured = {}

        def fake_decide(phrase, scene_description, editable_entities, **kwargs):
            captured["editable_entities"] = editable_entities
            return {"edited": False}

        with patch("DM_Improvisation.decide_entity_edit", side_effect=fake_decide):
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

        with patch("DM_Improvisation.decide_entity_edit", side_effect=fake_decide):
            self.dm_core._on_help_detected({"input": "adam, the wolf has a scar", "edit_candidate": True})

        self.assertEqual(self.dm_core.entities["wolf"]["description"], "A scarred, one-eyed wolf.")
        self.assertEqual(help_events[-1]["edited"]["name"], "wolf")

    def test_ad_hoc_item_name_colliding_with_a_live_entity_gets_disambiguated(self):
        # arena's own "wolf"/"wolf_2" are already live (see this class's own docstring) -- an
        # ad hoc item whose LLM-invented name collides with one must not silently overwrite it
        # (self.entities[name] = entity used to do exactly that before _unique_entity_key).
        entity = self._fake_creation(entity_overrides={"name": "wolf"}, location="ground")
        with patch("DM_Improvisation.generate_ad_hoc_item", return_value=entity):
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
        with patch("DM_Improvisation.generate_ad_hoc_creature", return_value=self._fake_hostile_creature(name="gladstone")):
            outcome = self.dm_core._attempt_creature_conjuring("summon my evil twin")

        self.assertTrue(outcome["created_creature"])
        self.assertEqual(outcome["name"], "gladstone_2")
        self.assertTrue(self.dm_core.entities["gladstone"]["is_player"])  # real player untouched
        self.assertIn("gladstone_2", self.dm_core.scenario_entities)
        self.assertEqual(self.dm_core.entities["gladstone_2"]["supertype"], "creature")

    def test_conjured_container_is_placed_at_the_players_current_band_not_band_1(self):
        self.dm_core.entities["gladstone"]["band"] = 3
        self.dm_core.current_target = None
        with patch("DM_Improvisation.generate_ad_hoc_item", return_value=self._fake_container_creation(locked=False)):
            self.dm_core._on_improvisation_requested({
                "intent": "examine", "phrase": "a crate", "input": "examine the old crate",
            })

        self.assertEqual(self.dm_core.get_band("old crate"), 3)


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


class TestReachableEntityNames(DMTestCase):
    """!
    @brief ImprovisationMixin._reachable_entity_names (DM_Improvisation.py) -- the shared
        "everything present/ground/inventory/equipped, minus the player" universe both
        _attempt_entity_removal and _attempt_entity_edit build off of. Exercised directly here,
        with no ADaM/NLP pipeline involved. scenario "arena" (DMTestCase's own default)
        declares "wolf" alongside the player, gladstone.
    """

    def test_includes_scene_ground_and_inventory_equipped_items_but_excludes_the_player(self):
        self.dm_core.scenario.setdefault("ground", []).append("a stone")
        self.dm_core.entities["gladstone"]["inventory"] = ["a rope"]
        self.dm_core.entities["gladstone"]["equipped"] = {"main_hand": "a dagger"}

        reachable = self.dm_core._reachable_entity_names()

        self.assertIn("wolf", reachable)
        self.assertIn("a stone", reachable)
        self.assertIn("a rope", reachable)
        self.assertIn("a dagger", reachable)
        self.assertNotIn("gladstone", reachable)


class TestShopScenario(DMTestCase):
    """!
    @brief End-to-end proof (mocked LLM, no live LM Studio needed) that Rules/Fantasy/
        scenarios/shop.toml's "shopkeeper" can sell "most general goods... despite not being
        defined entities" (the scenario this file exists to exercise) -- TARGET_CENTRIC_INTENTS'
        own "trade" handling in DM_Improvisation.py. "dagger" is the shopkeeper's one real,
        hand-authored good (shop.toml's own local shopkeeper entity), kept deliberately sparse
        so this test can show both the ordinary trade path and the ad hoc one working side by
        side.
    """
    scenario_name = "shop"

    def setUp(self):
        super().setUp()
        self.item_events = self._capture("item_interaction_resolved")

    def test_buying_a_real_pre_authored_good_still_works_normally(self):
        self.dm_core._on_item_interaction_detected({
            "intent": "trade", "item_name": "dagger", "input": "buy the dagger",
        })
        result = self.item_events[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["price"], 8)
        self.assertIn("dagger", self.dm_core.entities["gladstone"]["inventory"])

    def test_buying_an_undefined_general_good_conjures_it_into_the_shopkeepers_stock(self):
        fake_result = {
            "created": True,
            "location": "ground",  # deliberately ignored for "trade" -- see DM_Improvisation.py
            "entity": {
                "name": "coil of rope", "supertype": "object", "subtype": "tool",
                "description": "A sturdy coil of hempen rope, fifty feet long.",
                "value": 5, "ad_hoc": True,
            },
        }
        with patch("DM_Improvisation.generate_ad_hoc_item", return_value=fake_result):
            self.dm_core._on_improvisation_requested({
                "intent": "trade", "phrase": "a coil of rope", "input": "buy some rope",
            })

        result = self.item_events[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["price"], 5)
        self.assertIn("coil of rope", self.dm_core.entities["gladstone"]["inventory"])
        self.assertNotIn("coil of rope", self.dm_core.entities["shopkeeper"]["inventory"])
        self.assertNotIn("coil of rope", self.dm_core._current_ground_items())

    def test_buying_an_undefined_good_while_too_poor_still_gates_on_price(self):
        self.dm_core.entities["gladstone"]["currency"] = 0
        fake_result = {
            "created": True, "location": "ground",
            "entity": {
                "name": "lantern", "supertype": "object", "subtype": "tool",
                "description": "A dented tin lantern.", "value": 12, "ad_hoc": True,
            },
        }
        with patch("DM_Improvisation.generate_ad_hoc_item", return_value=fake_result):
            self.dm_core._on_improvisation_requested({
                "intent": "trade", "phrase": "a lantern", "input": "buy a lantern",
            })

        result = self.item_events[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "cant_afford")
        # Still conjured into the shopkeeper's own stock even though the purchase itself was
        # denied -- a later "buy the lantern" (once the player can afford it) should find it
        # waiting rather than needing to be improvised a second time.
        self.assertIn("lantern", self.dm_core.entities["shopkeeper"]["inventory"])

    def test_nothing_to_buy_when_no_target_is_present(self):
        self.dm_core.scenario_entities = ["gladstone"]  # shopkeeper stepped out
        not_understood = self._capture("action_not_understood")

        with patch("DM_Improvisation.generate_ad_hoc_item") as mock_generate:
            self.dm_core._on_improvisation_requested({
                "intent": "trade", "phrase": "a lantern", "input": "buy a lantern",
            })

        mock_generate.assert_not_called()  # short-circuits before ever asking the LLM
        self.assertEqual(len(not_understood), 1)

    def test_implausible_purchase_declines(self):
        not_understood = self._capture("action_not_understood")
        with patch("DM_Improvisation.generate_ad_hoc_item", return_value={"created": False, "reason": "declined"}):
            self.dm_core._on_improvisation_requested({
                "intent": "trade", "phrase": "the moon", "input": "buy the moon",
            })

        self.assertEqual(len(not_understood), 1)
        self.assertEqual(self.item_events, [])


class TestNpcGenerationDMCoreIntegration(DMTestCase):
    """!
    @brief _instance_entities' entity_template branch (DM_Rules.py/DM_NpcGeneration.py)
        against npc_generation_test.toml's own local "generated_stranger" entity_template --
        no live LLM, NPC_Generation._real_call_chat_completion is patched with a deterministic
        fake so this stays part of the fast offline suite (see test_integration.py for a real
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
        disposition, trust, confidence, respect, obligation, intimacy = default
        self.assertTrue(-40 <= disposition <= 40)
        self.assertEqual(trust, 0)
        self.assertEqual(confidence, 0)
        self.assertEqual(respect, 10)
        self.assertEqual(obligation, -20)
        self.assertTrue(-40 <= intimacy <= 40)

    def test_player_attitude_token_is_substituted_with_the_live_player_name(self):
        # npc_generation_test.toml's own generated_stranger authors this override toward the
        # literal token "player" -- it must resolve to whichever entity is actually
        # is_player = true (gladstone), not stay keyed to a string no live entity is ever named.
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
        # module default, since its own template doesn't override it), so it should
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
        # scenario_entity_test.toml's own "vault_specter_stub" is a real entity_template that
        # its own [scenario].entities deliberately never references (see
        # TestScenarioLocalEntities) -- unlike self.dm_core (this class's own
        # npc_generation_test fixture, which already has a *live*, generated
        # "generated_stranger" instance sitting in self.entities under that same key --
        # self.entities holds templates and live instances under the same keys, see CLAUDE.md's
        # "Scenarios and rooms"), self.entities here has no "vault_specter_stub" at all -- only
        # self.entity_templates does, proving the lookup itself is what's isolated, not just
        # that this particular scenario never happens to collide.
        dm = DMCore(EventBus(), scenario_name="scenario_entity_test")
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
            # Collides with creatures.toml's own "fire elemental" -- has to be an entity
            # loaded via load_rules, not character_test.toml's own local "wolf": the rename
            # collision check (apply_character_creation) runs before load_scenario_definition,
            # so a scenario-local entity isn't visible to it yet (see CLAUDE.md's "Character
            # creation").
            "name": "fire elemental",
        }

        dm = DMCore(bus, scenario_name="character_test", character=character)

        self.assertEqual(dm.player_name, "gladstone")  # rename rejected
        self.assertEqual(dm.entities["gladstone"]["skills"]["arcane"], {"dice": 8, "pips": 0})
        # untouched, not clobbered
        self.assertEqual(dm.entities["fire elemental"]["supertype"], "creature")
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


class TestScenarioLocalEntities(unittest.TestCase):
    """!
    @brief A scenario file's own [[entity]]/[[entity_template]] tables (DM_Rules.py's
        load_scenario_definition) -- lets a scenario-specific entity/NPC-generation stub live
        in the same file as the scenario that references it, instead of needing to be authored
        into a shared file like creatures.toml. Uses
        Rules/Fantasy/scenarios/scenario_entity_test.toml, whose "vault sentinel" entity and
        "vault_specter_stub" template exist nowhere else -- if load_scenario_definition didn't
        load them, [scenario].entities' reference to the former would fail with "unknown
        entity" and never make it into scenario_entities at all, and the latter would be
        entirely absent from self.entity_templates.
    """

    def test_scenario_local_entity_is_loaded_and_instanced(self):
        dm = DMCore(EventBus(), scenario_name="scenario_entity_test")

        self.assertEqual(dm.entities["vault sentinel"]["max_hp"], 10)
        self.assertEqual(dm.entities["vault sentinel"]["supertype"], "creature")
        self.assertIn("vault sentinel", dm.scenario_entities)

    def test_scenario_local_entity_template_is_loaded(self):
        dm = DMCore(EventBus(), scenario_name="scenario_entity_test")

        self.assertEqual(dm.entity_templates["vault_specter_stub"]["subtype"], "undead")
        # A stub template -- never instanced (not referenced by [scenario]/[[room]] entities),
        # so it must never show up in self.entities alongside real, directly usable entities.
        self.assertNotIn("vault_specter_stub", dm.entities)


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
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "finesse"}], "input": "I pick the lock"})

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
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "finesse"}], "input": "I pick the lock"})

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
        self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "finesse"}], "input": "I pick the lock"})

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

    def test_examine_an_item_already_in_inventory_ignores_an_unrelated_locked_target(self):
        # The chest (this scenario's own default scene target) stays locked and untouched --
        # proves source-resolution checks the player's own inventory *before* the locked-target
        # gate, not just when there's no target at all. Without that ordering, an ad hoc item
        # placed straight into inventory (DM_Improvisation.py) would wrongly report "locked"
        # whenever a locked container happened to be the scene's current default target.
        self.dm_core.entities["pocket lint"] = {
            "name": "pocket lint", "supertype": "object", "description": "A bit of pocket lint.",
        }
        self.dm_core.place_new_item("gladstone", "pocket lint")

        self.dm_core._on_item_interaction_detected({
            "intent": "examine", "item_name": "pocket lint", "input": "examine the pocket lint",
        })

        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertIsNone(result["container"])
        self.assertEqual(result["description"], "A bit of pocket lint.")
        self.assertIn("cursed dagger", self.dm_core.entities["chest"]["inventory"])  # untouched


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
        self.dm_core._on_turn_detected({
            "clauses": [{"kind": "action", "skill": "arcane", "target": "cursed dagger"}],
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


class TestOpenClose(DMTestCase):
    scenario_name = "dungeon"

    def setUp(self):
        super().setUp()
        self.resolved = self._capture("item_interaction_resolved")

    def _unlock_the_chest(self):
        self.dm_core.roll_dice = lambda dice, pips: 99
        self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "finesse"}], "input": "I pick the lock"})

    def _open(self):
        self.dm_core._on_item_interaction_detected({
            "intent": "open", "item_name": None, "input": "I open the chest",
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

    def test_using_a_poisonous_item_deals_real_damage(self):
        # Same {dice, pips} skill-stat shape as "healing", just routed through calculate_damage/
        # apply_damage instead of apply_healing -- see DM_Improvisation.py's own module notes on
        # why an ad hoc-conjured consumable can be marked poisonous instead of a free heal.
        self.dm_core.entities["nasty brew"] = {
            "name": "nasty brew", "supertype": "object", "subtype": "potion",
            "description": "A vial of something that smells wrong.", "usable": True,
            "skills": {"poison": {"dice": 2, "pips": 0}},
        }
        self.dm_core.entities["gladstone"]["inventory"].append("nasty brew")
        starting_hp = self.dm_core.get_current_hp("gladstone")

        result = self._use(item_name="nasty brew", roll_result=7)

        self.assertTrue(result["found"])
        self.assertEqual(result["healed"], 0)
        self.assertEqual(result["poisoned"], 7)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), starting_hp - 7)
        self.assertEqual(result["remaining_hp"], starting_hp - 7)
        self.assertNotIn("nasty brew", self.dm_core.entities["gladstone"]["inventory"])

    def test_poison_immunity_negates_the_damage_entirely(self):
        # calculate_damage's own immunity_tags check applies here exactly like a real attack --
        # a poison-conjured item isn't a special case that bypasses it.
        self.dm_core.entities["gladstone"]["immunity_tags"] = ["poison"]
        self.dm_core.entities["toxic vial"] = {
            "name": "toxic vial", "supertype": "object", "subtype": "potion",
            "description": "A small vial of venom.", "usable": True,
            "skills": {"poison": {"dice": 3, "pips": 0}},
        }
        self.dm_core.entities["gladstone"]["inventory"].append("toxic vial")
        starting_hp = self.dm_core.get_current_hp("gladstone")

        result = self._use(item_name="toxic vial", roll_result=10)

        self.assertEqual(result["poisoned"], 0)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), starting_hp)


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
    # Rules/Fantasy/scenarios/tavern.toml puts the player with a friendly NPC (its own local
    # innkeeper) instead of the default "arena" combat scenario.
    scenario_name = "tavern"

    def setUp(self):
        super().setUp()
        self.action_events = self._capture("action_resolved")
        self.round_events = self._capture("round_resolved")


    def test_talking_to_the_innkeeper_narrates_immediately_as_dialogue(self):
        self.dm_core._on_turn_detected({
            "clauses": [{"kind": "action", "skill": "charisma"}],
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
        # "fire elemental" (creatures.toml) rather than "wolf" -- it's the one creature still
        # loaded via load_rules regardless of scenario, so it's resolvable here even though
        # this fixture boots "tavern" (which never references it).
        self.dm_core.scenario = {
            "entities": [
                { "name": "gladstone", "band": 1 },
                { "name": "fire elemental", "band": 1 },
            ],
        }
        self.dm_core.load_scenario()

        self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "blades"}], "input": "I attack the fire elemental"})

        self.assertEqual(len(self.round_events), 1)
        self.assertEqual(self.action_events, [])
        self.assertEqual(self.round_events[0]["round"], 1)


class TestFreeformDialogue(DMTestCase):
    """!
    @brief DM_Dialogue.py's DialogueMixin -- the new diceless "directly address someone"
        channel, distinct from TestNpcDialogue above (which is the pre-existing, still-valid
        charisma skill check path). scenario "tavern" puts the player with a friendly NPC
        (its own local innkeeper), same fixture TestNpcDialogue itself uses.
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
        # hostile (ex: shouting at a wolf mid-fight) is allowed. "fire elemental" is already
        # loaded as a template (creatures.toml, via load_rules, regardless of scenario) even
        # though tavern.toml never instances it -- just needs to be added to the live scene
        # for this one check.
        self.dm_core.scenario_entities.append("fire elemental")
        self.assertTrue(self.dm_core.is_hostile("fire elemental", self.dm_core.player_name))

        result = self._talk("i talk to the fire elemental")

        self.assertTrue(result["found"])
        self.assertEqual(result["target"], "fire elemental")

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


class TestHelpChannel(DMTestCase):
    """!
    @brief DM_Help.py's HelpMixin -- the reserved "ADaM" out-of-character help channel. Unlike
        TestFreeformDialogue above, there's no "not found"/"denied" case to cover -- ADaM isn't
        a scene entity, so _on_help_detected always resolves.
    """
    scenario_name = "arena"

    def setUp(self):
        super().setUp()
        self.help_events = self._capture("help_resolved")

    def _ask(self, input_text="adam, help me"):
        self.dm_core._on_help_detected({"input": input_text})
        return self.help_events[-1]

    def test_reports_the_players_own_skills_and_gear(self):
        result = self._ask()

        self.assertIn("longsword", result["equipped"].values())
        self.assertIn("health potion", result["inventory"])
        self.assertTrue(any(entry.startswith("blades:") for entry in result["skills"]))
        self.assertTrue(any(entry.startswith("fireball") for entry in result["abilities"]))

    def test_reports_the_current_scene_and_present_entities(self):
        result = self._ask()

        self.assertTrue(result["scene_name"])
        self.assertTrue(result["scene_description"])
        self.assertEqual(result["present"], self.dm_core._describe_scenario_characters())

    def test_no_exits_in_a_flat_single_room_scenario(self):
        result = self._ask()
        self.assertEqual(result["exits"], [])

    def test_input_and_presence_snapshot_are_carried_through(self):
        result = self._ask("adam, what can i do")
        self.assertEqual(result["input"], "adam, what can i do")
        self.assertEqual(set(result["present_entities"]), set(self.dm_core.scenario_entities))


class TestHelpChannelExits(DMTestCase):
    """!
    @brief Multi-room-dungeon side of HelpMixin -- exits are only meaningful when self.rooms
        is populated (see _describe_available_exits), so this is exercised separately against
        "crypt" rather than folded into TestHelpChannel's own flat-scenario fixture.
    """
    scenario_name = "crypt"

    def test_lists_the_current_rooms_own_exits_with_friendly_destination_names(self):
        self.help_events = self._capture("help_resolved")
        self.dm_core._on_help_detected({"input": "adam, where can i go"})

        result = self.help_events[-1]
        # "entrance" (the starting room) has exactly one exit, "forward" to "hall_of_webs" --
        # whose own room name ("The Hall of Webs") should be reported, not the raw room key.
        self.assertEqual(result["exits"], [{"direction": "forward", "destination_name": "The Hall of Webs"}])


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
        [entity.attitudes] table at all (ex: arena.toml's wolf/field.toml's bandit) is hostile
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
        # of the whole template (ex: no "skills"/"max_hp" keys, which never change
        # post-instancing today). "equipped" *is* included -- see
        # test_equipped_slot_mapping_round_trips_through_save_load below for why.
        slot = self._track("test_save_writes_diff")
        self.dm_core.save_game(slot)
        data = self._read_dm_state(slot)

        self.assertEqual(data["scenario_key"], "arena")
        self.assertEqual(data["player_name"], "gladstone")
        self.assertEqual(data["scenario_entities"], self.dm_core.scenario_entities)
        gladstone_state = data["instances"]["gladstone"]
        self.assertEqual(
            set(gladstone_state.keys()),
            {"hp", "active_conditions", "currency", "inventory", "equipped", "band"},
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


    def test_equipped_slot_mapping_round_trips_through_save_load(self):
        # gladstone starts with rhand="longsword"/chest="chain mail" (characters.toml). Without
        # this fix, a reload always re-derives "equipped" from that static template mapping,
        # silently re-equipping the longsword regardless of what was actually equipped at save
        # time -- so this test unequips it first, proving the *cleared* slot survives a reload
        # rather than snapping back to the template's own default.
        slot = self._track("test_equipped_round_trip")
        self.dm_core._on_item_interaction_detected({
            "intent": "unequip", "item_name": "longsword", "input": "I unequip the longsword",
        })
        self.assertEqual(self.dm_core.entities["gladstone"]["equipped"], {"chest": "chain mail"})
        self.dm_core.save_game(slot)

        fresh_dm = DMCore(EventBus(), scenario_name="arena")  # boots with the template default
        fresh_dm.load_game(slot)

        self.assertEqual(fresh_dm.entities["gladstone"]["equipped"], {"chest": "chain mail"})
        self.assertIn("longsword", fresh_dm.entities["gladstone"]["inventory"])


    def test_dropped_items_round_trip_through_save_load(self):
        # arena is a plain single-room scenario, so ground state lives on self.scenario
        # directly (a flat list), not per-room -- see TestMultiRoomSaveLoad's own version of
        # this test for the per-room dict shape a dungeon uses instead.
        slot = self._track("test_ground_round_trip")
        self.dm_core._on_item_interaction_detected({
            "intent": "drop", "item_name": "health potion", "input": "I drop a health potion",
        })
        self.assertIn("health potion", self.dm_core._current_ground_items())
        self.dm_core.save_game(slot)

        fresh_dm = DMCore(EventBus(), scenario_name="arena")  # boots with an empty ground list
        self.assertEqual(fresh_dm._current_ground_items(), [])
        fresh_dm.load_game(slot)

        self.assertEqual(fresh_dm._current_ground_items(), ["health potion"])
        self.assertEqual(fresh_dm.entities["gladstone"]["inventory"].count("health potion"), 2)


    def test_ad_hoc_edited_description_round_trips_through_save_load(self):
        # "description" doesn't otherwise round-trip for a hand-authored, non-ad_hoc/
        # non-generated entity (it just re-derives from the static template on reload) --
        # DM_Improvisation.py's _attempt_entity_edit tags entity["edited"] = True specifically
        # so save_game knows to persist it explicitly instead of silently reverting.
        slot = self._track("test_edited_round_trip")
        self.dm_core.entities["wolf"]["description"] = "A scarred, one-eyed wolf."
        self.dm_core.entities["wolf"]["edited"] = True
        self.dm_core.save_game(slot)

        fresh_dm = DMCore(EventBus(), scenario_name="arena")  # boots with the template's own description
        fresh_dm.load_game(slot)

        self.assertEqual(fresh_dm.entities["wolf"]["description"], "A scarred, one-eyed wolf.")
        self.assertTrue(fresh_dm.entities["wolf"]["edited"])


    def test_ad_hoc_entity_round_trips_through_save_load(self):
        # An ad hoc entity (DM_Improvisation.py) has no static TOML template to re-derive
        # anything from on reload -- unlike every other saved instance, its *complete* dict has
        # to be saved and restored, not just a diff.
        slot = self._track("test_ad_hoc_round_trip")
        entity = {
            "name": "stone", "supertype": "object", "subtype": "misc",
            "description": "A smooth grey stone.", "value": 0, "ad_hoc": True,
        }
        self.dm_core.entities["stone"] = entity
        self.dm_core._current_ground_items().append("stone")
        self.dm_core.save_game(slot)

        fresh_dm = DMCore(EventBus(), scenario_name="arena")
        self.assertNotIn("stone", fresh_dm.entities)
        fresh_dm.load_game(slot)

        self.assertEqual(fresh_dm.entities["stone"], entity)
        self.assertIn("stone", fresh_dm._current_ground_items())

    def test_removed_entity_does_not_respawn_after_save_load(self):
        slot = self._track("test_removed_entity_round_trip")
        self.dm_core.remove_entity_from_scene("wolf")
        self.assertNotIn("wolf", self.dm_core.scenario_entities)
        self.dm_core.save_game(slot)

        fresh_dm = DMCore(EventBus(), scenario_name="arena")  # boots with "wolf" freshly instanced
        self.assertIn("wolf", fresh_dm.scenario_entities)
        fresh_dm.load_game(slot)

        self.assertNotIn("wolf", fresh_dm.scenario_entities)
        self.assertIn("wolf", fresh_dm.removed_entities)
        self.assertIn("wolf_2", fresh_dm.scenario_entities)


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
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "finesse"}], "input": "I try to disarm the trap"})

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
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "finesse"}], "input": "I try again"})
        self.assertEqual(self.action_events[-1]["actions"][0]["difficulty"], 0)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), hp_after_first_hit)


    def test_forward_succeeds_once_the_player_reaches_the_exit_band(self):
        with patch("random.randint", return_value=6):
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "finesse"}], "input": "I disarm the trap"})
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


    def test_dropped_items_round_trip_per_room(self):
        # An item dropped in a room the player has since left has to be saved/restored keyed
        # to *that* room specifically -- not the room the player is standing in when they save
        # -- since _current_ground_items() always reads/writes the current room's own "ground"
        # key (DM_Inventory.py).
        slot = self._track("test_crypt_ground_round_trip")
        self.dm_core._on_item_interaction_detected({
            "intent": "drop", "item_name": "health potion", "input": "I drop a health potion",
        })
        self.assertEqual(self.dm_core.rooms["entrance"].get("ground"), ["health potion"])
        self.dm_core.enter_room("hall_of_webs")
        self.dm_core.save_game(slot)

        fresh_dm = DMCore(EventBus(), scenario_name="crypt")  # boots back at "entrance"
        fresh_dm.load_game(slot)

        self.assertEqual(fresh_dm.rooms["entrance"].get("ground"), ["health potion"])
        self.assertEqual(fresh_dm.current_room_key, "hall_of_webs")
        self.assertEqual(fresh_dm.entities["gladstone"]["inventory"].count("health potion"), 2)


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


class TestAdamNarration(LLMTestCase):
    """!
    @brief LLMCore's own side of DM_Help.py's channel: generate_adam_response/
        _build_adam_system_message/_queue_adam_response. The load-bearing property under test
        is the isolation guarantee -- unlike every other narration trigger, an ADaM exchange
        must never touch context_window at all (see LLM_Core.py's own module notes for why).
    """

    def _help_payload(self, **overrides):
        payload = {
            "input": "what are my skills",
            "present_entities": ["gladstone"],
            "skills": ["blades: 5D+0", "finesse: 3D+0"],
            "abilities": ["fireball: A ball of fire."],
            "equipped": {"rhand": "longsword"},
            "inventory": ["longsword", "health potion"],
            "scene_name": "The Arena",
            "scene_description": "A large arena.",
            "present": ["gladstone - A man"],
            "exits": [{"direction": "forward", "destination_name": "The Hall of Webs"}],
        }
        payload.update(overrides)
        return payload

    def test_publishing_help_resolved_never_touches_context_window(self):
        # No thread/network mocking needed -- _queue_adam_response never appends to
        # context_window at all, synchronously, before the background thread even starts (the
        # same style TestFreeformDialogueNarration already uses to assert dialogue's own
        # pre-fetch context_window append, just proving the opposite here).
        self.assertEqual(self.llm_core.context_window, [])

        self.event_bus.publish("help_resolved", self._help_payload())

        self.assertEqual(self.llm_core.context_window, [])

    def test_system_message_includes_general_guidance_and_the_live_payload(self):
        message = self.llm_core._build_adam_system_message(self._help_payload(), rag_query=None)

        self.assertIn("ADaM", message)
        self.assertIn("out-of-character", message)
        # General command guidance -- the actual onboarding gap this persona closes.
        self.assertIn("equip/wear", message)
        self.assertIn("save/load", message)
        # The live, dynamic payload.
        self.assertIn("blades: 5D+0", message)
        self.assertIn("fireball: A ball of fire.", message)
        self.assertIn("longsword", message)
        self.assertIn("The Arena", message)
        self.assertIn("The Hall of Webs", message)

    def test_system_message_mentions_a_creature_conjured_this_turn(self):
        payload = self._help_payload(created_creature={"created_creature": True, "name": "cave rat"})
        message = self.llm_core._build_adam_system_message(payload, rag_query=None)
        self.assertIn("cave rat", message)
        self.assertIn("conjured", message)

    def test_system_message_mentions_an_edit_made_this_turn(self):
        payload = self._help_payload(edited={"edited": True, "name": "wolf", "reason": "player asked"})
        message = self.llm_core._build_adam_system_message(payload, rag_query=None)
        self.assertIn("wolf", message)
        self.assertIn("edited", message)

    def test_fetch_still_publishes_llm_response_ready_without_storing_in_context(self):
        # The real fetch path (threading.Thread + urllib.request.urlopen mocked, same style as
        # TestLlmDebugEvent) confirms _fetch_and_publish's new store_in_context=False actually
        # skips the append on the success path too, not just before the thread starts.
        response_events = []
        self.event_bus.subscribe("llm_response_ready", response_events.append)
        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps(
            {"choices": [{"message": {"content": "You know blades and finesse."}}]}
        ).encode("utf-8")

        with patch("threading.Thread") as mock_thread, \
             patch("urllib.request.urlopen", return_value=fake_response):
            self.event_bus.publish("help_resolved", self._help_payload())
            mock_thread.call_args.kwargs["target"]()

        self.assertEqual(response_events, ["You know blades and finesse."])
        self.assertEqual(self.llm_core.context_window, [])


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
