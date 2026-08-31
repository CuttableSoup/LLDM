import json
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, simpledialog
from collections import Counter

import resolution.Combat_Resolution as Combat_Resolution
from resolution.Character_Creation import load_character_creation_data
from gui.Character_Creation_GUI import run_character_creation_dialog
from dm.DM_Rules import list_available_scenarios, list_available_settings
from paths import PROJECT_ROOT

DEFAULT_SETTING = "Fantasy"

SAVES_DIR = "Saves"

class GUICore:
    """!
    @brief Main class handling the display and user interaction.
    """

    def __init__(self, event_bus, master=None, default_setting=DEFAULT_SETTING):
        """!
        @brief Initializes the GUI components.
        @param event_bus The central event bus instance.
        @param master An existing Tk widget to parent this window under, as a Toplevel --
            LLDM.py's own real usage never passes this (self.root is a genuine Tk() root, the
            app's only one). Test-only: creating and destroying a real Tk() root per test adds
            up across many tests in one process and can eventually corrupt Tcl's own shared
            interpreter state (an intermittent, environment-specific TclError seen in this
            repo's own test suite) -- passing one shared, already-created root in as `master`
            lets a whole TestCase class reuse it, while each GUICore instance still gets its
            own fully independent Toplevel/widget tree (mainloop/winfo_children/destroy all
            behave the same on a Toplevel as on Tk, so nothing else about this class needs to
            know or care which one self.root actually is).
        @param default_setting Which Rules/<setting> the Ruleset menu (below) starts on --
            LLDM.py's own main() passes its own --setting CLI argument through here so a
            quick-boot's setting and the GUI's own starting selection never disagree. Purely
            a starting point, not a lock: request_character_creation/request_scenario_load
            both read whatever the Ruleset menu is set to at the moment they're used, not
            this constructor argument.
        """
        self.event_bus = event_bus
        # Marshals every Tk-widget-touching call onto this thread -- Tkinter may only be
        # touched from here, but several event handlers below can fire from a background
        # thread (LLDM.py's own Ollama-bootstrap status via display_system_status, LLMCore's
        # background narration fetches via "llm_response_ready" et al.). Queuing instead of
        # calling straight through is what keeps a foreign-thread call from ever touching a
        # widget directly -- the crash this specifically fixes ("main thread is not in main
        # loop") happened when such a call landed *before* mainloop() (below, in start())
        # had even begun pumping events, but a straight cross-thread widget call is unsafe
        # regardless of timing, so this is unconditional, not just a pre-mainloop guard.
        self._main_thread = threading.main_thread()
        self._call_queue = queue.Queue()

        self.root = tk.Toplevel(master) if master is not None else tk.Tk()
        self.root.title("LLDM Interface")
        self.root.geometry("1000x600")

        # Character/File/Scenario live in dropdown menus on the window's native menu bar,
        # rather than always-visible buttons/an automatic dialog at boot. Character ->
        # Create... is what actually starts building a game -- neither LLDM.py nor GUICore
        # loads any scenario on its own -- opening the same race/point-buy dialog LLDM.py used
        # to launch unconditionally before constructing DMCore; publishes "character_created"
        # and (see request_character_creation) unlocks Scenario -> Load... rather than
        # starting a game outright, so the player still gets to pick which scenario the new
        # character starts in. File -> Load... (loading a save fully determines which
        # character/scenario to resume, so it lives with Save... rather than under Character)
        # pops up the existing-slots list. Scenario -> Load... pops up the list of real
        # scenarios (list_available_scenarios(), DM_Rules.py) -- disabled until Character ->
        # Create... has produced a pending character (see _set_scenario_menu_enabled). All
        # three publish events only -- none construct a DMCore directly, since GUICore has no
        # reference to one; LLDM.py's own main() is what's actually subscribed to react to
        # them (see its own module docs) before any DMCore exists, exactly the same
        # "communicate only through events, never a direct reference" rule every other core in
        # this app follows.
        self._pending_character = None
        self._game_started = False

        self.menu_bar = tk.Menu(self.root, tearoff=0)
        self.character_menu = tk.Menu(self.menu_bar, tearoff=0)
        self.character_menu.add_command(label="Create...", command=self.request_character_creation)
        self.menu_bar.add_cascade(label="Character", menu=self.character_menu)
        self.file_menu = tk.Menu(self.menu_bar, tearoff=0)
        self.file_menu.add_command(label="Save...", command=self.request_save)
        self.file_menu.add_command(label="Load...", command=self.request_load)
        self.menu_bar.add_cascade(label="File", menu=self.file_menu)
        self.scenario_menu = tk.Menu(self.menu_bar, tearoff=0)
        self.scenario_menu.add_command(
            label="Load...", command=self.request_scenario_load, state=tk.DISABLED,
        )
        self.menu_bar.add_cascade(label="Scenario", menu=self.scenario_menu)

        # Ruleset picks which Rules/<setting> (and, by extension, Settings/<setting> for RAG
        # sourcebooks) everything below reads from -- Character -> Create...'s own
        # load_character_creation_data, Scenario -> Load...'s own list_available_scenarios,
        # and (via the "setting" carried on "scenario_selected"/"load_requested") LLDM.py's
        # own start_game/LLMCore.set_setting. A radiobutton per Rules/ subdirectory
        # (list_available_settings(), DM_Rules.py) rather than a popup dialog, since there's
        # always a valid selection (self.setting_var) even if the player never opens this
        # menu at all -- default_setting seeds it. Locked shut (see _set_ruleset_menu_enabled)
        # the moment a character exists/a game has started, the same "can't retarget mid-game"
        # rule Scenario -> Load... enforces on itself once past its own one valid window.
        self.ruleset_menu = tk.Menu(self.menu_bar, tearoff=0)
        available_settings = list_available_settings()
        if default_setting not in available_settings:
            available_settings = [default_setting] + available_settings
        self.setting_var = tk.StringVar(value=default_setting)
        for setting_name in available_settings:
            self.ruleset_menu.add_radiobutton(
                label=setting_name, variable=self.setting_var, value=setting_name,
            )
        self.menu_bar.add_cascade(label="Ruleset", menu=self.ruleset_menu)

        self.root.config(menu=self.menu_bar)

        self.main_paned = tk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self.main_paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.history_frame = tk.Frame(self.main_paned)
        self.history_text = tk.Text(self.history_frame, wrap=tk.WORD, state=tk.DISABLED)
        self.history_text.pack(fill=tk.BOTH, expand=True)
        self.main_paned.add(self.history_frame, minsize=500)

        self.right_frame = tk.Frame(self.main_paned)
        self.notebook = ttk.Notebook(self.right_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        self.main_paned.add(self.right_frame, minsize=300)

        # Party: one collapsible tree node per party member, each expanding into its own
        # Equipment/Inventory/Conditions groups -- ttk.Treeview gives expand/collapse for
        # free instead of hand-rolling toggle widgets per section.
        self.party_tab = tk.Frame(self.notebook)
        self.party_tree = ttk.Treeview(self.party_tab, show="tree")
        self.party_tree.pack(fill=tk.BOTH, expand=True)
        self.notebook.add(self.party_tab, text="Party")

        self.notes_tab = tk.Frame(self.notebook)
        self.notes_text = tk.Text(self.notes_tab, wrap=tk.WORD)
        self.notes_text.pack(fill=tk.BOTH, expand=True)
        self.notebook.add(self.notes_tab, text="Notes")

        # Map is a free-form scratchpad the player sketches their own map onto -- the
        # engine never writes here, it's just a plain drawing canvas.
        self.map_tab = tk.Frame(self.notebook)

        self.map_toolbar = tk.Frame(self.map_tab)
        self.map_toolbar.pack(fill=tk.X, side=tk.TOP)

        self.map_pen_color = "black"
        for color in ("black", "red", "blue", "green"):
            tk.Button(
                self.map_toolbar, bg=color, width=2,
                command=lambda c=color: self.set_map_pen_color(c),
            ).pack(side=tk.LEFT, padx=2, pady=2)

        self.map_clear_button = tk.Button(self.map_toolbar, text="Clear", command=self.clear_map)
        self.map_clear_button.pack(side=tk.RIGHT, padx=2, pady=2)

        self.map_canvas = tk.Canvas(self.map_tab, bg="white")
        self.map_canvas.pack(fill=tk.BOTH, expand=True)
        self._map_last_point = None
        self.map_canvas.bind("<ButtonPress-1>", self._on_map_draw_start)
        self.map_canvas.bind("<B1-Motion>", self._on_map_draw_move)
        self.map_canvas.bind("<ButtonRelease-1>", self._on_map_draw_end)

        self.notebook.add(self.map_tab, text="Map")

        # Debug: exactly what went out in the most recent LLM request and what came back --
        # updated on "llm_debug_updated" (LLM_Core.py's fetch_from_llm), which fires
        # alongside "llm_response_ready" but carries the full request text (system message +
        # entire context_window sent), not just the narration text shown in the history pane.
        self.debug_tab = tk.Frame(self.notebook)

        tk.Label(self.debug_tab, text="Query").pack(fill=tk.X, anchor=tk.W)
        self.debug_query_text = tk.Text(self.debug_tab, wrap=tk.WORD, state=tk.DISABLED, height=1)
        self.debug_query_text.pack(fill=tk.BOTH, expand=True)

        tk.Label(self.debug_tab, text="Response").pack(fill=tk.X, anchor=tk.W)
        self.debug_response_text = tk.Text(self.debug_tab, wrap=tk.WORD, state=tk.DISABLED, height=1)
        self.debug_response_text.pack(fill=tk.BOTH, expand=True)

        self.notebook.add(self.debug_tab, text="Debug")

        self.input_frame = tk.Frame(self.root)
        self.input_frame.pack(fill=tk.X, side=tk.BOTTOM, padx=5, pady=5)

        self.input_entry = tk.Entry(self.input_frame)
        self.input_entry.pack(fill=tk.X, expand=True, side=tk.LEFT)
        self.input_entry.bind("<Return>", self.handle_input)

        self.submit_button = tk.Button(self.input_frame, text="Submit", command=self.submit_input)
        self.submit_button.pack(side=tk.RIGHT, padx=(5, 0))

        self.user_input = ""
        self.event_bus.publish("log_info", "GUICore initialized.")
        # Every one of these is wrapped in self._marshal -- the publishing thread is whatever
        # thread called event_bus.publish, which for several of these events is a background
        # one (see __init__'s own docstring above), not necessarily this window's own main
        # thread.
        self.event_bus.subscribe("llm_response_ready", self._marshal(self.display_llm_response))
        self.event_bus.subscribe("llm_debug_updated", self._marshal(self.display_llm_debug))
        self.event_bus.subscribe("rules_loaded", self._marshal(self.display_party_status))
        self.event_bus.subscribe("rules_loaded", self._marshal(self._on_game_started))
        # "party_status_changed" is DMCore's cheap re-publish after anything that can change
        # a party member's HP/equipment/inventory/conditions (see DM_Core.py's
        # _publish_party_status) -- distinct from "rules_loaded", which NLPCore also rebuilds
        # its embeddings from and so only fires once at boot/new game.
        self.event_bus.subscribe("party_status_changed", self._marshal(self.display_party_status))
        self.event_bus.subscribe("game_saved", self._marshal(self.display_game_saved))
        self.event_bus.subscribe("game_loaded", self._marshal(self.display_game_loaded))
        self.event_bus.subscribe("game_load_failed", self._marshal(self.display_game_load_failed))
        # GUICore owns and persists its own save-slot slice (Saves/<slot>/gui_state.json),
        # the same pattern DMCore/LLMCore each follow for their own state -- see CLAUDE.md's
        # "Saving and loading". Currently just the Notes tab's free text.
        self.event_bus.subscribe("save_requested", self._marshal(self._on_save_requested))
        self.event_bus.subscribe("load_requested", self._marshal(self._on_load_requested))

        # Kicked off here (main thread, at construction) and reschedules itself from within
        # itself thereafter -- so it only ever runs as part of mainloop()'s own event pump,
        # never invoked from a foreign thread. See _marshal/_run_on_main_thread just below.
        self.root.after(50, self._poll_call_queue)

    def _run_on_main_thread(self, fn, *args):
        """!
        @brief Runs fn(*args) immediately if already on the Tk main thread (every existing
            synchronous call site -- tests included -- keeps behaving exactly as before);
            otherwise queues it for _poll_call_queue to run on the next mainloop tick, rather
            than letting a foreign thread touch a widget itself. queue.Queue.put is plain,
            thread-safe Python -- unlike Tk's own after()/widget calls, it carries no risk of
            the "main thread is not in main loop" RuntimeError a direct cross-thread widget
            call can raise (worst of all before mainloop() has even started, since nothing is
            pumping events yet to make that call "safe by luck").
        @param fn The handler to run (ex: self.append_to_history).
        @param args Positional arguments to call it with.
        """
        if threading.current_thread() is self._main_thread:
            fn(*args)
        else:
            self._call_queue.put((fn, args))

    def _marshal(self, fn):
        """!
        @brief Wraps fn as a same-signature callable safe to hand to event_bus.subscribe --
            every subscriber here may be invoked from whichever thread published the event
            (see __init__'s own docstring). Delegates to _run_on_main_thread per call.
        @param fn The handler to protect (ex: self.display_llm_response).
        @return A wrapper calling fn(*args) via _run_on_main_thread.
        """
        return lambda *args: self._run_on_main_thread(fn, *args)

    def _poll_call_queue(self):
        """!
        @brief Drains _call_queue and reschedules itself -- the only code path that actually
            invokes a queued handler, since (per __init__) this only ever runs as part of
            mainloop()'s own event pump, i.e. always on the main thread.
        """
        try:
            while True:
                fn, args = self._call_queue.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        self.root.after(50, self._poll_call_queue)

    def start(self):
        self.root.mainloop()

    def handle_input(self, event):
        self.submit_input()

    def submit_input(self):
        self.user_input = self.input_entry.get()
        if not self.user_input.strip():
            return
        self.input_entry.delete(0, tk.END)
        self.append_to_history(f"> {self.user_input}\n")
        self.event_bus.publish("user_input_submitted", self.user_input)

    def append_to_history(self, text):
        self.history_text.config(state=tk.NORMAL)
        self.history_text.insert(tk.END, text)
        self.history_text.see(tk.END)
        self.history_text.config(state=tk.DISABLED)

    def display_llm_response(self, text):
        self.append_to_history(f"{text}\n\n")

    def display_system_status(self, message):
        """!
        @brief Shows a one-off status line in the History pane, prefixed the same "[System]"
            way display_game_saved/display_game_loaded/display_game_load_failed already are --
            for out-of-band status that isn't narration but the player still needs to see (ex:
            Ollama_Launcher.py's own download-progress/startup messages, relayed by LLDM.py's
            main() from a background thread -- possibly before mainloop() has even started,
            since the bootstrap thread races it -- see _run_on_main_thread for why this is
            safe regardless of when it's called or from which thread).
        @param message Plain text, no trailing newline needed.
        """
        self._run_on_main_thread(self.append_to_history, f"[System] {message}\n")

    def display_llm_debug(self, data):
        """!
        @brief Replaces the Debug tab's Query/Response boxes with the most recent LLM
            exchange -- overwritten each time ("most recent" only, no history kept), same
            redraw-not-append rule display_party_status follows for its own tree.
        @param data The "llm_debug_updated" payload ({"query": ..., "response": ...}) --
            "query" is the full request text (system message + entire context_window sent),
            "response" is the raw text that came back, or "[ERROR] ..." if the request failed.
        """
        for widget, text in ((self.debug_query_text, data.get("query", "")),
                              (self.debug_response_text, data.get("response", ""))):
            widget.config(state=tk.NORMAL)
            widget.delete("1.0", tk.END)
            widget.insert(tk.END, text)
            widget.config(state=tk.DISABLED)

    def request_character_creation(self):
        """!
        @brief Opens the race/point-buy character-creation dialog (Character_Creation_GUI.py),
            blocking modally the same way request_load's own picker does -- moved here from
            LLDM.py's main(), which used to run this unconditionally, before DMCore even
            existed, on every single boot. Publishes "character_created" with the dialog's own
            result ({"race", "allocation", "name"}) only if "Create" was actually pressed;
            cancelling leaves self.result None and nothing is published at all, so a cancelled
            dialog can't be mistaken for "create a character with no race/allocation" by
            whatever's listening. load_character_creation_data() re-scans Rules/Fantasy/*.toml
            directly (see its own module docstring) rather than asking a DMCore for the same
            data, since one may not exist yet -- the entire reason this event-published, no
            direct reference, pattern exists in the first place (see this class's own
            docstring further up).

            A freshly-created character doesn't start a game by itself anymore -- it just
            becomes self._pending_character and unlocks Scenario -> Load... (request_
            scenario_load), so the player picks which scenario to start it in rather than
            always landing in whatever LLDM.py's own default happens to be. No-ops (past
            publishing the event) if a game is already active -- see _on_game_started.
        """
        skills, races, character_creation = load_character_creation_data(
            os.path.join("Rules", self.setting_var.get())
        )
        character = run_character_creation_dialog(self.root, skills, races, character_creation)
        if character is None:
            return
        self.event_bus.publish("character_created", {"character": character})
        if self._game_started:
            return
        self._pending_character = character
        self._set_scenario_menu_enabled(True)
        self._set_ruleset_menu_enabled(False)

    def _set_scenario_menu_enabled(self, enabled):
        """!
        @brief Locks/unlocks the Scenario menu's own "Load..." entry.
        @param enabled True to unlock (a character is pending and no game has started yet).
        """
        self.scenario_menu.entryconfig(0, state=tk.NORMAL if enabled else tk.DISABLED)

    def _set_ruleset_menu_enabled(self, enabled):
        """!
        @brief Locks/unlocks every radiobutton in the Ruleset menu -- a character is built
            from a specific Rules/<setting> (request_character_creation), so the setting can't
            be changed out from under it once one exists; see request_character_creation/
            _on_game_started for the two points this actually gets locked at.
        @param enabled True to unlock (no character/game exists yet).
        """
        state = tk.NORMAL if enabled else tk.DISABLED
        last_index = self.ruleset_menu.index("end")
        if last_index is None:
            return  # no settings found under Rules/ at all -- nothing to lock/unlock
        for index in range(last_index + 1):
            self.ruleset_menu.entryconfig(index, state=state)

    def request_scenario_load(self):
        """!
        @brief Scenario -> Load...: opens a popup listing every real scenario
            (list_available_scenarios(), DM_Rules.py -- character_test excluded) for whichever
            character is currently self._pending_character (set by request_character_creation,
            which is also what unlocks this menu entry in the first place). Mirrors
            request_load's own save-slot picker Toplevel almost exactly, just listing
            scenarios instead of save slots. Publishes "scenario_selected" {"scenario_name",
            "character", "setting"} for the chosen scenario -- "setting" is whatever the
            Ruleset menu was set to at Create... time (self.setting_var, now locked -- see
            _set_ruleset_menu_enabled), so the scenario listed/loaded actually matches the
            Rules/<setting> the character itself was built from. LLDM.py's own
            on_scenario_selected is what actually constructs DMCore with them, since GUICore
            never does that itself -- then locks this menu entry shut again, since Create...
            (and, downstream of it, this picker) only ever starts the first game a session has.
        """
        if self._pending_character is None:
            return

        picker = tk.Toplevel(self.root)
        picker.title("Load Scenario")
        picker.transient(self.root)
        picker.grab_set()

        scenarios = list_available_scenarios(self.setting_var.get())
        if not scenarios:
            tk.Label(picker, text="No scenarios found.").pack(padx=10, pady=10)
            tk.Button(picker, text="Close", command=picker.destroy).pack(pady=(0, 10))
            return

        scenario_keys = [key for key, _name, _description in scenarios]
        listbox = tk.Listbox(picker, width=50)
        for _key, name, _description in scenarios:
            listbox.insert(tk.END, name)
        listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        listbox.selection_set(0)

        def load_selected(event=None):
            selection = listbox.curselection()
            if not selection:
                return
            scenario_name = scenario_keys[selection[0]]
            picker.destroy()
            character = self._pending_character
            self._pending_character = None
            self._set_scenario_menu_enabled(False)
            self.event_bus.publish(
                "scenario_selected",
                {
                    "scenario_name": scenario_name,
                    "character": character,
                    "setting": self.setting_var.get(),
                },
            )

        listbox.bind("<Double-Button-1>", load_selected)

        button_row = tk.Frame(picker)
        button_row.pack(fill=tk.X, padx=10, pady=(0, 10))
        tk.Button(button_row, text="Load", command=load_selected).pack(side=tk.LEFT)
        tk.Button(button_row, text="Cancel", command=picker.destroy).pack(side=tk.RIGHT)

    def _on_game_started(self, rules_data):
        """!
        @brief Fired alongside display_party_status on every "rules_loaded" -- which fires
            once per DMCore construction, covering every boot route (CLI quick-boot, Character
            -> Create... + Scenario -> Load..., and File -> Load...) -- so Scenario -> Load...,
            meaningful only in the narrow window before a game exists, can't be reopened by a
            later Create... attempt once one actually has (see CLAUDE.md's "Booting the game":
            Create... only ever starts the first game a session has).
        @param rules_data The "rules_loaded" payload (unused -- only the event's firing matters).
        """
        self._game_started = True
        self._pending_character = None
        self._set_scenario_menu_enabled(False)
        self._set_ruleset_menu_enabled(False)

    def request_save(self):
        """!
        @brief Prompts for a save name via a popup dialog, then publishes "save_requested" --
            the same event NLPCore's text intercept publishes for "save as <slot>", so
            DMCore/LLMCore handle it identically regardless of which trigger fired it.
        """
        slot_name = simpledialog.askstring("Save Game", "Name this save:", parent=self.root)
        if not slot_name:
            return
        slot_name = slot_name.strip()
        if not slot_name:
            return
        self.event_bus.publish("save_requested", {"slot": slot_name})

    def request_load(self):
        """!
        @brief Opens a popup listing every existing save slot (a subdirectory of Saves/)
            and publishes "load_requested" for whichever one the user picks.
        """
        picker = tk.Toplevel(self.root)
        picker.title("Load Game")
        picker.transient(self.root)
        picker.grab_set()

        slots = self._list_save_slots()
        if not slots:
            tk.Label(picker, text="No saved games found.").pack(padx=10, pady=10)
            tk.Button(picker, text="Close", command=picker.destroy).pack(pady=(0, 10))
            return

        listbox = tk.Listbox(picker, width=40)
        for slot in slots:
            listbox.insert(tk.END, slot)
        listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        listbox.selection_set(0)

        def load_selected(event=None):
            selection = listbox.curselection()
            if not selection:
                return
            slot_name = listbox.get(selection[0])
            picker.destroy()
            self.event_bus.publish("load_requested", {"slot": slot_name})

        listbox.bind("<Double-Button-1>", load_selected)

        button_row = tk.Frame(picker)
        button_row.pack(fill=tk.X, padx=10, pady=(0, 10))
        tk.Button(button_row, text="Load", command=load_selected).pack(side=tk.LEFT)
        tk.Button(button_row, text="Cancel", command=picker.destroy).pack(side=tk.RIGHT)

    def _list_save_slots(self):
        """!
        @brief Every existing save slot -- a subdirectory of Saves/ -- sorted for display.
            Resolved the same script-relative way as _save_slot_dir, so this lists the same
            directory Save/Load actually read and write regardless of the process's cwd.
        """
        saves_dir = os.path.join(PROJECT_ROOT, SAVES_DIR)
        if not os.path.isdir(saves_dir):
            return []
        return sorted(
            name for name in os.listdir(saves_dir)
            if os.path.isdir(os.path.join(saves_dir, name))
        )

    def display_game_saved(self, data):
        self.append_to_history(f"[System] Game saved as '{data.get('slot')}'.\n\n")

    def display_game_loaded(self, data):
        self.append_to_history(f"[System] Game loaded from '{data.get('slot')}'.\n\n")

    def display_game_load_failed(self, data):
        self.append_to_history(f"[System] No save named '{data.get('slot')}' found.\n\n")

    def _resolve_equip_slots(self, entity, equip_slot_rules):
        """!
        @brief Mirrors DMCore.get_equip_slots' own override precedence (DM_Rules.py) so the
            Party tab can list every valid [entity.equipped] slot for a member -- including
            one currently unfilled -- without DMCore needing to publish a pre-resolved list
            per entity. A "subtype"-specific rule for this entity's own supertype beats a
            supertype-only rule (no "subtype" key at all).
        @param entity The party member's own entity dict (reads "supertype"/"subtype").
        @param equip_slot_rules The "equip_slots" list from the rules_loaded/
            party_status_changed payload (rules.toml's own [[equip_slot]] table).
        @return The list of valid slot names, or [] if no rule matches this entity at all.
        """
        supertype = entity.get("supertype")
        subtype = entity.get("subtype")
        supertype_only_slots = None
        for rule in equip_slot_rules:
            if rule.get("supertype") != supertype:
                continue
            if "subtype" in rule:
                if rule.get("subtype") == subtype:
                    return list(rule.get("slots", []))
            elif supertype_only_slots is None:
                supertype_only_slots = list(rule.get("slots", []))
        return supertype_only_slots if supertype_only_slots is not None else []

    def display_party_status(self, rules_data):
        """!
        @brief Rebuilds the Party tab from "rules_loaded" (boot/new game) or
            "party_status_changed" (after anything that can change a party member's own
            state -- see DM_Core.py's _publish_party_status): one collapsible tree node per
            party member -- an entity with is_player = true (the player) or is_party = true
            (an ally like thane, see entity_schema.toml's is_party) that's actually part of
            the current playthrough -- labeled with its current/max HP and each expanding
            into its own Equipment/Skills/Abilities/Inventory/Conditions groups.
        @param rules_data The event payload ({"entities": ..., "equip_slots": ...,
            "scenario_entities": ...}, plus "skills" for "rules_loaded" specifically -- the
            skill catalog, unused here since each member's own skills live on the entity
            itself). "scenario_entities" is what keeps an is_party template not actually part
            of the current scenario off the Party tab just for sitting in self.entities --
            self.entities alone can't tell an instanced party member apart from an uninstanced
            template in the same dict (see DM_Combat.py's get_party_challenge_rating, which
            filters the same way; CLAUDE.md's "Architecture" has a worked example, though every
            is_party entity in Rules/Fantasy/ is scenario-local today, so no real content
            currently exercises this beyond a synthetic test).
        """
        self.event_bus.publish("log_info", "Displaying party status.")
        self.party_tree.delete(*self.party_tree.get_children())

        entities = rules_data.get("entities", {})
        equip_slot_rules = rules_data.get("equip_slots", [])
        scenario_entities = set(rules_data.get("scenario_entities", []))
        for entity_key, entity in entities.items():
            if entity_key not in scenario_entities:
                continue
            if not (entity.get("is_player") or entity.get("is_party")):
                continue
            name = entity.get("name", entity_key)
            hp = entity.get("hp", entity.get("max_hp", 0))
            max_hp = entity.get("max_hp", 0)
            member = self.party_tree.insert("", tk.END, text=f"{name} (HP: {hp}/{max_hp})", open=True)

            equipment_node = self.party_tree.insert(member, tk.END, text="Equipment", open=False)
            equipped = entity.get("equipped", {}) or {}
            # Every valid slot for this entity's own supertype/subtype is listed, filled or
            # not, so an empty ring/back slot is visible rather than just absent; a slot the
            # entity has equipped but that isn't actually valid (a data mismatch
            # _validate_equipped_slots already logs at load time) is still shown, appended
            # after the valid ones, so nothing equipped is silently hidden.
            valid_slots = self._resolve_equip_slots(entity, equip_slot_rules)
            slot_order = valid_slots + [slot for slot in equipped if slot not in valid_slots]
            for slot in slot_order:
                self.party_tree.insert(equipment_node, tk.END, text=f"{slot}: {equipped.get(slot, '(empty)')}")
            if not slot_order:
                self.party_tree.insert(equipment_node, tk.END, text="(none)")

            skills_node = self.party_tree.insert(member, tk.END, text="Skills", open=False)
            skills = entity.get("skills", {}) or {}
            for skill_name, stats in skills.items():
                dice = stats.get("dice", 0)
                pips = stats.get("pips", 0)
                rating = f"{dice}D" + (f"+{pips}" if pips else "")
                self.party_tree.insert(skills_node, tk.END, text=f"{skill_name}: {rating}")
            if not skills:
                self.party_tree.insert(skills_node, tk.END, text="(none)")

            abilities_node = self.party_tree.insert(member, tk.END, text="Abilities", open=False)
            abilities = entity.get("abilities", []) or []
            for ability in abilities:
                label = ability if isinstance(ability, str) else ability.get("name", "ability")
                self.party_tree.insert(abilities_node, tk.END, text=label)
            if not abilities:
                self.party_tree.insert(abilities_node, tk.END, text="(none)")

            inventory_node = self.party_tree.insert(member, tk.END, text="Inventory", open=False)
            items = entity.get("inventory", []) or []
            for item, count in Counter(items).items():
                label = item if count == 1 else f"{item} x{count}"
                self.party_tree.insert(inventory_node, tk.END, text=label)
            if not items:
                self.party_tree.insert(inventory_node, tk.END, text="(none)")

            conditions_node = self.party_tree.insert(member, tk.END, text="Conditions", open=False)
            active_conditions = Combat_Resolution.get_active_conditions(entities, entity_key) or {}
            for condition_name in active_conditions:
                self.party_tree.insert(conditions_node, tk.END, text=condition_name)
            if not active_conditions:
                self.party_tree.insert(conditions_node, tk.END, text="(none)")

    def set_map_pen_color(self, color):
        self.map_pen_color = color

    def _on_map_draw_start(self, event):
        self._map_last_point = (event.x, event.y)

    def _on_map_draw_move(self, event):
        if self._map_last_point is not None:
            x0, y0 = self._map_last_point
            self.map_canvas.create_line(
                x0, y0, event.x, event.y,
                fill=self.map_pen_color, width=2, capstyle=tk.ROUND, smooth=True,
            )
        self._map_last_point = (event.x, event.y)

    def _on_map_draw_end(self, event):
        self._map_last_point = None

    def clear_map(self):
        self.map_canvas.delete("all")

    def display_notes(self, notes_content):
        """!
        @brief Shows notes to the user.
        @param notes_content The text or data of the notes.
        """
        self.event_bus.publish("log_info", "Displaying notes.")
        self.notes_text.delete(1.0, tk.END)
        self.notes_text.insert(tk.END, notes_content)

    def _save_slot_dir(self, slot_name):
        """!
        @brief Mirrors DMCore._save_slot_dir/LLMCore._save_slot_dir exactly. GUICore has no
            reference to either -- the three cores only ever talk through events -- so this
            small path helper is deliberately duplicated here too, and must stay in sync:
            all three write sibling files into the same Saves/<slot_name>/ directory.
        @param slot_name The save slot's name, as given by the player.
        @return The absolute directory path for this slot.
        """
        safe_name = os.path.basename(slot_name.strip()) or "unnamed"
        return os.path.join(PROJECT_ROOT, SAVES_DIR, safe_name)

    def save_game(self, slot_name):
        """!
        @brief Writes this core's own slice of a save slot -- currently just the Notes tab's
            free text -- to Saves/<slot_name>/gui_state.json. DMCore/LLMCore independently
            write their own sibling files for the same slot (see CLAUDE.md's "Saving and
            loading" for why this isn't one combined file).
        @param slot_name The save slot's name (used as a directory name under Saves/).
        """
        slot_dir = self._save_slot_dir(slot_name)
        os.makedirs(slot_dir, exist_ok=True)
        data = {
            "version": 1,
            "notes": self.notes_text.get("1.0", "end-1c"),
        }
        with open(os.path.join(slot_dir, "gui_state.json"), "w") as f:
            json.dump(data, f, indent=2)
        self.event_bus.publish("log_info", f"GUI state saved to slot '{slot_name}'.")

    def load_game(self, slot_name):
        """!
        @brief Restores the Notes tab's text from Saves/<slot_name>/gui_state.json. A missing
            file just logs and leaves the current Notes tab alone -- DMCore's own load_game is
            what publishes "game_load_failed" for narrating that to the player, so this
            doesn't duplicate that feedback.
        @param slot_name The save slot's name to load.
        """
        path = os.path.join(self._save_slot_dir(slot_name), "gui_state.json")
        if not os.path.exists(path):
            self.event_bus.publish("log_error", f"No GUI state for slot '{slot_name}'.")
            return

        with open(path, "r") as f:
            data = json.load(f)

        self.display_notes(data.get("notes", ""))
        self.event_bus.publish("log_info", f"GUI state loaded from slot '{slot_name}'.")

    def _on_save_requested(self, data):
        """!
        @brief Event handler for a save request (from NLPCore's text intercept or the File
            menu's Save..., both publishing the same event DMCore/LLMCore also subscribe to).
        @param data The "save_requested" payload ({"slot": slot_name}).
        """
        slot_name = data.get("slot")
        if not slot_name:
            return
        self.save_game(slot_name)

    def _on_load_requested(self, data):
        """!
        @brief Event handler for a load request, mirroring _on_save_requested.
        @param data The "load_requested" payload ({"slot": slot_name}).
        """
        slot_name = data.get("slot")
        if not slot_name:
            return
        self.load_game(slot_name)

    def get_user_input(self):
        """!
        @brief Captures input from the user through the interface.
        @return The raw input string or event data.
        """
        self.event_bus.publish("log_info", "Waiting for user input.")
        self.root.update()
        input_val = self.user_input
        self.user_input = ""
        return input_val
