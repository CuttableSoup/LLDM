import sys
from pathlib import Path

# Add the project root to sys.path
project_root = Path(r"c:\Users\Administrator\Projects\LLDM")
sys.path.append(str(project_root))

from classes import RulesetLoader, Entity, Interaction, ValueSource

def verify_generic_yaml():
    print("--- Verifying generic.yaml loading ---")
    loader = RulesetLoader(project_root)
    
    # Load generic.yaml specifically
    generic_yaml_path = project_root / "generic.yaml"
    if not generic_yaml_path.exists():
        print(f"Error: {generic_yaml_path} not found.")
        return

    docs = loader._load_generic_yaml_all(generic_yaml_path)
    entity_data = docs[0]['entity']
    
    # Create entity using the new logic
    from classes import create_entity_from_dict
    print("Calling create_entity_from_dict...")
    entity = create_entity_from_dict(entity_data)
    print("create_entity_from_dict finished.")
    
    print(f"Entity Loaded: {entity.name}")
    print(f"Supertypes: {entity.supertype} -> {entity.type} -> {entity.subtype}")
    
    # Verify Interaction
    print(f"\n--- Verifying Interactions ({len(entity.interaction)}) ---")
    for inter in entity.interaction:
        print(f"Type: {inter.type}, Desc: {inter.description}")
        if inter.target_effect:
            eff = inter.target_effect[0]
            print(f"  Target Effect: {eff.name}")
            print(f"  Magnitude: Source={eff.magnitude.source}, Stat={eff.magnitude.stat}, Value={eff.magnitude.value}, Type={eff.magnitude.type}")
        
        if inter.user_requirement:
            req = inter.user_requirement[0]
            print(f"  User Requirement Type: {req.type}")
            if req.test:
                print(f"    Test: Source={req.test.source}, Stat={req.test.stat}")

    # Verify Ability
    print(f"\n--- Verifying Abilities ({len(entity.ability)}) ---")
    for ab in entity.ability:
        print(f"Type: {ab.type}")
        if ab.target_effect:
            eff = ab.target_effect[0]
            print(f"  Target Effect: {eff.name}")
            print(f"  Magnitude: Source={eff.magnitude.source}, Stat={eff.magnitude.stat}, Type={eff.magnitude.type}")

    # Verify Duration
    print(f"\n--- Verifying Durations ({len(entity.duration)}) ---")
    for dur in entity.duration:
        print(f"Frequency: {dur.frequency}")
        print(f"Length: Type={dur.length.type}, Value={dur.length.value}")

if __name__ == "__main__":
    verify_generic_yaml()
