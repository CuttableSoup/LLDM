import sys
from pathlib import Path
import yaml

# Add the project root to sys.path
project_root = Path(r"c:\Users\Administrator\Projects\LLDM")
sys.path.append(str(project_root))

from classes import create_entity_from_dict, Entity

def validate_all_entities():
    print("--- Validating All Entities ---")
    
    all_yaml_files = list(project_root.glob("**/*.yaml"))
    
    passed = 0
    failed = 0
    
    for yaml_file in all_yaml_files:
        try:
            with open(yaml_file, 'r', encoding='utf-8') as f:
                docs = list(yaml.safe_load_all(f))
        except Exception as e:
            print(f"[FAIL] {yaml_file.name}: Error loading YAML - {e}")
            failed += 1
            continue
            
        for i, doc in enumerate(docs):
            if not isinstance(doc, dict): continue
            
            if 'entity' in doc:
                entity_data = doc['entity']
                name = entity_data.get('name', f"Unnamed in {yaml_file.name}")
                
                try:
                    entity = create_entity_from_dict(entity_data)
                    # Basic check: did we get an Entity?
                    if isinstance(entity, Entity):
                        # Check specific fields that changed
                        if 'ally' in entity_data and isinstance(entity_data['ally'], dict):
                             # This should have failed type check if strict, but let's check the object
                             if not isinstance(entity.ally, list):
                                 print(f"[FAIL] {yaml_file.name} ({name}): 'ally' is {type(entity.ally)}, expected List")
                                 failed += 1
                                 continue
                        
                        # Check interaction
                        if 'interaction' in entity_data and not entity.interaction:
                             print(f"[WARN] {yaml_file.name} ({name}): Has 'interaction' data but parsed list is empty (parsing failed?)")
                        
                        # Check for old 'apply' usage
                        if 'apply' in entity_data:
                             print(f"[FAIL] {yaml_file.name} ({name}): Uses deprecated 'apply' field")
                             failed += 1
                             continue

                        # print(f"[PASS] {yaml_file.name} ({name})")
                        passed += 1
                    else:
                        print(f"[FAIL] {yaml_file.name} ({name}): create_entity_from_dict returned {type(entity)}")
                        failed += 1
                except Exception as e:
                    print(f"[FAIL] {yaml_file.name} ({name}): Exception - {e}")
                    failed += 1

    print(f"\nValidation Complete. Passed: {passed}, Failed: {failed}")

if __name__ == "__main__":
    validate_all_entities()
