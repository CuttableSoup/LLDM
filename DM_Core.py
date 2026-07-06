import copy
import os
import random
import tomllib

COMPARATORS = {
    ">": lambda actual, value: actual > value,
    "<": lambda actual, value: actual < value,
    ">=": lambda actual, value: actual >= value,
    "<=": lambda actual, value: actual <= value,
    "==": lambda actual, value: actual == value,
    "!=": lambda actual, value: actual != value,
    "in": lambda actual, value: actual in value,
    "not_in": lambda actual, value: actual not in value,
}


def scenario_file_path(scenario_name):
    """!
    @brief Resolves a scenario name to its file path under Rules/Fantasy/scenarios/.
    @param scenario_name The scenario's filename without extension (ex: "arena", "tavern").
    @return The absolute filepath, whether or not it actually exists.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "Rules", "Fantasy", "scenarios", f"{scenario_name}.toml")

class DMCore:
    """!
    @brief Main class handling the core mechanics of the RPG system.
    """

    def __init__(self, event_bus, scenario_name="arena"):
        """!
        @brief Initializes the DM core and loads system references.
        @param event_bus The central event bus instance.
        @param scenario_name Which scenario to load, matching a file in
               Rules/Fantasy/scenarios/ (ex: "arena" loads scenarios/arena.toml).
        """
        self.event_bus = event_bus
        self.skills = {}
        self.entities = {}
        self.scenario = {}
        self.scenario_entities = []
        self.rules = {}
        # No party/character selection exists yet, so the first loaded
        # player-like entity stands in as the active player character.
        self.player_name = "gladstone"
        self.round_number = 0
        self.load_rules(os.path.join("Rules", "Fantasy"))
        self.load_scenario_definition(scenario_name)
        self.load_scenario()
        self.event_bus.publish("log_info", "DMCore initialized.")
        self.event_bus.publish("rules_loaded", {"skills": self.skills, "entities": self.entities})
        characters = [
            description for description in (
                self.describe_character(entity_name) for entity_name in self.scenario_entities
            ) if description
        ]
        self.event_bus.publish("scenario_loaded", {
            "name": self.scenario.get("name"),
            "description": self.scenario.get("description"),
            "characters": characters,
        })
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
                    for key, value in data.items():
                        if key not in ("skill", "entity"):
                            self.rules[key] = value
                except Exception as e:
                    self.event_bus.publish("log_error", f"Error loading {filename}: {e}")

    def load_scenario_definition(self, scenario_name):
        """!
        @brief Reads a named scenario file from Rules/Fantasy/scenarios/ into self.scenario.
               Scenarios live in their own subdirectory rather than the flat Rules/Fantasy/
               scan in load_rules (which only keeps whichever [scenario] table it reads last),
               so multiple named scenarios can coexist and one is selected explicitly by name.
        @param scenario_name The scenario's filename without extension (ex: "arena", "tavern").
        @raises FileNotFoundError if no matching scenario file exists. Unlike load_rules'
                blanket per-file try/except, a missing/malformed scenario is fatal on purpose:
                silently continuing with an empty self.scenario used to let LLMCore narrate an
                opening scene with no name/description, which the LLM would happily hallucinate
                (ex: a "featureless gray void") with no indication anything had gone wrong.
        """
        filepath = scenario_file_path(scenario_name)

        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Scenario '{scenario_name}' not found (expected {filepath}).")

        with open(filepath, "rb") as f:
            data = tomllib.load(f)
        self.scenario = data.get("scenario", {})

    def load_scenario(self):
        """!
        @brief Instantiates each entity listed in the scenario as its own independent copy of its
               template, so duplicate creatures (ex: two wolves) get separate HP/conditions instead
               of sharing the same template dict.
        """
        self.scenario_entities = []
        occurrence_counts = {}

        for entry in self.scenario.get("entities", []):
            template_name = entry.get("name")
            template = self.entities.get(template_name)
            if template is None:
                self.event_bus.publish("log_error", f"Scenario references unknown entity: {template_name}")
                continue

            occurrence_counts[template_name] = occurrence_counts.get(template_name, 0) + 1
            occurrence = occurrence_counts[template_name]
            instance_name = template_name if occurrence == 1 else f"{template_name}_{occurrence}"

            instance = copy.deepcopy(template)
            instance["entity_id"] = instance_name
            instance["band"] = entry.get("band")
            # "conditions" is the template's starting state (ex: a chest's [entity.conditions.locked]);
            # "active_conditions" is the per-instance runtime dict apply_condition/dismiss_condition
            # mutate, so it must start as its own copy rather than sharing the template's dict.
            instance["active_conditions"] = dict(instance.get("conditions", {}))
            self.entities[instance_name] = instance
            self.scenario_entities.append(instance_name)

        self.event_bus.publish("log_info", f"Scenario loaded: {self.scenario_entities}")

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
        @brief Subtracts damage from an entity's current HP, floored at 0, and evaluates on_damage statuses.
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
        self.evaluate_statuses(entity_name, "on_damage")
        return entity["hp"]

    def get_comparable_value(self, entity_name, field):
        """!
        @brief Resolves a requirement's field name to a comparable value for an entity.
        @param entity_name The name of the entity to check.
        @param field The field name, either a derived value (ex: "hp_per_remain") or an entity attribute (ex: "supertype").
        @return The resolved value, or None if it can't be determined.
        """
        if field == "hp_per_remain":
            entity = self.entities.get(entity_name, {})
            max_hp = entity.get("max_hp", 0)
            if max_hp <= 0:
                return None
            return self.get_current_hp(entity_name) / max_hp
        return self.entities.get(entity_name, {}).get(field)

    def entity_matches_requirements(self, entity_name, requirements):
        """!
        @brief Checks whether an entity currently satisfies every comparison in a status's requirements.
        @param entity_name The name of the entity to check.
        @param requirements A list of {field, operator, value} comparisons, all of which must hold.
        @return True if every comparison is satisfied.
        """
        for comparison in requirements:
            compare = COMPARATORS.get(comparison.get("operator"))
            if compare is None:
                self.event_bus.publish("log_warning", f"Unknown requirement operator: {comparison.get('operator')}")
                return False

            actual_value = self.get_comparable_value(entity_name, comparison.get("field"))
            if actual_value is None or not compare(actual_value, comparison.get("value")):
                return False

        return True

    def get_applicable_statuses(self, entity_name, trigger):
        """!
        @brief Finds every status definition for a given trigger whose requirements the entity currently meets.
        @param entity_name The name of the entity to check.
        @param trigger The trigger name to filter statuses by (ex: "on_damage").
        @return A list of matching status definitions.
        """
        return [
            status for status in self.rules.get("status", [])
            if status.get("trigger") == trigger
            and self.entity_matches_requirements(entity_name, status.get("requirements", []))
        ]

    def apply_condition(self, entity_name, condition_name, duration=None, dismiss=None):
        """!
        @brief Marks a condition as active on an entity.
        @param entity_name The name of the entity gaining the condition.
        @param condition_name The name of the condition, as defined in the [[condition]] table.
        @param duration How long the condition lasts (ex: "fleeting", "scene", "permanent").
        @param dismiss What removes the condition (ex: "healing", "resurrection").
        """
        entity = self.entities.get(entity_name)
        if entity is None:
            return
        active_conditions = entity.setdefault("active_conditions", {})
        active_conditions[condition_name] = {"duration": duration, "dismiss": dismiss}
        self.event_bus.publish("log_info", f"{entity_name} gains condition '{condition_name}'.")

    def dismiss_condition(self, entity_name, condition_name):
        """!
        @brief Removes a condition from an entity, if it's currently active. This is the
               general-purpose counterpart to apply_condition -- ex: a chest's "locked"
               condition, seeded from its template's [entity.conditions], gets dismissed via
               apply_test_outcome's "dismiss_condition" key once its [entity.test] is passed.
        @param entity_name The name of the entity losing the condition.
        @param condition_name The name of the condition to remove.
        @return True if the condition was present and removed, False otherwise.
        """
        active_conditions = self.entities.get(entity_name, {}).get("active_conditions", {})
        if condition_name not in active_conditions:
            return False
        del active_conditions[condition_name]
        self.event_bus.publish("log_info", f"{entity_name} loses condition '{condition_name}'.")
        return True

    def is_locked(self, entity_name):
        """!
        @brief Whether an entity (ex: a chest) currently has the "locked" condition active.
        @param entity_name The name of the entity to check.
        @return True if "locked" is in the entity's active_conditions.
        """
        return "locked" in self.entities.get(entity_name, {}).get("active_conditions", {})

    def transfer_currency(self, from_name, to_name, amount=None):
        """!
        @brief Moves currency from one entity to another (ex: looting a chest's gold).
        @param from_name The name of the entity currency is taken from.
        @param to_name The name of the entity currency is given to.
        @param amount How much to move; if None, moves all of from_name's currency.
        @return The amount actually transferred (0 if either entity is missing or there's none to move).
        """
        source = self.entities.get(from_name)
        destination = self.entities.get(to_name)
        if source is None or destination is None:
            return 0

        available = source.get("currency", 0)
        moved = available if amount is None else min(amount, available)
        if moved <= 0:
            return 0

        source["currency"] = available - moved
        destination["currency"] = destination.get("currency", 0) + moved
        self.event_bus.publish("log_info", f"{moved} currency moved from {from_name} to {to_name}.")
        return moved

    def transfer_item(self, from_name, to_name, item_name):
        """!
        @brief Moves one occurrence of an item from one entity's inventory list to another's.
               Duplicates (ex: three "health potion" entries) represent quantity, so only one
               matching entry is removed per call.
        @param from_name The name of the entity the item is taken from.
        @param to_name The name of the entity the item is given to.
        @param item_name The name of the item to move.
        @return True if the item was present in from_name's inventory and moved, False otherwise.
        """
        source = self.entities.get(from_name)
        destination = self.entities.get(to_name)
        if source is None or destination is None:
            return False

        source_inventory = source.get("inventory", [])
        if item_name not in source_inventory:
            return False

        source_inventory.remove(item_name)
        destination.setdefault("inventory", []).append(item_name)
        self.event_bus.publish("log_info", f"{item_name} moved from {from_name} to {to_name}.")
        return True

    def loot_entity(self, from_name, to_name):
        """!
        @brief Moves everything -- all currency and every inventory item -- from one entity to
               another. Ex: taking a chest's contents once it's open (see apply_test_outcome's
               "loot" key).
        @param from_name The name of the entity being looted (ex: a chest).
        @param to_name The name of the entity receiving the loot (ex: the player).
        @return A {currency, items} summary of what actually moved, so callers (ex:
                _on_action_detected, for narration) know what was gained without the LLM having
                to invent it.
        """
        currency_moved = self.transfer_currency(from_name, to_name)
        items_moved = []
        for item_name in list(self.entities.get(from_name, {}).get("inventory", [])):
            if self.transfer_item(from_name, to_name, item_name):
                items_moved.append(item_name)
        return {"currency": currency_moved, "items": items_moved}

    def apply_test_outcome(self, entity_name, outcome):
        """!
        @brief Applies the pass/fail consequence of an entity's [entity.test] (ex: a chest's
               lock check), dispatching purely on which keys are present in outcome -- no
               "action" enum needed. A key of "dismiss_condition" removes that condition; a key
               of "condition" applies a new one (the same {condition, duration, dismiss} shape
               [[status]]'s own apply/test.fail blocks already use); a truthy "loot" key hands
               everything (currency + inventory) to the player via loot_entity. Any combination
               can be present at once, or the whole outcome can be empty/omitted for no consequence.
        @param entity_name The name of the entity the test was performed against.
        @param outcome The test's "pass" or "fail" table (or None/"" for no consequence).
        @return loot_entity's {currency, items} summary if "loot" applied, else None -- so the
                caller can narrate what was actually gained instead of leaving the LLM to guess.
        """
        if not outcome:
            return None
        dismiss_name = outcome.get("dismiss_condition")
        if dismiss_name:
            self.dismiss_condition(entity_name, dismiss_name)
        condition_name = outcome.get("condition")
        if condition_name:
            self.apply_condition(
                entity_name, condition_name,
                duration=outcome.get("duration"), dismiss=outcome.get("dismiss"),
            )
        if outcome.get("loot"):
            return self.loot_entity(entity_name, self.player_name)
        return None

    def evaluate_statuses(self, entity_name, trigger):
        """!
        @brief Applies every status matching the given trigger that the entity currently qualifies for.
        @param entity_name The name of the entity to evaluate.
        @param trigger The trigger name to evaluate (ex: "on_damage").
        @return The list of status definitions that were applied.
        """
        matched_statuses = self.get_applicable_statuses(entity_name, trigger)
        for status in matched_statuses:
            apply_block = status.get("apply")
            if apply_block and apply_block.get("condition"):
                self.apply_condition(
                    entity_name,
                    apply_block["condition"],
                    duration=apply_block.get("duration"),
                    dismiss=apply_block.get("dismiss"),
                )
        return matched_statuses

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
               and applies damage if the action hit with an attack ability. Combat (a target present that is
               hostile toward the player) narrates once per round via "round_resolved"; everything else
               (no target, or a non-hostile target like a tavern NPC) narrates immediately via "action_resolved".
        @param data The action_detected payload from NLPCore ({skill, score, input}).
        """
        skill_name = data.get("skill")
        if not skill_name:
            return

        target_name = self._get_target_name()
        test = self.entities.get(target_name, {}).get("test") if target_name else None
        if test and skill_name in test.get("skill", []):
            # An entity's own [entity.test] (ex: a chest's lock) is a flat difficulty check,
            # not an opposed roll -- it doesn't compete via the attacker's skill's `opposes`
            # list the way a creature's defense does. Any *other* skill against this same
            # target (ex: forcing the chest with "strength") still falls through to the normal
            # opposed-skill path below, e.g. resolved against its "fortitude" if it has one.
            result = self.resolve_action(self.player_name, skill_name, test.get("difficulty", 0))
            result["defender"] = target_name
            result["opposing_skill"] = None
            outcome = test.get("pass") if result["success"] else test.get("fail")
            loot = self.apply_test_outcome(target_name, outcome)
            if loot and (loot["currency"] or loot["items"]):
                result["loot"] = loot
        elif target_name:
            result = self.resolve_opposed_action(self.player_name, skill_name, target_name)
        else:
            result = self.resolve_action(self.player_name, skill_name)

        if result["success"] and target_name:
            ability = self.find_attack_ability(self.player_name, skill_name)
            if ability:
                result["damage"] = self.calculate_damage(self.player_name, target_name, ability)

        if target_name:
            defender_details = self.describe_character(target_name)
            if defender_details:
                result["defender_details"] = defender_details

        result["input"] = data.get("input")

        in_combat = target_name is not None and self.is_hostile(target_name, self.player_name)
        if in_combat:
            self.round_number += 1
            result["round"] = self.round_number
            self.event_bus.publish("round_resolved", result)
        else:
            self.event_bus.publish("action_resolved", result)

    def get_attitude(self, entity_name, toward_name):
        """!
        @brief Resolves entity_name's six-value attitude array toward toward_name: a specific
               name override, then a supertype override, then the entity's default.
        @param entity_name The name of the entity whose attitude is being read.
        @param toward_name The name of the entity being regarded.
        @return The [disposition, trust, confidence, respect, obligation, intimacy] attitude array.
        """
        attitudes = self.entities.get(entity_name, {}).get("attitudes", {})

        for override in attitudes.get("name", []):
            if toward_name in override:
                return override[toward_name]

        toward_supertype = self.entities.get(toward_name, {}).get("supertype")
        for override in attitudes.get("supertype", []):
            if toward_supertype in override:
                return override[toward_supertype]

        return attitudes.get("default", [0, 0, 0, 0, 0, 0])

    def is_hostile(self, entity_name, toward_name):
        """!
        @brief Whether entity_name is hostile enough toward toward_name to be treated as a combat
               target rather than a dialogue partner. An entity with no attitude data defaults to
               neutral (0), which still counts as combat-ready; only a positive (Friendly-leaning)
               disposition opts an entity out of combat routing. Inanimate objects (ex: a locked
               chest) are never hostile regardless of attitude data -- they have no combat intent,
               so a lockpicking attempt against one must not get batched into "round_resolved".
        @param entity_name The name of the entity being checked.
        @param toward_name The name of the entity it might be hostile toward.
        @return True if disposition (the attitude array's first value) is 0 or negative.
        """
        if self.entities.get(entity_name, {}).get("supertype") == "object":
            return False
        disposition = self.get_attitude(entity_name, toward_name)[0]
        return disposition <= 0

    def describe_character(self, entity_name):
        """!
        @brief Builds a flavor-text description of an entity for narration prompts, out of its
               purely descriptive data (description, qualities, memories, quotes) rather than
               mechanical data (skills/dice), since this is meant to tell the LLM who someone is.
        @param entity_name The name of the entity to describe.
        @return A formatted description string, or "" if the entity has no descriptive data.
        """
        entity = self.entities.get(entity_name, {})
        parts = []

        description = entity.get("description")
        if description:
            parts.append(description)

        qualities = entity.get("qualities")
        if qualities:
            parts.append("Qualities: " + ", ".join(f"{key} {value}" for key, value in qualities.items()))

        memories = entity.get("memories")
        if memories:
            parts.append("Memories: " + "; ".join(memories))

        quotes = entity.get("quotes")
        if quotes:
            parts.append("Known to say: " + "; ".join(f"\"{quote}\"" for quote in quotes))

        if not parts:
            return ""
        return f"{entity_name} - " + " | ".join(parts)

    def _get_target_name(self):
        """!
        @brief Picks the current opposed target from the instantiated scenario entities.
        @return The name of the first non-player entity instance in the scenario, or None if there isn't one.
        """
        for instance_name in self.scenario_entities:
            if instance_name != self.player_name:
                return instance_name
        return None