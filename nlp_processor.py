from __future__ import annotations
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass, field

try:
    import yaml
except ImportError:
    print("PyYAML not found. Please install: pip install PyYAML")
    yaml = None

# --- Imports for Semantic Similarity ---
try:
    from sentence_transformers import SentenceTransformer, util
    import torch
except ImportError:
    print("Warning: 'sentence-transformers' not found. Intent classification will not function.")
    print("Please install: pip install sentence-transformers")
    SentenceTransformer = None
    util = None
    torch = None

# --- New Imports for spaCy NER ---
try:
    import spacy
    from spacy.language import Language
    from spacy.matcher import Matcher
except ImportError:
    print("Warning: 'spacy' not found. Named Entity Recognition will not function.")
    print("Please install: pip install spacy")
    print("And download the model: python -m spacy download en_core_web_sm")
    spacy = None
    Language = None
    Matcher = None
# --- End New Imports ---


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
    
    Uses:
    - Semantic Similarity (sentence-transformers) for Intent Classification.
    - Token-based Matching (spaCy) for Named Entity Recognition.
    """
    
    # --- Intent Classification Attributes ---
    MODEL_NAME = 'all-MiniLM-L6-v2'
    SIMILARITY_THRESHOLD = 0.4 
    
    # --- NER Attributes ---
    SPACY_MODEL_NAME = 'en_core_web_sm'

    def __init__(self, ruleset_path: Path):
        """
        Initializes the processor, loads intents, and pre-computes
        embeddings for all intent keywords.
        
        Args:
            ruleset_path: Path to the active ruleset
                          (e.g., '.../rulesets/medievalfantasy')
        """
        
        self.intents: Dict[str, Intent] = {}
        
        if not yaml:
            raise ImportError("PyYAML is required to load intents.")
        if not SentenceTransformer or not util:
            print("CRITICAL: sentence-transformers library not found. Stopping.")
            raise ImportError("sentence-transformers library is required.")
        if not spacy or not Matcher:
            print("CRITICAL: spaCy library not found. Stopping.")
            raise ImportError("spaCy library is required.")
        
        # This will hold the mappings from skill_map.yaml
        self.skill_keyword_map: Dict[str, str] = {}
        
        # 1. Define paths
        root_path = ruleset_path.parent.parent
        core_intents_path = root_path / "intents.yaml"
        ruleset_intents_path = ruleset_path / "intents.yaml"
        skill_map_path = ruleset_path / "skll_map.yaml" #

        # 2. Load core intents
        print(f"NLP: Loading core intents from {core_intents_path.name}...")
        core_intents = self.load_intents_from_file(core_intents_path)
        self.intents.update(core_intents)
        
        # 3. Load ruleset-specific intents
        if ruleset_intents_path.exists():
            print(f"NLP: Loading ruleset intents from {ruleset_intents_path.name}...")
            ruleset_intents = self.load_intents_from_file(ruleset_intents_path)
            # This will merge/overwrite intents, e.g., adding keywords to USE_SKILL
            for name, intent_data in ruleset_intents.items():
                if name in self.intents:
                    self.intents[name].keywords.extend(intent_data.keywords)
                else:
                    self.intents[name] = intent_data
        else:
            print(f"NLP: No ruleset intents file found at {ruleset_intents_path.name}.")
            
        print(f"NLP: Loaded a total of {len(self.intents)} intents.")

        # 4. Load the skill map
        self.load_skill_map(skill_map_path)

        # 5. Load Intent Classification Model
        print(f"NLP: Loading sentence transformer model '{self.MODEL_NAME}'...")
        self.model = SentenceTransformer(self.MODEL_NAME)
        
        self.all_intent_keywords: List[Tuple[str, Intent]] = []
        keyword_corpus: List[str] = []

        for intent_name, intent_obj in self.intents.items():
            if intent_name == "OTHER":
                continue 
            for keyword in intent_obj.keywords:
                self.all_intent_keywords.append((keyword, intent_obj))
                keyword_corpus.append(keyword)
                
        print(f"NLP: Pre-computing embeddings for {len(keyword_corpus)} intent keywords...")
        self.keyword_embeddings = self.model.encode(
            keyword_corpus, 
            convert_to_tensor=True
        )
        
        # --- New: Load spaCy Model and Matcher ---
        print(f"NLP: Loading spaCy model '{self.SPACY_MODEL_NAME}'...")
        try:
            self.nlp: Language = spacy.load(self.SPACY_MODEL_NAME)
        except IOError:
            print(f"FATAL: spaCy model '{self.SPACY_MODEL_NAME}' not found.")
            print(f"Please run: python -m spacy download {self.SPACY_MODEL_NAME}")
            raise
            
        # Initialize the Matcher with the model's vocabulary
        self.matcher = Matcher(self.nlp.vocab)
        print("NLP: Initialization complete.")
        # --- End New ---

    def load_skill_map(self, filepath: Path):
        """Loads the keyword-to-skill mapping from skll_map.yaml."""
        if not filepath.exists():
            if filepath.name == "skill_map.yaml":
                filepath = filepath.with_name("skll_map.yaml")
                if filepath.exists():
                    print(f"NLP: Found 'skll_map.yaml' instead of 'skill_map.yaml'. Loading...")
                else:
                    print(f"NLP: No skill map file found at {filepath.name}. Using keywords as-is.")
                    return
            else:
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
        Classifies player input by finding the most semantically similar
        intent keyword.
        """
        other_intent = self.intents.get("OTHER", Intent(name="OTHER", description="", keywords=[]))

        if not text_input or not self.all_intent_keywords:
            return (other_intent, None)

        try:
            input_embedding = self.model.encode(
                text_input, 
                convert_to_tensor=True
            )
            
            cos_scores = util.cos_sim(input_embedding, self.keyword_embeddings)[0]
            
            top_score, top_index = torch.topk(cos_scores, k=1)
            
            top_score = top_score.item()
            top_index = top_index.item()

            if top_score >= self.SIMILARITY_THRESHOLD:
                matched_keyword, matched_intent = self.all_intent_keywords[top_index]
                
                print(f"NLP: Intent classified. "
                      f"Input='{text_input}', "
                      f"BestMatch='{matched_keyword}', "
                      f"Score={top_score:.4f}, "
                      f"Intent='{matched_intent.name}'")
                
                return (matched_intent, matched_keyword)
            else:
                print(f"NLP: No intent match found. "
                      f"Input='{text_input}', "
                      f"BestScore={top_score:.4f} (Threshold: {self.SIMILARITY_THRESHOLD})")
                return (other_intent, None)

        except Exception as e:
            print(f"Error during intent classification: {e}")
            return (other_intent, None)

    # --- REPLACED: This method now uses spaCy Matcher ---
    def extract_entities(self, text_input: str, known_entities: Dict[str, Entity]) -> List[Entity]:
        """
        Uses spaCy's Matcher to find game-specific entities in the text.
        """
        if not self.matcher or not self.nlp:
            return [] # spaCy failed to load

        # 1. Create a lowercase name -> Entity map for quick lookup
        # This is built on every call, which is fine since known_entities can change
        known_entities_lower_map = {name.lower(): obj for name, obj in known_entities.items()}

        # 2. Create Matcher patterns from our known_entities
        # We must remove old patterns first
        self.matcher.remove("GAME_ENTITY")
        patterns = []
        
        # Sort by length, longest first, to match "spike trap" before "spike"
        sorted_names = sorted(known_entities.keys(), key=len, reverse=True)
        
        for entity_name in sorted_names:
            # Create a pattern for each name, e.g., "spike trap"
            # becomes [{'LOWER': 'spike'}, {'LOWER': 'trap'}]
            pattern = [{"LOWER": word} for word in entity_name.lower().split()]
            patterns.append(pattern)
        
        # Add all patterns to the matcher
        self.matcher.add("GAME_ENTITY", patterns)

        # 3. Process the text and find matches
        doc = self.nlp(text_input)
        matches = self.matcher(doc)

        # 4. Convert matches back into Entity objects
        found_entities = []
        # Use a set to avoid adding the same entity multiple times
        # if it's mentioned more than once
        found_entity_names = set() 

        for match_id, start, end in matches:
            span = doc[start:end]
            span_text_lower = span.text.lower()
            
            if span_text_lower not in found_entity_names:
                entity_obj = known_entities_lower_map.get(span_text_lower)
                if entity_obj:
                    found_entities.append(entity_obj)
                    found_entity_names.add(span_text_lower)
                    
        if found_entities:
            print(f"NLP: Entities extracted: {[e.name for e in found_entities]}")

        return found_entities
    # --- END REPLACED METHOD ---

    def process_player_input(self, text_input: str, known_entities: Dict[str, Entity]) -> ProcessedInput:
        """
        Runs the full NLP pipeline on player input.
        """
        # 1. Classify Intent (uses semantic similarity)
        intent, matched_keyword = self.classify_intent(text_input)
        
        # 2. Extract Entities (now uses spaCy Matcher)
        targets = self.extract_entities(text_input, known_entities)
        
        # 3. Store the skill name if the intent was USE_SKILL
        skill_name_to_store = None
        if intent.name == "USE_SKILL" and matched_keyword:
            # This logic is data-driven
            skill_name_to_store = self.skill_keyword_map.get(matched_keyword, matched_keyword)
            print(f"NLP: Mapped skill. Keyword='{matched_keyword}', BaseSkill='{skill_name_to_store}'")
        
        return ProcessedInput(
            raw_text=text_input,
            intent=intent,
            targets=targets,
            skill_name=skill_name_to_store
        )

    def generate_npc_response(self, npc_entity: Entity, player_input: ProcessedInput, game_state: Dict[str, Any]) -> str:
        """
        (Placeholder) Simulates an LLM call to generate an NPC response.
        
        This function is unchanged.
        """
        
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