
import unittest
import logging
from pathlib import Path
from models import Entity, Interaction, Effect, Magnitude, GameTime
from interaction_manager import InteractionManager
from nlp_processor import NLPProcessor, ProcessedInput
# We need to mock GameController's process_player_actions_logic or import it if possible.
# Since GameController is in game_engine.py and has many dependencies, 
# I will copy the relevant logic or import the class and mock dependencies.
# Importing is better to test actual code.
from game_engine import GameController

# Mocking dependencies for GameController
class MockLLMManager:
    def generate_response(self, prompt, history):
        return "Narrative response"

class MockGameTime:
    def __init__(self):
        self.total_seconds = 0
    def advance_time(self, seconds):
        self.total_seconds += seconds
    def copy(self):
        return self

class TestNLPIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Setup logging to see NLP output
        logging.basicConfig(level=logging.INFO)
        
        # Initialize NLP Processor (this might take a moment to load models)
        # We point to the rulesets directory to load intents/skills
        cls.ruleset_path = Path("c:/Users/Administrator/Projects/LLDM/rulesets/medievalfantasy")
        cls.nlp = NLPProcessor(cls.ruleset_path)

# Mock GameController to avoid loading rulesets
class MockGameController(GameController):
    def __init__(self):
        # Skip super().__init__ to avoid loading rulesets
        self.interaction_manager = None
        self.game_time = None
        self.player_entity = None
        self.initiative_order = []
        self.llm_manager = None
        self.current_room = None
        self.entity_histories = {}
        self.round_history = []
        self.llm_chat_history = []

class TestNLPIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Setup logging to see NLP output
        logging.basicConfig(level=logging.INFO)
        
        # Initialize NLP Processor (this might take a moment to load models)
        # We point to the rulesets directory to load intents/skills
        cls.ruleset_path = Path("c:/Users/Administrator/Projects/LLDM/rulesets/medievalfantasy")
        cls.nlp = NLPProcessor(cls.ruleset_path)

    def setUp(self):
        self.interaction_manager = InteractionManager()
        self.game_time = MockGameTime()
        
        # Setup Entities
        self.player = Entity(name="Valerius", cur_hp=20, max_hp=20, supertype="creature")
        self.player.x = 5
        self.player.y = 7
        
        # Dummy is at (5, 5). South of dummy is (5, 6).
        self.dummy = Entity(name="Training Dummy", cur_hp=20, max_hp=20, supertype="creature")
        self.dummy.x = 5
        self.dummy.y = 5
        
        # Give player a weapon/attack interaction
        # Based on player.yaml, Valerius has 'longsword' skill or interaction.
        # We'll give him a generic 'attack' interaction for this test.
        attack_effect = Effect(name="damage", magnitude=Magnitude(value=5))
        self.player.interaction.append(
            Interaction(type="attack", target_effect=[attack_effect])
        )
        
        self.known_entities = {
            "Valerius": self.player,
            "Training Dummy": self.dummy
        }
        
        # Use MockGameController
        self.controller = MockGameController()
        self.controller.interaction_manager = self.interaction_manager
        self.controller.game_time = self.game_time
        self.controller.player_entity = self.player
        self.controller.initiative_order = [self.player, self.dummy]
        self.controller.llm_manager = MockLLMManager()
        self.controller.current_room = type('obj', (object,), {'legend': [], 'layers': []}) # Mock room
        self.controller.game_entities = self.known_entities # Fix: Assign game_entities
        
        # Mock move_entity to just update coordinates without map checks
        # We override the method on the instance
        def mock_move(entity, tx, ty):
            entity.x = tx
            entity.y = ty
            return True
        self.controller.move_entity = mock_move

    def test_imove_and_attack(self):
        user_input = "I move up and attack the dummy"
        
        # 1. NLP Processing
        processed_input = self.nlp.process_player_input(user_input, self.known_entities)
        
        print(f"DEBUG: Actions found: {[a.intent.name for a in processed_input.actions]}")
        print(f"DEBUG: Targets found: {[t.name for t in processed_input.targets]}")
        
        # Verify NLP results
        intent_names = [a.intent.name for a in processed_input.actions]
        self.assertIn("MOVE", intent_names)
        self.assertIn("ATTACK", intent_names)
        self.assertIn(self.dummy, processed_input.targets)
        
        # 2. Game Logic Execution
        # We pass self.known_entities as game_entities
        results = self.controller.process_player_actions_logic(self.player, processed_input, self.known_entities)
        
        # 3. Verify Movement
        # Player started at (5, 7). Dummy at (5, 5).
        # Move logic moves 1 step towards target.
        # (5, 7) -> (5, 5) implies dy = -2. 
        # Should move to (5, 6).
        self.assertEqual(self.player.x, 5)
        self.assertEqual(self.player.y, 6)
        print(f"DEBUG: Player moved to ({self.player.x}, {self.player.y})")
        
        # 4. Verify Attack/Damage
        # Dummy started with 20 HP. Attack deals 5 damage.
        self.assertEqual(self.dummy.cur_hp, 15)
        print(f"DEBUG: Dummy HP: {self.dummy.cur_hp}")

if __name__ == "__main__":
    unittest.main()
