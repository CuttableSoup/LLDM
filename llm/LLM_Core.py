import json
import os
import urllib.request
import threading

from dm.DM_ActionOutcome import (
    CraftEffect, DamageEffect, DefenderDetailsEffect, LootEffect, MissingMaterialsOutcome,
    MissingSpellMaterialsOutcome, MissingStationOutcome, MovementOutcome, NotCraftableOutcome,
    OutOfRangeOutcome, RevealEffect, RolledOutcome, SummonEffect,
)
from llm.LLM_Rag import RagIndex
from paths import PROJECT_ROOT

def _format_damage_effect(effect, actor):
    return f" {effect.defender} takes {effect.net_damage} damage ({effect.remaining_hp} HP remaining)."


def _format_reveal_effect(effect, actor):
    return f" The check reveals: {', '.join(effect.tags)}." if effect.tags else ""


def _format_loot_effect(effect, actor):
    gained = []
    if effect.currency:
        gained.append(f"{effect.currency} currency")
    gained.extend(effect.items)
    return f" The player gains: {', '.join(gained)}." if gained else ""


def _format_summon_effect(effect, actor):
    return f" {actor.capitalize()} summons {effect.name} to fight at their side."


def _format_craft_effect(effect, actor):
    return f" {actor.capitalize()} finishes crafting {effect.item_name}."


def _format_defender_details_effect(effect, actor):
    return f"\n{effect.text}"


# _describe_outcome's own dispatch table for a RolledOutcome's Effect list -- each formatter
# takes (effect, actor) and returns a narration fragment (leading with its own space/newline,
# or "" if it has nothing to add), so a new Effect subtype only ever needs one new entry here,
# never a change to _describe_outcome's own dispatch logic. Order matters -- narration reads
# damage first, then what a check revealed, then what was gained/summoned/crafted, with any
# defender flavor text trailing last.
_EFFECT_FORMATTERS = {
    DamageEffect: _format_damage_effect,
    RevealEffect: _format_reveal_effect,
    LootEffect: _format_loot_effect,
    SummonEffect: _format_summon_effect,
    CraftEffect: _format_craft_effect,
    DefenderDetailsEffect: _format_defender_details_effect,
}
_EFFECT_ORDER = [DamageEffect, RevealEffect, LootEffect, SummonEffect, CraftEffect, DefenderDetailsEffect]


def _format_out_of_range_outcome(outcome, actor):
    return (
        f"Skill used: {outcome.skill} -- {outcome.defender or 'the target'} is too far "
        f"away to reach with this right now, so no roll is attempted."
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
    success_word = "succeeds" if outcome.success else "fails"
    if outcome.opposing_skill:
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
        effects_by_type.setdefault(type(effect), []).append(effect)
    effects_text = "".join(
        _EFFECT_FORMATTERS[effect_type](effect, actor)
        for effect_type in _EFFECT_ORDER
        for effect in effects_by_type.get(effect_type, [])
    )

    return (
        f"Skill used: {outcome.skill} "
        f"(rolled {outcome.roll} vs difficulty {outcome.difficulty}{opposition}) "
        f"- the action {success_word}.{effects_text}"
    )


# _describe_outcome's own dispatch table for every ActionOutcome variant except
# MovementOutcome (which has no "input"/attempt-line shape at all -- see _describe_outcome's
# own early return for it). Mirrors _EFFECT_FORMATTERS' own (x, actor) -> str shape: a
# formatter returns only its own body text, never the shared attempt_line prefix, which
# _describe_outcome builds once and prepends regardless of which formatter ran.
_OUTCOME_FORMATTERS = {
    RolledOutcome: _format_rolled_outcome,
    OutOfRangeOutcome: _format_out_of_range_outcome,
    MissingSpellMaterialsOutcome: _format_missing_spell_materials_outcome,
    NotCraftableOutcome: _format_not_craftable_outcome,
    MissingStationOutcome: _format_missing_station_outcome,
    MissingMaterialsOutcome: _format_missing_materials_outcome,
}

class LLMCore:
    """!
    @brief Main class for handling the local LLM.
    """

    def __init__(self, event_bus, rag_source_dir=None):
        """!
        @brief Initializes the LLM core and loads necessary models.
        @param event_bus The central event bus instance.
        @param rag_source_dir Overrides RagIndex's default Settings/Fantasy/ source directory.
            Exists mainly so tests can point this at a directory with no PDFs (skipping the
            real sourcebook build entirely -- see RagIndex._build's early return) instead of
            every LLMCore() in the test suite kicking off a real, potentially minutes-long
            index build against whatever's actually in Settings/Fantasy/.
        """
        self.event_bus = event_bus
        self.event_bus.publish("log_info", "LLMCore initialized.")
        self.api_url = "http://127.0.0.1:11434/v1/chat/completions"
        # Ollama's OpenAI-compat endpoint 400s without an explicit "model" field (it can have
        # many models pulled at once, unlike LM Studio's "whatever's currently loaded"), so
        # every request built below includes this.
        self.model = "gemma4"
        # Builds itself on a background thread (see RagIndex.__init__) -- perform_rag returns
        # no context at all until it's ready, rather than blocking LLMCore's own boot on
        # potentially minutes of first-time PDF extraction/embedding.
        self.rag_index = RagIndex(event_bus, source_dir=rag_source_dir)
        self.context_window = []
        self.scenario_name = ""
        self.scenario_description = ""
        self.scenario_characters = []
        self.event_bus.subscribe("scenario_loaded", self.generate_scene_intro)
        self.event_bus.subscribe("round_resolved", self.generate_round_response)
        self.event_bus.subscribe("action_resolved", self.generate_response)
        self.event_bus.subscribe("action_not_understood", self.generate_clarification_response)
        self.event_bus.subscribe("item_interaction_resolved", self.generate_item_interaction_response)
        self.event_bus.subscribe("encounter_triggered", self.generate_encounter_response)
        self.event_bus.subscribe("dialogue_resolved", self.generate_npc_dialogue)
        self.event_bus.subscribe("help_resolved", self.generate_adam_response)
        self.event_bus.subscribe("save_requested", self._on_save_requested)
        self.event_bus.subscribe("load_requested", self._on_load_requested)
        self.event_bus.subscribe("game_load_failed", self.generate_load_failed_response)

    def set_setting(self, setting):
        """!
        @brief Repoints the RAG index at Settings/<setting>/ -- called by LLDM.py's own
            start_game right before constructing DMCore, so narration is grounded in the
            sourcebooks for whichever setting the player actually picked (GUICore's Ruleset
            menu, CLI --setting, or a loaded save's own "setting"), not whatever
            self.rag_index happened to default to at LLMCore construction time (before any
            setting was known). A no-op if the resolved source_dir hasn't actually changed
            (ex: the player picked "Fantasy", already this instance's own default, or is
            resuming a second game in the same setting) -- RagIndex._build can take minutes
            the first time, so this must never restart it needlessly.
        @param setting Which Rules/<setting> sibling under Settings/ to index (ex: "Fantasy",
            "Zombie") -- a setting with no matching Settings/<setting>/ directory (no PDFs
            authored yet, ex: "Zombie" today) just yields an empty index, the same
            "no sourcebook, no RAG context" fallback RagIndex._build already applies to a
            missing/empty source_dir.
        """
        source_dir = os.path.join(PROJECT_ROOT, "Settings", setting)
        if source_dir == self.rag_index.source_dir:
            return
        self.rag_index = RagIndex(self.event_bus, source_dir=source_dir)

    def perform_rag(self, query):
        """!
        @brief Retrieves the sourcebook passages most relevant to query, formatted for
            grounding a narration prompt. Delegates the actual embedding/matching to
            self.rag_index (see LLM_Rag.py) -- this method's only job is turning that raw
            (chunk, score) list into prompt-ready text, or "" if there's nothing to add
            (index not ready yet, or nothing cleared the confidence threshold).
        @param query The search query -- in practice, the narration prompt itself (see
            _queue_narration), since a full prompt embeds just as well as a hand-picked
            keyword query and needs no per-call-site plumbing to construct.
        @return The retrieved context as a string, or "" if there's nothing to add.
        """
        matches = self.rag_index.query(query)
        if not matches:
            return ""
        self.event_bus.publish("log_info", f"RAG retrieved {len(matches)} chunk(s) for query.")
        return "\n".join(f"({chunk['source']} p.{chunk['page']}) {chunk['text']}" for chunk, _score in matches)

    def update_context(self, last_turn_actions, conversations):
        """!
        @brief Updates the model context with actions from the last turn and recent conversations.
        @param last_turn_actions A list of actions taken in the previous turn.
        @param conversations The recent dialogue history.
        """
        self.event_bus.publish("log_info", "Updating LLM context.")

    def generate_npc_response(self, npc_memories, npc_quotes):
        """!
        @brief Generates dialogue or actions for an NPC using their memories and quotes.
        @param npc_memories A list of the NPC's specific memories.
        @param npc_quotes A list of quotes associated with the NPC.
        @return The generated output string.
        """
        self.event_bus.publish("log_info", "Generating NPC response.")
        return ""

    def _describe_outcome(self, outcome, actor="the player"):
        """!
        @brief Builds the shared roll/damage description used by every narration prompt --
            dispatches on outcome's own type (DM_ActionOutcome.py's tagged union) via
            _OUTCOME_FORMATTERS rather than probing an untyped dict for whichever optional keys
            happened to be set, or hand-copying a new isinstance branch per variant -- a new
            ActionOutcome variant only ever needs one new _OUTCOME_FORMATTERS entry (see
            test_unit.py's own completeness test), mirroring how a new Effect subtype only ever
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

        attempt_line = f"{actor.capitalize()} attempts: \"{outcome.input}\"\n" if outcome.input else ""
        return attempt_line + _OUTCOME_FORMATTERS[type(outcome)](outcome, actor)

    def _describe_player_actions(self, action_result):
        """!
        @brief Describes every action the player attempted this turn (see
            DMCore._on_turn_detected's own "Multiple actions" docstring) -- one
            _describe_outcome line per entry in action_result["actions"] (NLPCore always
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
        return penalty_text + "\n".join(self._describe_outcome(action) for action in actions)

    def generate_scene_intro(self, scenario_data):
        """!
        @brief Narrates the opening scene once, when a scenario is loaded, and remembers the
            scenario's name/description/characters so every later narration stays grounded
            in the setting and who's actually present.
        @param scenario_data The "scenario_loaded" payload ({name, description, characters}).
        """
        self.event_bus.publish("log_info", "Generating scenario intro narration.")

        self.scenario_name = scenario_data.get("name", "")
        self.scenario_description = scenario_data.get("description", "")
        self.scenario_characters = scenario_data.get("characters", [])

        characters_text = (
            "\nCharacters present: " + " | ".join(self.scenario_characters)
            if self.scenario_characters else ""
        )
        prompt = (
            f"The players are entering a new scenario: \"{self.scenario_name}\".\n"
            f"{self.scenario_description}{characters_text}\n"
            f"Narrate the opening scene in 2-3 sentences as the Game Master."
        )
        # No player input exists yet for this one -- the scenario's own name/description is
        # already a clean, undiluted query (see _queue_narration's rag_query docstring).
        self._queue_narration(
            prompt, rag_query=f"{self.scenario_name} {self.scenario_description}",
            present_entities=scenario_data.get("present_entities"),
        )

    def generate_round_response(self, action_result):
        """!
        @brief Narrates the end of a combat round, instead of narrating every skill use mid-fight.
        @param action_result The "round_resolved" payload (an action_resolved dict plus "round"
            and, if anyone else acted this round, "turns" -- a list of every other
            participant's own {"actor", "initiative", "outcome"} wrapper, enemies and allies
            alike, sorted by initiative by DMCore._resolve_combat_round).
        """
        self.event_bus.publish("log_info", f"Generating LLM response for combat round {action_result.get('round')}.")

        turns_text = "".join(
            f"\n{self._describe_outcome(turn['outcome'], actor=turn.get('actor', 'the creature'))}"
            for turn in action_result.get("turns", [])
        )
        prompt = (
            f"Combat round {action_result.get('round')}:\n"
            f"{self._describe_player_actions(action_result)}{turns_text}\n"
            f"Narrate the end of this combat round in 2-3 sentences as the Game Master, "
            f"covering both allies and enemies who acted."
        )
        self._queue_narration(
            prompt, rag_query=action_result.get("input"),
            present_entities=action_result.get("present_entities"),
        )

    def generate_response(self, action_result):
        """!
        @brief Narrates a single non-combat skill use immediately.
        @param action_result The "action_resolved" payload.
        """
        self.event_bus.publish("log_info", "Generating LLM response.")

        prompt = (
            f"{self._describe_player_actions(action_result)}\n"
            f"Narrate the outcome in 2-3 sentences as the Game Master."
        )
        self._queue_narration(
            prompt, rag_query=action_result.get("input"),
            present_entities=action_result.get("present_entities"),
        )

    def generate_clarification_response(self, data):
        """!
        @brief Narrates a brief in-character non-response when the player's input didn't match
            any recognizable skill (below NLPCore's confidence_threshold), so the player gets
            feedback instead of the app silently doing nothing (no dice roll, no event past
            NLPCore) and appearing to have stalled.
        @param data The "action_not_understood" payload ({input, score}).
        """
        self.event_bus.publish("log_info", "Generating clarification response for unmatched input.")

        prompt = (
            f"The player said: \"{data.get('input', '')}\"\n"
            f"This didn't match any recognizable action or skill check - no dice were rolled.\n"
            f"Respond in-character as the Game Master in 1-2 sentences: acknowledge what they "
            f"said without resolving any roll"
        )
        # This is the single most common place a player asks a genuine lore question (ex: "tell
        # me about Brevoy") that doesn't map to any skill -- exactly why the bare input, not the
        # boilerplate-padded prompt above, has to be what's queried (see _queue_narration).
        self._queue_narration(prompt, rag_query=data.get("input"))

    def generate_item_interaction_response(self, data):
        """!
        @brief Narrates an "examine"/"take"/"give"/"trade"/"open"/"close"/"advance"/"retreat"/
            "formation_behind"/"formation_abreast" attempt, resolved with no dice roll (see
            DMCore._on_item_interaction_detected). "examine" only ever describes; it's the
            deliberate alternative to items being auto-looted into the player's inventory the
            moment a container opens (ex: a cursed weapon should be seen and described before
            anyone decides to touch it).
        @param data The "item_interaction_resolved" payload ({intent, item_name, input, found,
            description?, container?, reason?, amount?, price?, moved?, members?, stance?}).
            "item_name" is None for "open"/"close"/"advance"/"retreat"/"formation_behind"/
            "formation_abreast"/"move"/"travel", which act on the scene directly rather than a
            named item; "moved" (advance/retreat only) is advance_or_retreat's own list of
            {entity, before, after} distance changes; "members"/"stance" (formation only) are
            DMCore._resolve_formation_intent's own resolved party member(s) and new stance;
            "location_name"/"location_description" (travel only) are the arrival location's own
            fields, alongside the same "room_name"/"room_description" "move" already carries.
        """
        intent = data.get("intent")
        self.event_bus.publish("log_info", f"Generating item interaction response ({intent}).")

        item_name = data.get("item_name")
        container = data.get("container")
        # "open"/"close" have no item_name to quote (they act on the target itself); every
        # other intent always has one by the time this fires.
        subject = f"\"{item_name}\"" if item_name else (container or "it")

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
                "cant_afford": f"the player can't afford the {data.get('price', 0)} currency it costs",
                "no_exit": "there's no way through in that direction",
                "wrong_band": "the player isn't standing in the right spot to reach that way out",
                "blocked_by_enemies": "something hostile is still standing in the way",
                "not_equippable": f"{subject} isn't something that can be worn or wielded",
                "cant_equip": f"{subject} has nothing on the player's own body it could go onto",
                "not_equipped": f"{subject} isn't currently equipped at all",
                "no_party": "there's no one from the player's own party here to direct",
            }.get(data.get("reason"), f"the player's attempt to {intent} {subject} doesn't apply here")
            prompt = (
                f"The player tries to {intent} {subject} "
                f"(input: \"{data.get('input', '')}\"), but {reason_text} -- no roll involved.\n"
                f"Narrate a brief, in-character explanation in 1-2 sentences as the Game Master."
            )
        elif intent == "examine":
            # "revealed" is only ever set once DMCore.is_identified(item_name) is true (a
            # passed [entity.test], ex: an arcane check) -- a plain look never carries it, so
            # a hidden property (ex: the cursed dagger's curse) only ever reaches this prompt
            # after a real roll actually earned it.
            revealed = data.get("revealed")
            revealed_text = f" Known properties: {', '.join(revealed)}." if revealed else ""
            prompt = (
                f"The player examines \"{item_name}\".\n"
                f"Description: {data.get('description', '')}{revealed_text}\n"
                f"Narrate what they observe in 2-3 sentences as the Game Master. This is only "
                f"looking -- nothing is taken, moved, or changed."
            )
        elif intent == "open":
            # Real contents (see DMCore._resolve_open_close_intent), never mechanical data --
            # without this the LLM had nothing to narrate from and invented plausible-sounding
            # treasure instead of what's actually inside.
            contents = data.get("contents") or []
            if contents:
                contents_text = "; ".join(contents)
                prompt = (
                    f"The player opens {container}, revealing: {contents_text}.\n"
                    f"Narrate this in 1-2 sentences as the Game Master, describing only what's "
                    f"actually there -- don't invent anything else."
                )
            else:
                prompt = (
                    f"The player opens {container}, and it's empty.\n"
                    f"Narrate this in 1-2 sentences as the Game Master."
                )
        elif intent == "close":
            prompt = (
                f"The player closes {container}.\n"
                f"Narrate this in 1-2 sentences as the Game Master."
            )
        elif intent == "equip":
            # "replaced" is the item that previously occupied this slot, if any -- real state
            # from DMCore._resolve_equip_intent, not invented, same rule every other roll/
            # transfer-bearing narration here already follows.
            replaced = data.get("replaced")
            replaced_text = f", replacing \"{replaced}\"" if replaced else ""
            prompt = (
                f"The player equips \"{item_name}\"{replaced_text}.\n"
                f"Narrate this in 1-2 sentences as the Game Master."
            )
        elif intent == "unequip":
            prompt = (
                f"The player unequips \"{item_name}\".\n"
                f"Narrate this in 1-2 sentences as the Game Master."
            )
        elif intent == "drop":
            prompt = (
                f"The player drops \"{item_name}\".\n"
                f"Narrate this in 1-2 sentences as the Game Master."
            )
        elif intent == "use":
            # "healed"/"poisoned"/"remaining_hp"/"charges_left"/"replaced_with" are
            # DMCore._resolve_use_intent's own real roll/consumption results, never invented --
            # same "feed the LLM the real mechanical outcome" rule every other roll-bearing
            # narration already follows. "healed"/"poisoned" are each 0 when the item carries no
            # such effect -- worded to not claim an effect that didn't happen. An ad hoc-
            # conjured consumable (DM_Improvisation.py) can carry either, so this is also where
            # a "helpful-looking" improvised potion turning out to be poison actually reads as
            # a real twist to the player, not a silent stat change.
            healed = data.get("healed", 0)
            poisoned = data.get("poisoned", 0)
            if healed:
                effect_text = f", restoring {healed} HP (now at {data.get('remaining_hp', 0)} HP)"
            elif poisoned:
                effect_text = f", dealing {poisoned} poison damage (now at {data.get('remaining_hp', 0)} HP)"
            else:
                effect_text = ""
            charges_left = data.get("charges_left", 0)
            if charges_left > 0:
                aftermath = f" It has {charges_left} charge(s) left."
            elif data.get("replaced_with"):
                aftermath = f" All used up, it's left behind only a {data['replaced_with']}."
            else:
                aftermath = " It's completely used up."
            prompt = (
                f"The player uses \"{item_name}\"{effect_text}.{aftermath}\n"
                f"Narrate this in 1-2 sentences as the Game Master."
            )
        elif intent in ("advance", "retreat"):
            # "moved" is advance_or_retreat's own {entity, before, after} list (DM_Movement.py)
            # -- real band-gap numbers already earned by the player's own movement, never
            # invented. Only the player's own band actually changes (see DM_Movement.py's
            # module note), so the effect on any two entities isn't necessarily the same
            # direction -- retreating from current_target can close the gap to something else
            # entirely, which is why this doesn't claim a uniform "moves away from everyone".
            moved = data.get("moved") or []
            if moved:
                movement_text = "; ".join(
                    f"{entry['entity']} ({entry['before']} -> {entry['after']} bands away)" for entry in moved
                )
                verb = "advances" if intent == "advance" else "retreats"
                prompt = (
                    f"The player {verb}, changing how many bands away everyone present now is: "
                    f"{movement_text}.\n"
                    f"Narrate this brief repositioning in 1-2 sentences as the Game Master -- if "
                    f"the numbers show the player got closer to one but farther from another, "
                    f"that's real, not a mistake."
                )
            else:
                prompt = (
                    f"The player tries to {intent}, but there's no one else here for it to matter "
                    f"against.\n"
                    f"Narrate this in 1-2 sentences as the Game Master."
                )
        elif intent in ("formation_behind", "formation_abreast"):
            # "members"/"stance" are DMCore._resolve_formation_intent's own real result --
            # whichever party member(s) it actually resolved (named in the input, or every
            # party member present if none was), never invented.
            members = data.get("members") or []
            members_text = " and ".join(members)
            stance_text = (
                "stay a band behind the player from now on" if data.get("stance") == "behind"
                else "walk abreast of the player from now on"
            )
            prompt = (
                f"The player directs {members_text} to {stance_text}.\n"
                f"Narrate this brief bit of party direction in 1-2 sentences as the Game Master."
            )
        elif intent == "move":
            # Taking a declared exit to a different room of the current location (see
            # DM_Rules.py's room-graph notes) -- unlike advance/retreat (repositioning
            # within one room), this replaces the whole scene, so the room's own name/
            # description/characters (DMCore._resolve_room_transition_intent) get folded into
            # ongoing narration grounding exactly the way generate_scene_intro does for a
            # brand-new scenario -- otherwise every later combat/action prompt in the new
            # room would keep citing the *previous* room's flavor text, stale the moment the
            # player actually moved.
            self.scenario_description = data.get("room_description", "")
            self.scenario_characters = data.get("characters", [])
            characters_text = (
                "\nCharacters present: " + " | ".join(self.scenario_characters)
                if self.scenario_characters else ""
            )
            prompt = (
                f"The player heads {data.get('direction', 'onward')}, arriving at: "
                f"\"{data.get('room_name', '')}\".\n"
                f"{self.scenario_description}{characters_text}\n"
                f"Narrate arriving in this new area in 2-3 sentences as the Game Master."
            )
        elif intent == "travel":
            # Taking a declared [[location.exit]] (or the current location's own "return_to")
            # to a different [[location]] entirely (see DM_Movement.py's _resolve_travel_intent)
            # -- the location-graph counterpart to "move" above, same grounding-refresh
            # reasoning. Grounds on the arrival *room*'s own name/description when the new
            # location has one active (ex: walking straight into a building's own interior),
            # else the location's own name/description (ex: an open town square with nothing
            # more specific to narrate).
            scene_name = data.get("room_name") or data.get("location_name", "")
            self.scenario_description = data.get("room_description") or data.get("location_description", "")
            self.scenario_characters = data.get("characters", [])
            characters_text = (
                "\nCharacters present: " + " | ".join(self.scenario_characters)
                if self.scenario_characters else ""
            )
            prompt = (
                f"The player travels to: \"{scene_name}\".\n"
                f"{self.scenario_description}{characters_text}\n"
                f"Narrate arriving in this new place in 2-3 sentences as the Game Master."
            )
        elif item_name == "currency":
            if intent == "give":
                prompt = (
                    f"The player gives {data.get('amount', 0)} currency to {container}.\n"
                    f"Narrate this in 1-2 sentences as the Game Master."
                )
            else:
                prompt = (
                    f"The player takes {data.get('amount', 0)} currency and adds it to their own.\n"
                    f"Narrate this in 1-2 sentences as the Game Master."
                )
        elif intent == "give":
            prompt = (
                f"The player gives \"{item_name}\" to {container}.\n"
                f"Narrate this in 1-2 sentences as the Game Master."
            )
        elif intent == "trade":
            prompt = (
                f"The player pays {data.get('price', 0)} currency to {container} in exchange "
                f"for \"{item_name}\".\n"
                f"Narrate this brief transaction in 1-2 sentences as the Game Master."
            )
        else:
            prompt = (
                f"The player takes \"{item_name}\" and adds it to their own inventory.\n"
                f"Narrate this in 1-2 sentences as the Game Master."
            )
        self._queue_narration(
            prompt, rag_query=data.get("input"), present_entities=data.get("present_entities"),
        )

    def generate_encounter_response(self, data):
        """!
        @brief Narrates a location/room's own random encounter roll (see DM_Encounters.py) --
            unlike every other trigger here, this one is never a response to something the
            player *did*; it fires as a side effect of simply arriving somewhere. Either a pure
            flavor beat ("description") or a newly-instanced entity ("entity_name") -- never
            both.
        @param data The "encounter_triggered" payload ({description?, entity_name?,
            present_entities}).
        """
        self.event_bus.publish("log_info", "Generating encounter response.")

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
        self._queue_narration(prompt, present_entities=data.get("present_entities"))

    def generate_npc_dialogue(self, data):
        """!
        @brief Narrates a direct, in-character reply from whoever the player addressed (see
            DM_Dialogue.py's DialogueMixin/NLP_Core.py's DIALOGUE_KEYWORDS) -- unlike every
            other narration trigger here, which speaks as the omniscient third-person Game
            Master, this one has the model answer *as* the named entity, first person,
            grounded only in what that entity has actually witnessed (see
            _filter_present_history) rather than the DM's own always-full context_window.
            Addressing a hostile entity is allowed (see DialogueMixin._resolve_dialogue) --
            whatever the model produces is free to read as hostile/dismissive in character,
            but the attempt itself is never denied for it.
        @param data The "dialogue_resolved" payload ({target, input, found, present_entities,
            persona?, attitude?, reason?, language_barrier?, target_language?,
            nonsense_phrase?}).
        """
        target = data.get("target")
        self.event_bus.publish("log_info", f"Generating NPC dialogue response ({target}).")

        if not data.get("found"):
            reason_text = {
                "no_one_here": "there's no one here to talk to",
                "not_present": f"{target or 'that'} isn't here to respond",
                "cant_talk": f"{target or 'that'} isn't something that can hold a conversation",
            }.get(data.get("reason"), "there's no one who can answer that right now")
            prompt = (
                f"The player tries to say something (input: \"{data.get('input', '')}\"), but "
                f"{reason_text} -- no reply is possible.\n"
                f"Narrate a brief, in-character explanation in 1-2 sentences as the Game Master."
            )
            self._queue_narration(
                prompt, rag_query=data.get("input"), present_entities=data.get("present_entities"),
            )
            return

        if data.get("language_barrier"):
            prompt = self._build_language_barrier_prompt(
                data.get("input", ""), target, data.get("target_language"), data.get("nonsense_phrase"),
            )
        else:
            prompt = f"The player says: \"{data.get('input', '')}\""

        self._queue_dialogue(
            target, data.get("persona", ""), data.get("attitude", ""), prompt,
            rag_query=data.get("input"), present_entities=data.get("present_entities"),
        )

    @staticmethod
    def _build_language_barrier_prompt(player_input, target, target_language, nonsense_phrase):
        """!
        @brief Builds the user-role prompt for a dialogue turn DM_Dialogue.py's
            _detect_language_barrier flagged as sharing no language with the player -- target
            still replies in character (persona/attitude still ground tone, via
            _build_dialogue_system_message), but the actual words must be invented gibberish,
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
        language_name = target_language or "a language the player doesn't know"
        prompt = (
            f"The player says: \"{player_input}\"\n"
            f"{target} does not understand this at all -- {target} only speaks {language_name}, "
            "a language the player doesn't share. Reply only with a short, untranslatable-"
            "sounding line of invented gibberish in that tongue -- no real words the player "
            "could understand, and don't translate or explain it."
        )
        if nonsense_phrase:
            prompt += (
                f" For phonetic flavor only (don't reuse it verbatim, invent your own line in "
                f"a similar style): \"{nonsense_phrase}\"."
            )
        prompt += (
            " You may add a brief physical gesture or expression showing confusion at not "
            "being understood either."
        )
        return prompt

    def _build_system_message(self, rag_query):
        """!
        @brief Builds the per-request system message: the standing GM framing plus whatever's
            specific to this exact request (scenario setting/characters, retrieved sourcebook
            lore) -- none of which is ever stored in context_window (see _queue_narration).
            Split out from _queue_narration as its own method purely so it's directly testable
            without mocking the network call.
        @param rag_query The text to retrieve sourcebook lore against (see perform_rag) --
            deliberately *not* always the full narration prompt (see _queue_narration's
            rag_query param for why).
        @return The complete system message string for this one request.
        """
        system_message = "You are the Game Master."
        if self.scenario_description:
            system_message += f" Setting: \"{self.scenario_name}\" - {self.scenario_description}"
        if self.scenario_characters:
            system_message += " Characters: " + " | ".join(self.scenario_characters)

        # Retrieved fresh per request from this specific prompt, not stored in context_window --
        # otherwise every future turn would replay every past turn's lore excerpts too, quickly
        # bloating the rolling window (see CLAUDE.md's "Narration triggers" for why setting/
        # characters already follow this same per-request-only pattern instead of being stored).
        rag_context = self.perform_rag(rag_query)
        if rag_context:
            system_message += (
                "\nReference lore from the campaign sourcebook, relevant to this moment "
                f"(use only what applies; don't contradict it):\n{rag_context}"
            )
        return system_message

    @staticmethod
    def _api_messages(entries):
        """!
        @brief Projects context_window entries down to the bare {"role", "content"} shape the
            chat-completions API actually expects, stripping the "present" bookkeeping tag
            (see _queue_narration's own present_entities param) that never leaves this process
            -- it's local presence metadata for _filter_present_history, not something Ollama
            has any use for.
        @param entries A list of context_window-shaped entries.
        @return The same entries, each reduced to just role/content.
        """
        return [{"role": entry["role"], "content": entry["content"]} for entry in entries]

    def _filter_present_history(self, entity_name):
        """!
        @brief The subset of context_window entity_name was actually present for -- room-level
            presence (see _queue_narration's present_entities param), not the DM's own always-
            full context_window. An entry with no "present" tag at all (ex:
            generate_clarification_response/generate_load_failed_response, neither of which
            DMCore is involved in producing, so there's no scenario_entities to tag them with)
            is excluded rather than assumed witnessed, the same for any entry from before
            entity_name existed or was ever in the same room as whatever it describes. This is
            what generate_npc_dialogue grounds a specific NPC's own reply in, instead of the
            omniscient window every other narration trigger reads from.
        @param entity_name The entity whose own witnessed history is being built.
        @return The ordered subset of context_window entries entity_name's own "present" tag
                includes -- same relative order and the same 100-message ceiling context_window
                itself already enforces; no separate cap here.
        """
        return [entry for entry in self.context_window if entity_name in (entry.get("present") or ())]

    def _fetch_and_publish(self, messages, present_entities, store_in_context=True):
        """!
        @brief The network call + response handling shared by _queue_narration/_queue_dialogue/
            _queue_adam_response's own background fetch threads -- everything downstream of
            "here are the messages to send" is identical either way: POST to Ollama,
            optionally append the reply to the shared context_window (tagged with
            present_entities, same as the prompt that prompted it, so it becomes part of what
            everyone present has now witnessed), and publish llm_response_ready/
            llm_debug_updated. Must never raise -- runs on a background thread with nothing to
            catch an exception it doesn't handle itself (see this file's own module note on
            LLM_Client.py's different contract).
        @param messages The complete [{"role", "content"}, ...] list to send, system message
            included.
        @param present_entities The presence tag to attach to the appended assistant turn.
        @param store_in_context Whether to append the reply to context_window at all -- True
            for every ordinary narration/dialogue trigger, False for _queue_adam_response,
            whose exchanges are deliberately excluded from the shared window entirely (see
            _queue_adam_response's own docstring for why).
        """
        data = {"model": self.model, "messages": messages, "temperature": 0.7, "max_tokens": 4096}
        # Exactly what's about to go over the wire, formatted for a human -- see
        # display_llm_debug (GUI_Core.py)'s Debug tab, not narration itself.
        query_text = "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in messages)
        req = urllib.request.Request(
            self.api_url,
            data=json.dumps(data).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )

        try:
            response = urllib.request.urlopen(req)
            result = json.loads(response.read().decode('utf-8'))
            llm_text = result['choices'][0]['message']['content']
            if store_in_context:
                self.context_window.append({"role": "assistant", "content": llm_text, "present": present_entities})
            self.event_bus.publish("llm_response_ready", llm_text)
            self.event_bus.publish("llm_debug_updated", {"query": query_text, "response": llm_text})
        except Exception as e:
            self.event_bus.publish("log_error", f"LLM connection failed: {e}")
            self.event_bus.publish("llm_response_ready", "System: Could not connect to the local LLM.")
            self.event_bus.publish("llm_debug_updated", {"query": query_text, "response": f"[ERROR] {e}"})

    def _queue_narration(self, prompt, rag_query=None, present_entities=None):
        """!
        @brief Appends a narration prompt to the rolling context window and fetches the LLM's response in the background.
        @param prompt The user-role prompt describing what just happened.
        @param rag_query What to retrieve sourcebook lore against -- defaults to prompt itself,
            but every call site that has the player's own raw input handy (ex:
            generate_clarification_response) passes that instead. A full narration prompt is
            padded with GM framing/roll-result boilerplate ("Narrate the outcome in 2-3
            sentences...", "no dice were rolled", etc.) that dilutes its sentence embedding
            enough to drop a genuinely lore-relevant query below RagIndex's confidence
            threshold -- the exact same dilution problem NLPCore's own map_to_action already
            has to work around (see NLP_Core.py's module notes) -- so the bare player input,
            when available, is a meaningfully better query than the full prompt.
        @param present_entities Room-level presence snapshot (DMCore's own scenario_entities
            at publish time) to tag this exchange with -- see _filter_present_history for what
            consumes it. None (the default) leaves the entry untagged, which excludes it from
            every per-entity dialogue view -- the events that don't carry this yet (ex:
            action_not_understood, game_load_failed) are meta/OOC anyway, nothing an NPC
            should be treated as having "witnessed".
        """
        self.context_window.append({"role": "user", "content": prompt, "present": present_entities})

        if len(self.context_window) > 100:
            self.context_window = self.context_window[-100:]

        system_message = self._build_system_message(rag_query if rag_query else prompt)

        def fetch_from_llm():
            messages = [{"role": "system", "content": system_message}] + self._api_messages(self.context_window)
            self._fetch_and_publish(messages, present_entities)

        threading.Thread(target=fetch_from_llm, daemon=True).start()

    def _build_dialogue_system_message(self, target, persona, attitude, rag_query):
        """!
        @brief The dialogue counterpart to _build_system_message -- speaks as target instead
            of the omniscient Game Master, grounded only in target's own persona/attitude
            rather than the standing GM framing/full scenario roster.
        @param target The entity being addressed, in-character.
        @param persona describe_character(target)'s own flavor text (DM_Social.py) -- who
            target is, purely descriptive data (no mechanical stats).
        @param attitude describe_attitude(target, player)'s own prose -- target's own
            disposition toward the player, to ground tone (warm, wary, hostile, ...).
        @param rag_query What to retrieve sourcebook lore against (see perform_rag).
        @return The complete system message string for this one dialogue request.
        """
        system_message = f"You are {target}, a character in an ongoing tabletop scene."
        if persona:
            system_message += f" {persona}"
        if attitude:
            system_message += f" {attitude}"
        system_message += (
            " Reply only as yourself, in first person -- never narrate, never speak as anyone "
            "else, and never act as the Game Master. A few sentences of spoken dialogue only."
        )

        rag_context = self.perform_rag(rag_query)
        if rag_context:
            system_message += (
                "\nReference lore from the campaign sourcebook, relevant to this moment "
                f"(use only what applies; don't contradict it):\n{rag_context}"
            )
        return system_message

    def _queue_dialogue(self, target, persona, attitude, prompt, rag_query=None, present_entities=None):
        """!
        @brief The dialogue counterpart to _queue_narration -- same rolling-window/background-
            fetch machinery, except the request sent to the model is built from target's own
            presence-filtered view of context_window (_filter_present_history), not the full
            window, under a system message that speaks as target rather than the omniscient
            Game Master (_build_dialogue_system_message). The exchange itself (the player's
            question, target's own reply) is still appended to the *shared* context_window,
            tagged with present_entities the same way any other narration is -- so it becomes
            part of what everyone in the room (including the omniscient narrator, and any
            other NPC present) has now witnessed, letting a second NPC in the same room later
            recall what was just said to the first one.
        @param target The entity being addressed, in-character.
        @param persona describe_character(target)'s own flavor text.
        @param attitude describe_attitude(target, player)'s own prose.
        @param prompt The user-role prompt: the player's own words.
        @param rag_query What to retrieve sourcebook lore against -- the player's own raw
            input, same convention every other narration trigger follows.
        @param present_entities Room-level presence snapshot to tag this exchange with (see
            _queue_narration's own param).
        """
        self.context_window.append({"role": "user", "content": prompt, "present": present_entities})

        if len(self.context_window) > 100:
            self.context_window = self.context_window[-100:]

        system_message = self._build_dialogue_system_message(
            target, persona, attitude, rag_query if rag_query else prompt,
        )

        def fetch_from_llm():
            # Read live at fetch time, not captured up front -- same reasoning
            # _queue_narration's own fetch_from_llm already follows for self.context_window:
            # another narration/dialogue call could append to the shared window between
            # queueing and actually fetching.
            history = self._filter_present_history(target)
            messages = [{"role": "system", "content": system_message}] + self._api_messages(history)
            self._fetch_and_publish(messages, present_entities)

        threading.Thread(target=fetch_from_llm, daemon=True).start()

    def generate_adam_response(self, data):
        """!
        @brief Narrates a reply from ADaM, the reserved out-of-character help persona (see
            DM_Help.py's own module docstring for what triggers this and what data it
            gathers) -- always resolves (there's no "not found" case; ADaM isn't a scene
            entity that can be absent) and always speaks directly to the player as an
            explicit meta/OOC assistant, never in-fiction.
        @param data The "help_resolved" payload (DM_Help.py's HelpMixin._on_help_detected).
        """
        self.event_bus.publish("log_info", "Generating ADaM response.")
        prompt = f"The player asks ADaM: \"{data.get('input', '')}\""
        self._queue_adam_response(prompt, data, rag_query=data.get("input"))

    def _build_adam_system_message(self, help_data, rag_query):
        """!
        @brief The ADaM counterpart to _build_dialogue_system_message -- speaks as ADaM, an
            explicitly out-of-character assistant, not the omniscient in-fiction Game Master
            (_build_system_message) or an in-world character (_build_dialogue_system_message).
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
            "invent skills, items, exits, or people that aren't listed.\n\n"
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

        rag_context = self.perform_rag(rag_query)
        if rag_context:
            system_message += (
                "\nReference lore from the campaign sourcebook, relevant to this moment "
                f"(use only what applies; don't contradict it):\n{rag_context}"
            )
        return system_message

    def _queue_adam_response(self, prompt, help_data, rag_query=None):
        """!
        @brief The ADaM counterpart to _queue_narration/_queue_dialogue -- same background-
            fetch machinery, but deliberately does *not* touch context_window at all, unlike
            every other narration trigger (which all append both the prompt and the reply to
            the shared rolling window). Two reasons: (1) tone -- context_window is replayed in
            full by every future _build_system_message-based GM narration call (no presence
            filtering there, only dialogue's own _filter_present_history does that), so a
            meta/OOC exchange left in it risks the GM later parroting mechanical facts
            in-fiction; (2) budget -- ADaM can be invoked repeatedly with dense payloads (full
            skill lists, exits, etc.), and left in the shared 100-message window that would
            crowd out real narrative history fast. Each invocation is therefore a standalone,
            stateless request: help_data is gathered fresh from live game state every time
            (DM_Help.py), not remembered from a prior ADaM exchange.
        @param prompt The user-role prompt: the player's own words, addressed to ADaM.
        @param help_data The "help_resolved" payload, threaded through to
            _build_adam_system_message.
        @param rag_query What to retrieve sourcebook lore against -- the player's own raw
            input, same convention every other narration trigger follows.
        """
        system_message = self._build_adam_system_message(help_data, rag_query if rag_query else prompt)
        messages = [{"role": "system", "content": system_message}, {"role": "user", "content": prompt}]

        def fetch_from_llm():
            self._fetch_and_publish(messages, present_entities=None, store_in_context=False)

        threading.Thread(target=fetch_from_llm, daemon=True).start()

    def _save_slot_dir(self, slot_name):
        """!
        @brief Mirrors DMCore._save_slot_dir exactly. LLMCore has no reference to DMCore --
            the two cores only ever talk through events -- so this small path helper is
            deliberately duplicated here rather than shared, and must stay in sync with
            DMCore's copy: both write sibling files into the same Saves/<slot_name>/
            directory for a given slot.
        @param slot_name The save slot's name, as given by the player.
        @return The absolute directory path for this slot.
        """
        safe_name = os.path.basename(slot_name.strip()) or "unnamed"
        return os.path.join(PROJECT_ROOT, "Saves", safe_name)

    def save_game(self, slot_name):
        """!
        @brief Writes this core's own slice of a save slot -- the rolling narration
            context_window plus scenario bookkeeping -- to
            Saves/<slot_name>/llm_state.json. DMCore independently writes its own
            dm_state.json sibling for the same slot (see CLAUDE.md's "Saving and loading"
            for why this isn't one combined file).
        @param slot_name The save slot's name (used as a directory name under Saves/).
        """
        slot_dir = self._save_slot_dir(slot_name)
        os.makedirs(slot_dir, exist_ok=True)
        data = {
            "version": 1,
            "context_window": self.context_window,
            "scenario_name": self.scenario_name,
            "scenario_description": self.scenario_description,
            "scenario_characters": self.scenario_characters,
        }
        with open(os.path.join(slot_dir, "llm_state.json"), "w") as f:
            json.dump(data, f, indent=2)
        self.event_bus.publish("log_info", f"LLM narration state saved to slot '{slot_name}'.")

    def load_game(self, slot_name):
        """!
        @brief Restores context_window/scenario bookkeeping from
            Saves/<slot_name>/llm_state.json, silently -- no LLM call, no new narration --
            so resuming a session doesn't reprint an opening-scene intro the way a genuine
            "scenario_loaded" would. A missing file just logs and leaves current state
            alone; DMCore's own load_game is what publishes "game_load_failed" for
            narrating that to the player (see generate_load_failed_response), so this
            doesn't duplicate that feedback.
        @param slot_name The save slot's name to load.
        """
        path = os.path.join(self._save_slot_dir(slot_name), "llm_state.json")
        if not os.path.exists(path):
            self.event_bus.publish("log_error", f"No LLM narration state for slot '{slot_name}'.")
            return

        with open(path, "r") as f:
            data = json.load(f)

        self.context_window = data.get("context_window", [])
        self.scenario_name = data.get("scenario_name", "")
        self.scenario_description = data.get("scenario_description", "")
        self.scenario_characters = data.get("scenario_characters", [])
        self.event_bus.publish("log_info", f"LLM narration state loaded from slot '{slot_name}'.")

    def _on_save_requested(self, data):
        """!
        @brief Event handler for a save request (from NLPCore's text intercept or a GUI/Textual
            button, both publishing the same event as DMCore's own handler).
        @param data The "save_requested" payload ({"slot": slot_name}).
        """
        slot_name = data.get("slot")
        if not slot_name:
            self.event_bus.publish("log_warning", "save_requested with no slot name; ignored.")
            return
        self.save_game(slot_name)

    def _on_load_requested(self, data):
        """!
        @brief Event handler for a load request, mirroring _on_save_requested.
        @param data The "load_requested" payload ({"slot": slot_name}).
        """
        slot_name = data.get("slot")
        if not slot_name:
            self.event_bus.publish("log_warning", "load_requested with no slot name; ignored.")
            return
        self.load_game(slot_name)

    def generate_load_failed_response(self, data):
        """!
        @brief Narrates a brief in-character acknowledgment when a requested save slot doesn't
            exist (DMCore's "game_load_failed") -- no roll, no state change, just feedback
            so the request doesn't silently do nothing (same rule
            generate_clarification_response already follows for unmatched input).
        @param data The "game_load_failed" payload ({"slot": slot_name, "reason": ...}).
        """
        prompt = (
            f"The player tried to load a save named \"{data.get('slot', '')}\", but no such "
            f"save exists -- nothing was loaded, no state changed.\n"
            f"Respond in-character as the Game Master in 1-2 sentences, acknowledging the "
            f"failed attempt without inventing what the save might have contained."
        )
        self._queue_narration(prompt)