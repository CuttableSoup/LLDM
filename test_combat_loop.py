import unittest
from unittest.mock import patch
from Event_Bus import EventBus
from DM_Core import DMCore


class TestCombatLoop(unittest.TestCase):
    def setUp(self):
        self.event_bus = EventBus()
        self.resolved = []
        self.event_bus.subscribe("action_resolved", self.resolved.append)
        self.dm_core = DMCore(self.event_bus)

    def test_find_attack_ability_prefers_equipped_weapon(self):
        # Gladstone has a longsword equipped in rhand, which uses the "blades" skill.
        ability = self.dm_core.find_attack_ability("gladstone", "blades")
        self.assertIsNotNone(ability)
        self.assertEqual(ability["name"], "longsword")

    def test_find_attack_ability_falls_back_to_innate_ability(self):
        # No equipped weapon uses "brawling", so the innate "punch" ability should be found instead.
        ability = self.dm_core.find_attack_ability("gladstone", "brawling")
        self.assertIsNotNone(ability)
        self.assertEqual(ability["name"], "punch")

    def test_find_attack_ability_returns_none_for_unmatched_skill(self):
        self.assertIsNone(self.dm_core.find_attack_ability("gladstone", "arcane"))

    def test_missed_attack_does_not_apply_damage(self):
        # wolf's dodge (6 dice) will always beat gladstone's blades (2 dice) at this fixed roll.
        with patch("random.randint", return_value=1):
            self.dm_core._on_action_detected({"skill": "blades", "input": "I attack with my sword"})

        result = self.resolved[-1]
        self.assertFalse(result["success"])
        self.assertNotIn("damage", result)
        self.assertEqual(self.dm_core.get_current_hp("wolf"), 16)

    def test_successful_attack_applies_damage_to_the_target(self):
        # Give the player an opponent with no matching opposing skill, so the attack auto-succeeds (difficulty 0).
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self.dm_core.scenario = {"entities": [{"name": "practice_dummy", "band": 0}]}

        with patch("random.randint", return_value=3):
            self.dm_core._on_action_detected({"skill": "blades", "input": "I attack with my sword"})

        result = self.resolved[-1]
        self.assertTrue(result["success"])
        self.assertIn("damage", result)
        self.assertEqual(result["damage"]["defender"], "practice_dummy")
        self.assertGreater(result["damage"]["net_damage"], 0)
        self.assertEqual(
            self.dm_core.get_current_hp("practice_dummy"),
            20 - result["damage"]["net_damage"],
        )

    def test_non_attack_skill_never_applies_damage(self):
        # "athletics" has no equipped weapon or innate ability tied to it, so no damage should occur even on a hit.
        self.dm_core.entities["practice_dummy"] = {"name": "practice_dummy", "max_hp": 20, "skills": {}}
        self.dm_core.scenario = {"entities": [{"name": "practice_dummy", "band": 0}]}

        self.dm_core._on_action_detected({"skill": "athletics", "input": "I climb the wall"})

        result = self.resolved[-1]
        self.assertNotIn("damage", result)


if __name__ == "__main__":
    unittest.main()
