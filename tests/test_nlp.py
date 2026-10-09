import json
import os
import tomllib
import unittest
from types import SimpleNamespace
from dm.DM_Core import DMCore
from dm.DM_Rules import RulesMixin, list_available_settings
from tests.event_contract import ValidatingEventBus
from nlp.Intent_Classification import (
    _clause_names_item,
    ADDRESS_ARTICLES,
    ADDRESS_NON_ADDRESSEES,
    ADDRESS_TERMINATORS,
    ADVANCE_KEYWORDS,
    extract_address_phrase,
    extract_item_phrase,
    CLOSE_KEYWORDS,
    CRAFT_KEYWORDS,
    DIALOGUE_KEYWORDS,
    DISMOUNT_KEYWORDS,
    DROP_KEYWORDS,
    EQUIP_KEYWORDS,
    EXAMINE_KEYWORDS,
    FORMATION_ABREAST_KEYWORDS,
    FORMATION_BEHIND_KEYWORDS,
    GIVE_KEYWORDS,
    HITCH_KEYWORDS,
    INTENT_PROTOTYPES,
    LORE_KEYWORDS,
    MOUNT_KEYWORDS,
    OPEN_KEYWORDS,
    REST_KEYWORDS,
    RETREAT_KEYWORDS,
    SCENE_QUERY_KEYWORDS,
    SPEAK_LANGUAGE_KEYWORDS,
    TAKE_KEYWORDS,
    TRADE_KEYWORDS,
    TRAVEL_KEYWORDS,
    UNEQUIP_KEYWORDS,
    UNHITCH_KEYWORDS,
    USE_KEYWORDS,
    Adjudication,
    IntentClassifier,
    _phrase_matches,
    detect_dialogue_intent,
    detect_help_intent,
    detect_implicit_speech,
    detect_item_intent,
    detect_out_of_character,
    detect_save_load_intent,
    detect_scene_query_intent,
    frame_speech,
    is_hypothetical,
    normalize_declared_verb,
    process_input,
    split_action_clauses,
)
from nlp.NLP_Core import NLPCore, SentenceTransformerMatcher, _base_verb
from tests.support import (
    FakeMatcher,
    script_llm,
)
# Module-level hooks start the shared patches (tests/support.py) for every test in this file.
from tests.support import setUpModule, tearDownModule  # noqa: F401


class TestGameBoot(unittest.TestCase):
    def test_boot_and_skill_identification(self):
        # 1. Initialize Event Bus
        event_bus = ValidatingEventBus()

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
        nlp_core = NLPCore(ValidatingEventBus())

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

    def test_threat_classification_reads_something_genuinely_different_from_disposition(self):
        # The actual point of this axis: a line can be admiring in *tone* (positive sentiment)
        # while still reading as physically threatening -- the deliberately valence-crossed
        # case NLP_Core.py's own module comment names as proof threat isn't just a relabeled
        # copy of disposition (see docs/social-dialogue.md's "Dialogue sentiment").
        nlp_core = NLPCore(ValidatingEventBus())

        admiring_but_threatening_label, _score = nlp_core.matcher.classify_threat(
            "your skill with that blade is terrifying, truly the deadliest fighter I've ever seen",
        )
        reassuring_label, reassuring_score = nlp_core.matcher.classify_threat(
            "you're safe here with me, nothing is going to hurt you, I promise",
        )
        informational_label, _score = nlp_core.matcher.classify_threat("how far is it to the next town")

        self.assertEqual(admiring_but_threatening_label, "negative")  # "physically threatened"
        self.assertEqual(reassuring_label, "positive")  # "physically safe"
        self.assertGreaterEqual(reassuring_score, nlp_core.matcher.sentiment_confidence_threshold)
        self.assertIsNone(informational_label)

    def test_familiarity_classification_reads_something_genuinely_different_from_disposition(self):
        # Same "genuinely separate axis" proof as threat above, for emotional closeness --
        # NLP_Core.py's own module comment names familiarity as the other axis validated this way.
        nlp_core = NLPCore(ValidatingEventBus())

        close_label, close_score = nlp_core.matcher.classify_familiarity(
            "I've known you my whole life -- you're like family to me",
        )
        distant_label, _score = nlp_core.matcher.classify_familiarity(
            "I don't know you, and frankly I don't care to",
        )
        informational_label, _score = nlp_core.matcher.classify_familiarity("how far is it to the next town")

        self.assertEqual(close_label, "positive")  # "emotionally close to the speaker"
        self.assertEqual(distant_label, "negative")  # "emotionally distant from the speaker"
        self.assertGreaterEqual(close_score, nlp_core.matcher.sentiment_confidence_threshold)
        self.assertIsNone(informational_label)


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
        cls.event_bus = ValidatingEventBus()
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

    def test_intent_router_separates_held_out_paraphrases_from_ordinary_actions(self):
        # The calibration artifact for INTENT_PROTOTYPES. FakeMatcher can prove the classifier
        # *routes* on a score, but only the real model can prove the scores actually separate
        # -- and this feature is worthless if they don't. Every phrase here is deliberately
        # held out of INTENT_PROTOTYPES itself (asserted below), so this measures
        # generalization rather than memorization.
        #
        # The negatives matter more than the positives: a false negative is just today's
        # behavior, while a false positive is a confidently wrong action where an honest "I
        # don't understand" was correct. Note several negatives are rejected because
        # OTHER_INTENT wins the argmax outright, not because they fall under a threshold --
        # that bucket, not the cutoff, is what does the real work here.
        positives = [
            ("check out the room", "scene_query"),
            ("who else is around", "scene_query"),
            ("whats in this place", "scene_query"),
            ("take a look around", "scene_query"),
            ("lets head over to the docks", "travel"),
            ("walk into the temple", "travel"),
            ("camp here until sunrise", "rest"),
            ("what do i know about goblins", "lore_check"),
            ("get behind me anne", "formation_behind"),
            ("walk next to me", "formation_abreast"),
        ]
        negatives = [
            "look at the chest", "examine the dagger", "search the room for hidden traps",
            "inspect the lock", "take the rope", "attack the wolf", "hey there innkeeper",
            "buy a rope", "who are you", "sharpen my blade",
        ]
        authored = {phrase for phrases in INTENT_PROTOTYPES.values() for phrase in phrases}

        for phrase, expected in positives:
            self.assertNotIn(phrase, authored, f"{phrase!r} must stay held out to mean anything")
            matched, score = self.nlp_core.matcher.map_to_intent(phrase)
            self.assertEqual(matched, expected, f"{phrase!r} scored {score:.3f}")

        for phrase in negatives:
            matched, score = self.nlp_core.matcher.map_to_intent(phrase)
            self.assertIsNone(matched, f"{phrase!r} wrongly routed to {matched} at {score:.3f}")

    def test_destination_matching_resolves_a_generic_noun_the_literal_scan_cannot(self):
        # The motivating case: DM_Movement.py's own whole-word name/alias scan can never get
        # from "the tavern" to a destination authored as "The White Deer Tavern and Inn", and
        # authoring "tavern" as an alias on every tavern in town is the phrase-by-phrase
        # treadmill this replaces. The bank is installed/restored explicitly because the
        # matcher is class-shared (same precedent as the register_item test below).
        matcher = self.nlp_core.matcher
        saved = (matcher.destination_embeddings, matcher.destination_indices)
        try:
            matcher.set_destinations([
                {"key": "white_deer", "name": "The White Deer Tavern and Inn", "aliases": []},
                {"key": "sandpoint_garrison", "name": "The Sandpoint Garrison", "aliases": []},
                {"key": "goblin_squash", "name": "Goblin Squash Stables", "aliases": []},
            ])
            self.assertEqual(matcher.map_to_destination("head into the tavern")[0], "white_deer")
            # An empty bank is a legitimate state (a location authoring no exits), and must
            # clear the previous one rather than leaving it matchable.
            matcher.set_destinations([])
            self.assertEqual(matcher.map_to_destination("head into the tavern"), (None, 0.0))
        finally:
            matcher.destination_embeddings, matcher.destination_indices = saved

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

    def test_item_catalog_updated_registers_a_targetable_entity_for_map_to_target(self):
        # Found by playtest: a narrated bystander could be spoken to but "kick old man hemlock"
        # never named him as a target -- the target bank was only ever built at boot.
        matcher = self.nlp_core.matcher
        saved = (matcher.item_embeddings, list(matcher.item_indices),
                 matcher.target_embeddings, list(matcher.target_indices))
        try:
            self.event_bus.publish("item_catalog_updated", {"entities": [
                {"name": "Old Man Hemlock", "description": "A stooped old fishmonger.", "targetable": True},
                {"name": "rubber chicken talisman", "description": "A glowing rubber chicken talisman."},
            ]})

            target, _score = matcher.map_to_target("kick old man hemlock")
            self.assertEqual(target, "Old Man Hemlock")
            self.assertNotIn("rubber chicken talisman", matcher.target_indices)
        finally:
            (matcher.item_embeddings, matcher.item_indices,
             matcher.target_embeddings, matcher.target_indices) = saved

    def test_keyword_fallback_ignores_a_keyword_inside_a_remark_rather_than_an_action(self):
        # Both observation's own keyword "find" -- the first leads a declared action, the
        # second is just a word in a suggestion (see NLP_Core.py's NON_ACTION_OPENERS). Scores
        # alone can't tell these apart: across the real logs, the keyword fallback's
        # conversation hits and genuine hits scored in the same 0.2-0.5 band.
        skill, _score = self.nlp_core.matcher.map_to_action("find the dockmaster")
        self.assertEqual(skill, "observation")

        skill, score = self.nlp_core.matcher.map_to_action("forget the lumber. let's find a private place.")
        self.assertIsNone(skill, f"remark wrongly rolled at {score:.3f}")

    def test_alternate_phrasing_never_scores_a_fragment_that_opens_like_a_question(self):
        # The original misfire: truncating at TOPIC_CLAUSE_MARKERS' " that " turned this into
        # "is your forge really", which scored as forgery once the banter around it was gone.
        skill, score = self.nlp_core.matcher.map_to_action(
            "gareth, is your forge really that hot? maybe we could cool off together.",
        )
        self.assertIsNone(skill, f"banter wrongly rolled {skill} at {score:.3f}")


class TestPlayerInputCorpus(unittest.TestCase):
    """!
    @brief A ratchet over tests/player_input_corpus.toml -- hand-labeled, setting-neutral
        inputs, run against EVERY setting under Rules/ (list_available_settings), so a setting
        added later is covered with no change here. Each setting is loaded rules-only -- the
        same skills/entities DMCore would publish in "rules_loaded", but no scenario, so no
        one scenario's items, exits, or cast can move these numbers.

        Individual phrasings are too noisy a target for the embedding matcher to pin one by
        one, so this asserts aggregate rates instead: how often conversation still rolls a
        skill, how often a genuine action is met with "I don't understand", and how often a
        social-skill attempt rolls. The same bounds apply to every setting, so each is set by
        whichever setting is currently worst -- lower one whenever the worst case improves; a
        change that has to raise one is a regression that has to justify itself. On failure,
        the message names the setting and lists every offending input.
    """

    # Measured once the fallback paths learned NON_ACTION_OPENERS, which took conversation
    # rolls from 26/52 to 14/52 (Fantasy), 26 to 13 (Pathfinder), and 11 to 9 (Zombie) without
    # costing a single genuine action anywhere. Current worst cases: conversation 14/52 in
    # Fantasy; actions and social skills 22/49 and 2/12 in Zombie, whose deliberately bare
    # skills.toml has no keywords for most investigation verbs and no bargaining/deception
    # skill at all -- richer Zombie skill data is what would let those two bounds tighten.
    MAX_CONVERSATION_ROLL_RATE = 0.27
    MAX_ACTION_NOT_UNDERSTOOD_RATE = 0.45
    MIN_SOCIAL_SKILL_ROLL_RATE = 0.16

    @classmethod
    def setUpClass(cls):
        cls.event_bus = ValidatingEventBus()
        cls.nlp_core = NLPCore(cls.event_bus)
        with open(os.path.join(os.path.dirname(__file__), "player_input_corpus.toml"), "rb") as corpus_file:
            cls.corpus = tomllib.load(corpus_file)["input"]

    def _load_setting_rules(self, setting):
        # RulesMixin.load_rules itself, run against a bare stand-in for DMCore -- the production
        # TOML scan, without booting a scenario that would drag its own scene into the matcher.
        rules = SimpleNamespace(event_bus=self.event_bus, skills={}, entities={}, entity_templates={}, rules={})
        RulesMixin.load_rules(rules, os.path.join("Rules", setting))
        self.nlp_core.classifier.on_rules_loaded({"skills": rules.skills, "entities": rules.entities})

    def test_corpus_outcomes_stay_within_their_measured_bounds_in_every_setting(self):
        settings = list_available_settings()
        self.assertTrue(settings, "no settings found under Rules/")
        totals = {label: sum(1 for entry in self.corpus if entry["label"] == label) for label in "ANS"}

        for setting in settings:
            with self.subTest(setting=setting):
                self._load_setting_rules(setting)
                conversation_rolls, actions_not_understood, social_skill_rolls = [], [], []
                for entry in self.corpus:
                    _processed, events, _adjudication = self.nlp_core.classifier.classify(entry["text"])
                    skills = [
                        clause.get("skill") for event in events if event["event"] == "turn_detected"
                        for clause in event["payload"]["clauses"] if clause.get("skill")
                    ]
                    names = [event["event"] for event in events]
                    if entry["label"] == "N" and skills:
                        conversation_rolls.append(f"{entry['text']!r} -> {skills}")
                    elif entry["label"] == "A" and "action_not_understood" in names:
                        actions_not_understood.append(repr(entry["text"]))
                    elif entry["label"] == "S" and skills:
                        social_skill_rolls.append(entry["text"])

                print(
                    f"\n[player input corpus] {setting}: conversation rolls "
                    f"{len(conversation_rolls)}/{totals['N']}, actions not understood "
                    f"{len(actions_not_understood)}/{totals['A']}, social-skill rolls "
                    f"{len(social_skill_rolls)}/{totals['S']}"
                )
                self.assertLessEqual(
                    len(conversation_rolls) / totals["N"], self.MAX_CONVERSATION_ROLL_RATE,
                    f"{setting}:\n" + "\n".join(conversation_rolls),
                )
                self.assertLessEqual(
                    len(actions_not_understood) / totals["A"], self.MAX_ACTION_NOT_UNDERSTOOD_RATE,
                    f"{setting}:\n" + "\n".join(actions_not_understood),
                )
                self.assertGreaterEqual(len(social_skill_rolls) / totals["S"], self.MIN_SOCIAL_SKILL_ROLL_RATE, setting)

    # The same corpus again, mid-conversation (IntentClassifier.set_conversation_partner), where
    # unmarked talk should reach the partner (detect_implicit_speech). Measured at introduction:
    # 39/52 conversation lines to dialogue in every setting (6 without a partner), actions
    # swallowed 2/49 ("wait for a better chance", and a question followed by an action), and
    # social-skill rolls unchanged from the partner-less pass.
    MIN_PARTNER_CONVERSATION_DIALOGUE_RATE = 0.75
    MAX_PARTNER_ACTION_DIALOGUE_RATE = 0.05

    def test_conversation_partner_routes_talk_without_swallowing_actions(self):
        totals = {label: sum(1 for entry in self.corpus if entry["label"] == label) for label in "ANS"}
        self.addCleanup(self.nlp_core.classifier.set_conversation_partner, None)

        for setting in list_available_settings():
            with self.subTest(setting=setting):
                self._load_setting_rules(setting)
                self.nlp_core.classifier.set_conversation_partner({"key": "listener", "name": "listener", "aliases": []})
                talk_missed, actions_swallowed, social_skill_rolls = [], [], []
                for entry in self.corpus:
                    _processed, events, _adjudication = self.nlp_core.classifier.classify(entry["text"])
                    names = [event["event"] for event in events]
                    rolled = any(
                        clause.get("skill") for event in events if event["event"] == "turn_detected"
                        for clause in event["payload"]["clauses"]
                    )
                    # A rules question ("do i need to roll for that?") is talk for ADaM, not the partner.
                    if entry["label"] == "N" and not {"dialogue_detected", "help_detected"} & set(names):
                        talk_missed.append(repr(entry["text"]))
                    # Swallowed means the action was lost to dialogue -- a line split into
                    # dialogue plus its own turn ("i shout 'get down!' and tackle the stranger")
                    # kept it.
                    elif entry["label"] == "A" and "dialogue_detected" in names and not (
                        {"turn_detected", "item_interaction_detected"} & set(names)
                    ):
                        actions_swallowed.append(repr(entry["text"]))
                    elif entry["label"] == "S" and rolled:
                        social_skill_rolls.append(entry["text"])

                print(
                    f"\n[player input corpus, mid-conversation] {setting}: conversation to dialogue "
                    f"{totals['N'] - len(talk_missed)}/{totals['N']}, actions to dialogue "
                    f"{len(actions_swallowed)}/{totals['A']}, social-skill rolls "
                    f"{len(social_skill_rolls)}/{totals['S']}"
                )
                self.assertGreaterEqual(
                    1 - len(talk_missed) / totals["N"], self.MIN_PARTNER_CONVERSATION_DIALOGUE_RATE,
                    f"{setting}:\n" + "\n".join(talk_missed),
                )
                self.assertLessEqual(
                    len(actions_swallowed) / totals["A"], self.MAX_PARTNER_ACTION_DIALOGUE_RATE,
                    f"{setting}:\n" + "\n".join(actions_swallowed),
                )
                self.assertGreaterEqual(len(social_skill_rolls) / totals["S"], self.MIN_SOCIAL_SKILL_ROLL_RATE, setting)


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

    def test_detect_item_intent_lore_check_vs_genuine_dialogue(self):
        self.assertEqual(detect_item_intent("what do you know about the troll"), "lore_check")
        # A genuine "ask ... about ..." must still fall through to dialogue, not lore_check.
        self.assertIsNone(detect_item_intent("ask the guard about the road"))

    def test_lore_check_never_joins_the_turn_pipeline(self):
        # A standalone lore-check input publishes only its own free-standing
        # item_interaction_detected -- never a turn_detected, so it can never cost a turn slot
        # or trigger a combat round the way an ordinary action-kind clause would (see
        # docs/extended-goals.md's "Knowledge checks revealing monster lore").
        classifier = IntentClassifier(FakeMatcher())
        _processed, events, _adjudication = classifier.classify("what do you know about the troll")
        self.assertEqual(events, [{"event": "item_interaction_detected", "payload": {
            "intent": "lore_check", "item_name": None,
            "input": "what do you know about the troll", "score": None,
        }}])

    def test_a_dialogue_verb_inside_a_subordinate_clause_is_not_dialogue(self):
        # Found by playtest: "...until they can't ask questions" sent a kick to dialogue.
        self.assertFalse(detect_dialogue_intent("kick my opponent until they can't ask questions."))
        self.assertFalse(detect_dialogue_intent("punch him before he can tell anyone"))
        self.assertTrue(detect_dialogue_intent("if you see her, tell her i'm here"))
        self.assertTrue(detect_dialogue_intent("wait until he arrives, then talk to him"))
        self.assertTrue(detect_dialogue_intent("tell him when you're ready"))

    def test_quoted_speech_counts_as_dialogue_without_a_dialogue_verb(self):
        self.assertTrue(detect_dialogue_intent('i approach the fishmonger. "is something going on?"'))
        self.assertFalse(detect_dialogue_intent("i approach the fishmonger"))
        self.assertFalse(detect_dialogue_intent("i don't know"))

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

    def test_detect_scene_query_intent_vs_item_and_skill_phrasing(self):
        self.assertTrue(detect_scene_query_intent("what do i see"))
        self.assertTrue(detect_scene_query_intent("who is here"))
        self.assertTrue(detect_scene_query_intent("describe the room"))
        # A genuine perception check must never be swallowed here first -- see
        # SCENE_QUERY_KEYWORDS' own module note on why it avoids bare "look"/"search"/etc.
        self.assertFalse(detect_scene_query_intent("search the room for hidden traps"))
        self.assertFalse(detect_scene_query_intent("look at the chest"))
        self.assertFalse(detect_scene_query_intent("attack the wolf"))

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
        _processed, events, _adjudication = classifier.classify("save as arena run 1")
        self.assertEqual(events, [{"event": "save_requested", "payload": {"slot": "arena run 1"}}])

    def test_multi_clause_input_publishes_multiple_actions(self):
        classifier = IntentClassifier(FakeMatcher(actions={
            "attack with my sword": ("blades", 0.9),
            "pick the lock": ("finesse", 0.8),
        }))
        _processed, events, _adjudication = classifier.classify("I attack with my sword and pick the lock")

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
        _processed, events, _adjudication = classifier.classify("I take the longsword and attack the wolf")

        self.assertEqual(len(events), 1)
        clauses = events[0]["payload"]["clauses"]
        self.assertEqual(len(clauses), 2)
        self.assertEqual(clauses[0], {"kind": "item", "intent": "take", "item_name": "longsword", "phrase": "longsword"})
        self.assertEqual(clauses[1]["kind"], "action")
        self.assertEqual(clauses[1]["skill"], "blades")

    def test_exempt_clause_mixed_with_an_item_clause_still_publishes_separately(self):
        # "retreat" stays free (West End Games' own movement exception) and never joins the
        # shared turn, even when another clause in the same input is a genuine item action.
        classifier = IntentClassifier(FakeMatcher(items={"take the longsword": ("longsword", 0.9)}))
        _processed, events, _adjudication = classifier.classify("take the longsword and retreat")

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event"], "item_interaction_detected")
        self.assertEqual(events[0]["payload"]["intent"], "retreat")
        self.assertEqual(events[1]["event"], "turn_detected")
        self.assertEqual(
            events[1]["payload"]["clauses"], [{"kind": "item", "intent": "take", "item_name": "longsword", "phrase": "longsword"}],
        )

    def test_craft_keyword_resolves_to_an_item_kind_clause(self):
        # Detected exactly like "give"/"take" (same map_to_item lookup, matching over every
        # known object-supertype entity regardless of scene presence -- see NLP_Core.py's own
        # item-catalog build) -- resolution (DM_Crafting.py) is what actually rolls dice for it.
        classifier = IntentClassifier(FakeMatcher(items={"craft an iron dagger": ("iron dagger", 0.9)}))
        _processed, events, _adjudication = classifier.classify("craft an iron dagger")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "turn_detected")
        self.assertEqual(
            events[0]["payload"]["clauses"], [{"kind": "item", "intent": "craft", "item_name": "iron dagger", "phrase": "iron dagger"}],
        )

    def test_item_verb_still_takes_priority_over_dialogue(self):
        # A genuine item verb naming an entity is never swallowed as dialogue, even though
        # "to thane" would otherwise read as conversational address.
        classifier = IntentClassifier(FakeMatcher(items={"give the longsword to thane": ("longsword", 0.9)}))
        _processed, events, _adjudication = classifier.classify("give the longsword to thane")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "turn_detected")
        self.assertEqual(
            events[0]["payload"]["clauses"], [{"kind": "item", "intent": "give", "item_name": "longsword", "phrase": "longsword"}],
        )

    def test_item_losing_verb_needs_the_item_actually_named(self):
        # Found by playtest: "give" gated on "might give a clue", map_to_item paired the
        # sentence's gist with "health potion" at 0.55, and the potion went to a bystander.
        idiom = "dip my finger into the dust, hoping to lift fragments that might give a clue"
        classifier = IntentClassifier(FakeMatcher(items={
            idiom: ("health potion", 0.55),
            "give her the potions": ("health potion", 0.6),
            "hand over the sword": ("longsword", 0.7),
        }))

        _processed, events, _adjudication = classifier.classify(idiom)
        self.assertNotIn("turn_detected", [event["event"] for event in events])

        for text, item, phrase in (
            ("give her the potions", "health potion", "potions"), ("hand over the sword", "longsword", "sword"),
        ):
            _processed, events, _adjudication = classifier.classify(text)
            self.assertEqual(
                events[0]["payload"]["clauses"], [{"kind": "item", "intent": "give", "item_name": item, "phrase": phrase}],
            )

    def test_a_pronoun_clause_takes_the_whole_inputs_target(self):
        # Found by playtest: the kick matched on "start kicking him", which names nobody.
        text = "shove the old man hemlock into the river and start kicking him"
        classifier = IntentClassifier(FakeMatcher(
            actions={"start kicking him": ("brawling", 0.7), "kick the door": ("brawling", 0.7)},
            targets={text: ("Old Man Hemlock", 0.6), "walk past the guard and kick the door": ("guard", 0.6)},
        ))

        _processed, events, _adjudication = classifier.classify(text)
        self.assertEqual(events[0]["payload"]["clauses"][-1].get("target"), "Old Man Hemlock")
        # No pronoun, no fallback -- the door's kick must not land on the guard.
        _processed, events, _adjudication = classifier.classify("walk past the guard and kick the door")
        self.assertIsNone(events[0]["payload"]["clauses"][-1].get("target"))

    def test_semantic_router_never_takes_travel_from_a_question(self):
        # Found by playtest: "is that argument about the docks...?" routed to travel and walked
        # the player to the shipyard mid-conversation.
        question = "is that argument about the docks or about something else entirely?"
        classifier = IntentClassifier(FakeMatcher(intents={
            question: ("travel", 0.59), "head into the tavern": ("travel", 0.9),
        }))

        _processed, events, _adjudication = classifier.classify(question)
        self.assertNotIn("travel", [event["payload"].get("intent") for event in events])
        _processed, events, _adjudication = classifier.classify("head into the tavern")
        self.assertEqual(events[0]["payload"]["intent"], "travel")

    def test_dialogue_wins_once_item_pass_finds_nothing(self):
        classifier = IntentClassifier(FakeMatcher())
        _processed, events, _adjudication = classifier.classify("talk to the wolf")
        self.assertEqual(
            events,
            [{
                "event": "dialogue_detected",
                "payload": {
                    "input": "talk to the wolf", "score": None, "implicit": False,
                    "speech_form": "greet", "utterance": None,
                    "sentiment": None, "sentiment_score": 0.0,
                    "threat_sentiment": None, "threat_score": 0.0,
                    "familiarity_sentiment": None, "familiarity_score": 0.0,
                    # The promotion gate's own evidence pair (see extract_address_phrase /
                    # map_to_present_entity) -- the phrase is extracted mechanically, the
                    # match comes back empty because FakeMatcher knows about no one here.
                    "address_phrase": "wolf", "address_match": None, "address_score": 0.0,
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
        _processed, events, _adjudication = classifier.classify("talk to the innkeeper, thank you")
        self.assertEqual(events[0]["payload"]["sentiment"], "positive")
        self.assertEqual(events[0]["payload"]["sentiment_score"], 0.7)

    def test_detect_implicit_speech_reads_talk_but_not_declared_actions(self):
        for text in (
            "do you ever get tired of all this hard work?",
            "let's find somewhere quieter",
            "forget the lumber. let's find a private place.",
            "come help me relax",
            "gareth, is your forge really that hot",
            "maybe a little break",
            "i'm starving.",
            "i bet the rooms here are lovely",
        ):
            self.assertTrue(detect_implicit_speech(text), text)
        for text in (
            "check the debris",
            "persuade him to lower the price",
            "threaten to report him",
            "hide behind the barrel",
            "draw my blade, then charge",
            # Found by playtest: a bare "i'm" opener sent these declared actions to dialogue.
            "i'm knocking this entire stall over. now.",
            "grab everything! i'm taking it all!",
        ):
            self.assertFalse(detect_implicit_speech(text), text)

    def test_unmarked_speech_is_dialogue_only_while_a_conversation_is_running(self):
        classifier = IntentClassifier(FakeMatcher())
        _processed, events, _adjudication = classifier.classify("Do you ever get tired of all this?")
        self.assertEqual(events[0]["event"], "action_not_understood")
        # Nothing claimed it -- told to the player out of character (LLMCore's FAILED_ATTEMPT_MESSAGES).
        self.assertEqual(events[0]["payload"]["reason"], "unmatched")

        classifier.set_conversation_partner({"key": "innkeeper", "name": "innkeeper", "aliases": []})
        _processed, events, _adjudication = classifier.classify("Do you ever get tired of all this?")
        self.assertEqual(events[0]["event"], "dialogue_detected")
        self.assertTrue(events[0]["payload"]["implicit"])

        classifier.set_conversation_partner(None)
        _processed, events, _adjudication = classifier.classify("Do you ever get tired of all this?")
        self.assertEqual(events[0]["event"], "action_not_understood")

    def test_unmarked_speech_reaches_whoever_is_present(self):
        # Found by playtest: 210 turns of talk to NPCs with no "talk to" never reached dialogue
        # once -- "you look strong, bram" rolled strength, "i bet the rooms are lovely" gambling.
        classifier = IntentClassifier(FakeMatcher(actions={
            "attack the goblin": ("blades", 0.9), "bet ten gold on red": ("gambling", 0.8),
        }))
        classifier.set_present_entities([{"key": "Bram", "name": "Bram", "subtype": "human", "aliases": []}])
        for text in ("You look strong, Bram.", "Let's find somewhere quieter.", "I bet the rooms here are lovely."):
            _processed, events, _adjudication = classifier.classify(text)
            self.assertEqual(events[0]["event"], "dialogue_detected", text)
            self.assertTrue(events[0]["payload"]["implicit"], text)
        # An order given by name, or a real wager, is still an action.
        for text in ("Bram, attack the goblin", "bet ten gold on red"):
            _processed, events, _adjudication = classifier.classify(text)
            self.assertEqual(events[0]["event"], "turn_detected", text)
        # Nobody here: nobody to hear it.
        classifier.set_present_entities([])
        _processed, events, _adjudication = classifier.classify("Let's find somewhere quieter.")
        self.assertEqual(events[0]["event"], "action_not_understood")

    def test_skill_matching_scores_a_name_neutral_phrasing_too(self):
        # Found by playtest: a name or pronoun dragged "(swings at elara)" (0.38, fly) and
        # "hit her again!" (0.38) under the confidence threshold, so no attack ever started.
        matcher = SimpleNamespace(present_names=["bread vendor", "elara"])
        neutralize = SentenceTransformerMatcher._neutralize_names
        self.assertEqual(neutralize(matcher, "(swings at elara)"), "swing at someone")
        self.assertEqual(neutralize(matcher, "(swings and tackles her)"), "swing and tackle someone")
        self.assertEqual(neutralize(matcher, "knock the bread vendor over!"), "knock someone over!")
        self.assertEqual(neutralize(matcher, "hit her again!"), "hit someone again!")
        # Only an emote's verbs are rewritten; a plain sentence keeps its own.
        self.assertEqual(neutralize(matcher, "she sells shells"), "she sells shells")
        self.assertEqual(
            [_base_verb(word) for word in ("swings", "lunges", "tries", "reaches", "kiss", "his")],
            ["swing", "lunge", "try", "reach", "kiss", "his"],
        )

    def test_a_gesture_is_never_a_skill_roll(self):
        # Found by playtest: "(bows head dramatically)" rolled missiles -- a bow is a weapon.
        classifier = IntentClassifier(FakeMatcher(actions={
            "(bows head dramatically)": ("missiles", 0.7), "draw my bow": ("missiles", 0.8),
        }))
        _processed, events, _adjudication = classifier.classify("(Bows head dramatically)")
        self.assertEqual(events[0]["event"], "action_not_understood")
        _processed, events, _adjudication = classifier.classify("draw my bow")
        self.assertEqual(events[0]["event"], "turn_detected")

    def test_a_line_mixing_talk_and_an_action_splits_in_the_order_written(self):
        # Found by playtest: once unmarked speech reached anyone present, a taunt anywhere in a
        # line swallowed the action beside it.
        classifier = IntentClassifier(FakeMatcher(actions={
            "punch bram.": ("brawling", 0.75), "hit her again!": ("brawling", 0.73),
            "never mind, i'll just take the goods instead!": ("psionics", 0.59),
            "a name, man.": ("appraise", 0.54),
        }))
        classifier.set_present_entities([{"key": "Bram", "name": "Bram", "subtype": "human", "aliases": []}])

        _processed, events, _adjudication = classifier.classify("You call that a fight? Punch Bram.")
        self.assertEqual([event["event"] for event in events], ["dialogue_detected", "turn_detected"])
        self.assertEqual(events[0]["payload"]["utterance"], "You call that a fight?")
        self.assertEqual(events[1]["payload"]["clauses"][0]["skill"], "brawling")

        _processed, events, _adjudication = classifier.classify("Hit her again! You deserve it.")
        self.assertEqual([event["event"] for event in events], ["turn_detected", "dialogue_detected"])
        self.assertEqual(events[1]["payload"]["utterance"], "You deserve it.")

        # An action half that resolves to nothing real leaves the whole line as talk.
        # A verbless fragment of the talk is never the action half, however it scores (found by
        # playtest: "a name, man." rolled appraise).
        for text in ("Forget the lumber. Let's find a private place.",
                     "Stomach for snacks? Never mind, I'll just take the goods instead!",
                     "A name, man. You gotta give me a name, not just a description of a group of guys."):
            _processed, events, _adjudication = classifier.classify(text)
            self.assertEqual([event["event"] for event in events], ["dialogue_detected"], text)

    def test_a_quoted_shout_beside_an_attack_keeps_the_attack(self):
        # Found by playtest: eleven of a brawler's forty turns paired a shout with an attack, and
        # every attack was dropped as dialogue.
        classifier = IntentClassifier(FakeMatcher(actions={
            "swing a fist at bram's side.": ("brawling", 0.45), "yell": ("intimidation", 0.6),
            "ask the guard": ("streetwise", 0.6),
        }))
        classifier.set_present_entities([{"key": "Bram", "name": "Bram", "subtype": "human", "aliases": []}])

        _processed, events, _adjudication = classifier.classify('I yell "Hey!" and swing a fist at Bram\'s side.')
        self.assertEqual([event["event"] for event in events], ["dialogue_detected", "turn_detected"])
        self.assertEqual(events[0]["payload"]["utterance"], "Hey!")
        # The tag on the quote ("yell") is never an action of its own.
        self.assertEqual([clause["skill"] for clause in events[1]["payload"]["clauses"]], ["brawling"])

        # Nothing real outside the quotes leaves the line whole.
        for text in ('I laugh, "Nice try," and shake my head.', 'ask the guard "where is the inn?"'):
            _processed, events, _adjudication = classifier.classify(text)
            self.assertEqual([event["event"] for event in events], ["dialogue_detected"], text)

    def test_described_speech_is_reported_dialogue_and_keeps_the_action_beside_it(self):
        # Found by playtest: nine of a brawler's forty turns were lines like these, and they came
        # back not-understood or rolled artistry/reflexes.
        classifier = IntentClassifier(FakeMatcher(actions={
            "taunt the vendor about his woodpile.": ("artistry", 0.6),
            "try to shove them.": ("bull rush", 0.88), "needs reinforcement.": ("strength", 0.3),
        }))
        classifier.set_present_entities([{"key": "Bram", "name": "Bram", "subtype": "human", "aliases": []}])

        _processed, events, _adjudication = classifier.classify("Taunt the vendor about his woodpile.")
        self.assertEqual([event["event"] for event in events], ["dialogue_detected"])
        self.assertEqual(events[0]["payload"]["utterance"], "You taunt the vendor about his woodpile.")

        _processed, events, _adjudication = classifier.classify("Yells that Bram's net looks flimsy and needs reinforcement.")
        self.assertEqual([event["event"] for event in events], ["dialogue_detected"])
        self.assertEqual(events[0]["payload"]["utterance"], "You yell that Bram's net looks flimsy and needs reinforcement.")

        _processed, events, _adjudication = classifier.classify("Shout a challenge, then try to shove them.")
        self.assertEqual([event["event"] for event in events], ["dialogue_detected", "turn_detected"])
        self.assertEqual(events[1]["payload"]["clauses"][0]["skill"], "bull rush")

        # Found by playtest: a hyphenated verb crashed the turn.
        _processed, events, _adjudication = classifier.classify("I mock-yell a challenge.")
        self.assertEqual(events[0]["payload"]["utterance"], "You mock yell a challenge.")

        # Nobody to hear it, or only wondering about it: left as it was.
        _processed, events, _adjudication = IntentClassifier(FakeMatcher()).classify("Yell for help.")
        self.assertEqual(events[0]["event"], "action_not_understood")
        _processed, events, _adjudication = classifier.classify("If I yell at him, will he run?")
        self.assertEqual(events[0]["payload"]["speech_form"], "verbatim")

    def test_following_or_heading_toward_someone_closes_the_distance_but_a_place_is_travel(self):
        # Found by playtest: "i follow her at a respectful distance" and "i'll proceed carefully
        # toward the wyrmwatch" were not understood; "head toward the docks" must still travel.
        classifier = IntentClassifier(FakeMatcher(destinations={"head toward the docks": ("shipyard", 0.8)}))
        _processed, events, _adjudication = classifier.classify("head toward the docks")
        self.assertEqual((events[0]["payload"]["intent"], events[0]["payload"]["destination"]), ("travel", "shipyard"))
        for text in ("I follow her at a respectful distance.", "I'll proceed carefully toward the Wyrmwatch.",
                     "walk toward the fishmonger"):
            _processed, events, _adjudication = classifier.classify(text)
            self.assertEqual(events[0]["payload"].get("intent"), "advance", text)

    def _adjudicating(self, verdicts, **matcher_kwargs):
        matcher = FakeMatcher(**matcher_kwargs)
        matcher.adjudications = verdicts
        classifier = IntentClassifier(matcher)
        classifier.set_present_entities([{"key": "Finn", "name": "Finn", "subtype": "human", "aliases": []}])
        return classifier, matcher

    def test_an_opening_word_guess_at_talk_is_checked_with_the_model(self):
        # Found by playtest: "let's go down that cut-through." went to dialogue on "let's".
        classifier, matcher = self._adjudicating({
            "let's go down that cut-through.": "action", "hmm, the tide seems early today.": "musing",
            "i'm not paying that much.": "speech",
        }, actions={"let's go down that cut-through.": ("athletics", 0.55)})
        classifier.set_recent_narration("Finn points toward a narrow alley.")

        _processed, events, adjudication = classifier.classify("Let's go down that cut-through.")
        self.assertEqual(events[0]["event"], "turn_detected")
        self.assertEqual((adjudication.verdict, adjudication.trigger), ("action", "declarative"))
        self.assertEqual(matcher.adjudicated[-1][1:], (("Finn",), None, "Finn points toward a narrow alley."))

        _processed, events, _adjudication = classifier.classify("Hmm, the tide seems early today.")
        self.assertEqual(events[0]["event"], "action_not_understood")
        _processed, events, _adjudication = classifier.classify("I'm not paying that much.")
        self.assertEqual(events[0]["event"], "dialogue_detected")

        # A question stays talk without asking.
        _processed, events, adjudication = classifier.classify("Where does it lead?")
        self.assertEqual(events[0]["event"], "dialogue_detected")
        self.assertFalse(adjudication.asked)

    def test_a_weak_action_beside_a_quote_is_checked_with_the_model(self):
        # Found by playtest: "reach out, tapping the heavy metal ring on his wrist" beside a quote
        # rolled polearms at 0.52 and was narrated as a sword strike.
        line = 'I smirk and reach out, tapping the ring on his wrist. "Maybe I\'ll prove it."'
        action_text = "smirk and reach out, tapping the ring on his wrist."
        classifier, _matcher = self._adjudicating({action_text: "speech"}, actions={"reach out": ("polearms", 0.52)})
        _processed, events, adjudication = classifier.classify(line)
        self.assertEqual([event["event"] for event in events], ["dialogue_detected"])
        self.assertEqual((adjudication.verdict, adjudication.trigger), ("speech", "weak_quoted"))

        classifier, _matcher = self._adjudicating({action_text: "action"}, actions={"reach out": ("polearms", 0.52)})
        _processed, events, _adjudication = classifier.classify(line)
        self.assertEqual([event["event"] for event in events], ["turn_detected", "dialogue_detected"])

    def test_a_gesture_beside_a_quote_keeps_both_halves(self):
        # Found by playtest (gooner persona): a caress beside a whisper was kept whole as talk five
        # times in forty turns, the gesture half dropped; seven more rolled strength/dodge for it.
        line = 'I smirk and reach out, tapping the ring on his wrist. "Maybe I\'ll prove it."'
        action_text = "smirk and reach out, tapping the ring on his wrist."
        verdict = {"kind": "gesture", "game_action": None, "item": None, "tone": "warm"}
        classifier, _matcher = self._adjudicating({action_text: verdict}, actions={"reach out": ("polearms", 0.52)})

        _processed, events, adjudication = classifier.classify(line)

        self.assertEqual([event["event"] for event in events], ["turn_detected", "dialogue_detected"])
        turn = events[0]["payload"]
        # The weak skill guess is replaced, not rolled beside the gesture; the turn's text is the
        # action half alone, so the target is never read out of the quote.
        self.assertEqual(
            turn["clauses"], [{"kind": "item", "intent": "gesture", "item_name": None, "phrase": None, "tone": "warm"}],
        )
        self.assertEqual(turn["input"], action_text)
        self.assertEqual((adjudication.verdict, adjudication.tone, adjudication.trigger), ("gesture", "warm", "weak_quoted"))

        # Speech first when only its tag precedes the quote, as for any other action beside a quote.
        spoken_first = 'I whisper "Maybe I\'ll prove it." and reach out, tapping the ring on his wrist.'
        classifier, _matcher = self._adjudicating(
            {"reach out, tapping the ring on his wrist.": verdict}, actions={"reach out": ("polearms", 0.52)},
        )
        _processed, events, _adjudication = classifier.classify(spoken_first)
        self.assertEqual(sorted(event["event"] for event in events), ["dialogue_detected", "turn_detected"])

        # An "action" verdict still rolls, and any other verdict still keeps the line as talk.
        classifier, _matcher = self._adjudicating({action_text: "action"}, actions={"reach out": ("polearms", 0.52)})
        self.assertEqual([e["event"] for e in classifier.classify(line)[1]], ["turn_detected", "dialogue_detected"])
        classifier, _matcher = self._adjudicating({action_text: "speech"}, actions={"reach out": ("polearms", 0.52)})
        self.assertEqual([e["event"] for e in classifier.classify(line)[1]], ["dialogue_detected"])

    def test_a_stage_direction_beside_talk_keeps_both_halves(self):
        # Found by the gooner persona, which writes every turn as "(stage direction) words": the model
        # could only name one kind for the whole line and said "action", losing the talk and the
        # gesture both, and eleven of forty turns came back not understood.
        gesture = {"kind": "gesture", "game_action": None, "item": None, "tone": "warm"}
        stare = "i pause, letting my stare linger on her for a beat too long."
        wink = "i wink."
        classifier, _matcher = self._adjudicating({stare: gesture, wink: gesture})

        # Stage direction first: it opens like an action, so the talk is told apart by the parentheses.
        _processed, events, adjudication = classifier.classify(f"(I pause, letting my stare linger on her for a beat too long.) Wouldn't dream of it.")
        self.assertEqual([event["event"] for event in events], ["turn_detected", "dialogue_detected"])
        self.assertEqual(
            events[0]["payload"]["clauses"],
            [{"kind": "item", "intent": "gesture", "item_name": None, "phrase": None, "tone": "warm"}],
        )
        self.assertEqual(events[1]["payload"]["utterance"], "Wouldn't dream of it.")
        self.assertEqual((adjudication.verdict, adjudication.trigger), ("gesture", "weak_split"))

        # Stage direction last: the talk comes first, in the order written.
        _processed, events, _adjudication = classifier.classify("You know it. (I wink.)")
        self.assertEqual([event["event"] for event in events], ["dialogue_detected", "turn_detected"])

        # Only the stage direction is put to the model, never the talk.
        asked = [text for text, *_ in classifier.matcher.adjudicated]
        self.assertEqual(asked, [stare, wink])

    def test_a_starred_emote_is_a_stage_direction_and_the_talk_beside_it_is_never_a_command(self):
        # Found by the gooner persona's third style, "*i lean in.* relax. a solid night's rest.": the
        # word "rest" in the talk ran a real rest, which an enemy nearby then silently refused, so the
        # turn produced no narration at all.
        gesture = {"kind": "gesture", "game_action": None, "item": None, "tone": "intimate"}
        pressed = "i keep my body pressed close to hers, whispering right against her ear."
        classifier, _matcher = self._adjudicating({pressed: gesture})

        _processed, events, _adjudication = classifier.classify(
            f"*I keep my body pressed close to hers, whispering right against her ear.* "
            f"Relax. You look like you could use a solid night's rest."
        )

        self.assertEqual([event["event"] for event in events], ["turn_detected", "dialogue_detected"])
        self.assertEqual(events[0]["payload"]["clauses"][0]["intent"], "gesture")
        self.assertEqual(events[1]["payload"]["utterance"], "Relax. You look like you could use a solid night's rest.")

    def test_a_bracketed_aside_is_not_a_stage_direction(self):
        from nlp.Intent_Classification import mask_talk, stage_directions
        self.assertEqual(stage_directions("walk to the docks (it's far)"), [])
        self.assertEqual([content for _s, _e, content in stage_directions("(i wink.) hi")], ["i wink."])
        self.assertEqual([content for _s, _e, content in stage_directions("*sighs* ok")], ["sighs"])
        # Only stage directions survive the talk mask, and only when there is talk beside them.
        self.assertEqual(mask_talk("walk to the docks (it's far)"), "walk to the docks (it's far)")
        self.assertEqual(mask_talk("*i lean in.* relax. a rest.").strip(), "*i lean in.*")
        self.assertEqual(mask_talk("*i lean in.*"), "*i lean in.*")

    def test_a_stage_direction_alone_or_beside_a_dialogue_keyword_is_unchanged(self):
        classifier, _matcher = self._adjudicating({})
        _processed, events, _adjudication = classifier.classify("(I draw my sword.)")
        self.assertEqual(events[0]["event"], "action_not_understood")
        _processed, events, _adjudication = classifier.classify("(I smile.) Ask Finn about rooms.")
        self.assertEqual([event["event"] for event in events], ["dialogue_detected"])

    def test_a_stage_direction_the_model_does_not_call_a_gesture_leaves_the_line_as_it_was(self):
        classifier, _matcher = self._adjudicating({"i wink.": "speech"})
        _processed, events, _adjudication = classifier.classify("(I wink.) Just trying to get close to you.")
        self.assertNotIn("turn_detected", [event["event"] for event in events])

    def test_a_weak_turn_or_an_unclaimed_line_is_checked_with_the_model(self):
        classifier, _matcher = self._adjudicating({
            "count to three for me finn": "speech", "explain how wounds heal": "game_question",
        }, actions={"count to three for me finn": ("appraise", 0.54)})
        _processed, events, adjudication = classifier.classify("Count to three for me Finn")
        self.assertEqual(events[0]["event"], "dialogue_detected")
        self.assertEqual((adjudication.verdict, adjudication.trigger), ("speech", "weak_turn"))

        _processed, events, adjudication = classifier.classify("Explain how wounds heal")
        self.assertEqual(events[0]["event"], "help_detected")
        self.assertEqual((adjudication.verdict, adjudication.trigger), ("game_question", "not_understood"))

        # Nothing carries over: an input that asks nobody gets a fresh, unasked record.
        _processed, events, adjudication = classifier.classify("take the longsword")
        self.assertFalse(adjudication.asked)

    def test_a_gesture_verdict_becomes_a_turn_costing_item_clause_carrying_its_tone(self):
        # "kiss her" matches no skill and no item: the model's call is all that stands between it
        # and being dropped. The clause is an item-kind one, so it takes a turn slot and never rolls.
        verdict = {"kind": "gesture", "game_action": None, "item": None, "tone": "intimate"}
        classifier, _matcher = self._adjudicating({"kiss finn": verdict, "bow to finn": verdict})

        for text in ("Kiss Finn", "Bow to Finn"):
            _processed, events, adjudication = classifier.classify(text)
            self.assertEqual([event["event"] for event in events], ["turn_detected"], text)
            [clause] = events[0]["payload"]["clauses"]
            self.assertEqual(
                clause, {"kind": "item", "intent": "gesture", "item_name": None, "phrase": None, "tone": "intimate"},
            )
            self.assertEqual((adjudication.verdict, adjudication.tone, adjudication.trigger), ("gesture", "intimate", "not_understood"))

    def test_a_gesture_verdict_also_displaces_a_weak_skill_guess(self):
        verdict = {"kind": "gesture", "game_action": None, "item": None, "tone": "warm"}
        classifier, _matcher = self._adjudicating(
            {"hug finn": verdict}, actions={"hug finn": ("brawling", 0.54)},
        )

        _processed, events, adjudication = classifier.classify("Hug Finn")

        [clause] = events[0]["payload"]["clauses"]
        self.assertEqual((clause["intent"], clause["tone"]), ("gesture", "warm"))
        self.assertEqual(adjudication.trigger, "weak_turn")

    def test_a_confident_skill_match_is_never_asked_about(self):
        verdict = {"kind": "gesture", "game_action": None, "item": None, "tone": "warm"}
        classifier, matcher = self._adjudicating(
            {"hug finn": verdict}, actions={"hug finn": ("brawling", 0.8)},
        )

        _processed, events, adjudication = classifier.classify("Hug Finn")

        self.assertEqual(events[0]["payload"]["clauses"], [{"kind": "action", "skill": "brawling", "score": 0.8}])
        self.assertFalse(adjudication.asked)

    def test_no_listener_means_a_gesture_is_never_asked_about(self):
        alone = FakeMatcher()
        alone.adjudications = {"dance": {"kind": "gesture", "game_action": None, "item": None, "tone": "neutral"}}
        _processed, events, _adjudication = IntentClassifier(alone).classify("Dance")
        self.assertEqual(events[0]["event"], "action_not_understood")
        self.assertFalse(getattr(alone, "adjudicated", []))

    def test_no_model_answer_leaves_the_rules_call(self):
        classifier, _matcher = self._adjudicating({}, actions={"count to three for me finn": ("appraise", 0.54)})
        _processed, events, _adjudication = classifier.classify("Count to three for me Finn")
        self.assertEqual(events[0]["event"], "turn_detected")
        _processed, events, _adjudication = classifier.classify("Stay right there!")
        self.assertEqual(events[0]["event"], "dialogue_detected")  # the "!" rule still applies

        # Nobody present: never asked at all.
        alone = FakeMatcher()
        alone.adjudications = {"look around for a place to rest.": "speech"}
        _processed, events, _adjudication = IntentClassifier(alone).classify("Look around for a place to rest.")
        self.assertFalse(getattr(alone, "adjudicated", []))

    def test_an_action_the_model_names_as_a_purchase_reaches_trade(self):
        # Found by playtest: both lines were judged actions but came back not understood.
        buy = lambda item: {"kind": "action", "game_action": "buy", "item": item}
        classifier, _matcher = self._adjudicating({
            "let's get the peppers.": buy("the smoked peppers"),
            "here are the coppers.": buy("Smoked Peppers"),
            "let's get the rope.": buy("the rope"),
        }, items={"smoked peppers": ("smoked peppers", 0.9)}, actions={"let's get the rope.": ("athletics", 0.4)})
        classifier.set_recent_narration("Barnaby offers smoked peppers for four coppers.")

        # Narrated stock with no catalog entry is improvised into the seller's inventory.
        _processed, events, adjudication = classifier.classify("Let's get the peppers.")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "improvisation_requested")
        self.assertEqual((events[0]["payload"]["intent"], events[0]["payload"]["phrase"]), ("trade", "the smoked peppers"))
        self.assertEqual(adjudication.action, ("buy", "the smoked peppers"))

        # Paying is buying the thing on offer, found in the catalog when it's there.
        _processed, events, _adjudication = classifier.classify("Here are the coppers.")
        self.assertEqual(events[0]["event"], "turn_detected")
        self.assertEqual(events[0]["payload"]["clauses"], [{"kind": "item", "intent": "trade", "item_name": "smoked peppers", "phrase": "smoked peppers"}])

        # A guessed skill doesn't beat the purchase.
        _processed, events, _adjudication = classifier.classify("Let's get the rope.")
        self.assertEqual(events[0]["event"], "improvisation_requested")
        self.assertEqual(events[0]["payload"]["intent"], "trade")

    def test_an_adjudicated_action_never_moves_money_or_guesses_an_item(self):
        classifier, _matcher = self._adjudicating({
            "let's settle up.": {"kind": "action", "game_action": "buy", "item": None},
            "here you go.": {"kind": "action", "game_action": "give", "item": "four coppers"},
            "there, all yours.": {"kind": "action", "game_action": "give", "item": "the gold pieces"},
            "here, for your trouble.": {"kind": "action", "game_action": "give", "item": "the shiny stuff"},
            "let's do this.": {"kind": "action", "game_action": "other", "item": "the door"},
        }, items={"the shiny stuff": ("currency", 1.0)})
        classifier.matcher.adjudications["i'll scoop up the coins."] = {"kind": "action", "game_action": "take", "item": "the coins"}
        for text in ("Let's settle up.", "Here you go.", "There, all yours.", "Let's do this.", "I'll scoop up the coins."):
            _processed, events, _adjudication = classifier.classify(text)
            self.assertNotIn(events[0]["event"], ("turn_detected", "improvisation_requested"), text)
            # Paying or handing over money is said to the listener; taking it is not.
            self.assertEqual(events[0]["event"] == "dialogue_detected", text in ("Here you go.", "There, all yours."), text)
        _processed, events, _adjudication = classifier.classify("Here, for your trouble.")
        self.assertFalse(any(
            clause.get("item_name") == "currency"
            for event in events for clause in event["payload"].get("clauses", [])
        ))

    def test_a_barked_line_nothing_else_claims_is_said_to_whoever_hears_it(self):
        # Found by playtest: six of a brawler's fifteen not-understood turns were lines like these.
        classifier = IntentClassifier(FakeMatcher())
        _processed, events, _adjudication = classifier.classify("Stay right there!")
        self.assertEqual(events[0]["event"], "action_not_understood")  # nobody to hear it

        classifier.set_present_entities([{"key": "Bram", "name": "Bram", "subtype": "human", "aliases": []}])
        for text in ("Stay right there!", "Keep your hands up!"):
            _processed, events, _adjudication = classifier.classify(text)
            self.assertEqual(events[0]["event"], "dialogue_detected", text)
            self.assertEqual(events[0]["payload"]["utterance"], text)
        _processed, events, _adjudication = classifier.classify("Stay right there")
        self.assertEqual(events[0]["event"], "action_not_understood")

    def test_an_unmatched_item_verb_is_improvised_rather_than_guessed_at(self):
        # Found by playtest: picking up a book the narrator had just described rolled finesse via
        # the keyword fallback (0.21) instead of making the book real.
        classifier = IntentClassifier(FakeMatcher(actions={"pick up the damp ledger book": ("finesse", 0.21)}))
        _processed, events, _adjudication = classifier.classify("pick up the damp ledger book")
        self.assertEqual(events[0]["event"], "improvisation_requested")

        classifier = IntentClassifier(FakeMatcher(actions={"pick up the damp ledger book": ("finesse", 0.7)}))
        _processed, events, _adjudication = classifier.classify("pick up the damp ledger book")
        self.assertEqual(events[0]["event"], "turn_detected")

    def test_a_hypothetical_never_gives_takes_or_travels(self):
        # Found by playtest: the first handed the player's whole purse to a bystander, the second
        # set off travel and the narrator invented a barrier.
        hypotheticals = (
            "but if i just use a coupon then i only gotta give you the promise of the actual coins next week right?",
            "if i promise to give you an empty bucket does that work?",
            "if i take the proof of the goods can i leave?",
            "what if i drop my sword?",
        )
        matcher = FakeMatcher()
        matcher.map_to_item = lambda clause: next(
            ((name, 0.9) for name in ("coin", "bucket", "goods", "sword") if name in clause), (None, 0.0),
        )
        classifier = IntentClassifier(matcher)
        classifier.set_present_entities([{"key": "Bram", "name": "Bram", "subtype": "human", "aliases": []}])
        for text in hypotheticals:
            _processed, events, _adjudication = classifier.classify(text)
            self.assertEqual([event["event"] for event in events], ["dialogue_detected"], text)
        # The same verbs, declared, still act.
        for text in ("give bram the coins", "can i take the goods?", "drop my sword"):
            _processed, events, _adjudication = classifier.classify(text)
            self.assertEqual(events[0]["event"], "turn_detected", text)

        self.assertFalse(is_hypothetical("can i take the sword?"))
        self.assertFalse(is_hypothetical("give him the coins."))
        self.assertTrue(is_hypothetical("should i give him the coins?"))

    def test_an_inflected_item_verb_still_reaches_its_intent(self):
        # Found by playtest: "(grabs a nearby loaf of bread)" and "i'm taking all of it" never
        # reached take. Only the main verb is rewritten -- "the opening" stays a noun.
        self.assertEqual(normalize_declared_verb("(grabs a nearby loaf of bread)"), "grab a nearby loaf of bread")
        self.assertEqual(normalize_declared_verb("i'm just taking the goods"), "take the goods")
        self.assertEqual(normalize_declared_verb("(drops the box)"), "drop the box")
        self.assertEqual(normalize_declared_verb("crawl through the opening"), "crawl through the opening")
        classifier = IntentClassifier(FakeMatcher(items={"(grabs a nearby loaf of bread)": ("bread", 0.8)}))
        _processed, events, _adjudication = classifier.classify("(Grabs a nearby loaf of bread)")
        self.assertEqual(events[0]["payload"]["clauses"][0]["intent"], "take")

    def test_a_question_about_the_game_itself_goes_to_adam(self):
        # Found by playtest: once unmarked talk reached anyone present, 150 turns of
        # rules-lawyering went to a fisherman as in-character dialogue.
        classifier = IntentClassifier(FakeMatcher(actions={"roll for initiative against the goblin": ("reflexes", 0.8)}))
        classifier.set_present_entities([{"key": "Bram", "name": "Bram", "subtype": "human", "aliases": []}])
        for text in ("Does the rulebook even say we have to know that?", "are we supposed to roll a dice for this?",
                     "and also what's the action economy, because i've done three things"):
            _processed, events, _adjudication = classifier.classify(text)
            self.assertEqual(events[0]["event"], "help_detected", text)
        _processed, events, _adjudication = classifier.classify("roll for initiative against the goblin")
        self.assertEqual(events[0]["event"], "turn_detected")

    def test_the_rules_of_something_in_the_fiction_stay_in_character(self):
        # Found by playtest: mid-conversation, this went to ADaM, which answered from the
        # sourcebook ("According to the provided lore, the Abyss...").
        self.assertFalse(detect_out_of_character(process_input(
            "No terms, you say? Does the deep have rules? What are the rules of the 'beautiful, terrible mess,' then?"
        )))
        self.assertTrue(detect_out_of_character(process_input("is that against the rules?")))

    def test_social_skill_attempt_still_rolls_mid_conversation(self):
        classifier = IntentClassifier(FakeMatcher(actions={"persuade him to lower the price": ("charisma", 0.9)}))
        classifier.set_conversation_partner({"key": "innkeeper", "name": "innkeeper", "aliases": []})
        _processed, events, _adjudication = classifier.classify("persuade him to lower the price")
        self.assertEqual(events[0]["event"], "turn_detected")
        self.assertEqual(events[0]["payload"]["clauses"][0]["skill"], "charisma")

    def _frame(self, raw):
        processed = process_input(raw)
        return frame_speech(raw, processed, detect_dialogue_intent(processed))

    def test_frame_speech_turns_a_bare_address_into_a_greeting(self):
        self.assertEqual(self._frame("Talk to the fishmonger"), {"speech_form": "greet", "utterance": None})
        self.assertEqual(self._frame("Greet Silas")["speech_form"], "greet")

    def test_frame_speech_restates_a_keyword_request_in_the_second_person(self):
        self.assertEqual(self._frame("Ask about the kelp beds")["utterance"], "You ask about the kelp beds.")
        self.assertEqual(self._frame("Tell Silas to back off")["utterance"], "You tell Silas to back off.")
        # From the keyword on: a movement clause before it is its own (quiet) intent.
        self.assertEqual(
            self._frame("I approach the merchant and ask about the celebration")["utterance"],
            "You ask about the celebration.",
        )

    def test_frame_speech_keeps_the_players_own_words_verbatim(self):
        self.assertEqual(
            self._frame('I approach the fishmonger. "Is something going on?"'),
            {"speech_form": "verbatim", "utterance": "Is something going on?"},
        )
        # Aimed back at the speaker: direct speech, not "You tell me...".
        self.assertEqual(self._frame("Tell me what you know")["speech_form"], "verbatim")
        # A keyword with nothing after it greets no one (found by playtest).
        self.assertEqual(self._frame("A gate, you say? Where does this passage open, and how can we tell?")["speech_form"],
                         "verbatim")
        self.assertEqual(
            frame_speech("Do you ever get tired?", process_input("Do you ever get tired?"), False),
            {"speech_form": "verbatim", "utterance": "Do you ever get tired?"},
        )

    def test_a_scare_quoted_word_is_not_the_players_spoken_line(self):
        # Found by playtest: 'ask them what the real "currents" are' was put to an NPC as the
        # player saying just "currents".
        framed = self._frame('Maybe I should ask them what the real "currents" are these days.')
        self.assertNotEqual(framed["utterance"], "currents")
        self.assertFalse(detect_dialogue_intent(process_input('i search the "abandoned" mill')))
        self.assertEqual(self._frame('"Hi!"')["utterance"], "Hi!")

    def test_dialogue_detected_carries_the_speech_framing(self):
        classifier = IntentClassifier(FakeMatcher())
        _processed, events, _adjudication = classifier.classify("Ask about the kelp beds")
        self.assertEqual(events[0]["payload"]["speech_form"], "reported")
        self.assertEqual(events[0]["payload"]["utterance"], "You ask about the kelp beds.")

    def test_lore_check_question_is_not_paired_with_implicit_dialogue(self):
        classifier = IntentClassifier(FakeMatcher())
        classifier.set_conversation_partner({"key": "innkeeper", "name": "innkeeper", "aliases": []})
        _processed, events, _adjudication = classifier.classify("what do you know about the troll")
        self.assertEqual([event["event"] for event in events], ["item_interaction_detected"])
        self.assertEqual(events[0]["payload"]["intent"], "lore_check")

    def test_adam_wins_over_both_item_verb_and_dialogue_in_the_same_input(self):
        # Checked ahead of both the item-interaction pass and DIALOGUE_KEYWORDS -- naming
        # "adam" anywhere in the input always reaches the help channel, never ordinary
        # dialogue or a real item turn, no matter what else the input contains.
        classifier = IntentClassifier(FakeMatcher(items={"the longsword to thane": ("longsword", 0.9)}))
        _processed, events, _adjudication = classifier.classify("talk to ADaM and give the longsword to thane")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "help_detected")
        self.assertIn("adam", events[0]["payload"]["input"])

    def test_removal_candidate_flag_only_set_when_removal_keywords_present(self):
        classifier = IntentClassifier(FakeMatcher())

        _processed, events, _adjudication = classifier.classify("adam, get rid of that torch")
        self.assertTrue(events[0]["payload"]["removal_candidate"])

        _processed, events, _adjudication = classifier.classify("adam, what are my skills")
        self.assertFalse(events[0]["payload"]["removal_candidate"])

    def test_creature_and_edit_candidate_flags_only_set_when_their_own_keywords_present(self):
        classifier = IntentClassifier(FakeMatcher())

        _processed, events, _adjudication = classifier.classify("adam, summon a wolf")
        self.assertTrue(events[0]["payload"]["creature_candidate"])
        self.assertFalse(events[0]["payload"]["edit_candidate"])
        self.assertFalse(events[0]["payload"]["removal_candidate"])

        _processed, events, _adjudication = classifier.classify("adam, change the torch's description")
        self.assertTrue(events[0]["payload"]["edit_candidate"])
        self.assertFalse(events[0]["payload"]["creature_candidate"])

        _processed, events, _adjudication = classifier.classify("adam, what are my skills")
        self.assertFalse(events[0]["payload"]["creature_candidate"])
        self.assertFalse(events[0]["payload"]["edit_candidate"])

    def test_bare_scene_query_reaches_its_own_channel_not_examine_or_clarification(self):
        # No "adam" said at all -- before this intent existed, "what do i see" matched no
        # EXAMINE_KEYWORDS phrase and would have fallen through to action_not_understood (or,
        # for an item-shaped phrasing, ad hoc item generation) with nothing to ground it.
        classifier = IntentClassifier(FakeMatcher())
        _processed, events, _adjudication = classifier.classify("what do i see")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "scene_query_detected")
        self.assertEqual(events[0]["payload"], {"input": "what do i see"})

    def test_semantic_router_rescues_a_phrasing_every_keyword_gate_missed(self):
        # The whole point of the router: "who all is here" is a trivial paraphrase of
        # SCENE_QUERY_KEYWORDS' own "who is here", and a total miss to a substring check.
        # Before this, it reached action_not_understood and the clarification prompt invented
        # three tavern patrons out of nothing.
        classifier = IntentClassifier(FakeMatcher(intents={"who all is here": ("scene_query", 0.9)}))
        _processed, events, _adjudication = classifier.classify("who all is here")

        self.assertEqual(events, [{"event": "scene_query_detected", "payload": {
            "input": "who all is here",
        }}])

    def test_semantic_router_never_shadows_a_real_skill_match(self):
        # THE regression guard for this feature. The router is only safe because it runs at
        # _finalize's give-up point -- if it ever ran earlier, or ran despite a matched clause,
        # it could silently reroute a genuine action. A matcher that would confidently route
        # this to scene_query must still lose to the skill that actually matched.
        classifier = IntentClassifier(FakeMatcher(
            actions={"search the room for hidden traps": ("observation", 0.8)},
            intents={"search the room for hidden traps": ("scene_query", 0.99)},
        ))
        _processed, events, _adjudication = classifier.classify("search the room for hidden traps")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "turn_detected")
        self.assertEqual(events[0]["payload"]["clauses"][0]["skill"], "observation")

    def test_semantic_router_declines_below_threshold_and_action_not_understood_still_fires(self):
        # A false negative costs nothing -- it's exactly today's behavior. That asymmetry is
        # why the intent thresholds sit above confidence_threshold rather than at it.
        classifier = IntentClassifier(FakeMatcher())
        _processed, events, _adjudication = classifier.classify("mrrgghfff")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "action_not_understood")

    def test_semantic_router_outranks_improvisation_only_on_the_stricter_bar(self):
        # EXAMINE_KEYWORDS' own "check out" makes this a recognized item verb while no
        # SCENE_QUERY_KEYWORDS phrase matches it, so without the router it reaches ad hoc item
        # generation and gets asked to conjure "the room" as a takeable object. (Note "look at
        # who's here" is NOT such a case -- it contains SCENE_QUERY_KEYWORDS' own "who's here"
        # and the keyword gate claims it long before either path.) The router is allowed to
        # take this -- but only on the higher bar, since it's displacing a working path rather
        # than filling a silent give-up. FakeMatcher applies the same 0.65 override threshold
        # the real matcher does.
        confident = IntentClassifier(FakeMatcher(intents={"check out the room": ("scene_query", 0.73)}))
        _processed, events, _adjudication = confident.classify("check out the room")
        self.assertEqual(events[0]["event"], "scene_query_detected")

        # Below it, improvisation keeps the turn exactly as it does today.
        marginal = IntentClassifier(FakeMatcher(intents={"check out the room": ("scene_query", 0.56)}))
        _processed, events, _adjudication = marginal.classify("check out the room")
        self.assertEqual(events[0]["event"], "improvisation_requested")

    def test_travel_carries_a_semantic_destination_from_both_producers(self):
        # One shared _travel_event builds this payload for the TRAVEL_KEYWORDS gate and the
        # router alike, so the two can't drift -- "the tavern" never matches "The White Deer
        # Tavern and Inn" literally, which is exactly what the destination key is for.
        matcher = FakeMatcher(
            destinations={"go to the tavern": ("white_deer", 0.8), "head into the tavern": ("white_deer", 0.8)},
            intents={"head into the tavern": ("travel", 0.9)},
        )
        # Producer 1: the keyword gate ("go to " is a TRAVEL_KEYWORDS phrase).
        _processed, events, _adjudication = IntentClassifier(matcher).classify("go to the tavern")
        self.assertEqual(events[0]["payload"]["destination"], "white_deer")
        # Producer 2: the router ("head into" is not a TRAVEL_KEYWORDS phrase at all).
        _processed, events, _adjudication = IntentClassifier(matcher).classify("head into the tavern")
        self.assertEqual(events[0]["payload"]["intent"], "travel")
        self.assertEqual(events[0]["payload"]["destination"], "white_deer")

    def test_adam_addressed_scene_question_still_reaches_the_help_channel(self):
        # ADAM_NAME_PATTERN is checked first regardless -- "adam, what do i see" must still
        # reach the out-of-character help channel, not this in-fiction one.
        classifier = IntentClassifier(FakeMatcher())
        _processed, events, _adjudication = classifier.classify("adam, what do i see")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "help_detected")

    def test_unmatched_item_verb_triggers_improvisation_instead_of_action_not_understood(self):
        # FakeMatcher's map_to_item/map_to_action both miss (default) for this phrase -- the
        # whole turn would otherwise resolve to nothing at all, so the recognized-but-unmatched
        # "take" verb becomes DM_Improvisation.py's own last-resort candidate instead.
        classifier = IntentClassifier(FakeMatcher())
        _processed, events, _adjudication = classifier.classify("take the strange glowing talisman")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "improvisation_requested")
        self.assertEqual(events[0]["payload"]["intent"], "take")

    def test_matched_clause_elsewhere_in_input_still_wins_over_improvisation(self):
        # A compound input where one clause resolves normally still takes the ordinary path --
        # extending ad hoc creation into multi-clause turns is deliberately out of scope (see
        # CLAUDE.md's "Ad hoc entity creation and removal").
        classifier = IntentClassifier(FakeMatcher(actions={"attack the wolf": ("blades", 0.9)}))
        _processed, events, _adjudication = classifier.classify("take the strange glowing talisman and attack the wolf")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "turn_detected")

    def test_low_confidence_input_publishes_action_not_understood(self):
        classifier = IntentClassifier(FakeMatcher())
        _processed, events, _adjudication = classifier.classify("Hey there innkeeper")
        self.assertEqual(events[0]["event"], "action_not_understood")

    def test_modifier_is_stripped_before_the_base_ability_match_and_attached_to_the_clause(self):
        # "empowered" is stripped out first (match_modifier), leaving "cast an fireball" to
        # score against map_to_action -- the whole point being that "fireball" alone is what
        # gets matched, undiluted by the modifier phrase still sitting in the sentence.
        classifier = IntentClassifier(FakeMatcher(
            actions={"cast an fireball": ("fireball", 0.9)},
            modifiers=["empowered"],
        ))
        _processed, events, _adjudication = classifier.classify("cast an empowered fireball")

        self.assertEqual(events[0]["event"], "turn_detected")
        action = events[0]["payload"]["clauses"][0]
        self.assertEqual(action["skill"], "fireball")
        self.assertEqual(action["modifier"], "empowered")

    def test_no_modifier_present_leaves_the_clause_without_a_modifier_key(self):
        classifier = IntentClassifier(FakeMatcher(
            actions={"cast a fireball": ("fireball", 0.9)},
            modifiers=["empowered"],
        ))
        _processed, events, _adjudication = classifier.classify("cast a fireball")

        action = events[0]["payload"]["clauses"][0]
        self.assertNotIn("modifier", action)

    def test_modifier_falls_back_to_the_unstripped_clause_when_nothing_else_is_left_to_match(self):
        # Stripping "power attack" out of "power attack the goblin" leaves only "the goblin" --
        # nothing there names a weapon skill at all. map_to_action must retry against the
        # original, unstripped clause (where FakeMatcher's own "power attack the goblin" entry
        # stands in for a real embedding/keyword match still finding "blades" via the word
        # "attack" inside the modifier's own name) rather than dropping the clause entirely.
        classifier = IntentClassifier(FakeMatcher(
            actions={"power attack the goblin": ("blades", 0.7)},
            modifiers=["power attack"],
        ))
        _processed, events, _adjudication = classifier.classify("power attack the goblin")

        self.assertEqual(events[0]["event"], "turn_detected")
        action = events[0]["payload"]["clauses"][0]
        self.assertEqual(action["skill"], "blades")
        self.assertEqual(action["modifier"], "power attack")

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
            "REST_KEYWORDS": REST_KEYWORDS,
            "MOUNT_KEYWORDS": MOUNT_KEYWORDS,
            "DISMOUNT_KEYWORDS": DISMOUNT_KEYWORDS,
            "HITCH_KEYWORDS": HITCH_KEYWORDS,
            "UNHITCH_KEYWORDS": UNHITCH_KEYWORDS,
            "LORE_KEYWORDS": LORE_KEYWORDS,
            "SCENE_QUERY_KEYWORDS": SCENE_QUERY_KEYWORDS,
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


class TestInputAdjudication(unittest.TestCase):
    """!
    @brief AdHoc_Generation.py's adjudicate_player_input -- the one enum-constrained call
        IntentClassifier makes for a line its rules can only guess at -- against a stubbed chat
        client. (The classifier's own use of it: TestIntentClassification's adjudication tests.)
    """

    @staticmethod
    def _answer(kind, **fields):
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "classify_input", "arguments": json.dumps({"kind": kind, "reason": "test", **fields}),
        }}]}}]}

    def test_the_model_picks_one_of_the_kinds_from_the_scene_it_is_given(self):
        from resolution.AdHoc_Generation import adjudicate_player_input
        sent = []
        stub = lambda api_url, messages, **kwargs: (sent.append(messages), self._answer("action"))[1]
        script_llm(self, stub)
        verdict, _reason = adjudicate_player_input(
            "Let's go down that cut-through.", ["Finn"], "Finn", "Finn points at an alley.")
        self.assertEqual(verdict, {"kind": "action", "game_action": None, "item": None, "tone": None})
        prompt = sent[0][-1]["content"]
        for expected in ("Finn", "talking to Finn", "Finn points at an alley.", "cut-through"):
            self.assertIn(expected, prompt)

    def test_an_off_list_answer_or_no_model_leaves_the_rules_to_decide(self):
        from resolution.AdHoc_Generation import adjudicate_player_input
        script_llm(self, lambda *a, **k: self._answer("attack"))
        self.assertEqual(adjudicate_player_input("x")[0], None)

        def unreachable(*args, **kwargs):
            raise ConnectionError
        script_llm(self, unreachable)
        self.assertEqual(adjudicate_player_input("x"), (None, "unavailable"))

    def test_an_action_can_name_an_item_action_and_its_item(self):
        from resolution.AdHoc_Generation import adjudicate_player_input
        def ask(answer):
            script_llm(self, lambda *a, **k: answer)
            return adjudicate_player_input("x")[0]

        self.assertEqual(ask(self._answer("action", game_action="buy", item=" the smoked peppers ")),
                         {"kind": "action", "game_action": "buy", "item": "the smoked peppers", "tone": None})
        # Only an action names one, only a real one other than "other", and only with its item.
        for answer in (self._answer("speech", game_action="buy", item="peppers"),
                       self._answer("action", game_action="other", item="peppers"),
                       self._answer("action", game_action="steal", item="peppers"),
                       self._answer("action", game_action="buy")):
            verdict = ask(answer)
            self.assertEqual((verdict["game_action"], verdict["item"]), (None, None))


    TONES = {"warm": "a kind act", "mocking": "a rude act"}

    def test_a_gesture_is_offered_only_when_the_setting_authors_tones_and_carries_its_tone(self):
        from resolution.AdHoc_Generation import adjudicate_player_input
        sent = []
        stub = lambda api_url, messages, **kwargs: (sent.append((messages, kwargs)), self._answer("gesture", tone="warm"))[1]
        script_llm(self, stub)

        verdict, _reason = adjudicate_player_input("hug her", ["Finn"], gesture_tones=self.TONES)

        self.assertEqual(verdict, {"kind": "gesture", "game_action": None, "item": None, "tone": "warm"})
        prompt = sent[0][0][-1]["content"]
        self.assertIn("- gesture:", prompt)
        self.assertIn("- mocking: a rude act", prompt)
        tool = sent[0][1]["tools"][0]["function"]["parameters"]["properties"]
        self.assertEqual(tool["tone"]["enum"], ["warm", "mocking"])
        self.assertIn("gesture", tool["kind"]["enum"])

        # No tones authored: the kind does not exist, so the same answer is off-list.
        verdict, reason = adjudicate_player_input("hug her", ["Finn"])
        self.assertEqual((verdict, reason), (None, "invalid_kind"))
        self.assertNotIn("- gesture:", sent[1][0][-1]["content"])

    def test_a_gesture_with_no_known_tone_leaves_the_rules_to_decide(self):
        from resolution.AdHoc_Generation import adjudicate_player_input
        for answer in (self._answer("gesture"), self._answer("gesture", tone="sad")):
            script_llm(self, lambda *a, **k: answer)
            self.assertEqual(adjudicate_player_input("x", gesture_tones=self.TONES), (None, "invalid_tone"))

    def test_only_a_gesture_keeps_a_tone(self):
        from resolution.AdHoc_Generation import adjudicate_player_input
        script_llm(self, lambda *a, **k: self._answer("speech", tone="warm"))
        verdict, _reason = adjudicate_player_input("x", gesture_tones=self.TONES)
        self.assertIsNone(verdict["tone"])


class TestEntityReference(unittest.TestCase):
    """!@brief resolution/Entity_Reference.py -- which entities a line of text literally names,
        crossed directly with plain dicts (no DMCore)."""

    ENTITIES = {
        "player": {"name": "Hero", "supertype": "character"},
        "sandpoint_townsfolk_2": {"name": "Fishmonger", "supertype": "creature"},
        "spice_merchant": {
            "name": "Orsin", "supertype": "creature", "aliases": ["spice merchant", "spice", "merchant"],
        },
        "chest": {"name": "Old Chest", "supertype": "object"},
    }
    KEYS = list(ENTITIES)

    def test_an_entity_is_named_by_key_display_name_or_alias(self):
        from resolution.Entity_Reference import first_named
        for text in ("greet sandpoint_townsfolk_2", "greet the fishmonger", "ask orsin"):
            with self.subTest(text=text):
                self.assertIsNotNone(first_named(text, self.ENTITIES, self.KEYS, exclude="player"))
        self.assertEqual(first_named("talk to the barkeep", self.ENTITIES, self.KEYS), None)

    def test_whole_words_only(self):
        from resolution.Entity_Reference import first_named
        self.assertIsNone(first_named("the fishmongers guild", self.ENTITIES, self.KEYS))

    def test_the_player_and_other_supertypes_can_be_excluded(self):
        from resolution.Entity_Reference import first_named
        self.assertIsNone(first_named("look at hero", self.ENTITIES, self.KEYS, exclude="player"))
        self.assertIsNone(first_named("open the old chest", self.ENTITIES, self.KEYS, supertype="creature"))

    def test_a_modifier_only_alias_can_be_skipped(self):
        from resolution.Entity_Reference import first_named
        self.assertEqual(first_named("kick the spice cart", self.ENTITIES, self.KEYS), "spice_merchant")
        self.assertIsNone(first_named("kick the spice cart", self.ENTITIES, self.KEYS, skip_modifier_aliases=True))
        self.assertEqual(
            first_named("pin the merchant", self.ENTITIES, self.KEYS, skip_modifier_aliases=True), "spice_merchant",
        )

    def test_reading_order_follows_the_text_not_the_declaration_order(self):
        from resolution.Entity_Reference import named_in_reading_order
        self.assertEqual(
            named_in_reading_order("hitch the old chest to the fishmonger", self.ENTITIES, self.KEYS),
            ["chest", "sandpoint_townsfolk_2"],
        )


class TestItemPhraseExtraction(unittest.TestCase):
    """!@brief Intent_Classification.py's extract_item_phrase -- the player's own words for an item,
        quoted by a "not here" notice. Unrecognized shapes return None (the notice names the item)."""

    def test_it_finds_the_words_after_the_item_verb(self):
        cases = {
            ("take the belt knife from the stall", "take"): "belt knife",
            ("i'll take a bag of figs", "take"): "bag of figs",
            ("(grabs the rope)", "take"): "rope",
            ("give her the potions", "give"): "potions",
            ("buy some smoked peppers for four coppers", "trade"): "smoked peppers",
            ("drink my healing draught", "use"): "healing draught",
            ("snatch a length of rope lying near the stall", "take"): "length of rope",
            ("grab the glimmering object without looking.", "take"): "glimmering object",
            ("take a bag of shining coins", "take"): "bag of shining coins",
        }
        for (clause, intent), expected in cases.items():
            with self.subTest(clause=clause):
                self.assertEqual(extract_item_phrase(clause, intent), expected)

    def test_no_verb_or_nothing_after_it_is_none(self):
        self.assertIsNone(extract_item_phrase("look around", "take"))
        self.assertIsNone(extract_item_phrase("take the", "take"))
        self.assertIsNone(extract_item_phrase("take the knife", "open"))


class TestAddressPhraseExtraction(unittest.TestCase):
    """!
    @brief Intent_Classification.py's extract_address_phrase -- the mechanical half of the
        promotion trigger. It only ever extracts; every phrasing it doesn't recognize returns
        None, which means no promotion, which means exactly today's behavior.
    """

    def test_it_finds_the_noun_after_a_dialogue_keyword(self):
        cases = {
            "ask the merchant what he is selling": "merchant",
            "greet the innkeeper": "innkeeper",
            "talk to garridan": "garridan",
            "speak with the grizzled fisherman": "grizzled fisherman",
            "chat with the old man by the fire": "old man",
            "tell the barkeep about the goblins": "barkeep",
            "say to the guard that i am leaving": "guard",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(extract_address_phrase(text), expected)

    def test_a_question_opener_addresses_no_one(self):
        for text in ("ask about the weather", "ask whats for sale", "ask where the road goes"):
            with self.subTest(text=text):
                self.assertIsNone(extract_address_phrase(text))

    def test_a_pronoun_addressee_is_not_a_name(self):
        for text in ("tell them to back off", "ask her about it", "greet everyone"):
            with self.subTest(text=text):
                self.assertIsNone(extract_address_phrase(text))

    def test_no_dialogue_keyword_and_nothing_after_one_both_yield_nothing(self):
        self.assertIsNone(extract_address_phrase("i swing my sword"))
        self.assertIsNone(extract_address_phrase("ask"))

    def test_the_held_out_battery_is_not_itself_the_word_lists(self):
        # Guards against the lists quietly growing to memorize these cases: none of the
        # extracted nouns may appear in any of the three word lists.
        for phrase in ("merchant", "innkeeper", "barkeep", "guard", "garridan"):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, ADDRESS_NON_ADDRESSEES)
                self.assertNotIn(phrase, ADDRESS_TERMINATORS)
                self.assertNotIn(phrase, ADDRESS_ARTICLES)


class TestSpokenWordsAreNotCommands(unittest.TestCase):
    """!
    @brief What the player says aloud must not trip the verbs that act on the world, and an item verb's
        object is what it acts on -- both found by the gooner playtest (a "rest" inside a whispered line
        ran a real rest, healing the character; "dropping my gaze to his spear tip" tried to drop a
        spear). FakeMatcher: no model load.
    """

    @staticmethod
    def _classify(line, **matcher_kwargs):
        classifier = IntentClassifier(FakeMatcher(**matcher_kwargs))
        classifier.set_present_entities([{"key": "Finn", "name": "Finn", "subtype": "human", "aliases": []}])
        return classifier.classify(line)[1]

    def test_spoken_quotes_are_blanked_to_the_same_length_and_scare_quotes_are_kept(self):
        from nlp.Intent_Classification import mask_speech_quotes
        text = 'eyes on his mouth. "you look like you need a rest." the "tide"'
        masked = mask_speech_quotes(text)
        self.assertEqual(len(masked), len(text))
        self.assertNotIn("rest", masked)
        self.assertIn('"tide"', masked)

    def test_a_verb_inside_a_spoken_quote_does_nothing_but_get_said(self):
        for line in (
            'I let my eyes drift over his chest. "You look like you need a rest."',
            'I nod at Finn. "Head to the docks, everyone."',
            'I whisper "Take the sword and drop it, then sleep." to Finn',
        ):
            with self.subTest(line=line):
                events = self._classify(line)
                self.assertEqual([event["event"] for event in events], ["dialogue_detected"])

    def test_the_same_verb_outside_a_quote_still_acts(self):
        events = self._classify("I make camp for the night")
        self.assertEqual(events[0]["payload"]["intent"], "rest")

    def test_rest_as_what_a_hand_does_is_not_resting(self):
        # Found by the gooner playtest: "my fingers rest on the buckle" ran three real rests in forty
        # turns -- the clock advanced and the party healed.
        for line in (
            "I let my hand rest on his forearm", "I rest my hands gently on his shoulders",
            "my hand ghosting up to rest on his forearm", "I sigh, letting the sword rest at my side",
        ):
            with self.subTest(line=line):
                self.assertIsNone(detect_item_intent(line))
        for line in ("I rest", "we rest for the night", "rest here a while", "make camp and rest on the hill"):
            with self.subTest(line=line):
                self.assertEqual(detect_item_intent(line), "rest")

    def test_an_item_verb_acts_on_its_direct_object_not_on_where_it_points(self):
        # "dropping my gaze to his spear tip" is about a gaze; the spear is only where it goes.
        events = self._classify(
            'I sigh loudly, dropping my gaze to his spear tip. "You are so cagey."',
            items={"dropping my gaze to his spear tip.": ("spear", 0.56)},
        )
        self.assertEqual([event["event"] for event in events], ["dialogue_detected"])

        self.assertFalse(_clause_names_item("drop my gaze to his spear tip", "spear"))
        self.assertTrue(_clause_names_item("give the sword to anne", "longsword"))
        self.assertTrue(_clause_names_item("give anne the sword", "longsword"))
        self.assertTrue(_clause_names_item("drop the spear", "spear"))


class TestAdjudicationRecord(unittest.TestCase):
    """!@brief Adjudication (Intent_Classification.py) -- the model is asked at most once per input."""

    def test_a_fresh_record_may_ask_and_a_recorded_one_may_not(self):
        record = Adjudication()
        self.assertTrue(record.may_ask())
        self.assertFalse(record.asked)

        record.record(None, "weak_turn")  # asked, and the model gave no usable answer

        self.assertFalse(record.may_ask())
        self.assertTrue(record.asked)
        self.assertEqual((record.verdict, record.trigger, record.action), (None, "weak_turn", None))

    def test_the_item_the_model_named_rides_with_the_verdict(self):
        record = Adjudication()
        record.record("action", "not_understood", ("buy", "the peppers"))
        self.assertEqual((record.verdict, record.action), ("action", ("buy", "the peppers")))


class TestInputStartSync(unittest.TestCase):
    """!
    @brief The classifier reads who is present before DMCore's own turn handlers run, so a roster
        change that nobody published a hook for must still be seen by the next input
        (DMCore._on_player_input_received is the sync point). FakeMatcher: no model load.
    """

    def test_a_roster_change_nobody_published_is_seen_by_the_next_input(self):
        bus = ValidatingEventBus()
        nlp = NLPCore(bus, FakeMatcher())
        core = DMCore(bus, scenario_name="debug", start_location="arena_grounds", setting="Fantasy")
        victim = next(name for name in core.scenario_entities if name != core.player_name)
        shown = core.entities[victim]["name"]
        bus.publish("user_input_submitted", "look around")
        before = nlp.classifier.present_names.count(shown)
        self.assertGreater(before, 0)

        core.scenario_entities.remove(victim)  # a mutation site that forgot its own publish
        bus.publish("user_input_submitted", "look around")

        self.assertEqual(nlp.classifier.present_names.count(shown), before - 1)


class TestConfirmationAnswers(unittest.TestCase):
    """!@brief NLPCore reads the input after a yes/no question as the answer (_answer_confirmation)
        -- exercised without loading NLPCore's models."""

    def _nlp(self):
        bus = ValidatingEventBus()
        answers = []
        bus.subscribe("confirmation_answered", answers.append)
        nlp = NLPCore(bus, FakeMatcher())
        nlp._awaiting_confirmation = True
        return nlp, answers

    def test_yes_and_no_are_consumed_and_anything_else_drops_the_question(self):
        for text, answer, consumed in (("Yes, do it.", "yes", True), ("no!", "no", True), ("where's the inn?", None, False)):
            with self.subTest(text=text):
                nlp, answers = self._nlp()
                self.assertEqual(nlp._answer_confirmation(text), consumed)
                self.assertEqual(answers[0]["answer"], answer)
                self.assertFalse(nlp._awaiting_confirmation)

    def test_nothing_pending_means_nothing_consumed(self):
        nlp, answers = self._nlp()
        nlp._awaiting_confirmation = False
        self.assertFalse(nlp._answer_confirmation("yes"))
        self.assertEqual(answers, [])


if __name__ == "__main__":
    unittest.main()
