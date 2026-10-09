"""!
@file Intent_Classification.py
@brief Pure, EventBus-independent intent classification for player input -- IntentClassifier
    resolves what a turn's raw text means (a meta-command, an item interaction, dialogue, a
    skill/ability turn, or nothing understood at all) and returns the ordered list of events
    to publish, without ever touching the EventBus itself. NLP_Core.py is the thin EventBus
    glue this module is built for -- same pure/glue split AdHoc_Generation.py is to
    DM_Improvisation.py, and NPC_Generation.py is to DM_NpcGeneration.py.

    IntentClassifier.classify() replaces NLP_Core.py's old _on_user_input -- see that method's
    former docstring (still recorded in docs/action-resolution.md's "Multiple actions" and
    docs/adam-improvisation.md's "Ad hoc entity creation and removal" sections) for why six
    unrelated whole-input concerns (save/load, ADaM, scene query, room direction,
    item-vs-dialogue-vs-skill classification, improvisation fallback) resolve in this exact
    priority order. This module makes that order the *interface*: IntentMatcher is the one
    seam a caller has to satisfy (real embedding matching in production, a canned stub in
    tests), everything else -- keyword tables, gate order, clause splitting -- is this
    module's own implementation, invisible to callers.
"""

from collections import namedtuple
import re

# intents/improvisation.py's shared intent-vocabulary constants are plain data with no DMCore/
# game-state coupling, so importing them doesn't compromise this module's own independence. See
# IMPROVISABLE_INTENTS, below, for what these three are actually used for here.
from intents.advance_retreat import TOWARD_PATTERN
from intents.registry import MATCHES
from intents.travel import LEAVE_PATTERN
from intents.improvisation import GROUND_AWARE_INTENTS, PLAYER_CENTRIC_INTENTS, TARGET_CENTRIC_INTENTS

# The free-standing intents' keyword phrases, regexes and semantic-router prototypes live with
# the intent that owns them (intents/<name>.py's MATCH, collected in intents/registry.py's
# MATCHES, in gate order) -- these module-level names are views of that manifest, kept so the
# rest of this file (and its tests) can keep reading them by name.
LORE_KEYWORDS = MATCHES["lore_check"].keywords
ADVANCE_KEYWORDS = MATCHES["advance"].keywords
RETREAT_KEYWORDS = MATCHES["retreat"].keywords
FORMATION_BEHIND_KEYWORDS = MATCHES["formation_behind"].keywords
FORMATION_ABREAST_KEYWORDS = MATCHES["formation_abreast"].keywords
SPEAK_LANGUAGE_KEYWORDS = MATCHES["speak_language"].keywords
REST_KEYWORDS = MATCHES["rest"].keywords
MOUNT_KEYWORDS = MATCHES["mount"].keywords
DISMOUNT_KEYWORDS = MATCHES["dismount"].keywords
HITCH_KEYWORDS = MATCHES["hitch"].keywords
UNHITCH_KEYWORDS = MATCHES["unhitch"].keywords
TRAVEL_KEYWORDS = MATCHES["travel"].keywords


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
TAKE_KEYWORDS = ("take ", "grab ", "pick up", "loot ", "snatch ")
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
# Attempting a recipe's own [entity.craft] check (DM_Crafting.py) against the named result
# item -- map_to_item resolves item_name the same way it does for "use"/"take"/..., matching
# over every supertype == "object" entity regardless of whether it's ever been instanced
# anywhere (a pure recipe/catalog entry). Deliberately no bare "make " here -- same reasoning
# CLOSE_KEYWORDS/a bare "use " already avoid: "make" is far too generic a verb to safely route
# every "make ..." phrase into crafting.
CRAFT_KEYWORDS = ("craft ", "craft a ", "craft an ", "craft the ", "brew ", "forge a ", "forge an ")
OPEN_KEYWORDS = ("open the ", "open it")
CLOSE_KEYWORDS = ("close the ", "close it", "shut the ", "shut it")
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

# Opening words that mark text as something other than a declared action -- a question ("can",
# "does", "why"), a hypothetical ("if", "maybe", "wait"), a suggestion ("let's", "we"), or a
# remark about someone/something else ("you", "it's", "this"). Consulted by map_to_action's
# two FALLBACK paths (see opens_like_an_action), never its direct semantic match, and by
# detect_implicit_speech while a conversation is running. Both map_to_action
# fallbacks work from a fragment of the input rather than the input as a whole, so they're the
# ones that turn an ordinary word into a bogus skill roll -- "let's find a room" (observation,
# keyword "find"), "shall we slip away" (escape, "slip"), "is your forge really" (forgery, an
# alternate phrasing truncated at " that "). A player declaring an action leads with the action
# ("find the dockmaster", "try to convince the guard", "i'll bargain with her over the cost"),
# which is why this is a list of what disqualifies rather than of what qualifies: an open-ended
# verb vocabulary is exactly what skills.toml's own data-driven keywords already are.
#
# Measured against 219 real logged inputs plus 66 targeted ones (plain actions, social-skill
# attempts, banter containing skill keywords): the keyword-fallback path alone produced 39
# conversation rolls against 22 genuine action rolls, with fully overlapping scores (0.22-0.48
# vs 0.21-0.47) -- so no keyword_fallback_floor could separate them, while this opening-word
# test kept every genuine one.
NON_ACTION_OPENERS = frozenset({
    "am", "is", "are", "was", "were", "do", "does", "did", "can", "could", "should", "would",
    "will", "shall", "may", "might", "must", "have", "has", "had",
    "why", "how", "what", "where", "who", "whom", "whose", "when", "which", "whether",
    "what's", "whats", "where's", "how's", "who's", "why's", "when's",
    "if", "so", "but", "or", "since", "because", "though", "although", "unless", "wait",
    "maybe", "perhaps", "like", "well", "honestly",
    "let's", "lets", "we", "we're", "we'll", "we've", "you", "you're", "you've", "your",
    "he", "she", "they", "it", "it's", "its", "this", "that", "these", "those", "there", "here",
    "i'd", "i'm", "im", "i've",
    # A sentence opening on an article describes something rather than doing it. Found by
    # playtest: "the wall thing i saw before" cast wall of fire, and "then the rules are
    # incomplete..." rolled psionics into a bystander.
    "the", "a", "an",
})
# Stripped before the opener is read, so "i'll bargain with her" and a mid-clause sentence
# starting "i study the pattern" both open on their real verb. process_input only strips a
# leading "i " from the very start of the whole input.
FIRST_PERSON_OPENERS = frozenset({"i", "i'll", "ill"})
# Skipped the same way, since they say nothing about whether what follows is talk or an action:
# "and also what's the action economy?" opens on "what's", "oh, kick him" on "kick".
LEADING_FILLER_WORDS = frozenset({"and", "also", "then", "oh", "ok", "okay", "ugh", "hey", "um", "uh", "ah"})
# "i bet the rooms here are lovely" is a remark, "bet ten gold on red" a wager. Found by playtest:
# the idiom rolled gambling seven times in sixty turns. Only the word after "bet" tells them apart.
# A clause opening on one of these is body language, not an attempt at anything -- never sent to
# skill matching (see _classify_skill_pass). Found by playtest: "(bows head dramatically)" rolled
# missiles (a bow is a weapon), "(grins savagely)" nearly rolled miracles.
GESTURE_VERBS = frozenset({
    "bow", "bows", "bowing", "nod", "nods", "nodding", "grin", "grins", "grinning", "smile",
    "smiles", "smiling", "shrug", "shrugs", "shrugging", "laugh", "laughs", "laughing", "sigh",
    "sighs", "sighing", "wink", "winks", "winking", "chuckle", "chuckles", "chuckling", "smirk",
    "smirks", "smirking", "frown", "frowns", "frowning", "blush", "blushes", "blushing",
})
# A skill matched in the action half of a mixed speech/action line must clear this to split it off
# (see IntentClassifier._split_speech_from_action) -- the same bar map_to_action's own direct
# match uses, so a keyword-fallback hit never takes a turn away from talk.
MIXED_ACTION_MIN_SCORE = 0.5
# A clause opening on one of these is only the tag on a quoted line ('i yell "hey!"') -- dropped
# from the action half when quoted speech is split off (see _split_quoted_speech), so the tag never
# rolls intimidation or performance beside the real action.
SPEECH_TAG_VERBS = frozenset({
    "say", "says", "said", "yell", "yells", "shout", "shouts", "scream", "screams", "cry", "cries",
    "call", "calls", "roar", "roars", "bellow", "bellows", "snarl", "snarls", "growl", "growls",
    "hiss", "hisses", "mutter", "mutters", "whisper", "whispers", "snicker", "snickers", "sneer",
    "sneers", "taunt", "taunts", "exclaim", "exclaims", "add", "adds", "reply", "replies",
    "saying", "yelling", "shouting", "screaming", "crying", "calling", "roaring", "bellowing",
    "snarling", "growling", "hissing", "muttering", "whispering", "snickering", "sneering",
    "taunting", "exclaiming", "adding", "replying",
}) | GESTURE_VERBS
# A sentence opening on one of these is a fragment of the talk around it, never the action half
# of a mixed line (see _split_speech_from_action). Found by playtest: "a name, man. you gotta give
# me a name" rolled appraise on "a name, man." (articles, the other fragment openers, are already
# NON_ACTION_OPENERS). Kept out of NON_ACTION_OPENERS, which also gates the skill fallbacks on
# whole inputs, where "fine, i'll take it" is still an action.
SPEECH_FRAGMENT_OPENERS = frozenset({
    "no", "nah", "nope", "yes", "yeah", "yep", "sure", "fine", "right", "okay", "ok",
})
# "i'm <verb>ing" words that describe a state rather than declare an action (opens_like_an_action).
STATIVE_PROGRESSIVES = frozenset({
    "feeling", "thinking", "starving", "wondering", "hoping", "kidding", "joking", "saying",
    "asking", "telling", "being", "listening", "waiting", "bleeding", "dying", "freezing",
})
BET_REMARK_FOLLOWERS = frozenset({
    "you", "your", "that", "that's", "the", "there", "there's", "it", "it's", "this", "we",
    "they", "he", "she", "i", "everyone", "nobody",
})


def opens_like_an_action(text):
    """!
    @brief Whether text opens the way a declared action does -- see NON_ACTION_OPENERS.
    @param text A processed (lowercased) sentence or fragment.
    @return False if its first word, after any FIRST_PERSON_OPENERS, is a NON_ACTION_OPENERS
        word; True otherwise (including empty text, which no fallback can match anyway).
    """
    words = re.findall(r"[a-z']+", text)
    while words and (words[0] in FIRST_PERSON_OPENERS or words[0] in LEADING_FILLER_WORDS):
        words = words[1:]
    if len(words) > 1 and words[0] == "bet" and words[1] in BET_REMARK_FOLLOWERS:
        return False
    if len(words) > 1 and words[0] in ("i'm", "im"):
        # "i'm knocking this stall over" declares an action; "i'm starving"/"i'm sure" remark.
        # Found by playtest once unmarked speech reached anyone present: the bare "i'm" opener
        # sent "grab everything! i'm taking it all!" to dialogue.
        verb = words[2] if words[1] in ("just", "really", "now", "still") and len(words) > 2 else words[1]
        return verb.endswith("ing") and verb not in STATIVE_PROGRESSIVES
    return not words or words[0] not in NON_ACTION_OPENERS

# A double-quoted span of at least a few characters -- see speech_quotes. Double quotes only: an
# apostrophe is a contraction far more often than a quotation mark.
QUOTED_SPEECH_PATTERN = re.compile(r'"([^"]{2,})"')
# A parenthesised or *starred* span: a stage direction ("(i wink at her.)", "*i wink at her*"), by the
# convention players write in.
STAGE_DIRECTION_PATTERN = re.compile(r"\(([^()]+)\)|\*([^*]+)\*")


def stage_directions(text):
    """!
    @brief The stage directions in text: (start, end, content) for every starred span, and every
        parenthesised one that reads as an action -- it opens on "i" or ends a sentence ("(i wink.)").
        A plain aside ("walk to the docks (it's far)") is not one, so a normal command with brackets
        is left whole.
    @param text Processed input.
    @return A list of (start, end, content), in order.
    """
    found = []
    for match in STAGE_DIRECTION_PATTERN.finditer(text or ""):
        content = (match.group(1) or match.group(2) or "").strip()
        first = re.match(r"[a-z']+", content)
        starred = match.group(0).startswith("*")
        if starred or (first and first.group() in FIRST_PERSON_OPENERS) or content.endswith((".", "!")):
            found.append((match.start(), match.end(), content))
    return found


def _blank_spans(text, spans, keep=False):
    """!@brief text with the given (start, end, ...) spans blanked to spaces, or everything else if keep."""
    inside = [False] * len(text)
    for start, end, *_ in spans:
        for index in range(start, end):
            inside[index] = True
    return "".join(char if inside[index] == keep else " " for index, char in enumerate(text))


def mask_talk(text):
    """!
    @brief What the player DOES in text, for the gates that act on the world: spoken quotes blanked
        (mask_speech_quotes) and, when the line has stage directions with talk beside them, the talk
        too -- only the stage directions are left. 'Relax. You could use a solid night's rest.' beside
        '*i lean in*' is not a request to rest. Found by playtest, where it ran one.
    @param text Processed input.
    @return The same text, the same length.
    """
    text = mask_speech_quotes(text)
    spans = stage_directions(text)
    if spans and re.search(r"[a-z]", _blank_spans(text, spans)):
        return _blank_spans(text, spans, keep=True)
    return text


def speech_quotes(text):
    """!
    @brief The quoted spans in text that read as a spoken line: three or more words, or one
        ending in sentence punctuation ("Hi!", "Wait."). A shorter bare span is a scare quote or
        a term: found by playtest, 'ask them what the real "currents" are' was sent to the
        nearest NPC as the player saying just "currents".
    @param text Raw or processed input.
    @return The spoken spans, stripped, in order.
    """
    return [quote.strip() for quote in QUOTED_SPEECH_PATTERN.findall(text or "") if _reads_as_speech(quote)]


def _reads_as_speech(quote):
    """!@brief Whether a quoted span is a spoken line (see speech_quotes) rather than a scare quote."""
    quote = quote.strip()
    return len(quote.split()) >= 3 or bool(re.search(r"[.!?,]$", quote))


def mask_speech_quotes(text):
    """!
    @brief text with every spoken quote (and its quotation marks) blanked to spaces, the same length
        so positions still line up. What a player says is not what they do: found by playtest, 'I
        let my eyes drift over his chest. "You look like you need a rest."' ran a rest (the clock
        advanced and the character healed) on the word inside the quote, before dialogue was ever
        considered. The verb gates that act on the world read this, not the raw line.
    @param text Processed input.
    @return The same text with the spoken quotes blanked.
    """
    return QUOTED_SPEECH_PATTERN.sub(
        lambda match: " " * len(match.group(0)) if _reads_as_speech(match.group(1)) else match.group(0), text or "",
    )

# extract_address_phrase's own three word lists (see that function for why a keyword-shaped
# mechanism is acceptable here and nowhere else in this file).
# A remainder OPENING with one of these means the player addressed no one nameable -- either
# they asked the room a question ("ask about the weather") or they used a pronoun for someone
# already in play ("tell them to back off"). Checked before articles are stripped, since these
# are exactly the words that can't be preceded by one.
ADDRESS_NON_ADDRESSEES = frozenset({
    "about", "what", "whats", "where", "wheres", "why", "how", "when", "whether", "if", "that",
    "me", "them", "him", "her", "it", "us", "you", "everyone", "anyone", "someone", "myself",
})
ADDRESS_ARTICLES = frozenset({"the", "a", "an", "this", "my", "his", "her", "their", "our"})
# Where the addressee ends and the rest of the sentence begins.
ADDRESS_TERMINATORS = frozenset({
    "about", "what", "where", "why", "how", "when", "whether", "if", "that", "to", "for",
    "and", "by", "near", "at", "in", "with", "over", "from", "behind", "beside",
})
# Punctuation to shave off each candidate word -- the player's own typing, not a token.
ADDRESS_STRIP_CHARS = ".,;:!?\"'"
# A crowd's worth of adjectives is still one person ("the old man by the fire"); past three
# words it stops being a way of naming someone and starts being a sentence.
MAX_ADDRESS_WORDS = 3
# extract_item_phrase's own word lists -- "of" stays out of the terminators ("a bag of figs").
ITEM_PHRASE_ARTICLES = ADDRESS_ARTICLES | {"some", "that", "those", "these", "your", "its"}
ITEM_PHRASE_TERMINATORS = frozenset({
    "from", "to", "for", "and", "then", "with", "off", "out", "at", "in", "on", "into", "onto",
    "so", "but", "while", "before", "after", "near", "by", "beside", "behind", "under", "without",
})
MAX_ITEM_PHRASE_WORDS = 4

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

# A free-standing, read-only "what's around me" question ("what do i see", "who is here") --
# checked as its own whole-input reserved gate, the same tier as ADAM_NAME_PATTERN just below it
# in classify()'s own gate order, so it never needs "adam" said aloud (unlike help_detected) but
# also never falls through to EXAMINE_KEYWORDS/item-interaction detection (which, on no matching
# item name, would otherwise reach DM_Improvisation.py's ad hoc item generation and invent
# something not actually in the scene -- exactly the "nothing to back it up" failure mode this
# intent exists to close). Answered from the same kind of live ground-truth snapshot ADaM's own
# help_detected already gathers (see DM_Help.py), but through its own event/handler rather than
# _on_help_detected: this channel is strictly read-only (never runs REMOVAL_KEYWORDS/
# CREATURE_KEYWORDS/EDIT_KEYWORDS' own higher-risk mutation checks), and is answered in the
# ordinary in-fiction Game Master voice rather than ADaM's own out-of-character persona (see
# LLM_Core.py's generate_scene_query_response). Deliberately long, distinguishing phrases rather
# than anything built on a bare "look"/"search"/"spot"/"notice"/"see"/"find" -- observation's own
# skills.toml keywords list every one of those as a single word, so a genuine "search the room
# for hidden traps" (an actual perception check, meant to roll dice) must never be swallowed here
# first the way a bare "look"/"see" would risk.
SCENE_QUERY_KEYWORDS = (
    "what do i see", "what can i see", "what all do i see",
    "who is here", "who's here", "who is around", "who's around",
    "what's in the room", "what is in the room", "what's here", "what is here",
    "describe the room", "describe my surroundings", "describe the scene",
)

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


# The one intent name that is never published -- a deliberate "none of the above" class for
# map_to_intent's own argmax (see INTENT_PROTOTYPES). Leading underscore so it can never collide
# with a real intent string in HANDLERS (intents/registry.py).
OTHER_INTENT = "_other"

# Semantic backstop for the keyword gates above, scored by the same embedding matcher skill/item/
# target matching already uses (map_to_intent) and consulted ONLY at _finalize's own give-up point
# -- so it can never shadow a skill, item, or dialogue match that already succeeded, which is the
# entire safety argument for routing this way at all rather than widening the keyword tables.
# The gates stay as precise fast paths; this only ever converts what would have been an
# action_not_understood (or, for a recognized-but-unmatched item verb, an ad hoc improvisation
# attempt) into a real intent.
#
# Why this exists: a keyword table can only ever list phrasings someone thought of. SCENE_QUERY_
# KEYWORDS has "who is here" but not "who all is here"; TRAVEL_KEYWORDS has "head to " but not
# "head into" -- trivial paraphrases to a human, total misses to a substring check, and the tail
# of them doesn't converge no matter how many get added.
#
# PHRASES MUST BE AUTHORED POST-process_input: lowercased, and with no leading filler prefix,
# since that function strips one ("i want to ", "i ", ...) before anything here is ever scored
# against. "head into the tavern", never "i head into the tavern" -- an unstripped prefix silently
# embeds a phrase the matcher will never see the equivalent of.
#
# OTHER_INTENT is load-bearing, not filler. Without a negative class, argmax picks one of the
# real intents for literally every input on earth, leaving an absolute cosine cutoff as the only
# thing standing between an ordinary action and a confidently mis-routed one; with it, the
# decision is discriminative and the threshold is only a backstop. Its phrases are deliberately
# ordinary skill/item actions -- exactly what reaches _finalize having merely scored below
# confidence_threshold, rather than anything exotic.
#
# Scope: read-only/low-stakes intents only. "rest" and "lore_check" are the two members with real
# side effects on a false positive (rest advances the block clock; lore_check rolls dice) -- both
# are still bounded and player-visible, unlike an item verb that could silently give away or drop
# something, which is why no item-named intent is routed here.
INTENT_PROTOTYPES = {
    # "look around"/"look over" phrasings belong here even though SCENE_QUERY_KEYWORDS
    # deliberately excludes a bare "look"/"search" -- that exclusion exists because a substring
    # gate runs BEFORE skill matching and would swallow a genuine perception check ("search the
    # room for hidden traps", an actual observation roll). This runs AFTER skill matching has
    # already declined, so the check it was protecting has had its shot and lost; excluding them
    # here bought nothing and left "look around the room" and "take a look around" stranded in
    # ad hoc item generation, being asked to conjure "a look around" as a physical object.
    "scene_query": (
        "what is around me", "what do i see", "who all is here", "who else is in the room",
        "describe my surroundings", "what does this place look like", "what is nearby",
        "look around the room", "look over this place", "have a look about the area",
        "take a look at this place", "take stock of the area",
    ),
    # Every free-standing intent that authors prototypes (intents/<name>.py's PROTOTYPES).
    **{name: match.prototypes for name, match in MATCHES.items() if match.prototypes},
    # Greetings and address-someone phrasings earn their place here as much as the action ones
    # do: "hey there innkeeper" scored 0.56 against travel's own "step inside the inn" purely on
    # "inn"/"innkeeper" before these existed -- a confident mis-route on an input whose correct
    # answer is "no action at all". Whatever reaches _finalize is what this bucket has to
    # cover, and greetings demonstrably reach it.
    OTHER_INTENT: (
        "attack the guard with my sword", "pick the lock on the chest",
        "search the room for hidden traps", "give the sword to anne",
        "cast a healing spell on thane", "climb the wall", "hide in the shadows",
        "persuade the merchant to lower his price", "drink the healing potion",
        "throw a dagger at the wolf",
        "good day to you shopkeeper", "hello there friend", "greetings traveler",
        "good morning to you", "hey you over there", "ask the guard about the road",
    ),
}

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
# DM_Core.py) despite needing no item lookup, the same way "give"/"take"/etc. do. "lore_check" is
# the one exemption granted despite actually rolling dice (Combat_Actions.py's
# _resolve_lore_check_intent) -- every other member here is free *because* it's diceless; this
# one is free by deliberate design instead, since a mid-fight Knowledge check shouldn't cost the
# player a turn (and hand the enemy a free one) just to think out loud.
EXEMPT_ITEM_INTENTS = frozenset(name for name, match in MATCHES.items() if match.exempt)
NO_ITEM_LOOKUP_INTENTS = frozenset({"open", "close"})

# The item-interaction verbs eligible for DM_Improvisation.py's ad hoc creation fallback (see
# classify()'s own "improvisation_requested" note) -- includes "trade" (ex: "buy a rope"
# from a shopkeeper who never had one on their own hand-authored inventory list -- a general
# store shouldn't need every possible good pre-authored to sell it): DM_Improvisation.py stocks
# the created item directly into the current scene target's own inventory for this one intent,
# rather than the ground/player inventory every other intent here uses. Computed as the union
# of intents/improvisation.py's own PLAYER_CENTRIC_INTENTS/GROUND_AWARE_INTENTS/TARGET_CENTRIC_
# INTENTS -- imported from there, not DM_Improvisation.py, so this module's own independence from
# DMCore/game state stays intact.
IMPROVISABLE_INTENTS = PLAYER_CENTRIC_INTENTS | GROUND_AWARE_INTENTS | TARGET_CENTRIC_INTENTS

# map_to_item checks these before any embedding match -- currency is a plain integer field
# (entity["currency"]), not an object-supertype entity with a name/description to embed.
CURRENCY_SYNONYMS = ("gold", "coin", "currency", "money")
# The adjudicator's game actions (see AdHoc_Generation.py's GAME_ACTIONS) as item intents.
ADJUDICATED_ITEM_INTENTS = {"buy": "trade", "give": "give", "take": "take", "use": "use"}
# Money named as an adjudicated action's item -- never routed: currency moves only as a trade's
# price, and an improvised "coppers" item handed over would be worse than not understanding.
MONEY_PATTERN = re.compile(
    r"\b(?:coins?|coppers?|silvers?|golds?|platinum|money|currency|payment|cash|purse|"
    r"(?:copper|silver|gold) pieces?|[cgsp]p)\b"
)

# Item verbs that silently cost the player something (an item handed over, dropped or used up;
# money spent) -- for these, a semantic map_to_item hit alone isn't enough, the clause has to
# actually name the item (see _clause_names_item). Found by playtest: "...fragments that might
# give a clue" gated "give", map_to_item paired it with "health potion" at 0.55 off the
# sentence's general gist, and the potion silently went to a bystander.
ITEM_LOSING_INTENTS = frozenset({"give", "drop", "trade", "use"})

# Semantic-router intents that move the player or the clock -- never taken from a question.
# The router only sees input every keyword gate already declined (a real "can i go to the
# docks?" hits TRAVEL_KEYWORDS first), so a question reaching it is talk, not a command. Found
# by playtest: "is that argument about the docks or about something else entirely?" routed to
# travel and walked the player to the shipyard mid-conversation.
QUESTION_BLOCKED_ROUTES = frozenset(name for name, match in MATCHES.items() if match.question_blocked)

# A line opening on one of these is a question even without its "?" -- never adjudicated as a
# possible action (see classify's "declarative" case).
QUESTION_OPENERS = frozenset({
    "who", "what", "where", "when", "why", "how", "which", "whose", "what's", "where's", "who's",
    "how's", "is", "are", "was", "were", "do", "does", "did", "can", "could", "should", "would",
    "will", "shall", "may", "might", "have", "has",
})
# A turn whose every skill clause scored below this is a guess -- with someone present, the model
# is asked whether the line was an action at all (see IntentClassifier._adjudicate).
WEAK_TURN_SCORE = 0.6

# Item intents that change what the player has or holds -- never taken from an is_hypothetical
# sentence (see _classify_item_pass). Read-only ones (examine, lore_check, open) still are.
HYPOTHETICAL_BLOCKED_INTENTS = ITEM_LOSING_INTENTS | {"take", "equip", "unequip", "craft"}

# A clause pointing back at someone the rest of the input named ("...and start kicking him") --
# _classify_skill_pass falls back to the whole input's target for these. Personal pronouns only:
# "it" is as likely a door as a person, and "walk past the guard and kick it" must not hit the guard.
REFERRING_PRONOUN_PATTERN = re.compile(r"\b(?:him|her|them)\b")

# _detect_save_load_intent checks these ahead of everything else (item intent included --
# a slot name could otherwise contain a word like "take" and misfire the item intercept).
# Longest/most-specific prefix first in each tuple, since matching stops at the first hit and
# a shorter prefix (ex: "save ") would otherwise swallow "game as " into the slot name.
SAVE_PREFIXES = ("save game as ", "save as ", "save game ", "save ")
LOAD_PREFIXES = ("load game as ", "load as ", "load game ", "load ")


class IntentMatcher:
    """!
    @brief The one seam IntentClassifier depends on -- everything embedding-based
        (skill/item/target matching, plus dialogue sentiment) is real ML inference in
        production and a canned stub in tests. Not a runtime-enforced interface (this project
        has no third-party Protocol dependency beyond typing, and duck typing is enough here)
        -- purely documentation of the methods a matcher must provide, plus the two
        catalog-maintenance calls. SentenceTransformerMatcher (NLP_Core.py) is the production
        adapter; FakeMatcher (tests/support.py) is the test adapter -- two real adapters justify
        this seam existing at all, not a hypothetical one authored just in case.
    """

    def on_rules_loaded(self, data):
        """!@brief Builds skill/item/target embeddings from a fresh "rules_loaded" payload."""
        raise NotImplementedError

    def register_item(self, name, description, targetable=False):
        """!@brief Incrementally registers one ad hoc item's name/description for map_to_item
            (and, if targetable, for map_to_target too)."""
        raise NotImplementedError

    def adjudicate(self, text, present_names=(), partner=None, recent_narration=""):
        """!
        @brief What a line the classifier's own rules can only guess at mainly is: "action",
            "speech", "game_question", "musing", or None (no model to ask, or no usable answer
            -- the rules then stand). See IntentClassifier._adjudicate.
        """
        return None

    def map_to_action(self, processed_text):
        """!@brief Returns (skill_name, score); skill_name is None below confidence."""
        raise NotImplementedError

    def match_modifier(self, processed_text):
        """!
        @brief Checks processed_text for a literal, whole-word/phrase hit against any live
            supertype == "modifier" entity's own name (ex: "power attack", "empowered") --
            checked and stripped BEFORE map_to_action runs on the remainder, so a modifier
            phrase never dilutes the base ability's own semantic match (ex: "cast an empowered
            fireball" still embeds cleanly against "fireball" alone once "empowered" is gone).
            Ownership (whether the acting entity actually trained this modifier) is a DMCore-
            side concern, not this method's -- this only ever answers "did the player's own
            words name one at all."
        @param processed_text The clause text being classified.
        @return (modifier_name, stripped_text) -- modifier_name is None and stripped_text is
            processed_text unchanged if no modifier name is present.
        """
        raise NotImplementedError

    def map_to_item(self, processed_text):
        """!@brief Returns (item_name, score); item_name is None below confidence."""
        raise NotImplementedError

    def map_to_target(self, processed_text):
        """!@brief Returns (entity_name, score); entity_name is None below confidence."""
        raise NotImplementedError

    def map_to_intent(self, processed_text, strict=False):
        """!
        @brief Scores processed_text against INTENT_PROTOTYPES -- the semantic backstop for the
            keyword intent gates, consulted only at _finalize's own give-up point.
        @param processed_text The whole processed input.
        @param strict Apply the higher of the two confidence bars, for when a match would
            displace an improvisation attempt rather than a bare action_not_understood.
        @return (intent_name, score) -- intent_name is None below confidence OR when
            OTHER_INTENT ("none of the above") wins the argmax, which is an ordinary,
            expected outcome here rather than a failure.
        """
        raise NotImplementedError

    def set_destinations(self, destinations):
        """!
        @brief REPLACES the current location's reachable-exit bank for map_to_destination.
            Deliberately not named register_* like register_item: that one appends (the item
            catalog only ever grows), while this one must discard the previous location's exits
            outright, since the reachable set changes wholesale on every move.
        @param destinations A list of {"key", "name", "aliases"} dicts -- possibly empty, which
            is a legitimate state (a location authoring no exits at all).
        """
        raise NotImplementedError

    def map_to_destination(self, processed_text):
        """!@brief Returns (destination_key, score) against the bank set_destinations last
            installed; destination_key is None below confidence or with no bank loaded."""
        raise NotImplementedError

    def set_present_entities(self, entities):
        """!
        @brief REPLACES the bank of who is currently in the scene, for map_to_present_entity.
            Same wholesale-replacement shape as set_destinations (and for the same reason: the
            present cast changes completely on every move), driven by DMCore's own
            scene_roster_updated -- see DM_Rules.py's _publish_scene_roster.
        @param entities A list of {"key", "name", "subtype", "aliases"} dicts -- possibly
            empty, which is legitimate (a scene with nobody else in it).
        """
        raise NotImplementedError

    def map_to_present_entity(self, processed_text):
        """!
        @brief Returns (entity_key, score) for "is this phrase plausibly someone already
            standing here?" against the bank set_present_entities last installed.

            A genuinely different question from map_to_target, which scores against a GLOBAL,
            never-scene-filtered catalog of every creature in the rules -- fine for "attack the
            wolf", useless for deciding whether "the merchant" names someone in this room or
            a merchant three towns away.

            Its confidence bar is deliberately its own, and deliberately permissive: the only
            consequence of a false positive here is that nobody gets materialized (today's
            behavior), while a false negative invents a duplicate of someone already present.
        @param processed_text The address phrase to score (see extract_address_phrase).
        @return (entity_key, score); entity_key is None below confidence or with no bank.
        """
        raise NotImplementedError

    def classify_sentiment(self, processed_text):
        """!@brief Returns (sentiment_label, score) for the disposition axis; label is None
            below confidence."""
        raise NotImplementedError

    def classify_threat(self, processed_text):
        """!@brief Returns (sentiment_label, score) for the threat axis; label is None below
            confidence."""
        raise NotImplementedError

    def classify_familiarity(self, processed_text):
        """!@brief Returns (sentiment_label, score) for the familiarity axis; label is None
            below confidence."""
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


# A sentence opening on one of these (after HYPOTHETICAL_LEAD_WORDS) wonders about an action
# rather than taking it -- see is_hypothetical.
CONDITIONAL_OPENERS = frozenset({"if", "unless", "suppose", "supposing", "assuming", "whether"})
HYPOTHETICAL_LEAD_WORDS = LEADING_FILLER_WORDS | {"but", "so", "what", "now", "wait", "well"}
# A question opening on one of these asks permission the way a player declares an action ("can i
# take the sword?") -- the one kind of question an item interaction may still come from.
PERMISSION_OPENERS = frozenset({"can", "could", "may"})


def is_hypothetical(sentence):
    """!
    @brief Whether a sentence wonders about an action rather than taking it: it opens on a
        conditional ("if i give you the coins...", "but if...", "what if..."), or it's a question
        that isn't a permission-style "can i...?". Found by playtest: "but if i use a coupon then
        i only gotta give you the promise of the actual coins next week right?" handed the
        player's whole purse to a bystander.
    @param sentence One processed sentence, its own terminal punctuation included.
    """
    words = re.findall(r"[a-z']+", sentence)
    while words and words[0] in HYPOTHETICAL_LEAD_WORDS and not (words[0] == "what" and words[1:2] != ["if"]):
        words = words[1:]
    if words and words[0] in CONDITIONAL_OPENERS:
        return True
    return sentence.rstrip().endswith("?") and bool(words) and words[0] not in PERMISSION_OPENERS


def hypothetical_spans(processed_text):
    """!@brief [(start, end)] of every is_hypothetical sentence in processed_text."""
    return [
        match.span() for match in re.finditer(r"[^.!?;]+[.!?;]*", processed_text or "")
        if is_hypothetical(match.group())
    ]


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


def _free_standing_match(processed_text, match):
    """!@brief Whether processed_text trips one free-standing intent's keyword gate or its extra patterns."""
    for pattern in match.ignore:
        processed_text = pattern.sub(" ", processed_text)
    return _keyword_gate(processed_text, match.keywords) or any(p.search(processed_text) for p in match.patterns)


def detect_item_intent(processed_text):
    """!
    @brief Checks processed input for an item-interaction verb, ahead of skill matching.
    @param processed_text The cleaned and processed player input.
    @return "examine", "equip", "unequip", "drop", "take", "give", "trade", "use", "craft",
        "open", "close", or a free-standing intent (intents/registry.py's MATCHES: "advance",
        "retreat", "formation_behind", "formation_abreast", "speak_language", "rest", "mount",
        "dismount", "hitch", "unhitch", "lore_check"), or None.
    """
    # lore_check's long phrases are gated ahead of every item verb that might sit inside them.
    for name, match in MATCHES.items():
        if match.before_items and match.item_pass and _free_standing_match(processed_text, match):
            return name
    if _keyword_gate(processed_text, EXAMINE_KEYWORDS):
        return "examine"
    # Checked ahead of EQUIP_KEYWORDS purely as defense in depth -- _phrase_matches' own
    # word-boundary check already keeps "unequip " from matching EQUIP_KEYWORDS' "equip "
    # (no boundary between the "un" and "equip" it's fused to), but TAKE_KEYWORDS' "take "
    # genuinely is a separate, correctly-bounded word inside "take off my armor", so this
    # order still matters for that one.
    if _keyword_gate(processed_text, UNEQUIP_KEYWORDS):
        return "unequip"
    if _keyword_gate(processed_text, EQUIP_KEYWORDS):
        return "equip"
    if _keyword_gate(processed_text, DROP_KEYWORDS):
        return "drop"
    if _keyword_gate(processed_text, TAKE_KEYWORDS):
        return "take"
    if _keyword_gate(processed_text, GIVE_KEYWORDS):
        return "give"
    if _keyword_gate(processed_text, TRADE_KEYWORDS):
        return "trade"
    if _keyword_gate(processed_text, USE_KEYWORDS):
        return "use"
    if _keyword_gate(processed_text, CRAFT_KEYWORDS):
        return "craft"
    if _keyword_gate(processed_text, OPEN_KEYWORDS):
        return "open"
    if _keyword_gate(processed_text, CLOSE_KEYWORDS):
        return "close"
    # The rest, in MATCHES order: formation, speak_language, rest, mount/dismount, hitch/unhitch,
    # then advance/retreat -- each more specific match ahead of the plain advance it could be
    # mistaken for ("stand behind", "mount the horse").
    for name, match in MATCHES.items():
        if not match.before_items and match.item_pass and _free_standing_match(processed_text, match):
            return name
    return None


def _verb_forms(base):
    """!@brief A regular verb's own inflections -- "grab" -> grabs/grabbing/grabbed, "take" ->
        takes/taking, "pry" -> pries/prying -- each mapped back to base."""
    forms = {base + "s", base + "ed", base + "ing"}
    if base.endswith("e"):
        forms |= {base + "d", base[:-1] + "ing"}
    if base.endswith("y"):
        forms |= {base[:-1] + "ies", base[:-1] + "ied"}
    if re.search(r"(?:ch|sh|x|z|ss)$", base):
        forms |= {base + "es"}
    if re.search(r"[^aeiou][aeiou][bdgmnpt]$", base):
        forms |= {base + base[-1] + "ing", base + base[-1] + "ed"}
    return {form: base for form in forms}


# The main verb of every item-interaction keyword (the first word of each phrase), with every
# inflection of it mapped back -- see normalize_declared_verb.
ITEM_VERB_BASES = {}
for _keywords in (
    EXAMINE_KEYWORDS, EQUIP_KEYWORDS, UNEQUIP_KEYWORDS, DROP_KEYWORDS, TAKE_KEYWORDS, GIVE_KEYWORDS,
    TRADE_KEYWORDS, USE_KEYWORDS, CRAFT_KEYWORDS, OPEN_KEYWORDS, CLOSE_KEYWORDS, MOUNT_KEYWORDS,
    DISMOUNT_KEYWORDS, HITCH_KEYWORDS, UNHITCH_KEYWORDS,
):
    for _phrase in _keywords:
        ITEM_VERB_BASES.update(_verb_forms(_phrase.split()[0]))
# The keywords behind each item intent that names an item -- see extract_item_phrase.
ITEM_INTENT_KEYWORDS = {
    "examine": EXAMINE_KEYWORDS, "equip": EQUIP_KEYWORDS, "unequip": UNEQUIP_KEYWORDS,
    "drop": DROP_KEYWORDS, "take": TAKE_KEYWORDS, "give": GIVE_KEYWORDS, "trade": TRADE_KEYWORDS,
    "use": USE_KEYWORDS, "craft": CRAFT_KEYWORDS,
}
# Verbs of speaking aloud, every inflection mapped back to its base -- a clause opening on one is
# said, not done ("yell insults at the guard", "taunt the vendor"). See
# IntentClassifier._split_spoken_clauses. Found by playtest: nine of a brawler's forty turns were
# lines like these, and they came back not-understood or rolled artistry/reflexes. Verbs that try
# to get something stay out -- "threaten", "demand", "goad" are social-skill attempts and roll
# ("demand an audience", player_input_corpus.toml) -- as does "curse" (a spell keyword).
SPEECH_ACT_VERBS = {}
for _base in (
    "yell", "shout", "scream", "holler", "bellow", "whisper", "mutter", "murmur", "taunt", "mock",
    "jeer", "insult", "heckle", "accuse", "complain", "boast", "brag", "growl",
    "snarl", "hiss", "snap",
):
    SPEECH_ACT_VERBS.update(_verb_forms(_base))
    SPEECH_ACT_VERBS[_base] = _base
# Words between a first-person opener and the verb it leads to: "i'm just taking it", "then i grab".
DECLARED_VERB_FILLER = frozenset({"just", "then", "and", "so", "now", "also", "quickly", "finally", "simply"})


def normalize_declared_verb(clause):
    """!
    @brief clause with its main verb put in the base form item keywords are written in, when
        that verb is an inflected item verb -- "(grabs a loaf of bread)" -> "grab a loaf of
        bread", "i'm taking all of it" -> "take all of it". Found by playtest: emotes and
        first-person progressives never reached take, so "(snags the bread)"-style turns came
        back not understood. Only the clause's MAIN verb (after a wrapping parenthesis, a
        first-person opener and filler words): "crawl through the opening" must not turn into
        an "open" intent. Anything else comes back unchanged.
    """
    text = clause.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    words = text.split()
    index = 0
    while index < len(words) and (words[index] in FIRST_PERSON_OPENERS or words[index] in ("i'm", "im")
                                  or words[index] in DECLARED_VERB_FILLER):
        index += 1
    if index >= len(words):
        return clause
    base = ITEM_VERB_BASES.get(words[index].strip(".,!?;"))
    if not base:
        return clause
    return " ".join([base] + words[index + 1:])


def _phrase_matches(phrase, processed_text):
    """!
    @brief Whole-word/whole-phrase containment check -- a word-boundary regex, not the raw
        substring test every keyword tuple in this file used to be checked with. A short
        phrase (ex: DIALOGUE_KEYWORDS' "ask ") could otherwise false-positive against a
        longer, unrelated word that merely happens to end the same way (ex: "mask", a real
        skill's own keyword -- see this file's own
        test_item_and_dialogue_keywords_never_collide_with_a_real_skill_keyword in
        tests/test_nlp.py, which is what caught this live and is what still enforces it). Every
        internal space in a multi-word
        phrase (ex: "close the ") stays literal; only the phrase's own two outer edges get a
        \\b boundary -- the leading/trailing whitespace most phrases in this file are
        authored with is stripped first so it doesn't end up inside that boundary.
    @param phrase One keyword/phrase from a KEYWORDS tuple.
    @param processed_text The cleaned and processed player input (or one clause of it).
    @return True if phrase appears in processed_text as a whole word/phrase.
    """
    return re.search(rf"\b{re.escape(phrase.strip())}\b", processed_text) is not None


def _keyword_gate(processed_text, keywords):
    """!@brief Shared "does this phrase contain any of these keyword phrases" check."""
    return any(_phrase_matches(keyword, processed_text) for keyword in keywords)


def _clause_names_item(clause, item_name):
    """!
    @brief Whether clause literally mentions item_name -- any 3+ letter word of the name
        appearing inside the clause ("potions" names "health potion"), or any 4+ letter word
        of the clause appearing inside a word of the name ("sword" names "longsword"). The
        guard ITEM_LOSING_INTENTS applies on top of map_to_item's semantic score.
    @param clause One processed clause of player input.
    @param item_name The entity key map_to_item returned ("currency" always passes, since
        map_to_item only returns it on a literal synonym hit).
    """
    if item_name == "currency":
        return True
    name_words = [word for word in re.findall(r"[a-z]+", item_name.lower()) if len(word) >= 3]
    clause_words = [word for word in re.findall(r"[a-z]+", clause.lower()) if len(word) >= 4]
    hits = [clause.find(word) for word in name_words if word in clause]
    hits += [
        clause.find(clause_word) for clause_word in clause_words
        if any(clause_word in name_word for name_word in name_words)
    ]
    if not hits:
        return False
    # The thing acted on comes before any "to"/"at": 'give the sword to anne' is about the sword,
    # but 'dropping my gaze to his spear tip' is about a gaze, and the spear only the place it goes.
    # Found by playtest: it dropped a spear the player never held (the drop was refused as absent).
    return not re.search(r"\b(?:to|at|toward|towards)\b", clause[:min(hits)])


def detect_dialogue_intent(processed_text):
    """!
    @brief True if processed_text contains any DIALOGUE_KEYWORDS phrase, or a quoted span of
        speech ("I approach the fishmonger. \\"Is something going on?\\"") -- a player who
        writes their own line of dialogue in quotation marks is talking to whoever they just
        named, whether or not they also spelled out "speak to". DMCore's literal addressee scan
        (DM_Dialogue.py's _literal_dialogue_target) still decides who, from the whole input.
        Keywords inside a subordinate clause don't count (SUBORDINATE_CLAUSE_PATTERN): found by
        playtest, "kick my opponent until they can't ask questions" went to dialogue on "ask".
    """
    main_clauses = SUBORDINATE_CLAUSE_PATTERN.sub("", processed_text or "")
    return _keyword_gate(main_clauses, DIALOGUE_KEYWORDS) or bool(speech_quotes(processed_text))


# A subordinate clause, up to the next punctuation -- what it says is a condition or a purpose,
# not what the player is doing: "kick him until they can't ask", "if you see her, tell her"
# (only "if you see her" goes; "tell her" is still the player talking).
SUBORDINATE_CLAUSE_PATTERN = re.compile(r"\b(?:until|unless|because|before|after|while|if|when)\b[^,.;!?]*")


# A leading "gareth, " -- stripped before detect_implicit_speech reads the opening word, so
# naming the listener first doesn't hide what follows. Capped at three words; a short clause
# stripped by mistake ("draw my blade, then charge") only leaves its remainder to be read, which
# still opens like an action.
VOCATIVE_PATTERN = re.compile(r"^[a-z'\-]+(?: [a-z'\-]+){0,2}, ")
# The speaker as the object of an opening imperative ("help me", "come help me", "join us") --
# aimed at someone else, not an action of the player's own. Within the first three words only:
# "persuade the captain to lend us his boat" names "us" too, but as the payoff of a social-skill
# attempt that still has to roll.
SPEAKER_OBJECT_PATTERN = re.compile(r"^(?:[a-z']+ ){1,2}(?:me|us)\b")
SENTENCE_SPLIT_PATTERN = re.compile(r"[.!;]+\s*")


def detect_implicit_speech(processed_text):
    """!
    @brief Whether processed_text reads as something said TO someone rather than something
        done, with no dialogue keyword or quotation marks to say so -- "do you ever get tired
        of all this?", "let's find somewhere quieter", "come help me relax". Only consulted
        while someone could hear it -- a conversation partner (see
        IntentClassifier.set_conversation_partner) or anyone else in the scene
        (IntentClassifier.anyone_present); in an empty scene the same text stays with the
        ordinary skill/intent passes.

        Deliberately cheap and structural, no model call: a question mark, an opening word that
        doesn't declare an action (NON_ACTION_OPENERS, the same list the skill fallbacks use to
        recognise banter), or the player as the object of an imperative -- in any sentence. A
        social-skill attempt ("persuade him to lower the price", "threaten to report him") opens
        on its own verb and names no "me"/"us", so it still reaches the skill pass and rolls.
    @param processed_text The processed player input.
    """
    text = (processed_text or "").strip()
    if "?" in text:
        return True
    # Every sentence, not just the first -- "forget the lumber. let's find a private place."
    # opens on a verb but is plainly talk by its second sentence.
    for sentence in SENTENCE_SPLIT_PATTERN.split(text):
        sentence = sentence.strip()
        # Read before the vocative strip too: VOCATIVE_PATTERN can't tell "gareth, is it hot"
        # from "you look strong, bram", which it would cut down to a bare "bram".
        if sentence and not opens_like_an_action(sentence):
            return True
        sentence = VOCATIVE_PATTERN.sub("", sentence)
        if sentence and (not opens_like_an_action(sentence) or SPEAKER_OBJECT_PATTERN.search(sentence)):
            return True
    return False


def extract_address_phrase(processed_text):
    """!
    @brief The noun phrase the player used to address someone, if they used one at all --
        ex: "ask the merchant what he is selling" -> "merchant". Published alongside
        dialogue_detected, which otherwise carries no notion of *who* at all.

        Exists for the promotion gate (DM_Core.py's _on_dialogue_detected): materializing an
        NPC the player reached for requires first knowing that they reached for anyone. That
        makes this deliberately mechanical rather than clever -- it extracts, it never
        classifies. Every DIALOGUE_KEYWORDS phrase is a prefix ("talk to ", "ask ", "greet "),
        so the addressee, when there is one, sits immediately after whichever one matched
        earliest.

        This is keyword-shaped machinery in a file that just moved *away* from keyword tables
        (see INTENT_PROTOTYPES/map_to_intent), which is only acceptable because it fails
        CLOSED: an unrecognized shape returns None, None means no promotion, and no promotion
        means exactly today's behavior. Nothing is ever created because this function guessed
        well; things are only ever *not* created because it guessed badly. The semantic layer
        behind it (map_to_present_entity) is what catches the phrasings the word lists miss,
        so the fix for a missed phrasing is to lean on that, not to keep growing these tuples.
    @param processed_text The cleaned, lowercased player input.
    @return The address phrase (1-3 words, articles stripped), or None if the player addressed
            no one nameable -- a question opener ("ask about the weather"), a pronoun ("tell
            them to back off"), or nothing at all after the keyword.

        Known limitation, accepted rather than worked around: an addressee named BEFORE the
        keyword isn't found ("walk up to the merchant and ask what he is selling" reads the
        remainder after "ask " and correctly declines on "what"). Recovering it would mean
        guessing which earlier noun was the object of some other verb, which is precisely the
        kind of cleverness that fails open. That phrasing is a movement clause anyway, and the
        follow-up turn ("ask the merchant what he is selling") extracts cleanly.
    """
    earliest = None
    for keyword in DIALOGUE_KEYWORDS:
        match = re.search(rf"\b{re.escape(keyword.strip())}\b", processed_text or "")
        if match and (earliest is None or match.end() < earliest):
            earliest = match.end()
    if earliest is None:
        return None

    words = [word.strip(ADDRESS_STRIP_CHARS) for word in (processed_text[earliest:] or "").split()]
    words = [word for word in words if word]
    if not words or words[0] in ADDRESS_NON_ADDRESSEES:
        return None

    while words and words[0] in ADDRESS_ARTICLES:
        words.pop(0)

    phrase = []
    for word in words[:MAX_ADDRESS_WORDS]:
        if word in ADDRESS_TERMINATORS:
            break
        phrase.append(word)
    return " ".join(phrase) or None


def extract_item_phrase(clause, intent):
    """!
    @brief The words the player used for the item an item clause acts on -- "take the belt
        knife from the stall" -> "belt knife". Carried on the clause so a "not here" notice
        quotes what the player said, not the catalog item map_to_item settled on (found by
        playtest: "belt knife" was told There's no "belt pouch" here). Mechanical like
        extract_address_phrase, and fails closed the same way: None means the notice names the
        matched item, as before.
    @param clause One processed clause of player input.
    @param intent The clause's item intent (detect_item_intent).
    @return The phrase (1-4 words, articles stripped), or None.
    """
    text = normalize_declared_verb(clause)
    earliest = None
    for keyword in ITEM_INTENT_KEYWORDS.get(intent, ()):
        match = re.search(rf"\b{re.escape(keyword.strip())}\b", text)
        if match and (earliest is None or match.end() < earliest):
            earliest = match.end()
    if earliest is None:
        return None
    words = [word.strip(ADDRESS_STRIP_CHARS) for word in text[earliest:].split()]
    words = [word for word in words if word]
    while words and words[0] in ITEM_PHRASE_ARTICLES:
        words.pop(0)
    phrase = []
    for word in words[:MAX_ITEM_PHRASE_WORDS]:
        # A trailing participle describes where the item is, not what it is: "a length of rope
        # lying near the stall" (found by playtest). Not straight after "of": "a bag of shining coins".
        if word in ITEM_PHRASE_TERMINATORS or (
            phrase and phrase[-1] != "of" and word.endswith("ing") and len(word) > 4
        ):
            break
        phrase.append(word)
    return " ".join(phrase) or None


def _opening_verb(clause):
    """!@brief A clause's first word after any FIRST_PERSON_OPENERS/LEADING_FILLER_WORDS, or ""."""
    words = [word for word in re.findall(r"[a-z']+", clause)]
    while words and (words[0] in FIRST_PERSON_OPENERS or words[0] in LEADING_FILLER_WORDS):
        words = words[1:]
    return words[0] if words else ""


def _original_casing(raw_input, processed_text):
    """!
    @brief The tail of raw_input that processed_text was made from, in the player's own casing
        -- process_input only strips, lowercases and drops a leading prefix, all of which keep
        the kept text's length, so the tail of the stripped raw input lines up exactly.
    """
    stripped = (raw_input or "").strip()
    if not processed_text or len(processed_text) > len(stripped):
        return processed_text or ""
    return stripped[len(stripped) - len(processed_text):]


def frame_speech(raw_input, processed_text, explicit):
    """!
    @brief How a dialogue line should be put to the narrator, so a command is never quoted as
        if it were speech -- "talk to the fishmonger" passed along as the player's own words
        had the model inventing a whole conversation around it.
          - "verbatim": the player's own words, to quote as said -- a quoted span (just the
            quote), implicit speech (the whole line, see detect_implicit_speech), or a
            keyword aimed back at the speaker ("tell me what you know").
          - "greet": a dialogue keyword naming someone and nothing more ("talk to the
            fishmonger") -- the NPC should open the conversation.
          - "reported": anything else keyword-led ("ask about the kelp beds", "tell silas to
            back off"), restated in the second person from the keyword on ("You ask about the
            kelp beds.") so the narrator hears what was asked, not how it was typed.
    @param raw_input The player's raw input, for casing.
    @param processed_text process_input(raw_input).
    @param explicit Whether a dialogue keyword or quotation marks triggered this at all.
    @return {"speech_form", "utterance"} -- utterance is None for "greet".
    """
    original = _original_casing(raw_input, processed_text)
    quotes = speech_quotes(original)
    if quotes:
        return {"speech_form": "verbatim", "utterance": " ".join(quotes)}
    if not explicit:
        return {"speech_form": "verbatim", "utterance": original}

    earliest = None
    for keyword in DIALOGUE_KEYWORDS:
        match = re.search(rf"\b{re.escape(keyword.strip())}\b", processed_text)
        if match and (earliest is None or match.start() < earliest.start()):
            earliest = match
    if earliest is None:
        return {"speech_form": "verbatim", "utterance": original}

    words = [word.strip(ADDRESS_STRIP_CHARS) for word in processed_text[earliest.end():].split()]
    words = [word for word in words if word]
    # Nothing after the keyword names no one to greet: found by playtest, "a gate, you say? where
    # does this passage open, and how can we tell?" was framed as a bare greeting on "tell", and
    # the reply ignored the question.
    if not words or words[0] in ("me", "us"):
        return {"speech_form": "verbatim", "utterance": original}

    # Skip past the addressee the same way extract_address_phrase reads it; anything left
    # over is what was actually asked or said.
    rest = list(words)
    while rest and rest[0] in ADDRESS_ARTICLES:
        rest.pop(0)
    for _ in range(MAX_ADDRESS_WORDS):
        if not rest or rest[0] in ADDRESS_TERMINATORS or rest[0] in ADDRESS_NON_ADDRESSEES:
            break
        rest.pop(0)
    if not rest:
        return {"speech_form": "greet", "utterance": None}

    clause = original[earliest.start():].strip().rstrip(".!")
    return {"speech_form": "reported", "utterance": f"You {clause[:1].lower()}{clause[1:]}."}


# Talk about the game rather than within it -- see detect_out_of_character. Phrases, not bare
# "roll"/"rules": "roll under the gate" (acrobatics) and "the rules of the guild" are in-fiction
# (found by playtest: "what are the rules of the 'beautiful, terrible mess'?" went to ADaM).
OUT_OF_CHARACTER_PATTERN = re.compile(
    r"\b(?:rulebook|rule ?book|the rules(?! of\b)|house rules?|game master|dungeon master|gm|dm|npcs?|"
    r"metagam\w*|game mechanics|mechanics|roll (?:a |the )?dice|roll for|dice rolls?|a dice|"
    r"the dice|d6|d20|saving throws?|bonus actions?|action economy|skill checks?|ability checks?|"
    r"perception checks?|character sheet|hit points|damage chart|turn order|experience points|"
    r"level up|xp)\b"
)


def detect_out_of_character(processed_text):
    """!
    @brief Whether the player is asking about the game itself -- "does the rulebook say...",
        "are we supposed to roll a dice for this?", "what's the action economy?" -- the kind of
        question ADaM (help_detected) answers. Found by playtest: once unmarked talk reached
        anyone present, 150 turns of rules-lawyering went to a fisherman as in-character
        dialogue. Needs both a game-mechanics term and a line that reads as talk rather than a
        declared action (detect_implicit_speech), so "roll for initiative against the goblin"
        or "i'm going to roll perception" still reach the skill pass.
    """
    return bool(OUT_OF_CHARACTER_PATTERN.search(processed_text or "")) and detect_implicit_speech(processed_text)


def detect_help_intent(processed_text):
    """!@brief True if processed_text contains the whole word "adam" (any case)."""
    return bool(ADAM_NAME_PATTERN.search(processed_text))


def detect_scene_query_intent(processed_text):
    """!@brief True if processed_text contains any SCENE_QUERY_KEYWORDS phrase."""
    return _keyword_gate(processed_text, SCENE_QUERY_KEYWORDS)


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
        if _keyword_gate(processed_text, phrases):
            return direction
    return None


def detect_travel_intent(processed_text):
    """!@brief True if processed_text contains any TRAVEL_KEYWORDS phrase, or the word "leave"."""
    return bool(LEAVE_PATTERN.search(processed_text)) or _keyword_gate(processed_text, TRAVEL_KEYWORDS)


def _travel_event(processed_text, matcher):
    """!
    @brief Builds the one "travel" item_interaction_detected event, for BOTH producers -- the
        TRAVEL_KEYWORDS gate in classify() and the semantic router in _finalize. Deliberately
        shared rather than duplicated: the payload now carries a matcher-resolved "destination",
        and two hand-synced copies of that shape would drift the first time either gains a field.
        A None destination is the ordinary case (no exit bank loaded, or nothing named
        confidently) and means DMCore falls back to its own literal name/alias scan, exactly as
        before this existed -- see DM_Movement.py's _resolve_location_exit.
    @param processed_text The whole processed input.
    @param matcher The IntentMatcher seam.
    @return One {"event", "payload"} dict.
    """
    destination, _score = matcher.map_to_destination(processed_text)
    return {"event": "item_interaction_detected", "payload": {
        "intent": "travel", "item_name": None, "input": processed_text, "score": None,
        "destination": destination,
    }}


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


class Adjudication:
    """!
    @brief The model's say on one input (see IntentClassifier._adjudicate), made at most once per
        input -- may_ask() is that rule. Lives for one classify() call and is returned with its
        result, so nothing carries over from one input to the next.
    @param asked Whether the model was consulted this input (its answer may still have been none).
    @param verdict "action"/"speech"/"game_question"/"musing"/"gesture", or None for no usable answer.
    @param trigger Why it was asked: "declarative", "weak_turn", "weak_quoted" or "not_understood".
    @param action (game_action, item) the model named with an "action" verdict, or None.
    @param tone The setting's gesture tone the model named with a "gesture" verdict, or None.
    """

    def __init__(self):
        self.asked = False
        self.verdict = None
        self.trigger = None
        self.action = None
        self.tone = None

    def may_ask(self):
        return not self.asked

    def record(self, verdict, trigger, action=None, tone=None):
        self.asked, self.verdict, self.trigger, self.action, self.tone = True, verdict, trigger, action, tone


# What classify() returns: processed_text for the caller's own log line, the events to publish in
# order, and the input's Adjudication.
ClassifiedInput = namedtuple("ClassifiedInput", ["processed", "events", "adjudication"])


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
        (save/load -> ADaM/help -> scene query -> room direction -> [per-clause item pass] ->
        dialogue-if-nothing-claimed -> [per-clause skill pass]), and a per-clause gate list the
        item pass runs for each clause (exempt-movement -> no-lookup-item -> item-lookup ->
        defer). Final aggregation (turn vs. improvisation-fallback vs. not-understood) is its
        own step, since it's genuine accumulation logic over every clause's outcome, not a gate
        itself. Scene query sits right after ADaM/help (not before) so "adam, what do i see"
        still reaches the out-of-character help channel rather than this in-fiction one --
        ADAM_NAME_PATTERN is checked first either way.
    """

    def __init__(self, matcher):
        """!
        @param matcher An IntentMatcher adapter -- SentenceTransformerMatcher in production,
            FakeMatcher in tests.
        """
        self.matcher = matcher
        # Who the player is currently talking to, as DMCore last published it (DM_Dialogue.py's
        # _set_conversation_partner) -- None when no conversation is running. Only its
        # presence matters here: it's what lets detect_implicit_speech route an unmarked line
        # to dialogue. DMCore, not this class, still decides who that line actually reaches.
        self.conversation_partner = None
        # Whether anyone besides the player is in the scene, as DMCore last published it -- with
        # nobody here, unmarked speech has no listener (see the dialogue gate in classify).
        self.anyone_present = False
        self.present_names = []
        self.recent_narration = ""

    def on_rules_loaded(self, data):
        """!@brief Forwards a "rules_loaded" payload to the matcher to build its embeddings."""
        self.matcher.on_rules_loaded(data)

    def register_item(self, name, description, targetable=False):
        """!@brief Forwards one ad hoc item's name/description to the matcher's own catalog."""
        self.matcher.register_item(name, description, targetable)

    def set_destinations(self, destinations):
        """!@brief Forwards the current location's reachable exits to the matcher's own bank."""
        self.matcher.set_destinations(destinations)

    def set_present_entities(self, entities):
        """!@brief Forwards the current scene's own cast to the matcher's own bank."""
        self.matcher.set_present_entities(entities)
        self.anyone_present = any(entity.get("key") for entity in entities or [])
        self.present_names = [entity.get("name") or entity.get("key") for entity in entities or [] if entity.get("key")]

    def set_recent_narration(self, text):
        """!@brief The narrator's latest words, as context for _adjudicate."""
        self.recent_narration = text or ""

    def set_conversation_partner(self, partner):
        """!@brief Records DMCore's current conversation partner ({"key", ...} or None)."""
        self.conversation_partner = partner

    def _adjudicate(self, processed, trigger, adjudication):
        """!
        @brief Asks the matcher's model what a line the rules can only guess at mainly is
            (IntentMatcher.adjudicate), for three cases, each only with someone present:
            "declarative" -- the rules called an unquestioning line talk on its opening words
            ("let's go down that cut-through."); "weak_turn" -- every skill clause scored below
            WEAK_TURN_SCORE; "not_understood" -- nothing claimed it. The model only picks the
            channel -- except that an "action" may name an item action and its item, kept in
            the input's Adjudication for _adjudicated_item_event and NLPCore's log.
        @param adjudication This input's Adjudication, which records the answer.
        @return "action"/"speech"/"game_question"/"musing", or None to leave the rules' call.
        """
        adjudicate = getattr(self.matcher, "adjudicate", None)
        verdict = adjudicate(
            processed, self.present_names,
            (self.conversation_partner or {}).get("name"), self.recent_narration,
        ) if adjudicate else None
        if isinstance(verdict, str):
            verdict = {"kind": verdict}
        verdict = verdict or {}
        kind = verdict.get("kind")
        game_action, item = verdict.get("game_action"), verdict.get("item")
        adjudication.record(
            kind, trigger, (game_action, item) if kind == "action" and game_action and item else None,
            verdict.get("tone") if kind == "gesture" else None,
        )
        return kind

    def _adjudicated_item_event(self, processed, adjudication, raw_input=""):
        """!
        @brief The item event for an "action" verdict that named buy/give/take/use and its item,
            when the rules found nothing to claim the line or only guessed a skill -- the item's own catalog entry if
            map_to_item finds one, else improvisation (which, for a purchase, stocks the seller
            with what the narrator offered). Found by playtest: "let's get the peppers." and
            "here are the coppers." were judged actions but came back not understood.
            A purchase or gift of nothing but money is said, not done: currency moves only as a
            trade's price, so "here are the coppers." after the sale went through is talk to the
            seller (found by replay, once the verdict's "reason" field was dropped).
        @return One {"event", "payload"} dict, or None (no such verdict, money taken, or a
            hypothetical line).
        """
        if not adjudication.action or adjudication.verdict != "action":
            return None
        game_action, item = adjudication.action
        intent = ADJUDICATED_ITEM_INTENTS.get(game_action)
        phrase = process_input(item)
        if not intent or not phrase:
            return None
        if MONEY_PATTERN.search(phrase):
            return self._verdict_event("speech", processed, raw_input) if intent in ("trade", "give") else None
        if intent in HYPOTHETICAL_BLOCKED_INTENTS and is_hypothetical(processed):
            return None
        item_name, _score = self.matcher.map_to_item(phrase)
        if item_name == "currency" or (item_name and intent in ITEM_LOSING_INTENTS and not _clause_names_item(phrase, item_name)):
            item_name = None
        if item_name:
            return {"event": "turn_detected", "payload": {
                "clauses": [{"kind": "item", "intent": intent, "item_name": item_name, "phrase": phrase}],
                "input": processed,
            }}
        return {"event": "improvisation_requested", "payload": {
            "intent": intent, "phrase": phrase, "input": processed,
        }}

    def _verdict_event(self, verdict, processed, raw_input, adjudication=None):
        """!
        @brief The event a non-action verdict routes to, or None ("action", or no verdict).
        @param adjudication This input's Adjudication, which holds the tone a "gesture" carries.
        """
        if verdict == "gesture":
            # A wordless act, claimed whole: an item-kind clause (so it takes a turn slot like any
            # item interaction, and never rolls) that no keyword gate produces -- only this verdict
            # does. DMCore picks the target; the model only names the tone.
            tone = adjudication.tone if adjudication else None
            if not tone:
                return None
            return {"event": "turn_detected", "payload": {
                "clauses": [{"kind": "item", "intent": "gesture", "item_name": None, "phrase": None, "tone": tone}],
                "input": processed,
            }}
        if verdict == "speech":
            return self._dialogue_event(processed, frame_speech(raw_input, processed, False), True)
        if verdict == "game_question":
            return self._help_event(processed)
        if verdict == "musing":
            # Thinking aloud -- nothing was attempted, so the narrator may acknowledge it (see
            # LLMCore.generate_clarification_response's own reason handling).
            return {"event": "action_not_understood", "payload": {"input": processed, "score": 0.0, "reason": "musing"}}
        return None

    @staticmethod
    def _help_event(processed):
        return {"event": "help_detected", "payload": {
            "input": processed,
            "removal_candidate": detect_removal_intent(processed),
            "creature_candidate": detect_creature_intent(processed),
            "edit_candidate": detect_edit_intent(processed),
        }}

    def classify(self, raw_input):
        """!
        @brief Classifies one whole turn of raw player input. See this class's own docstring
            for the two-level gate order; see docs/action-resolution.md's "Multiple actions" and
            docs/adam-improvisation.md's "Ad hoc entity creation and removal" sections for why
            that order is what it is.
        @param raw_input The raw string from "user_input_submitted".
        @return A ClassifiedInput (processed, events, adjudication) -- processed_text for the
            caller's own "Processing player input" log line, events a list of one or more
            {"event", "payload"} dicts to publish, in order, and this input's Adjudication.
            events is Almost always length 1; more than one when an
            EXEMPT_ITEM_INTENTS clause (ex: "retreat") shares the input with a real turn (ex:
            "attack the wolf and retreat" publishes the retreat's own item_interaction_detected
            immediately, then a separate turn_detected for the attack), or with dialogue (ex:
            "I approach the merchant and ask about the celebration" publishes the approach's own
            item_interaction_detected immediately, then a dialogue_detected for the question --
            see the dialogue check below).
        """
        processed = process_input(raw_input)
        events = []
        adjudication = Adjudication()

        save_load_intent, slot_name = detect_save_load_intent(processed)
        if save_load_intent:
            events.append({"event": f"{save_load_intent}_requested", "payload": {"slot": slot_name}})
            return ClassifiedInput(processed, events, adjudication)

        if detect_help_intent(processed) or detect_out_of_character(processed):
            # A question about the game itself reaches ADaM without "adam" said aloud -- see
            # detect_out_of_character.
            events.append(self._help_event(processed))
            return ClassifiedInput(processed, events, adjudication)

        if detect_scene_query_intent(processed):
            # See SCENE_QUERY_KEYWORDS' own module note -- a free-standing, diceless,
            # read-only question about the current scene, answered from live ground-truth
            # state (DM_Help.py's _on_scene_query_detected) rather than left to fall through
            # to item-interaction detection (and, on no matching item, ad hoc item
            # generation) or an ungrounded clarification response.
            events.append({"event": "scene_query_detected", "payload": {"input": processed}})
            return ClassifiedInput(processed, events, adjudication)

        # What the player says aloud is not what they do (see mask_speech_quotes): the gates that
        # move them or act on the world read the line with its spoken quotes blanked.
        acting = mask_talk(processed)
        direction = detect_direction(acting)
        if direction:
            # A different axis from "advance"/"retreat" below -- see DIRECTION_PHRASES'
            # module note. No item name to resolve at all, so map_to_item never runs for
            # this either; DMCore._find_room_exit is what actually decides whether this
            # direction resolves to a real exit from the player's current band.
            events.append({"event": "item_interaction_detected", "payload": {
                "intent": "move", "item_name": None, "direction": direction,
                "input": processed, "score": None,
            }})
            return ClassifiedInput(processed, events, adjudication)

        # Only from a sentence that isn't is_hypothetical: found by playtest, "if i take the
        # proof of the goods... can i leave?" set off travel and the narrator invented a barrier.
        if any(
            detect_travel_intent(sentence) and not is_hypothetical(sentence)
            for sentence in re.findall(r"[^.!?;]+[.!?;]*", acting)
        ):
            # Same tier as the direction check above, but for location-to-location travel.
            # Unlike every other gate here, this one does consult the matcher (for the named
            # destination -- see _travel_event); DMCore still gets the raw input and still
            # resolves the destination literally first, so a None here changes nothing.
            events.append(_travel_event(processed, self.matcher))
            return ClassifiedInput(processed, events, adjudication)
        if TOWARD_PATTERN.search(acting) and not is_hypothetical(processed):
            travel = _travel_event(processed, self.matcher)
            if travel["payload"]["destination"]:
                events.append(travel)
                return ClassifiedInput(processed, events, adjudication)

        turn_clauses, remaining_clauses, found_exempt, unmatched_item_verbs = self._classify_item_pass(
            processed, events,
        )

        # Dialogue is checked once, on the whole input, whenever the item pass didn't claim a
        # real, turn-costing item interaction -- an exempt clause (ex: "advance") is allowed to
        # share the turn with it, the same way an exempt "retreat" clause already shares a turn
        # with a following skill-pass action below: "I approach the merchant and ask about the
        # celebration" is two free, diceless actions, not one that silently drops the other.
        # Only a genuine item_interaction turn_clauses entry -- something that actually costs
        # the turn action -- still suppresses dialogue outright, the same priority the old
        # single-clause code already gave item intents over dialogue.
        # A running conversation widens the gate: a question, a suggestion, or banter with no
        # "talk to"/quotation marks goes to whoever the player is already talking to rather
        # than down to the skill pass (see detect_implicit_speech). Not when a free-standing
        # intent already claimed a clause: "what do you know about the troll" is a lore check
        # that happens to be phrased as a question, and pairing it with dialogue would silence
        # its own narration (see "quiet" below). With no conversation running yet, anyone in
        # the scene is a listener: DMCore's _resolve_dialogue_target already sends an unnamed
        # remark to the scene's default person. Found by playtest: 210 turns of talk to NPCs
        # with no "talk to" reached dialogue zero times, rolling gambling ("i bet..."), sunder
        # and husbandry instead. Only an empty scene leaves the line to the skill pass.
        explicit_dialogue = detect_dialogue_intent(processed)
        has_listener = (
            self.conversation_partner is not None
            or self.anyone_present
        )
        implicit_dialogue = (
            not explicit_dialogue and not found_exempt and has_listener
            and detect_implicit_speech(processed)
        )
        split_tried = False
        if has_listener and not turn_clauses and not explicit_dialogue and not found_exempt and stage_directions(processed):
            # "(i wink at her.) just trying to get close to you." -- the stage direction does not have to
            # look like talk for the rest of the line to be talk.
            split_tried = True
            mixed = self._split_speech_from_action(raw_input, processed, adjudication)
            if mixed:
                events.extend(mixed)
                return ClassifiedInput(processed, events, adjudication)
        if implicit_dialogue and not turn_clauses and "?" not in processed and _opening_verb(processed) not in QUESTION_OPENERS:
            # A line that is part stage direction, part talk -- "(i pause, letting my stare linger.)
            # wouldn't dream of it." -- is split before the whole line is put to the model, which
            # can only name one kind for it and says "action", losing the talk and the gesture both.
            split_tried = True
            mixed = self._split_speech_from_action(raw_input, processed, adjudication)
            if mixed:
                events.extend(mixed)
                return ClassifiedInput(processed, events, adjudication)
            # Talk only by its opening words -- ask (see _adjudicate). A question stays talk, with
            # or without its "?" ("where should i put it"): it almost never declares an action,
            # and is_hypothetical already covers the rest.
            verdict = self._adjudicate(processed, "declarative", adjudication)
            if verdict == "action":
                implicit_dialogue = False
            elif verdict in ("game_question", "musing", "gesture"):
                gesture_event = self._verdict_event(verdict, processed, raw_input, adjudication)
                if gesture_event:
                    events.append(gesture_event)
                    return ClassifiedInput(processed, events, adjudication)
        if not turn_clauses and has_listener and not speech_quotes(processed):
            spoken = self._split_spoken_clauses(raw_input, processed)
            if spoken:
                if found_exempt and all(event["event"] == "dialogue_detected" for event in spoken):
                    for event in events:
                        if event["event"] == "item_interaction_detected":
                            event["payload"]["quiet"] = True
                events.extend(spoken)
                return ClassifiedInput(processed, events, adjudication)

        if not turn_clauses and (explicit_dialogue or implicit_dialogue):
            if found_exempt:
                # The exempt clause(s) just appended above (ex: "advance") are about to share
                # this turn with real dialogue -- the movement is mechanically real (DMCore
                # still repositions the player for it) but its own narration is nearly always
                # meaningless filler ("you push through the crowd") next to an actual NPC
                # reply a few seconds later. "quiet" tells LLMCore's own
                # generate_item_interaction_response to skip narrating this specific
                # item_interaction_resolved rather than spend a second LLM call and a second
                # chat bubble on it -- the dialogue reply already implies the player reached
                # whoever they just addressed.
                for event in events:
                    if event["event"] == "item_interaction_detected":
                        event["payload"]["quiet"] = True
            if implicit_dialogue:
                mixed = None if split_tried else self._split_speech_from_action(raw_input, processed, adjudication)
            else:
                mixed = self._split_quoted_speech(raw_input, processed, adjudication)
            if mixed:
                events.extend(mixed)
                return ClassifiedInput(processed, events, adjudication)
            events.append(self._dialogue_event(
                processed, frame_speech(raw_input, processed, explicit_dialogue), implicit_dialogue,
            ))
            return ClassifiedInput(processed, events, adjudication)

        best_score = self._classify_skill_pass(
            remaining_clauses, turn_clauses, processed,
            item_verb_clauses={verb["phrase"] for verb in unmatched_item_verbs},
        )
        weak_turn = turn_clauses and has_listener and not found_exempt and all(
            clause["kind"] == "action" and clause.get("score", 1.0) < WEAK_TURN_SCORE for clause in turn_clauses
        )
        if weak_turn and adjudication.may_ask():
            verdict_event = self._verdict_event(
                self._adjudicate(processed, "weak_turn", adjudication), processed, raw_input, adjudication,
            )
            if verdict_event:
                events.append(verdict_event)
                return ClassifiedInput(processed, events, adjudication)
        # A guessed skill never beats the item action the model named for the same line.
        item_event = self._adjudicated_item_event(processed, adjudication, raw_input) if weak_turn else None
        if item_event:
            events.append(item_event)
            return ClassifiedInput(processed, events, adjudication)

        self._finalize(
            processed, turn_clauses, found_exempt, unmatched_item_verbs, best_score, events, adjudication, raw_input,
        )
        return ClassifiedInput(processed, events, adjudication)

    def _dialogue_event(self, processed, framing, implicit):
        """!
        @brief Builds one dialogue_detected event for processed (the whole input, or just its
            spoken sentences -- see _split_speech_from_action).
        @param framing frame_speech's own {"speech_form", "utterance"}.
        @param implicit Whether no dialogue keyword or quotation marks triggered it.
        """
        # Classified here, not left to DMCore, since sentiment-of-an-utterance is the same
        # kind of fast local model judgment call skill/item/target matching already is --
        # the matcher seam is what lets this stay local classification (see
        # SentenceTransformerMatcher.classify_sentiment/classify_threat/classify_familiarity)
        # rather than an LLM round trip. Three independent axis reads, not one -- each
        # score (the classifier's own confidence) rides along too -- DM_Social.py's
        # nudge_attitude scales each axis's actual nudge by its own score, rather than every
        # dialogue line of the same sentiment moving that axis by an identical flat amount.
        sentiment, sentiment_score = self.matcher.classify_sentiment(processed)
        threat_sentiment, threat_score = self.matcher.classify_threat(processed)
        familiarity_sentiment, familiarity_score = self.matcher.classify_familiarity(processed)
        # Evidence for DMCore's promotion gate, not a decision: "the player addressed
        # someone by this phrase" (mechanical -- see extract_address_phrase) and "somebody
        # already in the scene plausibly answers to it" (semantic). DMCore still resolves
        # the addressee literally first and is free to ignore both (see
        # _on_dialogue_detected) -- the same division of labour the travel path already
        # uses, where NLP offers a destination match and DM_Movement.py's literal exit
        # scan still wins. The matcher is only consulted when there's a phrase to score,
        # so an ordinary "ask about the weather" costs nothing extra.
        address_phrase = extract_address_phrase(processed)
        address_match, address_score = (
            self.matcher.map_to_present_entity(address_phrase) if address_phrase else (None, 0.0)
        )
        return {
            "event": "dialogue_detected",
            "payload": {
                "input": processed, "score": None, "implicit": implicit,
                "speech_form": framing["speech_form"], "utterance": framing["utterance"],
                "sentiment": sentiment, "sentiment_score": sentiment_score,
                "threat_sentiment": threat_sentiment, "threat_score": threat_score,
                "familiarity_sentiment": familiarity_sentiment, "familiarity_score": familiarity_score,
                "address_phrase": address_phrase,
                "address_match": address_match, "address_score": address_score,
            },
        }

    def _split_speech_from_action(self, raw_input, processed, adjudication=None):
        """!
        @brief Splits an implicit-speech input that also declares an action -- "stomach for
            snacks? never mind, i'll just take the goods instead!" -- into a turn for the action
            and dialogue for the words, in the order the player wrote them. Found by playtest:
            once unmarked speech reached anyone present, a taunt anywhere in a line swallowed
            the action beside it.

            A sentence is speech by detect_implicit_speech's own test, applied to it alone.
            Only a sentence that resolves to something real counts as the action: an item
            interaction with a matched item, or a skill matched semantically (a keyword-fallback
            hit, or an item verb naming no real item, is too weak to take a turn away from talk). Anything less leaves the whole input
            as dialogue, exactly as before -- "forget the lumber. let's find a private place."
            stays talk -- except that an action half nothing matched is put to the model once
            ("weak_split"), and a "gesture" verdict keeps it as a gesture turn beside the talk.
            Found by playtest: "(i slide a wink across the counter.) just trying to get close to
            you." matched no skill, so the wink was dropped, or the whole line came back as an
            unresolved action.
        @param adjudication This input's Adjudication, for the one model question.
        @return [event, ...] for both halves, or None to keep the input whole.
        """
        original = _original_casing(raw_input, processed)
        spans = stage_directions(processed)
        talk = _blank_spans(processed, spans)
        stage_mode = bool(spans and re.search(r"[a-z]", talk))
        if stage_mode:
            # A player who writes "(...)" is writing a stage direction by convention: what is inside is
            # the action and what is outside is the talk, with no word list to guess which sentence
            # is which ("wouldn't dream of it." opens like an action, and is plainly speech here).
            # Found by playtest: this is how the gooner persona writes every turn.
            action_text = " ".join(content for _start, _end, content in spans)
            speech_text = re.sub(r"\s+", " ", talk).strip()
            utterance = re.sub(r"\s+", " ", _blank_spans(original, spans)).strip()
            speech_first = bool(re.search(r"[a-z]", processed[:spans[0][0]]))
        else:
            speech, action = [], []
            # A closing parenthesis ends the stage direction it closes, not the talk that follows it.
            for match in re.finditer(r"[^.!?;]+[.!?;]*\)*", processed):
                sentence = match.group().strip()
                if not sentence:
                    continue
                first_word = re.match(r"[a-z']*", sentence).group()
                is_speech = detect_implicit_speech(sentence) or first_word in SPEECH_FRAGMENT_OPENERS
                (speech if is_speech else action).append(match)
            if not speech or not action:
                return None
            action_text = " ".join(match.group().strip() for match in action)
            speech_text = " ".join(match.group().strip() for match in speech)
            utterance = " ".join(original[match.start():match.end()].strip() for match in speech)
            speech_first = speech[0].start() < action[0].start()

        exempt_events = []
        turn_clauses, remaining, _found_exempt, unmatched = self._classify_item_pass(action_text, exempt_events)
        if unmatched and not stage_mode:
            # An item verb naming nothing real ("i'll just take the goods") -- the skill pass
            # would only guess at it ("never mind" -> psionics), too weak to split talk over.
            return None
        if unmatched:
            # Inside a stage direction the same verb is a false positive more often than not ("giving
            # her a knowing smile" is not a give), so nothing is trusted and the model decides below.
            turn_clauses = []
        else:
            self._classify_skill_pass(remaining, turn_clauses, action_text)
        turn_clauses = [
            clause for clause in turn_clauses
            if clause["kind"] == "item" or clause.get("score", 0.0) >= MIXED_ACTION_MIN_SCORE
        ]
        if not turn_clauses and adjudication is not None and adjudication.may_ask():
            gesture_text = action_text.strip("() ")
            if self._adjudicate(gesture_text, "weak_split", adjudication) == "gesture":
                gesture = self._verdict_event("gesture", gesture_text, raw_input, adjudication)
                if gesture:
                    turn_clauses = gesture["payload"]["clauses"]
                    action_text = gesture_text
        if not turn_clauses:
            return None

        dialogue = self._dialogue_event(speech_text, {"speech_form": "verbatim", "utterance": utterance}, True)
        turn = {"event": "turn_detected", "payload": {"clauses": turn_clauses, "input": action_text}}
        return [dialogue, turn] if speech_first else [turn, dialogue]

    def _split_spoken_clauses(self, raw_input, processed):
        """!
        @brief Speech the player describes rather than quotes -- "yell insults at the guard",
            "shout a challenge, then try to shove them" -- as reported dialogue ("You yell
            insults at the guard."), plus a turn for whatever else the line declares, in the
            order written. A clause is spoken when it opens on SPEECH_ACT_VERBS, and so is
            everything after a "<verb> that..." clause to the end of its sentence: "yell that
            his net looks flimsy and needs reinforcement" splits on "and", and "needs
            reinforcement" is still what was said. The rest is judged like any ordinary turn
            ("i shout 'get down!' and tackle the stranger" still tackles); if it resolves to
            nothing on its own, the whole line is left to the ordinary passes, as before this
            existed. A hypothetical ("if i yell at him, will he run?") is left alone too.
        @return [event, ...], or None if no clause opens on a speech verb.
        """
        clauses = split_action_clauses(processed)
        spoken, reporting = [], False
        for clause in clauses:
            if _opening_verb(clause) in SPEECH_ACT_VERBS:
                spoken.append(clause)
                reporting = bool(re.search(r"\bthat\b", clause))
            elif reporting:
                spoken.append(clause)
            if re.search(r"[.!?;]$", clause):
                reporting = False
        if not spoken or hypothetical_spans(processed):
            return None

        original = _original_casing(raw_input, processed)

        def reported(clause):
            # "You <base verb> <what follows, in the player's casing>." -- through any clauses
            # that continue it ("...and needs reinforcement"), so no words are dropped.
            start = processed.find(clause)
            last = clause
            for following in spoken[spoken.index(clause) + 1:]:
                if _opening_verb(following) in SPEECH_ACT_VERBS:
                    break
                last = following
            end = processed.find(last, start) + len(last)
            words = original[start:end].strip().rstrip(".!").split()
            verb = _opening_verb(clause)
            # Matched on a word's first part: _opening_verb reads "mock" out of "mock-yell", and
            # the rest of that word is kept ("You mock yell a challenge"). Found by playtest:
            # "I mock-yell a challenge" matched no whole word and crashed the turn.
            index = next(
                (i for i, word in enumerate(words) if re.findall(r"[a-z']+", word.lower())[:1] == [verb]), 0,
            )
            leftover = re.sub(rf"^[^a-z']*{re.escape(verb)}\W*", "", words[index].lower()) if words else ""
            tail = ([words[index][-len(leftover):]] if leftover else []) + words[index + 1:]
            return " ".join(["You", SPEECH_ACT_VERBS[verb], *tail]) + "."

        openers = [clause for clause in spoken if _opening_verb(clause) in SPEECH_ACT_VERBS]

        def dialogue(utterance):
            return self._dialogue_event(processed, {"speech_form": "reported", "utterance": utterance}, False)

        rest = [clause for clause in clauses if clause not in spoken]
        if not rest:
            return [dialogue(" ".join(reported(clause) for clause in openers))]
        action_text = ", ".join(rest)
        turn_clauses, remaining, _found_exempt, _unmatched = self._classify_item_pass(action_text, [])
        remaining = [clause for clause in remaining if _opening_verb(clause) not in SPEECH_TAG_VERBS]
        self._classify_skill_pass(remaining, turn_clauses, action_text)
        if not turn_clauses:
            return None
        said = dialogue(" ".join(reported(clause) for clause in openers))
        turn = {"event": "turn_detected", "payload": {"clauses": turn_clauses, "input": action_text}}
        return [said, turn] if clauses.index(spoken[0]) < clauses.index(rest[0]) else [turn, said]

    def _split_quoted_speech(self, raw_input, processed, adjudication):
        """!
        @brief The quoted-speech counterpart to _split_speech_from_action: 'i yell "hey!" and
            swing a fist at elara' is dialogue for the quoted words plus a turn for the rest, in
            the order written. Found by playtest: eleven of a brawler's forty turns paired a
            shout with an attack, and every attack was dropped as dialogue. Unlike the unmarked
            split, the text outside the quotes is judged like any ordinary turn (no
            MIXED_ACTION_MIN_SCORE bar, but the same weak-turn check with the model) -- the
            quotes already mark which part is talk. A clause
            that's only the tag on the quote (SPEECH_TAG_VERBS) or a dialogue keyword ('ask the
            guard "where is the inn?"') is never the action.
        @return [event, ...] for both halves, or None to keep the input whole.
        """
        original = _original_casing(raw_input, processed)
        quotes = speech_quotes(original)
        first_quote = processed.find('"')
        if not quotes or first_quote < 0:
            return None
        # Each quote becomes a clause break, so 'yell "hey!" and swing' splits around it.
        action_text = re.sub(r"(?:\s*,)+\s*", ", ", QUOTED_SPEECH_PATTERN.sub(",", processed))
        action_text = re.sub(r"\s+", " ", action_text).strip(" ,")
        turn_clauses, remaining, _found_exempt, _unmatched = self._classify_item_pass(action_text, [])
        remaining = [
            clause for clause in remaining
            if not detect_dialogue_intent(clause) and _opening_verb(clause) not in SPEECH_TAG_VERBS
        ]
        self._classify_skill_pass(remaining, turn_clauses, action_text)
        if not turn_clauses:
            return None
        # The same check an ordinary weak turn gets (see classify's weak_turn): a skill guessed
        # below WEAK_TURN_SCORE is put to the model, and anything but "action" keeps the whole
        # line as talk -- except a "gesture", whose wordless half is kept as its own turn beside
        # the quote. Found by playtest: "casually reach out, tapping the heavy metal ring on
        # his wrist" beside a quote rolled polearms at 0.52 and was narrated as a sword strike;
        # and a flirt's caress beside a whisper was dropped as talk five times in forty turns,
        # while seven more rolled strength, dodge or polearms for it.
        weak = all(clause["kind"] == "action" and clause.get("score", 1.0) < WEAK_TURN_SCORE for clause in turn_clauses)
        if weak and adjudication.may_ask():
            verdict = self._adjudicate(action_text, "weak_quoted", adjudication)
            if verdict == "gesture":
                gesture = self._verdict_event(verdict, action_text, raw_input, adjudication)
                if not gesture:
                    return None
                turn_clauses = gesture["payload"]["clauses"]
            elif verdict is not None and verdict != "action":
                return None

        dialogue = self._dialogue_event(processed, {"speech_form": "verbatim", "utterance": " ".join(quotes)}, False)
        turn = {"event": "turn_detected", "payload": {"clauses": turn_clauses, "input": action_text}}
        # Speech first when nothing but its tag comes before the first quote.
        lead = [word for word in re.findall(r"[a-z']+", processed[:first_quote])
                if word not in FIRST_PERSON_OPENERS and word not in LEADING_FILLER_WORDS]
        return [dialogue, turn] if all(word in SPEECH_TAG_VERBS for word in lead) else [turn, dialogue]

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
        hypothetical = hypothetical_spans(processed)
        cursor = 0
        # Spoken quotes are talk, not verbs (mask_speech_quotes): same length, so positions agree.
        scanned = mask_talk(processed)

        for clause in split_action_clauses(scanned):
            start = scanned.find(clause, cursor)
            cursor = start + len(clause)
            clause_intent = detect_item_intent(normalize_declared_verb(clause))
            if clause_intent in HYPOTHETICAL_BLOCKED_INTENTS and any(a <= start < b for a, b in hypothetical):
                remaining_clauses.append(clause)
                continue
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
                if item_name and clause_intent in ITEM_LOSING_INTENTS and not _clause_names_item(clause, item_name):
                    item_name = None
                if item_name:
                    turn_clauses.append({
                        "kind": "item", "intent": clause_intent, "item_name": item_name,
                        "phrase": extract_item_phrase(clause, clause_intent),
                    })
                    continue
                # A recognized "examine"/"take"/"give"/"trade" verb but no matching item name --
                # fall through to skill matching below rather than silently dropping it (ex:
                # could still be a legitimate skill phrase that happens to contain one of
                # these words), but remember it in case skill matching also comes up empty.
                if clause_intent in IMPROVISABLE_INTENTS:
                    unmatched_item_verbs.append({"intent": clause_intent, "phrase": clause})
            remaining_clauses.append(clause)

        return turn_clauses, remaining_clauses, found_exempt, unmatched_item_verbs

    def _classify_skill_pass(self, remaining_clauses, turn_clauses, processed="", item_verb_clauses=frozenset()):
        """!
        @brief Pass 2: skill/ability matching for whatever clauses the item pass didn't
            already claim. Appends matched clauses directly onto turn_clauses.
        @param remaining_clauses The item pass's own leftover clause list.
        @param turn_clauses The item pass's own accumulated list -- matched skill/ability
            entries are appended here directly.
        @param item_verb_clauses Clauses whose item verb named nothing real (the item pass's
            unmatched_item_verbs) -- only a direct match (MIXED_ACTION_MIN_SCORE) takes one, so a
            keyword-fallback guess never beats _finalize's improvisation for it. Found by
            playtest: "i pick up the damp ledger book" the narrator had just described rolled
            finesse at 0.21 instead of making the book real.
        @return best_score, the highest confidence score seen across every clause tried, for
            action_not_understood's own payload if nothing else claims the turn.
        """
        best_score = 0.0
        for clause in remaining_clauses:
            words = re.findall(r"[a-z']+", clause)
            while words and words[0] in FIRST_PERSON_OPENERS:
                words = words[1:]
            if words and words[0] in GESTURE_VERBS:
                continue
            # A trained combat-trick/metamagic modifier (ex: "power attack", "empowered") is
            # named literally, not semantically -- checked and stripped before map_to_action
            # runs on the remainder, so its own phrase never dilutes the base ability's match
            # (see match_modifier's own docstring). Matched clause text (post-strip) is what
            # both map_to_action and map_to_target score against below.
            modifier_name, matched_clause = self.matcher.match_modifier(clause)
            clause_skill, clause_score = self.matcher.map_to_action(matched_clause)
            if modifier_name and not clause_skill:
                # Stripping the modifier's own name left nothing map_to_action could match at
                # all (ex: "power attack the goblin" -> "the goblin", once "power attack"
                # itself -- the only word that named a weapon skill at all -- is gone). Retry
                # against the original, unstripped clause instead; modifier entities are never
                # themselves embedded (see on_rules_loaded's own supertype == "modifier"
                # exclusion), so this can only resolve to a real base ability/skill, never back
                # to the modifier itself. "empowered fireball" never reaches this branch --
                # "fireball" alone already matches confidently once "empowered" is stripped.
                clause_skill, clause_score = self.matcher.map_to_action(clause)
            best_score = max(best_score, clause_score)
            if not clause_skill or (clause in item_verb_clauses and clause_score < MIXED_ACTION_MIN_SCORE):
                continue
            action = {"kind": "action", "skill": clause_skill, "score": clause_score}
            if modifier_name:
                action["modifier"] = modifier_name
            # A confidently-matched creature name (ex: "attack the second wolf") is attached
            # as a target hint alongside the matched skill -- unlike item/save-load intent,
            # this never gates or replaces skill matching, it only enriches the same turn
            # entry. DMCore is what actually decides whether to honor it. Resolved per clause,
            # not against the whole input, so "attack the orc and cast a ward on thane" can
            # redirect each action at its own named target.
            target_name, _target_score = self.matcher.map_to_target(matched_clause)
            if not target_name and processed != clause and REFERRING_PRONOUN_PATTERN.search(matched_clause):
                # "shove hemlock into the river and start kicking him" -- the kick's own clause
                # only says "him"; whoever the whole input names is who "him" is.
                target_name, _target_score = self.matcher.map_to_target(processed)
            if target_name:
                action["target"] = target_name
            turn_clauses.append(action)
        return best_score

    def _finalize(
        self, processed, turn_clauses, found_exempt, unmatched_item_verbs, best_score, events, adjudication, raw_input="",
    ):
        """!
        @brief Decides the turn's final event once both passes have run, in order: a merged
            turn_detected if anything claimed the turn; nothing at all if an exempt clause
            already claimed the whole input; else the semantic router (_route_intent); else an
            improvisation_requested fallback if a recognized-but-unmatched item verb is
            available; else action_not_understood.
        @param events The classify()-owned events list; the final decision is appended here.
        """
        if turn_clauses:
            # Always published this way, even for the overwhelmingly common single-clause,
            # single-kind case -- see DM_Core.py's own "Multiple actions" docstring for why
            # the whole downstream pipeline (DMCore, LLMCore) is built around one consistent
            # shape rather than special-casing N=1 or a single clause kind.
            events.append({"event": "turn_detected", "payload": {"clauses": turn_clauses, "input": processed}})
            return
        if found_exempt:
            # An exempt clause (ex: a bare "retreat") already published its own free-standing
            # event and claimed the whole input -- nothing further to decide.
            return

        # Semantic backstop, reached only now that both passes have declined -- so it can never
        # shadow a real skill/item/dialogue match (see INTENT_PROTOTYPES). Called lazily, here
        # rather than up front, so an ordinary turn never pays for the encode at all.
        #
        # Ordered AHEAD of improvisation, but gated harder when improvisation is actually
        # available. "After improvisation" would really mean "never" for any input carrying a
        # recognized item verb, because DM_Improvisation.py's own decline path publishes
        # action_not_understood itself, from DMCore, where there is no matcher to consult -- and
        # that overlap set is exactly the wrong one to lose: EXAMINE_KEYWORDS' own "look at"/
        # "check out" make "look at who's here" and "check out the room" recognized examine
        # verbs, so they'd reach ad hoc item generation and be asked to conjure "who's here" as
        # a physical object, the precise invention the scene-query channel exists to prevent.
        # Displacing a working-ish path needs stronger evidence than a clean give-up does,
        # hence strict=.
        routed = self._route_intent(processed, strict=bool(unmatched_item_verbs))
        if routed:
            events.append(routed)
            return

        if unmatched_item_verbs:
            # The whole turn would otherwise resolve to nothing at all, but at least one clause
            # was a recognized item verb naming something that just doesn't exist yet -- last
            # resort before giving up: DM_Improvisation.py's own generate_ad_hoc_item gets a
            # chance to decide whether that's plausible to conjure into the scene (ex: "pick up
            # a stone"). Only the first candidate is used -- extending this into a genuinely
            # multi-clause improvisation attempt is out of scope for now.
            candidate = unmatched_item_verbs[0]
            events.append({"event": "improvisation_requested", "payload": {
                "intent": candidate["intent"], "phrase": candidate["phrase"], "input": processed,
                # The player's words for the item alone, for a notice to quote (extract_item_phrase).
                "item_phrase": extract_item_phrase(candidate["phrase"], candidate["intent"]),
            }})
            return

        has_listener = self.conversation_partner is not None or self.anyone_present
        verdict = None
        if has_listener and adjudication.may_ask():
            verdict = self._adjudicate(processed, "not_understood", adjudication)
            verdict_event = self._verdict_event(verdict, processed, raw_input, adjudication)
            if verdict_event:
                events.append(verdict_event)
                return
        item_event = self._adjudicated_item_event(processed, adjudication, raw_input)
        if item_event:
            events.append(item_event)
            return
        if verdict is not None:
            # The model judged it an action, but nothing here could resolve it -- an attempt
            # that failed, told to the player out of character rather than narrated.
            events.append({"event": "action_not_understood", "payload": {
                "input": processed, "score": best_score, "reason": "unresolved_action",
            }})
            return

        if processed.rstrip().endswith("!") and has_listener:
            # Nothing claimed it and someone can hear it: a barked line is said to them. Found by
            # playtest: "stay right there!", "keep your hands up!" and "hey! get back here!" were
            # six of a brawler's fifteen not-understood turns, mid-fight with a listener.
            events.append(self._dialogue_event(processed, frame_speech(raw_input, processed, False), True))
            return

        # Below confidence_threshold on every remaining clause, the item pass found nothing, and
        # the semantic router declined too: publish this instead of staying silent, so the player
        # gets some response rather than the app appearing to stall.
        events.append({"event": "action_not_understood", "payload": {
            "input": processed, "score": best_score, "reason": "unmatched",
        }})

    def _route_intent(self, processed, strict):
        """!
        @brief The semantic intent backstop (see INTENT_PROTOTYPES/_finalize) -- maps a whole
            input that every keyword gate and both matching passes already declined onto one of
            the routable intents, and builds the SAME event that intent's own keyword gate would
            have, so nothing downstream can tell the two producers apart.
        @param processed The whole processed input.
        @param strict True when a recognized-but-unmatched item verb is available, so an
            improvisation attempt is what this would be displacing rather than a bare
            action_not_understood -- raises the bar the match has to clear.
        @return One {"event", "payload"} dict, or None to let the caller fall through.
        """
        intent, _score = self.matcher.map_to_intent(processed, strict=strict)
        if not intent:
            return None
        if intent in QUESTION_BLOCKED_ROUTES and "?" in processed:
            return None
        if intent == "scene_query":
            return {"event": "scene_query_detected", "payload": {"input": processed}}
        if intent == "travel":
            return _travel_event(processed, self.matcher)
        # Every other routable intent is a free-standing item interaction (intents/registry.py's
        # own HANDLERS), carrying no item name -- the exact shape detect_item_intent's own exempt
        # clauses already publish.
        return {"event": "item_interaction_detected", "payload": {
            "intent": intent, "item_name": None, "input": processed, "score": None,
        }}
