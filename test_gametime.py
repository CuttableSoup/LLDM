import unittest
from models import GameTime

class TestGameTime(unittest.TestCase):
    def test_initialization(self):
        gt = GameTime(month=1, day=1, hour=8, minute=30, second=15)
        self.assertEqual(gt.year, 2000)
        self.assertEqual(gt.month, 1)
        self.assertEqual(gt.day, 1)
        self.assertEqual(gt.hour, 8)
        self.assertEqual(gt.minute, 30)
        self.assertEqual(gt.second, 15)
        # Check that total_seconds is less than a year
        self.assertLess(gt.total_seconds, gt.SECONDS_PER_YEAR)

    def test_advance_time_rollover(self):
        # Start at end of year 1
        gt = GameTime(year=1, month=12, day=30, hour=23, minute=59, second=59)
        gt.advance_time(1)
        self.assertEqual(gt.year, 2)
        self.assertEqual(gt.month, 1)
        self.assertEqual(gt.day, 1)
        self.assertEqual(gt.hour, 0)
        self.assertEqual(gt.minute, 0)
        self.assertEqual(gt.second, 0)
        self.assertEqual(gt.total_seconds, 0)

    def test_get_time_string(self):
        gt = GameTime(year=2000, month=1, day=1, hour=8)
        self.assertEqual(gt.get_time_string(), "Year 2000, Month 1, Day 1, Hour 08:00")

    def test_set_time(self):
        gt = GameTime()
        gt.set_time(year=2005, month=5, day=10, hour=12, minute=30, second=45)
        self.assertEqual(gt.year, 2005)
        self.assertEqual(gt.month, 5)
        self.assertEqual(gt.day, 10)
        self.assertEqual(gt.hour, 12)
        self.assertEqual(gt.minute, 30)
        self.assertEqual(gt.second, 45)

    def test_copy(self):
        gt1 = GameTime(year=2000, month=1, day=1, hour=8)
        gt2 = gt1.copy()
        self.assertEqual(gt1.year, gt2.year)
        self.assertEqual(gt1.total_seconds, gt2.total_seconds)
        self.assertNotEqual(id(gt1), id(gt2))

if __name__ == '__main__':
    unittest.main()
