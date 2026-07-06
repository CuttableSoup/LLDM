"""!
@file NLP_Core.py
@brief Receives and processes player input using semantic similarity.
"""

import numpy as np
from sentence_transformers import SentenceTransformer, util

class NLPCore:
    """!
    @brief Main class handling the interpretation of natural language input.
    """

    def __init__(self, event_bus):
        """!
        @brief Initializes the NLP core and loads semantic models.
        @param event_bus The central event bus instance.
        """
        self.event_bus = event_bus
        self.model = SentenceTransformer('all-MiniLM-L6-v2')
        self.skills_data = {}
        self.skill_names = []
        self.skill_embeddings = None
        # Below this cosine-similarity score, treat the input as not matching any skill at
        # all rather than forcing it onto whatever phrase happened to score highest 
        self.confidence_threshold = 0.5
        
        # Subscribe to rules_loaded event to build embeddings
        self.event_bus.subscribe("rules_loaded", self._on_rules_loaded)
        # Subscribe to user input
        self.event_bus.subscribe("user_input_submitted", self._on_user_input)
        
        self.event_bus.publish("log_info", "NLPCore initialized with SentenceTransformer.")

    def _on_user_input(self, player_input):
        """!
        @brief Event handler for user input.
        """
        processed = self.process_input(player_input)
        skill, score = self.map_to_action(processed)
        if skill:
            self.event_bus.publish("action_detected", {"skill": skill, "score": score, "input": processed})
        else:
            # Below confidence_threshold: publish this instead of staying silent, so the
            # player gets some response rather than the app appearing to stall.
            self.event_bus.publish("action_not_understood", {"input": processed, "score": score})


    def _on_rules_loaded(self, data):
        """!
        @brief Callback when rules are loaded from DMCore.
        @param data The rules data containing skills.
        """
        self.skills_data = data.get("skills", {})
        self.skill_names = list(self.skills_data.keys())

        if not self.skill_names:
            self.event_bus.publish("log_warning", "NLPCore: No skills loaded.")
            return

        # Prepare a list of phrases for each skill to avoid "dilution"
        # Each skill will have multiple embeddings
        self.skill_map = [] # List of (skill_name, embedding)

        all_phrases = []
        skill_indices = []

        for name, skill in self.skills_data.items():
            # The name is the most important
            all_phrases.append(name)
            skill_indices.append(name)
            
            # The description provides broad context
            desc = skill.get("description", "")
            if desc:
                all_phrases.append(desc)
                skill_indices.append(name)
                all_phrases.append(f"{name} {desc}")
                skill_indices.append(name)
            
            # Keywords
            for k in skill.get("keywords", []):
                all_phrases.append(k)
                skill_indices.append(name)
                all_phrases.append(f"{name} {k}")
                skill_indices.append(name)

        # Pre-calculate embeddings for all phrases
        all_embeddings = self.model.encode(all_phrases, convert_to_tensor=True)
        self.all_embeddings = all_embeddings
        self.skill_indices = skill_indices

        self.event_bus.publish("log_info", f"NLPCore: {len(all_phrases)} skill phrases encoded for {len(self.skill_names)} skills.")

    def process_input(self, player_input):
        """!
        @brief Processes the raw player input.
        @param player_input The string provided by the player.
        @return The processed text data.
        """
        # Basic cleaning: stripping whitespace and converting to lowercase
        processed_text = player_input.strip().lower()

        # Remove common "I want to", "I try to" prefixes
        prefixes = ["i want to ", "i try to ", "i'll try to ", "i will try to ", "i am going to ", "i'm going to ", "i "]
        for prefix in prefixes:
            if processed_text.startswith(prefix):
                processed_text = processed_text[len(prefix):]
                break

        self.event_bus.publish("log_info", f"Processing player input: {player_input} -> {processed_text}")
        return processed_text

    def map_to_action(self, processed_text):
        """!
        @brief Maps the processed text to a specific skill or action using semantic similarity.
        @param processed_text The cleaned and processed player input.
        @return A tuple of (skill_name, confidence_score).
        """
        if self.all_embeddings is None:
            self.event_bus.publish("log_error", "NLPCore: Skill embeddings not initialized.")
            return None, 0.0

        # Encode the player input
        input_embedding = self.model.encode(processed_text, convert_to_tensor=True)

        # Compute cosine similarity between input and ALL phrases
        cosine_scores = util.cos_sim(input_embedding, self.all_embeddings)[0]

        # Find the best match among all phrases
        best_phrase_idx = np.argmax(cosine_scores.cpu().numpy())
        best_score = cosine_scores[best_phrase_idx].item()
        best_skill = self.skill_indices[best_phrase_idx]

        if best_score < self.confidence_threshold:
            self.event_bus.publish(
                "log_info",
                f"Best match was {best_skill} (Score: {best_score:.4f}), below confidence "
                f"threshold ({self.confidence_threshold}) - no skill triggered."
            )
            return None, best_score

        self.event_bus.publish("log_info", f"Mapped input to action: {best_skill} via best phrase (Score: {best_score:.4f})")
        return best_skill, best_score