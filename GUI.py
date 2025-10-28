from __future__ import annotations
from typing import List, Dict, Any, Optional, Callable
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from pathlib import Path

try:
    from nlp_processor import NLPProcessor, ProcessedInput
except ImportError:
    print("Warning: 'nlp_processor.py' not found. Using placeholder classes.")
    class NLPProcessor:
        def __init__(self, *args): pass
        def process_player_input(self, *args): return None
    class ProcessedInput: pass

try:
    from classes import Entity, InventoryItem, Skill, RulesetLoader, Room
    from DebugWindow import DebugWindow
except ImportError:
    print("Warning: 'classes.py' not found. Using placeholder classes.")
    # Define placeholder for Room if classes.py is missing
    # RulesetLoader will also be missing, but we can't run without it.
    class Room: pass
    class Entity: pass
    class InventoryItem: pass
    class Skill: pass
    class RulesetLoader:
        def __init__(self, *args):
            print("FATAL: classes.py missing RulesetLoader")
        def load_all(self): pass
        def get_character(self, name): return None
        creatures = {}
        characters = {}
        scenario = None


class GameController:
    """
    Manages the game state, player input, and game loop logic.
    
    This class acts as the 'Controller' in an MVC pattern,
    connecting the data (Models from classes.py) to the GUI (Views).
    """

    def __init__(self, loader: RulesetLoader, ruleset_path: Path):
        """
        Initializes the game controller.
        
        Args:
            loader: A pre-initialized RulesetLoader instance.
        """
        self.loader = loader
        """The data loader with all ruleset data."""
        
        self.nlp_processor = NLPProcessor(ruleset_path / "intents.yaml")
        """The NLP system for processing commands."""
        
        self.player_entity: Optional[Entity] = None
        """The main player character entity."""
        
        self.game_entities: Dict[str, Entity] = {}
        """A dictionary of all entities in the scene, indexed by name."""
        
        # NEW: Store the current room
        self.current_room: Optional[Room] = None
        """The currently active room object."""
        
        # Load entities from the loader
        self.game_entities.update(self.loader.creatures)
        self.game_entities.update(self.loader.characters)

        # (Placeholder) List of all entities in the current encounter
        self.initiative_order: List[Entity] = []
        
        # (Placeholder) Game history for narrative summaries
        self.round_history: List[str] = []

        # --- GUI Callbacks ---
        self.update_narrative_callback: Callable[[str], None] = lambda text: None
        self.update_character_sheet_callback: Callable[[Entity], None] = lambda entity: None
        self.update_inventory_callback: Callable[[Entity], None] = lambda entity: None
        # NEW: Callback now passes the Room object
        self.update_map_callback: Callable[[Optional[Room]], None] = lambda room: None

    def start_game(self, player: Entity):
        """
        Initializes the game, loads the player, and starts the loop.
        
        Args:
            player: The pre-loaded player Entity object.
        """
        self.player_entity = player
        if player.name not in self.game_entities:
            self.game_entities[player.name] = player
            
        # NEW: Load the first room from the scenario
        if self.loader.scenario and self.loader.scenario.environment.rooms:
            self.current_room = self.loader.scenario.environment.rooms[0]
            print(f"Loaded initial room: {self.current_room.name}")
        else:
            print("Warning: No scenario or rooms found in loader.")
        
        # --- MODIFICATION START ---
        # Build the initiative order from the entities placed in the room
        self.initiative_order = []
        
        if self.current_room and self.current_room.map:
            # 1. Create a quick lookup map from char -> entity_name
            legend_lookup: Dict[str, str] = {}
            if self.current_room.legend:
                for item in self.current_room.legend:
                    legend_lookup[item.char] = item.entity

            # 2. Find all unique entity characters on the map
            placed_chars = set()
            for y, row in enumerate(self.current_room.map):
                for x, char_code in enumerate(row):
                    # 'x' is empty floor, 'G' is ground layer
                    if char_code != 'x' and char_code != 'G': 
                        placed_chars.add(char_code)
            
            # 3. Get the Entity object for each placed character
            print("--- Loading Entities for Initiative ---")
            for char_code in placed_chars:
                entity_name = legend_lookup.get(char_code)
                if not entity_name:
                    print(f"Warning: Character '{char_code}' on map but not in legend.")
                    continue
                
                # Check for creature/player
                entity_obj = self.game_entities.get(entity_name)
                
                if not entity_obj:
                    # Check for environment entity (dummy, chest, wall, etc.)
                    entity_obj = self.loader.environment_ents.get(entity_name)
                    if entity_obj and entity_name not in self.game_entities:
                        # Add to game_entities for tracking
                        self.game_entities[entity_name] = entity_obj
                
                if entity_obj:
                    if entity_obj not in self.initiative_order:
                        self.initiative_order.append(entity_obj)
                        print(f"Added '{entity_name}' (char: '{char_code}') to initiative.")
                else:
                    print(f"Warning: Entity '{entity_name}' (char: '{char_code}') not found in any loader.")

        else:
            # Fallback if no room is loaded
            print("Warning: No room loaded, adding only player to initiative.")
            if self.player_entity:
                self.initiative_order = [self.player_entity]

        # Ensure player is always in the list (if they weren't placed via 'P')
        if self.player_entity and self.player_entity not in self.initiative_order:
            print(f"Warning: Player '{self.player_entity.name}' not placed on map, adding to initiative.")
            self.initiative_order.append(self.player_entity)
            
        print(f"Starting game with {len(self.initiative_order)} entities in initiative.")
        # --- MODIFICATION END ---
        
        # Manually update GUI on start
        self.update_narrative_callback(f"The adventure begins for {player.name}...")
        self.update_character_sheet_callback(self.player_entity)
        self.update_inventory_callback(self.player_entity)
        # NEW: Pass the loaded room to the map callback
        self.update_map_callback(self.current_room)
        
        print("GameController started.")

    def process_player_input(self, player_input: str):
        """
        Receives raw text input from the InputBar and processes it
        using the hybrid parser model.
        """
        if not self.player_entity or not self.nlp_processor:
            return

        print(f"Processing input: {player_input}")
        
        # --- START MODIFICATION ---
        
        # 1. Run the NLP Pipeline
        # We pass all game_entities as the "known_entities" for the NER step
        processed_action = self.nlp_processor.process_player_input(
            player_input, 
            self.game_entities
        )

        if not processed_action:
            self.update_narrative_callback("Error: Could not process input.")
            return

        intent_name = processed_action.intent.name
        target_entities = processed_action.targets
        
        print(f"NLP Result: Intent={intent_name}, Targets={[e.name for e in target_entities]}")

        # 2. Triage & Process Action (Placeholder Logic)
        # This is where you would build your game logic for each intent
        
        action_taken = False
        narrative_msg = ""
        
        if intent_name == "ATTACK":
            if target_entities:
                target = target_entities[0] # Simple: just attack the first target
                narrative_msg = f"You attack {target.name}!"
                # (Placeholder) process_interaction(self.player_entity, "attack", target)
                self.round_history.append(f"{self.player_entity.name} attacks {target.name}.")
                action_taken = True
            else:
                narrative_msg = "You swing your weapon at the air."
                action_taken = True

        elif intent_name == "MOVE":
            if target_entities:
                target = target_entities[0]
                narrative_msg = f"You move towards {target.name}."
            else:
                narrative_msg = "You move to a new position."
            # (Placeholder) process_movement(...)
            action_taken = True

        elif intent_name == "INTERACT":
            if target_entities:
                target = target_entities[0]
                # Check if it's an "open" action or "talk" action
                if "open" in player_input.lower() and target.supertype == "object":
                    narrative_msg = f"You attempt to open {target.name}."
                else:
                    narrative_msg = f"You interact with {target.name}."
            else:
                narrative_msg = "You look around."
            action_taken = True

        # Fallback for OTHER or unhandled intents
        if not action_taken:
            narrative_msg = f"You say, \"{player_input}\""
            self.round_history.append(f"{self.player_entity.name} says: \"{player_input}\"")

        # 3. Update GUI
        self.update_narrative_callback(narrative_msg)
        if target_entities:
            # Update any targets that were affected
            for target in target_entities:
                self.update_character_sheet_callback(target)
        self.update_character_sheet_callback(self.player_entity)
        self.update_inventory_callback(self.player_entity)
        
        # 4. Trigger NPC turns
        # We pass the player's processed action to the NPCs
        # so they can react to it.
        self._run_npc_turns(processed_action)

    def _run_npc_turns(self, player_action: ProcessedInput): # <--- MODIFIED
        """
        Runs the 'else' block of the loop for all non-player characters.
        
        Args:
            player_action: The processed action the player just took.
        """
        print("Running NPC turns...")
        if not self.player_entity: return
        
        all_actions_taken = False
        
        # Get the current game state for the LLM
        # (This is simplified for the placeholder)
        game_state_context = self._get_current_game_state(self.player_entity)
        
        for npc in self.initiative_order:
            if npc == self.player_entity:
                continue 

            if not ("intelligent" in npc.status or "animalistic" in npc.status or "robotic" in npc.status):
                continue

            # --- START MODIFICATION ---
            
            # 1. (LLM) Generate NPC Response/Reaction to player's action
            reaction_narrative = self.nlp_processor.generate_npc_response(
                npc_entity=npc,
                player_input=player_action,
                game_state=game_state_context
            )
            
            if reaction_narrative:
                self.update_narrative_callback(reaction_narrative)
                self.round_history.append(reaction_narrative)
            
            # 2. (AI) Simulate NPC's own turn
            # (This is where you'd call the LLM for the NPC's *own* action)
            # For now, we'll keep the simple placeholder
            print(f"Simulating turn for {npc.name}")
            turn_narrative = f"{npc.name} takes its turn." 
            # (Placeholder: old 'I took my turn' was too chatty)
            
            # (Example: A real call might look like this)
            # npc_action = self.nlp_processor.generate_npc_action(npc, game_state_context)
            # self.process_npc_action(npc, npc_action)
            
            # --- END MODIFICATION ---
            
            self.update_narrative_callback(turn_narrative)
            self.round_history.append(turn_narrative)
            all_actions_taken = True

        # 5. (Placeholder) Process round updates (e.g., poison, regeneration)
        self._process_round_updates()
        
        # 6. (Placeholder) Generate narrative summary
        if all_actions_taken:
            # (Placeholder) summary = self.llm.get_narrative_summary("\n".join(self.round_history))
            summary = "The round ends." # Placeholder
            self.update_narrative_callback(f"\n--- Round Summary ---\n{summary}")
            self.round_history = [] # Clear history for next round

    def _get_current_game_state(self, actor: Entity) -> Dict[str, Any]:
        """(Helper) Gathers all context for an LLM prompt."""
        
        actors_in_room = [e.name for e in self.initiative_order if e.name != actor.name]
        
        # Format attitudes
        attitudes_str = "none"
        if actor.attitude:
            try:
                import json
                attitudes_str = json.dumps(actor.attitude) # Simple serialization
            except ImportError:
                pass
        
        # NEW: Add room objects to game state
        objects_in_room = []
        # (Placeholder) This needs to be populated from the room/legend
        # if self.current_room and self.current_room.objects:
        #     objects_in_room = [obj.name for obj in self.current_room.objects]

        return {
            "actors_present": ", ".join(actors_in_room) if actors_in_room else "none",
            "objects_present": ", ".join(objects_in_room) if objects_in_room else "none",
            "attitudes": attitudes_str,
            "game_history": "\n".join(self.round_history)
        }

    def _process_round_updates(self):
        """(Placeholder) Processes end-of-round effects like poison, regen, etc."""
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
            pady=5,
            font=("Arial", 10)
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
        
        # Placeholder text (will be cleared on update)
        self.map_canvas.create_text(
            150, 150, 
            text="Map Area", 
            font=("Arial", 20, "italic"), 
            fill="white"
        )
        print("MapPanel created.")
        
    def update_map(self, room: Optional[Room] = None, tokens: List[Entity] = []):
        """
        (MODIFIED) Redraws the map based on new data.
        
        Args:
            room: The Room object to display.
            tokens: A list of entities to draw on the map.
        """
        print("MAP: Refreshing map display.")
        self.map_canvas.delete("all") # Clear the canvas

        if not room:
            self.map_canvas.create_text(
                150, 150, 
                text="No Map Data Loaded", 
                font=("Arial", 20, "italic"), 
                fill="white"
            )
            return

        # --- Render Room Name and Description ---
        self.map_canvas.create_text(
            10, 10, 
            text=f"{room.name}: {room.description}", 
            font=("Arial", 14, "bold"), 
            fill="white",
            anchor="nw" # Anchor to top-left corner
        )

        # --- Render Map Grid ---
        TILE_SIZE = 25 # Size of each map tile in pixels
        MAP_OFFSET_Y = 40 # Offset to leave space for title
        
        # --- MODIFIED TILE_INFO ---
        # Define tile properties (color, text)
        TILE_INFO = {
            'W': ("#333", "Wall"),  # Dark grey
            'G': ("#888", "Ground"), # Lighter grey (from layer 0)
            'D': ("#8B4513", "Dummy"), # Brown
            'T': ("#DC143C", "Trap"),  # Crimson
            'C': ("#FFD700", "Chest"), # Gold
            'p': ("#708090", "Gate"),   # Slate grey (e.g. 'portcullis')

            # NEW: Add tokens from rooms.yaml legend
            'P': ("#007BFF", "Player"), # Blue
            'w': ("#A52A2A", "Wolf"),   # Brown (matches 'wolf')
            'g': ("#2F4F4F", "Giant"),  # Dark Slate Grey (matches 'giant')
            
            # 'x' is used as empty space in the rooms.yaml example
            'x': ("#505050", "Floor"),     # Mid-grey
        }
        DEFAULT_COLOR = "#FF00FF" # Magenta for unknown tiles

        map_grid = room.map
        if not map_grid:
            print("MAP: Room has no .map property to draw.")
            return
            
        # --- NEW: Render Ground Layer First (Optional, but looks better) ---
        if room.layers and room.layers[0]:
            for y, row in enumerate(room.layers[0]):
                for x, tile_char in enumerate(row):
                    color, text = TILE_INFO.get(tile_char, (DEFAULT_COLOR, "?"))
                    x0 = x * TILE_SIZE
                    y0 = (y * TILE_SIZE) + MAP_OFFSET_Y
                    x1 = x0 + TILE_SIZE
                    y1 = y0 + TILE_SIZE
                    self.map_canvas.create_rectangle(
                        x0, y0, x1, y1, 
                        fill=color, 
                        outline=color # No outline for ground
                    )

        # --- Render Object/Actor Layer (room.map) ---
        for y, row in enumerate(map_grid):
            for x, tile_char in enumerate(row):
                
                # Skip ground tiles in this layer to see ground layer underneath
                if tile_char == 'G':
                    continue
                
                color, text = TILE_INFO.get(tile_char, (DEFAULT_COLOR, "?"))
                
                # Calculate pixel coordinates
                x0 = x * TILE_SIZE
                y0 = (y * TILE_SIZE) + MAP_OFFSET_Y
                x1 = x0 + TILE_SIZE
                y1 = y0 + TILE_SIZE
                
                # Draw the tile rectangle
                self.map_canvas.create_rectangle(
                    x0, y0, x1, y1, 
                    fill=color, 
                    outline="#222" # Dark outline
                )
                
                # Draw the icon text (if not empty floor)
                if tile_char != 'x':
                    self.map_canvas.create_text(
                        x0 + (TILE_SIZE / 2),
                        y0 + (TILE_SIZE / 2),
                        text=tile_char,
                        font=("Arial", 12, "bold"),
                        fill="white"
                    )
        
        # (Placeholder for token rendering - currently handled by TILE_INFO)
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
            
        if not entity:
            return
            
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
        vitals_frame.pack(fill='x', expand=False, pady=5)
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
        self.attr_frame.pack(fill='x', expand=False, pady=5)
        
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
        if not entity:
            return
            
        print(f"CHAR SHEET: Refreshing for {entity.name}")
        
        # Update Vitals
        self.hp_bar['maximum'] = entity.max_hp if entity.max_hp > 0 else 1
        self.hp_bar['value'] = entity.cur_hp
        self.hp_label.config(text=f"{entity.cur_hp} / {entity.max_hp}")
        
        self.mp_bar['maximum'] = entity.max_mp if entity.max_mp > 0 else 1
        self.mp_bar['value'] = entity.cur_mp
        self.mp_label.config(text=f"{entity.cur_mp} / {entity.max_mp}")
        
        self.fp_bar['maximum'] = entity.max_fp if entity.max_fp > 0 else 1
        self.fp_bar['value'] = entity.cur_fp
        self.fp_label.config(text=f"{entity.cur_fp} / {entity.max_fp}")
        
        # Update Attributes
        for widget in self.attr_frame.winfo_children():
            widget.destroy() # Clear old labels
            
        row = 0
        for name, value in entity.attribute.items():
            ttk.Label(self.attr_frame, text=f"{name.capitalize()}:").grid(row=row, column=0, sticky='w')
            ttk.Label(self.attr_frame, text=str(value)).grid(row=row, column=1, sticky='e', padx=10)
            row += 1
            
        # Update Skills
        for widget in self.skills_frame.winfo_children():
            widget.destroy() # Clear old labels
            
        row = 0
        for name, skill_obj in entity.skill.items():
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
    
    def __init__(self, root_widget: tk.Tk, loader: RulesetLoader, ruleset_path: Path):
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
        self.controller = GameController(loader=loader, ruleset_path=ruleset_path)
        
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
        # (Requires DebugWindow class)
        # print("DebugWindow class not implemented.") # <-- REMOVE THIS LINE

        # --- ADD/UNCOMMENT THIS BLOCK ---
        if self.debug_window_instance and self.debug_window_instance.winfo_exists():
            # If window exists, bring to front
            self.debug_window_instance.lift()
            self.debug_window_instance.focus()
        else:
            # Otherwise, create a new one
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
        if not player:
            print("FATAL: No player entity provided to app.run()")
            self.narrative_panel.add_narrative_text("FATAL ERROR: No player entity could be loaded. See console for details.")
            return

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
    except Exception as e:
        print(f"An unexpected error occurred during loading: {e}")
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
    app = MainWindow(
        root_widget=root, 
        loader=loader, 
        ruleset_path=RULESET_PATH # Pass the path
    )
    
    # 6. Start the app (which calls root.mainloop())
    app.run(player=player_character)
