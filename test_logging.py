
import sys
import os
from pathlib import Path
import logging

# Mock dependencies
class MockLoader:
    def __init__(self):
        self.characters = {}
        self.entities_by_supertype = {}
        self.scenario = None

class MockLLMManager:
    def generate_response(self, prompt, history):
        return "Mock response"

# Add project root to path
sys.path.append(os.getcwd())

try:
    from game_engine import GameController
    from models import Entity, GameTime
    
    # Setup
    loader = MockLoader()
    llm_manager = MockLLMManager()
    ruleset_path = Path("rulesets/medievalfantasy")
    
    controller = GameController(loader, ruleset_path, llm_manager)
    
    # Create a dummy player
    player = Entity(name="TestPlayer", cur_hp=10, max_hp=10, cur_mp=10, max_mp=10, cur_fp=10, max_fp=10)
    
    # Start game (should log session start and start message)
    print("Starting game...")
    controller.start_game(player)
    
    # Process input (should log input)
    print("Processing input...")
    controller.process_player_input("look around")
    
    # Check log file
    log_path = Path("game_log.txt")
    if log_path.exists():
        print(f"\nLog file created at {log_path.absolute()}")
        with open(log_path, "r") as f:
            content = f.read()
            print("\n--- Log Content ---")
            print(content)
            print("-------------------")
            
            if "Session Started" in content and "TestPlayer" in content and "> look around" in content:
                print("\nSUCCESS: Log contains expected content.")
            else:
                print("\nFAILURE: Log missing expected content.")
    else:
        print("\nFAILURE: Log file not created.")

except Exception as e:
    print(f"\nERROR: {e}")
    import traceback
    traceback.print_exc()
