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
        self.rules = {}
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
                    for key, value in data.items():
                        if key not in ("skill", "entity", "scenario"):
                            self.rules[key] = value
                except Exception as e:
                    self.event_bus.publish("log_error", f"Error loading {filename}: {e}")

    def resolve_bonus(self, attacker_name, bonus):
        """!
        @brief Resolves a damage_value's bonus field, which may be a flat number or a
               "user.<rule>" reference (ex: "user.strength_damage") into a rules.toml formula.
        @param attacker_name The name of the entity dealing damage.
        @param bonus The bonus field from a damage_value table.
        @return The resolved flat bonus amount.
        """
        if isinstance(bonus, (int, float)):
            return bonus
        if not isinstance(bonus, str):
            return 0

        rule_name = bonus.split(".")[-1]
        formula = self.rules.get(rule_name)
        if not formula:
            self.event_bus.publish("log_warning", f"Unknown damage bonus reference: {bonus}")
            return 0

        skill_stats = self.entities.get(attacker_name, {}).get("skills", {}).get(formula.get("skill"), {"dice": 0})
        return skill_stats.get("dice", 0) // formula.get("divisor", 1)

    def resolve_damage_value(self, attacker_name, damage_value):
        """!
        @brief Rolls a damage_value's dice/pips and adds its resolved bonus.
        @param attacker_name The name of the entity dealing damage.
        @param damage_value A {dice, pips, bonus} table from an ability, weapon, or spell.
        @return The total rolled damage before any reduction.
        """
        dice = damage_value.get("dice", 0)
        pips = damage_value.get("pips", 0)
        if not isinstance(dice, (int, float)) or not isinstance(pips, (int, float)):
            self.event_bus.publish("log_warning", f"Unsupported damage dice/pips reference: {damage_value}")
            dice, pips = 0, 0

        bonus = self.resolve_bonus(attacker_name, damage_value.get("bonus", 0))
        return self.roll_dice(int(dice), int(pips)) + bonus

    def get_damage_reduction(self, defender_name, damage_tags):
        """!
        @brief Sums the rolled armor value of the defender's equipped items that resist any of the given damage tags.
        @param defender_name The name of the entity taking damage.
        @param damage_tags The damage tags of the incoming attack (ex: ["slashing"]).
        @return The total damage reduction.
        """
        equipped = self.entities.get(defender_name, {}).get("equipped", {})
        reduction = 0
        for item_name in equipped.values():
            item = self.entities.get(item_name, {})
            armor_value = item.get("armor_value")
            armor_tags = item.get("armor_tags", [])
            if armor_value and any(tag in armor_tags for tag in damage_tags):
                reduction += self.roll_dice(armor_value.get("dice", 0), armor_value.get("pips", 0))
        return reduction

    def get_current_hp(self, entity_name):
        """!
        @brief Gets an entity's current HP, initializing it from max_hp the first time it's needed.
        @param entity_name The name of the entity.
        @return The entity's current HP.
        """
        entity = self.entities.get(entity_name, {})
        if "hp" not in entity:
            entity["hp"] = entity.get("max_hp", 0)
        return entity["hp"]

    def apply_damage(self, entity_name, amount):
        """!
        @brief Subtracts damage from an entity's current HP, floored at 0.
        @param entity_name The name of the entity taking damage.
        @param amount The amount of damage to apply.
        @return The entity's remaining HP.
        """
        entity = self.entities.get(entity_name)
        if entity is None:
            return 0
        current_hp = self.get_current_hp(entity_name)
        entity["hp"] = max(0, current_hp - amount)
        self.event_bus.publish("log_info", f"{entity_name} takes {amount} damage ({current_hp} -> {entity['hp']} HP).")
        return entity["hp"]

    def calculate_damage(self, attacker_name, defender_name, ability):
        """!
        @brief Calculates and applies damage from an attacker's ability to a defender, including resistances.
        @param attacker_name The name of the entity dealing damage.
        @param defender_name The name of the entity taking damage.
        @param ability A table with damage_value {dice, pips, bonus} and damage_tags, such as a weapon, spell, or innate ability.
        @return A dict describing the raw damage, reduction, net damage, and the defender's remaining HP.
        """
        damage_value = ability.get("damage_value", {"dice": 0, "pips": 0, "bonus": 0})
        damage_tags = ability.get("damage_tags", [])

        raw_damage = self.resolve_damage_value(attacker_name, damage_value)
        reduction = self.get_damage_reduction(defender_name, damage_tags)
        net_damage = max(0, raw_damage - reduction)
        remaining_hp = self.apply_damage(defender_name, net_damage)

        self.event_bus.publish(
            "log_info",
            f"{attacker_name} deals {raw_damage} raw damage to {defender_name}, reduced by {reduction} -> {net_damage} net damage."
        )
        return {
            "attacker": attacker_name,
            "defender": defender_name,
            "raw_damage": raw_damage,
            "reduction": reduction,
            "net_damage": net_damage,
            "remaining_hp": remaining_hp,
        }

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

    def find_attack_ability(self, entity_name, skill_name):
        """!
        @brief Finds the entity's equipped weapon or innate ability that uses the given skill and deals damage.
        @param entity_name The name of the acting entity.
        @param skill_name The skill being used.
        @return The matching weapon/ability table (with damage_value and damage_tags), or None.
        """
        entity = self.entities.get(entity_name, {})

        for item_name in entity.get("equipped", {}).values():
            item = self.entities.get(item_name)
            if item and item.get("skill") == skill_name and "damage_value" in item:
                return item

        for ability_variants in entity.get("abilities", {}).values():
            for ability in ability_variants:
                if ability.get("skill") == skill_name and "damage_value" in ability:
                    return ability

        return None

    def _on_action_detected(self, data):
        """!
        @brief Event handler that resolves a detected player action, opposed by a scenario target if one exists,
               and applies damage if the action hit with an attack ability.
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

        if result["success"] and target_name:
            ability = self.find_attack_ability(self.player_name, skill_name)
            if ability:
                result["damage"] = self.calculate_damage(self.player_name, target_name, ability)

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