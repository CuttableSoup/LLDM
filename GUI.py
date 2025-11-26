"""
This module contains the main graphical user interface (GUI) for the LLDM application.

It uses Tkinter to create the main window and all the UI panels, including the narrative,
map, character sheet, and inventory. It also handles user input and communication with
the GameController.
"""
from __future__ import annotations
from typing import List, Any, Optional, Callable
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from tkinter.scrolledtext import ScrolledText
from pathlib import Path
import threading
import logging

logger = logging.getLogger("GUI")

# Attempt to import necessary modules, with placeholder classes for graceful failure.
try:
    from config_manager import ConfigManager
    from llm_manager import LLMManager, OLLAMA_MODELS
except ImportError:
    class ConfigManager: pass
    class LLMManager: pass
    OLLAMA_MODELS = {"Gemma 3 12B": "gemma3:12b"}

try:
    from classes import Entity, RulesetLoader, Room, GameController
    from DebugWindow import DebugWindow
except ImportError:
    logger.warning("Warning: 'classes.py' not found. Using placeholder classes.")
    class Room: pass
    class Entity: pass
    class InventoryItem: pass
    class Skill: pass
    class RulesetLoader:
        def __init__(self, *args):
            logger.critical("FATAL: classes.py missing RulesetLoader")
        def load_all(self): pass
        def get_character(self, name): return None
        creatures = {}
        characters = {}
        scenario = None
    class GameController:
        def __init__(self, *args): pass
        def process_player_input(self, *args): pass
        def start_game(self, *args): pass
        loader = None
        update_narrative_callback = lambda text: None
        update_character_sheet_callback = lambda entity: None
        update_inventory_callback = lambda entity: None
        update_map_callback = lambda room: None

class NarrativePanel(ttk.Frame):
    """A panel for displaying the game's narrative text."""
    def __init__(self, parent_widget: tk.Widget):
        """Initializes the NarrativePanel."""
        super().__init__(parent_widget)
        
        self.text_area = ScrolledText(
            self, 
            wrap=tk.WORD, 
            state='disabled', 
            padx=5, 
            pady=5,
            font=("Arial", 10)
        )
        self.text_area.pack(expand=True, fill='both')
        logger.info("NarrativePanel created.")
        
    def add_narrative_text(self, text: str):
        """Adds text to the narrative display."""
        self.text_area.config(state='normal')
        self.text_area.insert(tk.END, text + "\n\n")
        self.text_area.config(state='disabled')
        self.text_area.see(tk.END) # Auto-scroll to the end
        logger.debug(f"NARRATIVE: {text}")

class MapPanel(ttk.Frame):
    """A panel for displaying the game map."""
    def __init__(self, parent_widget: tk.Widget):
        """Initializes the MapPanel."""
        super().__init__(parent_widget, padding=10)
        
        self.map_canvas = tk.Canvas(self, bg='darkgrey', relief='sunken', borderwidth=2)
        self.map_canvas.pack(expand=True, fill='both')
        
        self.map_canvas.create_text(
            150, 150, 
            text="Map Area", 
            font=("Arial", 20, "italic"), 
            fill="white"
        )
        logger.info("MapPanel created.")
        
    def update_map(self, room: Optional[Room] = None, tokens: List[Entity] = []):
        """Updates the map display with the current room data."""
        logger.debug("MAP: Refreshing map display.")
        self.map_canvas.delete("all")

        if not room:
            self.map_canvas.create_text(
                150, 150, 
                text="No Map Data Loaded", 
                font=("Arial", 20, "italic"), 
                fill="white"
            )
            return

        self.map_canvas.create_text(
            10, 10, 
            text=f"{room.name}: {room.description}", 
            font=("Arial", 14, "bold"), 
            fill="white",
            anchor="nw"
        )

        TILE_INFO = {}
        DEFAULT_COLOR = "#FF00FF" # Bright pink for unknown tiles
        
        # Create a lookup table for tile information from the room's legend.
        if room.legend:
            for item in room.legend:
                if item.char and item.color:
                    map_name = item.map_name or item.entity or "?"
                    TILE_INFO[item.char] = (item.color, map_name)

        TILE_SIZE = 25
        MAP_OFFSET_Y = 40

        if not room.layers:
            logger.warning("MAP: Room has no .layers property to draw.")
            return
            
        # Draw each layer of the map.
        for layer_index, layer_grid in enumerate(room.layers):
            for y, row in enumerate(layer_grid):
                for x, tile_char in enumerate(row):
                    
                    if tile_char == 'x': # 'x' is considered empty space
                        continue
                    
                    color, text = TILE_INFO.get(tile_char, (DEFAULT_COLOR, "?"))
                    
                    x0 = x * TILE_SIZE
                    y0 = (y * TILE_SIZE) + MAP_OFFSET_Y
                    x1 = x0 + TILE_SIZE
                    y1 = y0 + TILE_SIZE
                    
                    outline_color = "#222"
                    if layer_index == 0:
                        outline_color = color
                    
                    self.map_canvas.create_rectangle(
                        x0, y0, x1, y1, 
                        fill=color, 
                        outline=outline_color
                    )
                    
                    # Don't draw text on the ground layer to avoid clutter.
                    if not (layer_index == 0 and tile_char == 'G'):
                        self.map_canvas.create_text(
                            x0 + (TILE_SIZE / 2),
                            y0 + (TILE_SIZE / 2),
                            text=tile_char,
                            font=("Arial", 12, "bold"),
                            fill="white"
                        )

class InventoryPanel(ttk.Frame):
    """A panel for displaying the player's inventory."""
    def __init__(self, parent_widget: tk.Widget):
        """Initializes the InventoryPanel."""
        super().__init__(parent_widget, padding=10)
        
        columns = ('item', 'qty', 'equipped')
        self.tree = ttk.Treeview(self, columns=columns, show='headings')
        
        self.tree.heading('item', text='Item')
        self.tree.heading('qty', text='Qty')
        self.tree.column('qty', width=40, anchor='center')
        self.tree.heading('equipped', text='Equipped')
        self.tree.column('equipped', width=70, anchor='center')
        
        self.tree.pack(expand=True, fill='both')
        logger.info("InventoryPanel created.")
        
    def update_inventory(self, entity: Entity):
        """Updates the inventory display for the given entity."""
        for i in self.tree.get_children():
            self.tree.delete(i)
            
        if not entity:
            return
            
        logger.debug(f"INVENTORY: Refreshing for {entity.name}")
        
        # Populate the treeview with inventory items.
        for item in entity.inventory:
            equipped_str = "Yes" if item.equipped else "No"
            parent_id = self.tree.insert(
                '', 
                tk.END, 
                values=(item.item, item.quantity, equipped_str)
            )
            
            # Display items within containers.
            if item.inventory:
                for sub_item in item.inventory:
                    self.tree.insert(
                        parent_id, 
                        tk.END, 
                        values=(f"  - {sub_item.item}", sub_item.quantity, "")
                    )

class CharacterPanel(ttk.Frame):
    """A panel for displaying the character sheet."""
    def __init__(self, parent_widget: tk.Widget):
        """Initializes the CharacterPanel."""
        super().__init__(parent_widget, padding=15)
        
        # Vitals section (HP, MP, FP)
        vitals_frame = ttk.LabelFrame(self, text="Vitals", padding=10)
        vitals_frame.pack(fill='x', expand=False, pady=5)
        vitals_frame.grid_columnconfigure(1, weight=1)

        ttk.Label(vitals_frame, text="HP:").grid(row=0, column=0, sticky='w')
        self.hp_bar = ttk.Progressbar(vitals_frame, orient='horizontal', mode='determinate')
        self.hp_bar.grid(row=0, column=1, sticky='we', padx=5)
        self.hp_label = ttk.Label(vitals_frame, text="0 / 0")
        self.hp_label.grid(row=0, column=2, sticky='e')
        
        ttk.Label(vitals_frame, text="MP:").grid(row=1, column=0, sticky='w')
        self.mp_bar = ttk.Progressbar(vitals_frame, orient='horizontal', mode='determinate')
        self.mp_bar.grid(row=1, column=1, sticky='we', padx=5)
        self.mp_label = ttk.Label(vitals_frame, text="0 / 0")
        self.mp_label.grid(row=1, column=2, sticky='e')
        
        ttk.Label(vitals_frame, text="FP:").grid(row=2, column=0, sticky='w')
        self.fp_bar = ttk.Progressbar(vitals_frame, orient='horizontal', mode='determinate')
        self.fp_bar.grid(row=2, column=1, sticky='we', padx=5)
        self.fp_label = ttk.Label(vitals_frame, text="0 / 0")
        self.fp_label.grid(row=2, column=2, sticky='e')

        # Attributes and Skills sections
        self.attr_frame = ttk.LabelFrame(self, text="Attributes", padding=10)
        self.attr_frame.pack(fill='x', expand=False, pady=5)
        
        self.skills_frame = ttk.LabelFrame(self, text="Skills", padding=10)
        self.skills_frame.pack(fill='both', expand=True, pady=5)
        
        logger.info("CharacterPanel created.")
        
    def update_character_sheet(self, entity: Entity):
        """Updates the character sheet with the entity's data."""
        if not entity:
            return
            
        logger.debug(f"CHAR SHEET: Refreshing for {entity.name}")
        
        # Update vitals bars and labels.
        self.hp_bar['maximum'] = entity.max_hp if entity.max_hp > 0 else 1
        self.hp_bar['value'] = entity.cur_hp
        self.hp_label.config(text=f"{entity.cur_hp} / {entity.max_hp}")
        
        self.mp_bar['maximum'] = entity.max_mp if entity.max_mp > 0 else 1
        self.mp_bar['value'] = entity.cur_mp
        self.mp_label.config(text=f"{entity.cur_mp} / {entity.max_mp}")
        
        self.fp_bar['maximum'] = entity.max_fp if entity.max_fp > 0 else 1
        self.fp_bar['value'] = entity.cur_fp
        self.fp_label.config(text=f"{entity.cur_fp} / {entity.max_fp}")
        
        # Clear and repopulate attributes.
        for widget in self.attr_frame.winfo_children():
            widget.destroy()
            
        row = 0
        for name, attr_obj in entity.attribute.items():
            ttk.Label(self.attr_frame, text=f"{name.capitalize()}:").grid(row=row, column=0, sticky='w')
            ttk.Label(self.attr_frame, text=str(attr_obj.base)).grid(row=row, column=1, sticky='e', padx=10)
            row += 1
            
        # Clear and repopulate skills.
        for widget in self.skills_frame.winfo_children():
            widget.destroy()
            
        row = 0
        for attr_name, attr_obj in entity.attribute.items():
            if not attr_obj.skill:
                continue
                
            ttk.Label(self.skills_frame, text=f"--- {attr_name.capitalize()} ---", font=("Arial", 9, "bold")).grid(row=row, column=0, columnspan=2, sticky='w', pady=(5,0))
            row += 1
            
            for skill_name, skill_obj in attr_obj.skill.items():
                ttk.Label(self.skills_frame, text=f"  {skill_name.capitalize()}:").grid(row=row, column=0, sticky='w')
                ttk.Label(self.skills_frame, text=str(skill_obj.base)).grid(row=row, column=1, sticky='e', padx=10)
                row += 1

class InfoMultipane(ttk.Notebook):
    """A notebook widget to hold the Character, Inventory, and Map panels."""
    def __init__(self, parent_widget: tk.Widget):
        """Initializes the InfoMultipane."""
        super().__init__(parent_widget)
        
        self.map_panel = MapPanel(parent_widget=self)
        self.inventory_panel = InventoryPanel(parent_widget=self)
        self.character_panel = CharacterPanel(parent_widget=self)
        
        self.add(self.character_panel, text='Character')
        self.add(self.inventory_panel, text='Inventory')
        self.add(self.map_panel, text='Map')
        
        logger.info("InfoMultipane (tab widget) created.")
        
    def get_character_panel(self) -> CharacterPanel:
        """Returns the CharacterPanel instance."""
        return self.character_panel
        
    def get_inventory_panel(self) -> InventoryPanel:
        """Returns the InventoryPanel instance."""
        return self.inventory_panel
        
    def get_map_panel(self) -> MapPanel:
        """Returns the MapPanel instance."""
        return self.map_panel


class InputBar(ttk.Frame):
    """A widget for user text input."""
    def __init__(self, parent_widget: tk.Widget,
                submit_callback: Callable[[str], None]):
        """Initializes the InputBar."""
        super().__init__(parent_widget, padding=5)
        self.submit_callback = submit_callback
        
        self.entry = ttk.Entry(self, font=("Arial", 11))
        self.entry.pack(side='left', fill='x', expand=True, padx=(0, 5))
        
        self.submit_button = ttk.Button(self, text="Send", command=self._on_user_submit)
        self.submit_button.pack(side='right')
        
        self.entry.bind("<Return>", self._on_user_submit)
        
        logger.info("InputBar created.")
        
    def _on_user_submit(self, event: Any = None):
        """Handles the submission of user input."""
        text = self.entry.get().strip()
        if text:
            self.submit_callback(text)
            self.entry.delete(0, tk.END)

class MainWindow:
    """The main window of the LLDM application."""
    def __init__(self, root_widget: tk.Tk, loader: RulesetLoader, ruleset_path: Path,
                 config_manager: ConfigManager, llm_manager: LLMManager):
        """Initializes the MainWindow."""
        self.root = root_widget
        self.root.title("LLDM - AI Dungeon Master")
        self.root.geometry("1200x800")
        
        self.debug_window_instance = None
        
        self.config_manager = config_manager
        self.llm_manager = llm_manager
        
        # Configure the grid layout
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=2)
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(1, weight=0)
        
        # Initialize the game controller
        self.controller = GameController(
            loader=loader, 
            ruleset_path=ruleset_path,
            llm_manager=self.llm_manager
        )
        
        # LLM configuration variables
        self.llm_mode_var = tk.StringVar(
            value=self.config_manager.get('mode', 'offline')
        )
        
        default_model = list(OLLAMA_MODELS.values())[0] if OLLAMA_MODELS else "gemma3:12b"
        self.ollama_model_var = tk.StringVar(
            value=self.config_manager.get('ollama_model', default_model)
        )
        
        self._create_menu()
        
        # Create and layout the main frames
        main_frame = ttk.Frame(self.root)
        main_frame.grid(row=0, column=0, columnspan=2, sticky='nsew', padx=10, pady=(10, 0))
        main_frame.grid_rowconfigure(0, weight=1)
        main_frame.grid_columnconfigure(0, weight=2)
        main_frame.grid_columnconfigure(1, weight=1)
        
        bottom_frame = ttk.Frame(self.root)
        bottom_frame.grid(row=1, column=0, columnspan=2, sticky='nsew', padx=10, pady=10)
        
        # Create the UI panels
        self.narrative_panel = NarrativePanel(parent_widget=main_frame)
        self.narrative_panel.grid(row=0, column=0, sticky='nsew', padx=(0, 5))
        
        self.info_multipane = InfoMultipane(parent_widget=main_frame)
        self.info_multipane.grid(row=0, column=1, sticky='nsew', padx=(5, 0))
        
        self.input_bar = InputBar(
            parent_widget=bottom_frame,
            submit_callback=self.controller.process_player_input
        )
        self.input_bar.pack(fill='x', expand=True)

        # Wire up the callbacks from the controller to the GUI panels
        self.controller.update_narrative_callback = self.narrative_panel.add_narrative_text
        self.controller.update_character_sheet_callback = self.info_multipane.get_character_panel().update_character_sheet
        self.controller.update_inventory_callback = self.info_multipane.get_inventory_panel().update_inventory
        self.controller.update_map_callback = self.info_multipane.get_map_panel().update_map
        
        logger.info("MainWindow created and all components wired up.")

    def _create_menu(self):
        """Creates the main menu bar."""
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        # File menu
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Exit", command=self.root.quit)

        # LLM menu
        llm_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="LLM", menu=llm_menu)
        
        mode_menu = tk.Menu(llm_menu, tearoff=0)
        llm_menu.add_cascade(label="Mode", menu=mode_menu)
        mode_menu.add_radiobutton(
            label="Offline (Ollama)", 
            variable=self.llm_mode_var, 
            value="offline",
            command=self._on_select_mode
        )
        mode_menu.add_radiobutton(
            label="Online (OpenRouter)", 
            variable=self.llm_mode_var, 
            value="online",
            command=self._on_select_mode
        )
        
        llm_menu.add_separator()

        model_menu = tk.Menu(llm_menu, tearoff=0)
        llm_menu.add_cascade(label="Ollama Model", menu=model_menu)
        
        for friendly_name, model_id in OLLAMA_MODELS.items():
            model_menu.add_radiobutton(
                label=friendly_name,
                variable=self.ollama_model_var,
                value=model_id,
                command=self._on_select_model
            )
        
        llm_menu.add_separator()
        
        llm_menu.add_command(
            label="Set OpenRouter API Key...",
            command=self._on_set_api_key
        )

        # Debug menu
        debug_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Debug", menu=debug_menu)
        if DebugWindow:
            debug_menu.add_command(label="View Loaded Ruleset", command=self._open_debug_window)
        else:
            debug_menu.add_command(label="View Loaded Ruleset", state="disabled")

    
    def _on_select_mode(self):
        """Handles the selection of the LLM mode (online/offline)."""
        mode = self.llm_mode_var.get()
        self.config_manager.set('mode', mode)
        self.narrative_panel.add_narrative_text(f"Switched to {mode} mode.")
        logger.info(f"Config: Set mode to {mode}")

    def _on_select_model(self):
        """Handles the selection of the Ollama model."""
        model_id = self.ollama_model_var.get()
        self.config_manager.set('ollama_model', model_id)
        self.narrative_panel.add_narrative_text(f"Set Ollama model to: {model_id}")
        logger.info(f"Config: Set ollama_model to {model_id}")
        
        # Check if the model needs to be downloaded.
        threading.Thread(
            target=self._check_and_pull_model, 
            args=(model_id,),
            daemon=True
        ).start()

    def _check_and_pull_model(self, model_id: str):
        """Checks if the selected Ollama model is available locally."""
        if not self.llm_manager.check_ollama_model(model_id):
            logger.info(f"Model {model_id} not found locally.")
            self.root.after(0, self._ask_to_pull_model, model_id)

    def _ask_to_pull_model(self, model_id: str):
        """Asks the user if they want to download the model."""
        if messagebox.askyesno(
            "Download Model?",
            f"The model '{model_id}' was not found on your system.\n\n" 
            "Would you like to download it now? This may take several minutes."
        ):
            self.narrative_panel.add_narrative_text(f"Starting download for {model_id}...")
            threading.Thread(
                target=self.llm_manager.pull_ollama_model,
                args=(model_id, self._model_pull_callback),
                daemon=True
            ).start()

    def _model_pull_callback(self, status_message: str):
        """Callback function to display the model download status."""
        self.root.after(0, self.narrative_panel.add_narrative_text, status_message)

    def _on_set_api_key(self):
        """Opens a dialog to set the OpenRouter API key."""
        current_key = self.config_manager.get('openrouter_key', '')
        
        new_key = simpledialog.askstring(
            "OpenRouter API Key",
            "Please enter your OpenRouter API key:",
            initialvalue=current_key
        )
        
        if new_key is not None:
            self.config_manager.set('openrouter_key', new_key)
            self.narrative_panel.add_narrative_text("OpenRouter API Key saved.")
            logger.info("Config: OpenRouter key updated.")
            

    def _open_debug_window(self):
        """Opens the debug window."""
        if self.debug_window_instance and self.debug_window_instance.winfo_exists():
            self.debug_window_instance.lift()
            self.debug_window_instance.focus()
        else:
            self.debug_window_instance = DebugWindow(
                parent=self.root, 
                loader=self.controller.loader
            )

    def run(self, player: Entity):
        """Starts the main game loop."""
        if not player:
            logger.critical("FATAL: No player entity provided to app.run()")
            self.narrative_panel.add_narrative_text("FATAL ERROR: No player entity could be loaded. See console for details.")
            return

        self.controller.start_game(player)
        
        logger.info("GUI Main Loop is running...")
        self.root.mainloop()