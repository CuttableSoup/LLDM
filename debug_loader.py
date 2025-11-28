from pathlib import Path
from loader import RulesetLoader

def test_loading():
    loader = RulesetLoader(Path("c:/Users/Administrator/Projects/LLDM/rulesets"))
    loader.load_all()
    
    # Check for the entity from generic.yaml
    # The name is dynamic: reference(self:parameter.name) -> flamebellow
    # So we look for 'flamebellow'
    
    entity = loader.get_character("flamebellow")
    if not entity:
        # Try finding it in supertypes if not a character
        for st, entities in loader.entities_by_supertype.items():
            if "flamebellow" in entities:
                entity = entities["flamebellow"]
                break
    
    if not entity:
        print("Entity 'flamebellow' not found.")
        return

    print(f"Entity: {entity.name}")
    print(f"Attributes keys: {list(entity.attribute.keys())}")
    
    # Check if 'arcane' (skill) or 'evocation' (specialization) are present
    print(f"Has 'arcane'? {'arcane' in entity.attribute}")
    print(f"Has 'evocation'? {'evocation' in entity.attribute}")

if __name__ == "__main__":
    test_loading()
