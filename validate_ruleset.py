"""
This script validates a ruleset for the LLDM application.

It checks for consistency and correctness in the YAML data files, ensuring that:
- All entities have valid types, attributes, and skills as defined in the schemas.
- All entities referenced in room legends exist.
- There are no duplicate entity names.
"""
import sys
from pathlib import Path
from typing import Dict, Any, List

try:
    import yaml
except ImportError:
    print("Error: PyYAML library not found.", file=sys.stderr)
    print("Please install it: pip install PyYAML", file=sys.stderr)
    sys.exit(1)

# Define the default path to the ruleset to be validated.
DEFAULT_RULESET_PATH = Path(__file__).parent / "rulesets" / "medievalfantasy"

# A set of schema file names that should not be treated as entity files.
SCHEMA_FILES = {"types.yaml", "attributes.yaml", "rooms.yaml"}

def load_yaml_docs(filepath: Path) -> List[Any]:
    """
    Loads all YAML documents from a file.

    Args:
        filepath: The path to the YAML file.

    Returns:
        A list of YAML documents.
    """
    if not filepath.exists():
        print(f"Warning: File not found, skipping: {filepath.name}", file=sys.stderr)
        return []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            docs = list(yaml.safe_load_all(f))
            return [doc for doc in docs if doc] # Filter out empty documents
    except Exception as e:
        print(f"Error parsing YAML file {filepath.name}: {e}", file=sys.stderr)
        return []

def load_types_schema(filepath: Path) -> Dict[str, Any]:
    """
    Loads the types schema from the types.yaml file.

    Args:
        filepath: The path to the types.yaml file.

    Returns:
        A dictionary representing the types schema.
    """
    schema = {}
    docs = load_yaml_docs(filepath)
    for doc in docs:
        if 'category' in doc:
            cat_data = doc['category']
            supertype = cat_data.get('supertype')
            if not supertype:
                continue
            
            schema[supertype] = {"types": {}}
            
            for type_name, type_data in cat_data.get('types', {}).items():
                schema[supertype]["types"][type_name] = {"subtypes": {}}
                if type_data and 'subtypes' in type_data:
                    for subtype_name in type_data['subtypes']:
                        schema[supertype]["types"][type_name]["subtypes"][subtype_name] = {}
    return schema

def load_attributes_schema(filepath: Path) -> Dict[str, Any]:
    """
    Loads the attributes schema from the attributes.yaml file.

    Args:
        filepath: The path to the attributes.yaml file.

    Returns:
        A dictionary representing the attributes schema.
    """
    schema = {}
    docs = load_yaml_docs(filepath)
    for doc in docs:
        if 'aptitude' in doc:
            aptitude_block = doc.get('aptitude', {})
            if not isinstance(aptitude_block, dict):
                continue

            # The attribute (e.g., 'physique') is now the key in the aptitude block
            for attr_name, attr_data in aptitude_block.items():
                if not isinstance(attr_data, dict):
                    continue
                
                schema[attr_name] = {"skills": {}}
                
                # The skills (e.g., 'blade') are the keys within the attribute block
                for skill_name, skill_data in attr_data.items():
                    # Filter out non-skill keys like 'description', 'opposes'
                    if not isinstance(skill_data, dict):
                        continue

                    schema[attr_name]["skills"][skill_name] = {"specialization": {}}
                    
                    # The specializations (e.g., 'longsword') are the keys within the skill block
                    if skill_data:
                        for spec_name, spec_data in skill_data.items():
                            # Filter out non-spec keys like 'description', 'opposes'
                            if not isinstance(spec_data, dict):
                                continue
                            
                            # Only add if it's a specialization (represented as a dict)
                            schema[attr_name]["skills"][skill_name]["specialization"][spec_name] = {}
                            
    return schema

def load_all_entities(ruleset_path: Path) -> Dict[str, Any]:
    """
    Loads all entities from the YAML files in the ruleset directory.

    Args:
        ruleset_path: The path to the ruleset directory.

    Returns:
        A dictionary of all entities, keyed by entity name.
    """
    entities = {}
    for yaml_file in ruleset_path.glob("**/*.yaml"):
        if yaml_file.name in SCHEMA_FILES:
            continue

        docs = load_yaml_docs(yaml_file)
        for doc in docs:
            if 'entity' in doc:
                entity_data = doc['entity']
                name = entity_data.get('name')
                
                if not name:
                    print(f"Warning: Found entity without a name in {yaml_file.name}", file=sys.stderr)
                    continue
                    
                if name in entities:
                    print(f"Warning: Duplicate entity name '{name}' found in {yaml_file.name}. "
                        f"Original was in {entities[name]['file']}", file=sys.stderr)
                
                entities[name] = {"data": entity_data, "file": yaml_file.name}
                
    return entities

def _validate_attr_skill_block(block: Dict, attr_schema: Dict, e_name: str, f_name: str, context: str) -> List[str]:
    """Helper function to validate an attribute/skill block (with dot.notation) within an entity."""
    errors = []
    if not isinstance(block, dict):
        return errors
        
    for key in block.keys():
        parts = key.split('.')
        
        # 1. Validate Attribute
        attr_name = parts[0]
        if attr_name not in attr_schema:
            errors.append(f"[{f_name}] Entity '{e_name}': Invalid attribute '{attr_name}' from key '{key}' in '{context}'")
            continue
        
        valid_skills = attr_schema[attr_name].get('skills', {})
        
        # 2. Validate Skill (if present)
        if len(parts) > 1:
            skill_name = parts[1]
            if skill_name not in valid_skills:
                errors.append(f"[{f_name}] Entity '{e_name}': Invalid skill '{skill_name}' from key '{key}' for attribute '{attr_name}' in '{context}'")
                continue
            
            valid_specs = valid_skills.get(skill_name, {}).get('specialization', {})
            
            # 3. Validate Specialization (if present)
            if len(parts) > 2:
                spec_name = parts[2]
                if spec_name not in valid_specs:
                    errors.append(f"[{f_name}] Entity '{e_name}': Invalid specialization '{spec_name}' from key '{key}' for skill '{skill_name}' in '{context}'")
                    continue

            # 4. Check for extra parts (e.g., physique.blade.longsword.extra)
            if len(parts) > 3:
                errors.append(f"[{f_name}] Entity '{e_name}': Key '{key}' in '{context}' has too many parts (max 3 allowed: attr.skill.spec).")

    return errors

def validate_entities(all_entities: Dict, types_schema: Dict, attr_schema: Dict) -> List[str]:
    """
    Validates all loaded entities against the type and attribute schemas.

    Args:
        all_entities: A dictionary of all entities.
        types_schema: The types schema.
        attr_schema: The attributes schema.

    Returns:
        A list of error messages.
    """
    errors = []
    
    for name, entity_info in all_entities.items():
        data = entity_info['data']
        filename = entity_info['file']
        
        # Validate supertype, type, and subtype.
        supertype = data.get('supertype')
        type_ = data.get('type')
        subtype = data.get('subtype')
        
        if supertype and supertype not in types_schema:
            errors.append(f"[{filename}] Entity '{name}': Invalid supertype '{supertype}'")
            continue
            
        type_node = types_schema.get(supertype, {}).get('types', {})
        if type_ and type_ not in type_node:
            errors.append(f"[{filename}] Entity '{name}': Invalid type '{type_}' for supertype '{supertype}'")
            continue
        
        subtype_node = type_node.get(type_, {}).get('subtypes', {})
        if subtype and subtype not in subtype_node:
            errors.append(f"[{filename}] Entity '{name}': Invalid subtype '{subtype}' for type '{type_}'")

        # Validate attribute and skill blocks.
        if 'attribute' in data:
            errors.extend(_validate_attr_skill_block(
                data['attribute'], attr_schema, name, filename, "attribute"
            ))
            
        if 'requirement' in data:
            errors.extend(_validate_attr_skill_block(
                data['requirement'], attr_schema, name, filename, "requirement"
            ))
            
        if 'status' in data:
            if isinstance(data['status'], dict) and 'attribute' in data['status']:
                errors.extend(_validate_attr_skill_block(
                    data['status']['attribute'], attr_schema, name, filename, "status.attribute"
                ))

    return errors

def validate_rooms(filepath: Path, all_entities: Dict) -> List[str]:
    """
    Validates the rooms file, ensuring all legend entities exist.

    Args:
        filepath: The path to the rooms.yaml file.
        all_entities: A dictionary of all entities.

    Returns:
        A list of error messages.
    """
    errors = []
    docs = load_yaml_docs(filepath)
    if not docs:
        return errors
        
    map_data = docs[0].get('map', {})
    env_data = map_data.get('environment', {})
    rooms = env_data.get('rooms', [])
    
    for room in rooms:
        room_name = room.get('name', 'Unnamed Room')
        legend = room.get('legend', [])
        for item in legend:
            entity_name = item.get('entity')
            if entity_name and entity_name not in all_entities:
                errors.append(f"[{filepath.name}] Room '{room_name}': "
                    f"Legend entity '{entity_name}' not found in any loaded entity files.")
    return errors

def main():
    """The main function for the validation script."""
    print(f"Starting validation for ruleset: {DEFAULT_RULESET_PATH}\n")
    
    all_errors = []
    
    # Define paths to the schema files.
    types_schema_path = DEFAULT_RULESET_PATH / "types.yaml"
    attr_schema_path = DEFAULT_RULESET_PATH / "attributes.yaml"
    rooms_path = DEFAULT_RULESET_PATH / "rooms.yaml"
    
    # Load the schemas.
    types_schema = load_types_schema(types_schema_path)
    if not types_schema:
        print(f"Error: Could not load types schema from {types_schema_path.name}", file=sys.stderr)
        sys.exit(1)
        
    attr_schema = load_attributes_schema(attr_schema_path)
    if not attr_schema:
        print(f"Error: Could not load attributes schema from {attr_schema_path.name}", file=sys.stderr)
        sys.exit(1)

    print(f"Successfully loaded {len(types_schema)} supertypes from types.yaml.")
    print(f"Successfully loaded {len(attr_schema)} attributes from attributes.yaml.")
    
    # Load all entities.
    all_entities = load_all_entities(DEFAULT_RULESET_PATH)
    if not all_entities:
        print("Error: No entities were loaded. Check paths and file contents.", file=sys.stderr)
        sys.exit(1)
        
    print(f"Successfully loaded {len(all_entities)} total entities.\n")
    print("--- Running Validation ---")
    
    # Run the validation checks.
    all_errors.extend(validate_entities(all_entities, types_schema, attr_schema))
    all_errors.extend(validate_rooms(rooms_path, all_entities))
    
    # Print the results.
    if not all_errors:
        print("\n--- Validation Complete ---")
        print("✅ All files are valid. No errors found.")
    else:
        print(f"\n--- Validation Complete ---")
        print(f"❌ Found {len(all_errors)} error(s):\n")
        for error in all_errors:
            print(f"  - {error}")
        sys.exit(1)

if __name__ == "__main__":
    main()