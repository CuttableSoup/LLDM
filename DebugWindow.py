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

# Import classes from your project
try:
    if TYPE_CHECKING:
        from classes import RulesetLoader, Entity
    from classes import create_entity_from_dict, Entity, RulesetLoader
except ImportError:
    print("DebugWindow Error: 'classes.py' not found.")
    # Define placeholder classes if missing
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
    """
    A tab for the DebugWindow that shows a list of all entities
    and an editor panel to view/modify their data as YAML.
    """
    
    def __init__(self, parent: ttk.Notebook, loader: RulesetLoader):
        """
        Initializes the Entity Debug Tab.
        
        Args:
            parent: The parent ttk.Notebook widget.
            loader: The main RulesetLoader instance containing all game data.
        """
        super().__init__(parent, padding=10)
        self.loader = loader
        
        # This will hold the currently selected entity's name
        self.selected_entity_name: str | None = None
        
        # This will hold a combined map of all entities for easy lookup
        self.all_entities: Dict[str, Entity] = {}

        if not yaml:
            ttk.Label(self, text="Error: PyYAML library is not installed.\n"
                                "Please run: pip install PyYAML",
                                font=("Arial", 14, "bold"), foreground="red"
            ).pack(expand=True, fill='both')
            return

        # --- Main Layout ---
        # Create a resizable paned window
        self.paned_window = ttk.PanedWindow(self, orient='horizontal')
        self.paned_window.pack(expand=True, fill='both')

        # --- Left Panel: Entity List ---
        left_frame = ttk.Frame(self.paned_window, padding=5)
        left_frame.grid_rowconfigure(0, weight=1)
        left_frame.grid_columnconfigure(0, weight=1)

        self.entity_listbox = tk.Listbox(left_frame, exportselection=False, font=("Arial", 10))
        self.entity_listbox.grid(row=0, column=0, sticky='nsew')
        
        scrollbar = ttk.Scrollbar(left_frame, orient='vertical', command=self.entity_listbox.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self.entity_listbox.config(yscrollcommand=scrollbar.set)
        
        # Add the left frame to the paned window
        self.paned_window.add(left_frame, weight=1) # Give it 1/3 of the space

        # --- Right Panel: Entity Editor ---
        right_frame = ttk.Frame(self.paned_window, padding=5)
        right_frame.grid_rowconfigure(0, weight=1)
        right_frame.grid_columnconfigure(0, weight=1)

        self.text_editor = ScrolledText(
            right_frame, 
            wrap=tk.WORD, 
            font=("Courier New", 10), 
            state='disabled' # Start disabled
        )
        self.text_editor.grid(row=0, column=0, columnspan=2, sticky='nsew')
        
        save_button = ttk.Button(
            right_frame, 
            text="Save Changes", 
            command=self._on_save_changes
        )
        save_button.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(5, 0))

        # Add the right frame to the paned window
        self.paned_window.add(right_frame, weight=2) # Give it 2/3 of the space

        # --- Final Steps ---
        # Populate the listbox with entity names
        self._populate_entity_list()
        
        # Bind the listbox selection event
        self.entity_listbox.bind("<<ListboxSelect>>", self._on_entity_select)

    def _populate_entity_list(self):
        """
        Gathers all entities from the loader and populates the listbox.
        """
        self.entity_listbox.delete(0, tk.END)
        self.all_entities.clear()
        
        # Combine all entity dictionaries into one
        self.all_entities.update(self.loader.characters)
        self.all_entities.update(self.loader.creatures)
        self.all_entities.update(self.loader.items)
        self.all_entities.update(self.loader.spells)
        self.all_entities.update(self.loader.conditions)
        self.all_entities.update(self.loader.environment_ents)
        
        # Populate the listbox
        for entity_name in sorted(self.all_entities.keys()):
            self.entity_listbox.insert(tk.END, entity_name)
            
    def _on_entity_select(self, event: Any = None):
        """
        Called when an entity is selected in the listbox.
        Displays the entity's data as YAML in the text editor.
        """
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
            # Convert the dataclass object to a dictionary
            entity_dict = dataclasses.asdict(entity_obj)
            
            # Dump the dictionary to a YAML string
            # Use SafeDumper to handle dataclass-to-dict conversion gracefully
            entity_yaml = yaml.dump(
                entity_dict, 
                indent=2, 
                sort_keys=False,
                Dumper=yaml.SafeDumper
            )
            
            # Update the text editor
            self.text_editor.config(state='normal')
            self.text_editor.delete('1.0', tk.END)
            self.text_editor.insert('1.0', entity_yaml)
            
        except Exception as e:
            self.text_editor.config(state='normal')
            self.text_editor.delete('1.0', tk.END)
            self.text_editor.insert('1.0', f"Error displaying entity:\n{e}")
            self.text_editor.config(state='disabled')

    def _on_save_changes(self):
        """
        Called when the 'Save Changes' button is pressed.
        Parses the YAML and updates the entity in the RulesetLoader.
        """
        if not self.selected_entity_name:
            messagebox.showerror("Save Error", "No entity is selected.")
            return

        text_content = self.text_editor.get('1.0', tk.END)
        
        try:
            # 1. Parse the YAML text from the editor
            new_data_dict = yaml.safe_load(text_content)
            if not isinstance(new_data_dict, dict):
                raise ValueError("Edited text is not a valid YAML dictionary.")
                
        except Exception as e:
            messagebox.showerror("Save Error", f"Failed to parse YAML:\n{e}")
            return

        try:
            # 2. Use create_entity_from_dict to create a new Entity object
            # This function is designed to handle nested dicts for skills, etc.
            new_entity = create_entity_from_dict(new_data_dict)
            if not new_entity:
                 raise ValueError("create_entity_from_dict returned None.")
                 
            new_entity.name = self.selected_entity_name # Ensure name consistency
            
            # 3. Find the original entity and replace it in the loader
            if self.selected_entity_name in self.loader.characters:
                self.loader.characters[self.selected_entity_name] = new_entity
            elif self.selected_entity_name in self.loader.creatures:
                self.loader.creatures[self.selected_entity_name] = new_entity
            elif self.selected_entity_name in self.loader.items:
                self.loader.items[self.selected_entity_name] = new_entity
            elif self.selected_entity_name in self.loader.spells:
                self.loader.spells[self.selected_entity_name] = new_entity
            elif self.selected_entity_name in self.loader.conditions:
                self.loader.conditions[self.selected_entity_name] = new_entity
            elif self.selected_entity_name in self.loader.environment_ents:
                self.loader.environment_ents[self.selected_entity_name] = new_entity
            
            # 4. Update our internal all_entities map
            self.all_entities[self.selected_entity_name] = new_entity
            
            messagebox.showinfo("Save Successful", 
                                f"Successfully saved changes to '{self.selected_entity_name}'.\n"
                                "Note: The main GUI may not refresh until an action occurs.")

        except Exception as e:
            messagebox.showerror("Save Error", f"Failed to update entity object:\n{e}")


class DebugWindow(tk.Toplevel):
    """
    A separate top-level window for debugging the game state,
    inspecting loaded data, and modifying entities.
    """
    
    def __init__(self, parent: tk.Tk, loader: RulesetLoader):
        """
        Initializes the debug window.
        
        Args:
            parent: The root tk.Tk() application window.
            loader: The main RulesetLoader instance.
        """
        super().__init__(parent)
        self.title("LLDM Debug Inspector")
        self.geometry("900x600")
        
        self.loader = loader
        
        # Create the main tabbed notebook
        self.notebook = ttk.Notebook(self)
        
        # --- Entities Tab ---
        self.entity_tab = EntityDebugTab(self.notebook, self.loader)
        self.notebook.add(self.entity_tab, text="Entities")
        
        # --- (Future Tabs) ---
        # You can add more tabs here later
        # e.g., ruleset_tab = ttk.Frame(self.notebook)
        # self.notebook.add(ruleset_tab, text="Rulesets")
        
        self.notebook.pack(expand=True, fill='both')

if __name__ == "__main__":
    """
    Example of how to run the DebugWindow for testing.
    This requires 'classes.py' and a valid ruleset path.
    """
    from pathlib import Path
    
    print("--- Initializing DebugWindow Test ---")
    
    # 1. Set the ruleset path
    RULESET_PATH = Path(__file__).parent / "rulesets" / "medievalfantasy"
    
    # 2. Initialize and run the loader
    try:
        loader = RulesetLoader(RULESET_PATH)
        loader.load_all()
        print(f"Loader finished. Loaded {len(loader.characters)} characters.")
    except Exception as e:
        print(f"Fatal Error during loading: {e}")
        exit(1)
        
    if not loader.characters:
        print("Warning: No characters were loaded.")
        
    # 4. Create the root tkinter window
    root = tk.Tk()
    root.title("Main App (Test)")
    root.geometry("400x200")
    
    # 5. Create the main debug window
    app = DebugWindow(root_widget=root, loader=loader)
    
    ttk.Label(root, text="This is the main app window.\nThe Debug Window is separate.").pack(pady=20)
    
    # 6. Start the app
    print("GUI Main Loop is running...")
    root.mainloop()