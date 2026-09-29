from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

from pool_manager.pool_v2 import benchmarks
from pool_manager.pool_v2.block_observer import BlockStore
from pool_manager.pool_v2.observation import read_archive
from pool_manager.pool_v2.protocol import ProtocolDataError
from pool_manager.pool_v2.qualifiers import credit_block, round_credits
from pool_manager.pool_v2.spool import Spool
from funds_helpers import DatabaseCase, WALLET
from observer_helpers import observation


class ObserverTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.store = BlockStore(self.db)
        self.store.initialize(8)

    def own(self):
        self.fund()
        row = benchmarks.reserve(self.db, self.member, "owned", creation_round=1, resource="CPU",
            selection={"block_id": "block-1"}, payload={"track_settings": {"t": {"num_bundles": 2}}},
            fee_limit=0, offer_expires_at=self.expiry)
        benchmarks.mark_submitting(self.db, row["id"])
        row = benchmarks.accept(self.db, row["id"], "b", {"benchmark_id": "b"}, actual_fee=0, evidence={"fixture": True})
        benchmarks.acknowledge(self.db, row["id"], self.member, row["assignment_digest"])

    def test_redundant_capture_replay_and_chunk_deduplication(self):
        data = observation()
        result = self.concurrent([lambda: self.store.record(data, collector="one"), lambda: self.store.record(data, collector="two")])
        self.assertTrue(all(isinstance(row, dict) and row["complete"] for row in result), result)
        self.assertEqual(self.row("SELECT count(*) AS n FROM observed_blocks")["n"], 1)
        self.assertEqual(self.row("SELECT count(*) AS n FROM capture_attempts")["n"], 2)
        before = self.row("SELECT count(*) AS n FROM observation_chunks")["n"]
        self.store.record(observation(9), collector="two")
        self.assertEqual(self.row("SELECT count(*) AS n FROM observation_chunks")["n"], before)
        replay, snapshot = BlockStore(self.db).read("block-8")
        self.assertEqual(replay, data)
        self.assertEqual(snapshot.height, 8)
        self.assertEqual(self.store.status()["contiguous_height"], 9)

    def test_gap_is_held_then_recovered_by_other_collector(self):
        self.own()
        for height in (8, 10, 11):
            self.store.record(observation(height), collector="one")
            credit_block(self.db, f"block-{height}", WALLET)
        self.assertEqual(self.store.status()["missing_heights"], [9])
        self.assertEqual(self.store.status()["contiguous_height"], 8)
        with self.assertRaises(ProtocolDataError): round_credits(self.db, 3)
        self.store.record(observation(9), collector="replica")
        self.assertFalse(self.store.round_coverage(3))  # Captured, but not yet attributed.
        credit_block(self.db, "block-9", WALLET)
        self.assertEqual(self.store.status()["contiguous_height"], 11)
        self.assertTrue(self.store.round_coverage(3))
        self.assertEqual(round_credits(self.db, 3), {str(self.member): Fraction(4)})
        credit_block(self.db, "block-9", WALLET)
        self.assertEqual(round_credits(self.db, 3), {str(self.member): Fraction(4)})

    def test_unrecoverable_gap_does_not_prevent_a_different_complete_round(self):
        self.store.record(observation(8), collector="one")
        for height in range(12, 16):
            self.store.record(observation(height), collector="one")
            credit_block(self.db, f"block-{height}", "new-empty-pool")
        self.assertFalse(self.store.round_coverage(3))
        self.assertTrue(self.store.round_coverage(4))
        self.assertEqual(round_credits(self.db, 4), {})  # Proven zero, not a gap.
        self.assertEqual(self.store.status()["contiguous_height"], 8)

    def test_partial_and_conflicting_snapshots_never_support_credit(self):
        incomplete = observation(8)
        incomplete["players"].clear()
        self.assertFalse(self.store.record(incomplete, collector="one")["complete"])
        self.assertEqual(self.store.status()["latest_seen_height"], 8)
        self.assertEqual(self.store.status()["contiguous_height"], 7)
        self.store.record(observation(8), collector="replica")
        conflict = observation(8, identity="different-eight")
        self.assertFalse(self.store.record(conflict, collector="three")["complete"])
        self.assertEqual(self.store.status()["contiguous_height"], 7)
        self.assertEqual(self.store.status()["conflicting_heights"], [8])
        self.store.record(observation(9), collector="one")
        self.assertEqual(self.store.status()["contiguous_height"], 7)
        with self.assertRaisesRegex(ProtocolDataError, "conflicting"):
            self.store.read("block-8")

    def test_inconsistent_parent_and_round_are_not_silently_accepted(self):
        self.store.record(observation(8), collector="one")
        self.assertFalse(self.store.record(observation(9, previous="wrong-parent"), collector="one")["complete"])
        data = observation(10)
        data["start"]["block"]["details"]["round"] = 7
        data["end"]["block"]["details"]["round"] = 7
        self.assertFalse(self.store.record(data, collector="one")["complete"])

    def test_unknown_pool_owner_is_gap_even_when_snapshot_is_complete(self):
        self.store.record(observation(8), collector="one")
        with self.assertRaisesRegex(ProtocolDataError, "ownership"):
            credit_block(self.db, "block-8", WALLET)
        self.assertEqual(self.row("SELECT count(*) AS n FROM credited_blocks")["n"], 0)

    def test_spool_survives_database_outage_and_preserves_partial_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Spool(directory)
            data = observation(8)
            path = first.save(data, {"collector": "one"})
            chunks = len(list(Path(directory).glob("chunks/*/*.gz")))
            second_path = first.save(observation(9), {"collector": "one"})
            self.assertEqual(len(list(Path(directory).glob("chunks/*/*.gz"))), chunks)
            resumed = Spool(directory)
            self.assertEqual(set(resumed.pending()), {path, second_path})
            recovered, metadata, error = resumed.read(path)
            self.assertEqual(recovered, data)
            result = self.store.record(recovered, collector=metadata["collector"], error=error)
            self.assertTrue(result["complete"])
            resumed.recorded(path)
            self.assertEqual(resumed.pending(), [second_path])
            partial = {"start": data["start"]}
            failed = resumed.save(partial, {"collector": "one"}, "API unavailable")
            self.assertEqual(resumed.read(failed), (partial, {"collector": "one"}, "API unavailable"))

    def test_live_fixture_storage_replays_all_protocol_data(self):
        data = read_archive(next((Path(__file__).parent / "fixtures").glob("block-1351111*")))["observation"]
        result = self.store.record(data, collector="recorded-live")
        self.assertTrue(result["complete"], result)
        restored, _ = self.store.read(result["block_id"])
        self.assertEqual(restored, data)

    def test_running_collector_keeps_capturing_while_recorder_is_blocked(self):
        from tools.observe_tig_v2 import main
        release, waiting = threading.Event(), threading.Event()
        test = self
        class DelayedStore(BlockStore):
            def initialize(self, height):
                waiting.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("capture stalled behind database")
                super().initialize(height)
        with tempfile.TemporaryDirectory() as directory:
            instances = []
            class Client:
                def __init__(self, url):
                    self.data = observation(8+len(instances))
                    self.records = []
                    instances.append(self)
                    if len(instances) == 2:
                        test.assertTrue(waiting.wait(timeout=1))
                        test.assertEqual(len(Spool(directory).pending()), 1)
                        release.set()
                def get(self, path, params=None):
                    if path == "/get-block": return self.data["start"]
                    if path == "/get-benchmarks": return self.data["players"][params["player_id"]]
                    return self.data[{"/get-algorithms": "algorithms", "/get-challenges": "challenges", "/get-opow": "opow"}[path]]
            with patch("tools.observe_tig_v2.PublicTigClient", Client), patch("tools.observe_tig_v2.BlockStore", DelayedStore), \
                 patch("tools.observe_tig_v2.signal.signal"), patch.dict("os.environ", {"POOL_V2_DATABASE_DSN": self.db.dsn}):
                try:
                    result = main(["--collector", "test", "--spool", directory, "--launch-height", "8",
                                   "--captures", "2", "--poll-seconds", "0.01"])
                finally:
                    release.set()
            self.assertEqual(result, 0)
            self.assertEqual(self.store.status()["contiguous_height"], 9)
