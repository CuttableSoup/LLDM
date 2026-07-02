import os
import random
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
        self.scenario = {}
        # No party/character selection exists yet, so the first loaded
        # player-like entity stands in as the active player character.
        self.player_name = "gladstone"
        self.load_rules(os.path.join("Rules", "Fantasy"))
        self.event_bus.publish("log_info", "DMCore initialized.")
        self.event_bus.publish("rules_loaded", {"skills": self.skills, "entities": self.entities})
        self.event_bus.subscribe("action_detected", self._on_action_detected)

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
                    if "scenario" in data:
                        self.scenario = data["scenario"]
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

    def roll_dice(self, dice, pips):
        """!
        @brief Rolls the D6 dice pool and adds flat pips, per the D6 system.
        @param dice The number of six-sided dice to roll.
        @param pips The flat bonus added to the dice total.
        @return The total of the roll.
        """
        return sum(random.randint(1, 6) for _ in range(max(dice, 0))) + pips

    def resolve_action(self, entity_name, skill_name, difficulty=0):
        """!
        @brief Resolves the outcome of an entity using a skill against a difficulty.
        @param entity_name The name of the entity performing the action.
        @param skill_name The skill being used.
        @param difficulty The target number the roll must meet or beat. Defaults to 0 (auto-success) when not supplied.
        @return A dict describing the roll and whether it succeeded.
        """
        entity = self.entities.get(entity_name, {})
        skill_stats = entity.get("skills", {}).get(skill_name, {"dice": 1, "pips": 0})
        roll = self.roll_dice(skill_stats.get("dice", 1), skill_stats.get("pips", 0))
        success = roll >= difficulty
        self.event_bus.publish(
            "log_info",
            f"Resolved action: {entity_name} used {skill_name}, rolled {roll} vs difficulty {difficulty} -> {'success' if success else 'failure'}."
        )
        return {
            "entity": entity_name,
            "skill": skill_name,
            "roll": roll,
            "difficulty": difficulty,
            "success": success,
        }

    def get_opposing_skill(self, skill_name, defender_name):
        """!
        @brief Finds the defender's best (highest-rated) skill among a skill's opposing skills.
        @param skill_name The attacker's skill.
        @param defender_name The name of the defending entity.
        @return The defender's highest-rated matching opposing skill name, or None if it has none of them.
        """
        opposes = self.skills.get(skill_name, {}).get("opposes", [])
        defender_skills = self.entities.get(defender_name, {}).get("skills", {})
        best_skill = None
        best_rating = None
        for opposing_skill in opposes:
            stats = defender_skills.get(opposing_skill)
            if stats is None:
                continue
            # Pips convert to a die every 3 (see notes.txt), so rate skills on that common scale.
            rating = stats.get("dice", 0) * 3 + stats.get("pips", 0)
            if best_rating is None or rating > best_rating:
                best_rating = rating
                best_skill = opposing_skill
        return best_skill

    def resolve_opposed_action(self, attacker_name, skill_name, defender_name):
        """!
        @brief Resolves a skill roll opposed by a defending entity's matching skill.
        @param attacker_name The name of the acting entity.
        @param skill_name The skill being used by the attacker.
        @param defender_name The name of the opposing entity.
        @return A dict describing the roll, the opposing skill used (if any), and the outcome.
        """
        opposing_skill = self.get_opposing_skill(skill_name, defender_name)
        if opposing_skill:
            defender_stats = self.entities[defender_name]["skills"][opposing_skill]
            difficulty = self.roll_dice(defender_stats.get("dice", 1), defender_stats.get("pips", 0))
        else:
            difficulty = 0

        result = self.resolve_action(attacker_name, skill_name, difficulty)
        result["defender"] = defender_name
        result["opposing_skill"] = opposing_skill
        return result

    def _on_action_detected(self, data):
        """!
        @brief Event handler that resolves a detected player action, opposed by a scenario target if one exists.
        @param data The action_detected payload from NLPCore ({skill, score, input}).
        """
        skill_name = data.get("skill")
        if not skill_name:
            return

        target_name = self._get_target_name()
        if target_name:
            result = self.resolve_opposed_action(self.player_name, skill_name, target_name)
        else:
            result = self.resolve_action(self.player_name, skill_name)

        result["input"] = data.get("input")
        self.event_bus.publish("action_resolved", result)

    def _get_target_name(self):
        """!
        @brief Picks the current opposed target from the loaded scenario.
        @return The name of the first non-player entity in the scenario, or None if there isn't one.
        """
        for entity in self.scenario.get("entities", []):
            name = entity.get("name")
            if name and name != self.player_name:
                return name
        return None