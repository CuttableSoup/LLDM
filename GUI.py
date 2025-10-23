from __future__ import annotations
from typing import List, Dict, Any, Optional, Callable
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from pathlib import Path

try:
    from debug_gui import DebugWindow
except ImportError:
    print("Warning: debug_gui.py not found. Debug window will not be available.")
    DebugWindow = None

try:
    from classes import Entity, InventoryItem, Skill, RulesetLoader
except ImportError:
    print("Warning: 'classes.py' not found. Using placeholder classes.")

try:
    from dungeonmaster import IntentParser, LLMInterface, process_interaction, process_attitudes
except ImportError:
    print("FATAL: dungeonmaster.py not found. Game logic will not work.")
    # You might want to exit or define placeholder functions
    exit(1)

class GameController:
    """
    Manages the game state, player input, and game loop logic.
    
    This class acts as the 'Controller' in an MVC pattern,
    connecting the data (Models from classes.py) to the GUI (Views).
    """

    def __init__(self, loader: RulesetLoader):
        """
        Initializes the game controller.
        
        Args:
            loader: A pre-initialized RulesetLoader instance.
        """
        self.loader = loader
        """The data loader with all ruleset data."""
        
        self.player_entity: Optional[Entity] = None
        """The main player character entity."""
        
        self.game_entities: Dict[str, Entity] = {}
        """A dictionary of all entities in the scene, indexed by name."""
        
        # Load entities from the loader
        self.game_entities.update(self.loader.creatures)
        self.game_entities.update(self.loader.characters)
        
        # --- NEW: Initialize logic modules ---
        self.parser = IntentParser()
        self.llm = LLMInterface()

        # (Placeholder) List of all entities in the current encounter
        self.initiative_order: List[Entity] = []
        
        # (Placeholder) Game history for narrative summaries
        self.round_history: List[str] = []

        # --- GUI Callbacks ---
        self.update_narrative_callback: Callable[[str], None] = lambda text: None
        self.update_character_sheet_callback: Callable[[Entity], None] = lambda entity: None
        self.update_inventory_callback: Callable[[Entity], None] = lambda entity: None
        self.update_map_callback: Callable[[], None] = lambda: None

    def start_game(self, player: Entity):
        """
        Initializes the game, loads the player, and starts the loop.
        
        Args:
            player: The pre-loaded player Entity object.
        """
        self.player_entity = player
        if player.name not in self.game_entities:
            self.game_entities[player.name] = player
        
        # (Placeholder) Set up the initial scene
        # This is where you'd add NPCs to the encounter
        # For testing, we'll add the first creature from the loader
        if self.loader.creatures:
            first_creature_name = list(self.loader.creatures.keys())[0]
            first_creature = self.loader.creatures[first_creature_name]
            if first_creature.name not in self.game_entities:
                 self.game_entities[first_creature.name] = first_creature
            
            # (Placeholder) Add entities to the combat order
            self.initiative_order = [self.player_entity, first_creature]
            
            print(f"Starting game with {player.name} and {first_creature.name}.")
        else:
            self.initiative_order = [self.player_entity]
            print(f"Starting game with only {player.name}.")
        
        # Manually update GUI on start
        self.update_narrative_callback(f"The adventure begins for {player.name}...")
        self.update_character_sheet_callback(self.player_entity)
        self.update_inventory_callback(self.player_entity)
        self.update_map_callback()
        
        print("GameController started.")

    def process_player_input(self, player_input: str):
        """
        Receives raw text input from the InputBar and processes it
        using the hybrid parser model.
        """
        if not self.player_entity:
            return

        print(f"Processing input: {player_input}")
        
        # 1. Fast Intent + Entity Pipeline
        pipeline_result = self.parser.run_fast_pipeline(player_input)
        
        confidence = pipeline_result['confidence']
        action = pipeline_result['intent']
        target_name = pipeline_result['target']
        language = pipeline_result['language']
        
        # 2. Triage: Fallback to Pattern-Constrained LLM if needed
        # (Confidence < 0.7 is an example threshold)
        if confidence < 0.7:
            print(f"Low confidence ({confidence:.2f}). Falling back to LLM parser...")
            # Get the list of all available skills for the player
            player_skills = list(self.player_entity.skills.keys())
            
            llm_result = self.llm.run_llm_parser(player_input, player_skills)
            action = llm_result['intent']
            target_name = llm_result['target']
            language = llm_result['language']
        
        # 3. Process the results
        target_entity = self.game_entities.get(target_name)
        
        if action:
            self.update_narrative_callback(f"You: \"{player_input}\"")
            process_interaction(self.player_entity, action, target_entity)
            process_attitudes(self.player_entity, target_entity, action, language)
            
            # (Placeholder) Log action for summary
            self.round_history.append(f"{self.player_entity.name} {action}s {target_name}.")
        else:
            self.update_narrative_callback(f"You say, \"{player_input}\"")
            self.round_history.append(f"{self.player_entity.name} says: \"{player_input}\"")
        
        # 4. Update GUI with any state changes
        # (e.g., if HP changed or an item was used)
        if target_entity:
            self.update_character_sheet_callback(target_entity) # Update target's sheet if visible
        self.update_character_sheet_callback(self.player_entity)
        self.update_inventory_callback(self.player_entity)
        
        # 5. Trigger NPC turns
        self._run_npc_turns()

    def _run_npc_turns(self):
        """
        Runs the 'else' block of the loop for all non-player characters.
        """
        print("Running NPC turns...")
        if not self.player_entity: return
        
        # This defines the "tools" NPCs can use.
        # This list comes from your llm_calls.py pseudocode.
        # It's a placeholder; a real one would be loaded from a file.
        npc_tools = [
            {
                "type": "function", "function": {
                    "name": "execute_skill_check",
                    "description": "Use a non-magical skill on an object or another character.",
                    "parameters": {"type": "object", "properties": {
                        "skill": {"type": "string", "description": "The name of the skill being used."},
                        "target": {"type": "string", "description": "The target of the skill (an object or character name)."}
                    }, "required": ["skill", "target"]}
                }
            },
            {
                "type": "function", "function": {
                    "name": "manage_item",
                    "description": "Manage an item: equip, unequip, or use.",
                    "parameters": {"type": "object", "properties": {
                        "action": {"type": "string", "enum": ["equip", "unequip", "use"]},
                        "item_name": {"type": "string"}
                    }, "required": ["action", "item_name"]}
                }
            }
        ]
        
        all_actions_taken = False
        
        for npc in self.initiative_order:
            if npc == self.player_entity:
                continue # Skip the player

            # 1. Get Game State for the NPC
            game_state = self._get_current_game_state(npc)
            
            # 2. Get NPC action from LLM
            llm_result = self.llm.get_npc_action(npc, game_state, npc_tools)
            
            narrative = llm_result['narrative']
            mechanical_data = llm_result['mechanical_data']
            
            # 3. Output narrative
            if narrative:
                self.update_narrative_callback(narrative)
                self.round_history.append(narrative)
            
            # 4. Execute mechanical action
            if mechanical_data:
                action_name = mechanical_data['name']
                arguments = mechanical_data['arguments']
                
                # (Placeholder) This is where you would call an ActionHandler
                # For now, we'll just print it.
                print(f"MECHANICS: {npc.name} calls tool '{action_name}' with args: {arguments}")
                
                # (Placeholder) Simulate a skill check
                if action_name == 'execute_skill_check':
                    target_entity = self.game_entities.get(arguments.get('target'))
                    if target_entity:
                        process_interaction(npc, arguments.get('skill', 'attack'), target_entity)
                
                all_actions_taken = True

        # 5. (Placeholder) Process round updates (e.g., poison, regeneration)
        self._process_round_updates()
        
        # 6. (Placeholder) Generate narrative summary
        if all_actions_taken:
            summary = self.llm.get_narrative_summary("\n".join(self.round_history))
            self.update_narrative_callback(f"\n--- Round Summary ---\n{summary}")
            self.round_history = [] # Clear history for next round

    def _get_current_game_state(self, actor: Entity) -> Dict[str, Any]:
        """(Helper) Gathers all context for an LLM prompt."""
        
        # This is a stub, but it's what your `npc_action` in GUI.py was doing
        actors_in_room = [e.name for e in self.initiative_order if e.name != actor.name]
        
        # Format attitudes
        attitudes_str = "none"
        if actor.attitudes:
            attitudes_str = json.dumps(actor.attitudes) # Simple serialization
        
        return {
            "actors_present": ", ".join(actors_in_room) if actors_in_room else "none",
            "objects_present": "none", # (Placeholder)
            "attitudes": attitudes_str,
            "game_history": "\n".join(self.round_history)
        }

    def _process_round_updates(self):
        """(Placeholder) Processes end-of-round effects like poison, regen, etc."""
        # for entity in self.initiative_order:
        #   if 'poisoned' in entity.tags:
        #       entity.cur_hp -= 1
        #       self.update_narrative_callback(f"{entity.name} takes 1 poison damage.")
        #   self.update_character_sheet_callback(entity)
        pass

class NarrativePanel(ttk.Frame):
    """
    The GUI component (left pane) that displays the
    game's narrative, dialogue, and combat logs.
    """
    
    def __init__(self, parent_widget: tk.Widget):
        """
        Initializes the text box panel.
        
        Args:
            parent_widget: The parent tkinter widget.
        """
        super().__init__(parent_widget)
        
        self.text_area = ScrolledText(
            self, 
            wrap=tk.WORD, 
            state='disabled', 
            padx=5, 
            pady=5
        )
        self.text_area.pack(expand=True, fill='both')
        print("NarrativePanel created.")
        
    def add_narrative_text(self, text: str):
        """
        Appends a new line of text to the narrative display.
        
        Args:
            text: The string to add (e.g., "You open the door.")
        """
        self.text_area.config(state='normal')
        self.text_area.insert(tk.END, text + "\n\n")
        self.text_area.config(state='disabled')
        self.text_area.see(tk.END) # Auto-scroll to the bottom
        print(f"NARRATIVE: {text}")
        pass

class MapPanel(ttk.Frame):
    """
    The GUI panel within the InfoMultipane that displays
    the game map, tokens, and location data.
    """
    
    def __init__(self, parent_widget: tk.Widget):
        """
        Initializes the map view.
        
        Args:
            parent_widget: The parent multipane/tab widget.
        """
        super().__init__(parent_widget, padding=10)
        
        # Placeholder for a map. A Canvas is ideal for drawing.
        self.map_canvas = tk.Canvas(self, bg='darkgrey', relief='sunken', borderwidth=2)
        self.map_canvas.pack(expand=True, fill='both')
        
        # Placeholder text
        self.map_canvas.create_text(
            150, 150, 
            text="Map Area", 
            font=("Arial", 20, "italic"), 
            fill="white"
        )
        print("MapPanel created.")
        
    def update_map(self, map_data: Any = None, tokens: List[Entity] = []):
        """
        Redraws the map based on new data.
        
        Args:
            map_data: The map grid/image (placeholder).
            tokens: A list of entities to draw on the map.
        """
        # (GUI logic to clear canvas and draw/redraw map/tokens)
        print("MAP: Refreshing map display.")
        pass


class InventoryPanel(ttk.Frame):
    """
    The GUI panel within the InfoMultipane that displays
    the player's inventory using a Treeview.
    """
    
    def __init__(self, parent_widget: tk.Widget):
        """
        Initializes the inventory view.
        
        Args:
            parent_widget: The parent multipane/tab widget.
        """
        super().__init__(parent_widget, padding=10)
        
        columns = ('item', 'qty', 'equipped')
        self.tree = ttk.Treeview(self, columns=columns, show='headings')
        
        self.tree.heading('item', text='Item')
        self.tree.heading('qty', text='Qty')
        self.tree.column('qty', width=40, anchor='center')
        self.tree.heading('equipped', text='Equipped')
        self.tree.column('equipped', width=70, anchor='center')
        
        self.tree.pack(expand=True, fill='both')
        print("InventoryPanel created.")
        
    def update_inventory(self, entity: Entity):
        """
        Refreshes the inventory display with the entity's items.
        
        Args:
            entity: The entity whose inventory should be displayed.
        """
        # Clear existing items
        for i in self.tree.get_children():
            self.tree.delete(i)
            
        print(f"INVENTORY: Refreshing for {entity.name}")
        
        for item in entity.inventory:
            equipped_str = "Yes" if item.equipped else "No"
            # Insert top-level item
            parent_id = self.tree.insert(
                '', 
                tk.END, 
                values=(item.item, item.quantity, equipped_str)
            )
            
            # Insert nested items
            if item.inventory:
                for sub_item in item.inventory:
                    self.tree.insert(
                        parent_id, 
                        tk.END, 
                        values=(f"  - {sub_item.item}", sub_item.quantity, "")
                    )
        pass


class CharacterPanel(ttk.Frame):
    """
    The GUI panel within the InfoMultipane that displays
    the player's character sheet (stats, skills, vitals).
    """
    
    def __init__(self, parent_widget: tk.Widget):
        """
        Initializes the character sheet view.
        
        Args:
            parent_widget: The parent multipane/tab widget.
        """
        super().__init__(parent_widget, padding=15)
        
        # --- Vitals Frame ---
        vitals_frame = ttk.LabelFrame(self, text="Vitals", padding=10)
        vitals_frame.pack(fill='x', expand=True, pady=5)
        vitals_frame.grid_columnconfigure(1, weight=1)

        # HP
        ttk.Label(vitals_frame, text="HP:").grid(row=0, column=0, sticky='w')
        self.hp_bar = ttk.Progressbar(vitals_frame, orient='horizontal', mode='determinate')
        self.hp_bar.grid(row=0, column=1, sticky='we', padx=5)
        self.hp_label = ttk.Label(vitals_frame, text="0 / 0")
        self.hp_label.grid(row=0, column=2, sticky='e')
        
        # MP
        ttk.Label(vitals_frame, text="MP:").grid(row=1, column=0, sticky='w')
        self.mp_bar = ttk.Progressbar(vitals_frame, orient='horizontal', mode='determinate')
        self.mp_bar.grid(row=1, column=1, sticky='we', padx=5)
        self.mp_label = ttk.Label(vitals_frame, text="0 / 0")
        self.mp_label.grid(row=1, column=2, sticky='e')
        
        # FP
        ttk.Label(vitals_frame, text="FP:").grid(row=2, column=0, sticky='w')
        self.fp_bar = ttk.Progressbar(vitals_frame, orient='horizontal', mode='determinate')
        self.fp_bar.grid(row=2, column=1, sticky='we', padx=5)
        self.fp_label = ttk.Label(vitals_frame, text="0 / 0")
        self.fp_label.grid(row=2, column=2, sticky='e')

        # --- Attributes Frame ---
        self.attr_frame = ttk.LabelFrame(self, text="Attributes", padding=10)
        self.attr_frame.pack(fill='x', expand=True, pady=5)
        
        # --- Skills Frame ---
        self.skills_frame = ttk.LabelFrame(self, text="Skills", padding=10)
        self.skills_frame.pack(fill='both', expand=True, pady=5)
        
        print("CharacterPanel created.")
        
    def update_character_sheet(self, entity: Entity):
        """
        Refreshes the character sheet with the entity's data.
        
        Args:
            entity: The entity whose stats should be displayed.
        """
        print(f"CHAR SHEET: Refreshing for {entity.name}")
        
        # Update Vitals
        self.hp_bar['maximum'] = entity.max_hp
        self.hp_bar['value'] = entity.cur_hp
        self.hp_label.config(text=f"{entity.cur_hp} / {entity.max_hp}")
        
        self.mp_bar['maximum'] = entity.max_mp
        self.mp_bar['value'] = entity.cur_mp
        self.mp_label.config(text=f"{entity.cur_mp} / {entity.max_mp}")
        
        self.fp_bar['maximum'] = entity.max_fp
        self.fp_bar['value'] = entity.cur_fp
        self.fp_label.config(text=f"{entity.cur_fp} / {entity.max_fp}")
        
        # Update Attributes
        for widget in self.attr_frame.winfo_children():
            widget.destroy() # Clear old labels
            
        row = 0
        for name, value in entity.attributes.items():
            ttk.Label(self.attr_frame, text=f"{name.capitalize()}:").grid(row=row, column=0, sticky='w')
            ttk.Label(self.attr_frame, text=str(value)).grid(row=row, column=1, sticky='e', padx=10)
            row += 1
            
        # Update Skills
        for widget in self.skills_frame.winfo_children():
            widget.destroy() # Clear old labels
            
        row = 0
        for name, skill_obj in entity.skills.items():
            ttk.Label(self.skills_frame, text=f"{name.capitalize()}:").grid(row=row, column=0, sticky='w')
            ttk.Label(self.skills_frame, text=str(skill_obj.base)).grid(row=row, column=1, sticky='e', padx=10)
            row += 1
        pass

class InfoMultipane(ttk.Notebook):
    """
    The GUI component (right pane) that holds the tabs for
    Map, Inventory, and Character Sheet.
    """
    
    def __init__(self, parent_widget: tk.Widget):
        """
        Initializes the multipane (tabbed widget).
        
        Args:
            parent_widget: The main window's frame.
        """
        super().__init__(parent_widget)
        
        # Create the child panels
        self.map_panel = MapPanel(parent_widget=self)
        self.inventory_panel = InventoryPanel(parent_widget=self)
        self.character_panel = CharacterPanel(parent_widget=self)
        
        # Add panels as tabs
        self.add(self.character_panel, text='Character')
        self.add(self.inventory_panel, text='Inventory')
        self.add(self.map_panel, text='Map')
        
        print("InfoMultipane (tab widget) created.")
        
    def get_character_panel(self) -> CharacterPanel:
        """Returns the instance of the character panel."""
        return self.character_panel
        
    def get_inventory_panel(self) -> InventoryPanel:
        """Returns the instance of the inventory panel."""
        return self.inventory_panel
        
    def get_map_panel(self) -> MapPanel:
        """Returns the instance of the map panel."""
        return self.map_panel


class InputBar(ttk.Frame):
    """
    The GUI component (bottom) for player text input.
    It captures the text and sends it to the GameController.
    """
    
    def __init__(self, parent_widget: tk.Widget,
                submit_callback: Callable[[str], None]):
        """
        Initializes the input bar.
        
        Args:
            parent_widget: The parent GUI object.
            submit_callback: The function to call
            when the user presses Enter.
        """
        super().__init__(parent_widget, padding=5)
        self.submit_callback = submit_callback
        
        self.entry = ttk.Entry(self, font=("Arial", 11))
        self.entry.pack(side='left', fill='x', expand=True, padx=(0, 5))
        
        self.submit_button = ttk.Button(self, text="Send", command=self._on_user_submit)
        self.submit_button.pack(side='right')
        
        # Bind the <Return> key to the submit function
        self.entry.bind("<Return>", self._on_user_submit)
        
        print("InputBar created.")
        
    def _on_user_submit(self, event: Any = None):
        """
        (Internal) Called when the user presses Enter or clicks Submit.
        
        Args:
            event: The (optional) event object from tkinter binding.
        """
        text = self.entry.get().strip()
        if text:
            # Send the text to the controller
            self.submit_callback(text)
            
            # Clear the text box
            self.entry.delete(0, tk.END)
        pass

class MainWindow:
    """
    The main application window that contains all other
    GUI components (NarrativePanel, InfoMultipane, InputBar).
    """
    
    def __init__(self, root_widget: tk.Tk, loader: RulesetLoader):
        """
        Initializes the main window and creates all child widgets.
        
        Args:
            root_widget: The root object of the GUI framework (tk.Tk()).
            loader: A RulesetLoader that has already run load_all().
        """
        self.root = root_widget
        self.root.title("AI Dungeon Master")
        self.root.geometry("1200x800")
        
        self.debug_window_instance = None # To hold a reference to the debug window
        
        # Configure root grid
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=2) # Left panel (narrative)
        self.root.grid_columnconfigure(1, weight=1) # Right panel (multipane)
        self.root.grid_rowconfigure(1, weight=0) # Bottom input bar
        
        # 1. Initialize the Game Controller
        self.controller = GameController(loader=loader)
        
        # --- NEW: Create Menu Bar ---
        self._create_menu()
        
        # 2. Create main layout frames
        main_frame = ttk.Frame(self.root)
        main_frame.grid(row=0, column=0, columnspan=2, sticky='nsew', padx=10, pady=(10, 0))
        main_frame.grid_rowconfigure(0, weight=1)
        main_frame.grid_columnconfigure(0, weight=2)
        main_frame.grid_columnconfigure(1, weight=1)
        
        bottom_frame = ttk.Frame(self.root)
        bottom_frame.grid(row=1, column=0, columnspan=2, sticky='nsew', padx=10, pady=10)
        
        # 3. Initialize the GUI components
        
        # Left Panel
        self.narrative_panel = NarrativePanel(parent_widget=main_frame)
        self.narrative_panel.grid(row=0, column=0, sticky='nsew', padx=(0, 5))
        
        # Right Multipane
        self.info_multipane = InfoMultipane(parent_widget=main_frame)
        self.info_multipane.grid(row=0, column=1, sticky='nsew', padx=(5, 0))
        
        # Bottom Input Bar
        self.input_bar = InputBar(
            parent_widget=bottom_frame,
            submit_callback=self.controller.process_player_input
        )
        self.input_bar.pack(fill='x', expand=True)

        # 4. Connect Controller callbacks to GUI update methods
        self.controller.update_narrative_callback = self.narrative_panel.add_narrative_text
        self.controller.update_character_sheet_callback = self.info_multipane.get_character_panel().update_character_sheet
        self.controller.update_inventory_callback = self.info_multipane.get_inventory_panel().update_inventory
        self.controller.update_map_callback = self.info_multipane.get_map_panel().update_map
        
        print("MainWindow created and all components wired up.")

    def _create_menu(self):
        """Creates the main application menu bar."""
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        # File Menu
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Exit", command=self.root.quit)

        # Debug Menu
        debug_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Debug", menu=debug_menu)
        
        if DebugWindow:
            debug_menu.add_command(label="View Loaded Ruleset", command=self._open_debug_window)
        else:
            debug_menu.add_command(label="View Loaded Ruleset", state="disabled")

    def _open_debug_window(self):
        """
        Opens the debug inspector window. If one is already open,
        it brings it to the front.
        """
        if self.debug_window_instance and self.debug_window_instance.winfo_exists():
            self.debug_window_instance.lift()
            self.debug_window_instance.focus()
        else:
            self.debug_window_instance = DebugWindow(
                parent=self.root, 
                loader=self.controller.loader
            )

    def run(self, player: Entity):
        """
        Starts the game logic and the GUI main loop.
        
        Args:
            player: The player character to start the game with.
        """
        # Start the game logic (which will trigger initial GUI updates)
        self.controller.start_game(player)
        
        # Start the GUI main loop
        print("GUI Main Loop is running...")
        self.root.mainloop()
        pass

if __name__ == "__main__":
    """
    Example of how to run the application.
    """
    
    print("--- Initializing Application ---")
    
    # 1. Set the ruleset path
    # Assumes 'rulesets' directory is in the same folder as GUI.py
    RULESET_PATH = Path(__file__).parent / "rulesets" / "medievalfantasy"
    
    # 2. Initialize and run the loader
    try:
        loader = RulesetLoader(RULESET_PATH)
        loader.load_all()
    except FileNotFoundError as e:
        print(f"Fatal Error: {e}")
        print("Please ensure the 'rulesets/medievalfantasy' directory exists.")
        exit(1)
    except ImportError:
        print("Fatal Error: 'PyYAML' library not found.")
        print("Please install it using: pip install PyYAML")
        exit(1)

    # 3. Get the player character from the loader
    # We assume a 'Valerius.yaml' file exists in 'characters/'
    player_character = loader.get_character("Valerius")
    
    if not player_character:
        print("Error: Default player 'Valerius' not found in ruleset.")
        # As a fallback, create a minimal entity to avoid crashing
        player_character = Entity(
            name="Valerius (Fallback)",
            cur_hp=1, max_hp=1, cur_mp=1, max_mp=1, cur_fp=1, max_fp=1
        )
    
    # 4. Create the root tkinter window
    root = tk.Tk()
    
    # Use a modern theme
    style = ttk.Style(root)
    try:
        # 'clam' is a good, simple, cross-platform theme
        style.theme_use('clam') 
    except tk.TclError:
        print("Ttk 'clam' theme not available, using default.")
    
    # 5. Create the main window, passing in the loaded data
    app = MainWindow(root_widget=root, loader=loader)
    
    # 6. Start the app (which calls root.mainloop())
    app.run(player=player_character)