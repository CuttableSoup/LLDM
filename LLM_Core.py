import json
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

    def _describe_outcome(self, action_result):
        """!
        @brief Builds the shared roll/damage description used by every narration prompt.
        @param action_result A resolved action dict (from "action_resolved" or "round_resolved").
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

        return (
            f"The player attempts: \"{action_result.get('input', '')}\"\n"
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
        @param action_result The "round_resolved" payload (an action_resolved dict plus "round").
        """
        self.event_bus.publish("log_info", f"Generating LLM response for combat round {action_result.get('round')}.")

        prompt = (
            f"Combat round {action_result.get('round')}:\n"
            f"{self._describe_outcome(action_result)}\n"
            f"Narrate the end of this combat round in 2-3 sentences as the Game Master."
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