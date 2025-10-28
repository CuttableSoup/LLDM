from __future__ import annotations
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass, field # <--- ADDED THIS IMPORT

try:
    import yaml
except ImportError:
    print("PyYAML not found. Please install: pip install PyYAML")
    yaml = None

# Import your data classes
try:
    from classes import Entity
except ImportError:
    print("Warning: 'classes.py' not found. Using placeholder Entity.")
    class Entity:
        name: str = ""
        quote: List[str] = []

@dataclass
class Intent:
    """Holds data for a single intent."""
    name: str
    description: str
    keywords: List[str]

@dataclass
class ProcessedInput:
    """A structured object for the output of the NLP pipeline."""
    raw_text: str
    intent: Intent
    targets: List[Entity] = field(default_factory=list)
    # e.g., for "give potion to Kael", targets would be [potion_entity, kael_entity]
    
class NLPProcessor:
    """
    Handles processing natural language input from the player
    and generating responses for NPCs.
    
    This is a placeholder implementation that uses simple keyword
    matching. This can be swapped out for real ML models.
    """
    
    def __init__(self, intents_filepath: Path):
        """
        Initializes the processor and loads the intent definitions.
        
        Args:
            intents_filepath: Path to the 'intents.yaml' file.
        """
        self.intents: Dict[str, Intent] = {}
        if not yaml:
            raise ImportError("PyYAML is required to load intents.")
        self.load_intents(intents_filepath)

    def load_intents(self, filepath: Path):
        """Loads intent definitions from the YAML file."""
        print(f"NLP: Loading intents from {filepath.name}...")
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            
            if not data or 'intents' not in data:
                print(f"Warning: 'intents:' key not found in {filepath.name}")
                return
                
            for intent_data in data['intents']:
                intent = Intent(
                    name=intent_data.get('name', 'UNKNOWN'),
                    description=intent_data.get('description', ''),
                    keywords=intent_data.get('keywords', [])
                )
                if intent.name != 'UNKNOWN':
                    self.intents[intent.name] = intent
            
            print(f"NLP: Loaded {len(self.intents)} intents.")

        except Exception as e:
            print(f"Error loading intents file {filepath}: {e}")

    def classify_intent(self, text_input: str) -> Intent:
        """
        (Placeholder) Simulates Intent Classification.
        
        Finds the best-matching intent based on keywords.
        A real implementation would use semantic similarity models.
        """
        text_lower = text_input.lower()
        
        # Iterate and find the first match
        for intent in self.intents.values():
            if intent.name == "OTHER": # Skip the fallback
                continue
            for keyword in intent.keywords:
                if keyword in text_lower:
                    return intent
                    
        # Fallback to OTHER
        return self.intents.get("OTHER", Intent(name="OTHER", description="", keywords=[]))

    def extract_entities(self, text_input: str, known_entities: Dict[str, Entity]) -> List[Entity]:
        """
        (Placeholder) Simulates Named Entity Recognition (NER).
        
        Finds entities whose names appear in the text.
        A real implementation would use a proper NER model to handle
        pronouns, context, and descriptors ("the orc", "it", "the chest").
        """
        text_lower = text_input.lower()
        found_entities = []
        
        for entity_name, entity_obj in known_entities.items():
            if entity_name.lower() in text_lower:
                found_entities.append(entity_obj)
                
        return found_entities

    def process_player_input(self, text_input: str, known_entities: Dict[str, Entity]) -> ProcessedInput:
        """
        Runs the full (simulated) NLP pipeline on player input.
        
        Args:
            text_input: The raw string from the player.
            known_entities: The dictionary of all entities in the
                           controller's current scope (room, game, etc.)
                           
        Returns:
            A ProcessedInput object with the intent and targets.
        """
        # 1. Classify Intent
        intent = self.classify_intent(text_input)
        
        # 2. Extract Entities
        targets = self.extract_entities(text_input, known_entities)
        
        return ProcessedInput(
            raw_text=text_input,
            intent=intent,
            targets=targets
        )

    def generate_npc_response(self, npc_entity: Entity, player_input: ProcessedInput, game_state: Dict[str, Any]) -> str:
        """
        (Placeholder) Simulates an LLM call to generate an NPC response.
        
        A real implementation would build a detailed prompt with the
        NPC's personality, memories, attitudes, game state, and the
        player's input, then call an LLM API.
        
        Args:
            npc_entity: The Entity object for the NPC who is speaking.
            player_input: The processed input from the player.
            game_state: A dictionary of context (e.g., other actors, history).
        
        Returns:
            A string response for the NPC to "say".
        """
        
        # Simple rule-based simulation
        
        # 1. Check for a direct "talk" intent
        if player_input.intent.name == "INTERACT" and npc_entity in player_input.targets:
            # If NPC has a quote, use it
            if npc_entity.quote:
                return f"{npc_entity.name} says: \"{npc_entity.quote[0]}\""
            else:
                return f"{npc_entity.name} looks at you expectantly."
        
        # 2. Check if NPC was attacked
        if player_input.intent.name == "ATTACK" and npc_entity in player_input.targets:
            return f"{npc_entity.name} shouts: \"Aargh! You'll pay for that!\""
            
        # 3. Fallback for other actions (e.g., player moves or attacks someone else)
        # In a real system, the LLM would decide if the NPC
        # should comment on the player's actions.
        
        # For this placeholder, we'll just return None to say nothing.
        return None