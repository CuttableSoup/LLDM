"""!
@file NLP_Core.py
@brief Receives and processes player input using semantic similarity.
"""

import re

import numpy as np
import torch
from sentence_transformers import SentenceTransformer, util

# AdHoc_Generation.py is the one project-internal import this module makes -- it's pure/
# DMCore-independent (no DMCore/game-state coupling of its own), so importing its shared
# intent-vocabulary constants doesn't compromise this module's own independence from DMCore.
# See IMPROVISABLE_INTENTS, below, for what these three are actually used for here.
from AdHoc_Generation import GROUND_AWARE_INTENTS, PLAYER_CENTRIC_INTENTS, TARGET_CENTRIC_INTENTS

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
# Free-form conversational address -- bypasses the skill/dice system entirely, the same as
# every item/movement intent above (see DM_Core.py's "Items and movement as intents"), but
# checked only after item-interaction detection has already had its shot, so a genuine item
# verb never gets swallowed as dialogue just because it happens to also name an entity (ex:
# "give the sword to Anne" stays "give", never reaches this check at all). Phrases, not bare
# words, for the same collision-avoidance reason every other keyword tuple in this file
# follows -- "talk to "/"speak to "/"speak with " avoid colliding with the languages skill's
# own "speak" keyword and the persuasion-family skill's own "talk" keyword (skills.toml) the
# same way EXAMINE_KEYWORDS avoids a bare "close ". Unlike item intents, there's no item name
# (or, really, any name) resolved here at all -- DMCore's own DialogueMixin (DM_Dialogue.py)
# is what figures out *who* is being addressed, the same "search the raw input for a
# currently-present entity's own name" approach DM_Movement.py's formation handling already
# uses, rather than a second global embedding catalog.
DIALOGUE_KEYWORDS = (
    "talk to ", "speak to ", "speak with ", "ask ", "tell ", "say to ", "greet ", "chat with ",
)

# Reserved persona name for the out-of-character help/guidance channel (see DM_Help.py) -- a
# fixed, always-available meta-command in the same spirit as save/load, not an in-fiction
# dialogue target: no scene entity is ever named "adam", so DialogueMixin's own "search
# scenario_entities for a named entity" resolution would never find it and would silently fall
# back to whatever the default scene target happens to be. Checked as its own whole-input,
# pre-clause-split reserved word instead, ahead of both item-interaction detection and
# DIALOGUE_KEYWORDS, so "talk to ADaM"/"ask ADaM about my skills" reach the help channel rather
# than being swallowed as ordinary dialogue. \b-anchored, case-insensitive, so "Adam"/"ADAM"
# all match but "adamant" doesn't. Known, accepted tradeoff: reserves the literal name "adam"
# the same way DM_Rules.py's PLAYER_PLACEHOLDER reserves "player" -- no future entity in any
# setting can be named Adam without colliding with this.
ADAM_NAME_PATTERN = re.compile(r"\badam\b", re.IGNORECASE)

# A cheap, local pre-check on top of ADAM_NAME_PATTERN -- attached to help_detected's own
# payload as "removal_candidate" so DM_Help.py only pays for a synchronous ad hoc-removal LLM
# call (AdHoc_Generation.py's decide_entity_removal) on a message that actually smells like a
# removal request, not on every ordinary "ADaM, what are my skills" question. Purely a gate on
# whether to *ask* the LLM at all -- the LLM's own tool_choice="auto"/"decline" is still the
# real arbiter of whether anything actually gets removed.
REMOVAL_KEYWORDS = (
    "remove", "get rid of", "destroy", "delete", "banish", "dismiss", "make it disappear",
    "make them disappear",
)

# Mirrors REMOVAL_KEYWORDS exactly, for "creature_candidate" -- gates whether DM_Help.py bothers
# calling AdHoc_Generation.py's generate_ad_hoc_creature (a synchronous LLM call) at all, not
# the real arbiter of whether anything actually gets conjured (that's still the LLM's own
# tool_choice="auto"/"decline").
CREATURE_KEYWORDS = (
    "summon", "conjure", "spawn", "bring in", "there's a", "there is a", "add a", "appears",
)

# Mirrors REMOVAL_KEYWORDS/CREATURE_KEYWORDS exactly, for "edit_candidate" -- gates whether
# DM_Help.py bothers calling AdHoc_Generation.py's decide_entity_edit.
EDIT_KEYWORDS = (
    "change", "edit", "make the", "make it", "is now", "describe it as", "describe the",
)

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

# Multi-action detection (the West End Games D6 "multiple actions" rule -- see DM_Core.py's
# own "Multiple actions" docstring): splits the final skill-matching fallback into one or more
# independently-matched clauses, so "I attack the orc and cast a ward" resolves as two separate
# actions rather than one diluted embedding match across the whole sentence. Deliberately a
# *different*, wider delimiter set than CLAUSE_SEPARATORS below (which exists purely to
# generate alternate *phrasings* of what's still treated as one action) -- "and"/"then" name
# real action boundaries here, not just punctuation a single sentence happens to contain.
# \b-anchored so "and"/"then" only ever splits on the standalone word, never a substring inside
# another word (ex: "handle", "sandbox").
ACTION_CLAUSE_PATTERN = re.compile(r"--|[,;:?]|\band\b|\bthen\b")

# Partitions _detect_item_intent's own return set for _on_user_input's per-clause turn
# classification (see DM_Core.py's "Multiple actions" docstring) -- two independent axes, not
# one: EXEMPT_ITEM_INTENTS is a *rules* distinction (movement/directing-the-party are free per
# West End Games' own exceptions, same as speech, so a clause classified this way is published
# immediately as its own free-standing item_interaction_detected and never joins the shared
# per-turn action count at all); NO_ITEM_LOOKUP_INTENTS is a purely *technical* one (these two
# act on the current scene target directly, so map_to_item never runs for them) that's
# independent of whether the intent is exempt -- "open"/"close" still cost a turn action (see
# DM_Core.py) despite needing no item lookup, the same way "give"/"take"/etc. do.
EXEMPT_ITEM_INTENTS = frozenset({"advance", "retreat", "formation_behind", "formation_abreast"})
NO_ITEM_LOOKUP_INTENTS = frozenset({"open", "close"})

# The item-interaction verbs eligible for DM_Improvisation.py's ad hoc creation fallback (see
# _on_user_input's own "improvisation_requested" note) -- includes "trade" (ex: "buy a rope"
# from a shopkeeper who never had one on their own hand-authored inventory list -- a general
# store shouldn't need every possible good pre-authored to sell it): DM_Improvisation.py stocks
# the created item directly into the current scene target's own inventory for this one intent,
# rather than the ground/player inventory every other intent here uses. Computed as the union
# of AdHoc_Generation.py's own PLAYER_CENTRIC_INTENTS/GROUND_AWARE_INTENTS/TARGET_CENTRIC_
# INTENTS -- imported from there, not DM_Improvisation.py, since AdHoc_Generation.py is the one
# module both this file and DM_Improvisation.py already treat as pure/DMCore-independent, so
# this module's own independence from DMCore/game state stays intact.
IMPROVISABLE_INTENTS = PLAYER_CENTRIC_INTENTS | GROUND_AWARE_INTENTS | TARGET_CENTRIC_INTENTS

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
        # DM_Improvisation.py publishes this whenever an ad hoc entity is created or restored
        # from a save -- see _on_item_catalog_updated's own docstring.
        self.event_bus.subscribe("item_catalog_updated", self._on_item_catalog_updated)
        
        self.event_bus.publish("log_info", "NLPCore initialized with SentenceTransformer.")

    def _on_user_input(self, player_input):
        """!
        @brief Event handler for user input. Splits the input into one or more clauses (see
            _split_action_clauses) and classifies each independently as an item interaction or
            a skill/ability action, merging both kinds into a single "turn_detected" event --
            see DM_Core.py's own "Multiple actions" docstring for why these two used to be
            entirely separate pipelines and why that was wrong (drawing a weapon, picking
            something up, and swinging a sword all cost the same shared per-turn action
            economy in West End Games D6; only movement and speech are actually free).

            Three whole-input checks still run *ahead* of clause splitting, exactly as before
            this merge (save/load and direction) plus one new one (ADaM): save/load (a
            meta-command, never split at all), ADaM (also a meta-command -- see
            ADAM_NAME_PATTERN's own module note -- checked right after save/load so it beats
            both item-interaction detection and DIALOGUE_KEYWORDS to the punch), and inter-room
            movement (_detect_direction -- a different axis from advance/retreat below, see
            DIRECTION_PHRASES' own module note). Dialogue detection also still runs against
            the *whole* input, not per clause (left that way deliberately for now -- full
            multi-intent-type composition involving dialogue is still out of scope), but only
            once no clause resolved to an item interaction or an exempt movement/formation
            intent -- preserving the exact priority order the old single-clause code already
            gave item intents over dialogue (so a genuine item verb naming an entity, ex:
            "give the sword to Anne", is never swallowed as dialogue).

            If the whole turn would otherwise resolve to nothing at all (no turn_clauses, no
            exempt clause), but at least one clause was a recognized item verb naming something
            map_to_item couldn't match, "improvisation_requested" is published instead of
            "action_not_understood" -- DM_Improvisation.py's own last-resort ad hoc entity
            creation fallback (see IMPROVISABLE_INTENTS' own module note).
        @param player_input The raw string from "user_input_submitted".
        """
        processed = self.process_input(player_input)

        save_load_intent, slot_name = self._detect_save_load_intent(processed)
        if save_load_intent:
            self.event_bus.publish(f"{save_load_intent}_requested", {"slot": slot_name})
            return

        if self._detect_help_intent(processed):
            self.event_bus.publish("help_detected", {
                "input": processed,
                "removal_candidate": self._detect_removal_intent(processed),
                "creature_candidate": self._detect_creature_intent(processed),
                "edit_candidate": self._detect_edit_intent(processed),
            })
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

        # Pass 1: item-interaction classification, per clause. EXEMPT_ITEM_INTENTS (movement/
        # directing the party) publish their own free-standing item_interaction_detected
        # immediately, in clause order, and never join the shared turn -- same as _detect_
        # direction above, just resolved per clause instead of against the whole input (ex:
        # "attack the wolf and retreat" still lets the retreat through even though the whole
        # sentence isn't a pure movement command). Everything else that resolves as an item
        # interaction (NO_ITEM_LOOKUP_INTENTS' "open"/"close", or any other intent with a
        # confidently-matched item_name) joins turn_clauses -- these *do* cost a turn action
        # (see DM_Core.py's "Multiple actions"), just never a dice roll. A clause that doesn't
        # resolve as an item interaction at all is deferred to pass 2 below.
        turn_clauses = []
        remaining_clauses = []
        found_exempt = False
        # Recognized item verbs (see IMPROVISABLE_INTENTS) whose own map_to_item call found
        # nothing -- tracked separately from remaining_clauses so, if the whole turn otherwise
        # resolves to nothing at all, DM_Improvisation.py gets a last-resort shot at conjuring
        # a plausible object instead of this input simply dead-ending (see this method's own
        # "improvisation_requested" note, below).
        unmatched_item_verbs = []
        for clause in self._split_action_clauses(processed):
            clause_intent = self._detect_item_intent(clause)
            if clause_intent in EXEMPT_ITEM_INTENTS:
                found_exempt = True
                self.event_bus.publish("item_interaction_detected", {
                    "intent": clause_intent, "item_name": None, "input": processed, "score": None,
                })
                continue
            if clause_intent in NO_ITEM_LOOKUP_INTENTS:
                turn_clauses.append({"kind": "item", "intent": clause_intent, "item_name": None})
                continue
            if clause_intent:
                item_name, item_score = self.map_to_item(clause)
                if item_name:
                    turn_clauses.append({"kind": "item", "intent": clause_intent, "item_name": item_name})
                    continue
                # A recognized "examine"/"take"/"give"/"trade" verb but no matching item name --
                # fall through to skill matching below rather than silently dropping it (ex:
                # could still be a legitimate skill phrase that happens to contain one of
                # these words), but remember it in case skill matching also comes up empty.
                if clause_intent in IMPROVISABLE_INTENTS:
                    unmatched_item_verbs.append({"intent": clause_intent, "phrase": clause})
            remaining_clauses.append(clause)

        # Dialogue is checked once, on the whole input, only once pass 1 found nothing at all
        # (no item interaction, no exempt movement/formation clause) -- the same priority the
        # old single-clause code already gave item intents over dialogue.
        if not turn_clauses and not found_exempt and self._detect_dialogue_intent(processed):
            self.event_bus.publish("dialogue_detected", {"input": processed, "score": None})
            return

        # Pass 2: skill/ability matching for whatever clauses pass 1 didn't already claim.
        best_score = 0.0
        for clause in remaining_clauses:
            clause_skill, clause_score = self.map_to_action(clause)
            best_score = max(best_score, clause_score)
            if not clause_skill:
                continue
            action = {"kind": "action", "skill": clause_skill, "score": clause_score}
            # A confidently-matched creature name (ex: "attack the second wolf") is attached
            # as a target hint alongside the matched skill -- unlike item/save-load intent,
            # this never gates or replaces skill matching, it only enriches the same turn
            # entry. DMCore is what actually decides whether to honor it (see
            # _on_turn_detected's explicit_target validation) -- matching here is
            # global/scene-unaware, same division of labor as map_to_item. Resolved per clause,
            # not against the whole input, so "attack the orc and cast a ward on thane" can
            # redirect each action at its own named target.
            target_name, target_score = self.map_to_target(clause)
            if target_name:
                action["target"] = target_name
            turn_clauses.append(action)

        if turn_clauses:
            # Always published this way, even for the overwhelmingly common single-clause,
            # single-kind case -- see DM_Core.py's own "Multiple actions" docstring for why
            # the whole downstream pipeline (DMCore, LLMCore) is built around one consistent
            # shape rather than special-casing N=1 or a single clause kind.
            self.event_bus.publish("turn_detected", {"clauses": turn_clauses, "input": processed})
        elif not found_exempt and unmatched_item_verbs:
            # The whole turn would otherwise resolve to nothing at all, but at least one clause
            # was a recognized item verb naming something that just doesn't exist yet -- last
            # resort before giving up: DM_Improvisation.py's own generate_ad_hoc_item gets a
            # chance to decide whether that's plausible to conjure into the scene (ex: "pick up
            # a stone"). Only the first candidate is used -- extending this into a genuinely
            # multi-clause improvisation attempt is out of scope for now (a second, understood
            # clause in the same input still silently drops any of its own unmatched item verbs,
            # unchanged from today's behavior, since turn_clauses would be non-empty and this
            # branch wouldn't even run).
            candidate = unmatched_item_verbs[0]
            self.event_bus.publish("improvisation_requested", {
                "intent": candidate["intent"], "phrase": candidate["phrase"], "input": processed,
            })
        elif not found_exempt:
            # Below confidence_threshold on every remaining clause, and pass 1 found nothing
            # either: publish this instead of staying silent, so the player gets some response
            # rather than the app appearing to stall.
            self.event_bus.publish("action_not_understood", {"input": processed, "score": best_score})

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

    def _keyword_gate(self, processed_text, keywords):
        """!
        @brief Shared shape behind every plain "does this phrase contain any of these keyword
            substrings" check in this file (_detect_dialogue_intent/_detect_removal_intent/
            _detect_creature_intent/_detect_edit_intent, below) -- each of those keeps its own
            named method rather than calling this directly, since the name itself (ex:
            "removal_candidate") is what makes _on_user_input's own payload-building code
            self-documenting without a comment; only the one-line body was ever duplicated.
        @param processed_text The cleaned and processed player input.
        @param keywords The keyword tuple to check against (ex: REMOVAL_KEYWORDS).
        @return True if processed_text contains any keywords phrase, else False.
        """
        return any(keyword in processed_text for keyword in keywords)

    def _detect_dialogue_intent(self, processed_text):
        """!
        @brief Checks processed input for conversational-address phrasing, checked last among
            the pre-skill-matching intents -- after direction and item-interaction detection
            have both already had their shot -- so a genuine movement phrase or item verb
            always wins over a coincidental dialogue-keyword substring. Unlike
            _detect_item_intent, there's no further NLPCore-side resolution to do (no item to
            look up) -- this is purely a yes/no "does this look like the player talking to
            someone" call; DMCore's DialogueMixin (DM_Dialogue.py) resolves who's actually
            being addressed once this fires.
        @param processed_text The cleaned and processed player input.
        @return True if processed_text contains any DIALOGUE_KEYWORDS phrase, else False.
        """
        return self._keyword_gate(processed_text, DIALOGUE_KEYWORDS)

    def _detect_help_intent(self, processed_text):
        """!
        @brief Checks processed input for the reserved "adam" persona name -- see
            ADAM_NAME_PATTERN's own module note for why this is checked ahead of everything
            else (including dialogue and item-interaction detection) rather than folded into
            either.
        @param processed_text The cleaned and processed player input.
        @return True if processed_text contains the whole word "adam" (any case), else False.
        """
        return bool(ADAM_NAME_PATTERN.search(processed_text))

    def _detect_removal_intent(self, processed_text):
        """!
        @brief Checks an ADaM-addressed message for REMOVAL_KEYWORDS -- see that constant's
            own module note for why this is a cheap local gate, not the actual decision (the
            LLM's own decide_entity_removal, AdHoc_Generation.py, is the real arbiter).
        @param processed_text The cleaned and processed player input (already known to have
            matched ADAM_NAME_PATTERN).
        @return True if processed_text contains any REMOVAL_KEYWORDS phrase, else False.
        """
        return self._keyword_gate(processed_text, REMOVAL_KEYWORDS)

    def _detect_creature_intent(self, processed_text):
        """!
        @brief Checks an ADaM-addressed message for CREATURE_KEYWORDS -- see that constant's
            own module note for why this is a cheap local gate, not the actual decision (the
            LLM's own generate_ad_hoc_creature, AdHoc_Generation.py, is the real arbiter).
        @param processed_text The cleaned and processed player input (already known to have
            matched ADAM_NAME_PATTERN).
        @return True if processed_text contains any CREATURE_KEYWORDS phrase, else False.
        """
        return self._keyword_gate(processed_text, CREATURE_KEYWORDS)

    def _detect_edit_intent(self, processed_text):
        """!
        @brief Checks an ADaM-addressed message for EDIT_KEYWORDS -- see that constant's own
            module note for why this is a cheap local gate, not the actual decision (the LLM's
            own decide_entity_edit, AdHoc_Generation.py, is the real arbiter).
        @param processed_text The cleaned and processed player input (already known to have
            matched ADAM_NAME_PATTERN).
        @return True if processed_text contains any EDIT_KEYWORDS phrase, else False.
        """
        return self._keyword_gate(processed_text, EDIT_KEYWORDS)

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

    def _on_item_catalog_updated(self, data):
        """!
        @brief Incrementally registers newly-created (or reload-restored) ad hoc entities into
            item_embeddings/item_indices, the same two-phrase-per-item ([name], [description])
            shape _on_rules_loaded already builds for the whole static catalog. Without this, an
            ad hoc entity (DM_Improvisation.py, AdHoc_Generation.py) would only ever be
            reachable on the one turn it's created (dispatched directly by name) -- any later
            reference (ex: "drop the stone") would miss map_to_item again, since NLPCore's own
            embeddings are otherwise only ever (re)built once, from "rules_loaded" (a reload
            doesn't republish that event -- DM_Persistence.py's load_game publishes this
            instead, once, as a batch, after restoring every saved ad hoc entity).
        @param data The "item_catalog_updated" payload ({"entities": [{"name",
            "description"}, ...]}).
        """
        entries = data.get("entities", [])
        if not entries:
            return

        new_phrases = []
        new_indices = []
        for entry in entries:
            name = entry.get("name")
            if not name:
                continue
            new_phrases.append(name)
            new_indices.append(name)
            description = entry.get("description", "")
            if description:
                new_phrases.append(description)
                new_indices.append(name)

        if not new_phrases:
            return

        new_embeddings = self.model.encode(new_phrases, convert_to_tensor=True)
        if self.item_embeddings is None:
            self.item_embeddings = new_embeddings
        else:
            self.item_embeddings = torch.cat([self.item_embeddings, new_embeddings], dim=0)
        self.item_indices.extend(new_indices)

        self.event_bus.publish("log_info", f"NLPCore: {len(new_phrases)} item phrases registered for {len(entries)} ad hoc item(s).")

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

    def _split_action_clauses(self, processed_text):
        """!
        @brief Splits processed_text into one or more independent action clauses on
            ACTION_CLAUSE_PATTERN, only ever reached once save/load, direction, item-
            interaction, and dialogue detection have all already missed (see _on_user_input) --
            by this point the input is squarely skill/ability territory, so splitting on
            "and"/"then" here doesn't risk cutting into item or dialogue phrasing those earlier
            tiers already had first refusal on. A plain single-action input (no "and"/"then"/
            punctuation at all) splits into exactly one clause -- the whole text, unchanged --
            so this is a strict generalization of the old single-match path, not a special
            case bolted on top of it: _on_user_input's own loop over this method's result
            behaves identically to the pre-multi-action code whenever it returns one clause.
        @param processed_text The cleaned and processed player input.
        @return A list of one or more non-empty, stripped clause strings, in input order.
        """
        clauses = [clause.strip() for clause in ACTION_CLAUSE_PATTERN.split(processed_text)]
        return [clause for clause in clauses if clause]

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