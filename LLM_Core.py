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
        self.event_bus.subscribe("action_resolved", self.generate_response)

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

    def generate_response(self, action_result):
        self.event_bus.publish("log_info", "Generating LLM response.")

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

        prompt = (
            f"The player attempts: \"{action_result.get('input', '')}\"\n"
            f"Skill used: {action_result.get('skill')} "
            f"(rolled {action_result.get('roll')} vs difficulty {action_result.get('difficulty')}{opposition}) "
            f"- the action {outcome}.{damage_text}\n"
            f"Narrate the outcome in 2-3 sentences as the Game Master."
        )
        self.context_window.append({"role": "user", "content": prompt})

        if len(self.context_window) > 100:
            self.context_window = self.context_window[-100:]

        def fetch_from_llm():
            data = {
                "messages": [{"role": "system", "content": "You are the Game Master."}] + self.context_window,
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