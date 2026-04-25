import unittest
from unittest.mock import MagicMock, patch
from Event_Bus import EventBus
from NLP_Core import NLPCore
from DM_Core import DMCore
import os

class TestGameBoot(unittest.TestCase):
    def test_boot_and_skill_identification(self):
        # 1. Initialize Event Bus
        event_bus = EventBus()
        
        # 2. Track action_detected events
        detected_actions = []
        def on_action_detected(data):
            detected_actions.append(data)
        event_bus.subscribe("action_detected", on_action_detected)
        
        # 3. Initialize NLPCore FIRST so it doesn't miss rules_loaded
        nlp_core = NLPCore(event_bus)
        
        # 4. Initialize DMCore (this triggers rules_loaded)
        dm_core = DMCore(event_bus)
        
        # Verify that skills were actually loaded into nlp_core
        self.assertGreater(len(nlp_core.skill_names), 0, "No skills loaded into NLPCore")
        
        # 5. Simulate user input
        test_input = "I attack with my sword"
        event_bus.publish("user_input_submitted", test_input)
        
        # 6. Verify skill identification
        self.assertGreater(len(detected_actions), 0, "No action detected event published")
        last_action = detected_actions[-1]
        self.assertEqual(last_action["skill"], "blades")
        self.assertGreater(last_action["score"], 0.5)
        print(f"Integration Test Success: '{test_input}' -> {last_action['skill']} ({last_action['score']:.4f})")

if __name__ == "__main__":
    unittest.main()
