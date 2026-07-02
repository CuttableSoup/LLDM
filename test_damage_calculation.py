import unittest
from unittest.mock import patch
from Event_Bus import EventBus
from DM_Core import DMCore


class TestDamageCalculation(unittest.TestCase):
    def setUp(self):
        self.event_bus = EventBus()
        self.dm_core = DMCore(self.event_bus)

    def test_bonus_resolves_flat_number(self):
        self.assertEqual(self.dm_core.resolve_bonus("gladstone", 5), 5)

    def test_bonus_resolves_from_rule_reference(self):
        # strength_damage rule: skill="strength", divisor=2. Gladstone's strength is 2 dice, so bonus = 2 // 2 = 1.
        self.assertEqual(self.dm_core.resolve_bonus("gladstone", "user.strength_damage"), 1)
        # Without the "user." prefix should resolve the same way.
        self.assertEqual(self.dm_core.resolve_bonus("gladstone", "strength_damage"), 1)

    def test_bonus_unknown_reference_defaults_to_zero(self):
        self.assertEqual(self.dm_core.resolve_bonus("gladstone", "user.made_up_rule"), 0)

    @patch("random.randint", return_value=4)
    def test_damage_value_rolls_dice_and_adds_bonus(self, mock_randint):
        # 2 dice @ 4 each + 1 pip + strength_damage bonus (1) = 10
        total = self.dm_core.resolve_damage_value(
            "gladstone", {"dice": 2, "pips": 1, "bonus": "user.strength_damage"}
        )
        self.assertEqual(total, 10)

    def test_damage_value_ignores_unsupported_dice_reference(self):
        # Indirect references like "user.weapon.dice" (used by techniques.toml) aren't
        # resolvable yet; they should degrade to 0 dice rather than raise.
        total = self.dm_core.resolve_damage_value(
            "gladstone", {"dice": "user.weapon.dice", "pips": "user.weapon.pips", "bonus": 0}
        )
        self.assertEqual(total, 0)

    @patch("random.randint", return_value=3)
    def test_damage_reduction_only_applies_to_matching_tags(self, mock_randint):
        # Gladstone wears chain mail: armor_value = 2 dice, tags = physical/piercing/bludgeoning/slashing.
        self.assertEqual(self.dm_core.get_damage_reduction("gladstone", ["bludgeoning"]), 6)
        self.assertEqual(self.dm_core.get_damage_reduction("gladstone", ["fire"]), 0)

    def test_get_current_hp_initializes_from_max_hp(self):
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), 36)

    def test_apply_damage_subtracts_and_floors_at_zero(self):
        self.dm_core.apply_damage("gladstone", 10)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), 26)
        self.dm_core.apply_damage("gladstone", 1000)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), 0)

    @patch("random.randint", return_value=2)
    def test_calculate_damage_applies_unresisted_damage_to_hp(self, mock_randint):
        # Fireball: 5 dice, no bonus, fire damage - gladstone's chain mail doesn't resist fire.
        fireball = {"damage_value": {"dice": 5, "pips": 0, "bonus": 0}, "damage_tags": ["fire"]}
        result = self.dm_core.calculate_damage("wolf", "gladstone", fireball)

        self.assertEqual(result["raw_damage"], 10)  # 5 dice @ 2 each
        self.assertEqual(result["reduction"], 0)
        self.assertEqual(result["net_damage"], 10)
        self.assertEqual(result["remaining_hp"], 26)
        self.assertEqual(self.dm_core.get_current_hp("gladstone"), 26)

    @patch("random.randint", return_value=3)
    def test_calculate_damage_reduced_by_matching_armor(self, mock_randint):
        # Punch: 0 dice + strength_damage bonus (1), bludgeoning - chain mail resists bludgeoning (2 dice @ 3 each = 6).
        punch = {"damage_value": {"dice": 0, "pips": 0, "bonus": "user.strength_damage"}, "damage_tags": ["bludgeoning"]}
        result = self.dm_core.calculate_damage("wolf", "gladstone", punch)

        self.assertEqual(result["raw_damage"], 1)
        self.assertEqual(result["reduction"], 6)
        self.assertEqual(result["net_damage"], 0)
        self.assertEqual(result["remaining_hp"], 36)


if __name__ == "__main__":
    unittest.main()
