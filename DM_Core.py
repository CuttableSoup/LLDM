import copy
import json
import os
import random
import tomllib

from DM_Inventory import InventoryMixin
from DM_Social import SocialMixin

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

class DMCore(InventoryMixin, SocialMixin):
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
        self.round_number = 0
        # The filename passed to load_scenario_definition -- distinct from self.scenario's
        # own "name" field (a display string, ex: "The Arena") -- kept so save_game/load_game
        # know which scenarios/*.toml file a saved slot belongs to.
        self.scenario_key = scenario_name
        self.load_rules(os.path.join("Rules", "Fantasy"))
        # No party/character selection exists yet, so the one entity template marked
        # is_player = true (characters.toml's gladstone) stands in as the active player
        # character. Resolved once here, from templates, rather than per-scenario-load, so
        # ad-hoc test scenarios that omit gladstone entirely still keep the same player_name
        # they booted with.
        self.player_name = self._resolve_player_name()
        self.load_scenario_definition(scenario_name)
        self.load_scenario()
        self.event_bus.publish("log_info", "DMCore initialized.")
        self.event_bus.publish("rules_loaded", {"skills": self.skills, "entities": self.entities})
        self.event_bus.publish("scenario_loaded", {
            "name": self.scenario.get("name"),
            "description": self.scenario.get("description"),
            "characters": self._describe_scenario_characters(),
        })
        self.event_bus.subscribe("action_detected", self._on_action_detected)
        self.event_bus.subscribe("item_interaction_detected", self._on_item_interaction_detected)
        self.event_bus.subscribe("save_requested", self._on_save_requested)
        self.event_bus.subscribe("load_requested", self._on_load_requested)

    def _describe_scenario_characters(self):
        """!
        @brief Builds the "characters" roster (describe_character per scenario instance,
               skipping entities with no descriptive data) shared by scenario_loaded's
               initial payload and game_loaded's post-load payload.
        @return A list of non-empty character description strings.
        """
        return [
            description for description in (
                self.describe_character(entity_name, toward_name=self.player_name)
                for entity_name in self.scenario_entities
            ) if description
        ]

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

    def _resolve_player_name(self):
        """!
        @brief Finds the one entity template marked `is_player = true` (ex: characters.toml's
               gladstone) and returns its name, to stand in as the active player character.
        @raises ValueError if no loaded entity template has `is_player = true` -- fatal on
                purpose, same reasoning as load_scenario_definition's missing-scenario-file
                check: silently falling back to some default here would let the rest of
                DMCore run against a player_name that matches no real entity, failing later
                in confusing, indirect ways instead of failing clearly at boot.
        @return The name of the player entity template.
        """
        for name, entity in self.entities.items():
            if entity.get("is_player"):
                return name
        raise ValueError("No entity template has is_player = true; cannot determine the player character.")

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
        dice = self.resolve_weapon_reference(attacker_name, damage_value.get("dice", 0), "dice")
        pips = self.resolve_weapon_reference(attacker_name, damage_value.get("pips", 0), "pips")
        if not isinstance(dice, (int, float)) or not isinstance(pips, (int, float)):
            self.event_bus.publish("log_warning", f"Unsupported damage dice/pips reference: {damage_value}")
            dice, pips = 0, 0

        bonus = self.resolve_bonus(attacker_name, damage_value.get("bonus", 0))
        return self.roll_dice(int(dice), int(pips)) + bonus

    def get_equipped_weapon(self, entity_name):
        """!
        @brief Finds the first of an entity's equipped items that deals damage.
        @param entity_name The name of the entity to check.
        @return The equipped weapon's entity table, or None if nothing equipped has a damage_value.
        """
        entity = self.entities.get(entity_name, {})
        for item_name in entity.get("equipped", {}).values():
            item = self.entities.get(item_name)
            if item and "damage_value" in item:
                return item
        return None

    def resolve_weapon_reference(self, attacker_name, value, field):
        """!
        @brief Resolves a damage_value's dice/pips field when it's the "user.weapon.<field>"
               indirection (ex: techniques.toml's cleave, whose damage scales with whatever
               weapon the attacker currently has equipped, rather than a fixed amount).
        @param attacker_name The name of the entity dealing damage.
        @param value The dice or pips field from a damage_value table.
        @param field Which field this is ("dice" or "pips"), matched against "user.weapon.<field>".
        @return value unchanged if it isn't that reference; otherwise the attacker's equipped
                weapon's matching field, or 0 if the attacker has no equipped weapon.
        """
        if value != f"user.weapon.{field}":
            return value
        weapon = self.get_equipped_weapon(attacker_name)
        if weapon is None:
            return 0
        return weapon.get("damage_value", {}).get(field, 0)

    def get_damage_reduction(self, defender_name, damage_tags):
        """!
        @brief Sums the rolled reduction against the given damage tags: the defender's own
               innate resistance_value/resistance_tags (ex: a fire elemental's inherent
               resistance to physical damage) plus the rolled armor value of any equipped
               items that resist the same tags. Both are static, tag-matched traits of the
               entity/item -- distinct from active_conditions, which represent temporary state
               gained/lost during play (see CLAUDE.md's tags-vs-conditions note).
        @param defender_name The name of the entity taking damage.
        @param damage_tags The damage tags of the incoming attack (ex: ["fire"]).
        @return The total damage reduction.
        """
        defender = self.entities.get(defender_name, {})
        reduction = 0

        resistance_value = defender.get("resistance_value")
        resistance_tags = defender.get("resistance_tags", [])
        if resistance_value and any(tag in resistance_tags for tag in damage_tags):
            reduction += self.roll_dice(resistance_value.get("dice", 0), resistance_value.get("pips", 0))

        for item_name in defender.get("equipped", {}).values():
            item = self.entities.get(item_name, {})
            armor_value = item.get("armor_value")
            armor_tags = item.get("armor_tags", [])
            if armor_value and any(tag in armor_tags for tag in damage_tags):
                reduction += self.roll_dice(armor_value.get("dice", 0), armor_value.get("pips", 0))

        return reduction

    def get_vulnerability_bonus(self, defender_name, damage_tags):
        """!
        @brief Rolls the extra damage a defender's own vulnerability_value/vulnerability_tags
               (ex: the fire elemental's vulnerability to "water") adds on a matching hit --
               the mirror image of resistance_value/resistance_tags in get_damage_reduction,
               just added to raw damage instead of subtracted. Innate to the entity only (no
               equipped-item counterpart, unlike armor's resistance side); a static, tag-matched
               trait rather than active_conditions' temporary state (see CLAUDE.md's
               tags-vs-conditions note).
        @param defender_name The name of the entity taking damage.
        @param damage_tags The damage tags of the incoming attack (ex: ["water"]).
        @return The rolled bonus damage, or 0 if no tag matches.
        """
        defender = self.entities.get(defender_name, {})
        vulnerability_value = defender.get("vulnerability_value")
        vulnerability_tags = defender.get("vulnerability_tags", [])
        if vulnerability_value and any(tag in vulnerability_tags for tag in damage_tags):
            return self.roll_dice(vulnerability_value.get("dice", 0), vulnerability_value.get("pips", 0))
        return 0

    def is_immune_to(self, defender_name, damage_tags):
        """!
        @brief Whether an entity's immunity_tags fully negate an incoming attack's damage tags
               (ex: a fire elemental's immunity to "fire"). Distinct from resistance/armor, which
               reduce damage by a rolled amount -- immunity is an absolute, tag-matched block,
               mirroring notes.txt's "poison damage tagged so undead are immune" example.
        @param defender_name The name of the entity taking damage.
        @param damage_tags The damage tags of the incoming attack (ex: ["fire"]).
        @return True if any damage tag matches the defender's immunity_tags.
        """
        immunity_tags = self.entities.get(defender_name, {}).get("immunity_tags", [])
        return any(tag in immunity_tags for tag in damage_tags)

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

    def is_closed(self, entity_name):
        """!
        @brief Whether a container (ex: a chest) currently has the "closed" condition active.
               Mirrors is_locked exactly. Absent from active_conditions means not closed (open)
               by default, so any container with no [entity.conditions.closed] seeded in TOML
               is unaffected -- only items.toml's chest opts into this today.
        @param entity_name The name of the entity to check.
        @return True if "closed" is in the entity's active_conditions.
        """
        return "closed" in self.entities.get(entity_name, {}).get("active_conditions", {})

    def is_test_available(self, entity_name, test, skill_name):
        """!
        @brief Whether an entity's [entity.test] can currently be attempted with the given
               skill. Gates the test on the entity's *current* active_conditions, not just
               whether the skill matches -- without this, ex: an already-picked chest's
               [entity.test] would keep re-triggering on repeat attempts (harmless only by
               accident, since there'd be nothing left to loot), and a "jammed" condition
               applied on a failed attempt would have no actual effect on future ones.
        @param entity_name The name of the entity being tested (ex: a chest).
        @param test The entity's test table ({difficulty, skill, requires_condition,
               blocks_if_condition, pass, fail}).
        @param skill_name The skill the player is attempting to use.
        @return True if skill_name matches test["skill"], test["requires_condition"] (if set)
                is currently active, and test["blocks_if_condition"] (if set) is not.
        """
        if skill_name not in test.get("skill", []):
            return False
        active_conditions = self.entities.get(entity_name, {}).get("active_conditions", {})
        requires = test.get("requires_condition")
        if requires and requires not in active_conditions:
            return False
        blocks = test.get("blocks_if_condition")
        if blocks and blocks in active_conditions:
            return False
        return True

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
        @brief Calculates and applies damage from an attacker's ability to a defender, including
               immunity, resistance/armor reduction, and vulnerability.
        @param attacker_name The name of the entity dealing damage.
        @param defender_name The name of the entity taking damage.
        @param ability A table with damage_value {dice, pips, bonus} and damage_tags, such as a weapon, spell, or innate ability.
        @return A dict describing the raw damage, reduction, vulnerability bonus, net damage, and the defender's remaining HP.
        """
        damage_value = ability.get("damage_value", {"dice": 0, "pips": 0, "bonus": 0})
        damage_tags = ability.get("damage_tags", [])

        raw_damage = self.resolve_damage_value(attacker_name, damage_value)
        if self.is_immune_to(defender_name, damage_tags):
            # An absolute block -- a matching immunity negates the hit entirely, so
            # vulnerability (which only matters once damage is actually getting through)
            # never applies alongside it.
            reduction = raw_damage
            vulnerability_bonus = 0
        else:
            reduction = self.get_damage_reduction(defender_name, damage_tags)
            vulnerability_bonus = self.get_vulnerability_bonus(defender_name, damage_tags)
        net_damage = max(0, raw_damage + vulnerability_bonus - reduction)
        remaining_hp = self.apply_damage(defender_name, net_damage)

        self.event_bus.publish(
            "log_info",
            f"{attacker_name} deals {raw_damage} raw damage to {defender_name}"
            f"{f' (+{vulnerability_bonus} vulnerability)' if vulnerability_bonus else ''}"
            f", reduced by {reduction} -> {net_damage} net damage."
        )
        return {
            "attacker": attacker_name,
            "defender": defender_name,
            "raw_damage": raw_damage,
            "reduction": reduction,
            "vulnerability_bonus": vulnerability_bonus,
            "net_damage": net_damage,
            "remaining_hp": remaining_hp,
        }

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
               An equipped weapon matching skill_name always wins over an ability/technique that
               also matches it (ex: gladstone's plain longsword swing over "cleave", both usable
               via "blades") -- there's no player-facing way to choose a technique over a basic
               attack on the same skill yet; see CLAUDE.md's cleave note.
        @param entity_name The name of the acting entity.
        @param skill_name The skill being used.
        @return The matching weapon/ability table (with damage_value and damage_tags), or None.
        """
        entity = self.entities.get(entity_name, {})

        for item_name in entity.get("equipped", {}).values():
            item = self.entities.get(item_name)
            if item and self.ability_matches_skill(item, skill_name) and "damage_value" in item:
                return item

        for ability in entity.get("abilities", []):
            ability = self.resolve_ability(ability)
            if ability and self.ability_matches_skill(ability, skill_name) and "damage_value" in ability:
                return ability

        return None

    def ability_matches_skill(self, ability, skill_name):
        """!
        @brief Whether an ability/weapon's "skill" field matches the given skill name -- either
               a single skill (ex: a weapon's own skill) or, for a multi-skill technique (ex:
               techniques.toml's cleave, usable via either "blades" or "axes"), a list any one
               of which counts as a match.
        @param ability The ability/weapon/spell/technique table to check.
        @param skill_name The skill being used.
        @return True if skill_name matches, directly or via list membership.
        """
        ability_skill = ability.get("skill")
        if isinstance(ability_skill, list):
            return skill_name in ability_skill
        return ability_skill == skill_name

    def resolve_ability(self, ability):
        """!
        @brief Resolves one entry from an entity's flat abilities list (mirroring how
               "inventory" is a flat list of item names) to its definition table. An entry
               is either a fully inlined table (ex: gladstone's "punch", wolf's "bite" --
               innate abilities unique to that one entity, not shared anywhere else) or a
               plain string naming a shared catalog entity (ex: gladstone's "fireball",
               which points at the standalone spell defined once in spells.toml and looked
               up here the same way equipped items are looked up by name via
               self.entities). Keeps that shared data in one place instead of requiring
               every caster to carry its own copy that can drift out of sync.
        @param ability Either an ability/spell/technique table, or a string name to look up.
        @return The resolved ability table, or None if a string reference doesn't match
                any loaded entity.
        """
        if isinstance(ability, str):
            return self.entities.get(ability)
        return ability

    def resolve_named_ability(self, entity_name, ability_name):
        """!
        @brief Checks whether ability_name literally names one of entity_name's own abilities
               (ex: NLPCore matched "I cleave through them" directly to the technique "cleave"
               rather than the plain skill "blades" it happens to share with an equipped
               weapon). This is what lets a named technique/spell win over
               find_attack_ability's equipped-weapon-first priority -- the exact ability is
               already known here, rather than inferred from a skill name afterward.
        @param entity_name The name of the acting entity.
        @param ability_name The candidate ability name (ex: action_detected's "skill" field).
        @return The resolved ability table if entity_name actually has it, else None.
        """
        entity = self.entities.get(entity_name, {})
        for ability in entity.get("abilities", []):
            resolved = self.resolve_ability(ability)
            if resolved and resolved.get("name") == ability_name:
                return resolved
        return None

    def select_ability_skill(self, entity_name, ability):
        """!
        @brief Picks which single skill to roll an ability with, when its "skill" field lists
               multiple options (ex: cleave's ["blades", "axes"]) -- the entity's highest-rated
               skill among them, using the same rating convention as get_opposing_skill
               (dice*3 + pips). A single-string "skill" is returned unchanged.
        @param entity_name The name of the entity attempting the ability.
        @param ability The ability table.
        @return The resolved skill name to roll, or None if the ability has no skill at all.
        """
        ability_skill = ability.get("skill")
        if not isinstance(ability_skill, list):
            return ability_skill

        entity_skills = self.entities.get(entity_name, {}).get("skills", {})
        best_skill = None
        best_rating = None
        for candidate in ability_skill:
            stats = entity_skills.get(candidate)
            if stats is None:
                continue
            rating = stats.get("dice", 0) * 3 + stats.get("pips", 0)
            if best_rating is None or rating > best_rating:
                best_rating = rating
                best_skill = candidate
        if best_skill is not None:
            return best_skill
        return ability_skill[0] if ability_skill else None

    def choose_behavior(self, entity_name):
        """!
        @brief Picks the first entry in an entity's [[entity.behavior]] list whose
               requirements are currently met, in declaration order -- the same
               {field, operator, value} requirement engine [[status]] already uses
               (entity_matches_requirements), just read from "behavior" instead of
               "status". Ex: creatures.toml's wolf has one behavior, "always attack
               while hp_per_remain >= 0.01", so it keeps attacking until it's
               effectively dead and then simply stops matching any behavior at all.
        @param entity_name The name of the entity choosing a behavior.
        @return The first matching behavior definition, or None if none match (or
                the entity has no behavior list at all).
        """
        for behavior in self.entities.get(entity_name, {}).get("behavior", []):
            if self.entity_matches_requirements(entity_name, behavior.get("requirements", [])):
                return behavior
        return None

    def resolve_behavior_action(self, entity_name, target_name):
        """!
        @brief Resolves an entity's currently-chosen behavior as an opposed action against a
               target. A behavior names a specific *action* (ex: creatures.toml's wolf names
               "bite", one of its own abilities) rather than a bare skill -- reusing
               resolve_named_ability + select_ability_skill, the exact same lookup the
               player's own named-technique path (ex: "cleave") already uses, rather than
               going through find_attack_ability's equipped-weapon-first priority. That
               priority exists to disambiguate a skill name shared by multiple things; a
               behavior already knows exactly which ability it means, so there's nothing
               to disambiguate.
        @param entity_name The name of the acting entity (ex: a wolf).
        @param target_name The name of the entity being acted against (ex: the player).
        @return The behavior's resolution result dict (same shape as resolve_opposed_action's,
                plus "damage" on a successful hit), or None if no behavior currently matches
                or its named action isn't actually one of the entity's own abilities.
        """
        behavior = self.choose_behavior(entity_name)
        if behavior is None:
            return None

        action_name = behavior.get("action")
        ability = self.resolve_named_ability(entity_name, action_name)
        if ability is None:
            self.event_bus.publish(
                "log_warning", f"{entity_name}'s behavior names unknown action '{action_name}'."
            )
            return None

        skill_name = self.select_ability_skill(entity_name, ability)
        result = self.resolve_opposed_action(entity_name, skill_name, target_name)

        if result["success"]:
            result["damage"] = self.calculate_damage(entity_name, target_name, ability)

        return result

    def _on_action_detected(self, data):
        """!
        @brief Event handler that resolves a detected player action, opposed by a scenario target if one exists,
               and applies damage if the action hit with an attack ability. Combat (a target present that is
               hostile toward the player) narrates once per round via "round_resolved"; everything else
               (no target, or a non-hostile target like a tavern NPC) narrates immediately via "action_resolved".
        @param data The action_detected payload from NLPCore ({skill, score, input}). "skill" is
               usually a plain skill name, but may also be a named technique/spell the player
               owns (ex: "cleave") -- resolve_named_ability/select_ability_skill are what
               convert that into the skill it's actually rolled with, while keeping the named
               ability itself to use directly for damage further down.
        """
        skill_name = data.get("skill")
        if not skill_name:
            return

        named_ability = self.resolve_named_ability(self.player_name, skill_name)
        if named_ability:
            skill_name = self.select_ability_skill(self.player_name, named_ability) or skill_name

        target_name = self._get_target_name()
        test = self.entities.get(target_name, {}).get("test") if target_name else None
        via_test = False
        if test and self.is_test_available(target_name, test, skill_name):
            via_test = True
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

        if result["success"] and target_name and not via_test:
            # A test-path success (ex: a lockpick) is a flat difficulty check, not an attack --
            # it must never also roll bonus weapon damage even if skill_name happens to match
            # an equipped weapon/ability (ex: a future finesse-based dagger matching the chest's
            # finesse-skill lock test).
            ability = named_ability or self.find_attack_ability(self.player_name, skill_name)
            if ability:
                result["damage"] = self.calculate_damage(self.player_name, target_name, ability)

        if target_name:
            defender_details = self.describe_character(target_name, toward_name=self.player_name)
            if defender_details:
                result["defender_details"] = defender_details

        result["input"] = data.get("input")

        in_combat = target_name is not None and self.is_hostile(target_name, self.player_name)
        if in_combat:
            self.round_number += 1
            result["round"] = self.round_number
            # The target gets to act back the same round, via its own [[entity.behavior]]
            # (ex: a wolf biting back) -- this is what makes combat mutual instead of the
            # player only ever being the one rolling to hit. A target with no matching
            # behavior (no behavior data at all, or none of its requirements currently hold,
            # ex: it's already at 0 HP) simply doesn't act.
            enemy_result = self.resolve_behavior_action(target_name, self.player_name)
            if enemy_result:
                result["enemy_action"] = enemy_result
            self.event_bus.publish("round_resolved", result)
        else:
            self.event_bus.publish("action_resolved", result)

    def _on_item_interaction_detected(self, data):
        """!
        @brief Event handler for a free-text item-interaction match (see NLPCore.map_to_item):
               "examine"/"take"/"give"/"trade" against a named item, or "open"/"close" against
               the scene target itself. Deliberately bypasses the whole skill/dice system --
               none of these warrant a roll. Publishes "item_interaction_resolved" either way,
               with enough detail for narration to explain a miss (locked, closed, not present,
               not takeable, ...) rather than staying silent.

               "take"/"trade" move an item from the target to the player; "give" moves one from
               the player to the target -- same transfer_item/transfer_currency primitives,
               just with source/destination swapped. "trade" additionally charges the item's
               TOML `value` as a price (denied outright if the player can't afford it, rather
               than a partial payment). "examine" and "open"/"close" never move anything.
        @param data The item_interaction_detected payload from NLPCore
               ({intent, item_name, input, score}). "item_name" is None for "open"/"close",
               which act on the scene target directly rather than a named item.
        """
        intent = data.get("intent")
        item_name = data.get("item_name")
        input_text = data.get("input")
        target_name = self._get_target_name()

        def resolved(found, **extra):
            self.event_bus.publish("item_interaction_resolved", {
                "intent": intent, "item_name": item_name, "input": input_text, "found": found, **extra,
            })

        if target_name and self.is_locked(target_name):
            resolved(False, reason="locked", container=target_name)
            return

        if intent in ("open", "close"):
            self._resolve_open_close_intent(intent, target_name, resolved)
            return

        if item_name == target_name:
            # Interacting with the container/creature itself, not something inside it -- there's
            # nothing to "take"/"give"/"trade" about the target as a whole.
            if intent == "examine":
                description = self.describe_character(target_name, toward_name=self.player_name) or ""
                resolved(True, description=description)
            else:
                resolved(False, reason="not_takeable")
            return

        if target_name and self.is_closed(target_name):
            # A closed (but unlocked) container can still be examined/opened from the outside
            # (handled above/below) -- only reaching its *contents* is gated here.
            resolved(False, reason="closed", container=target_name)
            return

        if intent == "give":
            if not target_name:
                resolved(False, reason="no_recipient")
                return
            source_name, destination_name = self.player_name, target_name
        else:
            source_name, destination_name = target_name, self.player_name

        if item_name == "currency":
            if intent == "trade":
                # Trading for currency itself is meaningless -- nothing to buy or sell here.
                # Checked before availability, since this is wrong regardless of the amount.
                resolved(False, reason="not_takeable")
                return
            # Currency is a plain "currency" integer field, not an inventory item -- handled
            # separately from transfer_item/source_inventory below.
            available = self.entities.get(source_name, {}).get("currency", 0) if source_name else 0
            if available <= 0:
                resolved(False, reason="not_present")
                return
            if intent == "examine":
                resolved(True, description=f"{available} currency", container=target_name)
            else:
                moved = self.transfer_currency(source_name, destination_name)
                resolved(True, container=target_name, amount=moved)
            return

        source_inventory = self.entities.get(source_name, {}).get("inventory", []) if source_name else []
        if item_name not in source_inventory:
            resolved(False, reason="not_present")
            return

        if intent == "examine":
            description = self.entities.get(item_name, {}).get("description", "")
            resolved(True, description=description, container=target_name)
        elif intent == "trade":
            price = self.entities.get(item_name, {}).get("value", 0)
            buyer_currency = self.entities.get(self.player_name, {}).get("currency", 0)
            if buyer_currency < price:
                resolved(False, reason="cant_afford", price=price)
                return
            self.transfer_currency(self.player_name, target_name, price)
            self.transfer_item(source_name, destination_name, item_name)
            resolved(True, container=target_name, price=price)
        else:
            self.transfer_item(source_name, destination_name, item_name)
            resolved(True, container=target_name)

    def _resolve_open_close_intent(self, intent, target_name, resolved):
        """!
        @brief Handles "open"/"close" against the current scene target directly -- these act on
               the container itself, not a named item inside it, so (unlike the other item
               intents) they never go through map_to_item at all. Gated to subtype ==
               "container" (ex: items.toml's chest) so aiming "open"/"close" at a creature or a
               plain object with no openable nature fails safely instead of silently applying a
               nonsensical condition to it.
        @param intent "open" or "close".
        @param target_name The current scene target's name, or None if there isn't one.
        @param resolved The item_interaction_resolved publisher closure from the caller.
        """
        if not target_name or self.entities.get(target_name, {}).get("subtype") != "container":
            resolved(False, reason="not_openable")
            return

        if intent == "open":
            if not self.is_closed(target_name):
                resolved(False, reason="already_open", container=target_name)
                return
            self.dismiss_condition(target_name, "closed")
        else:
            if self.is_closed(target_name):
                resolved(False, reason="already_closed", container=target_name)
                return
            self.apply_condition(target_name, "closed", duration="permanent", dismiss="")

        resolved(True, container=target_name)

    def _get_target_name(self):
        """!
        @brief Picks the current opposed target from the instantiated scenario entities.
        @return The name of the first non-player entity instance in the scenario, or None if there isn't one.
        """
        for instance_name in self.scenario_entities:
            if instance_name != self.player_name:
                return instance_name
        return None

    def _save_slot_dir(self, slot_name):
        """!
        @brief Resolves a save slot name to its directory under Saves/. LLMCore computes this
               same path independently (it has no reference to DMCore, by design -- the two
               only ever communicate through events) and must be kept in sync with it, since
               both write sibling files into the same slot directory.
        @param slot_name The save slot's name, as given by the player.
        @return The absolute directory path for this slot. os.path.basename strips any
                path-separator components first, so a slot name can't escape Saves/ (ex: a
                slot literally named "../../etc" resolves to Saves/etc, not outside Saves/).
        """
        base_dir = os.path.dirname(os.path.abspath(__file__))
        safe_name = os.path.basename(slot_name.strip()) or "unnamed"
        return os.path.join(base_dir, "Saves", safe_name)

    def save_game(self, slot_name):
        """!
        @brief Writes this core's mechanical state to Saves/<slot_name>/dm_state.json -- a
               diff from a fresh instantiation (round_number, scenario_entities, and each
               instance's hp/active_conditions/currency/inventory), not a raw dump of
               self.entities, which also holds every static template. Loading re-instantiates
               fresh from Rules/Fantasy TOML and overlays this diff on top, so a save doesn't
               freeze stale stats if templates are edited between sessions. LLMCore
               independently saves its own sibling file (context_window) for the same slot --
               see CLAUDE.md's "Saving and loading" section for why the two cores don't share
               one combined file.
        @param slot_name The save slot's name (used as a directory name under Saves/).
        """
        slot_dir = self._save_slot_dir(slot_name)
        os.makedirs(slot_dir, exist_ok=True)
        data = {
            "version": 1,
            "scenario_key": self.scenario_key,
            "player_name": self.player_name,
            "round_number": self.round_number,
            "scenario_entities": self.scenario_entities,
            "instances": {
                name: {
                    "hp": self.get_current_hp(name),
                    "active_conditions": self.entities.get(name, {}).get("active_conditions", {}),
                    "currency": self.entities.get(name, {}).get("currency", 0),
                    "inventory": self.entities.get(name, {}).get("inventory", []),
                }
                for name in self.scenario_entities
            },
        }
        with open(os.path.join(slot_dir, "dm_state.json"), "w") as f:
            json.dump(data, f, indent=2)
        self.event_bus.publish("log_info", f"Game saved to slot '{slot_name}'.")
        # Distinct from the log_info line above (Debug-tab-only) -- GUI/Textual subscribe to
        # this directly so a save gets a plain visible confirmation in the main history pane
        # without spending an LLM call narrating something as mundane as "you saved the game."
        self.event_bus.publish("game_saved", {"slot": slot_name})

    def load_game(self, slot_name):
        """!
        @brief Restores mechanical state from Saves/<slot_name>/dm_state.json: re-reads every
               Rules/Fantasy/*.toml template fresh via load_rules, then reloads the saved
               scenario via the same load_scenario_definition/load_scenario path __init__
               uses, then overlays each instance's saved hp/active_conditions/currency/
               inventory on top. A saved instance with no matching entity after re-instancing
               (ex: the scenario file changed) is skipped rather than crashing.

               The load_rules call is not optional: self.entities holds both static templates
               and live instances under the same keys (a single-occurrence instance like
               "wolf" *overwrites* self.entities["wolf"] the moment load_scenario first runs --
               see "Scenario instancing" in CLAUDE.md), so calling load_scenario() alone would
               re-instance from whatever's currently sitting in self.entities, which after the
               very first load is the *live, possibly-mutated instance*, not the pristine
               template. Re-running load_rules first is what actually makes a resumed save
               pick up current TOML stats rather than freezing stale in-memory ones.

               Publishes "game_loaded" on success -- deliberately not "scenario_loaded", so
               LLMCore restores its own saved state silently instead of narrating a brand-new
               opening scene on every resume. Publishes "game_load_failed" if the slot doesn't
               exist, so the player gets feedback rather than the request silently doing
               nothing (same rule action_not_understood already follows for unmatched input).
        @param slot_name The save slot's name to load.
        """
        path = os.path.join(self._save_slot_dir(slot_name), "dm_state.json")
        if not os.path.exists(path):
            self.event_bus.publish("log_error", f"No save slot named '{slot_name}'.")
            self.event_bus.publish("game_load_failed", {"slot": slot_name, "reason": "not_found"})
            return

        with open(path, "r") as f:
            data = json.load(f)

        self.player_name = data.get("player_name", self.player_name)
        self.round_number = data.get("round_number", 0)
        self.scenario_key = data.get("scenario_key", self.scenario_key)
        self.load_rules(os.path.join("Rules", "Fantasy"))
        self.load_scenario_definition(self.scenario_key)
        self.load_scenario()

        for name, state in data.get("instances", {}).items():
            entity = self.entities.get(name)
            if entity is None:
                continue
            entity["hp"] = state.get("hp", entity.get("max_hp", 0))
            entity["active_conditions"] = state.get("active_conditions", {})
            entity["currency"] = state.get("currency", entity.get("currency", 0))
            entity["inventory"] = state.get("inventory", entity.get("inventory", []))

        self.event_bus.publish("log_info", f"Game loaded from slot '{slot_name}'.")
        self.event_bus.publish("game_loaded", {
            "slot": slot_name,
            "name": self.scenario.get("name"),
            "description": self.scenario.get("description"),
            "characters": self._describe_scenario_characters(),
        })

    def _on_save_requested(self, data):
        """!
        @brief Event handler for a save request (from NLPCore's text intercept or a GUI/Textual
               button, both publishing the same event) -- a missing/blank slot name just logs
               a warning rather than saving to some default location unasked.
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