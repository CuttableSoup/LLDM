import json
import unittest
from unittest.mock import patch
import resolution.Combat_Resolution as Combat_Resolution
import resolution.World_Map as World_Map
import resolution.Law_Resolution as Law_Resolution
from resolution.Program_Interpreter import run_program
from dm.DM_Core import DMCore
from resolution.Law_Enforcement import ARREST_CHOICES, STALL_LIMIT, LawEnforcement, LawWorld
from tests.event_contract import ValidatingEventBus
from persistence.slot import MemorySlotStore
from nlp.NLP_Core import NLPCore
import resolution.Combat_Actions as Combat_Actions
from tests.support import (
    DMTestCase,
    FakeMatcher,
    script_llm,
)
# Module-level hooks start the shared patches (tests/support.py) for every test in this file.
from tests.support import setUpModule, tearDownModule  # noqa: F401


class TestLawResolution(unittest.TestCase):
    """!@brief Law_Resolution.py's pure helpers (docs/law.md)."""

    def test_a_law_matches_by_kind_name_or_tag_and_matchless_laws_cover_everything(self):
        undead_ban = {"crime": "banned_presence", "match": {"subtypes": ["undead"]}}
        outlaw_ban = {"crime": "banned_presence", "match": {"names": ["red mask"]}}
        self.assertTrue(Law_Resolution.law_matches(undead_ban, {"subtype": "undead"}))
        self.assertFalse(Law_Resolution.law_matches(undead_ban, {"subtype": "humanoid"}))
        self.assertFalse(Law_Resolution.law_matches(undead_ban, None))
        self.assertTrue(Law_Resolution.law_matches(outlaw_ban, {"name": "red mask"}))
        self.assertTrue(Law_Resolution.law_matches({"crime": "theft"}, None))
        self.assertEqual(
            Law_Resolution.matching_laws([undead_ban, outlaw_ban, {"crime": "theft"}], "banned_presence", {"subtype": "undead"}),
            [undead_ban],
        )

    def test_a_location_law_replaces_the_polity_law_it_shadows_and_adds_the_rest(self):
        polity = [{"crime": "theft", "fine": 5}, {"crime": "assault", "fine": 10}]
        location = [{"crime": "theft", "fine": 50}, {"crime": "banned_ability", "fine": 1, "match": {"supertypes": ["spell"]}}]
        merged = Law_Resolution.merge_laws(polity, location)
        self.assertEqual([(law["crime"], law["fine"]) for law in merged], [("assault", 10), ("theft", 50), ("banned_ability", 1)])

    def test_fame_and_infamy_add_up_rather_than_cancel(self):
        signed, magnitude = Law_Resolution.effective_acclaim({"acclaim": 5}, {"acclaim": -5})
        self.assertEqual((signed, magnitude), (0, 10))
        self.assertEqual(Law_Resolution.effective_acclaim({}, None), (0, 0))

    def test_recognition_bands(self):
        bands = [{"min_acclaim": 1, "tier": "difficult"}, {"min_acclaim": 10, "tier": "automatic"}]
        tiers = [{"name": "difficult", "difficulty": 15}]
        self.assertIsNone(Law_Resolution.recognition_difficulty(0, bands, tiers))
        self.assertEqual(Law_Resolution.recognition_difficulty(3, bands, tiers), 15)
        self.assertEqual(Law_Resolution.recognition_difficulty(12, bands, tiers), 0)

    def test_a_murder_supersedes_the_assault_on_the_same_victim(self):
        records = {}
        Law_Resolution.file_report(records, "Crown", "gladstone", {"fine": 10, "acclaim": -2}, {"crime": "assault", "victim": "thane"})
        record = Law_Resolution.file_report(records, "Crown", "gladstone", {"fine": 100, "acclaim": -6}, {"crime": "murder", "victim": "thane"})
        self.assertEqual((record["bounty"], record["acclaim"]), (100, -6))
        self.assertTrue(record["crimes"][0]["superseded"])


class TestLaw(DMTestCase):
    """!
    @brief DM_Law.py end to end, in debug.toml's general store put under Fantasy's "Test Crown"
        polity (a test-only fixture carrying every crime kind). The shopkeeper is the usual
        witness and victim.
    """
    start_location = "general_store"

    def setUp(self):
        super().setUp()
        self.dm_core.locations[self.dm_core.current_location_key]["polity"] = "Test Crown"
        self.dm_core.entities["gladstone"]["currency"] = 10

    def _steal(self):
        self.dm_core._on_item_interaction_detected({"intent": "take", "item_name": "dagger", "input": "steal the dagger"})

    def _add_person(self, name, **fields):
        entity = {
            "name": name, "supertype": "creature", "subtype": "humanoid", "max_hp": 10,
            "languages": ["common"], "attitudes": {"default": [20, 0, 0]}, **fields,
        }
        self.dm_core.entities[name] = entity
        self.dm_core._place_new_entity(name, entity, 1)
        self.dm_core.scenario_entities.append(name)

    def _record(self, identity="gladstone"):
        return self.dm_core.legal_records.get("Test Crown", {}).get(identity)

    def test_a_witnessed_theft_is_known_at_once_and_filed_at_the_next_block(self):
        self._steal()
        [seen] = self.dm_core.entities["shopkeeper"]["known_crimes"]
        self.assertEqual((seen["crime"], seen["offender"], seen["victim"]), ("theft", "gladstone", "shopkeeper"))
        self.assertIsNone(self._record())

        self.dm_core.advance_blocks(1)
        self.assertEqual((self._record()["bounty"], self._record()["acclaim"]), (5, -1))

    def test_silencing_every_witness_before_time_passes_keeps_the_record_clean(self):
        self._steal()
        Combat_Resolution.apply_damage(self.dm_core.world, "shopkeeper", 1000)
        self.dm_core.advance_blocks(1)
        self.assertIsNone(self._record())

    def test_an_enforcer_files_what_it_sees_immediately(self):
        self.dm_core.entities["shopkeeper"]["tags"] = ["law_enforcer"]
        self._steal()
        self.assertEqual(self._record()["bounty"], 5)
        self.assertIn("is wanted in Test Crown", " ".join(self.dm_core.legal_facts_for("shopkeeper")))

    def test_no_polity_means_no_law(self):
        del self.dm_core.locations[self.dm_core.current_location_key]["polity"]
        self._steal()
        self.dm_core.advance_blocks(1)
        self.assertNotIn("known_crimes", self.dm_core.entities["shopkeeper"])
        self.assertEqual(self.dm_core.legal_records, {})

    def test_emptying_a_container_is_not_theft(self):
        self.assertEqual(self.dm_core.report_crime("theft", "gladstone", victim="dagger"), [])

    def test_only_a_fumbled_sleight_of_hand_is_reported(self):
        maneuver = self.dm_core.entities["sleight of hand"]
        ctx = {"actor": "gladstone", "target": "shopkeeper"}
        run_program(maneuver["on_pass"], ctx, self.dm_core.entities, self.dm_core.rules, self.event_bus)
        self.assertNotIn("known_crimes", self.dm_core.entities["shopkeeper"])
        run_program(maneuver["on_fail"], ctx, self.dm_core.entities, self.dm_core.rules, self.event_bus)
        self.assertEqual(self.dm_core.entities["shopkeeper"]["known_crimes"][0]["crime"], "theft")

    def _disguise(self, quality):
        run_program(
            self.dm_core.entities["don a disguise"]["on_pass"], {"actor": "gladstone", "roll": quality},
            self.dm_core.entities, self.dm_core.rules, self.event_bus,
        )

    def test_a_disguised_theft_is_blamed_on_the_disguise_unless_seen_through(self):
        self._disguise(30)
        self._stub_roll_dice(1)  # the shopkeeper's observation can't beat 30
        self._steal()
        self.dm_core.advance_blocks(1)
        disguise = self.dm_core.entities["gladstone"]["disguise"]
        self.assertIsNone(self._record())
        self.assertEqual(self._record(disguise["identity"])["bounty"], 5)
        self.assertIn("Was robbed by a disguised stranger.", self.dm_core.legal_facts_for("shopkeeper"))

        # A fresh disguise is a fresh identity, and this time the witness sees through it.
        self._disguise(2)
        self._stub_roll_dice(40)
        self.dm_core.entities["gladstone"]["inventory"].remove("dagger")
        self.dm_core.entities["shopkeeper"]["inventory"].append("dagger")
        self._steal()
        self.dm_core.advance_blocks(1)
        self.assertEqual(self._record()["bounty"], 5)

    def test_assault_then_kill_is_murder_charged_once(self):
        self._add_person("customer")
        self.dm_core.note_assault("gladstone", "shopkeeper")
        Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "shopkeeper", {"damage_value": {"dice": 0, "pips": 0, "bonus": 1000}})
        self.assertEqual([seen["crime"] for seen in self.dm_core.entities["customer"]["known_crimes"]], ["assault", "murder"])
        self.dm_core.advance_blocks(1)
        self.assertEqual((self._record()["bounty"], self._record()["acclaim"]), (100, -6))

    def test_a_witness_turns_fearful_of_the_offender_but_not_hostile(self):
        self._add_person("customer")
        before = self.dm_core.get_attitude("customer", "gladstone")
        self.dm_core.note_assault("gladstone", "shopkeeper")
        Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "shopkeeper", {"damage_value": {"dice": 0, "pips": 0, "bonus": 1000}})
        after = self.dm_core.get_attitude("customer", "gladstone")
        # Assault (0.5) then murder (1.0) -- capped at the action drift cap of 60 per axis.
        self.assertEqual(after[0] - before[0], -60)
        self.assertEqual(after[1] - before[1], -60)
        self.assertFalse(self.dm_core.is_hostile("customer", "gladstone"))

    def test_a_theft_witness_cools_a_little_and_the_victim_gets_no_double_dose(self):
        self._add_person("customer")
        victim_before = self.dm_core.get_attitude("shopkeeper", "gladstone")
        self._steal()
        self.assertEqual(self.dm_core.get_attitude("customer", "gladstone")[0], 20 - 15)
        # The victim's own drift is the existing "theft" event alone.
        victim_drift = self.dm_core.get_attitude("shopkeeper", "gladstone")[0] - victim_before[0]
        self.assertGreater(victim_drift, -15)

    def test_killing_someone_who_struck_first_is_no_crime(self):
        self._add_person("customer")
        Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "shopkeeper", {"damage_value": {"dice": 0, "pips": 0, "bonus": 1000}})
        self.assertNotIn("known_crimes", self.dm_core.entities["customer"])

    def test_attacking_a_bystander_is_an_assault(self):
        self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "blades", "target": "shopkeeper"}], "input": "i attack the shopkeeper"})
        self.assertEqual(self.dm_core.entities["shopkeeper"]["known_crimes"][0]["crime"], "assault")
        self.assertEqual(self.dm_core.entities["shopkeeper"]["assaulted_by"], ["gladstone"])

    def _cast(self, school):
        spell = {"name": "bone chill", "supertype": "spell", "subtype": school}
        self.dm_core.entities["bone chill"] = spell
        self.dm_core.observe_ability_use("gladstone", spell)

    def test_a_banned_spell_is_a_crime_only_to_a_witness_who_identifies_it(self):
        self._stub_roll_dice(1)
        self._cast("necromancy")
        self.assertNotIn("known_crimes", self.dm_core.entities["shopkeeper"])

        self._stub_roll_dice(40)
        self._cast("necromancy")
        [seen] = self.dm_core.entities["shopkeeper"]["known_crimes"]
        self.assertEqual((seen["crime"], seen["subject"]), ("banned_ability", "bone chill"))

        self._cast("evocation")  # not banned here
        self.assertEqual(len(self.dm_core.entities["shopkeeper"]["known_crimes"]), 1)

    def test_where_all_magic_is_banned_an_unidentified_spell_still_counts(self):
        self.dm_core.locations[self.dm_core.current_location_key]["law"] = [
            {"crime": "banned_ability", "match": {"supertypes": ["spell"]}, "fine": 1, "acclaim": 0},
        ]
        self._stub_roll_dice(1)
        self._cast("evocation")
        self.assertEqual(self.dm_core.entities["shopkeeper"]["known_crimes"][0]["subject"], "a spell")

    def _bring_undead(self, **fields):
        self._add_person("bone servant", subtype="undead", is_party=True, **fields)

    def test_an_obvious_undead_companion_is_recognized_once(self):
        self._bring_undead(acclaim=-20)  # "automatic" band -- anyone recognizes a walking corpse
        self.dm_core.check_presence()
        self.dm_core.check_presence()
        [seen] = self.dm_core.entities["shopkeeper"]["known_crimes"]
        self.assertEqual((seen["crime"], seen["subject"], seen["offender"]), ("banned_presence", "bone servant", "bone servant"))

    def test_an_unknown_face_is_never_recognized(self):
        self._bring_undead()  # acclaim 0 -- nobody knows what it is
        self.dm_core.check_presence()
        self.assertNotIn("known_crimes", self.dm_core.entities["shopkeeper"])

    def test_a_good_disguise_hides_a_banned_presence(self):
        self._bring_undead(acclaim=-20)
        self.dm_core.entities["bone servant"]["disguise"] = {"quality": 30, "alias": "a hooded figure", "identity": "bone servant (disguise 1)"}
        self._stub_roll_dice(1)
        self.dm_core.check_presence()
        self.assertNotIn("known_crimes", self.dm_core.entities["shopkeeper"])

    def test_only_a_witness_carries_the_crime_into_its_persona(self):
        self._add_person("customer")
        self._steal()
        self._add_person("latecomer")
        self.assertIn("Was robbed by gladstone.", self.dm_core.describe_character("shopkeeper"))
        self.assertIn("Saw gladstone steal from shopkeeper.", self.dm_core.describe_character("customer"))
        self.assertNotIn("steal", self.dm_core.describe_character("latecomer"))

    def test_records_and_witness_knowledge_survive_save_and_reload(self):
        self._disguise(30)
        self._stub_roll_dice(1)
        self._steal()
        self.dm_core.advance_blocks(1)
        store = MemorySlotStore()
        self.dm_core.slot_store = store
        self.dm_core.save_game("law_slot")
        reloaded = DMCore(
            ValidatingEventBus(), scenario_name="debug", start_location="general_store", setting="Fantasy", slot_store=store,
        )
        reloaded.load_game("law_slot")
        self.assertEqual(reloaded.legal_records, self.dm_core.legal_records)
        self.assertEqual(reloaded.entities["shopkeeper"]["known_crimes"], self.dm_core.entities["shopkeeper"]["known_crimes"])
        self.assertEqual(reloaded.entities["gladstone"]["disguise"], self.dm_core.entities["gladstone"]["disguise"])


class TestEnforcement(DMTestCase):
    """!
    @brief DM_Enforcement.py end to end, in the same general store under "Test Crown" TestLaw
        uses, with a town guard (law_enforcer) standing in it. Theft from the shopkeeper is
        the usual crime: Test Crown fines it 5.
    """
    start_location = "general_store"

    def setUp(self):
        super().setUp()
        self.dm_core.locations[self.dm_core.current_location_key]["polity"] = "Test Crown"
        self.dm_core.entities["gladstone"]["currency"] = 10
        self.confronted = self._capture("arrest_confronted")
        self.resolved = self._capture("arrest_resolved")
        self.awaiting = self._capture("arrest_awaiting")
        self.notices = self._capture("player_notice")

    def _add_person(self, name, **fields):
        entity = {
            "name": name, "supertype": "creature", "subtype": "humanoid", "max_hp": 10,
            "languages": ["common"], "attitudes": {"default": [20, 0, 0]}, **fields,
        }
        self.dm_core.entities[name] = entity
        self.dm_core._place_new_entity(name, entity, 1)
        self.dm_core.scenario_entities.append(name)

    def _add_guard(self, name="guard", tags=("law_enforcer",)):
        self._add_person(name, tags=list(tags), skills={
            "willpower": {"dice": 2, "pips": 0}, "observation": {"dice": 2, "pips": 0},
            "streetwise": {"dice": 2, "pips": 0},
        })

    def _steal(self):
        self.dm_core._on_item_interaction_detected({"intent": "take", "item_name": "dagger", "input": "steal the dagger"})

    def _record(self, identity="gladstone"):
        return self.dm_core.legal_records.get("Test Crown", {}).get(identity)

    def _answer(self, choice, text=None):
        self.event_bus.publish("arrest_answered", {"choice": choice, "input": text or choice})

    def _wanted(self, bounty=5, acclaim=0):
        Law_Resolution.file_report(
            self.dm_core.legal_records, "Test Crown", "gladstone", {"fine": bounty, "acclaim": -1},
            {"crime": "theft", "victim": "shopkeeper", "block": 0},
        )
        self.dm_core.entities["gladstone"]["acclaim"] = acclaim

    # -- Starting -----------------------------------------------------------------------

    def test_an_enforcer_who_sees_a_crime_confronts_at_once(self):
        self._add_guard()
        self._steal()
        self.assertEqual(self._record()["bounty"], 5)
        [demand] = self.confronted
        self.assertEqual((demand["kind"], demand["witnessed"], demand["amount"]), ("arrest", True, 5))
        self.assertEqual(demand["charges"], ["theft (shopkeeper)"])
        self.assertEqual(self.awaiting[-1]["choices"], ["pay", "surrender", "bribe", "bluff", "resist"])
        # The options ride on the narration, so they're shown after it, not ahead of it.
        self.assertIn("Reply with one of: pay, surrender, bribe <amount>, bluff, resist.", demand["notice"])
        self.assertEqual(self.notices, [])

    def test_the_demand_waits_until_the_input_has_resolved(self):
        self._add_guard()
        self.event_bus.publish("player_input_received", "steal the dagger")
        self._steal()
        self.assertEqual(self.confronted, [])
        self.event_bus.publish("player_input_handled", {"input": "steal the dagger"})
        self.assertEqual(len(self.confronted), 1)
        self.assertTrue(self.dm_core.pending_arrest["announced"])

    def test_a_guard_who_is_the_victim_fights_instead_of_arresting(self):
        self._add_guard()
        self.dm_core.nudge_attitude_from_event("guard", "gladstone", "assaulted", 1.0)
        self.dm_core.note_assault("gladstone", "guard")
        self.assertIsNone(self.dm_core.pending_arrest)
        self.assertEqual(self.confronted, [])

    def test_a_wanted_player_is_arrested_only_once_recognized(self):
        self._wanted(acclaim=0)
        self._stub_roll_dice(1)  # magnitude 1 is "very difficult" -- the guard's streetwise fails
        self._add_guard()
        self.dm_core.law_enforcement.check_enforcement()
        self.assertIsNone(self.dm_core.pending_arrest)
        self.assertEqual(self.dm_core.entities["guard"]["enforcement_checks"], {"gladstone|": False})

        self._stub_roll_dice(100)  # checked once per guard per identity: no second chance
        self.dm_core.law_enforcement.check_enforcement()
        self.assertIsNone(self.dm_core.pending_arrest)

        self._add_guard("captain")
        self.dm_core.law_enforcement.check_enforcement()
        self.assertEqual(self.dm_core.pending_arrest["enforcer"], "captain")

    def test_famous_enough_is_recognized_without_a_roll(self):
        self._wanted(acclaim=25)
        self._stub_roll_dice(0)
        self._add_guard()
        self.dm_core.law_enforcement.check_enforcement()
        self.assertEqual(self.dm_core.pending_arrest["enforcer"], "guard")
        self.assertFalse(self.confronted[0]["witnessed"])

    def test_a_disguise_the_guard_cant_see_through_hides_a_wanted_player(self):
        self._wanted(acclaim=25)
        run_program(
            self.dm_core.entities["don a disguise"]["on_pass"], {"actor": "gladstone", "roll": 30},
            self.dm_core.entities, self.dm_core.rules, self.event_bus,
        )
        self._stub_roll_dice(1)  # the guard's observation can't beat 30
        self._add_guard()
        self.dm_core.law_enforcement.check_enforcement()
        self.assertIsNone(self.dm_core.pending_arrest)

    def test_past_kill_on_sight_the_guard_attacks_instead(self):
        self._wanted(bounty=100, acclaim=25)
        self._add_guard()
        self.dm_core.law_enforcement.check_enforcement()
        self.assertIsNone(self.dm_core.pending_arrest)
        self.assertEqual(self.confronted[0]["kind"], "kill_on_sight")
        self.assertTrue(self.dm_core.is_hostile("guard", "gladstone"))

    def test_below_arrest_at_nobody_bothers(self):
        self._wanted(bounty=5, acclaim=25)
        World_Map.find_polity(self.dm_core.rules, "Test Crown")["arrest_at"] = 10
        self.addCleanup(World_Map.find_polity(self.dm_core.rules, "Test Crown").__setitem__, "arrest_at", 1)
        self._add_guard()
        self.dm_core.law_enforcement.check_enforcement()
        self.assertIsNone(self.dm_core.pending_arrest)

    # -- The replies --------------------------------------------------------------------

    def test_paying_settles_the_bounty_and_ends_it(self):
        self._add_guard()
        self._steal()
        self._answer("pay")
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 5)
        self.assertEqual(self.dm_core.entities["guard"]["currency"], 5)
        self.assertEqual(self._record()["bounty"], 0)
        self.assertTrue(all(crime["settled"] for crime in self._record()["crimes"]))
        self.assertEqual(self._record()["acclaim"], -1)  # acclaim never decays
        self.assertIsNone(self.dm_core.pending_arrest)
        self.assertEqual(self.resolved[-1]["outcome"], "paid")

    def test_paying_without_the_money_asks_again(self):
        self.dm_core.entities["gladstone"]["currency"] = 2
        self._add_guard()
        self._steal()
        self._answer("pay")
        self.assertIsNotNone(self.dm_core.pending_arrest)
        self.assertIn("You have 2 coins, not the 5 coins owed.", self.notices[-1]["message"])

    def test_surrendering_short_serves_the_rest_in_jail(self):
        self.dm_core.locations["debug_hub"]["jail"] = "tavern_floor"
        self.dm_core.entities["gladstone"]["currency"] = 2
        self._add_guard()
        self._steal()
        block = self.dm_core.current_block
        self._answer("surrender")
        # 3 unpaid at 0.2 blocks per unit is 0.6, rounded up to one block.
        self.assertEqual(self.dm_core.current_block, block + 1)
        self.assertEqual(self.dm_core.current_location_key, "tavern_floor")
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 0)
        self.assertEqual(self._record()["bounty"], 0)
        outcome = self.resolved[-1]
        self.assertEqual((outcome["outcome"], outcome["blocks"], outcome["jail_name"]), ("surrendered", 1, "The Rusty Tankard"))

    def test_surrendering_with_enough_money_serves_no_time(self):
        self._add_guard()
        self._steal()
        block = self.dm_core.current_block
        self._answer("surrender")
        self.assertEqual(self.dm_core.current_block, block)
        self.assertEqual(self.resolved[-1]["blocks"], 0)

    def test_a_taken_bribe_buys_this_guard_off(self):
        self._add_guard()
        self._steal()
        self._stub_roll_dice(10)  # equal rolls; an offer of the whole bounty eases it by 5
        self._answer("bribe", "bribe him 5 gold")
        self.assertIsNone(self.dm_core.pending_arrest)
        self.assertEqual(self.resolved[-1]["outcome"], "bribed")
        self.assertEqual(self.dm_core.entities["guard"]["looked_away"], {"gladstone": 5})
        self.assertEqual(self._record()["bounty"], 5)  # the record itself stands
        self.dm_core.law_enforcement.check_enforcement()
        self.assertIsNone(self.dm_core.pending_arrest)

    def test_an_incorruptible_guard_refuses_and_the_demand_stands(self):
        self._add_guard(tags=("law_enforcer", "incorruptible"))
        self._steal()
        self._stub_roll_dice(100)
        self._answer("bribe", "bribe 10 gold")
        self.assertEqual(self.resolved[-1]["outcome"], "bribe_refused")
        self.assertIn("Reply with one of: pay, surrender, bluff, resist.", self.resolved[-1]["notice"])
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 10)
        self.assertEqual(self.awaiting[-1]["choices"], ["pay", "surrender", "bluff", "resist"])

    def test_a_bribe_needs_an_amount_and_the_money(self):
        self._add_guard()
        self._steal()
        self._answer("bribe", "slip him something")
        self.assertIn("Say how much", self.notices[-1]["message"])
        self._answer("bribe", "bribe 50 gold")
        self.assertIn("You only have 10 coins.", self.notices[-1]["message"])
        self.assertNotIn("bribe", self.dm_core.pending_arrest["tried"])

    def test_a_bluff_is_harder_when_the_guard_saw_it(self):
        self._add_guard()
        self._steal()
        self._stub_roll_dice(10)  # equal rolls, but witnessed adds 5
        self._answer("bluff")
        self.assertEqual(self.resolved[-1]["outcome"], "bluff_failed")
        self.assertIsNotNone(self.dm_core.pending_arrest)
        self._answer("bluff")
        self.assertIn("You already tried a bluff.", self.notices[-1]["message"])

    def test_a_good_bluff_means_the_guard_no_longer_knows_them(self):
        self._wanted(acclaim=25)
        self._add_guard()
        self.dm_core.law_enforcement.check_enforcement()
        self._stub_roll_dice(10)
        self._answer("bluff")
        self.assertEqual(self.resolved[-1]["outcome"], "bluffed")
        self.assertFalse(self.dm_core.entities["guard"]["enforcement_checks"]["gladstone|"])
        self.dm_core.law_enforcement.check_enforcement()
        self.assertIsNone(self.dm_core.pending_arrest)

    def test_resisting_turns_the_guards_hostile_and_is_a_crime(self):
        self._add_guard()
        self._add_guard("second guard")
        self._steal()
        self._answer("resist")
        self.assertTrue(self.dm_core.is_hostile("guard", "gladstone"))
        self.assertTrue(self.dm_core.is_hostile("second guard", "gladstone"))
        self.assertEqual(self._record()["bounty"], 25)
        self.assertEqual(self._record()["crimes"][-1]["crime"], "resisting_arrest")
        self.assertEqual(self.resolved[-1]["outcome"], "resisted")

    def _input(self, text, act):
        self.event_bus.publish("player_input_received", text)
        act()
        self.event_bus.publish("player_input_handled", {"input": text})

    def test_walking_away_is_fleeing(self):
        self._add_guard()
        self._steal()
        self._input("go to the hub", lambda: self.dm_core._enter_location("debug_hub"))
        self.assertEqual(self.resolved[-1]["outcome"], "fled")
        self.assertTrue(self.dm_core.is_hostile("guard", "gladstone"))
        self.assertEqual(self._record()["bounty"], 25)

    def test_attacking_anyone_is_resisting(self):
        self._add_guard()
        self._add_person("bystander")
        self._steal()
        self._input("punch the bystander", lambda: (
            self._answer("other", "punch the bystander"), self.dm_core.note_assault("gladstone", "bystander"),
        ))
        self.assertEqual(self.resolved[-1]["how"], "attacked")

    def test_carrying_on_twice_is_resisting_but_talk_is_not(self):
        self._add_guard()
        self._steal()

        def carry_on():
            self._answer("other", "look around")
            self.event_bus.publish("turn_detected", {"clauses": [], "input": "look around"})

        self._input("what's the charge?", lambda: self._answer("other", "what's the charge?"))
        self.assertEqual(self.dm_core.pending_arrest["strikes"], 0)
        self._input("look around", carry_on)
        self.assertEqual((self.dm_core.pending_arrest["strikes"], self.confronted[-1]["kind"]), (1, "repeat"))
        self._input("look around", carry_on)
        self.assertEqual(self.resolved[-1]["how"], "ignored")

    def test_an_open_arrest_survives_save_and_reload(self):
        self._add_guard()
        self._steal()
        store = MemorySlotStore()
        self.dm_core.slot_store = store
        self.dm_core.save_game("arrest_slot")
        event_bus = ValidatingEventBus()
        awaiting = []
        event_bus.subscribe("arrest_awaiting", awaiting.append)
        reloaded = DMCore(
            event_bus, scenario_name="debug", start_location="general_store", setting="Fantasy", slot_store=store,
        )
        reloaded.load_game("arrest_slot")
        self.assertEqual(reloaded.pending_arrest["enforcer"], "guard")
        self.assertEqual(awaiting[-1]["choices"], ["pay", "surrender", "bribe", "bluff", "resist"])

    def test_when_the_victim_is_a_guard_another_guard_steps_in(self):
        self._add_guard()
        self._add_guard("jailer")
        self.dm_core.nudge_attitude_from_event("guard", "gladstone", "assaulted", 1.0)
        self.dm_core.note_assault("gladstone", "guard")
        self.assertEqual(self.dm_core.pending_arrest["enforcer"], "jailer")

    def test_a_landmark_is_under_its_towns_law(self):
        del self.dm_core.locations["general_store"]["polity"]
        self.dm_core.locations["debug_hub"]["polity"] = "Test Crown"
        self.assertEqual(self.dm_core.current_polity(), "Test Crown")


class TestSandpointEnforcement(DMTestCase):
    """!@brief The shipped Varisia/Sandpoint enforcement data, in the garrison (sheriff + jailer)."""
    scenario_name = "lost_coast"
    setting = "Pathfinder"  # lost_coast is Golarion-sourced content, kept isolated under Rules/Pathfinder/
    start_location = "garrison"

    def test_the_garrison_is_under_varisian_law_and_is_sandpoints_jail(self):
        self.assertEqual(self.dm_core.current_polity(), "Varisia")
        self.assertEqual(self.dm_core.law_enforcement.jail_location(), "garrison")

    def test_a_reload_keeps_the_guards_able_to_witness(self):
        # Found by playtest: the reload replay instanced the garrison while standing elsewhere,
        # so its polity-language default never applied and nobody there could witness a crime.
        self.dm_core.slot_store = MemorySlotStore()
        self.dm_core.save_game("garrison_slot")
        self.dm_core.load_game("garrison_slot")
        self.assertEqual(self.dm_core.entities["Vachedi"]["languages"], ["varisian"])
        self.dm_core.nudge_attitude_from_event("Belor Hemlock", "gladstone", "assaulted", 1.0)
        self.dm_core.note_assault("gladstone", "Belor Hemlock")
        self.assertEqual(self.dm_core.pending_arrest["enforcer"], "Vachedi")


class TestEnforcementHelpers(unittest.TestCase):
    """!@brief Law_Resolution.py's jail/bribe helpers and Inventory_Resolution.py's money parser."""

    def test_jail_time_rounds_up_and_nothing_owed_is_no_time(self):
        self.assertEqual(Law_Resolution.jail_blocks(3, 0.2), 1)
        self.assertEqual(Law_Resolution.jail_blocks(10, 0.2), 2)
        self.assertEqual(Law_Resolution.jail_blocks(11, 0.2), 3)
        self.assertEqual(Law_Resolution.jail_blocks(0, 0.2), 0)

    def test_a_bigger_bribe_is_easier_and_a_tiny_one_an_insult(self):
        bands = [{"min_share": 1.0, "modifier": -5}, {"min_share": 0.5, "modifier": 0}, {"min_share": 0.25, "modifier": 5}]
        self.assertEqual(Law_Resolution.bribe_modifier(10, 10, bands), -5)
        self.assertEqual(Law_Resolution.bribe_modifier(5, 10, bands), 0)
        self.assertEqual(Law_Resolution.bribe_modifier(3, 10, bands), 5)
        self.assertIsNone(Law_Resolution.bribe_modifier(1, 10, bands))

    def test_money_is_read_in_the_settings_coins(self):
        from resolution.Inventory_Resolution import parse_currency_amount
        coins = [{"name": "gold piece", "worth": 1}, {"name": "silver piece", "worth": 0.1}, {"name": "copper piece", "worth": 0.01}]
        for text, amount in (("bribe him with 5 gold", 5), ("8 sp", 0.8), ("20 silver pieces", 2),
                             ("3 coppers", 0.03), ("5 coins", 5), ("slip him something", None)):
            with self.subTest(text=text):
                self.assertEqual(parse_currency_amount(text, coins), amount)
        self.assertEqual(parse_currency_amount("12 coins"), 12)


class TestArrestReplies(unittest.TestCase):
    """!@brief NLPCore reads the input after an arrest demand as the reply (_answer_arrest) --
        exercised through a real NLPCore built on a FakeMatcher, so no model loads."""

    def _nlp(self, choices=("pay", "surrender", "bribe", "bluff", "resist")):
        bus = ValidatingEventBus()
        answers = []
        bus.subscribe("arrest_answered", answers.append)
        nlp = NLPCore(bus, FakeMatcher())
        nlp._arrest_choices = list(choices)
        return nlp, answers

    def test_a_reply_opening_with_an_option_is_that_option(self):
        nlp, answers = self._nlp()
        with patch("nlp.NLP_Core.classify_arrest_reply") as model:
            self.assertTrue(nlp._answer_arrest("Bribe him with 5 gold"))
        model.assert_not_called()
        self.assertEqual(answers, [{"choice": "bribe", "input": "Bribe him with 5 gold"}])
        self.assertIsNone(nlp._arrest_choices)

    def test_an_i_before_the_option_still_reads_as_the_option(self):
        # Found by playtest: "I resist." went to the model.
        for text, choice in (("I resist.", "resist"), ("I'll pay him", "pay"), ("ok, i surrender", "surrender")):
            with self.subTest(text=text):
                nlp, answers = self._nlp()
                with patch("nlp.NLP_Core.classify_arrest_reply") as model:
                    self.assertTrue(nlp._answer_arrest(text))
                model.assert_not_called()
                self.assertEqual(answers[0]["choice"], choice)

    def test_anything_else_asks_the_model(self):
        nlp, answers = self._nlp()
        with patch("nlp.NLP_Core.classify_arrest_reply", return_value=("surrender", "")):
            self.assertTrue(nlp._answer_arrest("fine, take me in"))
        self.assertEqual(answers[0]["choice"], "surrender")

        nlp, answers = self._nlp()
        with patch("nlp.NLP_Core.classify_arrest_reply", return_value=(None, "unavailable")):
            self.assertFalse(nlp._answer_arrest("what's the charge?"))
        self.assertEqual(answers[0]["choice"], "other")

    def test_an_option_already_tried_is_not_an_answer(self):
        nlp, answers = self._nlp(choices=("pay", "surrender", "resist"))
        with patch("nlp.NLP_Core.classify_arrest_reply", return_value=("other", "")):
            self.assertFalse(nlp._answer_arrest("bluff again"))
        self.assertEqual(answers[0]["choice"], "other")

    def test_saving_leaves_the_question_open(self):
        nlp, answers = self._nlp()
        self.assertFalse(nlp._answer_arrest("save game1"))
        self.assertEqual(answers, [])
        self.assertEqual(nlp._arrest_choices[0], "pay")


class TestArrestReplyClassification(unittest.TestCase):
    """!@brief AdHoc_Generation.py's classify_arrest_reply against a stubbed chat client."""

    def _reply(self, answer):
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "classify_reply", "arguments": json.dumps({"answer": answer}),
        }}]}}]}

    def test_an_offered_answer_comes_back(self):
        from resolution.AdHoc_Generation import classify_arrest_reply
        calls = []

        def client(*args, **kwargs):
            calls.append((args, kwargs))
            return self._reply("bluff")
        script_llm(self, client)
        self.assertEqual(classify_arrest_reply("you've got the wrong man", "Belor", "5 gold pieces"), ("bluff", ""))
        self.assertIn("Belor is arresting the player's character for 5 gold pieces.", calls[0][0][1][1]["content"])

    def test_an_answer_not_on_offer_is_no_answer(self):
        from resolution.AdHoc_Generation import classify_arrest_reply
        script_llm(self, lambda *a, **k: self._reply("bribe"))
        result = classify_arrest_reply("x", choices=["pay", "resist"])
        self.assertEqual(result, (None, "invalid_answer"))


class FakeLawWorld(LawWorld):
    """!
    @brief A LawWorld with no DMCore behind it: plain dicts for the state, and one ordered
        timeline of everything LawEnforcement did to it -- events published and world changes
        alike -- so a test can assert on ordering. rolls scripts resolve_action's outcomes.
    """

    def __init__(self):
        self.entities = {
            "hero": {"name": "Hero", "currency": 10, "hp": 10, "max_hp": 10},
            "guard": {"name": "the guard", "tags": ["law_enforcer"], "hp": 10, "max_hp": 10},
        }
        self.rules = {
            "polity": [{"name": "Crown", "arrest_at": 1, "jail": "cells"}],
            "law": {
                "jail_blocks_per_unit": 0.5, "bribe": [{"min_share": 0, "modifier": 0}],
                "recognition": [{"min_acclaim": 1, "tier": "automatic"}],
            },
            "currency": {"denomination": [{"name": "gold", "value": 1, "plural": "gold"}]},
        }
        self.scenario_entities = ["hero", "guard"]
        self.player_name = "hero"
        self.current_location_key = "square"
        self.locations = {"square": {"name": "The Square"}, "cells": {"name": "The Cells"}}
        self.current_block = 0
        self.hostile = set()
        self.rolls = []
        self.timeline = []

    def events(self, name):
        return [payload for kind, label, payload in self.timeline if kind == "event" and label == name]

    def current_polity(self):
        return "Crown"

    def sees_through(self, witness, subject):
        return True

    def is_hostile(self, entity_name, toward_name):
        return entity_name in self.hostile

    def is_party_member(self, entity_name):
        return entity_name == self.player_name

    def resolve_action(self, entity_name, skill_name, difficulty=0):
        outcome = self.rolls.pop(0) if self.rolls else {"success": True, "roll": 10}
        return {"roll": 10, **outcome}

    def nudge_attitude(self, entity_name, toward_name, event_name, magnitude):
        self.timeline.append(("nudge", event_name, entity_name))
        if event_name in ("resisted_arrest", "wanted_dead"):
            self.hostile.add(entity_name)

    def transfer_currency(self, from_name, to_name, amount):
        self.entities[from_name]["currency"] -= amount
        self.entities[to_name]["currency"] = self.entities[to_name].get("currency", 0) + amount
        self.timeline.append(("transfer", to_name, amount))

    def format_currency(self, amount):
        return f"{amount} gold"

    def enter_location(self, location_key):
        self.current_location_key = location_key
        self.timeline.append(("move", location_key, None))

    def advance_blocks(self, blocks):
        self.current_block += blocks
        self.timeline.append(("advance", blocks, None))

    def hours_for_blocks(self, blocks):
        return blocks * 2

    def publish(self, event, payload):
        self.timeline.append(("event", event, payload))


class TestLawEnforcementWithoutDMCore(unittest.TestCase):
    """!
    @brief resolution/Law_Enforcement.py driven entirely through a FakeLawWorld -- the arrest
        flow with no DMCore, TOML boot or EventBus.
    """

    LAW = {"fine": 5, "acclaim": -1}
    LINE = {"crime": "theft", "victim": "guard", "subject": None, "block": 0}

    def setUp(self):
        self.world = FakeLawWorld()
        self.law = LawEnforcement(self.world)

    def _wanted(self, bounty=5):
        self.law.file_report({
            "polity": "Crown", "identity": "hero", "law": {"fine": bounty, "acclaim": -1}, "line": self.LINE,
        })

    def _confront(self):
        self._wanted()
        self.law.check_enforcement()
        self.assertTrue(self.law.pending_arrest)

    def test_a_wanted_player_is_confronted_when_the_guard_recognizes_them(self):
        self._wanted()
        self.law.check_enforcement()

        [demand] = self.world.events("arrest_confronted")
        self.assertEqual(demand["kind"], "arrest")
        self.assertEqual(demand["amount"], 5)
        self.assertEqual(self.world.events("arrest_awaiting"), [{"choices": list(ARREST_CHOICES)}])
        self.assertIn("pay", demand["notice"])

    def test_a_bounty_under_arrest_at_is_ignored(self):
        self.world.rules["polity"][0]["arrest_at"] = 10
        self._wanted(bounty=5)
        self.law.check_enforcement()
        self.assertIsNone(self.law.pending_arrest)

    def test_a_confrontation_started_mid_input_is_announced_only_once_the_input_is_handled(self):
        self.law.on_input_started()
        self._wanted()
        self.law.enforcer_witnessed("guard", "Crown", "hero")
        self.assertEqual(self.world.events("arrest_confronted"), [])

        self.law.on_input_handled()
        self.assertEqual(len(self.world.events("arrest_confronted")), 1)

    def test_paying_settles_the_record_and_hands_the_money_over(self):
        self._confront()
        self.law.on_arrest_answered({"choice": "pay"})

        self.assertEqual(self.law.legal_records["Crown"]["hero"]["bounty"], 0)
        self.assertEqual(self.world.entities["guard"]["currency"], 5)
        self.assertIsNone(self.law.pending_arrest)
        self.assertEqual(self.world.events("arrest_resolved")[-1]["outcome"], "paid")

    def test_paying_without_enough_money_keeps_the_demand_open(self):
        self.world.entities["hero"]["currency"] = 2
        self._confront()
        self.law.on_arrest_answered({"choice": "pay"})

        self.assertTrue(self.law.pending_arrest)
        self.assertIn("You have 2 gold", self.world.events("player_notice")[-1]["message"])

    def test_surrender_announces_before_moving_to_jail_and_advancing_the_clock(self):
        self.world.entities["hero"]["currency"] = 1
        self._confront()
        self.law.on_arrest_answered({"choice": "surrender"})

        order = [(kind, label) for kind, label, _ in self.world.timeline if kind in ("move", "advance") or label == "arrest_resolved"]
        self.assertEqual(order, [("event", "arrest_resolved"), ("move", "cells"), ("advance", 2)])
        resolved = self.world.events("arrest_resolved")[-1]
        self.assertEqual((resolved["outcome"], resolved["blocks"], resolved["hours"]), ("surrendered", 2, 4))
        self.assertEqual(self.law.legal_records["Crown"]["hero"]["bounty"], 0)

    def test_an_incorruptible_enforcer_refuses_a_bribe_without_a_roll(self):
        self.world.entities["guard"]["tags"].append("incorruptible")
        self._confront()
        self.law.on_arrest_answered({"choice": "bribe", "input": "bribe 5 gold"})

        self.assertEqual(self.world.events("arrest_resolved")[-1]["outcome"], "bribe_refused")
        self.assertEqual(self.world.entities["hero"]["currency"], 10)
        self.assertEqual(self.law.legal_records["Crown"]["hero"]["bounty"], 5)

    def test_a_taken_bribe_makes_the_enforcer_look_away_until_the_bounty_rises(self):
        self._confront()
        self.law.on_arrest_answered({"choice": "bribe", "input": "bribe 5 gold"})

        self.assertEqual(self.world.events("arrest_resolved")[-1]["outcome"], "bribed")
        self.assertEqual(self.world.entities["guard"]["looked_away"], {"hero": 5})
        self.law.check_enforcement()
        self.assertIsNone(self.law.pending_arrest)

    def test_a_bribe_with_no_amount_asks_again(self):
        self._confront()
        self.law.on_arrest_answered({"choice": "bribe", "input": "bribe him"})

        self.assertTrue(self.law.pending_arrest)
        self.assertIn("how much", self.world.events("player_notice")[-1]["message"])

    def test_a_fooled_enforcer_stops_taking_the_player_for_that_identity(self):
        self._confront()
        self.world.rolls = [{"roll": 3}, {"success": True}]
        self.law.on_arrest_answered({"choice": "bluff"})

        self.assertEqual(self.world.events("arrest_resolved")[-1]["outcome"], "bluffed")
        self.assertIs(self.world.entities["guard"]["enforcement_checks"]["hero|"], False)

    def test_resisting_turns_every_enforcer_hostile_and_files_the_resisting_law(self):
        self.world.rules["polity"][0]["law"] = [{"crime": "resisting_arrest", "fine": 20, "acclaim": -2}]
        self._confront()
        self.law.on_arrest_answered({"choice": "resist"})

        self.assertIn("guard", self.world.hostile)
        self.assertEqual(self.law.legal_records["Crown"]["hero"]["bounty"], 25)
        self.assertEqual(self.world.events("arrest_resolved")[-1]["outcome"], "resisted")

    def test_leaving_the_location_counts_as_fleeing(self):
        self._confront()
        self.world.current_location_key = "elsewhere"
        self.law.on_input_started()
        self.law.on_input_handled()

        self.assertIsNone(self.law.pending_arrest)
        self.assertEqual(self.world.events("arrest_resolved")[-1]["how"], "fled")

    def test_carrying_on_twice_counts_as_resisting(self):
        self._confront()
        for _ in range(STALL_LIMIT):
            self.law.on_input_started()
            self.law.on_arrest_answered({"choice": "other"})
            self.law.note_player_acted()
            self.law.on_input_handled()

        self.assertEqual(self.world.events("arrest_resolved")[-1]["how"], "ignored")

    def test_a_queued_report_is_filed_only_if_a_witness_lived(self):
        self.world.entities["witness"] = {"hp": 0, "max_hp": 5}
        self.law.queue_report({
            "polity": "Crown", "identity": "hero", "law": self.LAW, "line": self.LINE, "witnesses": ["witness"],
        })
        self.law.file_pending_reports()
        self.assertEqual(self.law.legal_records, {})

        self.world.entities["witness"]["hp"] = 5
        self.law.queue_report({
            "polity": "Crown", "identity": "hero", "law": self.LAW, "line": self.LINE, "witnesses": ["witness"],
        })
        self.law.file_pending_reports()
        self.assertEqual(self.law.legal_records["Crown"]["hero"]["bounty"], 5)

    def test_state_round_trips_and_an_announced_arrest_asks_its_question_again(self):
        self._confront()
        saved = self.law.snapshot()

        reloaded = LawEnforcement(self.world)
        reloaded.restore(json.loads(json.dumps(saved)))
        self.world.timeline.clear()
        reloaded.resume_after_load()

        self.assertEqual(reloaded.legal_records, self.law.legal_records)
        self.assertEqual(reloaded.pending_arrest, self.law.pending_arrest)
        self.assertEqual(self.world.events("arrest_awaiting"), [{"choices": list(ARREST_CHOICES)}])


if __name__ == "__main__":
    unittest.main()
