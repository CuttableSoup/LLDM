import asyncio
import shutil
import threading
import tkinter as tk
import unittest
from unittest.mock import patch
import pytest
from gui.Character_Creation_GUI import CharacterCreationDialog
from dm.DM_Rules import list_available_scenarios
from gui.GUI_Core import GUICore
from tests.event_contract import ValidatingEventBus
from gui.Textual_Core import TextualCore
from textual.widgets import Button, RichLog
from tests.support import (
    _new_tk_root_with_retry,
)
# Module-level hooks start the shared patches (tests/support.py) for every test in this file.
from tests.support import setUpModule, tearDownModule  # noqa: F401


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

    def _make_dialog(self, player_exp=0):
        return CharacterCreationDialog(
            self.root, self.skills, self.races, self.character_creation, player_exp,
        )


    def test_create_sets_result_and_closes_the_dialog(self):
        dialog = self._make_dialog()
        dialog.allocation_vars["alpha"].set(2)
        dialog.allocation_vars["gamma"].set(1)

        dialog.create_button.invoke()

        self.assertEqual(
            dialog.result,
            {"race": "human", "allocation": {"alpha": 2, "gamma": 1}, "pip_spend": [], "abilities": [], "languages": [], "name": ""},
        )
        self.assertEqual(dialog.winfo_exists(), 0)

    def test_create_includes_a_trimmed_custom_name_when_entered(self):
        dialog = self._make_dialog()
        dialog.allocation_vars["alpha"].set(2)
        dialog.allocation_vars["gamma"].set(1)
        dialog.name_var.set("  Aria  ")

        dialog.create_button.invoke()

        self.assertEqual(dialog.result["name"], "Aria")


    def test_train_button_spends_a_pip_at_the_current_dice_cost(self):
        # human baseline: alpha/beta/gamma all 2D -- one pip on alpha costs 2 XP.
        dialog = self._make_dialog(player_exp=10)

        dialog.train_buttons["alpha"].invoke()

        self.assertEqual(dialog.pip_spend, ["alpha"])
        self.assertEqual(dialog.trained_skills["alpha"], {"dice": 2, "pips": 1})
        self.assertEqual(dialog.remaining_exp, 8)
        self.assertEqual(dialog.total_labels["alpha"]["total"].cget("text"), "2D +1p")
        self.assertEqual(dialog.exp_remaining_label.cget("text"), "XP remaining: 8 / 10")

    def test_a_third_trained_pip_rolls_over_into_a_die_and_a_higher_next_cost(self):
        dialog = self._make_dialog(player_exp=20)

        for _ in range(3):
            dialog.train_buttons["alpha"].invoke()

        self.assertEqual(dialog.trained_skills["alpha"], {"dice": 3, "pips": 0})
        self.assertEqual(dialog.remaining_exp, 20 - (2 + 2 + 2))  # each pip still cost 2D
        self.assertEqual(dialog.train_buttons["alpha"].cget("text"), "Train (3 xp)")  # next pip costs the new 3D

    def test_train_button_is_disabled_once_exp_runs_out(self):
        dialog = self._make_dialog(player_exp=2)

        dialog.train_buttons["alpha"].invoke()  # spends the only 2 XP available

        self.assertEqual(str(dialog.train_buttons["alpha"].cget("state")), tk.DISABLED)
        self.assertEqual(str(dialog.train_buttons["beta"].cget("state")), tk.DISABLED)

    def test_train_click_past_zero_exp_is_a_silent_no_op(self):
        # tk.Button.invoke() already refuses to fire a disabled button's own command -- this
        # calls _on_train_clicked directly instead, to prove the method's own internal
        # affordability guard is real defense-in-depth, not just something the disabled button
        # state happens to prevent from ever being exercised.
        dialog = self._make_dialog(player_exp=2)
        dialog.train_buttons["alpha"].invoke()  # spends the only 2 XP available
        self.assertEqual(dialog.remaining_exp, 0)

        dialog._on_train_clicked("beta")

        self.assertEqual(dialog.pip_spend, ["alpha"])
        self.assertEqual(dialog.remaining_exp, 0)

    def test_changing_allocation_after_training_rebases_the_next_costs_live(self):
        # self.pip_spend is always replayed fresh from scratch (_recompute_training), never a
        # sunk cost locked in at click time -- so raising alpha's own point-buy allocation
        # *after* already training it there doesn't just add a new die on top, it also
        # retroactively re-prices that same one already-bought pip at the new, higher 4D cost.
        dialog = self._make_dialog(player_exp=10)
        dialog.train_buttons["alpha"].invoke()  # 2D -> 2D1p, costing 2, 8 XP left
        self.assertEqual(dialog.remaining_exp, 8)

        dialog.allocation_vars["alpha"].set(2)  # alpha's own baseline+allocation is now 4D

        self.assertEqual(dialog.pip_spend, ["alpha"])  # the earlier purchase still replays fine
        self.assertEqual(dialog.trained_skills["alpha"], {"dice": 4, "pips": 1})
        self.assertEqual(dialog.remaining_exp, 10 - 4)  # re-priced at the new 4D cost, not the original 2
        self.assertEqual(dialog.train_buttons["alpha"].cget("text"), "Train (4 xp)")

    def test_race_switch_clears_pip_spend_and_refunds_exp(self):
        dialog = self._make_dialog(player_exp=10)
        dialog.train_buttons["alpha"].invoke()
        self.assertEqual(dialog.remaining_exp, 8)

        dialog.race_combo.current(1)  # switch to elf
        dialog._on_race_selected()

        self.assertEqual(dialog.pip_spend, [])
        self.assertEqual(dialog.remaining_exp, 10)

    def _make_ability_dialog(self, player_exp):
        abilities = {
            "spark": {"supertype": "spell", "difficulty": 10, "description": "A flicker."},
            "bolt": {"supertype": "spell", "difficulty": 25, "description": "A heavy bolt."},
        }
        return CharacterCreationDialog(
            self.root, self.skills, self.races, self.character_creation, player_exp, abilities,
        )

    def test_ticking_an_ability_spends_xp_and_locks_what_is_no_longer_affordable(self):
        dialog = self._make_ability_dialog(player_exp=3)
        dialog.ability_checks["bolt"].invoke()  # 25 / 10 rounds to 3 xp

        self.assertEqual(dialog.remaining_exp, 0)
        self.assertEqual(str(dialog.ability_checks["spark"]["state"]), "disabled")
        self.assertEqual(str(dialog.ability_checks["bolt"]["state"]), "normal")  # still untickable
        self.assertEqual(str(dialog.train_buttons["alpha"]["state"]), "disabled")

        dialog.ability_checks["bolt"].invoke()  # untick refunds
        self.assertEqual(dialog.remaining_exp, 3)
        self.assertEqual(str(dialog.ability_checks["spark"]["state"]), "normal")
        dialog.destroy()

    def test_training_is_repriced_against_what_abilities_leave(self):
        dialog = self._make_ability_dialog(player_exp=3)
        dialog.ability_checks["spark"].invoke()  # 1 xp; a pip on a 2D skill costs 2
        self.assertEqual(dialog.remaining_exp, 2)
        dialog.train_buttons["alpha"].invoke()
        self.assertEqual(dialog.remaining_exp, 0)
        self.assertEqual(dialog.pip_spend, ["alpha"])
        dialog.destroy()

    def test_chosen_abilities_are_included_in_the_create_result(self):
        dialog = self._make_ability_dialog(player_exp=3)
        dialog.allocation_vars["alpha"].set(2)
        dialog.allocation_vars["gamma"].set(1)
        dialog.ability_checks["spark"].invoke()

        dialog.create_button.invoke()

        self.assertEqual(dialog.result["abilities"], ["spark"])

    def test_pip_spend_is_included_in_the_create_result(self):
        dialog = self._make_dialog(player_exp=10)
        dialog.allocation_vars["alpha"].set(2)
        dialog.allocation_vars["gamma"].set(1)
        dialog.train_buttons["beta"].invoke()

        dialog.create_button.invoke()

        self.assertEqual(dialog.result["pip_spend"], ["beta"])


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
        self.event_bus = ValidatingEventBus()
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
        self.assertEqual(
            [self.gui.menu_bar.entrycget(i, "label") for i in range(4)],
            ["File", "Ruleset", "Character", "Scenario"],
        )
        self.assertEqual(self.gui.setting_var.get(), "Pathfinder")
        ruleset_labels = [
            self.gui.ruleset_menu.entrycget(i, "label")
            for i in range(self.gui.ruleset_menu.index("end") + 1)
        ]
        self.assertNotIn("Fantasy", ruleset_labels)

        self.assertEqual(self.gui.character_menu.index("end"), 1)
        self.assertEqual(self.gui.character_menu.entrycget(0, "label"), "Create...")
        self.assertEqual(self.gui.character_menu.entrycget(1, "label"), "Choose Default...")

        self.assertEqual(self.gui.file_menu.index("end"), 1)
        self.assertEqual(self.gui.file_menu.entrycget(0, "label"), "Save...")
        self.assertEqual(self.gui.file_menu.entrycget(1, "label"), "Load...")

        self.assertEqual(self.gui.scenario_menu.index("end"), 0)
        self.assertEqual(self.gui.scenario_menu.entrycget(0, "label"), "Load...")
        self.assertEqual(str(self.gui.scenario_menu.entrycget(0, "state")), tk.DISABLED)

    @patch("gui.GUI_Core.run_character_creation_dialog")
    @patch("gui.GUI_Core.load_player_starting_exp", return_value=0)
    @patch("gui.GUI_Core.load_character_creation_data", return_value=({}, [], {}))
    def test_character_creation_unlocks_scenario_menu_and_load_publishes_scenario_selected(
        self, mock_load, mock_exp, mock_dialog,
    ):
        self.gui.setting_var.set("Fantasy")  # the hidden test-fixture setting that owns "debug"
        mock_dialog.return_value = {"race": "elf", "allocation": {"arcane": 5}, "name": "Aria"}
        self.gui.request_character_creation()

        self.assertEqual(str(self.gui.scenario_menu.entrycget(0, "state")), tk.NORMAL)

        events = []
        self.event_bus.subscribe("scenario_selected", events.append)

        self.gui.request_scenario_load()
        picker = next(w for w in self.gui.root.winfo_children() if isinstance(w, tk.Toplevel))
        listbox = next(w for w in picker.winfo_children() if isinstance(w, tk.Listbox))
        scenario_keys = [key for key, _name, _description in list_available_scenarios()]
        debug_index = scenario_keys.index("debug")
        listbox.selection_clear(0, tk.END)
        listbox.selection_set(debug_index)
        button_row = next(w for w in picker.winfo_children() if isinstance(w, tk.Frame))
        load_button = next(
            w for w in button_row.winfo_children()
            if isinstance(w, tk.Button) and w.cget("text") == "Load"
        )

        load_button.invoke()

        self.assertEqual(events, [{
            "scenario_name": "debug",
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
             patch("gui.GUI_Core.load_player_starting_exp", return_value=0), \
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
    @patch("gui.GUI_Core.load_learnable_languages", return_value=["varisian"])
    @patch("gui.GUI_Core.load_learnable_abilities", return_value={"spark": {}})
    @patch("gui.GUI_Core.load_player_starting_exp", return_value=100)
    @patch("gui.GUI_Core.load_character_creation_data", return_value=({}, [], {}))
    def test_request_character_creation_publishes_character_created_with_the_dialogs_result(
        self, mock_load, mock_exp, mock_abilities, mock_languages, mock_dialog,
    ):
        mock_dialog.return_value = {"race": "elf", "allocation": {"arcane": 5}, "name": "Aria"}
        events = []
        self.event_bus.subscribe("character_created", events.append)

        self.gui.request_character_creation()

        mock_load.assert_called_once()
        mock_exp.assert_called_once()
        mock_dialog.assert_called_once_with(self.gui.root, {}, [], {}, 100, {"spark": {}}, ["varisian"])
        self.assertEqual(
            events, [{"character": {"race": "elf", "allocation": {"arcane": 5}, "name": "Aria"}}],
        )


def lines_of(app, widget_id):
    return [str(line) for line in app.query_one(f"#{widget_id}", RichLog).lines]


@pytest.mark.asyncio
async def test_user_input_and_llm_response_mirror_into_history():
    event_bus = ValidatingEventBus()
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
    event_bus = ValidatingEventBus()
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
    event_bus = ValidatingEventBus()
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
