import copy
import shutil
import unittest
from unittest.mock import patch
import resolution.Combat_Resolution as Combat_Resolution
import resolution.Social_Resolution as Social_Resolution
from resolution.Program_Interpreter import evaluate_condition, run_program
from dm.DM_ActionOutcome import (
    ActionPreventedOutcome,
    CureEffect,
    DamageEffect,
    DispelEffect,
    MissingSpellMaterialsOutcome,
    MovementOutcome,
    RolledOutcome,
    SummonEffect,
)
from tests.event_contract import ValidatingEventBus
from resolution.World_Context import WorldContext
import resolution.Combat_Actions as Combat_Actions
from tests.support import (
    DMTestCase,
    apply_effects,
)
# Module-level hooks start the shared patches (tests/support.py) for every test in this file.
from tests.support import setUpModule, tearDownModule  # noqa: F401


class TestSpellMaterials(DMTestCase):
    # arena's own wolf is already a live, hostile current_target the moment DMCore loads (its
    # own auto-claim at scenario load, same state every other TestCombatLoop test relies on),
    # so casting at it resolves as combat ("round_resolved"), not the no-combat "action_resolved"
    # path. "arc lance" (spells.toml) is on gladstone's own abilities list and needs 1x
    # "iron filings", pre-seeded in his starting inventory (characters.toml). Casting resolves as
    # a flat check against arc lance's own authored "difficulty" (10) -- gladstone's own 2D
    # arcane vs. that fixed number, not an opposed roll against the wolf at all (see
    # _resolve_roll's own "ability.get('difficulty')" branch, DM_Core.py) -- so a fixed
    # random.randint value alone is enough to force either outcome.
    def setUp(self):
        super().setUp()
        self.resolved = self._capture("round_resolved")

    def _cast(self, extra_clauses=None):
        clauses = [{"kind": "action", "skill": "arc lance"}]
        clauses.extend(extra_clauses or [])
        self.dm_core._on_turn_detected({"clauses": clauses, "input": "I cast arc lance at the wolf"})
        return self.resolved[-1]["actions"][0]

    def test_missing_material_fails_the_cast_without_rolling(self):
        self.dm_core.entities["gladstone"]["inventory"].remove("iron filings")

        result = self._cast()

        self.assertIsInstance(result, MissingSpellMaterialsOutcome)

    def test_successful_cast_consumes_the_material(self):
        # gladstone's 2D arcane at a fixed per-die value of 6 rolls 12, clearing arc lance's own
        # difficulty of 10.
        with patch("random.randint", return_value=6):
            result = self._cast()

        self.assertTrue(result.success)
        self.assertTrue(any(isinstance(effect, DamageEffect) for effect in result.effects))
        self.assertNotIn("iron filings", self.dm_core.entities["gladstone"]["inventory"])

    def test_failed_cast_still_consumes_the_material(self):
        # gladstone's 2D arcane at a fixed per-die value of 1 rolls 2, well under arc lance's own
        # difficulty of 10 -- the material is still spent, same as a botched craft attempt's own
        # materials.
        with patch("random.randint", return_value=1):
            result = self._cast()

        self.assertFalse(result.success)
        self.assertFalse(any(isinstance(effect, DamageEffect) for effect in result.effects))
        self.assertNotIn("iron filings", self.dm_core.entities["gladstone"]["inventory"])

    def test_entity_test_on_the_target_overrides_the_abilitys_own_difficulty(self):
        # A target that authors its own [entity.test] for the ability's skill is the actual
        # "specify the skill to resist" mechanism a spell is expected to lean on -- it's checked
        # ahead of ability.get("difficulty") in _resolve_roll (DM_Core.py) and wins outright when
        # it matches, overriding the ability's own flat difficulty fallback entirely.
        self.dm_core.entities["wolf"]["test"] = {"skill": ["arcane"], "difficulty": 20}

        with patch("random.randint", return_value=6):
            result = self._cast()

        # gladstone's 2D arcane at a fixed per-die value of 6 rolls 12 -- clears arc lance's own
        # difficulty (10) but not the wolf's own authored resistance (20), and a via_test roll
        # never rolls the ability's own bonus weapon damage.
        self.assertFalse(result.success)
        self.assertFalse(any(isinstance(effect, DamageEffect) for effect in result.effects))

    def test_a_different_ability_never_touches_the_material(self):
        # gladstone's own longsword (skill="blades") carries no "materials" field at all --
        # _consume_spell_materials_if_rolled must be a complete no-op for it.
        with patch("random.randint", return_value=6):
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "blades"}], "input": "I attack with my sword"})

        self.assertIn("iron filings", self.dm_core.entities["gladstone"]["inventory"])


class TestEntityBehavior(DMTestCase):
    def setUp(self):
        super().setUp()
        self.resolved = self._capture("round_resolved")

    def test_choose_behavior_matches_while_the_entity_is_alive(self):
        # debug.toml's wolf: a single behavior, "always bite while hp_per_remain >= 0.01".
        behavior = Combat_Actions.choose_behavior(self.dm_core.world, "wolf")
        assert behavior is not None
        self.assertEqual(behavior["action"], "bite")


    def test_choose_behavior_can_pick_between_a_ranged_and_melee_option_by_distance(self):
        # "distance_to_target" isn't used by any shipped creature yet (see get_comparable_value),
        # but is available for exactly this: a hypothetical archer-brawler choosing its bow
        # while the gap is still open, falling to its fists once the target closes in --
        # opponent_name has to be passed through choose_behavior for this to resolve at all.
        self.dm_core.entities["archer_dummy"] = {
            "name": "archer_dummy", "max_hp": 20, "skills": {},
            "behavior": [
                {
                    "requirements": [{"field": "distance_to_target", "operator": ">", "value": 0}],
                    "action": "shoot",
                },
                {"requirements": [], "action": "punch"},
            ],
        }
        self.dm_core.entities["archer_dummy"]["band"] = 4
        self.dm_core.entities["gladstone"]["band"] = 1

        behavior = Combat_Actions.choose_behavior(self.dm_core.world, "archer_dummy", "gladstone")
        self.assertEqual(behavior["action"], "shoot")

        self.dm_core.entities["archer_dummy"]["band"] = 1
        behavior = Combat_Actions.choose_behavior(self.dm_core.world, "archer_dummy", "gladstone")
        self.assertEqual(behavior["action"], "punch")


    def test_has_condition_gates_a_behavior_entry_off_the_entitys_own_condition(self):
        # A paralyzed creature shouldn't "act" at 0 dice -- it should match nothing and stand
        # down entirely, the same "no matching entry" fallback an entity with no behavior list
        # at all already gets. See Rules/Pathfinder/reference/pathfinder_mapping.toml's
        # condition_pattern "A" recipe.
        self.dm_core.entities["paralyzed_dummy"] = {
            "name": "paralyzed_dummy", "max_hp": 20, "skills": {},
            "active_conditions": {"paralyzed": {"duration": "permanent", "dismiss": ""}},
            "behavior": [
                {
                    "requirements": [{"field": "has_condition:paralyzed", "operator": "==", "value": False}],
                    "action": "bite",
                },
            ],
        }
        self.assertIsNone(Combat_Actions.choose_behavior(self.dm_core.world, "paralyzed_dummy"))

        del self.dm_core.entities["paralyzed_dummy"]["active_conditions"]["paralyzed"]
        behavior = Combat_Actions.choose_behavior(self.dm_core.world, "paralyzed_dummy")
        self.assertEqual(behavior["action"], "bite")


    def test_opponent_has_condition_reacts_to_the_targets_own_condition(self):
        # A creature that presses its advantage while its target is stunned, falling back to a
        # normal attack otherwise -- opponent_name has to be threaded through choose_behavior
        # for this to resolve at all, same as distance_to_target above.
        self.dm_core.entities["predator_dummy"] = {
            "name": "predator_dummy", "max_hp": 20, "skills": {},
            "behavior": [
                {
                    "requirements": [{"field": "opponent_has_condition:stunned", "operator": "==", "value": True}],
                    "action": "finishing_blow",
                },
                {"requirements": [], "action": "bite"},
            ],
        }
        self.assertEqual(Combat_Actions.choose_behavior(self.dm_core.world, "predator_dummy", "gladstone")["action"], "bite")

        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "stunned", duration="rounds", length=1, dismiss="")
        self.assertEqual(
            Combat_Actions.choose_behavior(self.dm_core.world, "predator_dummy", "gladstone")["action"], "finishing_blow",
        )

        # No opponent_name at all -- resolves to None, same as distance_to_target with no
        # opponent, never accidentally matching a status requirement (which never passes one).
        self.assertIsNone(
            Combat_Resolution.get_comparable_value(self.dm_core.world, "predator_dummy", "opponent_has_condition:stunned"),
        )


    def test_wraith_stands_down_entirely_while_warded(self):
        # creatures.toml's "wraith" is the shipped has_condition example -- both its behavior
        # entries share a "not warded" gate, so a holy ward suppresses its turn entirely rather
        # than just its preferred attack (choose_behavior returns None, same as an entity with
        # no behavior list at all).
        self.assertEqual(Combat_Actions.choose_behavior(self.dm_core.world, "wraith", "gladstone")["action"], "chilling claw")

        self.dm_core.entities["wraith"]["active_conditions"] = {
            "warded": {"duration": "scene", "dismiss": ""},
        }
        self.assertIsNone(Combat_Actions.choose_behavior(self.dm_core.world, "wraith", "gladstone"))


    def test_wraith_prefers_life_drain_against_a_wounded_target(self):
        # creatures.toml's "wraith" is the shipped opponent_has_condition example -- it favors
        # draining an already-wounded target over its plain claw, checked ahead of the fallback
        # attack in declaration order.
        self.assertEqual(Combat_Actions.choose_behavior(self.dm_core.world, "wraith", "gladstone")["action"], "chilling claw")

        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "wounded", duration="permanent", dismiss="")
        self.assertEqual(Combat_Actions.choose_behavior(self.dm_core.world, "wraith", "gladstone")["action"], "life drain")


    def test_resolve_behavior_action_strikes_back_and_applies_damage(self):
        # An unarmored, skill-less target so the wolf's bite always lands and nothing
        # reduces the raw damage -- isolates resolve_behavior_action from armor/opposed-skill
        # specifics, which are already covered by TestDamageCalculation/TestOpposedResolution.
        self.dm_core.entities["target_dummy"] = {"name": "target_dummy", "max_hp": 20, "skills": {}}

        with patch("random.randint", return_value=4):
            result = Combat_Actions.resolve_behavior_action(self.dm_core.world, "wolf", "target_dummy")

        assert result is not None
        self.assertTrue(result.success)
        self.assertEqual(result.skill, "brawling")
        damage_effects = [effect for effect in result.effects if isinstance(effect, DamageEffect)]
        self.assertEqual(len(damage_effects), 1)
        damage = damage_effects[0]
        self.assertGreater(damage.net_damage, 0)
        self.assertEqual(
            Combat_Resolution.get_current_hp(self.dm_core.world, "target_dummy"),
            20 - damage.net_damage,
        )


    def test_resolve_behavior_action_nudges_the_defenders_attitude_toward_the_attacker(self):
        # NPC-action-driven attitude drift: resolve_behavior_action shares
        # _apply_damage_if_hit's own call-site shape (DM_Core.py's _nudge_combat_hit_attitude)
        # -- the "combat_hit" nudge lands on target_dummy's attitude toward "wolf", the entity
        # that actually swung, never toward the player, who wasn't involved in this turn at all.
        self.dm_core.entities["target_dummy"] = {
            "name": "target_dummy", "max_hp": 20, "skills": {},
            "attitudes": {"default": [0, 0, 0]},
        }

        with patch("random.randint", return_value=4):
            result = Combat_Actions.resolve_behavior_action(self.dm_core.world, "wolf", "target_dummy")

        assert result is not None
        self.assertTrue(result.success)
        damage_effects = [effect for effect in result.effects if isinstance(effect, DamageEffect)]
        magnitude = damage_effects[0].net_damage / 20
        disposition, threat = (
            self.dm_core.get_attitude("target_dummy", "wolf")[axis] for axis in (0, 1)
        )
        self.assertAlmostEqual(disposition, -20 * magnitude)
        self.assertAlmostEqual(threat, -15 * magnitude)
        self.assertEqual(
            self.dm_core.get_attitude("target_dummy", self.dm_core.player_name), [0, 0, 0],
        )


    def test_resolve_behavior_action_bonds_bystanders_toward_the_attacker(self):
        # "Bonds made on the battlefield" generalizes the same way -- thane already hates
        # target_dummy specifically (a name-override disposition <= -100 toward it), so it
        # should warm toward "wolf", who just hit it, not toward the player, who never acted
        # this turn.
        self.dm_core.entities["target_dummy"] = {"name": "target_dummy", "max_hp": 20, "skills": {}}
        self.dm_core.scenario_entities.append("target_dummy")
        self.dm_core.entities["thane"]["attitudes"] = {
            "default": [40, 40, 40],
            "name": [{"target_dummy": [-100, 0, 0]}],
        }

        with patch("random.randint", return_value=4):
            result = Combat_Actions.resolve_behavior_action(self.dm_core.world, "wolf", "target_dummy")

        assert result is not None
        damage_effects = [effect for effect in result.effects if isinstance(effect, DamageEffect)]
        magnitude = damage_effects[0].net_damage / 20
        # thane has no name-override toward "wolf" specifically, so its own "default" [40, 40,
        # 40] base is what the shared_enemy drift stacks on top of.
        disposition = self.dm_core.get_attitude("thane", "wolf")[0]
        self.assertAlmostEqual(disposition, 40 + 5 * magnitude)
        self.assertEqual(self.dm_core.get_attitude("thane", self.dm_core.player_name), [40, 40, 40])


    def test_roll_initiative_pools_dodge_and_untrained_observation(self):
        # wolf has dodge 6D/0 pips and no observation skill at all -- rules.toml's [[initiative]]
        # still pools it in at the same untrained 0D/0 pips resolve_action defaults missing
        # skills to, so the pool is just dodge's own 6D (observation contributes nothing).
        with patch("random.randint", return_value=4):
            initiative = Combat_Actions.roll_initiative(self.dm_core.world, "wolf")
        self.assertEqual(initiative, 24)


    def test_current_target_advances_to_next_hostile_when_current_dies(self):
        Combat_Resolution.apply_damage(self.dm_core.world, "wolf", 999)
        with patch("random.randint", return_value=1):
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "athletics"}], "input": "I reposition"})
        self.assertEqual(self.dm_core.current_target, "wolf_2")


class TestProgramInterpreter(unittest.TestCase):
    """!
    @brief Program_Interpreter.py's own pure do/if engine -- direct, bare-dict tests, no
        EventBus/DMCore needed.
    """

    def setUp(self):
        self.event_bus = ValidatingEventBus()
        self.rules = {"attitude_event": [
            {"name": "intimidated", "disposition": -10, "threat": -25, "familiarity": -5},
        ]}
        self.entities = {
            "hero": {"name": "hero", "max_hp": 20, "hp": 20},
            "victim": {"name": "victim", "max_hp": 20, "hp": 20, "attitudes": {"default": [0, 0, 0]}},
        }

    def test_condition_op_applies_a_condition_to_the_resolved_role(self):
        run_program(
            {"do": "condition", "entity": "target", "name": "prone", "duration": "scene"},
            {"actor": "hero", "target": "victim"}, self.entities, self.rules, self.event_bus,
        )
        self.assertIn("prone", self.entities["victim"]["active_conditions"])

    def test_dismiss_condition_op_removes_an_active_condition(self):
        self.entities["victim"]["active_conditions"] = {"prone": {"duration": "scene", "dismiss": None}}
        run_program(
            {"do": "dismiss_condition", "entity": "target", "name": "prone"},
            {"actor": "hero", "target": "victim"}, self.entities, self.rules, self.event_bus,
        )
        self.assertNotIn("prone", self.entities["victim"]["active_conditions"])

    def test_attitude_op_nudges_every_axis_by_the_resolved_event(self):
        run_program(
            {"do": "attitude", "entity": "target", "toward": "actor", "event": "intimidated", "magnitude": 1.0},
            {"actor": "hero", "target": "victim"}, self.entities, self.rules, self.event_bus,
        )
        self.assertEqual(self.entities["victim"]["action_attitude_deltas"]["hero"], [-10, -25, -5])

    def test_a_step_list_runs_every_step_in_order(self):
        program = [
            {"do": "condition", "entity": "target", "name": "prone", "duration": "scene"},
            {"do": "condition", "entity": "target", "name": "shaken", "duration": "scene"},
        ]
        run_program(program, {"actor": "hero", "target": "victim"}, self.entities, self.rules, self.event_bus)
        self.assertIn("prone", self.entities["victim"]["active_conditions"])
        self.assertIn("shaken", self.entities["victim"]["active_conditions"])

    def test_if_then_only_runs_when_the_condition_holds(self):
        self.entities["victim"]["hp"] = 5  # 25% of max_hp -- < 0.5
        run_program(
            {
                "if": "target.hp_per_remain < 0.5",
                "then": {"do": "condition", "entity": "target", "name": "prone", "duration": "scene"},
            },
            {"actor": "hero", "target": "victim"}, self.entities, self.rules, self.event_bus,
        )
        self.assertIn("prone", self.entities["victim"]["active_conditions"])

    def test_if_else_runs_when_the_condition_fails(self):
        run_program(
            {
                "if": "target.hp_per_remain < 0.5",  # false -- full hp
                "then": {"do": "condition", "entity": "target", "name": "prone", "duration": "scene"},
                "else": {"do": "condition", "entity": "target", "name": "shaken", "duration": "scene"},
            },
            {"actor": "hero", "target": "victim"}, self.entities, self.rules, self.event_bus,
        )
        self.assertNotIn("prone", self.entities["victim"].get("active_conditions", {}))
        self.assertIn("shaken", self.entities["victim"]["active_conditions"])

    def test_all_requires_every_sub_condition(self):
        condition = {"all": ["target.hp_per_remain <= 1.0", "target.has_condition:prone == false"]}
        self.assertTrue(evaluate_condition(condition, {"actor": "hero", "target": "victim"}, self.entities))
        self.entities["victim"]["active_conditions"] = {"prone": {}}
        self.assertFalse(evaluate_condition(condition, {"actor": "hero", "target": "victim"}, self.entities))

    def test_any_matches_on_a_single_sub_condition(self):
        condition = {"any": ["target.has_condition:prone == true", "target.hp_per_remain <= 1.0"]}
        self.assertTrue(evaluate_condition(condition, {"actor": "hero", "target": "victim"}, self.entities))

    def test_none_matches_when_no_sub_condition_holds(self):
        condition = {"none": ["target.has_condition:prone == true"]}
        self.assertTrue(evaluate_condition(condition, {"actor": "hero", "target": "victim"}, self.entities))

    def test_missing_role_in_ctx_is_a_quiet_no_op_not_an_error(self):
        run_program(
            {"do": "condition", "entity": "target", "name": "prone", "duration": "scene"},
            {"actor": "hero"}, self.entities, self.rules, self.event_bus,
        )  # no "target" in ctx -- must not raise, and must change nothing

    def test_unknown_op_raises(self):
        with self.assertRaises(ValueError):
            run_program(
                {"do": "not_a_real_op"}, {"actor": "hero", "target": "victim"},
                self.entities, self.rules, self.event_bus,
            )

    def test_step_missing_a_required_arg_raises(self):
        with self.assertRaises(ValueError):
            run_program(
                {"do": "condition", "entity": "target"}, {"actor": "hero", "target": "victim"},
                self.entities, self.rules, self.event_bus,
            )

    def test_a_literal_entity_name_instead_of_a_role_token_raises(self):
        with self.assertRaises(ValueError):
            run_program(
                {"do": "condition", "entity": "victim", "name": "prone"}, {"actor": "hero", "target": "victim"},
                self.entities, self.rules, self.event_bus,
            )

    def test_malformed_condition_string_raises(self):
        with self.assertRaises(ValueError):
            run_program(
                {"if": "not a real expression", "then": {"do": "dismiss_condition", "entity": "target", "name": "prone"}},
                {"actor": "hero", "target": "victim"}, self.entities, self.rules, self.event_bus,
            )

    def test_damage_op_deals_real_damage_through_calculate_damage(self):
        with patch("random.randint", return_value=3):
            run_program(
                {"do": "damage", "entity": "target", "dice": 2, "pips": 0, "bonus": 0, "tags": ["fire"]},
                {"actor": "hero", "target": "victim"}, self.entities, self.rules, self.event_bus,
            )
        self.assertEqual(self.entities["victim"]["hp"], 14)  # 20 - (2 * 3)

    def test_heal_op_restores_hp(self):
        self.entities["victim"]["hp"] = 10
        with patch("random.randint", return_value=3):
            run_program(
                {"do": "heal", "entity": "target", "dice": 2, "pips": 0, "bonus": 0},
                {"actor": "hero", "target": "victim"}, self.entities, self.rules, self.event_bus,
            )
        self.assertEqual(self.entities["victim"]["hp"], 16)  # 10 + (2 * 3)


class TestUniversalAbilities(DMTestCase):
    """!
    @brief Universal (untrained) abilities -- maneuvers.toml's trip/sunder/disarm (listed under
        athletics' own "abilities" field) and intimidate (under intimidation's), plus
        resolve_named_ability's own skill-list fallback (Combat_Actions.py).
    """

    def test_athletics_lists_its_own_cmb_style_maneuvers(self):
        self.assertEqual(
            set(self.dm_core.skills["athletics"]["abilities"]),
            {"trip", "sunder", "bull rush", "grapple", "pin", "disarm"},
        )

    def test_trickery_lists_its_own_maneuvers(self):
        self.assertEqual(set(self.dm_core.skills["trickery"]["abilities"]), {"dirty trick", "feint"})

    def test_sunder_is_reachable_from_every_melee_weapon_skill(self):
        for skill_name in ("athletics", "blades", "axes", "polearms", "brawling"):
            self.assertIn("sunder", self.dm_core.skills[skill_name].get("abilities", []))

    def test_universal_abilities_set_is_built_at_load_time(self):
        self.assertEqual(
            self.dm_core.universal_abilities,
            {
                "trip", "sunder", "disarm", "bull rush", "grapple", "pin", "intimidate",
                "dirty trick", "feint", "escape artist", "sleight of hand", "treat wounds", "charm",
                "don a disguise", "drop the disguise",
            },
        )

    def test_resolve_named_ability_finds_a_universal_ability_gladstone_doesnt_own(self):
        owned_names = {
            a if isinstance(a, str) else a.get("name") for a in self.dm_core.entities["gladstone"].get("abilities", [])
        }
        self.assertNotIn("trip", owned_names)

        ability = Combat_Actions.resolve_named_ability(self.dm_core.world, "gladstone", "trip")

        self.assertIsNotNone(ability)
        self.assertEqual(ability["name"], "trip")

    def test_resolve_named_ability_still_prefers_an_owned_ability_over_a_universal_one(self):
        # gladstone's own "punch" is an owned innate ability -- not universal at all -- confirms
        # the ownership check still runs first (unaffected by the universal fallback).
        ability = Combat_Actions.resolve_named_ability(self.dm_core.world, "gladstone", "punch")
        self.assertIsNotNone(ability)

    def test_resolve_named_ability_returns_none_for_a_name_matching_nothing(self):
        self.assertIsNone(Combat_Actions.resolve_named_ability(self.dm_core.world, "gladstone", "not_a_real_ability_name"))

    def test_a_universal_ability_defaults_to_melee_range(self):
        # trip/sunder each write range = 0 explicitly; is_in_range's own unconditional
        # default is unchanged either way.
        for name in ("trip", "sunder"):
            self.assertEqual(self.dm_core.entities[name].get("range", 0), 0)


class TestCombatTrickAndMetamagicModifiers(DMTestCase):
    """!
    @brief modifiers.toml's "power attack"/"empowered" -- the trained (never universal)
        counterpart to TestUniversalAbilities above: a supertype == "modifier" [[entity]]
        stacked onto another named ability at cast time via "applies_to"/"skill_divisor"/
        "damage_bonus"/"damage_multiplier" (see entity_schema.toml's own field reference and
        Rules/Pathfinder/reference/pathfinder_mapping.toml's Metamagic row).
    """

    def setUp(self):
        super().setUp()
        self.resolved = self._capture("round_resolved")

    # --- resolve_action/resolve_opposed_action: skill_divisor -------------------------------

    def test_skill_divisor_divides_the_base_skill_rating_before_other_modifiers_stack(self):
        # gladstone's blades is 5D+0 (rating 15) -- power attack's skill_divisor = 2 halves that
        # rating to 7.5, floored back to {dice, pips} via the same dice*3+pips scale (2D+1).
        with patch("random.randint", return_value=3):
            full = Combat_Resolution.resolve_action(self.dm_core.world, "gladstone", "blades")
            halved = Combat_Resolution.resolve_action(self.dm_core.world, "gladstone", "blades", skill_divisor=2)

        self.assertEqual(full["roll"], 15)   # 5D @ 3 = 15
        self.assertEqual(halved["roll"], 7)  # 2D+1 @ 3 = 7

    def test_skill_divisor_of_one_is_a_no_op(self):
        with patch("random.randint", return_value=3):
            result = Combat_Resolution.resolve_action(self.dm_core.world, "gladstone", "blades", skill_divisor=1)
        self.assertEqual(result["roll"], 15)

    def test_resolve_opposed_action_skill_divisor_never_touches_the_defenders_roll(self):
        self.dm_core.entities["test_defender"] = {
            "name": "test_defender", "skills": {"dodge": {"dice": 6, "pips": 0}},
        }
        with patch("random.randint", return_value=3):
            unmodified = Combat_Resolution.resolve_opposed_action(self.dm_core.world, "gladstone", "blades", "test_defender")
            halved = Combat_Resolution.resolve_opposed_action(self.dm_core.world, 
                "gladstone", "blades", "test_defender", skill_divisor=2,
            )

        self.assertEqual(unmodified["difficulty"], 18)  # defender's own 6D @ 3, unaffected
        self.assertEqual(halved["difficulty"], 18)
        self.assertEqual(unmodified["roll"], 15)
        self.assertEqual(halved["roll"], 7)

    # --- Ownership: trained, never universal -------------------------------------------------

    def test_power_attack_and_empowered_are_owned_by_gladstone_not_universal(self):
        self.assertNotIn("power attack", self.dm_core.universal_abilities)
        self.assertNotIn("empowered", self.dm_core.universal_abilities)
        self.assertIsNotNone(Combat_Actions.resolve_named_ability(self.dm_core.world, "gladstone", "power attack"))
        self.assertIsNotNone(Combat_Actions.resolve_named_ability(self.dm_core.world, "gladstone", "empowered"))

    def test_an_entity_that_never_trained_it_cannot_resolve_it_at_all(self):
        # wolf owns no abilities list entry named "power attack", and it's not universal either
        # (unlike "trip"/"cleave") -- resolve_named_ability must come back empty, the same
        # "untrained" outcome an unowned, non-universal named ability always has.
        self.assertIsNone(Combat_Actions.resolve_named_ability(self.dm_core.world, "wolf", "power attack"))

    # --- _resolve_action_modifier: the clause-level lookup _on_turn_detected uses -----------

    def test_resolve_action_modifier_returns_none_for_no_modifier_name(self):
        self.assertIsNone(self.dm_core._resolve_action_modifier(None))

    def test_resolve_action_modifier_returns_none_for_an_untrained_name(self):
        self.dm_core.player_name = "wolf"
        self.assertIsNone(self.dm_core._resolve_action_modifier("power attack"))

    # --- _apply_ability_modifier: the per-cast copy, applies_to gating ----------------------

    def test_applies_to_mismatch_leaves_the_ability_and_skill_divisor_untouched(self):
        # "empowered" only applies_to supertypes = ["spell"] -- aimed at a weapon strike
        # (longsword, supertype "object"/subtype "weapon"), it should never actually fire.
        longsword = self.dm_core.entities["longsword"]
        empowered = Combat_Actions.resolve_named_ability(self.dm_core.world, "gladstone", "empowered")

        self.assertFalse(Combat_Resolution.matches_supertype_or_subtype(longsword, empowered["applies_to"]))

    def test_apply_ability_modifier_never_mutates_the_shared_entity(self):
        longsword = self.dm_core.entities["longsword"]
        original_damage_value = dict(longsword["damage_value"])
        power_attack = Combat_Actions.resolve_named_ability(self.dm_core.world, "gladstone", "power attack")

        modified = self.dm_core._apply_ability_modifier(longsword, power_attack)

        self.assertEqual(longsword["damage_value"], original_damage_value)
        self.assertNotEqual(modified["damage_value"]["dice"], original_damage_value["dice"])
        self.assertIsNot(modified, longsword)

    def test_apply_ability_modifier_adds_damage_bonus_dice(self):
        longsword = self.dm_core.entities["longsword"]
        power_attack = Combat_Actions.resolve_named_ability(self.dm_core.world, "gladstone", "power attack")

        modified = self.dm_core._apply_ability_modifier(longsword, power_attack)

        self.assertEqual(modified["damage_value"]["dice"], longsword["damage_value"]["dice"] + 2)
        self.assertEqual(modified["damage_value"]["pips"], longsword["damage_value"]["pips"])

    def test_apply_ability_modifier_applies_damage_multiplier(self):
        fireball = self.dm_core.entities["fireball"]
        empowered = Combat_Actions.resolve_named_ability(self.dm_core.world, "gladstone", "empowered")

        modified = self.dm_core._apply_ability_modifier(fireball, empowered)

        self.assertEqual(modified["damage_value"]["dice"], fireball["damage_value"]["dice"] * 1.5)

    def test_apply_ability_modifier_is_a_no_op_on_an_ability_with_no_damage_value(self):
        # "dispel magic" deals no damage at all -- neither damage_bonus nor damage_multiplier
        # has anything to act on, so the copy comes back with no "damage_value" key either,
        # same "wrong shape wastes it" precedent a mismatched applies_to already has.
        dispel_magic = self.dm_core.entities["dispel magic"]
        empowered = Combat_Actions.resolve_named_ability(self.dm_core.world, "gladstone", "empowered")

        modified = self.dm_core._apply_ability_modifier(dispel_magic, empowered)

        self.assertNotIn("damage_value", modified)

    # --- End-to-end through _on_turn_detected -------------------------------------------------

    def test_power_attack_costs_accuracy_and_pays_off_in_damage_end_to_end(self):
        # A no-skills target auto-succeeds (difficulty 0) regardless of skill_divisor, isolating
        # the damage-bonus half from the accuracy-cost half (already covered above in isolation).
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 40, "skills": {}}
        self._load_ad_hoc_scenario([{"name": "practice_dummy", "band": 1}])

        with patch("random.randint", return_value=3):
            self.dm_core._on_turn_detected({
                "clauses": [{"kind": "action", "skill": "blades"}],
                "input": "I attack the practice dummy",
            })
        baseline_damage = [e for e in self.resolved[-1]["actions"][0].effects if isinstance(e, DamageEffect)][0]

        self.dm_core.entities["practice_dummy_2"] = {"name": "practice_dummy_2", "max_hp": 40, "skills": {}}
        self._load_ad_hoc_scenario([{"name": "practice_dummy_2", "band": 1}])
        with patch("random.randint", return_value=3):
            self.dm_core._on_turn_detected({
                "clauses": [{"kind": "action", "skill": "blades", "modifier": "power attack"}],
                "input": "I power attack the practice dummy",
            })
        power_attack_damage = [e for e in self.resolved[-1]["actions"][0].effects if isinstance(e, DamageEffect)][0]

        # +2 extra damage dice, each stubbed to roll 3 -- the flat, deterministic delta
        # power attack's own damage_bonus should add on top of whatever the unmodified swing
        # already dealt (attacker/strength_damage bonus resolution is identical either way, so
        # it cancels out of the comparison).
        self.assertEqual(power_attack_damage.net_damage - baseline_damage.net_damage, 6)
        # The shared longsword entity itself is never mutated by any of this.
        self.assertEqual(self.dm_core.entities["longsword"]["damage_value"], {"dice": 1, "pips": 2, "bonus": "strength_damage"})

    def test_modifier_aimed_at_the_wrong_shape_of_ability_just_wastes_it(self):
        # "empowered" only applies_to spells -- naming it alongside a plain weapon swing should
        # resolve as an ordinary, unmodified attack (no 1.5x damage_multiplier applied) rather
        # than erroring or silently applying anyway.
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 40, "skills": {}}
        self._load_ad_hoc_scenario([{"name": "practice_dummy", "band": 1}])
        with patch("random.randint", return_value=3):
            self.dm_core._on_turn_detected({
                "clauses": [{"kind": "action", "skill": "blades"}],
                "input": "I attack the practice dummy",
            })
        baseline_damage = [e for e in self.resolved[-1]["actions"][0].effects if isinstance(e, DamageEffect)][0]

        self.dm_core.entities["practice_dummy_2"] = {"name": "practice_dummy_2", "max_hp": 40, "skills": {}}
        self._load_ad_hoc_scenario([{"name": "practice_dummy_2", "band": 1}])
        with patch("random.randint", return_value=3):
            self.dm_core._on_turn_detected({
                "clauses": [{"kind": "action", "skill": "blades", "modifier": "empowered"}],
                "input": "I empowered attack the practice dummy",
            })
        mismatched_modifier_damage = [e for e in self.resolved[-1]["actions"][0].effects if isinstance(e, DamageEffect)][0]

        self.assertEqual(mismatched_modifier_damage.net_damage, baseline_damage.net_damage)
        self.assertEqual(self.dm_core.entities["longsword"]["damage_value"], {"dice": 1, "pips": 2, "bonus": "strength_damage"})


class TestAbilityOutcomeProgram(DMTestCase):
    """!
    @brief DM_Core.py's own _run_ability_outcome_program -- the attachment point that runs a
        resolved ability's own on_pass/on_fail once a real roll happens. Exercised directly against
        a constructed RolledOutcome rather than a full _on_turn_detected pass, so these stay
        deterministic without depending on wolf's own (nonexistent) opposing skill/dice rolls.
    """

    def setUp(self):
        super().setUp()
        self.dm_core.entities["target_dummy"] = {
            "name": "target_dummy", "max_hp": 20, "hp": 20, "attitudes": {"default": [0, 0, 0]},
        }

    def test_trip_on_pass_applies_prone_to_the_target(self):
        trip = self.dm_core.entities["trip"]
        result = RolledOutcome(entity="gladstone", skill="athletics", roll=15, difficulty=5, success=True)

        apply_effects(self.dm_core, result, "athletics", None, trip, "target_dummy", via_test=False)

        self.assertIn("prone", self.dm_core.entities["target_dummy"]["active_conditions"])

    def test_trip_on_fail_does_nothing_since_no_on_fail_is_authored(self):
        trip = self.dm_core.entities["trip"]
        result = RolledOutcome(entity="gladstone", skill="athletics", roll=1, difficulty=15, success=False)

        apply_effects(self.dm_core, result, "athletics", None, trip, "target_dummy", via_test=False)

        self.assertNotIn("prone", self.dm_core.entities["target_dummy"].get("active_conditions", {}))

    def test_intimidate_on_pass_applies_shaken_once_threat_is_already_past_the_threshold(self):
        # intimidate's own step 2 ("if target.threat < -50") reads target_dummy's live attitude
        # -- set low enough here on its own template default that the conditional fires
        # regardless of step 1's own nudge (see the next test for why step 1 doesn't move it).
        self.dm_core.entities["target_dummy"]["attitudes"]["default"] = [0, -60, 0]
        intimidate = self.dm_core.entities["intimidate"]
        result = RolledOutcome(entity="gladstone", skill="intimidation", roll=15, difficulty=5, success=True)

        apply_effects(self.dm_core, result, "intimidation", None, intimidate, "target_dummy", via_test=False)

        self.assertIn("shaken", self.dm_core.entities["target_dummy"]["active_conditions"])

    def test_intimidate_on_pass_step_one_is_a_no_op_since_roll_margin_is_not_yet_a_real_field(self):
        # Step 1's own magnitude ("actor.roll_margin") is a still-open normalization question --
        # "roll_margin" resolves to None (no such field on any entity), so nudge_attitude_from_event's
        # own falsy-magnitude no-op applies. Documented here as current, honest behavior rather
        # than silently assumed to work.
        self.dm_core.entities["target_dummy"]["attitudes"]["default"] = [0, -60, 0]
        intimidate = self.dm_core.entities["intimidate"]
        result = RolledOutcome(entity="gladstone", skill="intimidation", roll=15, difficulty=5, success=True)

        apply_effects(self.dm_core, result, "intimidation", None, intimidate, "target_dummy", via_test=False)

        self.assertNotIn("action_attitude_deltas", self.dm_core.entities["target_dummy"])

    def test_intimidate_on_pass_skips_shaken_when_still_above_the_threshold(self):
        intimidate = self.dm_core.entities["intimidate"]
        result = RolledOutcome(entity="gladstone", skill="intimidation", roll=15, difficulty=5, success=True)

        apply_effects(self.dm_core, result, "intimidation", None, intimidate, "target_dummy", via_test=False)

        self.assertNotIn("shaken", self.dm_core.entities["target_dummy"].get("active_conditions", {}))

    def test_intimidate_on_fail_nudges_attitude_via_failed_intimidation(self):
        intimidate = self.dm_core.entities["intimidate"]
        result = RolledOutcome(entity="gladstone", skill="intimidation", roll=2, difficulty=15, success=False)

        apply_effects(self.dm_core, result, "intimidation", None, intimidate, "target_dummy", via_test=False)

        deltas = self.dm_core.entities["target_dummy"]["action_attitude_deltas"]["gladstone"]
        self.assertEqual(deltas, [-1.5, 3.0, -1.5])  # failed_intimidation's own deltas @ magnitude 0.3

    def test_never_fires_for_a_via_test_roll(self):
        trip = self.dm_core.entities["trip"]
        result = RolledOutcome(entity="gladstone", skill="athletics", roll=15, difficulty=5, success=True)

        apply_effects(self.dm_core, result, "athletics", None, trip, "target_dummy", via_test=True)

        self.assertNotIn("prone", self.dm_core.entities["target_dummy"].get("active_conditions", {}))

    def test_bull_rush_on_pass_applies_staggered(self):
        bull_rush = self.dm_core.entities["bull rush"]
        result = RolledOutcome(entity="gladstone", skill="athletics", roll=15, difficulty=5, success=True)

        apply_effects(self.dm_core, result, "athletics", None, bull_rush, "target_dummy", via_test=False)

        self.assertIn("staggered", self.dm_core.entities["target_dummy"]["active_conditions"])

    def test_grapple_on_pass_applies_grappled(self):
        grapple = self.dm_core.entities["grapple"]
        result = RolledOutcome(entity="gladstone", skill="athletics", roll=15, difficulty=5, success=True)

        apply_effects(self.dm_core, result, "athletics", None, grapple, "target_dummy", via_test=False)

        self.assertIn("grappled", self.dm_core.entities["target_dummy"]["active_conditions"])

    def test_dirty_trick_on_pass_applies_dazzled(self):
        dirty_trick = self.dm_core.entities["dirty trick"]
        result = RolledOutcome(entity="gladstone", skill="trickery", roll=15, difficulty=5, success=True)

        apply_effects(self.dm_core, result, "trickery", None, dirty_trick, "target_dummy", via_test=False)

        self.assertIn("dazzled", self.dm_core.entities["target_dummy"]["active_conditions"])

    def test_feint_on_pass_applies_flat_footed(self):
        feint = self.dm_core.entities["feint"]
        result = RolledOutcome(entity="gladstone", skill="trickery", roll=15, difficulty=5, success=True)

        apply_effects(self.dm_core, result, "trickery", None, feint, "target_dummy", via_test=False)

        self.assertIn("flat_footed", self.dm_core.entities["target_dummy"]["active_conditions"])

    def test_none_of_the_new_maneuvers_fire_on_a_failed_roll(self):
        for name, skill_name, condition_name in (
            ("bull rush", "athletics", "staggered"), ("grapple", "athletics", "grappled"),
            ("dirty trick", "trickery", "dazzled"), ("feint", "trickery", "flat_footed"),
        ):
            ability = self.dm_core.entities[name]
            result = RolledOutcome(entity="gladstone", skill=skill_name, roll=1, difficulty=15, success=False)

            apply_effects(self.dm_core, result, skill_name, None, ability, "target_dummy", via_test=False)

            self.assertNotIn(condition_name, self.dm_core.entities["target_dummy"].get("active_conditions", {}))

    # --- inject_directive / suggestion --------------------------------------------------

    def test_suggestion_on_pass_plants_the_raw_turn_text_as_a_directive(self):
        # spells.toml's own "suggestion" omits a literal "text" on purpose -- see its own
        # comment -- so the op falls back to ctx["input"], threaded in here as input_text.
        suggestion = self.dm_core.entities["suggestion"]
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=15, difficulty=5, success=True)

        apply_effects(self.dm_core, 
            result, "arcane", None, suggestion, "target_dummy", via_test=False,
            input_text="tell him to open the gate",
        )

        self.assertEqual(
            self.dm_core.entities["target_dummy"]["prompt_directive"],
            {"text": "tell him to open the gate", "source": "gladstone", "expires_in_blocks": 1},
        )

    def test_suggestion_on_a_failed_roll_plants_nothing(self):
        suggestion = self.dm_core.entities["suggestion"]
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=1, difficulty=15, success=False)

        apply_effects(self.dm_core, 
            result, "arcane", None, suggestion, "target_dummy", via_test=False,
            input_text="tell him to open the gate",
        )

        self.assertNotIn("prompt_directive", self.dm_core.entities["target_dummy"])

    def test_inject_directive_with_no_input_text_and_no_literal_text_is_a_no_op(self):
        suggestion = self.dm_core.entities["suggestion"]
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=15, difficulty=5, success=True)

        apply_effects(self.dm_core, result, "arcane", None, suggestion, "target_dummy", via_test=False)

        self.assertNotIn("prompt_directive", self.dm_core.entities["target_dummy"])

    def test_inject_directive_literal_text_wins_over_ctx_input(self):
        scripted = {
            "skill": "arcane", "on_pass": {"do": "inject_directive", "entity": "target", "text": "a scripted directive"},
        }
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=15, difficulty=5, success=True)

        apply_effects(self.dm_core, 
            result, "arcane", None, scripted, "target_dummy", via_test=False, input_text="whatever the player typed",
        )

        self.assertEqual(
            self.dm_core.entities["target_dummy"]["prompt_directive"],
            {"text": "a scripted directive", "source": "gladstone"},
        )

    def test_inject_directive_no_ops_against_an_inanimate_object(self):
        self.dm_core.entities["crate"] = {"name": "crate", "max_hp": 10, "hp": 10, "supertype": "object"}
        suggestion = self.dm_core.entities["suggestion"]
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=15, difficulty=5, success=True)

        apply_effects(self.dm_core, 
            result, "arcane", None, suggestion, "crate", via_test=False, input_text="open yourself",
        )

        self.assertNotIn("prompt_directive", self.dm_core.entities["crate"])

    def test_inject_directive_no_ops_against_a_dead_entity(self):
        self.dm_core.entities["target_dummy"]["hp"] = 0
        suggestion = self.dm_core.entities["suggestion"]
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=15, difficulty=5, success=True)

        apply_effects(self.dm_core, 
            result, "arcane", None, suggestion, "target_dummy", via_test=False, input_text="get up",
        )

        self.assertNotIn("prompt_directive", self.dm_core.entities["target_dummy"])


class TestPromptDirective(DMTestCase):
    """!
    @brief DM_Social.py's describe_character surfacing a planted prompt_directive (see
        Social_Resolution.py's set_prompt_directive and TestAbilityOutcomeProgram's own
        inject_directive tests for how one actually gets planted) into narration prompts, plus
        its save/load round-trip (DM_Persistence.py).
    """

    def setUp(self):
        super().setUp()
        self.dm_core.entities["target_dummy"] = {
            "name": "target_dummy", "max_hp": 20, "hp": 20, "description": "A plain townsperson.",
        }

    def test_describe_character_appends_the_directive_when_present(self):
        self.dm_core.entities["target_dummy"]["prompt_directive"] = {
            "text": "open the gate", "source": "gladstone",
        }
        description = self.dm_core.describe_character("target_dummy")
        self.assertIn("Currently privately convinced (planted by gladstone): \"open the gate\"", description)

    def test_describe_character_carries_voice_beside_known_lines(self):
        self.dm_core.entities["target_dummy"]["voice"] = "clipped, dockside slang"
        self.dm_core.entities["target_dummy"]["quotes"] = ["Mind the ropes."]
        description = self.dm_core.describe_character("target_dummy")
        self.assertIn("Voice: clipped, dockside slang", description)
        self.assertIn('Lines in their voice: "Mind the ropes."', description)

    def test_describe_character_omits_anything_when_no_directive_is_planted(self):
        description = self.dm_core.describe_character("target_dummy")
        self.assertNotIn("Currently privately convinced", description)

    def test_describe_character_falls_back_to_someone_when_source_is_unknown(self):
        self.dm_core.entities["target_dummy"]["prompt_directive"] = {"text": "flee", "source": None}
        description = self.dm_core.describe_character("target_dummy")
        self.assertIn("planted by someone", description)

    def test_prompt_directive_round_trips_through_save_and_load(self):
        # A real, scenario-instanced entity, not the synthetic target_dummy above -- save_game's
        # own _all_known_instance_names walks location_runtime's own persistent_names, so an
        # entity added straight to self.entities with no location ever instancing it (like
        # target_dummy here) wouldn't actually be in the save file at all.
        slot_name = "test_prompt_directive_slot"
        self.addCleanup(shutil.rmtree, self.dm_core._save_slot_dir(slot_name), ignore_errors=True)
        player_name = self.dm_core.player_name
        self.dm_core.entities[player_name]["prompt_directive"] = {
            "text": "open the gate", "source": "an unseen voice",
        }

        self.dm_core.save_game(slot_name)
        self.dm_core.load_game(slot_name)

        self.assertEqual(
            self.dm_core.entities[player_name]["prompt_directive"],
            {"text": "open the gate", "source": "an unseen voice"},
        )

    def test_a_duration_less_directive_never_expires_no_matter_how_many_blocks_pass(self):
        self.dm_core.entities["target_dummy"]["prompt_directive"] = {
            "text": "open the gate", "source": "gladstone",
        }
        self.dm_core.advance_blocks(100)
        self.assertEqual(
            self.dm_core.entities["target_dummy"]["prompt_directive"]["text"], "open the gate",
        )

    def test_a_timed_directive_survives_until_its_own_block_countdown_runs_out(self):
        Social_Resolution.set_prompt_directive(
            self.dm_core.entities, "target_dummy", "open the gate", "gladstone", duration_blocks=2,
        )
        self.dm_core.advance_blocks(1)
        self.assertIsNotNone(self.dm_core.entities["target_dummy"]["prompt_directive"])
        self.dm_core.advance_blocks(1)
        self.assertIsNone(self.dm_core.entities["target_dummy"]["prompt_directive"])

    def test_a_timed_directive_expires_in_one_bulk_advance_past_its_own_countdown(self):
        Social_Resolution.set_prompt_directive(
            self.dm_core.entities, "target_dummy", "open the gate", "gladstone", duration_blocks=2,
        )
        self.dm_core.advance_blocks(5)
        self.assertIsNone(self.dm_core.entities["target_dummy"]["prompt_directive"])

    def test_inject_directive_op_forwards_duration_into_the_planted_directive(self):
        run_program(
            {"do": "inject_directive", "entity": "target", "text": "flee", "duration": 3},
            {"actor": "gladstone", "target": "target_dummy"},
            self.dm_core.entities, self.dm_core.rules, self.dm_core.event_bus,
        )
        self.assertEqual(
            self.dm_core.entities["target_dummy"]["prompt_directive"]["expires_in_blocks"], 3,
        )


class TestMorePathfinderManeuvers(DMTestCase):
    """!
    @brief The second wave of Pathfinder-inspired universal abilities: sunder's own object-vs-
        creature branch, pin (grapple-gated), escape artist (self-targeting), sleight of hand
        (the first real transfer_currency op caller -- Inventory_Resolution.py), treat wounds,
        and charm (the positive mirror of intimidate, with real, non-"roll_margin" magnitudes).
    """

    def setUp(self):
        super().setUp()
        self.dm_core.entities["target_dummy"] = {
            "name": "target_dummy", "max_hp": 20, "hp": 20, "attitudes": {"default": [0, 0, 0]},
            "supertype": "creature", "currency": 40,
        }
        self.dm_core.entities["crate"] = {"name": "crate", "max_hp": 10, "hp": 10, "supertype": "object"}

    def _run(self, ability_name, skill_name, target_name, success=True, actor="gladstone"):
        ability = self.dm_core.entities[ability_name]
        result = RolledOutcome(
            entity=actor, skill=skill_name, roll=15 if success else 1, difficulty=5 if success else 15,
            success=success,
        )
        apply_effects(self.dm_core, result, skill_name, None, ability, target_name, via_test=False)
        return result

    def test_sunder_condition_disarms_a_creatures_weapon(self):
        self._run("sunder", "blades", "target_dummy")
        self.assertIn("sundered_weapon", self.dm_core.entities["target_dummy"]["active_conditions"])

    def test_sunder_deals_real_damage_to_an_object(self):
        with patch("random.randint", return_value=3):
            self._run("sunder", "blades", "crate")
        self.assertLess(self.dm_core.entities["crate"]["hp"], 10)

    def test_sunder_is_rollable_via_any_melee_weapon_skill(self):
        for skill_name in ("athletics", "blades", "axes", "polearms", "brawling"):
            self.dm_core.entities["target_dummy"]["active_conditions"] = {}
            self._run("sunder", skill_name, "target_dummy")
            self.assertIn("sundered_weapon", self.dm_core.entities["target_dummy"]["active_conditions"])

    def test_pin_only_lands_on_an_already_grappled_target(self):
        self.dm_core.entities["target_dummy"]["active_conditions"] = {"grappled": {"duration": "scene", "dismiss": None}}
        self._run("pin", "athletics", "target_dummy")
        self.assertIn("pinned", self.dm_core.entities["target_dummy"]["active_conditions"])

    def test_pin_is_a_no_op_without_grappled_first(self):
        self._run("pin", "athletics", "target_dummy")
        self.assertNotIn("pinned", self.dm_core.entities["target_dummy"].get("active_conditions", {}))

    def test_escape_artist_dismisses_the_actors_own_grappled_condition(self):
        self.dm_core.entities["gladstone"].setdefault("active_conditions", {})["grappled"] = {
            "duration": "scene", "dismiss": None,
        }
        self._run("escape artist", "escape", "target_dummy")
        self.assertNotIn("grappled", self.dm_core.entities["gladstone"]["active_conditions"])

    def test_sleight_of_hand_steals_all_the_targets_currency(self):
        self.dm_core.entities["gladstone"]["currency"] = 0
        self._run("sleight of hand", "finesse", "target_dummy")
        self.assertEqual(self.dm_core.entities["target_dummy"]["currency"], 0)
        self.assertEqual(self.dm_core.entities["gladstone"]["currency"], 40)

    def test_sleight_of_hand_nudges_the_victims_attitude_via_theft(self):
        self._run("sleight of hand", "finesse", "target_dummy")
        deltas = self.dm_core.entities["target_dummy"]["action_attitude_deltas"]["gladstone"]
        self.assertEqual(deltas, [-7.5, 0, -6.0])  # theft {-15, 0, -12} @ magnitude 0.5

    def test_treat_wounds_heals_the_target(self):
        self.dm_core.entities["target_dummy"]["hp"] = 5
        with patch("random.randint", return_value=3):
            self._run("treat wounds", "medicine", "target_dummy")
        self.assertEqual(self.dm_core.entities["target_dummy"]["hp"], 14)  # 5 + (3 * 3)

    def test_charm_on_pass_nudges_attitude_positively(self):
        self._run("charm", "charisma", "target_dummy", success=True)
        deltas = self.dm_core.entities["target_dummy"]["action_attitude_deltas"]["gladstone"]
        self.assertEqual(deltas, [9.0, 3.0, 6.0])  # charmed {15, 5, 10} @ magnitude 0.6

    def test_charm_on_fail_only_mildly_dents_attitude(self):
        self._run("charm", "charisma", "target_dummy", success=False)
        deltas = self.dm_core.entities["target_dummy"]["action_attitude_deltas"]["gladstone"]
        self.assertAlmostEqual(deltas[0], -0.6)  # failed_charm disposition -3 @ magnitude 0.2


class TestEntityTestOutcomeProgram(DMTestCase):
    """!
    @brief DM_Core.py's own _run_test_outcome_program -- [entity.test]'s own on_pass/on_fail,
        sibling to its existing flat pass/fail tables. No shipped [entity.test]
        authors on_pass/on_fail yet, so this exercises the wiring directly against a synthetic
        test table.
    """

    def test_on_pass_runs_when_the_test_succeeds(self):
        self.dm_core.entities["chest_dummy"] = {"name": "chest_dummy", "max_hp": 1, "hp": 1}
        test = {
            "difficulty": 5, "skill": ["finesse"],
            "on_pass": {"do": "condition", "entity": "actor", "name": "shaken", "duration": "scene"},
        }

        self.dm_core._run_test_outcome_program(test, True, "chest_dummy")

        self.assertIn("shaken", self.dm_core.entities["gladstone"]["active_conditions"])

    def test_on_fail_runs_when_the_test_fails_and_on_pass_does_not(self):
        self.dm_core.entities["chest_dummy"] = {"name": "chest_dummy", "max_hp": 1, "hp": 1}
        test = {
            "difficulty": 5, "skill": ["finesse"],
            "on_pass": {"do": "condition", "entity": "actor", "name": "shaken", "duration": "scene"},
            "on_fail": {"do": "condition", "entity": "actor", "name": "prone", "duration": "scene"},
        }

        self.dm_core._run_test_outcome_program(test, False, "chest_dummy")

        self.assertNotIn("shaken", self.dm_core.entities["gladstone"].get("active_conditions", {}))
        self.assertIn("prone", self.dm_core.entities["gladstone"]["active_conditions"])


class TestOnInteractProgram(DMTestCase):
    """!
    @brief The cursed dagger's own [entity.on_interact.equip] -- items.toml's shipped worked
        example of making a curse real, not just flavor.
    """

    def setUp(self):
        super().setUp()
        self.dm_core.entities["gladstone"]["inventory"].append("cursed dagger")

    def test_equipping_an_unidentified_cursed_dagger_curses_the_wearer(self):
        resolved = self._capture("item_interaction_resolved")

        self.dm_core._on_item_interaction_detected({"intent": "equip", "item_name": "cursed dagger", "input": "I equip the cursed dagger"})

        self.assertTrue(resolved[-1]["found"])
        self.assertIn("cursed", self.dm_core.entities["gladstone"]["active_conditions"])

    def test_equipping_an_already_identified_cursed_dagger_does_not_curse_the_wearer(self):
        Combat_Resolution.apply_condition(self.dm_core.world, "cursed dagger", "identified", duration="permanent", dismiss="")

        self.dm_core._on_item_interaction_detected({"intent": "equip", "item_name": "cursed dagger", "input": "I equip the cursed dagger"})

        self.assertNotIn("cursed", self.dm_core.entities["gladstone"]["active_conditions"])

    def test_a_denied_interaction_never_runs_the_program(self):
        # Not actually in inventory -- _resolve_equip_intent denies this as "not_present" before
        # resolved(True, ...) is ever reached, so on_interact must never fire either.
        self.dm_core.entities["gladstone"]["inventory"].remove("cursed dagger")

        self.dm_core._on_item_interaction_detected({"intent": "equip", "item_name": "cursed dagger", "input": "I equip the cursed dagger"})

        self.assertNotIn("cursed", self.dm_core.entities["gladstone"]["active_conditions"])


class TestOnDamageProgram(DMTestCase):
    """!
    @brief The troll's own [entity.on_damage] -- creatures.toml's shipped worked example of
        "A troll's temper".
    """

    def setUp(self):
        super().setUp()
        self._load_ad_hoc_scenario(
            [{"name": "gladstone", "band": 1}, {"name": "troll", "band": 1}], bands=4, enclosed=True,
        )

    def test_dropping_below_half_hp_enrages_the_troll(self):
        Combat_Resolution.apply_damage(self.dm_core.world, "troll", 21, actor_name="gladstone")  # 40 -> 19, 47.5%

        self.assertIn("enraged", self.dm_core.entities["troll"]["active_conditions"])

    def test_staying_above_half_hp_does_not_enrage_the_troll(self):
        Combat_Resolution.apply_damage(self.dm_core.world, "troll", 5, actor_name="gladstone")  # 40 -> 35

        self.assertNotIn("enraged", self.dm_core.entities["troll"].get("active_conditions", {}))

    def test_enraged_is_not_re_applied_once_already_active(self):
        Combat_Resolution.apply_damage(self.dm_core.world, "troll", 21, actor_name="gladstone")
        self.dm_core.entities["troll"]["active_conditions"]["enraged"]["duration"] = "marker"

        Combat_Resolution.apply_damage(self.dm_core.world, "troll", 1, actor_name="gladstone")

        # Still the same marker -- apply_condition would have overwritten it with a fresh
        # {"duration": "rounds", "length": 5, ...} entry if the condition step had fired again.
        self.assertEqual(self.dm_core.entities["troll"]["active_conditions"]["enraged"]["duration"], "marker")


class TestOnRoundUpkeepProgram(DMTestCase):
    """!@brief The generic [entity.on_round_upkeep] attachment point (DM_Status.py's own
        run_round_upkeep wrapper) -- no shipped entity authors this yet, so this exercises the
        wiring directly against a synthetic entity."""

    def test_runs_alongside_the_ordinary_per_round_upkeep_loop(self):
        self.dm_core.entities["ticking_dummy"] = {
            "name": "ticking_dummy", "max_hp": 10, "hp": 10,
            "on_round_upkeep": {"do": "condition", "entity": "target", "name": "shaken", "duration": "scene"},
        }
        self.dm_core.scenario_entities.append("ticking_dummy")

        self.dm_core.run_round_upkeep()

        self.assertIn("shaken", self.dm_core.entities["ticking_dummy"]["active_conditions"])

    def test_never_runs_for_a_dead_entity(self):
        self.dm_core.entities["dead_dummy"] = {
            "name": "dead_dummy", "max_hp": 10, "hp": 0,
            "on_round_upkeep": {"do": "condition", "entity": "target", "name": "shaken", "duration": "scene"},
        }
        self.dm_core.scenario_entities.append("dead_dummy")

        self.dm_core.run_round_upkeep()

        self.assertNotIn("shaken", self.dm_core.entities["dead_dummy"].get("active_conditions", {}))


class TestOnEnterProgram(DMTestCase):
    """!@brief The generic [entity.on_enter] attachment point (DM_Rules.py's own
        _enter_location) -- no shipped entity authors this yet, so this exercises the wiring
        directly against a synthetic entity."""

    def test_runs_once_the_entity_is_present_in_a_freshly_entered_location(self):
        self.dm_core.entity_templates["altar"] = {
            "name": "altar", "supertype": "object", "max_hp": 1,
            "on_enter": {"do": "condition", "entity": "target", "name": "identified", "duration": "permanent"},
        }
        self.dm_core.entities["altar"] = dict(self.dm_core.entity_templates["altar"])

        self._load_ad_hoc_scenario([{"name": "gladstone", "band": 1}, {"name": "altar", "band": 1}])

        self.assertIn("identified", self.dm_core.entities["altar"]["active_conditions"])


class TestSummoning(DMTestCase):
    """!
    @brief A spell's own "summon" field (spells.toml's "summon spectral wolf"), DM_Summoning.py's
        _summon_creature/_expire_summon_if_due, and DM_Core.py's own _apply_summon_if_hit/
        _apply_damage_if_hit gating fix.
    """

    def test_summon_creature_places_a_living_non_hostile_ally(self):
        name = self.dm_core._summon_creature({"name": "spectral wolf", "duration": 3})

        self.assertEqual(name, "spectral wolf")
        self.assertIn("spectral wolf", self.dm_core.scenario_entities)
        entity = self.dm_core.entities["spectral wolf"]
        self.assertEqual(entity["band"], Combat_Resolution.get_band(self.dm_core.world, "gladstone"))
        self.assertTrue(entity["ad_hoc"])
        self.assertEqual(entity["summon_expires_in"], 3)
        self.assertFalse(self.dm_core.is_hostile("spectral wolf", self.dm_core.player_name))

    def test_summon_creature_disambiguates_repeat_casts(self):
        first = self.dm_core._summon_creature({"name": "spectral wolf", "duration": 3})
        second = self.dm_core._summon_creature({"name": "spectral wolf", "duration": 3})

        self.assertEqual(first, "spectral wolf")
        self.assertEqual(second, "spectral wolf_2")
        self.assertIn("spectral wolf", self.dm_core.scenario_entities)
        self.assertIn("spectral wolf_2", self.dm_core.scenario_entities)

    def test_summon_creature_returns_none_for_an_unknown_template(self):
        before = list(self.dm_core.scenario_entities)
        name = self.dm_core._summon_creature({"name": "nonexistent thing", "duration": 3})

        self.assertIsNone(name)
        self.assertEqual(self.dm_core.scenario_entities, before)

    def test_expire_summon_if_due_removes_the_entity_at_zero(self):
        self.dm_core._summon_creature({"name": "spectral wolf", "duration": 1})
        self.dm_core.run_round_upkeep()
        self.assertNotIn("spectral wolf", self.dm_core.scenario_entities)
        self.assertNotIn("spectral wolf", self.dm_core.entities["spectral wolf"].get("active_conditions", {}))

    def test_run_round_upkeep_survives_an_expiry_mid_iteration(self):
        # Regression check for the list(self.scenario_entities) snapshot -- without it, removing
        # "spectral wolf" from self.scenario_entities while still iterating it could skip
        # whatever's ordered right after it. Order matters here: the wolf has to land *before*
        # the troll in scenario_entities for a missing snapshot to actually skip the troll's own
        # regeneration, so the troll is instanced and appended after the wolf, not before.
        # Empty entities list -- avoids re-instancing "gladstone" a second time as an orphaned
        # "gladstone_2" (see the previous test's own comment for why).
        self._load_ad_hoc_scenario([])
        self.dm_core._summon_creature({"name": "spectral wolf", "duration": 1})
        self.dm_core._instance_entities([{"name": "troll", "band": 1}])
        self.dm_core.scenario_entities.append("troll")
        self.assertEqual(self.dm_core.scenario_entities, ["gladstone", "spectral wolf", "troll"])
        Combat_Resolution.apply_damage(self.dm_core.world, "troll", 10)

        self.dm_core.run_round_upkeep()

        self.assertNotIn("spectral wolf", self.dm_core.scenario_entities)
        self.assertGreater(Combat_Resolution.get_current_hp(self.dm_core.world, "troll"), 30)  # still healed this round

    def test_apply_summon_if_hit_with_no_current_target_auto_succeeds(self):
        # Empty entities list -- _instance_location_persistent_names' own "guarantee" fallback
        # inserts self.player_name directly without re-instancing it, so this doesn't collide
        # with the "gladstone" the parent setUp already instanced once via "arena" (unlike
        # explicitly listing {"name": "gladstone", ...} again here, which would instead produce
        # a second, orphaned "gladstone_2" instance -- see debug.toml's own real-scenario
        # precedent for this same "never name the player" convention).
        self._load_ad_hoc_scenario([])
        self.assertIsNone(self.dm_core.current_target)
        resolved = self._capture("action_resolved")

        with patch("random.randint", return_value=4):
            self.dm_core._on_turn_detected({
                "clauses": [{"kind": "action", "skill": "summon spectral wolf"}], "input": "I summon a wolf",
            })

        self.assertIn("spectral wolf", self.dm_core.scenario_entities)
        action = resolved[-1]["actions"][0]
        self.assertEqual([e.name for e in action.effects if isinstance(e, SummonEffect)], ["spectral wolf"])
        self.assertFalse(any(isinstance(effect, DamageEffect) for effect in action.effects))

    def test_apply_summon_if_hit_against_a_hostile_target_ticks_this_rounds_upkeep_too(self):
        # debug.toml's own default scenario already has a hostile wolf as current_target --
        # this exercises the targeted cast path (a flat check against the spell's own authored
        # difficulty of 10; gladstone's 2D arcane at a fixed per-die value of 5 rolls exactly
        # 10), and confirms _resolve_combat_round's own run_round_upkeep (which fires later in
        # the same turn) already counts this round against the freshly-summoned wolf's own
        # duration.
        resolved = self._capture("round_resolved")

        with patch("random.randint", return_value=5):
            self.dm_core._on_turn_detected({
                "clauses": [{"kind": "action", "skill": "summon spectral wolf"}], "input": "I summon a wolf",
            })

        self.assertIn("spectral wolf", self.dm_core.scenario_entities)
        action = resolved[-1]["actions"][0]
        self.assertEqual([e.name for e in action.effects if isinstance(e, SummonEffect)], ["spectral wolf"])
        self.assertEqual(self.dm_core.entities["spectral wolf"]["summon_expires_in"], 3)  # 4 - 1


class TestCreateSpawn(DMTestCase):
    """!
    @brief create_spawn (an ability field) + Combat_Actions.py's own calculate_damage death-hook +
        DM_Summoning.py's _advance_pending_spawn -- the Pathfinder Wight/Shadow "kills become
        one of us" shape: a real kill stashes a pending-spawn record on the corpse, ticked down
        once per round (even for a dead entity) until it instances a new, permanent entity at
        the corpse's own band.
    """

    def setUp(self):
        super().setUp()
        self.dm_core._instance_entities([{"name": "pickpocket", "band": 1}])
        self.dm_core.scenario_entities.append("pickpocket")

    def _kill_with_create_spawn(self, target_name, requirements=None):
        ability = {
            "damage_value": {"dice": 10, "pips": 0, "bonus": 0}, "damage_tags": [],
            "create_spawn": {"name": "coyote", "delay_rounds": 2, "requirements": requirements or []},
        }
        Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", target_name, ability)

    def test_a_real_kill_matching_requirements_stashes_a_pending_spawn_on_the_corpse(self):
        requirements = [{"field": "subtype", "operator": "==", "value": "humanoid"}]
        self._kill_with_create_spawn("pickpocket", requirements)
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "pickpocket"), 0)
        pending = self.dm_core.entities["pickpocket"]["pending_spawn"]
        self.assertEqual(pending, {"name": "coyote", "band": Combat_Resolution.get_band(self.dm_core.world, "pickpocket"), "rounds_remaining": 2})

    def test_a_kill_not_matching_requirements_stashes_nothing(self):
        requirements = [{"field": "subtype", "operator": "==", "value": "humanoid"}]
        self._kill_with_create_spawn("wolf", requirements)
        self.assertNotIn("pending_spawn", self.dm_core.entities["wolf"])

    def test_no_requirements_matches_anything(self):
        self._kill_with_create_spawn("wolf")
        self.assertIn("pending_spawn", self.dm_core.entities["wolf"])

    def test_a_non_lethal_hit_stashes_nothing(self):
        ability = {
            "damage_value": {"dice": 0, "pips": 0, "bonus": 0}, "damage_tags": [],
            "create_spawn": {"name": "coyote", "delay_rounds": 2},
        }
        Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "pickpocket", ability)
        self.assertNotIn("pending_spawn", self.dm_core.entities["pickpocket"])

    def test_pending_spawn_ticks_down_and_instances_a_new_permanent_entity(self):
        self._kill_with_create_spawn("pickpocket")
        self.dm_core.run_round_upkeep()
        self.assertEqual(self.dm_core.entities["pickpocket"]["pending_spawn"]["rounds_remaining"], 1)
        self.assertNotIn("coyote", self.dm_core.scenario_entities)

        self.dm_core.run_round_upkeep()

        self.assertNotIn("pending_spawn", self.dm_core.entities["pickpocket"])
        self.assertIn("coyote", self.dm_core.scenario_entities)
        coyote = self.dm_core.entities["coyote"]
        self.assertEqual(coyote["band"], Combat_Resolution.get_band(self.dm_core.world, "pickpocket"))
        self.assertTrue(coyote["ad_hoc"])
        self.assertNotIn("summon_expires_in", coyote)  # permanent, unlike an ordinary summon

    def test_pending_spawn_keeps_ticking_on_a_dead_corpse_across_upkeep(self):
        # The corpse itself has 0 HP and would otherwise be skipped by run_round_upkeep's own
        # ordinary "if hp <= 0: continue" gate -- _advance_pending_spawn is deliberately called
        # before that check.
        self._kill_with_create_spawn("pickpocket")
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "pickpocket"), 0)
        self.dm_core.run_round_upkeep()
        self.dm_core.run_round_upkeep()
        self.assertIn("coyote", self.dm_core.scenario_entities)

    def test_ability_with_no_create_spawn_stashes_nothing(self):
        ability = {"damage_value": {"dice": 10, "pips": 0, "bonus": 0}, "damage_tags": []}
        Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "pickpocket", ability)
        self.assertNotIn("pending_spawn", self.dm_core.entities["pickpocket"])


class TestDispelMagic(DMTestCase):
    """!
    @brief A spell's own "dispel" field ({supertypes, subtypes}), matches_supertype_or_subtype
        (Combat_Resolution.py -- shared with get_damage_bonus_vs's own "Holy"/"Bane" matching),
        and DM_Core.py's own _apply_dispel_if_hit. Shipped as spells.toml's "dispel magic"
        ({supertypes = ["spell"], subtypes = ["spell"]} -- "anything magical", not narrowly
        scoped to one particular effect shape), targeting spells.toml's "flame wall" via its
        shared subtype = "spell".
    """

    def setUp(self):
        super().setUp()
        # Empty entities list -- see TestPersistentTerrainHazards' own setUp comment for why
        # "gladstone" is never named explicitly here.
        self._load_ad_hoc_scenario([])
        self.dm_core._instance_entities([{"name": "flame wall", "band": 1}])
        self.dm_core.scenario_entities.append("flame wall")

    def test_matches_supertype_or_subtype_matches_on_either_list(self):
        wall = self.dm_core.entities["flame wall"]
        self.assertTrue(Combat_Resolution.matches_supertype_or_subtype(wall, {"subtypes": ["spell"]}))
        self.assertTrue(Combat_Resolution.matches_supertype_or_subtype(wall, {"supertypes": ["object"]}))
        self.assertFalse(Combat_Resolution.matches_supertype_or_subtype(wall, {"subtypes": ["trap"]}))
        # Neither key present at all matches nothing, not everything.
        self.assertFalse(Combat_Resolution.matches_supertype_or_subtype(wall, {}))

    def test_matches_supertype_or_subtype_also_matches_a_live_spell_catalog_entrys_supertype(self):
        # The shipped filter matches "spell" on BOTH axes -- a bare spell-catalog entity's own
        # supertype is always "spell", covering that shape too, not just a conjured effect
        # object's subtype (flame wall, above).
        fireball = self.dm_core.entities["fireball"]
        self.assertTrue(Combat_Resolution.matches_supertype_or_subtype(fireball, {"supertypes": ["spell"]}))

    def test_apply_dispel_if_hit_banishes_a_matching_target(self):
        ability = {"dispel": {"supertypes": ["spell"], "subtypes": ["spell"]}}
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=10, difficulty=0, success=True)
        apply_effects(self.dm_core, result, None, ability, None, "flame wall")
        self.assertNotIn("flame wall", self.dm_core.scenario_entities)
        self.assertEqual([e.name for e in result.effects if isinstance(e, DispelEffect)], ["flame wall"])

    def test_apply_dispel_if_hit_ignores_a_non_matching_target(self):
        # Aiming "dispel magic" at gladstone (supertype "creature") does nothing -- the real
        # Pathfinder "used on the wrong thing just wastes the action" shape, not a hard gate.
        ability = {"dispel": {"supertypes": ["spell"], "subtypes": ["spell"]}}
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=10, difficulty=0, success=True)
        apply_effects(self.dm_core, result, None, ability, None, "gladstone")
        self.assertIn("gladstone", self.dm_core.scenario_entities)
        self.assertEqual(result.effects, [])

    def test_apply_dispel_if_hit_does_nothing_on_a_failed_roll(self):
        ability = {"dispel": {"supertypes": ["spell"], "subtypes": ["spell"]}}
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=1, difficulty=10, success=False)
        apply_effects(self.dm_core, result, None, ability, None, "flame wall")
        self.assertIn("flame wall", self.dm_core.scenario_entities)

    def test_casting_dispel_magic_end_to_end_banishes_the_flame_wall(self):
        # "flame wall" has no skills at all, so a high roll always wins the ordinary opposed
        # check "dispel magic" falls to (no "difficulty" authored on it).
        self.dm_core.current_target = "flame wall"
        resolved = self._capture("action_resolved")
        with patch("random.randint", return_value=6):
            self.dm_core._on_turn_detected({
                "clauses": [{"kind": "action", "skill": "dispel magic"}], "input": "I dispel the flame wall",
            })
        self.assertNotIn("flame wall", self.dm_core.scenario_entities)
        action = resolved[-1]["actions"][0]
        self.assertEqual([e.name for e in action.effects if isinstance(e, DispelEffect)], ["flame wall"])


class TestCureConditionType(DMTestCase):
    """!
    @brief cure (an ability field, {supertypes, subtypes}) + dismiss_matching_conditions
        (Combat_Resolution.py) + DM_Core.py's own _apply_cure_if_hit -- matches_supertype_or_
        subtype reused unchanged against the [[condition]] catalog instead of the entity one
        (the Pathfinder "Remove Disease"/"Neutralize Poison" shape: the caster doesn't need to
        name the specific affliction, just its kind). Shipped as spells.toml's "cure disease"
        ({subtypes = ["disease"]}), targeting rules.toml's own "filth fever" via its shared
        subtype = "disease".
    """

    def setUp(self):
        super().setUp()
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "filth fever", duration="permanent", dismiss="")

    def test_matches_supertype_or_subtype_matches_the_condition_catalogs_own_classification(self):
        filth_fever = Combat_Resolution._find_condition_def(self.dm_core.world, "filth fever")
        self.assertTrue(Combat_Resolution.matches_supertype_or_subtype(filth_fever, {"subtypes": ["disease"]}))
        self.assertTrue(Combat_Resolution.matches_supertype_or_subtype(filth_fever, {"supertypes": ["affliction"]}))
        self.assertFalse(Combat_Resolution.matches_supertype_or_subtype(filth_fever, {"subtypes": ["curse"]}))

    def test_apply_cure_if_hit_dismisses_a_matching_condition(self):
        ability = {"cure": {"subtypes": ["disease"]}}
        result = RolledOutcome(entity="gladstone", skill="miracles", roll=10, difficulty=0, success=True)
        apply_effects(self.dm_core, result, None, ability, None, "gladstone")
        self.assertFalse(Combat_Resolution.has_condition(self.dm_core.world, "gladstone", "filth fever"))
        self.assertEqual([e.conditions for e in result.effects if isinstance(e, CureEffect)], [["filth fever"]])

    def test_apply_cure_if_hit_ignores_a_non_matching_condition(self):
        # gladstone isn't afflicted with anything matching "curse" -- casting it does nothing,
        # the same "used on the wrong thing just wastes the action" shape dispel already has.
        ability = {"cure": {"subtypes": ["curse"]}}
        result = RolledOutcome(entity="gladstone", skill="miracles", roll=10, difficulty=0, success=True)
        apply_effects(self.dm_core, result, None, ability, None, "gladstone")
        self.assertTrue(Combat_Resolution.has_condition(self.dm_core.world, "gladstone", "filth fever"))
        self.assertEqual([e.conditions for e in result.effects if isinstance(e, CureEffect)], [[]])

    def test_apply_cure_if_hit_does_nothing_on_a_failed_roll(self):
        ability = {"cure": {"subtypes": ["disease"]}}
        result = RolledOutcome(entity="gladstone", skill="miracles", roll=1, difficulty=10, success=False)
        apply_effects(self.dm_core, result, None, ability, None, "gladstone")
        self.assertTrue(Combat_Resolution.has_condition(self.dm_core.world, "gladstone", "filth fever"))
        self.assertEqual(result.effects, [])

    def test_casting_cure_disease_end_to_end_cures_filth_fever(self):
        self.dm_core.current_target = "gladstone"
        resolved = self._capture("action_resolved")
        self._stub_roll_dice(10)  # beats "cure disease"'s own flat difficulty of 8
        self.dm_core._on_turn_detected({
            "clauses": [{"kind": "action", "skill": "cure disease"}], "input": "I cure gladstone's disease",
        })
        self.assertFalse(Combat_Resolution.has_condition(self.dm_core.world, "gladstone", "filth fever"))
        action = resolved[-1]["actions"][0]
        self.assertEqual([e.conditions for e in action.effects if isinstance(e, CureEffect)], [["filth fever"]])


class TestBandit(DMTestCase):
    # "bandit" is debug.toml's own local entity now (creatures.toml no longer carries one --
    # see its own comment) -- booting "field" first is what makes it resolvable at all, before
    # setUp below overrides self.dm_core.scenario/re-runs load_scenario() with a custom band
    # layout.
    scenario_name = "debug"
    start_location = "field_grounds"

    def setUp(self):
        super().setUp()
        self._load_ad_hoc_scenario(
            [{"name": "gladstone", "band": 1}, {"name": "bandit", "band": 5}], bands=8, enclosed=False,
        )


    def test_favors_the_bow_at_a_distance(self):
        # Starting gap is 4 -- exactly the short bow's own range, so it's both "not adjacent"
        # (distance_to_target > 0, the behavior's own requirement) and actually reachable.
        behavior = Combat_Actions.choose_behavior(self.dm_core.world, "bandit", "gladstone")
        self.assertEqual(behavior["action"], "short bow")

        turn = Combat_Actions.resolve_behavior_action(self.dm_core.world, "bandit", "gladstone")
        self.assertEqual(turn.skill, "missiles")
        self.assertNotIsInstance(turn, MovementOutcome)


class TestStatusEvaluation(DMTestCase):
    def test_hp_per_remain_requirement_matches_current_percentage(self):
        # gladstone: max_hp 36. At 18 hp (50%) the "wounded" status (0.40-0.59) should match.
        Combat_Resolution.apply_damage(self.dm_core.world, "gladstone", 18)
        matched_names = [s["name"] for s in Combat_Resolution.get_applicable_statuses(self.dm_core.world, "gladstone", "on_damage")]
        self.assertIn("wounded", matched_names)
        self.assertNotIn("severe", matched_names)


    def test_apply_damage_auto_applies_matching_condition(self):
        Combat_Resolution.apply_damage(self.dm_core.world, "gladstone", 18)  # -> 50% hp -> "wounded"
        self.assertIn("wounded", self.dm_core.entities["gladstone"]["active_conditions"])


    def test_dead_condition_is_not_auto_dismissed_by_healing(self):
        # "dead"'s apply block sets dismiss = "resurrection", so simple healing must not
        # revive it via the same automatic sweep that clears "wounded"/"stunned"/etc.
        Combat_Resolution.apply_damage(self.dm_core.world, "gladstone", 36)  # 0% -> dead
        self.assertIn("dead", self.dm_core.entities["gladstone"]["active_conditions"])
        Combat_Resolution.apply_healing(self.dm_core.world, "gladstone", 999)
        self.assertIn("dead", self.dm_core.entities["gladstone"]["active_conditions"])


class TestRequirementsEngine(DMTestCase):
    """!
    @brief The `between` operator, {"all"|"any"|"none"} boolean nesting in
        entity_matches_requirements, and [entity.test]'s new optional "requirements" field --
        see docs/combat.md's "Status and conditions"/"Entity tests".
    """

    def test_between_matches_the_same_wound_tier_the_old_two_requirement_form_did(self):
        # gladstone: max_hp 36. rules.toml's "wounded" tier is now authored as a single
        # between = [0.40, 0.59] requirement instead of two chained >=/<= ones.
        Combat_Resolution.apply_damage(self.dm_core.world, "gladstone", 18)  # -> 50% hp
        matched_names = [s["name"] for s in Combat_Resolution.get_applicable_statuses(self.dm_core.world, "gladstone", "on_damage")]
        self.assertIn("wounded", matched_names)
        self.assertNotIn("severe", matched_names)

    def test_between_is_inclusive_at_both_ends(self):
        entities = {"gladstone": {"hp": 5, "max_hp": 10}}
        requirements = [{"field": "hp_per_remain", "operator": "between", "value": [0.5, 0.5]}]
        self.assertTrue(Combat_Resolution.entity_matches_requirements(WorldContext(entities=entities, event_bus=self.event_bus), "gladstone", requirements))

    def test_any_matches_if_either_branch_holds(self):
        entities = {"gladstone": {"hp": 10, "max_hp": 10, "active_conditions": {"prone": {}}}}
        requirements = [{"any": [
            {"field": "has_condition:paralyzed", "operator": "==", "value": True},
            {"field": "has_condition:prone", "operator": "==", "value": True},
        ]}]
        self.assertTrue(Combat_Resolution.entity_matches_requirements(WorldContext(entities=entities, event_bus=self.event_bus), "gladstone", requirements))

    def test_none_fails_when_any_branch_holds(self):
        entities = {"gladstone": {"hp": 10, "max_hp": 10, "active_conditions": {"prone": {}}}}
        requirements = [{"none": [
            {"field": "has_condition:paralyzed", "operator": "==", "value": True},
            {"field": "has_condition:prone", "operator": "==", "value": True},
        ]}]
        self.assertFalse(Combat_Resolution.entity_matches_requirements(WorldContext(entities=entities, event_bus=self.event_bus), "gladstone", requirements))

    def test_all_and_any_nest_inside_each_other(self):
        entities = {"gladstone": {"hp": 3, "max_hp": 10, "active_conditions": {"shaken": {}}}}
        requirements = [{"all": [
            {"field": "hp_per_remain", "operator": "<", "value": 0.5},
            {"any": [
                {"field": "has_condition:shaken", "operator": "==", "value": True},
                {"field": "has_condition:frightened", "operator": "==", "value": True},
            ]},
        ]}]
        self.assertTrue(Combat_Resolution.entity_matches_requirements(WorldContext(entities=entities, event_bus=self.event_bus), "gladstone", requirements))
        entities["gladstone"]["hp"] = 9  # 90% -- fails the "all" branch's own hp_per_remain check now
        self.assertFalse(Combat_Resolution.entity_matches_requirements(WorldContext(entities=entities, event_bus=self.event_bus), "gladstone", requirements))

    def test_between_evaluates_in_a_program_if_step(self):
        entities = {"gladstone": {"hp": 5, "max_hp": 10}}
        self.assertTrue(evaluate_condition("actor.hp_per_remain between [0.4, 0.6]", {"actor": "gladstone"}, entities))
        self.assertFalse(evaluate_condition("actor.hp_per_remain between [0.7, 1.0]", {"actor": "gladstone"}, entities))

    def test_entity_test_requirements_field_gates_availability(self):
        self.dm_core.entities["dummy_test_target"] = {"name": "dummy_test_target", "hp": 3, "max_hp": 10}
        test = {
            "skill": ["finesse"],
            "requirements": [{"field": "hp_per_remain", "operator": "between", "value": [0.0, 0.5]}],
        }
        self.assertTrue(self.dm_core.is_test_available("dummy_test_target", test, "finesse"))
        self.dm_core.entities["dummy_test_target"]["hp"] = 9
        self.assertFalse(self.dm_core.is_test_available("dummy_test_target", test, "finesse"))

    def test_existing_requires_condition_only_tests_are_unaffected(self):
        # The shipped chest lock still only authors requires_condition/blocks_if_condition --
        # no "requirements" key at all -- and must keep working exactly as before.
        self.dm_core.entities["dummy_chest"] = {"name": "dummy_chest", "active_conditions": {"locked": {}}}
        test = {"skill": ["finesse"], "requires_condition": "locked", "blocks_if_condition": "jammed"}
        self.assertTrue(self.dm_core.is_test_available("dummy_chest", test, "finesse"))
        del self.dm_core.entities["dummy_chest"]["active_conditions"]["locked"]
        self.assertFalse(self.dm_core.is_test_available("dummy_chest", test, "finesse"))


class TestConditionModifiers(DMTestCase):
    """!
    @brief get_condition_modifier (DM_Status.py) and its use in resolve_action/
        resolve_opposed_action (Combat_Resolution.py) -- a [[condition]] entry's own modifier now
        actually costs dice/pips/bonus, not just narration (see CLAUDE.md's "Status and
        conditions").
    """

    def test_get_condition_modifier_sums_matching_active_conditions(self):
        # rules.toml's own "wounded" [[condition]] entry is {dice: -1, pips: 0, bonus: 0}.
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "wounded", duration="permanent", dismiss="")
        self.assertEqual(
            Combat_Resolution.get_condition_modifier(self.dm_core.world, "gladstone"),
            {"dice": -1, "pips": 0, "bonus": 0},
        )

    def test_get_condition_modifier_sums_the_surprised_condition(self):
        # rules.toml's own "surprised" [[condition]] entry is {dice: -2, pips: 0, bonus: 0} --
        # heavier than "wounded"'s -1, per docs/downtime.md's "Night watch and surprise".
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "surprised", duration="rounds", length=1, dismiss="")
        self.assertEqual(
            Combat_Resolution.get_condition_modifier(self.dm_core.world, "gladstone"),
            {"dice": -2, "pips": 0, "bonus": 0},
        )

    def test_get_condition_modifier_ignores_conditions_with_no_rules_entry(self):
        # "hidden" is a plain presence flag (see items.toml's dart trap) with no [[condition]]
        # entry of its own -- it must not silently contribute a modifier.
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "hidden", duration="permanent", dismiss="")
        self.assertEqual(
            Combat_Resolution.get_condition_modifier(self.dm_core.world, "gladstone"),
            {"dice": 0, "pips": 0, "bonus": 0},
        )

    def test_get_condition_modifier_applies_only_to_the_scoped_skill(self):
        # rules.toml's own "dazzled" now authors applies_to = ["observation"] -- Pathfinder's
        # Dazzled is sight-only, not a blanket penalty.
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "dazzled", duration="permanent", dismiss="")
        self.assertEqual(
            Combat_Resolution.get_condition_modifier(self.dm_core.world, "gladstone", "observation"),
            {"dice": -1, "pips": 0, "bonus": 0},
        )
        self.assertEqual(
            Combat_Resolution.get_condition_modifier(self.dm_core.world, "gladstone", "blades"),
            {"dice": 0, "pips": 0, "bonus": 0},
        )

    def test_get_condition_modifier_with_no_skill_name_skips_scoped_conditions(self):
        # No skill context at all (skill_name=None, the default) can't match an "applies_to"
        # list -- same "can't match without a value" precedent distance_to_target already
        # follows with no opponent_name.
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "dazzled", duration="permanent", dismiss="")
        self.assertEqual(
            Combat_Resolution.get_condition_modifier(self.dm_core.world, "gladstone"),
            {"dice": 0, "pips": 0, "bonus": 0},
        )

    def test_get_condition_modifier_unscoped_condition_still_applies_regardless_of_skill(self):
        # "wounded" authors no applies_to at all -- it must still apply globally, the
        # pre-existing behavior for every condition that doesn't opt into scoping.
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "wounded", duration="permanent", dismiss="")
        self.assertEqual(
            Combat_Resolution.get_condition_modifier(self.dm_core.world, "gladstone", "observation"),
            {"dice": -1, "pips": 0, "bonus": 0},
        )

    def test_resolve_action_scoped_condition_only_penalizes_the_named_skill(self):
        # gladstone's observation: 2D+0; blades: 5D+0. "dazzled" (applies_to = ["observation"])
        # must cost a die on the former but leave the latter untouched.
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "dazzled", duration="permanent", dismiss="")
        with patch("random.randint", return_value=3):
            observation_result = Combat_Resolution.resolve_action(self.dm_core.world, "gladstone", "observation")
            blades_result = Combat_Resolution.resolve_action(self.dm_core.world, "gladstone", "blades")
        self.assertEqual(observation_result["roll"], 3)   # (2 - 1) * 3
        self.assertEqual(blades_result["roll"], 15)       # 5 * 3, unpenalized

    def test_resolve_action_folds_condition_dice_penalty_into_the_roll(self):
        # gladstone's blades: 5D+0. "wounded" is -1D, same floor-at-zero rule dice_penalty uses.
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "wounded", duration="permanent", dismiss="")
        with patch("random.randint", return_value=3):
            result = Combat_Resolution.resolve_action(self.dm_core.world, "gladstone", "blades")
        self.assertEqual(result["roll"], 12)  # (5 - 1) * 3

    def test_resolve_opposed_action_applies_the_defenders_own_condition_modifier(self):
        # The defender's active_conditions reduce their own roll independently of
        # dice_penalty, which never touches the defender's side at all (see
        # TestMultipleActions.test_resolve_opposed_action_penalty_never_touches_the_defenders_roll).
        self.dm_core.entities["test_defender"] = {
            "name": "test_defender", "skills": {"dodge": {"dice": 6, "pips": 0}},
        }
        Combat_Resolution.apply_condition(self.dm_core.world, "test_defender", "stunned", duration="rounds", length=1, dismiss="")
        with patch("random.randint", return_value=3):
            result = Combat_Resolution.resolve_opposed_action(self.dm_core.world, "gladstone", "blades", "test_defender")
        self.assertEqual(result["difficulty"], 15)  # (6 - 1) * 3


class TestOnHitCondition(DMTestCase):
    """!
    @brief on_hit_condition (an ability field) + apply_on_hit_condition (Combat_Resolution.py)
        -- an ability applies a [[condition]] directly to whoever it just hit, no
        [entity.test] detour needed (the Pathfinder "Wounding"/poison-on-hit shape).
    """

    def test_a_successful_hit_applies_the_named_condition(self):
        ability = {
            "damage_value": {"dice": 0, "pips": 0, "bonus": 0},
            "damage_tags": ["slashing"],
            "on_hit_condition": {"condition": "bleeding", "duration": "permanent", "dismiss": ""},
        }
        self.dm_core.rules.setdefault("condition", []).append(
            {"name": "bleeding", "upkeep_damage": {"dice": 1, "pips": 0, "bonus": 0}}
        )
        Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "wolf", ability)
        self.assertIn("bleeding", self.dm_core.entities["wolf"]["active_conditions"])

    def test_chance_below_100_can_fail_to_apply(self):
        ability = {
            "damage_value": {"dice": 0, "pips": 0, "bonus": 0},
            "damage_tags": [],
            "on_hit_condition": {"condition": "bleeding", "chance": 1},
        }
        with patch("random.randint", return_value=99):
            Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "wolf", ability)
        self.assertNotIn("bleeding", self.dm_core.entities["wolf"].get("active_conditions", {}))

    def test_immune_defender_never_gains_the_condition(self):
        # wolf immune to "slashing" for this test -- a hit it's fully immune to shouldn't also
        # inflict a condition tied to that same damage type.
        self.dm_core.entities["wolf"]["immunity_tags"] = ["slashing"]
        ability = {
            "damage_value": {"dice": 0, "pips": 0, "bonus": 0},
            "damage_tags": ["slashing"],
            "on_hit_condition": {"condition": "bleeding"},
        }
        Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "wolf", ability)
        self.assertNotIn("bleeding", self.dm_core.entities["wolf"].get("active_conditions", {}))

    def test_ability_with_no_on_hit_condition_is_unaffected(self):
        ability = {"damage_value": {"dice": 0, "pips": 0, "bonus": 0}, "damage_tags": []}
        Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "wolf", ability)
        self.assertEqual(self.dm_core.entities["wolf"].get("active_conditions", {}), {})


class TestDamageBonusVs(DMTestCase):
    """!
    @brief damage_bonus_vs (an ability field) + get_damage_bonus_vs (Combat_Resolution.py) --
        extra rolled damage that only applies against a defender of a particular kind, matched
        by supertype/subtype (the Pathfinder "Holy"/"Bane" shape).
    """

    def test_bonus_applies_when_defenders_supertype_matches(self):
        self.dm_core.entities["wolf"]["supertype"] = "undead"
        ability = {
            "damage_value": {"dice": 0, "pips": 0, "bonus": 0},
            "damage_tags": [],
            "damage_bonus_vs": {"supertypes": ["undead"], "value": {"dice": 0, "pips": 0, "bonus": 5}},
        }
        result = Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "wolf", ability)
        self.assertEqual(result["bonus_vs"], 5)
        self.assertEqual(result["net_damage"], 5)

    def test_bonus_does_not_apply_when_supertype_does_not_match(self):
        # wolf's own real supertype ("creature") never matches "undead".
        ability = {
            "damage_value": {"dice": 0, "pips": 0, "bonus": 0},
            "damage_tags": [],
            "damage_bonus_vs": {"supertypes": ["undead"], "value": {"dice": 0, "pips": 0, "bonus": 5}},
        }
        result = Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "wolf", ability)
        self.assertEqual(result["bonus_vs"], 0)


class TestDamageBonusIfCondition(DMTestCase):
    """!
    @brief damage_bonus_if_condition (an ability field) + get_damage_bonus_if_condition
        (Combat_Resolution.py) -- extra rolled damage that only applies while the defender
        currently carries a named condition (the Pathfinder Sneak Attack shape), a twin of
        damage_bonus_vs keyed off the defender's own state rather than its type.
    """

    def test_bonus_applies_when_defender_carries_the_condition(self):
        self.dm_core.entities["wolf"]["active_conditions"] = {"flat_footed": {"duration": "permanent"}}
        ability = {
            "damage_value": {"dice": 0, "pips": 0, "bonus": 0},
            "damage_tags": [],
            "damage_bonus_if_condition": {"condition": "flat_footed", "value": {"dice": 0, "pips": 0, "bonus": 5}},
        }
        result = Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "wolf", ability)
        self.assertEqual(result["bonus_if_condition"], 5)
        self.assertEqual(result["net_damage"], 5)

    def test_bonus_does_not_apply_when_defender_lacks_the_condition(self):
        ability = {
            "damage_value": {"dice": 0, "pips": 0, "bonus": 0},
            "damage_tags": [],
            "damage_bonus_if_condition": {"condition": "flat_footed", "value": {"dice": 0, "pips": 0, "bonus": 5}},
        }
        result = Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "wolf", ability)
        self.assertEqual(result["bonus_if_condition"], 0)

    def test_immune_defender_never_gets_the_bonus(self):
        self.dm_core.entities["wolf"]["active_conditions"] = {"flat_footed": {"duration": "permanent"}}
        self.dm_core.entities["wolf"]["immunity_tags"] = ["slashing"]
        ability = {
            "damage_value": {"dice": 1, "pips": 0, "bonus": 0},
            "damage_tags": ["slashing"],
            "damage_bonus_if_condition": {"condition": "flat_footed", "value": {"dice": 0, "pips": 0, "bonus": 5}},
        }
        result = Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "wolf", ability)
        self.assertEqual(result["bonus_if_condition"], 0)
        self.assertEqual(result["net_damage"], 0)


class TestConditionImmunity(DMTestCase):
    """!
    @brief immune_conditions (an entity field) + apply_condition's own immunity gate
        (Combat_Resolution.py) -- immunity to a named condition or condition *kind*, matched by
        that [[condition]] entry's own supertype/subtype (the Pathfinder "Immune to charm,
        sleep, mind-affecting" shape), distinct from immunity_tags' damage-tag-only matching.
    """

    def setUp(self):
        super().setUp()
        self.dm_core.rules.setdefault("condition", []).append(
            {"name": "charmed", "supertype": "affliction", "subtype": "charm",
             "modifier": {"dice": 0, "pips": 0, "bonus": 0}}
        )

    def test_matching_subtype_blocks_the_condition_entirely(self):
        self.dm_core.entities["wolf"]["immune_conditions"] = {"subtypes": ["charm"]}
        Combat_Resolution.apply_condition(self.dm_core.world, "wolf", "charmed", duration="permanent")
        self.assertNotIn("charmed", self.dm_core.entities["wolf"].get("active_conditions", {}))

    def test_matching_supertype_blocks_the_condition_entirely(self):
        self.dm_core.entities["wolf"]["immune_conditions"] = {"supertypes": ["affliction"]}
        Combat_Resolution.apply_condition(self.dm_core.world, "wolf", "charmed", duration="permanent")
        self.assertNotIn("charmed", self.dm_core.entities["wolf"].get("active_conditions", {}))

    def test_non_matching_immune_conditions_still_lets_it_land(self):
        self.dm_core.entities["wolf"]["immune_conditions"] = {"subtypes": ["sleep"]}
        Combat_Resolution.apply_condition(self.dm_core.world, "wolf", "charmed", duration="permanent")
        self.assertIn("charmed", self.dm_core.entities["wolf"]["active_conditions"])

    def test_no_immune_conditions_field_is_unaffected(self):
        Combat_Resolution.apply_condition(self.dm_core.world, "wolf", "charmed", duration="permanent")
        self.assertIn("charmed", self.dm_core.entities["wolf"]["active_conditions"])


class TestEquippedSkillBonus(DMTestCase):
    """!
    @brief equipped_skill_bonus (an item field) + get_equipped_skill_bonus (Combat_
        Resolution.py) -- a worn item's own passive skill-dice bonus, folded into resolve_
        action/resolve_opposed_action (the Pathfinder "Ring/belt/wondrous stat bonus" shape).
    """

    def setUp(self):
        super().setUp()
        self.dm_core.entities["ring of observation"] = {
            "name": "ring of observation", "equipped_skill_bonus": {"skill": "observation", "dice": 1, "pips": 0},
        }
        self.dm_core.entities["gladstone"]["inventory"].append("ring of observation")
        self.dm_core.entities["gladstone"]["equipped"]["ring"] = "ring of observation"

    def test_get_equipped_skill_bonus_sums_a_matching_worn_item(self):
        self.assertEqual(
            Combat_Resolution.get_equipped_skill_bonus(self.dm_core.world, "gladstone", "observation"),
            {"dice": 1, "pips": 0},
        )

    def test_get_equipped_skill_bonus_ignores_a_non_matching_skill(self):
        self.assertEqual(
            Combat_Resolution.get_equipped_skill_bonus(self.dm_core.world, "gladstone", "blades"),
            {"dice": 0, "pips": 0},
        )

    def test_resolve_action_folds_the_worn_bonus_into_the_roll(self):
        # gladstone's observation is 2D+0; the ring adds +1D.
        with patch("random.randint", return_value=3):
            result = Combat_Resolution.resolve_action(self.dm_core.world, "gladstone", "observation")
        self.assertEqual(result["roll"], 9)  # (2 + 1) * 3

    def test_resolve_opposed_action_folds_the_defenders_own_worn_bonus(self):
        self.dm_core.entities["test_defender"] = {
            "name": "test_defender", "skills": {"dodge": {"dice": 2, "pips": 0}},
            "equipped": {"ring": "ring of observation"},
        }
        self.dm_core.entities["ring of observation"]["equipped_skill_bonus"] = {"skill": "dodge", "dice": 2, "pips": 0}
        with patch("random.randint", return_value=3):
            result = Combat_Resolution.resolve_opposed_action(self.dm_core.world, "gladstone", "blades", "test_defender")
        self.assertEqual(result["difficulty"], 12)  # (2 + 2) * 3


class TestDestroyEquipped(DMTestCase):
    """!
    @brief destroy_equipped (an ability field) + apply_destroy_equipped (Combat_Resolution.py)
        -> Inventory_Resolution.destroy_equipped_item, and the matching "destroy_equipped"
        Program_Interpreter op -- the Pathfinder Rust Monster / disarm shape: destroys whatever
        the target has equipped in a given slot outright, no partial-damage tracking.
    """

    def setUp(self):
        super().setUp()
        self.dm_core.entities["wolf"]["inventory"] = ["rusty shortsword"]
        self.dm_core.entities["wolf"]["equipped"] = {"rhand": "rusty shortsword"}
        self.dm_core.entities["rusty shortsword"] = {"name": "rusty shortsword", "supertype": "object", "subtype": "weapon"}

    def test_a_successful_hit_destroys_the_equipped_item(self):
        ability = {
            "damage_value": {"dice": 0, "pips": 0, "bonus": 0}, "damage_tags": [],
            "destroy_equipped": {"slot": "rhand", "chance": 100},
        }
        Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "wolf", ability)
        self.assertNotIn("rhand", self.dm_core.entities["wolf"]["equipped"])
        self.assertNotIn("rusty shortsword", self.dm_core.entities["wolf"]["inventory"])

    def test_chance_below_100_can_fail_to_destroy(self):
        ability = {
            "damage_value": {"dice": 0, "pips": 0, "bonus": 0}, "damage_tags": [],
            "destroy_equipped": {"slot": "rhand", "chance": 1},
        }
        with patch("random.randint", return_value=99):
            Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "wolf", ability)
        self.assertEqual(self.dm_core.entities["wolf"]["equipped"]["rhand"], "rusty shortsword")

    def test_empty_slot_is_a_harmless_no_op(self):
        del self.dm_core.entities["wolf"]["equipped"]["rhand"]
        ability = {
            "damage_value": {"dice": 0, "pips": 0, "bonus": 0}, "damage_tags": [],
            "destroy_equipped": {"slot": "rhand", "chance": 100},
        }
        Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "wolf", ability)
        self.assertIn("rusty shortsword", self.dm_core.entities["wolf"]["inventory"])

    def test_ability_with_no_destroy_equipped_is_unaffected(self):
        ability = {"damage_value": {"dice": 0, "pips": 0, "bonus": 0}, "damage_tags": []}
        Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "wolf", ability)
        self.assertEqual(self.dm_core.entities["wolf"]["equipped"]["rhand"], "rusty shortsword")

    def test_disarm_maneuvers_program_op_destroys_the_equipped_item(self):
        run_program(
            {"do": "destroy_equipped", "entity": "target", "slot": "rhand"},
            {"actor": "gladstone", "target": "wolf"},
            self.dm_core.entities, self.dm_core.rules, self.dm_core.event_bus,
        )
        self.assertNotIn("rhand", self.dm_core.entities["wolf"]["equipped"])
        self.assertNotIn("rusty shortsword", self.dm_core.entities["wolf"]["inventory"])


class TestAbilityCooldown(DMTestCase):
    """!
    @brief cooldown_rounds (an ability field) + tick_ability_cooldowns (Combat_Resolution.py)
        + the derived requirement field "ability_ready:<name>" -- an ability recharges over N
        rounds after use, and a behavior list can gate itself off the same ability meanwhile
        (the Pathfinder "breath weapon usable once every 1d4 rounds" shape).
    """

    def setUp(self):
        super().setUp()
        self.dm_core.entities["wolf"]["abilities"] = [{
            "name": "howl", "supertype": "innate", "subtype": "weapon", "skill": "brawling",
            "damage_tags": ["sonic"], "damage_value": {"dice": 1, "pips": 0, "bonus": 0},
            "cooldown_rounds": 2,
        }]
        self.dm_core.entities["wolf"]["behavior"] = [
            {"requirements": [{"field": "hp_per_remain", "operator": ">=", "value": 0.01}], "action": "howl"},
        ]

    def test_ability_ready_is_true_before_first_use(self):
        self.assertTrue(Combat_Resolution.get_comparable_value(self.dm_core.world, "wolf", "ability_ready:howl"))

    def test_using_the_ability_sets_its_cooldown(self):
        Combat_Actions.resolve_behavior_action(self.dm_core.world, "wolf", "gladstone")
        self.assertEqual(self.dm_core.entities["wolf"]["ability_cooldowns"]["howl"], 2)
        self.assertFalse(Combat_Resolution.get_comparable_value(self.dm_core.world, "wolf", "ability_ready:howl"))

    def test_run_round_upkeep_ticks_the_cooldown_down_to_zero_and_removes_it(self):
        Combat_Actions.resolve_behavior_action(self.dm_core.world, "wolf", "gladstone")
        self.dm_core.run_round_upkeep()
        self.assertEqual(self.dm_core.entities["wolf"]["ability_cooldowns"]["howl"], 1)
        self.dm_core.run_round_upkeep()
        self.assertNotIn("howl", self.dm_core.entities["wolf"].get("ability_cooldowns", {}))
        self.assertTrue(Combat_Resolution.get_comparable_value(self.dm_core.world, "wolf", "ability_ready:howl"))

    def test_a_behavior_entry_can_gate_off_while_the_ability_is_on_cooldown(self):
        self.dm_core.entities["wolf"]["behavior"] = [
            {
                "requirements": [
                    {"field": "hp_per_remain", "operator": ">=", "value": 0.01},
                    {"field": "ability_ready:howl", "operator": "==", "value": True},
                ],
                "action": "howl",
            },
            {"requirements": [{"field": "hp_per_remain", "operator": ">=", "value": 0.01}], "action": "bite"},
        ]
        self.dm_core.entities["wolf"]["ability_cooldowns"] = {"howl": 2}
        behavior = Combat_Actions.choose_behavior(self.dm_core.world, "wolf", "gladstone")
        self.assertEqual(behavior["action"], "bite")


class TestProximityStatuses(DMTestCase):
    """!
    @brief evaluate_proximity_statuses (DM_Status.py) + the [[status]] "on_action" trigger --
        applies a condition to nearby OTHER entities off the acting entity's own requirements,
        rather than self-applying the way "on_damage" statuses do (the Pathfinder "Fear aura/
        Frightful Presence" shape).
    """

    def setUp(self):
        super().setUp()
        self.dm_core.rules.setdefault("status", []).append({
            "trigger": "on_action",
            "requirements": [{"field": "subtype", "operator": "==", "value": "animal"}],
            "apply": {"condition": "shaken", "duration": "permanent", "dismiss": "", "radius": 5, "side": "enemies"},
        })

    def test_applies_the_condition_to_a_nearby_hostile_entity(self):
        # "wolf_2" (debug.toml's second wolf, same band as "wolf") has no [entity.attitudes]
        # table of its own -- is_hostile treats it as hostile unconditionally, same as "wolf",
        # so it passes the status's own side = "enemies" filter relative to the actor.
        Combat_Actions.evaluate_proximity_statuses(self.dm_core.world, "wolf", "on_action")
        self.assertIn("shaken", self.dm_core.entities["wolf_2"]["active_conditions"])

    def test_never_applies_the_condition_to_the_actor_itself(self):
        Combat_Actions.evaluate_proximity_statuses(self.dm_core.world, "wolf", "on_action")
        self.assertNotIn("shaken", self.dm_core.entities["wolf"].get("active_conditions", {}))

    def test_out_of_radius_entity_is_unaffected(self):
        self.dm_core.rules["status"][-1]["apply"]["radius"] = 0
        self.dm_core.entities["wolf_2"]["band"] = 5
        self.dm_core.entities["wolf"]["band"] = 1
        Combat_Actions.evaluate_proximity_statuses(self.dm_core.world, "wolf", "on_action")
        self.assertNotIn("shaken", self.dm_core.entities["wolf_2"].get("active_conditions", {}))

    def test_actor_not_matching_requirements_applies_nothing(self):
        self.dm_core.entities["wolf"]["subtype"] = "elemental"
        Combat_Actions.evaluate_proximity_statuses(self.dm_core.world, "wolf", "on_action")
        self.assertNotIn("shaken", self.dm_core.entities["wolf_2"].get("active_conditions", {}))

    def test_resolve_behavior_action_fires_on_action_statuses_on_a_landed_hit(self):
        # An unarmored, skill-less target, added to the live scene so evaluate_proximity_
        # statuses' own nearby-entity scan (self.scenario_entities) can see it, so the wolf's
        # bite always lands -- isolating this from opposed-roll specifics, same precedent
        # TestEntityBehavior's own test_resolve_behavior_action_strikes_back_and_applies_damage
        # already follows.
        self.dm_core.entities["target_dummy"] = {"name": "target_dummy", "max_hp": 20, "skills": {}}
        self.dm_core.scenario_entities.append("target_dummy")
        with patch("random.randint", return_value=4):
            Combat_Actions.resolve_behavior_action(self.dm_core.world, "wolf", "target_dummy")
        self.assertIn("shaken", self.dm_core.entities["target_dummy"]["active_conditions"])


class TestPersistentTerrainHazards(DMTestCase):
    """!
    @brief The [[status]] "on_round" trigger -- run_round_upkeep (DM_Status.py) now also calls
        evaluate_proximity_statuses once a round, per living scene entity, the same function
        TestProximityStatuses' own "on_action" tests already exercise. The Pathfinder
        "Persistent terrain/obstacle spells" shape (Wall of Fire): rules.toml's own
        "flame wall zone" ([[status]]) + "burning" ([[condition]]), spells.toml's own
        "flame wall" (the hazard entity) + "wall of fire" (the spell that conjures it via the
        ordinary summon mechanism).
    """

    def setUp(self):
        super().setUp()
        # Empty entities list -- naming "gladstone" explicitly here would produce a second,
        # orphaned "gladstone_2" alongside the one DMTestCase's own default scenario already
        # instanced (see TestSummoning's own precedent/comment for why); the player is
        # guaranteed present in the new location regardless (_instance_location_persistent_names).
        self._load_ad_hoc_scenario([], bands=4, enclosed=True)
        self.dm_core._instance_entities([{"name": "flame wall", "band": 1}])
        self.dm_core.scenario_entities.append("flame wall")

    def test_applies_burning_to_a_co_band_entity(self):
        Combat_Actions.evaluate_proximity_statuses(self.dm_core.world, "flame wall", "on_round")
        self.assertIn("burning", self.dm_core.entities["gladstone"]["active_conditions"])

    def test_never_applies_burning_to_the_wall_itself(self):
        Combat_Actions.evaluate_proximity_statuses(self.dm_core.world, "flame wall", "on_round")
        self.assertNotIn("burning", self.dm_core.entities["flame wall"].get("active_conditions", {}))

    def test_an_entity_outside_the_walls_band_is_unaffected(self):
        self.dm_core.entities["gladstone"]["band"] = 3
        Combat_Actions.evaluate_proximity_statuses(self.dm_core.world, "flame wall", "on_round")
        self.assertNotIn("burning", self.dm_core.entities["gladstone"].get("active_conditions", {}))

    def test_burning_lapses_once_the_entity_leaves_and_is_not_refreshed(self):
        # "burning" is authored duration = "rounds"/length = 1 specifically so it only ever
        # outlasts the round it was granted in -- stepping out before the ordinary
        # tick_condition_durations sweep runs again means it's never refreshed, so it lapses on
        # its own rather than lingering as a debuff for having merely walked through once.
        Combat_Actions.evaluate_proximity_statuses(self.dm_core.world, "flame wall", "on_round")
        self.assertIn("burning", self.dm_core.entities["gladstone"]["active_conditions"])
        self.dm_core.entities["gladstone"]["band"] = 3
        Combat_Resolution.tick_condition_durations(WorldContext(entities=self.dm_core.entities, event_bus=self.event_bus), "gladstone", "rounds")
        Combat_Actions.evaluate_proximity_statuses(self.dm_core.world, "flame wall", "on_round")
        self.assertNotIn("burning", self.dm_core.entities["gladstone"].get("active_conditions", {}))

    def test_run_round_upkeep_deals_real_damage_over_two_rounds_while_standing_in_the_fire(self):
        # "burning"'s own upkeep_damage (dice=2, pips=0) always rolls at least 2, so two full
        # rounds standing in it guarantees at least one real damage tick regardless of which
        # order scenario_entities happens to process gladstone/the wall in this round.
        start_hp = Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone")
        self.dm_core.run_round_upkeep()
        self.dm_core.run_round_upkeep()
        self.assertLess(Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone"), start_hp)

    def test_summon_places_a_flame_wall_like_any_other_summoned_entity(self):
        # _summon_creature (DM_Summoning.py) never cared what kind of entity it was placing --
        # an inanimate hazard works exactly like "summon spectral wolf"'s own ally creature.
        name = self.dm_core._summon_creature({"name": "flame wall", "duration": 5})
        self.assertEqual(name, "flame wall_2")
        self.assertIn("flame wall_2", self.dm_core.scenario_entities)
        self.assertEqual(self.dm_core.entities["flame wall_2"]["summon_expires_in"], 5)

    def test_a_summoned_flame_wall_expires_and_is_removed_after_its_duration(self):
        self.dm_core._summon_creature({"name": "flame wall", "duration": 2})
        self.dm_core.run_round_upkeep()
        self.assertIn("flame wall_2", self.dm_core.scenario_entities)
        self.dm_core.run_round_upkeep()
        self.assertNotIn("flame wall_2", self.dm_core.scenario_entities)

    def test_flame_wall_cannot_be_chopped_down_by_an_ordinary_weapon(self):
        # current_target has no is_hostile gate (items.toml's dart trap already proves a
        # non-hostile object can be targeted/rolled against), so without immunity_tags = ["any"]
        # a player could just attack and destroy the hazard for free. A generic weapon hit
        # (any damage_tags at all) should net zero damage against it.
        sword_swing = {"damage_value": {"dice": 3, "pips": 0, "bonus": 0}, "damage_tags": ["slashing"]}
        result = Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "flame wall", sword_swing)
        self.assertEqual(result["net_damage"], 0)
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "flame wall"), 20)


class TestDelayedTriggeredMagic(DMTestCase):
    """!
    @brief The [[status]] "on_arrival" trigger + its own optional "self_dismiss" field --
        evaluate_proximity_statuses (DM_Status.py), fired once per scene entry by DM_Rules.py's
        own _evaluate_arrival_statuses (called from _enter_location and enter_room). The
        Pathfinder "Delayed/triggered magic" shape (Glyph of Warding): items.toml's own
        "warding glyph" (the hazard entity, seeded with [entity.conditions.armed]) + rules.toml's
        own "warding glyph shock" ([[status]], self_dismiss = "armed" -- spends itself the first
        time it actually catches someone).
    """

    def setUp(self):
        super().setUp()
        # Empty entities list -- see TestPersistentTerrainHazards' own setUp comment for why
        # "gladstone" is never named explicitly here.
        self._load_ad_hoc_scenario([], bands=4, enclosed=True)
        self.dm_core._instance_entities([{"name": "warding glyph", "band": 1}])
        self.dm_core.scenario_entities.append("warding glyph")

    def test_applies_shaken_to_a_co_band_entity(self):
        self.dm_core._evaluate_arrival_statuses()
        self.assertIn("shaken", self.dm_core.entities["gladstone"]["active_conditions"])

    def test_never_applies_shaken_to_the_glyph_itself(self):
        self.dm_core._evaluate_arrival_statuses()
        self.assertNotIn("shaken", self.dm_core.entities["warding glyph"].get("active_conditions", {}))

    def test_an_entity_outside_the_glyphs_band_is_unaffected(self):
        self.dm_core.entities["gladstone"]["band"] = 3
        self.dm_core._evaluate_arrival_statuses()
        self.assertNotIn("shaken", self.dm_core.entities["gladstone"].get("active_conditions", {}))

    def test_self_dismiss_spends_the_glyph_the_first_time_it_catches_someone(self):
        self.assertTrue(Combat_Resolution.has_condition(self.dm_core.world, "warding glyph", "armed"))
        self.dm_core._evaluate_arrival_statuses()
        self.assertFalse(Combat_Resolution.has_condition(self.dm_core.world, "warding glyph", "armed"))

    def test_a_spent_glyph_never_triggers_again(self):
        self.dm_core._evaluate_arrival_statuses()  # spends it -- gladstone is shaken once
        Combat_Resolution.dismiss_condition(self.dm_core.world, "gladstone", "shaken")
        self.dm_core._evaluate_arrival_statuses()  # armed is gone -- requirements no longer match
        self.assertNotIn("shaken", self.dm_core.entities["gladstone"].get("active_conditions", {}))

    def test_self_dismiss_does_not_fire_if_nobody_was_in_range(self):
        # Nobody actually caught by the glyph this pass -- it stays armed for a future arrival,
        # rather than being wasted on an empty room.
        self.dm_core.entities["gladstone"]["band"] = 3
        self.dm_core._evaluate_arrival_statuses()
        self.assertTrue(Combat_Resolution.has_condition(self.dm_core.world, "warding glyph", "armed"))

    def test_a_freshly_applied_upkeep_damage_condition_is_resolved_immediately(self):
        # "warding glyph blast" applies "burning" (upkeep_damage), which would otherwise just
        # sit inert -- apply_round_upkeep is normally only ever called from an actual combat
        # round or a completed rest, neither guaranteed to happen soon (or at all) after simply
        # walking into a room. _evaluate_arrival_statuses resolves one implicit round of upkeep
        # immediately instead, which is what makes a real damage-dealing "Blast" glyph shape
        # possible by reusing "burning" directly, no new field needed.
        start_hp = Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone")
        self.dm_core._evaluate_arrival_statuses()
        self.assertLess(Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone"), start_hp)

    def test_a_one_round_blast_condition_is_dismissed_in_the_same_call(self):
        # "burning" is authored duration = "rounds"/length = 1 specifically so the same implicit
        # upkeep tick that deals its damage also ticks it straight to 0 and dismisses it -- an
        # instant blast, not a lingering fire.
        self.dm_core._evaluate_arrival_statuses()
        self.assertNotIn("burning", self.dm_core.entities["gladstone"].get("active_conditions", {}))

    def test_a_non_upkeep_condition_is_unaffected_by_the_immediate_upkeep_resolution(self):
        # "shaken" carries no upkeep_damage/upkeep_heal of its own -- resolving one implicit
        # round of upkeep for it is a harmless no-op, it still just sits active for its own
        # authored length the ordinary way.
        self.dm_core._evaluate_arrival_statuses()
        self.assertIn("shaken", self.dm_core.entities["gladstone"]["active_conditions"])


class TestDelayedTriggeredMagicWiring(DMTestCase):
    """!
    @brief Proves the real wiring (_enter_location -> _evaluate_arrival_statuses), not just the
        underlying evaluate_proximity_statuses call TestDelayedTriggeredMagic's own tests
        already exercise -- a dedicated class/setUp so the glyph is placed directly in the ad
        hoc location's own entities list, present the moment load_scenario() itself calls
        _enter_location, rather than instanced mid-scene afterward the way that class's shared
        setUp does for its own more granular tests.
    """

    def setUp(self):
        super().setUp()
        self._load_ad_hoc_scenario([{"name": "warding glyph", "band": 1}], bands=4, enclosed=True)

    def test_entering_a_location_with_the_glyph_already_present_triggers_it(self):
        self.assertIn("shaken", self.dm_core.entities["gladstone"]["active_conditions"])
        self.assertFalse(Combat_Resolution.has_condition(self.dm_core.world, "warding glyph", "armed"))


class TestConcealment(DMTestCase):
    """!
    @brief miss_chance (a [[condition]] field) + get_concealment (Combat_Resolution.py) -- a
        successful attack roll can still miss outright against a concealed/invisible defender,
        unless the attacker's own ability authors ignores_concealment.
    """

    def setUp(self):
        super().setUp()
        self.dm_core.rules.setdefault("condition", []).append({"name": "invisible", "miss_chance": 50})
        # dice=0 on the attacker's own skill means roll_dice consumes zero random.randint
        # calls for the roll itself (an empty range) -- the only randint call left to control
        # is the concealment check, so patch("random.randint", return_value=...) unambiguously
        # targets just that roll.
        self.dm_core.entities["test_attacker"] = {"name": "test_attacker", "skills": {"blades": {"dice": 0, "pips": 5}}}
        self.dm_core.entities["test_defender"] = {"name": "test_defender", "skills": {}}
        Combat_Resolution.apply_condition(self.dm_core.world, "test_defender", "invisible", duration="permanent", dismiss="")

    def test_get_concealment_reads_the_active_conditions_own_miss_chance(self):
        self.assertEqual(Combat_Resolution.get_concealment(self.dm_core.world, "test_defender"), 50)

    def test_get_concealment_is_zero_with_no_matching_condition(self):
        self.assertEqual(Combat_Resolution.get_concealment(self.dm_core.world, "test_attacker"), 0)

    def test_a_roll_under_the_miss_chance_forces_an_otherwise_successful_hit_to_miss(self):
        with patch("random.randint", return_value=50):
            result = Combat_Resolution.resolve_opposed_action(self.dm_core.world, "test_attacker", "blades", "test_defender")
        self.assertFalse(result["success"])
        self.assertTrue(result["concealed_miss"])

    def test_a_roll_over_the_miss_chance_leaves_the_hit_untouched(self):
        with patch("random.randint", return_value=51):
            result = Combat_Resolution.resolve_opposed_action(self.dm_core.world, "test_attacker", "blades", "test_defender")
        self.assertTrue(result["success"])
        self.assertNotIn("concealed_miss", result)

    def test_ignores_concealment_bypasses_the_check_entirely(self):
        ability = {"skill": "blades", "ignores_concealment": True}
        with patch("random.randint", return_value=1):  # would otherwise definitely trigger a miss
            result = Combat_Resolution.resolve_opposed_action(self.dm_core.world, "test_attacker", "blades", "test_defender", ability=ability)
        self.assertTrue(result["success"])


class TestStatDrain(DMTestCase):
    """!
    @brief drain (a [[condition]] field) + apply_condition/dismiss_condition
        (Combat_Resolution.py) -- a permanent base-stat mutation, distinct from the ordinary
        roll-time-only "modifier" (the Pathfinder "Energy Drained" shape).
    """

    def setUp(self):
        super().setUp()
        self.dm_core.rules.setdefault("condition", []).append(
            {"name": "energy drained", "drain": {"skill": "athletics", "dice": 1, "pips": 1}}
        )

    def test_applying_the_condition_permanently_drains_the_named_skill(self):
        # gladstone's athletics is 2D+2.
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "energy drained", duration="permanent", dismiss="")
        self.assertEqual(self.dm_core.entities["gladstone"]["skills"]["athletics"], {"dice": 1, "pips": 1})

    def test_dismissing_the_condition_restores_the_exact_drained_amount(self):
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "energy drained", duration="permanent", dismiss="")
        Combat_Resolution.dismiss_condition(self.dm_core.world, "gladstone", "energy drained")
        self.assertEqual(self.dm_core.entities["gladstone"]["skills"]["athletics"], {"dice": 2, "pips": 2})

    def test_drain_is_clamped_and_does_not_go_below_zero(self):
        self.dm_core.rules["condition"][-1]["drain"] = {"skill": "athletics", "dice": 10, "pips": 10}
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "energy drained", duration="permanent", dismiss="")
        self.assertEqual(self.dm_core.entities["gladstone"]["skills"]["athletics"], {"dice": 0, "pips": 0})
        Combat_Resolution.dismiss_condition(self.dm_core.world, "gladstone", "energy drained")
        # Restores only what was actually removed (2D+2), not the nominal 10D+10.
        self.assertEqual(self.dm_core.entities["gladstone"]["skills"]["athletics"], {"dice": 2, "pips": 2})

    def test_reapplying_an_already_active_condition_does_not_double_drain(self):
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "energy drained", duration="permanent", dismiss="")
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "energy drained", duration="permanent", dismiss="")
        self.assertEqual(self.dm_core.entities["gladstone"]["skills"]["athletics"], {"dice": 1, "pips": 1})

    def test_condition_with_no_drain_field_is_unaffected(self):
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "wounded", duration="permanent", dismiss="")
        self.assertEqual(self.dm_core.entities["gladstone"]["skills"]["athletics"], {"dice": 2, "pips": 2})


class TestFormOverride(DMTestCase):
    """!
    @brief form (a [[condition]] field) + _apply_form_override/dismiss_condition
        (Combat_Resolution.py) -- a whole stat-block swap, distinct from both the ordinary
        roll-time-only "modifier" and the permanent-but-single-skill "drain" (the Pathfinder
        Polymorph/Baleful Polymorph shape). rules.toml's own "polymorphed" (form = "coyote")
        is used directly rather than a throwaway condition, exercising the shipped content.
    """

    def test_applying_the_condition_overrides_the_form_fields(self):
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "polymorphed", duration="permanent", dismiss="")
        gladstone = self.dm_core.entities["gladstone"]
        self.assertEqual(gladstone["name"], "coyote")
        self.assertEqual(gladstone["supertype"], "creature")
        self.assertEqual(gladstone["subtype"], "animal")
        self.assertEqual(gladstone["max_hp"], 10)
        self.assertEqual(gladstone["skills"]["brawling"], {"dice": 3, "pips": 0})
        self.assertEqual([a["name"] for a in gladstone["abilities"]], ["bite"])

    def test_applying_the_condition_leaves_gear_and_current_hp_untouched(self):
        gladstone = self.dm_core.entities["gladstone"]
        original_inventory = list(gladstone["inventory"])
        original_equipped = dict(gladstone["equipped"])
        original_hp = Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone")

        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "polymorphed", duration="permanent", dismiss="")

        self.assertEqual(gladstone["inventory"], original_inventory)
        self.assertEqual(gladstone["equipped"], original_equipped)
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone"), original_hp)

    def test_dismissing_restores_the_exact_pre_transform_fields(self):
        gladstone = self.dm_core.entities["gladstone"]
        original_name = gladstone["name"]
        original_supertype = gladstone["supertype"]
        original_subtype = gladstone["subtype"]
        original_max_hp = gladstone["max_hp"]
        original_skills = copy.deepcopy(gladstone["skills"])
        original_abilities = copy.deepcopy(gladstone["abilities"])

        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "polymorphed", duration="permanent", dismiss="")
        Combat_Resolution.dismiss_condition(self.dm_core.world, "gladstone", "polymorphed")

        self.assertEqual(gladstone["name"], original_name)
        self.assertEqual(gladstone["supertype"], original_supertype)
        self.assertEqual(gladstone["subtype"], original_subtype)
        self.assertEqual(gladstone["max_hp"], original_max_hp)
        self.assertEqual(gladstone["skills"], original_skills)
        self.assertEqual(gladstone["abilities"], original_abilities)

    def test_reapplying_an_already_active_form_does_not_resnapshot(self):
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "polymorphed", duration="permanent", dismiss="")
        # Mutate the live (already-coyote) skills the way an intervening drain/damage might --
        # a re-snapshot on refresh would clobber this back to the coyote's own base value.
        self.dm_core.entities["gladstone"]["skills"]["brawling"] = {"dice": 1, "pips": 0}
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "polymorphed", duration="permanent", length=5, dismiss="")
        self.assertEqual(self.dm_core.entities["gladstone"]["skills"]["brawling"], {"dice": 1, "pips": 0})

    def test_condition_with_no_form_field_is_unaffected(self):
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "wounded", duration="permanent", dismiss="")
        self.assertEqual(self.dm_core.entities["gladstone"]["name"], "gladstone")

    def test_form_override_survives_save_and_load(self):
        slot_name = "test_form_override_round_trip_slot"
        self.addCleanup(shutil.rmtree, self.dm_core._save_slot_dir(slot_name), ignore_errors=True)
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "polymorphed", duration="permanent", dismiss="")

        self.dm_core.save_game(slot_name)
        self.dm_core.load_game(slot_name)

        gladstone = self.dm_core.entities["gladstone"]
        self.assertEqual(gladstone["name"], "coyote")
        self.assertEqual(gladstone["max_hp"], 10)
        self.assertIn("polymorphed", gladstone["active_conditions"])

        Combat_Resolution.dismiss_condition(self.dm_core.world, "gladstone", "polymorphed")
        self.assertEqual(self.dm_core.entities["gladstone"]["name"], "gladstone")

    def test_break_enchantment_cures_polymorph_by_kind(self):
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "polymorphed", duration="permanent", dismiss="")
        Combat_Resolution.dismiss_matching_conditions(self.dm_core.world, "gladstone", {"subtypes": ["transmutation"]})
        self.assertEqual(self.dm_core.entities["gladstone"]["name"], "gladstone")
        self.assertNotIn("polymorphed", self.dm_core.entities["gladstone"]["active_conditions"])


class TestPeriodicTest(DMTestCase):
    """!
    @brief periodic_test (a [[condition]] field) + tick_periodic_tests (Combat_Resolution.py) --
        a recurring self-save that starts after an optional onset delay, then repeats every
        interval until cure_after_successes consecutive passes cures it outright (the Pathfinder
        poison "Frequency 1/round" / disease "Frequency 1/day" shape, Rules/Fantasy/reference/
        pathfinder_mapping.toml's Poison/Dying-Stable-Disabled rows). gladstone's own finesse is
        3D+0, fortitude 2D+0 (characters.toml).
    """

    def setUp(self):
        super().setUp()
        self.dm_core.rules.setdefault("condition", []).append({
            "name": "test toxin",
            "periodic_test": {
                "skill": "fortitude",
                "difficulty": 12,
                "onset": {"unit": "rounds", "length": 2},
                "interval": {"unit": "rounds", "length": 1},
                "on_fail": {"drain": [{"skill": "finesse", "dice": 1, "pips": 0}]},
                "cure_after_successes": 2,
            },
        })

    def test_no_test_is_rolled_before_onset_elapses(self):
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "test toxin", duration="permanent", dismiss="")
        self._stub_roll_dice(1)  # would fail against difficulty 12 if a save were actually rolled
        self.dm_core.run_round_upkeep()  # onset: 2 -> 1, no test yet
        self.assertEqual(self.dm_core.entities["gladstone"]["skills"]["finesse"], {"dice": 3, "pips": 0})

    def test_the_first_save_rolls_the_round_onset_elapses(self):
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "test toxin", duration="permanent", dismiss="")
        self._stub_roll_dice(1)
        self.dm_core.run_round_upkeep()
        self.dm_core.run_round_upkeep()  # onset: 1 -> 0, first save rolled and fails
        self.assertEqual(self.dm_core.entities["gladstone"]["skills"]["finesse"], {"dice": 2, "pips": 0})

    def test_a_failed_save_resets_consecutive_successes(self):
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "test toxin", duration="permanent", dismiss="")
        self._stub_roll_dice(15)  # beats difficulty 12
        self.dm_core.run_round_upkeep()
        self.dm_core.run_round_upkeep()  # save #1 passes
        periodic = Combat_Resolution.get_active_conditions(self.dm_core.world, "gladstone")["test toxin"]["_periodic"]
        self.assertEqual(periodic["successes"], 1)
        self._stub_roll_dice(1)
        self.dm_core.run_round_upkeep()  # save #2 fails
        self.assertEqual(periodic["successes"], 0)
        self.assertEqual(self.dm_core.entities["gladstone"]["skills"]["finesse"], {"dice": 2, "pips": 0})

    def test_cure_after_successes_consecutive_passes_auto_dismisses(self):
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "test toxin", duration="permanent", dismiss="")
        self._stub_roll_dice(15)
        self.dm_core.run_round_upkeep()  # onset: 2 -> 1
        self.dm_core.run_round_upkeep()  # onset elapses, save #1 passes
        self.assertTrue(Combat_Resolution.has_condition(self.dm_core.world, "gladstone", "test toxin"))
        self.dm_core.run_round_upkeep()  # interval elapses, save #2 passes -- cured
        self.assertFalse(Combat_Resolution.has_condition(self.dm_core.world, "gladstone", "test toxin"))

    def test_dismissing_restores_the_total_accumulated_drain_across_multiple_failures(self):
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "test toxin", duration="permanent", dismiss="")
        self._stub_roll_dice(1)  # every save fails
        self.dm_core.run_round_upkeep()
        self.dm_core.run_round_upkeep()  # save #1 fails -- finesse 3 -> 2
        self.dm_core.run_round_upkeep()  # save #2 fails -- finesse 2 -> 1
        self.assertEqual(self.dm_core.entities["gladstone"]["skills"]["finesse"], {"dice": 1, "pips": 0})
        Combat_Resolution.dismiss_condition(self.dm_core.world, "gladstone", "test toxin")
        self.assertEqual(self.dm_core.entities["gladstone"]["skills"]["finesse"], {"dice": 3, "pips": 0})

    def test_a_days_denominated_onset_and_interval_convert_through_the_block_clock(self):
        # blocks_per_day is 3 (rules.toml) -- one day of onset is 3 blocks.
        self.dm_core.rules["condition"][-1]["periodic_test"]["onset"] = {"unit": "days", "length": 1}
        self.dm_core.rules["condition"][-1]["periodic_test"]["interval"] = {"unit": "days", "length": 1}
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "test toxin", duration="permanent", dismiss="")
        self._stub_roll_dice(1)
        self.dm_core.advance_blocks(2)
        self.assertEqual(self.dm_core.entities["gladstone"]["skills"]["finesse"], {"dice": 3, "pips": 0})
        self.dm_core.advance_blocks(1)  # 3rd block -- onset elapses, first save rolled and fails
        self.assertEqual(self.dm_core.entities["gladstone"]["skills"]["finesse"], {"dice": 2, "pips": 0})

    def test_a_condition_with_no_periodic_test_field_is_unaffected(self):
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "wounded", duration="permanent", dismiss="")
        self.dm_core.run_round_upkeep()
        self.dm_core.run_round_upkeep()
        self.assertEqual(self.dm_core.entities["gladstone"]["skills"]["finesse"], {"dice": 3, "pips": 0})


class TestForcedActionTargetOverride(DMTestCase):
    """!
    @brief override_target (a [[condition]] field) + resolve_override_target (Combat_
        Resolution.py) -- hijacks WHO an entity's turn is aimed at, folded into resolve_
        behavior_action (the Pathfinder Confused/Dominate shape).
    """

    def test_resolve_override_target_is_none_with_no_matching_condition(self):
        self.assertIsNone(Combat_Resolution.resolve_override_target(self.dm_core.world, "wolf", ["gladstone", "thane"]))

    def test_random_override_picks_from_the_given_candidates(self):
        self.dm_core.rules.setdefault("condition", []).append({"name": "confused", "override_target": "random"})
        Combat_Resolution.apply_condition(self.dm_core.world, "wolf", "confused", duration="permanent", dismiss="")
        with patch("random.choice", return_value="thane"):
            self.assertEqual(Combat_Resolution.resolve_override_target(self.dm_core.world, "wolf", ["gladstone", "thane"]), "thane")

    def test_random_override_with_no_candidates_returns_none(self):
        self.dm_core.rules.setdefault("condition", []).append({"name": "confused", "override_target": "random"})
        Combat_Resolution.apply_condition(self.dm_core.world, "wolf", "confused", duration="permanent", dismiss="")
        self.assertIsNone(Combat_Resolution.resolve_override_target(self.dm_core.world, "wolf", []))

    def test_a_literal_override_resolves_to_a_real_living_entity(self):
        self.dm_core.rules.setdefault("condition", []).append({"name": "dominated", "override_target": "thane"})
        Combat_Resolution.apply_condition(self.dm_core.world, "wolf", "dominated", duration="permanent", dismiss="")
        self.assertEqual(Combat_Resolution.resolve_override_target(self.dm_core.world, "wolf", []), "thane")

    def test_a_literal_override_naming_a_dead_entity_resolves_to_none(self):
        self.dm_core.entities["thane"]["hp"] = 0
        self.dm_core.rules.setdefault("condition", []).append({"name": "dominated", "override_target": "thane"})
        Combat_Resolution.apply_condition(self.dm_core.world, "wolf", "dominated", duration="permanent", dismiss="")
        self.assertIsNone(Combat_Resolution.resolve_override_target(self.dm_core.world, "wolf", []))

    def test_resolve_behavior_action_attacks_the_overridden_target_instead(self):
        # wolf is normally acting against "gladstone" this turn -- "dominated" redirects it to
        # attack "target_dummy" instead, with everything else (behavior/ability selection,
        # range, roll, damage) running completely unchanged.
        self.dm_core.entities["target_dummy"] = {"name": "target_dummy", "max_hp": 20, "skills": {}}
        self.dm_core.scenario_entities.append("target_dummy")
        self.dm_core.rules.setdefault("condition", []).append({"name": "dominated", "override_target": "target_dummy"})
        Combat_Resolution.apply_condition(self.dm_core.world, "wolf", "dominated", duration="permanent", dismiss="")
        with patch("random.randint", return_value=4):
            result = Combat_Actions.resolve_behavior_action(self.dm_core.world, "wolf", "gladstone")
        assert result is not None
        self.assertTrue(result.success)
        self.assertEqual(result.defender, "target_dummy")
        self.assertLess(Combat_Resolution.get_current_hp(self.dm_core.world, "target_dummy"), 20)
        self.assertEqual(
            Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone"), self.dm_core.entities["gladstone"]["max_hp"],
        )


class TestSkillGroups(DMTestCase):
    """!
    @brief [[skill_group]] (rules.toml) + get_skill_group_members (Combat_Resolution.py) --
        lets a [[condition]]'s own applies_to or an item's own equipped_skill_bonus.skill
        address a whole cluster of skills by one shared name, standing in for the attribute
        layer this engine deliberately doesn't have.
    """

    def test_a_defined_group_name_expands_to_its_member_skills(self):
        # rules.toml's own shipped "strength" group.
        self.assertEqual(
            Combat_Resolution.get_skill_group_members(self.dm_core.world, "strength"),
            ["strength", "athletics", "blades", "axes", "brawling"],
        )

    def test_an_undefined_name_passes_through_as_a_literal_skill(self):
        self.assertEqual(Combat_Resolution.get_skill_group_members(self.dm_core.world, "observation"), ["observation"])

    def test_a_list_mixing_a_group_and_a_literal_skill_expands_each_entry(self):
        self.assertEqual(
            Combat_Resolution.get_skill_group_members(self.dm_core.world, ["dexterity", "observation"]),
            ["finesse", "acrobatics", "dodge", "escape", "observation"],
        )

    def test_condition_applies_to_a_group_penalizes_every_member_skill(self):
        self.dm_core.rules.setdefault("condition", []).append(
            {"name": "weakened", "modifier": {"dice": -1, "pips": 0, "bonus": 0}, "applies_to": ["strength"]}
        )
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "weakened", duration="permanent", dismiss="")
        # "blades" is in the "strength" group -- penalized; "observation" isn't -- untouched.
        self.assertEqual(
            Combat_Resolution.get_condition_modifier(self.dm_core.world, "gladstone", "blades"),
            {"dice": -1, "pips": 0, "bonus": 0},
        )
        self.assertEqual(
            Combat_Resolution.get_condition_modifier(self.dm_core.world, "gladstone", "observation"),
            {"dice": 0, "pips": 0, "bonus": 0},
        )

    def test_equipped_skill_bonus_naming_a_group_buffs_every_member_skill(self):
        self.dm_core.entities["belt of giant strength"] = {
            "name": "belt of giant strength", "equipped_skill_bonus": {"skill": "strength", "dice": 1, "pips": 0},
        }
        self.dm_core.entities["gladstone"]["inventory"].append("belt of giant strength")
        self.dm_core.entities["gladstone"]["equipped"]["belt"] = "belt of giant strength"
        # "blades"/"athletics" are in the "strength" group -- buffed; "observation" isn't.
        self.assertEqual(
            Combat_Resolution.get_equipped_skill_bonus(self.dm_core.world, "gladstone", "blades"),
            {"dice": 1, "pips": 0},
        )
        self.assertEqual(
            Combat_Resolution.get_equipped_skill_bonus(self.dm_core.world, "gladstone", "athletics"),
            {"dice": 1, "pips": 0},
        )
        self.assertEqual(
            Combat_Resolution.get_equipped_skill_bonus(self.dm_core.world, "gladstone", "observation"),
            {"dice": 0, "pips": 0},
        )


class TestActionPrevented(DMTestCase):
    """!
    @brief is_action_prevented (DM_Status.py) and rules.toml's own "pinned" -- the first
        [[condition]] to author prevents_action = true, closing the gap this engine's own
        flat-roll-modifier condition system used to have against Pathfinder's real "pinned"
        (which stops a character from acting at all, not just penalizes the roll).
    """

    def test_is_action_prevented_true_once_a_prevents_action_condition_is_active(self):
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "pinned", duration="permanent", dismiss="")
        self.assertTrue(Combat_Actions.is_action_prevented(self.dm_core.world, "gladstone"))

    def test_is_action_prevented_false_for_an_ordinary_dice_penalty_condition(self):
        # "wounded" is a real [[condition]] entry (a modifier), but never authors
        # prevents_action -- only carrying a penalty must not also block acting outright.
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "wounded", duration="permanent", dismiss="")
        self.assertFalse(Combat_Actions.is_action_prevented(self.dm_core.world, "gladstone"))

    def test_is_action_prevented_false_with_no_conditions_at_all(self):
        self.assertFalse(Combat_Actions.is_action_prevented(self.dm_core.world, "gladstone"))

    def test_players_own_turn_is_denied_outright_with_no_roll_while_pinned(self):
        round_events = self._capture("round_resolved")
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "pinned", duration="permanent", dismiss="")

        self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "blades"}], "input": "I attack the wolf"})

        action = round_events[-1]["actions"][0]
        self.assertIsInstance(action, ActionPreventedOutcome)
        self.assertEqual(action.skill, "blades")

    def test_resolve_behavior_action_returns_none_when_the_actor_is_pinned(self):
        # wolf's own [[entity.behavior]] would otherwise resolve "bite" against gladstone --
        # pinned pre-empts that entirely, the same "doesn't act" outcome an entity with no
        # matching behavior at all already gets.
        Combat_Resolution.apply_condition(self.dm_core.world, "wolf", "pinned", duration="permanent", dismiss="")
        self.assertIsNone(Combat_Actions.resolve_behavior_action(self.dm_core.world, "wolf", "gladstone"))

    def test_pin_maneuver_actually_stops_its_target_from_acting_next(self):
        # End-to-end: pin lands on an already-grappled target, and the resulting "pinned"
        # condition genuinely prevents that target's own next action, not just a penalized one.
        self.dm_core.entities["wolf"]["active_conditions"] = {
            "grappled": {"duration": "permanent", "dismiss": None},
        }
        pin = self.dm_core.entities["pin"]
        result = RolledOutcome(entity="gladstone", skill="athletics", roll=15, difficulty=5, success=True)
        apply_effects(self.dm_core, result, "athletics", None, pin, "wolf", via_test=False)

        self.assertIn("pinned", self.dm_core.entities["wolf"]["active_conditions"])
        self.assertTrue(Combat_Actions.is_action_prevented(self.dm_core.world, "wolf"))
        self.assertIsNone(Combat_Actions.resolve_behavior_action(self.dm_core.world, "wolf", "gladstone"))


if __name__ == "__main__":
    unittest.main()
