"""!
@file Narration_Prompts.py
@brief Everything that decides *what the narrator is told*, as plain functions over a
    NarratorState and the event payloads -- no network, no threads, no EventBus. LLMCore keeps
    the transport: the rolling context window, publish ordering, sourcebook retrieval and the
    request itself. Each trigger has one builder here (scene_intro, item_interaction, arrest,
    ...) returning what to do with it:

    - Narration: a prompt to send, with the system-message kind that frames it (the standing
      Game Master, a dialogue reply, the out-of-character ADaM, a scene query), its retrieval
      query, presence tags and label;
    - Notice: an out-of-character line for the player, no narration (a failed attempt, a fled
      arrest);
    - Skip: nothing to do beyond the log line.

    Every result carries the log line LLMCore publishes for it. The system-message builders take
    the already-retrieved sourcebook text, so nothing here calls out to anything.

    NarratorState is the little bit of scene knowledge the prompts read -- scenario name/
    description, who is present, where the player is, the population hint. LLMCore holds one and
    exposes its fields as attributes; the free-standing intents' narrate() functions receive it
    directly (intents/registry.py), which is why they can be tested without an LLMCore.
"""

import re
from dataclasses import dataclass

from dm.DM_ActionOutcome import (
    ActionPreventedOutcome, CraftEffect, CureEffect, DamageEffect, DefenderDetailsEffect, DispelEffect,
    LanguageBarrierOutcome, LootEffect, MissingMaterialsOutcome, MissingSpellMaterialsOutcome,
    MissingStationOutcome, MovementOutcome, NotCraftableOutcome, OutOfRangeOutcome, RevealEffect,
    RolledOutcome, SummonEffect, TeleportEffect, TransferOutcome,
)
from intents.item_named import DEFAULT_ITEM_INTENT, ITEM_NAMED, coin_text as _coin_text
from intents.registry import HANDLERS as FREE_STANDING_INTENT_HANDLERS
from resolution.Inventory_Resolution import format_currency

USER_REFERENCE_PATTERN = re.compile(r"\b(the user)('s)?\b", re.IGNORECASE)

def address_player_as_you(text):
    """!
    @brief Rewrites the chat API's own word for the player ("the user") as "you". Found by
        playtest: an NPC once "stares at the user", and every later reply copied it from the
        history -- 30 turns of it.
    @param text A model reply.
    @return text with "the user" -> "you" and "the user's" -> "your", capitalized at a
        sentence start.
    """
    def replace(match):
        word = "your" if match.group(2) else "you"
        return word.capitalize() if match.group(1)[0] == "T" else word
    return USER_REFERENCE_PATTERN.sub(replace, text)

def _format_damage_effect(effect, actor):
    return f" {effect.defender} takes {effect.net_damage} damage ({effect.remaining_hp} HP remaining)."

def _format_reveal_effect(effect, actor):
    return f" The check reveals: {', '.join(effect.tags)}." if effect.tags else ""

# Out-of-character replies for an attempt the engine couldn't resolve (an action_not_understood
# "reason", or an item denial below) -- sent as a "player_notice" instead of narrated, so a
# failure never becomes prose the narrator fills with things that didn't happen (found by
# playtest: an unresolved "I'll buy the lantern" was narrated as the shopkeeper handing it over).
# Nothing reaches the context window and no game time passes; the player just tries again.
FAILED_ATTEMPT_MESSAGES = {
    "unresolved_action": (
        "Not sure what that does in the game. Try saying what you do and who or what it's aimed "
        "at -- \"punch the guard\", \"search the crate\", \"buy the rope\"."
    ),
    "unmatched": "Didn't catch that. Say what your character does, or put what they say in quotes.",
    "not_present": "There's no \"{item_name}\" here to {intent}.",
    "no_recipient": "There's no one here to give that to.",
    "no_seller": "There's no one here to buy \"{phrase}\" from.",
    "improvisation_declined": "\"{phrase}\" isn't something you can {intent} here.",
    "improvisation_unavailable": "Couldn't work that out just now -- try again.",
}

def _format_loot_effect(effect, actor):
    gained = []
    if effect.currency:
        gained.append(effect.currency_text or format_currency(effect.currency))
    gained.extend(effect.items)
    return f" The player gains: {', '.join(gained)}." if gained else ""

def _format_summon_effect(effect, actor):
    return f" {actor.capitalize()} summons {effect.name} to fight at their side."

def _format_craft_effect(effect, actor):
    return f" {actor.capitalize()} finishes crafting {effect.item_name}."

def _format_defender_details_effect(effect, actor):
    return f"\n{effect.text}"

def _format_teleport_effect(effect, actor):
    if effect.location:
        return f" {effect.entity.capitalize()} vanishes and reappears at {effect.location}."
    return f" {effect.entity.capitalize()} vanishes and reappears elsewhere in the fight."

def _format_dispel_effect(effect, actor):
    return f" {effect.name.capitalize()} unravels and vanishes."

def _format_cure_effect(effect, actor):
    if not effect.conditions:
        return ""
    return f" {effect.target.capitalize()} is cured of {', '.join(effect.conditions)}."

# describe_outcome's own dispatch table for a RolledOutcome's Effect list -- each formatter
# takes (effect, actor) and returns a narration fragment (leading with its own space/newline,
# or "" if it has nothing to add), so a new Effect subtype only ever needs one new entry here,
# never a change to describe_outcome's own dispatch logic. Order matters -- narration reads
# damage first, then what a check revealed, then what was gained/summoned/crafted/teleported/
# dispelled/cured, with any defender flavor text trailing last.
_EFFECT_FORMATTERS = {
    DamageEffect: _format_damage_effect,
    RevealEffect: _format_reveal_effect,
    LootEffect: _format_loot_effect,
    SummonEffect: _format_summon_effect,
    CraftEffect: _format_craft_effect,
    TeleportEffect: _format_teleport_effect,
    DispelEffect: _format_dispel_effect,
    CureEffect: _format_cure_effect,
    DefenderDetailsEffect: _format_defender_details_effect,
}

_EFFECT_ORDER = [
    DamageEffect, RevealEffect, LootEffect, SummonEffect, CraftEffect, TeleportEffect, DispelEffect,
    CureEffect, DefenderDetailsEffect,
]

def _format_out_of_range_outcome(outcome, actor):
    return (
        f"Skill used: {outcome.skill} -- {outcome.defender or 'the target'} is too far "
        f"away to reach with this right now, so no roll is attempted."
    )

def _format_language_barrier_outcome(outcome, actor):
    return (
        f"Skill used: {outcome.skill} -- {actor.capitalize()} and "
        f"{outcome.defender or 'the target'} share no language, so the attempt never even "
        f"lands and no roll is attempted."
    )

def _format_action_prevented_outcome(outcome, actor):
    return (
        f"{actor.capitalize()} tries to act, but a condition holding them (ex: pinned) "
        f"leaves them unable to do anything at all this turn, so no roll is attempted."
    )

def _format_missing_spell_materials_outcome(outcome, actor):
    return (
        f"Skill used: {outcome.skill} -- {actor.capitalize()} lacks the "
        f"material component this needs, so no roll is attempted."
    )

def _format_not_craftable_outcome(outcome, actor):
    return f"{actor.capitalize()} tries to craft {outcome.item_name}, but there's no known way to make that."

def _format_missing_station_outcome(outcome, actor):
    return f"Crafting {outcome.item_name} needs a {outcome.station} nearby, and none is here."

def _format_missing_materials_outcome(outcome, actor):
    return f"{actor.capitalize()} doesn't have the materials on hand to craft {outcome.item_name}."

def _format_rolled_outcome(outcome, actor):
    if getattr(outcome, "trivial", False):
        # Rated "trivial" (DMCore._untargeted_difficulty) -- no dice at all.
        return f"Skill used: {outcome.skill} - trivial, no roll needed; it simply happens."
    success_word = "succeeds" if outcome.success else "fails"
    incidental = getattr(outcome, "incidental_target", False)
    if incidental:
        # Rolled against whoever was the default target, not anyone the player aimed it at --
        # naming them invites the narrator to make it an attack on them (DM_ActionOutcome.py).
        opposition = ""
    elif outcome.opposing_skill:
        opposition = f" opposed by {outcome.defender}'s {outcome.opposing_skill}"
    elif outcome.defender:
        opposition = f" against {outcome.defender} (no defense)"
    else:
        opposition = ""

    # Dispatched by type rather than a fixed set of if-checks -- a new Effect subtype only
    # ever needs a new _EFFECT_FORMATTERS entry, never a change here. _EFFECT_ORDER (not
    # insertion order) fixes the narration order regardless of which producer appended which
    # effect first.
    effects_by_type = {}
    for effect in outcome.effects:
        if incidental and isinstance(effect, DefenderDetailsEffect):
            # Describing the bystander would bring back exactly who the line above leaves out.
            # Found by playtest: still introduced, the sheriff took a shoulder-check.
            continue
        effects_by_type.setdefault(type(effect), []).append(effect)
    effects_text = "".join(
        _EFFECT_FORMATTERS[effect_type](effect, actor)
        for effect_type in _EFFECT_ORDER
        for effect in effects_by_type.get(effect_type, [])
    )

    # Found by playtest: told only "brawling ... succeeds" for "strike them until they drop!" with
    # nobody there to strike, the narrator invented an opponent and a fifty-turn fight the game
    # knew nothing about.
    no_opponent_text = (
        " There is no opponent: nobody here is being fought, so the blow meets only air or "
        "objects -- don't invent anyone being hit." if getattr(outcome, "no_opponent", False) else ""
    )
    if incidental:
        no_opponent_text += " It isn't an attack on anyone -- narrate only what the player wrote."
    return (
        f"Skill used: {outcome.skill} "
        f"(rolled {outcome.roll} vs difficulty {outcome.difficulty}{opposition}) "
        f"- the action {success_word}.{effects_text}{no_opponent_text}"
    )

# describe_outcome's own dispatch table for every ActionOutcome variant except
# MovementOutcome (which has no "input"/attempt-line shape at all -- see describe_outcome's
# own early return for it). Mirrors _EFFECT_FORMATTERS' own (x, actor) -> str shape: a
# formatter returns only its own body text, never the shared attempt_line prefix, which
# describe_outcome builds once and prepends regardless of which formatter ran.
_OUTCOME_FORMATTERS = {
    RolledOutcome: _format_rolled_outcome,
    OutOfRangeOutcome: _format_out_of_range_outcome,
    LanguageBarrierOutcome: _format_language_barrier_outcome,
    ActionPreventedOutcome: _format_action_prevented_outcome,
    MissingSpellMaterialsOutcome: _format_missing_spell_materials_outcome,
    NotCraftableOutcome: _format_not_craftable_outcome,
    MissingStationOutcome: _format_missing_station_outcome,
    MissingMaterialsOutcome: _format_missing_materials_outcome,
}


@dataclass
class Narration:
    """!
    @brief A prompt for the narrator. kind picks the system message that frames it: "narration"
        (the standing Game Master -- scenario, cast, location rule), "dialogue" (an NPC's reply,
        grounded only in target_key's own persona/attitude and the history that NPC witnessed),
        "adam" (the out-of-character help persona, a standalone request that never joins the
        context window), "scene_query" (the strict facts-only Game Master, data = the scene
        snapshot).
    """

    prompt: str
    kind: str = "narration"
    rag_query: str | None = None
    present_entities: list | None = None
    label: str | None = None
    notice: str | None = None
    log: str | None = None
    data: dict | None = None
    target_key: str | None = None
    speaker: str | None = None
    persona: str = ""
    attitude: str = ""


@dataclass
class Notice:
    """!@brief An out-of-character line shown to the player instead of narration (a "player_notice")."""

    message: str
    reason: str
    input: str = ""
    log: str | list | None = None


@dataclass
class Skip:
    """!@brief Nothing to narrate this time -- only the log line."""

    log: str | None = None


def failed_attempt(reason, data, log_before=None):
    """!
    @brief The out-of-character Notice for an attempt the engine couldn't resolve (a
        FAILED_ATTEMPT_MESSAGES reason) -- nothing reaches the context window and no game time
        passes; the player just tries again. log_before is a line the caller already owes the log
        (published first).
    """
    fields = {"item_name": "that", "intent": "do that with", "phrase": "that", **{
        key: value for key, value in data.items() if isinstance(value, str) and value
    }}
    return Notice(
        FAILED_ATTEMPT_MESSAGES[reason].format(**fields), reason, data.get("input", ""),
        log=[line for line in (log_before, f"Failed attempt ({reason}): told the player out of character.") if line],
    )


class NarratorState:
    """!
    @brief What the narration prompts know about the scene: the scenario, who is in it, where
        the player is and where they can go. Written by LLMCore as scenario_loaded /
        scene_roster_updated / location_exits_updated arrive (and by the move/travel intents'
        narrate(), which refresh it the moment the party arrives somewhere new).
    """

    def __init__(self):
        self.scenario_name = ""
        self.scenario_description = ""
        self.scenario_characters = []
        # Read by scene_length_instruction; refreshed from DMCore on scenario_loaded/scene_roster_updated.
        self.population = {"sentences": "2-3", "hint": ""}
        # Where the player actually is, and where they can actually go -- see location_rule.
        self.scene_name = ""
        self.exit_names = []

    def location_rule(self, label):
        """!
        @brief The standing "the player stays put" instruction appended to every GM narration's
            system message -- except a real move/travel, the one narration whose whole job is
            arriving somewhere. Found by playtest: twice, the narrator walked the player out of
            Sandpoint's market into a tavern or underground ruins over a few clarification/skill
            replies while the engine never moved, so every later turn described a place the game
            wasn't in. Naming the real exits gives a player who wants to leave the actual way out.
        @param label The narration's own label (see _fetch_and_publish).
        @return The instruction text, or "" when there's no scene to pin to or it's an arrival.
        """
        if not self.scene_name or (label or "").split(":")[-1] in ("move", "travel"):
            return ""
        rule = (
            f"\nThe player is at {self.scene_name} and stays there: never move them to another "
            "place, building, room, or area in this narration -- only the game moves the player. "
            "If they set off somewhere, narrate them heading that way or looking toward it, "
            "still here at the end."
        )
        if self.exit_names:
            rule += f" Ways out from here: {', '.join(self.exit_names)}."
        return rule

    def scene_length_instruction(self, kind):
        """!
        @brief The closing instruction of a scene-setting narration prompt (scenario intro,
            arrival, move): how many sentences to write and, where the location opts in to
            narration-driven population, a request to show the place populated. Longer prose for
            those locations is deliberate -- it gives DMCore's extraction pass (see
            DM_Improvisation.py's _on_scene_narration_ready) time to run while the player reads.
        @param kind What is being narrated, ex: "the opening scene", "arriving in this new area".
        @return The instruction text.
        """
        sentences = self.population.get("sentences") or "2-3"
        text = f"Narrate {kind} in {sentences} sentences as the Game Master."
        hint = self.population.get("hint")
        if hint:
            text += (
                f" Show the place populated ({hint}): name a few of the people, say what each "
                f"does, and give each something specific they are doing right now."
            )
        return text


def describe_outcome(outcome, actor="the player"):
    """!
    @brief Builds the shared roll/damage description used by every narration prompt --
        dispatches on outcome's own type (DM_ActionOutcome.py's tagged union) via
        _OUTCOME_FORMATTERS rather than probing an untyped dict for whichever optional keys
        happened to be set, or hand-copying a new isinstance branch per variant -- a new
        ActionOutcome variant only ever needs one new _OUTCOME_FORMATTERS entry (see
        tests/test_llm.py's own completeness test), mirroring how a new Effect subtype only ever
        needs a new _EFFECT_FORMATTERS entry.
    @param outcome One ActionOutcome variant (from an "action_resolved"/"round_resolved"
        payload's own "actions" list, or a "turns" entry's own "outcome").
    @param actor Who performed this action, for the leading "X attempts" line -- defaults
        to the player, but a creature's own behavior-driven action (ex: a wolf's bite)
        passes its own name instead so the narration doesn't misattribute it.
    @return The outcome description as a string.
    """
    # A creature/ally's own turn was a move rather than an attack -- either a deliberate
    # `action = "advance"`/"retreat"` behavior entry (ex: fleeing once badly hurt) or its
    # own fallback when the attack it chose couldn't currently reach its target. No roll
    # happens for a move, so this is worded as repositioning, not a missed attack --
    # mirrors the player's own "advance"/"retreat" wording in
    # generate_item_interaction_response, just per-actor. Excluded from _OUTCOME_FORMATTERS
    # entirely -- unlike every other variant, it carries no "input" at all, so it has no
    # attempt_line prefix to share in the dispatch below.
    if isinstance(outcome, MovementOutcome):
        verb = "advances toward" if outcome.direction == "advance" else "retreats from"
        opponent = outcome.opponent or "its target"
        return f"{actor.capitalize()} {verb} {opponent} ({outcome.before} -> {outcome.after} bands away)."

    # An NPC's own autonomous "steal"/"gift" behavior entry -- same "no 'input' at all"
    # exclusion from _OUTCOME_FORMATTERS as MovementOutcome above.
    if isinstance(outcome, TransferOutcome):
        item_text = "some coin" if outcome.item_name == "currency" else outcome.item_name
        verb = f"steals {item_text} from" if outcome.direction == "steal" else f"gives {item_text} to"
        return f"{actor.capitalize()} {verb} {outcome.target}."

    attempt_line = f"{actor.capitalize()} attempts: \"{outcome.input}\"\n" if outcome.input else ""
    return attempt_line + _OUTCOME_FORMATTERS[type(outcome)](outcome, actor)


def describe_player_actions(action_result):
    """!
    @brief Describes every action the player attempted this turn (see
        DMCore._on_turn_detected's own "Multiple actions" docstring) -- one
        describe_outcome line per entry in action_result["actions"] (NLPCore always
        publishes this as a list, even for the ordinary single-action turn -- see
        NLP_Core.py's ACTION_CLAUSE_PATTERN/_split_action_clauses), preceded by a note
        naming the shared -1D-per-additional-action penalty whenever there was more than
        one, so the model's narration reads as one character splitting their attention
        across several things at once, not N independent, equally-precise attacks.
    @param action_result The "action_resolved"/"round_resolved" payload.
    @return The combined description string for every action the player attempted this turn.
    """
    actions = action_result.get("actions", [])
    penalty_text = ""
    if len(actions) > 1:
        penalty_text = (
            f"The player attempts {len(actions)} actions this turn -- each one rolls at "
            f"-{len(actions) - 1}D for splitting their attention.\n"
        )
    # Found by playtest: "grabbing the finest jar of spices" mismatched to polearms, the narrator
    # handed the player a polearm they don't own, and the player LLM swung it for 15 turns.
    # The skill is which dice were rolled; what happened is what the player wrote.
    gear = action_result.get("player_gear")
    # The roll stays behind the screen too. Found by playtest: "The successful roll means your
    # strike connects cleanly".
    fidelity_text = (
        "\nNarrate what the player actually tried, as they wrote it -- the skill only says which "
        "dice were rolled, so don't name it or turn the attempt into a different action. Never "
        "mention dice, rolls, difficulty or checks: tell only what happens in the scene."
    )
    if gear is not None:
        fidelity_text += (
            f" The player's gear is exactly: {', '.join(gear) or 'nothing equipped'} -- never "
            "give them a weapon or item they don't have."
        )
    return penalty_text + "\n".join(describe_outcome(action) for action in actions) + fidelity_text


def build_speech_prompt(speaker, speech_form, utterance):
    """!
    @brief The user-role prompt for an ordinary dialogue turn, shaped by how the player
        actually spoke (Intent_Classification.py's frame_speech) -- so "talk to the
        fishmonger" reaches the model as walking up to him, not as words to answer, and
        "ask about the kelp beds" as a question about the kelp beds rather than a command
        to quote. Always second person: the player character is "you", never "the player",
        which the model otherwise copies straight into the reply.
    @param speaker The addressee's display label.
    @param speech_form "greet", "reported", or "verbatim" (anything else reads as verbatim).
    @param utterance The player's own words ("verbatim"), a second-person restatement
        ("reported", ex: "You ask about the kelp beds."), or ignored ("greet").
    @return The prompt string.
    """
    if speech_form == "greet":
        return (
            f"You approach {speaker} to talk. {speaker} speaks first: a greeting or opening "
            f"line, the way they'd actually meet a stranger (or someone they know)."
        )
    if speech_form == "reported":
        return f"Speaking to {speaker}: {utterance}"
    return f"You say to {speaker}: \"{utterance}\""


def build_language_barrier_prompt(player_input, target, target_language, nonsense_phrase):
    """!
    @brief Builds the user-role prompt for a dialogue turn DM_Dialogue.py's
        _detect_language_barrier flagged as sharing no language with the player -- target
        still replies in character (persona/attitude still ground tone, via
        dialogue_system_message), but the actual words must be invented gibberish,
        never a real answer to what was asked.
    @param player_input The player's own raw words -- target can't understand them, but the
        model still needs them to react to *something* being said at all.
    @param target The addressed entity's name.
    @param target_language The tongue target actually spoke (DM_Dialogue.py), or None if
        somehow unresolved.
    @param nonsense_phrase A races.toml-authored style example of what that tongue sounds
        like, or None if no race claims it (ex: a scenario-authored language) -- the model
        is told explicitly not to reuse it verbatim, just to match its phonetic flavor.
    @return The complete prompt string.
    """
    language_name = target_language or "a language you don't know"
    prompt = (
        f"You say to {target}: \"{player_input}\"\n"
        f"{target} does not understand this at all -- {target} only speaks {language_name}, "
        f"a language you don't share. Narrate {target} replying with a short, quoted, "
        "untranslatable-sounding line of invented gibberish in that tongue -- no real words "
        "you could understand, and don't translate or explain it."
    )
    if nonsense_phrase:
        prompt += (
            f" For phonetic flavor only (don't reuse it verbatim, invent your own line in "
            f"a similar style): \"{nonsense_phrase}\"."
        )
    prompt += (
        f" You may narrate {target} adding a brief physical gesture or expression showing "
        "confusion at not being understood either."
    )
    return prompt


def system_message(state, rag_context, label=None):
    """!
    @brief Builds the per-request system message: the standing GM framing plus whatever's
        specific to this exact request (scenario setting/characters, retrieved sourcebook
        lore) -- none of which is ever stored in context_window (see _queue_narration).
        Split out from _queue_narration as its own method purely so it's directly testable
        without mocking the network call.
    @param rag_query The text to retrieve sourcebook lore against (see perform_rag) --
        deliberately *not* always the full narration prompt (see _queue_narration's
        rag_query param for why).
    @param label The narration's own label, for location_rule's move/travel exemption.
    @return The complete system message string for this one request.
    """
    system_message = "You are the Game Master."
    if state.scenario_description:
        system_message += f" Setting: \"{state.scenario_name}\" - {state.scenario_description}"
    if state.scenario_characters:
        system_message += " Characters: " + " | ".join(state.scenario_characters)
    system_message += state.location_rule(label)

    # Retrieved fresh per request from this specific prompt, not stored in context_window --
    # otherwise every future turn would replay every past turn's lore excerpts too, quickly
    # bloating the rolling window (see CLAUDE.md's "Narration triggers" for why setting/
    # characters already follow this same per-request-only pattern instead of being stored).
    if rag_context:
        system_message += (
            "\nReference lore from the campaign sourcebook, relevant to this moment "
            f"(use only what applies; don't contradict it):\n{rag_context}"
        )
    return system_message


def dialogue_system_message(target, persona, attitude, rag_context):
    """!
    @brief The dialogue counterpart to system_message -- narrates target's reply
        the same way system_message narrates everything else (third person, as the
        omniscient Game Master), just grounded only in target's own persona/attitude
        rather than the standing GM framing/full scenario roster. Only the player ever
        speaks in the first person; the model must always write as the narrator quoting
        target, never as target itself.
    @param target The entity being addressed, in-character.
    @param persona describe_character(target)'s own flavor text (DM_Social.py) -- who
        target is, purely descriptive data (no mechanical stats), including its own
        "voice"/"quotes" for how they talk.
    @param attitude describe_attitude(target, player)'s own prose -- target's own
        disposition toward the player, to ground tone (warm, wary, hostile, ...).
    @param rag_query What to retrieve sourcebook lore against (see perform_rag).
    @return The complete system message string for this one dialogue request.
    """
    system_message = (
        f"You are the Game Master, voicing {target} in an ongoing tabletop scene. The "
        f"player character is \"you\" -- never call them \"the player\" or \"the user\"."
    )
    if persona:
        system_message += f"\nWho {target} is: {persona}"
    if attitude:
        system_message += f"\nHow {target} feels about you: {attitude}"
    system_message += (
        f"\nWrite {target}'s reply as mostly {target}'s own spoken words, in quotes, the way "
        f"this particular person really talks: everyday words, contractions, fragments where "
        f"natural. If a voice or known lines are given above, match them; if not, sound like "
        f"an ordinary person of {target}'s station, not a storyteller. At most one short "
        f"action beat (a gesture or expression) -- no scenery, no one else's actions, and "
        f"never narrate what you do. Tag the speech at most once (ex: The innkeeper shrugs. "
        f"\"Can't say I've heard that name.\"). Length follows mood: guarded, busy or "
        f"hostile gets a line or two; friendly and interested, three or four sentences at "
        f"most. Third person only -- never write as {target} in the first person outside "
        f"the quotes.\n"
        "Actually answer what was asked: if you have no specific fact to draw on, invent a "
        "small, plausible, in-setting detail rather than deflecting -- a real person asked a "
        "direct question gives a real answer, even a brief or mistaken one. Only stonewall, "
        "demand clarification, or turn the question aside if who they are or how they feel "
        "above specifically calls for secrecy, suspicion, or hostility, and even then don't "
        "repeat the same deflection you already gave earlier in this conversation -- "
        "escalate or change tack instead."
    )

    if rag_context:
        system_message += (
            "\nReference lore from the campaign sourcebook, relevant to this moment "
            f"(use only what applies; don't contradict it):\n{rag_context}"
        )
    return system_message


def adam_system_message(help_data, rag_context):
    """!
    @brief The ADaM counterpart to dialogue_system_message -- speaks as ADaM, an
        explicitly out-of-character assistant, not the omniscient in-fiction Game Master
        (system_message) or an in-world character (dialogue_system_message).
        Grounded in a static paragraph of general command/verb guidance (the actual
        onboarding gap this persona exists to close) plus help_data's own live snapshot of
        the player's mechanical state and the current scene (DM_Help.py). Also mentions
        help_data's own "removed"/"created_creature"/"edited" outcomes, if present
        (DM_Improvisation.py's _attempt_entity_removal/_attempt_creature_conjuring/
        _attempt_entity_edit, via DM_Help.py's own "removal_candidate"/"creature_candidate"/
        "edit_candidate" handling) -- the one case(s) this payload describes something ADaM
        itself just *did*, not just facts to report.
    @param help_data The "help_resolved" payload.
    @param rag_query What to retrieve sourcebook lore against (see perform_rag).
    @return The complete system message string for this one request.
    """
    system_message = (
        "You are ADaM (Artificial Dungeon and Master), an out-of-character assistant "
        "speaking directly to the player as yourself -- never narrating in-fiction events, "
        "never speaking as the Game Master or any character in the scene. Answer the "
        "player's question plainly and concisely, using only the facts given below; never "
        "invent skills, items, exits, or people that aren't listed. Never mention \"the "
        "facts\", \"the data\", \"the provided lore\" or \"the information provided\" -- "
        "the player can't see them; if something isn't covered, say the game has no rule "
        "or entry for it, and suggest something they can actually do.\n\n"
        "The game understands free text mapped onto these kinds of actions: skill/ability "
        "actions (ex: \"attack the wolf\", \"cast fireball\"); item actions (examine, "
        "equip/wear, unequip/take off, drop, take, give, trade, open, close, use/drink); "
        "movement (advance/retreat within a scene, or a direction to leave a room through "
        "an exit); talking directly to someone present (\"talk to X\", \"ask X about "
        "...\"); directing the party (\"stay behind me\"/\"walk beside me\"); and "
        "save/load (\"save as <name>\", \"load <name>\")."
    )

    if help_data.get("skills"):
        system_message += "\n\nThe player's own skills: " + "; ".join(help_data["skills"])
    if help_data.get("abilities"):
        system_message += "\nThe player's own abilities: " + "; ".join(help_data["abilities"])
    if help_data.get("equipped"):
        equipped = ", ".join(f"{slot}: {item}" for slot, item in help_data["equipped"].items())
        system_message += f"\nCurrently equipped: {equipped}"
    if help_data.get("inventory"):
        system_message += "\nInventory: " + ", ".join(help_data["inventory"])
    if help_data.get("scene_name") or help_data.get("scene_description"):
        system_message += (
            f"\n\nCurrent scene: \"{help_data.get('scene_name', '')}\" - "
            f"{help_data.get('scene_description', '')}"
        )
    if help_data.get("present"):
        system_message += "\nPresent here: " + " | ".join(help_data["present"])
    if help_data.get("ground_items"):
        system_message += "\nOn the ground here: " + "; ".join(help_data["ground_items"])
    if help_data.get("exits"):
        # A room exit carries a "direction" ("forward", to a sibling room in the same
        # location); a location exit doesn't (reachable by naming the destination itself,
        # from anywhere in the location -- see DM_Movement.py's _resolve_travel_intent) --
        # rendered without the "direction (to X)" framing so it doesn't read as "None (to
        # The Sooted Anvil)".
        exits = ", ".join(
            f"{exit_info['direction']} (to {exit_info.get('destination_name')})"
            if exit_info.get("direction") else str(exit_info.get("destination_name"))
            for exit_info in help_data["exits"]
        )
        system_message += f"\nExits from here: {exits}"
    removed = help_data.get("removed")
    if removed and removed.get("removed"):
        system_message += (
            f"\n\nYou just removed \"{removed.get('name')}\" from the scene entirely "
            f"(reason: {removed.get('reason', 'as requested')}) -- mention this happened."
        )
    created_creature = help_data.get("created_creature")
    if created_creature and created_creature.get("created_creature"):
        system_message += (
            f"\n\nYou just conjured \"{created_creature.get('name')}\" into the scene -- "
            "mention this happened, describing what appeared."
        )
    edited = help_data.get("edited")
    if edited and edited.get("edited"):
        system_message += (
            f"\n\nYou just edited \"{edited.get('name')}\" "
            f"(reason: {edited.get('reason', 'as requested')}) -- mention what changed."
        )

    if rag_context:
        system_message += (
            "\nReference lore from the campaign sourcebook, relevant to this moment "
            f"(use only what applies; don't contradict it):\n{rag_context}"
        )
    return system_message


def scene_query_system_message(scene_data, rag_context):
    """!
    @brief The scene-query counterpart to adam_system_message -- the same strict
        "use only the facts given below; never invent" grounding discipline (this intent
        exists specifically to close the "the LLM goes wild describing things that aren't
        there" gap for a bare, unaddressed scene question, the same gap ADaM's own help
        channel already closed for explicitly-addressed OOC questions), but speaks as the
        omniscient in-fiction Game Master, never breaking character into ADaM's own explicit
        meta persona.
    @param scene_data The "scene_query_resolved" payload.
    @param rag_query What to retrieve sourcebook lore against (see perform_rag).
    @return The complete system message string for this one request.
    """
    system_message = (
        "You are the Game Master, answering the player's own direct question about what "
        "their character currently perceives. Answer plainly and concisely, in-fiction, "
        "using only the facts given below; never invent people, items, or exits that "
        "aren't listed, and never describe anything not actually present."
    )
    if scene_data.get("scene_name") or scene_data.get("scene_description"):
        system_message += (
            f"\n\nCurrent scene: \"{scene_data.get('scene_name', '')}\" - "
            f"{scene_data.get('scene_description', '')}"
        )
    if scene_data.get("present"):
        system_message += "\nPresent here: " + " | ".join(scene_data["present"])
    if scene_data.get("ground_items"):
        system_message += "\nOn the ground here: " + "; ".join(scene_data["ground_items"])
    if scene_data.get("exits"):
        # Same "direction (to destination)" vs. bare destination-name rendering
        # adam_system_message's own exits line already uses.
        exits = ", ".join(
            f"{exit_info['direction']} (to {exit_info.get('destination_name')})"
            if exit_info.get("direction") else str(exit_info.get("destination_name"))
            for exit_info in scene_data["exits"]
        )
        system_message += f"\nExits from here: {exits}"

    if rag_context:
        system_message += (
            "\nReference lore from the campaign sourcebook, relevant to this moment "
            f"(use only what applies; don't contradict it):\n{rag_context}"
        )
    return system_message


def scene_intro(state, scenario_data):
    """!
    @brief Narrates the opening scene once, when a scenario is loaded, and remembers the
        scenario's name/description/characters so every later narration stays grounded
        in the setting and who's actually present.
    @param scenario_data The "scenario_loaded" payload ({name, description, characters,
        skip_intro?}).
    """
    log = None
    state.scenario_name = scenario_data.get("name", "")
    state.scenario_description = scenario_data.get("description", "")
    state.scenario_characters = scenario_data.get("characters", [])
    state.population = dict(scenario_data.get("population") or state.population)
    state.scene_name = scenario_data.get("name", "")

    if scenario_data.get("skip_intro"):
        # Set by DMCore's own throwaway pre-load construction (see DMCore.__init__'s own
        # publish_intro_narration param, LLDM.py's on_load_requested) -- this
        # scenario_loaded fires from a DMCore that's about to be immediately superseded by
        # a real load_game() overlay (including a fresh background-NPC roll), so an intro
        # generated from this snapshot could describe a cast the player never actually
        # gets. The bookkeeping just above still updates -- harmless, and DM_Rules.py's own
        # scene_roster_updated corrects "characters" again moments later regardless --
        # only the actual LLM call and the chat message it would produce are skipped.
        return Skip("Skipping scenario intro narration (throwaway pre-load construction).")

    log = "Generating scenario intro narration."

    characters_text = (
        "\nCharacters present: " + " | ".join(state.scenario_characters)
        if state.scenario_characters else ""
    )
    prompt = (
        f"The players are entering a new scenario: \"{state.scenario_name}\".\n"
        f"{state.scenario_description}{characters_text}\n"
        f"{state.scene_length_instruction('the opening scene')}"
    )
    # No player input exists yet for this one -- the scenario's own name/description is
    # already a clean, undiluted query (see _queue_narration's rag_query docstring).
    return Narration(
        prompt, rag_query=f"{state.scenario_name} {state.scenario_description}",
        present_entities=scenario_data.get("present_entities"), label="scenario_intro", log=log,
    )


def round_response(state, action_result):
    """!
    @brief Narrates the end of a combat round, instead of narrating every skill use mid-fight.
    @param action_result The "round_resolved" payload (an action_resolved dict plus "round"
        and, if anyone else acted this round, "turns" -- a list of every other
        participant's own {"actor", "initiative", "outcome"} wrapper, enemies and allies
        alike, sorted by initiative by DMCore._resolve_combat_round).
    """
    log = f"Generating LLM response for combat round {action_result.get('round')}."

    # Each turn opens on its actor. A behavior-driven attack carries no "input", so
    # describe_outcome gives it no "X attempts" line -- found by playtest, an assaulted
    # vendor's bare "Skill used: charisma ..." right after the player's own action was
    # narrated round after round as the player's ("you follow up with a commanding word").
    turns_text = "".join(
        f"\n{turn.get('actor', 'the creature')}'s own turn (not the player's): "
        f"{describe_outcome(turn['outcome'], actor=turn.get('actor', 'the creature'))}"
        for turn in action_result.get("turns", [])
    )
    prompt = (
        f"Combat round {action_result.get('round')}:\n"
        f"{describe_player_actions(action_result)}{turns_text}\n"
        f"Narrate the end of this combat round in 2-3 sentences as the Game Master, "
        f"covering both allies and enemies who acted."
    )
    return Narration(
        prompt, rag_query=action_result.get("input"),
        present_entities=action_result.get("present_entities"),
        label=f"combat_round:{action_result.get('round')}", log=log,
    )


def response(state, action_result):
    """!
    @brief Narrates a single non-combat skill use immediately.
    @param action_result The "action_resolved" payload.
    """
    log = "Generating LLM response."

    prompt = (
        f"{describe_player_actions(action_result)}\n"
        f"Narrate the outcome in 2-3 sentences as the Game Master."
    )
    return Narration(
        prompt, rag_query=action_result.get("input"),
        present_entities=action_result.get("present_entities"), label="skill_response", log=log,
    )


def clarification(state, data):
    """!
    @brief Narrates a brief in-character non-response when the player's input didn't match
        any recognizable skill (below NLPCore's confidence_threshold), so the player gets
        feedback instead of the app silently doing nothing (no dice roll, no event past
        NLPCore) and appearing to have stalled. This is the one narration trigger with the
        least real state behind it -- nothing resolved, nothing found, no reason string the
        way item/dialogue denial carry -- so it's also the easiest for the model to fill the
        gap with invented people/places/events (ex: a scene question phrased in a way
        NLPCore's own keyword gates miss entirely -- see docs/adam-improvisation.md's "Scene
        queries" for the intent this is a fallback *underneath*, not a substitute for). The
        prompt explicitly forbids that rather than relying on the standing system message
        alone, the same belt-and-suspenders convention generate_load_failed_response's own
        "without inventing what the save might have contained" already follows.
    @param data The "action_not_understood" payload ({input, score}).
    """
    log = None
    reason = data.get("reason")
    if reason in FAILED_ATTEMPT_MESSAGES:
        return failed_attempt(reason, data)
    log = "Generating clarification response for unmatched input."

    prompt = (
        f"The player said: \"{data.get('input', '')}\"\n"
        f"This didn't match any recognizable action or skill check - no dice were rolled.\n"
        f"Respond in-character as the Game Master in 1-2 sentences: acknowledge what they "
        f"said without resolving any roll, and without inventing any new character, item, "
        f"or location that hasn't already been established in this scene."
    )
    # This is the single most common place a player asks a genuine lore question (ex: "tell
    # me about Brevoy") that doesn't map to any skill -- exactly why the bare input, not the
    # boilerplate-padded prompt above, has to be what's queried (see _queue_narration).
    return Narration(prompt, rag_query=data.get("input"), label="clarification", log=log)


def item_interaction(state, data):
    """!
    @brief Narrates an "examine"/"take"/"give"/"trade"/"open"/"close" attempt against a
        named item or the scene target, or one of the thirteen free-standing intents (see
        CONTEXT.md's "Free-standing intent") -- each resolved with no dice roll (see
        DMCore._on_item_interaction_detected) and, for the free-standing group, narrated
        entirely by its own module under intents/ (intents/registry.py's own HANDLERS
        manifest), including its own failure-reason text, rather than a branch here.
        A denied attempt ("found" false) narrates its own real reason (DMCore's own
        "locked"/"not_present"/"cant_afford"/... below) but is told explicitly not to invent
        anything past it -- the same "state the real fact, don't embellish with fiction"
        discipline "open"'s own empty/non-empty branches and generate_load_failed_response
        already follow.

        "examine" only ever describes; it's the deliberate alternative to items being
        auto-looted into the player's inventory the moment a container opens (ex: a cursed
        weapon should be seen and described before anyone decides to touch it).
    @param data The "item_interaction_resolved" payload ({intent, item_name, input, found,
        description?, container?, reason?, amount?, price?, ...}) -- for a free-standing
        intent, whatever extra fields its own intents/ module's resolve() attached (see
        that module's own docstring). "item_name" is None for "open"/"close" and every
        free-standing intent, none of which act on a named item.
    """
    log = None
    intent = data.get("intent")
    if data.get("quiet"):
        # Set by IntentClassifier.classify (Intent_Classification.py) when this exempt
        # clause (ex: "advance") shared its turn with real dialogue -- narrating "you push
        # through the crowd" as its own separate LLM call/chat bubble right before the
        # actual NPC reply is redundant noise; the dialogue reply already implies the
        # player reached whoever they addressed. The move itself already happened
        # (DM_Movement.py, before this event ever fired) -- only its own narration is
        # skipped.
        return Skip(f"Skipping item interaction narration ({intent}) -- quiet.")
    log = f"Generating item interaction response ({intent})."

    handler = FREE_STANDING_INTENT_HANDLERS.get(intent)
    if handler:
        _resolve, narrate = handler
        return Narration(
            narrate(state, data), rag_query=data.get("input"),
            present_entities=data.get("present_entities"), label=f"item_interaction:{intent}", log=log,
        )

    item_name = data.get("item_name")
    container = data.get("container")
    # "open"/"close" have no item_name to quote (they act on the target itself); every
    # other intent always has one by the time this fires.
    subject = f"\"{item_name}\"" if item_name else (container or "it")

    if not data.get("found") and data.get("reason") in ("not_present", "no_recipient"):
        # Nothing in the world said no -- the thing or person simply isn't here. Told out
        # of character, not narrated (see FAILED_ATTEMPT_MESSAGES). Quotes what the player
        # said ("belt knife"), not the catalog item it was matched to ("belt pouch").
        return failed_attempt(data["reason"], {**data, "item_name": data.get("phrase") or item_name}, log_before=log)

    if not data.get("found"):
        reason_text = {
            "locked": f"{container or 'it'} is locked shut and can't be reached yet",
            "closed": f"{container or 'it'} is closed and needs to be opened first",
            "not_present": f"there's no \"{item_name}\" here to {intent}",
            "not_takeable": f"{subject} isn't something that can be picked up, given, or traded",
            "not_usable": f"{subject} isn't something that can be used like that",
            "no_recipient": "there's no one here to give it to",
            "not_openable": f"{subject} isn't something that can be opened or closed",
            "already_open": f"{container or 'it'} is already open",
            "already_closed": f"{container or 'it'} is already closed",
            "cant_afford": f"the player can't afford the {_coin_text(data, 'price')} it costs",
            "not_equippable": f"{subject} isn't something that can be worn or wielded",
            "cant_equip": f"{subject} has nothing on the player's own body it could go onto",
            "not_equipped": f"{subject} isn't currently equipped at all",
        }.get(data.get("reason"), f"the player's attempt to {intent} {subject} doesn't apply here")
        prompt = (
            f"The player tries to {intent} {subject} "
            f"(input: \"{data.get('input', '')}\"), but {reason_text} -- no roll involved.\n"
            f"Narrate a brief, in-character explanation in 1-2 sentences as the Game Master, "
            f"stating only that reason -- don't invent any other person, item, or detail to "
            f"explain it."
        )
    else:
        prompt = ITEM_NAMED.get(intent, DEFAULT_ITEM_INTENT).narrate(data)
    return Narration(
        prompt, rag_query=data.get("input"), present_entities=data.get("present_entities"),
        label=f"item_interaction:{intent}", log=log,
    )


def encounter(state, data):
    """!
    @brief Narrates a location/room's own random encounter roll (see DM_Encounters.py) --
        unlike every other trigger here, this one is never a response to something the
        player *did*; it fires as a side effect of simply arriving somewhere. Either a pure
        flavor beat ("description") or a newly-instanced entity ("entity_name") -- never
        both.
    @param data The "encounter_triggered" payload ({description?, entity_name?,
        present_entities}).
    """
    log = "Generating encounter response."

    entity_name = data.get("entity_name")
    if entity_name:
        prompt = (
            f"As the player arrives, something new is here: \"{entity_name}\".\n"
            f"Narrate this arrival in 1-2 sentences as the Game Master, introducing them "
            f"into the scene."
        )
    else:
        prompt = (
            f"As the player arrives: {data.get('description', '')}\n"
            f"Narrate this brief moment in 1-2 sentences as the Game Master."
        )
    return Narration(prompt, present_entities=data.get("present_entities"), label="encounter", log=log)


def arrest(state, data):
    """!
    @brief Narrates a guard's arrest -- the demand ("arrest_confronted") or how it ended
        ("arrest_resolved"), both from DM_Enforcement.py. Every fact (charges, amounts, jail
        time, who is wanted) comes from the payload, which DMCore built from the polity
        record; the narrator only voices it. Fleeing is told out of character: the guard
        is no longer in the scene to narrate.
    @param data The payload -- {enforcer, polity, addressed_as, amount_text, charges,
        present_entities} plus "kind" (arrest/repeat/kill_on_sight) or "outcome" (paid,
        surrendered, bribed, bribe_refused, bluffed, bluff_failed, resisted, fled) and that
        outcome's own facts.
    """
    log = None
    enforcer = data.get("enforcer") or "the guard"
    charges = ", ".join(data.get("charges") or []) or "breaking the law"
    known_as = f" They know you as {data['addressed_as']}." if data.get("addressed_as") else ""
    outcome = data.get("outcome")
    if outcome == "fled":
        return Notice(**{
            "message": f"You fled from {enforcer}. Resisting arrest is now on your record in {data.get('polity')}.",
            "reason": "arrest", "input": "",
        })
    if outcome is None:
        kind = data.get("kind")
        if kind == "kill_on_sight":
            beat = (f"{enforcer} recognizes you -- wanted in {data.get('polity')} for {charges} -- "
                    f"and attacks at once, without offering terms.{known_as}")
        elif kind == "repeat":
            beat = (f"{enforcer} repeats the demand, patience running out: {data.get('amount_text')} "
                    f"for {charges}, or you come along to the cells.")
        else:
            seen = " They saw it happen with their own eyes." if data.get("witnessed") else ""
            # Words, not hands, until the player answers. Found by playtest: the sheriff "places
            # an authoritative hand on your arm" before the player had said anything.
            beat = (f"{enforcer} steps in to arrest you for {charges}.{seen}{known_as} They demand "
                    f"{data.get('amount_text')}, or you come along to the cells. They only tell you "
                    f"so -- don't narrate them touching, grabbing or restraining you.")
    elif outcome == "paid":
        beat = f"You pay {enforcer} the {data.get('paid_text')} owed. The charges are settled."
    elif outcome == "surrendered":
        paid = f" They take the {data.get('paid_text')} you have." if data.get("paid") else ""
        if data.get("blocks"):
            where = data.get("jail_name") or "custody"
            beat = (f"You surrender to {enforcer}.{paid} You serve {data.get('hours')} hours in {where} "
                    f"for the rest, and are released with the charges settled.")
        else:
            beat = f"You surrender to {enforcer}.{paid} That covers it; the charges are settled."
    elif outcome == "bribed":
        beat = f"{enforcer} pockets your {data.get('offer_text')} and looks the other way."
    elif outcome == "bribe_refused":
        beat = f"{enforcer} refuses your offer of {data.get('offer_text')}. The demand stands."
    elif outcome == "bluffed":
        beat = f"{enforcer} believes your story and lets you go, thinking they had the wrong person."
    elif outcome == "bluff_failed":
        beat = f"{enforcer} doesn't believe a word of it. The demand stands."
    else:
        refusal = {
            "attacked": "You answer with violence instead",
            "ignored": f"You ignore {enforcer}'s demand once too often",
        }.get(data.get("how"), "You refuse to submit")
        # Only the turn toward a fight: whether the guard lands a hand on the player is the
        # combat round's to decide. Found by playtest: the narrator had the jailer tackle and
        # pin the player on the spot.
        beat = (f"{refusal}. {enforcer} turns on you, ready to take you by force. Don't narrate "
                f"them grabbing, hitting or restraining you -- the fight hasn't happened yet.")
    prompt = (
        f"{beat}\nNarrate this in 1-3 sentences as the Game Master. Say nothing about amounts, "
        f"charges, jail time or outcomes beyond what is stated here."
    )
    # The reply options, shown after this narration rather than ahead of it (DM_Enforcement.py).
    return Narration(prompt, present_entities=data.get("present_entities"), label="arrest", notice=data.get("notice"), log=log)


def npc_dialogue(state, data):
    """!
    @brief Narrates a direct, in-character reply from whoever the player addressed (see
        DM_Dialogue.py's DialogueMixin/NLP_Core.py's DIALOGUE_KEYWORDS) -- still the
        omniscient third-person Game Master narrating, same as every other trigger here
        (see dialogue_system_message's own docstring for why it's never the named
        entity speaking in the first person), just grounded only in what that entity has
        actually witnessed (see _filter_present_history) rather than the DM's own
        always-full context_window. Addressing a hostile entity is allowed (see
        DialogueMixin._resolve_dialogue) -- whatever the model produces is free to read as
        hostile/dismissive in character, but the attempt itself is never denied for it.

        A "not found" target (no one by that name present, or nothing present at all) falls
        back to an ordinary third-person Game Master explanation instead -- there's no
        persona/attitude to speak from when nothing was actually addressed. That explanation
        names its own real reason (DialogueMixin's own "not_present"/"cant_talk"/
        "no_one_here") but is otherwise told explicitly not to invent anything past it (ex:
        where a named-but-absent target supposedly went) -- without this, the model reliably
        fabricated a whole scene to explain the absence rather than just stating it.
    @param data The "dialogue_resolved" payload ({target, input, found, present_entities,
        persona?, attitude?, reason?, language_barrier?, target_language?,
        nonsense_phrase?}).
    """
    log = None
    target = data.get("target")
    log = f"Generating NPC dialogue response ({target})."

    if not data.get("found"):
        reason_text = {
            "no_one_here": "there's no one here to talk to",
            "not_present": f"{target or 'that'} isn't here to respond",
            "dead": f"{target or 'that'} is dead",
            "cant_talk": f"{target or 'that'} isn't something that can hold a conversation",
        }.get(data.get("reason"), "there's no one who can answer that right now")
        prompt = (
            f"You try to say something (\"{data.get('utterance') or data.get('input', '')}\"), "
            f"but {reason_text} -- no reply is possible.\n"
            f"Narrate a brief, in-character explanation in 1-2 sentences as the Game Master, "
            f"stating only that reason -- don't invent where anyone went, who they're with, "
            f"or any other detail to explain their absence."
        )
        return Narration(
            prompt, rag_query=data.get("input"), present_entities=data.get("present_entities"),
            label=f"dialogue_not_found:{target}", log=log,
        )

    # The DM's own display label ("the Fishmonger") rather than the raw entity key
    # ("market_person_3"), which the model otherwise parrots back as a name.
    speaker = data.get("target_label") or target
    if data.get("language_barrier"):
        prompt = build_language_barrier_prompt(
            data.get("utterance") or data.get("input", ""), speaker,
            data.get("target_language"), data.get("nonsense_phrase"),
        )
    else:
        prompt = build_speech_prompt(
            speaker, data.get("speech_form"), data.get("utterance") or data.get("input", ""),
        )

    return Narration(
        prompt, kind="dialogue", target_key=target, speaker=speaker, persona=data.get('persona', ''), attitude=data.get('attitude', ''),
        rag_query=data.get("input"), present_entities=data.get("present_entities"),
        label=f"dialogue:{target}", log=log,
    )


def load_failed(state, data):
    """!
    @brief Narrates a brief in-character acknowledgment when a requested save slot doesn't
        exist (DMCore's "game_load_failed") -- no roll, no state change, just feedback
        so the request doesn't silently do nothing (same rule
        generate_clarification_response already follows for unmatched input).
    @param data The "game_load_failed" payload ({"slot": slot_name, "reason": ...}).
    """
    log = None
    if data.get("reason", "not_found") == "not_found":
        problem = "no such save exists"
    else:
        problem = "that save is from an incompatible version or is damaged"
    prompt = (
        f"The player tried to load a save named \"{data.get('slot', '')}\", but {problem} "
        f"-- nothing was loaded, no state changed.\n"
        f"Respond in-character as the Game Master in 1-2 sentences, acknowledging the "
        f"failed attempt without inventing what the save might have contained."
    )
    return Narration(prompt, label="load_failed", log=log)


def adam(state, data):
    """!
    @brief Narrates a reply from ADaM, the reserved out-of-character help persona (see
        DM_Help.py's own module docstring for what triggers this and what data it
        gathers) -- always resolves (there's no "not found" case; ADaM isn't a scene
        entity that can be absent) and always speaks directly to the player as an
        explicit meta/OOC assistant, never in-fiction.
    @param data The "help_resolved" payload (DM_Help.py's HelpMixin._on_help_detected).
    """
    log = "Generating ADaM response."
    prompt = f"The player asks ADaM: \"{data.get('input', '')}\""
    return Narration(prompt, kind="adam", data=data, rag_query=data.get("input"), label="adam", log=log)


def scene_query(state, data):
    """!
    @brief Narrates a reply to a free-standing "what do I see"/"who is here" scene query
        (see DM_Help.py's own _on_scene_query_detected) -- always resolves, the same way
        ADaM's own help channel does, but speaks as the ordinary in-fiction Game Master
        instead of ADaM's out-of-character persona, and joins context_window like any other
        narration trigger (unlike ADaM's own deliberately-excluded exchanges), since "there's
        a locked chest here" is exactly the kind of fact a later turn should be able to
        build on.
    @param data The "scene_query_resolved" payload (DM_Help.py's
        HelpMixin._on_scene_query_detected).
    """
    log = "Generating scene query response."
    prompt = f"The player asks: \"{data.get('input', '')}\""
    return Narration(
        prompt, kind="scene_query", data=data, rag_query=data.get("input"),
        present_entities=data.get("present_entities"), label="scene_query", log=log,
    )

