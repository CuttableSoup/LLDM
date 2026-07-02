import unittest
from Event_Bus import EventBus
from DM_Core import DMCore


class TestOpposedResolution(unittest.TestCase):
    def setUp(self):
        self.event_bus = EventBus()
        self.dm_core = DMCore(self.event_bus)

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

        chosen = self.dm_core.get_opposing_skill("blades", "test_defender")
        self.assertEqual(chosen, "brawling")

        result = self.dm_core.resolve_opposed_action("gladstone", "blades", "test_defender")
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

        chosen = self.dm_core.get_opposing_skill("blades", "test_defender")
        self.assertEqual(chosen, "dodge")

    def test_wolf_scenario_defender_picks_dodge_over_brawling(self):
        # Regression check against the real creatures.toml data:
        # wolf dodge = 6*3=18, wolf brawling = 5*3=15, so dodge should win.
        chosen = self.dm_core.get_opposing_skill("blades", "wolf")
        self.assertEqual(chosen, "dodge")

    def test_no_matching_opposing_skill_defaults_to_zero(self):
        self.dm_core.entities["empty_defender"] = {"name": "empty_defender", "skills": {}}

        chosen = self.dm_core.get_opposing_skill("blades", "empty_defender")
        self.assertIsNone(chosen)

        result = self.dm_core.resolve_opposed_action("gladstone", "blades", "empty_defender")
        self.assertIsNone(result["opposing_skill"])
        self.assertEqual(result["difficulty"], 0)

    def test_no_difficulty_passed_defaults_to_zero(self):
        result = self.dm_core.resolve_action("gladstone", "blades")
        self.assertEqual(result["difficulty"], 0)


if __name__ == "__main__":
    unittest.main()
