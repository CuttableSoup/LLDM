from __future__ import annotations
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass, field
import re
import logging

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

@dataclass
class Intent:
    name: str
    description: str
    keywords: List[str]

@dataclass
class ActionComponent:
    intent: Intent
    keyword: str
    skill_name: Optional[str] = None

@dataclass
class ProcessedInput:
    raw_text: str
    actions: List[ActionComponent] = field(default_factory=list)
    targets: List[Entity] = field(default_factory=list)
    
class NLPProcessor:
    MODEL_NAME = 'all-MiniLM-L6-v2'
    SIMILARITY_THRESHOLD = 0.4 
    SPACY_MODEL_NAME = 'en_core_web_sm'

    def __init__(self, ruleset_path: Path):
        self.intents: Dict[str, Intent] = {}
        
        if not yaml:
            raise ImportError("PyYAML is required to load intents.")
        if not SentenceTransformer or not util:
            print("CRITICAL: sentence-transformers library not found. Stopping.")
            raise ImportError("sentence-transformers library is required.")
        if not spacy or not Matcher:
            print("CRITICAL: spaCy library not found. Stopping.")
            raise ImportError("spaCy library is required.")
        
        self.skill_keyword_map: Dict[str, str] = {}
        
        root_path = ruleset_path.parent.parent
        core_intents_path = root_path / "intents.yaml"
        ruleset_intents_path = ruleset_path / "intents.yaml"
        skill_map_path = ruleset_path / "skll_map.yaml"

        print(f"NLP: Loading core intents from {core_intents_path.name}...")
        core_intents = self.load_intents_from_file(core_intents_path)
        self.intents.update(core_intents)
        
        if ruleset_intents_path.exists():
            print(f"NLP: Loading ruleset intents from {ruleset_intents_path.name}...")
            ruleset_intents = self.load_intents_from_file(ruleset_intents_path)
            for name, intent_data in ruleset_intents.items():
                if name in self.intents:
                    self.intents[name].keywords.extend(intent_data.keywords)
                else:
                    self.intents[name] = intent_data
        else:
            print(f"NLP: No ruleset intents file found at {ruleset_intents_path.name}.")
            
        print(f"NLP: Loaded a total of {len(self.intents)} intents.")

        self.load_skill_map(skill_map_path)

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
        
        print(f"NLP: Loading spaCy model '{self.SPACY_MODEL_NAME}'...")
        try:
            self.nlp: Language = spacy.load(self.SPACY_MODEL_NAME)
        except IOError:
            print(f"FATAL: spaCy model '{self.SPACY_MODEL_NAME}' not found.")
            print(f"Please run: python -m spacy download {self.SPACY_MODEL_NAME}")
            raise
            
        print("NLP: Initialization complete.")


    def load_skill_map(self, filepath: Path):
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

    def classify_intent(self, text_input: str) -> Optional[Tuple[Intent, str]]:
        if not text_input or not self.all_intent_keywords:
            return None

        try:
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
        
        logger.info(f"NLP_NER: extract_entities called for text: '{text_input}'")
        logger.info(f"NLP_NER: Received {len(known_entities)} known_entities. Names: {list(known_entities.keys())}")
        
        matcher = Matcher(self.nlp.vocab)
        
        if not self.nlp or not known_entities:
            logger.warning("NLP_NER: NLP model or known_entities list is empty. Aborting NER.")
            return []

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
        
        targets = self.extract_entities(text_input, known_entities)
        
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
            
            if is_first_clause or has_action_word:
                logger.info(f"NLP: Processing clause: '{clause}' (First Clause: {is_first_clause}, Has Verb: {has_action_word})")
                result = self.classify_intent(clause)
                if result:
                    all_matched_actions.append(result)
            else:
                pos_tags = [f"{token.text}({token.pos_})" for token in doc]
                logger.info(f"NLP: Skipping clause (not first, no VERB/AUX): '{clause}'. POS: {pos_tags}")

            is_first_clause = False

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
        
        other_intent = self.intents.get("OTHER")
        if not action_components and other_intent:
            logger.info("NLP: No specific intents found. Defaulting to OTHER.")
            action_components.append(
                ActionComponent(intent=other_intent, keyword="")
            )

        return ProcessedInput(
            raw_text=text_input,
            actions=action_components,
            targets=targets
        )

    def generate_npc_response(self, npc_entity: Entity, player_input: ProcessedInput, game_state: Dict[str, Any]) -> str:
        
        for action in player_input.actions:
            if action.intent.name == "DIALOGUE" and npc_entity in player_input.targets:
                if npc_entity.quote:
                    return f"{npc_entity.name} says: \"{npc_entity.quote[0]}\""
                else:
                    return f"{npc_entity.name} looks at you expectantly."
            
            if action.intent.name == "ATTACK" and npc_entity in player_input.targets:
                return f"{npc_entity.name} shouts: \"Aargh! You'll pay for that!\""
            
        return None