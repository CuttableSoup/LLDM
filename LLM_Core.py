import json
import os
import urllib.request
import threading

from LLM_Rag import RagIndex

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
        self.api_url = "http://127.0.0.1:1234/v1/chat/completions"
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
        self.event_bus.subscribe("save_requested", self._on_save_requested)
        self.event_bus.subscribe("load_requested", self._on_load_requested)
        self.event_bus.subscribe("game_load_failed", self.generate_load_failed_response)

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

    def _describe_outcome(self, action_result, actor="the player"):
        """!
        @brief Builds the shared roll/damage description used by every narration prompt.
        @param action_result A resolved action dict (from "action_resolved" or "round_resolved",
            or an "enemy_action" sub-result resolved via a creature's own behavior).
        @param actor Who performed this action, for the leading "X attempts" line -- defaults
            to the player, but a creature's own behavior-driven action (ex: a wolf's bite)
            passes its own name instead so the narration doesn't misattribute it.
        @return The outcome description as a string.
        """
        # Set only by resolve_behavior_action (DM_Combat.py) when a creature/ally's own turn
        # was a move rather than an attack -- either a deliberate `action = "advance"`/
        # "retreat"` behavior entry (ex: fleeing once badly hurt) or its own fallback when the
        # attack it chose couldn't currently reach its target. No roll happens for a move, so
        # this is worded as repositioning, not a missed attack -- mirrors the player's own
        # "advance"/"retreat" wording in generate_item_interaction_response, just per-actor.
        if action_result.get("movement"):
            verb = "advances toward" if action_result["movement"] == "advance" else "retreats from"
            opponent = action_result.get("opponent") or "its target"
            return (
                f"{actor.capitalize()} {verb} {opponent} "
                f"({action_result.get('before')} -> {action_result.get('after')} bands away)."
            )

        # Set only by DMCore._on_action_detected/resolve_behavior_action when get_range_modifier
        # (DM_Movement.py) says the target is too far away for this weapon/ability to reach at
        # all -- no roll was attempted (unlike every other outcome this builds a line for), so
        # this is worded as a distance problem rather than a missed attack.
        if action_result.get("reason") == "out_of_range":
            defender = action_result.get("defender")
            input_text = action_result.get("input")
            attempt_line = f"{actor.capitalize()} attempts: \"{input_text}\"\n" if input_text else ""
            return (
                f"{attempt_line}"
                f"Skill used: {action_result.get('skill')} -- {defender or 'the target'} is too far "
                f"away to reach with this right now, so no roll is attempted."
            )

        outcome = "succeeds" if action_result.get("success") else "fails"
        defender = action_result.get("defender")
        opposing_skill = action_result.get("opposing_skill")
        if opposing_skill:
            opposition = f" opposed by {defender}'s {opposing_skill}"
        elif defender:
            opposition = f" against {defender} (no defense)"
        else:
            opposition = ""
        damage = action_result.get("damage")
        if damage:
            damage_text = (
                f" {damage['defender']} takes {damage['net_damage']} damage"
                f" ({damage['remaining_hp']} HP remaining)."
            )
        else:
            damage_text = ""

        # Set only by DMCore._resolve_item_test on a passed check whose outcome had a truthy
        # "reveal" key (ex: the cursed dagger's arcane check) -- the item's own "tags", handed
        # over only now that a real roll actually earned them, never before.
        revealed = action_result.get("revealed")
        revealed_text = f" The check reveals: {', '.join(revealed)}." if revealed else ""

        loot = action_result.get("loot")
        if loot:
            gained = []
            if loot.get("currency"):
                gained.append(f"{loot['currency']} currency")
            gained.extend(loot.get("items", []))
            loot_text = f" The player gains: {', '.join(gained)}." if gained else ""
        else:
            loot_text = ""

        defender_details = action_result.get("defender_details")
        details_text = f"\n{defender_details}" if defender_details else ""

        input_text = action_result.get("input")
        attempt_line = f"{actor.capitalize()} attempts: \"{input_text}\"\n" if input_text else ""

        return (
            f"{attempt_line}"
            f"Skill used: {action_result.get('skill')} "
            f"(rolled {action_result.get('roll')} vs difficulty {action_result.get('difficulty')}{opposition}) "
            f"- the action {outcome}.{damage_text}{revealed_text}{loot_text}{details_text}"
        )

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
        self._queue_narration(prompt, rag_query=f"{self.scenario_name} {self.scenario_description}")

    def generate_round_response(self, action_result):
        """!
        @brief Narrates the end of a combat round, instead of narrating every skill use mid-fight.
        @param action_result The "round_resolved" payload (an action_resolved dict plus "round"
            and, if anyone else acted this round, "turns" -- a list of every other
            participant's resolved action, enemies and allies alike, each already tagged
            with "actor" by DMCore._on_action_detected).
        """
        self.event_bus.publish("log_info", f"Generating LLM response for combat round {action_result.get('round')}.")

        turns_text = "".join(
            f"\n{self._describe_outcome(turn, actor=turn.get('actor', 'the creature'))}"
            for turn in action_result.get("turns", [])
        )
        prompt = (
            f"Combat round {action_result.get('round')}:\n"
            f"{self._describe_outcome(action_result)}{turns_text}\n"
            f"Narrate the end of this combat round in 2-3 sentences as the Game Master, "
            f"covering both allies and enemies who acted."
        )
        self._queue_narration(prompt, rag_query=action_result.get("input"))

    def generate_response(self, action_result):
        """!
        @brief Narrates a single non-combat skill use immediately.
        @param action_result The "action_resolved" payload.
        """
        self.event_bus.publish("log_info", "Generating LLM response.")

        prompt = (
            f"{self._describe_outcome(action_result)}\n"
            f"Narrate the outcome in 2-3 sentences as the Game Master."
        )
        self._queue_narration(prompt, rag_query=action_result.get("input"))

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
        @brief Narrates an "examine"/"take"/"give"/"trade"/"open"/"close"/"advance"/"retreat"
            attempt, resolved with no dice roll (see DMCore._on_item_interaction_detected).
            "examine" only ever describes; it's the deliberate alternative to items being
            auto-looted into the player's inventory the moment a container opens (ex: a
            cursed weapon should be seen and described before anyone decides to touch it).
        @param data The "item_interaction_resolved" payload ({intent, item_name, input, found,
            description?, container?, reason?, amount?, price?, moved?}). "item_name" is None
            for "open"/"close"/"advance"/"retreat", which act on the scene directly rather
            than a named item; "moved" (advance/retreat only) is advance_or_retreat's own
            list of {entity, before, after} distance changes.
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
        elif intent == "use":
            # "healed"/"remaining_hp"/"charges_left"/"replaced_with" are
            # DMCore._resolve_use_intent's own real roll/consumption results, never invented --
            # same "feed the LLM the real mechanical outcome" rule every other roll-bearing
            # narration already follows. "healed" is 0 for an item with no healing effect
            # wired yet (ex: a future wand) -- worded to not claim an effect that didn't happen.
            healed = data.get("healed", 0)
            effect_text = (
                f", restoring {healed} HP (now at {data.get('remaining_hp', 0)} HP)" if healed
                else ""
            )
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
        elif intent == "move":
            # Taking a declared exit to a different room of the current multi-room dungeon
            # (see DM_Rules.py's room-graph notes) -- unlike advance/retreat (repositioning
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
        self._queue_narration(prompt, rag_query=data.get("input"))

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

    def _queue_narration(self, prompt, rag_query=None):
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
        """
        self.context_window.append({"role": "user", "content": prompt})

        if len(self.context_window) > 100:
            self.context_window = self.context_window[-100:]

        system_message = self._build_system_message(rag_query if rag_query else prompt)

        def fetch_from_llm():
            data = {
                "messages": [{"role": "system", "content": system_message}] + self.context_window,
                "temperature": 0.7,
                "max_tokens": 4096
            }
            req = urllib.request.Request(
                self.api_url, 
                data=json.dumps(data).encode('utf-8'), 
                headers={'Content-Type': 'application/json'}
            )
            
            try:
                response = urllib.request.urlopen(req)
                result = json.loads(response.read().decode('utf-8'))
                llm_text = result['choices'][0]['message']['content']
                self.context_window.append({"role": "assistant", "content": llm_text})
                self.event_bus.publish("llm_response_ready", llm_text)
            except Exception as e:
                self.event_bus.publish("log_error", f"LLM connection failed: {e}")
                self.event_bus.publish("llm_response_ready", "System: Could not connect to the local LLM.")

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
        base_dir = os.path.dirname(os.path.abspath(__file__))
        safe_name = os.path.basename(slot_name.strip()) or "unnamed"
        return os.path.join(base_dir, "Saves", safe_name)

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