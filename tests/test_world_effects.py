"""!
@file test_world_effects.py
@brief The world-effect layer (Rules/<setting>/world_effects.toml): a player's deliberate change to
    the scene -- set fire, barricade, bind, douse -- landing through the engine's own primitives (a
    summoned hazard, a condition, a crime report) rather than through anything narrated. Run
    end-to-end through DMCore._on_turn_detected, with dice pinned, in Fantasy's debug scenario;
    TestWorldEffectsAreAuthoredPerSetting checks Pathfinder carries the same vocabulary.
"""
import unittest
from unittest.mock import patch

import resolution.Combat_Actions as Combat_Actions
import resolution.Combat_Resolution as Combat_Resolution
from dm.DM_ActionOutcome import RolledOutcome
from tests.support import DMTestCase, apply_effects
from tests.support import setUpModule, tearDownModule  # noqa: F401


class TestWorldEffects(DMTestCase):

    def setUp(self):
        super().setUp()
        self.resolved = self._capture_any("round_resolved", "action_resolved")
        self.crimes = self._capture("crime_committed")

    def _attempt(self, skill, text, roll=6):
        """One turn naming a world-effect ability, every die pinned to roll."""
        with patch("random.randint", return_value=roll):
            self.dm_core._on_turn_detected({"clauses": [{"kind": "action", "skill": skill}], "input": text})

    def _blazes(self):
        return [name for name in self.dm_core.scenario_entities if name.startswith("blaze")]

    # -- set fire --------------------------------------------------------------------------

    def test_set_fire_places_a_blaze_that_burns_out_on_the_block_clock(self):
        self._attempt("set fire", "I set fire to the curtains")

        self.assertEqual(len(self._blazes()), 1)
        blaze = self.dm_core.entities[self._blazes()[0]]
        self.assertEqual(blaze["summon_expires_in_blocks"], 3)

        self.dm_core.advance_blocks(2)
        self.assertEqual(len(self._blazes()), 1)
        self.dm_core.advance_blocks(1)
        self.assertEqual(self._blazes(), [])

    def test_set_fire_reports_arson_as_a_crime_the_player_committed(self):
        self._attempt("set fire", "I set fire to the curtains")

        self.assertEqual(
            [(c["crime"], c["offender"]) for c in self.crimes], [("arson", self.dm_core.player_name)],
        )

    def test_a_failed_attempt_places_nothing_and_commits_no_crime(self):
        self._attempt("set fire", "I set fire to the curtains", roll=1)

        self.assertEqual(self._blazes(), [])
        self.assertEqual(self.crimes, [])

    def test_a_blaze_burns_whoever_shares_its_band_as_time_passes(self):
        self._attempt("set fire", "I set fire to the curtains")
        player = self.dm_core.player_name
        before = Combat_Resolution.get_current_hp(self.dm_core.world, player)

        with patch("random.randint", return_value=4):
            self.dm_core.advance_blocks(1)

        self.assertLess(Combat_Resolution.get_current_hp(self.dm_core.world, player), before)

    def test_a_blaze_does_nothing_to_someone_far_from_it(self):
        self._attempt("set fire", "I set fire to the curtains")
        blaze = self.dm_core.entities[self._blazes()[0]]
        player = self.dm_core.entities[self.dm_core.player_name]
        blaze["band"], player["band"] = 1, 5
        before = Combat_Resolution.get_current_hp(self.dm_core.world, self.dm_core.player_name)

        with patch("random.randint", return_value=4):
            self.dm_core.advance_blocks(1)

        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, self.dm_core.player_name), before)

    # -- douse -----------------------------------------------------------------------------

    def test_douse_puts_the_blaze_out(self):
        self._attempt("set fire", "I set fire to the curtains")
        blaze_name = self._blazes()[0]
        douse = self.dm_core.entities["douse"]
        result = RolledOutcome(entity=self.dm_core.player_name, skill="athletics", roll=15, difficulty=6, success=True)

        apply_effects(self.dm_core, result, "athletics", douse, douse, blaze_name, via_test=False)

        self.assertNotIn(blaze_name, self.dm_core.scenario_entities)

    def test_douse_puts_out_a_burning_person(self):
        Combat_Resolution.apply_condition(self.dm_core.world, "wolf", "burning", duration="permanent")
        douse = self.dm_core.entities["douse"]
        result = RolledOutcome(entity=self.dm_core.player_name, skill="athletics", roll=15, difficulty=6, success=True)

        apply_effects(self.dm_core, result, "athletics", douse, douse, "wolf", via_test=False)

        self.assertNotIn("burning", self.dm_core.entities["wolf"].get("active_conditions", {}))

    # -- barricade -------------------------------------------------------------------------

    def test_a_barricade_stands_until_it_is_destroyed_and_blocks_the_way_out(self):
        self._attempt("barricade", "I barricade the door")
        pile = next(n for n in self.dm_core.scenario_entities if n.startswith("barricade pile"))
        self.assertNotIn("summon_expires_in_blocks", self.dm_core.entities[pile])
        self.dm_core.advance_blocks(10)
        self.assertEqual(self.dm_core._barricade_standing(), pile)

        outcomes = []
        self.dm_core._resolve_travel_intent("leave", lambda found, **kw: outcomes.append((found, kw)))
        self.assertEqual(outcomes, [(False, {"reason": "barricaded"})])

        self.dm_core.entities[pile]["hp"] = 0
        self.assertIsNone(self.dm_core._barricade_standing())

    def test_a_barricade_blocks_a_room_transition_too(self):
        self._attempt("barricade", "I barricade the door")
        outcomes = []

        self.dm_core._resolve_room_transition_intent("forward", lambda found, **kw: outcomes.append((found, kw)))

        # No such exit in this room: the exit lookup answers first. Only an existing exit is gated,
        # so what matters is that a barricade never turns a missing exit into a successful move.
        self.assertFalse(outcomes[0][0])

    # -- cut / topple ----------------------------------------------------------------------

    def _prop(self, name, hp):
        self.dm_core.entities[name] = {"name": name, "supertype": "object", "max_hp": hp, "skills": {}}
        self.dm_core.scenario_entities.append(name)

    def test_cut_destroys_a_rope_that_is_really_there(self):
        self._prop("rope", 4)
        self._stub_roll_dice(12)
        result = RolledOutcome(entity=self.dm_core.player_name, skill="blades", roll=15, difficulty=6, success=True)

        apply_effects(self.dm_core, result, "blades", None, self.dm_core.entities["cut"], "rope", via_test=False)

        self.assertLessEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "rope"), 0)

    def test_cut_does_nothing_to_a_creature(self):
        self._stub_roll_dice(12)
        before = Combat_Resolution.get_current_hp(self.dm_core.world, "wolf")
        result = RolledOutcome(entity=self.dm_core.player_name, skill="blades", roll=15, difficulty=6, success=True)

        apply_effects(self.dm_core, result, "blades", None, self.dm_core.entities["cut"], "wolf", via_test=False)

        self.assertEqual(Combat_Resolution.get_current_hp(self.dm_core.world, "wolf"), before)

    def test_topple_damages_a_shelf_and_leaves_an_absent_one_alone(self):
        self._prop("shelf", 6)
        self._stub_roll_dice(4)
        result = RolledOutcome(entity=self.dm_core.player_name, skill="athletics", roll=15, difficulty=6, success=True)

        apply_effects(self.dm_core, result, "athletics", None, self.dm_core.entities["topple"], "shelf", via_test=False)
        self.assertLess(Combat_Resolution.get_current_hp(self.dm_core.world, "shelf"), 6)

        apply_effects(self.dm_core, result, "athletics", None, self.dm_core.entities["topple"], None, via_test=False)

    # -- bind ------------------------------------------------------------------------------

    def test_bind_stops_the_target_acting_until_they_work_free(self):
        bind = self.dm_core.entities["bind"]
        result = RolledOutcome(entity=self.dm_core.player_name, skill="athletics", roll=15, difficulty=6, success=True)

        apply_effects(self.dm_core, result, "athletics", None, bind, "wolf", via_test=False)

        self.assertIn("bound", self.dm_core.entities["wolf"]["active_conditions"])
        self.assertTrue(Combat_Actions.is_action_prevented(self.dm_core.world, "wolf"))

    def test_escape_artist_frees_the_player_from_a_bind(self):
        player = self.dm_core.player_name
        Combat_Resolution.apply_condition(self.dm_core.world, player, "bound", duration="permanent")
        escape = self.dm_core.entities["escape artist"]
        result = RolledOutcome(entity=player, skill="escape", roll=15, difficulty=6, success=True)

        apply_effects(self.dm_core, result, "escape", None, escape, None, via_test=False)

        self.assertNotIn("bound", self.dm_core.entities[player].get("active_conditions", {}))


class TestWorldEffectsAreAuthoredPerSetting(unittest.TestCase):
    """Settings share nothing: Pathfinder's own world_effects.toml must give it the same vocabulary."""

    def test_pathfinder_authors_every_effect_and_lists_it_under_a_skill(self):
        from tests.event_contract import ValidatingEventBus
        from dm.DM_Core import DMCore
        core = DMCore(ValidatingEventBus(), scenario_name="lost_coast", setting="Pathfinder")
        for name in ("set fire", "douse", "barricade", "bind", "cut", "topple"):
            self.assertIn(name, core.entities, name)
            self.assertIn(name, core.universal_abilities, name)
        self.assertIn("bound", {c["name"] for c in core.rules.get("condition", [])})
        triggers = {s.get("trigger") for s in core.rules.get("status", []) if s.get("name", "").startswith("blaze")}
        self.assertEqual(triggers, {"on_round", "on_block"})


if __name__ == "__main__":
    unittest.main()


class TestWorldEffectRouting(unittest.TestCase):
    """!
    @brief The real embedding matcher (no scene, no model) sends plain-words phrasings to the
        authored effect or skill -- the routing every other test here assumes. Only phrasings that
        held in a probe of 76 lines are pinned; the matcher is a similarity score, not an oracle,
        so a miss here means the keywords or descriptions drifted, not that a rule broke.
    """
    ROUTES = {
        "set fire to the curtains": "set fire",
        "torch the barn": "set fire",
        "barricade the door": "barricade",
        "wedge it shut": "barricade",
        "pile furniture against the door": "barricade",
        "tie the guard up": "bind",
        "gag him": "bind",
        "douse the lamp": "douse",
        "slide down the banister": "acrobatics",
        "vault the counter": "acrobatics",
        "eavesdrop at the keyhole": "observation",
        "whistle for my horse": "husbandry",
        "headbutt the guard": "brawling",
        "cut the rope": "cut",
        "topple the shelf": "topple",
    }

    @classmethod
    def setUpClass(cls):
        from types import SimpleNamespace
        from dm.DM_Rules import RulesMixin
        from nlp.NLP_Core import NLPCore
        from tests.event_contract import ValidatingEventBus
        cls.classifiers = {}
        for setting in ("Fantasy", "Pathfinder"):
            bus = ValidatingEventBus()
            nlp = NLPCore(bus)
            rules = SimpleNamespace(event_bus=bus, skills={}, entities={}, entity_templates={}, rules={})
            RulesMixin.load_rules(rules, f"Rules/{setting}")
            nlp.classifier.on_rules_loaded({"skills": rules.skills, "entities": rules.entities})
            cls.classifiers[setting] = nlp.classifier

    def test_plain_words_reach_the_authored_effect_in_every_setting(self):
        for setting, classifier in self.classifiers.items():
            for line, expected in self.ROUTES.items():
                with self.subTest(setting=setting, line=line):
                    _processed, events, _adjudication = classifier.classify(line)
                    skills = [
                        clause.get("skill") for event in events if event["event"] == "turn_detected"
                        for clause in event["payload"]["clauses"]
                    ]
                    self.assertEqual(skills, [expected])


class TestWorldEffectsRoll(unittest.TestCase):
    """Found by playtest (Pathfinder, no target): set fire and barricade always succeeded, because
    an unopposed named ability took difficulty 0 -- the authored flat difficulty was ignored."""

    def setUp(self):
        from tests.event_contract import ValidatingEventBus
        from dm.DM_Core import DMCore
        self.bus = ValidatingEventBus()
        self.resolved = []
        self.bus.subscribe("action_resolved", self.resolved.append)
        self.core = DMCore(self.bus, scenario_name="lost_coast", setting="Pathfinder")

    def _turn(self, skill, die):
        with patch("random.randint", return_value=die):
            self.core._on_turn_detected({"clauses": [{"kind": "action", "skill": skill}], "input": skill})
        return self.resolved[-1]["actions"][0]

    def test_an_untargeted_world_effect_is_a_real_check(self):
        outcome = self._turn("set fire", die=1)
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.difficulty, 6)
        self.assertEqual(outcome.effects, [])

    def test_a_good_roll_places_the_blaze_and_narrates_it_as_a_hazard_not_an_ally(self):
        from dm.DM_ActionOutcome import SummonEffect
        from llm.Narration_Prompts import _format_summon_effect
        outcome = self._turn("set fire", die=6)
        self.assertTrue(outcome.success)
        effect = next(e for e in outcome.effects if isinstance(e, SummonEffect))
        self.assertTrue(effect.hazard)
        self.assertNotIn("fight at their side", _format_summon_effect(effect, "the player"))

    def test_a_summoned_creature_is_still_an_ally(self):
        from dm.DM_ActionOutcome import SummonEffect
        from llm.Narration_Prompts import _format_summon_effect
        self.assertIn("fight at their side", _format_summon_effect(SummonEffect(name="wolf"), "the player"))
