"""!
@file GUI_Core.py
@brief Controls the input and output of the graphical user interface.
"""

class GUICore:
    """!
    @brief Main class handling the display and user interaction.
    """

    def __init__(self, event_bus):
        """!
        @brief Initializes the GUI components.
        @param event_bus The central event bus instance.
        """
        self.event_bus = event_bus
        self.event_bus.publish("log_info", "GUICore initialized.")

    def display_party_status(self, health_data, inventory_data):
        """!
        @brief Renders health and inventory for the party.
        @param health_data The health information of the party members.
        @param inventory_data The inventory contents of the party members.
        """
        self.event_bus.publish("log_info", "Displaying party status.")

    def display_notes(self, notes_content):
        """!
        @brief Shows notes to the user.
        @param notes_content The text or data of the notes.
        """
        self.event_bus.publish("log_info", "Displaying notes.")

    def render_combat_field(self, map_data):
        """!
        @brief Draws a representation of the combat field or map.
        @param map_data The positional and environmental data of the combat field.
        """
        self.event_bus.publish("log_info", "Rendering combat field.")

    def get_user_input(self):
        """!
        @brief Captures input from the user through the interface.
        @return The raw input string or event data.
        """
        self.event_bus.publish("log_info", "Waiting for user input.")
        return ""