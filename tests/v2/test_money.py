import unittest
from fractions import Fraction

from pool_manager.pool_v2.money import FundsError, TIG, allocate_rewards, collateral, multiplier


class MoneyTests(unittest.TestCase):
    def test_max_track_and_exact_multiplier_with_one_final_rounding(self):
        self.assertEqual(collateral([3, 5, 4], "0.4"), (50 * TIG, 20 * TIG))
        self.assertEqual(collateral([5], "0.000000000000000000001"), (50 * TIG, 1))
        self.assertEqual(collateral([5], "0"), (50 * TIG, 0))
        # More than Decimal's default 28-digit context, with no intermediate rounding.
        self.assertEqual(collateral([1], "0.100000000000000000000000000000000001"), (10 * TIG, TIG + 1))

    def test_invalid_multiplier_and_bundle_inputs(self):
        for value in (0.4, True, "NaN", "Infinity", "-0.01", "1.01", "1e-999999"):
            with self.subTest(value=value), self.assertRaises(FundsError):
                multiplier(value)
        for counts in ([], [True], [0], [1.0], [-1]):
            with self.subTest(counts=counts), self.assertRaises(FundsError):
                collateral(counts, "1")

    def test_allocations_preserve_units_and_stable_ties(self):
        self.assertEqual(allocate_rewards(100, {"bob": Fraction(6, 5), "alice": Fraction(9, 5)}),
                         (5, {"bob": 38, "alice": 57}))
        self.assertEqual(allocate_rewards(2, {"c": 1, "b": 1, "a": 1}), (0, {"c": 0, "b": 1, "a": 1}))
        self.assertEqual(allocate_rewards(103, {}), (103, {}))
        self.assertEqual(allocate_rewards(103, {"a": 0}), (103, {"a": 0}))
        for pot in (0, 1, 19, 20, 101, 10 ** 50 + 3):
            fee, members = allocate_rewards(pot, {"a": Fraction(13, 17), "b": Fraction(23, 997), "c": 0})
            self.assertEqual(fee + sum(members.values()), pot)
            self.assertEqual(members["c"], 0)
