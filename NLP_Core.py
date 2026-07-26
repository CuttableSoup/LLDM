"""!
@file NLP_Core.py
@brief Receives and processes player input using semantic similarity.
"""

import re

import numpy as np
from sentence_transformers import SentenceTransformer, util

# Substring checks against processed input to decide item-interaction intent, before any skill
# matching runs. Phrases (not bare words) where a bare word would collide with an existing
# skill phrasing already in use -- ex: "pick" alone would misfire on "I pick the lock"
# (finesse), so "pick up" (the two-word phrase) is required instead. Same reasoning for
# OPEN/CLOSE_KEYWORDS requiring "the"/"it" rather than a bare "close " -- "blades"'s own
# description is "Using swords and knives in close combat.", which a bare "close " would
# misfire on before skill matching ever got a chance to run.
EXAMINE_KEYWORDS = ("examine", "inspect", "look at", "check out")
# Moves an item already in the player's own inventory into a worn/wielded [entity.equipped]
# slot -- see DMCore._resolve_equip_intent. No collision risk with any skill's own keyword
# list (checked by grep against skills.toml -- none of these four phrases appear there).
EQUIP_KEYWORDS = ("equip ", "wear ", "wield ", "put on ")
# Checked ahead of both EQUIP_KEYWORDS ("unequip " literally contains "equip " as a
# substring -- "un" + "equip ") and TAKE_KEYWORDS ("take off" would otherwise match
# TAKE_KEYWORDS' own "take " substring first and misfire as a plain "take"). Deliberately
# just these two phrases,
# not a broader "remove "/"take off my " -- "remove" collides with real item names
# (items.toml's "dart trap"/"scythe trap") and finesse's own "disarm"/"trap" keywords, so
# "remove the trap" needs to keep falling through to a disarm skill check, not get swallowed
# here as an attempt to unequip something named "trap". See DMCore._resolve_unequip_intent.
UNEQUIP_KEYWORDS = ("unequip ", "take off")
# Moves an item out of inventory onto the current room/scene's own ground (see
# DMCore._resolve_drop_intent) -- unlike "give"/"trade" (both aimed at the current target),
# this one has no recipient at all.
DROP_KEYWORDS = ("drop ", "discard ", "put down")
TAKE_KEYWORDS = ("take ", "grab ", "pick up", "loot ")
# "give"/"trade" move an item the opposite directions ("give" is player -> target, "trade" is
# target -> player but paid) -- see DMCore._on_item_interaction_detected. TRADE_KEYWORDS
# deliberately avoids every word in skills.toml's "appraise" keywords list (evaluation,
# commerce, investigation, value, price, worth, cost, identify, examine), so a phrase like
# "what's this worth" still reaches appraise instead of being swallowed here.
GIVE_KEYWORDS = ("give ", "hand over", "offer ")
TRADE_KEYWORDS = ("trade ", "buy ", "purchase ")
# Consuming or activating an item already in the player's own inventory -- see
# DMCore._resolve_use_intent. The intent name is the generic "use" (not "drink"), so this one
# mechanism can grow to cover more than potions later (ex: a wand's own "wave "/"point at ")
# just by adding new phrases here, without touching DMCore at all. A bare "use " deliberately
# isn't included: it's far too generic a verb (could plausibly mean almost anything) to
# safely route every "use ..." phrase into item-use handling the way these specific verbs can.
USE_KEYWORDS = ("drink ", "quaff ", "drink it")
OPEN_KEYWORDS = ("open the ", "open it")
CLOSE_KEYWORDS = ("close the ", "close it", "shut the ", "shut it")
# Movement/positioning (see DM_Movement.py) -- like open/close, these act on the whole scene
# rather than a named item, so no map_to_item lookup ever runs for them either. Phrases, not
# bare "move ", since a bare word would swallow unrelated skill phrasing the same way a bare
# "close " would have (see the module note above) -- none of these collide with any
# skills.toml keyword list. Deliberately no "close the distance" here even though it's a
# natural phrasing -- CLOSE_KEYWORDS' "close the " is checked first (see _detect_item_intent)
# and would swallow it as a "close" intent instead.
ADVANCE_KEYWORDS = ("advance", "move closer", "approach", "move toward", "move in", "step closer")
RETREAT_KEYWORDS = ("retreat", "back away", "back off", "fall back", "step back", "withdraw", "move away")
# Party positioning (see DM_Core._resolve_formation_intent / CLAUDE.md's "Party formation") --
# like advance/retreat above, these act on the scene (specifically, whichever party member is
# named, or the whole party if none is) rather than a named item, so no map_to_item lookup ever
# runs for them either; unlike advance/retreat, DMCore -- not NLPCore -- is what figures out
# *who* is being addressed, by searching the raw input for a party member's own name. None of
# these phrases collide with ADVANCE/RETREAT_KEYWORDS' own substrings (ex: "fall back" is
# retreat, "fall in behind" is not "fall back").
FORMATION_BEHIND_KEYWORDS = (
    "stay behind", "get behind", "hang back", "keep behind", "fall in behind", "stand behind",
)
FORMATION_ABREAST_KEYWORDS = (
    "walk beside", "stay beside", "stay abreast", "walk with me", "walk alongside", "flank me",
    "stand beside", "walk abreast",
)
# Macro, inter-room movement in a multi-room dungeon (see DM_Rules.py's room-graph notes) --
# a different axis entirely from ADVANCE/RETREAT_KEYWORDS above (which only ever reposition
# the player's *band* within the current room). A direction here names one of the current
# room's own [[room.exit]] entries (DMCore._find_room_exit) -- which exit actually resolves
# depends on both the direction *and* the player's current band, so a room can have more than
# one exit (ex: "right" at band 2 vs "forward" at band 3), a real branch rather than just a
# forward/back corridor. Phrasing deliberately spells out "room"/"door"/"deeper"/"way" so this
# can't be confused with plain intra-room advance/retreat, which say nothing about leaving the
# room at all.
DIRECTION_PHRASES = {
    "forward": (
        "next room", "proceed deeper", "continue deeper", "go deeper", "through the door",
        "onward into the dungeon", "continue onward", "move on ahead", "go forward",
        "head forward", "continue forward",
    ),
    "back": (
        "previous room", "last room", "go back the way", "back the way we came",
        "the room behind", "back the way i came",
    ),
    "left": ("go left", "head left", "turn left", "to the left", "the left passage", "the left exit"),
    "right": ("go right", "head right", "turn right", "to the right", "the right passage", "the right exit"),
}

# map_to_item checks these before any embedding match -- currency is a plain integer field
# (entity["currency"]), not an object-supertype entity with a name/description to embed.
CURRENCY_SYNONYMS = ("gold", "coin", "currency", "money")

# _detect_save_load_intent checks these ahead of everything else (item intent included --
# a slot name could otherwise contain a word like "take" and misfire the item intercept).
# Longest/most-specific prefix first in each tuple, since matching stops at the first hit and
# a shorter prefix (ex: "save ") would otherwise swallow "game as " into the slot name.
SAVE_PREFIXES = ("save game as ", "save as ", "save game ", "save ")
LOAD_PREFIXES = ("load game as ", "load as ", "load game ", "load ")

# map_to_action's alternate-phrasing candidates: markers that introduce a topic clause rather
# than describing the action itself (ex: "...about the road" in "have you heard anything about
# the road"). Truncating at the first one found gives a second, less-diluted candidate to score
# against the same skill-phrase bank -- see the confidence-threshold dilution gotcha in
# NLP_Core.py's module notes. Mirrors process_input's own prefix-stripping convention rather
# than a full parse.
TOPIC_CLAUSE_MARKERS = (" about ", " regarding ", " concerning ", " if ", " whether ", " that ")
CLAUSE_SEPARATORS = ("--", "?", ",", ";", ":")

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

        direction = self._detect_direction(processed)
        if direction:
            # A different axis from "advance"/"retreat" below -- see DIRECTION_PHRASES'
            # module note. No item name to resolve at all, so map_to_item never runs for
            # this either; DMCore._find_room_exit is what actually decides whether this
            # direction resolves to a real exit from the player's current band.
            self.event_bus.publish("item_interaction_detected", {
                "intent": "move", "item_name": None, "direction": direction,
                "input": processed, "score": None,
            })
            return

        intent = self._detect_item_intent(processed)
        if intent in ("open", "close", "advance", "retreat", "formation_behind", "formation_abreast"):
            # These act on the current scene target directly (ex: "open the chest" opens
            # *the* chest, not some named item inside it), or on the scene as a whole (ex:
            # "advance" moves relative to every living entity, "formation_behind" directs
            # whichever party member is named in the input, or everyone present if none is --
            # see DMCore._resolve_formation_intent) -- no item name to resolve at all, so
            # map_to_item never runs for any of them either.
            self.event_bus.publish("item_interaction_detected", {
                "intent": intent, "item_name": None, "input": processed, "score": None,
            })
            return
        if intent:
            item_name, item_score = self.map_to_item(processed)
            if item_name:
                self.event_bus.publish("item_interaction_detected", {
                    "intent": intent, "item_name": item_name, "input": processed, "score": item_score,
                })
                return
            # A recognized "examine"/"take"/"give"/"trade" verb but no matching item name --
            # fall through to skill matching below rather than silently dropping it (ex: could
            # still be a legitimate skill phrase that happens to contain one of these words).

        skill, score = self.map_to_action(processed)
        if skill:
            payload = {"skill": skill, "score": score, "input": processed}
            # A confidently-matched creature name (ex: "attack the second wolf") is attached
            # as a target hint alongside the matched skill -- unlike item/save-load intent,
            # this never gates or replaces skill matching, it only enriches the same
            # action_detected payload. DMCore is what actually decides whether to honor it
            # (see _on_action_detected's explicit_target validation) -- matching here is
            # global/scene-unaware, same division of labor as map_to_item.
            target_name, target_score = self.map_to_target(processed)
            if target_name:
                payload["target"] = target_name
            self.event_bus.publish("action_detected", payload)
        else:
            # Below confidence_threshold: publish this instead of staying silent, so the
            # player gets some response rather than the app appearing to stall.
            self.event_bus.publish("action_not_understood", {"input": processed, "score": score})

    def _detect_item_intent(self, processed_text):
        """!
        @brief Checks processed input for an item-interaction verb, ahead of skill matching.
            Inter-room movement is checked separately, before this (see _detect_direction),
            since it isn't keyed to a single fixed intent name the way these are.
        @param processed_text The cleaned and processed player input.
        @return "examine", "equip", "unequip", "drop", "take", "give", "trade", "use",
            "open", "close", "advance", "retreat", "formation_behind", "formation_abreast",
            or None.
        """
        if any(keyword in processed_text for keyword in EXAMINE_KEYWORDS):
            return "examine"
        # Checked ahead of EQUIP_KEYWORDS -- "unequip " contains EQUIP_KEYWORDS' own "equip "
        # as a literal substring ("un" + "equip "), so this order isn't optional.
        if any(keyword in processed_text for keyword in UNEQUIP_KEYWORDS):
            return "unequip"
        if any(keyword in processed_text for keyword in EQUIP_KEYWORDS):
            return "equip"
        if any(keyword in processed_text for keyword in DROP_KEYWORDS):
            return "drop"
        if any(keyword in processed_text for keyword in TAKE_KEYWORDS):
            return "take"
        if any(keyword in processed_text for keyword in GIVE_KEYWORDS):
            return "give"
        if any(keyword in processed_text for keyword in TRADE_KEYWORDS):
            return "trade"
        if any(keyword in processed_text for keyword in USE_KEYWORDS):
            return "use"
        if any(keyword in processed_text for keyword in OPEN_KEYWORDS):
            return "open"
        if any(keyword in processed_text for keyword in CLOSE_KEYWORDS):
            return "close"
        # Checked ahead of ADVANCE_KEYWORDS -- "stand behind"/"walk abreast" etc. don't
        # collide with any advance/retreat phrase, but formation is the more specific match
        # whenever both could plausibly apply.
        if any(keyword in processed_text for keyword in FORMATION_BEHIND_KEYWORDS):
            return "formation_behind"
        if any(keyword in processed_text for keyword in FORMATION_ABREAST_KEYWORDS):
            return "formation_abreast"
        if any(keyword in processed_text for keyword in ADVANCE_KEYWORDS):
            return "advance"
        if any(keyword in processed_text for keyword in RETREAT_KEYWORDS):
            return "retreat"
        return None

    def _detect_direction(self, processed_text):
        """!
        @brief Checks processed input for a room-exit direction, ahead of both save/load
            intent's own callers and skill matching -- see DIRECTION_PHRASES' module note.
        @param processed_text The cleaned and processed player input.
        @return "forward", "back", "left", "right", or None.
        """
        for direction, phrases in DIRECTION_PHRASES.items():
            if any(phrase in processed_text for phrase in phrases):
                return direction
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

    def _generate_match_candidates(self, processed_text):
        """!
        @brief Builds alternate, less-diluted phrasings of the player's input to re-score
            against the same skill-phrase bank as the full sentence. A long sentence with a
            trailing topic clause (ex: "...about the road") or a leading aside (ex: "I'm
            sorry -- ...") pools toward that clause's own semantics in the whole-sentence
            embedding and away from the terse imperative skill phrases, which is what drops
            genuinely-actionable input below confidence_threshold (see the dilution gotcha in
            this file's module notes). Cheap and heuristic on purpose -- mirrors
            process_input's own prefix-stripping convention rather than a full parse.
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