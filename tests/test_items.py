import os
import pathlib
import shutil
import unittest
from unittest.mock import patch
import resolution.Combat_Resolution as Combat_Resolution
from resolution.Inventory_Resolution import format_currency
from dm.DM_ActionOutcome import (
    CraftEffect,
    LootEffect,
    MissingMaterialsOutcome,
    MissingStationOutcome,
    MovementOutcome,
    NotCraftableOutcome,
    RevealEffect,
    TransferOutcome,
)
from intents.registry import HANDLERS as FREE_STANDING_INTENT_HANDLERS
from resolution.Item_Outcome import build_item_interaction_outcome
import resolution.Conveyance as Conveyance
import resolution.Combat_Actions as Combat_Actions
from tests.support import (
    DMTestCase,
)
# Module-level hooks start the shared patches (tests/support.py) for every test in this file.
from tests.support import setUpModule, tearDownModule  # noqa: F401


class TestFreeStandingIntentHandlers(unittest.TestCase):
    """!
    @brief Direct, isolated coverage of every intents/ module's own narrate() (see CONTEXT.md's
        "Free-standing intent") -- the actual testability payoff of collapsing DM_Core.py's
        dispatch and LLM_Core.py's narration ladder into per-intent handlers. None of these
        need a real DMCore/LLMCore/scenario at all: narrate() is a pure function of the
        "item_interaction_resolved" payload (plus, for move/travel only, the llm_core object
        whose scenario_description/scenario_characters it updates for ongoing narration
        grounding -- proven here with a bare object carrying just those two attributes, not a
        real LLMCore, since narrate_move/narrate_travel never call anything else on it).
        resolve()'s own behavior stays covered by the existing end-to-end DMCore tests
        (TestMovementAndRange, TestDowntime, TestGridTravel, ...), which dispatch through
        DMCore._on_item_interaction_detected exactly as before -- this class only closes the
        narration-side gap that had no coverage prior to this collapse.
    """

    class _FakeLLMCore:
        """A stand-in for LLMCore carrying only the two attributes narrate_move/narrate_travel
        actually touch -- proves those two functions need nothing else from LLMCore, and that
        every other free-standing intent's narrate() needs no llm_core at all (called with
        None, below)."""
        scenario_description = ""
        scenario_characters = []

        @staticmethod
        def scene_length_instruction(kind):
            return f"Narrate {kind} in 2-3 sentences as the Game Master."

    def _narrate(self, intent, data, llm_core=None):
        _resolve, narrate = FREE_STANDING_INTENT_HANDLERS[intent]
        return narrate(llm_core, data)

    def test_narrate_advance_retreat_reports_real_band_gap_changes(self):
        prompt = self._narrate("advance", {
            "intent": "advance", "found": True,
            "moved": [{"entity": "wolf", "before": 3, "after": 2}],
        })
        self.assertIn("wolf (3 -> 2 bands away)", prompt)
        self.assertIn("advances", prompt)

    def test_narrate_advance_retreat_handles_no_one_else_present(self):
        prompt = self._narrate("retreat", {"intent": "retreat", "found": True, "moved": []})
        self.assertIn("no one else here", prompt)

    def test_narrate_formation_reports_real_members_and_stance(self):
        prompt = self._narrate("formation_behind", {
            "intent": "formation_behind", "found": True,
            "members": ["anne"], "stance": "behind",
        })
        self.assertIn("anne", prompt)
        self.assertIn("stay a band behind", prompt)

    def test_narrate_formation_explains_no_party_present(self):
        prompt = self._narrate("formation_abreast", {
            "intent": "formation_abreast", "found": False, "reason": "no_party", "input": "walk beside me",
        })
        self.assertIn("no one from the player's own party here", prompt)

    def test_narrate_speak_language_reports_the_real_resolved_language(self):
        prompt = self._narrate("speak_language", {
            "intent": "speak_language", "found": True, "language": "elvish",
        })
        self.assertIn("speaking elvish", prompt)

    def test_narrate_speak_language_explains_an_unknown_language(self):
        prompt = self._narrate("speak_language", {
            "intent": "speak_language", "found": False, "reason": "unknown_language", "input": "speak in dwarvish",
        })
        self.assertIn("doesn't actually know any language", prompt)

    def test_narrate_rest_reports_real_healed_amounts_and_time(self):
        prompt = self._narrate("rest", {
            "intent": "rest", "found": True, "blocks_spent": 2,
            "healed": {"gladstone": {"healed": 10, "remaining_hp": 26}},
            "time": {"is_day": False, "day": 1},
        })
        self.assertIn("gladstone recovers 10 HP (now at 26 HP)", prompt)
        self.assertIn("night, day 1", prompt)

    def test_narrate_rest_never_claims_recovery_that_didnt_happen(self):
        prompt = self._narrate("rest", {
            "intent": "rest", "found": True, "blocks_spent": 1, "healed": {},
            "time": {"is_day": True, "day": 0},
        })
        self.assertIn("no one recovers any HP", prompt)

    def test_narrate_move_grounds_ongoing_narration_on_the_new_room(self):
        llm_core = self._FakeLLMCore()
        prompt = self._narrate("move", {
            "intent": "move", "found": True, "direction": "forward",
            "room_name": "the crypt entrance", "room_description": "Cold air rises from below.",
            "characters": ["thane"],
        }, llm_core)
        self.assertIn("the crypt entrance", prompt)
        self.assertIn("Cold air rises from below.", prompt)
        self.assertIn("Characters present: thane", prompt)
        # narrate only reads: LLMCore is the one writer of the narrator's scene state.
        self.assertEqual(llm_core.scenario_description, "")

    def test_narrate_move_explains_each_failure_reason(self):
        for reason, expected_phrase in (
            ("no_exit", "no way through"), ("wrong_band", "right spot"),
            ("blocked_by_enemies", "hostile is still standing"),
        ):
            with self.subTest(reason=reason):
                prompt = self._narrate("move", {
                    "intent": "move", "found": False, "reason": reason, "direction": "forward", "input": "go forward",
                })
                self.assertIn(expected_phrase, prompt)

    def test_narrate_travel_grounds_ongoing_narration_and_reports_elapsed_time(self):
        llm_core = self._FakeLLMCore()
        prompt = self._narrate("travel", {
            "intent": "travel", "found": True, "location_name": "border stones",
            "location_description": "A ring of weathered stones.", "characters": [],
            "blocks_spent": 1, "distance": 4.0, "time": {"is_day": True, "day": 0},
        }, llm_core)
        self.assertIn("border stones", prompt)
        self.assertIn("1 block(s) of travel time", prompt)
        self.assertIn("A ring of weathered stones.", prompt)
        self.assertEqual(llm_core.scenario_description, "")

    def test_narrate_travel_omits_elapsed_time_for_an_ordinary_exit_graph_hop(self):
        llm_core = self._FakeLLMCore()
        prompt = self._narrate("travel", {
            "intent": "travel", "found": True, "location_name": "town square",
            "location_description": "A bustling square.", "characters": [],
        }, llm_core)
        self.assertNotIn("block(s) of travel time", prompt)

    def test_narrate_travel_names_the_location_not_just_the_arrival_room(self):
        # A player who typed "the tavern" and is narrated arriving in "Common Room" alone has
        # no way to tell a correct destination match from a wrong one -- and semantic
        # destination matching (NLP_Core.py's map_to_destination) makes the player's words and
        # the arrival room's authored name differ routinely, where the literal name scan alone
        # mostly guaranteed they'd agree. Naming the location back is what keeps a guessed
        # destination checkable by the person who guessed at it.
        prompt = self._narrate("travel", {
            "intent": "travel", "found": True,
            "location_name": "The White Deer Tavern and Inn", "room_name": "Common Room",
            "room_description": "A low-beamed taproom.", "characters": [],
        }, self._FakeLLMCore())
        self.assertIn("The White Deer Tavern and Inn", prompt)
        self.assertIn("Common Room", prompt)

    def test_narrate_travel_explains_each_failure_reason(self):
        for reason, expected_phrase in (
            ("no_exit", "no way through"),
            ("blocked_by_enemies", "hostile is still standing"),
            ("downtime_interrupted", "unresolved threat"),
        ):
            with self.subTest(reason=reason):
                prompt = self._narrate("travel", {
                    "intent": "travel", "found": False, "reason": reason, "input": "i travel to nowhere",
                })
                self.assertIn(expected_phrase, prompt)

    def test_narrate_mount_reports_the_real_target(self):
        prompt = self._narrate("mount", {"intent": "mount", "found": True, "target": "horse"})
        self.assertIn("climbs onto horse", prompt)

    def test_narrate_mount_explains_each_failure_reason(self):
        for reason, expected_phrase in (
            ("already_mounted", "already mounted"),
            ("not_present", "nothing here matches"),
            ("target_down", "is down"),
            ("target_hostile", "is hostile"),
            ("not_a_mount", "not something meant to be ridden"),
            ("bulk_exceeded", "no room for another rider"),
        ):
            with self.subTest(reason=reason):
                prompt = self._narrate("mount", {
                    "intent": "mount", "found": False, "reason": reason, "input": "mount the horse",
                })
                self.assertIn(expected_phrase, prompt)

    def test_narrate_dismount_reports_the_real_target(self):
        prompt = self._narrate("dismount", {"intent": "dismount", "found": True, "target": "horse"})
        self.assertIn("dismounts from horse", prompt)

    def test_narrate_dismount_explains_not_being_mounted(self):
        prompt = self._narrate("dismount", {"intent": "dismount", "found": False, "input": "dismount"})
        self.assertIn("aren't mounted on anything", prompt)

    def test_narrate_hitch_reports_the_real_puller_and_vehicle(self):
        prompt = self._narrate("hitch", {"intent": "hitch", "found": True, "puller": "horse", "vehicle": "cart"})
        self.assertIn("hitches horse to cart", prompt)

    def test_narrate_hitch_explains_each_failure_reason(self):
        for reason, expected_phrase in (
            ("not_present", "two things here"),
            ("target_down", "is down"),
            ("target_hostile", "is hostile"),
            ("not_a_puller", "not something capable of pulling"),
            ("not_a_vehicle", "not something meant to be hitched"),
            ("already_hitched", "already hitched"),
        ):
            with self.subTest(reason=reason):
                prompt = self._narrate("hitch", {
                    "intent": "hitch", "found": False, "reason": reason, "input": "hitch the horse to the cart",
                })
                self.assertIn(expected_phrase, prompt)

    def test_narrate_unhitch_reports_the_real_puller_and_vehicle(self):
        prompt = self._narrate("unhitch", {"intent": "unhitch", "found": True, "puller": "horse", "vehicle": "cart"})
        self.assertIn("unhitches horse from cart", prompt)

    def test_narrate_unhitch_explains_each_failure_reason(self):
        for reason, expected_phrase in (
            ("not_present", "nothing here matches"),
            ("not_hitched", "isn't actually hitched"),
        ):
            with self.subTest(reason=reason):
                prompt = self._narrate("unhitch", {
                    "intent": "unhitch", "found": False, "reason": reason, "input": "unhitch the horse",
                })
                self.assertIn(expected_phrase, prompt)

    def test_narrate_lore_check_reports_the_real_target_and_revealed_tags(self):
        prompt = self._narrate("lore_check", {
            "intent": "lore_check", "found": True, "target": "giant spider", "skill": "survival",
            "revealed": ["fire"],
        })
        self.assertIn("giant spider", prompt)
        self.assertIn("fire", prompt)

    def test_narrate_lore_check_still_grounded_when_nothing_is_revealed(self):
        # A pass with no resistance/immunity/vulnerability/damage_tags authored at all (ex:
        # a plain wolf) must not fabricate traits that were never there.
        prompt = self._narrate("lore_check", {
            "intent": "lore_check", "found": True, "target": "wolf", "skill": "survival", "revealed": [],
        })
        self.assertIn("wolf", prompt)
        self.assertIn("nothing noteworthy", prompt)

    def test_narrate_lore_check_explains_each_failure_reason(self):
        for reason, expected_phrase in (
            ("not_present", "nothing here matches"),
            ("no_lore_available", "nothing comes to mind"),
            ("check_failed", "nothing useful surfaces"),
        ):
            with self.subTest(reason=reason):
                prompt = self._narrate("lore_check", {
                    "intent": "lore_check", "found": False, "reason": reason, "target": "giant spider",
                    "input": "what do you know about the giant spider",
                })
                self.assertIn(expected_phrase, prompt)


class TestFormatCurrency(unittest.TestCase):
    """!@brief Inventory_Resolution.format_currency -- one number spelled out as a setting's coins."""

    PATHFINDER = [
        {"name": "gold piece", "worth": 1},
        {"name": "silver piece", "worth": 0.1},
        {"name": "copper piece", "worth": 0.01},
    ]

    def test_breaks_an_amount_into_coins_largest_first(self):
        for amount, expected in (
            (0.8, "8 silver pieces"),
            (0.04, "4 copper pieces"),
            (1, "1 gold piece"),
            (15, "15 gold pieces"),
            (1.24, "1 gold piece, 2 silver pieces and 4 copper pieces"),
            (2.5, "2 gold pieces and 5 silver pieces"),
            (0.7000000000000001, "7 silver pieces"),
            (0, "0 copper pieces"),
        ):
            with self.subTest(amount=amount):
                self.assertEqual(format_currency(amount, self.PATHFINDER), expected)

    def test_order_and_plural_come_from_the_entries(self):
        coins = [{"name": "penny", "plural": "pence", "worth": 1}, {"name": "shilling", "worth": 12}]
        self.assertEqual(format_currency(13, coins), "1 shilling and 1 penny")
        self.assertEqual(format_currency(2, coins), "2 pence")

    def test_no_denominations_reads_as_plain_coins(self):
        self.assertEqual(format_currency(5), "5 coins")
        self.assertEqual(format_currency(1), "1 coin")
        self.assertEqual(format_currency(0.8), "0.8 coins")


class TestTransferBehavior(DMTestCase):
    """!
    @brief [[entity.behavior]]'s own "steal"/"gift" action (Combat_Actions.py's TRANSFER_ACTIONS/
        _resolve_transfer_behavior) -- an NPC autonomously moving an item or currency, the
        same "theft"/"favor" attitude nudge DM_Inventory.py's player-driven "take"/"give"
        already fires, just entity-initiated. creatures.toml's "pickpocket" is the shipped
        worked example.
    """

    def setUp(self):
        super().setUp()
        self.dm_core.entities["wolf"]["behavior"] = [
            {"requirements": [], "action": "steal", "item": "health potion"},
        ]

    def test_steal_moves_a_named_item_from_target_to_actor(self):
        result = Combat_Actions.resolve_behavior_action(self.dm_core.world, "wolf", "gladstone")

        self.assertIsInstance(result, TransferOutcome)
        self.assertEqual(result.direction, "steal")
        self.assertEqual(result.item_name, "health potion")
        self.assertIn("health potion", self.dm_core.entities["wolf"]["inventory"])
        self.assertEqual(self.dm_core.entities["gladstone"]["inventory"].count("health potion"), 2)

    def test_steal_nudges_the_victims_attitude_toward_the_thief(self):
        # gladstone's own attitudes table (characters.toml) starts at a flat [0, 0, 0] default.
        base_familiarity = self.dm_core.get_attitude("gladstone", "wolf")[2]

        Combat_Actions.resolve_behavior_action(self.dm_core.world, "wolf", "gladstone")

        # health potion's own TOML value against SIGNIFICANT_VALUE (25) -- "theft" fires on
        # gladstone's own attitude *toward* wolf, the thief, not the reverse.
        value = self.dm_core.entities["health potion"]["value"]
        familiarity = self.dm_core.get_attitude("gladstone", "wolf")[2]
        self.assertNotEqual(familiarity, base_familiarity)
        self.assertAlmostEqual(familiarity, 0 + -12 * min(1.0, value / 25))

    def test_gift_moves_a_named_item_from_actor_to_target(self):
        self.dm_core.entities["wolf"]["behavior"] = [
            {"requirements": [], "action": "gift", "item": "longsword"},
        ]
        self.dm_core.entities["wolf"]["inventory"] = ["longsword"]

        result = Combat_Actions.resolve_behavior_action(self.dm_core.world, "wolf", "gladstone")

        self.assertEqual(result.direction, "gift")
        self.assertIn("longsword", self.dm_core.entities["gladstone"]["inventory"])
        self.assertNotIn("longsword", self.dm_core.entities["wolf"]["inventory"])

    def test_steal_is_a_no_op_when_the_target_doesnt_actually_have_the_item(self):
        self.dm_core.entities["wolf"]["behavior"] = [
            {"requirements": [], "action": "steal", "item": "iron dagger"},
        ]
        self.assertIsNone(Combat_Actions.resolve_behavior_action(self.dm_core.world, "wolf", "gladstone"))

    def test_steal_currency_moves_a_capped_amount_via_the_reserved_sentinel(self):
        self.dm_core.entities["wolf"]["behavior"] = [
            {"requirements": [], "action": "steal", "item": "currency", "amount": 10},
        ]
        self.dm_core.entities["gladstone"]["currency"] = 100

        result = Combat_Actions.resolve_behavior_action(self.dm_core.world, "wolf", "gladstone")

        self.assertEqual(result.item_name, "currency")
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 90)
        self.assertEqual(self.dm_core.entities["wolf"]["currency"], 10)

    def test_steal_currency_is_a_no_op_when_the_target_is_broke(self):
        self.dm_core.entities["wolf"]["behavior"] = [
            {"requirements": [], "action": "steal", "item": "currency"},
        ]
        self.dm_core.entities["gladstone"]["currency"] = 0
        self.assertIsNone(Combat_Actions.resolve_behavior_action(self.dm_core.world, "wolf", "gladstone"))

    def test_pickpocket_steals_a_modest_sum_then_flees_once_actually_hit(self):
        [name] = self.dm_core._instance_entities([{"name": "pickpocket", "band": 1}])
        self.dm_core.scenario_entities.append(name)
        self.dm_core.entities["gladstone"]["currency"] = 100

        result = Combat_Actions.resolve_behavior_action(self.dm_core.world, name, "gladstone")
        self.assertIsInstance(result, TransferOutcome)
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 90)

        Combat_Resolution.apply_damage(self.dm_core.world, name, 1)  # any hit at all crosses its own 0.90 threshold
        fled = Combat_Actions.resolve_behavior_action(self.dm_core.world, name, "gladstone")
        self.assertIsInstance(fled, MovementOutcome)
        self.assertEqual(fled.direction, "retreat")


class TestImprovisedContainerPlacement(DMTestCase):
    """!
    @brief An improvised container/trap goes to the FRONT of scenario_entities. Found by playtest:
        a reload appended it to the end instead (save -> load -> save drifted), and as first in
        line it became the default listener for unaddressed trash talk.
    """

    def _place_crate(self):
        crate = {"name": "Crate of Fish", "description": "A crate of fish.", "supertype": "object",
                 "subtype": "container", "ad_hoc": True, "inventory": []}
        self.dm_core._place_and_register_scene_entity("Crate of Fish", crate, insert_front=True, claim_target=False)

    def test_scene_order_survives_save_and_load(self):
        slot = "test_container_order_round_trip"
        self.addCleanup(shutil.rmtree, os.path.join("Saves", slot), ignore_errors=True)
        self._place_crate()
        before = list(self.dm_core.scenario_entities)

        self.dm_core.save_game(slot)
        self.dm_core.load_game(slot)

        self.assertEqual(self.dm_core.scenario_entities, before)

    def test_an_object_is_never_the_default_listener(self):
        self._place_crate()
        self.assertNotEqual(self.dm_core._resolve_dialogue_target("you'll regret that"), "Crate of Fish")

    def test_the_default_listener_is_someone_who_understands_the_player(self):
        # Found by playtest: the first person in the scene spoke only another tongue, so 80
        # turns of unnamed talk came back as gibberish while others who shared it stood by.
        first, second = [name for name in self.dm_core.scenario_entities if not self.dm_core._is_party_member(name)][:2]
        self.dm_core.entities[first]["languages"] = ["dwarvish"]
        self.assertEqual(self.dm_core._resolve_dialogue_target("nice weather"), second)


class TestLockedChest(DMTestCase):
    # Rules/Fantasy/scenarios/debug.toml puts the player alone with a locked chest
    # (items.toml's "chest": [entity.test] {difficulty=12, skill=["finesse"]}, starting
    # condition "locked").
    scenario_name = "debug"
    start_location = "cellar"

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
    # debug.toml's chest carries a "cursed dagger" plus currency=20, for exercising
    # examine (read-only) vs take (transfers) without any dice roll involved.
    scenario_name = "debug"
    start_location = "cellar"

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
        Combat_Resolution.apply_condition(self.dm_core.world, "cursed dagger", "identified", duration="permanent", dismiss="")

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
        self.assertEqual(result["description"], "20 coins")
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
    scenario_name = "debug"
    start_location = "cellar"

    def setUp(self):
        super().setUp()
        self.action_events = self._capture("action_resolved")
        self.round_events = self._capture("round_resolved")
        Combat_Resolution.dismiss_condition(self.dm_core.world, "chest", "locked")
        Combat_Resolution.dismiss_condition(self.dm_core.world, "chest", "closed")

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
        self.assertTrue(Combat_Actions.is_identified(self.dm_core.world, "cursed dagger"))


class TestOpenClose(DMTestCase):
    scenario_name = "debug"
    start_location = "cellar"

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


class TestBulk(DMTestCase):
    # debug.toml's chest (cursed dagger, bulk 1) -- same fixture TestItemInteraction/
    # TestOpenClose already use for take/open.
    scenario_name = "debug"
    start_location = "cellar"

    def setUp(self):
        super().setUp()
        self.resolved = self._capture("item_interaction_resolved")

    def _unlock_and_open_the_chest(self):
        self._stub_roll_dice(99)
        self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "finesse"}], "input": "I pick the lock"})
        self.dm_core._on_item_interaction_detected({
            "intent": "open", "item_name": None, "input": "I open the chest",
        })

    def _pad_gladstones_bulk_to_the_cap(self):
        # Cheaper than accumulating real loot -- a single throwaway heavy item pushes
        # gladstone's own get_current_bulk straight to max_bulk (7 -- see
        # test_get_max_bulk_is_min_bulk_plus_strength_dice_times_mod_multiplier below), so the
        # very next "take"/"trade" has zero room left regardless of the item's own bulk.
        self.dm_core.entities["anvil"] = {"name": "anvil", "supertype": "object", "description": "A heavy anvil.", "bulk": 7}
        self.dm_core.entities["gladstone"]["inventory"].append("anvil")

    def test_get_max_bulk_is_min_bulk_plus_strength_dice_times_mod_multiplier(self):
        # Fantasy's own rules.toml [bulk] table: min_bulk = 3, mod_multiplier = 2 -- gladstone's
        # own strength is 2D (characters.toml), so 3 + 2*2 = 7.
        self.assertEqual(Conveyance.max_bulk(self.dm_core.world, "gladstone"), 7)

    def test_get_current_bulk_sums_the_inventorys_own_bulk_fields(self):
        # longsword(1) + chain mail(1) + 3x health potion(0 each) + iron filings(0) = 2.
        self.assertEqual(Conveyance.current_bulk(self.dm_core.world, "gladstone"), 2)

    def test_take_is_denied_once_it_would_exceed_max_bulk(self):
        self._pad_gladstones_bulk_to_the_cap()
        self._unlock_and_open_the_chest()

        self.dm_core._on_item_interaction_detected({
            "intent": "take", "item_name": "cursed dagger", "input": "I take the cursed dagger",
        })

        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "bulk_exceeded")
        self.assertNotIn("cursed dagger", self.dm_core.entities["gladstone"]["inventory"])
        self.assertIn("cursed dagger", self.dm_core.entities["chest"]["inventory"])

    def test_dropping_an_item_frees_capacity_for_a_later_take(self):
        self._pad_gladstones_bulk_to_the_cap()
        self._unlock_and_open_the_chest()

        self.dm_core._on_item_interaction_detected({
            "intent": "drop", "item_name": "anvil", "input": "I drop the anvil",
        })
        self.dm_core._on_item_interaction_detected({
            "intent": "take", "item_name": "cursed dagger", "input": "I take the cursed dagger",
        })

        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertIn("cursed dagger", self.dm_core.entities["gladstone"]["inventory"])

    def test_trade_is_denied_once_it_would_exceed_max_bulk_and_charges_no_currency(self):
        # debug.toml's chest doubles as a "shop" -- same reuse
        # test_trade_charges_the_items_toml_value (TestGiveAndTrade) relies on, just against the
        # scenario's own already-instanced chest rather than a fresh ad hoc one (a second ad hoc
        # "chest" would collide with this class's own scenario_name = "dungeon" load in setUp
        # and get disambiguated to "chest_2" -- see _instance_entities' own docstring).
        self._pad_gladstones_bulk_to_the_cap()
        self._unlock_and_open_the_chest()
        starting_currency = self.dm_core.entities["gladstone"]["currency"]

        self.dm_core._on_item_interaction_detected({
            "intent": "trade", "item_name": "cursed dagger", "input": "I buy the cursed dagger",
        })

        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "bulk_exceeded")
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], starting_currency)
        self.assertNotIn("cursed dagger", self.dm_core.entities["gladstone"]["inventory"])

    def test_get_max_bulk_returns_none_when_the_setting_authors_no_bulk_rule(self):
        del self.dm_core.rules["bulk"]
        self.assertIsNone(Conveyance.max_bulk(self.dm_core.world, "gladstone"))

    def test_take_is_never_denied_when_the_setting_authors_no_bulk_rule(self):
        del self.dm_core.rules["bulk"]
        self.dm_core.entities["anvil"] = {"name": "anvil", "supertype": "object", "description": "A heavy anvil.", "bulk": 999}
        self.dm_core.entities["gladstone"]["inventory"].append("anvil")
        self._unlock_and_open_the_chest()

        self.dm_core._on_item_interaction_detected({
            "intent": "take", "item_name": "cursed dagger", "input": "I take the cursed dagger",
        })

        result = self.resolved[-1]
        self.assertTrue(result["found"])

    def _add_horse(self):
        [name] = self.dm_core._instance_entities([{"name": "horse", "band": 1}])
        self.dm_core.scenario_entities.append(name)
        return name

    def test_get_carrying_capacity_is_max_bulk_for_a_leaf_provider(self):
        self._add_horse()
        # creatures.toml's own horse: strength 4D -> Fantasy's [bulk] formula, 3 + 4*2 = 11.
        self.assertEqual(Conveyance.carrying_capacity(self.dm_core.world, "horse"), 11)

    def test_get_carrying_capacity_sums_a_carts_own_pulling_team(self):
        first = self._add_horse()
        second = self._add_horse()
        self.dm_core.entities["cart"] = {
            "name": "cart", "supertype": "object", "description": "A rickety cart.",
            "max_hp": 20, "mount": [first, second],
        }
        self.dm_core.scenario_entities.append("cart")

        self.assertEqual(Conveyance.carrying_capacity(self.dm_core.world, "cart"), 22)  # 11 + 11

    def test_get_carrying_capacity_drops_a_dead_puller_from_the_sum(self):
        first = self._add_horse()
        second = self._add_horse()
        Combat_Resolution.apply_damage(self.dm_core.world, second, 999)
        self.dm_core.entities["cart"] = {
            "name": "cart", "supertype": "object", "description": "A rickety cart.",
            "max_hp": 20, "mount": [first, second],
        }
        self.dm_core.scenario_entities.append("cart")

        self.assertEqual(Conveyance.carrying_capacity(self.dm_core.world, "cart"), 11)  # only the live one

    def test_get_current_bulk_folds_in_a_mounted_riders_own_body_and_gear(self):
        self._add_horse()
        self.dm_core.entities["gladstone"]["mount"] = "horse"
        self.dm_core.entities["gladstone"]["bulk"] = 5  # body weight as cargo

        # gladstone's own gear (longsword + chain mail = 2, see
        # test_get_current_bulk_sums_the_inventorys_own_bulk_fields) counts too by default
        # (rules.toml's own [bulk] table: count_rider_gear = true).
        self.assertEqual(Conveyance.current_bulk(self.dm_core.world, "horse"), 5 + 2)

    def test_get_current_bulk_excludes_rider_gear_when_count_rider_gear_is_false(self):
        self.dm_core.rules["bulk"]["count_rider_gear"] = False
        self._add_horse()
        self.dm_core.entities["gladstone"]["mount"] = "horse"
        self.dm_core.entities["gladstone"]["bulk"] = 5

        self.assertEqual(Conveyance.current_bulk(self.dm_core.world, "horse"), 5)

    def test_would_exceed_mount_capacity_true_once_a_riders_own_load_overflows_it(self):
        self._add_horse()
        self.dm_core.entities["horse"]["max_bulk"] = 1
        self.assertTrue(Conveyance.would_exceed_capacity(self.dm_core.world, "horse", "gladstone"))

    def test_would_exceed_mount_capacity_false_when_uncapped(self):
        self._add_horse()
        del self.dm_core.rules["bulk"]  # horse authors no "max_bulk" override of its own
        self.assertFalse(Conveyance.would_exceed_capacity(self.dm_core.world, "horse", "gladstone"))


class TestGiveAndTrade(DMTestCase):
    # debug.toml's innkeeper -- a living recipient, unlike the dungeon's chest, so give
    # actually has somewhere sensible to go.
    scenario_name = "debug"
    start_location = "tavern_floor"

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
        # docs/social-dialogue.md's "Social and attitudes"); rules.toml's own "favor"
        # [[attitude_event]] restores roughly that lost weight into disposition/familiarity
        # instead, mirroring "theft"'s own magnitude.
        base_familiarity = self.dm_core.entities["innkeeper"]["attitudes"]["default"][2]

        self.dm_core._on_item_interaction_detected({
            "intent": "give", "item_name": "health potion", "input": "I give the innkeeper a health potion",
        })

        familiarity = self.dm_core.get_attitude("innkeeper", self.dm_core.player_name)[2]
        self.assertAlmostEqual(familiarity, base_familiarity + 12 * (15 / 25))

    def test_taking_currency_from_a_living_entity_nudges_a_theft_attitude(self):
        # "theft" (same wiring, currency branch) -- magnitude scaled by however much moved
        # against SIGNIFICANT_VALUE, capped at 1.0 (the innkeeper's own 40 currency exceeds it).
        # Stealing reads as reduced closeness now, not reduced trust -- trust was dropped as an
        # axis entirely (see docs/social-dialogue.md's "Social and attitudes").
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
        Combat_Resolution.apply_damage(self.dm_core.world, "innkeeper", 9999)

        self.dm_core._on_item_interaction_detected({
            "intent": "take", "item_name": "cursed dagger", "input": "I take the innkeeper's cursed dagger",
        })

        self.assertIn("cursed dagger", self.dm_core.entities["gladstone"]["inventory"])
        self.assertNotIn("action_attitude_deltas", self.dm_core.entities["innkeeper"])


    def test_trade_charges_the_items_toml_value_and_moves_it_to_the_player(self):
        # debug.toml's chest holds "cursed dagger" (value = 5); tavern's innkeeper has
        # neither, so build an ad-hoc scenario reusing the chest as a "shop" for this test.
        self._load_ad_hoc_scenario([{"name": "gladstone", "band": 1}, {"name": "chest", "band": 1}])
        Combat_Resolution.dismiss_condition(self.dm_core.world, "chest", "locked")
        Combat_Resolution.dismiss_condition(self.dm_core.world, "chest", "closed")
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
        # a second, orphaned "gladstone_2" instance -- see debug.toml's own real-scenario
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
        Combat_Resolution.dismiss_condition(self.dm_core.world, "chest", "locked")
        Combat_Resolution.dismiss_condition(self.dm_core.world, "chest", "closed")
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
        Combat_Resolution.apply_damage(self.dm_core.world, "gladstone", 20)  # 36 -> 16
        starting_count = self.dm_core.entities["gladstone"]["inventory"].count("health potion")

        # roll_dice is stubbed to return roll_result directly (same convention
        # TestItemTargetedSkillCheck's own _check_the_dagger mock already uses), so this is
        # the healing roll's total, not per-die.
        result = self._use(roll_result=6)

        self.assertTrue(result["found"])
        self.assertEqual(result["healed"], 6)
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone"), 22)
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
        starting_hp = Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone")

        result = self._use(item_name="nasty brew", roll_result=7)

        self.assertTrue(result["found"])
        self.assertEqual(result["healed"], 0)
        self.assertEqual(result["poisoned"], 7)
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone"), starting_hp - 7)
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
        starting_hp = Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone")

        result = self._use(item_name="toxic vial", roll_result=10)

        self.assertEqual(result["poisoned"], 0)
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone"), starting_hp)


class TestCrafting(DMTestCase):
    # items.toml's "iron dagger" ([entity.craft]: skill=["strength","finesse"], difficulty=10,
    # requires_station="forge", materials=2x iron ingot + 1x leather strip) is globally loaded
    # (items.toml, not scenario-local) regardless of scenario_name -- "arena" (DMTestCase's own
    # default) is fine; only the "forge" station itself (debug.toml-local) needs manually placing.
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
        original_resolve_action = Combat_Resolution.resolve_action

        def spy_resolve_action(ctx, entity_name, skill_name, difficulty=0, dice_penalty=0, skill_divisor=1):
            seen_dice_penalties.append(dice_penalty)
            return original_resolve_action(ctx, entity_name, skill_name, difficulty, dice_penalty=dice_penalty)

        patcher = patch.object(Combat_Resolution, "resolve_action", spy_resolve_action)
        patcher.start()
        self.addCleanup(patcher.stop)

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
    scenario_name = "debug"
    start_location = "cellar"


    def test_loot_entity_moves_currency_and_every_inventory_item(self):
        # Give the chest some items too, not just currency, to exercise the full sweep.
        self.dm_core.entities["chest"]["inventory"] = ["health potion", "health potion"]

        self.dm_core.loot_entity("chest", "gladstone")

        self.assertEqual(self.dm_core.entities["chest"]["currency"], 0)
        self.assertEqual(self.dm_core.entities["chest"].get("inventory"), [])
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 120)
        self.assertEqual(self.dm_core.entities["gladstone"]["inventory"].count("health potion"), 5)


class TestContainerRestrictions(DMTestCase):
    """!
    @brief container_capacity/container_allowed_supertypes/container_allowed_subtypes
        (Inventory_Resolution.py's get_container_rejection_reason) -- the shared gate
        transfer_item/place_new_item both check before moving anything into a container's own
        "inventory", so every existing mover (give/take/trade, loot_entity, ADaM placement)
        respects it uniformly, not just player-typed commands. items.toml's "spellbook"
        (container_allowed_supertypes) and "bag of holding" (container_capacity) are the
        shipped worked examples this class exercises directly.
    """

    def test_transfer_item_refuses_the_wrong_supertype(self):
        self.dm_core.entities["gladstone"]["inventory"].append("iron dagger")

        moved = self.dm_core.transfer_item("gladstone", "spellbook", "iron dagger")

        self.assertFalse(moved)
        self.assertIn("iron dagger", self.dm_core.entities["gladstone"]["inventory"])
        self.assertNotIn("iron dagger", self.dm_core.entities["spellbook"]["inventory"])

    def test_transfer_item_allows_a_matching_supertype(self):
        self.dm_core.entities["gladstone"]["inventory"].append("suggestion")

        moved = self.dm_core.transfer_item("gladstone", "spellbook", "suggestion")

        self.assertTrue(moved)
        self.assertIn("suggestion", self.dm_core.entities["spellbook"]["inventory"])
        self.assertNotIn("suggestion", self.dm_core.entities["gladstone"]["inventory"])

    def test_transfer_item_refuses_once_container_capacity_would_be_exceeded(self):
        # "bag of holding" starts with one health potion (bulk 0) and container_capacity = 40.
        self.dm_core.entities["anvil"] = {
            "name": "anvil", "supertype": "object", "description": "A heavy anvil.", "bulk": 41,
        }
        self.dm_core.entities["gladstone"]["inventory"].append("anvil")

        moved = self.dm_core.transfer_item("gladstone", "bag of holding", "anvil")

        self.assertFalse(moved)
        self.assertIn("anvil", self.dm_core.entities["gladstone"]["inventory"])
        self.assertNotIn("anvil", self.dm_core.entities["bag of holding"]["inventory"])

    def test_transfer_item_allows_up_to_the_containers_own_capacity(self):
        self.dm_core.entities["anvil"] = {
            "name": "anvil", "supertype": "object", "description": "A heavy anvil.", "bulk": 40,
        }
        self.dm_core.entities["gladstone"]["inventory"].append("anvil")

        moved = self.dm_core.transfer_item("gladstone", "bag of holding", "anvil")

        self.assertTrue(moved)
        self.assertIn("anvil", self.dm_core.entities["bag of holding"]["inventory"])

    def test_place_new_item_returns_false_and_places_nothing_when_refused(self):
        placed = self.dm_core.place_new_item("spellbook", "iron dagger")

        self.assertFalse(placed)
        self.assertNotIn("iron dagger", self.dm_core.entities["spellbook"].get("inventory", []))

    def test_place_new_item_returns_true_and_places_the_item_when_allowed(self):
        placed = self.dm_core.place_new_item("spellbook", "arc lance")

        self.assertTrue(placed)
        self.assertIn("arc lance", self.dm_core.entities["spellbook"]["inventory"])

    def test_loot_entity_leaves_a_refused_item_with_the_source(self):
        # The restriction gates what a container may *receive*, not what it gives up -- so this
        # exercises loot_entity moving *into* a restricted container (a spellbook stocking
        # itself from a mixed pile) rather than out of one. An item a restricted destination
        # refuses stays behind with the source rather than vanishing -- transfer_item's own
        # False return is what loot_entity already checks.
        self.dm_core.entities["loose pile"] = {
            "name": "loose pile", "supertype": "object", "description": "A loose pile of things.",
            "currency": 5, "inventory": ["suggestion", "iron dagger"],
        }

        summary = self.dm_core.loot_entity("loose pile", "spellbook")

        self.assertEqual(summary["currency"], 5)
        self.assertIn("suggestion", summary["items"])
        self.assertNotIn("iron dagger", summary["items"])
        self.assertIn("iron dagger", self.dm_core.entities["loose pile"]["inventory"])
        self.assertIn("suggestion", self.dm_core.entities["spellbook"]["inventory"])

    def test_containers_own_nested_bulk_never_counts_against_the_carrier(self):
        self.dm_core.entities["gladstone"]["inventory"].append("bag of holding")
        before = Conveyance.current_bulk(self.dm_core.world, "gladstone")

        # Filling the bag right up to its own container_capacity (40) still only ever costs
        # gladstone the bag's own flat "bulk" (2) -- get_current_bulk never recurses into an
        # item's own nested "inventory".
        self.dm_core.entities["anvil"] = {
            "name": "anvil", "supertype": "object", "description": "A heavy anvil.", "bulk": 40,
        }
        self.dm_core.entities["bag of holding"]["inventory"].append("anvil")

        self.assertEqual(Conveyance.current_bulk(self.dm_core.world, "gladstone"), before)

    def test_give_denies_wrong_item_type_without_moving_anything(self):
        self._load_ad_hoc_scenario([{"name": "gladstone", "band": 1}, {"name": "spellbook", "band": 1}])
        resolved = self._capture("item_interaction_resolved")
        self.dm_core.entities["gladstone"]["inventory"].append("iron dagger")

        self.dm_core._on_item_interaction_detected({
            "intent": "give", "item_name": "iron dagger", "input": "I put the iron dagger in the spellbook",
        })

        result = resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "wrong_item_type")
        self.assertIn("iron dagger", self.dm_core.entities["gladstone"]["inventory"])

    def test_give_allows_a_matching_supertype(self):
        self._load_ad_hoc_scenario([{"name": "gladstone", "band": 1}, {"name": "spellbook", "band": 1}])
        resolved = self._capture("item_interaction_resolved")
        self.dm_core.entities["gladstone"]["inventory"].append("suggestion")

        self.dm_core._on_item_interaction_detected({
            "intent": "give", "item_name": "suggestion", "input": "I put the suggestion scroll in the spellbook",
        })

        result = resolved[-1]
        self.assertTrue(result["found"])
        self.assertIn("suggestion", self.dm_core.entities["spellbook"]["inventory"])

    def test_trade_denies_a_refused_destination_before_charging_any_currency(self):
        # No shipped container is ever a "trade"/"take" destination (both always move toward
        # the player) -- this exercises the same gate against the player entity itself, future-
        # proofing for a setting that authors container fields on the player. "spellbook" (no
        # locked/closed conditions of its own, unlike "chest") stands in as an ordinary,
        # reachable seller here -- what it's selling is beside the point. Set container_
        # allowed_supertypes *after* loading -- load_scenario is always a fresh re-instancing
        # (see its own docstring), so anything set on "gladstone" beforehand would just be
        # discarded.
        self._load_ad_hoc_scenario([{"name": "gladstone", "band": 1}, {"name": "spellbook", "band": 1}])
        self.dm_core.entities["gladstone"]["container_allowed_supertypes"] = ["spell"]
        resolved = self._capture("item_interaction_resolved")
        self.dm_core.entities["spellbook"]["inventory"].append("iron dagger")
        starting_currency = self.dm_core.entities["gladstone"]["currency"]

        self.dm_core._on_item_interaction_detected({
            "intent": "trade", "item_name": "iron dagger", "input": "I buy the iron dagger",
        })

        result = resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "wrong_item_type")
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], starting_currency)
        self.assertNotIn("iron dagger", self.dm_core.entities["gladstone"]["inventory"])


class TestEquipSlots(DMTestCase):
    def test_get_equip_slots_prefers_subtype_match_over_supertype_only_entry(self):
        self.dm_core.rules["equip_slot"] = [
            {"supertype": "creature", "slots": ["default_slot"]},
            {"supertype": "creature", "subtype": "humanoid", "slots": ["rhand", "chest"]},
        ]
        self.assertEqual(self.dm_core.get_equip_slots("gladstone"), ["rhand", "chest"])


class TestRoomMoveCarriesItsDirection(DMTestCase):
    """!
    @brief The room-move narrator reads "direction" ("The player heads forward, arriving at ...");
        found by the event contract: nothing used to send it, so every move read "heads onward".
    """
    scenario_name = "debug"
    start_location = "crypt"

    def test_a_move_publishes_the_direction_it_tried(self):
        resolved = self._capture("item_interaction_resolved")
        self.dm_core._on_item_interaction_detected({"intent": "move", "item_name": None, "direction": "forward", "input": "go forward"})

        # Hostiles in the entrance block the way (found is False) -- the direction rides along either way.
        self.assertEqual(resolved[-1]["direction"], "forward")

    def test_a_refused_move_carries_it_too(self):
        resolved = self._capture("item_interaction_resolved")
        self.dm_core._on_item_interaction_detected({"intent": "move", "item_name": None, "direction": "left", "input": "go left"})

        self.assertFalse(resolved[-1]["found"])
        self.assertEqual(resolved[-1]["direction"], "left")


COMMON_OUTCOME_KEYS = {"intent", "item_name", "input", "found", "present_entities", "quiet", "phrase"}


class TestItemInteractionOutcome(unittest.TestCase):
    """!@brief resolution/Item_Outcome.py -- the payload every item interaction outcome shares."""

    def test_every_outcome_carries_the_common_fields(self):
        outcome = build_item_interaction_outcome("take", "dagger", "take the dagger", True, ["gladstone"])

        self.assertEqual(set(outcome), COMMON_OUTCOME_KEYS)
        self.assertEqual(outcome["quiet"], False)
        self.assertIsNone(outcome["phrase"])

    def test_the_intents_own_fields_ride_along_and_win_over_a_common_one(self):
        outcome = build_item_interaction_outcome(
            "take", "dagger", "take it", False, [], reason="locked", container="chest", input="what the intent saw",
        )

        self.assertEqual(outcome["reason"], "locked")
        self.assertEqual(outcome["container"], "chest")
        self.assertEqual(outcome["input"], "what the intent saw")

    def test_amounts_are_spelled_in_the_settings_own_coins(self):
        outcome = build_item_interaction_outcome(
            "trade", "bread", "buy bread", True, [], format_currency=lambda amount: f"{amount} coins", price=3, amount=2,
        )

        self.assertEqual((outcome["price_text"], outcome["amount_text"]), ("3 coins", "2 coins"))
        bare = build_item_interaction_outcome("trade", "bread", "buy bread", True, [], format_currency=str)
        self.assertNotIn("price_text", bare)

    def test_the_roster_is_a_snapshot_not_the_live_list(self):
        roster = ["gladstone"]
        outcome = build_item_interaction_outcome("move", None, "go", True, roster)
        roster.append("wolf")

        self.assertEqual(outcome["present_entities"], ["gladstone"])


class TestItemInteractionPublisher(DMTestCase):
    """!@brief DMCore._publish_item_interaction -- the one place every path an intent resolves by ends."""

    def setUp(self):
        super().setUp()
        self.resolved = self._capture("item_interaction_resolved")

    def test_a_direct_turn_carries_the_common_fields(self):
        self.dm_core._on_item_interaction_detected({
            "intent": "examine", "item_name": None, "input": "examine it", "phrase": "it", "quiet": True,
        })

        self.assertTrue(COMMON_OUTCOME_KEYS <= set(self.resolved[-1]))
        self.assertEqual((self.resolved[-1]["quiet"], self.resolved[-1]["phrase"]), (True, "it"))

    def test_an_improvised_scenery_beat_carries_the_common_fields(self):
        fake_result = {"created": False, "scenery": True, "description": "Claw marks score the stone."}

        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value=fake_result):
            self.dm_core._on_improvisation_requested({
                "intent": "examine", "phrase": "the wall", "input": "examine the wall",
            })

        self.assertTrue(COMMON_OUTCOME_KEYS <= set(self.resolved[-1]))
        self.assertEqual(self.resolved[-1]["description"], "Claw marks score the stone.")

    def test_a_resumed_downtime_carries_the_common_fields(self):
        self.dm_core.pending_downtime = {"kind": "rest"}

        with patch.object(self.dm_core, "_advance_pending_rest", return_value={"interrupted": False, "blocks_spent": 2}):
            self.dm_core._resume_pending_downtime()

        self.assertTrue(COMMON_OUTCOME_KEYS <= set(self.resolved[-1]))
        self.assertEqual((self.resolved[-1]["intent"], self.resolved[-1]["blocks_spent"]), ("rest", 2))

    def test_the_party_panel_refreshes_after_every_outcome(self):
        refreshed = self._capture("party_status_changed")

        self.dm_core._on_item_interaction_detected({"intent": "examine", "item_name": None, "input": "examine it"})

        self.assertEqual(len(refreshed), 1)
        self.assertEqual(len(self.resolved), 1)

    def test_nothing_else_publishes_the_event(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        publishers = [
            path.relative_to(root).as_posix()
            for folder in ("dm", "intents", "resolution", "llm", "nlp")
            for path in (root / folder).glob("*.py")
            if 'publish("item_interaction_resolved"' in path.read_text(encoding="utf-8")
        ]

        self.assertEqual(publishers, ["dm/DM_Core.py"])


if __name__ == "__main__":
    unittest.main()
