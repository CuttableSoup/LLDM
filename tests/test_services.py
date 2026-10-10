import unittest

import resolution.Service_Resolution as Service_Resolution
from dm.DM_Core import DMCore
from intents.service import ADULTS_ONLY, CONTENT_LEVELS, narrate_service
from resolution.Data_Validation import DataValidator
from resolution.World_Context import WorldContext
from tests.event_contract import ValidatingEventBus
import resolution.Combat_Resolution as Combat_Resolution
from tests.support import DMTestCase, FakeMatcher, ScriptedSession
# Module-level hooks start the shared patches (tests/support.py) for every test in this file.
from tests.support import setUpModule, tearDownModule  # noqa: F401


class TestServiceResolutionPure(unittest.TestCase):
    """!@brief Service_Resolution.py over plain dicts: which service a phrase names, what refuses it."""

    ENTITIES = {
        "garridan": {"name": "Garridan", "service": [
            {"name": "a room for the night", "aliases": ["a room", "lodging"], "price": 0.5},
        ]},
        "ysolde": {"name": "Ysolde Marrow", "aliases": ["ysolde"], "service": [
            {"name": "a night", "aliases": ["her company"], "price": 1.2, "min_disposition": 20},
        ]},
        "jorrick": {"name": "Jorrick Dane", "aliases": ["jorrick", "the mercenary"], "service": [
            {"name": "his sword", "price": 5, "joins_party": True},
        ]},
        "hero": {"name": "Hero", "currency": 3},
    }
    PROVIDERS = ["garridan", "ysolde", "jorrick"]

    def _match(self, phrase, input_text=""):
        provider, service = Service_Resolution.match_service(self.ENTITIES, self.PROVIDERS, phrase, input_text)
        return provider, service and service["name"]

    def test_a_service_is_found_by_its_name_or_an_alias_with_articles_ignored(self):
        self.assertEqual(self._match("a room for the night"), ("garridan", "a room for the night"))
        self.assertEqual(self._match("lodging"), ("garridan", "a room for the night"))
        self.assertEqual(self._match("her company"), ("ysolde", "a night"))

    def test_the_longer_match_wins_and_a_bare_part_never_beats_a_whole(self):
        # "night" alone is part of Garridan's "room night" and all of Ysolde's "night": hers wins.
        self.assertEqual(self._match("a night"), ("ysolde", "a night"))
        self.assertEqual(self._match("a room for the night"), ("garridan", "a room for the night"))

    def test_naming_a_provider_with_one_service_buys_it(self):
        self.assertEqual(self._match("", "hire jorrick"), ("jorrick", "his sword"))
        self.assertEqual(self._match("the mercenary", "hire the mercenary"), ("jorrick", "his sword"))

    def test_nothing_offered_matches_nothing(self):
        self.assertEqual(self._match("a glimmering object"), (None, None))
        self.assertEqual(self._match("", "buy some figs"), (None, None))

    def test_refusals_are_stated_reasons_and_the_order_is_fixed(self):
        night = self.ENTITIES["ysolde"]["service"][0]
        sword = self.ENTITIES["jorrick"]["service"][0]
        refuse = lambda provider, service, disposition=50: Service_Resolution.refusal(
            self.ENTITIES, "hero", provider, service, disposition,
        )
        self.assertEqual(refuse("ysolde", night, disposition=10), "too_cold")
        self.assertEqual(refuse("ysolde", night), "cant_afford" if night["price"] > 3 else None)
        self.assertEqual(refuse("jorrick", sword), "cant_afford")  # 5 > 3
        self.ENTITIES["hero"]["currency"] = 10
        self.addCleanup(self.ENTITIES["hero"].__setitem__, "currency", 3)
        self.assertIsNone(refuse("jorrick", sword))
        self.ENTITIES["jorrick"]["is_party"] = True
        self.addCleanup(self.ENTITIES["jorrick"].pop, "is_party", None)
        self.assertEqual(refuse("jorrick", sword), "already_joined")

    def test_the_offer_line_quotes_the_authored_price(self):
        lines = Service_Resolution.offer_lines(self.ENTITIES, "ysolde", lambda amount: f"{amount} gp")
        self.assertEqual(len(lines), 1)
        self.assertIn("a night: 1.2 gp", lines[0])
        self.assertIn("never another figure", lines[0])
        self.assertEqual(Service_Resolution.offer_lines(self.ENTITIES, "hero", str), [])


class TestServicePurchase(DMTestCase):
    """!@brief DM_Services.py -- buying a service through the improvisation seam, over the tavern fixture."""
    scenario_name = "debug"
    start_location = "tavern_floor"

    def setUp(self):
        super().setUp()
        self.item_events = self._capture("item_interaction_resolved")
        self.not_understood = self._capture("action_not_understood")
        self.keeper = next(
            name for name in self.dm_core.scenario_entities
            if self.dm_core.entities[name].get("name") == self.dm_core.entities["innkeeper"].get("name")
        )
        self.player = self.dm_core.entities[self.dm_core.player_name]
        self.player["currency"] = 10
        self.dm_core.entities[self.keeper]["service"] = [
            {"name": "a room for the night", "aliases": ["a room"], "price": 0.5, "overnight": True, "rest": True},
            {"name": "a private word", "aliases": ["a quiet word"], "price": 2, "min_disposition": 90},
            {"name": "company", "price": 1, "content": "sexual", "blocks": 1},
        ]

    def _buy(self, phrase, input_text=None):
        self.dm_core._on_improvisation_requested({
            "intent": "trade", "phrase": phrase, "item_phrase": phrase, "input": input_text or f"buy {phrase}",
        })
        return self.item_events[-1]

    def test_buying_a_service_takes_the_price_and_passes_the_night(self):
        before_block = self.dm_core.current_block
        keeper_before = self.dm_core.entities[self.keeper].get("currency", 0)

        result = self._buy("a room")

        self.assertEqual((result["intent"], result["found"], result["service"]), ("service", True, "a room for the night"))
        self.assertAlmostEqual(self.player["currency"], 9.5)
        self.assertAlmostEqual(self.dm_core.entities[self.keeper].get("currency", 0), keeper_before + 0.5)
        self.assertGreater(self.dm_core.current_block, before_block)
        self.assertIn("healed", result)
        self.assertTrue(result["price_text"])
        self.assertEqual(self.not_understood, [])

    def test_take_is_a_purchase_too_when_it_names_a_service(self):
        # "i'll take your services" opens on the take verb, which is how it reaches the seam.
        self.dm_core._on_improvisation_requested({
            "intent": "take", "phrase": "take a room", "item_phrase": "a room", "input": "i'll take a room",
        })
        self.assertEqual(self.item_events[-1]["intent"], "service")
        self.assertAlmostEqual(self.player["currency"], 9.5)

    def test_a_phrase_that_names_no_service_still_reaches_item_generation(self):
        called = []
        import dm.DM_Improvisation as DM_Improvisation
        original = DM_Improvisation.generate_ad_hoc_item
        DM_Improvisation.generate_ad_hoc_item = lambda *a, **k: (called.append(a), {"created": False, "reason": "declined"})[1]
        self.addCleanup(setattr, DM_Improvisation, "generate_ad_hoc_item", original)

        self.dm_core._on_improvisation_requested({
            "intent": "trade", "phrase": "a glimmering object", "item_phrase": "a glimmering object", "input": "buy a glimmering object",
        })

        self.assertEqual(len(called), 1)
        self.assertEqual(len(self.item_events), 0)
        self.assertAlmostEqual(self.player["currency"], 10)

    def test_a_player_who_cannot_pay_pays_nothing_and_is_told_why(self):
        self.player["currency"] = 0.1

        result = self._buy("a room")

        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "cant_afford")
        self.assertAlmostEqual(self.player["currency"], 0.1)

    def test_someone_who_feels_too_coldly_will_not_sell(self):
        self.dm_core.entities[self.keeper]["attitudes"] = {"default": [0, 0, 0]}

        result = self._buy("a quiet word")

        self.assertEqual((result["found"], result["reason"]), (False, "too_cold"))
        self.assertAlmostEqual(self.player["currency"], 10)

    def test_time_passing_services_are_refused_with_enemies_about(self):
        self._load_ad_hoc_scenario([{"name": "innkeeper", "band": 1}, {"name": "fire elemental", "band": 1}])
        keeper = next(
            name for name in self.dm_core.scenario_entities
            if self.dm_core.entities[name].get("name") == self.dm_core.entities["innkeeper"].get("name")
        )
        self.dm_core.entities[keeper]["service"] = [
            {"name": "a room for the night", "aliases": ["a room"], "price": 0.5, "overnight": True, "rest": True},
        ]
        self.player["currency"] = 10

        result = self._buy("a room")

        self.assertEqual((result["found"], result["reason"]), (False, "enemies_near"))
        self.assertAlmostEqual(self.player["currency"], 10)

    def test_a_service_beats_a_catalog_item_the_seller_does_not_hold(self):
        # Found by playtest: "buy the sword" matched a catalog longsword and was refused as "no sword here".
        self.dm_core.entities[self.keeper]["service"] = [{"name": "his sword", "aliases": ["a sword"], "price": 2}]

        self.dm_core._on_item_interaction_detected({
            "intent": "trade", "item_name": "longsword", "phrase": "sword", "input": "buy the sword",
        })

        self.assertEqual(self.item_events[-1]["intent"], "service")
        self.assertAlmostEqual(self.player["currency"], 8)

    def test_an_item_the_seller_really_holds_is_still_an_item(self):
        keeper = self.dm_core.entities[self.keeper]
        keeper["service"] = [{"name": "his sword", "aliases": ["a sword"], "price": 2}]
        keeper.setdefault("inventory", []).append("longsword")

        self.dm_core._on_item_interaction_detected({
            "intent": "trade", "item_name": "longsword", "phrase": "sword", "input": "buy the sword",
        })

        self.assertEqual(self.item_events[-1]["intent"], "trade")

    def test_the_provider_quotes_the_price_the_engine_will_charge(self):
        description = self.dm_core.describe_character(self.keeper)
        self.assertIn("a room for the night", description)
        self.assertIn("never another figure", description)

    def test_a_hire_joins_and_follows_the_party(self):
        entity = self.dm_core.entities[self.keeper]
        entity["service"] = [{"name": "his sword", "aliases": ["the sword"], "price": 5, "joins_party": True, "follow_offset": 0}]

        result = self._buy("his sword")

        self.assertTrue(result["found"])
        self.assertTrue(result["joined"])
        self.assertTrue(entity["is_party"] and entity["hired"])
        self.assertTrue(self.dm_core._is_party_member(self.keeper))
        # And a second hire is refused rather than charged twice.
        again = self._buy("his sword")
        self.assertEqual((again["found"], again["reason"]), (False, "already_joined"))
        self.assertAlmostEqual(self.player["currency"], 5)

    def test_a_hire_is_carried_into_the_next_scene_and_survives_a_save(self):
        import shutil
        entity = self.dm_core.entities[self.keeper]
        entity["service"] = [{"name": "his sword", "price": 1, "joins_party": True}]
        self._buy("his sword")
        self.dm_core.scenario_entities.remove(self.keeper)  # a new scene rebuilt without them

        self.dm_core._carry_mounts_into_scene()

        self.assertIn(self.keeper, self.dm_core.scenario_entities)

        slot = "test_hired_service_slot"
        self.addCleanup(shutil.rmtree, self.dm_core._save_slot_dir(slot), ignore_errors=True)
        self.dm_core.save_game(slot)
        self.dm_core.load_game(slot)
        reloaded = self.dm_core.entities[self.keeper]
        self.assertTrue(reloaded.get("hired") and reloaded.get("is_party"))
        self.assertIn(self.keeper, self.dm_core.scenario_entities)


class TestServiceNarration(unittest.TestCase):
    """!@brief intents/service.py -- what the narrator is told, at each content level."""

    SEXUAL = {
        "found": True, "service": "a night", "provider_label": "Ysolde", "price_text": "1 gold piece, 2 silver pieces",
        "content": "sexual", "input": "buy a night",
    }

    class _Llm:
        def __init__(self, level):
            self.content_level = level

    def test_fade_is_the_default_and_cuts_away(self):
        for llm in (None, self._Llm("fade"), self._Llm(None)):
            prompt = narrate_service(llm, self.SEXUAL)
            self.assertIn(CONTENT_LEVELS["fade"], prompt)
            self.assertNotIn(CONTENT_LEVELS["explicit"], prompt)

    def test_explicit_is_only_what_the_player_chose(self):
        prompt = narrate_service(self._Llm("explicit"), self.SEXUAL)
        self.assertIn(CONTENT_LEVELS["explicit"], prompt)
        self.assertNotIn(CONTENT_LEVELS["fade"], prompt)

    def test_adults_only_is_said_at_every_level_and_only_for_sexual_content(self):
        for level in ("fade", "explicit"):
            self.assertIn(ADULTS_ONLY, narrate_service(self._Llm(level), self.SEXUAL))
        plain = dict(self.SEXUAL, content=None, service="a room for the night")
        prompt = narrate_service(self._Llm("explicit"), plain)
        self.assertNotIn(ADULTS_ONLY, prompt)
        self.assertNotIn(CONTENT_LEVELS["explicit"], prompt)

    def test_the_price_is_the_engines_and_a_refusal_states_its_reason(self):
        self.assertIn("1 gold piece, 2 silver pieces", narrate_service(None, self.SEXUAL))
        refused = narrate_service(None, dict(self.SEXUAL, found=False, reason="cant_afford"))
        self.assertIn("cannot pay", refused)
        self.assertIn("1 gold piece, 2 silver pieces", refused)

    def test_the_content_level_is_a_two_way_choice(self):
        from llm.LLM_Core import LLMCore
        bus = ValidatingEventBus()
        llm = LLMCore(bus, rag_source_dir=".")
        self.assertEqual(llm.content_level, "fade")
        bus.publish("content_level_selected", {"level": "explicit"})
        self.assertEqual(llm.content_level, "explicit")
        llm.set_content_level("anything else")
        self.assertEqual(llm.content_level, "fade")


class TestServiceValidation(unittest.TestCase):
    """!@brief Data_Validation.py's [[entity.service]] shape checks."""

    def _problems(self, services):
        world = WorldContext(
            entities={"npc": {"name": "npc", "supertype": "creature", "service": services}},
            rules={"attitude_event": [{"name": "favor"}]}, skills={},
        )
        return [problem.message for problem in DataValidator(world, {}, {}).validate()]

    def test_a_well_formed_service_is_clean(self):
        self.assertEqual(self._problems([{
            "name": "a night", "price": 1.2, "aliases": ["her company"], "overnight": True, "rest": True,
            "content": "sexual", "attitude_event": "favor", "min_disposition": 20, "joins_party": False, "blocks": 0,
        }]), [])

    def test_each_bad_field_is_reported(self):
        messages = " | ".join(self._problems([{
            "price": -1, "aliases": "x", "rest": "yes", "content": "violent", "attitude_event": "nope", "blocks": 1.5,
        }]))
        for expected in ("needs a name", "price should not be negative", "aliases should be a list", "rest should be a bool",
                         "content should be one of", "attitude_event 'nope'", "blocks should be a whole number"):
            self.assertIn(expected, messages)

    def test_a_missing_price_is_reported(self):
        self.assertTrue(any("needs a numeric price" in m for m in self._problems([{"name": "x"}])))


class TestServiceSession(unittest.TestCase):
    """!@brief The whole offline pipeline: "buy a room for the night" typed, and a room bought."""

    def test_a_room_is_bought_through_the_whole_pipeline(self):
        session = ScriptedSession(self, FakeMatcher(), start_location="tavern_floor")
        keeper = next(
            name for name in session.core.scenario_entities
            if session.core.entities[name].get("name") == session.core.entities["innkeeper"].get("name")
        )
        session.core.entities[keeper]["service"] = [
            {"name": "a room for the night", "aliases": ["a room"], "price": 0.5, "overnight": True, "rest": True},
        ]
        session.core.entities[session.core.player_name]["currency"] = 5

        events = session.say("buy a room for the night")

        resolved = session.payloads(events, "item_interaction_resolved")
        self.assertEqual([r["intent"] for r in resolved], ["service"], session.names(events))
        self.assertTrue(resolved[0]["found"])
        self.assertAlmostEqual(session.core.entities[session.core.player_name]["currency"], 4.5)
        self.assertNotIn("action_not_understood", session.names(events))


class TestServiceConversationFlow(unittest.TestCase):
    """!
    @brief Found by the Ysolde/Jorrick playtest, where no purchase ever went through: the model that
        sorts an ambiguous line was never told what was for sale, "service" did not match "his
        services", a weak "blade" in a price question was narrated as an attack, and "keep the rest"
        ran a real rest. FakeMatcher: no model load.
    """

    @staticmethod
    def _classifier(**matcher_kwargs):
        from nlp.Intent_Classification import IntentClassifier
        matcher = FakeMatcher(**matcher_kwargs)
        classifier = IntentClassifier(matcher)
        classifier.set_present_entities([{
            "key": "jorrick", "name": "Jorrick Dane", "subtype": "human", "aliases": ["jorrick"],
            "services": ["his sword (5 gold pieces)"],
        }])
        return classifier, matcher

    def test_the_singular_matches_the_plural(self):
        entities = {"jorrick": {"name": "Jorrick Dane", "service": [{"name": "his sword", "aliases": ["his services"], "price": 5}]}}
        self.assertEqual(Service_Resolution.match_service(entities, ["jorrick"], "service", "")[0], "jorrick")
        self.assertEqual(Service_Resolution.match_service(entities, ["jorrick"], "his service", "")[0], "jorrick")

    def test_the_model_is_told_what_is_for_sale(self):
        classifier, matcher = self._classifier()
        matcher.adjudications = {"done. your services for the night, then.": {"kind": "action", "game_action": "buy", "item": "his sword"}}

        classifier.classify("Done. Your services for the night, then.")

        self.assertEqual(matcher.last_offers, ["Jorrick Dane: his sword (5 gold pieces)"])

    def test_an_item_verb_that_names_nothing_real_is_put_to_the_model_when_someone_sells(self):
        # "Deal. Start when I give the word." opens on a give verb. With a seller present it is as
        # likely an acceptance as an item, and only the model, told what is for sale, can say which.
        line = "Deal. Start when I give the word."
        classifier, matcher = self._classifier()

        matcher.adjudications = {"deal. start when i give the word.": "speech"}
        _processed, events, adjudication = classifier.classify(line)
        self.assertEqual([event["event"] for event in events], ["dialogue_detected"])
        self.assertEqual(adjudication.trigger, "offered")

        classifier, matcher = self._classifier()
        matcher.adjudications = {
            "deal. start when i give the word.": {"kind": "action", "game_action": "buy", "item": "his sword"},
        }
        _processed, events, _adjudication = classifier.classify(line)
        self.assertEqual(
            [(event["event"], event["payload"]["intent"], event["payload"]["phrase"]) for event in events],
            [("improvisation_requested", "trade", "his sword")],
        )

    def test_with_nobody_selling_an_unmatched_verb_is_never_put_to_the_model(self):
        from nlp.Intent_Classification import IntentClassifier
        matcher = FakeMatcher()
        classifier = IntentClassifier(matcher)
        classifier.set_present_entities([{"key": "finn", "name": "Finn", "subtype": "human", "aliases": []}])
        matcher.adjudications = {"give the word": "speech"}

        _processed, events, adjudication = classifier.classify("give the word")

        self.assertEqual(events[0]["event"], "improvisation_requested")
        self.assertFalse(adjudication.asked)

    def test_a_weak_blade_in_a_price_question_is_talk_not_an_attack(self):
        classifier, matcher = self._classifier(actions={"reliable blade": ("blades", 0.55)})
        matcher.adjudications = {
            "jorrick. i need a clean, reliable blade and someone skilled with it.": "speech",
        }

        _processed, events, adjudication = classifier.classify(
            "Jorrick. I need a clean, reliable blade and someone skilled with it. What are your prices?"
        )

        self.assertEqual([event["event"] for event in events], ["dialogue_detected"])
        self.assertEqual((adjudication.verdict, adjudication.trigger), ("speech", "weak_split"))

    def test_a_stage_direction_that_opens_the_line_masks_the_talk_after_it(self):
        from nlp.Intent_Classification import mask_talk, stage_directions
        line = "(patting the pocket of my coat, producing a small pouch) here. keep the rest."
        self.assertEqual([c for _s, _e, c in stage_directions(line)], ["patting the pocket of my coat, producing a small pouch"])
        self.assertNotIn("rest", mask_talk(line))
        classifier, _matcher = self._classifier()
        _processed, events, _adjudication = classifier.classify(
            "(Patting the pocket of my coat, producing a small pouch) Here. Keep the rest."
        )
        self.assertNotIn("rest", [event["payload"].get("intent") for event in events])
        # A short aside at the end of an ordinary command is still not a stage direction.
        self.assertEqual(stage_directions("walk to the docks (it's far)"), [])


class TestCoachToMagnimar(DMTestCase):
    """!
    @brief The shipped Sandpoint coach (Hesper Cobb, `travel_to = "magnimar"`): a paid ride that is
        the ordinary grid trip underneath, so the road's own rules -- a faster pace, an ambush
        pausing it, a save mid-journey -- are checked against the real lost_coast data.
    """
    scenario_name = "lost_coast"
    setting = "Pathfinder"  # lost_coast is Golarion-sourced content, kept isolated under Rules/Pathfinder/
    start_location = "coach_stand"

    def setUp(self):
        super().setUp()
        self.item_events = self._capture("item_interaction_resolved")
        self.driver = "Hesper Cobb"
        self.player = self.dm_core.entities[self.dm_core.player_name]
        self.player["currency"] = 10
        self._stub_encounter_roll("nothing")

    def _stub_encounter_roll(self, result):
        import dm.DM_Encounters as DM_Encounters
        original = DM_Encounters.resolve_varied_value
        DM_Encounters.resolve_varied_value = lambda choices: result
        self.addCleanup(setattr, DM_Encounters, "resolve_varied_value", original)

    def _buy(self, phrase="a seat on the coach to magnimar", input_text=None):
        self.dm_core._on_improvisation_requested({
            "intent": "trade", "phrase": phrase, "item_phrase": phrase, "input": input_text or f"buy {phrase}",
        })
        return self.item_events[-1]

    def test_the_driver_is_at_the_stand_and_the_data_is_clean(self):
        self.assertIn(self.driver, self.dm_core.scenario_entities)
        world = WorldContext(
            entities=self.dm_core.entities, rules=self.dm_core.rules, skills=self.dm_core.skills,
        )
        problems = [p.message for p in DataValidator(world, self.dm_core.entity_templates, self.dm_core.locations).validate()]
        self.assertEqual([m for m in problems if "service" in m or "travel_to" in m or "coach" in m], [])

    def test_the_stand_is_reachable_from_the_town_hub(self):
        self.dm_core._enter_location("sandpoint")
        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "go to the coach stand",
        })
        self.assertEqual(self.dm_core.current_location_key, "coach_stand")
        self.assertIn(self.driver, self.dm_core.scenario_entities)

    def test_the_seat_takes_the_fare_and_arrives_in_magnimar_in_two_blocks(self):
        before_block = self.dm_core.current_block
        driver_before = self.dm_core.entities[self.driver].get("currency", 0)

        result = self._buy()

        self.assertEqual((result["intent"], result["found"], result["service"]), ("service", True, "a seat on the coach to Magnimar"))
        self.assertTrue(result["travelled"])
        self.assertFalse(result["interrupted"])
        self.assertEqual(self.dm_core.current_location_key, "magnimar")
        self.assertEqual(result["location_name"], "Magnimar")
        self.assertEqual(result["polity"], "Varisia")
        # 60 miles at the coach's 30 a block, not the party's own 24: two blocks, not three.
        self.assertEqual(result["blocks_spent"], 2)
        self.assertEqual(self.dm_core.current_block - before_block, 2)
        self.assertAlmostEqual(self.player["currency"], 10 - 1.8)
        self.assertAlmostEqual(self.dm_core.entities[self.driver].get("currency", 0), driver_before + 1.8)
        self.assertIsNone(self.dm_core.pending_downtime)
        # The driver stays where the coach stand is.
        self.assertNotIn(self.driver, self.dm_core.scenario_entities)

    def test_a_ride_arrives_before_walking_would(self):
        self._buy()
        coach_blocks = self.item_events[-1]["blocks_spent"]
        self.dm_core._enter_location("sandpoint")
        self.dm_core._on_item_interaction_detected({"intent": "travel", "item_name": None, "input": "i travel to magnimar"})
        self.assertEqual(self.item_events[-1]["blocks_spent"], 3)
        self.assertLess(coach_blocks, 3)

    def test_other_ways_of_asking_for_the_ride_find_it(self):
        for phrase in ("a ride to magnimar", "passage to magnimar", "the coach", "a seat"):
            with self.subTest(phrase=phrase):
                self.dm_core._enter_location("coach_stand")
                self.player["currency"] = 10
                result = self._buy(phrase, f"i'd like {phrase}")
                self.assertEqual(result["intent"], "service")
                self.assertTrue(result["found"], result)
                self.assertEqual(self.dm_core.current_location_key, "magnimar")

    def test_the_driver_quotes_the_fare_the_engine_charges(self):
        description = self.dm_core.describe_character(self.driver)
        self.assertIn("a seat on the coach to Magnimar", description)
        self.assertIn(self.dm_core.format_currency(1.8), description)

    def test_a_player_who_cannot_pay_goes_nowhere_and_pays_nothing(self):
        self.player["currency"] = 1.0
        before_block = self.dm_core.current_block

        result = self._buy()

        self.assertEqual((result["found"], result["reason"]), (False, "cant_afford"))
        self.assertEqual(self.dm_core.current_location_key, "coach_stand")
        self.assertEqual(self.dm_core.current_block, before_block)
        self.assertAlmostEqual(self.player["currency"], 1.0)

    def test_enemies_nearby_refuse_the_ride(self):
        self.dm_core.entities[self.driver]["service"][0]["price"] = 1
        self._load_ad_hoc_scenario([{"name": "Hesper Cobb", "band": 1}, {"name": "goblin", "band": 1}])
        driver = next(
            name for name in self.dm_core.scenario_entities
            if self.dm_core.entities[name].get("name") == "Hesper Cobb"
        )
        self.dm_core.entities[driver]["service"] = [
            {"name": "a ride", "price": 1, "travel_to": "magnimar"},
        ]
        self.dm_core.locations["magnimar"] = {"key": "magnimar", "grid": {"x": 210, "y": 0}}
        self.player["currency"] = 10

        result = self._buy("a ride")

        self.assertEqual((result["found"], result["reason"]), (False, "enemies_near"))
        self.assertAlmostEqual(self.player["currency"], 10)

    def test_a_ride_to_where_you_already_are_is_refused(self):
        self.dm_core.entities[self.driver]["service"][0]["travel_to"] = "sandpoint"

        result = self._buy()

        self.assertEqual((result["found"], result["reason"]), (False, "already_there"))
        self.assertAlmostEqual(self.player["currency"], 10)

    def test_a_destination_with_no_grid_point_is_refused(self):
        self.dm_core.entities[self.driver]["service"][0]["travel_to"] = "garrison"

        result = self._buy()

        self.assertEqual((result["found"], result["reason"]), (False, "no_route"))
        self.assertAlmostEqual(self.player["currency"], 10)

    def test_a_second_ride_is_refused_while_a_journey_is_paused(self):
        self._stub_encounter_roll("goblin")
        self._buy()
        self.assertIsNotNone(self.dm_core.pending_downtime)
        self.player["currency"] = 10

        # With the ambusher still standing there, the fight comes first...
        beset = self._buy()
        self.assertEqual((beset["found"], beset["reason"]), (False, "enemies_near"))

        # ...and once it is dead but the trip has not been resumed, the unfinished trip is the reason.
        Combat_Resolution.apply_damage(self.dm_core.world, "goblin", 999)
        again = self._buy()

        self.assertEqual((again["found"], again["reason"]), (False, "downtime_interrupted"))
        self.assertAlmostEqual(self.player["currency"], 10)

    def test_an_ambush_on_the_road_pauses_the_ride_and_it_resumes_at_the_coachs_pace(self):
        self._stub_encounter_roll("goblin")
        before_block = self.dm_core.current_block

        result = self._buy()

        # Paid, set out, and caught on the road: the purchase is narrated as interrupted.
        self.assertTrue(result["found"])
        self.assertTrue(result["interrupted"])
        self.assertFalse(result["travelled"])
        self.assertAlmostEqual(self.player["currency"], 10 - 1.8)
        self.assertEqual(self.dm_core.pending_downtime["kind"], "travel")
        self.assertEqual(self.dm_core.pending_downtime["speed"], 30)
        self.assertIn("goblin", self.dm_core.scenario_entities)

        self._stub_encounter_roll("nothing")
        Combat_Resolution.apply_damage(self.dm_core.world, "goblin", 999)
        self.dm_core._resolve_combat_round({"actions": []})

        self.assertIsNone(self.dm_core.pending_downtime)
        self.assertEqual(self.dm_core.current_location_key, "magnimar")
        self.assertEqual(self.dm_core.current_block - before_block, 2)
        arrival = self.item_events[-1]
        self.assertEqual((arrival["intent"], arrival["found"], arrival["location_name"]), ("travel", True, "Magnimar"))

    def test_a_paused_ride_keeps_its_pace_through_a_save(self):
        import shutil
        self._stub_encounter_roll("goblin")
        self._buy()
        slot = "test_coach_pending_slot"
        self.addCleanup(shutil.rmtree, self.dm_core._save_slot_dir(slot), ignore_errors=True)
        self.dm_core.save_game(slot)

        self.dm_core.load_game(slot)

        self.assertEqual(self.dm_core.pending_downtime["speed"], 30)
        self.assertEqual(self.dm_core.pending_downtime["destination_key"], "magnimar")

    def test_a_hired_sword_rides_along_and_is_still_in_the_party_on_arrival(self):
        sword = "Jorrick Dane"
        self.dm_core._enter_location("white_deer")
        self.assertIn(sword, self.dm_core.scenario_entities)
        self.player["currency"] = 20
        self.dm_core._on_improvisation_requested({
            "intent": "trade", "phrase": "his sword", "item_phrase": "his sword", "input": "hire jorrick",
        })
        self.assertTrue(self.dm_core.entities[sword]["hired"])
        self.dm_core._enter_location("coach_stand")
        self.assertIn(sword, self.dm_core.scenario_entities)

        result = self._buy()

        self.assertTrue(result["found"], result)
        self.assertEqual(self.dm_core.current_location_key, "magnimar")
        self.assertIn(sword, self.dm_core.scenario_entities)
        self.assertTrue(self.dm_core._is_party_member(sword))

    def test_a_return_coach_runs_the_road_the_other_way(self):
        self._buy()
        self.assertEqual(self.dm_core.current_location_key, "magnimar")
        self.assertIn("Ottilie Marsh", self.dm_core.scenario_entities)
        before_block = self.dm_core.current_block

        result = self._buy("a seat on the coach to sandpoint")

        self.assertTrue(result["found"], result)
        self.assertEqual(self.dm_core.current_location_key, "sandpoint")
        self.assertEqual(result["blocks_spent"], 2)
        self.assertEqual(self.dm_core.current_block - before_block, 2)
        self.assertAlmostEqual(self.player["currency"], 10 - 1.8 * 2)

    def test_the_whole_pipeline_from_a_typed_line(self):
        session = ScriptedSession(self, FakeMatcher(), scenario_name="lost_coast", start_location="coach_stand", setting="Pathfinder")
        session.core.entities[session.core.player_name]["currency"] = 10

        events = session.say("buy a seat on the coach to magnimar")

        resolved = session.payloads(events, "item_interaction_resolved")
        self.assertEqual([r["intent"] for r in resolved], ["service"], session.names(events))
        self.assertTrue(resolved[0]["found"])
        self.assertEqual(session.core.current_location_key, "magnimar")
        self.assertNotIn("action_not_understood", session.names(events))


class TestCoachNarration(unittest.TestCase):
    """!@brief intents/service.py -- the ride is told as the engine resolved it."""

    BASE = {
        "found": True, "service": "a seat on the coach to Magnimar", "provider_label": "Hesper Cobb",
        "price_text": "1 gold piece, 8 silver pieces", "input": "buy a ride",
    }

    def test_an_arrival_names_the_destination_and_the_time_on_the_road(self):
        prompt = narrate_service(None, dict(
            self.BASE, travelled=True, location_name="Magnimar", location_description="A sprawling port city.",
            blocks_spent=2, time={"day": 1},
        ))
        self.assertIn("Magnimar", prompt)
        self.assertIn("2 block(s)", prompt)
        self.assertNotIn("Time passes", prompt)
        self.assertIn("1 gold piece, 8 silver pieces", prompt)

    def test_an_interrupted_ride_does_not_claim_an_arrival(self):
        prompt = narrate_service(None, dict(self.BASE, travelled=False, interrupted=True))
        self.assertIn("cut short", prompt)
        self.assertNotIn("carries the player to", prompt)

    def test_each_route_refusal_is_stated(self):
        for reason, words in (("no_route", "cannot take the player"), ("already_there", "already there"),
                              ("downtime_interrupted", "unfinished")):
            with self.subTest(reason=reason):
                prompt = narrate_service(None, dict(self.BASE, found=False, reason=reason))
                self.assertIn(words, prompt)


class TestTravelServiceValidation(unittest.TestCase):
    """!@brief Data_Validation.py's travel_to / travel_speed checks."""

    LOCATIONS = {"magnimar": {"grid": {"x": 210, "y": 0}}, "cellar": {}}

    def _problems(self, **fields):
        world = WorldContext(
            entities={"npc": {"name": "npc", "supertype": "creature", "service": [dict({"name": "a ride", "price": 1}, **fields)]}},
            rules={}, skills={},
        )
        return " | ".join(p.message for p in DataValidator(world, {}, self.LOCATIONS).validate())

    def test_a_gridded_destination_and_a_positive_speed_are_clean(self):
        self.assertEqual(self._problems(travel_to="magnimar", travel_speed=30), "")

    def test_an_unknown_or_ungridded_destination_is_reported(self):
        self.assertIn("travel_to 'nowhere'", self._problems(travel_to="nowhere"))
        self.assertIn("travel_to 'cellar'", self._problems(travel_to="cellar"))

    def test_a_bad_speed_is_reported(self):
        for speed in (0, -5, "fast", True):
            with self.subTest(speed=speed):
                self.assertIn("travel_speed should be a positive", self._problems(travel_to="magnimar", travel_speed=speed))

    def test_a_speed_without_a_destination_is_reported(self):
        self.assertIn("no effect without travel_to", self._problems(travel_speed=30))


if __name__ == "__main__":
    unittest.main()
