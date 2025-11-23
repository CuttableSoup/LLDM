"""
This module handles Natural Language Processing (NLP) for the LLDM application.

It uses sentence-transformers for intent classification and spaCy for named entity
recognition (NER). The processor hardcodes core game intents and dynamically
builds a 'USE_SKILL' intent by loading keywords from 'aptitude' blocks
in any ruleset YAML file.
"""
from __future__ import annotations
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass, field
import re
import logging

# Attempt to import necessary libraries, with warnings for missing dependencies.
try:
    import yaml
except ImportError:
    print("PyYAML not found. Please install: pip install PyYAML")
    yaml = None

try:
    from sentence_transformers import SentenceTransformer, util
    import torch
except ImportError:
    print("Warning: 'sentence-transformers' not found. Intent classification will not function.")
    print("Please install: pip install sentence-transformers")
    SentenceTransformer = None
    util = None
    torch = None

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

try:
    from classes import Entity, Attribute, Skill
except ImportError:
    print("Warning: 'classes.py' not found. Using placeholder Entity.")
    class Entity:
        name: str = ""
        quote: List[str] = []
    class Attribute: pass
    class Skill: pass

logger = logging.getLogger("NLPTestLogger")


# --- Hardcoded base intents ---
CORE_INTENTS_DATA = [
    {
    "name": "ATTACK",
    "description": "To assault or strike at an entity.",
    "keywords": [
        "attack", "hit", "swing", "strike", "fight", "harm",
        "punch", "kick", "stab", "slash",
        "cast", "conjure", "invoke", "manifest", "use", "chant", "pray"
        ]
    },
    {
    "name": "USE",
    "description": "To use, open, or manipulate an object or entity.",
    "keywords": [
        "open", "get", "take", "use", "pull", "push", "look",
        "inspect", "examine", "touch", "read", "activate", "deactivate",
        "equip", "unequip"
        ]
    },
    {
    "name": "MOVE",
    "description": "To change physical location.",
    "keywords": [
        "move", "go", "go to", "walk", "run", "head",
        "travel", "approach", "leave", "exit", "enter", "flee"
        ]
    },
    {
    "name": "TAKE",
    "description": "To use, open, or manipulate an object or entity.",
    "keywords": [
        "take"
        ]
    },
    {
    "name": "GIVE",
    "description": "To give an item to another entity.",
    "keywords": [
        "give", "hand", "offer"
        ]
    },
    {
    "name": "DIALOGUE",
    "description": "To engage in conversation with an entity.",
    "keywords": [
        "talk", "speak", "say", "ask", "tell", "shout",
        "whisper", "dialogue", "question", "respond"
        ]
    },
    {
    "name": "TRADE",
    "description": "To exchange items with another entity.",
    "keywords": [
        "trade", "buy", "sell", "barter", "purchase", "shop", "vendor"
        ]
    },
    {
    "name": "CRAFT",
    "description": "To create or repair an item.",
    "keywords": [
        "craft", "build", "make", "forge", "brew", "create", "repair", "fix"
        ]
    },
    {
    "name": "MEMORIZE",
    "description": "To study or commit something to memory.",
        "keywords": [
            "memorize", "study", "learn", "read", "recall"
        ]
    },
    {
        "name": "ASK_ADAM",
        "description": "To ask an out-of-character question to the assistant.",
        "keywords": [
            "what is", "who is", "where is", "can I", "what do I see",
            "what's", "who's", "where's", "how do I", "tell me about"
        ]
    },
    {
        "name": "USE_SKILL",
        "description": "To explicitly use a character skill.",
        "keywords": [ # This will be populated from aptitude blocks
        ]
    },
    {
        "name": "OTHER",
        "description": "General conversation or actions not covered.",
        "keywords": [ # Fallback intent
        ]
    }
]


@dataclass
class Intent:
    """Represents a player's intent, loaded from a YAML file."""
    name: str
    description: str
    keywords: List[str]

@dataclass
class ActionComponent:
    """Represents a single action component identified in the player's input."""
    intent: Intent
    keyword: str
    skill_name: Optional[str] = None

@dataclass
class ProcessedInput:
    """Represents the processed output of the NLP pipeline."""
    raw_text: str
    actions: List[ActionComponent] = field(default_factory=list)
    targets: List[Entity] = field(default_factory=list)
    interaction_entities: List[Entity] = field(default_factory=list)
    
class NLPProcessor:
    """Processes player input to understand intent and extract entities."""
    MODEL_NAME = 'all-MiniLM-L6-v2'  # The sentence-transformer model to use.
    SIMILARITY_THRESHOLD = 0.4       # The minimum similarity score for intent classification.
    SPACY_MODEL_NAME = 'en_core_web_sm' # The spaCy model for NER.

    def __init__(self, ruleset_path: Path):
        """Initializes the NLPProcessor."""
        self.intents: Dict[str, Intent] = {}
        
        # Ensure all required libraries are available.
        if not yaml:
            raise ImportError("PyYAML is required to load intents.")
        if not SentenceTransformer or not util:
            print("CRITICAL: sentence-transformers library not found. Stopping.")
            raise ImportError("sentence-transformers library is required.")
        if not spacy or not Matcher:
            print("CRITICAL: spaCy library not found. Stopping.")
            raise ImportError("spaCy library is required.")
        
        self.skill_keyword_map: Dict[str, str] = {}
        
        # --- Load hardcoded intents ---
        print("NLP: Loading hardcoded core intents...")
        for intent_data in CORE_INTENTS_DATA:
            intent = Intent(
                name=intent_data.get('name', 'UNKNOWN'),
                description=intent_data.get('description', ''),
                keywords=intent_data.get('keywords', [])
            )
            if intent.name != 'UNKNOWN':
                self.intents[intent.name] = intent
        
        print(f"NLP: Loaded {len(self.intents)} core intents.")

        # --- Load attributes.yaml to find skill keywords ---
        self.all_intent_keywords: List[Tuple[str, Intent]] = []
        keyword_corpus: List[str] = []

        # 1. Add keywords from all core intents
        use_skill_intent = self.intents.get("USE_SKILL")
        for intent_name, intent_obj in self.intents.items():
            if intent_name == "OTHER" or intent_name == "USE_SKILL":
                continue 
            for keyword in intent_obj.keywords:
                self.all_intent_keywords.append((keyword, intent_obj))
                keyword_corpus.append(keyword)

        # 2. Scan all YAML files for 'aptitude:' blocks and parse skill keywords
        if use_skill_intent:
            print(f"NLP: Scanning for skill keywords in {ruleset_path}...")
            
            for yaml_file in ruleset_path.glob("**/*.yaml"):
                try:
                    with open(yaml_file, 'r', encoding='utf-8') as f:
                        attr_docs = [doc for doc in yaml.safe_load_all(f) if doc]
                    
                    for doc in attr_docs:
                        if 'aptitude' not in doc or not isinstance(doc['aptitude'], dict):
                            continue

                        # Found an aptitude block, process it
                        for attr_name, attr_data in doc.get('aptitude', {}).items():
                            if not isinstance(attr_data, dict): continue
                            
                            # Loop through skills (e.g., 'blade', 'athletic')
                            for skill_name, skill_data in attr_data.items():
                                if not isinstance(skill_data, dict): continue

                                # Add keywords from the skill
                                skill_keywords = skill_data.get('keywords', [])
                                for keyword in skill_keywords:
                                    self.all_intent_keywords.append((keyword, use_skill_intent))
                                    keyword_corpus.append(keyword)
                                    # Map keyword to its skill name
                                    self.skill_keyword_map[keyword] = skill_name
                                
                                # Loop through specializations (e.g., 'longsword', 'telepathy')
                                for spec_name, spec_data in skill_data.items():
                                    if not isinstance(spec_data, dict): continue
                                    
                                    # Add keywords from the specialization
                                    spec_keywords = spec_data.get('keywords', [])
                                    for keyword in spec_keywords:
                                        self.all_intent_keywords.append((keyword, use_skill_intent))
                                        keyword_corpus.append(keyword)
                                        # Map specialization keyword to its own name
                                        self.skill_keyword_map[keyword] = spec_name

                except Exception as e:
                    print(f"Warning: Error parsing {yaml_file.name} for aptitudes: {e}")
        else:
            print(f"NLP: USE_SKILL intent missing, skipping dynamic keyword loading.")
        
        print(f"NLP: Built skill map with {len(self.skill_keyword_map)} entries.")

        # Load the sentence-transformer model.
        print(f"NLP: Loading sentence transformer model '{self.MODEL_NAME}'...")
        self.model = SentenceTransformer(self.MODEL_NAME)
        
        # Pre-compute embeddings for all keywords for faster similarity search.
        print(f"NLP: Pre-computing embeddings for {len(keyword_corpus)} intent keywords...")
        if not keyword_corpus:
            print("NLP Warning: No keywords found. Intent classification will fail.")
            self.keyword_embeddings = None
        else:
            self.keyword_embeddings = self.model.encode(
                keyword_corpus, 
                convert_to_tensor=True
            )
        
        # Load the spaCy model.
        print(f"NLP: Loading spaCy model '{self.SPACY_MODEL_NAME}'...")
        try:
            self.nlp: Language = spacy.load(self.SPACY_MODEL_NAME)
        except IOError:
            print(f"FATAL: spaCy model '{self.SPACY_MODEL_NAME}' not found.")
            print(f"Please run: python -m spacy download {self.SPACY_MODEL_NAME}")
            raise
            
        print("NLP: Initialization complete.")


    def classify_intent(self, text_input: str) -> Optional[Tuple[Intent, str]]:
        """
        Classifies the intent of a given text input.

        Args:
            text_input: The text to classify.

        Returns:
            A tuple containing the matched Intent and the keyword that matched, or None.
        """
        if not text_input or not self.all_intent_keywords or self.keyword_embeddings is None:
            return None

        try:
            # Encode the input text and compare its similarity to the keyword embeddings.
            input_embedding = self.model.encode(
                text_input, 
                convert_to_tensor=True
            )
            
            cos_scores = util.cos_sim(input_embedding, self.keyword_embeddings)[0]
            top_score, top_index = torch.topk(cos_scores, k=1)
            
            top_score_item = top_score.item()
            top_index_item = top_index.item()

            if top_score_item >= self.SIMILARITY_THRESHOLD:
                keyword, intent = self.all_intent_keywords[top_index_item]
                logger.info(f"NLP: classify_intent processed clause: '{text_input}'. "
                            f"Best Match=['{intent.name}' (from '{keyword}', score={top_score_item:.2f})]")
                return (intent, keyword)
            else:
                logger.info(f"NLP: No intent match for clause: '{text_input}'. "
                            f"BestScore={top_score_item:.4f} (Threshold: {self.SIMILARITY_THRESHOLD})")
                return None

        except Exception as e:
            logger.error(f"Error during intent classification for clause '{text_input}': {e}")
            return None

    def extract_entities(self, text_input: str, known_entities: Dict[str, Entity]) -> List[Entity]:
        """
        Extracts known entities from the text input using spaCy's Matcher.

        Args:
            text_input: The text to extract entities from.
            known_entities: A dictionary of known entities in the game.

        Returns:
            A list of Entity objects found in the text.
        """
        logger.info(f"NLP_NER: extract_entities called for text: '{text_input}'")
        logger.info(f"NLP_NER: Received {len(known_entities)} known_entities. Names: {list(known_entities.keys())}")
        
        matcher = Matcher(self.nlp.vocab)
        
        if not self.nlp or not known_entities:
            logger.warning("NLP_NER: NLP model or known_entities list is empty. Aborting NER.")
            return []

        # Create patterns for the spaCy Matcher from the known entity names.
        known_entities_lower_map = {name.lower(): obj for name, obj in known_entities.items()}
        patterns = []
        sorted_names = sorted(known_entities.keys(), key=len, reverse=True)
        
        for entity_name in sorted_names:
            pattern = [{"LOWER": word} for word in entity_name.lower().split()]
            patterns.append(pattern)
        
        if not patterns:
            logger.warning("NLP_NER: No patterns were generated for the matcher.")
            return []
            
        matcher.add("GAME_ENTITY", patterns)
        logger.info(f"NLP_NER: Added {len(patterns)} patterns to matcher. (e.g., {patterns[0]})")

        doc = self.nlp(text_input)
        matches = matcher(doc)

        found_entities = []
        found_entity_names = set() 

        # Process the matches and retrieve the corresponding Entity objects.
        for match_id, start, end in matches:
            span = doc[start:end]
            span_text_lower = span.text.lower()
            
            if span_text_lower not in found_entity_names:
                entity_obj = known_entities_lower_map.get(span_text_lower)
                if entity_obj:
                    found_entities.append(entity_obj)
                    found_entity_names.add(span_text_lower)
                    
        if found_entities:
            logger.info(f"NLP_NER: Entities extracted: {[e.name for e in found_entities]}")
        else:
            logger.info(f"NLP_NER: Matcher found 0 entities in: '{text_input}'")

        return found_entities

    def process_player_input(self, text_input: str, known_entities: Dict[str, Entity]) -> ProcessedInput:
        """
        Processes the full player input string.

        This method splits the input into clauses, classifies the intent of each clause,
        and extracts entities from the original text.

        Args:
            text_input: The raw player input string.
            known_entities: A dictionary of known entities in the game.

        Returns:
            A ProcessedInput object containing the results of the NLP pipeline.
        """
        all_found_entities = self.extract_entities(text_input, known_entities)
        
        # Separate targets from interaction entities (spells, etc.)
        targets = []
        interaction_entities = []
        for e in all_found_entities:
            if e.supertype in ("creature", "object", "environment"):
                targets.append(e)
            elif e.supertype == "supernatural":
                interaction_entities.append(e)
        
        # Split the input into clauses based on conjunctions.
        clauses = re.split(r'[,]| and | then ', text_input, flags=re.IGNORECASE)
        clauses = [clause.strip() for clause in clauses if clause.strip()]
        
        if not clauses:
            clauses = [text_input] 
            
        logger.info(f"NLP: Processing input. Split into {len(clauses)} clauses: {clauses}")

        all_matched_actions: List[Tuple[Intent, str]] = []
        
        is_first_clause = True
        for clause in clauses:
            
            doc = self.nlp(clause)
            has_action_word = any(token.pos_ in ["VERB", "AUX"] for token in doc)
            
            # Only classify intent if it's the first clause or contains a verb.
            if is_first_clause or has_action_word:
                logger.info(f"NLP: Processing clause: '{clause}' (First Clause: {is_first_clause}, Has Verb: {has_action_word})")
                result = self.classify_intent(clause)
                if result:
                    all_matched_actions.append(result)
            else:
                pos_tags = [f"{token.text}({token.pos_})" for token in doc]
                logger.info(f"NLP: Skipping clause (not first, no VERB/AUX): '{clause}'. POS: {pos_tags}")

            is_first_clause = False

        # Consolidate the matched intents, keeping only one of each type.
        final_intents: Dict[str, Tuple[Intent, str]] = {}
        for intent, keyword in all_matched_actions:
            if intent.name not in final_intents:
                final_intents[intent.name] = (intent, keyword)
                
        matched_actions = list(final_intents.values())

        action_components: List[ActionComponent] = []
        
        for intent, keyword in matched_actions:
            skill_name_to_store = None
            if intent.name == "USE_SKILL" and keyword:
                skill_name_to_store = self.skill_keyword_map.get(keyword, keyword)
                logger.info(f"NLP: Mapped skill. Keyword='{keyword}', BaseSkill='{skill_name_to_store}'")
            
            action_components.append(
                ActionComponent(
                    intent=intent,
                    keyword=keyword,
                    skill_name=skill_name_to_store
                )
            )
        
        # If no specific intents are found, default to the "OTHER" intent.
        other_intent = self.intents.get("OTHER")
        if not action_components and other_intent:
            logger.info("NLP: No specific intents found. Defaulting to OTHER.")
            action_components.append(
                ActionComponent(intent=other_intent, keyword="")
            )

        return ProcessedInput(
            raw_text=text_input,
            actions=action_components,
            targets=targets,
            interaction_entities=interaction_entities
        )

    def generate_npc_response(self, npc_entity: Entity, player_input: ProcessedInput, game_state: Dict[str, Any]) -> str:
        """
        Generates a simple, rule-based response for an NPC.

        This is a placeholder for more complex LLM-based NPC logic.

        Args:
            npc_entity: The NPC entity.
            player_input: The processed player input.
            game_state: The current state of the game.

        Returns:
            A string representing the NPC's response, or None.
        """
        for action in player_input.actions:
            if action.intent.name == "DIALOGUE" and npc_entity in player_input.targets:
                if npc_entity.quote:
                    return f"{npc_entity.name} says: \"{npc_entity.quote[0]}\""
                else:
                    return f"{npc_entity.name} looks at you expectantly."
            
            if action.intent.name == "ATTACK" and npc_entity in player_input.targets:
                return f"{npc_entity.name} shouts: \"Aargh! You'll pay for that!\""
            
        return None