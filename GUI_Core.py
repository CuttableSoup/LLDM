import tkinter as tk
from tkinter import ttk

class GUICore:
    """!
    @brief Main class handling the display and user interaction.
    """

    def __init__(self, event_bus):
        """!
        @brief Initializes the GUI components.
        @param event_bus The central event bus instance.
        """
        self.event_bus = event_bus
        
        self.root = tk.Tk()
        self.root.title("LLDM Interface")
        self.root.geometry("1000x600")
        
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
        
        self.party_tab = tk.Frame(self.notebook)
        self.party_text = tk.Text(self.party_tab, wrap=tk.WORD)
        self.party_text.pack(fill=tk.BOTH, expand=True)
        self.notebook.add(self.party_tab, text="Party Status")
        
        self.notes_tab = tk.Frame(self.notebook)
        self.notes_text = tk.Text(self.notes_tab, wrap=tk.WORD)
        self.notes_text.pack(fill=tk.BOTH, expand=True)
        self.notebook.add(self.notes_tab, text="Notes")
        
        self.map_tab = tk.Frame(self.notebook)
        self.map_text = tk.Text(self.map_tab, wrap=tk.NONE)
        self.map_text.pack(fill=tk.BOTH, expand=True)
        self.notebook.add(self.map_tab, text="Map")

        self.debug_tab = tk.Frame(self.notebook)
        self.debug_text = tk.Text(self.debug_tab, wrap=tk.WORD)
        self.debug_text.pack(fill=tk.BOTH, expand=True)
        self.notebook.add(self.debug_tab, text="Debug")
        
        self.input_frame = tk.Frame(self.root)
        self.input_frame.pack(fill=tk.X, side=tk.BOTTOM, padx=5, pady=5)

        self.input_entry = tk.Entry(self.input_frame)
        self.input_entry.pack(fill=tk.X, expand=True, side=tk.LEFT)
        self.input_entry.bind("<Return>", self.handle_input)

        self.submit_button = tk.Button(self.input_frame, text="Submit", command=self.submit_input)
        self.submit_button.pack(side=tk.RIGHT, padx=(5, 0))

        # A slot-name field plus Save/Load buttons -- deliberately just another way to publish
        # the same "save_requested"/"load_requested" events NLPCore's text intercept publishes
        # (ex: "save as arena-run-1"), not a separate mechanism. Sits above the input row so the
        # main input stays the bottom-most, most-used control.
        self.save_frame = tk.Frame(self.root)
        self.save_frame.pack(fill=tk.X, side=tk.BOTTOM, padx=5)

        self.slot_entry = tk.Entry(self.save_frame)
        self.slot_entry.pack(fill=tk.X, expand=True, side=tk.LEFT)

        self.load_button = tk.Button(self.save_frame, text="Load", command=self.request_load)
        self.load_button.pack(side=tk.RIGHT, padx=(5, 0))

        self.save_button = tk.Button(self.save_frame, text="Save", command=self.request_save)
        self.save_button.pack(side=tk.RIGHT, padx=(5, 0))

        self.user_input = ""
        self.event_bus.publish("log_info", "GUICore initialized.")
        self.event_bus.subscribe("llm_response_ready", self.display_llm_response)
        self.event_bus.subscribe("rules_loaded", self.display_debug_info)
        self.event_bus.subscribe("game_saved", self.display_game_saved)
        self.event_bus.subscribe("game_loaded", self.display_game_loaded)
        self.event_bus.subscribe("game_load_failed", self.display_game_load_failed)

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

    def request_save(self):
        """!
        @brief Publishes "save_requested" with the slot-name field's current text -- the same
               event NLPCore's text intercept publishes for "save as <slot>", so DMCore/LLMCore
               handle it identically regardless of which trigger fired it.
        """
        slot_name = self.slot_entry.get().strip()
        if not slot_name:
            return
        self.event_bus.publish("save_requested", {"slot": slot_name})

    def request_load(self):
        """!
        @brief Publishes "load_requested", mirroring request_save.
        """
        slot_name = self.slot_entry.get().strip()
        if not slot_name:
            return
        self.event_bus.publish("load_requested", {"slot": slot_name})

    def display_game_saved(self, data):
        self.append_to_history(f"[System] Game saved as '{data.get('slot')}'.\n\n")

    def display_game_loaded(self, data):
        self.append_to_history(f"[System] Game loaded from '{data.get('slot')}'.\n\n")

    def display_game_load_failed(self, data):
        self.append_to_history(f"[System] No save named '{data.get('slot')}' found.\n\n")

    def display_party_status(self, health_data, inventory_data):
        """!
        @brief Renders health and inventory for the party.
        @param health_data The health information of the party members.
        @param inventory_data The inventory contents of the party members.
        """
        self.event_bus.publish("log_info", "Displaying party status.")
        self.party_text.delete(1.0, tk.END)
        self.party_text.insert(tk.END, f"Health:\n{health_data}\n\nInventory:\n{inventory_data}")

    def display_notes(self, notes_content):
        """!
        @brief Shows notes to the user.
        @param notes_content The text or data of the notes.
        """
        self.event_bus.publish("log_info", "Displaying notes.")
        self.notes_text.delete(1.0, tk.END)
        self.notes_text.insert(tk.END, notes_content)

    def render_combat_field(self, map_data):
        """!
        @brief Draws a representation of the combat field or map.
        @param map_data The positional and environmental data of the combat field.
        """
        self.event_bus.publish("log_info", "Rendering combat field.")
        self.map_text.delete(1.0, tk.END)
        self.map_text.insert(tk.END, map_data)

    def display_debug_info(self, rules_data):
        self.event_bus.publish("log_info", "Displaying debug info.")
        self.debug_text.delete(1.0, tk.END)
        skills = rules_data.get("skills", {})
        entities = rules_data.get("entities", {})
        
        self.debug_text.insert(tk.END, "--- Skills ---\n")
        for data in skills.items():
            self.debug_text.insert(tk.END, f"{data}\n\n")
            
        self.debug_text.insert(tk.END, "--- Entities ---\n")
        for data in entities.items():
            self.debug_text.insert(tk.END, f"{data}\n\n")

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