import copy
import os
import tomllib


def scenario_file_path(scenario_name):
    """!
    @brief Resolves a scenario name to its file path under Rules/Fantasy/scenarios/.
    @param scenario_name The scenario's filename without extension (ex: "arena", "tavern").
    @return The absolute filepath, whether or not it actually exists.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "Rules", "Fantasy", "scenarios", f"{scenario_name}.toml")


class RulesMixin:
    """!
    @brief TOML rules/entity loading and scenario instancing (DMCore mixin -- only ever
           composed into DMCore, never instantiated on its own; relies on
           self.skills/self.entities/self.rules/self.scenario/self.scenario_entities/
           self.event_bus/self.player_name, set up by DMCore.__init__).
           _describe_scenario_characters calls self.describe_character (SocialMixin).
    """

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

        # Keeps current_target in sync with scenario_entities on every load -- covers
        # __init__, load_game, and ad-hoc test scenarios that reassign self.scenario directly
        # and call load_scenario() again (see CLAUDE.md's "Scenario instancing").
        self.current_target = self._choose_combat_target()

        self.event_bus.publish("log_info", f"Scenario loaded: {self.scenario_entities}")
