"""!
@file Character_Creation_GUI.py
@brief The interactive character-creation dialog: pick a race, then spend a fixed pool of
    skill dice across every skill (see Character_Creation.py for the underlying race/point-buy
    data and math -- this file is pure Tkinter UI on top of it, no game rules of its own).
"""

import tkinter as tk
from tkinter import ttk

from resolution.Character_Creation import get_race, race_baseline_skills, validate_allocation


class CharacterCreationDialog(tk.Toplevel):
    """!
    @brief A modal Toplevel: an optional name field, a race selector, a scrollable per-skill
        allocation list below (baseline dice, an editable spend, and the resulting total), a
        running "dice remaining" counter, and Create/Cancel buttons. Blocks the caller via
        wait_window() -- LLDM.py's main() constructs this with GUICore's own root as parent,
        before DMCore exists, the same way GUICore.request_load already blocks on a
        Toplevel/Listbox selection (see GUI_Core.py). self.result is {"race": ..., "allocation":
        {...}, "name": ...} once "Create" is pressed, or None if cancelled/closed -- the exact
        shape DMCore.__init__'s own "character" param expects (see DM_CharacterCreation.py).
        "name" is always present but may be "" (left blank), which apply_character_creation
        treats as "keep the player template's own name" rather than renaming anything.
    """

    def __init__(self, parent, skills, races, character_creation):
        """!
        @param parent The Tk root/Toplevel this dialog is transient to and modal over.
        @param skills {name: skill_table}, from Character_Creation.load_character_creation_data.
        @param races The list of race tables (same source).
        @param character_creation The point-buy constants table (same source).
        """
        super().__init__(parent)
        self.title("Create Your Character")
        self.transient(parent)
        self.grab_set()
        self.resizable(False, True)

        self.skills = skills
        self.races = races
        self.character_creation = character_creation
        self.pool_dice = character_creation.get("pool_dice", 15)
        self.max_per_skill = character_creation.get("max_allocation_per_skill", 5)
        self.name_var = tk.StringVar(value="")
        self.result = None

        self.skill_names = sorted(skills.keys())
        self.allocation_vars = {name: tk.IntVar(value=0) for name in self.skill_names}
        self.total_labels = {}
        self.baseline = {}

        self._build_widgets()
        race_names = [race.get("name", "") for race in self.races]
        if race_names:
            self.race_combo.current(0)
            self._on_race_selected()

    def _build_widgets(self):
        """!
        @brief Lays out every widget once; _on_race_selected/_on_allocation_changed only ever
            update values on these same widgets afterward, never rebuild them.
        """
        name_row = tk.Frame(self)
        name_row.pack(fill=tk.X, padx=10, pady=(10, 0))
        tk.Label(name_row, text="Name:").pack(side=tk.LEFT)
        tk.Entry(name_row, textvariable=self.name_var, width=24).pack(side=tk.LEFT, padx=(5, 0))
        tk.Label(
            name_row, text="(optional -- leave blank to keep the default character's own name)",
        ).pack(side=tk.LEFT, padx=(10, 0))

        race_row = tk.Frame(self)
        race_row.pack(fill=tk.X, padx=10, pady=(10, 5))
        tk.Label(race_row, text="Race:").pack(side=tk.LEFT)
        self.race_combo = ttk.Combobox(
            race_row, values=[race.get("name", "") for race in self.races],
            state="readonly", width=20,
        )
        self.race_combo.pack(side=tk.LEFT, padx=(5, 10))
        self.race_combo.bind("<<ComboboxSelected>>", lambda event: self._on_race_selected())

        self.race_description_label = tk.Label(race_row, text="", wraplength=320, justify=tk.LEFT)
        self.race_description_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.remaining_label = tk.Label(self, text="", font=("TkDefaultFont", 10, "bold"))
        self.remaining_label.pack(pady=(0, 5))

        header_row = tk.Frame(self)
        header_row.pack(fill=tk.X, padx=10)
        for text, width in (("Skill", 18), ("Baseline", 10), ("Allocate", 10), ("Total", 8)):
            tk.Label(header_row, text=text, width=width, anchor=tk.W).pack(side=tk.LEFT)

        list_frame = tk.Frame(self)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10)
        canvas = tk.Canvas(list_frame, height=360, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        rows_frame = tk.Frame(canvas)
        canvas.create_window((0, 0), window=rows_frame, anchor=tk.NW)
        rows_frame.bind(
            "<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox(tk.ALL))
        )

        for name in self.skill_names:
            row = tk.Frame(rows_frame)
            row.pack(fill=tk.X)
            tk.Label(row, text=name, width=18, anchor=tk.W).pack(side=tk.LEFT)
            baseline_label = tk.Label(row, text="-", width=10, anchor=tk.W)
            baseline_label.pack(side=tk.LEFT)
            self.total_labels[name] = {"baseline": baseline_label}

            var = self.allocation_vars[name]
            spin = ttk.Spinbox(
                row, from_=0, to=self.max_per_skill, width=6, textvariable=var,
                command=self._on_allocation_changed,
            )
            spin.pack(side=tk.LEFT, padx=(0, 10))
            var.trace_add("write", lambda *_args, n=name: self._on_allocation_changed(n))

            total_label = tk.Label(row, text="-", width=8, anchor=tk.W)
            total_label.pack(side=tk.LEFT)
            self.total_labels[name]["total"] = total_label

        button_row = tk.Frame(self)
        button_row.pack(fill=tk.X, padx=10, pady=10)
        self.create_button = tk.Button(button_row, text="Create", command=self._on_create)
        self.create_button.pack(side=tk.RIGHT)
        tk.Button(button_row, text="Cancel", command=self._on_cancel).pack(side=tk.RIGHT, padx=(0, 10))

    def _current_race(self):
        """!
        @return The currently-selected race table, or None if nothing's selected yet.
        """
        return get_race(self.races, self.race_combo.get())

    def _on_race_selected(self):
        """!
        @brief Recomputes every skill's own baseline for the newly-selected race, resets every
            allocation back to 0 (a race switch invalidates whatever was already spent -- a
            skill's own baseline/cap relationship can change entirely), and refreshes the
            description text plus every row's displayed numbers.
        """
        race = self._current_race()
        self.race_description_label.config(text=(race or {}).get("description", ""))
        self.baseline = race_baseline_skills(self.skills, race)

        for name in self.skill_names:
            self.allocation_vars[name].set(0)
            self.total_labels[name]["baseline"].config(text=f"{self.baseline[name]}D")

        self._refresh_totals()

    def _on_allocation_changed(self, name=None):
        """!
        @brief Re-clamps a single skill's own allocation into [0, max_per_skill] (a Spinbox's
            from_/to only constrains its arrows, not manually-typed text) and refreshes every
            derived display value -- called on every keystroke/arrow-click in any skill's own
            Spinbox (IntVar's own write trace, or the Spinbox's command=, whichever fires).
        @param name The skill whose Spinbox just changed, or None (ex: the plain command=
            callback, which doesn't know which one) to just re-clamp everything.
        """
        names = [name] if name else self.skill_names
        for skill_name in names:
            var = self.allocation_vars[skill_name]
            try:
                value = var.get()
            except tk.TclError:
                value = 0
            clamped = max(0, min(self.max_per_skill, value))
            if clamped != value:
                var.set(clamped)
        self._refresh_totals()

    def _refresh_totals(self):
        """!
        @brief Recomputes the remaining-dice counter and every row's own "Total" (baseline +
            allocated) label, and enables "Create" only once every allocated dice is spent
            (remaining == 0) -- validate_allocation itself is still the real gate at Create
            time, this is purely so the button's own enabled state matches what a confirm
            would actually accept.
        """
        spent = 0
        for name in self.skill_names:
            allocated = self.allocation_vars[name].get()
            spent += allocated
            self.total_labels[name]["total"].config(text=f"{self.baseline[name] + allocated}D")

        remaining = self.pool_dice - spent
        self.remaining_label.config(text=f"Dice remaining: {remaining} / {self.pool_dice}")
        self.create_button.config(state=tk.NORMAL if remaining == 0 else tk.DISABLED)

    def _on_create(self):
        """!
        @brief Validates the current allocation one last time (belt-and-suspenders against
            the Create button's own enabled-state check) and, if it passes, sets self.result
            and closes the dialog.
        """
        race_name = self.race_combo.get()
        allocation = {name: var.get() for name, var in self.allocation_vars.items() if var.get()}
        ok, reason = validate_allocation(self.skills, self._current_race(), self.character_creation, allocation)
        if not ok:
            self.remaining_label.config(text=reason)
            return
        self.result = {
            "race": race_name, "allocation": allocation, "name": self.name_var.get().strip(),
        }
        self.destroy()

    def _on_cancel(self):
        """!
        @brief Leaves self.result as None (the caller's signal to fall back to the player
            template's own default, untouched skills -- see DM_CharacterCreation.py).
        """
        self.result = None
        self.destroy()


def run_character_creation_dialog(parent, skills, races, character_creation):
    """!
    @brief Constructs and blocks on a CharacterCreationDialog until it's closed.
    @param parent The Tk root/Toplevel to parent the dialog to.
    @param skills, races, character_creation See Character_Creation.load_character_creation_data.
    @return {"race": ..., "allocation": {...}, "name": ...}, or None if cancelled -- ready to pass straight
            into DMCore's own "character" constructor param.
    """
    dialog = CharacterCreationDialog(parent, skills, races, character_creation)
    parent.wait_window(dialog)
    return dialog.result
