"""!
@file NLP_Core.py
@brief EventBus glue over Intent_Classification.py's IntentClassifier -- receives player input,
    delegates classification, and publishes whatever events come back. Owns no classification
    logic itself: SentenceTransformerMatcher (below) is the production IntentMatcher adapter,
    holding the loaded SentenceTransformer model and its precomputed skill/item/target
    embeddings, plus a separate fine-tuned transformer pipeline for dialogue tone (see
    classify_sentiment); NLPCore only ever subscribes to events and publishes the classifier's
    own results, the same "pure module + thin glue" split AdHoc_Generation.py/
    DM_Improvisation.py already established.
"""

import re

import numpy as np
import torch
from sentence_transformers import SentenceTransformer, util
from transformers import pipeline

from Intent_Classification import CURRENCY_SYNONYMS, IntentClassifier, IntentMatcher

# classify_sentiment's own model -- a general-purpose 3-class (negative/neutral/positive)
# sentiment classifier fine-tuned on tweets, not this class's own semantic-similarity embedding
# model. Sentiment-of-an-utterance needs broad, compositional coverage across however a player
# might phrase something (ex: "get out of my sight" -- clearly hostile, but with no single word
# a lexicon-based analyzer like VADER would flag), which only a model actually trained on
# labeled sentiment examples provides; tweets are a much closer register match for terse,
# informal, second-person dialogue than the movie-review data most "default" sentiment models
# (ex: distilbert-sst2) are trained on. Named here, not inline in __init__, so it's easy to find/
# swap without hunting through the constructor.
SENTIMENT_MODEL_NAME = "cardiffnlp/twitter-roberta-base-sentiment-latest"

# map_to_action's alternate-phrasing candidates: markers that introduce a topic clause rather
# than describing the action itself (ex: "...about the road" in "have you heard anything about
# the road"). Truncating at the first one found gives a second, less-diluted candidate to score
# against the same skill-phrase bank -- see the confidence-threshold dilution gotcha below.
# Mirrors Intent_Classification.process_input's own prefix-stripping convention rather than a
# full parse.
TOPIC_CLAUSE_MARKERS = (" about ", " regarding ", " concerning ", " if ", " whether ", " that ")
CLAUSE_SEPARATORS = ("--", "?", ",", ";", ":")


class SentenceTransformerMatcher(IntentMatcher):
    """!
    @brief The production IntentMatcher adapter -- owns the loaded SentenceTransformer model
        and every precomputed skill/item/target embedding tensor, and is the one place in this
        file that still publishes "log_info"/"log_error" (the granular "mapped input to X via Y
        (score: Z)" diagnostics), since encoding/scoring is where those facts actually become
        known. IntentClassifier itself never touches the EventBus -- see that module's own
        docstring.
    """

    def __init__(self, event_bus):
        self.event_bus = event_bus
        self.model = SentenceTransformer('all-MiniLM-L6-v2')
        self.skills_data = {}
        self.skill_names = []
        self.skill_indices = []
        self.all_embeddings = None
        self.item_embeddings = None
        self.item_indices = []
        self.target_embeddings = None
        self.target_indices = []
        # Below this cosine-similarity score, treat the input as not matching any skill at
        # all rather than forcing it onto whatever phrase happened to score highest
        self.confidence_threshold = 0.5
        # A literal keyword hit (see _match_by_keyword) is independent evidence from the
        # semantic score, so it's allowed to rescue a match that misses confidence_threshold
        # on every phrasing tried -- but the matched skill's own best embedding score still has
        # to clear this much lower floor, so a coincidental keyword collision on an otherwise
        # unrelated sentence doesn't get accepted on keyword evidence alone.
        self.keyword_fallback_floor = 0.2
        # A separate pipeline, not self.model -- SENTIMENT_MODEL_NAME is a fine-tuned
        # classification head (3-way softmax over negative/neutral/positive), not an embedding
        # model, so there's no shared weights or shared encode() call to reuse here. Local, CPU
        # inference (no device= override) -- consistent with everything else in this class,
        # never a network call.
        self.sentiment_pipeline = pipeline(
            "sentiment-analysis", model=SENTIMENT_MODEL_NAME, tokenizer=SENTIMENT_MODEL_NAME,
        )
        # The winning class's own softmax probability has to clear this before classify_sentiment
        # commits to a label -- for a 3-way split, chance alone sits around 0.33, so this is
        # "meaningfully more confident than chance," not an arbitrary tone-strength cutoff the
        # way VADER's old compound-score threshold was.
        self.sentiment_confidence_threshold = 0.5

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

    def on_rules_loaded(self, data):
        """!
        @brief Builds skill/item/target embeddings from a fresh "rules_loaded" payload.
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
        self.all_embeddings = self.model.encode(all_phrases, convert_to_tensor=True)
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

        # Also build target-name embeddings, for explicit targeting (map_to_target) -- the same
        # pattern as item_phrases above, just over two kinds of entity: every non-player
        # "creature" (for combat retargeting), plus every entity carrying its own
        # [entity.test] regardless of supertype (ex: items.toml's "cursed dagger", for an
        # item-level skill check like an arcane curse-detection attempt -- see
        # DMCore._resolve_item_test_target). A test-bearing entity opts into being targetable
        # this way purely by having the data; nothing else about it changes. Global catalog,
        # not scene-filtered -- DMCore is what checks the matched name is actually reachable
        # (a live, hostile, in-scene creature for combat; a reachable, testable item otherwise)
        # before honoring it.
        target_phrases = []
        target_indices = []
        for name, entity in entities_data.items():
            if entity.get("is_player"):
                continue
            if entity.get("supertype") != "creature" and not entity.get("test"):
                continue
            target_phrases.append(name)
            target_indices.append(name)
            description = entity.get("description", "")
            if description:
                target_phrases.append(description)
                target_indices.append(name)

        if target_phrases:
            self.target_embeddings = self.model.encode(target_phrases, convert_to_tensor=True)
            self.target_indices = target_indices
        else:
            self.target_embeddings = None
            self.target_indices = []

        self.event_bus.publish("log_info", f"NLPCore: {len(target_phrases)} target phrases encoded for {len(set(target_indices))} targetable entities.")

    def register_item(self, name, description):
        """!
        @brief Incrementally registers one newly-created (or reload-restored) ad hoc entity
            into item_embeddings/item_indices, the same two-phrase-per-item ([name],
            [description]) shape on_rules_loaded already builds for the whole static catalog.
            Without this, an ad hoc entity (DM_Improvisation.py, AdHoc_Generation.py) would
            only ever be reachable on the one turn it's created (dispatched directly by name)
            -- any later reference (ex: "drop the stone") would miss map_to_item again, since
            these embeddings are otherwise only ever (re)built once, from "rules_loaded" (a
            reload doesn't republish that event -- DM_Persistence.py's load_game publishes
            "item_catalog_updated" instead, once, as a batch, after restoring every saved ad
            hoc entity).
        @param name The entity's own dict key/entity_id.
        @param description The entity's own "description" field, if any.
        """
        if not name:
            return

        new_phrases = [name]
        new_indices = [name]
        if description:
            new_phrases.append(description)
            new_indices.append(name)

        new_embeddings = self.model.encode(new_phrases, convert_to_tensor=True)
        if self.item_embeddings is None:
            self.item_embeddings = new_embeddings
        else:
            self.item_embeddings = torch.cat([self.item_embeddings, new_embeddings], dim=0)
        self.item_indices.extend(new_indices)

        self.event_bus.publish("log_info", f"NLPCore: {len(new_phrases)} item phrases registered for 1 ad hoc item(s).")

    def _generate_match_candidates(self, processed_text):
        """!
        @brief Builds alternate, less-diluted phrasings of the player's input to re-score
            against the same skill-phrase bank as the full sentence. A long sentence with a
            trailing topic clause (ex: "...about the road") or a leading aside (ex: "I'm
            sorry -- ...") pools toward that clause's own semantics in the whole-sentence
            embedding and away from the terse imperative skill phrases, which is what drops
            genuinely-actionable input below confidence_threshold. Cheap and heuristic on
            purpose -- mirrors Intent_Classification.process_input's own prefix-stripping
            convention rather than a full parse.
        @param processed_text The cleaned and processed player input.
        @return A list of candidate strings to score, always including the original text
            first (deduplicated, order-preserving).
        """
        candidates = [processed_text]

        for marker in TOPIC_CLAUSE_MARKERS:
            index = processed_text.find(marker)
            if index > 0:
                candidates.append(processed_text[:index].strip())

        for separator in CLAUSE_SEPARATORS:
            if separator in processed_text:
                for clause in processed_text.split(separator):
                    clause = clause.strip()
                    if clause:
                        candidates.append(clause)

        seen = set()
        unique_candidates = []
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                unique_candidates.append(candidate)
        return unique_candidates

    def _match_by_keyword(self, processed_text):
        """!
        @brief Checks processed_text for a literal, whole-word hit against every skill's own
            skills.toml keyword list -- the fallback map_to_action reaches for once every
            phrasing candidate has scored below confidence_threshold semantically. Word
            boundaries matter here (ex: skill "artistry"'s keyword "art" must not match inside
            "start"); a multi-word keyword (ex: "black market") matches as the exact phrase.
            Returns every skill with a literal hit, not just the first declared -- two
            different skills can each have their own keyword present in the same sentence
            (ex: "cost" for appraise and "dagger" for blades both appearing in "what's this
            dagger worth"), and _best_keyword_match picks between them by embedding score
            rather than accepting whichever happens to come first in skills.toml.
        @param processed_text The cleaned and processed player input.
        @return A list of every matching skill's name, in skills.toml declaration order.
        """
        matches = []
        for name, skill in self.skills_data.items():
            for keyword in skill.get("keywords", []):
                if re.search(rf"\b{re.escape(keyword)}\b", processed_text):
                    matches.append(name)
                    break
        return matches

    def _best_keyword_match(self, processed_text, cosine_scores):
        """!
        @brief Picks the strongest-scoring skill among every literal keyword hit in
            processed_text (see _match_by_keyword), rather than accepting skills.toml's
            arbitrary declaration order when more than one skill has a hit. Only ever called
            as a fallback once every phrasing candidate has already missed
            confidence_threshold semantically (see map_to_action).
        @param processed_text The cleaned and processed player input.
        @param cosine_scores The (num_candidates x num_phrases) matrix map_to_action already
            computed for this input, reused here instead of re-encoding anything.
        @return (skill_name, score), or (None, -1.0) if no skill had a literal keyword hit.
        """
        best_skill, best_score = None, -1.0
        for name in self._match_by_keyword(processed_text):
            positions = [i for i, skill_name in enumerate(self.skill_indices) if skill_name == name]
            score = cosine_scores[:, positions].max().item()
            if score > best_score:
                best_skill, best_score = name, score
        return best_skill, best_score

    def map_to_action(self, processed_text):
        """!
        @brief Maps the processed text to a specific skill or action using semantic similarity.
            Tries several phrasings of the input (see _generate_match_candidates) against the
            full skill-phrase bank in one batched call, and falls back to a literal keyword hit
            (see _match_by_keyword) if every phrasing still misses confidence_threshold.
        @param processed_text The cleaned and processed player input.
        @return A tuple of (skill_name, confidence_score).
        """
        if self.all_embeddings is None:
            self.event_bus.publish("log_error", "NLPCore: Skill embeddings not initialized.")
            return None, 0.0

        candidates = self._generate_match_candidates(processed_text)
        candidate_embeddings = self.model.encode(candidates, convert_to_tensor=True)

        # cosine_scores is (num_candidates x num_phrases) -- the best (candidate, phrase) pair
        # anywhere in the matrix wins, so a topic-stripped or clause-split phrasing can win over
        # the full sentence without needing to know in advance which one will score highest.
        cosine_scores = util.cos_sim(candidate_embeddings, self.all_embeddings).cpu().numpy()
        best_candidate_idx, best_phrase_idx = np.unravel_index(np.argmax(cosine_scores), cosine_scores.shape)
        best_score = cosine_scores[best_candidate_idx, best_phrase_idx].item()
        best_skill = self.skill_indices[best_phrase_idx]

        if best_score >= self.confidence_threshold:
            matched_candidate = candidates[best_candidate_idx]
            if matched_candidate != processed_text:
                self.event_bus.publish(
                    "log_info",
                    f"Mapped input to action: {best_skill} via alternate phrasing "
                    f"\"{matched_candidate}\" (Score: {best_score:.4f})"
                )
            else:
                self.event_bus.publish("log_info", f"Mapped input to action: {best_skill} via best phrase (Score: {best_score:.4f})")
            return best_skill, best_score

        keyword_skill, keyword_score = self._best_keyword_match(processed_text, cosine_scores)
        if keyword_skill and keyword_score >= self.keyword_fallback_floor:
            self.event_bus.publish(
                "log_info",
                f"Mapped input to action: {keyword_skill} via keyword fallback (Score: {keyword_score:.4f})"
            )
            return keyword_skill, keyword_score

        self.event_bus.publish(
            "log_info",
            f"Best match was {best_skill} (Score: {best_score:.4f}), below confidence "
            f"threshold ({self.confidence_threshold}) - no skill triggered."
        )
        return None, best_score

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

    def classify_sentiment(self, processed_text):
        """!
        @brief Classifies the tone of a dialogue line via sentiment_pipeline's own 3-way
            (negative/neutral/positive) softmax -- only called from Intent_Classification.py's
            own dialogue branch (see CLAUDE.md's "Dialogue"), never for skill/item/target
            matching. Uses a fine-tuned classifier rather than this class's own embedding model
            or a lexicon-based analyzer: sentiment-of-an-utterance needs broad, compositional
            coverage across however a player might phrase something (ex: "get out of my sight"
            -- clearly hostile, no single word a lexicon lookup would flag), which only a model
            trained on real labeled sentiment examples reliably provides.
        @param processed_text The cleaned and processed player input.
        @return A tuple of (sentiment_label, confidence_score); confidence_score is the winning
                class's own softmax probability (0..1), keeping the same "always a non-negative
                confidence" shape every other matcher method already returns. sentiment_label is
                "negative"/"positive" once that probability clears sentiment_confidence_threshold
                and the winning class isn't "neutral" -- None otherwise, the "no strong tone
                either way" case, covering purely informational dialogue, genuinely neutral
                phrasing, and a low-confidence call the model itself isn't sure about.
        """
        result = self.sentiment_pipeline(processed_text)[0]
        raw_label, score = result["label"].lower(), result["score"]

        if raw_label == "neutral" or score < self.sentiment_confidence_threshold:
            return None, score

        self.event_bus.publish("log_info", f"Classified dialogue sentiment: {raw_label} (Score: {score:.4f})")
        return raw_label, score

    def map_to_target(self, processed_text):
        """!
        @brief Maps the processed text to a specific entity's name using semantic similarity,
            the same way map_to_item does for items but against target_embeddings/
            target_indices -- every non-player "creature" (for combat retargeting) plus every
            entity carrying its own [entity.test] regardless of supertype (ex: "cursed
            dagger", for an item-level skill check). Matching itself doesn't distinguish the
            two kinds of match; DMCore is what decides whether a returned name is a combat
            redirect or an item-test target based on what the entity actually is. Ties between
            identically-named/described instances (ex: two plain "wolf" instances sharing one
            template's text) resolve to whichever was declared first, the same inherent
            limitation map_to_item already has for duplicate item names -- this isn't real
            multi-instance disambiguation (ex: "the wounded wolf").
        @param processed_text The cleaned and processed player input.
        @return A tuple of (entity_name, confidence_score); entity_name is None below
                confidence_threshold or if no target embeddings are loaded.
        """
        if self.target_embeddings is None:
            return None, 0.0

        input_embedding = self.model.encode(processed_text, convert_to_tensor=True)
        cosine_scores = util.cos_sim(input_embedding, self.target_embeddings)[0]

        best_phrase_idx = np.argmax(cosine_scores.cpu().numpy())
        best_score = cosine_scores[best_phrase_idx].item()
        best_target = self.target_indices[best_phrase_idx]

        if best_score < self.confidence_threshold:
            return None, best_score

        self.event_bus.publish("log_info", f"Mapped input to target: {best_target} (Score: {best_score:.4f})")
        return best_target, best_score


class NLPCore:
    """!
    @brief Thin EventBus glue over IntentClassifier -- subscribes to player input and catalog
        updates, delegates to the classifier, and publishes whatever it returns. Owns no
        classification logic of its own; see Intent_Classification.py for that.
    """

    def __init__(self, event_bus):
        """!
        @brief Initializes the NLP core and loads semantic models.
        @param event_bus The central event bus instance.
        """
        self.event_bus = event_bus
        self.matcher = SentenceTransformerMatcher(event_bus)
        self.classifier = IntentClassifier(self.matcher)

        self.event_bus.subscribe("rules_loaded", self._on_rules_loaded)
        self.event_bus.subscribe("user_input_submitted", self._on_user_input)
        # DM_Improvisation.py publishes this whenever an ad hoc entity is created or restored
        # from a save -- see SentenceTransformerMatcher.register_item's own docstring.
        self.event_bus.subscribe("item_catalog_updated", self._on_item_catalog_updated)

        self.event_bus.publish("log_info", "NLPCore initialized with SentenceTransformer.")

    def _on_user_input(self, player_input):
        """!
        @brief Event handler for user input -- delegates to IntentClassifier.classify() and
            publishes every event it returns, in order. See that method's own docstring for
            why more than one event can come back from a single turn.
        @param player_input The raw string from "user_input_submitted".
        """
        processed, events = self.classifier.classify(player_input)
        self.event_bus.publish("log_info", f"Processing player input: {player_input} -> {processed}")
        for event in events:
            self.event_bus.publish(event["event"], event["payload"])

    def _on_rules_loaded(self, data):
        """!
        @brief Callback when rules are loaded from DMCore.
        @param data The rules data containing skills and entities.
        """
        self.classifier.on_rules_loaded(data)

    def _on_item_catalog_updated(self, data):
        """!
        @brief Forwards every entity in the "item_catalog_updated" payload to the classifier's
            own register_item, one at a time.
        @param data The "item_catalog_updated" payload ({"entities": [{"name",
            "description"}, ...]}).
        """
        for entry in data.get("entities", []):
            self.classifier.register_item(entry.get("name"), entry.get("description", ""))
