"""!
@file Character_Creation_GUI.py
@brief The interactive character-creation dialog: pick a race, spend a fixed pool of skill dice
    across every skill, then optionally spend the character's own starting XP training
    individual skills further, one pip at a time (see Character_Creation.py for the underlying
    race/point-buy/training math -- this file is pure Tkinter UI on top of it, no game rules of
    its own).
"""

import tkinter as tk
from tkinter import ttk

from resolution.Character_Creation import (
    get_race, race_baseline_skills, spend_exp_on_skills, validate_allocation,
)


class CharacterCreationDialog(tk.Toplevel):
    """!
    @brief A modal Toplevel: an optional name field, a race selector, a scrollable per-skill
        allocation list below (baseline dice, an editable spend, the resulting total, and a
        "Train" button spending starting XP one pip at a time -- see spend_exp_on_skills), a
        running "dice remaining"/"XP remaining" counter pair, and Create/Cancel buttons. Blocks
        the caller via wait_window() -- LLDM.py's main() constructs this with GUICore's own
        root as parent, before DMCore exists, the same way GUICore.request_load already blocks
        on a Toplevel/Listbox selection (see GUI_Core.py). self.result is {"race": ...,
        "allocation": {...}, "pip_spend": [...], "name": ...} once "Create" is pressed, or None
        if cancelled/closed -- the exact shape DMCore.__init__'s own "character" param expects
        (see DM_CharacterCreation.py). "name" is always present but may be "" (left blank),
        which apply_character_creation treats as "keep the player template's own name" rather
        than renaming anything.
    """

    def __init__(self, parent, skills, races, character_creation, player_exp=0):
        """!
        @param parent The Tk root/Toplevel this dialog is transient to and modal over.
        @param skills {name: skill_table}, from Character_Creation.load_character_creation_data.
        @param races The list of race tables (same source).
        @param character_creation The point-buy constants table (same source).
        @param player_exp The is_player template's own starting "exp" balance (ex:
            Character_Creation.load_player_starting_exp) -- what the "Train" button spends
            from. Defaults to 0 (no XP to train with, every "Train" button starts disabled)
            rather than raising, so a caller with no player template resolvable yet still gets
            a usable dialog.
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
        self.player_exp = player_exp
        self.name_var = tk.StringVar(value="")
        self.result = None

        self.skill_names = sorted(skills.keys())
        self.allocation_vars = {name: tk.IntVar(value=0) for name in self.skill_names}
        self.total_labels = {}
        self.train_buttons = {}
        self.baseline = {}
        # The literal, ordered "raise this skill by one more pip" replay log --
        # spend_exp_on_skills' own third argument, and exactly what self.result's own
        # "pip_spend" ends up as verbatim. self.trained_skills/self.remaining_exp (below) are
        # purely this list's own cached replay result, recomputed by _recompute_training
        # whenever anything that could change it happens (race switch, allocation edit, or a
        # new "Train" click) -- never hand-maintained incrementally, so there's only ever one
        # source of truth to keep in sync.
        self.pip_spend = []
        self.trained_skills = {}
        self.remaining_exp = player_exp

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
        self.remaining_label.pack(pady=(0, 0))
        self.exp_remaining_label = tk.Label(self, text="", font=("TkDefaultFont", 10, "bold"))
        self.exp_remaining_label.pack(pady=(0, 5))

        header_row = tk.Frame(self)
        header_row.pack(fill=tk.X, padx=10)
        for text, width in (
            ("Skill", 18), ("Baseline", 10), ("Allocate", 10), ("Total", 8), ("Train", 14),
        ):
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

            train_button = tk.Button(
                row, text="Train", width=12,
                command=lambda n=name: self._on_train_clicked(n),
            )
            train_button.pack(side=tk.LEFT)
            self.train_buttons[name] = train_button

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
            skill's own baseline/cap relationship can change entirely), refreshes the
            description text plus every row's displayed numbers, and clears self.pip_spend too
            (same reasoning -- a training purchase's own cost was based on a dice count this
            race switch may have just changed out from under it, so there's nothing sensible
            left to replay).
        """
        race = self._current_race()
        self.race_description_label.config(text=(race or {}).get("description", ""))
        self.baseline = race_baseline_skills(self.skills, race)
        self.pip_spend = []

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
        @brief Recomputes the remaining-dice counter, and enables "Create" only once every
            allocated dice is spent (remaining == 0) -- validate_allocation itself is still the
            real gate at Create time, this is purely so the button's own enabled state matches
            what a confirm would actually accept. Row "Total" labels are left to
            _recompute_training (called at the end of this method), since a row's own final
            total now also depends on however many pips have been trained onto it.
        """
        spent = 0
        for name in self.skill_names:
            spent += self.allocation_vars[name].get()

        remaining = self.pool_dice - spent
        self.remaining_label.config(text=f"Dice remaining: {remaining} / {self.pool_dice}")
        self.create_button.config(state=tk.NORMAL if remaining == 0 else tk.DISABLED)

        self._recompute_training()

    def _current_base_skills(self):
        """!
        @return {skill_name: {"dice": int, "pips": 0}} -- baseline plus whatever's currently
                allocated in each skill's own Spinbox, the same shape build_character_skills
                itself produces. What spend_exp_on_skills replays self.pip_spend on top of, so
                the training cost/rollover the player sees here always matches whatever
                point-buy spend is currently on-screen, even before "Create" is pressed.
        """
        return {
            name: {"dice": self.baseline[name] + self.allocation_vars[name].get(), "pips": 0}
            for name in self.skill_names
        }

    def _recompute_training(self):
        """!
        @brief The single source of truth for everything training-related on screen --
            self.trained_skills/self.remaining_exp are never hand-updated incrementally, only
            ever rebuilt here by replaying self.pip_spend (via spend_exp_on_skills) on top of
            _current_base_skills() and self.player_exp. If an allocation/race change made under
            self.pip_spend no longer affordable (its own cost basis shifted), the offending
            *and every later* entry are dropped -- trimmed one at a time off the end until the
            replay succeeds again -- rather than leaving the dialog stuck showing a rejected
            state; a brand new "Train" click (_on_train_clicked) only ever appends once this
            method has already confirmed the click is affordable, so it can never itself be the
            entry that gets trimmed. Refreshes every row's own "Total" label/"Train" button text
            and enabled state, plus the "XP remaining" counter, to match.
        """
        base_skills = self._current_base_skills()
        while True:
            trained, remaining, reason = spend_exp_on_skills(base_skills, self.player_exp, self.pip_spend)
            if reason is None:
                break
            self.pip_spend.pop()
        self.trained_skills = trained
        self.remaining_exp = remaining

        for name in self.skill_names:
            entry = trained[name]
            pip_suffix = f" +{entry['pips']}p" if entry["pips"] else ""
            self.total_labels[name]["total"].config(text=f"{entry['dice']}D{pip_suffix}")

            cost = entry["dice"]
            self.train_buttons[name].config(text=f"Train ({cost} xp)")
            self.train_buttons[name].config(
                state=tk.NORMAL if self.remaining_exp >= cost else tk.DISABLED
            )

        self.exp_remaining_label.config(text=f"XP remaining: {self.remaining_exp} / {self.player_exp}")

    def _on_train_clicked(self, name):
        """!
        @brief Spends one more pip on name, if currently affordable (self.trained_skills'/
            self.remaining_exp's own latest _recompute_training snapshot already reflects the
            live cost) -- appends to self.pip_spend and recomputes. A no-op click (ex: a stale
            button click racing a race switch that just invalidated everything) can't overspend,
            since _recompute_training is always what actually decides the outcome, never this
            method's own belief about affordability.
        @param name The skill whose "Train" button was just clicked.
        """
        if self.remaining_exp < self.trained_skills[name]["dice"]:
            return
        self.pip_spend.append(name)
        self._recompute_training()

    def _on_create(self):
        """!
        @brief Validates the current allocation one last time (belt-and-suspenders against
            the Create button's own enabled-state check) and, if it passes, sets self.result
            (including self.pip_spend verbatim -- apply_character_creation replays it fresh
            server-side rather than trusting anything computed here) and closes the dialog.
        """
        race_name = self.race_combo.get()
        allocation = {name: var.get() for name, var in self.allocation_vars.items() if var.get()}
        ok, reason = validate_allocation(self.skills, self._current_race(), self.character_creation, allocation)
        if not ok:
            self.remaining_label.config(text=reason)
            return
        self.result = {
            "race": race_name, "allocation": allocation, "pip_spend": list(self.pip_spend),
            "name": self.name_var.get().strip(),
        }
        self.destroy()

    def _on_cancel(self):
        """!
        @brief Leaves self.result as None (the caller's signal to fall back to the player
            template's own default, untouched skills -- see DM_CharacterCreation.py).
        """
        self.result = None
        self.destroy()


def run_character_creation_dialog(parent, skills, races, character_creation, player_exp=0):
    """!
    @brief Constructs and blocks on a CharacterCreationDialog until it's closed.
    @param parent The Tk root/Toplevel to parent the dialog to.
    @param skills, races, character_creation See Character_Creation.load_character_creation_data.
    @param player_exp The is_player template's own starting "exp" (ex:
        Character_Creation.load_player_starting_exp) -- forwarded to CharacterCreationDialog.
    @return {"race": ..., "allocation": {...}, "pip_spend": [...], "name": ...}, or None if
            cancelled -- ready to pass straight into DMCore's own "character" constructor param.
    """
    dialog = CharacterCreationDialog(parent, skills, races, character_creation, player_exp)
    parent.wait_window(dialog)
    return dialog.result
