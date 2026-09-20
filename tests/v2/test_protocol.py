import copy
from contextlib import redirect_stdout
from fractions import Fraction
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pool_manager.pool_v2.observation import (
    CaptureError, PublicTigClient, capture_snapshot, read_archive, write_archive,
)
from pool_manager.pool_v2.protocol import (
    Bundle, ProtocolDataError, Snapshot, equal_bundle_credit, validate_reports, validate_snapshot,
)


FIXTURES = Path(__file__).parent / "fixtures"
FIRST = "block-1351111-c3f0ecba985199e301f3bf8b87512750.json.gz"
SECOND = "block-1351112-5b53b0f7dc052261bb5d83fb2922e07d.json.gz"


class RecordedSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.recording = read_archive(FIXTURES / FIRST)

    def observation(self):
        return copy.deepcopy(self.recording["observation"])

    def first_active_record(self, observation, collection, id_field):
        active = set(observation["start"]["block"]["data"]["active_ids"]["benchmark"])
        for player in observation["players"].values():
            for record in player[collection]:
                if record[id_field] in active:
                    return player[collection], record
        self.fail("fixture has no active benchmark")

    def test_recorded_complete_blocks_reconcile_and_are_consecutive(self):
        first = validate_snapshot(self.recording["observation"])
        second_recording = read_archive(FIXTURES / SECOND)
        second = validate_snapshot(second_recording["observation"])
        self.assertEqual((first.height, second.height), (1351111, 1351112))
        self.assertEqual(second.previous_block_id, first.block_id)
        for snapshot, metadata in ((first, self.recording["metadata"]),
                                   (second, second_recording["metadata"])):
            self.assertEqual(len(snapshot.precommits), metadata["active_benchmarks"])
            self.assertEqual(len(snapshot.bundles), metadata["eligible_bundles"])
            credits = equal_bundle_credit(snapshot)
            self.assertEqual(sum(credits.values(), Fraction()), 4000)
            self.assertTrue(all(Fraction() <= credit <= 1 for credit in credits.values()))

    def test_block_changing_during_fetch_is_rejected(self):
        data = self.observation()
        data["end"]["block"]["id"] = "different-block"
        with self.assertRaisesRegex(ProtocolDataError, "block changed"):
            validate_snapshot(data)

    def test_missing_active_precommit_is_not_an_empty_result(self):
        data = self.observation()
        records, record = self.first_active_record(data, "precommits", "benchmark_id")
        records.remove(record)
        with self.assertRaisesRegex(ProtocolDataError, "missing 1 active benchmark precommits"):
            validate_snapshot(data)

    def test_missing_player_response_blocks_accounting(self):
        data = self.observation()
        data["players"].pop(next(iter(data["players"])))
        with self.assertRaisesRegex(ProtocolDataError, "do not cover"):
            validate_snapshot(data)

    def test_truncated_bundle_scores_are_rejected(self):
        data = self.observation()
        _, record = self.first_active_record(data, "benchmarks", "id")
        record["details"]["average_quality_by_bundle"].pop()
        with self.assertRaisesRegex(ProtocolDataError, "bundle count"):
            validate_snapshot(data)

    def test_null_hyperparameters_are_valid_but_a_missing_field_is_not(self):
        data = self.observation()
        _, record = self.first_active_record(data, "precommits", "benchmark_id")
        record["details"]["hyperparameters"] = None
        validate_snapshot(data)
        del record["details"]["hyperparameters"]
        with self.assertRaisesRegex(ProtocolDataError, "hyperparameters"):
            validate_snapshot(data)

    def test_duplicate_records_are_rejected(self):
        data = self.observation()
        records, record = self.first_active_record(data, "precommits", "benchmark_id")
        records.append(record)
        with self.assertRaisesRegex(ProtocolDataError, "duplicate"):
            validate_snapshot(data)

    def test_mismatched_authoritative_totals_are_rejected(self):
        data = self.observation()
        challenge = data["challenges"]["challenges"][0]
        counts = challenge["block_data"]["num_qualifiers_by_track"]
        counts[next(iter(counts))] += 1
        with self.assertRaisesRegex(ProtocolDataError, "challenge totals"):
            validate_snapshot(data)

    def test_failed_fetch_preserves_partial_evidence(self):
        observation = self.recording["observation"]

        class Client:
            def get(self, path, params=None):
                if path == "/get-algorithms":
                    raise TimeoutError("simulated API outage")
                if path == "/get-benchmarks":
                    return observation["players"][params["player_id"]]
                return observation[{
                    "/get-challenges": "challenges", "/get-opow": "opow",
                    "/get-block": "end",
                }[path]]

        with self.assertRaises(CaptureError) as caught:
            capture_snapshot(Client(), start=observation["start"])
        self.assertIn("simulated API outage", str(caught.exception))
        self.assertEqual(set(caught.exception.observation["players"]), set(observation["players"]))
        self.assertNotIn("algorithms", caught.exception.observation)
        with self.assertRaises(ProtocolDataError):
            validate_snapshot(caught.exception.observation)


class EqualCreditTests(unittest.TestCase):
    def snapshot(self, qualities, quota):
        bundles = tuple(Bundle("alice" if i < 3 else "bob", i, "pool", "challenge", "algo", "track", q)
                        for i, q in enumerate(qualities))
        return Snapshot("block", "previous", 1, 1, 0, bundles,
                        {("pool", "challenge", "algo", "track"): quota}, {}, {}, {})

    def test_three_places_shared_by_five_tied_bundles(self):
        credits = equal_bundle_credit(self.snapshot([7] * 5, 3))
        self.assertEqual(set(credits.values()), {Fraction(3, 5)})
        self.assertEqual(sum(v for (owner, _), v in credits.items() if owner == "alice"), Fraction(9, 5))
        self.assertEqual(sum(v for (owner, _), v in credits.items() if owner == "bob"), Fraction(6, 5))

    def test_only_the_boundary_tie_shares_credit(self):
        credits = equal_bundle_credit(self.snapshot([9, 7, 7, 7, 1], 2))
        self.assertEqual(list(credits.values()), [1, Fraction(1, 3), Fraction(1, 3), Fraction(1, 3), 0])

    def test_zero_qualifiers_still_requires_a_validated_snapshot(self):
        self.assertEqual(set(equal_bundle_credit(self.snapshot([9, 7], 0)).values()), {Fraction()})

    def test_impossible_quota_is_not_silently_capped(self):
        with self.assertRaises(ProtocolDataError):
            equal_bundle_credit(self.snapshot([9, 7], 3))


class ReportTests(unittest.TestCase):
    def test_recorded_arbitrations_are_linked_by_report_id(self):
        for round_number, count in ((129, 16), (130, 46)):
            results = validate_reports(read_archive(FIXTURES / f"reports-{round_number}.json.gz"))
            self.assertEqual(len(results), count)
            self.assertEqual({v["result"] for v in results.values()}, {"nonreproducible"})

    def test_final_inconclusive_is_distinct_from_pending_and_upheld(self):
        payload = read_archive(FIXTURES / "reports-129.json.gz")
        arbitration = payload["arbitrations"][0]
        report_id = arbitration["report_id"]
        for result in ("inconclusive", "reproducible", "nonreproducible"):
            arbitration["details"]["result"] = result
            self.assertEqual(validate_reports(payload)[report_id]["result"], result)
        payload["arbitrations"].remove(arbitration)
        self.assertIsNone(validate_reports(payload)[report_id]["result"])

    def test_unknown_result_or_missing_response_is_not_treated_as_no_reports(self):
        payload = read_archive(FIXTURES / "reports-129.json.gz")
        payload["arbitrations"][0]["details"]["result"] = "future-result"
        with self.assertRaises(ProtocolDataError):
            validate_reports(payload)
        with self.assertRaises(ProtocolDataError):
            validate_reports({})
        self.assertEqual(validate_reports({"reports": [], "arbitrations": []}), {})

    def test_unconfirmed_arbitration_remains_pending(self):
        payload = read_archive(FIXTURES / "reports-129.json.gz")
        arbitration = payload["arbitrations"][0]
        arbitration["state"] = None
        self.assertIsNone(validate_reports(payload)[arbitration["report_id"]]["result"])

    def test_decision_cannot_precede_the_report(self):
        payload = read_archive(FIXTURES / "reports-129.json.gz")
        payload["arbitrations"][0]["state"]["block_confirmed"] = 0
        with self.assertRaisesRegex(ProtocolDataError, "precedes its report"):
            validate_reports(payload)


class ProbeCoverageTests(unittest.TestCase):
    def observation(self, height, identity, previous):
        block = {"id": identity, "config": {}, "data": {"active_ids": {
            "benchmark": ["b"], "opow": ["p"], "code": ["a"], "challenge": ["c"],
        }}, "details": {"height": height, "round": 1, "timestamp": 1,
                        "prev_block_id": previous,
                        "num_active": {"benchmark": 1, "opow": 1, "code": 1, "challenge": 1}}}
        confirmed = {"block_confirmed": height - 1}
        return {
            "start": {"block": block}, "end": {"block": copy.deepcopy(block)},
            "algorithms": {"codes": [{"id": "a", "details": {"challenge_id": "c"},
                "block_data": {"adoption": "1", "num_qualifiers_by_track_by_player": {"t": {"p": 1}}}}]},
            "challenges": {"challenges": [{"id": "c", "config": {"active_tracks": {"t": {}}},
                "block_data": {"num_qualifiers_by_track": {"t": 1}}}]},
            "opow": {"opow": [{"player_id": "p", "block_data": {
                "num_qualifiers_by_challenge_by_track": {"c": {"t": 1}}}}]},
            "players": {"p": {
                "precommits": [{"benchmark_id": "b", "state": confirmed,
                    "settings": {"player_id": "p", "challenge_id": "c", "algorithm_id": "a", "track_id": "t"},
                    "details": {"block_started": height - 1, "num_bundles": 1, "fuel_budget": 0,
                                "compute_type": "aws_c7a", "hyperparameters": None}}],
                "benchmarks": [{"id": "b", "state": confirmed, "details": {
                    "stopped": False, "num_active_bundles": 1, "average_quality_by_bundle": [7]}}],
                "proofs": [{"benchmark_id": "b", "state": confirmed, "details": {"block_active": height}}],
                "frauds": [],
            }},
        }

    def test_gap_or_fork_never_counts_as_consecutive_coverage(self):
        from tools import probe_tig_v2 as probe

        for height, previous in ((3, "block-1"), (2, "another-parent")):
            with self.subTest(height=height, previous=previous):
                observations = [self.observation(1, "block-1", "genesis"),
                                self.observation(height, "block-next", previous)]
                starts = iter(v["start"] for v in observations)

                class Client:
                    records = []

                    def get(self, path, params=None):
                        return next(starts)

                with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()), \
                     patch.object(probe, "PublicTigClient", return_value=Client()), \
                     patch.object(probe, "capture_snapshot", side_effect=observations), \
                     patch.object(probe.time, "time", return_value=2):
                    result = probe.main(["--output", root, "--snapshots", "2", "--poll-interval", "0.001"])
                    summary = read_archive(Path(root) / "summary.json.gz")
                    self.assertEqual(result, 1)
                    self.assertEqual(len(summary["snapshots"]), 2)
                    self.assertFalse(summary["consecutive"])


class ArchiveTests(unittest.TestCase):
    def test_archive_round_trip_preserves_observed_nulls_and_large_integers(self):
        data = {"hyperparameters": None, "token_units": 10**30, "incomplete": True}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "observation.json.gz"
            write_archive(path, data)
            self.assertEqual(read_archive(path), data)
            self.assertEqual(list(Path(root).iterdir()), [path])

    def test_probe_rejects_submission_paths_and_embedded_credentials(self):
        with self.assertRaises(ValueError):
            PublicTigClient("https://user:secret@example.com")
        with self.assertRaises(ValueError):
            PublicTigClient("https://example.com").get("/submit-precommit")


if __name__ == "__main__":
    unittest.main()
