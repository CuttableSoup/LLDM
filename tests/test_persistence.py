import json
import os
import shutil
import tempfile
import unittest
import resolution.Combat_Resolution as Combat_Resolution
from dm.DM_Core import DMCore
from paths import PROJECT_ROOT
from tests.event_contract import ValidatingEventBus
from persistence.slot import (
    FORMAT_VERSION,
    VERSION_KEY,
    FileSlotStore,
    MemorySlotStore,
    Persistable,
    SaveError,
    restore_all,
    snapshot_all,
)
import LLDM
from tests.support import (
    DMTestCase,
)
# Module-level hooks start the shared patches (tests/support.py) for every test in this file.
from tests.support import setUpModule, tearDownModule  # noqa: F401


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
        # of the whole template (ex: no "max_hp" key, which never changes post-instancing
        # today). "equipped" *is* included -- see
        # test_equipped_slot_mapping_round_trips_through_save_load below for why. "skills"/
        # "qualities"/"languages" *are* included too, for the player specifically -- see
        # TestCharacterCreationRename's own test_a_customized_characters_build_survives_save_
        # and_load for why (character creation can diverge these from the template's own
        # baseline, with no other source of truth to re-derive them from on reload).
        slot = self._track("test_save_writes_diff")
        self.dm_core.save_game(slot)
        data = self._read_dm_state(slot)

        self.assertEqual(data["scenario_key"], "debug")
        self.assertEqual(data["player_name"], "gladstone")
        self.assertEqual(data["scenario_entities"], self.dm_core.scenario_entities)
        gladstone_state = data["instances"]["gladstone"]
        self.assertEqual(
            set(gladstone_state.keys()),
            {
                "hp", "active_conditions", "currency", "exp", "inventory", "equipped", "band",
                "attitude_deltas", "action_attitude_deltas", "current_language", "prompt_directive",
                "mount", "skills", "qualities", "languages", "abilities",
            },
        )


    def test_load_restores_saved_state_over_further_changes(self):
        slot = self._track("test_load_restores_state")
        Combat_Resolution.apply_damage(self.dm_core.world, "wolf", 10)  # wolf at 6/16
        self.dm_core.save_game(slot)

        Combat_Resolution.apply_damage(self.dm_core.world, "wolf", 6)  # wolf now at 0/16, diverged further from the save
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "wolf"), 0)

        self.dm_core.load_game(slot)

        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "wolf"), 6)


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

        fresh_dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds")  # boots with the template default
        fresh_dm.load_game(slot)

        self.assertEqual(fresh_dm.entities["gladstone"]["equipped"], {"chest": "chain mail"})
        self.assertIn("longsword", fresh_dm.entities["gladstone"]["inventory"])


    def test_accumulated_exp_round_trips_through_save_load(self):
        # gladstone starts at exp = 10 (characters.toml) -- without saving "exp" as its own
        # per-instance field, a reload would silently reset any XP _award_xp_for_defeat
        # (Combat_Actions.py) accumulated back down to that static template value.
        slot = self._track("test_exp_round_trip")
        self.dm_core.entities["gladstone"]["exp"] += 21  # as if a wolf had just been defeated
        self.dm_core.save_game(slot)

        fresh_dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds")  # boots with the template default
        self.assertEqual(fresh_dm.entities["gladstone"]["exp"], 10)
        fresh_dm.load_game(slot)

        self.assertEqual(fresh_dm.entities["gladstone"]["exp"], 31)


    def test_current_block_round_trips_through_save_and_load(self):
        # A downtime clock that forgot elapsed time on reload would let a save-scum trivially
        # dodge whatever eventually consumes it (ex: a bad watch roll) -- see docs/downtime.md.
        slot = self._track("test_current_block_round_trip")
        self.dm_core.rest(2)
        self.assertEqual(self.dm_core.current_block, 2)
        self.dm_core.save_game(slot)

        fresh_dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds")  # boots at current_block = 0
        self.assertEqual(fresh_dm.current_block, 0)
        fresh_dm.load_game(slot)

        self.assertEqual(fresh_dm.current_block, 2)


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

        fresh_dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds")  # boots with an empty ground list
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

        fresh_dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds")  # boots with the template's own description
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

        fresh_dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds")
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
        Combat_Resolution.apply_damage(self.dm_core.world, name, 5)  # 16 -> 11
        self.dm_core.save_game(slot)

        fresh_dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds")
        self.assertNotIn(name, fresh_dm.scenario_entities)
        fresh_dm.load_game(slot)

        self.assertIn(name, fresh_dm.scenario_entities)
        self.assertEqual(Combat_Resolution.get_current_hp(fresh_dm.world, name), 11)
        self.assertEqual(fresh_dm.entities[name]["summon_expires_in"], 3)
        self.assertFalse(fresh_dm.is_hostile(name, fresh_dm.player_name))

    def test_removed_entity_does_not_respawn_after_save_load(self):
        slot = self._track("test_removed_entity_round_trip")
        self.dm_core.remove_entity_from_scene("wolf")
        self.assertNotIn("wolf", self.dm_core.scenario_entities)
        self.dm_core.save_game(slot)

        fresh_dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds")  # boots with "wolf" freshly instanced
        self.assertIn("wolf", fresh_dm.scenario_entities)
        fresh_dm.load_game(slot)

        self.assertNotIn("wolf", fresh_dm.scenario_entities)
        self.assertIn("wolf", fresh_dm.removed_entities)
        self.assertIn("wolf_2", fresh_dm.scenario_entities)


class TestMultiRoomSaveLoad(DMTestCase):
    scenario_name = "debug"
    start_location = "crypt"

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
        Combat_Resolution.apply_damage(self.dm_core.world, "giant spider", 5)
        self.dm_core.save_game(slot)

        fresh_bus = ValidatingEventBus()
        fresh_dm = DMCore(fresh_bus, scenario_name="debug", start_location="crypt")  # boots back at "entrance"
        fresh_dm.load_game(slot)

        self.assertEqual(fresh_dm.current_room_key, "hall_of_webs")
        self.assertEqual(fresh_dm.scenario_entities, ["gladstone", "thane", "anne", "giant spider"])
        self.assertEqual(Combat_Resolution.get_current_hp(fresh_dm.world, "giant spider"), 9)


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

        fresh_dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="crypt")  # boots back at "entrance"
        fresh_dm.load_game(slot)

        self.assertEqual(fresh_dm.rooms["entrance"].get("ground"), ["health potion"])
        self.assertEqual(fresh_dm.current_room_key, "hall_of_webs")
        self.assertEqual(fresh_dm.entities["gladstone"]["inventory"].count("health potion"), 2)


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
        verbatim instead. debug.toml is one location -- this test injects a second, "b_wing", to
        actually exercise cross-location interleaving, the same "inject after construction, then
        drive it with low-level DMCore calls directly" technique TestDuplicateEntityNamesAcrossRooms
        already uses for a single extra room.
    """
    scenario_name = "debug"
    start_location = "crypt"

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
        # debug.toml itself declares no such collision (kept collision-free by hand, per its
        # own comments), so this is injected the same way TestDuplicateEntityNamesAcrossRooms
        # injects its own "ambush_nook" room.
        self.dm_core.locations["crypt"]["rooms"]["guard_chamber"]["entities"].append(
            {"name": "giant spider", "band": 3},
        )

        self.dm_core._enter_location("b_wing")  # first ever "giant spider" -> "giant spider"
        self.dm_core._enter_location("crypt", arrival_room="guard_chamber")  # second -> "_2"

        self.assertIn("giant spider", self.dm_core.entities)
        self.assertIn("giant spider_2", self.dm_core.entities)
        Combat_Resolution.apply_damage(self.dm_core.world, "giant spider", 9)  # b_wing's own spider: 14 -> 5
        Combat_Resolution.apply_damage(self.dm_core.world, "giant spider_2", 3)  # crypt's guard_chamber spider: 14 -> 11

        slot_name = "test_crypt_interleaved_location_slot"
        self.addCleanup(shutil.rmtree, self.dm_core._save_slot_dir(slot_name), ignore_errors=True)
        self.dm_core.save_game(slot_name)

        fresh_dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="crypt")
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
        self.assertEqual(Combat_Resolution.get_current_hp(fresh_dm.world, "giant spider"), 5)
        self.assertEqual(Combat_Resolution.get_current_hp(fresh_dm.world, "giant spider_2"), 11)

    def test_load_falls_back_gracefully_for_a_save_missing_entity_instancing_order(self):
        # Backward compatibility: a save written before self.entity_instancing_order existed
        # simply has no such key -- load_game must still restore the game (via
        # _replay_nested_instancing), not crash.
        self.dm_core.enter_room("hall_of_webs")
        Combat_Resolution.apply_damage(self.dm_core.world, "giant spider", 6)  # 14 -> 8

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

        fresh_dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="crypt")
        fresh_dm.load_game(slot_name)

        self.assertEqual(fresh_dm.current_room_key, "hall_of_webs")
        self.assertEqual(Combat_Resolution.get_current_hp(fresh_dm.world, "giant spider"), 8)


class TestSaveSlotStore(unittest.TestCase):
    """!
    @brief persistence/slot.py -- the slot store interface, run against both adapters (the real
        filesystem one and the in-memory one tests use) so the in-memory fake can't drift from
        the real behaviour -- plus the Persistable merge/restore helpers.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stores = {"file": FileSlotStore(self.tmp.name), "memory": MemorySlotStore()}

    def test_a_part_round_trips_and_carries_the_format_version(self):
        for name, store in self.stores.items():
            with self.subTest(store=name):
                store.write("slot", "dm_state", {"round_number": 3})
                data = store.read("slot", "dm_state")
                self.assertEqual(data["round_number"], 3)
                self.assertEqual(data[VERSION_KEY], FORMAT_VERSION)

    def test_a_missing_part_is_not_found(self):
        for name, store in self.stores.items():
            with self.subTest(store=name):
                with self.assertRaises(SaveError) as caught:
                    store.read("nowhere", "dm_state")
                self.assertEqual(caught.exception.reason, "not_found")

    def test_an_unversioned_or_old_part_is_unsupported_but_a_tolerant_read_still_works(self):
        store = self.stores["file"]
        os.makedirs(store.slot_dir("old"))
        with open(store.path("old", "dm_state"), "w") as f:
            json.dump({"scenario_key": "crypt", "version": 2}, f)

        with self.assertRaises(SaveError) as caught:
            store.read("old", "dm_state")
        self.assertEqual(caught.exception.reason, "unsupported_version")
        self.assertEqual(store.read("old", "dm_state", check_version=False)["scenario_key"], "crypt")

    def test_a_damaged_part_is_corrupt_not_a_crash(self):
        store = self.stores["file"]
        os.makedirs(store.slot_dir("bad"))
        with open(store.path("bad", "dm_state"), "w") as f:
            f.write("{not json")

        with self.assertRaises(SaveError) as caught:
            store.read("bad", "dm_state")
        self.assertEqual(caught.exception.reason, "corrupt")

    def test_a_write_leaves_no_temp_file_behind(self):
        store = self.stores["file"]
        store.write("slot", "dm_state", {"a": 1})
        self.assertEqual(os.listdir(store.slot_dir("slot")), ["dm_state.json"])

    def test_a_slot_name_cannot_escape_the_saves_root(self):
        for name, store in self.stores.items():
            with self.subTest(store=name):
                self.assertEqual(os.path.basename(store.slot_dir("../../evil")), "evil")

    def test_slots_are_listed_by_whichever_parts_exist(self):
        for name, store in self.stores.items():
            with self.subTest(store=name):
                store.write("b_slot", "dm_state", {})
                store.write("a_slot", "llm_state", {})
                self.assertEqual(store.list_slots(), ["a_slot", "b_slot"])

    def test_snapshot_all_merges_in_order_and_rejects_a_duplicate_key(self):
        class Part(Persistable):
            def __init__(self, **keys):
                self.keys = keys
                self.restored = None

            def snapshot(self):
                return dict(self.keys)

            def restore(self, data):
                self.restored = data

        self.assertEqual(snapshot_all([Part(a=1), Part(b=2)]), {"a": 1, "b": 2})
        with self.assertRaises(ValueError):
            snapshot_all([Part(a=1), Part(a=2)])

        first, second = Part(), Part()
        calls = []
        first.restore = lambda data: calls.append("first")
        second.restore = lambda data: calls.append("second")
        restore_all([first, second], {})
        self.assertEqual(calls, ["first", "second"])


class TestDMSaveSlotRoundTrip(DMTestCase):
    """!
    @brief DMCore's save/load over an in-memory slot store -- the participants' own keys round
        trip, and a part that can't be read is rejected before anything live is touched.
    """

    def setUp(self):
        super().setUp()
        self.store = MemorySlotStore()
        self.dm_core.slot_store = self.store
        self.failed = []
        self.event_bus.subscribe("game_load_failed", self.failed.append)

    def test_every_participant_contributes_its_own_keys_with_no_overlap(self):
        data = snapshot_all(self.dm_core.save_parts)
        for key in (
            "round_number", "current_block", "legal_records", "removed_entities", "known_locations",
            "player_name", "instances", "current_target", "recent_narration", "conversation_partner",
        ):
            self.assertIn(key, data)

    def test_the_clock_and_session_focus_round_trip(self):
        self.dm_core.round_number = 7
        self.dm_core.current_block = 12
        self.dm_core.save_game("clock_slot")

        fresh = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds", slot_store=self.store)
        fresh.load_game("clock_slot")

        self.assertEqual(fresh.round_number, 7)
        self.assertEqual(fresh.current_block, 12)

    def test_an_unsupported_slot_fails_cleanly_and_leaves_live_state_alone(self):
        self.store.write("stale", "dm_state", {"round_number": 99})
        self.store._parts[("stale", "dm_state")] = '{"round_number": 99}'  # no format_version
        self.dm_core.round_number = 4

        self.dm_core.load_game("stale")

        self.assertEqual(self.failed, [{"slot": "stale", "reason": "unsupported_version"}])
        self.assertEqual(self.dm_core.round_number, 4)

    def test_a_missing_slot_still_reports_not_found(self):
        self.dm_core.load_game("never_saved")
        self.assertEqual(self.failed, [{"slot": "never_saved", "reason": "not_found"}])


if __name__ == "__main__":
    unittest.main()
