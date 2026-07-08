"""!
@file NLP_Core.py
@brief Receives and processes player input using semantic similarity.
"""

import numpy as np
from sentence_transformers import SentenceTransformer, util

# Substring checks against processed input to decide "examine" vs "take" intent, before any
# skill matching runs. Phrases (not bare words) where a bare word would collide with an
# existing skill phrasing already in use -- ex: "pick" alone would misfire on "I pick the lock"
# (finesse), so "pick up" (the two-word phrase) is required instead.
EXAMINE_KEYWORDS = ("examine", "inspect", "look at", "check out")
TAKE_KEYWORDS = ("take ", "grab ", "pick up", "loot ")

# map_to_item checks these before any embedding match -- currency is a plain integer field
# (entity["currency"]), not an object-supertype entity with a name/description to embed.
CURRENCY_SYNONYMS = ("gold", "coin", "currency", "money")

# _detect_save_load_intent checks these ahead of everything else (item intent included --
# a slot name could otherwise contain a word like "take" and misfire the item intercept).
# Longest/most-specific prefix first in each tuple, since matching stops at the first hit and
# a shorter prefix (ex: "save ") would otherwise swallow "game as " into the slot name.
SAVE_PREFIXES = ("save game as ", "save as ", "save game ", "save ")
LOAD_PREFIXES = ("load game as ", "load as ", "load game ", "load ")

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
        self.item_embeddings = None
        self.item_indices = []
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

        save_load_intent, slot_name = self._detect_save_load_intent(processed)
        if save_load_intent:
            self.event_bus.publish(f"{save_load_intent}_requested", {"slot": slot_name})
            return

        intent = self._detect_item_intent(processed)
        if intent:
            item_name, item_score = self.map_to_item(processed)
            if item_name:
                self.event_bus.publish("item_interaction_detected", {
                    "intent": intent, "item_name": item_name, "input": processed, "score": item_score,
                })
                return
            # A recognized "examine"/"take" verb but no matching item name -- fall through to
            # skill matching below rather than silently dropping it (ex: could still be a
            # legitimate skill phrase that happens to contain one of these words).

        skill, score = self.map_to_action(processed)
        if skill:
            self.event_bus.publish("action_detected", {"skill": skill, "score": score, "input": processed})
        else:
            # Below confidence_threshold: publish this instead of staying silent, so the
            # player gets some response rather than the app appearing to stall.
            self.event_bus.publish("action_not_understood", {"input": processed, "score": score})

    def _detect_item_intent(self, processed_text):
        """!
        @brief Checks processed input for an "examine" or "take" verb, ahead of skill matching.
        @param processed_text The cleaned and processed player input.
        @return "examine", "take", or None.
        """
        if any(keyword in processed_text for keyword in EXAMINE_KEYWORDS):
            return "examine"
        if any(keyword in processed_text for keyword in TAKE_KEYWORDS):
            return "take"
        return None

    def _detect_save_load_intent(self, processed_text):
        """!
        @brief Checks processed input for a "save"/"load" command, ahead of item and skill
               matching -- a meta-command, not an in-fiction action, so it never should reach
               either. Unlike map_to_item's embedding match, the slot name is arbitrary
               player-chosen text with no catalog to match against, so it's extracted by
               prefix-stripping instead (same style as process_input's own "i want to " etc.
               prefix list).
        @param processed_text The cleaned and processed player input.
        @return (intent, slot_name) where intent is "save"/"load"/None. If a prefix matched
                but nothing followed it (empty slot name), returns (None, None) instead --
                falls through to normal skill matching rather than saving/loading to a blank
                name.
        """
        for prefix in SAVE_PREFIXES:
            if processed_text.startswith(prefix):
                slot_name = processed_text[len(prefix):].strip()
                return ("save", slot_name) if slot_name else (None, None)
        for prefix in LOAD_PREFIXES:
            if processed_text.startswith(prefix):
                slot_name = processed_text[len(prefix):].strip()
                return ("load", slot_name) if slot_name else (None, None)
        return None, None


    def _add_phrases(self, all_phrases, indices, key, data):
        """!
        @brief Appends name/description/keyword phrases for one skill or technique/spell
               entity to the shared phrase/index lists used to build a single embedding
               matrix -- multiple phrases per entry avoid "dilution" from averaging
               everything into one embedding.
        @param all_phrases The phrase list to append to.
        @param indices The parallel list of keys (one per phrase in all_phrases).
        @param key The skill or ability name these phrases resolve to on a match.
        @param data The skill/entity table, read for "description" and "keywords".
        """
        all_phrases.append(key)
        indices.append(key)

        description = data.get("description", "")
        if description:
            all_phrases.append(description)
            indices.append(key)
            all_phrases.append(f"{key} {description}")
            indices.append(key)

        for keyword in data.get("keywords", []):
            all_phrases.append(keyword)
            indices.append(key)
            all_phrases.append(f"{key} {keyword}")
            indices.append(key)

    def _on_rules_loaded(self, data):
        """!
        @brief Callback when rules are loaded from DMCore.
        @param data The rules data containing skills and entities.
        """
        self.skills_data = data.get("skills", {})
        self.skill_names = list(self.skills_data.keys())
        entities_data = data.get("entities", {})

        if not self.skill_names:
            self.event_bus.publish("log_warning", "NLPCore: No skills loaded.")
            return

        # Prepare a list of phrases for each skill to avoid "dilution"
        # Each skill will have multiple embeddings
        self.skill_map = [] # List of (skill_name, embedding)

        all_phrases = []
        skill_indices = []

        for name, skill in self.skills_data.items():
            self._add_phrases(all_phrases, skill_indices, name, skill)

        # Techniques/spells (ex: "cleave", "fireball") are matched in this same embedding
        # space, so a player naming one directly (ex: "I cleave through them") can resolve
        # to that exact ability instead of only the skill it happens to share with a plain
        # weapon -- see DMCore.resolve_named_ability, which gates this on the acting entity
        # actually owning that ability before treating the match as anything but a skill name.
        for name, entity in entities_data.items():
            if entity.get("supertype") not in ("technique", "spell"):
                continue
            self._add_phrases(all_phrases, skill_indices, name, entity)

        # Pre-calculate embeddings for all phrases
        all_embeddings = self.model.encode(all_phrases, convert_to_tensor=True)
        self.all_embeddings = all_embeddings
        self.skill_indices = skill_indices

        self.event_bus.publish("log_info", f"NLPCore: {len(all_phrases)} skill/ability phrases encoded for {len(self.skill_names)} skills.")

        # Also build item-name embeddings, for "examine"/"take" item-interaction matching
        # (map_to_item). Only "object" supertype entities are physical items a player could
        # plausibly examine or pick up -- this indexes every known item by name/description,
        # not just what's in the current scene; DMCore is what checks whether the matched item
        # is actually present in the current target's inventory.
        item_phrases = []
        item_indices = []
        for name, entity in entities_data.items():
            if entity.get("supertype") != "object":
                continue
            item_phrases.append(name)
            item_indices.append(name)
            description = entity.get("description", "")
            if description:
                item_phrases.append(description)
                item_indices.append(name)

        if item_phrases:
            self.item_embeddings = self.model.encode(item_phrases, convert_to_tensor=True)
            self.item_indices = item_indices
        else:
            self.item_embeddings = None
            self.item_indices = []

        self.event_bus.publish("log_info", f"NLPCore: {len(item_phrases)} item phrases encoded for {len(set(item_indices))} items.")

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

    def map_to_item(self, processed_text):
        """!
        @brief Maps the processed text to a specific item's name using semantic similarity,
               the same way map_to_action does for skills but against item_embeddings/item_indices.
               Currency is checked first as a fixed synonym list rather than semantically --
               it's a plain "currency" integer field on entities, not an object-supertype entity
               with a name/description of its own, so there's nothing to embed it against.
        @param processed_text The cleaned and processed player input.
        @return A tuple of (item_name, confidence_score); item_name is None below confidence_threshold.
        """
        if any(synonym in processed_text for synonym in CURRENCY_SYNONYMS):
            return "currency", 1.0

        if self.item_embeddings is None:
            return None, 0.0

        input_embedding = self.model.encode(processed_text, convert_to_tensor=True)
        cosine_scores = util.cos_sim(input_embedding, self.item_embeddings)[0]

        best_phrase_idx = np.argmax(cosine_scores.cpu().numpy())
        best_score = cosine_scores[best_phrase_idx].item()
        best_item = self.item_indices[best_phrase_idx]

        if best_score < self.confidence_threshold:
            return None, best_score

        self.event_bus.publish("log_info", f"Mapped input to item: {best_item} (Score: {best_score:.4f})")
        return best_item, best_score