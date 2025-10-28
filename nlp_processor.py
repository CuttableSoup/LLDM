from __future__ import annotations
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass, field

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
    skill_name: Optional[str] = None
    
class NLPProcessor:
    """
    Handles processing natural language input from the player
    and generating responses for NPCs.
    """
    
    def __init__(self, ruleset_path: Path):
        """
        Initializes the processor and loads the intent definitions
        from both the root directory and the ruleset directory.
        
        Args:
            ruleset_path: Path to the active ruleset
                          (e.g., '.../rulesets/medievalfantasy')
        """
        self.intents: Dict[str, Intent] = {}
        if not yaml:
            raise ImportError("PyYAML is required to load intents.")
        
        # This will hold the mappings from skill_map.yaml
        self.skill_keyword_map: Dict[str, str] = {}
        
        # 1. Define paths
        root_path = ruleset_path.parent.parent
        core_intents_path = root_path / "intents.yaml"
        ruleset_intents_path = ruleset_path / "intents.yaml"
        skill_map_path = ruleset_path / "skill_map.yaml" # <-- New path

        # 2. Load core intents
        print(f"NLP: Loading core intents from {core_intents_path.name}...")
        core_intents = self.load_intents_from_file(core_intents_path)
        self.intents.update(core_intents)
        
        # 3. Load ruleset-specific intents (if file exists)
        if ruleset_intents_path.exists():
            print(f"NLP: Loading ruleset intents from {ruleset_intents_path.name}...")
            ruleset_intents = self.load_intents_from_file(ruleset_intents_path)
            self.intents.update(ruleset_intents)
        else:
            print(f"NLP: No ruleset intents file found at {ruleset_intents_path.name}.")
            
        print(f"NLP: Loaded a total of {len(self.intents)} intents.")

        # 4. Load the skill map
        self.load_skill_map(skill_map_path)

    def load_skill_map(self, filepath: Path):
        """Loads the keyword-to-skill mapping from skill_map.yaml."""
        if not filepath.exists():
            print(f"NLP: No skill map file found at {filepath.name}. Using keywords as-is.")
            return
            
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            
            if data and 'skill_map' in data and isinstance(data['skill_map'], dict):
                self.skill_keyword_map = data['skill_map']
                print(f"NLP: Loaded {len(self.skill_keyword_map)} skill keyword mappings.")
            else:
                print(f"Warning: '{filepath.name}' is invalid or empty.")
        except Exception as e:
            print(f"Error loading skill map file {filepath}: {e}")

    def load_intents_from_file(self, filepath: Path) -> Dict[str, Intent]:
        """Loads intent definitions from a single YAML file."""
        # ... (this function is unchanged from last time) ...
        loaded_intents: Dict[str, Intent] = {}
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            
            if not data or 'intents' not in data:
                print(f"Warning: 'intents:' key not found in {filepath.name}")
                return loaded_intents
                
            for intent_data in data['intents']:
                intent = Intent(
                    name=intent_data.get('name', 'UNKNOWN'),
                    description=intent_data.get('description', ''),
                    keywords=intent_data.get('keywords', [])
                )
                if intent.name != 'UNKNOWN':
                    loaded_intents[intent.name] = intent
            
            return loaded_intents

        except Exception as e:
            print(f"Error loading intents file {filepath}: {e}")
            return loaded_intents

    def classify_intent(self, text_input: str) -> Tuple[Intent, Optional[str]]:
        """
        (Placeholder) Simulates Intent Classification.
        """
        # ... (this function is unchanged from last time) ...
        text_lower = text_input.lower()
        
        for intent in self.intents.values():
            if intent.name == "OTHER":
                continue
            for keyword in intent.keywords:
                if keyword in text_lower:
                    return (intent, keyword) 
                    
        other_intent = self.intents.get("OTHER", Intent(name="OTHER", description="", keywords=[]))
        return (other_intent, None)

    def extract_entities(self, text_input: str, known_entities: Dict[str, Entity]) -> List[Entity]:
        """
        (Placeholder) Simulates Named Entity Recognition (NER).
        """
        # ... (this function is unchanged from last time) ...
        text_lower = text_input.lower()
        found_entities = []
        
        for entity_name, entity_obj in known_entities.items():
            if entity_name.lower() in text_lower:
                found_entities.append(entity_obj)
                
        return found_entities

    def process_player_input(self, text_input: str, known_entities: Dict[str, Entity]) -> ProcessedInput:
        """
        Runs the full (simulated) NLP pipeline on player input.
        """
        # 1. Classify Intent
        intent, matched_keyword = self.classify_intent(text_input)
        
        # 2. Extract Entities
        targets = self.extract_entities(text_input, known_entities)
        
        # 3. Store the skill name if the intent was USE_SKILL
        skill_name_to_store = None
        if intent.name == "USE_SKILL" and matched_keyword:
            # --- This logic is now data-driven ---
            # Use the map to find the base skill name.
            # Fall back to the keyword itself if not in the map
            # (for base skills like 'athletic', 'blade', etc.)
            skill_name_to_store = self.skill_keyword_map.get(matched_keyword, matched_keyword)
        
        return ProcessedInput(
            raw_text=text_input,
            intent=intent,
            targets=targets,
            skill_name=skill_name_to_store # This now stores the *base skill name*
        )

    def generate_npc_response(self, npc_entity: Entity, player_input: ProcessedInput, game_state: Dict[str, Any]) -> str:
        """
        (Placeholder) Simulates an LLM call to generate an NPC response.
        """
        # ... (this function is unchanged from last time) ...
        
        # 1. Check for a direct "talk" intent
        if player_input.intent.name == "DIALOGUE" and npc_entity in player_input.targets:
            if npc_entity.quote:
                return f"{npc_entity.name} says: \"{npc_entity.quote[0]}\""
            else:
                return f"{npc_entity.name} looks at you expectantly."
        
        # 2. Check if NPC was attacked
        if player_input.intent.name == "ATTACK" and npc_entity in player_input.targets:
            return f"{npc_entity.name} shouts: \"Aargh! You'll pay for that!\""
            
        # 3. Fallback
        return None