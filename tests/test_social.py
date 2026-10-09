import shutil
import unittest
from unittest.mock import patch
import resolution.Combat_Resolution as Combat_Resolution
import resolution.Social_Resolution as Social_Resolution
from dm.DM_ActionOutcome import DamageEffect
from dm.DM_Core import DMCore, RECENT_NARRATION_CHARS, RECENT_NARRATION_TURNS
from dm.DM_Dialogue import CONVERSATION_IDLE_TURNS
from dm.DM_Improvisation import MAX_PROMOTED_PER_SCENE
from dm.DM_Social import TALK_ATTITUDE_DRIFT_CAP
from tests.event_contract import ValidatingEventBus
from persistence.slot import MemorySlotStore
from nlp.Intent_Classification import detect_item_intent
from tests.support import (
    DMTestCase,
)
# Module-level hooks start the shared patches (tests/support.py) for every test in this file.
from tests.support import setUpModule, tearDownModule  # noqa: F401


class TestSocialResolutionPure(unittest.TestCase):
    """!
    @brief Social_Resolution.py's own pure nudge_attitude_from_event/apply_capped_drift --
        direct, bare-dict tests, no DMCore instance needed (see this file's own module-shape
        precedent, Combat_Resolution.py/Inventory_Resolution.py). DM_Social.py's own
        thin-wrapper behavior is
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


class TestNpcDialogue(DMTestCase):
    # Rules/Fantasy/scenarios/debug.toml puts the player with a friendly NPC (its own local
    # innkeeper) instead of the default "arena" combat scenario.
    scenario_name = "debug"
    start_location = "tavern_floor"

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
    scenario_name = "debug"
    start_location = "tavern_floor"

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
        # though debug.toml never instances it -- just needs to be added to the live scene
        # for this one check.
        self.dm_core.scenario_entities.append("fire elemental")
        self.assertTrue(self.dm_core.is_hostile("fire elemental", self.dm_core.player_name))

        result = self._talk("i talk to the fire elemental")

        self.assertTrue(result["found"])
        self.assertEqual(result["target"], "fire elemental")

    def test_absent_or_dead_target_is_denied(self):
        dead_result = self._talk("i talk to the innkeeper")
        Combat_Resolution.apply_damage(self.dm_core.world, "innkeeper", 9999)

        result = self._talk("i talk to the innkeeper")

        self.assertTrue(dead_result["found"])  # sanity: alive, this would have worked before
        self.assertFalse(result["found"])
        # Said outright -- told only "isn't here", the narrator had a corpse gasping for air.
        self.assertEqual(result["reason"], "dead")

    def test_an_unnamed_remark_never_goes_to_the_dead(self):
        # Found by playtest: once the bystander being talked to died, every unnamed remark still
        # went to the corpse through _get_target_name's fallback.
        for name in list(self.dm_core.scenario_entities):
            if name != self.dm_core.player_name and not self.dm_core._is_party_member(name):
                Combat_Resolution.apply_damage(self.dm_core.world, name, 9999)
        self.assertIsNone(self.dm_core._default_listener())

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
        # a second, orphaned "gladstone_2" instance -- see debug.toml's own real-scenario
        # precedent for this same "never name the player" convention).
        self._load_ad_hoc_scenario([])

        result = self._talk("hello? is anyone there")

        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "no_one_here")

    def _add_second_speaker(self):
        # A second person in the tavern, placed after the innkeeper so the innkeeper stays
        # the default target -- the partner has to beat that default to be seen at all.
        self.dm_core.scenario_entities.append("thane")

    def test_unnamed_line_goes_to_the_conversation_partner_over_the_default(self):
        self._add_second_speaker()
        self._talk("i talk to thane")

        result = self._talk("what have you heard lately")

        self.assertEqual(result["target"], "thane")

    def test_naming_someone_else_switches_the_partner(self):
        self._add_second_speaker()
        self._talk("i talk to thane")
        self._talk("i ask the innkeeper about the road")

        self.assertEqual(self._talk("and the weather")["target"], "innkeeper")

    def test_partner_changes_are_published_for_nlp(self):
        updates = self._capture("conversation_partner_updated")
        self._talk("i talk to the innkeeper")
        self._talk("thanks")  # same partner again -- no second publish

        self.assertEqual([update["partner"]["key"] for update in updates], ["innkeeper"])

    def test_conversation_lapses_after_idle_turns(self):
        self._talk("i talk to the innkeeper")
        for _ in range(CONVERSATION_IDLE_TURNS - 1):
            self.dm_core._tick_conversation_partner()
        self.assertEqual(self.dm_core.conversation_partner["key"], "innkeeper")

        self.dm_core._tick_conversation_partner()
        self.assertIsNone(self.dm_core.conversation_partner)

    def test_a_real_turn_counts_against_the_conversation(self):
        self._talk("i talk to the innkeeper")
        self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "observation"}], "input": "look around"})
        self.assertEqual(self.dm_core.conversation_partner["idle_turns"], 1)

    def test_talking_again_resets_the_idle_count(self):
        self._talk("i talk to the innkeeper")
        self.dm_core._tick_conversation_partner()
        self._talk("one more thing")
        self.assertEqual(self.dm_core.conversation_partner["idle_turns"], 0)

    def test_dead_partner_ends_the_conversation(self):
        self._add_second_speaker()
        self._talk("i talk to thane")
        Combat_Resolution.apply_damage(self.dm_core.world, "thane", 9999)

        result = self._talk("are you all right")

        # Falls through to the default target, who then becomes the new partner.
        self.assertEqual(result["target"], "innkeeper")
        self.assertEqual(self.dm_core.conversation_partner["key"], "innkeeper")

    def test_changing_location_ends_the_conversation(self):
        self._talk("i talk to the innkeeper")
        self._load_ad_hoc_scenario([])
        self.assertIsNone(self.dm_core.conversation_partner)

    def test_conversation_partner_round_trips_through_save_and_load(self):
        slot_name = "test_conversation_partner_slot"
        self.addCleanup(shutil.rmtree, self.dm_core._save_slot_dir(slot_name), ignore_errors=True)
        updates = self._capture("conversation_partner_updated")
        self._talk("i talk to the innkeeper")
        self.dm_core._tick_conversation_partner()

        self.dm_core.save_game(slot_name)
        self.dm_core.load_game(slot_name)

        self.assertEqual(self.dm_core.conversation_partner, {"key": "innkeeper", "idle_turns": 1})
        # NLP has to hear about it too, or it can't route the next unmarked line.
        self.assertEqual(updates[-1]["partner"]["key"], "innkeeper")

    def test_speech_framing_passes_through_to_narration(self):
        self.dm_core._on_dialogue_detected({
            "input": "ask the innkeeper about the road", "speech_form": "reported",
            "utterance": "You ask the innkeeper about the road.",
        })
        result = self.dialogue_events[-1]
        self.assertEqual(result["speech_form"], "reported")
        self.assertEqual(result["utterance"], "You ask the innkeeper about the road.")

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
    scenario_name = "debug"
    start_location = "tavern_floor"

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

    def test_a_bilingual_player_who_never_chose_a_language_speaks_whichever_is_shared(self):
        # Found by playtest: under "only the first known language counts", a Varisian-first
        # default character heard gibberish from every NPC left on the "common" default.
        self.dm_core.entities[self.dm_core.player_name]["languages"] = ["common", "elvish"]
        self.dm_core.entities["innkeeper"]["languages"] = ["elvish"]

        result = self._talk("i talk to the innkeeper")

        self.assertNotIn("language_barrier", result)

    def test_an_explicitly_chosen_language_is_the_only_one_spoken(self):
        # "speak in common" so the elvish innkeeper can't follow is still a real choice.
        self.dm_core.entities[self.dm_core.player_name]["languages"] = ["common", "elvish"]
        self.dm_core.entities[self.dm_core.player_name]["current_language"] = "common"
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
    scenario_name = "debug"
    start_location = "tavern_floor"

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


class TestGestures(DMTestCase):
    """!
    @brief DM_Dialogue.py's _resolve_gesture_intent and intents/gesture.py -- a wordless act the
        adjudicator classified, resolved as a diceless item-kind clause. scenario "tavern", the
        same friendly-innkeeper fixture TestFreeformDialogue uses.
    """
    scenario_name = "debug"
    start_location = "tavern_floor"

    def setUp(self):
        super().setUp()
        self.item_events = self._capture("item_interaction_resolved")

    def _gesture(self, input_text, tone):
        self.dm_core._on_turn_detected({
            "clauses": [{"kind": "item", "intent": "gesture", "item_name": None, "phrase": None, "tone": tone}],
            "input": input_text,
        })
        return self.item_events[-1]

    def _drift(self, who="innkeeper"):
        return self.dm_core.entities[who].get("action_attitude_deltas", {}).get(self.dm_core.player_name)

    def test_a_gesture_nudges_the_named_target_through_its_tones_event(self):
        result = self._gesture("bow to the innkeeper", "respectful")

        self.assertTrue(result["found"])
        self.assertEqual((result["intent"], result["target"], result["tone"]), ("gesture", "innkeeper", "respectful"))
        self.assertEqual(self._drift(), [5, 0, 1])
        self.assertFalse(result["unwelcome"])
        self.assertTrue(result["persona"])
        self.assertTrue(result["attitude"])

    def test_the_target_becomes_the_conversation_partner(self):
        self._gesture("bow to the innkeeper", "respectful")
        self.assertEqual(self.dm_core.conversation_partner["key"], "innkeeper")

    def test_a_gesture_never_rolls_dice(self):
        rolled = []
        original = Combat_Resolution.roll_dice
        Combat_Resolution.roll_dice = lambda dice, pips: rolled.append((dice, pips)) or 3
        self.addCleanup(setattr, Combat_Resolution, "roll_dice", original)

        self._gesture("hug the innkeeper", "warm")

        self.assertEqual(rolled, [])

    def test_an_intimate_gesture_from_someone_who_dislikes_you_is_unwanted(self):
        self.dm_core.entities["innkeeper"]["attitudes"] = {"default": [-30, 0, 0]}

        result = self._gesture("kiss the innkeeper", "intimate")

        self.assertTrue(result["unwelcome"])
        self.assertEqual(self._drift(), [-12, -10, -6])

    def test_an_intimate_gesture_from_a_friend_lands_as_intended(self):
        self.dm_core.entities["innkeeper"]["attitudes"] = {"default": [40, 0, 0]}

        result = self._gesture("kiss the innkeeper", "intimate")

        self.assertFalse(result["unwelcome"])
        self.assertEqual(self._drift(), [8, 0, 10])

    def test_only_the_target_is_nudged(self):
        self._load_ad_hoc_scenario([
            {"name": "innkeeper", "band": 1}, {"name": "fire elemental", "band": 1},
        ])
        before = {name: dict(entity.get("action_attitude_deltas", {})) for name, entity in self.dm_core.entities.items()}

        result = self._gesture("hug the innkeeper", "warm")

        # A reloaded scene instances the innkeeper under a suffixed key; the result names it.
        self.assertIn(result["target"], self.dm_core.scenario_entities)
        self.assertTrue(self._drift(result["target"]))
        for name, entity in self.dm_core.entities.items():
            if name != result["target"]:
                self.assertEqual(entity.get("action_attitude_deltas", {}), before[name], name)

    def test_with_two_people_and_no_name_a_gesture_is_aimed_at_no_one(self):
        self._load_ad_hoc_scenario([
            {"name": "innkeeper", "band": 1}, {"name": "fire elemental", "band": 1},
        ])

        result = self._gesture("dance", "neutral")

        self.assertTrue(result["found"])
        self.assertIsNone(result["target"])
        self.assertIsNone(self._drift())

    def test_a_gendered_pronoun_means_the_one_person_it_can_only_mean(self):
        # Found by playtest: "pull him into a deep kiss" met no one with three people in the scene.
        self._load_ad_hoc_scenario([
            {"name": "innkeeper", "band": 1}, {"name": "fire elemental", "band": 1},
        ])

        her = self._gesture("kiss her", "intimate")
        self.dm_core._set_conversation_partner(None)  # the first gesture made her the partner
        him = self._gesture("kiss him", "intimate")

        self.assertEqual(self.dm_core.entities[her["target"]]["name"], self.dm_core.entities["innkeeper"]["name"])
        self.assertIsNone(him["target"])  # nobody present is male: "him" means nobody

    def test_the_only_other_person_present_is_the_target_without_being_named(self):
        result = self._gesture("curtsy", "respectful")
        self.assertEqual(result["target"], "innkeeper")

    def test_a_dead_target_is_declined_and_nothing_moves(self):
        self.dm_core.entities["innkeeper"]["hp"] = 0

        result = self._gesture("hug the innkeeper", "warm")

        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "dead")
        self.assertIsNone(self._drift())

    def test_a_tone_the_setting_does_not_author_narrates_without_moving_attitude(self):
        result = self._gesture("hug the innkeeper", "ecstatic")

        self.assertTrue(result["found"])
        self.assertIsNone(self._drift())

    def test_a_gesture_costs_a_turn_slot_like_any_item_interaction(self):
        penalties = []
        original = self.dm_core._resolve_roll
        self.dm_core._resolve_roll = lambda *args, **kwargs: (penalties.append((args, kwargs)), original(*args, **kwargs))[1]

        self.dm_core._on_turn_detected({
            "clauses": [
                {"kind": "item", "intent": "gesture", "item_name": None, "phrase": None, "tone": "respectful"},
                {"kind": "action", "skill": "charisma"},
            ],
            "input": "bow to the innkeeper and persuade her",
        })

        [(args, kwargs)] = penalties
        self.assertEqual(kwargs.get("dice_penalty", args[3] if len(args) > 3 else None), 1)  # two clauses, one die

    def test_narration_describes_the_real_reaction_and_never_a_made_up_one(self):
        from intents.gesture import narrate_gesture
        prompt = narrate_gesture(None, {
            "found": True, "tone": "intimate", "target": "innkeeper", "target_label": "Marta",
            "persona": "a stout innkeeper", "attitude": "She is wary of you.", "unwelcome": True,
            "input": "kiss the innkeeper",
        })
        for expected in ("Marta", "a stout innkeeper", "She is wary of you.", "unwelcome", "no roll"):
            self.assertIn(expected, prompt)
        self.assertIn("at no one", narrate_gesture(None, {"found": True, "tone": "neutral", "input": "dance"}))
        self.assertIn("dead", narrate_gesture(None, {"found": False, "reason": "dead", "target": "marta", "input": "hug marta"}))


class TestGestureTones(unittest.TestCase):
    """!@brief Social_Resolution.gesture_tones/gesture_event_name over plain rules dicts, and every
        shipped setting that authors tones."""

    RULES = {"attitude_event": [
        {"name": "combat_hit", "disposition": -20},
        {"name": "gesture_warm", "tone": "warm", "description": "a kind act", "disposition": 6,
         "unwelcome_below": -20, "unwelcome_event": "gesture_unwelcome"},
        {"name": "gesture_unwelcome", "disposition": -4},
        {"name": "gesture_plain", "tone": "plain"},
    ]}

    def test_a_tone_is_an_attitude_event_carrying_a_tone_field(self):
        self.assertEqual(Social_Resolution.gesture_tones(self.RULES), {"warm": "a kind act", "plain": "plain"})
        self.assertEqual(Social_Resolution.gesture_tones({}), {})

    def test_unwelcome_only_below_the_events_own_threshold(self):
        pick = lambda tone, disposition: Social_Resolution.gesture_event_name(self.RULES, tone, disposition)
        self.assertEqual(pick("warm", 0), ("gesture_warm", False))
        self.assertEqual(pick("warm", -20), ("gesture_warm", False))
        self.assertEqual(pick("warm", -21), ("gesture_unwelcome", True))
        self.assertEqual(pick("plain", -90), ("gesture_plain", False))
        self.assertEqual(pick("missing", 0), (None, False))

    def test_every_tone_and_unwelcome_event_a_setting_authors_exists(self):
        import tomllib
        for setting in ("Fantasy", "Pathfinder"):
            with open(f"Rules/{setting}/rules.toml", "rb") as handle:
                rules = tomllib.load(handle)
            names = {event["name"] for event in rules["attitude_event"]}
            tones = Social_Resolution.gesture_tones(rules)
            self.assertTrue({"warm", "intimate", "respectful", "mocking", "neutral"} <= set(tones), setting)
            for event in rules["attitude_event"]:
                if event.get("unwelcome_event"):
                    self.assertIn(event["unwelcome_event"], names, setting)
        # A setting that authors none (Zombie) keeps today's behavior: no gesture kind at all.
        with open("Rules/Zombie/rules.toml", "rb") as handle:
            self.assertEqual(Social_Resolution.gesture_tones(tomllib.load(handle)), {})


class TestHelpChannel(DMTestCase):
    """!
    @brief DM_Help.py's HelpMixin -- the reserved "ADaM" out-of-character help channel. Unlike
        TestFreeformDialogue above, there's no "not found"/"denied" case to cover -- ADaM isn't
        a scene entity, so _on_help_detected always resolves.
    """
    scenario_name = "debug"

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

    def test_reports_ground_items_hidden_ones_excluded(self):
        # The concrete gap docs/adam-improvisation.md's "Scene queries" names: before
        # _describe_ground_items existed, "what do I see" (even through ADaM) couldn't reflect
        # anything actually dropped in the room.
        self.dm_core._current_ground_items().append("dagger")
        result = self._ask()
        self.assertTrue(any(entry.startswith("dagger:") for entry in result["ground_items"]))

        Combat_Resolution.apply_condition(self.dm_core.world, "dagger", "hidden")
        result = self._ask()
        self.assertFalse(any(entry.startswith("dagger:") for entry in result["ground_items"]))


class TestHelpChannelExits(DMTestCase):
    """!
    @brief Multi-room-dungeon side of HelpMixin -- exits are only meaningful when self.rooms
        is populated (see _describe_available_exits), so this is exercised separately against
        "crypt" rather than folded into TestHelpChannel's own flat-scenario fixture.
    """
    scenario_name = "debug"
    start_location = "crypt"

    def test_lists_the_current_rooms_own_exits_with_friendly_destination_names(self):
        self.help_events = self._capture("help_resolved")
        self.dm_core._on_help_detected({"input": "adam, where can i go"})

        result = self.help_events[-1]
        # "entrance" (the starting room) has exactly one exit, "forward" to "hall_of_webs" --
        # whose own room name ("The Hall of Webs") should be reported, not the raw room key.
        self.assertEqual(result["exits"], [{"direction": "forward", "destination_name": "The Hall of Webs"}])


class TestSceneQueryChannel(DMTestCase):
    """!
    @brief DM_Help.py's own _on_scene_query_detected -- the free-standing, no-"adam"-needed
        counterpart to TestHelpChannel above. Shares the same live-ground-truth-snapshot shape
        (present roster, scene description, exits, ground items) but never the player's own
        mechanical state, and never runs ADaM's own removal/creature/edit mutation gates.
    """
    scenario_name = "debug"

    def setUp(self):
        super().setUp()
        self.scene_query_events = self._capture("scene_query_resolved")

    def _ask(self, input_text="what do i see"):
        self.dm_core._on_scene_query_detected({"input": input_text})
        return self.scene_query_events[-1]

    def test_reports_the_current_scene_and_present_entities(self):
        result = self._ask()

        self.assertTrue(result["scene_name"])
        self.assertTrue(result["scene_description"])
        self.assertEqual(result["present"], self.dm_core._describe_scenario_characters())
        self.assertEqual(set(result["present_entities"]), set(self.dm_core.scenario_entities))

    def test_reports_ground_items(self):
        self.dm_core._current_ground_items().append("dagger")
        result = self._ask()
        self.assertTrue(any(entry.startswith("dagger:") for entry in result["ground_items"]))

    def test_no_exits_in_a_flat_single_room_scenario(self):
        result = self._ask()
        self.assertEqual(result["exits"], [])

    def test_input_is_carried_through(self):
        result = self._ask("who is here")
        self.assertEqual(result["input"], "who is here")

    def test_never_reports_player_mechanical_state_or_mutates_the_scene(self):
        # Unlike help_resolved, this payload has no skills/abilities/equipped/inventory at all
        # -- and a phrase that would smell like a removal request through ADaM's own gates must
        # never actually remove anything here, since this channel never runs those checks.
        result = self._ask("destroy the wolf, what do i see")

        self.assertNotIn("skills", result)
        self.assertNotIn("abilities", result)
        self.assertNotIn("equipped", result)
        self.assertNotIn("inventory", result)
        self.assertNotIn("wolf", self.dm_core.removed_entities)


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
        [entity.attitudes] table at all (ex: debug.toml's wolf/debug.toml's bandit) is hostile
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


class TestDialoguePromotion(DMTestCase):
    """!
    @brief Promotion on reference (DM_Core.py's _promote_addressed_npc /
        DM_Improvisation.py's _attempt_dialogue_promotion) -- materializing someone the player
        addressed who isn't in the scene, and, far more often, correctly declining to.
    """
    start_location = "tavern_floor"

    def setUp(self):
        super().setUp()
        self.dialogue_events = self._capture("dialogue_resolved")

    def _fake_npc(self):
        return {"created": True, "entity": {
            "name": "Ferrin", "description": "A soot-streaked smith wiping her hands on an apron.",
            "supertype": "creature", "subtype": "npc", "max_hp": 8,
            "skills": {"observation": {"dice": 2, "pips": 0}},
            "attitudes": {"default": [10, 0, 0]}, "ad_hoc": True,
        }}

    def _talk(self, input_text, address_phrase=None, address_match=None):
        self.dm_core._on_dialogue_detected({
            "input": input_text, "address_phrase": address_phrase, "address_match": address_match,
            "sentiment": None, "sentiment_score": 0.0,
        })
        return self.dialogue_events[-1]

    def test_addressing_someone_absent_materializes_them_and_replies_in_character(self):
        with patch("dm.DM_Improvisation.generate_referenced_npc", return_value=self._fake_npc()):
            before = len(self.dialogue_events)
            result = self._talk("ask the blacksmith about repairs", "blacksmith")

        # Exactly one resolved event for the turn -- promotion feeds the ordinary found=True
        # path rather than producing a denial plus a second narration.
        self.assertEqual(len(self.dialogue_events) - before, 1)
        self.assertTrue(result["found"])
        self.assertEqual(result["target"], "Ferrin")
        self.assertTrue(result["persona"])
        self.assertIn("Ferrin", result["present_entities"])

    def test_a_promoted_npc_is_placed_as_a_harmless_bystander(self):
        with patch("dm.DM_Improvisation.generate_referenced_npc", return_value=self._fake_npc()):
            self._talk("ask the blacksmith about repairs", "blacksmith")

        entity = self.dm_core.entities["Ferrin"]
        self.assertFalse(self.dm_core.is_hostile("Ferrin", self.dm_core.player_name))
        self.assertNotIn("behavior", entity)
        self.assertTrue(entity["ad_hoc"])
        self.assertNotEqual(self.dm_core.current_target, "Ferrin")

    def test_naming_someone_present_never_promotes(self):
        with patch("dm.DM_Improvisation.generate_referenced_npc") as never_called:
            result = self._talk("ask the innkeeper about the road", "innkeeper")

        self.assertEqual(never_called.call_count, 0)
        self.assertEqual(result["target"], "innkeeper")

    def test_an_alias_match_never_promotes(self):
        self.dm_core.entities["innkeeper"]["aliases"] = ["the barkeep"]
        with patch("dm.DM_Improvisation.generate_referenced_npc") as never_called:
            result = self._talk("greet the barkeep", "barkeep")

        self.assertEqual(never_called.call_count, 0)
        self.assertEqual(result["target"], "innkeeper")

    def test_a_semantic_match_against_someone_present_never_promotes(self):
        with patch("dm.DM_Improvisation.generate_referenced_npc") as never_called:
            self._talk("greet the publican", "publican", address_match="innkeeper")

        self.assertEqual(never_called.call_count, 0)

    def test_addressing_no_one_in_particular_never_promotes(self):
        with patch("dm.DM_Improvisation.generate_referenced_npc") as never_called:
            self._talk("ask about the weather", None)

        self.assertEqual(never_called.call_count, 0)

    def test_promotion_is_refused_while_a_live_hostile_is_present(self):
        # Nobody wanders into a knife fight to sell you fruit.
        self.dm_core.entities["angry wolf"] = {
            "name": "angry wolf", "supertype": "creature", "max_hp": 10, "hp": 10,
        }
        self.dm_core._place_new_entity("angry wolf", self.dm_core.entities["angry wolf"], 1)
        self.dm_core.scenario_entities.append("angry wolf")

        with patch("dm.DM_Improvisation.generate_referenced_npc") as never_called:
            self.assertIsNone(self.dm_core._attempt_dialogue_promotion("blacksmith"))

        self.assertEqual(never_called.call_count, 0)

    def test_promotion_stops_once_the_scene_holds_its_budget_of_ad_hoc_entities(self):
        for index in range(MAX_PROMOTED_PER_SCENE):
            name = f"stranger_{index}"
            self.dm_core.entities[name] = {
                "name": name, "supertype": "creature", "max_hp": 5, "hp": 5,
                "attitudes": {"default": [0, 0, 0]}, "ad_hoc": True,
            }
            self.dm_core._place_new_entity(name, self.dm_core.entities[name], 1)
            self.dm_core.scenario_entities.append(name)

        with patch("dm.DM_Improvisation.generate_referenced_npc") as never_called:
            self.assertIsNone(self.dm_core._attempt_dialogue_promotion("blacksmith"))

        self.assertEqual(never_called.call_count, 0)

    def test_a_combat_capable_result_is_declined_rather_than_defanged(self):
        hostile = self._fake_npc()
        hostile["entity"]["behavior"] = [{"requirements": [], "action": "advance"}]
        warnings = self._capture("log_warning")

        with patch("dm.DM_Improvisation.generate_referenced_npc", return_value=hostile):
            self.assertIsNone(self.dm_core._attempt_dialogue_promotion("blacksmith"))

        self.assertNotIn("Ferrin", self.dm_core.entities)
        self.assertTrue([w for w in warnings if "combat-capable" in w])

    def test_a_declined_promotion_falls_through_to_the_grounded_denial(self):
        # The floor this whole feature rests on: when nobody is materialized, the turn resolves
        # exactly as it did before promotion existed.
        self.dm_core.scenario_entities = [self.dm_core.player_name]
        with patch("dm.DM_Improvisation.generate_referenced_npc", return_value={"created": False, "reason": "declined"}):
            result = self._talk("ask the blacksmith about repairs", "blacksmith")

        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "no_one_here")

    def test_a_promoted_npc_survives_save_and_reload(self):
        with patch("dm.DM_Improvisation.generate_referenced_npc", return_value=self._fake_npc()):
            self._talk("ask the blacksmith about repairs", "blacksmith")

        store = MemorySlotStore()
        self.dm_core.slot_store = store
        self.dm_core.save_game("promoted_slot")
        reloaded = DMCore(
            ValidatingEventBus(), scenario_name="debug", start_location="tavern_floor", setting="Fantasy", slot_store=store,
        )
        reloaded.load_game("promoted_slot")

        self.assertIn("Ferrin", reloaded.scenario_entities)
        self.assertEqual(reloaded.entities["Ferrin"]["description"], self._fake_npc()["entity"]["description"])


class TestRecentNarrationBuffer(DMTestCase):
    """!
    @brief DMCore.recent_narration -- grounding for a promoted NPC's flavor, and never a
        trigger for one (see _on_llm_response_ready).
    """

    def test_narration_is_remembered_and_capped(self):
        for index in range(RECENT_NARRATION_TURNS + 2):
            self.event_bus.publish("llm_response_ready", f"beat {index}")

        self.assertEqual(len(self.dm_core.recent_narration), RECENT_NARRATION_TURNS)
        self.assertEqual(self.dm_core.recent_narration[-1], f"beat {RECENT_NARRATION_TURNS + 1}")

    def test_engine_failure_notices_are_not_scene_narration(self):
        self.event_bus.publish("llm_response_ready", "System: Could not connect to the local LLM.")
        self.event_bus.publish("llm_response_ready", "   ")

        self.assertEqual(list(self.dm_core.recent_narration), [])

    def test_a_long_beat_is_truncated(self):
        self.event_bus.publish("llm_response_ready", "x" * (RECENT_NARRATION_CHARS + 500))

        self.assertEqual(len(self.dm_core.recent_narration[-1]), RECENT_NARRATION_CHARS)

    def test_the_buffer_round_trips_through_save_and_load(self):
        self.event_bus.publish("llm_response_ready", "A merchant argues outside the tavern.")

        store = MemorySlotStore()
        self.dm_core.slot_store = store
        self.dm_core.save_game("narration_slot")
        reloaded = DMCore(ValidatingEventBus(), scenario_name="debug", setting="Fantasy", slot_store=store)
        reloaded.load_game("narration_slot")

        self.assertEqual(list(reloaded.recent_narration), ["A merchant argues outside the tavern."])


if __name__ == "__main__":
    unittest.main()
