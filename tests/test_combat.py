import json
import os
import random
import shutil
import unittest
from unittest.mock import patch
import resolution.Combat_Resolution as Combat_Resolution
from resolution.Challenge_Rating import (
    calculate_challenge_rating,
    calculate_party_challenge_rating,
    skill_rating,
)
from resolution.Combat_Simulator import best_offense_skill, run_matchup, simulate_fight
from dm.DM_ActionOutcome import (
    DamageEffect,
    LanguageBarrierOutcome,
    OutOfRangeOutcome,
    RolledOutcome,
    SummonEffect,
    TeleportEffect,
)
from dm.DM_Core import DMCore
from dm.DM_Social import TALK_ATTITUDE_DRIFT_CAP, ACTION_ATTITUDE_DRIFT_CAP
from resolution.Combat_Actions import CombatHooks
from tests.event_contract import ValidatingEventBus
from resolution.World_Context import WorldContext
import resolution.Ability_Effects as Ability_Effects
import resolution.Combat_Actions as Combat_Actions
from resolution.Action_Target import ASSAULT_CONFIRM_SCORE, ActionTargetScene, resolve_action_target
from tests.support import (
    apply_effects,
    DMTestCase,
    REAL_UNTARGETED_DIFFICULTY,
    scripted_llm,
)
# Module-level hooks start the shared patches (tests/support.py) for every test in this file.
from tests.support import setUpModule, tearDownModule  # noqa: F401


class TestOpposedResolution(DMTestCase):
    def test_highest_value_opposing_skill_is_used(self):
        # blades opposes = ['dodge', 'blades', 'brawling', 'axes', 'polearms']
        # 'dodge' is listed first, but 'brawling' rates higher (5*3=15 vs 2*3=6),
        # so 'brawling' must be the one chosen and rolled.
        self.dm_core.entities["test_defender"] = {
            "name": "test_defender",
            "skills": {
                "dodge": {"dice": 2, "pips": 0},
                "brawling": {"dice": 5, "pips": 0},
            },
        }

        chosen = Combat_Resolution.get_opposing_skill(self.dm_core.world, "blades", "test_defender")
        self.assertEqual(chosen, "brawling")

        result = Combat_Resolution.resolve_opposed_action(self.dm_core.world, "gladstone", "blades", "test_defender")
        self.assertEqual(result["opposing_skill"], "brawling")
        self.assertEqual(result["defender"], "test_defender")
        # 5 dice + 0 pips can only roll between 5 and 30
        self.assertGreaterEqual(result["difficulty"], 5)
        self.assertLessEqual(result["difficulty"], 30)

    def test_pips_count_toward_the_rating(self):
        # 'dodge' has fewer dice (2) than 'brawling' (3), but +3 pips bumps its
        # rating past brawling's: dodge = 2*3+3=9, brawling = 3*3+0=9... so add one
        # more pip to make dodge strictly higher and confirm pips are honored.
        self.dm_core.entities["test_defender"] = {
            "name": "test_defender",
            "skills": {
                "dodge": {"dice": 2, "pips": 4},
                "brawling": {"dice": 3, "pips": 0},
            },
        }

        chosen = Combat_Resolution.get_opposing_skill(self.dm_core.world, "blades", "test_defender")
        self.assertEqual(chosen, "dodge")


class TestDamageCalculation(DMTestCase):
    def test_bonus_resolves_flat_number(self):
        self.assertEqual(Combat_Resolution.resolve_bonus(self.dm_core.world, "gladstone", 5), 5)


    @patch("random.randint", return_value=4)
    def test_damage_value_rolls_dice_and_adds_bonus(self, mock_randint):
        # 2 dice @ 4 each + 1 pip + strength_damage bonus (1) = 10
        total = Combat_Resolution.resolve_damage_value(self.dm_core.world, 
            "gladstone", {"dice": 2, "pips": 1, "bonus": "user.strength_damage"}
        )
        self.assertEqual(total, 10)


    def test_apply_damage_subtracts_and_floors_at_zero(self):
        Combat_Resolution.apply_damage(self.dm_core.world, "gladstone", 10)
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone"), 26)
        Combat_Resolution.apply_damage(self.dm_core.world, "gladstone", 1000)
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "gladstone"), 0)


    @patch("random.randint", return_value=3)
    def test_calculate_damage_reduced_by_matching_armor(self, mock_randint):
        # Punch: 0 dice + strength_damage bonus (1), bludgeoning - chain mail resists bludgeoning (2 dice @ 3 each = 6).
        punch = {"damage_value": {"dice": 0, "pips": 0, "bonus": "user.strength_damage"}, "damage_tags": ["bludgeoning"]}
        result = Combat_Actions.calculate_damage(self.dm_core.world, "wolf", "gladstone", punch)

        self.assertEqual(result["raw_damage"], 1)
        self.assertEqual(result["reduction"], 6)
        self.assertEqual(result["net_damage"], 0)
        self.assertEqual(result["remaining_hp"], 36)

    def test_fire_elemental_is_immune_to_fire_tag(self):
        self.assertTrue(Combat_Resolution.is_immune_to(self.dm_core.world, "fire elemental", ["fire"]))
        self.assertFalse(Combat_Resolution.is_immune_to(self.dm_core.world, "fire elemental", ["slashing"]))
        # Immunity is a hard tag match, not a rolled amount -- an entity with no
        # immunity_tags at all (gladstone) is never immune to anything.
        self.assertFalse(Combat_Resolution.is_immune_to(self.dm_core.world, "gladstone", ["fire"]))

    def test_immunity_tags_any_is_a_wildcard_matching_every_damage_tag(self):
        # "any" (is_immune_to, Combat_Resolution.py) is immune to every damage_tags value,
        # present or future -- no need to enumerate each physical/energy type by hand, or
        # revisit this entity's own list when a new damage_tags value is invented elsewhere.
        self.dm_core.entities["target_dummy"] = {"name": "target_dummy", "immunity_tags": ["any"]}
        self.assertTrue(Combat_Resolution.is_immune_to(self.dm_core.world, "target_dummy", ["fire"]))
        self.assertTrue(Combat_Resolution.is_immune_to(self.dm_core.world, "target_dummy", ["slashing"]))
        self.assertTrue(Combat_Resolution.is_immune_to(self.dm_core.world, "target_dummy", ["a damage type nobody has invented yet"]))
        # Also immune to a tagless attack -- an ordinary enumerated immunity_tags list could
        # never match an empty damage_tags, since there's nothing in it to compare against.
        self.assertTrue(Combat_Resolution.is_immune_to(self.dm_core.world, "target_dummy", []))

    def test_damage_tags_any_is_unpreventable_except_by_immunity_tags_any(self):
        # damage_tags = ["any"] needs no special-casing of its own: no real resistance_tags/
        # armor_tags/vulnerability_tags list would ever legitimately contain the literal string
        # "any", so it already skips every ordinary defender's reduction/vulnerability by
        # construction -- only a defender's own immunity_tags = ["any"] can stop it.
        self.dm_core.entities["armored_dummy"] = {
            "name": "armored_dummy", "resistance_value": {"dice": 5, "pips": 0},
            "resistance_tags": ["fire", "slashing", "piercing", "bludgeoning"],
        }
        self.assertEqual(Combat_Resolution.get_damage_reduction(self.dm_core.world, "armored_dummy", ["any"]), 0)
        self.dm_core.entities["immune_dummy"] = {"name": "immune_dummy", "immunity_tags": ["any"]}
        self.assertTrue(Combat_Resolution.is_immune_to(self.dm_core.world, "immune_dummy", ["any"]))


    @patch("random.randint", return_value=4)
    def test_immunity_overrides_vulnerability_when_both_tags_present(self, mock_randint):
        # An attack tagged both "fire" (immune) and "water" (vulnerable) should still be fully
        # negated -- immunity is an absolute block that wins outright, not just a bigger number
        # in the same tug-of-war as resistance/vulnerability.
        hybrid_attack = {"damage_value": {"dice": 4, "pips": 0, "bonus": 0}, "damage_tags": ["fire", "water"]}
        result = Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "fire elemental", hybrid_attack)

        self.assertEqual(result["vulnerability_bonus"], 0)
        self.assertEqual(result["net_damage"], 0)


    @patch("random.randint", return_value=3)
    def test_resistance_bypass_tag_skips_the_defenders_own_resistance(self, mock_randint):
        # fire elemental resists ["physical", "piercing", "bludgeoning", "slashing"] at 2D --
        # opt it into a Pathfinder "DR/magic" shape and confirm a magic-tagged hit skips that
        # reduction entirely, while an otherwise-identical mundane hit still gets reduced.
        self.dm_core.entities["fire elemental"]["resistance_bypass_tags"] = ["magic"]

        self.assertEqual(Combat_Resolution.get_damage_reduction(self.dm_core.world, "fire elemental", ["slashing"]), 6)
        self.assertEqual(Combat_Resolution.get_damage_reduction(self.dm_core.world, "fire elemental", ["slashing", "magic"]), 0)


    @patch("random.randint", return_value=3)
    def test_armor_bypass_tag_skips_that_items_own_reduction(self, mock_randint):
        # gladstone has no innate resistance of his own -- chain mail's armor_value/armor_tags
        # is the only source of reduction here, so this isolates the item-side bypass path from
        # get_damage_reduction's own resistance_bypass_tags branch above.
        self.dm_core.entities["chain mail"]["armor_bypass_tags"] = ["magic"]

        self.assertEqual(Combat_Resolution.get_damage_reduction(self.dm_core.world, "gladstone", ["bludgeoning"]), 6)
        self.assertEqual(Combat_Resolution.get_damage_reduction(self.dm_core.world, "gladstone", ["bludgeoning", "magic"]), 0)


    @patch("random.randint", return_value=3)
    def test_wraith_resists_ordinary_weapons_but_silver_bypasses_it(self, mock_randint):
        # creatures.toml's "wraith" is the shipped resistance_bypass_tags example (DR/silver).
        # An ordinary slashing hit is reduced (3D @ 3 each = 9); the same hit tagged "silver"
        # bypasses that reduction entirely, even though "slashing" still matches resistance_tags.
        self.assertEqual(Combat_Resolution.get_damage_reduction(self.dm_core.world, "wraith", ["slashing"]), 9)
        self.assertEqual(Combat_Resolution.get_damage_reduction(self.dm_core.world, "wraith", ["slashing", "silver"]), 0)


    def test_landing_a_hit_nudges_the_defenders_combat_attitude(self):
        # arena's wolf normally has no [entity.attitudes] table at all (unconditionally
        # hostile -- see is_hostile), so it's given one here just for this test; the "combat_hit"
        # nudge (DM_Social.py's nudge_attitude_from_event, wired from _apply_damage_if_hit) is
        # scaled by net_damage / max_hp, not a flat per-swing amount.
        self.dm_core.entities["wolf"]["attitudes"] = {"default": [0, 0, 0]}
        result = RolledOutcome(entity="gladstone", skill="melee", roll=0, difficulty=0, success=True)
        ability = {"damage_value": {"dice": 0, "pips": 0, "bonus": 5}, "damage_tags": []}

        apply_effects(self.dm_core, result, "melee", None, ability, "wolf", via_test=False)

        self.assertEqual(result.effects[0].net_damage, 5)
        magnitude = 5 / self.dm_core.entities["wolf"]["max_hp"]
        disposition, threat = (
            self.dm_core.get_attitude("wolf", self.dm_core.player_name)[axis] for axis in (0, 1)
        )
        self.assertAlmostEqual(disposition, -20 * magnitude)
        self.assertAlmostEqual(threat, -15 * magnitude)

    def test_landing_a_hit_bonds_other_entities_hostile_to_the_same_target(self):
        # "Bonds made on the battlefield" (DM_Core.py's _nudge_shared_enemy_bonds) -- an
        # onlooker who already considers "wolf" an enemy (a name-override disposition <= -100
        # toward it specifically, not just a generic hostile-to-everyone default) warms up
        # toward the player when the player hits it, scaled by the same magnitude as the
        # target's own "combat_hit" nudge. thane is arena's real ally entity (is_party = true),
        # already present in scenario_entities and alive.
        self.dm_core.entities["thane"]["attitudes"] = {
            "default": [0, 0, 0],
            "name": [{"wolf": [-100, 0, 0]}],
        }
        result = RolledOutcome(entity="gladstone", skill="melee", roll=0, difficulty=0, success=True)
        ability = {"damage_value": {"dice": 0, "pips": 0, "bonus": 5}, "damage_tags": []}

        apply_effects(self.dm_core, result, "melee", None, ability, "wolf", via_test=False)

        magnitude = result.effects[0].net_damage / self.dm_core.entities["wolf"]["max_hp"]
        disposition = self.dm_core.get_attitude("thane", self.dm_core.player_name)[0]
        self.assertAlmostEqual(disposition, 5 * magnitude)

    def test_shared_enemy_bond_skips_an_observer_thats_neutral_to_the_target(self):
        # thane has real attitude data but no particular opinion of "wolf" specifically (falls
        # back to its own all-neutral default) -- not hostile toward it, so no bond forms.
        self.dm_core.entities["thane"]["attitudes"] = {"default": [0, 0, 0]}
        result = RolledOutcome(entity="gladstone", skill="melee", roll=0, difficulty=0, success=True)
        ability = {"damage_value": {"dice": 0, "pips": 0, "bonus": 5}, "damage_tags": []}

        apply_effects(self.dm_core, result, "melee", None, ability, "wolf", via_test=False)

        self.assertNotIn("action_attitude_deltas", self.dm_core.entities["thane"])


class TestResolveTargets(DMTestCase):
    """!
    @brief Combat_Actions.py's resolve_targets -- the {number, aoe, side} multi-target/area-of-
        effect mechanic (entity_schema.toml's "targets" field). Arena's default layout puts
        gladstone/thane/wolf/wolf_2 all at band 1 (wolf_2 -- see DM_Rules.py's own
        occurrence-count suffixing), so aoe-radius tests mutate "band" directly.
    """

    def test_no_targets_table_is_just_target_name(self):
        # Every ordinary weapon/most spells -- unchanged single-target behavior.
        ability = {"damage_value": {"dice": 1, "pips": 0, "bonus": 0}, "damage_tags": []}
        self.assertEqual(Combat_Actions.resolve_targets(self.dm_core.world, "gladstone", "wolf", ability), ["wolf"])

    def test_untargeted_ability_resolves_to_a_single_none(self):
        # An ability with no current_target at all still runs its own on_pass/on_fail exactly
        # once, against no one -- resolve_targets never widens a None target.
        ability = {"targets": {"number": 3, "aoe": 5, "side": "all"}}
        self.assertEqual(Combat_Actions.resolve_targets(self.dm_core.world, "gladstone", None, ability), [None])

    def test_side_defaults_to_enemies_and_target_is_always_first(self):
        # cleave's own shape: {number = 3, aoe = 0} -- every other hostile sharing wolf's own
        # band (wolf_2), but not thane (an ally).
        ability = {"targets": {"number": 3, "aoe": 0}}
        result = Combat_Actions.resolve_targets(self.dm_core.world, "gladstone", "wolf", ability)
        self.assertEqual(result[0], "wolf")
        self.assertIn("wolf_2", result)
        self.assertNotIn("thane", result)

    def test_number_caps_the_combined_list(self):
        ability = {"targets": {"number": 1, "aoe": 0}}
        self.assertEqual(Combat_Actions.resolve_targets(self.dm_core.world, "gladstone", "wolf", ability), ["wolf"])

    def test_side_all_ignores_hostility(self):
        # fireball's own shape -- an indiscriminate blast catches an ally (and even the caster
        # themselves, arena's whole roster sharing band 1) standing in it too.
        ability = {"targets": {"number": 0, "aoe": 0, "side": "all"}}
        result = Combat_Actions.resolve_targets(self.dm_core.world, "gladstone", "wolf", ability)
        self.assertCountEqual(result, ["wolf", "wolf_2", "thane", "gladstone"])

    def test_side_allies_excludes_hostiles(self):
        # A Pathfinder-style channeling that only touches allies.
        ability = {"targets": {"number": 0, "aoe": 0, "side": "allies"}}
        result = Combat_Actions.resolve_targets(self.dm_core.world, "gladstone", "thane", ability)
        self.assertIn("thane", result)
        self.assertNotIn("wolf", result)
        self.assertNotIn("wolf_2", result)

    def test_aoe_radius_excludes_entities_out_of_band_range(self):
        self.dm_core.entities["wolf_2"]["band"] = 4
        ability = {"targets": {"number": 0, "aoe": 1, "side": "all"}}
        result = Combat_Actions.resolve_targets(self.dm_core.world, "gladstone", "wolf", ability)
        self.assertNotIn("wolf_2", result)

    def test_aoe_radius_includes_entities_within_range(self):
        self.dm_core.entities["wolf_2"]["band"] = 2
        ability = {"targets": {"number": 0, "aoe": 1, "side": "all"}}
        result = Combat_Actions.resolve_targets(self.dm_core.world, "gladstone", "wolf", ability)
        self.assertIn("wolf_2", result)

    def test_dead_entities_are_never_included(self):
        self.dm_core.entities["wolf_2"]["hp"] = 0
        ability = {"targets": {"number": 0, "aoe": 0, "side": "all"}}
        result = Combat_Actions.resolve_targets(self.dm_core.world, "gladstone", "wolf", ability)
        self.assertNotIn("wolf_2", result)

    @patch("random.randint", return_value=3)
    def test_apply_damage_if_hit_deals_damage_to_every_resolved_target(self, mock_randint):
        ability = {
            "damage_value": {"dice": 0, "pips": 0, "bonus": 5}, "damage_tags": [],
            "targets": {"number": 0, "aoe": 0, "side": "enemies"},
        }
        result = RolledOutcome(entity="gladstone", skill="melee", roll=0, difficulty=0, success=True)

        apply_effects(self.dm_core, result, "melee", None, ability, "wolf", via_test=False)

        defenders = {effect.defender for effect in result.effects}
        self.assertEqual(defenders, {"wolf", "wolf_2"})
        self.assertNotIn("thane", defenders)

    def test_side_self_always_resolves_to_the_caster_ignoring_target_and_aoe(self):
        # A personal ward standing in for a hostile current_target -- side = "self" must never
        # actually hit "wolf", regardless of aoe/number, or spill onto thane despite sharing
        # gladstone's own band.
        ability = {"targets": {"number": 5, "aoe": 5, "side": "self"}}
        self.assertEqual(Combat_Actions.resolve_targets(self.dm_core.world, "gladstone", "wolf", ability), ["gladstone"])

    def test_side_self_needs_no_target_at_all(self):
        ability = {"targets": {"side": "self"}}
        self.assertEqual(Combat_Actions.resolve_targets(self.dm_core.world, "gladstone", None, ability), ["gladstone"])

    @patch("random.randint", return_value=3)
    def test_apply_damage_if_hit_applies_a_self_only_ability_with_no_target(self, mock_randint):
        # target_name=None -- an ordinary damage ability would previously never even attempt
        # this (see the untargeted-ability test above); side = "self" is the one case where
        # _apply_damage_if_hit's own gate no longer requires a named target.
        ability = {
            "damage_value": {"dice": 0, "pips": 0, "bonus": 3}, "damage_tags": [],
            "targets": {"side": "self"},
        }
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=0, difficulty=0, success=True)

        apply_effects(self.dm_core, result, "arcane", None, ability, None, via_test=False)

        self.assertEqual(len(result.effects), 1)
        self.assertEqual(result.effects[0].defender, "gladstone")


class TestSaveForHalf(DMTestCase):
    """!
    @brief save_for_half (an ability field) + negates_save_for_half (an entity field, a list of
        skill names) + DM_Core.py's own _resolve_save_for_half -- the Pathfinder Reflex-half AoE
        shape. Only ever checked for a target resolve_targets widened onto, never target_name
        itself (which already resolved through the ordinary opposed roll). Arena's default
        layout puts gladstone/thane/wolf/wolf_2 all at band 1 (see TestResolveTargets) -- "wolf"
        is target_name (primary), "wolf_2" the AoE-widened secondary target. difficulty = 0
        forces a pass regardless of the roll (a 2d6 total can't go below 2). wolf_2 actually
        trains reflexes at 2D (debug.toml), not untrained -- a difficulty = 10 save is *usually*
        a fail but passes on a 2d6 of 10+ (1-in-6), so the two tests below that need a forced
        fail go through _stub_roll_dice(0) rather than relying on difficulty alone; an earlier
        version of this class assumed an untrained (0-dice, deterministic-0) defender and
        flaked at exactly that ~17% rate. negates_save_for_half is checked by literal skill
        match against the save's own "skill" (the Pathfinder Evasion trait is exactly
        ["reflexes"]), not a bare boolean, so a save_for_half effect keyed to a different skill
        is unaffected by it.
    """

    def _blast(self, difficulty):
        return {
            "damage_value": {"dice": 0, "pips": 0, "bonus": 10}, "damage_tags": [],
            "targets": {"number": 0, "aoe": 0, "side": "all"},
            "save_for_half": {"skill": "reflexes"}, "difficulty": difficulty,
        }

    def _effect_for(self, result, defender_name):
        return next(e for e in result.effects if e.defender == defender_name)

    def test_primary_target_is_never_affected_by_save_for_half(self):
        # difficulty = 0 would force a *pass* if this were ever checked against wolf -- proving
        # the primary target still always takes full, un-saved-against damage regardless.
        result = RolledOutcome(entity="gladstone", skill="melee", roll=0, difficulty=0, success=True)
        apply_effects(self.dm_core, result, "melee", None, self._blast(0), "wolf", via_test=False)
        self.assertEqual(self._effect_for(result, "wolf").net_damage, 10)

    def test_secondary_target_takes_full_damage_on_a_failed_save(self):
        self._stub_roll_dice(0)  # forces wolf_2's own 2d6 reflexes save below difficulty 10
        result = RolledOutcome(entity="gladstone", skill="melee", roll=0, difficulty=0, success=True)
        apply_effects(self.dm_core, result, "melee", None, self._blast(10), "wolf", via_test=False)
        self.assertEqual(self._effect_for(result, "wolf_2").net_damage, 10)

    def test_secondary_target_takes_half_damage_on_a_passed_save_without_evasion(self):
        result = RolledOutcome(entity="gladstone", skill="melee", roll=0, difficulty=0, success=True)
        apply_effects(self.dm_core, result, "melee", None, self._blast(0), "wolf", via_test=False)
        self.assertEqual(self._effect_for(result, "wolf_2").net_damage, 5)

    def test_secondary_target_with_evasion_takes_no_damage_on_a_passed_save(self):
        self.dm_core.entities["wolf_2"]["negates_save_for_half"] = ["reflexes"]
        result = RolledOutcome(entity="gladstone", skill="melee", roll=0, difficulty=0, success=True)
        apply_effects(self.dm_core, result, "melee", None, self._blast(0), "wolf", via_test=False)
        self.assertNotIn("wolf_2", [e.defender for e in result.effects])

    def test_evasion_does_nothing_on_a_failed_save(self):
        self._stub_roll_dice(0)  # forces wolf_2's own 2d6 reflexes save below difficulty 10
        self.dm_core.entities["wolf_2"]["negates_save_for_half"] = ["reflexes"]
        result = RolledOutcome(entity="gladstone", skill="melee", roll=0, difficulty=0, success=True)
        apply_effects(self.dm_core, result, "melee", None, self._blast(10), "wolf", via_test=False)
        self.assertEqual(self._effect_for(result, "wolf_2").net_damage, 10)

    def test_evasion_does_not_apply_to_a_save_for_half_keyed_to_a_different_skill(self):
        # Pathfinder's real Evasion is textually a Reflex-save-only trait -- a hypothetical
        # fortitude-keyed save_for_half effect (a poison cloud, say) still only ever halves for
        # an entity whose own negates_save_for_half lists "reflexes", never negates.
        self.dm_core.entities["wolf_2"]["negates_save_for_half"] = ["reflexes"]
        ability = {
            "damage_value": {"dice": 0, "pips": 0, "bonus": 10}, "damage_tags": [],
            "targets": {"number": 0, "aoe": 0, "side": "all"},
            "save_for_half": {"skill": "fortitude"}, "difficulty": 0,
        }
        result = RolledOutcome(entity="gladstone", skill="melee", roll=0, difficulty=0, success=True)
        apply_effects(self.dm_core, result, "melee", None, ability, "wolf", via_test=False)
        self.assertEqual(self._effect_for(result, "wolf_2").net_damage, 5)

    def test_ability_with_no_save_for_half_is_unaffected(self):
        ability = {
            "damage_value": {"dice": 0, "pips": 0, "bonus": 10}, "damage_tags": [],
            "targets": {"number": 0, "aoe": 0, "side": "all"},
        }
        result = RolledOutcome(entity="gladstone", skill="melee", roll=0, difficulty=0, success=True)
        apply_effects(self.dm_core, result, "melee", None, ability, "wolf", via_test=False)
        self.assertEqual(self._effect_for(result, "wolf").net_damage, 10)
        self.assertEqual(self._effect_for(result, "wolf_2").net_damage, 10)


class TestActionDrivenAttitudeDrift(DMTestCase):
    """!
    @brief DM_Social.py's nudge_attitude_from_event -- the [[attitude_event]] (rules.toml)
        driven counterpart to nudge_attitude's own dialogue-sentiment drift, applied from a
        resolved player action (combat/theft/favor) instead of the tone of something said. See
        CLAUDE.md's "Extended goals" -- "Actions sway attitudes by varying degrees". Tracked in
        its own "action_attitude_deltas" accumulator/cap (ACTION_ATTITUDE_DRIFT_CAP), independent
        of nudge_attitude's own "attitude_deltas"/TALK_ATTITUDE_DRIFT_CAP -- combat hit wiring is
        covered separately, in TestDamageCalculation, right where _apply_damage_if_hit lives.
    """
    scenario_name = "debug"
    start_location = "tavern_floor"

    def test_event_scales_every_axis_by_magnitude(self):
        base = self.dm_core.entities["innkeeper"]["attitudes"]["default"]

        self.dm_core.nudge_attitude_from_event("innkeeper", self.dm_core.player_name, "favor", 0.6)

        after = self.dm_core.get_attitude("innkeeper", self.dm_core.player_name)
        disposition, threat, familiarity = (
            value - starting for value, starting in zip(after, base)
        )
        self.assertAlmostEqual(disposition, 9.0)
        self.assertEqual(threat, 0)
        self.assertAlmostEqual(familiarity, 7.2)

    def test_action_drift_is_capped_independently_of_talk_drift(self):
        # Push both accumulators toward the same axis (disposition) as far as they'll go --
        # dialogue sentiment (TALK_ATTITUDE_DRIFT_CAP) and a run of "favor" events
        # (ACTION_ATTITUDE_DRIFT_CAP) -- and confirm each caps on its own terms rather than
        # sharing one ceiling between the two accumulators.
        for _ in range(50):
            self.dm_core.nudge_attitude("innkeeper", self.dm_core.player_name, {"disposition": ("positive", 1.0)})
        for _ in range(50):
            self.dm_core.nudge_attitude_from_event("innkeeper", self.dm_core.player_name, "favor", 1.0)

        base_disposition = self.dm_core.entities["innkeeper"]["attitudes"]["default"][0]
        disposition = self.dm_core.get_attitude("innkeeper", self.dm_core.player_name)[0]

        self.assertEqual(disposition, base_disposition + TALK_ATTITUDE_DRIFT_CAP + ACTION_ATTITUDE_DRIFT_CAP)

    def test_unknown_event_and_ungated_targets_are_no_ops(self):
        before = list(self.dm_core.get_attitude("innkeeper", self.dm_core.player_name))
        self.dm_core.nudge_attitude_from_event("innkeeper", self.dm_core.player_name, "not_a_real_event", 1.0)
        self.assertEqual(self.dm_core.get_attitude("innkeeper", self.dm_core.player_name), before)

        # An inanimate object has no feelings to nudge -- same precedent is_hostile already sets.
        self.dm_core.entities["stone idol"] = {"name": "stone idol", "supertype": "object", "attitudes": {"default": [0] * 3}}
        self.dm_core.nudge_attitude_from_event("stone idol", self.dm_core.player_name, "favor", 1.0)
        self.assertNotIn("action_attitude_deltas", self.dm_core.entities["stone idol"])

        # A tableless entity (ex: arena's own wolf) has nothing to nudge either.
        self.dm_core.entities["test_tableless"] = {"name": "test_tableless", "max_hp": 10}
        self.dm_core.nudge_attitude_from_event("test_tableless", self.dm_core.player_name, "favor", 1.0)
        self.assertNotIn("action_attitude_deltas", self.dm_core.entities["test_tableless"])

    def test_dead_entity_is_not_aware_of_anything_happening_to_it(self):
        # A dead (or never-conscious) entity isn't aware of a theft, a gift, or anything else --
        # same reasoning that makes a killing blow's own "combat_hit" nudge a no-op too, since
        # the target's HP is already 0 by the time _apply_damage_if_hit gets around to it.
        Combat_Resolution.apply_damage(self.dm_core.world, "innkeeper", 9999)
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "innkeeper"), 0)

        self.dm_core.nudge_attitude_from_event("innkeeper", self.dm_core.player_name, "favor", 1.0)

        self.assertNotIn("action_attitude_deltas", self.dm_core.entities["innkeeper"])


class TestMultipleActions(DMTestCase):
    """!
    @brief The West End Games D6 "multiple actions" rule (see DM_Core.py's own
        _on_action_detected docstring): every action beyond the first attempted in one turn
        costs every one of that turn's actions a cumulative -1D, and however many actions the
        player attempts, exactly one round resolves -- never one round per action.
    """

    def test_resolve_action_dice_penalty_reduces_the_pool_not_the_pips(self):
        with patch("random.randint", return_value=3):
            full = Combat_Resolution.resolve_action(self.dm_core.world, "gladstone", "blades")  # 5D+0
            penalized = Combat_Resolution.resolve_action(self.dm_core.world, "gladstone", "blades", dice_penalty=2)  # 3D+0

        self.assertEqual(full["roll"], 15)
        self.assertEqual(penalized["roll"], 9)

    def test_resolve_action_dice_penalty_floors_at_zero_dice(self):
        with patch("random.randint", return_value=3):
            result = Combat_Resolution.resolve_action(self.dm_core.world, "gladstone", "charisma", dice_penalty=99)  # 2D+0

        self.assertEqual(result["roll"], 0)

    def test_resolve_opposed_action_penalty_never_touches_the_defenders_roll(self):
        self.dm_core.entities["test_defender"] = {
            "name": "test_defender", "skills": {"dodge": {"dice": 6, "pips": 0}},
        }
        with patch("random.randint", return_value=3):
            unpenalized = Combat_Resolution.resolve_opposed_action(self.dm_core.world, "gladstone", "blades", "test_defender")
            penalized = Combat_Resolution.resolve_opposed_action(self.dm_core.world, 
                "gladstone", "blades", "test_defender", dice_penalty=2,
            )

        # The defender's own dodge roll (6D @ 3 = 18) is identical either way -- only the
        # attacker's own roll (5D vs 3D @ 3 each) is reduced by the penalty.
        self.assertEqual(unpenalized["difficulty"], 18)
        self.assertEqual(penalized["difficulty"], 18)
        self.assertEqual(unpenalized["roll"], 15)
        self.assertEqual(penalized["roll"], 9)

    def test_two_actions_in_one_turn_each_roll_at_minus_1d_and_resolve_as_one_round(self):
        # A no-skills target auto-succeeds (difficulty 0) with no opposing roll to muddy the
        # numbers -- isolates the penalty itself, same "practice_dummy" pattern TestCombatLoop
        # already uses.
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self._load_ad_hoc_scenario([{"name": "gladstone", "band": 1}, {"name": "practice_dummy", "band": 1}])
        round_events = self._capture("round_resolved")
        starting_round = self.dm_core.round_number

        with patch("random.randint", return_value=3):
            self.dm_core._on_turn_detected({
                "clauses": [{"kind": "action", "skill": "blades"}, {"kind": "action", "skill": "blades"}],
                "input": "I attack the practice dummy and attack it again",
            })

        # Not two rounds -- one, no matter how many actions the player attempted this turn.
        self.assertEqual(len(round_events), 1)
        self.assertEqual(self.dm_core.round_number, starting_round + 1)
        actions = round_events[0]["actions"]
        self.assertEqual(len(actions), 2)
        # blades is 5D+0 -- at -1D (two actions this turn) each rolls 4D @ 3 = 12.
        self.assertEqual(actions[0].roll, 12)
        self.assertEqual(actions[1].roll, 12)

    def test_three_actions_apply_minus_2d(self):
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self._load_ad_hoc_scenario([{"name": "gladstone", "band": 1}, {"name": "practice_dummy", "band": 1}])
        round_events = self._capture("round_resolved")

        with patch("random.randint", return_value=3):
            self.dm_core._on_turn_detected({
                "clauses": [
                    {"kind": "action", "skill": "blades"}, {"kind": "action", "skill": "blades"},
                    {"kind": "action", "skill": "blades"},
                ],
                "input": "I attack it, attack it again, and attack it once more",
            })

        # blades is 5D+0 -- at -2D (three actions this turn) each rolls 3D @ 3 = 9.
        for action in round_events[0]["actions"]:
            self.assertEqual(action.roll, 9)

    def test_item_test_only_turn_never_triggers_a_round_even_if_current_target_is_hostile(self):
        # Regression: self.current_target ("wolf", hostile from scenario load) must not leak
        # into the round-trigger decision when every action this turn was actually an item
        # test, which never touches self.current_target at all (see _on_action_detected's own
        # "engaged_combat_target" note) -- an early version of this batching mistakenly
        # checked self.current_target's hostility unconditionally, turning "appraise a potion"
        # into a combat round just because a hostile wolf happened to already be the player's
        # standing target from scenario load.
        action_events = self._capture("action_resolved")
        round_events = self._capture("round_resolved")
        self.assertTrue(self.dm_core.is_hostile(self.dm_core.current_target, self.dm_core.player_name))

        self._stub_roll_dice(99)
        self.dm_core._on_turn_detected({
            "clauses": [{"kind": "action", "skill": "appraise", "target": "health potion"}],
            "input": "I appraise the health potion",
        })

        self.assertEqual(round_events, [])
        self.assertEqual(len(action_events), 1)

    def test_mixed_item_test_and_attack_batch_shares_the_penalty_and_still_one_round(self):
        # An item *test* (ex: appraising a potion) rolls dice, so it shares the turn's penalty
        # just like an opposed attack does -- distinct from a diceless item *interaction*
        # (give/take/equip/...), covered by the tests below.
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self._load_ad_hoc_scenario([{"name": "gladstone", "band": 1}, {"name": "practice_dummy", "band": 1}])
        round_events = self._capture("round_resolved")

        with patch("random.randint", return_value=3):
            self.dm_core._on_turn_detected({
                "clauses": [
                    {"kind": "action", "skill": "appraise", "target": "health potion"},
                    {"kind": "action", "skill": "blades"},
                ],
                "input": "I appraise the potion and attack the dummy",
            })

        self.assertEqual(len(round_events), 1)
        actions = round_events[0]["actions"]
        self.assertEqual(len(actions), 2)
        # appraise is 4D+0 -- at -1D (two actions this turn) rolls 3D @ 3 = 9, clearing the
        # health potion's own test difficulty (4).
        self.assertEqual(actions[0].roll, 9)
        self.assertTrue(actions[0].success)
        # blades is 5D+0 -- at -1D rolls 4D @ 3 = 12.
        self.assertEqual(actions[1].roll, 12)

    def test_item_interaction_clause_shares_the_penalty_but_never_rolls_itself(self):
        # Drawing a weapon, picking something up, giving/opening/using an item all cost the
        # same shared per-turn action economy a skill/ability action does in West End Games
        # D6 -- only movement and speech are actually free. An item-interaction clause resolves
        # via the ordinary, unchanged item-interaction pipeline (narrating separately, via its
        # own item_interaction_resolved) and never receives dice_penalty itself (it has
        # nothing to roll), but it still counts toward this turn's total N.
        item_events = self._capture("item_interaction_resolved")
        round_events = self._capture("round_resolved")

        with patch("random.randint", return_value=3):
            self.dm_core._on_turn_detected({
                "clauses": [
                    {"kind": "item", "intent": "drop", "item_name": "health potion"},
                    {"kind": "action", "skill": "blades"},
                ],
                "input": "I drop a health potion and attack the wolf",
            })

        self.assertEqual(len(item_events), 1)
        self.assertEqual(item_events[0]["intent"], "drop")
        self.assertTrue(item_events[0]["found"])
        self.assertIn("health potion", self.dm_core._current_ground_items())

        self.assertEqual(len(round_events), 1)
        actions = round_events[0]["actions"]
        self.assertEqual(len(actions), 1)
        # blades is 5D+0 -- at -1D (the drop counts as this turn's other action, even though
        # it never rolls) rolls 4D @ 3 = 12, not the unpenalized 5D @ 3 = 15 a lone attack
        # would get.
        self.assertEqual(actions[0].roll, 12)

    def test_item_only_turn_publishes_via_item_interaction_resolved_not_action_resolved(self):
        action_events = self._capture("action_resolved")
        round_events = self._capture("round_resolved")
        item_events = self._capture("item_interaction_resolved")

        self.dm_core._on_turn_detected({
            "clauses": [{"kind": "item", "intent": "drop", "item_name": "health potion"}],
            "input": "I drop a health potion",
        })

        self.assertEqual(len(item_events), 1)
        self.assertEqual(action_events, [])
        self.assertEqual(round_events, [])


class TestCombatLoop(DMTestCase):
    def setUp(self):
        super().setUp()
        # These tests always face a scenario target, so combat narration ("round_resolved")
        # is what fires, not the no-combat "action_resolved" path.
        self.resolved = self._capture("round_resolved")

    def test_find_attack_ability_prefers_equipped_weapon(self):
        # Gladstone has a longsword equipped in rhand, which uses the "blades" skill.
        ability = Combat_Actions.find_attack_ability(self.dm_core.world, "gladstone", "blades")
        assert ability is not None
        self.assertEqual(ability["name"], "longsword")

    def test_find_attack_ability_falls_back_to_innate_ability(self):
        # No equipped weapon uses "brawling", so the innate "punch" ability should be found instead.
        ability = Combat_Actions.find_attack_ability(self.dm_core.world, "gladstone", "brawling")
        assert ability is not None
        self.assertEqual(ability["name"], "punch")


    def test_select_ability_skill_picks_best_rated_option_from_a_skill_list(self):
        # cleave's skill is ["blades", "axes"]; gladstone has "blades" (5 dice) and no "axes"
        # entry at all, so "blades" must be the one selected.
        cleave = self.dm_core.entities["cleave"]
        self.assertEqual(Combat_Actions.select_ability_skill(self.dm_core.world, "gladstone", cleave), "blades")


    def test_missed_attack_does_not_apply_damage(self):
        # wolf's dodge (6 dice) will always beat gladstone's blades (2 dice) at this fixed roll.
        with patch("random.randint", return_value=1):
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "blades"}], "input": "I attack with my sword"})

        result = self.resolved[-1]
        action = result["actions"][0]
        self.assertFalse(action.success)
        self.assertFalse(any(isinstance(effect, DamageEffect) for effect in action.effects))
        self.assertEqual(result["round"], 1)
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "wolf"), 16)

    def test_successful_attack_applies_damage_to_the_target(self):
        # Give the player an opponent with no matching opposing skill, so the attack auto-succeeds (difficulty 0).
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self._load_ad_hoc_scenario([{"name": "practice_dummy", "band": 1}])

        with patch("random.randint", return_value=3):
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "blades"}], "input": "I attack with my sword"})

        result = self.resolved[-1]
        action = result["actions"][0]
        self.assertTrue(action.success)
        damage_effects = [effect for effect in action.effects if isinstance(effect, DamageEffect)]
        self.assertEqual(len(damage_effects), 1)
        damage = damage_effects[0]
        self.assertEqual(damage.defender, "practice_dummy")
        self.assertGreater(damage.net_damage, 0)
        self.assertEqual(
            Combat_Resolution.get_current_hp(self.dm_core.world, "practice_dummy"),
            20 - damage.net_damage,
        )


class TestMovementAndRange(DMTestCase):
    def setUp(self):
        super().setUp()  # arena: bands=4, enclosed=true, everyone starts band 1
        self.resolved = self._capture_any("round_resolved", "action_resolved")


    def test_get_distance_between_computes_the_gap(self):
        self.dm_core.entities["gladstone"]["band"] = 2
        self.dm_core.entities["wolf"]["band"] = 4
        self.dm_core.entities["wolf_2"]["band"] = 1
        self.assertEqual(Combat_Resolution.get_distance_between(self.dm_core.world, "gladstone", "wolf"), 2)
        self.assertEqual(Combat_Resolution.get_distance_between(self.dm_core.world, "wolf", "wolf_2"), 3)

    # --- move_entity: floor, and enclosed-vs-open ceiling ----------------------------------

    def test_move_entity_clamps_at_band_one_floor(self):
        self.dm_core.entities["wolf"]["band"] = 2
        self.assertEqual(self.dm_core.move_entity("wolf", -5), 1)


    def test_move_entity_is_unbounded_when_not_enclosed(self):
        field = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="field_grounds")  # bands=6, enclosed=false
        field.entities["wolf"]["band"] = 6
        self.assertEqual(field.move_entity("wolf", 20), 26)  # no ceiling at all -- can flee

    # --- advance_or_retreat: direction is toward/away from current_target ------------------

    def test_advance_moves_the_player_toward_current_target(self):
        self.assertEqual(self.dm_core.current_target, "wolf")
        self.dm_core.entities["gladstone"]["band"] = 1
        self.dm_core.entities["wolf"]["band"] = 4

        self.dm_core.advance_or_retreat("advance")

        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "gladstone"), 2)  # moved one band toward wolf

    # --- is_in_range -------------------------------------------------------------------

    def test_weapon_and_spell_range_thresholds(self):
        # (item, defender band, expected) -- gladstone stays at band 1 throughout, so
        # defender band doubles as the gap between them. Covers melee (longsword has no
        # "range" field, defaulting to 0), a reach weapon (spear, range=1), a ranged
        # weapon (long bow, range=6), and a spell (fireball, range=5), each right at and
        # one band past its own limit.
        cases = [
            ("longsword", 1, True), ("longsword", 2, False),
            ("spear", 1, True), ("spear", 2, True), ("spear", 3, False),
            ("long bow", 7, True), ("long bow", 8, False),
            ("fireball", 6, True), ("fireball", 7, False),
        ]
        for item_name, band, expected in cases:
            with self.subTest(item=item_name, band=band):
                ability = self.dm_core.entities[item_name]
                self.dm_core.entities["wolf"]["band"] = band
                self.assertEqual(Combat_Actions.is_in_range(self.dm_core.world, "gladstone", "wolf", ability), expected)

    # --- integration through _on_action_detected / resolve_behavior_action ---------------

    def test_out_of_range_attack_is_denied_without_a_roll(self):
        self.dm_core.entities["wolf"]["band"] = 3  # longsword needs gap 0

        self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "blades"}], "input": "I attack with my sword"})

        result = self.resolved[-1]
        action = result["actions"][0]
        self.assertIsInstance(action, OutOfRangeOutcome)

    # --- _ability_requires_language / language_dependent gate ----------------------------

    def test_ability_requires_language_reads_the_resolved_abilitys_own_flag(self):
        # maneuvers.toml's "charm" is language_dependent = true, "intimidate" isn't.
        charm = self.dm_core.entities["charm"]
        intimidate = self.dm_core.entities["intimidate"]
        self.assertTrue(Combat_Actions._ability_requires_language(self.dm_core.world, "charisma", charm))
        self.assertFalse(Combat_Actions._ability_requires_language(self.dm_core.world, "intimidation", intimidate))

    def test_ability_requires_language_falls_back_to_the_skills_own_abilities_list(self):
        # A bare "charisma" use (no named ability -- ex: "persuade the guard") still finds
        # charm's own flag via skills.toml's charisma -> ["charm"], the same skill-declared
        # universal-ability list find_attack_ability deliberately never scans itself.
        self.assertTrue(Combat_Actions._ability_requires_language(self.dm_core.world, "charisma", None))
        self.assertFalse(Combat_Actions._ability_requires_language(self.dm_core.world, "intimidation", None))
        # An unrelated skill with no such abilities list at all is simply False, not an error.
        self.assertFalse(Combat_Actions._ability_requires_language(self.dm_core.world, "blades", None))

    def test_language_gated_ability_against_a_no_shared_language_target_is_denied_without_a_roll(self):
        self.dm_core.entities["wolf"]["band"] = 1  # charm's own range defaults to 0 (melee)
        self.dm_core.entities["wolf"]["languages"] = ["dwarvish"]

        self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "charm"}], "input": "I charm the wolf"})

        result = self.resolved[-1]
        action = result["actions"][0]
        self.assertIsInstance(action, LanguageBarrierOutcome)

    def test_language_gated_ability_with_a_shared_language_rolls_normally(self):
        self.dm_core.entities["wolf"]["band"] = 1

        self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "charm"}], "input": "I charm the wolf"})

        result = self.resolved[-1]
        action = result["actions"][0]
        self.assertIsInstance(action, RolledOutcome)


class TestMediumAccess(DMTestCase):
    """!
    @brief medium (an entity field) + has_medium_access (DM_Movement.py) -- gates who can
        engage whom by medium ("air"/"water"/"earth" vs. the default "ground"), the Pathfinder
        Fly/Swim(submerge)/Burrow shape. Deliberately not a spatial axis -- band distance is
        unaffected; this is a second, independent reachability gate alongside is_in_range.
    """

    def test_a_ground_defender_is_always_reachable(self):
        longsword = self.dm_core.entities["longsword"]
        self.assertTrue(Combat_Actions.has_medium_access(self.dm_core.world, "gladstone", "wolf", longsword))

    def test_melee_cannot_reach_a_different_medium_defender(self):
        longsword = self.dm_core.entities["longsword"]
        self.dm_core.entities["wolf"]["medium"] = "air"
        self.assertFalse(Combat_Actions.has_medium_access(self.dm_core.world, "gladstone", "wolf", longsword))

    def test_matching_medium_attacker_reaches_the_defender(self):
        longsword = self.dm_core.entities["longsword"]
        self.dm_core.entities["wolf"]["medium"] = "air"
        self.dm_core.entities["gladstone"]["medium"] = "air"
        self.assertTrue(Combat_Actions.has_medium_access(self.dm_core.world, "gladstone", "wolf", longsword))

    def test_a_ranged_ability_reaches_any_medium_regardless(self):
        fireball = self.dm_core.entities["fireball"]
        self.dm_core.entities["wolf"]["medium"] = "air"
        self.assertTrue(Combat_Actions.has_medium_access(self.dm_core.world, "gladstone", "wolf", fireball))

    def test_no_ability_at_all_always_reaches(self):
        self.dm_core.entities["wolf"]["medium"] = "air"
        self.assertTrue(Combat_Actions.has_medium_access(self.dm_core.world, "gladstone", "wolf", None))

    def test_player_melee_attack_on_an_airborne_target_is_denied_without_a_roll(self):
        self.dm_core.entities["wolf"]["band"] = 1  # in band range -- only medium blocks it
        self.dm_core.entities["wolf"]["medium"] = "air"
        events = self._capture_any("round_resolved", "action_resolved")

        self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "blades"}], "input": "I attack with my sword"})

        action = events[-1]["actions"][0]
        self.assertIsInstance(action, OutOfRangeOutcome)

    def test_player_ranged_spell_on_an_airborne_target_still_rolls(self):
        self.dm_core.entities["wolf"]["band"] = 1
        self.dm_core.entities["wolf"]["medium"] = "air"
        events = self._capture_any("round_resolved", "action_resolved")

        self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "arcane"}], "input": "I cast fireball"})

        action = events[-1]["actions"][0]
        self.assertIsInstance(action, RolledOutcome)

    def test_resolve_behavior_action_returns_none_instead_of_advancing_toward_a_different_medium(self):
        # wolf's own "bite" is melee -- normally an out-of-range target gets an "advance"
        # fallback (see TestMovementAndRange), but a medium mismatch can never be fixed by
        # closing band distance, so the entity simply doesn't act instead.
        self.dm_core.entities["gladstone"]["medium"] = "air"
        self.assertIsNone(Combat_Actions.resolve_behavior_action(self.dm_core.world, "wolf", "gladstone"))


class TestAbilityEffectsForNpcs(DMTestCase):
    """!
    @brief resolution/Ability_Effects.py is one implementation for every actor -- an NPC that uses
        an ability gets the same effects the player gets from it (summon, teleport, spell
        materials), each gated by what the ability itself authors. Crossed through
        apply_ability_effects with "wolf" (the arena's hostile creature) as the actor.
    """

    def _landed(self):
        return RolledOutcome(entity="wolf", skill="melee", roll=10, difficulty=1, success=True)

    def _cast(self, ability, result=None, target="gladstone"):
        result = result or self._landed()
        Ability_Effects.apply_ability_effects(
            self.dm_core.world, "wolf", result, "melee", ability, ability, target,
        )
        return result

    def test_an_npcs_summon_lands_at_the_npcs_own_band(self):
        self.dm_core.entities["wolf"]["band"] = 2
        before = set(self.dm_core.scenario_entities)

        result = self._cast({"name": "call the pack", "summon": {"name": "spectral wolf", "duration": 2}})

        summoned = [e.name for e in result.effects if isinstance(e, SummonEffect)]
        self.assertEqual(len(summoned), 1)
        self.assertEqual(set(self.dm_core.scenario_entities) - before, set(summoned))
        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, summoned[0]), 2)

    def test_an_npc_can_teleport_between_bands_but_never_relocate_the_scene(self):
        location = self.dm_core.current_location_key

        result = self._cast({"name": "blink", "teleport_to_band": 3, "teleport_to_location": {"location": "elsewhere"}})

        teleports = [e for e in result.effects if isinstance(e, TeleportEffect)]
        self.assertEqual([(e.entity, e.band, e.location) for e in teleports], [("wolf", 3, None)])
        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "wolf"), 3)
        self.assertEqual(self.dm_core.current_location_key, location)

    def test_an_npc_without_the_materials_still_casts_and_spends_nothing(self):
        self.dm_core.entities["wolf"]["inventory"] = []

        result = self._cast({"name": "hex", "materials": [{"item": "bat guano", "quantity": 1}], "summon": {"name": "spectral wolf", "duration": 1}})

        self.assertEqual(len([e for e in result.effects if isinstance(e, SummonEffect)]), 1)
        self.assertEqual(self.dm_core.entities["wolf"]["inventory"], [])

    def test_an_ability_that_authors_no_effects_does_nothing_beyond_damage(self):
        result = self._cast({"name": "growl"})

        self.assertEqual(result.effects, [])

    def test_a_failed_roll_applies_no_effect_at_all(self):
        result = RolledOutcome(entity="wolf", skill="melee", roll=1, difficulty=10, success=False)

        self._cast({"name": "blink", "teleport_to_band": 3}, result)

        self.assertEqual(result.effects, [])


class TestTeleport(DMTestCase):
    """!
    @brief teleport_to_band/teleport_to_location (ability fields) + _apply_teleport_if_hit
        (DM_Core.py) -- relocates the player outright on a successful cast (Dimension Door/
        Teleport), reusing move_entity's own existing clamp and _enter_location's own
        machinery rather than inventing a new movement/location mechanism.
    """

    def test_teleport_to_band_moves_the_player_and_appends_a_teleport_effect(self):
        # arena_grounds: bands=4, enclosed=true, everyone starts band 1.
        ability = {"name": "dimension door", "teleport_to_band": 3}
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=10, difficulty=1, success=True)

        apply_effects(self.dm_core, result, None, ability, None, None)

        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "gladstone"), 3)
        effects = [e for e in result.effects if isinstance(e, TeleportEffect)]
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0].band, 3)
        self.assertIsNone(effects[0].location)

    def test_teleport_to_band_is_clamped_to_the_rooms_own_ceiling(self):
        ability = {"name": "dimension door", "teleport_to_band": 99}
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=10, difficulty=1, success=True)

        apply_effects(self.dm_core, result, None, ability, None, None)

        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "gladstone"), 4)  # arena_grounds' own ceiling

    def test_teleport_to_location_moves_the_player_to_a_different_location(self):
        ability = {"name": "teleport", "teleport_to_location": {"location": "crypt"}}
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=10, difficulty=1, success=True)

        apply_effects(self.dm_core, result, None, ability, None, None)

        self.assertEqual(self.dm_core.current_location_key, "crypt")
        effects = [e for e in result.effects if isinstance(e, TeleportEffect)]
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0].location, "crypt")
        self.assertIsNone(effects[0].band)

    def test_no_effect_on_a_failed_roll(self):
        ability = {"name": "dimension door", "teleport_to_band": 3}
        result = RolledOutcome(entity="gladstone", skill="arcane", roll=1, difficulty=10, success=False)

        apply_effects(self.dm_core, result, None, ability, None, None)

        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "gladstone"), 1)  # unchanged
        self.assertEqual(result.effects, [])

    def test_no_effect_with_no_named_ability(self):
        result = RolledOutcome(entity="gladstone", skill="blades", roll=10, difficulty=1, success=True)

        apply_effects(self.dm_core, result, None, None, None, None)

        self.assertEqual(result.effects, [])

    def test_end_to_end_casting_relocates_the_player_via_the_real_roll_pipeline(self):
        # _resolve_roll + _finish_rolled_outcome together, the same pipeline a real turn goes
        # through -- named_ability passed explicitly (skipping NLP name resolution, which
        # would otherwise have to disambiguate gladstone's several other "arcane" spells) with
        # no target_name, so this resolves via the untargeted, always-succeeds branch.
        dimension_door = {"name": "dimension door", "skill": "arcane", "teleport_to_band": 3}
        result, ability, via_test = self.dm_core._resolve_roll("arcane", dimension_door, None)
        self.dm_core._finish_rolled_outcome(result, "arcane", dimension_door, ability, None, via_test)

        self.assertTrue(result.success)
        self.assertEqual(Combat_Resolution.get_band(self.dm_core.world, "gladstone"), 3)
        self.assertTrue(any(isinstance(e, TeleportEffect) for e in result.effects))


class TestLoreCheck(DMTestCase):
    # arena: gladstone/wolf/wolf_2/thane all start band 1. wolf is subtype "animal" (survival's
    # own lore_types, skills.toml) but authors no resistance/immunity/vulnerability/damage_tags
    # of its own; thane is subtype "humanoid" -- no [[skill]] authors a lore_types matching it
    # at all. "giant spider" (debug.toml's shared entity catalog, subtype "animal",
    # vulnerability_tags = ["fire"]) is instanced fresh per test that needs real revealed tags.

    def setUp(self):
        super().setUp()
        self.resolved = self._capture("item_interaction_resolved")

    def _add_spider(self, band=1):
        [name] = self.dm_core._instance_entities([{"name": "giant spider", "band": band}])
        self.dm_core.scenario_entities.append(name)
        return name

    def test_lore_check_denied_when_no_present_entity_is_named(self):
        self.dm_core._on_item_interaction_detected({
            "intent": "lore_check", "item_name": None, "input": "what do you know about the dragon",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "not_present")

    def test_lore_check_denied_against_a_creature_with_no_matching_lore_skill(self):
        self.dm_core._on_item_interaction_detected({
            "intent": "lore_check", "item_name": None, "input": "what do you know about thane",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "no_lore_available")
        self.assertEqual(result["target"], "thane")

    def test_lore_check_success_reveals_the_targets_own_tags(self):
        name = self._add_spider()
        self._stub_roll_dice(999)  # guarantees a pass regardless of the CR-scaled difficulty
        self.dm_core._on_item_interaction_detected({
            "intent": "lore_check", "item_name": None, "input": f"what do you know about the {name}",
        })
        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["target"], name)
        self.assertEqual(result["skill"], "survival")
        self.assertEqual(result["revealed"], ["fire"])
        self.assertTrue(Combat_Actions.is_identified(self.dm_core.world, name))

    def test_lore_check_success_against_a_target_with_no_tags_reveals_nothing(self):
        self._stub_roll_dice(999)
        self.dm_core._on_item_interaction_detected({
            "intent": "lore_check", "item_name": None, "input": "what do you know about the wolf",
        })
        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["skill"], "survival")
        self.assertEqual(result["revealed"], [])

    def test_lore_check_fails_and_reveals_nothing_on_a_bad_roll(self):
        name = self._add_spider()
        self._stub_roll_dice(0)  # guarantees a fail against any positive CR-scaled difficulty
        self.dm_core._on_item_interaction_detected({
            "intent": "lore_check", "item_name": None, "input": f"what do you know about the {name}",
        })
        result = self.resolved[-1]
        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "check_failed")
        self.assertFalse(Combat_Actions.is_identified(self.dm_core.world, name))

    def test_lore_check_already_identified_skips_the_roll_entirely(self):
        name = self._add_spider()
        Combat_Resolution.apply_condition(self.dm_core.world, name, "identified", duration="permanent")
        self._stub_roll_dice(0)  # would fail any real roll -- proves no roll is attempted
        self.dm_core._on_item_interaction_detected({
            "intent": "lore_check", "item_name": None, "input": f"what do you know about the {name}",
        })
        result = self.resolved[-1]
        self.assertTrue(result["found"])
        self.assertEqual(result["revealed"], ["fire"])


class TestRoundUpkeep(DMTestCase):
    """!
    @brief The generic per-round upkeep hook (run_round_upkeep/apply_round_upkeep/
        get_condition_upkeep, DM_Status.py) and creatures.toml's "troll" -- the shipped
        regeneration-suppressed-by-fire example (see rules.toml's own "regenerating"
        [[condition]] entry and Rules/Pathfinder/reference/pathfinder_mapping.toml's
        creature_ability "Regeneration / Fast Healing" row).
    """

    def setUp(self):
        super().setUp()
        self._load_ad_hoc_scenario(
            [{"name": "gladstone", "band": 1}, {"name": "troll", "band": 1}], bands=4, enclosed=True,
        )

    def test_instancing_seeds_the_regenerating_condition_from_the_template(self):
        # creatures.toml's troll authors [entity.conditions.regenerating] permanently --
        # _instance_entities copies it into active_conditions the moment it's placed in a scene.
        self.assertIn("regenerating", self.dm_core.entities["troll"]["active_conditions"])

    def test_get_condition_upkeep_reads_the_trolls_regenerating_condition(self):
        upkeep = self.dm_core.get_condition_upkeep("troll")
        self.assertEqual(upkeep["heal"], {"dice": 2, "pips": 0, "bonus": 0})
        self.assertEqual(upkeep["damage"], {"dice": 0, "pips": 0, "bonus": 0})

    def test_calculate_damage_records_recent_damage_tags_on_the_defender(self):
        fireball = {"damage_value": {"dice": 2, "pips": 0, "bonus": 0}, "damage_tags": ["fire"]}
        with patch("random.randint", return_value=3):
            Combat_Actions.calculate_damage(self.dm_core.world, "gladstone", "troll", fireball)
        self.assertIn("fire", self.dm_core.entities["troll"]["recent_damage_tags"])

    @patch("random.randint", return_value=3)
    def test_apply_round_upkeep_heals_and_clears_recent_damage_tags(self, mock_randint):
        Combat_Resolution.apply_damage(self.dm_core.world, "troll", 10)  # 40 -> 30
        self.dm_core.entities["troll"]["recent_damage_tags"] = {"slashing"}

        self.dm_core.apply_round_upkeep("troll")

        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "troll"), 36)  # 30 + (2D @ 3 each = 6)
        self.assertEqual(self.dm_core.entities["troll"]["recent_damage_tags"], set())

    @patch("random.randint", return_value=3)
    def test_apply_round_upkeep_suppressed_by_a_matching_fire_tag(self, mock_randint):
        Combat_Resolution.apply_damage(self.dm_core.world, "troll", 10)  # 40 -> 30
        self.dm_core.entities["troll"]["recent_damage_tags"] = {"fire"}

        self.dm_core.apply_round_upkeep("troll")

        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "troll"), 30)  # no heal this round
        self.assertEqual(self.dm_core.entities["troll"]["recent_damage_tags"], set())

    def test_run_round_upkeep_skips_dead_entities(self):
        Combat_Resolution.apply_damage(self.dm_core.world, "troll", 999)
        self.dm_core.run_round_upkeep()
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "troll"), 0)

    def test_run_round_upkeep_expires_surprised_after_one_round(self):
        # Night watch applies "surprised" with duration="rounds", length=1 -- run_round_upkeep's
        # own generic condition tick (Combat_Resolution.tick_condition_durations) is what
        # actually expires it. See docs/downtime.md's "Night watch and surprise".
        Combat_Resolution.apply_condition(self.dm_core.world, "gladstone", "surprised", duration="rounds", length=1, dismiss="")
        self.dm_core.run_round_upkeep()
        self.assertFalse(Combat_Resolution.has_condition(self.dm_core.world, "gladstone", "surprised"))

    @patch("random.randint", return_value=3)
    def test_resolve_combat_round_regenerates_the_troll_unless_burned_this_round(self, mock_randint):
        # _resolve_combat_round is the real per-round entry point (DM_Core.py) -- confirms the
        # hook is actually wired in, not just directly callable.
        Combat_Resolution.apply_damage(self.dm_core.world, "troll", 10)  # 40 -> 30, no fire tag recorded
        self.dm_core._resolve_combat_round({"actions": []})
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "troll"), 36)  # healed 2D @ 3 = 6

        Combat_Resolution.apply_damage(self.dm_core.world, "troll", 10)  # 36 -> 26
        self.dm_core.entities["troll"]["recent_damage_tags"] = {"fire"}
        self.dm_core._resolve_combat_round({"actions": []})
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "troll"), 26)  # suppressed this round

    @patch("random.randint", return_value=3)
    def test_apply_downtime_upkeep_scales_the_roll_by_blocks_spent(self, mock_randint):
        # One aggregate roll over the whole span, not one per block -- 2D * 3 blocks = 6D @ 3
        # each = 18, matching rest()'s own fortitude-scaling precedent.
        Combat_Resolution.apply_damage(self.dm_core.world, "troll", 30)  # 40 -> 10
        self.dm_core.apply_downtime_upkeep(3)
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "troll"), 28)  # 10 + 18

    def test_apply_downtime_upkeep_is_a_no_op_for_zero_blocks(self):
        Combat_Resolution.apply_damage(self.dm_core.world, "troll", 30)
        self.dm_core.apply_downtime_upkeep(0)
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "troll"), 10)

    @patch("random.randint", return_value=3)
    def test_apply_downtime_upkeep_is_still_suppressed_by_a_matching_recent_damage_tag(self, mock_randint):
        Combat_Resolution.apply_damage(self.dm_core.world, "troll", 30)
        self.dm_core.entities["troll"]["recent_damage_tags"] = {"fire"}
        self.dm_core.apply_downtime_upkeep(3)
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "troll"), 10)  # no heal at all

    @patch("random.randint", return_value=3)
    def test_resting_regenerates_the_troll_alongside_the_partys_own_fortitude_healing(self, mock_randint):
        # The real entry point (DM_Time.py's rest -> _finish_pending_rest), not
        # apply_downtime_upkeep called directly -- confirms the hook is actually wired in.
        Combat_Resolution.apply_damage(self.dm_core.world, "gladstone", 10)
        Combat_Resolution.apply_damage(self.dm_core.world, "troll", 30)  # 40 -> 10, not a party member

        result = self.dm_core.rest(2)

        self.assertFalse(result["interrupted"])
        self.assertIn("gladstone", result["healed"])  # party's own fortitude healing
        self.assertNotIn("troll", result["healed"])  # not a party member, no fortitude entry
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "troll"), 22)  # 10 + (2D*2 blocks @ 3 = 12)


class TestAttackingAnyone(DMTestCase):
    """!
    @brief Any NPC can be attacked (not always wisely): an attack naming a non-hostile creature
        redirects to it and applies the "assaulted" attitude event (rules.toml), which carries
        it past is_hostile's -100 via its own cap. Arena's own thane (an ally, disposition 40)
        is the fixture.
    """

    def _attack(self, skill, target, text):
        self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": skill, "target": target}], "input": text})

    def test_attacking_a_named_non_hostile_creature_targets_it_and_turns_it_hostile(self):
        self.assertFalse(self.dm_core.is_hostile("thane", "gladstone"))
        self._attack("blades", "thane", "i attack thane")

        self.assertEqual(self.dm_core.current_target, "thane")
        self.assertTrue(self.dm_core.is_hostile("thane", "gladstone"))
        # Through the attitude itself, so the narrator's own describe_attitude agrees.
        self.assertLessEqual(self.dm_core.get_attitude("thane", "gladstone")[0], -100)

    def test_a_weak_match_never_starts_a_fight(self):
        # Found by playtest: "use the fire for dramatic effect" (fireball, 0.57) and a remark
        # matched to psionics at 0.30 set a market burning and killed two bystanders.
        # Both fell through to the conversation partner -- the victim was never named.
        # Now asked rather than refused -- see ASSAULT_CONFIRM_SCORE.
        self._clear_the_fight()
        self.dm_core._set_conversation_partner("thane")
        notices = self._capture("player_notice")
        self.dm_core._on_turn_detected({"clauses": [
            {"kind": "action", "skill": "blades", "score": 0.57},
        ], "input": "use the fire for dramatic effect"})

        self.assertFalse(self.dm_core.is_hostile("thane", "gladstone"))
        self.assertEqual([n["message"] for n in notices], ["Attack thane? (yes/no)"])
        self.event_bus.publish("confirmation_answered", {"answer": "no", "input": "no"})
        self.assertFalse(self.dm_core.is_hostile("thane", "gladstone"))
        self.assertEqual(notices[-1]["message"], "You hold off.")

        # Naming the victim is intent enough, even on a modest match ("trip silas", 0.645).
        self.dm_core._on_turn_detected({"clauses": [
            {"kind": "action", "skill": "blades", "target": "thane", "score": 0.6},
        ], "input": "slash thane"})
        self.assertTrue(self.dm_core.is_hostile("thane", "gladstone"))

    def test_a_yes_runs_the_attack_that_was_asked_about(self):
        # Found by playtest: "let's see what that knife is good for" (0.66) killed a bystander
        # outright under the old 0.65 bar; "Fight me!" (0.58) was simply refused.
        self._clear_the_fight()
        self.dm_core._set_conversation_partner("thane")
        self.dm_core._on_turn_detected({"clauses": [
            {"kind": "action", "skill": "blades", "score": 0.66},
        ], "input": "let's see what this knife is good for"})
        self.assertFalse(self.dm_core.is_hostile("thane", "gladstone"))

        self.event_bus.publish("confirmation_answered", {"answer": "yes", "input": "yes"})
        self.assertTrue(self.dm_core.is_hostile("thane", "gladstone"))
        self.assertIsNone(self.dm_core.pending_confirmation)

    def test_typing_something_else_drops_the_question(self):
        self._clear_the_fight()
        self.dm_core._set_conversation_partner("thane")
        self.dm_core._on_turn_detected({"clauses": [
            {"kind": "action", "skill": "blades", "score": 0.6},
        ], "input": "fight me"})
        self.event_bus.publish("confirmation_answered", {"answer": None, "input": "where's the inn?"})
        self.assertIsNone(self.dm_core.pending_confirmation)
        self.event_bus.publish("confirmation_answered", {"answer": "yes", "input": "yes"})
        self.assertFalse(self.dm_core.is_hostile("thane", "gladstone"))

    def test_an_assault_maneuver_counts_as_an_attack_but_a_friendly_one_does_not(self):
        # Found by playtest: "shove over, you lumbering dockworker" matched bull rush, which deals
        # no damage, so it could never be aimed at a non-hostile NPC.
        self._attack("treat wounds", "thane", "i treat thane's wounds")
        self.assertFalse(self.dm_core.is_hostile("thane", "gladstone"))

        self._attack("bull rush", "thane", "i bull rush thane")
        self.assertEqual(self.dm_core.current_target, "thane")
        self.assertTrue(self.dm_core.is_hostile("thane", "gladstone"))

    def _clear_the_fight(self):
        for wolf in ("wolf", "wolf_2"):
            Combat_Resolution.apply_damage(self.dm_core.world, wolf, 999)
        self.dm_core.current_target = "thane"  # the "first living non-player" leftover

    def test_a_non_attack_that_lands_on_a_bystander_by_default_names_no_target(self):
        # Found by playtest: a gesture rolled polearms against the default target, and the
        # narrator, told "against Belor Hemlock", wrote a sword strike on him.
        resolved = []
        self.event_bus.subscribe("action_resolved", resolved.append)
        self._clear_the_fight()
        self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "observation"}], "input": "look him over"})
        [outcome] = resolved[-1]["actions"]
        self.assertTrue(outcome.incidental_target)

        self.dm_core._on_turn_detected({
            "clauses": [{"kind": "action", "skill": "observation", "target": "thane"}], "input": "look thane over",
        })
        [outcome] = resolved[-1]["actions"]
        self.assertFalse(outcome.incidental_target)

    def test_an_attack_naming_nobody_goes_at_the_conversation_partner(self):
        # Found by playtest: "my turn to hit you!" mid-argument targeted nothing at all.
        self._clear_the_fight()
        self.dm_core._set_conversation_partner("thane")
        self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "blades"}], "input": "my turn to hit you!"})

        self.assertTrue(self.dm_core.is_hostile("thane", "gladstone"))

    def test_an_attack_describing_someone_present_finds_them_but_not_by_a_modifier_word(self):
        # Found by playtest: "pin the merchant's feet" never reached the spice merchant. Narrated
        # people carry their occupation words as aliases -- the head word counts, "spice" doesn't.
        self._clear_the_fight()
        self.dm_core.entities["thane"]["aliases"] = ["merchant", "spice", "spice merchant"]
        attack = {"clauses": [{"kind": "action", "skill": "blades"}]}

        self.dm_core._on_turn_detected({**attack, "input": "kick the spice cart over"})
        self.assertFalse(self.dm_core.is_hostile("thane", "gladstone"))

        self.dm_core._on_turn_detected({**attack, "input": "shove my blade at the merchant's feet"})
        self.assertTrue(self.dm_core.is_hostile("thane", "gladstone"))

    def test_a_gendered_pronoun_finds_the_only_person_it_can_mean(self):
        # Found by playtest: "kick her into the street", no name and no conversation, met only
        # air while the one woman present -- the bread vendor -- stood right there.
        self._clear_the_fight()
        # A bystander, not the fixture's ally: a pronoun never picks out a party member.
        self.dm_core.entities["thane"].pop("is_party", None)
        self.dm_core.entities["thane"]["qualities"] = {"gender": "male"}
        attack = {"clauses": [{"kind": "action", "skill": "brawling"}]}

        self.dm_core._on_turn_detected({**attack, "input": "kick her into the street"})
        self.assertFalse(self.dm_core.is_hostile("thane", "gladstone"))

        self.dm_core._on_turn_detected({**attack, "input": "kick him into the street"})
        self.assertTrue(self.dm_core.is_hostile("thane", "gladstone"))

    def test_an_attack_with_no_one_to_hit_says_so_and_spares_a_leftover_ally(self):
        resolved = []
        self.event_bus.subscribe("action_resolved", resolved.append)
        self._clear_the_fight()
        thane_hp = Combat_Resolution.get_current_hp(self.dm_core.world, "thane")
        self._stub_roll_dice(20)

        self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": "blades"}], "input": "strike them until they drop!"})

        [outcome] = resolved[-1]["actions"]
        self.assertTrue(outcome.no_opponent)
        self.assertIsNone(outcome.defender)
        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "thane"), thane_hp)
        self.assertFalse(self.dm_core.is_hostile("thane", "gladstone"))

    def test_a_later_combat_hit_never_pulls_an_assault_back_under_the_ordinary_cap(self):
        self.dm_core.nudge_attitude_from_event("thane", "gladstone", "assaulted", 1.0)
        self.dm_core.nudge_attitude_from_event("thane", "gladstone", "combat_hit", 0.5)
        self.assertTrue(self.dm_core.is_hostile("thane", "gladstone"))

    def test_an_assaulted_npc_with_no_behavior_fights_back_even_after_a_reload(self):
        slot = "test_assaulted_round_trip"
        self.addCleanup(shutil.rmtree, os.path.join("Saves", slot), ignore_errors=True)
        self.dm_core.entities["thane"].pop("behavior")
        self.dm_core.nudge_attitude_from_event("thane", "gladstone", "assaulted", 1.0)
        self.dm_core.save_game(slot)

        fresh_dm = DMCore(ValidatingEventBus(), scenario_name="debug", start_location="arena_grounds")
        fresh_dm.load_game(slot)
        fresh_dm.entities["thane"].pop("behavior")  # re-instanced from the template, which has one
        fresh_dm._resolve_combat_round({"actions": []})
        thane = fresh_dm.entities["thane"]
        self.assertEqual(thane["behavior"][-1]["action"], f"{thane['name']} attack")

    def test_an_assaulted_merchant_fights_back_with_a_blow_not_charisma(self):
        # Found by playtest: charisma is an offense-role skill, so an assaulted trinket vendor
        # whose best one was charisma "fought back" with a charisma roll every round.
        thane = self.dm_core.entities["thane"]
        thane.pop("behavior")
        thane["skills"] = {"charisma": {"dice": 4, "pips": 0}, "brawling": {"dice": 1, "pips": 0}}
        self.dm_core.nudge_attitude_from_event("thane", "gladstone", "assaulted", 1.0)
        self.dm_core._arm_if_turned_hostile("thane", "gladstone")
        self.assertEqual(thane["abilities"][-1]["skill"], "brawling")

        thane["skills"] = {"charisma": {"dice": 4, "pips": 0}}
        thane.pop("behavior")
        self.dm_core._arm_if_turned_hostile("thane", "gladstone")
        self.assertEqual(thane["abilities"][-1]["skill"], "brawling")
        # Untrained is 0D, which never hits; an armed bystander gets the 1D anyone has.
        self.assertEqual(thane["skills"]["brawling"]["dice"], 1)

    def test_an_authored_hostile_without_behavior_is_left_unarmed(self):
        # Hostile by its own authored attitude, not by anything that happened in play.
        thane = self.dm_core.entities["thane"]
        thane.pop("behavior")
        thane["attitudes"] = {"default": [-120, 0, 0]}
        self.dm_core._arm_if_turned_hostile("thane", "gladstone")
        self.assertNotIn("behavior", thane)

    def test_an_unowned_catalog_ability_rolls_on_its_own_skill(self):
        # Found by playtest: "kick" matched a horse's own innate ability, which the player
        # doesn't own and isn't a skill -- it rolled 0 dice under its own name.
        self.assertEqual(self.dm_core._resolve_action_skill("kick"), ("brawling", None))


class TestUntargetedDifficulty(DMTestCase):
    """!
    @brief An unopposed check's difficulty: a [[difficulty_tier]] the model picks, "trivial"
        for no roll, a fallback when the model can't answer. Found by playtest: it was always
        0, so ~800 turns of searching/climbing/sneaking never once failed.
    """

    def setUp(self):
        super().setUp()
        restore = patch.object(DMCore, "_untargeted_difficulty", REAL_UNTARGETED_DIFFICULTY)
        restore.start()
        self.addCleanup(restore.stop)

    def _rated(self, tier):
        reply = {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "rate_difficulty", "arguments": json.dumps({"tier": tier, "reason": "test"}),
        }}]}}]}
        return scripted_llm(return_value=reply)

    def test_the_rated_tier_sets_the_difficulty(self):
        with self._rated("difficult"):
            result, _ability, _via_test = self.dm_core._resolve_roll("athletics", None, None, input_text="climb the wall")
        self.assertEqual(result.difficulty, 15)
        self.assertFalse(result.trivial)

    def test_the_rating_call_turns_the_models_reasoning_off(self):
        # Measured on gemma4: reasoning on took 5-15s a rating and sometimes ran out of tokens
        # mid-thought; off, under a second and always a valid tier.
        with self._rated("easy") as client:
            self.dm_core._untargeted_difficulty("observation", "look around")
        self.assertEqual(client.call_args.kwargs.get("reasoning_effort"), "none")

    def test_a_trivial_task_skips_the_roll(self):
        with self._rated("trivial"):
            result, _ability, _via_test = self.dm_core._resolve_roll("observation", None, None, input_text="look at the cup")
        self.assertTrue(result.trivial and result.success)

    def test_an_unreachable_model_falls_back_to_the_skills_default_then_the_settings(self):
        with scripted_llm(side_effect=ConnectionError):
            self.assertEqual(self.dm_core._untargeted_difficulty("athletics", "climb"), (10, False))
            self.dm_core.skills["athletics"]["default_difficulty"] = "easy"
            self.assertEqual(self.dm_core._untargeted_difficulty("athletics", "climb"), (6, False))

    def test_a_named_spell_cast_at_no_one_is_never_rated(self):
        # spells.toml authors an untargeted cast as automatic ("summoning before a fight starts
        # is trivial") -- only a plain unopposed skill check is rated.
        with scripted_llm() as never_called:
            result, _ability, _via_test = self.dm_core._resolve_roll(
                "arcane", {"name": "glyph", "skill": "arcane", "difficulty": 12}, None, input_text="cast glyph",
            )
        self.assertEqual(result.difficulty, 0)
        self.assertEqual(never_called.call_count, 0)

    def test_a_setting_with_no_tiers_keeps_difficulty_zero(self):
        self.dm_core.rules.pop("difficulty_tier")
        with scripted_llm() as never_called:
            self.assertEqual(self.dm_core._untargeted_difficulty("athletics", "climb"), (0, False))
        self.assertEqual(never_called.call_count, 0)


class FakeActionTargetScene(ActionTargetScene):
    """!
    @brief Test adapter for resolve_action_target's scene port: a handful of entity dicts, who is
        hostile, and each one's HP (out of 16), with no DMCore. Real adapter: DM_ActionTarget.py.
    """

    def __init__(self, entities, hostile=(), hp=None, current_target=None, partner_key=None, party=()):
        self.entities = entities
        self.scenario_entities = list(entities)
        self.player_name = "hero"
        self.current_target = current_target
        self.partner_key = partner_key
        self._hostile, self._hp, self._party = set(hostile), hp or {}, set(party)

    def hp(self, key):
        return self._hp.get(key, 16)

    def hp_fraction(self, key):
        return self.hp(key) / 16

    def is_hostile(self, key):
        return key in self._hostile

    def is_party_member(self, key):
        return key in self._party

    def is_hidden(self, key):
        return False


def _creature(name, aliases=(), gender=None):
    entity = {"name": name, "supertype": "creature", "aliases": list(aliases)}
    if gender:
        entity["qualities"] = {"gender": gender}
    return entity


class TestActionTargetResolution(unittest.TestCase):
    """!
    @brief resolution/Action_Target.py -- who a skill or ability clause is aimed at, through its one
        interface (resolve_action_target) over a fake scene. Arena's "wolf"/"wolf_2" and ally
        "thane" are the shape; every clause passes "wolf" as NLPCore's own naive target guess
        (map_to_target always prefers the plain species name over a literal "wolf_2" string).
    """

    def _arena(self, **kwargs):
        entities = {"wolf": _creature("Wolf"), "wolf_2": _creature("Wolf"), "thane": _creature("Thane")}
        kwargs.setdefault("hostile", {"wolf", "wolf_2"})
        kwargs.setdefault("current_target", "wolf")
        return FakeActionTargetScene(entities, **kwargs)

    def test_no_qualifier_leaves_the_naive_guess_unchanged(self):
        verdict = resolve_action_target(self._arena(), "wolf", "I attack the wolf", True)
        self.assertEqual((verdict.target, verdict.current_target, verdict.assaulting), ("wolf", "wolf", False))

    def test_ordinal_second_redirects_to_the_second_instance(self):
        verdict = resolve_action_target(self._arena(), "wolf", "I attack the second wolf", True)
        self.assertEqual((verdict.target, verdict.current_target), ("wolf_2", "wolf_2"))

    def test_other_redirects_away_from_the_already_current_instance(self):
        verdict = resolve_action_target(self._arena(current_target="wolf"), "wolf", "I attack the other wolf", True)
        self.assertEqual(verdict.target, "wolf_2")
        verdict = resolve_action_target(self._arena(current_target="wolf_2"), "wolf", "I attack the other wolf", True)
        self.assertEqual(verdict.target, "wolf")

    def test_wounded_redirects_to_the_instance_actually_below_the_cutoff(self):
        # 5/16 = 0.3125, under the 0.40 "wounded" cutoff; wolf_2 stays undamaged.
        scene = self._arena(hp={"wolf": 5}, current_target="wolf_2")
        self.assertEqual(resolve_action_target(scene, "wolf", "I attack the wounded wolf", True).target, "wolf")

    def test_wounded_is_ignored_when_nothing_is_actually_wounded(self):
        self.assertEqual(resolve_action_target(self._arena(), "wolf", "I attack the wounded wolf", True).target, "wolf")

    def test_healthy_redirects_away_from_the_wounded_instance(self):
        scene = self._arena(hp={"wolf": 5})
        self.assertEqual(resolve_action_target(scene, "wolf", "I attack the healthy wolf", True).target, "wolf_2")

    def test_a_single_instance_ignores_the_qualifier_word(self):
        scene = self._arena(current_target="wolf")
        verdict = resolve_action_target(scene, "thane", "talk to the other thane", False)
        self.assertEqual(verdict.current_target, "wolf")  # thane is no hostile target for a non-attack

    def test_a_dead_sibling_is_not_a_candidate(self):
        scene = self._arena(hp={"wolf_2": 0})
        self.assertEqual(resolve_action_target(scene, "wolf", "I attack the second wolf", True).target, "wolf")

    def test_a_non_attack_never_redirects_to_a_non_hostile_creature(self):
        verdict = resolve_action_target(self._arena(), "thane", "i study thane", False)
        self.assertEqual((verdict.target, verdict.current_target, verdict.assaulting), ("wolf", "wolf", False))

    def test_an_attack_on_a_named_non_hostile_creature_is_an_assault(self):
        verdict = resolve_action_target(self._arena(), "thane", "i hit thane", True)
        self.assertEqual((verdict.target, verdict.current_target, verdict.assaulting), ("thane", "thane", True))
        self.assertFalse(verdict.inferred)

    def test_a_named_victim_is_never_asked_about_however_weak_the_match(self):
        # "trip silas" (0.645) still lands -- naming the victim is intent enough.
        scene = FakeActionTargetScene({"silas": _creature("Silas")})
        verdict = resolve_action_target(scene, "silas", "trip silas", True, score=0.5)
        self.assertEqual((verdict.assaulting, verdict.confirm_first), (True, False))

    def _market(self, **kwargs):
        entities = {
            "merchant": _creature("Marla", ["spice merchant", "spice", "merchant"], gender="female"),
            "guard": _creature("Belor", gender="male"),
        }
        return FakeActionTargetScene(entities, **kwargs)

    def test_an_attack_that_named_nobody_finds_who_its_words_describe(self):
        verdict = resolve_action_target(self._market(), None, "pin the merchant's feet", True)
        self.assertEqual((verdict.target, verdict.assaulting, verdict.inferred), ("merchant", True, False))

    def test_a_single_modifier_word_is_not_a_name(self):
        verdict = resolve_action_target(self._market(), None, "kick the spice cart", True)
        self.assertEqual((verdict.target, verdict.no_opponent), (None, True))

    def test_a_pronoun_means_the_one_person_it_can_only_mean(self):
        verdict = resolve_action_target(self._market(), None, "kick her into the street", True, score=0.9)
        self.assertEqual((verdict.target, verdict.assaulting, verdict.inferred), ("merchant", True, True))

    def test_a_pronoun_with_two_candidates_means_nobody(self):
        scene = self._market()
        scene.entities["baker"] = _creature("Tilda", gender="female")
        scene.scenario_entities.append("baker")
        verdict = resolve_action_target(scene, None, "kick her into the street", True, score=0.9)
        self.assertEqual((verdict.target, verdict.no_opponent), (None, True))

    def test_a_pronoun_never_picks_a_party_member(self):
        scene = self._market(party={"merchant"})
        self.assertIsNone(resolve_action_target(scene, None, "kick her", True, score=0.9).target)

    def test_the_conversation_partner_is_the_last_resort(self):
        verdict = resolve_action_target(self._market(partner_key="guard"), None, "my turn to hit you!", True, score=0.9)
        self.assertEqual((verdict.target, verdict.assaulting, verdict.inferred), ("guard", True, True))

    def test_an_inferred_victim_on_a_weak_match_is_asked_about_not_attacked(self):
        scene = self._market(partner_key="guard", current_target="merchant")
        verdict = resolve_action_target(
            scene, None, "use the fire for dramatic effect", True, score=ASSAULT_CONFIRM_SCORE - 0.01,
        )
        self.assertEqual((verdict.target, verdict.confirm_first), ("guard", True))
        self.assertEqual(verdict.current_target, "merchant")  # nothing was applied

    def test_an_inferred_victim_at_the_bar_is_attacked(self):
        verdict = resolve_action_target(
            self._market(partner_key="guard"), None, "my turn to hit you!", True, score=ASSAULT_CONFIRM_SCORE,
        )
        self.assertFalse(verdict.confirm_first)

    def test_an_attack_with_no_one_to_hit_says_so_and_spares_a_leftover_ally(self):
        scene = FakeActionTargetScene({"thane": _creature("Thane")}, current_target="thane", party={"thane"})
        verdict = resolve_action_target(scene, None, "strike them until they drop!", True)
        self.assertEqual((verdict.target, verdict.no_opponent, verdict.assaulting), (None, True, False))
        self.assertEqual(verdict.current_target, "thane")

    def test_an_object_is_fair_game_with_no_fight_on(self):
        scene = FakeActionTargetScene({"crate": {"name": "Crate", "supertype": "object"}}, current_target="crate")
        verdict = resolve_action_target(scene, None, "smash it", True)
        self.assertEqual((verdict.target, verdict.no_opponent, verdict.assaulting), ("crate", False, False))

    def test_a_non_attack_said_to_someone_else_is_incidental_to_the_default_target(self):
        # Found by playtest: tapping the ring on the jailer's wrist rolled against the sheriff.
        scene = FakeActionTargetScene(
            {"sheriff": _creature("Belor"), "jailer": _creature("Dane")}, current_target="sheriff",
        )
        verdict = resolve_action_target(scene, None, "casually reach out, tapping the heavy metal ring", False)
        self.assertTrue(verdict.incidental)
        self.assertFalse(resolve_action_target(scene, "jailer", "tap the ring", False).incidental)
        self.assertFalse(resolve_action_target(self._arena(), None, "study the room", False).incidental)


class TestChallengeRating(unittest.TestCase):
    """!
    @brief Challenge_Rating.py's pure "how powerful is this entity" math -- no DMCore, same
        independence Character_Creation.py's own TestCharacterCreation exercises above.
    """

    def test_skill_rating_converts_pips_to_the_shared_pip_scale(self):
        self.assertEqual(skill_rating(dice=5, pips=0), 15)
        self.assertEqual(skill_rating(dice=2, pips=2), 8)
        self.assertEqual(skill_rating(dice=0, pips=0), 0)

    def test_calculate_challenge_rating_save_component_averages_across_every_save_given(self):
        # offense_side has to be nonzero for survival_side's own components to show up at all
        # (CR = round(2*sqrt(offense_side * survival_side)) -- zero on either side zeroes the
        # whole thing, see Challenge_Rating.py's own module docstring), so every test below
        # gives offense a real, nonzero baseline rather than isolating a survival-side component
        # against an all-zero rest the way the old flat-sum formula could.
        # offense_side: blades 5D=15. One real save (fortitude 5D=15) plus two absent ones ({}
        # -- untrained, rating 0) -- the average has to be taken across all three slots (5, not
        # 15), the same way an entity missing two of Pathfinder's own three saves reads as
        # genuinely easier to lock down with a save-or-suck effect, not merely "unrated" on
        # those two. survival_side = save_component(5) + defense(0) + hp(0) = 5.
        # CR = round(2*sqrt(15*5)) = round(2*8.660) = 17.
        offense_skill = {"dice": 5, "pips": 0}
        save_ratings = [{"dice": 5, "pips": 0}, {}, {}]
        rating = calculate_challenge_rating(offense_skill, 0, 0, {}, save_ratings, max_hp=0)
        self.assertEqual(rating, 17)

    def test_calculate_challenge_rating_combines_offense_and_survival_by_twice_their_geometric_mean(self):
        # offense_side: blades 5D=15 + damage 5D=15 -> 30. survival_side: dodge 5D=15 + save
        # (fortitude 4D=12, willpower 2D=6, reflexes absent=0 -> round(18/3)=6) + hp (36//3=12)
        # -> 15+6+12=33. CR = round(2*sqrt(30*33)) = round(2*31.464) = 63 -- close to the old
        # flat-sum total (30+15+6+12=63, identical here) because this build is nearly balanced
        # (offense_side 30 vs survival_side 33) -- AM-GM's whole point is that a balanced split
        # keeps its old additive-scale value; see the "no trained skills" test below for what a
        # lopsided one does instead.
        offense_skill = {"dice": 5, "pips": 0}
        defense_skill = {"dice": 5, "pips": 0}
        save_ratings = [{"dice": 4, "pips": 0}, {"dice": 2, "pips": 0}, {}]
        rating = calculate_challenge_rating(offense_skill, 5, 0, defense_skill, save_ratings, max_hp=36)
        self.assertEqual(rating, 63)

    def test_calculate_challenge_rating_zero_offense_side_is_always_zero_regardless_of_survival_side(self):
        # No offense-tagged skill trained and no damage at all -> offense_side = 0 -> CR = 0 no
        # matter how much HP/defense/save survival_side has -- a creature that can never deal
        # damage poses no combat danger, by construction (Challenge_Rating.py's own module
        # docstring); this is the deliberate, disclosed behavior change from the old flat-sum
        # formula, where a durable-but-harmless entity still accumulated CR from HP alone.
        self.assertEqual(calculate_challenge_rating({}, 0, 0, {}, [], max_hp=1000), 0)

    def test_calculate_challenge_rating_handles_an_entity_with_no_trained_skills(self):
        # offense_side: no trained skill, but damage_dice/pips=1/1 given directly ->
        # skill_rating(1,1)=4. survival_side: hp 9//3=3. CR = round(2*sqrt(4*3)) = round(6.928) = 7.
        self.assertEqual(calculate_challenge_rating({}, 1, 1, {}, [], max_hp=9), 7)

    def test_calculate_challenge_rating_rewards_balance_over_skew_at_equal_totals(self):
        # The actual property the multiplicative combination was built for (Challenge_Rating.py's
        # own module docstring): hold offense_side + survival_side fixed at the same total (32
        # here), and a balanced split scores strictly higher than a lopsided one -- AM-GM
        # (2*sqrt(A*B) <= A+B, equality only at A == B). This is a direct, unit-level proof of
        # the fix, independent of the Monte Carlo simulator's own (noisier) win-rate evidence.
        balanced = calculate_challenge_rating({"dice": 5, "pips": 1}, 0, 0, {}, [], max_hp=48)  # 16 & 16
        skewed = calculate_challenge_rating({"dice": 9, "pips": 0}, 0, 0, {}, [], max_hp=15)  # 27 & 5, same total (32)
        self.assertGreater(balanced, skewed)

    def test_calculate_party_challenge_rating_is_the_sum_not_the_average(self):
        self.assertEqual(calculate_party_challenge_rating([41, 26, 21]), 88)
        self.assertEqual(calculate_party_challenge_rating([]), 0)


# A minimal skills_catalog fixture for Combat_Simulator's own tests -- unlike NpcGeneration's
# own FAKE_SKILLS_CATALOG (below), this one authors "opposes" too, since resolve_opposed_action
# needs it to find a real defending skill (an offense skill with no "opposes" entry always rolls
# against difficulty 0, per Combat_Resolution.get_opposing_skill/resolve_opposed_action -- fine
# for CR math, which never resolves an actual roll, but not for a simulated fight that does).
SIM_SKILLS_CATALOG = {
    "blades": {"combat_role": "offense", "opposes": ["dodge"]},
    "dodge": {"combat_role": "defense", "opposes": ["blades"]},
}


class TestCombatSimulator(unittest.TestCase):
    """!
    @brief Combat_Simulator.py's pure Monte Carlo fight resolution -- no DMCore/live LLM,
        exercised directly against bare entity dicts the same way TestNpcGeneration exercises
        NPC_Generation.py's own pure functions.
    """

    def setUp(self):
        random.seed(1234)  # deterministic across environments for the statistical assertions below

    @staticmethod
    def _fighter(name, dice, pips, max_hp, damage_dice=0, damage_pips=0):
        return {
            "name": name,
            "skills": {"blades": {"dice": dice, "pips": pips}, "dodge": {"dice": dice, "pips": pips}},
            "max_hp": max_hp,
            "damage_value": {"dice": damage_dice, "pips": damage_pips, "bonus": 0},
            "damage_tags": ["slashing"],
        }

    def test_best_offense_skill_picks_the_highest_rated_offense_tagged_skill(self):
        entity = {"skills": {"blades": {"dice": 2, "pips": 0}, "dodge": {"dice": 5, "pips": 0}}}
        self.assertEqual(best_offense_skill(entity, SIM_SKILLS_CATALOG), "blades")

    def test_best_offense_skill_is_none_with_no_offense_tagged_skill_trained(self):
        entity = {"skills": {"dodge": {"dice": 5, "pips": 0}}}
        self.assertIsNone(best_offense_skill(entity, SIM_SKILLS_CATALOG))

    def test_overwhelming_offense_beats_a_helpless_defender(self):
        strong = lambda: self._fighter("strong", dice=8, pips=0, max_hp=30, damage_dice=4, damage_pips=0)
        helpless = lambda: {"name": "helpless", "skills": {}, "max_hp": 6}
        result = run_matchup(strong, helpless, rules={}, skills_catalog=SIM_SKILLS_CATALOG, trials=50)
        self.assertGreater(result["a_win_rate"], 0.9)

    def test_simulate_fight_terminates_within_max_rounds_when_neither_side_can_land_a_hit(self):
        entities = {
            "a": {"name": "a", "skills": {}, "max_hp": 10},
            "b": {"name": "b", "skills": {}, "max_hp": 10},
        }
        outcome = simulate_fight(entities, {}, SIM_SKILLS_CATALOG, "a", "b", ValidatingEventBus(), max_rounds=5)
        self.assertTrue(outcome["timeout"])
        self.assertIsNone(outcome["winner"])
        self.assertEqual(outcome["rounds"], 5)

    def test_run_matchup_win_rates_are_bounded_and_account_for_timeouts(self):
        build_a = lambda: self._fighter("a", dice=4, pips=0, max_hp=20, damage_dice=2, damage_pips=0)
        build_b = lambda: self._fighter("b", dice=3, pips=0, max_hp=15, damage_dice=1, damage_pips=0)
        result = run_matchup(build_a, build_b, rules={}, skills_catalog=SIM_SKILLS_CATALOG, trials=100)
        for rate in (result["a_win_rate"], result["b_win_rate"], result["timeout_rate"]):
            self.assertGreaterEqual(rate, 0)
            self.assertLessEqual(rate, 1)
        self.assertAlmostEqual(
            result["a_win_rate"] + result["b_win_rate"] + result["timeout_rate"], 1.0, places=9,
        )

    def test_identical_builds_land_close_to_an_even_split(self):
        build = lambda: self._fighter("fighter", dice=4, pips=0, max_hp=20, damage_dice=2, damage_pips=0)
        # Both sides use the same build factory but need distinct names -- run_matchup keys
        # entities by each build's own "name", so a genuinely identical build has to be cloned
        # with the other side's name swapped in, not the literal same dict twice.
        def build_a():
            entity = build()
            entity["name"] = "a"
            return entity

        def build_b():
            entity = build()
            entity["name"] = "b"
            return entity

        result = run_matchup(build_a, build_b, rules={}, skills_catalog=SIM_SKILLS_CATALOG, trials=300)
        self.assertLess(abs(result["a_win_rate"] - result["b_win_rate"]), 0.15)


class TestChallengeRatingDMCoreIntegration(DMTestCase):
    """!
    @brief get_challenge_rating/get_party_challenge_rating (Combat_Actions.py) against debug.toml's
        real gladstone/thane/wolf data -- confirms the DMCore-side glue (finding each entity's
        best offense package, its own combat_role-tagged defense/save skills, filtering the
        party by is_player/is_party) feeds Challenge_Rating.py's pure math the right numbers,
        not just that the math itself is right (TestChallengeRating already covers that in
        isolation).
    """

    def test_gladstone_rating_picks_arcane_and_fireball_as_the_best_offense_package(self):
        # _best_offense_package maximizes skill+damage together, not damage alone -- arcane
        # 2D=6 + fireball's own 5D=15 (=21) edges out blades 5D=15 + longsword's 1D+2=5 (=20),
        # even though the longsword's own *skill* is rated higher on its own. offense_side=21.
        # survival_side: dodge 5D=15 + save (fortitude/reflexes/willpower all 2D=6 each -> avg
        # 6) + hp (36//3=12) -> 33. CR = round(2*sqrt(21*33)) = round(2*26.32) = 53.
        self.assertEqual(Combat_Actions.get_challenge_rating(self.dm_core.world, "gladstone"), 53)

    def test_thane_rating_uses_his_own_best_offense_package(self):
        # offense_side: one of his 4D=12 combat skills + shortsword strike's own 2D=6 -> 18.
        # survival_side: dodge 3D=9 + save (fortitude 3D=9, reflexes/willpower 2D=6 each -> avg
        # 7) + hp (24//3=8) -> 24. CR = round(2*sqrt(18*24)) = round(2*20.78) = 42.
        self.assertEqual(Combat_Actions.get_challenge_rating(self.dm_core.world, "thane"), 42)

    def test_wolf_rating_uses_its_own_bite(self):
        # offense_side: brawling 5D=15 + bite's own 1D=3 -> 18. survival_side: dodge 6D=18 +
        # save (fortitude/reflexes/willpower all 2D=6 each -> avg 6) + hp (16//3=5) -> 29.
        # CR = round(2*sqrt(18*29)) = round(2*22.85) = 46.
        self.assertEqual(Combat_Actions.get_challenge_rating(self.dm_core.world, "wolf"), 46)

    def test_unknown_entity_rates_zero(self):
        self.assertEqual(Combat_Actions.get_challenge_rating(self.dm_core.world, "nobody"), 0)

    def test_party_rating_sums_gladstone_and_thane_but_not_the_wolves(self):
        self.assertEqual(Combat_Actions.get_party_challenge_rating(self.dm_core.world), 53 + 42)


class TestXpAward(DMTestCase):
    """!
    @brief _award_xp_for_defeat (Combat_Actions.py), triggered from calculate_damage the moment a
        hostile entity's HP first reaches 0 -- debug.toml's own gladstone (is_player, starts
        with exp = 100)/thane (is_party, no authored "exp" -- starts at the implicit 0) and its
        first wolf (hostile by default, challenge rating 46 -- TestChallengeRatingDMCoreIntegration).
    """

    def _drop_the_wolf_to_one_hp(self):
        self.dm_core.entities["wolf"]["hp"] = 1

    def _deal_five_damage(self, attacker="gladstone", defender="wolf"):
        # Untagged, so nothing on the defender's own resistance/armor ever reduces it -- keeps
        # every test's own net_damage a fixed, known 5 regardless of which entity is targeted.
        Combat_Actions.calculate_damage(self.dm_core.world, attacker, defender, {"damage_value": {"dice": 0, "pips": 0, "bonus": 5}, "damage_tags": []})

    def test_defeating_a_hostile_entity_awards_its_challenge_rating_as_xp_by_default(self):
        self._drop_the_wolf_to_one_hp()
        self._deal_five_damage()

        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "wolf"), 0)
        self.assertEqual(self.dm_core.entities["gladstone"]["exp"], 10 + 46)
        self.assertEqual(self.dm_core.entities["thane"]["exp"], 46)

    def test_custom_exp_field_overrides_the_challenge_rating_default(self):
        self.dm_core.entities["wolf"]["exp"] = 5
        self._drop_the_wolf_to_one_hp()
        self._deal_five_damage()

        self.assertEqual(self.dm_core.entities["gladstone"]["exp"], 10 + 5)

    def test_an_authored_exp_of_zero_grants_no_xp_at_all(self):
        # Presence, not truthiness -- an authored 0 is a deliberate "worth nothing" override,
        # distinct from never authoring "exp" at all (which falls back to the challenge rating).
        self.dm_core.entities["wolf"]["exp"] = 0
        self._drop_the_wolf_to_one_hp()
        self._deal_five_damage()

        self.assertEqual(self.dm_core.entities["gladstone"]["exp"], 10)
        self.assertEqual(self.dm_core.entities["thane"].get("exp", 0), 0)

    def test_xp_multiplier_scales_the_award(self):
        self.dm_core.rules["xp"]["xp_multiplier"] = 3
        self._drop_the_wolf_to_one_hp()
        self._deal_five_damage()

        self.assertEqual(self.dm_core.entities["gladstone"]["exp"], 10 + 46 * 3)

    def test_divide_between_party_splits_the_award_evenly_by_floor_division(self):
        self.dm_core.rules["xp"]["divide_between_party"] = True
        self._drop_the_wolf_to_one_hp()
        self._deal_five_damage()

        # 46 // 2 party members (gladstone, thane) = 23 each, not 46 each.
        self.assertEqual(self.dm_core.entities["gladstone"]["exp"], 10 + 23)
        self.assertEqual(self.dm_core.entities["thane"]["exp"], 23)

    def test_a_second_hit_against_an_already_dead_entity_awards_no_further_xp(self):
        self._drop_the_wolf_to_one_hp()
        self._deal_five_damage()
        gladstone_exp_after_the_kill = self.dm_core.entities["gladstone"]["exp"]

        self._deal_five_damage()  # the wolf is already at 0 HP -- previous_hp is 0, not > 0

        self.assertEqual(self.dm_core.entities["gladstone"]["exp"], gladstone_exp_after_the_kill)

    def test_defeating_a_non_hostile_entity_awards_no_xp(self):
        # thane is is_party, friendly disposition -- never hostile toward the player, so his
        # own defeat (however it happened) is never treated as a party accomplishment.
        self.dm_core.entities["thane"]["hp"] = 1
        self._deal_five_damage(attacker="wolf", defender="thane")

        self.assertEqual(self.dm_core.entities["gladstone"]["exp"], 10)
        self.assertEqual(self.dm_core.entities["thane"].get("exp", 0), 0)

    def test_a_passed_entity_test_with_no_xp_key_awards_nothing(self):
        # items.toml's own chest lock -- [entity.test.pass] is only {dismiss_condition =
        # "locked"}, no "xp" key -- proving apply_test_outcome's own xp handling is genuinely
        # opt-in per outcome, not automatic for every passed [entity.test] (ex:
        # TestMultiRoomDungeon's own dart trap, which does author xp = true).
        self.dm_core.apply_test_outcome("chest", {"dismiss_condition": "locked"})

        self.assertEqual(self.dm_core.entities["gladstone"]["exp"], 10)
        self.assertEqual(self.dm_core.entities["thane"].get("exp", 0), 0)


class FakeCombatHooks(CombatHooks):
    """!@brief CombatHooks that record every call -- Combat_Actions driven with no DMCore."""

    def __init__(self, hostile=()):
        self.hostile = set(hostile)
        self.calls = []

    def is_hostile(self, entity_name, toward_name):
        return entity_name in self.hostile

    def note_kill(self, killer, victim):
        self.calls.append(("note_kill", killer, victim))

    def nudge_combat_hit_attitude(self, target_name, attacker_name, net_damage):
        self.calls.append(("nudge_combat_hit_attitude", target_name, attacker_name, net_damage))

    def nudge_attitude_from_event(self, entity_name, toward_name, event_name, magnitude):
        self.calls.append(("nudge_attitude_from_event", entity_name, toward_name, event_name))

    def move_toward_or_away(self, entity_name, opponent_name, direction):
        self.calls.append(("move", entity_name, opponent_name, direction))

    def transfer_item(self, from_name, to_name, item_name):
        self.calls.append(("transfer_item", from_name, to_name, item_name))
        return True


class TestCombatActionsWithoutDMCore(unittest.TestCase):
    """!
    @brief resolution/Combat_Actions.py over a bare WorldContext and a fake CombatHooks -- the
        parts of combat that used to need a booted DMCore.
    """

    HIT = {"damage_value": {"dice": 0, "pips": 0, "bonus": 10}, "damage_tags": []}

    def _world(self, hostile=("orc",), **entities):
        base = {
            "hero": {"name": "Hero", "max_hp": 10, "hp": 10, "band": 1, "skills": {}},
            "orc": {"name": "Orc", "max_hp": 5, "hp": 5, "band": 1, "skills": {}},
        }
        base.update(entities)
        self.hooks = FakeCombatHooks(hostile=hostile)
        return WorldContext(
            base, event_bus=ValidatingEventBus(), scenario_entities=list(base), player_name="hero", hooks=self.hooks,
        )

    def test_a_killing_blow_reports_the_kill_through_the_hooks(self):
        ctx = self._world()
        result = Combat_Actions.calculate_damage(ctx, "hero", "orc", self.HIT)

        self.assertEqual(result["remaining_hp"], 0)
        self.assertIn(("note_kill", "hero", "orc"), self.hooks.calls)

    def test_a_blow_that_does_not_kill_reports_nothing(self):
        ctx = self._world()
        Combat_Actions.calculate_damage(ctx, "hero", "orc", {"damage_value": {"dice": 0, "pips": 0, "bonus": 1}, "damage_tags": []})
        self.assertEqual(self.hooks.calls, [])

    def test_hitting_an_already_dead_target_is_not_a_second_kill(self):
        ctx = self._world(orc={"name": "Orc", "max_hp": 5, "hp": 0, "band": 1, "skills": {}})
        Combat_Actions.calculate_damage(ctx, "hero", "orc", self.HIT)
        self.assertEqual(self.hooks.calls, [])

    def test_create_spawn_is_stashed_on_the_corpse_when_its_requirements_hold(self):
        ctx = self._world(orc={"name": "Orc", "max_hp": 5, "hp": 5, "band": 2, "subtype": "humanoid", "skills": {}})
        ability = {**self.HIT, "create_spawn": {
            "name": "wight", "delay_rounds": 3,
            "requirements": [{"field": "subtype", "operator": "==", "value": "humanoid"}],
        }}
        Combat_Actions.calculate_damage(ctx, "hero", "orc", ability)

        self.assertEqual(ctx.entities["orc"]["pending_spawn"], {"name": "wight", "band": 2, "rounds_remaining": 3})

    def test_an_enemies_only_area_ability_skips_the_allies_standing_beside_the_target(self):
        ctx = self._world(
            hostile=("orc", "goblin"),
            goblin={"name": "Goblin", "max_hp": 3, "hp": 3, "band": 1, "skills": {}},
            ally={"name": "Ally", "max_hp": 3, "hp": 3, "band": 1, "skills": {}},
        )
        ability = {"targets": {"number": 0, "aoe": 0, "side": "enemies"}}

        targets = Combat_Actions.resolve_targets(ctx, "hero", "orc", ability)

        self.assertEqual(targets[0], "orc")
        self.assertIn("goblin", targets)
        self.assertNotIn("ally", targets)

    def test_a_self_sided_ability_targets_only_the_caster(self):
        ctx = self._world()
        self.assertEqual(
            Combat_Actions.resolve_targets(ctx, "hero", None, {"targets": {"side": "self"}}), ["hero"],
        )

    def test_a_stunned_entity_cannot_act(self):
        ctx = self._world()
        Combat_Resolution.apply_condition(ctx, "orc", "stunned", duration="rounds", length=1, dismiss="")
        ctx.rules["condition"] = [{"name": "stunned", "prevents_action": True}]

        self.assertTrue(Combat_Actions.is_action_prevented(ctx, "orc"))
        self.assertFalse(Combat_Actions.is_action_prevented(ctx, "hero"))


if __name__ == "__main__":
    unittest.main()
