from __future__ import annotations
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText
from typing import TYPE_CHECKING, Dict, Any
import dataclasses
try:
    import yaml
except ImportError:
    print("DebugWindow Error: PyYAML not found. Please install: pip install PyYAML")
    yaml = None
try:
    if TYPE_CHECKING:
        from loader import RulesetLoader
        from models import Entity
    from models import Entity
    from loader import RulesetLoader, create_entity_from_dict
except ImportError as e:
    print(f"DebugWindow Error: Core module not found ({e}).")
    class RulesetLoader:
        characters: Dict[str, Any] = {}
        creatures: Dict[str, Any] = {}
        items: Dict[str, Any] = {}
        spells: Dict[str, Any] = {}
        conditions: Dict[str, Any] = {}
        environment_ents: Dict[str, Any] = {}
    class Entity: pass
    def create_entity_from_dict(data): return None
class EntityDebugTab(ttk.Frame):
    def __init__(self, parent: ttk.Notebook, loader: RulesetLoader):
        """
        Initializes the EntityDebugTab.
        Args:
            parent: The parent notebook widget.
            loader: The RulesetLoader instance containing the game data.
        """
        super().__init__(parent, padding=10)
        self.loader = loader
        self.selected_entity_name: str | None = None
        self.all_entities: Dict[str, Entity] = {}
        if not yaml:
            ttk.Label(self, text="Error: PyYAML library is not installed.\n"
                                "Please run: pip install PyYAML",
                                font=("Arial", 14, "bold"), foreground="red"
            ).pack(expand=True, fill='both')
            return
        self.paned_window = ttk.PanedWindow(self, orient='horizontal')
        self.paned_window.pack(expand=True, fill='both')
        left_frame = ttk.Frame(self.paned_window, padding=5)
        left_frame.grid_rowconfigure(0, weight=1)
        left_frame.grid_columnconfigure(0, weight=1)
        self.entity_listbox = tk.Listbox(left_frame, exportselection=False, font=("Arial", 10))
        self.entity_listbox.grid(row=0, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(left_frame, orient='vertical', command=self.entity_listbox.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self.entity_listbox.config(yscrollcommand=scrollbar.set)
        self.paned_window.add(left_frame, weight=1)
        right_frame = ttk.Frame(self.paned_window, padding=5)
        right_frame.grid_rowconfigure(0, weight=1)
        right_frame.grid_columnconfigure(0, weight=1)
        self.text_editor = ScrolledText(
            right_frame,
            wrap=tk.WORD,
            font=("Courier New", 10),
            state='disabled'
        )
        self.text_editor.grid(row=0, column=0, columnspan=2, sticky='nsew')
        save_button = ttk.Button(
            right_frame,
            text="Save Changes",
            command=self._on_save_changes
        )
        save_button.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(5, 0))
        self.paned_window.add(right_frame, weight=2)
        self._populate_entity_list()
        self.entity_listbox.bind("<<ListboxSelect>>", self._on_entity_select)
    def _populate_entity_list(self):
        self.entity_listbox.delete(0, tk.END)
        self.all_entities.clear()
        self.all_entities.update(self.loader.characters)
        for st_dict in self.loader.entities_by_supertype.values():
            self.all_entities.update(st_dict)
        for entity_name in sorted(self.all_entities.keys()):
            self.entity_listbox.insert(tk.END, entity_name)
    def _on_entity_select(self, event: Any = None):
        selected_indices = self.entity_listbox.curselection()
        if not selected_indices:
            return
        self.selected_entity_name = self.entity_listbox.get(selected_indices[0])
        entity_obj = self.all_entities.get(self.selected_entity_name)
        if not entity_obj:
            self.text_editor.config(state='normal')
            self.text_editor.delete('1.0', tk.END)
            self.text_editor.insert('1.0', f"Error: Could not find entity '{self.selected_entity_name}'")
            self.text_editor.config(state='disabled')
            return
        try:
            entity_dict = dataclasses.asdict(entity_obj)
            entity_yaml = yaml.dump(
                entity_dict,
                indent=2,
                sort_keys=False,
                Dumper=yaml.SafeDumper
            )
            self.text_editor.config(state='normal')
            self.text_editor.delete('1.0', tk.END)
            self.text_editor.insert('1.0', entity_yaml)
        except Exception as e:
            self.text_editor.config(state='normal')
            self.text_editor.delete('1.0', tk.END)
            self.text_editor.insert('1.0', f"Error displaying entity:\n{e}")
            self.text_editor.config(state='disabled')
    def _on_save_changes(self):
        if not self.selected_entity_name:
            messagebox.showerror("Save Error", "No entity is selected.")
            return
        text_content = self.text_editor.get('1.0', tk.END)
        try:
            new_data_dict = yaml.safe_load(text_content)
            if not isinstance(new_data_dict, dict):
                raise ValueError("Edited text is not a valid YAML dictionary.")
        except Exception as e:
            messagebox.showerror("Save Error", f"Failed to parse YAML:\n{e}")
            return
        try:
            new_entity = create_entity_from_dict(new_data_dict)
            if not new_entity:
                raise ValueError("create_entity_from_dict returned None.")
            new_entity.name = self.selected_entity_name
            if self.selected_entity_name in self.loader.characters:
                self.loader.characters[self.selected_entity_name] = new_entity
            else:
                for st_dict in self.loader.entities_by_supertype.values():
                    if self.selected_entity_name in st_dict:
                        st_dict[self.selected_entity_name] = new_entity
                        break
            self.all_entities[self.selected_entity_name] = new_entity
            messagebox.showinfo("Save Successful",
                                f"Successfully saved changes to '{self.selected_entity_name}'.\n"
                                "Note: The main GUI may not refresh until an action occurs.")
        except Exception as e:
            messagebox.showerror("Save Error", f"Failed to update entity object:\n{e}")
class DebugWindow(tk.Toplevel):
    def __init__(self, parent: tk.Tk, loader: RulesetLoader):
        """
        Initializes the DebugWindow.
        Args:
            parent: The parent Tkinter window.
            loader: The RulesetLoader instance.
        """
        super().__init__(parent)
        self.title("LLDM Debug Inspector")
        self.geometry("900x600")
        self.loader = loader
        self.notebook = ttk.Notebook(self)
        self.entity_tab = EntityDebugTab(self.notebook, self.loader)
        self.notebook.add(self.entity_tab, text="Entities")
        self.notebook.pack(expand=True, fill='both')
if __name__ == "__main__":
    from pathlib import Path
    print("--- Initializing DebugWindow Test ---")
    RULESET_PATH = Path(__file__).parent / "rulesets" / "medievalfantasy"
    try:
        loader = RulesetLoader(RULESET_PATH)
        loader.load_all()
        print(f"Loader finished. Loaded {len(loader.characters)} characters.")
    except Exception as e:
        print(f"Fatal Error during loading: {e}")
        exit(1)
    if not loader.characters:
        print("Warning: No characters were loaded.")
    root = tk.Tk()
    root.title("Main App (Test)")
    root.geometry("400x200")
    app = DebugWindow(parent=root, loader=loader)
    ttk.Label(root, text="This is the main app window.\nThe Debug Window is separate.").pack(pady=20)
    print("GUI Main Loop is running...")
    root.mainloop()
