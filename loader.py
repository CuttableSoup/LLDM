"""
loader.py

This module handles loading game data from YAML files and converting it into
Entity and Environment objects.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import fields

try:
    import yaml
except ImportError:
    yaml = None

from models import (
    Entity, Quality, Cost, DurationComponent, Magnitude, Effect, 
    Requirement, Interaction, Trigger, Attribute, InventoryItem,
    Scenario, Environment, Room, RoomLegendItem
)

logger = logging.getLogger("Loader")

class RulesetLoader:
    def __init__(self, ruleset_path: Path):
        if not yaml:
            raise ImportError("PyYAML is required to load rulesets.")
        self.ruleset_path = ruleset_path
        
        self.characters: Dict[str, Entity] = {} 
        self.entities_by_supertype: Dict[str, Dict[str, Entity]] = {}
        self.scenario: Optional[Scenario] = None
        self.attributes: List[Any] = []
        self.types: List[Any] = []
        
        logger.info(f"RulesetLoader initialized for path: {self.ruleset_path}")

    def load_all(self):
        """Loads all YAML files from the ruleset directory in two passes."""
        if not self.ruleset_path.is_dir():
            logger.error(f"Ruleset path not found: {self.ruleset_path}")
            return
        
        all_yaml_files = list(self.ruleset_path.glob("**/*.yaml"))
        schema_files_paths = set()

        # --- PASS 1: Load Schemas (Aptitudes, Types, Scenarios) ---
        for yaml_file in all_yaml_files:
            docs = self._load_generic_yaml_all(yaml_file)
            if not docs:
                continue
            
            is_schema = False
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                if 'aptitude' in doc:
                    self.attributes.append(doc)
                    is_schema = True
                elif 'category' in doc:
                    self.types.append(doc)
                    is_schema = True
                elif 'map' in doc:
                    self._load_scenario_from_data(doc, yaml_file.name)
                    is_schema = True
            
            if is_schema:
                schema_files_paths.add(yaml_file)

        # Dynamic initialization of supertypes found in schema
        dynamic_supertypes = {
            doc.get('category', {}).get('supertype') 
            for doc in self.types 
            if isinstance(doc.get('category'), dict) and doc.get('category', {}).get('supertype')
        }
        for st in dynamic_supertypes:
            self.entities_by_supertype[st] = {}

        # --- PASS 2: Load Entities ---
        for yaml_file in all_yaml_files:
            if yaml_file in schema_files_paths:
                continue
            
            for entity_data in self._load_generic_yaml_all(yaml_file):
                if isinstance(entity_data, dict) and 'entity' in entity_data:
                    data = entity_data['entity']
                    if 'name' not in data:
                        continue
                    
                    entity_obj = create_entity_from_dict(data)
                    
                    if data.get("is_player", False):
                        self.characters[entity_obj.name] = entity_obj
                    elif entity_obj.supertype and entity_obj.supertype in self.entities_by_supertype:
                        self.entities_by_supertype[entity_obj.supertype][entity_obj.name] = entity_obj
                    else:
                        logger.warning(f"Uncategorized entity '{entity_obj.name}' (Supertype: {entity_obj.supertype})")

    def _load_generic_yaml_all(self, file_path: Path) -> List[Any]:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return [doc for doc in yaml.safe_load_all(f) if doc]
        except Exception as e:
            logger.error(f"Error loading YAML {file_path}: {e}")
            return []

    def _load_scenario_from_data(self, data: Dict, file_name: str):
        try:
            map_data = data.get('map', {})
            if not map_data:
                return
            
            env_data = map_data.get('environment', {})
            parsed_rooms = []
            
            for room_data in env_data.get('rooms', []):
                parsed_legend = [
                    RoomLegendItem(**item) 
                    for item in room_data.get('legend', []) 
                    if isinstance(item, dict)
                ]
                room_data['legend'] = parsed_legend
                parsed_rooms.append(Room(**room_data))
            
            self.scenario = Scenario(
                scenario_name=map_data.get('name', 'Unnamed Scenario'),
                environment=Environment(rooms=parsed_rooms)
            )
        except Exception as e:
            logger.error(f"Error loading scenario from {file_name}: {e}")

    def get_character(self, name: str) -> Optional[Entity]:
        return self.characters.get(name)


# --- Helper Functions ---

def create_entity_from_dict(data: Dict[str, Any]) -> Entity:
    """Creates an Entity object from a dictionary, handling nested structures."""
    data_copy = data.copy()

    # Recursively create nested dataclass objects.
    if 'quality' in data_copy:
        data_copy['quality'] = Quality(**data_copy['quality'])
    if 'cost' in data_copy:
        data_copy['cost'] = Cost(**data_copy['cost'])
    if 'duration' in data_copy:
        data_copy['duration'] = DurationComponent(**data_copy['duration'])

    def _parse_effects(effect_list: List[Dict]) -> List[Effect]:
        parsed = []
        for eff in effect_list:
            if 'magnitude' in eff:
                if isinstance(eff['magnitude'], dict):
                    eff['magnitude'] = Magnitude(**eff['magnitude'])
                elif isinstance(eff['magnitude'], (int, float, str)):
                     # Fallback for simple values to default static magnitude
                     eff['magnitude'] = Magnitude(value=eff['magnitude'])
            
            if 'duration' in eff and isinstance(eff['duration'], dict):
                 dur = eff['duration']
                 if isinstance(dur.get('length'), dict):
                     dur['length'] = dur['length'].get('value', 0)
                 eff['duration'] = DurationComponent(**dur)
            
            # Separate parameters
            eff_args = {}
            parameters = {}
            known_fields = {'name', 'magnitude', 'duration', 'entity', 'apply', 'inventory'}
            
            for k, v in eff.items():
                if k in known_fields:
                    eff_args[k] = v
                else:
                    parameters[k] = v
            
            if parameters:
                eff_args['parameters'] = parameters
            
            parsed.append(Effect(**eff_args))
        return parsed

    def _parse_requirements(req_list: List[Dict]) -> List[Requirement]:
        if not req_list:
            return []
        parsed = []
        for req in req_list:
            if 'test' in req:
                req_obj = Requirement(type='test', test=req['test'])
                if 'difficulty' in req:
                     req_obj.difficulty = req['difficulty']
                parsed.append(req_obj)
            elif 'ally' in req:
                 req_obj = Requirement(type='ally', name=req['ally'].get('name'))
                 parsed.append(req_obj)
            elif 'name' in req:
                parsed.append(Requirement(type='name', name=req['name']))
            elif 'relation' in req:
                parsed.append(Requirement(type='relation', relation=req['relation']))
            elif 'or' in req:
                req_obj = Requirement(type='or')
                sub_reqs = req['or'] if isinstance(req['or'], list) else [{k:v} for k,v in req['or'].items()]
                req_obj.sub_requirements = _parse_requirements(sub_reqs)
                parsed.append(req_obj)
            elif 'not' in req:
                req_obj = Requirement(type='not')
                sub_reqs = req['not'] if isinstance(req['not'], list) else [{k:v} for k,v in req['not'].items()]
                req_obj.sub_requirements = _parse_requirements(sub_reqs)
                parsed.append(req_obj)
            else:
                 for k, v in req.items():
                     if k not in ['test', 'ally', 'name', 'relation', 'or', 'not']:
                         parsed.append(Requirement(type='property', name=k, relation=v))
        return parsed

    def _parse_interactions(inter_list: List[Dict]) -> List[Interaction]:
        parsed = []
        for item in inter_list:
            inter = Interaction(
                type=item.get('type', ''),
                description=item.get('description', ''),
                range=item.get('range', 0)
            )
            # Effects
            if 'target' in item and 'effect' in item['target']:
                inter.target_effect = _parse_effects(item['target']['effect'])
            if 'user' in item and 'effect' in item['user']:
                inter.user_effect = _parse_effects(item['user']['effect'])
            if 'self' in item and 'effect' in item['self']:
                inter.self_effect = _parse_effects(item['self']['effect'])
            # Requirements
            if 'target' in item and 'requirement' in item['target']:
                inter.target_requirement = _parse_requirements(item['target']['requirement'])
            if 'user' in item and 'requirement' in item['user']:
                inter.user_requirement = _parse_requirements(item['user']['requirement'])
            if 'self' in item and 'requirement' in item['self']:
                inter.self_requirement = _parse_requirements(item['self']['requirement'])
            parsed.append(inter)
        return parsed

    def _parse_triggers(trigger_list: List[Dict]) -> List[Trigger]:
        parsed = []
        for item in trigger_list:
            trig = Trigger(
                frequency=item.get('frequency', ''),
                length=item.get('length', '*'),
                timestamp=item.get('timestamp')
            )
            # Effects and Requirements logic is shared with Interaction
            if 'target' in item:
                if 'effect' in item['target']: trig.target_effect = _parse_effects(item['target']['effect'])
                if 'requirement' in item['target']: trig.target_requirement = _parse_requirements(item['target']['requirement'])
            if 'user' in item:
                if 'effect' in item['user']: trig.user_effect = _parse_effects(item['user']['effect'])
                if 'requirement' in item['user']: trig.user_requirement = _parse_requirements(item['user']['requirement'])
            if 'self' in item:
                if 'effect' in item['self']: trig.self_effect = _parse_effects(item['self']['effect'])
                if 'requirement' in item['self']: trig.self_requirement = _parse_requirements(item['self']['requirement'])
            parsed.append(trig)
        return parsed

    if 'interaction' in data_copy:
        data_copy['interaction'] = _parse_interactions(data_copy['interaction'])
    if 'ability' in data_copy:
        data_copy['ability'] = _parse_interactions(data_copy['ability'])
    if 'trigger' in data_copy:
        data_copy['trigger'] = _parse_triggers(data_copy['trigger'])

    # Attribute flattening logic
    final_attributes: Dict[str, Attribute] = {}
    if 'attribute' in data_copy:
        raw_attributes = data_copy['attribute']
        def process_attr(attr_map: Dict, path_prefix=""):
            for key, value in attr_map.items():
                if key == 'choice': continue
                current_path = f"{path_prefix}{key}"
                if isinstance(value, (int, float)):
                    final_attributes[current_path] = Attribute(base=value)
                elif isinstance(value, dict):
                    base_val = value.get('base', 0)
                    final_attributes[current_path] = Attribute(base=base_val)
                    if 'skill' in value and isinstance(value['skill'], dict):
                        process_attr(value['skill'], path_prefix=f"{current_path}.")
                    if 'specialization' in value and isinstance(value['specialization'], dict):
                         process_attr(value['specialization'], path_prefix=f"{current_path}.")
        if isinstance(raw_attributes, dict):
            process_attr(raw_attributes)
        data_copy['attribute'] = final_attributes

    # Inventory flattening logic
    def _create_inventory(items_list: List[Dict]) -> List[InventoryItem]:
        output = []
        for item_data in items_list:
            if 'item' in item_data:
                nested_inv_data = item_data.pop('inventory', [])
                nested_inv = _create_inventory(nested_inv_data)
                output.append(InventoryItem(**item_data, inventory=nested_inv))
        return output

    if 'inventory' in data_copy:
        all_inventory_entries = data_copy.get('inventory', [])
        item_entries = [entry for entry in all_inventory_entries if 'item' in entry]
        data_copy['inventory'] = _create_inventory(item_entries)
        rule_entries = [entry['requirement'] for entry in all_inventory_entries if 'requirement' in entry]
        data_copy['inventory_rules'] = rule_entries

    # Ensure Movement dictionaries exist
    if 'move' in data_copy: data_copy['move'] = data_copy.get('move', {})
    if 'passable' in data_copy: data_copy['passable'] = data_copy.get('passable', {})

    # Create Entity
    entity_field_names = {f.name for f in fields(Entity)}
    filtered_data = {k: v for k, v in data_copy.items() if k in entity_field_names}
    
    # Defaults for Cur/Max stats
    for stat in ['hp', 'mp', 'fp']:
        if f'max_{stat}' in filtered_data and f'cur_{stat}' not in filtered_data:
            filtered_data[f'cur_{stat}'] = filtered_data[f'max_{stat}']

    entity = Entity(**filtered_data)
    resolve_entity_references(entity)
    return entity

def resolve_entity_references(entity: Entity):
    """Recursively resolves 'reference(source:path)' strings in the entity's fields."""
    import re
    ref_pattern = re.compile(r"reference\(([^:]+):([^)]+)\)")

    def _resolve_single_ref(match, context_entity: Entity) -> Any:
        source, path = match.group(1), match.group(2)
        if source == 'self':
            current = context_entity
            try:
                for part in path.split('.'):
                    current = current.get(part) if isinstance(current, dict) else getattr(current, part)
                return current
            except (AttributeError, KeyError):
                logger.warning(f"Could not resolve reference '{match.group(0)}' in entity '{context_entity.name}'")
                return match.group(0)
        else:
             logger.warning(f"Unsupported reference source '{source}' in '{match.group(0)}'")
             return match.group(0)

    def _resolve_value(value: Any, context_entity: Entity) -> Any:
        if isinstance(value, str):
            match = ref_pattern.fullmatch(value.strip())
            if match: return _resolve_single_ref(match, context_entity)
            if "reference(" in value:
                 return ref_pattern.sub(lambda m: str(_resolve_single_ref(m, context_entity)), value)
            return value
        elif isinstance(value, list):
            return [_resolve_value(item, context_entity) for item in value]
        elif isinstance(value, dict):
            return {k: _resolve_value(v, context_entity) for k, v in value.items()}
        elif hasattr(value, '__dataclass_fields__'):
             for f in fields(value):
                 setattr(value, f.name, _resolve_value(getattr(value, f.name), context_entity))
             return value
        return value

    for f in fields(entity):
        setattr(entity, f.name, _resolve_value(getattr(entity, f.name), entity))