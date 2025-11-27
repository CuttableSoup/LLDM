from pathlib import Path
import logging
import sys

# Add the current directory to sys.path to ensure we can import classes
sys.path.append(str(Path(__file__).parent))

from classes import RulesetLoader

# Setup logging
logging.basicConfig(level=logging.INFO)

def test_loading():
    project_root = Path(__file__).parent
    ruleset_path = project_root / "rulesets" / "medievalfantasy"
    
    print(f"Loading rulesets from: {ruleset_path}")
    
    if not ruleset_path.exists():
        print(f"Error: Path {ruleset_path} does not exist.")
        return

    loader = RulesetLoader(ruleset_path)
    loader.load_all()
    
    print("\n--- Loaded Characters ---")
    for name, entity in loader.characters.items():
        print(f"- {name}")

    print("\n--- Loaded Entities by Supertype ---")
    for supertype, entities in loader.entities_by_supertype.items():
        print(f"Supertype: {supertype}")
        for name, entity in entities.items():
            print(f"  - {name} (HP: {entity.cur_hp}/{entity.max_hp})")

if __name__ == "__main__":
    test_loading()
