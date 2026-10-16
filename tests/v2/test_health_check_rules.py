"""The host health check judges block history by the deployed missing-block tolerance."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
from mainnet_health_check_v2 import block_issues, spending_issues

NOW = 1_800_000_000


def status(**overrides):
    base = {'initialized': True, 'latest_timestamp': NOW - 30, 'missing_heights': [1375817, 1376650],
            'waived_missing_heights': [], 'unresolved_missing_heights': [1375817, 1376650],
            'conflicting_heights': [], 'resolved_conflicting_heights': [1382587, 1385323],
            'missing_block_policy': {'policy': 'bounded-missing-blocks-v1', 'revision': 1,
                                     'max_missing_per_round': 50, 'warn_missing_per_round': 10},
            'missing_by_round': [{'round': 137, 'missing': 2, 'waived': 0, 'ended': True, 'state': 'tolerated'}],
            'held_rounds': []}
    base.update(overrides)
    return base


class SpendingRuleTests(unittest.TestCase):
    def test_missing_cap_and_nearly_used_windows_are_issues(self):
        self.assertEqual(spending_issues({'configured': False}), ['no spending cap is recorded; the pool commits no fees without one'])
        cap = {'tig_fee_window_units': '1000', 'native_fee_window_units': '0'}
        self.assertEqual(spending_issues({'configured': True, 'cap': cap, 'window': {'tig_fee_units': 900, 'native_fee_units': 0}}), [])
        self.assertEqual(spending_issues({'configured': True, 'cap': cap, 'window': {'tig_fee_units': 901, 'native_fee_units': 5}}),
                         ['TIG fee spending cap window is more than 90% committed'])


class HealthRuleTests(unittest.TestCase):
    def test_tolerated_and_waived_gaps_are_not_issues(self):
        self.assertEqual(block_issues(status(), NOW), [])
        self.assertEqual(block_issues(status(missing_by_round=[{'round': 137, 'missing': 12, 'waived': 1, 'ended': True, 'state': 'warning'}]), NOW), [])

    def test_held_rounds_current_overrun_conflicts_and_lag_are_issues(self):
        held = status(missing_by_round=[{'round': 137, 'missing': 60, 'waived': 0, 'ended': True, 'state': 'held'}], held_rounds=[137])
        self.assertEqual(block_issues(held, NOW), ['rounds held: missing blocks exceed the tolerance: 137'])
        current = status(missing_by_round=[{'round': 138, 'missing': 51, 'waived': 0, 'ended': False, 'state': 'open'}])
        self.assertEqual(block_issues(current, NOW), ['current round already exceeds the missing-block tolerance: 138'])
        within = status(missing_by_round=[{'round': 138, 'missing': 3, 'waived': 0, 'ended': False, 'state': 'open'}])
        self.assertEqual(block_issues(within, NOW), [])
        self.assertEqual(block_issues(status(conflicting_heights=[1385323]), NOW),
                         ['block history has unresolved conflicting heights: 1385323'])
        self.assertEqual(block_issues(status(latest_timestamp=NOW - 181), NOW), ['protocol observation is more than three minutes behind'])
        self.assertEqual(block_issues(status(latest_timestamp=None), NOW), ['protocol observation is more than three minutes behind'])

    def test_releases_without_the_tolerance_keep_the_strict_rule(self):
        legacy = status()
        for key in ('missing_by_round', 'held_rounds', 'missing_block_policy'):
            legacy.pop(key)
        self.assertEqual(block_issues(legacy, NOW), ['block history has unresolved gaps'])
        legacy['unresolved_missing_heights'] = []
        self.assertEqual(block_issues(legacy, NOW), [])
        self.assertEqual(block_issues({'initialized': False}, NOW), ['block observation is not initialized'])


if __name__ == '__main__':
    unittest.main()
