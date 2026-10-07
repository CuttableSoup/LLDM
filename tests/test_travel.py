import os
import shutil
import tomllib
import unittest
from unittest.mock import patch
import dm.DM_Encounters as DM_Encounters
import resolution.Calendar as Calendar
import resolution.Combat_Resolution as Combat_Resolution
import resolution.World_Map as World_Map
from resolution.Inventory_Resolution import format_currency
from dm.DM_ActionOutcome import DamageEffect
from dm.DM_Core import DMCore
from dm.DM_Travel import ROAD_ENCOUNTER_KEY
from tests.event_contract import ValidatingEventBus
from nlp.Intent_Classification import detect_item_intent
from llm.LLM_Core import LLMCore
import resolution.Conveyance as Conveyance
from tests.support import (
    DMTestCase,
    scripted_llm,
)
# Module-level hooks start the shared patches (tests/support.py) for every test in this file.
from tests.support import setUpModule, tearDownModule  # noqa: F401


class TestMount(DMTestCase):
    # arena: bands=4, enclosed=true, gladstone/wolf/wolf_2 all start band 1, current_target
    # is "wolf" -- wolf is hostile by default (no [entity.attitudes] table of its own).

    def setUp(self):
        super().setUp()
        self.resolved = self._capture("item_interaction_resolved")

    def _add_horse(self, band=1):
        [name] = self.dm_core._instance_entities([{"name": "horse", "band": band}])
        self.dm_core.scenario_entities.append(name)
        return name

    def test_mount_sets_the_players_own_mount_field(self):
        self._add_horse()
        self.dm_core._on_item_interaction_detected({
            "intent": "mount", "item_name": None, "input": "i mount the horse",
        })
        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["target"], "horse")
        self.assertEqual(self.dm_core.entities["gladstone"]["mount"], "horse")

    def test_mount_snaps_the_players_band_to_the_mounts_own_band(self):
        self._add_horse(band=3)
        self.dm_core.entities["gladstone"]["band"] = 1

        self.dm_core._on_item_interaction_detected({
            "intent": "mount", "item_name": None, "input": "i mount the horse",
        })

        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "gladstone"), 3)

    def test_mount_denied_when_no_present_entity_is_named(self):
        self.dm_core._on_item_interaction_detected({
            "intent": "mount", "item_name": None, "input": "i mount the nonexistent thing",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_present")

    def test_mount_denied_against_a_downed_target(self):
        self._add_horse()
        Combat_Resolution.apply_damage(self.dm_core.world, "horse", 999)

        self.dm_core._on_item_interaction_detected({
            "intent": "mount", "item_name": None, "input": "i mount the horse",
        })

        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "target_down")

    def test_mount_denied_against_a_hostile_target(self):
        # wolf is hostile by default -- can't just climb onto something trying to kill you.
        self.dm_core._on_item_interaction_detected({
            "intent": "mount", "item_name": None, "input": "i mount the wolf",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "target_hostile")

    def test_mount_denied_against_a_non_conveyance_target(self):
        # thane -- arena's own friendly ally -- authors no travel_speed and has no live "mount"
        # chain of his own, so he's present/alive/non-hostile and still not a valid mount:
        # nothing should let the fiction imply climbing onto an ordinary person.
        self.dm_core._on_item_interaction_detected({
            "intent": "mount", "item_name": None, "input": "i mount thane",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_a_mount")
        self.assertNotIn("mount", self.dm_core.entities["gladstone"])

    def test_mount_denied_once_already_mounted(self):
        self._add_horse()
        self.dm_core.entities["gladstone"]["mount"] = "horse"

        self.dm_core._on_item_interaction_detected({
            "intent": "mount", "item_name": None, "input": "i mount the horse",
        })

        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "already_mounted")

    def test_mount_ignores_a_stale_reference_to_a_since_dead_mount(self):
        # A previous mount died mid-scene without an explicit "dismount" -- shouldn't block a
        # fresh mount attempt (see entity_schema.toml's own "mount" comment: losing a mount,
        # by any means, just unwinds the relationship, no bespoke penalty or lingering block).
        self._add_horse()
        self.dm_core.entities["gladstone"]["mount"] = "horse"
        Combat_Resolution.apply_damage(self.dm_core.world, "horse", 999)
        [name] = self.dm_core._instance_entities([{"name": "horse", "band": 1}])
        self.dm_core.scenario_entities.append(name)

        self.dm_core._on_item_interaction_detected({
            "intent": "mount", "item_name": None, "input": f"i mount the {name}",
        })

        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertEqual(self.dm_core.entities["gladstone"]["mount"], name)

    def test_mount_denied_when_it_would_exceed_the_mounts_own_carrying_capacity(self):
        # gladstone's own carried gear (longsword + chain mail) is 2 bulk -- capping the horse
        # below that denies the mount even though the horse's own body contributes nothing.
        self._add_horse()
        self.dm_core.entities["horse"]["max_bulk"] = 1

        self.dm_core._on_item_interaction_detected({
            "intent": "mount", "item_name": None, "input": "i mount the horse",
        })

        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "bulk_exceeded")
        self.assertNotIn("mount", self.dm_core.entities["gladstone"])

    def test_dismount_clears_the_mount_field(self):
        self._add_horse()
        self.dm_core.entities["gladstone"]["mount"] = "horse"

        self.dm_core._on_item_interaction_detected({
            "intent": "dismount", "item_name": None, "input": "i dismount",
        })

        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["target"], "horse")
        self.assertNotIn("mount", self.dm_core.entities["gladstone"])

    def test_dismount_denied_when_not_mounted(self):
        self.dm_core._on_item_interaction_detected({
            "intent": "dismount", "item_name": None, "input": "i dismount",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_mounted")

    def test_advance_carries_the_mounted_players_own_horse_along(self):
        self._add_horse(band=1)
        self.dm_core.entities["gladstone"]["mount"] = "horse"
        self.dm_core.entities["gladstone"]["band"] = 1
        self.dm_core.entities["wolf"]["band"] = 4

        self.dm_core.advance_or_retreat("advance")

        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "gladstone"), 2)
        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "horse"), 2)  # dragged along, no separate check

    def test_a_mounts_own_retreat_behavior_carries_its_rider_along(self):
        # The reverse direction from advance_or_retreat: the horse moves under its own
        # initiative (move_toward_or_away, the same primitive its "retreat" [[entity.behavior]]
        # entry uses), and the player -- currently mounted on it -- comes along too.
        self._add_horse(band=2)
        self.dm_core.entities["gladstone"]["mount"] = "horse"
        self.dm_core.entities["gladstone"]["band"] = 2
        self.dm_core.entities["wolf"]["band"] = 1

        self.dm_core.move_toward_or_away("horse", "wolf", "retreat")

        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "horse"), 3)
        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "gladstone"), 3)

    def test_mount_round_trips_through_save_and_load(self):
        slot_name = "test_mount_round_trip_slot"
        self.addCleanup(shutil.rmtree, self.dm_core._save_slot_dir(slot_name), ignore_errors=True)
        self._add_horse()
        self.dm_core.entities["gladstone"]["mount"] = "horse"

        self.dm_core.save_game(slot_name)
        self.dm_core.entities["gladstone"]["mount"] = None  # prove load actually restores it
        self.dm_core.load_game(slot_name)

        self.assertEqual(self.dm_core.entities["gladstone"].get("mount"), "horse")

    def test_advance_is_denied_while_the_mount_is_overloaded(self):
        self._add_horse()
        self.dm_core.entities["horse"]["max_bulk"] = 0  # gladstone's own gear alone overflows it
        self.dm_core.entities["gladstone"]["mount"] = "horse"
        self.dm_core.entities["gladstone"]["band"] = 1
        self.dm_core.entities["wolf"]["band"] = 4

        self.dm_core._on_item_interaction_detected({
            "intent": "advance", "item_name": None, "input": "i advance",
        })

        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "mount_overloaded")
        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "gladstone"), 1)  # never moved

    def test_advance_is_allowed_again_once_the_overload_clears(self):
        self._add_horse()
        self.dm_core.entities["horse"]["max_bulk"] = 0
        self.dm_core.entities["gladstone"]["mount"] = "horse"
        self.dm_core.entities["gladstone"]["band"] = 1
        self.dm_core.entities["wolf"]["band"] = 4

        self.dm_core.entities["horse"]["max_bulk"] = 100  # dropped the cargo, room to move again
        self.dm_core._on_item_interaction_detected({
            "intent": "advance", "item_name": None, "input": "i advance",
        })

        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "gladstone"), 2)


class TestHitch(DMTestCase):
    # arena: bands=4, enclosed=true, gladstone/wolf/wolf_2 all start band 1 -- wolf is
    # hostile by default (no [entity.attitudes] table of its own).

    def setUp(self):
        super().setUp()
        self.resolved = self._capture("item_interaction_resolved")

    def _add_horse(self, band=1):
        [name] = self.dm_core._instance_entities([{"name": "horse", "band": band}])
        self.dm_core.scenario_entities.append(name)
        return name

    def _add_cart(self):
        # mount = "" -- the shipped placeholder convention (entity_schema.toml) for a template
        # meant to serve as a vehicle: present as a key, resolving to no live entity yet, so
        # _resolve_hitch_intent's own "not_a_vehicle" eligibility gate (an ordinary NPC nothing
        # ever declared hitchable) doesn't also reject a legitimate, not-yet-hitched cart.
        self.dm_core.entities["cart"] = {
            "name": "cart", "supertype": "object", "description": "A rickety cart.", "max_hp": 20,
            "mount": "",
        }
        self.dm_core.scenario_entities.append("cart")

    def test_hitch_promotes_an_absent_mount_field_to_a_bare_string(self):
        self._add_horse()
        self._add_cart()

        self.dm_core._on_item_interaction_detected({
            "intent": "hitch", "item_name": None, "input": "i hitch the horse to the cart",
        })

        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["puller"], "horse")
        self.assertEqual(result["vehicle"], "cart")
        self.assertEqual(self.dm_core.entities["cart"]["mount"], "horse")

    def test_hitching_a_second_horse_promotes_the_field_to_a_list(self):
        first = self._add_horse()
        second = self._add_horse()
        self._add_cart()
        self.dm_core.entities["cart"]["mount"] = first

        self.dm_core._on_item_interaction_detected({
            "intent": "hitch", "item_name": None, "input": f"i hitch the {second} to the cart",
        })

        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertEqual(self.dm_core.entities["cart"]["mount"], [first, second])

    def test_hitch_direction_is_first_named_pulls_second_named_regardless_of_phrasing(self):
        # Two entities that are both eligible as puller (own travel_speed) *and* as vehicle (an
        # authored, empty "mount" placeholder) -- either direction is equally legal on paper, so
        # this isolates that puller/vehicle roles follow pure left-to-right reading order, not a
        # guess based on either entity's own stats (see _resolve_hitch_intent's own docstring).
        first = self._add_horse()
        self.dm_core.entities[first]["mount"] = ""
        second = self._add_horse()
        self.dm_core.entities[second]["mount"] = ""

        self.dm_core._on_item_interaction_detected({
            "intent": "hitch", "item_name": None, "input": f"i hitch the {second} to the {first}",
        })

        # second is named first here -- it becomes the puller precisely because of word order,
        # not because either horse is somehow more "puller-shaped" than the other.
        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["puller"], second)
        self.assertEqual(result["vehicle"], first)
        self.assertEqual(self.dm_core.entities[first]["mount"], second)

    def test_hitch_denied_when_fewer_than_two_entities_are_named(self):
        self._add_horse()
        self.dm_core._on_item_interaction_detected({
            "intent": "hitch", "item_name": None, "input": "i hitch the horse up",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_present")

    def test_hitch_denied_against_a_downed_puller(self):
        self._add_horse()
        self._add_cart()
        Combat_Resolution.apply_damage(self.dm_core.world, "horse", 999)

        self.dm_core._on_item_interaction_detected({
            "intent": "hitch", "item_name": None, "input": "i hitch the horse to the cart",
        })

        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "target_down")

    def test_hitch_denied_against_a_hostile_puller(self):
        self._add_cart()
        self.dm_core._on_item_interaction_detected({
            "intent": "hitch", "item_name": None, "input": "i hitch the wolf to the cart",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "target_hostile")

    def test_hitch_denied_when_puller_is_not_a_valid_conveyance(self):
        # thane -- arena's own friendly ally -- authors no travel_speed and pulls nothing, so
        # he's present/alive/non-hostile and still can't serve as a puller.
        self._add_cart()
        self.dm_core._on_item_interaction_detected({
            "intent": "hitch", "item_name": None, "input": "i hitch thane to the cart",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_a_puller")
        self.assertFalse(self.dm_core.entities["cart"]["mount"])  # still the empty placeholder

    def test_hitch_denied_when_vehicle_has_no_authored_mount_field(self):
        # thane authors no "mount" field at all -- nothing ever declared him hitchable, so he
        # doesn't retroactively become a valid vehicle just because a horse gets named at him.
        # Closes the two-step version of the same gap "not_a_mount" closes for "mount" directly:
        # without this, hitching a horse onto an arbitrary NPC and then mounting that NPC would
        # otherwise still work.
        self._add_horse()
        self.dm_core._on_item_interaction_detected({
            "intent": "hitch", "item_name": None, "input": "i hitch the horse to thane",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_a_vehicle")
        self.assertNotIn("mount", self.dm_core.entities["thane"])

    def test_hitch_denied_when_already_hitched(self):
        self._add_horse()
        self._add_cart()
        self.dm_core.entities["cart"]["mount"] = "horse"

        self.dm_core._on_item_interaction_detected({
            "intent": "hitch", "item_name": None, "input": "i hitch the horse to the cart",
        })

        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "already_hitched")

    def test_a_hitched_cart_gains_the_horses_own_travel_speed(self):
        # The actual payoff: get_carrying_capacity/_resolve_travel_speed already walk any
        # "mount" chain (see TestGridTravel/TestBulk in this file) -- hitching is just the
        # player-facing way that chain gets built during play instead of being hand-authored.
        self._add_horse()
        self._add_cart()

        self.dm_core._on_item_interaction_detected({
            "intent": "hitch", "item_name": None, "input": "i hitch the horse to the cart",
        })

        self.assertEqual(Conveyance.travel_speed(self.dm_core.world, "cart"), 40)  # creatures.toml's own horse

    def test_unhitch_removes_a_bare_string_mount_field_entirely(self):
        self._add_horse()
        self._add_cart()
        self.dm_core.entities["cart"]["mount"] = "horse"

        self.dm_core._on_item_interaction_detected({
            "intent": "unhitch", "item_name": None, "input": "i unhitch the horse",
        })

        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["vehicle"], "cart")
        self.assertNotIn("mount", self.dm_core.entities["cart"])

    def test_unhitch_removes_one_entry_from_a_multi_horse_team(self):
        first = self._add_horse()
        second = self._add_horse()
        self._add_cart()
        self.dm_core.entities["cart"]["mount"] = [first, second]

        self.dm_core._on_item_interaction_detected({
            "intent": "unhitch", "item_name": None, "input": f"i unhitch the {first}",
        })

        self.assertEqual(self.dm_core.entities["cart"]["mount"], [second])

    def test_unhitch_denied_when_not_hitched_to_anything(self):
        self._add_horse()
        self.dm_core._on_item_interaction_detected({
            "intent": "unhitch", "item_name": None, "input": "i unhitch the horse",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_hitched")


class TestDowntime(DMTestCase):
    # debug.toml authors no [time] table, so DM_Time.py's own default (24 hours/day, 16
    # daylight, 3 blocks/day -- an 8-hour block) is what every test here exercises.

    def test_get_time_state_starts_at_day_zero_block_zero_daytime(self):
        state = self.dm_core.get_time_state()
        self.assertEqual(state, {
            "day": 0, "block_in_day": 0, "hour": 0.0, "is_day": True,
            "blocks_per_day": 3, "hours_per_day": 24,
            "year": 4726, "month": "Abadius", "day_of_month": 1,
            "date_label": "day 1 of Abadius, Year 4726",
        })
        self.assertTrue(self.dm_core.is_daytime())

    def test_day_night_is_read_off_elapsed_hours_not_block_index_parity(self):
        # Block 2 starts at hour 16 -- exactly daylight_hours -- so it's night; block 3 wraps
        # to day 1, block_in_day 0, daytime again. Proves is_day comes from real elapsed
        # hours against daylight_hours, not simply "last of every three".
        self.dm_core.advance_blocks(2)
        state = self.dm_core.get_time_state()
        self.assertEqual(state["block_in_day"], 2)
        self.assertEqual(state["hour"], 16.0)
        self.assertFalse(state["is_day"])

        self.dm_core.advance_blocks(1)
        state = self.dm_core.get_time_state()
        self.assertEqual(state["day"], 1)
        self.assertEqual(state["block_in_day"], 0)
        self.assertTrue(state["is_day"])

    def test_advance_blocks_is_current_blocks_only_mutation(self):
        self.assertEqual(self.dm_core.current_block, 0)
        self.dm_core.advance_blocks(5)
        self.assertEqual(self.dm_core.current_block, 5)
        self.dm_core.advance_blocks()  # default: one block
        self.assertEqual(self.dm_core.current_block, 6)

    def test_rest_heals_party_scaled_by_fortitude_and_advances_the_clock(self):
        # gladstone's own fortitude is {dice: 2, pips: 0} (characters.toml) -- _stub_roll_dice
        # makes every roll_dice call return 10 regardless of dice/pips actually passed, so this
        # only has to prove the roll happened and landed, not re-derive the D6 dice math.
        self._stub_roll_dice(10)
        Combat_Resolution.apply_damage(self.dm_core.world, "gladstone", 20)  # 36 max_hp -> 16 current
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone"), 16)

        result = self.dm_core.rest(2)

        self.assertEqual(self.dm_core.current_block, 2)  # advanced by blocks spent
        self.assertEqual(result["healed"]["gladstone"], {"healed": 10, "remaining_hp": 26})
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone"), 26)
        # arena's wolf is hostile, not is_party -- never healed by a party rest.
        self.assertNotIn("wolf", result["healed"])
        self.assertEqual(result["time"], self.dm_core.get_time_state())

    def test_rest_never_heals_a_dead_party_member(self):
        self._stub_roll_dice(10)
        Combat_Resolution.apply_damage(self.dm_core.world, "gladstone", 999)
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone"), 0)

        result = self.dm_core.rest()

        self.assertNotIn("gladstone", result["healed"])

    def test_rest_intent_is_diceless_and_free_standing(self):
        # A plain "rest" spends exactly one block; overnight/dawn/morning phrasing spends a
        # whole day's worth (blocks_per_day) -- DMCore decides this from the raw input itself,
        # the same "NLP only flags the intent" split travel/formation/speak_language follow.
        resolved_events = self._capture("item_interaction_resolved")

        self.dm_core._on_item_interaction_detected({
            "intent": "rest", "item_name": None, "input": "i rest",
        })
        self.assertEqual(self.dm_core.current_block, 1)
        self.assertEqual(resolved_events[-1]["blocks_spent"], 1)

        self.dm_core._on_item_interaction_detected({
            "intent": "rest", "item_name": None, "input": "we camp for the night",
        })
        self.assertEqual(self.dm_core.current_block, 4)  # 1 + blocks_per_day (3)
        self.assertEqual(resolved_events[-1]["blocks_spent"], 3)

    def test_detect_item_intent_recognizes_rest_phrases(self):
        for phrase in ("i rest", "let's make camp", "set up camp here", "i sleep", "camp for the night"):
            with self.subTest(phrase=phrase):
                self.assertEqual(detect_item_intent(phrase), "rest")

    def test_detect_item_intent_take_a_rest_is_still_a_take_not_a_rest(self):
        # TAKE_KEYWORDS' own "take " is checked well ahead of REST_KEYWORDS -- documented,
        # deliberate ordering (see REST_KEYWORDS' own module comment), not an oversight.
        self.assertEqual(detect_item_intent("take a rest"), "take")


class TestCalendar(DMTestCase):
    """!
    @brief rules.toml's own [[calendar_month]] table + Calendar.date_from_day/get_calendar_date
        (DM_Time.py) -- converts get_time_state()'s own absolute "day" counter into a
        year/month/day_of_month against Golarion's real calendar (Abadius..Kuthona, 365 days,
        no leap year), folded into get_time_state()'s own "date_label" for narration.
    """

    # Rules/Fantasy's own [time] authors starting_year = 4726 (Golarion's current year, Age of
    # Lost Omens) -- day 0 falls in that year, not a generic "year 1".

    def test_day_zero_is_the_first_of_the_first_month_in_the_starting_year(self):
        self.assertEqual(
            self.dm_core.get_calendar_date(), {"year": 4726, "month": "Abadius", "day_of_month": 1},
        )

    def test_a_date_mid_month_lands_on_the_right_day_of_month(self):
        # Abadius (31 days) + Calistril (28 days) = 59 -- day 60 is the 2nd day past both,
        # i.e. Pharast 2.
        self.dm_core.advance_blocks(60 * 3)  # 3 blocks/day
        self.assertEqual(
            self.dm_core.get_calendar_date(), {"year": 4726, "month": "Pharast", "day_of_month": 2},
        )

    def test_the_year_rolls_over_after_all_twelve_months(self):
        self.dm_core.advance_blocks(365 * 3)
        self.assertEqual(
            self.dm_core.get_calendar_date(), {"year": 4727, "month": "Abadius", "day_of_month": 1},
        )

    def test_date_label_is_folded_into_get_time_state(self):
        self.dm_core.advance_blocks(60 * 3)
        self.assertEqual(self.dm_core.get_time_state()["date_label"], "day 2 of Pharast, Year 4726")

    def test_starting_year_defaults_to_one_when_a_setting_authors_no_time_table_at_all(self):
        del self.dm_core.rules["time"]
        self.assertEqual(
            self.dm_core.get_calendar_date(), {"year": 1, "month": "Abadius", "day_of_month": 1},
        )

    def test_a_setting_with_no_calendar_table_falls_back_to_a_bare_day_count(self):
        self.dm_core.rules["calendar_month"] = []
        self.assertIsNone(self.dm_core.get_calendar_date())
        state = self.dm_core.get_time_state()
        self.assertIsNone(state["year"])
        self.assertIsNone(state["month"])
        self.assertIsNone(state["day_of_month"])
        self.assertEqual(state["date_label"], "day 0")

    # _seed_starting_date (a scenario's own [scenario] "start_month"/"start_day") -- exercised
    # by calling it directly against debug.toml's already-booted dm_core rather than
    # constructing a second DMCore, the same "call the private method directly" pattern other
    # boot-time-only behavior in this file already uses.

    def test_seed_starting_date_sets_current_block_from_start_month_and_day(self):
        # Abadius (31) + Calistril (28) + Pharast (31) + Gozran (30) + Desnus (31) +
        # Sarenith (30) = 181 days before Erastus -- Erastus 1 is day-of-year 181 (0-indexed).
        self.dm_core.scenario["start_month"] = "Erastus"
        self.dm_core.scenario["start_day"] = 1
        self.dm_core._seed_starting_date()
        self.assertEqual(self.dm_core.current_block, 181 * 3)
        self.assertEqual(
            self.dm_core.get_calendar_date(), {"year": 4726, "month": "Erastus", "day_of_month": 1},
        )

    def test_seed_starting_date_start_day_defaults_to_one(self):
        self.dm_core.scenario["start_month"] = "Calistril"
        self.dm_core._seed_starting_date()
        self.assertEqual(
            self.dm_core.get_calendar_date(), {"year": 4726, "month": "Calistril", "day_of_month": 1},
        )

    def test_seed_starting_date_is_a_noop_when_the_scenario_authors_neither_field(self):
        self.dm_core._seed_starting_date()
        self.assertEqual(self.dm_core.current_block, 0)

    def test_seed_starting_date_logs_an_error_and_leaves_current_block_alone_for_an_unknown_month(self):
        errors = self._capture("log_error")
        self.dm_core.scenario["start_month"] = "Notamonth"
        self.dm_core._seed_starting_date()
        self.assertEqual(self.dm_core.current_block, 0)
        self.assertTrue(errors)

    def test_seed_starting_date_logs_an_error_for_a_day_out_of_range(self):
        errors = self._capture("log_error")
        self.dm_core.scenario["start_month"] = "Calistril"
        self.dm_core.scenario["start_day"] = 29  # Calistril only has 28
        self.dm_core._seed_starting_date()
        self.assertEqual(self.dm_core.current_block, 0)
        self.assertTrue(errors)


class TestLostCoastStartingDate(DMTestCase):
    """!
    @brief lost_coast.toml's own shipped start_month/start_day worked example ("Erastus 1" --
        Sandpoint's Swallowtail Festival) -- exercises _seed_starting_date through a real boot
        rather than a direct call, and proves a reload restores the save's own elapsed time
        rather than re-seeding the scenario's start date on top of it.
    """
    scenario_name = "lost_coast"
    setting = "Pathfinder"  # lost_coast is Golarion-sourced content, kept isolated under Rules/Pathfinder/
    start_location = None  # leaves lost_coast.toml's own [scenario].start_location alone.

    def test_booting_the_scenario_seeds_the_shipped_start_date(self):
        self.assertEqual(self.dm_core.current_block, 181 * 3)
        self.assertEqual(
            self.dm_core.get_calendar_date(), {"year": 4726, "month": "Erastus", "day_of_month": 1},
        )

    def test_reloading_a_save_restores_elapsed_time_not_the_scenario_start_date(self):
        slot_name = "test_lost_coast_start_date_round_trip_slot"
        self.addCleanup(shutil.rmtree, self.dm_core._save_slot_dir(slot_name), ignore_errors=True)
        self.dm_core.advance_blocks(10)
        elapsed_block = self.dm_core.current_block
        self.dm_core.save_game(slot_name)

        self.dm_core.current_block = 0  # prove load restores the save, not the scenario default
        self.dm_core.load_game(slot_name)

        self.assertEqual(self.dm_core.current_block, elapsed_block)


class TestGridTravel(DMTestCase):
    # debug.toml: "trailhead" (grid 0,0, start_location) and "border_stones" (grid 24,0), both
    # seeded into known_locations by the scenario's own [scenario].known_locations, both inside
    # world_map.toml's "the open plains" region (-60..120 x, -60..60 y) naming the "plains"
    # environment. rules.toml's [travel] default_speed is 24 (1 grid unit = 1 mile -- see that
    # file's own comment), so this 24-mile hop costs exactly one block.
    scenario_name = "debug"
    start_location = "trailhead"

    def _stub_encounter_roll(self, result):
        """Forces DM_Encounters.py's own resolve_varied_value call to always return result,
        regardless of the weighted table passed in -- the encounter-table analog of
        DMTestCase._stub_roll_dice. Also records every table (the raw "encounter" list) it was
        called with, so a test can assert *which* day/night table actually got rolled."""
        calls = []

        def fake_resolve_varied_value(choices):
            calls.append(choices)
            return result

        original = DM_Encounters.resolve_varied_value
        DM_Encounters.resolve_varied_value = fake_resolve_varied_value
        self.addCleanup(setattr, DM_Encounters, "resolve_varied_value", original)
        return calls

    def test_grid_travel_computes_distance_and_blocks_and_advances_the_clock(self):
        self._stub_encounter_roll("nothing")
        resolved_events = self._capture("item_interaction_resolved")

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })

        self.assertEqual(self.dm_core.current_location_key, "border_stones")
        self.assertEqual(self.dm_core.current_block, 1)
        result = resolved_events[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["blocks_spent"], 1)
        self.assertEqual(result["distance"], 24.0)
        self.assertEqual(result["time"], self.dm_core.get_time_state())

    def test_grid_travel_denies_a_destination_that_isnt_known(self):
        self.dm_core.known_locations.discard("border_stones")
        resolved_events = self._capture("item_interaction_resolved")

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })

        result = resolved_events[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "no_exit")
        self.assertEqual(self.dm_core.current_location_key, "trailhead")  # never moved

    def test_grid_travel_denies_a_wholly_unknown_name(self):
        resolved_events = self._capture("item_interaction_resolved")

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to nowhereville",
        })

        self.assertFalse(resolved_events[-1]["found"])
        self.assertEqual(resolved_events[-1]["reason"], "no_exit")

    def test_grid_travel_rolls_the_environment_table_and_instances_a_hostile_creature(self):
        self._stub_encounter_roll("wild boar")

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })

        self.assertIn("wild boar", self.dm_core.scenario_entities)
        self.assertEqual(self.dm_core.current_target, "wild boar")  # hostile by default

    def test_grid_travel_rolls_the_day_table_by_day_and_night_table_by_night(self):
        calls = self._stub_encounter_roll("nothing")
        day_table = World_Map.find_environment(self.dm_core.rules, "plains")["day_encounter"]
        night_table = World_Map.find_environment(self.dm_core.rules, "plains")["night_encounter"]

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })
        self.assertEqual(calls[-1], day_table)  # block 0 starts at hour 0 -- daytime

        self.dm_core._enter_location("trailhead")
        self.dm_core.current_block = 2  # hour 16 -- night, per rules.toml's [time] table
        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })
        self.assertEqual(calls[-1], night_table)

    def test_resolve_region_environment_matches_inside_the_map_and_none_outside_it(self):
        self.assertEqual(self.dm_core.resolve_region_environment(0, 0), "plains")
        self.assertEqual(self.dm_core.resolve_region_environment(24, 0), "plains")
        self.assertIsNone(self.dm_core.resolve_region_environment(1000, 1000))

    def test_party_travel_speed_falls_back_to_rules_toml_default(self):
        self.assertEqual(self.dm_core._party_travel_speed(), 24)  # rules.toml's [travel] table

    def test_party_travel_speed_uses_an_entitys_own_override_when_present(self):
        self.dm_core.entities["gladstone"]["travel_speed"] = 2
        self.assertEqual(self.dm_core._party_travel_speed(), 2)

    def _add_horse(self):
        [name] = self.dm_core._instance_entities([{"name": "horse", "band": 1}])
        self.dm_core.scenario_entities.append(name)
        return name

    def test_party_travel_speed_uses_a_mounted_players_own_horse(self):
        self._add_horse()
        self.dm_core.entities["gladstone"]["mount"] = "horse"
        self.assertEqual(self.dm_core._party_travel_speed(), 40)  # creatures.toml's own horse

    def test_party_travel_speed_walks_a_mount_chain_through_a_cart(self):
        # A rider defers to their cart, which in turn defers to whichever horse pulls it --
        # see entity_schema.toml's own "mount" comment.
        self._add_horse()
        self.dm_core.entities["cart"] = {
            "name": "cart", "supertype": "object", "description": "A rickety cart.",
            "max_hp": 20, "mount": "horse",
        }
        self.dm_core.scenario_entities.append("cart")
        self.dm_core.entities["gladstone"]["mount"] = "cart"

        self.assertEqual(self.dm_core._party_travel_speed(), 40)  # creatures.toml's own horse

    def test_party_travel_speed_paces_a_cart_to_its_slowest_horse(self):
        first = self._add_horse()
        self.dm_core.entities[first]["travel_speed"] = 10
        second = self._add_horse()
        self.dm_core.entities[second]["travel_speed"] = 6
        self.dm_core.entities["cart"] = {
            "name": "cart", "supertype": "object", "description": "A rickety cart.",
            "max_hp": 20, "mount": [first, second],
        }
        self.dm_core.scenario_entities.append("cart")
        self.dm_core.entities["gladstone"]["mount"] = "cart"

        self.assertEqual(self.dm_core._party_travel_speed(), 6)  # paced to the slower horse

    def test_party_travel_speed_ignores_a_dead_mount_and_falls_back_to_default(self):
        self._add_horse()
        Combat_Resolution.apply_damage(self.dm_core.world, "horse", 999)
        self.dm_core.entities["gladstone"]["mount"] = "horse"

        self.assertEqual(self.dm_core._party_travel_speed(), 24)  # rules.toml's own default_speed

    def test_a_mounted_horse_survives_traveling_to_a_new_location(self):
        # Unlike an ordinary ally (see _add_party_member's own comment below, and
        # TestMount), a mount doesn't need seeding into every location's own
        # persistent_names by hand -- DM_Rules.py's _carry_mounts_into_scene does this
        # automatically off the player's own live "mount" field, every time.
        self._stub_encounter_roll("nothing")
        self._add_horse()
        self.dm_core.entities["gladstone"]["mount"] = "horse"

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })

        self.assertEqual(self.dm_core.current_location_key, "border_stones")
        self.assertIn("horse", self.dm_core.scenario_entities)
        self.assertEqual(self.dm_core.entities["gladstone"]["mount"], "horse")
        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "horse"), Combat_Resolution.get_band(self.dm_core.world, "gladstone"))

    def test_grid_travel_denied_while_the_mount_is_overloaded(self):
        self._add_horse()
        self.dm_core.entities["horse"]["max_bulk"] = 0
        self.dm_core.entities["gladstone"]["mount"] = "horse"
        resolved_events = self._capture("item_interaction_resolved")

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })

        result = resolved_events[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "mount_overloaded")
        self.assertEqual(self.dm_core.current_location_key, "trailhead")  # never moved

    def _add_party_member(self, name):
        # Both of debug.toml's locations author an empty "entities" list, so a freeform
        # _enter_location rebuilds scenario_entities from location_runtime's own cached
        # persistent_names on every arrival (DM_Rules.py) -- a name merely appended to
        # scenario_entities directly would be wiped out the moment travel's own
        # _enter_location(destination_key) runs. Seeding it into both locations' own
        # persistent_names instead makes it survive travel exactly like a real
        # hand-authored ally (ex: debug.toml's thane) would.
        self.dm_core.entities[name] = {
            "is_party": True, "name": name, "hp": 10, "max_hp": 10, "skills": {},
        }
        self.dm_core.scenario_entities.append(name)
        for location_key in ("trailhead", "border_stones"):
            cache = self.dm_core.location_runtime.setdefault(location_key, {})
            persistent_names = cache.get("persistent_names")
            if persistent_names is None:
                # A location not yet visited (ex: border_stones, before the first travel in
                # these tests) has no cache at all yet -- _instance_location_persistent_names
                # would normally guarantee the player is in it (DM_Rules.py); reproduce that
                # here since this helper builds the cache directly instead of going through it.
                persistent_names = [self.dm_core.player_name]
                cache["persistent_names"] = persistent_names
            if name not in persistent_names:
                persistent_names.append(name)

    def test_night_watch_solo_party_is_always_surprised_by_a_hostile_night_encounter(self):
        self._stub_encounter_roll("wild boar")
        self.dm_core.current_block = 2  # hour 16 -- night, per rules.toml's [time] table

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })

        self.assertTrue(Combat_Resolution.has_condition(self.dm_core.world, "gladstone", "surprised"))
        self.assertEqual(self.dm_core.watch_rotation_index, 0)  # nobody to rotate to -- no roll

    def test_night_watch_never_rolled_against_a_daytime_encounter(self):
        self._stub_encounter_roll("wild boar")
        # default current_block is 0 -- daytime

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })

        self.assertFalse(Combat_Resolution.has_condition(self.dm_core.world, "gladstone", "surprised"))

    def test_night_watch_never_rolled_against_a_non_hostile_night_encounter(self):
        self._stub_encounter_roll("nothing")
        self.dm_core.current_block = 2

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })

        self.assertFalse(Combat_Resolution.has_condition(self.dm_core.world, "gladstone", "surprised"))
        self.assertEqual(self.dm_core.watch_rotation_index, 0)

    def test_night_watch_with_a_party_surprises_everyone_on_a_failed_observation_roll(self):
        self._add_party_member("thane")
        self._stub_encounter_roll("wild boar")
        self._stub_roll_dice(3)  # well under plains' own watch_difficulty of 9
        self.dm_core.current_block = 2

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })

        self.assertTrue(Combat_Resolution.has_condition(self.dm_core.world, "gladstone", "surprised"))
        self.assertTrue(Combat_Resolution.has_condition(self.dm_core.world, "thane", "surprised"))
        self.assertEqual(self.dm_core.watch_rotation_index, 1)

    def test_night_watch_with_a_party_applies_no_condition_on_a_successful_watch(self):
        self._add_party_member("thane")
        self._stub_encounter_roll("wild boar")
        self._stub_roll_dice(99)  # comfortably clears plains' own watch_difficulty of 9
        self.dm_core.current_block = 2

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })

        self.assertFalse(Combat_Resolution.has_condition(self.dm_core.world, "gladstone", "surprised"))
        self.assertFalse(Combat_Resolution.has_condition(self.dm_core.world, "thane", "surprised"))
        self.assertEqual(self.dm_core.watch_rotation_index, 1)  # still advances on a pass

    def test_night_watch_rotation_advances_through_the_party_across_hostile_nights(self):
        self._add_party_member("thane")
        self._stub_encounter_roll("wild boar")
        self._stub_roll_dice(99)
        watchers = []
        original_resolve_action = Combat_Resolution.resolve_action

        def spy(ctx, entity_name, skill_name, difficulty=0, dice_penalty=0, skill_divisor=1):
            if skill_name == "observation":
                watchers.append(entity_name)
            return original_resolve_action(ctx, entity_name, skill_name, difficulty, dice_penalty, skill_divisor)

        patcher = patch.object(Combat_Resolution, "resolve_action", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dm_core.current_block = 2  # night

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })
        # Travel now pauses on a hostile block (docs/downtime.md's "Pausing for a fight")
        # instead of arriving inline -- clear the ambush and let the trip actually complete
        # before issuing the second one, the same way a real fight would resolve it.
        Combat_Resolution.apply_damage(self.dm_core.world, "wild boar", 999)
        self.dm_core._resume_pending_downtime()
        self.dm_core.current_block = 5  # next night block (5 % 3 == 2)
        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the trailhead",
        })

        self.assertEqual(watchers, ["gladstone", "thane"])

    def test_rest_consults_the_current_locations_environment(self):
        # trailhead sits at grid (0, 0), inside world_map.toml's "the open plains" region --
        # rest (DM_Time.py) now rolls that same environment's own tables via
        # _resolve_environment_block, exactly like travel already does per block.
        self._stub_encounter_roll("wild boar")
        self.dm_core.current_block = 2  # hour 16 -- night, per rules.toml's [time] table

        self.dm_core.rest(1)

        self.assertIn("wild boar", self.dm_core.scenario_entities)
        self.assertTrue(Combat_Resolution.has_condition(self.dm_core.world, "gladstone", "surprised"))  # solo -- always caught

    def test_rest_rolls_the_day_table_by_day_and_night_table_by_night(self):
        calls = self._stub_encounter_roll("nothing")
        day_table = World_Map.find_environment(self.dm_core.rules, "plains")["day_encounter"]
        night_table = World_Map.find_environment(self.dm_core.rules, "plains")["night_encounter"]

        self.dm_core.rest(1)
        self.assertEqual(calls[-1], day_table)

        self.dm_core.current_block = 2  # night
        self.dm_core.rest(1)
        self.assertEqual(calls[-1], night_table)

    def test_rest_healing_is_unaffected_by_a_hostile_night_block(self):
        self._stub_encounter_roll("wild boar")
        self._stub_roll_dice(10)
        Combat_Resolution.apply_damage(self.dm_core.world, "gladstone", 20)  # 36 max_hp -> 16 current
        self.dm_core.current_block = 2  # night -- rolls the hostile encounter and pauses

        paused = self.dm_core.rest(1)
        self.assertTrue(paused["interrupted"])

        # Clearing the ambush and letting the rest actually finish -- healing is one
        # aggregate roll computed only once the rest completes, never gated on whatever the
        # per-block environment rolls turned up along the way.
        Combat_Resolution.apply_damage(self.dm_core.world, "wild boar", 999)
        result = self.dm_core._advance_pending_rest()

        # gladstone's own fortitude is {dice: 2, pips: 0} (characters.toml).
        self.assertEqual(result["healed"]["gladstone"], {"healed": 10, "remaining_hp": 26})

    def test_rest_at_a_location_with_no_grid_never_consults_an_environment(self):
        # A location with no "grid" field at all has no environment mapped onto it --
        # _current_environment resolves to None, so rest behaves exactly as it did before this
        # existed (see TestDowntime, which exercises this same path against debug.toml).
        self.dm_core.locations["trailhead"].pop("grid")
        calls = self._stub_encounter_roll("wild boar")
        self.dm_core.current_block = 2

        self.dm_core.rest(1)

        self.assertEqual(calls, [])
        self.assertNotIn("wild boar", self.dm_core.scenario_entities)

    def test_hostile_travel_pauses_the_block_clock_and_enters_an_encounter_site(self):
        self._stub_encounter_roll("wild boar")
        resolved_events = self._capture("item_interaction_resolved")

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })

        self.assertIsNotNone(self.dm_core.pending_downtime)
        self.assertEqual(self.dm_core.pending_downtime["kind"], "travel")
        self.assertEqual(self.dm_core.pending_downtime["destination_key"], "border_stones")
        self.assertEqual(self.dm_core.current_location_key, ROAD_ENCOUNTER_KEY)
        self.assertIn("wild boar", self.dm_core.scenario_entities)
        self.assertEqual(self.dm_core.current_target, "wild boar")
        # No arrival narration this turn -- travel hasn't actually finished.
        self.assertEqual(resolved_events, [])

    def test_hostile_travel_preserves_a_partys_live_hp_and_conditions_across_the_site_swap(self):
        self._add_party_member("thane")
        Combat_Resolution.apply_damage(self.dm_core.world, "thane", 4)
        Combat_Resolution.apply_condition(self.dm_core.world, "thane", "wounded", duration="permanent", dismiss="")
        self._stub_encounter_roll("wild boar")

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })

        # Still "thane" -- not re-instanced as a fresh "thane_2" copy of the template, and
        # its live hp/condition from before the ambush survived the site swap intact.
        self.assertIn("thane", self.dm_core.scenario_entities)
        self.assertNotIn("thane_2", self.dm_core.entities)
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "thane"), 6)  # 10 - 4
        self.assertTrue(Combat_Resolution.has_condition(self.dm_core.world, "thane", "wounded"))

    def test_travel_resumes_automatically_once_the_hostile_dies_in_combat(self):
        self._stub_encounter_roll("wild boar")
        resolved_events = self._capture("item_interaction_resolved")
        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })
        self.assertIsNotNone(self.dm_core.pending_downtime)

        Combat_Resolution.apply_damage(self.dm_core.world, "wild boar", 999)
        self.dm_core._resolve_combat_round({"actions": []})  # the ordinary per-turn hook

        self.assertIsNone(self.dm_core.pending_downtime)
        self.assertEqual(self.dm_core.current_location_key, "border_stones")
        self.assertEqual(resolved_events[-1]["intent"], "travel")
        self.assertTrue(resolved_events[-1]["found"])
        self.assertEqual(resolved_events[-1]["location_name"], "the border stones")

    def test_second_travel_is_denied_while_a_downtime_is_pending(self):
        self._stub_encounter_roll("wild boar")
        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })
        resolved_events = self._capture("item_interaction_resolved")

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the trailhead",
        })

        self.assertFalse(resolved_events[-1]["found"])
        self.assertEqual(resolved_events[-1]["reason"], "downtime_interrupted")
        self.assertIsNotNone(self.dm_core.pending_downtime)  # not stomped by the new attempt

    def test_rest_is_denied_while_a_downtime_is_pending(self):
        self._stub_encounter_roll("wild boar")
        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })

        result = self.dm_core.rest(1)

        self.assertTrue(result["interrupted"])
        self.assertEqual(result["reason"], "downtime_interrupted")

    def test_a_stale_pending_downtime_auto_resumes_once_its_blocker_is_gone(self):
        # The hostile is removed by something other than the ordinary combat-round hook (ex:
        # ADaM despawning it) -- a later travel/rest attempt should still find its own way
        # clear, rather than denying forever.
        self._stub_encounter_roll("wild boar")
        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })
        Combat_Resolution.apply_damage(self.dm_core.world, "wild boar", 999)  # dead, but _resolve_combat_round never ran
        self._stub_encounter_roll("nothing")  # the fresh trip's own block should roll clean
        resolved_events = self._capture("item_interaction_resolved")

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the trailhead",
        })

        # The stale trip to border_stones completes first (published manually, no "resolved"
        # closure available for it any more), then the fresh request to trailhead succeeds too.
        self.assertEqual(len(resolved_events), 2)
        self.assertTrue(resolved_events[0]["found"])
        self.assertEqual(resolved_events[0]["location_name"], "the border stones")
        self.assertTrue(resolved_events[1]["found"])
        self.assertEqual(resolved_events[1]["location_name"], "the trailhead")
        self.assertIsNone(self.dm_core.pending_downtime)

    def test_pending_travel_round_trips_through_save_and_load(self):
        slot = "test_pending_travel_round_trip"
        slot_dir = os.path.join("Saves", slot)
        self.addCleanup(shutil.rmtree, slot_dir, ignore_errors=True)

        self._stub_encounter_roll("wild boar")
        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })
        Combat_Resolution.apply_damage(self.dm_core.world, "wild boar", 5)
        self.dm_core.save_game(slot)

        fresh_dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="trailhead")
        fresh_dm.load_game(slot)

        # current_location_key resolves to a real, non-empty location (not {}) -- the
        # ephemeral site's own shape was reinjected from pending_downtime before this ran.
        self.assertEqual(fresh_dm.current_location_key, ROAD_ENCOUNTER_KEY)
        self.assertEqual(fresh_dm.locations[ROAD_ENCOUNTER_KEY]["name"], "the road")
        self.assertIsNotNone(fresh_dm.pending_downtime)
        self.assertEqual(fresh_dm.pending_downtime["kind"], "travel")
        # The hostile's own live hp survived reload (ad_hoc = True -- DM_Encounters.py).
        self.assertIn("wild boar", fresh_dm.scenario_entities)
        self.assertEqual(Combat_Resolution.get_current_hp(fresh_dm.world, "wild boar"), fresh_dm.entities["wild boar"]["max_hp"] - 5)

        # Resolving the fight after reload still auto-resumes travel correctly.
        Combat_Resolution.apply_damage(fresh_dm.world, "wild boar", 999)
        fresh_dm._resolve_combat_round({"actions": []})
        self.assertIsNone(fresh_dm.pending_downtime)
        self.assertEqual(fresh_dm.current_location_key, "border_stones")

    def test_pending_rest_round_trips_through_save_and_load(self):
        slot = "test_pending_rest_round_trip"
        slot_dir = os.path.join("Saves", slot)
        self.addCleanup(shutil.rmtree, slot_dir, ignore_errors=True)

        self._stub_encounter_roll("wild boar")
        self.dm_core.rest(2)
        self.dm_core.save_game(slot)

        fresh_dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="trailhead")
        fresh_dm.load_game(slot)

        self.assertIsNotNone(fresh_dm.pending_downtime)
        self.assertEqual(fresh_dm.pending_downtime, {"kind": "rest", "blocks_total": 2, "blocks_done": 1})

        Combat_Resolution.apply_damage(fresh_dm.world, "wild boar", 999)
        self._stub_encounter_roll("nothing")  # the remaining block rolls clean this time
        result = fresh_dm._advance_pending_rest()
        self.assertFalse(result["interrupted"])
        self.assertIsNone(fresh_dm.pending_downtime)

    def test_known_locations_round_trips_through_save_and_load(self):
        slot = "test_known_locations_round_trip"
        slot_dir = os.path.join("Saves", slot)
        self.addCleanup(shutil.rmtree, slot_dir, ignore_errors=True)

        # Deterministic and uneventful -- a hostile roll would now pause travel (see "Pausing
        # for a fight") and add its own ephemeral encounter site to known_locations, which
        # isn't what this test is about.
        self._stub_encounter_roll("nothing")
        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the border stones",
        })
        self.dm_core.save_game(slot)

        fresh_dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="trailhead")
        fresh_dm.load_game(slot)

        # load_game's own load_scenario_definition/load_scenario re-derivation has no way to
        # see the "trailhead" start_location override above (that's an __init__-only param) --
        # it lands at debug.toml's own raw [scenario].start_location ("debug_hub") for a moment
        # before jumping to this save's real position, and that transient landing is enough to
        # mark "debug_hub" known too, permanently. Real play is unaffected: a normal boot with
        # no override already starts at debug_hub, so this never adds anything a real player
        # save wouldn't already have picked up at the very first turn.
        self.assertEqual(fresh_dm.known_locations, {"trailhead", "border_stones", "debug_hub"})

    def test_watch_rotation_index_round_trips_through_save_and_load(self):
        slot = "test_watch_rotation_index_round_trip"
        slot_dir = os.path.join("Saves", slot)
        self.addCleanup(shutil.rmtree, slot_dir, ignore_errors=True)

        self.dm_core.watch_rotation_index = 3
        self.dm_core.save_game(slot)

        fresh_dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="trailhead")
        fresh_dm.load_game(slot)

        self.assertEqual(fresh_dm.watch_rotation_index, 3)


class TestWorldMapExpansion(DMTestCase):
    # lost_coast.toml: "sandpoint" (grid 150,0, start_location) and "magnimar" (grid 210,0) --
    # a real ~60-mile separation (1 grid unit = 1 mile, rules.toml's own [travel] comment) --
    # both inside world_map.toml's "the Lost Coast" region (terrain = "coastal_forest",
    # speed_multiplier 0.6; polity = "Varisia") -- see docs/downtime.md's "Terrain, roads, and
    # polities". A [[road]] running the exact Sandpoint-Magnimar line at speed_multiplier = 1.0
    # overrides that slowdown back to the plain default_speed of 24, so the shipped trip still
    # costs exactly 3 blocks (distance 60 / speed 24), a single day.
    scenario_name = "lost_coast"
    setting = "Pathfinder"  # lost_coast is Golarion-sourced content, kept isolated under Rules/Pathfinder/
    start_location = None  # lost_coast.toml is its own file, untouched by the debug.toml merge --
    # None leaves its own [scenario].start_location ("sandpoint") alone, overriding
    # DMTestCase's own "arena_grounds" default, which doesn't exist in this scenario at all.

    def _stub_encounter_roll(self, result):
        original = DM_Encounters.resolve_varied_value
        DM_Encounters.resolve_varied_value = lambda choices: result
        self.addCleanup(setattr, DM_Encounters, "resolve_varied_value", original)

    def test_terrain_at_matches_inside_the_map_and_none_outside_it(self):
        self.assertEqual(World_Map.terrain_at(self.dm_core.rules, 150, 0), "coastal_forest")
        self.assertIsNone(World_Map.terrain_at(self.dm_core.rules, 1000, 1000))

    def test_effective_speed_multiplier_is_1_0_with_nothing_authored(self):
        # debug.toml's own region authors no "terrain" at all -- the regression guard that
        # "nothing authored" still behaves exactly as it did before terrain/roads existed.
        self.assertEqual(World_Map.effective_speed_multiplier(self.dm_core.rules, 1000, 1000), 1.0)

    def test_effective_speed_multiplier_uses_terrain_off_the_road(self):
        # y=5 is more than the road's own width (2) away from its y=0 line -- pure terrain.
        self.assertEqual(World_Map.effective_speed_multiplier(self.dm_core.rules, 180, 5), 0.6)

    def test_effective_speed_multiplier_overrides_terrain_on_the_road(self):
        self.assertEqual(World_Map.effective_speed_multiplier(self.dm_core.rules, 180, 0), 1.0)

    def test_road_multiplier_is_none_off_the_roads_own_width(self):
        self.assertIsNone(World_Map.road_multiplier(self.dm_core.rules, 180, 5))

    def _stub_bent_road(self, path, width=2, speed_multiplier=1.0):
        # A synthetic multi-leg [[road]] pushed directly onto the already-loaded rules dict --
        # cheaper than authoring a whole new world_map.toml region just to exercise "path", and
        # restored automatically so it can't leak into any other test in this class.
        original = list(self.dm_core.rules.get("road", []))
        self.dm_core.rules["road"] = original + [
            {"path": [{"x": x, "y": y} for x, y in path], "width": width, "speed_multiplier": speed_multiplier},
        ]
        self.addCleanup(lambda: self.dm_core.rules.__setitem__("road", original))

    def test_road_points_falls_back_to_from_to_when_no_path_authored(self):
        # The shipped Sandpoint-Magnimar road still authors "from"/"to", not "path" -- the
        # two-point degenerate case road_points must keep producing unchanged.
        road = self.dm_core.rules["road"][0]
        self.assertEqual(World_Map.road_points(road), [(150, 0), (210, 0)])

    def test_road_multiplier_follows_a_bent_path(self):
        # An L-shaped road (0,0) -> (10,0) -> (10,10): a straight from/to line would cut the
        # corner and miss (10,0) entirely by more than its own width -- only per-leg checking
        # (not one straight shot between the endpoints) can ever match this point.
        self._stub_bent_road([(0, 0), (10, 0), (10, 10)], width=1)
        self.assertEqual(World_Map.road_multiplier(self.dm_core.rules, 10, 0), 1.0)
        self.assertEqual(World_Map.road_multiplier(self.dm_core.rules, 5, 0), 1.0)
        self.assertEqual(World_Map.road_multiplier(self.dm_core.rules, 10, 5), 1.0)
        # (5, 5) is off both legs -- more than 1 unit from either the x-axis or the x=10 line --
        # proof this isn't secretly falling back to a single from-first-to-last segment, which
        # would place it well within a wide straight-line tolerance instead.
        self.assertIsNone(World_Map.road_multiplier(self.dm_core.rules, 5, 5))

    def test_grid_travel_costs_the_same_3_blocks_the_road_always_promised(self):
        self._stub_encounter_roll("nothing")
        resolved_events = self._capture("item_interaction_resolved")

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to magnimar",
        })

        result = resolved_events[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["blocks_spent"], 3)
        self.assertEqual(result["distance"], 60.0)

    def _stub_exit_off_sandpoint(self, destination_key, destination_location):
        # A synthetic non-gridded [[location.exit]] pushed directly onto the already-loaded
        # "sandpoint" location (grid 150,0) -- cheaper than authoring a whole new scenario file
        # just to prove a gridded location's own exit graph is actually reachable.
        self.dm_core.locations[destination_key] = destination_location
        self.addCleanup(self.dm_core.locations.pop, destination_key, None)
        original_exits = list(self.dm_core.locations["sandpoint"].get("exit", []))
        self.dm_core.locations["sandpoint"]["exit"] = original_exits + [
            {"destination": destination_key},
        ]
        self.addCleanup(self.dm_core.locations["sandpoint"].__setitem__, "exit", original_exits)

    def test_gridded_location_still_honors_its_own_named_exit(self):
        # Confirms the fix for the exit-graph/grid-travel conflict: sandpoint carries "grid"
        # for its Magnimar trip, but that must not swallow a named move to a local, non-gridded
        # exit the way it did before _resolve_travel_intent tried the exit graph first.
        self._stub_exit_off_sandpoint("dock_shop", {
            "key": "dock_shop", "name": "The Dock Shop",
            "description": "A cramped shop smelling of tar and fish.", "entities": [],
            "return_to": "sandpoint",
        })
        resolved_events = self._capture("item_interaction_resolved")

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the dock shop",
        })

        result = resolved_events[-1]
        self.assertTrue(result["found"])
        self.assertEqual(self.dm_core.current_location_key, "dock_shop")

    def test_gridded_location_with_exits_still_falls_back_to_grid_travel(self):
        # The same sandpoint, now also carrying a local exit, must still resolve an unrelated,
        # explicitly-named grid destination through grid travel rather than denying "no_exit".
        self._stub_encounter_roll("nothing")
        self._stub_exit_off_sandpoint("dock_shop", {
            "key": "dock_shop", "name": "The Dock Shop",
            "description": "A cramped shop smelling of tar and fish.", "entities": [],
            "return_to": "sandpoint",
        })
        resolved_events = self._capture("item_interaction_resolved")

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to magnimar",
        })

        result = resolved_events[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["blocks_spent"], 3)

    def test_grid_travel_arrival_names_its_own_polity(self):
        self._stub_encounter_roll("nothing")
        resolved_events = self._capture("item_interaction_resolved")

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to magnimar",
        })

        self.assertEqual(resolved_events[-1]["polity"], "Varisia")

    def test_background_npc_is_labelled_by_role_not_as_a_name(self):
        self.dm_core.entities["crowd_1"] = {
            "name": "Fishmonger", "background": True, "description": "A trader.", "supertype": "creature",
        }
        self.assertEqual(self.dm_core.display_label("crowd_1"), "the Fishmonger")
        self.assertIn("refer to them only by role", self.dm_core.describe_character("crowd_1"))

    def test_a_narrated_person_with_a_personal_name_is_labelled_by_it(self):
        # Found by playtest: a narrated "Elara" labelled as unnamed had the model writing
        # "The Elara pauses" and switching to "The Fishmonger" mid-reply.
        self.dm_core.entities["Elara"] = {
            "name": "Elara", "background": True, "source": "narration", "supertype": "creature",
            "qualities": {"occupation": "Fishmonger"},
        }
        self.dm_core.entities["crowd_2"] = {
            "name": "Fishmonger", "background": True, "source": "narration", "supertype": "creature",
            "qualities": {"occupation": "fishmonger"},
        }
        self.assertEqual(self.dm_core.display_label("Elara"), "Elara")
        self.assertNotIn("refer to them only by role", self.dm_core.describe_character("Elara"))
        self.assertEqual(self.dm_core.display_label("crowd_2"), "the Fishmonger")

    def test_elf_can_buy_varisian_for_one_xp_at_character_creation(self):
        from resolution.Character_Creation import load_learnable_languages
        self.assertIn("varisian", load_learnable_languages("Rules/Pathfinder"))
        self.assertNotIn("common", load_learnable_languages("Rules/Pathfinder"))
        player = self.dm_core.entities[self.dm_core.player_name]
        player["languages"] = ["common", "elvish"]
        player["exp"] = 5
        self.dm_core.setting = "Pathfinder"

        self.dm_core.apply_character_creation({"languages": ["varisian"]})

        self.assertEqual(player["languages"], ["common", "elvish", "varisian"])
        self.assertEqual(player["exp"], 4)

    def test_language_purchase_rejects_known_or_unaffordable(self):
        from resolution.Character_Creation import spend_exp_on_languages
        catalog = ["elvish", "varisian"]
        self.assertEqual(spend_exp_on_languages(5, ["elvish"], catalog, ["common", "elvish"], {})[0], 5)
        self.assertIsNotNone(spend_exp_on_languages(5, ["elvish"], catalog, ["common", "elvish"], {})[1])
        self.assertIsNotNone(spend_exp_on_languages(1, ["elvish", "varisian"], catalog, ["common"], {})[1])
        self.assertEqual(spend_exp_on_languages(3, ["elvish", "varisian"], catalog, ["common"], {}), (1, None))

    def test_instanced_entity_with_no_authored_languages_inherits_the_polity_default(self):
        self.dm_core.entities["test_no_lang"] = {
            "name": "test_no_lang", "supertype": "creature", "max_hp": 5,
        }
        self.dm_core.current_location_key = "magnimar"  # already inside "the Lost Coast"

        [instance_name] = self.dm_core._instance_entities([{"name": "test_no_lang", "band": 1}])

        self.assertEqual(self.dm_core.entities[instance_name]["languages"], ["varisian"])

    def test_instanced_entity_with_authored_languages_is_left_untouched(self):
        self.dm_core.entities["test_with_lang"] = {
            "name": "test_with_lang", "supertype": "creature", "max_hp": 5,
            "languages": ["elvish"],
        }
        self.dm_core.current_location_key = "magnimar"

        [instance_name] = self.dm_core._instance_entities([{"name": "test_with_lang", "band": 1}])

        self.assertEqual(self.dm_core.entities[instance_name]["languages"], ["elvish"])

    def test_a_narrated_local_speaks_the_polity_language_whatever_the_narrator_gave_them(self):
        # Found by playtest: a Sandpoint fishmonger extracted as speaking only "common" answered
        # the (Varisian-speaking) default character in gibberish for 27 turns straight.
        self.dm_core.current_location_key = "magnimar"
        self.dm_core._pending_population.append({
            "scene": (self.dm_core.current_location_key, self.dm_core.current_room_key),
            "people": [{"name": "Elara Fenn", "supertype": "creature", "max_hp": 5, "hp": 5,
                        "languages": ["common"], "source": "narration", "background": True}],
        })

        self.dm_core._apply_pending_population()

        self.assertEqual(self.dm_core.entities["Elara Fenn"]["languages"], ["varisian", "common"])
        self.assertEqual(self.dm_core._detect_language_barrier("Elara Fenn"), (None, None))

    def test_magnimars_own_dockhand_speaks_varisian_by_the_polity_default(self):
        self._stub_encounter_roll("nothing")
        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to magnimar",
        })
        self.assertEqual(self.dm_core.entities["dockhand"]["languages"], ["varisian"])

    def _add_synthetic_impassable_water(self):
        # A synthetic region/terrain/destination disjoint from "the Lost Coast" itself (whose
        # own bounds run through x=240 -- see world_map.toml), injected directly rather than
        # shipped -- lost_coast.toml's own real geography has no water crossing to test
        # against, so this exercises the general impassable-terrain mechanism on its own terms
        # instead. World_Map.region_at matches the first region whose bounds contain a point, so
        # this has to sit strictly past "the Lost Coast" own max_x or it would never be reached.
        self.dm_core.rules["region"].append({
            "name": "test water", "terrain": "water", "min_x": 300, "max_x": 350,
            "min_y": -5, "max_y": 5,
        })
        self.dm_core.locations["far_island"] = {
            "key": "far_island", "name": "Far Island", "description": "",
            "grid": {"x": 350, "y": 0}, "entities": [],
        }
        self.dm_core.known_locations.add("far_island")

    def test_route_is_passable_denies_impassable_water_without_the_aquatic_tag(self):
        self._add_synthetic_impassable_water()
        self.assertFalse(World_Map.route_is_passable(
            self.dm_core.rules, {"x": 150, "y": 0}, {"x": 350, "y": 0}, self.dm_core._party_conveyance_tags(),
        ))

    def test_route_is_passable_allows_impassable_water_with_the_aquatic_tag(self):
        self._add_synthetic_impassable_water()
        self.dm_core.entities["gladstone"]["terrain_tags"] = ["aquatic"]
        self.assertTrue(World_Map.route_is_passable(
            self.dm_core.rules, {"x": 150, "y": 0}, {"x": 350, "y": 0}, self.dm_core._party_conveyance_tags(),
        ))

    def test_grid_travel_denies_impassable_terrain_without_the_right_conveyance(self):
        self._add_synthetic_impassable_water()
        resolved_events = self._capture("item_interaction_resolved")

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to far island",
        })

        result = resolved_events[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "impassable_terrain")
        self.assertEqual(self.dm_core.current_location_key, "sandpoint")  # never moved

    def test_resolve_conveyance_tags_walks_a_mount_chain(self):
        self.dm_core.entities["rowboat"] = {
            "name": "rowboat", "supertype": "object", "description": "A rowboat.",
            "max_hp": 10, "terrain_tags": ["aquatic"],
        }
        self.dm_core.scenario_entities.append("rowboat")
        self.dm_core.entities["gladstone"]["mount"] = "rowboat"

        self.assertEqual(Conveyance.terrain_tags(self.dm_core.world, "gladstone"), {"aquatic"})

    def test_sandpoint_expansion_added_the_exploring_sandpoint_landmarks(self):
        # sandpoint + magnimar (2) plus the 52 numbered "Exploring Sandpoint" landmarks pulled
        # from the "Sandpoint, Light of the Lost Coast" sourcebook (see lost_coast.toml's own
        # comment above sandpoint's [[location.exit]] list) -- not one test per landmark, just
        # proof the location count actually grew roughly as expected.
        self.assertGreaterEqual(len(self.dm_core.locations), 54)
        self.assertIn("rusty_dragon", self.dm_core.locations)

    def test_travel_to_a_real_sandpoint_landmark_resolves_via_its_own_named_exit(self):
        # Mirrors test_gridded_location_still_honors_its_own_named_exit above, but against real
        # authored content instead of a stubbed exit: sandpoint carries "grid" for its Magnimar
        # trip, yet a named move to one of its own [[location.exit]] landmarks must still resolve
        # through the exit graph rather than falling through to (or being swallowed by) grid
        # travel.
        resolved_events = self._capture("item_interaction_resolved")

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the rusty dragon",
        })

        result = resolved_events[-1]
        self.assertTrue(result["found"])
        self.assertEqual(self.dm_core.current_location_key, "rusty_dragon")

    def test_semantic_destination_rescues_a_generic_noun_but_never_outranks_a_named_one(self):
        # Both halves of _resolve_location_exit's precedence, against real authored Sandpoint
        # content. The literal whole-word scan runs first and always wins, so a destination the
        # player named outright can never be rerouted by a fuzzy match on some other exit --
        # that ordering is what makes adding a semantic match safe at all.
        resolved_events = self._capture("item_interaction_resolved")

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None,
            "input": "i travel to the rusty dragon", "destination": "goblin_squash_stables",
        })
        self.assertEqual(self.dm_core.current_location_key, "rusty_dragon")

        # And the half the literal scan could never do: "the tavern" names no exit whole-word,
        # so without a semantic key this denies with reason "no_exit" (or wanders off to
        # "return_to") no matter how obvious the player's meaning was.
        self.dm_core._enter_location("sandpoint")
        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None,
            "input": "head into the tavern", "destination": "rusty_dragon",
        })
        self.assertTrue(resolved_events[-1]["found"])
        self.assertEqual(self.dm_core.current_location_key, "rusty_dragon")

    def test_entering_a_location_publishes_its_exits_for_semantic_matching(self):
        # _enter_location is the single mutation site for current_location_key, so this one
        # hook is what keeps NLPCore's destination bank in sync across scenario start, travel,
        # grid arrival, teleport and load_game alike -- no separate persistence hook needed.
        published = self._capture("location_exits_updated")

        self.dm_core._enter_location("sandpoint")

        keys = {entry["key"] for entry in published[-1]["destinations"]}
        self.assertIn("rusty_dragon", keys)
        # Carries the destination's friendly name (what "the tavern" actually has to match
        # against), never its description -- see set_destinations.
        rusty = next(e for e in published[-1]["destinations"] if e["key"] == "rusty_dragon")
        self.assertTrue(rusty["name"])
        self.assertNotIn("description", rusty)

    def test_magnimar_expansion_added_its_nine_districts_and_their_landmarks(self):
        # Magnimar's own sourcebook ("Magnimar, City of Monuments") frames the whole city as
        # nine districts, each with its own lettered gazetteer -- unlike Sandpoint's single flat
        # landmark list, so the split went one level deeper (magnimar_<district>.toml siblings,
        # each a district hub exiting to that district's own landmarks -- see lost_coast.toml's
        # own comment above magnimar's [[location.exit]] list). Not one test per landmark, just
        # proof the location count actually grew roughly as expected and a couple of real
        # district hubs/landmarks are really there.
        self.assertGreaterEqual(len(self.dm_core.locations), 150)
        self.assertIn("dockway", self.dm_core.locations)
        self.assertIn("old_fang", self.dm_core.locations)

    def test_travel_to_a_real_magnimar_landmark_resolves_via_its_own_named_exit(self):
        # sandpoint -> magnimar is the shipped grid trip (3 blocks); magnimar -> dockway ->
        # old_fang are both real authored [[location.exit]] hops one and two levels down the
        # district hub-and-spoke -- proof the whole nested exit graph resolves end to end, not
        # just magnimar's own top-level district exits.
        self._stub_encounter_roll("nothing")
        resolved_events = self._capture("item_interaction_resolved")

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to magnimar",
        })
        self.assertEqual(self.dm_core.current_location_key, "magnimar")

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to dockway",
        })
        self.assertEqual(self.dm_core.current_location_key, "dockway")

        self.dm_core._on_item_interaction_detected({
            "intent": "travel", "item_name": None, "input": "i travel to the old fang",
        })

        result = resolved_events[-1]
        self.assertTrue(result["found"])
        self.assertEqual(self.dm_core.current_location_key, "old_fang")


class TestScenarioLoading(DMTestCase):
    def test_duplicate_entities_get_unique_instance_names(self):
        # debug.toml's own location lists gladstone and thane (persistent across the whole
        # location); its one room lists wolf twice (room-local) -- scenario_entities is
        # persistent_entities + this room's own instances, in that order.
        self.assertEqual(self.dm_core.scenario_entities, ["gladstone", "thane", "wolf", "wolf_2"])
        self.assertIn("wolf", self.dm_core.entities)
        self.assertIn("wolf_2", self.dm_core.entities)

    def test_current_target_defaults_to_the_first_hostile_entity_skipping_allies(self):
        # thane (non-hostile, an ally) is listed after both wolves in debug.toml, but even if
        # it weren't, current_target must never default to an ally -- it's chosen by hostility,
        # not by list position.
        self.assertEqual(self.dm_core.current_target, "wolf")


class TestAmbientEncounter(DMTestCase):
    """!
    @brief [[location.encounter]]'s own "ambient" trigger (_resolve_ambient_encounter,
        DM_Encounters.py) -- a repeating per-turn roll, called from DM_Core.py's
        _on_turn_detected, as opposed to "on_enter"'s own once-per-arrival check. Uses arena's
        own room ("grounds", under location "arena_grounds") to attach a synthetic "encounter"
        list directly, the same way TestGridTravel's own _stub_encounter_roll fakes
        DM_Encounters.resolve_varied_value rather than depending on real randomness.
    """

    def setUp(self):
        super().setUp()
        self.encounter_events = self._capture("encounter_triggered")

    def _stub_encounter_roll(self, result):
        original = DM_Encounters.resolve_varied_value
        DM_Encounters.resolve_varied_value = lambda choices: result
        self.addCleanup(setattr, DM_Encounters, "resolve_varied_value", original)

    def _set_room_encounter(self, trigger, choices):
        room = self.dm_core.rooms[self.dm_core.current_room_key]
        room["encounter"] = [{"name": "test ambience", "trigger": trigger, "encounter": choices}]

    def _take_an_ordinary_turn(self):
        self.dm_core._on_turn_detected({
            "clauses": [{"kind": "action", "skill": "blades"}], "input": "I attack the wolf",
        })

    def _kill_both_wolves(self):
        # arena's own room lists "wolf" twice (see TestScenarioLoading) -- both have to be
        # down for _any_hostile_present to actually go false.
        Combat_Resolution.apply_damage(self.dm_core.world, "wolf", 999)
        Combat_Resolution.apply_damage(self.dm_core.world, "wolf_2", 999)

    def test_ambient_encounter_fires_on_an_ordinary_turn_and_narrates_a_flavor_beat(self):
        self._set_room_encounter("ambient", [{"a distant howl echoes off the stone": 100}])
        self._stub_encounter_roll("a distant howl echoes off the stone")
        self._kill_both_wolves()  # no hostile present, so ambient can actually fire

        self._take_an_ordinary_turn()

        self.assertEqual(self.encounter_events[-1]["description"], "a distant howl echoes off the stone")

    def test_ambient_encounter_never_fires_while_a_hostile_is_already_present(self):
        self._set_room_encounter("ambient", [{"a distant howl echoes off the stone": 100}])
        self._stub_encounter_roll("a distant howl echoes off the stone")
        # arena's own wolves are alive and hostile by default -- _any_hostile_present is True.

        self._take_an_ordinary_turn()

        self.assertEqual(self.encounter_events, [])

    def test_an_on_enter_only_entry_is_never_rolled_by_the_ambient_check(self):
        self._set_room_encounter("on_enter", [{"should never fire from a turn": 100}])
        self._stub_encounter_roll("should never fire from a turn")
        self._kill_both_wolves()

        self._take_an_ordinary_turn()

        self.assertEqual(self.encounter_events, [])

    def test_ambient_encounter_can_instance_a_hostile_creature_and_claim_it_as_current_target(self):
        # "fire elemental" -- creatures.toml's own shared, hostile-by-default entity (no
        # [entity.attitudes] table of its own), not scenario-local, so it's guaranteed loaded
        # regardless of which scenario this test runs against.
        self._set_room_encounter("ambient", [{"fire elemental": 100}])
        self._stub_encounter_roll("fire elemental")
        self._kill_both_wolves()
        self.dm_core.current_target = None  # nothing currently claimed

        self._take_an_ordinary_turn()

        self.assertIn("fire elemental", self.dm_core.scenario_entities)
        self.assertEqual(self.dm_core.current_target, "fire elemental")


class TestLoadDoesNotRerollArrivalEncounters(DMTestCase):
    """!
    @brief load_game re-enters the saved location through _enter_location, which also rolls
        its "on_enter" encounters -- a reload must rebuild state only, never roll (or narrate)
        a fresh encounter. Found by a playtest's save -> load -> save drifting when a
        "Sandpoint Watchman" spawned on the reload. debug.toml's "town_square" carries an
        "on_enter" table.
    """
    start_location = "town_square"

    def test_reloading_a_save_rolls_no_on_enter_encounter(self):
        slot_name = "test_load_no_reroll_slot"
        self.addCleanup(shutil.rmtree, self.dm_core._save_slot_dir(slot_name), ignore_errors=True)
        original = DM_Encounters.resolve_varied_value
        DM_Encounters.resolve_varied_value = lambda choices: "A street performer juggles knives for scattered coin."
        self.addCleanup(setattr, DM_Encounters, "resolve_varied_value", original)
        encounter_events = self._capture("encounter_triggered")
        self.dm_core.save_game(slot_name)

        self.dm_core.load_game(slot_name)

        self.assertEqual(encounter_events, [])

    def test_entering_the_location_for_real_still_rolls_its_on_enter_encounter(self):
        original = DM_Encounters.resolve_varied_value
        DM_Encounters.resolve_varied_value = lambda choices: "A street performer juggles knives for scattered coin."
        self.addCleanup(setattr, DM_Encounters, "resolve_varied_value", original)
        encounter_events = self._capture("encounter_triggered")

        self.dm_core._enter_location("town_square")

        self.assertEqual(encounter_events[-1]["description"], "A street performer juggles knives for scattered coin.")
        self.assertFalse(self.dm_core._restoring_save)


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
        scenarios/debug.toml's "shopkeeper" can sell "most general goods... despite not being
        defined entities" (the scenario this file exists to exercise) -- TARGET_CENTRIC_INTENTS'
        own "trade" handling in DM_Improvisation.py. "dagger" is the shopkeeper's one real,
        hand-authored good (debug.toml's own local shopkeeper entity), kept deliberately sparse
        so this test can show both the ordinary trade path and the ad hoc one working side by
        side.
    """
    scenario_name = "debug"
    start_location = "general_store"

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

    def test_an_improvised_purchase_is_priced_off_the_quote_just_narrated(self):
        self.dm_core.recent_narration.extend([
            "The shopkeeper wipes the counter.",
            "\"Lantern? Nine silver, and it's yours.\"",
        ])
        self.dm_core.rules["currency"] = {"pricing_note": "value is in gold pieces."}
        self.dm_core.entities["gladstone"]["currency"] = 1
        fake_result = {
            "created": True, "location": "ground",
            "entity": {
                "name": "lantern", "supertype": "object", "subtype": "tool",
                "description": "A dented tin lantern.", "value": 0.9, "ad_hoc": True,
            },
        }
        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value=fake_result) as mock_generate:
            self.dm_core._on_improvisation_requested({
                "intent": "trade", "phrase": "a lantern", "input": "I'll take it",
            })

        kwargs = mock_generate.call_args.kwargs
        self.assertIn("Nine silver", kwargs["recent_narration"])
        self.assertIn("wipes the counter", kwargs["recent_narration"])
        self.assertEqual(kwargs["pricing_note"], "value is in gold pieces.")
        result = self.item_events[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["price"], 0.9)
        self.assertEqual(result["price_text"], "0.9 coins")  # debug authors no denominations
        # Exactly 0.1, not float subtraction's 0.09999999999999998 (Inventory_Resolution._settle).
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 0.1)
        self.assertIn("lantern", self.dm_core.entities["gladstone"]["inventory"])

    def test_a_non_trade_improvisation_gets_no_narration(self):
        self.dm_core.recent_narration.append("\"Lantern? Eight silver.\"")
        with patch("dm.DM_Improvisation.generate_ad_hoc_item", return_value={"created": False}) as mock_generate:
            self.dm_core._on_improvisation_requested({
                "intent": "take", "phrase": "a lantern", "input": "grab a lantern",
            })
        self.assertEqual(mock_generate.call_args.kwargs["recent_narration"], "")

    def test_pathfinder_authors_a_pricing_note(self):
        with open(os.path.join("Rules", "Pathfinder", "rules.toml"), "rb") as f:
            currency = tomllib.load(f)["currency"]
        self.assertIn("gold pieces", currency["pricing_note"])
        self.assertEqual(
            format_currency(1.24, currency["denomination"]),
            "1 gold piece, 2 silver pieces and 4 copper pieces",
        )

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


class TestAmbientEncounterSkipsLlmGeneration(DMTestCase):
    """!
    @brief DM_Encounters.py's own "ambient" trigger (_resolve_ambient_encounter) forces
        skip_llm_generation=True through to _instance_entities/generate_npc_stats -- the one
        encounter context with no natural pause (unlike "on_enter" or a travel block) to
        justify a synchronous LLM call landing on an arbitrary player turn. Uses
        scenario_entity_test's own "vault_specter_stub" -- a real generate=true
        entity_template deliberately never pre-referenced by any [[location.room]] entities
        (see TestNpcGenerationDMCoreIntegration's own "never accidentally referenced" test) --
        so resolving it here is a genuine template lookup, not a name collision with an
        already-instanced live entity.
    """
    scenario_name = "debug"
    start_location = "vault"

    def test_ambient_trigger_never_calls_the_llm_even_for_a_generate_true_template(self):
        def fail_if_called(*args, **kwargs):
            raise AssertionError("an ambient-triggered generation must never call the LLM")

        room = self.dm_core.rooms[self.dm_core.current_room_key]
        room["encounter"] = [
            {"name": "ambient specter", "trigger": "ambient", "encounter": [{"vault_specter_stub": 100}]},
        ]
        original = DM_Encounters.resolve_varied_value
        DM_Encounters.resolve_varied_value = lambda choices: "vault_specter_stub"
        self.addCleanup(setattr, DM_Encounters, "resolve_varied_value", original)
        Combat_Resolution.apply_damage(self.dm_core.world, "vault sentinel", 999)  # clear the room so ambient can fire

        with scripted_llm(side_effect=fail_if_called):
            self.dm_core._on_turn_detected({
                "clauses": [{"kind": "action", "skill": "athletics"}], "input": "I wait",
            })

        new_names = [name for name in self.dm_core.entities if name.startswith("vault_specter_stub")]
        self.assertEqual(len(new_names), 1)
        entity = self.dm_core.entities[new_names[0]]
        self.assertTrue(entity["generated"])
        self.assertEqual(entity["name"], "Unnamed Stranger")  # NPC_Generation.py's offline-fallback name


class TestScenarioLocalEntities(unittest.TestCase):
    """!
    @brief A scenario file's own [[entity]]/[[entity_template]] tables (DM_Rules.py's
        load_scenario_definition) -- lets a scenario-specific entity/NPC-generation stub live
        in the same file as the scenario that references it, instead of needing to be authored
        into a shared file like creatures.toml. Uses
        Rules/Fantasy/scenarios/debug.toml, whose "vault sentinel" entity and
        "vault_specter_stub" template exist nowhere else -- if load_scenario_definition didn't
        load them, [scenario].entities' reference to the former would fail with "unknown
        entity" and never make it into scenario_entities at all, and the latter would be
        entirely absent from self.entity_templates.
    """

    def test_scenario_local_entity_is_loaded_and_instanced(self):
        dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="vault")

        self.assertEqual(dm.entities["vault sentinel"]["max_hp"], 10)
        self.assertEqual(dm.entities["vault sentinel"]["supertype"], "creature")
        self.assertIn("vault sentinel", dm.scenario_entities)

    def test_scenario_local_entity_template_is_loaded(self):
        dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="vault")

        self.assertEqual(dm.entity_templates["vault_specter_stub"]["subtype"], "undead")
        # A stub template -- never instanced (not referenced by [scenario]/[[room]] entities),
        # so it must never show up in self.entities alongside real, directly usable entities.
        self.assertNotIn("vault_specter_stub", dm.entities)


class TestMultiRoomDungeon(DMTestCase):
    scenario_name = "debug"
    start_location = "crypt"

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
        # exactly the way the original debug.toml's chest already works.
        self.assertEqual(self.dm_core.current_target, "dart trap")


    def test_party_formation_holds_after_advancing(self):
        # thane (follow_offset = 0) walks abreast; anne (follow_offset = -1) trails one band
        # behind -- both snap back into formation the moment the player's own band changes
        # (_apply_party_formation, DM_Movement.py), not just at scenario load.
        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "gladstone"), 1)
        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "thane"), 1)
        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "anne"), 1)  # -1 clamped to the floor

        self.dm_core.advance_or_retreat("advance")  # entrance is 2 bands -- room to actually move

        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "gladstone"), 2)
        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "thane"), 2)  # walks abreast
        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "anne"), 1)  # one band behind, no longer clamped

        self.dm_core.advance_or_retreat("retreat")

        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "gladstone"), 1)
        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "thane"), 1)
        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "anne"), 1)


    def test_hidden_trap_fails_its_notice_roll_and_stays_out_of_the_roster(self):
        with patch("random.randint", return_value=1):  # observation 1D=1, under difficulty 4
            dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="crypt")
        self.assertTrue(dm.is_hidden("dart trap"))
        roster_text = " ".join(dm._describe_scenario_characters())
        self.assertNotIn("dart trap", roster_text)


    def test_successful_disarm_awards_xp_to_the_whole_party(self):
        # dart trap's own custom "exp" (items.toml, 9 -- see _award_xp_for_defeat's own
        # docstring for why a trap authors this instead of relying on get_challenge_rating) via
        # its [entity.test.pass]'s xp = true, paired with dismiss_condition = "armed". crypt's
        # party is gladstone (is_player, starts at exp = 100)/thane/anne (is_party, no
        # authored "exp" -- start at the implicit 0).
        with patch("random.randint", return_value=6):  # finesse well clears difficulty 9
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "finesse"}], "input": "I disarm the trap"})

        self.assertTrue(self.action_events[-1]["actions"][0].success)
        self.assertNotIn("armed", self.dm_core.entities["dart trap"]["active_conditions"])
        self.assertEqual(self.dm_core.entities["gladstone"]["exp"], 10 + 9)
        self.assertEqual(self.dm_core.entities["thane"]["exp"], 9)
        self.assertEqual(self.dm_core.entities["anne"]["exp"], 9)

    def test_a_second_disarm_attempt_is_impossible_and_awards_no_further_xp(self):
        # Once "armed" is dismissed, is_test_available's own requires_condition gate makes this
        # same test permanently unavailable -- there's no real second attempt to even make, the
        # same single-fire guarantee a combat kill gets from HP never rising back above 0.
        with patch("random.randint", return_value=6):
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "finesse"}], "input": "I disarm the trap"})
        gladstone_exp_after_the_disarm = self.dm_core.entities["gladstone"]["exp"]

        with patch("random.randint", return_value=6):
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "finesse"}], "input": "I disarm the trap again"})

        self.assertEqual(self.dm_core.entities["gladstone"]["exp"], gladstone_exp_after_the_disarm)

    def test_failed_disarm_damages_the_player_and_arms_blocks_further_attempts(self):
        starting_hp = Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone")
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
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone"), starting_hp - 1)
        self.assertIn("triggered", self.dm_core.entities["dart trap"]["active_conditions"])
        self.assertIn("armed", self.dm_core.entities["dart trap"]["active_conditions"])  # fail never dismisses it

        # blocks_if_condition="triggered" -- a repeat attempt must fall through to the normal
        # opposed path (difficulty 0, no HP loss) instead of rolling and re-damaging again.
        hp_after_first_hit = Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone")
        with patch("random.randint", return_value=6):
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "finesse"}], "input": "I try again"})
        self.assertEqual(self.action_events[-1]["actions"][0].difficulty, 0)
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone"), hp_after_first_hit)


    def test_forward_succeeds_once_the_player_reaches_the_exit_band(self):
        with patch("random.randint", return_value=6):
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "finesse"}], "input": "I disarm the trap"})
        self.dm_core.advance_or_retreat("advance")  # band 1 -> 2, toward the trap/exit
        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "gladstone"), 2)

        result = self._move("forward")

        self.assertTrue(result["found"])
        self.assertEqual(result["room_name"], "The Hall of Webs")
        self.assertEqual(self.dm_core.current_room_key, "hall_of_webs")
        self.assertEqual(self.dm_core.scenario_entities, ["gladstone", "thane", "anne", "giant spider"])
        self.assertEqual(self.dm_core.current_target, "giant spider")
        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "gladstone"), 1)  # this exit's own arrival_band

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
        Combat_Resolution.apply_damage(self.dm_core.world, "giant spider", 999)
        self._move("forward")  # -> guard_chamber

        self._move("back")  # -> back to hall_of_webs

        self.assertEqual(self.dm_core.current_room_key, "hall_of_webs")
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "giant spider"), 0)
        # current_target re-falls-back past the dead spider since nothing else is hostile/alive.
        self.assertNotEqual(self.dm_core.current_target, "giant spider")


class TestRoomLevelPresenceScoping(unittest.TestCase):
    """!
    @brief The actual payoff of room-level presence tagging: DMCore and LLMCore wired
        together over one real event bus (debug.toml, same room graph TestMultiRoomDungeon
        exercises) -- an entity met only after a room transition has no access to what was
        narrated before it existed, while a party member who traveled through both rooms
        does. NLPCore is deliberately left out (dialogue/movement are triggered directly on
        dm_core, the same way TestMultiRoomDungeon's own _move helper does) -- this is about
        presence tagging flowing correctly between the two real cores, not NLP matching.
    """

    def setUp(self):
        self.event_bus = ValidatingEventBus()
        # LLMCore must exist (and be subscribed) before DMCore's own __init__ publishes its
        # first "scenario_loaded" -- same ordering TestGameBoot already requires for NLPCore's
        # "rules_loaded" subscription, for the exact same reason.
        self.llm_core = LLMCore(self.event_bus, rag_source_dir=os.path.join("Rules", "Fantasy"))
        self.dm_core = DMCore(self.event_bus, scenario_name="debug", start_location="crypt")

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

        # thane persisted across both rooms (debug.toml's own [scenario].entities), so his own
        # witnessed history spans the entrance narration *and* everything since.
        self.assertEqual(len(thane_history), len(self.llm_core.context_window))


class TestDuplicateEntityNamesAcrossRooms(DMTestCase):
    """!
    @brief The DM_Rules.py "Known gaps" entry this fixes: self.entity_occurrence_counts is now
        scoped to the whole DMCore's lifetime, not to one _instance_entities call, so two
        different rooms in the same multi-room dungeon (debug.toml) that happen to declare the
        same creature name disambiguate against each other instead of the second one's own
        _place_new_entity silently overwriting the first's live HP/conditions under the same
        self.entities key. debug.toml itself never actually declares such a collision (see its
        own comment on "kept in sync by hand"), so this injects a second room reusing
        "giant spider" (already real in hall_of_webs) directly onto self.dm_core.rooms, the
        same "inject a room, then enter_room into it" technique TestMultiRoomDungeon's own
        move/revisit tests already use.
    """
    scenario_name = "debug"
    start_location = "crypt"

    def _inject_colliding_room(self):
        self.dm_core.rooms["ambush_nook"] = {
            "key": "ambush_nook", "name": "Ambush Nook", "bands": 1, "enclosed": True,
            "entities": [{"name": "giant spider", "band": 1}],
        }

    def test_second_rooms_duplicate_name_disambiguates_instead_of_colliding(self):
        self.dm_core.enter_room("hall_of_webs")
        self.assertIn("giant spider", self.dm_core.entities)
        Combat_Resolution.apply_damage(self.dm_core.world, "giant spider", 9)  # 14 max_hp -> 5, so an overwrite is detectable
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "giant spider"), 5)

        self._inject_colliding_room()
        self.dm_core.enter_room("ambush_nook")

        self.assertEqual(self.dm_core.scenario_entities, ["gladstone", "thane", "anne", "giant spider_2"])
        self.assertIn("giant spider_2", self.dm_core.entities)
        # The second instance is a fresh, full-HP copy of the template -- not the first's own
        # wounded dict reused/aliased.
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "giant spider_2"), 14)
        # The critical assertion: the first spider's live, wounded state must survive
        # untouched -- before this fix, the second room's own instancing overwrote
        # self.entities["giant spider"] outright.
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "giant spider"), 5)

    def test_save_then_load_restores_both_disambiguated_instances_correctly(self):
        self.dm_core.enter_room("hall_of_webs")
        Combat_Resolution.apply_damage(self.dm_core.world, "giant spider", 9)
        self._inject_colliding_room()
        self.dm_core.enter_room("ambush_nook")
        Combat_Resolution.apply_damage(self.dm_core.world, "giant spider_2", 3)

        slot_name = "test_crypt_duplicate_name_slot"
        slot_dir = self.dm_core._save_slot_dir(slot_name)
        self.addCleanup(shutil.rmtree, slot_dir, ignore_errors=True)
        self.dm_core.save_game(slot_name)

        # load_game's own load_scenario_definition re-reads debug.toml fresh from disk, which
        # would otherwise wipe the injected "ambush_nook" room -- re-inject it the moment
        # self.locations exists again, exactly where load_game itself populates it, before the
        # location_runtime replay loop (which needs it present) runs.
        fresh_dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="crypt")
        real_load_scenario_definition = fresh_dm.load_scenario_definition

        def load_scenario_definition_with_ambush_nook(scenario_name):
            real_load_scenario_definition(scenario_name)
            fresh_dm.locations["crypt"]["rooms"]["ambush_nook"] = {
                "key": "ambush_nook", "name": "Ambush Nook", "bands": 1, "enclosed": True,
                "entities": [{"name": "giant spider", "band": 1}],
            }

        fresh_dm.load_scenario_definition = load_scenario_definition_with_ambush_nook
        fresh_dm.load_game(slot_name)

        self.assertEqual(Combat_Resolution.get_current_hp(fresh_dm.world, "giant spider"), 5)
        self.assertEqual(Combat_Resolution.get_current_hp(fresh_dm.world, "giant spider_2"), 11)


class TestCalendarPure(unittest.TestCase):
    """!@brief resolution/Calendar.py over plain rules dicts -- the block clock as a date, no DMCore."""

    RULES = {
        "time": {"hours_per_day": 24, "daylight_hours": 16, "blocks_per_day": 3, "starting_year": 4726},
        "calendar_month": [{"name": "Abadius", "days": 31}, {"name": "Calistril", "days": 28}],
    }

    def test_day_zero_is_the_first_of_the_first_month_in_the_starting_year(self):
        self.assertEqual(
            Calendar.date_from_day(self.RULES, 0), {"year": 4726, "month": "Abadius", "day_of_month": 1},
        )

    def test_the_count_walks_into_the_next_month_and_wraps_into_the_next_year(self):
        self.assertEqual(Calendar.date_from_day(self.RULES, 31)["month"], "Calistril")
        self.assertEqual(Calendar.date_from_day(self.RULES, 59), {"year": 4727, "month": "Abadius", "day_of_month": 1})

    def test_day_of_year_is_the_inverse_of_the_month_walk(self):
        for day in (0, 30, 31, 58):
            date = Calendar.date_from_day(self.RULES, day)
            self.assertEqual(Calendar.day_of_year(self.RULES, date["month"], date["day_of_month"]), day)
        self.assertIsNone(Calendar.day_of_year(self.RULES, "Abadius", 32))
        self.assertIsNone(Calendar.day_of_year(self.RULES, "Nowhere", 1))

    def test_a_setting_with_no_calendar_reads_as_a_bare_day(self):
        state = Calendar.time_state({}, 7)
        self.assertEqual((state["day"], state["date_label"], state["year"]), (2, "day 2", None))

    def test_the_label_names_the_date_when_a_calendar_is_authored(self):
        self.assertEqual(Calendar.time_state(self.RULES, 0)["date_label"], "day 1 of Abadius, Year 4726")

    def test_a_block_counts_as_day_if_it_starts_before_dusk(self):
        # 3 blocks of 8 hours: blocks start at hours 0, 8 and 16 -- dusk (16) is the third's start.
        self.assertEqual([Calendar.time_state({}, block)["is_day"] for block in range(3)], [True, True, False])


class TestWorldMapPure(unittest.TestCase):
    """!@brief resolution/World_Map.py over plain rules dicts -- where a point is and what it costs."""

    RULES = {
        "region": [
            {"min_x": 0, "max_x": 100, "min_y": 0, "max_y": 100, "terrain": "forest", "environment": "woods", "polity": "Varisia"},
            {"min_x": 100, "max_x": 200, "min_y": 0, "max_y": 100, "terrain": "sea"},
        ],
        "terrain": [
            {"name": "forest", "speed_multiplier": 0.5},
            {"name": "sea", "impassable": True, "requires_tag": "aquatic"},
        ],
        "road": [{"from": {"x": 0, "y": 50}, "to": {"x": 90, "y": 50}, "width": 2, "speed_multiplier": 1.5}],
        "polity": [{"name": "Varisia", "language": "Varisian"}],
        "environment": [{"name": "woods"}],
    }

    def test_a_point_resolves_to_the_first_region_containing_it(self):
        self.assertEqual(World_Map.terrain_at(self.RULES, 10, 10), "forest")
        self.assertEqual(World_Map.environment_at(self.RULES, 10, 10), "woods")
        self.assertEqual(World_Map.polity_at(self.RULES, 10, 10), "Varisia")

    def test_a_point_outside_every_region_has_nothing(self):
        self.assertIsNone(World_Map.region_at(self.RULES, 500, 500))
        self.assertIsNone(World_Map.environment_at(self.RULES, 500, 500))

    def test_lookups_by_name(self):
        self.assertEqual(World_Map.find_polity(self.RULES, "Varisia")["language"], "Varisian")
        self.assertEqual(World_Map.find_environment(self.RULES, "woods"), {"name": "woods"})
        self.assertIsNone(World_Map.find_terrain(self.RULES, "lava"))

    def test_terrain_slows_travel_and_nothing_authored_is_unmodified(self):
        self.assertEqual(World_Map.effective_speed_multiplier(self.RULES, 10, 10), 0.5)
        self.assertEqual(World_Map.effective_speed_multiplier(self.RULES, 500, 500), 1.0)

    def test_a_road_overrides_the_terrain_it_crosses_only_within_its_width(self):
        self.assertEqual(World_Map.effective_speed_multiplier(self.RULES, 40, 51), 1.5)
        self.assertEqual(World_Map.effective_speed_multiplier(self.RULES, 40, 60), 0.5)
        self.assertIsNone(World_Map.road_multiplier(self.RULES, 40, 60))

    def test_impassable_terrain_needs_the_required_tag_in_the_party(self):
        self.assertTrue(World_Map.terrain_blocks_travel(self.RULES, 150, 10, {"hero": set()}))
        self.assertFalse(World_Map.terrain_blocks_travel(self.RULES, 150, 10, {"hero": {"aquatic"}}))
        self.assertFalse(World_Map.terrain_blocks_travel(self.RULES, 10, 10, {"hero": set()}))

    def test_a_route_is_denied_whole_when_any_stretch_is_impassable_to_everyone(self):
        origin, destination = {"x": 50, "y": 10}, {"x": 190, "y": 10}
        self.assertFalse(World_Map.route_is_passable(self.RULES, origin, destination, {"hero": set()}))
        self.assertTrue(World_Map.route_is_passable(self.RULES, origin, destination, {"hero": set(), "boat": {"aquatic"}}))
        self.assertTrue(World_Map.route_is_passable(self.RULES, origin, origin, {"hero": set()}))


if __name__ == "__main__":
    unittest.main()
