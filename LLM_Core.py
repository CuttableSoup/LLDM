"""!
@file LLM_Core.py
@brief Controls the context and output of the large language model.
"""

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