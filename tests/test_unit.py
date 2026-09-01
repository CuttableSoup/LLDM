import asyncio
import json
import os
import shutil
import tempfile
import threading
import tkinter as tk
import tomllib
import unittest
import zipfile
from typing import get_args
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from sentence_transformers import SentenceTransformer

import resolution.Combat_Resolution as Combat_Resolution
import resolution.Social_Resolution as Social_Resolution
from resolution.Program_Interpreter import evaluate_condition, run_program
from resolution.Character_Creation import (
    build_character_skills,
    get_race,
    load_character_creation_data,
    race_baseline_skills,
    validate_allocation,
)
from resolution.AdHoc_Generation import (
    decide_entity_edit,
    decide_entity_removal,
    generate_ad_hoc_creature,
    generate_ad_hoc_item,
)
from gui.Character_Creation_GUI import CharacterCreationDialog
from resolution.Challenge_Rating import calculate_challenge_rating, calculate_party_challenge_rating, skill_rating
from dm.DM_ActionOutcome import (
    ActionOutcome, CraftEffect, DamageEffect, DefenderDetailsEffect, LanguageBarrierOutcome,
    LootEffect, MissingMaterialsOutcome, MissingSpellMaterialsOutcome, MissingStationOutcome,
    MovementOutcome, NotCraftableOutcome, OutOfRangeOutcome, RevealEffect, RolledOutcome,
    SummonEffect,
)
from dm.DM_Core import DMCore
from dm.DM_Rules import list_available_scenarios
from dm.DM_Social import TALK_ATTITUDE_DRIFT_CAP, ACTION_ATTITUDE_DRIFT_CAP
from Event_Bus import EventBus
from gui.GUI_Core import GUICore
from paths import PROJECT_ROOT
from nlp.Intent_Classification import (
    ADVANCE_KEYWORDS,
    CLOSE_KEYWORDS,
    CRAFT_KEYWORDS,
    DIALOGUE_KEYWORDS,
    DROP_KEYWORDS,
    EQUIP_KEYWORDS,
    EXAMINE_KEYWORDS,
    FORMATION_ABREAST_KEYWORDS,
    FORMATION_BEHIND_KEYWORDS,
    GIVE_KEYWORDS,
    OPEN_KEYWORDS,
    RETREAT_KEYWORDS,
    SPEAK_LANGUAGE_KEYWORDS,
    TAKE_KEYWORDS,
    TRADE_KEYWORDS,
    TRAVEL_KEYWORDS,
    UNEQUIP_KEYWORDS,
    USE_KEYWORDS,
    IntentClassifier,
    _phrase_matches,
    detect_dialogue_intent,
    detect_help_intent,
    detect_item_intent,
    detect_save_load_intent,
    split_action_clauses,
)
import LLDM
from llm.LLM_Core import LLMCore, _OUTCOME_FORMATTERS
import llm.Ollama_Launcher as Ollama_Launcher
from llm.Ollama_Launcher import ensure_ollama_running
from llm.LLM_Rag import RagIndex
from nlp.NLP_Core import NLPCore
from resolution.NPC_Generation import (
    _describe_qualities,
    fit_skills_to_cr,
    generate_npc_stats,
    load_npc_keywords,
    resolve_varied_value,
)
from gui.Textual_Core import TextualCore
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

    def _stub_roll_dice(self, roll_result):
        """Forces every dice roll anywhere in the resolution graph (Combat_Resolution.py) to
        the same flat total, regardless of dice/pips -- the same convenience a bare
        `self.dm_core.roll_dice = lambda ...` gave back when roll_dice was still a DMCore
        method every call site reached through self. Patched at the module level since
        Combat_Resolution.py's own internal callers (resolve_action, calculate_damage, ...)
        now call the bare module function directly, not self.roll_dice -- restored via
        addCleanup so it can't leak into a later test."""
        original = Combat_Resolution.roll_dice
        Combat_Resolution.roll_dice = lambda dice, pips: roll_result
        self.addCleanup(setattr, Combat_Resolution, "roll_dice", original)

    def _load_ad_hoc_scenario(self, entities, bands=None, enclosed=True):
        """Swaps in a throwaway [[location]] (freeform if bands is None, else one
        [[location.room]] with the given bands/enclosed) and loads it -- the ad-hoc-scenario
        equivalent of directly authoring a scenario TOML file, for a test that just needs a
        specific, minimal entity roster rather than any of the real shipped scenarios. Mirrors
        DM_Rules.py's own [[location]] shape exactly, just built in Python instead of TOML."""
        if bands is None:
            location = {"key": "ad_hoc", "entities": entities}
        else:
            location = {
                "key": "ad_hoc", "start_room": "ad_hoc_room",
                "rooms": {"ad_hoc_room": {"key": "ad_hoc_room", "bands": bands, "enclosed": enclosed, "entities": entities}},
            }
        self.dm_core.locations = {"ad_hoc": location}
        self.dm_core.scenario = {"start_location": "ad_hoc"}
        self.dm_core.load_scenario()

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

    def test_sentiment_classification_via_nli_zero_shot_classifier(self):
        # classify_sentiment is backed by a separate NLI (natural-language-inference) pipeline
        # (NLI_MODEL_NAME, scored zero-shot against SENTIMENT_CANDIDATE_LABELS), not this class's
        # own embedding model, so this needs no DMCore/rules load at all -- just NLPCore itself,
        # constructed the same way every other real-model test here does rather than
        # instantiating SentenceTransformerMatcher directly.
        nlp_core = NLPCore(EventBus())

        hostile_label, hostile_score = nlp_core.matcher.classify_sentiment("I hate you and never want to see you again")
        warm_label, warm_score = nlp_core.matcher.classify_sentiment("thank you so much, you have been wonderful and I am truly grateful")
        informational_label, _score = nlp_core.matcher.classify_sentiment("how far is it to the next town")
        # A lexicon-based analyzer (this project's earlier VADER-backed implementation) reads
        # this as flat neutral -- no single word here is in its dictionary. A model that actually
        # understands language has to generalize compositionally to catch it, which is the whole
        # reason this class was swapped in over VADER.
        curt_dismissal_label, _score = nlp_core.matcher.classify_sentiment("get out of my sight")
        # The zero-shot pipeline's own bare-default labels/hypothesis template misread plain
        # informational questions like this one as negative/positive -- SENTIMENT_CANDIDATE_LABELS/
        # SENTIMENT_HYPOTHESIS_TEMPLATE were specifically tuned to fix this; this assertion is
        # what actually guards the regression, not the "how far..." case above (which happened
        # to pass even under the untuned default).
        another_informational_label, _score = nlp_core.matcher.classify_sentiment("do you know where the blacksmith is")

        self.assertEqual(hostile_label, "negative")
        self.assertEqual(warm_label, "positive")
        self.assertEqual(curt_dismissal_label, "negative")
        self.assertGreaterEqual(hostile_score, nlp_core.matcher.sentiment_confidence_threshold)
        self.assertGreaterEqual(warm_score, nlp_core.matcher.sentiment_confidence_threshold)
        self.assertIsNone(informational_label)
        self.assertIsNone(another_informational_label)


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

    def __init__(self, actions=None, items=None, targets=None, sentiments=None, threats=None, familiarities=None):
        self._actions = actions or {}
        self._items = items or {}
        self._targets = targets or {}
        self._sentiments = sentiments or {}
        self._threats = threats or {}
        self._familiarities = familiarities or {}

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

    def classify_sentiment(self, processed_text):
        return self._sentiments.get(processed_text, (None, 0.0))

    def classify_threat(self, processed_text):
        return self._threats.get(processed_text, (None, 0.0))

    def classify_familiarity(self, processed_text):
        return self._familiarities.get(processed_text, (None, 0.0))


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

    def test_craft_keyword_resolves_to_an_item_kind_clause(self):
        # Detected exactly like "give"/"take" (same map_to_item lookup, matching over every
        # known object-supertype entity regardless of scene presence -- see NLP_Core.py's own
        # item-catalog build) -- resolution (DM_Crafting.py) is what actually rolls dice for it.
        classifier = IntentClassifier(FakeMatcher(items={"craft an iron dagger": ("iron dagger", 0.9)}))
        _processed, events = classifier.classify("craft an iron dagger")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "turn_detected")
        self.assertEqual(
            events[0]["payload"]["clauses"], [{"kind": "item", "intent": "craft", "item_name": "iron dagger"}],
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
        self.assertEqual(
            events,
            [{
                "event": "dialogue_detected",
                "payload": {
                    "input": "talk to the wolf", "score": None, "sentiment": None, "sentiment_score": 0.0,
                    "threat_sentiment": None, "threat_score": 0.0,
                    "familiarity_sentiment": None, "familiarity_score": 0.0,
                },
            }],
        )

    def test_dialogue_detected_carries_the_matcher_own_sentiment_classification(self):
        # classify_sentiment is only ever called once dialogue is confirmed detected -- the
        # matcher's own canned (label, score) result for the processed input rides along on the
        # same event, not a second round trip DMCore would have to fetch separately. The score
        # matters just as much as the label now -- DM_Social.py's nudge_attitude scales the
        # actual attitude drift by it (see CLAUDE.md's "Dialogue sentiment").
        classifier = IntentClassifier(FakeMatcher(sentiments={"talk to the innkeeper, thank you": ("positive", 0.7)}))
        _processed, events = classifier.classify("talk to the innkeeper, thank you")
        self.assertEqual(events[0]["payload"]["sentiment"], "positive")
        self.assertEqual(events[0]["payload"]["sentiment_score"], 0.7)

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
        # phrase this file declares should actually *match* (via _phrase_matches -- the same
        # word-boundary check every real gate uses, not a raw substring test) a plain sentence
        # that merely uses a real skill's own keyword as a whole word -- otherwise a sentence
        # clearly about that skill could get silently swallowed by item/dialogue detection,
        # which always runs first. Skill keywords are checked space-padded (" {keyword} "), the
        # minimal sentence context a keyword could plausibly appear in.
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
            "CRAFT_KEYWORDS": CRAFT_KEYWORDS,
            "OPEN_KEYWORDS": OPEN_KEYWORDS, "CLOSE_KEYWORDS": CLOSE_KEYWORDS,
            "ADVANCE_KEYWORDS": ADVANCE_KEYWORDS, "RETREAT_KEYWORDS": RETREAT_KEYWORDS,
            "FORMATION_BEHIND_KEYWORDS": FORMATION_BEHIND_KEYWORDS,
            "FORMATION_ABREAST_KEYWORDS": FORMATION_ABREAST_KEYWORDS,
            "DIALOGUE_KEYWORDS": DIALOGUE_KEYWORDS,
            "TRAVEL_KEYWORDS": TRAVEL_KEYWORDS,
            "SPEAK_LANGUAGE_KEYWORDS": SPEAK_LANGUAGE_KEYWORDS,
        }
        # No known exceptions remain: appraise's own skills.toml keywords deliberately exclude
        # "examine" (EXAMINE_KEYWORDS' own item-detection word, checked first) precisely so this
        # matrix can be a real, unconditional invariant rather than needing a carve-out for a
        # skill keyword that could never actually be reached anyway.
        for tuple_name, keyword_tuple in keyword_tuples_by_name.items():
            for phrase in keyword_tuple:
                for skill_keyword in skill_keywords:
                    self.assertFalse(
                        _phrase_matches(phrase, f" {skill_keyword} "),
                        f"{tuple_name}'s {phrase!r} matches a sentence using skill keyword "
                        f"{skill_keyword!r}",
                    )

    def test_dialogue_keyword_no_longer_false_positives_on_a_containing_skill_keyword(self):
        # The regression this fix closes: DIALOGUE_KEYWORDS' "ask " used to be a raw substring
        # of "mask" (disguise's own skills.toml keyword), so a sentence about disguising with a
        # mask -- naming no item-interaction verb at all -- would misfire as dialogue detection
        # before skill matching ever got a chance to run.
        self.assertFalse(detect_dialogue_intent("mask my presence"))
        # The true positive this fix must not have broken in the process.
        self.assertTrue(detect_dialogue_intent("ask the guard about the road"))


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
        result = RolledOutcome(
            entity="gladstone", skill="finesse", roll=18, difficulty=12,
            success=True, defender="chest", effects=[LootEffect(currency=20, items=[])],
            input="I pick the lock",
        )
        description = self.llm_core._describe_outcome(result)
        self.assertIn("20 currency", description)

    def test_describe_outcome_mentions_a_successful_summon(self):
        # Without this, a summoning spell's own roll outcome narrates exactly like an ordinary
        # no-damage opposed check -- nothing tells the LLM a creature actually appeared.
        result = RolledOutcome(
            entity="gladstone", skill="arcane", roll=18, difficulty=12,
            success=True, effects=[SummonEffect(name="spectral wolf")],
            input="I summon a wolf",
        )
        description = self.llm_core._describe_outcome(result)
        self.assertIn("summons spectral wolf", description)

    def test_outcome_formatters_cover_every_actionoutcome_variant(self):
        # A future ActionOutcome variant with no matching _OUTCOME_FORMATTERS entry would only
        # surface as a live KeyError mid-narration -- this catches it as a fast, obvious unit
        # test instead, the same "one new variant per commit" pattern this table exists to keep
        # up with. MovementOutcome is the one deliberate exception -- it has no "input" at all,
        # so it never reaches _OUTCOME_FORMATTERS (see _describe_outcome's own early return).
        for variant in get_args(ActionOutcome):
            if variant is MovementOutcome:
                continue
            self.assertIn(variant, _OUTCOME_FORMATTERS)


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

    def test_language_barrier_dialogue_queues_gibberish_prompt_not_the_players_words(self):
        # DM_Dialogue.py's _detect_language_barrier still resolves "found": True (the target is
        # present and willing to react) but flags language_barrier instead of a normal reply --
        # the queued prompt must steer the model away from actually answering what was asked.
        self.event_bus.publish("dialogue_resolved", {
            "target": "innkeeper", "input": "where is the nearest blacksmith",
            "found": True, "language_barrier": True, "target_language": "dwarvish",
            "nonsense_phrase": "Grunthak dol bregnir uzdum",
            "persona": "innkeeper - A weary tavern keeper.",
            "attitude": "Attitude toward gladstone: is warm and well-disposed toward them.",
            "present_entities": ["gladstone", "innkeeper"],
        })

        prompt = self.llm_core.context_window[-1]["content"]
        self.assertIn("does not understand this at all", prompt)
        self.assertIn("dwarvish", prompt)
        self.assertIn("Grunthak dol bregnir uzdum", prompt)
        # The player's own words are still relayed as context (so the model reacts to *something*
        # being said), but the prompt must not read as an ordinary answerable dialogue turn.
        self.assertIn("invented gibberish", prompt)

    def test_language_barrier_prompt_omits_example_when_no_race_claims_the_language(self):
        prompt = LLMCore._build_language_barrier_prompt(
            "hello", "stranger", "goblin tongue", None,
        )
        self.assertIn("goblin tongue", prompt)
        self.assertNotIn("For phonetic flavor", prompt)

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
        result = {"actions": [RolledOutcome(entity="gladstone", skill="blades", roll=15, difficulty=10, success=True)]}
        description = self.llm_core._describe_player_actions(result)
        self.assertNotIn("splitting their attention", description)
        self.assertIn("Skill used: blades", description)

    def test_two_actions_name_the_shared_penalty_and_describe_both(self):
        result = {"actions": [
            RolledOutcome(entity="gladstone", skill="blades", roll=12, difficulty=10, success=True),
            RolledOutcome(entity="gladstone", skill="finesse", roll=9, difficulty=12, success=False),
        ]}
        description = self.llm_core._describe_player_actions(result)
        self.assertIn("2 actions this turn", description)
        self.assertIn("-1D", description)
        self.assertIn("Skill used: blades", description)
        self.assertIn("Skill used: finesse", description)

    def test_three_actions_name_minus_2d(self):
        result = {"actions": [
            RolledOutcome(entity="gladstone", skill="blades", roll=9, difficulty=10, success=False),
            RolledOutcome(entity="gladstone", skill="finesse", roll=9, difficulty=12, success=False),
            RolledOutcome(entity="gladstone", skill="charisma", roll=9, difficulty=10, success=False),
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


    @patch("random.randint", return_value=3)
    def test_resistance_bypass_tag_skips_the_defenders_own_resistance(self, mock_randint):
        # fire elemental resists ["physical", "piercing", "bludgeoning", "slashing"] at 2D --
        # opt it into a Pathfinder "DR/magic" shape and confirm a magic-tagged hit skips that
        # reduction entirely, while an otherwise-identical mundane hit still gets reduced.
        self.dm_core.entities["fire elemental"]["resistance_bypass_tags"] = ["magic"]

        self.assertEqual(self.dm_core.get_damage_reduction("fire elemental", ["slashing"]), 6)
        self.assertEqual(self.dm_core.get_damage_reduction("fire elemental", ["slashing", "magic"]), 0)


    @patch("random.randint", return_value=3)
    def test_armor_bypass_tag_skips_that_items_own_reduction(self, mock_randint):
        # gladstone has no innate resistance of his own -- chain mail's armor_value/armor_tags
        # is the only source of reduction here, so this isolates the item-side bypass path from
        # get_damage_reduction's own resistance_bypass_tags branch above.
        self.dm_core.entities["chain mail"]["armor_bypass_tags"] = ["magic"]

        self.assertEqual(self.dm_core.get_damage_reduction("gladstone", ["bludgeoning"]), 6)
        self.assertEqual(self.dm_core.get_damage_reduction("gladstone", ["bludgeoning", "magic"]), 0)


    @patch("random.randint", return_value=3)
    def test_wraith_resists_ordinary_weapons_but_silver_bypasses_it(self, mock_randint):
        # creatures.toml's "wraith" is the shipped resistance_bypass_tags example (DR/silver).
        # An ordinary slashing hit is reduced (3D @ 3 each = 9); the same hit tagged "silver"
        # bypasses that reduction entirely, even though "slashing" still matches resistance_tags.
        self.assertEqual(self.dm_core.get_damage_reduction("wraith", ["slashing"]), 9)
        self.assertEqual(self.dm_core.get_damage_reduction("wraith", ["slashing", "silver"]), 0)


    def test_landing_a_hit_nudges_the_defenders_combat_attitude(self):
        # arena's wolf normally has no [entity.attitudes] table at all (unconditionally
        # hostile -- see is_hostile), so it's given one here just for this test; the "combat_hit"
        # nudge (DM_Social.py's nudge_attitude_from_event, wired from _apply_damage_if_hit) is
        # scaled by net_damage / max_hp, not a flat per-swing amount.
        self.dm_core.entities["wolf"]["attitudes"] = {"default": [0, 0, 0]}
        result = RolledOutcome(entity="gladstone", skill="melee", roll=0, difficulty=0, success=True)
        ability = {"damage_value": {"dice": 0, "pips": 0, "bonus": 5}, "damage_tags": []}

        self.dm_core._apply_damage_if_hit(result, "melee", None, ability, "wolf", via_test=False)

        self.assertEqual(result.effects[0].net_damage, 5)
        magnitude = 5 / self.dm_core.entities["wolf"]["max_hp"]
        disposition, threat = (
            self.dm_core.get_attitude("wolf", self.dm_core.player_name)[axis] for axis in (0, 1)
        )
        self.assertAlmostEqual(disposition, -20 * magnitude)
        self.assertAlmostEqual(threat, -15 * magnitude)

    def test_landing_a_hit_bonds_other_entities_hostile_to_the_same_target(self):
        # "Bonds made on the battlefield" (DM_Core.py's _nudge_shared_enemy_bonds) -- an
        # onlooker who already considers "wolf" an enemy (a name-override disposition <= -100
        # toward it specifically, not just a generic hostile-to-everyone default) warms up
        # toward the player when the player hits it, scaled by the same magnitude as the
        # target's own "combat_hit" nudge. thane is arena's real ally entity (is_party = true),
        # already present in scenario_entities and alive.
        self.dm_core.entities["thane"]["attitudes"] = {
            "default": [0, 0, 0],
            "name": [{"wolf": [-100, 0, 0]}],
        }
        result = RolledOutcome(entity="gladstone", skill="melee", roll=0, difficulty=0, success=True)
        ability = {"damage_value": {"dice": 0, "pips": 0, "bonus": 5}, "damage_tags": []}

        self.dm_core._apply_damage_if_hit(result, "melee", None, ability, "wolf", via_test=False)

        magnitude = result.effects[0].net_damage / self.dm_core.entities["wolf"]["max_hp"]
        disposition = self.dm_core.get_attitude("thane", self.dm_core.player_name)[0]
        self.assertAlmostEqual(disposition, 5 * magnitude)

    def test_shared_enemy_bond_skips_an_observer_thats_neutral_to_the_target(self):
        # thane has real attitude data but no particular opinion of "wolf" specifically (falls
        # back to its own all-neutral default) -- not hostile toward it, so no bond forms.
        self.dm_core.entities["thane"]["attitudes"] = {"default": [0, 0, 0]}
        result = RolledOutcome(entity="gladstone", skill="melee", roll=0, difficulty=0, success=True)
        ability = {"damage_value": {"dice": 0, "pips": 0, "bonus": 5}, "damage_tags": []}

        self.dm_core._apply_damage_if_hit(result, "melee", None, ability, "wolf", via_test=False)

        self.assertNotIn("action_attitude_deltas", self.dm_core.entities["thane"])


class TestActionDrivenAttitudeDrift(DMTestCase):
    """!
    @brief DM_Social.py's nudge_attitude_from_event -- the [[attitude_event]] (rules.toml)
        driven counterpart to nudge_attitude's own dialogue-sentiment drift, applied from a
        resolved player action (combat/theft/favor) instead of the tone of something said. See
        CLAUDE.md's "Extended goals" -- "Actions sway attitudes by varying degrees". Tracked in
        its own "action_attitude_deltas" accumulator/cap (ACTION_ATTITUDE_DRIFT_CAP), independent
        of nudge_attitude's own "attitude_deltas"/TALK_ATTITUDE_DRIFT_CAP -- combat hit wiring is
        covered separately, in TestDamageCalculation, right where _apply_damage_if_hit lives.
    """
    scenario_name = "tavern"

    def test_event_scales_every_axis_by_magnitude(self):
        base = self.dm_core.entities["innkeeper"]["attitudes"]["default"]

        self.dm_core.nudge_attitude_from_event("innkeeper", self.dm_core.player_name, "favor", 0.6)

        after = self.dm_core.get_attitude("innkeeper", self.dm_core.player_name)
        disposition, threat, familiarity = (
            value - starting for value, starting in zip(after, base)
        )
        self.assertAlmostEqual(disposition, 6.0)
        self.assertEqual(threat, 0)
        self.assertAlmostEqual(familiarity, 3.6)

    def test_action_drift_is_capped_independently_of_talk_drift(self):
        # Push both accumulators toward the same axis (disposition) as far as they'll go --
        # dialogue sentiment (TALK_ATTITUDE_DRIFT_CAP) and a run of "favor" events
        # (ACTION_ATTITUDE_DRIFT_CAP) -- and confirm each caps on its own terms rather than
        # sharing one ceiling between the two accumulators.
        for _ in range(50):
            self.dm_core.nudge_attitude("innkeeper", self.dm_core.player_name, {"disposition": ("positive", 1.0)})
        for _ in range(50):
            self.dm_core.nudge_attitude_from_event("innkeeper", self.dm_core.player_name, "favor", 1.0)

        base_disposition = self.dm_core.entities["innkeeper"]["attitudes"]["default"][0]
        disposition = self.dm_core.get_attitude("innkeeper", self.dm_core.player_name)[0]

        self.assertEqual(disposition, base_disposition + TALK_ATTITUDE_DRIFT_CAP + ACTION_ATTITUDE_DRIFT_CAP)

    def test_unknown_event_and_ungated_targets_are_no_ops(self):
        before = list(self.dm_core.get_attitude("innkeeper", self.dm_core.player_name))
        self.dm_core.nudge_attitude_from_event("innkeeper", self.dm_core.player_name, "not_a_real_event", 1.0)
        self.assertEqual(self.dm_core.get_attitude("innkeeper", self.dm_core.player_name), before)

        # An inanimate object has no feelings to nudge -- same precedent is_hostile already sets.
        self.dm_core.entities["stone idol"] = {"name": "stone idol", "supertype": "object", "attitudes": {"default": [0] * 3}}
        self.dm_core.nudge_attitude_from_event("stone idol", self.dm_core.player_name, "favor", 1.0)
        self.assertNotIn("action_attitude_deltas", self.dm_core.entities["stone idol"])

        # A tableless entity (ex: arena's own wolf) has nothing to nudge either.
        self.dm_core.entities["test_tableless"] = {"name": "test_tableless", "max_hp": 10}
        self.dm_core.nudge_attitude_from_event("test_tableless", self.dm_core.player_name, "favor", 1.0)
        self.assertNotIn("action_attitude_deltas", self.dm_core.entities["test_tableless"])

    def test_dead_entity_is_not_aware_of_anything_happening_to_it(self):
        # A dead (or never-conscious) entity isn't aware of a theft, a gift, or anything else --
        # same reasoning that makes a killing blow's own "combat_hit" nudge a no-op too, since
        # the target's HP is already 0 by the time _apply_damage_if_hit gets around to it.
        self.dm_core.apply_damage("innkeeper", 9999)
        self.assertEqual(self.dm_core.get_current_hp("innkeeper"), 0)

        self.dm_core.nudge_attitude_from_event("innkeeper", self.dm_core.player_name, "favor", 1.0)

        self.assertNotIn("action_attitude_deltas", self.dm_core.entities["innkeeper"])


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
        self._load_ad_hoc_scenario([{"name": "gladstone", "band": 1}, {"name": "practice_dummy", "band": 1}])
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
        self.assertEqual(actions[0].roll, 12)
        self.assertEqual(actions[1].roll, 12)

    def test_three_actions_apply_minus_2d(self):
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self._load_ad_hoc_scenario([{"name": "gladstone", "band": 1}, {"name": "practice_dummy", "band": 1}])
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
            self.assertEqual(action.roll, 9)

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

        self._stub_roll_dice(99)
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
        self._load_ad_hoc_scenario([{"name": "gladstone", "band": 1}, {"name": "practice_dummy", "band": 1}])
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
        self.assertEqual(actions[0].roll, 9)
        self.assertTrue(actions[0].success)
        # blades is 5D+0 -- at -1D rolls 4D @ 3 = 12.
        self.assertEqual(actions[1].roll, 12)

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
        self.assertEqual(actions[0].roll, 12)

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
        self.assertFalse(action.success)
        self.assertFalse(any(isinstance(effect, DamageEffect) for effect in action.effects))
        self.assertEqual(result["round"], 1)
        self.assertEqual(self.dm_core.get_current_hp("wolf"), 16)

    def test_successful_attack_applies_damage_to_the_target(self):
        # Give the player an opponent with no matching opposing skill, so the attack auto-succeeds (difficulty 0).
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self._load_ad_hoc_scenario([{"name": "practice_dummy", "band": 1}])

        with patch("random.randint", return_value=3):
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "blades"}], "input": "I attack with my sword"})

        result = self.resolved[-1]
        action = result["actions"][0]
        self.assertTrue(action.success)
        damage_effects = [effect for effect in action.effects if isinstance(effect, DamageEffect)]
        self.assertEqual(len(damage_effects), 1)
        damage = damage_effects[0]
        self.assertEqual(damage.defender, "practice_dummy")
        self.assertGreater(damage.net_damage, 0)
        self.assertEqual(
            self.dm_core.get_current_hp("practice_dummy"),
            20 - damage.net_damage,
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
        self.assertIsInstance(action, OutOfRangeOutcome)

    # --- _ability_requires_language / language_dependent gate ----------------------------

    def test_ability_requires_language_reads_the_resolved_abilitys_own_flag(self):
        # maneuvers.toml's "charm" is language_dependent = true, "intimidate" isn't.
        charm = self.dm_core.entities["charm"]
        intimidate = self.dm_core.entities["intimidate"]
        self.assertTrue(self.dm_core._ability_requires_language("charisma", charm))
        self.assertFalse(self.dm_core._ability_requires_language("intimidation", intimidate))

    def test_ability_requires_language_falls_back_to_the_skills_own_abilities_list(self):
        # A bare "charisma" use (no named ability -- ex: "persuade the guard") still finds
        # charm's own flag via skills.toml's charisma -> ["charm"], the same skill-declared
        # universal-ability list find_attack_ability deliberately never scans itself.
        self.assertTrue(self.dm_core._ability_requires_language("charisma", None))
        self.assertFalse(self.dm_core._ability_requires_language("intimidation", None))
        # An unrelated skill with no such abilities list at all is simply False, not an error.
        self.assertFalse(self.dm_core._ability_requires_language("blades", None))

    def test_language_gated_ability_against_a_no_shared_language_target_is_denied_without_a_roll(self):
        self.dm_core.entities["wolf"]["band"] = 1  # charm's own range defaults to 0 (melee)
        self.dm_core.entities["wolf"]["languages"] = ["dwarvish"]

        self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "charm"}], "input": "I charm the wolf"})

        result = self.resolved[-1]
        action = result["actions"][0]
        self.assertIsInstance(action, LanguageBarrierOutcome)

    def test_language_gated_ability_with_a_shared_language_rolls_normally(self):
        self.dm_core.entities["wolf"]["band"] = 1

        self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "charm"}], "input": "I charm the wolf"})

        result = self.resolved[-1]
        action = result["actions"][0]
        self.assertIsInstance(action, RolledOutcome)


class TestSpellMaterials(DMTestCase):
    # arena's own wolf is already a live, hostile current_target the moment DMCore loads (its
    # own auto-claim at scenario load, same state every other TestCombatLoop test relies on),
    # so casting at it resolves as combat ("round_resolved"), not the no-combat "action_resolved"
    # path. "arc lance" (spells.toml) is on gladstone's own abilities list and needs 1x
    # "iron filings", pre-seeded in his starting inventory (characters.toml). Casting resolves as
    # a flat check against arc lance's own authored "difficulty" (10) -- gladstone's own 2D
    # arcane vs. that fixed number, not an opposed roll against the wolf at all (see
    # _resolve_roll's own "ability.get('difficulty')" branch, DM_Core.py) -- so a fixed
    # random.randint value alone is enough to force either outcome.
    def setUp(self):
        super().setUp()
        self.resolved = self._capture("round_resolved")

    def _cast(self, extra_clauses=None):
        clauses = [{"kind": "action", "skill": "arc lance"}]
        clauses.extend(extra_clauses or [])
        self.dm_core._on_turn_detected({"clauses": clauses, "input": "I cast arc lance at the wolf"})
        return self.resolved[-1]["actions"][0]

    def test_missing_material_fails_the_cast_without_rolling(self):
        self.dm_core.entities["gladstone"]["inventory"].remove("iron filings")

        result = self._cast()

        self.assertIsInstance(result, MissingSpellMaterialsOutcome)

    def test_successful_cast_consumes_the_material(self):
        # gladstone's 2D arcane at a fixed per-die value of 6 rolls 12, clearing arc lance's own
        # difficulty of 10.
        with patch("random.randint", return_value=6):
            result = self._cast()

        self.assertTrue(result.success)
        self.assertTrue(any(isinstance(effect, DamageEffect) for effect in result.effects))
        self.assertNotIn("iron filings", self.dm_core.entities["gladstone"]["inventory"])

    def test_failed_cast_still_consumes_the_material(self):
        # gladstone's 2D arcane at a fixed per-die value of 1 rolls 2, well under arc lance's own
        # difficulty of 10 -- the material is still spent, same as a botched craft attempt's own
        # materials.
        with patch("random.randint", return_value=1):
            result = self._cast()

        self.assertFalse(result.success)
        self.assertFalse(any(isinstance(effect, DamageEffect) for effect in result.effects))
        self.assertNotIn("iron filings", self.dm_core.entities["gladstone"]["inventory"])

    def test_entity_test_on_the_target_overrides_the_abilitys_own_difficulty(self):
        # A target that authors its own [entity.test] for the ability's skill is the actual
        # "specify the skill to resist" mechanism a spell is expected to lean on -- it's checked
        # ahead of ability.get("difficulty") in _resolve_roll (DM_Core.py) and wins outright when
        # it matches, overriding the ability's own flat difficulty fallback entirely.
        self.dm_core.entities["wolf"]["test"] = {"skill": ["arcane"], "difficulty": 20}

        with patch("random.randint", return_value=6):
            result = self._cast()

        # gladstone's 2D arcane at a fixed per-die value of 6 rolls 12 -- clears arc lance's own
        # difficulty (10) but not the wolf's own authored resistance (20), and a via_test roll
        # never rolls the ability's own bonus weapon damage.
        self.assertFalse(result.success)
        self.assertFalse(any(isinstance(effect, DamageEffect) for effect in result.effects))

    def test_a_different_ability_never_touches_the_material(self):
        # gladstone's own longsword (skill="blades") carries no "materials" field at all --
        # _consume_spell_materials_if_rolled must be a complete no-op for it.
        with patch("random.randint", return_value=6):
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "blades"}], "input": "I attack with my sword"})

        self.assertIn("iron filings", self.dm_core.entities["gladstone"]["inventory"])


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


    def test_has_condition_gates_a_behavior_entry_off_the_entitys_own_condition(self):
        # A paralyzed creature shouldn't "act" at 0 dice -- it should match nothing and stand
        # down entirely, the same "no matching entry" fallback an entity with no behavior list
        # at all already gets. See Rules/Fantasy/reference/pathfinder_conversion.md's §1
        # Pattern A recipe.
        self.dm_core.entities["paralyzed_dummy"] = {
            "name": "paralyzed_dummy", "max_hp": 20, "skills": {},
            "active_conditions": {"paralyzed": {"duration": "fleeting", "dismiss": ""}},
            "behavior": [
                {
                    "requirements": [{"field": "has_condition:paralyzed", "operator": "==", "value": False}],
                    "action": "bite",
                },
            ],
        }
        self.assertIsNone(self.dm_core.choose_behavior("paralyzed_dummy"))

        del self.dm_core.entities["paralyzed_dummy"]["active_conditions"]["paralyzed"]
        behavior = self.dm_core.choose_behavior("paralyzed_dummy")
        self.assertEqual(behavior["action"], "bite")


    def test_opponent_has_condition_reacts_to_the_targets_own_condition(self):
        # A creature that presses its advantage while its target is stunned, falling back to a
        # normal attack otherwise -- opponent_name has to be threaded through choose_behavior
        # for this to resolve at all, same as distance_to_target above.
        self.dm_core.entities["predator_dummy"] = {
            "name": "predator_dummy", "max_hp": 20, "skills": {},
            "behavior": [
                {
                    "requirements": [{"field": "opponent_has_condition:stunned", "operator": "==", "value": True}],
                    "action": "finishing_blow",
                },
                {"requirements": [], "action": "bite"},
            ],
        }
        self.assertEqual(self.dm_core.choose_behavior("predator_dummy", "gladstone")["action"], "bite")

        self.dm_core.apply_condition("gladstone", "stunned", duration="fleeting", dismiss="")
        self.assertEqual(
            self.dm_core.choose_behavior("predator_dummy", "gladstone")["action"], "finishing_blow",
        )

        # No opponent_name at all -- resolves to None, same as distance_to_target with no
        # opponent, never accidentally matching a status requirement (which never passes one).
        self.assertIsNone(
            self.dm_core.get_comparable_value("predator_dummy", "opponent_has_condition:stunned"),
        )


    def test_wraith_stands_down_entirely_while_warded(self):
        # creatures.toml's "wraith" is the shipped has_condition example -- both its behavior
        # entries share a "not warded" gate, so a holy ward suppresses its turn entirely rather
        # than just its preferred attack (choose_behavior returns None, same as an entity with
        # no behavior list at all).
        self.assertEqual(self.dm_core.choose_behavior("wraith", "gladstone")["action"], "chilling claw")

        self.dm_core.entities["wraith"]["active_conditions"] = {
            "warded": {"duration": "scene", "dismiss": ""},
        }
        self.assertIsNone(self.dm_core.choose_behavior("wraith", "gladstone"))


    def test_wraith_prefers_life_drain_against_a_wounded_target(self):
        # creatures.toml's "wraith" is the shipped opponent_has_condition example -- it favors
        # draining an already-wounded target over its plain claw, checked ahead of the fallback
        # attack in declaration order.
        self.assertEqual(self.dm_core.choose_behavior("wraith", "gladstone")["action"], "chilling claw")

        self.dm_core.apply_condition("gladstone", "wounded", duration="permanent", dismiss="")
        self.assertEqual(self.dm_core.choose_behavior("wraith", "gladstone")["action"], "life drain")


    def test_resolve_behavior_action_strikes_back_and_applies_damage(self):
        # An unarmored, skill-less target so the wolf's bite always lands and nothing
        # reduces the raw damage -- isolates resolve_behavior_action from armor/opposed-skill
        # specifics, which are already covered by TestDamageCalculation/TestOpposedResolution.
        self.dm_core.entities["target_dummy"] = {"name": "target_dummy", "max_hp": 20, "skills": {}}

        with patch("random.randint", return_value=4):
            result = self.dm_core.resolve_behavior_action("wolf", "target_dummy")

        assert result is not None
        self.assertTrue(result.success)
        self.assertEqual(result.skill, "brawling")
        damage_effects = [effect for effect in result.effects if isinstance(effect, DamageEffect)]
        self.assertEqual(len(damage_effects), 1)
        damage = damage_effects[0]
        self.assertGreater(damage.net_damage, 0)
        self.assertEqual(
            self.dm_core.get_current_hp("target_dummy"),
            20 - damage.net_damage,
        )


    def test_resolve_behavior_action_nudges_the_defenders_attitude_toward_the_attacker(self):
        # NPC-action-driven attitude drift: resolve_behavior_action shares
        # _apply_damage_if_hit's own call-site shape (DM_Core.py's _nudge_combat_hit_attitude)
        # -- the "combat_hit" nudge lands on target_dummy's attitude toward "wolf", the entity
        # that actually swung, never toward the player, who wasn't involved in this turn at all.
        self.dm_core.entities["target_dummy"] = {
            "name": "target_dummy", "max_hp": 20, "skills": {},
            "attitudes": {"default": [0, 0, 0]},
        }

        with patch("random.randint", return_value=4):
            result = self.dm_core.resolve_behavior_action("wolf", "target_dummy")

        assert result is not None
        self.assertTrue(result.success)
        damage_effects = [effect for effect in result.effects if isinstance(effect, DamageEffect)]
        magnitude = damage_effects[0].net_damage / 20
        disposition, threat = (
            self.dm_core.get_attitude("target_dummy", "wolf")[axis] for axis in (0, 1)
        )
        self.assertAlmostEqual(disposition, -20 * magnitude)
        self.assertAlmostEqual(threat, -15 * magnitude)
        self.assertEqual(
            self.dm_core.get_attitude("target_dummy", self.dm_core.player_name), [0, 0, 0],
        )


    def test_resolve_behavior_action_bonds_bystanders_toward_the_attacker(self):
        # "Bonds made on the battlefield" generalizes the same way -- thane already hates
        # target_dummy specifically (a name-override disposition <= -100 toward it), so it
        # should warm toward "wolf", who just hit it, not toward the player, who never acted
        # this turn.
        self.dm_core.entities["target_dummy"] = {"name": "target_dummy", "max_hp": 20, "skills": {}}
        self.dm_core.scenario_entities.append("target_dummy")
        self.dm_core.entities["thane"]["attitudes"] = {
            "default": [40, 40, 40],
            "name": [{"target_dummy": [-100, 0, 0]}],
        }

        with patch("random.randint", return_value=4):
            result = self.dm_core.resolve_behavior_action("wolf", "target_dummy")

        assert result is not None
        damage_effects = [effect for effect in result.effects if isinstance(effect, DamageEffect)]
        magnitude = damage_effects[0].net_damage / 20
        # thane has no name-override toward "wolf" specifically, so its own "default" [40, 40,
        # 40] base is what the shared_enemy drift stacks on top of.
        disposition = self.dm_core.get_attitude("thane", "wolf")[0]
        self.assertAlmostEqual(disposition, 40 + 5 * magnitude)
        self.assertEqual(self.dm_core.get_attitude("thane", self.dm_core.player_name), [40, 40, 40])


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


class TestRoundUpkeep(DMTestCase):
    """!
    @brief The generic per-round upkeep hook (run_round_upkeep/apply_round_upkeep/
        get_condition_upkeep, DM_Status.py) and creatures.toml's "troll" -- the shipped
        regeneration-suppressed-by-fire example (see rules.toml's own "regenerating"
        [[condition]] entry and Rules/Fantasy/reference/pathfinder_conversion.md's #6).
    """

    def setUp(self):
        super().setUp()
        self._load_ad_hoc_scenario(
            [{"name": "gladstone", "band": 1}, {"name": "troll", "band": 1}], bands=4, enclosed=True,
        )

    def test_instancing_seeds_the_regenerating_condition_from_the_template(self):
        # creatures.toml's troll authors [entity.conditions.regenerating] permanently --
        # _instance_entities copies it into active_conditions the moment it's placed in a scene.
        self.assertIn("regenerating", self.dm_core.entities["troll"]["active_conditions"])

    def test_get_condition_upkeep_reads_the_trolls_regenerating_condition(self):
        upkeep = self.dm_core.get_condition_upkeep("troll")
        self.assertEqual(upkeep["heal"], {"dice": 2, "pips": 0, "bonus": 0})
        self.assertEqual(upkeep["damage"], {"dice": 0, "pips": 0, "bonus": 0})

    def test_calculate_damage_records_recent_damage_tags_on_the_defender(self):
        fireball = {"damage_value": {"dice": 2, "pips": 0, "bonus": 0}, "damage_tags": ["fire"]}
        with patch("random.randint", return_value=3):
            self.dm_core.calculate_damage("gladstone", "troll", fireball)
        self.assertIn("fire", self.dm_core.entities["troll"]["recent_damage_tags"])

    @patch("random.randint", return_value=3)
    def test_apply_round_upkeep_heals_and_clears_recent_damage_tags(self, mock_randint):
        self.dm_core.apply_damage("troll", 10)  # 40 -> 30
        self.dm_core.entities["troll"]["recent_damage_tags"] = {"slashing"}

        self.dm_core.apply_round_upkeep("troll")

        self.assertEqual(self.dm_core.get_current_hp("troll"), 36)  # 30 + (2D @ 3 each = 6)
        self.assertEqual(self.dm_core.entities["troll"]["recent_damage_tags"], set())

    @patch("random.randint", return_value=3)
    def test_apply_round_upkeep_suppressed_by_a_matching_fire_tag(self, mock_randint):
        self.dm_core.apply_damage("troll", 10)  # 40 -> 30
        self.dm_core.entities["troll"]["recent_damage_tags"] = {"fire"}

        self.dm_core.apply_round_upkeep("troll")

        self.assertEqual(self.dm_core.get_current_hp("troll"), 30)  # no heal this round
        self.assertEqual(self.dm_core.entities["troll"]["recent_damage_tags"], set())

    def test_run_round_upkeep_skips_dead_entities(self):
        self.dm_core.apply_damage("troll", 999)
        self.dm_core.run_round_upkeep()
        self.assertEqual(self.dm_core.get_current_hp("troll"), 0)

    @patch("random.randint", return_value=3)
    def test_resolve_combat_round_regenerates_the_troll_unless_burned_this_round(self, mock_randint):
        # _resolve_combat_round is the real per-round entry point (DM_Core.py) -- confirms the
        # hook is actually wired in, not just directly callable.
        self.dm_core.apply_damage("troll", 10)  # 40 -> 30, no fire tag recorded
        self.dm_core._resolve_combat_round({"actions": []})
        self.assertEqual(self.dm_core.get_current_hp("troll"), 36)  # healed 2D @ 3 = 6

        self.dm_core.apply_damage("troll", 10)  # 36 -> 26
        self.dm_core.entities["troll"]["recent_damage_tags"] = {"fire"}
        self.dm_core._resolve_combat_round({"actions": []})
        self.assertEqual(self.dm_core.get_current_hp("troll"), 26)  # suppressed this round


class TestProgramInterpreter(unittest.TestCase):
    """!
    @brief Program_Interpreter.py's own pure do/if engine -- direct, bare-dict tests, no
        EventBus/DMCore needed (see docs/design/skill_effect_language.md's own "Module shape").
    """

    def setUp(self):
        self.event_bus = EventBus()
        self.rules = {"attitude_event": [
            {"name": "intimidated", "disposition": -10, "threat": -25, "familiarity": -5},
        ]}
        self.entities = {
            "hero": {"name": "hero", "max_hp": 20, "hp": 20},
            "victim": {"name": "victim", "max_hp": 20, "hp": 20, "attitudes": {"default": [0, 0, 0]}},
        }

    def test_condition_op_applies_a_condition_to_the_resolved_role(self):
        run_program(
            {"do": "condition", "entity": "target", "name": "prone", "duration": "scene"},
            {"actor": "hero", "target": "victim"}, self.entities, self.rules, self.event_bus,
        )
        self.assertIn("prone", self.entities["victim"]["active_conditions"])

    def test_dismiss_condition_op_removes_an_active_condition(self):
        self.entities["victim"]["active_conditions"] = {"prone": {"duration": "scene", "dismiss": None}}
        run_program(
            {"do": "dismiss_condition", "entity": "target", "name": "prone"},
            {"actor": "hero", "target": "victim"}, self.entities, self.rules, self.event_bus,
        )
        self.assertNotIn("prone", self.entities["victim"]["active_conditions"])

    def test_attitude_op_nudges_every_axis_by_the_resolved_event(self):
        run_program(
            {"do": "attitude", "entity": "target", "toward": "actor", "event": "intimidated", "magnitude": 1.0},
            {"actor": "hero", "target": "victim"}, self.entities, self.rules, self.event_bus,
        )
        self.assertEqual(self.entities["victim"]["action_attitude_deltas"]["hero"], [-10, -25, -5])

    def test_a_step_list_runs_every_step_in_order(self):
        program = [
            {"do": "condition", "entity": "target", "name": "prone", "duration": "scene"},
            {"do": "condition", "entity": "target", "name": "shaken", "duration": "scene"},
        ]
        run_program(program, {"actor": "hero", "target": "victim"}, self.entities, self.rules, self.event_bus)
        self.assertIn("prone", self.entities["victim"]["active_conditions"])
        self.assertIn("shaken", self.entities["victim"]["active_conditions"])

    def test_if_then_only_runs_when_the_condition_holds(self):
        self.entities["victim"]["hp"] = 5  # 25% of max_hp -- < 0.5
        run_program(
            {
                "if": "target.hp_per_remain < 0.5",
                "then": {"do": "condition", "entity": "target", "name": "prone", "duration": "scene"},
            },
            {"actor": "hero", "target": "victim"}, self.entities, self.rules, self.event_bus,
        )
        self.assertIn("prone", self.entities["victim"]["active_conditions"])

    def test_if_else_runs_when_the_condition_fails(self):
        run_program(
            {
                "if": "target.hp_per_remain < 0.5",  # false -- full hp
                "then": {"do": "condition", "entity": "target", "name": "prone", "duration": "scene"},
                "else": {"do": "condition", "entity": "target", "name": "shaken", "duration": "scene"},
            },
            {"actor": "hero", "target": "victim"}, self.entities, self.rules, self.event_bus,
        )
        self.assertNotIn("prone", self.entities["victim"].get("active_conditions", {}))
        self.assertIn("shaken", self.entities["victim"]["active_conditions"])

    def test_all_requires_every_sub_condition(self):
        condition = {"all": ["target.hp_per_remain <= 1.0", "target.has_condition:prone == false"]}
        self.assertTrue(evaluate_condition(condition, {"actor": "hero", "target": "victim"}, self.entities))
        self.entities["victim"]["active_conditions"] = {"prone": {}}
        self.assertFalse(evaluate_condition(condition, {"actor": "hero", "target": "victim"}, self.entities))

    def test_any_matches_on_a_single_sub_condition(self):
        condition = {"any": ["target.has_condition:prone == true", "target.hp_per_remain <= 1.0"]}
        self.assertTrue(evaluate_condition(condition, {"actor": "hero", "target": "victim"}, self.entities))

    def test_none_matches_when_no_sub_condition_holds(self):
        condition = {"none": ["target.has_condition:prone == true"]}
        self.assertTrue(evaluate_condition(condition, {"actor": "hero", "target": "victim"}, self.entities))

    def test_missing_role_in_ctx_is_a_quiet_no_op_not_an_error(self):
        run_program(
            {"do": "condition", "entity": "target", "name": "prone", "duration": "scene"},
            {"actor": "hero"}, self.entities, self.rules, self.event_bus,
        )  # no "target" in ctx -- must not raise, and must change nothing

    def test_unknown_op_raises(self):
        with self.assertRaises(ValueError):
            run_program(
                {"do": "not_a_real_op"}, {"actor": "hero", "target": "victim"},
                self.entities, self.rules, self.event_bus,
            )

    def test_step_missing_a_required_arg_raises(self):
        with self.assertRaises(ValueError):
            run_program(
                {"do": "condition", "entity": "target"}, {"actor": "hero", "target": "victim"},
                self.entities, self.rules, self.event_bus,
            )

    def test_a_literal_entity_name_instead_of_a_role_token_raises(self):
        with self.assertRaises(ValueError):
            run_program(
                {"do": "condition", "entity": "victim", "name": "prone"}, {"actor": "hero", "target": "victim"},
                self.entities, self.rules, self.event_bus,
            )

    def test_malformed_condition_string_raises(self):
        with self.assertRaises(ValueError):
            run_program(
                {"if": "not a real expression", "then": {"do": "dismiss_condition", "entity": "target", "name": "prone"}},
                {"actor": "hero", "target": "victim"}, self.entities, self.rules, self.event_bus,
            )

    def test_damage_op_deals_real_damage_through_calculate_damage(self):
        with patch("random.randint", return_value=3):
            run_program(
                {"do": "damage", "entity": "target", "dice": 2, "pips": 0, "bonus": 0, "tags": ["fire"]},
                {"actor": "hero", "target": "victim"}, self.entities, self.rules, self.event_bus,
            )
        self.assertEqual(self.entities["victim"]["hp"], 14)  # 20 - (2 * 3)

    def test_heal_op_restores_hp(self):
        self.entities["victim"]["hp"] = 10
        with patch("random.randint", return_value=3):
            run_program(
                {"do": "heal", "entity": "target", "dice": 2, "pips": 0, "bonus": 0},
                {"actor": "hero", "target": "victim"}, self.entities, self.rules, self.event_bus,
            )
        self.assertEqual(self.entities["victim"]["hp"], 16)  # 10 + (2 * 3)


class TestSocialResolutionPure(unittest.TestCase):
    """!
    @brief Social_Resolution.py's own pure nudge_attitude_from_event/apply_capped_drift --
        direct, bare-dict tests (see docs/design/skill_effect_language.md's own "Prerequisite:
        pure cores for attitude and transfer"). DM_Social.py's own thin-wrapper behavior is
        already covered indirectly by every existing attitude-drift test in this file (ex:
        TestCombatLoop's combat_hit/shared_enemy assertions), which never changed shape.
    """

    def setUp(self):
        self.rules = {"attitude_event": [
            {"name": "combat_hit", "disposition": -20, "threat": -15, "familiarity": -10},
        ]}

    def test_nudges_all_three_axes_scaled_by_magnitude(self):
        entities = {"victim": {"max_hp": 20, "hp": 20, "attitudes": {"default": [0, 0, 0]}}}
        Social_Resolution.nudge_attitude_from_event(entities, self.rules, "victim", "hero", "combat_hit", 0.5)
        self.assertEqual(entities["victim"]["action_attitude_deltas"]["hero"], [-10.0, -7.5, -5.0])

    def test_no_op_without_an_attitudes_table_at_all(self):
        entities = {"victim": {"max_hp": 20, "hp": 20}}
        Social_Resolution.nudge_attitude_from_event(entities, self.rules, "victim", "hero", "combat_hit", 1.0)
        self.assertNotIn("action_attitude_deltas", entities["victim"])

    def test_no_op_for_a_dead_entity(self):
        entities = {"victim": {"max_hp": 20, "hp": 0, "attitudes": {"default": [0, 0, 0]}}}
        Social_Resolution.nudge_attitude_from_event(entities, self.rules, "victim", "hero", "combat_hit", 1.0)
        self.assertNotIn("action_attitude_deltas", entities["victim"])

    def test_no_op_for_an_object_supertype(self):
        entities = {"chest": {"max_hp": 20, "hp": 20, "supertype": "object", "attitudes": {"default": [0, 0, 0]}}}
        Social_Resolution.nudge_attitude_from_event(entities, self.rules, "chest", "hero", "combat_hit", 1.0)
        self.assertNotIn("action_attitude_deltas", entities["chest"])

    def test_no_op_for_an_unknown_event_name(self):
        entities = {"victim": {"max_hp": 20, "hp": 20, "attitudes": {"default": [0, 0, 0]}}}
        Social_Resolution.nudge_attitude_from_event(entities, self.rules, "victim", "hero", "not_a_real_event", 1.0)
        self.assertNotIn("action_attitude_deltas", entities["victim"])

    def test_accumulated_drift_is_capped(self):
        entities = {"victim": {"max_hp": 20, "hp": 20, "attitudes": {"default": [0, 0, 0]}}}
        for _ in range(20):
            Social_Resolution.nudge_attitude_from_event(entities, self.rules, "victim", "hero", "combat_hit", 1.0)
        self.assertEqual(
            entities["victim"]["action_attitude_deltas"]["hero"][0], -Social_Resolution.ACTION_ATTITUDE_DRIFT_CAP,
        )


class TestUniversalAbilities(DMTestCase):
    """!
    @brief Universal (untrained) abilities -- maneuvers.toml's trip/disarm/sunder (listed under
        athletics' own "abilities" field) and intimidate (under intimidation's), plus
        resolve_named_ability's own skill-list fallback (DM_Combat.py) -- see
        docs/design/skill_effect_language.md's "Universal (untrained) abilities".
    """

    def test_athletics_lists_its_own_cmb_style_maneuvers(self):
        self.assertEqual(
            set(self.dm_core.skills["athletics"]["abilities"]),
            {"trip", "disarm", "sunder", "bull rush", "grapple", "pin"},
        )

    def test_trickery_lists_its_own_maneuvers(self):
        self.assertEqual(set(self.dm_core.skills["trickery"]["abilities"]), {"dirty trick", "feint"})

    def test_sunder_is_reachable_from_every_melee_weapon_skill(self):
        for skill_name in ("athletics", "blades", "axes", "polearms", "brawling"):
            self.assertIn("sunder", self.dm_core.skills[skill_name].get("abilities", []))

    def test_universal_abilities_set_is_built_at_load_time(self):
        self.assertEqual(
            self.dm_core.universal_abilities,
            {
                "trip", "disarm", "sunder", "bull rush", "grapple", "pin", "intimidate",
                "dirty trick", "feint", "escape artist", "sleight of hand", "treat wounds", "charm",
            },
        )

    def test_resolve_named_ability_finds_a_universal_ability_gladstone_doesnt_own(self):
        owned_names = {
            a if isinstance(a, str) else a.get("name") for a in self.dm_core.entities["gladstone"].get("abilities", [])
        }
        self.assertNotIn("trip", owned_names)

        ability = self.dm_core.resolve_named_ability("gladstone", "trip")

        self.assertIsNotNone(ability)
        self.assertEqual(ability["name"], "trip")

    def test_resolve_named_ability_still_prefers_an_owned_ability_over_a_universal_one(self):
        # gladstone's own "punch" is an owned innate ability -- not universal at all -- confirms
        # the ownership check still runs first (unaffected by the universal fallback).
        ability = self.dm_core.resolve_named_ability("gladstone", "punch")
        self.assertIsNotNone(ability)

    def test_resolve_named_ability_returns_none_for_a_name_matching_nothing(self):
        self.assertIsNone(self.dm_core.resolve_named_ability("gladstone", "not_a_real_ability_name"))

    def test_a_universal_ability_defaults_to_melee_range(self):
        # trip/disarm/sunder each write range = 0 explicitly; is_in_range's own unconditional
        # default is unchanged either way.
        for name in ("trip", "disarm", "sunder"):
            self.assertEqual(self.dm_core.entities[name].get("range", 0), 0)


class TestAbilityOutcomeProgram(DMTestCase):
    """!
    @brief DM_Core.py's own _run_ability_outcome_program -- the attachment point that runs a
        resolved ability's own on_pass/on_fail once a real roll happens (see
        docs/design/skill_effect_language.md's "Attachment points"). Exercised directly against
        a constructed RolledOutcome rather than a full _on_turn_detected pass, so these stay
        deterministic without depending on wolf's own (nonexistent) opposing skill/dice rolls.
    """

    def setUp(self):
        super().setUp()
        self.dm_core.entities["target_dummy"] = {
            "name": "target_dummy", "max_hp": 20, "hp": 20, "attitudes": {"default": [0, 0, 0]},
        }

    def test_trip_on_pass_applies_prone_to_the_target(self):
        trip = self.dm_core.entities["trip"]
        result = RolledOutcome(entity="gladstone", skill="athletics", roll=15, difficulty=5, success=True)

        self.dm_core._run_ability_outcome_program(result, "athletics", None, trip, "target_dummy", via_test=False)

        self.assertIn("prone", self.dm_core.entities["target_dummy"]["active_conditions"])

    def test_trip_on_fail_does_nothing_since_no_on_fail_is_authored(self):
        trip = self.dm_core.entities["trip"]
        result = RolledOutcome(entity="gladstone", skill="athletics", roll=1, difficulty=15, success=False)

        self.dm_core._run_ability_outcome_program(result, "athletics", None, trip, "target_dummy", via_test=False)

        self.assertNotIn("prone", self.dm_core.entities["target_dummy"].get("active_conditions", {}))

    def test_intimidate_on_pass_applies_shaken_once_threat_is_already_past_the_threshold(self):
        # intimidate's own step 2 ("if target.threat < -50") reads target_dummy's live attitude
        # -- set low enough here on its own template default that the conditional fires
        # regardless of step 1's own nudge (see the next test for why step 1 doesn't move it).
        self.dm_core.entities["target_dummy"]["attitudes"]["default"] = [0, -60, 0]
        intimidate = self.dm_core.entities["intimidate"]
        result = RolledOutcome(entity="gladstone", skill="intimidation", roll=15, difficulty=5, success=True)

        self.dm_core._run_ability_outcome_program(result, "intimidation", None, intimidate, "target_dummy", via_test=False)

        self.assertIn("shaken", self.dm_core.entities["target_dummy"]["active_conditions"])

    def test_intimidate_on_pass_step_one_is_a_no_op_since_roll_margin_is_not_yet_a_real_field(self):
        # Step 1's own magnitude ("actor.roll_margin") is exactly the design doc's own
        # acknowledged open question (docs/design/skill_effect_language.md's "Open questions") --
        # "roll_margin" resolves to None (no such field on any entity), so nudge_attitude_from_event's
        # own falsy-magnitude no-op applies. Documented here as current, honest behavior rather
        # than silently assumed to work.
        self.dm_core.entities["target_dummy"]["attitudes"]["default"] = [0, -60, 0]
        intimidate = self.dm_core.entities["intimidate"]
        result = RolledOutcome(entity="gladstone", skill="intimidation", roll=15, difficulty=5, success=True)

        self.dm_core._run_ability_outcome_program(result, "intimidation", None, intimidate, "target_dummy", via_test=False)

        self.assertNotIn("action_attitude_deltas", self.dm_core.entities["target_dummy"])

    def test_intimidate_on_pass_skips_shaken_when_still_above_the_threshold(self):
        intimidate = self.dm_core.entities["intimidate"]
        result = RolledOutcome(entity="gladstone", skill="intimidation", roll=15, difficulty=5, success=True)

        self.dm_core._run_ability_outcome_program(result, "intimidation", None, intimidate, "target_dummy", via_test=False)

        self.assertNotIn("shaken", self.dm_core.entities["target_dummy"].get("active_conditions", {}))

    def test_intimidate_on_fail_nudges_attitude_via_failed_intimidation(self):
        intimidate = self.dm_core.entities["intimidate"]
        result = RolledOutcome(entity="gladstone", skill="intimidation", roll=2, difficulty=15, success=False)

        self.dm_core._run_ability_outcome_program(result, "intimidation", None, intimidate, "target_dummy", via_test=False)

        deltas = self.dm_core.entities["target_dummy"]["action_attitude_deltas"]["gladstone"]
        self.assertEqual(deltas, [-1.5, 3.0, -1.5])  # failed_intimidation's own deltas @ magnitude 0.3

    def test_never_fires_for_a_via_test_roll(self):
        trip = self.dm_core.entities["trip"]
        result = RolledOutcome(entity="gladstone", skill="athletics", roll=15, difficulty=5, success=True)

        self.dm_core._run_ability_outcome_program(result, "athletics", None, trip, "target_dummy", via_test=True)

        self.assertNotIn("prone", self.dm_core.entities["target_dummy"].get("active_conditions", {}))

    def test_bull_rush_on_pass_applies_staggered(self):
        bull_rush = self.dm_core.entities["bull rush"]
        result = RolledOutcome(entity="gladstone", skill="athletics", roll=15, difficulty=5, success=True)

        self.dm_core._run_ability_outcome_program(result, "athletics", None, bull_rush, "target_dummy", via_test=False)

        self.assertIn("staggered", self.dm_core.entities["target_dummy"]["active_conditions"])

    def test_grapple_on_pass_applies_grappled(self):
        grapple = self.dm_core.entities["grapple"]
        result = RolledOutcome(entity="gladstone", skill="athletics", roll=15, difficulty=5, success=True)

        self.dm_core._run_ability_outcome_program(result, "athletics", None, grapple, "target_dummy", via_test=False)

        self.assertIn("grappled", self.dm_core.entities["target_dummy"]["active_conditions"])

    def test_dirty_trick_on_pass_applies_dazzled(self):
        dirty_trick = self.dm_core.entities["dirty trick"]
        result = RolledOutcome(entity="gladstone", skill="trickery", roll=15, difficulty=5, success=True)

        self.dm_core._run_ability_outcome_program(result, "trickery", None, dirty_trick, "target_dummy", via_test=False)

        self.assertIn("dazzled", self.dm_core.entities["target_dummy"]["active_conditions"])

    def test_feint_on_pass_applies_flat_footed(self):
        feint = self.dm_core.entities["feint"]
        result = RolledOutcome(entity="gladstone", skill="trickery", roll=15, difficulty=5, success=True)

        self.dm_core._run_ability_outcome_program(result, "trickery", None, feint, "target_dummy", via_test=False)

        self.assertIn("flat_footed", self.dm_core.entities["target_dummy"]["active_conditions"])

    def test_none_of_the_new_maneuvers_fire_on_a_failed_roll(self):
        for name, skill_name, condition_name in (
            ("bull rush", "athletics", "staggered"), ("grapple", "athletics", "grappled"),
            ("dirty trick", "trickery", "dazzled"), ("feint", "trickery", "flat_footed"),
        ):
            ability = self.dm_core.entities[name]
            result = RolledOutcome(entity="gladstone", skill=skill_name, roll=1, difficulty=15, success=False)

            self.dm_core._run_ability_outcome_program(result, skill_name, None, ability, "target_dummy", via_test=False)

            self.assertNotIn(condition_name, self.dm_core.entities["target_dummy"].get("active_conditions", {}))

    # --- inject_directive / suggestion --------------------------------------------------

    def test_suggestion_on_pass_plants_the_raw_turn_text_as_a_directive(self):
        # spells.toml's own "suggestion" omits a literal "text" on purpose -- see its own
        # comment -- so the op falls back to ctx["input"], threaded in here as input_text.
        suggestion = self.dm_core.entities["suggestion"]
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=15, difficulty=5, success=True)

        self.dm_core._run_ability_outcome_program(
            result, "arcane", None, suggestion, "target_dummy", via_test=False,
            input_text="tell him to open the gate",
        )

        self.assertEqual(
            self.dm_core.entities["target_dummy"]["prompt_directive"],
            {"text": "tell him to open the gate", "source": "gladstone"},
        )

    def test_suggestion_on_a_failed_roll_plants_nothing(self):
        suggestion = self.dm_core.entities["suggestion"]
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=1, difficulty=15, success=False)

        self.dm_core._run_ability_outcome_program(
            result, "arcane", None, suggestion, "target_dummy", via_test=False,
            input_text="tell him to open the gate",
        )

        self.assertNotIn("prompt_directive", self.dm_core.entities["target_dummy"])

    def test_inject_directive_with_no_input_text_and_no_literal_text_is_a_no_op(self):
        suggestion = self.dm_core.entities["suggestion"]
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=15, difficulty=5, success=True)

        self.dm_core._run_ability_outcome_program(result, "arcane", None, suggestion, "target_dummy", via_test=False)

        self.assertNotIn("prompt_directive", self.dm_core.entities["target_dummy"])

    def test_inject_directive_literal_text_wins_over_ctx_input(self):
        scripted = {
            "skill": "arcane", "on_pass": {"do": "inject_directive", "entity": "target", "text": "a scripted directive"},
        }
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=15, difficulty=5, success=True)

        self.dm_core._run_ability_outcome_program(
            result, "arcane", None, scripted, "target_dummy", via_test=False, input_text="whatever the player typed",
        )

        self.assertEqual(
            self.dm_core.entities["target_dummy"]["prompt_directive"],
            {"text": "a scripted directive", "source": "gladstone"},
        )

    def test_inject_directive_no_ops_against_an_inanimate_object(self):
        self.dm_core.entities["crate"] = {"name": "crate", "max_hp": 10, "hp": 10, "supertype": "object"}
        suggestion = self.dm_core.entities["suggestion"]
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=15, difficulty=5, success=True)

        self.dm_core._run_ability_outcome_program(
            result, "arcane", None, suggestion, "crate", via_test=False, input_text="open yourself",
        )

        self.assertNotIn("prompt_directive", self.dm_core.entities["crate"])

    def test_inject_directive_no_ops_against_a_dead_entity(self):
        self.dm_core.entities["target_dummy"]["hp"] = 0
        suggestion = self.dm_core.entities["suggestion"]
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=15, difficulty=5, success=True)

        self.dm_core._run_ability_outcome_program(
            result, "arcane", None, suggestion, "target_dummy", via_test=False, input_text="get up",
        )

        self.assertNotIn("prompt_directive", self.dm_core.entities["target_dummy"])


class TestPromptDirective(DMTestCase):
    """!
    @brief DM_Social.py's describe_character surfacing a planted prompt_directive (see
        Social_Resolution.py's set_prompt_directive and TestAbilityOutcomeProgram's own
        inject_directive tests for how one actually gets planted) into narration prompts, plus
        its save/load round-trip (DM_Persistence.py).
    """

    def setUp(self):
        super().setUp()
        self.dm_core.entities["target_dummy"] = {
            "name": "target_dummy", "max_hp": 20, "hp": 20, "description": "A plain townsperson.",
        }

    def test_describe_character_appends_the_directive_when_present(self):
        self.dm_core.entities["target_dummy"]["prompt_directive"] = {
            "text": "open the gate", "source": "gladstone",
        }
        description = self.dm_core.describe_character("target_dummy")
        self.assertIn("Currently privately convinced (planted by gladstone): \"open the gate\"", description)

    def test_describe_character_omits_anything_when_no_directive_is_planted(self):
        description = self.dm_core.describe_character("target_dummy")
        self.assertNotIn("Currently privately convinced", description)

    def test_describe_character_falls_back_to_someone_when_source_is_unknown(self):
        self.dm_core.entities["target_dummy"]["prompt_directive"] = {"text": "flee", "source": None}
        description = self.dm_core.describe_character("target_dummy")
        self.assertIn("planted by someone", description)

    def test_prompt_directive_round_trips_through_save_and_load(self):
        # A real, scenario-instanced entity, not the synthetic target_dummy above -- save_game's
        # own _all_known_instance_names walks location_runtime's own persistent_names, so an
        # entity added straight to self.entities with no location ever instancing it (like
        # target_dummy here) wouldn't actually be in the save file at all.
        slot_name = "test_prompt_directive_slot"
        self.addCleanup(shutil.rmtree, self.dm_core._save_slot_dir(slot_name), ignore_errors=True)
        player_name = self.dm_core.player_name
        self.dm_core.entities[player_name]["prompt_directive"] = {
            "text": "open the gate", "source": "an unseen voice",
        }

        self.dm_core.save_game(slot_name)
        self.dm_core.load_game(slot_name)

        self.assertEqual(
            self.dm_core.entities[player_name]["prompt_directive"],
            {"text": "open the gate", "source": "an unseen voice"},
        )


class TestMorePathfinderManeuvers(DMTestCase):
    """!
    @brief The second wave of Pathfinder-inspired universal abilities: sunder's own object-vs-
        creature branch, pin (grapple-gated), escape artist (self-targeting), sleight of hand
        (the first real transfer_currency op caller -- Inventory_Resolution.py), treat wounds,
        and charm (the positive mirror of intimidate, with real, non-"roll_margin" magnitudes).
    """

    def setUp(self):
        super().setUp()
        self.dm_core.entities["target_dummy"] = {
            "name": "target_dummy", "max_hp": 20, "hp": 20, "attitudes": {"default": [0, 0, 0]},
            "supertype": "creature", "currency": 40,
        }
        self.dm_core.entities["crate"] = {"name": "crate", "max_hp": 10, "hp": 10, "supertype": "object"}

    def _run(self, ability_name, skill_name, target_name, success=True, actor="gladstone"):
        ability = self.dm_core.entities[ability_name]
        result = RolledOutcome(
            entity=actor, skill=skill_name, roll=15 if success else 1, difficulty=5 if success else 15,
            success=success,
        )
        self.dm_core._run_ability_outcome_program(result, skill_name, None, ability, target_name, via_test=False)
        return result

    def test_sunder_condition_disarms_a_creatures_weapon(self):
        self._run("sunder", "blades", "target_dummy")
        self.assertIn("sundered_weapon", self.dm_core.entities["target_dummy"]["active_conditions"])

    def test_sunder_deals_real_damage_to_an_object(self):
        with patch("random.randint", return_value=3):
            self._run("sunder", "blades", "crate")
        self.assertLess(self.dm_core.entities["crate"]["hp"], 10)

    def test_sunder_is_rollable_via_any_melee_weapon_skill(self):
        for skill_name in ("athletics", "blades", "axes", "polearms", "brawling"):
            self.dm_core.entities["target_dummy"]["active_conditions"] = {}
            self._run("sunder", skill_name, "target_dummy")
            self.assertIn("sundered_weapon", self.dm_core.entities["target_dummy"]["active_conditions"])

    def test_pin_only_lands_on_an_already_grappled_target(self):
        self.dm_core.entities["target_dummy"]["active_conditions"] = {"grappled": {"duration": "scene", "dismiss": None}}
        self._run("pin", "athletics", "target_dummy")
        self.assertIn("pinned", self.dm_core.entities["target_dummy"]["active_conditions"])

    def test_pin_is_a_no_op_without_grappled_first(self):
        self._run("pin", "athletics", "target_dummy")
        self.assertNotIn("pinned", self.dm_core.entities["target_dummy"].get("active_conditions", {}))

    def test_escape_artist_dismisses_the_actors_own_grappled_condition(self):
        self.dm_core.entities["gladstone"].setdefault("active_conditions", {})["grappled"] = {
            "duration": "scene", "dismiss": None,
        }
        self._run("escape artist", "escape", "target_dummy")
        self.assertNotIn("grappled", self.dm_core.entities["gladstone"]["active_conditions"])

    def test_sleight_of_hand_steals_all_the_targets_currency(self):
        self.dm_core.entities["gladstone"]["currency"] = 0
        self._run("sleight of hand", "finesse", "target_dummy")
        self.assertEqual(self.dm_core.entities["target_dummy"]["currency"], 0)
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 40)

    def test_sleight_of_hand_nudges_the_victims_attitude_via_theft(self):
        self._run("sleight of hand", "finesse", "target_dummy")
        deltas = self.dm_core.entities["target_dummy"]["action_attitude_deltas"]["gladstone"]
        self.assertEqual(deltas, [-7.5, 0, -6.0])  # theft {-15, 0, -12} @ magnitude 0.5

    def test_treat_wounds_heals_the_target(self):
        self.dm_core.entities["target_dummy"]["hp"] = 5
        with patch("random.randint", return_value=3):
            self._run("treat wounds", "medicine", "target_dummy")
        self.assertEqual(self.dm_core.entities["target_dummy"]["hp"], 14)  # 5 + (3 * 3)

    def test_charm_on_pass_nudges_attitude_positively(self):
        self._run("charm", "charisma", "target_dummy", success=True)
        deltas = self.dm_core.entities["target_dummy"]["action_attitude_deltas"]["gladstone"]
        self.assertEqual(deltas, [9.0, 3.0, 6.0])  # charmed {15, 5, 10} @ magnitude 0.6

    def test_charm_on_fail_only_mildly_dents_attitude(self):
        self._run("charm", "charisma", "target_dummy", success=False)
        deltas = self.dm_core.entities["target_dummy"]["action_attitude_deltas"]["gladstone"]
        self.assertAlmostEqual(deltas[0], -0.6)  # failed_charm disposition -3 @ magnitude 0.2


class TestEntityTestOutcomeProgram(DMTestCase):
    """!
    @brief DM_Core.py's own _run_test_outcome_program -- [entity.test]'s own on_pass/on_fail,
        sibling to its existing flat pass/fail tables (see
        docs/design/skill_effect_language.md's "Attachment points"). No shipped [entity.test]
        authors on_pass/on_fail yet, so this exercises the wiring directly against a synthetic
        test table.
    """

    def test_on_pass_runs_when_the_test_succeeds(self):
        self.dm_core.entities["chest_dummy"] = {"name": "chest_dummy", "max_hp": 1, "hp": 1}
        test = {
            "difficulty": 5, "skill": ["finesse"],
            "on_pass": {"do": "condition", "entity": "actor", "name": "shaken", "duration": "scene"},
        }

        self.dm_core._run_test_outcome_program(test, True, "chest_dummy")

        self.assertIn("shaken", self.dm_core.entities["gladstone"]["active_conditions"])

    def test_on_fail_runs_when_the_test_fails_and_on_pass_does_not(self):
        self.dm_core.entities["chest_dummy"] = {"name": "chest_dummy", "max_hp": 1, "hp": 1}
        test = {
            "difficulty": 5, "skill": ["finesse"],
            "on_pass": {"do": "condition", "entity": "actor", "name": "shaken", "duration": "scene"},
            "on_fail": {"do": "condition", "entity": "actor", "name": "prone", "duration": "scene"},
        }

        self.dm_core._run_test_outcome_program(test, False, "chest_dummy")

        self.assertNotIn("shaken", self.dm_core.entities["gladstone"].get("active_conditions", {}))
        self.assertIn("prone", self.dm_core.entities["gladstone"]["active_conditions"])


class TestOnInteractProgram(DMTestCase):
    """!
    @brief The cursed dagger's own [entity.on_interact.equip] -- items.toml's shipped worked
        example for docs/design/skill_effect_language.md's "The cursed dagger, actually cursed".
    """

    def setUp(self):
        super().setUp()
        self.dm_core.entities["gladstone"]["inventory"].append("cursed dagger")

    def test_equipping_an_unidentified_cursed_dagger_curses_the_wearer(self):
        resolved = self._capture("item_interaction_resolved")

        self.dm_core._on_item_interaction_detected({"intent": "equip", "item_name": "cursed dagger", "input": "I equip the cursed dagger"})

        self.assertTrue(resolved[-1]["found"])
        self.assertIn("cursed", self.dm_core.entities["gladstone"]["active_conditions"])

    def test_equipping_an_already_identified_cursed_dagger_does_not_curse_the_wearer(self):
        self.dm_core.apply_condition("cursed dagger", "identified", duration="permanent", dismiss="")

        self.dm_core._on_item_interaction_detected({"intent": "equip", "item_name": "cursed dagger", "input": "I equip the cursed dagger"})

        self.assertNotIn("cursed", self.dm_core.entities["gladstone"]["active_conditions"])

    def test_a_denied_interaction_never_runs_the_program(self):
        # Not actually in inventory -- _resolve_equip_intent denies this as "not_present" before
        # resolved(True, ...) is ever reached, so on_interact must never fire either.
        self.dm_core.entities["gladstone"]["inventory"].remove("cursed dagger")

        self.dm_core._on_item_interaction_detected({"intent": "equip", "item_name": "cursed dagger", "input": "I equip the cursed dagger"})

        self.assertNotIn("cursed", self.dm_core.entities["gladstone"]["active_conditions"])


class TestOnDamageProgram(DMTestCase):
    """!
    @brief The troll's own [entity.on_damage] -- creatures.toml's shipped worked example for
        docs/design/skill_effect_language.md's "A troll's temper".
    """

    def setUp(self):
        super().setUp()
        self._load_ad_hoc_scenario(
            [{"name": "gladstone", "band": 1}, {"name": "troll", "band": 1}], bands=4, enclosed=True,
        )

    def test_dropping_below_half_hp_enrages_the_troll(self):
        self.dm_core.apply_damage("troll", 21, actor_name="gladstone")  # 40 -> 19, 47.5%

        self.assertIn("enraged", self.dm_core.entities["troll"]["active_conditions"])

    def test_staying_above_half_hp_does_not_enrage_the_troll(self):
        self.dm_core.apply_damage("troll", 5, actor_name="gladstone")  # 40 -> 35

        self.assertNotIn("enraged", self.dm_core.entities["troll"].get("active_conditions", {}))

    def test_enraged_is_not_re_applied_once_already_active(self):
        self.dm_core.apply_damage("troll", 21, actor_name="gladstone")
        self.dm_core.entities["troll"]["active_conditions"]["enraged"]["duration"] = "marker"

        self.dm_core.apply_damage("troll", 1, actor_name="gladstone")

        # Still the same marker -- apply_condition would have overwritten it with a fresh
        # {"duration": "scene", ...} entry if the condition step had fired again.
        self.assertEqual(self.dm_core.entities["troll"]["active_conditions"]["enraged"]["duration"], "marker")


class TestOnRoundUpkeepProgram(DMTestCase):
    """!@brief The generic [entity.on_round_upkeep] attachment point (DM_Status.py's own
        run_round_upkeep wrapper) -- no shipped entity authors this yet, so this exercises the
        wiring directly against a synthetic entity."""

    def test_runs_alongside_the_ordinary_per_round_upkeep_loop(self):
        self.dm_core.entities["ticking_dummy"] = {
            "name": "ticking_dummy", "max_hp": 10, "hp": 10,
            "on_round_upkeep": {"do": "condition", "entity": "target", "name": "shaken", "duration": "scene"},
        }
        self.dm_core.scenario_entities.append("ticking_dummy")

        self.dm_core.run_round_upkeep()

        self.assertIn("shaken", self.dm_core.entities["ticking_dummy"]["active_conditions"])

    def test_never_runs_for_a_dead_entity(self):
        self.dm_core.entities["dead_dummy"] = {
            "name": "dead_dummy", "max_hp": 10, "hp": 0,
            "on_round_upkeep": {"do": "condition", "entity": "target", "name": "shaken", "duration": "scene"},
        }
        self.dm_core.scenario_entities.append("dead_dummy")

        self.dm_core.run_round_upkeep()

        self.assertNotIn("shaken", self.dm_core.entities["dead_dummy"].get("active_conditions", {}))


class TestOnEnterProgram(DMTestCase):
    """!@brief The generic [entity.on_enter] attachment point (DM_Rules.py's own
        _enter_location) -- no shipped entity authors this yet, so this exercises the wiring
        directly against a synthetic entity."""

    def test_runs_once_the_entity_is_present_in_a_freshly_entered_location(self):
        self.dm_core.entity_templates["altar"] = {
            "name": "altar", "supertype": "object", "max_hp": 1,
            "on_enter": {"do": "condition", "entity": "target", "name": "identified", "duration": "permanent"},
        }
        self.dm_core.entities["altar"] = dict(self.dm_core.entity_templates["altar"])

        self._load_ad_hoc_scenario([{"name": "gladstone", "band": 1}, {"name": "altar", "band": 1}])

        self.assertIn("identified", self.dm_core.entities["altar"]["active_conditions"])


class TestSummoning(DMTestCase):
    """!
    @brief A spell's own "summon" field (spells.toml's "summon spectral wolf"), DM_Summoning.py's
        _summon_creature/_expire_summon_if_due, and DM_Core.py's own _apply_summon_if_hit/
        _apply_damage_if_hit gating fix.
    """

    def test_summon_creature_places_a_living_non_hostile_ally(self):
        name = self.dm_core._summon_creature({"name": "spectral wolf", "duration": 3})

        self.assertEqual(name, "spectral wolf")
        self.assertIn("spectral wolf", self.dm_core.scenario_entities)
        entity = self.dm_core.entities["spectral wolf"]
        self.assertEqual(entity["band"], self.dm_core.get_band("gladstone"))
        self.assertTrue(entity["ad_hoc"])
        self.assertEqual(entity["summon_expires_in"], 3)
        self.assertFalse(self.dm_core.is_hostile("spectral wolf", self.dm_core.player_name))

    def test_summon_creature_disambiguates_repeat_casts(self):
        first = self.dm_core._summon_creature({"name": "spectral wolf", "duration": 3})
        second = self.dm_core._summon_creature({"name": "spectral wolf", "duration": 3})

        self.assertEqual(first, "spectral wolf")
        self.assertEqual(second, "spectral wolf_2")
        self.assertIn("spectral wolf", self.dm_core.scenario_entities)
        self.assertIn("spectral wolf_2", self.dm_core.scenario_entities)

    def test_summon_creature_returns_none_for_an_unknown_template(self):
        before = list(self.dm_core.scenario_entities)
        name = self.dm_core._summon_creature({"name": "nonexistent thing", "duration": 3})

        self.assertIsNone(name)
        self.assertEqual(self.dm_core.scenario_entities, before)

    def test_expire_summon_if_due_removes_the_entity_at_zero(self):
        self.dm_core._summon_creature({"name": "spectral wolf", "duration": 1})
        self.dm_core.run_round_upkeep()
        self.assertNotIn("spectral wolf", self.dm_core.scenario_entities)
        self.assertNotIn("spectral wolf", self.dm_core.entities["spectral wolf"].get("active_conditions", {}))

    def test_run_round_upkeep_survives_an_expiry_mid_iteration(self):
        # Regression check for the list(self.scenario_entities) snapshot -- without it, removing
        # "spectral wolf" from self.scenario_entities while still iterating it could skip
        # whatever's ordered right after it. Order matters here: the wolf has to land *before*
        # the troll in scenario_entities for a missing snapshot to actually skip the troll's own
        # regeneration, so the troll is instanced and appended after the wolf, not before.
        # Empty entities list -- avoids re-instancing "gladstone" a second time as an orphaned
        # "gladstone_2" (see the previous test's own comment for why).
        self._load_ad_hoc_scenario([])
        self.dm_core._summon_creature({"name": "spectral wolf", "duration": 1})
        self.dm_core._instance_entities([{"name": "troll", "band": 1}])
        self.dm_core.scenario_entities.append("troll")
        self.assertEqual(self.dm_core.scenario_entities, ["gladstone", "spectral wolf", "troll"])
        self.dm_core.apply_damage("troll", 10)

        self.dm_core.run_round_upkeep()

        self.assertNotIn("spectral wolf", self.dm_core.scenario_entities)
        self.assertGreater(self.dm_core.get_current_hp("troll"), 30)  # still healed this round

    def test_apply_summon_if_hit_with_no_current_target_auto_succeeds(self):
        # Empty entities list -- _instance_location_persistent_names' own "guarantee" fallback
        # inserts self.player_name directly without re-instancing it, so this doesn't collide
        # with the "gladstone" the parent setUp already instanced once via "arena" (unlike
        # explicitly listing {"name": "gladstone", ...} again here, which would instead produce
        # a second, orphaned "gladstone_2" instance -- see town.toml's own real-scenario
        # precedent for this same "never name the player" convention).
        self._load_ad_hoc_scenario([])
        self.assertIsNone(self.dm_core.current_target)
        resolved = self._capture("action_resolved")

        with patch("random.randint", return_value=4):
            self.dm_core._on_turn_detected({
                "clauses": [{"kind": "action", "skill": "summon spectral wolf"}], "input": "I summon a wolf",
            })

        self.assertIn("spectral wolf", self.dm_core.scenario_entities)
        action = resolved[-1]["actions"][0]
        self.assertEqual([e.name for e in action.effects if isinstance(e, SummonEffect)], ["spectral wolf"])
        self.assertFalse(any(isinstance(effect, DamageEffect) for effect in action.effects))

    def test_apply_summon_if_hit_against_a_hostile_target_ticks_this_rounds_upkeep_too(self):
        # arena.toml's own default scenario already has a hostile wolf as current_target --
        # this exercises the targeted cast path (a flat check against the spell's own authored
        # difficulty of 10; gladstone's 2D arcane at a fixed per-die value of 5 rolls exactly
        # 10), and confirms _resolve_combat_round's own run_round_upkeep (which fires later in
        # the same turn) already counts this round against the freshly-summoned wolf's own
        # duration.
        resolved = self._capture("round_resolved")

        with patch("random.randint", return_value=5):
            self.dm_core._on_turn_detected({
                "clauses": [{"kind": "action", "skill": "summon spectral wolf"}], "input": "I summon a wolf",
            })

        self.assertIn("spectral wolf", self.dm_core.scenario_entities)
        action = resolved[-1]["actions"][0]
        self.assertEqual([e.name for e in action.effects if isinstance(e, SummonEffect)], ["spectral wolf"])
        self.assertEqual(self.dm_core.entities["spectral wolf"]["summon_expires_in"], 3)  # 4 - 1


class TestBandit(DMTestCase):
    # "bandit" is field.toml's own local entity now (creatures.toml no longer carries one --
    # see its own comment) -- booting "field" first is what makes it resolvable at all, before
    # setUp below overrides self.dm_core.scenario/re-runs load_scenario() with a custom band
    # layout.
    scenario_name = "field"

    def setUp(self):
        super().setUp()
        self._load_ad_hoc_scenario(
            [{"name": "gladstone", "band": 1}, {"name": "bandit", "band": 5}], bands=8, enclosed=False,
        )


    def test_favors_the_bow_at_a_distance(self):
        # Starting gap is 4 -- exactly the short bow's own range, so it's both "not adjacent"
        # (distance_to_target > 0, the behavior's own requirement) and actually reachable.
        behavior = self.dm_core.choose_behavior("bandit", "gladstone")
        self.assertEqual(behavior["action"], "short bow")

        turn = self.dm_core.resolve_behavior_action("bandit", "gladstone")
        self.assertEqual(turn.skill, "missiles")
        self.assertNotIsInstance(turn, MovementOutcome)


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


class TestConditionModifiers(DMTestCase):
    """!
    @brief get_condition_modifier (DM_Status.py) and its use in resolve_action/
        resolve_opposed_action (DM_Combat.py) -- a [[condition]] entry's own modifier now
        actually costs dice/pips/bonus, not just narration (see CLAUDE.md's "Status and
        conditions").
    """

    def test_get_condition_modifier_sums_matching_active_conditions(self):
        # rules.toml's own "wounded" [[condition]] entry is {dice: -1, pips: 0, bonus: 0}.
        self.dm_core.apply_condition("gladstone", "wounded", duration="permanent", dismiss="")
        self.assertEqual(
            self.dm_core.get_condition_modifier("gladstone"),
            {"dice": -1, "pips": 0, "bonus": 0},
        )

    def test_get_condition_modifier_ignores_conditions_with_no_rules_entry(self):
        # "hidden" is a plain presence flag (see items.toml's dart trap) with no [[condition]]
        # entry of its own -- it must not silently contribute a modifier.
        self.dm_core.apply_condition("gladstone", "hidden", duration="permanent", dismiss="")
        self.assertEqual(
            self.dm_core.get_condition_modifier("gladstone"),
            {"dice": 0, "pips": 0, "bonus": 0},
        )

    def test_resolve_action_folds_condition_dice_penalty_into_the_roll(self):
        # gladstone's blades: 5D+0. "wounded" is -1D, same floor-at-zero rule dice_penalty uses.
        self.dm_core.apply_condition("gladstone", "wounded", duration="permanent", dismiss="")
        with patch("random.randint", return_value=3):
            result = self.dm_core.resolve_action("gladstone", "blades")
        self.assertEqual(result["roll"], 12)  # (5 - 1) * 3

    def test_resolve_opposed_action_applies_the_defenders_own_condition_modifier(self):
        # The defender's active_conditions reduce their own roll independently of
        # dice_penalty, which never touches the defender's side at all (see
        # TestMultipleActions.test_resolve_opposed_action_penalty_never_touches_the_defenders_roll).
        self.dm_core.entities["test_defender"] = {
            "name": "test_defender", "skills": {"dodge": {"dice": 6, "pips": 0}},
        }
        self.dm_core.apply_condition("test_defender", "stunned", duration="fleeting", dismiss="")
        with patch("random.randint", return_value=3):
            result = self.dm_core.resolve_opposed_action("gladstone", "blades", "test_defender")
        self.assertEqual(result["difficulty"], 15)  # (6 - 1) * 3


class TestScenarioLoading(DMTestCase):
    def test_duplicate_entities_get_unique_instance_names(self):
        # arena.toml's own location lists gladstone and thane (persistent across the whole
        # location); its one room lists wolf twice (room-local) -- scenario_entities is
        # persistent_entities + this room's own instances, in that order.
        self.assertEqual(self.dm_core.scenario_entities, ["gladstone", "thane", "wolf", "wolf_2"])
        self.assertIn("wolf", self.dm_core.entities)
        self.assertIn("wolf_2", self.dm_core.entities)

    def test_current_target_defaults_to_the_first_hostile_entity_skipping_allies(self):
        # thane (non-hostile, an ally) is listed after both wolves in arena.toml, but even if
        # it weren't, current_target must never default to an ally -- it's chosen by hostility,
        # not by list position.
        self.assertEqual(self.dm_core.current_target, "wolf")


class TestMultiInstanceTargeting(DMTestCase):
    """!
    @brief DMCore._resolve_named_instance_ambiguity, exercised through _apply_target_redirect
        -- arena's own "wolf"/"wolf_2" (both live, both max_hp 16) are the fixture. Every
        clause here passes "wolf" as NLPCore's own naive target guess (map_to_target always
        prefers the plain species name over a literal "wolf_2" string -- see DM_Core.py's own
        module note) alongside varied input phrasing, the same shape map_to_target's real
        output would take.
    """

    def test_no_qualifier_leaves_the_naive_guess_unchanged(self):
        self.dm_core._apply_target_redirect("wolf", "I attack the wolf")
        self.assertEqual(self.dm_core.current_target, "wolf")

    def test_ordinal_second_redirects_to_the_second_instance(self):
        self.dm_core._apply_target_redirect("wolf", "I attack the second wolf")
        self.assertEqual(self.dm_core.current_target, "wolf_2")

    def test_other_redirects_away_from_the_already_current_instance(self):
        self.assertEqual(self.dm_core.current_target, "wolf")  # the default, before redirect
        self.dm_core._apply_target_redirect("wolf", "I attack the other wolf")
        self.assertEqual(self.dm_core.current_target, "wolf_2")

    def test_wounded_redirects_to_the_instance_actually_below_the_cutoff(self):
        # wolf max_hp 16; 11 damage -> 5/16 = 0.3125, under the 0.40 "wounded" cutoff. wolf_2
        # stays undamaged, so only wolf itself qualifies as "the wounded wolf" here.
        self.dm_core.apply_damage("wolf", 11)
        self.dm_core._apply_target_redirect("wolf", "I attack the wounded wolf")
        self.assertEqual(self.dm_core.current_target, "wolf")

    def test_healthy_redirects_away_from_the_wounded_instance(self):
        self.dm_core.apply_damage("wolf", 11)  # wolf: 5/16, under the cutoff; wolf_2: full
        self.dm_core._apply_target_redirect("wolf", "I attack the healthy wolf")
        self.assertEqual(self.dm_core.current_target, "wolf_2")

    def test_single_instance_short_circuits_with_no_redirect(self):
        # thane has no same-family sibling at all -- the qualifier word is simply irrelevant.
        self.assertEqual(
            self.dm_core._resolve_named_instance_ambiguity("thane", "talk to the other thane"),
            "thane",
        )


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
            raise ConnectionError("no Ollama")

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
            raise ConnectionError("no Ollama")

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
        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value=self._fake_creation(location="ground")):
            self.dm_core._on_improvisation_requested({
                "intent": "take", "phrase": "a stone", "input": "pick up a stone",
            })

        self.assertIn("stone", self.dm_core.entities["gladstone"]["inventory"])
        self.assertNotIn("stone", self.dm_core._current_ground_items())
        self.assertEqual(self.catalog_events, [
            {"entities": [{"name": "stone", "description": "A smooth grey stone."}]},
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
        starting_hp = self.dm_core.get_current_hp("gladstone")

        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value=entity):
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
        starting_hp = self.dm_core.get_current_hp("gladstone")
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
        self.assertLess(self.dm_core.get_current_hp("gladstone"), starting_hp)

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
        self.assertEqual(self.dm_core.get_band("cave rat"), self.dm_core.get_band("gladstone"))

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
        self.dm_core._current_ground_items().append("a stone")
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
    @brief End-to-end proof (mocked LLM, no live Ollama needed) that Rules/Fantasy/
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
        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value=fake_result):
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
        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value=fake_result):
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

        with patch("dm.DM_Improvisation.generate_ad_hoc_item") as mock_generate:
            self.dm_core._on_improvisation_requested({
                "intent": "trade", "phrase": "a lantern", "input": "buy a lantern",
            })

        mock_generate.assert_not_called()  # short-circuits before ever asking the LLM
        self.assertEqual(len(not_understood), 1)

    def test_implausible_purchase_declines(self):
        not_understood = self._capture("action_not_understood")
        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value={"created": False, "reason": "declined"}):
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
        end-to-end Ollama round trip).
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
        with patch("resolution.NPC_Generation._real_call_chat_completion", new=fake_call):
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
        # npc_generation_test.toml's own generated_stranger authors this override toward the
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

        with patch("resolution.NPC_Generation._real_call_chat_completion", new=different_fake_call):
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

    def test_elf_character_gains_elvish_alongside_the_templates_own_common(self):
        character = {
            "race": "elf",
            "allocation": {"arcane": 5, "stealth": 5, "observation": 5},
        }
        dm = DMCore(EventBus(), scenario_name="arena", character=character)
        self.assertEqual(dm.entities["gladstone"]["languages"], ["common", "elvish"])

    def test_human_character_gains_no_new_language_since_common_is_already_there(self):
        character = {
            "race": "human",
            "allocation": {"arcane": 5, "stealth": 5, "observation": 5},
        }
        dm = DMCore(EventBus(), scenario_name="arena", character=character)
        self.assertEqual(dm.entities["gladstone"]["languages"], ["common"])


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


class TestOllamaLauncher(unittest.TestCase):
    """!
    @brief Ollama_Launcher.py's ensure_ollama_running -- exercised entirely through its own
        is_reachable/which/popen/download/fetch_text injection seams (the same dependency-
        injection pattern call_chat_completion's own callers use elsewhere), so this needs no
        real network probe, PATH lookup, subprocess spawn, or multi-gigabyte download. Every
        test that could otherwise reach _install_vendored_ollama passes its own isolated
        vendor_dir (a TemporaryDirectory), never the real vendor/ this module ships with.
    """

    def _write_fake_zip(self, zip_path):
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("ollama.exe", b"fake-binary-contents")

    def test_already_running_is_a_noop(self):
        fake_which = MagicMock()
        fake_popen = MagicMock()
        fake_pull = MagicMock()
        result = ensure_ollama_running(
            is_reachable=lambda host: True, which=fake_which, popen=fake_popen,
            list_models=lambda host: ["gemma4:latest"], pull_model=fake_pull,
        )
        self.assertIsNone(result)
        fake_which.assert_not_called()
        fake_popen.assert_not_called()
        fake_pull.assert_not_called()  # already pulled -- nothing to do

    def test_spawns_ollama_serve_when_system_executable_found(self):
        fake_process = MagicMock(pid=1234)
        fake_popen = MagicMock(return_value=fake_process)
        result = ensure_ollama_running(
            is_reachable=lambda host: False, which=lambda name: "C:\\real\\ollama.exe", popen=fake_popen,
            ready_timeout=0,  # never becomes reachable in this test -- skip the model-pull wait
        )
        self.assertIs(result, fake_process)
        args, kwargs = fake_popen.call_args
        self.assertEqual(args[0], ["C:\\real\\ollama.exe", "serve"])

    def test_failed_launch_returns_none(self):
        def exploding_popen(*args, **kwargs):
            raise OSError("no permission")

        result = ensure_ollama_running(
            is_reachable=lambda host: False, which=lambda name: "C:\\real\\ollama.exe", popen=exploding_popen,
        )
        self.assertIsNone(result)

    def test_missing_everywhere_and_failed_install_returns_none(self):
        with tempfile.TemporaryDirectory() as vendor_dir:
            def failing_download(url, dest_path, log):
                raise ConnectionError("offline")

            fake_popen = MagicMock()
            result = ensure_ollama_running(
                is_reachable=lambda host: False, which=lambda name: None, popen=fake_popen,
                download=failing_download, fetch_text=lambda url: "", vendor_dir=vendor_dir,
            )
            self.assertIsNone(result)
            fake_popen.assert_not_called()

    def test_installs_a_vendored_copy_when_nothing_found(self):
        with tempfile.TemporaryDirectory() as vendor_dir:
            def fake_download(url, dest_path, log):
                self._write_fake_zip(dest_path)

            fake_process = MagicMock(pid=99)
            fake_popen = MagicMock(return_value=fake_process)

            result = ensure_ollama_running(
                is_reachable=lambda host: False, which=lambda name: None, popen=fake_popen,
                download=fake_download, fetch_text=lambda url: "", vendor_dir=vendor_dir,
                ready_timeout=0,  # never becomes reachable in this test -- skip the model-pull wait
            )

            self.assertIs(result, fake_process)
            args, kwargs = fake_popen.call_args
            self.assertTrue(args[0][0].endswith("ollama.exe"))
            self.assertTrue(os.path.exists(args[0][0]))
            # The downloaded zip archive itself is cleaned up after extraction, not left behind.
            self.assertEqual(os.listdir(vendor_dir), ["ollama.exe"])

    def test_reuses_a_previously_vendored_copy_without_downloading(self):
        with tempfile.TemporaryDirectory() as vendor_dir:
            existing = os.path.join(vendor_dir, "ollama.exe")
            with open(existing, "wb") as f:
                f.write(b"already installed")

            fake_download = MagicMock()
            fake_process = MagicMock(pid=7)
            fake_popen = MagicMock(return_value=fake_process)

            result = ensure_ollama_running(
                is_reachable=lambda host: False, which=lambda name: None, popen=fake_popen,
                download=fake_download, vendor_dir=vendor_dir,
                ready_timeout=0,  # never becomes reachable in this test -- skip the model-pull wait
            )

            self.assertIs(result, fake_process)
            fake_download.assert_not_called()
            args, kwargs = fake_popen.call_args
            self.assertEqual(args[0][0], existing)

    def test_checksum_mismatch_discards_the_download(self):
        with tempfile.TemporaryDirectory() as vendor_dir:
            def fake_download(url, dest_path, log):
                self._write_fake_zip(dest_path)

            fake_popen = MagicMock()
            result = ensure_ollama_running(
                is_reachable=lambda host: False, which=lambda name: None, popen=fake_popen,
                download=fake_download,
                fetch_text=lambda url: "0" * 64 + "  ./ollama-windows-amd64.zip\n",
                vendor_dir=vendor_dir,
            )

            self.assertIsNone(result)
            fake_popen.assert_not_called()
            self.assertEqual(os.listdir(vendor_dir), [])

    def test_pulls_a_missing_model_once_the_server_is_reachable(self):
        fake_pull = MagicMock()
        result = ensure_ollama_running(
            is_reachable=lambda host: True, list_models=lambda host: [], pull_model=fake_pull,
        )
        self.assertIsNone(result)  # already running -- no process for this call to own
        fake_pull.assert_called_once()
        args, kwargs = fake_pull.call_args
        self.assertEqual(args[1], "gemma4")  # (host, model, log)

    def test_skips_pulling_a_model_already_present_under_its_implicit_latest_tag(self):
        # /api/tags always reports a tag ("gemma4:latest"), even though the bare "gemma4" (the
        # default model requested here) never explicitly names one -- _model_already_pulled has
        # to bridge that, not just do an exact string match.
        fake_pull = MagicMock()
        ensure_ollama_running(
            is_reachable=lambda host: True, list_models=lambda host: ["gemma4:latest"], pull_model=fake_pull,
        )
        fake_pull.assert_not_called()

    def test_gives_up_on_model_check_if_server_never_becomes_reachable(self):
        fake_list_models = MagicMock()
        fake_pull = MagicMock()
        logged = []
        result = ensure_ollama_running(
            is_reachable=lambda host: False, which=lambda name: "C:\\real\\ollama.exe",
            popen=MagicMock(return_value=MagicMock(pid=1)), list_models=fake_list_models,
            pull_model=fake_pull, ready_timeout=0, log=logged.append,
        )
        self.assertIsNotNone(result)  # the server process itself still spawned successfully
        fake_list_models.assert_not_called()
        fake_pull.assert_not_called()
        self.assertTrue(any("never became reachable" in message for message in logged))

    def test_default_pull_model_tolerates_a_total_with_no_completed_yet(self):
        # Regression: Ollama's own "pulling <digest>" status line can carry "total" before
        # "completed" has appeared at all -- _default_pull_model used to compute
        # `completed * 100 // total` unconditionally once total was truthy, crashing with
        # "unsupported operand type(s) for *: 'NoneType' and 'int'" on a real pull.
        class _FakeStreamResponse:
            def __init__(self, lines):
                self._lines = lines

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                return iter(self._lines)

        lines = [
            json.dumps({"status": "pulling manifest"}).encode("utf-8"),
            json.dumps({"status": "pulling abc123", "total": 100}).encode("utf-8"),
            json.dumps({"status": "pulling abc123", "total": 100, "completed": 50}).encode("utf-8"),
            json.dumps({"status": "success"}).encode("utf-8"),
        ]
        logged = []
        with patch("urllib.request.urlopen", return_value=_FakeStreamResponse(lines)):
            Ollama_Launcher._default_pull_model("http://127.0.0.1:11434", "gemma4", logged.append)

        self.assertIn("success", logged)


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
        self.assertFalse(result.success)
        self.assertEqual(result.defender, "chest")
        self.assertIsNone(result.opposing_skill)
        self.assertEqual(result.difficulty, 12)
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
        self.assertTrue(result.success)
        self.assertEqual(result.defender, "chest")
        self.assertFalse(any(isinstance(effect, LootEffect) for effect in result.effects))

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
        self._stub_roll_dice(99)
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

    def test_taking_the_target_itself_is_not_takeable(self):
        self._unlock_the_chest()
        self._open_the_chest()

        self.dm_core._on_item_interaction_detected({
            "intent": "take", "item_name": "chest", "input": "I take the chest",
        })

        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_takeable")

    def test_examine_currency_reports_amount_without_moving_it(self):
        self._unlock_the_chest()
        self._open_the_chest()

        self.dm_core._on_item_interaction_detected({
            "intent": "examine", "item_name": "currency", "input": "I check the chest for coins",
        })

        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["description"], "20 currency")
        self.assertEqual(self.dm_core.entities["chest"]["currency"], 20)

    def test_taking_currency_moves_all_of_it(self):
        self._unlock_the_chest()
        self._open_the_chest()
        starting_currency = self.dm_core.entities["gladstone"]["currency"]

        self.dm_core._on_item_interaction_detected({
            "intent": "take", "item_name": "currency", "input": "I take the coins",
        })

        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["amount"], 20)
        self.assertEqual(self.dm_core.entities["chest"]["currency"], 0)
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], starting_currency + 20)


class TestItemTargetedSkillCheck(DMTestCase):
    scenario_name = "dungeon"

    def setUp(self):
        super().setUp()
        self.action_events = self._capture("action_resolved")
        self.round_events = self._capture("round_resolved")
        self.dm_core.dismiss_condition("chest", "locked")
        self.dm_core.dismiss_condition("chest", "closed")

    def _check_the_dagger(self, roll_result):
        self._stub_roll_dice(roll_result)
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
        self.assertTrue(result.success)
        self.assertEqual(result.defender, "cursed dagger")
        self.assertIsNone(result.opposing_skill)
        reveal_effects = [effect for effect in result.effects if isinstance(effect, RevealEffect)]
        self.assertEqual(len(reveal_effects), 1)
        self.assertEqual(reveal_effects[0].tags, ["cursed"])
        self.assertTrue(self.dm_core.is_identified("cursed dagger"))


class TestOpenClose(DMTestCase):
    scenario_name = "dungeon"

    def setUp(self):
        super().setUp()
        self.resolved = self._capture("item_interaction_resolved")

    def _unlock_the_chest(self):
        self._stub_roll_dice(99)
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


    def test_give_nudges_a_favor_attitude_toward_the_recipient(self):
        # "favor" (DM_Social.py's nudge_attitude_from_event, wired from _resolve_transfer_intent)
        # -- magnitude scaled by the gift's own TOML value (health potion = 15) against
        # DM_Inventory.py's SIGNIFICANT_VALUE (25): 15/25 = 0.6. A gift reads as increased
        # closeness now, not a formal debt -- obligation was dropped as an axis entirely (see
        # CLAUDE.md's "Social and attitudes").
        base_familiarity = self.dm_core.entities["innkeeper"]["attitudes"]["default"][2]

        self.dm_core._on_item_interaction_detected({
            "intent": "give", "item_name": "health potion", "input": "I give the innkeeper a health potion",
        })

        familiarity = self.dm_core.get_attitude("innkeeper", self.dm_core.player_name)[2]
        self.assertAlmostEqual(familiarity, base_familiarity + 6 * (15 / 25))

    def test_taking_currency_from_a_living_entity_nudges_a_theft_attitude(self):
        # "theft" (same wiring, currency branch) -- magnitude scaled by however much moved
        # against SIGNIFICANT_VALUE, capped at 1.0 (the innkeeper's own 40 currency exceeds it).
        # Stealing reads as reduced closeness now, not reduced trust -- trust was dropped as an
        # axis entirely (see CLAUDE.md's "Social and attitudes").
        base_familiarity = self.dm_core.entities["innkeeper"]["attitudes"]["default"][2]

        self.dm_core._on_item_interaction_detected({
            "intent": "take", "item_name": "currency", "input": "I take the innkeeper's coin purse",
        })

        familiarity = self.dm_core.get_attitude("innkeeper", self.dm_core.player_name)[2]
        self.assertAlmostEqual(familiarity, base_familiarity - 12)  # -12 at magnitude 1.0 (theft's own full-strength familiarity delta)

    def test_taking_an_item_from_a_living_entity_nudges_a_theft_attitude(self):
        # "cursed dagger" (value 5), not "health potion" -- gladstone's own template already
        # starts carrying health potions (see test_give's own assertion above), which would
        # route this through the "already owned" self-transfer no-op path instead of real theft.
        self.dm_core.entities["innkeeper"].setdefault("inventory", []).append("cursed dagger")
        base_disposition = self.dm_core.entities["innkeeper"]["attitudes"]["default"][0]

        self.dm_core._on_item_interaction_detected({
            "intent": "take", "item_name": "cursed dagger", "input": "I take the innkeeper's cursed dagger",
        })

        disposition = self.dm_core.get_attitude("innkeeper", self.dm_core.player_name)[0]
        self.assertAlmostEqual(disposition, base_disposition - 15 * (5 / 25))

    def test_taking_from_an_incapacitated_victim_is_not_theft_they_were_aware_of(self):
        # An unconscious/dead victim isn't aware of anything being taken from them -- the item
        # still moves (transfer_item doesn't care about HP), but no attitude nudge registers,
        # since nudge_attitude_from_event itself gates on the target actually being alive.
        self.dm_core.entities["innkeeper"].setdefault("inventory", []).append("cursed dagger")
        self.dm_core.apply_damage("innkeeper", 9999)

        self.dm_core._on_item_interaction_detected({
            "intent": "take", "item_name": "cursed dagger", "input": "I take the innkeeper's cursed dagger",
        })

        self.assertIn("cursed dagger", self.dm_core.entities["gladstone"]["inventory"])
        self.assertNotIn("action_attitude_deltas", self.dm_core.entities["innkeeper"])


    def test_trade_charges_the_items_toml_value_and_moves_it_to_the_player(self):
        # dungeon.toml's chest holds "cursed dagger" (value = 5); tavern's innkeeper has
        # neither, so build an ad-hoc scenario reusing the chest as a "shop" for this test.
        self._load_ad_hoc_scenario([{"name": "gladstone", "band": 1}, {"name": "chest", "band": 1}])
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

    def test_give_declines_with_no_recipient(self):
        # Empty entities list -- _instance_location_persistent_names' own "guarantee" fallback
        # inserts self.player_name directly without re-instancing it, so this doesn't collide
        # with the "gladstone" the parent setUp already instanced once via "arena" (unlike
        # explicitly listing {"name": "gladstone", ...} again here, which would instead produce
        # a second, orphaned "gladstone_2" instance -- see town.toml's own real-scenario
        # precedent for this same "never name the player" convention).
        self._load_ad_hoc_scenario([])

        self.dm_core._on_item_interaction_detected({
            "intent": "give", "item_name": "health potion", "input": "I give away a health potion",
        })

        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "no_recipient")
        self.assertIn("health potion", self.dm_core.entities["gladstone"]["inventory"])

    def test_trade_declines_when_player_cant_afford_it(self):
        self._load_ad_hoc_scenario([{"name": "gladstone", "band": 1}, {"name": "chest", "band": 1}])
        self.dm_core.dismiss_condition("chest", "locked")
        self.dm_core.dismiss_condition("chest", "closed")
        self.dm_core.entities["gladstone"]["currency"] = 0

        self.dm_core._on_item_interaction_detected({
            "intent": "trade", "item_name": "cursed dagger", "input": "I buy the cursed dagger",
        })

        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "cant_afford")
        self.assertEqual(result["price"], 5)
        self.assertNotIn("cursed dagger", self.dm_core.entities["gladstone"]["inventory"])


class TestUseItem(DMTestCase):
    def setUp(self):
        super().setUp()
        self.resolved = self._capture("item_interaction_resolved")

    def _use(self, item_name="health potion", roll_result=6):
        self._stub_roll_dice(roll_result)
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


class TestCrafting(DMTestCase):
    # items.toml's "iron dagger" ([entity.craft]: skill=["strength","finesse"], difficulty=10,
    # requires_station="forge", materials=2x iron ingot + 1x leather strip) is globally loaded
    # (items.toml, not scenario-local) regardless of scenario_name -- "arena" (DMTestCase's own
    # default) is fine; only the "forge" station itself (town.toml-local) needs manually placing.
    def setUp(self):
        super().setUp()
        self.action_events = self._capture("action_resolved")
        self.round_events = self._capture("round_resolved")
        self.dm_core.entities["gladstone"].setdefault("inventory", []).extend(
            ["iron ingot", "iron ingot", "leather strip"],
        )

    def _place_forge(self):
        self.dm_core.entities["forge"] = {
            "name": "forge", "supertype": "object", "subtype": "prop", "provides_station": "forge",
        }
        self.dm_core.scenario_entities.append("forge")

    def _craft(self, roll_result, item_name="iron dagger", extra_clauses=None):
        self._stub_roll_dice(roll_result)
        clauses = [{"kind": "item", "intent": "craft", "item_name": item_name}]
        clauses.extend(extra_clauses or [])
        self.dm_core._on_turn_detected({"clauses": clauses, "input": "I craft an iron dagger"})
        return self.action_events[-1]["actions"][0]

    def test_missing_station_fails_without_rolling_or_consuming_materials(self):
        result = self._craft(roll_result=99)

        self.assertIsInstance(result, MissingStationOutcome)
        self.assertEqual(self.dm_core.entities["gladstone"]["inventory"].count("iron ingot"), 2)

    def test_missing_materials_fails_without_rolling(self):
        self._place_forge()
        self.dm_core.entities["gladstone"]["inventory"] = []

        result = self._craft(roll_result=99)

        self.assertIsInstance(result, MissingMaterialsOutcome)

    def test_not_craftable_item_fails_without_rolling(self):
        # "health potion" has its own [entity.test] but no [entity.craft] block at all.
        self._place_forge()

        result = self._craft(roll_result=99, item_name="health potion")

        self.assertIsInstance(result, NotCraftableOutcome)

    def test_successful_craft_consumes_materials_and_places_the_item(self):
        self._place_forge()

        result = self._craft(roll_result=99)  # clears iron dagger's own difficulty (10)

        self.assertTrue(result.success)
        craft_effects = [effect for effect in result.effects if isinstance(effect, CraftEffect)]
        self.assertEqual([e.item_name for e in craft_effects], ["iron dagger"])
        self.assertIsNone(result.defender)
        self.assertIsNone(result.opposing_skill)
        inventory = self.dm_core.entities["gladstone"]["inventory"]
        self.assertEqual(inventory.count("iron ingot"), 0)
        self.assertEqual(inventory.count("leather strip"), 0)
        self.assertIn("iron dagger", inventory)

    def test_failed_craft_still_consumes_materials_but_grants_nothing(self):
        self._place_forge()

        result = self._craft(roll_result=0)  # never clears difficulty 10

        self.assertFalse(result.success)
        self.assertFalse(any(isinstance(effect, CraftEffect) for effect in result.effects))
        inventory = self.dm_core.entities["gladstone"]["inventory"]
        self.assertEqual(inventory.count("iron ingot"), 0)
        self.assertEqual(inventory.count("leather strip"), 0)
        self.assertNotIn("iron dagger", inventory)

    def test_crafting_never_engages_combat(self):
        self._place_forge()

        self._craft(roll_result=99)

        self.assertEqual(self.round_events, [])

    def test_dice_penalty_from_a_multi_clause_turn_reaches_the_craft_roll(self):
        self._place_forge()
        seen_dice_penalties = []
        original_resolve_action = self.dm_core.resolve_action

        def spy_resolve_action(entity_name, skill_name, difficulty=0, dice_penalty=0):
            seen_dice_penalties.append(dice_penalty)
            return original_resolve_action(entity_name, skill_name, difficulty, dice_penalty=dice_penalty)

        self.dm_core.resolve_action = spy_resolve_action

        self._craft(
            roll_result=99,
            extra_clauses=[{"kind": "item", "intent": "examine", "item_name": "iron dagger"}],
        )

        self.assertEqual(seen_dice_penalties, [1])


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
        self.assertEqual(result.defender, "innkeeper")
        self.assertNotIn("round", self.action_events[0])
        self.assertFalse(any(isinstance(effect, DamageEffect) for effect in result.effects))


    def test_fighting_a_hostile_target_still_batches_into_round_resolved(self):
        # Sanity check the branch didn't regress combat routing for an actually hostile target.
        # "fire elemental" (creatures.toml) rather than "wolf" -- it's the one creature still
        # loaded via load_rules regardless of scenario, so it's resolvable here even though
        # this fixture boots "tavern" (which never references it).
        self._load_ad_hoc_scenario([
            { "name": "gladstone", "band": 1 },
            { "name": "fire elemental", "band": 1 },
        ])

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

    def _talk(self, input_text, sentiment=None, score=1.0):
        self.dm_core._on_dialogue_detected({"input": input_text, "sentiment": sentiment, "sentiment_score": score})
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
        # Empty entities list -- _instance_location_persistent_names' own "guarantee" fallback
        # inserts self.player_name directly without re-instancing it, so this doesn't collide
        # with the "gladstone" the parent setUp already instanced once via "arena" (unlike
        # explicitly listing {"name": "gladstone", ...} again here, which would instead produce
        # a second, orphaned "gladstone_2" instance -- see town.toml's own real-scenario
        # precedent for this same "never name the player" convention).
        self._load_ad_hoc_scenario([])

        result = self._talk("hello? is anyone there")

        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "no_one_here")

    def test_every_dialogue_resolution_is_tagged_with_current_presence(self):
        result = self._talk("i talk to the innkeeper")
        self.assertEqual(set(result["present_entities"]), set(self.dm_core.scenario_entities))

    def test_positive_sentiment_raises_disposition_and_negative_lowers_it(self):
        # score=1.0 (max confidence) with SENTIMENT_INTENSITY_SCALE at its current default of 1
        # means one turn's own nudge is exactly +-1.0 -- see DM_Social.py's nudge_attitude.
        before = self.dm_core.get_attitude("innkeeper", self.dm_core.player_name)[0]

        self._talk("i talk to the innkeeper", sentiment="positive", score=1.0)
        after_positive = self.dm_core.get_attitude("innkeeper", self.dm_core.player_name)[0]
        self.assertEqual(after_positive, before + 1.0)

        self._talk("i talk to the innkeeper", sentiment="negative", score=1.0)
        after_negative = self.dm_core.get_attitude("innkeeper", self.dm_core.player_name)[0]
        self.assertEqual(after_negative, before)

    def test_a_more_confident_sentiment_score_moves_disposition_further(self):
        # The whole point of scaling by score instead of a flat per-sentiment amount: a line the
        # classifier was only mildly confident about should move the needle less than one it was
        # very confident about. Calls nudge_attitude directly (not through _talk/dialogue
        # resolution) against two different entities so the two magnitudes can be compared
        # without one call's own drift compounding onto the other's.
        self.dm_core.nudge_attitude("innkeeper", self.dm_core.player_name, {"disposition": ("positive", 0.55)})
        mild_drift = self.dm_core.entities["innkeeper"]["attitude_deltas"][self.dm_core.player_name][0]

        self.dm_core.entities["test_entity_two"] = {"name": "test_entity_two"}
        self.dm_core.nudge_attitude("test_entity_two", self.dm_core.player_name, {"disposition": ("positive", 0.95)})
        strong_drift = self.dm_core.entities["test_entity_two"]["attitude_deltas"][self.dm_core.player_name][0]

        self.assertEqual(mild_drift, 0.55)
        self.assertEqual(strong_drift, 0.95)
        self.assertLess(mild_drift, strong_drift)

    def test_sentiment_drift_is_capped_and_reflected_in_this_turn_own_reply(self):
        base = self.dm_core.entities["innkeeper"].get("attitudes", {}).get("default", [0, 0, 0])[0]

        for _ in range(50):
            result = self._talk("i talk to the innkeeper", sentiment="positive", score=1.0)

        disposition = self.dm_core.get_attitude("innkeeper", self.dm_core.player_name)[0]

        self.assertEqual(disposition, base + TALK_ATTITUDE_DRIFT_CAP)
        # This turn's own returned attitude description already reflects the (capped) drift --
        # the local-classification design's whole point over an async LLM call, which would
        # only apply the nudge after the reply had already been built.
        self.assertTrue(result["attitude"])

    def test_neutral_or_unrecognized_sentiment_never_nudges_attitude(self):
        before = self.dm_core.get_attitude("innkeeper", self.dm_core.player_name)[0]
        self._talk("i ask the innkeeper about the road", sentiment=None, score=0.0)
        self.assertEqual(self.dm_core.get_attitude("innkeeper", self.dm_core.player_name)[0], before)

    def test_attitude_drift_survives_save_and_load(self):
        slot_name = "test_sentiment_drift_slot"
        self.addCleanup(shutil.rmtree, self.dm_core._save_slot_dir(slot_name), ignore_errors=True)

        self._talk("i talk to the innkeeper", sentiment="negative")
        drifted = self.dm_core.get_attitude("innkeeper", self.dm_core.player_name)[0]

        self.dm_core.save_game(slot_name)
        self.dm_core.load_game(slot_name)

        self.assertEqual(self.dm_core.get_attitude("innkeeper", self.dm_core.player_name)[0], drifted)


class TestLanguageBarrier(DMTestCase):
    """!
    @brief DM_Dialogue.py's _detect_language_barrier -- both entities default to ["common"]
        (entity_schema.toml) unless an author narrows one, so this only ever fires once a
        scenario/entity deliberately restricts a "languages" list, or the player's own chosen
        race (races.toml) doesn't cover it. scenario "tavern" for the same friendly-innkeeper
        fixture TestFreeformDialogue already uses.
    """
    scenario_name = "tavern"

    def setUp(self):
        super().setUp()
        self.dialogue_events = self._capture("dialogue_resolved")

    def _talk(self, input_text):
        self.dm_core._on_dialogue_detected({"input": input_text})
        return self.dialogue_events[-1]

    def test_no_shared_language_is_flagged_with_the_races_own_nonsense_phrase(self):
        self.dm_core.entities["innkeeper"]["languages"] = ["dwarvish"]

        result = self._talk("i talk to the innkeeper")

        self.assertTrue(result["found"])
        self.assertTrue(result["language_barrier"])
        self.assertEqual(result["target_language"], "dwarvish")
        self.assertEqual(result["nonsense_phrase"], "Grunthak dol bregnir uzdum")

    def test_a_shared_language_never_triggers_the_barrier(self):
        self.dm_core.entities["innkeeper"]["languages"] = ["dwarvish", "common"]

        result = self._talk("i talk to the innkeeper")

        self.assertNotIn("language_barrier", result)

    def test_default_common_on_both_sides_never_triggers_the_barrier(self):
        # Neither gladstone nor the innkeeper author "languages" explicitly here -- both fall
        # back to ["common"] (entity_schema.toml's own default), so ordinary tavern dialogue is
        # unaffected by this feature entirely.
        result = self._talk("i talk to the innkeeper")
        self.assertNotIn("language_barrier", result)

    def test_language_barrier_skips_the_sentiment_nudge(self):
        self.dm_core.entities["innkeeper"]["languages"] = ["dwarvish"]
        before = self.dm_core.get_attitude("innkeeper", self.dm_core.player_name)[0]

        self.dm_core._on_dialogue_detected({
            "input": "i talk to the innkeeper", "sentiment": "positive", "sentiment_score": 1.0,
        })

        self.assertEqual(self.dm_core.get_attitude("innkeeper", self.dm_core.player_name)[0], before)

    def test_a_bilingual_player_is_not_understood_by_every_known_language_at_once(self):
        # The gap this feature closes: a bilingual player defaults to speaking only the first
        # of their own known languages (current_language, DM_Dialogue.py's _current_language),
        # not "every language they know at once" -- so an elvish-only innkeeper no longer
        # silently understands a player who never actually switched to Elvish.
        self.dm_core.entities[self.dm_core.player_name]["languages"] = ["common", "elvish"]
        self.dm_core.entities["innkeeper"]["languages"] = ["elvish"]

        result = self._talk("i talk to the innkeeper")

        self.assertTrue(result["language_barrier"])

    def test_switching_current_language_closes_the_barrier(self):
        self.dm_core.entities[self.dm_core.player_name]["languages"] = ["common", "elvish"]
        self.dm_core.entities["innkeeper"]["languages"] = ["elvish"]
        self.dm_core.entities[self.dm_core.player_name]["current_language"] = "elvish"

        result = self._talk("i talk to the innkeeper")

        self.assertNotIn("language_barrier", result)


class TestCurrentLanguage(DMTestCase):
    """!
    @brief DM_Dialogue.py's _current_language/_resolve_language_intent -- the player's own
        persistent, single-language choice (see TestLanguageBarrier for the barrier check
        this feeds). scenario "tavern", same fixture TestLanguageBarrier uses.
    """
    scenario_name = "tavern"

    def setUp(self):
        super().setUp()
        self.item_events = self._capture("item_interaction_resolved")

    def _speak(self, input_text):
        intent = detect_item_intent(input_text)
        self.dm_core._on_item_interaction_detected({"intent": intent, "item_name": None, "input": input_text})
        return self.item_events[-1]

    def test_defaults_to_the_first_known_language_when_never_switched(self):
        self.dm_core.entities[self.dm_core.player_name]["languages"] = ["common", "elvish"]
        self.assertEqual(self.dm_core._current_language(), "common")

    def test_speaking_a_known_language_switches_the_active_one(self):
        self.dm_core.entities[self.dm_core.player_name]["languages"] = ["common", "elvish"]

        result = self._speak("i speak in elvish")

        self.assertTrue(result["found"])
        self.assertEqual(result["language"], "elvish")
        self.assertEqual(self.dm_core._current_language(), "elvish")

    def test_naming_a_language_the_player_doesnt_know_is_declined(self):
        result = self._speak("i speak in dwarvish")

        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "unknown_language")
        self.assertEqual(self.dm_core._current_language(), "common")

    def test_current_language_round_trips_through_save_and_load(self):
        slot_name = "test_current_language_slot"
        self.addCleanup(shutil.rmtree, self.dm_core._save_slot_dir(slot_name), ignore_errors=True)
        self.dm_core.entities[self.dm_core.player_name]["languages"] = ["common", "elvish"]
        self._speak("i speak in elvish")

        self.dm_core.save_game(slot_name)
        self.dm_core.load_game(slot_name)

        self.assertEqual(self.dm_core._current_language(), "elvish")


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
        # gladstone's undead override: disposition/familiarity = -100 (hostile), threat = 100 --
        # a genuine mix of extremes in one attitude array.
        self.dm_core.entities["zombie"] = {"name": "zombie", "supertype": "undead"}

        description = self.dm_core.describe_attitude("gladstone", "zombie")

        self.assertIn("Attitude toward zombie:", description)
        self.assertIn("wants them gone, one way or another", description)  # disposition: hostile
        self.assertIn("feels bold and confident around them", description)  # threat: friendly (100 boundary)
        self.assertIn("is repulsed by them", description)  # familiarity: hostile


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
            "supertype": "creature", "attitudes": {"default": [-40, 0, 0]},
        }
        self.dm_core.entities["hostile_npc"] = {
            "supertype": "creature", "attitudes": {"default": [-100, 0, 0]},
        }
        self.assertFalse(self.dm_core.is_hostile("wary_npc", self.dm_core.player_name))
        self.assertTrue(self.dm_core.is_hostile("hostile_npc", self.dm_core.player_name))

    def test_object_supertype_is_never_hostile_regardless_of_attitude_data(self):
        self.dm_core.entities["angry_chest"] = {
            "supertype": "object", "attitudes": {"default": [-100, 0, 0]},
        }
        self.assertFalse(self.dm_core.is_hostile("angry_chest", self.dm_core.player_name))


class TestEquipSlots(DMTestCase):
    def test_get_equip_slots_prefers_subtype_match_over_supertype_only_entry(self):
        self.dm_core.rules["equip_slot"] = [
            {"supertype": "creature", "slots": ["default_slot"]},
            {"supertype": "creature", "subtype": "humanoid", "slots": ["rhand", "chest"]},
        ]
        self.assertEqual(self.dm_core.get_equip_slots("gladstone"), ["rhand", "chest"])


class TestValidation(DMTestCase):
    """DM_Validation.py's referential-integrity checks. Each synthetic-data test injects a
    minimal bad entity/entity_template/location directly into self.dm_core's own dicts (rather
    than authoring a throwaway TOML file) and re-runs validate_loaded_data() -- since the real
    arena fixture is already proven clean below, any error captured after that must come from
    the injected data."""

    def test_real_shipped_data_boots_with_zero_validation_errors(self):
        # Every real scenario this repo ships, in both settings -- a regression guard that a
        # future data edit doesn't quietly introduce a dangling reference, and proof the
        # validator is genuinely setting-agnostic (no Fantasy-specific assumption anywhere in
        # DM_Validation.py).
        for setting in ("Fantasy", "Zombie"):
            for scenario_key, _name, _description in list_available_scenarios(setting):
                errors = []
                bus = EventBus()
                bus.subscribe("log_error", errors.append)
                DMCore(bus, scenario_name=scenario_key, setting=setting)
                self.assertEqual(errors, [], f"{setting}/{scenario_key} produced validation errors: {errors}")

    def test_skill_reference_checks(self):
        self.dm_core.entities["bad_skills_widget"] = {
            "name": "bad_skills_widget", "supertype": "object", "skill": "nonexistent_skill",
            "abilities": [{"name": "zap", "skill": ["blades", "nonexistent_ability_skill"]}],
            "test": {"skill": ["nonexistent_test_skill"]},
            "craft": {"skill": ["nonexistent_craft_skill"]},
            "notice": {"skill": "nonexistent_notice_skill"},
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()

        for expected in (
            "nonexistent_skill", "nonexistent_ability_skill", "nonexistent_test_skill",
            "nonexistent_craft_skill", "nonexistent_notice_skill",
        ):
            self.assertTrue(any(expected in e for e in errors), f"missing error for {expected}")
        # "blades" is a real skill -- not flagged alongside its bogus list-mate.
        self.assertFalse(any("'blades'" in e for e in errors))

    def test_behavior_action_reference(self):
        self.dm_core.entities["bad_behavior_widget"] = {
            "name": "bad_behavior_widget", "supertype": "creature",
            "abilities": [{"name": "real move", "skill": "brawling"}],
            "behavior": [
                {"requirements": [], "action": "nonexistent_move"},
                {"requirements": [], "action": "real move"},
                {"requirements": [], "action": "advance"},
                {"requirements": [], "action": "retreat"},
            ],
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()

        self.assertTrue(any("nonexistent_move" in e for e in errors))
        # A real owned ability, and the two reserved movement words, are never flagged.
        self.assertFalse(any("real move" in e for e in errors))
        self.assertFalse(any("advance" in e for e in errors))
        self.assertFalse(any("retreat" in e for e in errors))

    def test_entity_name_reference_checks(self):
        self.dm_core.entities["bad_refs_widget"] = {
            "name": "bad_refs_widget", "supertype": "object",
            "inventory": ["nonexistent_item"],
            "equipped": {"rhand": "nonexistent_weapon"},
            "replace_with": "nonexistent_husk",
            "damage_value": {"dice": 1, "pips": 0, "bonus": "user.nonexistent_rule"},
            "abilities": [{
                "name": "bad ability", "skill": "arcane",
                "summon": {"name": "nonexistent_summon"},
                "materials": [{"item": "nonexistent_material", "quantity": 1}],
            }],
            "craft": {"skill": ["strength"], "materials": [{"item": "nonexistent_craft_material", "quantity": 1}]},
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()

        for expected in (
            "nonexistent_item", "nonexistent_weapon", "nonexistent_husk", "nonexistent_rule",
            "nonexistent_summon", "nonexistent_material", "nonexistent_craft_material",
        ):
            self.assertTrue(any(expected in e for e in errors), f"missing error for {expected}")

    def test_summon_template_reference(self):
        self.dm_core.entities["bad_summon_template_widget"] = {
            "name": "bad_summon_template_widget", "supertype": "object",
            "abilities": [{"name": "conjure", "skill": "arcane", "summon": {"template": "nonexistent_template"}}],
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()
        self.assertTrue(any("nonexistent_template" in e for e in errors))

    def test_entity_template_forbidden_fields(self):
        self.dm_core.entity_templates["bad_shape_template"] = {
            "name": "bad_shape_template", "supertype": "creature",
            "skills": {"blades": {"dice": 2, "pips": 0}}, "max_hp": 10,
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()

        self.assertTrue(any("'skills'" in e for e in errors))
        self.assertTrue(any("'max_hp'" in e for e in errors))
        # "name" is required on a template (self.entity_templates is indexed by it) -- never
        # itself flagged as a forbidden field.
        self.assertFalse(any("'name'" in e for e in errors))

    def test_location_reference_checks(self):
        self.dm_core.locations["bad_location"] = {
            "key": "bad_location", "start_room": "nonexistent_start_room", "return_to": "nonexistent_location",
            "entities": [{"name": "nonexistent_persistent_entity"}],
            "exit": [{"destination": "nonexistent_destination"}, {"destination": "arena_grounds", "arrival_room": "nonexistent_arrival_room"}],
            "rooms": {
                "real_room": {
                    "key": "real_room", "bands": 2,
                    "entities": [{"template": "nonexistent_room_template"}],
                    "exit": [{"destination": "nonexistent_sibling_room"}],
                },
            },
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()

        for expected in (
            "nonexistent_start_room", "nonexistent_location", "nonexistent_persistent_entity",
            "nonexistent_destination", "nonexistent_arrival_room", "nonexistent_room_template",
            "nonexistent_sibling_room",
        ):
            self.assertTrue(any(expected in e for e in errors), f"missing error for {expected}")

    def test_player_placeholder_in_entities_list_is_not_flagged(self):
        self.dm_core.locations["placeholder_location"] = {
            "key": "placeholder_location", "entities": [{"name": "player"}],
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()
        self.assertEqual(errors, [])


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
            {
                "hp", "active_conditions", "currency", "inventory", "equipped", "band",
                "attitude_deltas", "action_attitude_deltas", "current_language", "prompt_directive",
            },
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
        saves_root = os.path.join(PROJECT_ROOT, "Saves")
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

    def test_collect_ad_hoc_entities_includes_scenario_entities_and_strips_damage_tags(self):
        # A live scenario_entities-only ad hoc entity (no ground/inventory reachability at all)
        # -- exactly DM_Summoning.py's own summoned allies and DM_Improvisation.py's own
        # conjured creatures/containers/traps. "recent_damage_tags" (a plain set, not
        # JSON-serializable) is stripped from the copied dict regardless of whether it's
        # present, since save_game would otherwise crash trying to json.dump it.
        name = self.dm_core._summon_creature({"name": "spectral wolf", "duration": 3})
        self.dm_core.entities[name]["recent_damage_tags"] = {"cold"}

        collected = self.dm_core._collect_ad_hoc_entities()

        self.assertIn(name, collected)
        self.assertNotIn("recent_damage_tags", collected[name])
        self.assertEqual(collected[name]["summon_expires_in"], 3)

    def test_ad_hoc_scene_participant_round_trips_through_save_load(self):
        # The actual save/load round trip for the same shape the test above checks in
        # isolation -- a summoned ally is a live scenario_entities participant with no ground/
        # inventory reachability, so without _collect_ad_hoc_entities' own scenario_entities
        # scan (and load_game re-appending it), it would silently vanish on reload even though
        # every *other* ad hoc entity (ex: the ground-item "stone" above) already round-trips.
        slot = self._track("test_ad_hoc_scene_participant_round_trip")
        name = self.dm_core._summon_creature({"name": "spectral wolf", "duration": 3})
        self.dm_core.apply_damage(name, 5)  # 16 -> 11
        self.dm_core.save_game(slot)

        fresh_dm = DMCore(EventBus(), scenario_name="arena")
        self.assertNotIn(name, fresh_dm.scenario_entities)
        fresh_dm.load_game(slot)

        self.assertIn(name, fresh_dm.scenario_entities)
        self.assertEqual(fresh_dm.get_current_hp(name), 11)
        self.assertEqual(fresh_dm.entities[name]["summon_expires_in"], 3)
        self.assertFalse(fresh_dm.is_hostile(name, fresh_dm.player_name))

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
        self.assertFalse(result.success)
        # Trap's fail damage is 3d (patched to 1 each = 3 raw), reduced by chain mail's own
        # 2d "piercing" armor coverage (also patched to 1 each = 2) -- net 1, not 0, which is
        # exactly why the trap deals 3 dice and not 2 (see the items.toml comment).
        damage_effects = [effect for effect in result.effects if isinstance(effect, DamageEffect)]
        self.assertEqual(len(damage_effects), 1)
        self.assertEqual(damage_effects[0].net_damage, 1)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), starting_hp - 1)
        self.assertIn("triggered", self.dm_core.entities["dart trap"]["active_conditions"])
        self.assertIn("armed", self.dm_core.entities["dart trap"]["active_conditions"])  # fail never dismisses it

        # blocks_if_condition="triggered" -- a repeat attempt must fall through to the normal
        # opposed path (difficulty 0, no HP loss) instead of rolling and re-damaging again.
        hp_after_first_hit = self.dm_core.get_current_hp("gladstone")
        with patch("random.randint", return_value=6):
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "finesse"}], "input": "I try again"})
        self.assertEqual(self.action_events[-1]["actions"][0].difficulty, 0)
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


class TestDuplicateEntityNamesAcrossRooms(DMTestCase):
    """!
    @brief The DM_Rules.py "Known gaps" entry this fixes: self.entity_occurrence_counts is now
        scoped to the whole DMCore's lifetime, not to one _instance_entities call, so two
        different rooms in the same multi-room dungeon (crypt.toml) that happen to declare the
        same creature name disambiguate against each other instead of the second one's own
        _place_new_entity silently overwriting the first's live HP/conditions under the same
        self.entities key. crypt.toml itself never actually declares such a collision (see its
        own comment on "kept in sync by hand"), so this injects a second room reusing
        "giant spider" (already real in hall_of_webs) directly onto self.dm_core.rooms, the
        same "inject a room, then enter_room into it" technique TestMultiRoomDungeon's own
        move/revisit tests already use.
    """
    scenario_name = "crypt"

    def _inject_colliding_room(self):
        self.dm_core.rooms["ambush_nook"] = {
            "key": "ambush_nook", "name": "Ambush Nook", "bands": 1, "enclosed": True,
            "entities": [{"name": "giant spider", "band": 1}],
        }

    def test_second_rooms_duplicate_name_disambiguates_instead_of_colliding(self):
        self.dm_core.enter_room("hall_of_webs")
        self.assertIn("giant spider", self.dm_core.entities)
        self.dm_core.apply_damage("giant spider", 9)  # 14 max_hp -> 5, so an overwrite is detectable
        self.assertEqual(self.dm_core.get_current_hp("giant spider"), 5)

        self._inject_colliding_room()
        self.dm_core.enter_room("ambush_nook")

        self.assertEqual(self.dm_core.scenario_entities, ["gladstone", "thane", "anne", "giant spider_2"])
        self.assertIn("giant spider_2", self.dm_core.entities)
        # The second instance is a fresh, full-HP copy of the template -- not the first's own
        # wounded dict reused/aliased.
        self.assertEqual(self.dm_core.get_current_hp("giant spider_2"), 14)
        # The critical assertion: the first spider's live, wounded state must survive
        # untouched -- before this fix, the second room's own instancing overwrote
        # self.entities["giant spider"] outright.
        self.assertEqual(self.dm_core.get_current_hp("giant spider"), 5)

    def test_save_then_load_restores_both_disambiguated_instances_correctly(self):
        self.dm_core.enter_room("hall_of_webs")
        self.dm_core.apply_damage("giant spider", 9)
        self._inject_colliding_room()
        self.dm_core.enter_room("ambush_nook")
        self.dm_core.apply_damage("giant spider_2", 3)

        slot_name = "test_crypt_duplicate_name_slot"
        slot_dir = self.dm_core._save_slot_dir(slot_name)
        self.addCleanup(shutil.rmtree, slot_dir, ignore_errors=True)
        self.dm_core.save_game(slot_name)

        # load_game's own load_scenario_definition re-reads crypt.toml fresh from disk, which
        # would otherwise wipe the injected "ambush_nook" room -- re-inject it the moment
        # self.locations exists again, exactly where load_game itself populates it, before the
        # location_runtime replay loop (which needs it present) runs.
        fresh_dm = DMCore(EventBus(), scenario_name="crypt")
        real_load_scenario_definition = fresh_dm.load_scenario_definition

        def load_scenario_definition_with_ambush_nook(scenario_name):
            real_load_scenario_definition(scenario_name)
            fresh_dm.locations["crypt"]["rooms"]["ambush_nook"] = {
                "key": "ambush_nook", "name": "Ambush Nook", "bands": 1, "enclosed": True,
                "entities": [{"name": "giant spider", "band": 1}],
            }

        fresh_dm.load_scenario_definition = load_scenario_definition_with_ambush_nook
        fresh_dm.load_game(slot_name)

        self.assertEqual(fresh_dm.get_current_hp("giant spider"), 5)
        self.assertEqual(fresh_dm.get_current_hp("giant spider_2"), 11)


class TestInterleavedLocationSaveLoad(DMTestCase):
    """!
    @brief The residual gap _instance_entities' own docstring used to carry even after
        TestDuplicateEntityNamesAcrossRooms' fix: a same-location, cross-*room* collision was
        already correctly disambiguated live, but DM_Persistence.py's load_game re-derived every
        visited scope from scratch via a *nested* "each location, then all of that location's
        own rooms" loop -- which doesn't reproduce the true chronological order when the player
        interleaves visits across two *different* locations (leaves one location mid-dungeon,
        visits a second, then returns to the first for a room they hadn't seen yet). self.entity_
        instancing_order (DM_Rules.py) now records that exact live order and load_game replays it
        verbatim instead. crypt.toml is one location -- this test injects a second, "b_wing", to
        actually exercise cross-location interleaving, the same "inject after construction, then
        drive it with low-level DMCore calls directly" technique TestDuplicateEntityNamesAcrossRooms
        already uses for a single extra room.
    """
    scenario_name = "crypt"

    def _inject_b_wing(self, dm_core):
        dm_core.locations["b_wing"] = {
            "key": "b_wing", "name": "B Wing", "start_room": "b_room", "entities": [],
            "rooms": {
                "b_room": {
                    "key": "b_room", "name": "B Room", "bands": 1, "enclosed": True,
                    "entities": [{"name": "giant spider", "band": 1}],
                },
            },
        }

    def test_save_then_load_preserves_interleaved_cross_location_disambiguation(self):
        # True chronological order: b_wing's own spider is instanced *before* crypt's
        # guard_chamber (visited only after returning from b_wing), even though crypt itself
        # was entered first (at __init__) and would sort first under a naive "group every
        # location's own rooms together" replay.
        self._inject_b_wing(self.dm_core)
        # A second, colliding reference alongside guard_chamber's own real "iron chest" --
        # crypt.toml itself declares no such collision (kept collision-free by hand, per its
        # own comments), so this is injected the same way TestDuplicateEntityNamesAcrossRooms
        # injects its own "ambush_nook" room.
        self.dm_core.locations["crypt"]["rooms"]["guard_chamber"]["entities"].append(
            {"name": "giant spider", "band": 3},
        )

        self.dm_core._enter_location("b_wing")  # first ever "giant spider" -> "giant spider"
        self.dm_core._enter_location("crypt", arrival_room="guard_chamber")  # second -> "_2"

        self.assertIn("giant spider", self.dm_core.entities)
        self.assertIn("giant spider_2", self.dm_core.entities)
        self.dm_core.apply_damage("giant spider", 9)  # b_wing's own spider: 14 -> 5
        self.dm_core.apply_damage("giant spider_2", 3)  # crypt's guard_chamber spider: 14 -> 11

        slot_name = "test_crypt_interleaved_location_slot"
        self.addCleanup(shutil.rmtree, self.dm_core._save_slot_dir(slot_name), ignore_errors=True)
        self.dm_core.save_game(slot_name)

        fresh_dm = DMCore(EventBus(), scenario_name="crypt")
        real_load_scenario_definition = fresh_dm.load_scenario_definition

        def load_scenario_definition_with_b_wing(scenario_name):
            real_load_scenario_definition(scenario_name)
            self._inject_b_wing(fresh_dm)
            fresh_dm.locations["crypt"]["rooms"]["guard_chamber"]["entities"].append(
                {"name": "giant spider", "band": 3},
            )

        fresh_dm.load_scenario_definition = load_scenario_definition_with_b_wing
        fresh_dm.load_game(slot_name)

        # Had load_game fallen back to grouping crypt's own rooms together (the pre-fix
        # replay order), guard_chamber's spider would have claimed the bare "giant spider"
        # name instead -- these two assertions are the real regression guard.
        self.assertEqual(fresh_dm.get_current_hp("giant spider"), 5)
        self.assertEqual(fresh_dm.get_current_hp("giant spider_2"), 11)

    def test_load_falls_back_gracefully_for_a_save_missing_entity_instancing_order(self):
        # Backward compatibility: a save written before self.entity_instancing_order existed
        # simply has no such key -- load_game must still restore the game (via
        # _replay_nested_instancing), not crash.
        self.dm_core.enter_room("hall_of_webs")
        self.dm_core.apply_damage("giant spider", 6)  # 14 -> 8

        slot_name = "test_crypt_no_instancing_order_slot"
        slot_dir = self.dm_core._save_slot_dir(slot_name)
        self.addCleanup(shutil.rmtree, slot_dir, ignore_errors=True)
        self.dm_core.save_game(slot_name)

        save_path = os.path.join(slot_dir, "dm_state.json")
        with open(save_path) as f:
            data = json.load(f)
        del data["entity_instancing_order"]
        with open(save_path, "w") as f:
            json.dump(data, f)

        fresh_dm = DMCore(EventBus(), scenario_name="crypt")
        fresh_dm.load_game(slot_name)

        self.assertEqual(fresh_dm.current_room_key, "hall_of_webs")
        self.assertEqual(fresh_dm.get_current_hp("giant spider"), 8)


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
        urllib.request.urlopen mocked in place of a real Ollama connection."""

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

    def test_display_system_status_appends_to_the_history_pane(self):
        # Used by LLDM.py's main() to relay Ollama_Launcher.py's own bootstrap status into the
        # GUI (see CLAUDE.md's "LLM integration") -- same "[System] ..." prefix convention
        # display_game_saved/display_game_loaded/display_game_load_failed already use.
        self.gui.display_system_status("Ollama already running.")
        content = self.gui.history_text.get("1.0", tk.END)
        self.assertIn("[System] Ollama already running.", content)

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

    @patch("gui.GUI_Core.run_character_creation_dialog")
    @patch("gui.GUI_Core.load_character_creation_data", return_value=({}, [], {}))
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
            "setting": "Fantasy",
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
        with patch("gui.GUI_Core.load_character_creation_data", return_value=({}, [], {})), \
             patch("gui.GUI_Core.run_character_creation_dialog", return_value={"race": "human", "allocation": {}, "name": "X"}):
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

    @patch("gui.GUI_Core.run_character_creation_dialog")
    @patch("gui.GUI_Core.load_character_creation_data", return_value=({}, [], {}))
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
