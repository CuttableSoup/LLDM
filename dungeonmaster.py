#!/usr/bin/env python3

"""
Dungeon Master Logic Core (dungeonmaster.py)

This file contains the core logic for parsing input and driving the
game, as described in the pseudocode. It is designed to be
imported and used by the GameController in GUI.py.
"""

import requests
import json
import textwrap
import spacy
from sentence_transformers import SentenceTransformer, util
from typing import List, Dict, Any, Optional

# --- Data Models (from classes.py) ---
# We import these for type hinting and data access
try:
    from classes import Entity
except ImportError:
    print("Warning: 'classes.py' not found. DM logic may fail.")
    # Define minimal placeholder
    class Entity:
        name: str = "Placeholder"
        skills: Dict = {}
        cur_hp: int = 1
        attitudes: Dict = {}

# --- Constants ---
# Assumes an OpenAI-compatible API endpoint for Gemma
LLM_URL = "http://127.0.0.1:1234/v1/chat/completions"
LLM_HEADERS = {"Content-Type": "application/json"}
MODEL_NAME = "google/gemma-3-12b" # As specified

# --- Triage Parser (Fast Pipeline) ---

class IntentParser:
    """
    Handles the fast "Triage" pipeline using semantic similarity
    and Named Entity Recognition (NER).
    """
    
    def __init__(self):
        print("Loading IntentParser models...")
        # 1. Load NER model
        try:
            self.ner_model = spacy.load("en_core_web_sm")
        except IOError:
            print("Spacy model 'en_core_web_sm' not found.")
            print("Please run: python -m spacy download en_core_web_sm")
            self.ner_model = None

        # 2. Load Semantic Similarity model
        try:
            self.semantic_model = SentenceTransformer('all-MiniLM-L6-v2')
        except Exception as e:
            print(f"Could not load SentenceTransformer: {e}")
            self.semantic_model = None
            
        # 3. Define canonical intents and pre-compute embeddings
        self.known_intents = {
            'inquire': "asking a question to get information",
            'persuade': "politely convincing someone with reason or charm",
            'intimidate': "threatening or frightening someone to force cooperation",
            'deceive': "lying to or misleading someone",
            'negotiate': "bartering or making a deal with someone",
            'physical_attack': "attacking a target with a melee or ranged weapon",
            'cast_spell': "casting a magical spell",
            'move_to_location': "moving to a different location, place, or position",
            'examine': "examining an object, person, or area in detail",
            'take_item': "picking up or taking an object",
            'give_item': "giving an item to another character",
            'equip_item': "equipping a weapon, armor, or other piece of gear",
            'use_item_on_object': "using an item on another object (e.g., key on a door)",
        }
        
        if self.semantic_model:
            self.intent_phrases = list(self.known_intents.values())
            self.intent_keys = list(self.known_intents.keys())
            self.intent_embeddings = self.semantic_model.encode(
                self.intent_phrases, 
                convert_to_tensor=True
            )
        print("IntentParser models loaded.")

    def run_fast_pipeline(self, text: str) -> Dict[str, Any]:
        """
        Runs the full Triage pipeline on raw user text.
        
        Returns:
            A dictionary with:
            - 'confidence': The semantic similarity score (0.0 to 1.0)
            - 'intent': The best-matched intent key (e.g., 'physical_attack')
            - 'target': The primary target found (e.g., 'goblin')
            - 'language': A simple sentiment/tone (placeholder)
        """
        if not self.semantic_model or not self.ner_model:
            print("Error: Parsing models not loaded. Returning empty pipeline result.")
            return {'confidence': 0.0, 'intent': None, 'target': None, 'language': None}

        # 1. Classify Intent
        dialogue_embedding = self.semantic_model.encode(text, convert_to_tensor=True)
        cosine_scores = util.cos_sim(dialogue_embedding, self.intent_embeddings)
        best_match_index = cosine_scores.argmax()
        
        confidence = cosine_scores[0][best_match_index].item()
        intent = self.intent_keys[best_match_index]
        
        # 2. Extract Entities
        doc = self.ner_model(text)
        target = None
        # Find the first relevant entity (Person, Org, Location, Product)
        for ent in doc.ents:
            if ent.label_ in ("PERSON", "ORG", "GPE", "LOC", "PRODUCT"):
                target = ent.text
                break
        
        # 3. Analyze Sentiment (Simple Placeholder)
        # A real implementation could use a dedicated sentiment model.
        language = "neutral"
        if "!" in text or intent == "intimidate":
            language = "aggressive"
        if "?" in text:
            language = "inquisitive"

        return {
            'confidence': confidence,
            'intent': intent,
            'target': target,
            'language': language
        }


# --- LLM Interface (Fallback Parser & NPC Actions) ---

class LLMInterface:
    """
    Handles all calls to the local LLM at http://127.0.0.1:1234.
    """
    
    def __init__(self, url: str = LLM_URL):
        self.url = url
        self.headers = LLM_HEADERS

    def _make_llm_call(self, messages: List[Dict], tools: Optional[List] = None) -> Optional[Dict]:
        """Helper function to make a raw request to the LLM."""
        payload = {
            "model": MODEL_NAME,
            "messages": messages,
            "temperature": 0.7,
        }
        
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
            
        try:
            response = requests.post(
                self.url, 
                headers=self.headers, 
                json=payload, 
                timeout=30
            )
            response.raise_for_status() # Raise an error for bad status codes
            return response.json()
        
        except requests.exceptions.RequestException as e:
            print(f"LLM API Error: {e}")
            return None

    def run_llm_parser(self, text: str, skills_list: List[str]) -> Dict[str, Any]:
        """
        The "Fallback Librarian" (Pattern-Constrained) parser.
        Maps ambiguous text to a list of known skills.
        """
        print(f"Running LLM fallback parser for: '{text}'")
        
        # Create the JSON schema for the constrained output
        json_schema = {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "The best-fit skill from the provided list",
                    "enum": skills_list
                },
                "target": {
                    "type": "string",
                    "description": "The primary target of the action."
                }
            },
            "required": ["action", "target"]
        }
        
        prompt = f"""
        You are a game logic parser. The user said: "{text}".
        
        Your task is to map this user's narrative action to the closest
        matching skill from the following list: {skills_list}.
        
        Respond with *only* a JSON object that follows this schema:
        {json.dumps(json_schema, indent=2)}
        """
        
        messages = [{"role": "user", "content": prompt}]
        
        # NOTE: This assumes your Gemma endpoint supports JSON Mode / constrained output.
        # If it doesn't, this prompt will *ask* for JSON. We will try to parse
        # it from the response.
        
        response_json = self._make_llm_call(messages)
        
        if not response_json:
            return {'intent': None, 'target': None, 'language': 'neutral'}

        try:
            content = response_json["choices"][0]["message"]["content"]
            # The LLM will often wrap the JSON in ```json ... ```
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            
            parsed_json = json.loads(content)
            
            return {
                'intent': parsed_json.get('action'),
                'target': parsed_json.get('target'),
                'language': 'narrative' # Mark as coming from a narrative prompt
            }
        except (Exception, json.JSONDecodeError) as e:
            print(f"LLM Fallback Error: Could not parse response. {e}")
            print(f"Raw response: {response_json}")
            return {'intent': None, 'target': None, 'language': 'neutral'}

    def get_npc_action(self, npc: Entity, game_state: Dict[str, Any], tools: List[Dict]) -> Dict[str, Any]:
        """
        Generates an NPC's action using the Function Calling prompt.
        
        Args:
            npc: The Entity object for the NPC.
            game_state: A dictionary containing contextual info.
            tools: The list of function definitions for the LLM.
        """
        # This prompt is adapted from the one in your GUI.py
        prompt_template = textwrap.dedent("""
        You are an AI Game Master controlling an NPC named {actor_name}.
        Your task is to determine the NPC's next action, generate their dialogue or a 
        description of the action IN THIRD PERSON, AND select the appropriate 
        function to call if a mechanical action is taken.

        - Actors Present: {actors_present}
        - Objects Present: {objects_present}
        - Current Attitudes: {attitudes}
        - Current Mood/Personality: {personality}
        - Character Skills: {skills}
        - Recent Game History: {game_history}
        
        Your task is to generate a narrative: Write a short line of dialogue or a 1-2 
        sentence description of the action from the NPC's perspective.
        
        If your action can be represented by a mechanical tool, call the tool.
        Otherwise, just provide the narrative content.
        NOTE: It is better to call no tool than to call one without reason.
        """).strip()
        
        prompt = prompt_template.format(
            actor_name=npc.name,
            actors_present=game_state.get('actors_present', 'none'),
            objects_present=game_state.get('objects_present', 'none'),
            attitudes=game_state.get('attitudes', 'none'),
            personality=game_state.get('personality', 'none'),
            skills=list(npc.skills.keys()) if npc.skills else 'none',
            game_history=game_state.get('game_history', 'none')
        )
        
        messages = [{"role": "user", "content": prompt}]
        response_json = self._make_llm_call(messages, tools=tools)
        
        if not response_json:
            return {"narrative": f"{npc.name} seems confused and does nothing.", "mechanical_data": None}

        message = response_json.get("choices", [{}])[0].get("message", {})
        
        narrative_output = message.get("content", "").strip()
        tool_call_data = None

        if message.get("tool_calls"):
            tool_call = message['tool_calls'][0]['function']
            # Ensure arguments are loaded as JSON, not just a string
            try:
                arguments = json.loads(tool_call['arguments'])
            except json.JSONDecodeError:
                print(f"Warning: Tool call arguments from LLM were not valid JSON: {tool_call['arguments']}")
                arguments = {}
                
            tool_call_data = {
                'name': tool_call['name'],
                'arguments': arguments
            }
        
        return {"narrative": narrative_output, "mechanical_data": tool_call_data}

    def get_narrative_summary(self, history: str) -> str:
        """
        Generates a narrative summary of a round's actions.
        """
        print("Generating narrative summary...")
        
        prompt = textwrap.dedent(f"""
        You are a narrative AI. The following events just occurred in a game:
        
        {history}
        
        Please write a single, engaging narrative paragraph that summarizes these events.
        Do not add any commentary. Just write the summary.
        """).strip()
        
        messages = [{"role": "user", "content": prompt}]
        response_json = self._make_llm_call(messages)
        
        if not response_json:
            return "The world spins on, the events unrecorded..."

        narrative = response_json.get("choices", [{}])[0].get("message", {}).get("content", "")
        return narrative.strip()


# --- Game Logic Functions ---

def process_interaction(actor: Entity, action: str, target: Optional[Entity]):
    """
    (Pseudo-method) Enacts the game mechanics for an action.
    This is where you would roll dice, change HP, move items, etc.
    """
    if not target:
        print(f"MECHANICS: {actor.name} performs '{action}' on... no one.")
        return
        
    print(f"MECHANICS: {actor.name} performs '{action}' on '{target.name}'")
    
    # --- Example Logic ---
    if action == 'physical_attack':
        # This is where you would have your dice roll logic
        # e.g., actor_roll = d20 + actor.skills.blades.base
        # e.g., target_roll = d20 + target.skills.dodge.base
        damage = 5 # Placeholder
        target.cur_hp -= damage
        print(f"          > {target.name} takes {damage} damage! New HP: {target.cur_hp}")
        
    elif action == 'give_item':
        # This would require an 'item' parameter as well
        # e.g., item = actor.inventory.pop('item_name')
        # e.g., target.inventory.append(item)
        print(f"          > {actor.name} gives an item to {target.name}.")
        
    else:
        print(f"          > No mechanical rule found for '{action}'.")


def process_attitudes(actor: Entity, target: Optional[Entity], action: str, language: str):
    """
    (Pseudo-method) Updates NPC attitudes based on interaction.
    This is where you would modify the 5 emotional axes.
    """
    if not target or target == actor:
        return # Can't change your own attitude
        
    print(f"ATTITUDES: '{target.name}' reacts to {actor.name}'s {language} '{action}'")
    
    # This is a stub. The real implementation would:
    # 1. Find the attitude entry for 'actor' in 'target.attitudes'
    # 2. Parse the 5-value string (e.g., "0,0,0,0,0")
    # 3. Modify the values based on the 'action' and 'language'
    #    - e.g., if action=='intimidate': confidence -= 0.4, disposition -= 0.2
    #    - e.g., if action=='persuade': trust += 0.3, disposition += 0.1
    # 4. Save the new string back to 'target.attitudes'
    
    print(f"          > Attitude of {target.name} towards {actor.name} would be updated here.")