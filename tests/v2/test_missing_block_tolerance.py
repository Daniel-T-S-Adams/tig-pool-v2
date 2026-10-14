"""Bounded missing-block tolerance: a few unrecoverable heights are zero credit, more hold the round."""

from contextlib import redirect_stdout
from fractions import Fraction
import importlib.util
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from pool_manager.pool_v2 import benchmarks, dashboard
from pool_manager.pool_v2.block_observer import BlockStore
from pool_manager.pool_v2.protocol import ProtocolDataError
from pool_manager.pool_v2.qualifiers import credit_block, round_credits
from funds_helpers import DatabaseCase, WALLET
from observer_helpers import observation


class MissingBlockToleranceTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.store = BlockStore(self.db)
        self.store.initialize(8)
        self.fund()
        row = benchmarks.reserve(self.db, self.member, "owned", creation_round=1, resource="CPU",
            selection={"block_id": "block-1"}, payload={"track_settings": {"t": {"num_bundles": 2}}},
            fee_limit=0, offer_expires_at=self.expiry)
        benchmarks.mark_submitting(self.db, row["id"])
        row = benchmarks.accept(self.db, row["id"], "b", {"benchmark_id": "b"}, actual_fee=0, evidence={"fixture": True})
        benchmarks.acknowledge(self.db, row["id"], self.member, row["assignment_digest"])

    def record(self, *heights, credit=True):
        for height in heights:
            result = self.store.record(observation(height), collector="one")
            self.assertTrue(result["complete"], result)
            if credit:
                credit_block(self.db, result["block_id"], WALLET)

    def policy(self, limit, warn=0):
        return self.store.set_gap_policy(max_missing_per_round=limit, warn_missing_per_round=warn,
                                         actor="operator", reason="test policy")

    def test_default_policy_is_recorded_by_the_migration(self):
        policy = self.store.gap_policy()
        self.assertEqual((policy["policy"], policy["max_missing_per_round"], policy["warn_missing_per_round"]),
                         ("bounded-zero-credit-v1", 50, 10))
        self.assertEqual(self.store.status()["missing_block_policy"],
                         {"policy": "bounded-zero-credit-v1", "revision": 1, "max_missing_per_round": 50,
                          "warn_missing_per_round": 10})

    def test_a_few_missing_heights_are_zero_credit_once_the_round_has_ended(self):
        self.record(8, 10)   # Round 3 is heights 8..11; height 9 is never captured.
        coverage = self.store.round_gaps(3)
        self.assertEqual((coverage["ended"], coverage["complete"]), (False, False))
        with self.assertRaisesRegex(ProtocolDataError, "incomplete"):
            round_credits(self.db, 3)
        self.record(11)
        coverage = self.store.round_gaps(3)
        self.assertEqual((coverage["ended"], coverage["complete"], coverage["credited_complete"]), (True, True, True))
        self.assertEqual((coverage["missing_heights"], coverage["observed"], coverage["credited"]), ([9], 3, 3))
        self.assertFalse(coverage["warning"])
        self.assertTrue(self.store.round_coverage(3))
        self.assertEqual(round_credits(self.db, 3), {str(self.member): Fraction(3)})
        status = self.store.status()
        self.assertEqual((status["unresolved_missing_heights"], status["contiguous_height"]), ([9], 8))
        self.assertEqual(status["missing_by_round"],
                         [{"round": 3, "missing": 1, "waived": 0, "ended": True, "state": "tolerated"}])
        self.assertEqual(status["held_rounds"], [])

    def test_tolerated_height_still_needs_real_credit_after_a_genuine_backfill(self):
        self.record(8, 10, 11)
        self.assertTrue(self.store.round_coverage(3))
        self.store.record(observation(9), collector="replica")
        self.assertTrue(self.store.round_coverage(3, require_credits=False))
        self.assertFalse(self.store.round_coverage(3))
        credit_block(self.db, "block-9", WALLET)
        self.assertTrue(self.store.round_coverage(3))
        self.assertEqual(round_credits(self.db, 3), {str(self.member): Fraction(4)})
        self.assertEqual(self.store.status()["missing_by_round"], [])

    def test_more_missing_heights_than_the_limit_hold_the_round(self):
        self.policy(1)
        self.record(8, 12)   # Round 3 lacks 9, 10 and 11; round 4 has started.
        coverage = self.store.round_gaps(3)
        self.assertEqual((coverage["ended"], coverage["missing"], coverage["complete"]), (True, 3, False))
        self.assertEqual(coverage["missing_heights"], [9, 10, 11])
        self.assertFalse(self.store.round_coverage(3, require_credits=False))
        with self.assertRaisesRegex(ProtocolDataError, "incomplete"):
            round_credits(self.db, 3)
        status = self.store.status()
        self.assertEqual(status["held_rounds"], [3])
        self.assertEqual(status["missing_by_round"][0]["state"], "held")
        # A reviewed higher limit releases the round; status follows the new revision.
        self.policy(3)
        self.assertTrue(self.store.round_coverage(3))
        self.assertEqual(self.store.status()["held_rounds"], [])
        self.assertEqual(round_credits(self.db, 3), {str(self.member): Fraction(1)})

    def test_warning_level_flags_a_round_before_it_is_held(self):
        self.policy(3, warn=1)
        self.record(8, 11, 12)   # 9 and 10 missing: above the warning level, within the limit.
        coverage = self.store.round_gaps(3)
        self.assertEqual((coverage["complete"], coverage["warning"], coverage["missing_heights"]), (True, True, [9, 10]))
        self.assertEqual(self.store.status()["missing_by_round"][0]["state"], "warning")
        self.assertEqual(self.store.status()["held_rounds"], [])

    def test_an_unfinished_round_is_never_tolerated(self):
        self.record(8, 9)   # Round 3 has not ended on chain; two heights simply do not exist yet.
        coverage = self.store.round_gaps(3)
        self.assertEqual((coverage["ended"], coverage["missing"], coverage["complete"]), (False, 2, False))
        self.assertEqual(self.store.status()["missing_by_round"], [])
        self.record(10)
        self.assertFalse(self.store.round_coverage(3))
        self.record(12, credit=False)   # The next round began; 11 is now a real gap within the limit.
        self.assertTrue(self.store.round_coverage(3))
        self.assertEqual(self.store.status()["missing_by_round"],
                         [{"round": 3, "missing": 1, "waived": 0, "ended": True, "state": "tolerated"}])

    def test_conflicting_block_holds_the_round_regardless_of_the_limit(self):
        self.record(8, 10, 11)
        self.assertTrue(self.store.round_coverage(3))
        self.assertFalse(self.store.record(observation(10, identity="different-ten"), collector="three")["complete"])
        coverage = self.store.round_gaps(3)
        self.assertEqual((coverage["conflicting"], coverage["complete"]), (True, False))
        self.assertEqual(self.store.status()["conflicting_heights"], [10])

    def test_round_without_any_observed_block_stays_held(self):
        for height in (12, 14, 15):
            result = self.store.record(observation(height), collector="one")
            credit_block(self.db, result["block_id"], "new-empty-pool")
        self.assertFalse(self.store.round_coverage(3))   # Nothing observed in round 3 at all.
        self.assertTrue(self.store.round_coverage(4))   # One tolerated gap, proven zero credit.
        self.assertEqual(round_credits(self.db, 4), {})
        status = self.store.status()
        self.assertEqual([(row["round"], row["state"]) for row in status["missing_by_round"]],
                         [(3, "held"), (4, "tolerated")])
        self.assertEqual(status["held_rounds"], [3])

    def test_policy_revisions_are_append_only_and_validated(self):
        first = self.store.gap_policy()
        self.assertEqual(self.policy(50, warn=10)["revision"], first["revision"])   # Identical limits add nothing.
        second = self.policy(2, warn=1)
        self.assertEqual(second["revision"], first["revision"] + 1)
        self.assertEqual(self.store.gap_policy()["max_missing_per_round"], 2)
        for limit, warn in ((-1, 0), (2, 3), ("2", 0), (2, None)):
            with self.assertRaises(ValueError):
                self.store.set_gap_policy(max_missing_per_round=limit, warn_missing_per_round=warn,
                                          actor="operator", reason="bad")
        with self.assertRaises(ValueError):
            self.store.set_gap_policy(max_missing_per_round=1, warn_missing_per_round=0, actor=" ", reason="bad")
        for statement in ("UPDATE missing_block_policies SET max_missing_per_round=99",
                          "DELETE FROM missing_block_policies", "TRUNCATE missing_block_policies"):
            with self.assertRaises(Exception):
                with self.db.transaction() as cursor:
                    cursor.execute(statement)
        self.assertEqual(self.row("SELECT count(*) AS n FROM missing_block_policies")["n"], 2)

    def test_operator_dashboard_shows_missing_blocks_per_round(self):
        self.record(8, 10, 11)
        rows = {row["round"]: row for row in dashboard.operator(self.db)["rounds"]}
        self.assertEqual((rows[3]["observed_blocks"], rows[3]["credited_blocks"], rows[3]["missing_blocks"],
                          rows[3]["gap_state"]), (3, 3, 1, "tolerated"))
        self.assertEqual((rows[1]["missing_blocks"], rows[1]["gap_state"]), (0, None))

    def test_operator_tool_shows_and_appends_the_policy(self):
        spec = importlib.util.spec_from_file_location(
            "set_missing_block_policy_v2", Path(__file__).resolve().parents[2] / "tools" / "set_missing_block_policy_v2.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.dict(os.environ, {"POOL_V2_DATABASE_DSN": os.environ["POOL_V2_TEST_DSN"]}):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(module.main([]), 0)
            self.assertEqual(json.loads(out.getvalue())["max_missing_per_round"], 50)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(module.main(["--max-missing-per-round", "5", "--warn-missing-per-round", "2",
                                              "--actor", "operator", "--reason", "tighter limit"]), 0)
            self.assertEqual(json.loads(out.getvalue())["revision"], 2)
            with self.assertRaises(SystemExit):
                module.main(["--max-missing-per-round", "5"])
            with self.assertRaises(SystemExit):
                module.main(["--max-missing-per-round", "1", "--warn-missing-per-round", "2",
                             "--actor", "operator", "--reason", "invalid"])
        self.assertEqual(self.store.gap_policy()["max_missing_per_round"], 5)
