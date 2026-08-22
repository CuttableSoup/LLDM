"""!
@file Intent_Classification.py
@brief Pure, EventBus-independent intent classification for player input -- IntentClassifier
    resolves what a turn's raw text means (a meta-command, an item interaction, dialogue, a
    skill/ability turn, or nothing understood at all) and returns the ordered list of events
    to publish, without ever touching the EventBus itself. NLP_Core.py is the thin EventBus
    glue this module is built for -- same pure/glue split AdHoc_Generation.py is to
    DM_Improvisation.py, and NPC_Generation.py is to DM_NpcGeneration.py.

    IntentClassifier.classify() replaces NLP_Core.py's old _on_user_input -- see that method's
    former docstring (still recorded in CLAUDE.md's "Multiple actions"/"Ad hoc entity creation"
    sections) for why five unrelated whole-input concerns (save/load, ADaM, room direction,
    item-vs-dialogue-vs-skill classification, improvisation fallback) resolve in this exact
    priority order. This module makes that order the *interface*: IntentMatcher is the one
    seam a caller has to satisfy (real embedding matching in production, a canned stub in
    tests), everything else -- keyword tables, gate order, clause splitting -- is this
    module's own implementation, invisible to callers.
"""

import re

# AdHoc_Generation.py is the one project-internal import this module makes -- it's pure/
# DMCore-independent (no DMCore/game-state coupling of its own), so importing its shared
# intent-vocabulary constants doesn't compromise this module's own independence. See
# IMPROVISABLE_INTENTS, below, for what these three are actually used for here.
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
# list (checked by test_keyword_tables_never_collide_with_a_skill_keyword, below).
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
# natural phrasing -- CLOSE_KEYWORDS' "close the " is checked first (see item_intent_gates)
# and would swallow it as a "close" intent instead.
ADVANCE_KEYWORDS = ("advance", "move closer", "approach", "move toward", "move in", "step closer")
RETREAT_KEYWORDS = ("retreat", "back away", "back off", "fall back", "step back", "withdraw", "move away")
# Party positioning (see DM_Core._resolve_formation_intent / CLAUDE.md's "Party formation") --
# like advance/retreat above, these act on the scene (specifically, whichever party member is
# named, or the whole party if none is) rather than a named item, so no map_to_item lookup ever
# runs for them either; unlike advance/retreat, DMCore -- not this module -- is what figures out
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

# Partitions item_intent_gates' own return set for classify()'s per-clause turn
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
# classify()'s own "improvisation_requested" note) -- includes "trade" (ex: "buy a rope"
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


class IntentMatcher:
    """!
    @brief The one seam IntentClassifier depends on -- everything embedding-based
        (skill/item/target matching) is real ML inference in production and a canned stub in
        tests. Not a runtime-enforced interface (this project has no third-party Protocol
        dependency beyond typing, and duck typing is enough here) -- purely documentation of
        the three methods a matcher must provide, plus the two catalog-maintenance calls.
        SentenceTransformerMatcher (NLP_Core.py) is the production adapter; FakeMatcher
        (test_unit.py) is the test adapter -- two real adapters justify this seam existing at
        all, not a hypothetical one authored just in case.
    """

    def on_rules_loaded(self, data):
        """!@brief Builds skill/item/target embeddings from a fresh "rules_loaded" payload."""
        raise NotImplementedError

    def register_item(self, name, description):
        """!@brief Incrementally registers one ad hoc item's name/description for map_to_item."""
        raise NotImplementedError

    def map_to_action(self, processed_text):
        """!@brief Returns (skill_name, score); skill_name is None below confidence."""
        raise NotImplementedError

    def map_to_item(self, processed_text):
        """!@brief Returns (item_name, score); item_name is None below confidence."""
        raise NotImplementedError

    def map_to_target(self, processed_text):
        """!@brief Returns (entity_name, score); entity_name is None below confidence."""
        raise NotImplementedError


def process_input(player_input):
    """!
    @brief Cleans raw player input: strips whitespace, lowercases, and drops one leading
        filler prefix ("I want to ", "I try to ", ...) if present.
    @param player_input The raw string from "user_input_submitted".
    @return The processed text.
    """
    processed_text = player_input.strip().lower()

    prefixes = ["i want to ", "i try to ", "i'll try to ", "i will try to ", "i am going to ", "i'm going to ", "i "]
    for prefix in prefixes:
        if processed_text.startswith(prefix):
            processed_text = processed_text[len(prefix):]
            break

    return processed_text


def split_action_clauses(processed_text):
    """!
    @brief Splits processed_text into one or more independent action clauses on
        ACTION_CLAUSE_PATTERN, only ever reached once save/load, direction, item-interaction,
        and dialogue detection have all already missed (see classify()) -- by this point the
        input is squarely skill/ability territory, so splitting on "and"/"then" here doesn't
        risk cutting into item or dialogue phrasing those earlier tiers already had first
        refusal on. A plain single-action input (no "and"/"then"/punctuation at all) splits
        into exactly one clause -- the whole text, unchanged -- so this is a strict
        generalization of the old single-match path, not a special case bolted on top of it.
    @param processed_text The cleaned and processed player input.
    @return A list of one or more non-empty, stripped clause strings, in input order.
    """
    clauses = [clause.strip() for clause in ACTION_CLAUSE_PATTERN.split(processed_text)]
    return [clause for clause in clauses if clause]


def detect_item_intent(processed_text):
    """!
    @brief Checks processed input for an item-interaction verb, ahead of skill matching.
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


def _keyword_gate(processed_text, keywords):
    """!@brief Shared "does this phrase contain any of these keyword phrases" check."""
    return any(keyword in processed_text for keyword in keywords)


def detect_dialogue_intent(processed_text):
    """!@brief True if processed_text contains any DIALOGUE_KEYWORDS phrase."""
    return _keyword_gate(processed_text, DIALOGUE_KEYWORDS)


def detect_help_intent(processed_text):
    """!@brief True if processed_text contains the whole word "adam" (any case)."""
    return bool(ADAM_NAME_PATTERN.search(processed_text))


def detect_removal_intent(processed_text):
    """!@brief True if an ADaM-addressed message contains a REMOVAL_KEYWORDS phrase."""
    return _keyword_gate(processed_text, REMOVAL_KEYWORDS)


def detect_creature_intent(processed_text):
    """!@brief True if an ADaM-addressed message contains a CREATURE_KEYWORDS phrase."""
    return _keyword_gate(processed_text, CREATURE_KEYWORDS)


def detect_edit_intent(processed_text):
    """!@brief True if an ADaM-addressed message contains an EDIT_KEYWORDS phrase."""
    return _keyword_gate(processed_text, EDIT_KEYWORDS)


def detect_direction(processed_text):
    """!@brief Returns "forward"/"back"/"left"/"right", or None -- see DIRECTION_PHRASES."""
    for direction, phrases in DIRECTION_PHRASES.items():
        if any(phrase in processed_text for phrase in phrases):
            return direction
    return None


def detect_save_load_intent(processed_text):
    """!
    @brief Checks processed input for a "save"/"load" command, ahead of item and skill
        matching -- a meta-command, not an in-fiction action. The slot name is arbitrary
        player-chosen text with no catalog to match against, so it's extracted by
        prefix-stripping instead.
    @param processed_text The cleaned and processed player input.
    @return (intent, slot_name) where intent is "save"/"load"/None. If a prefix matched but
            nothing followed it (empty slot name), returns (None, None) instead -- falls
            through to normal skill matching rather than saving/loading to a blank name.
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


class IntentClassifier:
    """!
    @brief Resolves what a whole turn's raw player input means, and returns the ordered list
        of EventBus events (as plain {"event", "payload"} dicts) that a caller should publish
        to realize it -- never publishes anything itself. Own state is limited to the one
        IntentMatcher seam (embedding-based skill/item/target matching); every other decision
        here is keyword/regex logic over processed text, testable without a matcher at all
        wherever a gate never reaches map_to_action/map_to_item/map_to_target.

        classify()'s own gate order is two levels, mirroring the real control flow rather than
        forcing everything into one flat list: an outer sequence of whole-input gates
        (save/load -> ADaM/help -> room direction -> [per-clause item pass] -> dialogue-if-
        nothing-claimed -> [per-clause skill pass]), and a per-clause gate list the item pass
        runs for each clause (exempt-movement -> no-lookup-item -> item-lookup -> defer). Final
        aggregation (turn vs. improvisation-fallback vs. not-understood) is its own step, since
        it's genuine accumulation logic over every clause's outcome, not a gate itself.
    """

    def __init__(self, matcher):
        """!
        @param matcher An IntentMatcher adapter -- SentenceTransformerMatcher in production,
            FakeMatcher in tests.
        """
        self.matcher = matcher

    def on_rules_loaded(self, data):
        """!@brief Forwards a "rules_loaded" payload to the matcher to build its embeddings."""
        self.matcher.on_rules_loaded(data)

    def register_item(self, name, description):
        """!@brief Forwards one ad hoc item's name/description to the matcher's own catalog."""
        self.matcher.register_item(name, description)

    def classify(self, raw_input):
        """!
        @brief Classifies one whole turn of raw player input. See this class's own docstring
            for the two-level gate order; see CLAUDE.md's "Multiple actions" and "Ad hoc entity
            creation and removal" sections for why that order is what it is.
        @param raw_input The raw string from "user_input_submitted".
        @return (processed_text, events) -- processed_text for the caller's own "Processing
            player input" log line, and events a list of one or more {"event", "payload"}
            dicts to publish, in order. Almost always length 1; more than one only when an
            EXEMPT_ITEM_INTENTS clause (ex: "retreat") shares the input with a real turn (ex:
            "attack the wolf and retreat" publishes the retreat's own item_interaction_detected
            immediately, then a separate turn_detected for the attack).
        """
        processed = process_input(raw_input)
        events = []

        save_load_intent, slot_name = detect_save_load_intent(processed)
        if save_load_intent:
            events.append({"event": f"{save_load_intent}_requested", "payload": {"slot": slot_name}})
            return processed, events

        if detect_help_intent(processed):
            events.append({"event": "help_detected", "payload": {
                "input": processed,
                "removal_candidate": detect_removal_intent(processed),
                "creature_candidate": detect_creature_intent(processed),
                "edit_candidate": detect_edit_intent(processed),
            }})
            return processed, events

        direction = detect_direction(processed)
        if direction:
            # A different axis from "advance"/"retreat" below -- see DIRECTION_PHRASES'
            # module note. No item name to resolve at all, so map_to_item never runs for
            # this either; DMCore._find_room_exit is what actually decides whether this
            # direction resolves to a real exit from the player's current band.
            events.append({"event": "item_interaction_detected", "payload": {
                "intent": "move", "item_name": None, "direction": direction,
                "input": processed, "score": None,
            }})
            return processed, events

        turn_clauses, remaining_clauses, found_exempt, unmatched_item_verbs = self._classify_item_pass(
            processed, events,
        )

        # Dialogue is checked once, on the whole input, only once the item pass found nothing
        # at all (no item interaction, no exempt movement/formation clause) -- the same
        # priority the old single-clause code already gave item intents over dialogue.
        if not turn_clauses and not found_exempt and detect_dialogue_intent(processed):
            events.append({"event": "dialogue_detected", "payload": {"input": processed, "score": None}})
            return processed, events

        best_score = self._classify_skill_pass(remaining_clauses, turn_clauses)

        self._finalize(processed, turn_clauses, found_exempt, unmatched_item_verbs, best_score, events)
        return processed, events

    def _classify_item_pass(self, processed, events):
        """!
        @brief Pass 1: item-interaction classification, per clause. EXEMPT_ITEM_INTENTS
            (movement/directing the party) are appended to events immediately, in clause
            order, and never join the shared turn. Everything else that resolves as an item
            interaction (NO_ITEM_LOOKUP_INTENTS' "open"/"close", or any other intent with a
            confidently-matched item_name) joins turn_clauses -- these *do* cost a turn action
            (see DM_Core.py's "Multiple actions"), just never a dice roll. A clause that
            doesn't resolve as an item interaction at all is left for the skill pass.
        @param processed The whole processed input (used only for exempt-intent payloads,
            which have always carried the whole input rather than just their own clause).
        @param events The classify()-owned events list; exempt clauses are appended directly.
        @return (turn_clauses, remaining_clauses, found_exempt, unmatched_item_verbs) --
            turn_clauses is the list of {"kind": "item", ...} entries so far; remaining_clauses
            is every clause this pass didn't claim at all, left for the skill pass;
            unmatched_item_verbs is every recognized item verb whose own map_to_item call found
            nothing, tracked separately for the improvisation fallback (see _finalize) -- note
            a clause can land in both remaining_clauses and unmatched_item_verbs at once (ex:
            "give" with no matching item name is still tried against skill matching, in case
            it's coincidentally a legitimate skill phrase too).
        """
        turn_clauses = []
        remaining_clauses = []
        found_exempt = False
        unmatched_item_verbs = []

        for clause in split_action_clauses(processed):
            clause_intent = detect_item_intent(clause)
            if clause_intent in EXEMPT_ITEM_INTENTS:
                found_exempt = True
                events.append({"event": "item_interaction_detected", "payload": {
                    "intent": clause_intent, "item_name": None, "input": processed, "score": None,
                }})
                continue
            if clause_intent in NO_ITEM_LOOKUP_INTENTS:
                turn_clauses.append({"kind": "item", "intent": clause_intent, "item_name": None})
                continue
            if clause_intent:
                item_name, _item_score = self.matcher.map_to_item(clause)
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

        return turn_clauses, remaining_clauses, found_exempt, unmatched_item_verbs

    def _classify_skill_pass(self, remaining_clauses, turn_clauses):
        """!
        @brief Pass 2: skill/ability matching for whatever clauses the item pass didn't
            already claim. Appends matched clauses directly onto turn_clauses.
        @param remaining_clauses The item pass's own leftover clause list.
        @param turn_clauses The item pass's own accumulated list -- matched skill/ability
            entries are appended here directly.
        @return best_score, the highest confidence score seen across every clause tried, for
            action_not_understood's own payload if nothing else claims the turn.
        """
        best_score = 0.0
        for clause in remaining_clauses:
            clause_skill, clause_score = self.matcher.map_to_action(clause)
            best_score = max(best_score, clause_score)
            if not clause_skill:
                continue
            action = {"kind": "action", "skill": clause_skill, "score": clause_score}
            # A confidently-matched creature name (ex: "attack the second wolf") is attached
            # as a target hint alongside the matched skill -- unlike item/save-load intent,
            # this never gates or replaces skill matching, it only enriches the same turn
            # entry. DMCore is what actually decides whether to honor it. Resolved per clause,
            # not against the whole input, so "attack the orc and cast a ward on thane" can
            # redirect each action at its own named target.
            target_name, _target_score = self.matcher.map_to_target(clause)
            if target_name:
                action["target"] = target_name
            turn_clauses.append(action)
        return best_score

    def _finalize(self, processed, turn_clauses, found_exempt, unmatched_item_verbs, best_score, events):
        """!
        @brief Decides the turn's final event once both passes have run: a merged
            turn_detected if anything claimed the turn, else an improvisation_requested
            fallback if a recognized-but-unmatched item verb is available, else
            action_not_understood -- unless an exempt clause already claimed the whole input
            (found_exempt with nothing else), in which case nothing further publishes at all.
        @param events The classify()-owned events list; the final decision is appended here.
        """
        if turn_clauses:
            # Always published this way, even for the overwhelmingly common single-clause,
            # single-kind case -- see DM_Core.py's own "Multiple actions" docstring for why
            # the whole downstream pipeline (DMCore, LLMCore) is built around one consistent
            # shape rather than special-casing N=1 or a single clause kind.
            events.append({"event": "turn_detected", "payload": {"clauses": turn_clauses, "input": processed}})
        elif not found_exempt and unmatched_item_verbs:
            # The whole turn would otherwise resolve to nothing at all, but at least one clause
            # was a recognized item verb naming something that just doesn't exist yet -- last
            # resort before giving up: DM_Improvisation.py's own generate_ad_hoc_item gets a
            # chance to decide whether that's plausible to conjure into the scene (ex: "pick up
            # a stone"). Only the first candidate is used -- extending this into a genuinely
            # multi-clause improvisation attempt is out of scope for now.
            candidate = unmatched_item_verbs[0]
            events.append({"event": "improvisation_requested", "payload": {
                "intent": candidate["intent"], "phrase": candidate["phrase"], "input": processed,
            }})
        elif not found_exempt:
            # Below confidence_threshold on every remaining clause, and the item pass found
            # nothing either: publish this instead of staying silent, so the player gets some
            # response rather than the app appearing to stall.
            events.append({"event": "action_not_understood", "payload": {"input": processed, "score": best_score}})
