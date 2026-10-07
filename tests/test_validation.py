import unittest
from dm.DM_Core import DMCore
from dm.DM_Rules import list_available_scenarios
from tests.event_contract import ValidatingEventBus
from resolution.World_Context import WorldContext
from tests.support import (
    DMTestCase,
)
# Module-level hooks start the shared patches (tests/support.py) for every test in this file.
from tests.support import setUpModule, tearDownModule  # noqa: F401


class TestDataValidator(unittest.TestCase):
    """!@brief resolution/Data_Validation.py crossed directly -- loaded data in, Problem records
        out, no DMCore, event bus or scenario boot."""

    def _validate(self, entities):
        from resolution.Data_Validation import DataValidator
        world = WorldContext(
            entities=entities, rules={"equip_slot": [{"supertype": "character", "slots": ["main_hand"]}]},
            skills={"blades": {}},
        )
        return DataValidator(world, {}, {}).validate()

    def test_clean_data_returns_no_problems(self):
        self.assertEqual(self._validate({
            "hero": {"name": "hero", "supertype": "character", "equipped": {"main_hand": "stick"}, "skill": "blades"},
            "stick": {"name": "stick", "supertype": "object"},
        }), [])

    def test_problems_come_back_as_records_instead_of_log_events(self):
        problems = self._validate({
            "hero": {"name": "hero", "supertype": "character", "equipped": {"tail": "x"}, "skill": "nope"},
        })
        messages = [p.message for p in problems]
        self.assertTrue(any("slot 'tail'" in m for m in messages), messages)
        self.assertTrue(any("unknown skill 'nope'" in m for m in messages), messages)
        self.assertTrue(all(isinstance(p.message, str) for p in problems))


class TestValidation(DMTestCase):
    """Data_Validation.py's referential-integrity *and* field-shape/type checks. Each
    synthetic-data test injects a minimal bad entity/entity_template/location directly into
    self.dm_core's own dicts (rather than authoring a throwaway TOML file) and re-runs
    validate_loaded_data() -- since the real arena fixture is already proven clean below, any
    error captured after that must come from the injected data."""

    def test_real_shipped_data_boots_with_zero_validation_errors(self):
        # Every real scenario this repo ships, across every setting -- a regression guard that a
        # future data edit doesn't quietly introduce a dangling reference, and proof the
        # validator is genuinely setting-agnostic (no Fantasy-specific assumption anywhere in
        # Data_Validation.py).
        for setting in ("Fantasy", "Zombie", "Pathfinder"):
            for scenario_key, _name, _description in list_available_scenarios(setting):
                errors = []
                bus = ValidatingEventBus()
                bus.subscribe("log_error", errors.append)
                DMCore(bus, scenario_name=scenario_key, setting=setting)
                self.assertEqual(errors, [], f"{setting}/{scenario_key} produced validation errors: {errors}")

    def test_skill_reference_checks(self):
        self.dm_core.entities["bad_skills_widget"] = {
            "name": "bad_skills_widget", "supertype": "object", "skill": "nonexistent_skill",
            "abilities": [{"name": "zap", "skill": ["blades", "nonexistent_ability_skill"]}],
            "test": {"skill": ["nonexistent_test_skill"]},
            "craft": {"skill": ["nonexistent_craft_skill"]},
            "notice": {"skill": "nonexistent_notice_skill"},
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()

        for expected in (
            "nonexistent_skill", "nonexistent_ability_skill", "nonexistent_test_skill",
            "nonexistent_craft_skill", "nonexistent_notice_skill",
        ):
            self.assertTrue(any(expected in e for e in errors), f"missing error for {expected}")
        # "blades" is a real skill -- not flagged alongside its bogus list-mate.
        self.assertFalse(any("'blades'" in e for e in errors))

    def test_behavior_action_reference(self):
        self.dm_core.entities["bad_behavior_widget"] = {
            "name": "bad_behavior_widget", "supertype": "creature",
            "abilities": [{"name": "real move", "skill": "brawling"}],
            "behavior": [
                {"requirements": [], "action": "nonexistent_move"},
                {"requirements": [], "action": "real move"},
                {"requirements": [], "action": "advance"},
                {"requirements": [], "action": "retreat"},
            ],
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()

        self.assertTrue(any("nonexistent_move" in e for e in errors))
        # A real owned ability, and the two reserved movement words, are never flagged.
        self.assertFalse(any("real move" in e for e in errors))
        self.assertFalse(any("advance" in e for e in errors))
        self.assertFalse(any("retreat" in e for e in errors))

    def test_entity_name_reference_checks(self):
        self.dm_core.entities["bad_refs_widget"] = {
            "name": "bad_refs_widget", "supertype": "object",
            "inventory": ["nonexistent_item"],
            "equipped": {"rhand": "nonexistent_weapon"},
            "replace_with": "nonexistent_husk",
            "damage_value": {"dice": 1, "pips": 0, "bonus": "user.nonexistent_rule"},
            "abilities": [{
                "name": "bad ability", "skill": "arcane",
                "summon": {"name": "nonexistent_summon"},
                "materials": [{"item": "nonexistent_material", "quantity": 1}],
            }],
            "craft": {"skill": ["strength"], "materials": [{"item": "nonexistent_craft_material", "quantity": 1}]},
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()

        for expected in (
            "nonexistent_item", "nonexistent_weapon", "nonexistent_husk", "nonexistent_rule",
            "nonexistent_summon", "nonexistent_material", "nonexistent_craft_material",
        ):
            self.assertTrue(any(expected in e for e in errors), f"missing error for {expected}")

    def test_container_content_type_reference_checks(self):
        self.dm_core.entities["bad_container_widget"] = {
            "name": "bad_container_widget", "supertype": "object",
            "container_allowed_supertypes": ["spell"],
            "inventory": ["iron dagger", "suggestion"],
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()

        self.assertTrue(any("iron dagger" in e and "container_allowed_supertypes" in e for e in errors))
        # "suggestion" is a real spell -- matches the allow-list, never flagged.
        self.assertFalse(any("'suggestion'" in e for e in errors))

    def test_container_content_subtype_reference_check(self):
        self.dm_core.entities["bad_quiver_widget"] = {
            "name": "bad_quiver_widget", "supertype": "object",
            "container_allowed_subtypes": ["potion"],
            "inventory": ["iron dagger"],
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()

        self.assertTrue(any("iron dagger" in e and "container_allowed_subtypes" in e for e in errors))

    def test_summon_template_reference(self):
        self.dm_core.entities["bad_summon_template_widget"] = {
            "name": "bad_summon_template_widget", "supertype": "object",
            "abilities": [{"name": "conjure", "skill": "arcane", "summon": {"template": "nonexistent_template"}}],
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()
        self.assertTrue(any("nonexistent_template" in e for e in errors))

    def test_entity_template_forbidden_fields(self):
        self.dm_core.entity_templates["bad_shape_template"] = {
            "name": "bad_shape_template", "supertype": "creature",
            "skills": {"blades": {"dice": 2, "pips": 0}}, "max_hp": 10,
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()

        self.assertTrue(any("'skills'" in e for e in errors))
        self.assertTrue(any("'max_hp'" in e for e in errors))
        # "name" is required on a template (self.entity_templates is indexed by it) -- never
        # itself flagged as a forbidden field.
        self.assertFalse(any("'name'" in e for e in errors))

    def test_location_reference_checks(self):
        self.dm_core.locations["bad_location"] = {
            "key": "bad_location", "start_room": "nonexistent_start_room", "return_to": "nonexistent_location",
            "entities": [{"name": "nonexistent_persistent_entity"}],
            "exit": [{"destination": "nonexistent_destination"}, {"destination": "arena_grounds", "arrival_room": "nonexistent_arrival_room"}],
            "rooms": {
                "real_room": {
                    "key": "real_room", "bands": 2,
                    "entities": [{"template": "nonexistent_room_template"}],
                    "exit": [{"destination": "nonexistent_sibling_room"}],
                },
            },
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()

        for expected in (
            "nonexistent_start_room", "nonexistent_location", "nonexistent_persistent_entity",
            "nonexistent_destination", "nonexistent_arrival_room", "nonexistent_room_template",
            "nonexistent_sibling_room",
        ):
            self.assertTrue(any(expected in e for e in errors), f"missing error for {expected}")

    def test_player_placeholder_in_entities_list_is_not_flagged(self):
        self.dm_core.locations["placeholder_location"] = {
            "key": "placeholder_location", "entities": [{"name": "player"}],
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()
        self.assertEqual(errors, [])

    # --- Field shape/type -----------------------------------------------------------------

    def test_scalar_field_type_checks(self):
        self.dm_core.entities["bad_scalars_widget"] = {
            "name": "bad_scalars_widget", "supertype": "object",
            "max_hp": "twenty", "is_party": "yes", "description": 123,
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()

        for expected in ("max_hp should be a int/float", "is_party should be a bool", "description should be a str"):
            self.assertTrue(any(expected in e for e in errors), f"missing error for {expected!r}")

    def test_scalar_field_type_checks_tolerate_absent_fields(self):
        # A field simply not being authored at all is never an error -- only the wrong type
        # present is.
        self.dm_core.entities["bare_widget"] = {"name": "bare_widget", "supertype": "object"}
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()
        self.assertEqual(errors, [])

    def test_string_list_field_type_check(self):
        self.dm_core.entities["bad_tags_widget"] = {
            "name": "bad_tags_widget", "supertype": "object", "damage_tags": "slashing",
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()
        self.assertTrue(any("damage_tags should be a list of strings" in e for e in errors))

    def test_dice_table_field_type_checks(self):
        self.dm_core.entities["bad_damage_widget"] = {
            "name": "bad_damage_widget", "supertype": "object",
            "damage_value": {"dice": "two", "pips": 0, "bonus": []},
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()
        self.assertTrue(any("damage_value.dice should be a number" in e for e in errors))
        self.assertTrue(any('damage_value.bonus should be a number or a "user.<rule>" string' in e for e in errors))

    def test_dice_table_field_rejects_a_bare_non_user_prefixed_string(self):
        # Only "user.<field>" strings are tolerated on dice/pips -- an arbitrary bad string
        # (ex: a typo'd number) still has to be flagged.
        self.dm_core.entities["typo_widget"] = {
            "name": "typo_widget", "supertype": "object", "damage_value": {"dice": "two", "pips": 0},
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()
        self.assertTrue(any("damage_value.dice should be a number" in e for e in errors))

    def test_dice_table_field_tolerates_the_user_weapon_dice_string_shape(self):
        # techniques.toml's own "cleave" -- dice/pips resolved off the wielded weapon at roll
        # time, a documented string shape, not a real mistake.
        self.dm_core.entities["cleave_like"] = {
            "name": "cleave_like", "supertype": "technique",
            "damage_value": {"dice": "user.weapon.dice", "pips": "user.weapon.pips", "bonus": 0},
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()
        self.assertEqual(errors, [])

    def test_skills_table_shape_check(self):
        self.dm_core.entities["bad_skills_shape_widget"] = {
            "name": "bad_skills_shape_widget", "supertype": "creature",
            "skills": {"blades": {"dice": "five", "pips": 0}},
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()
        self.assertTrue(any("[entity.skills].blades.dice should be a number" in e for e in errors))

    def test_equipped_table_shape_check(self):
        self.dm_core.entities["bad_equipped_widget"] = {
            "name": "bad_equipped_widget", "supertype": "creature", "equipped": {"rhand": 123},
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()
        self.assertTrue(any("[entity.equipped] should be a table of slot -> item name" in e for e in errors))

    def test_attitudes_table_shape_checks(self):
        self.dm_core.entities["bad_attitudes_widget"] = {
            "name": "bad_attitudes_widget", "supertype": "creature",
            "attitudes": {
                "default": [0, 0],  # wrong length
                "name": [{"gladstone": [0, "not a number", 0]}],
            },
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()
        self.assertTrue(any(
            "[entity.attitudes].default should be a list of exactly 3 numbers" in e for e in errors
        ))
        self.assertTrue(any(
            "[[entity.attitudes.name]].gladstone[1] should be a number" in e for e in errors
        ))

    def test_attitudes_axes_allow_varied_values_only_on_entity_templates(self):
        varied_axes = {"default": [{"min": -40, "max": 40}, 0, {"min": -40, "max": 40}]}
        self.dm_core.entity_templates["template_with_varied_attitudes"] = {
            "name": "template_with_varied_attitudes", "attitudes": varied_axes,
        }
        self.dm_core.entities["entity_with_varied_attitudes"] = {
            "name": "entity_with_varied_attitudes", "supertype": "creature", "attitudes": varied_axes,
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()

        self.assertFalse(any("template_with_varied_attitudes" in e for e in errors))
        self.assertTrue(any(
            "entity_with_varied_attitudes" in e and "[0] should be a number" in e for e in errors
        ))

    def test_behavior_list_shape_checks(self):
        self.dm_core.entities["bad_behavior_widget"] = {
            "name": "bad_behavior_widget", "supertype": "creature",
            "behavior": [
                {"requirements": [{"field": "hp_per_remain"}], "action": "bite"},  # missing operator/value
                {"requirements": [], "action": 123},
            ],
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()
        self.assertTrue(any(
            "requirements[0] should be a {field, operator, value} table" in e for e in errors
        ))
        self.assertTrue(any("[[entity.behavior]][1] action should be a string" in e for e in errors))

    def test_behavior_list_accepts_nested_all_any_none_requirements(self):
        self.dm_core.entities["nested_requirement_widget"] = {
            "name": "nested_requirement_widget", "supertype": "creature",
            "abilities": [{"name": "bite", "supertype": "innate", "subtype": "weapon", "skill": "brawling"}],
            "behavior": [{
                "requirements": [{"any": [
                    {"field": "has_condition:prone", "operator": "==", "value": True},
                    {"all": [{"field": "hp_per_remain", "operator": "between", "value": [0.0, 0.5]}]},
                ]}],
                "action": "bite",
            }],
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()
        self.assertFalse(any("nested_requirement_widget" in e for e in errors))

    def test_ability_shape_checks_cover_targets_summon_and_materials(self):
        self.dm_core.entities["bad_ability_widget"] = {
            "name": "bad_ability_widget", "supertype": "creature",
            "abilities": [{
                "name": "bad_zap", "supertype": "innate", "subtype": "weapon", "skill": "arcane",
                "targets": {"number": "one", "side": 5},
                "summon": {"name": "wraith", "template": "generated_stranger"},  # both, not exactly one
                "materials": [{"item": "iron filings", "quantity": "one"}],
            }],
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()

        for expected in (
            "targets.number should be a number", "targets.side should be a string",
            "summon should author exactly one of \"name\"/\"template\"",
            "materials[0].quantity should be a number",
        ):
            self.assertTrue(any(expected in e for e in errors), f"missing error for {expected!r}")

    def test_ability_shape_checks_also_apply_to_a_standalone_ability_entity(self):
        # A weapon/spell/technique catalog entity is itself ability-shaped at its own top
        # level, not just when referenced from some other entity's "abilities" list.
        self.dm_core.entities["bad_weapon"] = {
            "name": "bad_weapon", "supertype": "object", "subtype": "weapon", "skill": "blades",
            "range": "far",
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()
        self.assertTrue(any("entity 'bad_weapon' range should be a number" in e for e in errors))

    def test_entity_template_generation_field_checks(self):
        self.dm_core.entity_templates["bad_template"] = {
            "name": "bad_template", "target_cr": "not player, party, or a number",
            "cr_multiplier": "big", "hint": 5,
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()

        for expected in (
            "target_cr should be a number", "cr_multiplier should be a number or a varied value",
            "hint should be a string or a varied value",
        ):
            self.assertTrue(any(expected in e for e in errors), f"missing error for {expected!r}")

    def test_entity_template_generation_fields_allow_varied_value_shapes(self):
        self.dm_core.entity_templates["varied_template"] = {
            "name": "varied_template", "target_cr": "party",
            "cr_multiplier": {"min": 0.8, "max": 1.2},
            "hint": [{"a stranger": 30}, {"a merchant": 30}],
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()
        self.assertEqual(errors, [])

    def test_location_shape_checks(self):
        self.dm_core.locations["bad_shape_location"] = {
            "key": 123, "name": "Bad Shapes", "grid": {"x": "zero", "y": 0},
            "exit": [{"destination": 5, "aliases": "not a list"}],
            "start_room": "shapeless_room",
            "rooms": {
                "shapeless_room": {
                    "key": "shapeless_room", "bands": "two", "enclosed": "yes",
                    "exit": [{"destination": "shapeless_room", "band": "one"}],
                },
            },
        }
        errors = self._capture("log_error")
        self.dm_core.validate_loaded_data()

        for expected in (
            "key should be a str", "grid should be a {x, y} table of numbers",
            "[[location.exit]] destination should be a string",
            "[[location.exit]] aliases should be a list of strings",
            "bands should be a number", "enclosed should be a boolean",
            "[[location.room.exit]] band should be a number",
        ):
            self.assertTrue(any(expected in e for e in errors), f"missing error for {expected!r}")


if __name__ == "__main__":
    unittest.main()
