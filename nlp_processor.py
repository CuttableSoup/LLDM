from __future__ import annotations
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass, field
import re
import logging
try:
    import yaml
except ImportError:
    logger = logging.getLogger("NLP")
    logger.warning("PyYAML not found. Please install: pip install PyYAML")
    yaml = None
try:
    from sentence_transformers import SentenceTransformer, util
    import torch
except ImportError:
    logger = logging.getLogger("NLP")
    logger.warning("Warning: 'sentence-transformers' not found. Intent classification will not function.")
    logger.warning("Please install: pip install sentence-transformers")
    SentenceTransformer = None
    util = None
    torch = None
try:
    import spacy
    from spacy.language import Language
    from spacy.matcher import Matcher
except ImportError:
    logger = logging.getLogger("NLP")
    logger.warning("Warning: 'spacy' not found. Named Entity Recognition will not function.")
    logger.warning("Please install: pip install spacy")
    logger.warning("And download the model: python -m spacy download en_core_web_sm")
    spacy = None
    Language = None
    Matcher = None
try:
    from models import Entity, Attribute, Skill
except ImportError:
    logger = logging.getLogger("NLP")
    logger.warning("Warning: 'models.py' not found. Using placeholder Entity.")
    class Entity:
        name: str = ""
        quote: List[str] = []
        supertype: str = ""
    class Attribute: pass
    class Skill: pass
logger = logging.getLogger("NLP")
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
        "keywords": [
        ]
    },
    {
        "name": "OTHER",
        "description": "General conversation or actions not covered.",
        "keywords": [
        ]
    }
]
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
    interaction_entities: List[Entity] = field(default_factory=list)
class NLPProcessor:
    MODEL_NAME = 'all-MiniLM-L6-v2'
    SIMILARITY_THRESHOLD = 0.4
    SPACY_MODEL_NAME = 'en_core_web_sm'
    def __init__(self, ruleset_path: Path):
        self.intents: Dict[str, Intent] = {}
        if not yaml:
            raise ImportError("PyYAML is required to load intents.")
        if not SentenceTransformer or not util:
            logger.critical("CRITICAL: sentence-transformers library not found. Stopping.")
            raise ImportError("sentence-transformers library is required.")
        if not spacy or not Matcher:
            logger.critical("CRITICAL: spaCy library not found. Stopping.")
            raise ImportError("spaCy library is required.")
        self.skill_keyword_map: Dict[str, str] = {}
        logger.info("NLP: Loading hardcoded core intents...")
        for intent_data in CORE_INTENTS_DATA:
            intent = Intent(
                name=intent_data.get('name', 'UNKNOWN'),
                description=intent_data.get('description', ''),
                keywords=intent_data.get('keywords', [])
            )
            if intent.name != 'UNKNOWN':
                self.intents[intent.name] = intent
        logger.info(f"NLP: Loaded {len(self.intents)} core intents.")
        self.all_intent_keywords: List[Tuple[str, Intent]] = []
        keyword_corpus: List[str] = []
        use_skill_intent = self.intents.get("USE_SKILL")
        for intent_name, intent_obj in self.intents.items():
            if intent_name == "OTHER" or intent_name == "USE_SKILL":
                continue
            for keyword in intent_obj.keywords:
                self.all_intent_keywords.append((keyword, intent_obj))
                keyword_corpus.append(keyword)
        if use_skill_intent:
            logger.info(f"NLP: Scanning for skill keywords in {ruleset_path}...")
            for yaml_file in ruleset_path.glob("**/*.yaml"):
                try:
                    with open(yaml_file, 'r', encoding='utf-8') as f:
                        attr_docs = [doc for doc in yaml.safe_load_all(f) if doc]
                    for doc in attr_docs:
                        if 'aptitude' not in doc or not isinstance(doc['aptitude'], dict):
                            continue
                        for attr_name, attr_data in doc.get('aptitude', {}).items():
                            if not isinstance(attr_data, dict): continue
                            for skill_name, skill_data in attr_data.items():
                                if not isinstance(skill_data, dict): continue
                                skill_keywords = skill_data.get('keywords', [])
                                for keyword in skill_keywords:
                                    self.all_intent_keywords.append((keyword, use_skill_intent))
                                    keyword_corpus.append(keyword)
                                    self.skill_keyword_map[keyword] = skill_name
                                for spec_name, spec_data in skill_data.items():
                                    if not isinstance(spec_data, dict): continue
                                    spec_keywords = spec_data.get('keywords', [])
                                    for keyword in spec_keywords:
                                        self.all_intent_keywords.append((keyword, use_skill_intent))
                                        keyword_corpus.append(keyword)
                                        self.skill_keyword_map[keyword] = spec_name
                except Exception as e:
                    logger.warning(f"Warning: Error parsing {yaml_file.name} for aptitudes: {e}")
        else:
            logger.warning(f"NLP: USE_SKILL intent missing, skipping dynamic keyword loading.")
        logger.info(f"NLP: Built skill map with {len(self.skill_keyword_map)} entries.")
        logger.info(f"NLP: Loading sentence transformer model '{self.MODEL_NAME}'...")
        self.model = SentenceTransformer(self.MODEL_NAME)
        logger.info(f"NLP: Pre-computing embeddings for {len(keyword_corpus)} intent keywords...")
        if not keyword_corpus:
            logger.warning("NLP Warning: No keywords found. Intent classification will fail.")
            self.keyword_embeddings = None
        else:
            self.keyword_embeddings = self.model.encode(
                keyword_corpus,
                convert_to_tensor=True
            )
        logger.info(f"NLP: Loading spaCy model '{self.SPACY_MODEL_NAME}'...")
        try:
            self.nlp: Language = spacy.load(self.SPACY_MODEL_NAME)
        except IOError:
            logger.critical(f"FATAL: spaCy model '{self.SPACY_MODEL_NAME}' not found.")
            logger.critical(f"Please run: python -m spacy download {self.SPACY_MODEL_NAME}")
            raise
        logger.info("NLP: Initialization complete.")
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
        known_entities_lower_map = {name.lower(): obj for name, obj in known_entities.items()}
        patterns = []
        sorted_names = sorted(known_entities.keys(), key=len, reverse=True)
        for entity_name in sorted_names:
            pattern = [{"LOWER": word} for word in entity_name.lower().split()]
            patterns.append(pattern)
            words = entity_name.lower().split()
            if len(words) > 1:
                for word in words:
                    if len(word) > 3:
                        patterns.append([{"LOWER": word}])
                        if word not in known_entities_lower_map:
                            known_entities_lower_map[word] = known_entities[entity_name]
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
        targets = []
        interaction_entities = []
        for e in all_found_entities:
            if e.supertype in ("creature", "object", "environment"):
                targets.append(e)
            elif e.supertype == "supernatural":
                interaction_entities.append(e)
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
