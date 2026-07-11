import json
import os
import urllib.request
import threading

class LLMCore:
    """!
    @brief Main class for handling the local LLM.
    """

    def __init__(self, event_bus):
        """!
        @brief Initializes the LLM core and loads necessary models.
        @param event_bus The central event bus instance.
        """
        self.event_bus = event_bus
        self.event_bus.publish("log_info", "LLMCore initialized.")
        self.api_url = "http://127.0.0.1:1234/v1/chat/completions"
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
        @brief Retrieves augmented generation data from the sourcebook.
        @param query The search query.
        @return The context retrieved from the sourcebook.
        """
        self.event_bus.publish("log_info", f"Performing RAG for query: {query}")
        return ""

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
            f"- the action {outcome}.{damage_text}{loot_text}{details_text}"
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
        self._queue_narration(prompt)

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
        self._queue_narration(prompt)

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
        self._queue_narration(prompt)

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
        self._queue_narration(prompt)

    def generate_item_interaction_response(self, data):
        """!
        @brief Narrates an "examine"/"take"/"give"/"trade"/"open"/"close" attempt, resolved
            with no dice roll (see DMCore._on_item_interaction_detected). "examine" only
            ever describes; it's the deliberate alternative to items being auto-looted into
            the player's inventory the moment a container opens (ex: a cursed weapon should
            be seen and described before anyone decides to touch it).
        @param data The "item_interaction_resolved" payload ({intent, item_name, input, found,
            description?, container?, reason?, amount?, price?}). "item_name" is None for
            "open"/"close", which act on "container" (the scene target) directly.
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
                "no_recipient": "there's no one here to give it to",
                "not_openable": f"{subject} isn't something that can be opened or closed",
                "already_open": f"{container or 'it'} is already open",
                "already_closed": f"{container or 'it'} is already closed",
                "cant_afford": f"the player can't afford the {data.get('price', 0)} currency it costs",
            }.get(data.get("reason"), f"the player's attempt to {intent} {subject} doesn't apply here")
            prompt = (
                f"The player tries to {intent} {subject} "
                f"(input: \"{data.get('input', '')}\"), but {reason_text} -- no roll involved.\n"
                f"Narrate a brief, in-character explanation in 1-2 sentences as the Game Master."
            )
        elif intent == "examine":
            prompt = (
                f"The player examines \"{item_name}\".\n"
                f"Description: {data.get('description', '')}\n"
                f"Narrate what they observe in 2-3 sentences as the Game Master. This is only "
                f"looking -- nothing is taken, moved, or changed."
            )
        elif intent == "open":
            prompt = (
                f"The player opens {container}.\n"
                f"Narrate this in 1-2 sentences as the Game Master."
            )
        elif intent == "close":
            prompt = (
                f"The player closes {container}.\n"
                f"Narrate this in 1-2 sentences as the Game Master."
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
        self._queue_narration(prompt)

    def _queue_narration(self, prompt):
        """!
        @brief Appends a narration prompt to the rolling context window and fetches the LLM's response in the background.
        @param prompt The user-role prompt describing what just happened.
        """
        self.context_window.append({"role": "user", "content": prompt})

        if len(self.context_window) > 100:
            self.context_window = self.context_window[-100:]

        system_message = "You are the Game Master."
        if self.scenario_description:
            system_message += f" Setting: \"{self.scenario_name}\" - {self.scenario_description}"
        if self.scenario_characters:
            system_message += " Characters: " + " | ".join(self.scenario_characters)

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