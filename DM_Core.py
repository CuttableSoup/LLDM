import os
import tomllib

class DMCore:
    """!
    @brief Main class handling the core mechanics of the RPG system.
    """

    def __init__(self, event_bus):
        """!
        @brief Initializes the DM core and loads system references.
        @param event_bus The central event bus instance.
        """
        self.event_bus = event_bus
        self.skills = {}
        self.entities = {}
        self.load_rules(os.path.join("Rules", "Fantasy"))
        self.event_bus.publish("log_info", "DMCore initialized.")
        self.event_bus.publish("rules_loaded", {"skills": self.skills, "entities": self.entities})

    def load_rules(self, rules_dir):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        full_dir = os.path.join(base_dir, rules_dir)

        if not os.path.exists(full_dir):
            self.event_bus.publish("log_error", f"Rules directory not found: {full_dir}")
            return

        for filename in os.listdir(full_dir):
            if filename.endswith(".toml"):
                filepath = os.path.join(full_dir, filename)
                try:
                    with open(filepath, "rb") as f:
                        data = tomllib.load(f)
                    if "skill" in data:
                        for skill in data["skill"]:
                            self.skills[skill.get("name")] = skill
                    if "entity" in data:
                        for entity in data["entity"]:
                            self.entities[entity.get("name")] = entity
                except Exception as e:
                    self.event_bus.publish("log_error", f"Error loading {filename}: {e}")

    def calculate_damage(self, attacker_stats, defender_stats):
        """!
        @brief Calculates damage interactions including resistances.
        @param attacker_stats The offensive capabilities and damage values.
        @param defender_stats The defensive capabilities and armor values.
        @return The final calculated damage to be applied.
        """
        self.event_bus.publish("log_info", "Calculating damage.")
        return 0

    def manage_combat(self, participants):
        """!
        @brief Manages combat interactions between entities.
        @param participants A list of entities involved in the combat.
        """
        self.event_bus.publish("log_info", "Managing combat phase.")

    def process_entity_state(self, entity_health, entity_inventory, entity_attitudes):
        """!
        @brief Processes interactions between health, inventory, and attitudes.
        @param entity_health The current health status of the entity.
        @param entity_inventory The items held by the entity.
        @param entity_attitudes The attitude metrics of the entity.
        """
        self.event_bus.publish("log_info", "Processing entity state.")

    def resolve_action(self, skills, abilities):
        """!
        @brief Resolves the outcome of using skills.
        @param skills The skills applied to the action.
        @param abilities used during the action.
        @return The result of the action.
        """
        self.event_bus.publish("log_info", "Resolving action.")
        return True