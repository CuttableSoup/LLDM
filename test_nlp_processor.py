import unittest
import logging
from pathlib import Path
from typing import List, Dict

from nlp_processor import NLPProcessor
from classes import Entity

RULESET_PATH = Path(__file__).parent / "rulesets" / "medievalfantasy"

LOG_FILE = "test_nlp_results.log"

class TestNLPProcessor(unittest.TestCase):
    processor: NLPProcessor
    dummy: Entity
    chest: Entity
    wolf: Entity
    known_entities: Dict[str, Entity]
    logger: logging.Logger
    
    @classmethod
    def setUpClass(cls):
        
        cls.logger = logging.getLogger("NLPTestLogger")
        cls.logger.setLevel(logging.INFO)
        
        handler = logging.FileHandler(LOG_FILE, mode='w', encoding='utf-8')
        
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        
        if not cls.logger.hasHandlers():
            cls.logger.addHandler(handler)
        
        cls.logger.info("--- STARTING NLP PROCESSOR TEST RUN ---")
        
        cls.logger.info("Loading NLP Model for testing (this may take a moment)...")
        print("--- Loading NLP Model for testing (this may take a moment) ---")
        try:
            cls.processor = NLPProcessor(RULESET_PATH)
        except ImportError as e:
            cls.logger.critical(f"FATAL: Failed to import a required library: {e}")
            cls.logger.critical("Please ensure 'sentence-transformers', 'torch', and 'spacy' are installed.")
            cls.logger.critical("pip install sentence-transformers torch spacy")
            cls.logger.critical("python -m spacy download en_core_web_sm")
            raise
        except IOError as e:
            cls.logger.critical(f"FATAL: Failed to load a required model: {e}")
            cls.logger.critical("Please ensure the spaCy model is downloaded: python -m spacy download en_core_web_sm")
            raise

        cls.dummy = Entity(name="dummy")
        cls.chest = Entity(name="chest")
        cls.wolf = Entity(name="wolf")
        
        cls.known_entities = {
            "dummy": cls.dummy,
            "chest": cls.chest,
            "wolf": cls.wolf,
        }
        
        cls.logger.info("NLP Model and mock entities loaded successfully.")
        print("--- NLP Model loaded. Running tests... ---")

    @classmethod
    def tearDownClass(cls):
        cls.logger.info("--- NLP PROCESSOR TEST RUN COMPLETE ---")

    def assert_intent_targets(self, text: str, expected_intent_names: List[str], expected_target_entities: List[Entity]):
        result = self.processor.process_player_input(text, self.known_entities)
        
        found_intent_names = sorted([a.intent.name for a in result.actions])
        found_target_names = sorted([t.name for t in result.targets])
        
        expected_intents_sorted = sorted(expected_intent_names)
        expected_targets_sorted = sorted([e.name for e in expected_target_entities])
        
        self.logger.info(f"--- Testing Phrase ---")
        self.logger.info(f"INPUT:    \"{text}\"")
        self.logger.info(f"FOUND:    Intents={found_intent_names}, Targets={found_target_names}")
        self.logger.info(f"EXPECTED: Intents={expected_intents_sorted}, Targets={expected_targets_sorted}")

        try:
            self.assertEqual(
                found_intent_names, 
                expected_intents_sorted,
                f"Failed intent check for: '{text}'"
            )
            
            self.assertEqual(
                found_target_names, 
                expected_targets_sorted,
                f"Failed target check for: '{text}'"
            )
            self.logger.info("RESULT:   PASSED")

        except AssertionError as e:
            self.logger.error(f"RESULT:   FAILED! {e}")
            raise e
        finally:
            self.logger.info("-" * 20)

    
    def test_one_action_one_target(self):
        self.assert_intent_targets(
            text="attack the dummy",
            expected_intent_names=['ATTACK'],
            expected_target_entities=[self.dummy]
        )

    def test_one_action_one_target_natural(self):
        self.assert_intent_targets(
            text="I want to hit the dummy",
            expected_intent_names=['ATTACK'],
            expected_target_entities=[self.dummy]
        )

    def test_one_action_two_targets(self):
        self.assert_intent_targets(
            text="look at the dummy and the chest",
            expected_intent_names=['INTERACT'],
            expected_target_entities=[self.dummy, self.chest]
        )
        
    def test_one_action_three_targets(self):
        self.assert_intent_targets(
            text="examine the dummy, the chest, and the wolf",
            expected_intent_names=['INTERACT'],
            expected_target_entities=[self.dummy, self.chest, self.wolf]
        )

    def test_two_actions_one_target(self):
        self.assert_intent_targets(
            text="move to and attack the dummy",
            expected_intent_names=['MOVE', 'ATTACK'],
            expected_target_entities=[self.dummy]
        )

    def test_two_actions_one_target_natural(self):
        self.assert_intent_targets(
            text="I'm going to run at the wolf and attack it",
            expected_intent_names=['MOVE', 'ATTACK'],
            expected_target_entities=[self.wolf]
        )
        
    def test_two_actions_two_targets(self):
        self.assert_intent_targets(
            text="go to the chest and look at the dummy",
            expected_intent_names=['MOVE', 'INTERACT'],
            expected_target_entities=[self.chest, self.dummy]
        )
        
    def test_two_actions_three_targets(self):
        self.assert_intent_targets(
            text="go to the chest and attack the dummy and the wolf",
            expected_intent_names=['MOVE', 'ATTACK'],
            expected_target_entities=[self.chest, self.dummy, self.wolf]
        )
        
    def test_three_actions_one_target(self):
        self.assert_intent_targets(
            text="move to the dummy, attack it, and then inspect it",
            expected_intent_names=['MOVE', 'ATTACK', 'INTERACT'],
            expected_target_entities=[self.dummy]
        )
        
    def test_three_actions_two_targets(self):
        self.assert_intent_targets(
            text="move to the chest, open it, and talk to the dummy",
            expected_intent_names=['MOVE', 'INTERACT', 'DIALOGUE'],
            expected_target_entities=[self.chest, self.dummy]
        )
        
    def test_three_actions_three_targets(self):
        self.assert_intent_targets(
            text="move to the dummy, attack the wolf, and open the chest",
            expected_intent_names=['MOVE', 'ATTACK', 'INTERACT'],
            expected_target_entities=[self.dummy, self.wolf, self.chest]
        )
        
    def test_skill_intent_mapping(self):
        text = "pick lock on the chest"
        expected_intent = 'USE_SKILL'
        expected_skill = 'trickery'
        expected_target = 'chest'

        self.logger.info(f"--- Testing Phrase ---")
        self.logger.info(f"INPUT:    \"{text}\"")
        
        result = self.processor.process_player_input(text, self.known_entities)
        
        found_intent = result.actions[0].intent.name if result.actions else "None"
        found_skill = result.actions[0].skill_name if result.actions else "None"
        found_targets = sorted([t.name for t in result.targets])

        self.logger.info(f"FOUND:    Intent={found_intent}, Skill={found_skill}, Targets={found_targets}")
        self.logger.info(f"EXPECTED: Intent={expected_intent}, Skill={expected_skill}, Targets=['{expected_target}']")

        try:
            self.assertEqual(len(result.actions), 1, "Should only find one action")
            self.assertEqual(found_intent, expected_intent, "Intent should be 'USE_SKILL'")
            self.assertEqual(found_skill, expected_skill, "Skill 'pick lock' should be mapped to 'trickery'")
            self.assertIn(self.chest, result.targets, "Failed to find 'chest' target")
            self.logger.info("RESULT:   PASSED")
        except AssertionError as e:
            self.logger.error(f"RESULT:   FAILED! {e}")
            raise e
        finally:
            self.logger.info("-" * 20)

    def test_no_intent_no_target(self):
        text = "what a nice day"
        expected_intent = 'OTHER'
        
        self.logger.info(f"--- Testing Phrase ---")
        self.logger.info(f"INPUT:    \"{text}\"")

        result = self.processor.process_player_input(text, self.known_entities)
        
        found_intent = result.actions[0].intent.name if result.actions else "None"
        found_targets = sorted([t.name for t in result.targets])
        
        self.logger.info(f"FOUND:    Intent={found_intent}, Targets={found_targets}")
        self.logger.info(f"EXPECTED: Intent={expected_intent}, Targets=[]")

        try:
            self.assertEqual(len(result.actions), 1)
            self.assertEqual(found_intent, expected_intent)
            self.assertEqual(len(result.targets), 0)
            self.logger.info("RESULT:   PASSED")
        except AssertionError as e:
            self.logger.error(f"RESULT:   FAILED! {e}")
            raise e
        finally:
            self.logger.info("-" * 20)

if __name__ == "__main__":
    unittest.main()