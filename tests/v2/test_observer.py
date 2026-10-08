from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch
import uuid

from psycopg2.extras import Json, execute_values

from pool_manager.pool_v2 import benchmarks, controls
from pool_manager.pool_v2.block_observer import BlockStore, _pack, _semantic, canonical_observation
from pool_manager.pool_v2.observation import read_archive
from pool_manager.pool_v2.protocol import ProtocolDataError, validate_snapshot
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
        attempt = self.store.record(observation(9), collector="two")
        # The next block shares every record and page; only its manifest chunk is new.
        self.assertEqual(self.row("SELECT count(*) AS n FROM observation_chunks")["n"], before + 1)
        reference = self.row("SELECT manifest->>'manifest_ref' AS r FROM capture_attempts WHERE id=%s", (attempt["attempt_id"],))["r"]
        self.assertEqual(self.row("SELECT count(*) AS n FROM observation_chunks WHERE digest=%s", (reference,))["n"], 1)
        replay, snapshot = BlockStore(self.db).read("block-8")
        self.assertEqual(replay, canonical_observation(data))
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

    def test_one_time_prelaunch_gap_waiver_keeps_gap_visible_and_excludes_it_from_credits(self):
        for height in (8, 10, 11):
            self.store.record(observation(height), collector="one")
            credit_block(self.db, f"block-{height}", "new-empty-pool")
        with self.assertRaisesRegex(ProtocolDataError, "remain paused"):
            self.store.waive_prelaunch_gap(9, actor="operator", reason="prelaunch capture gap",
                evidence={"review": "no pool submissions before launch"})
        controls.set_pause(self.db, True, actor="operator", reason="prelaunch validation",
                           event_key="pause-for-gap-waiver")
        waiver = self.store.waive_prelaunch_gap(9, actor="operator", reason="prelaunch capture gap",
            evidence={"review": "no pool submissions before launch"})
        replay = self.store.waive_prelaunch_gap(9, actor="operator", reason="prelaunch capture gap",
            evidence={"review": "no pool submissions before launch"})
        self.assertEqual(waiver, replay)
        self.assertEqual(waiver["assumed_block_id"], "block-9")
        with self.assertRaisesRegex(ProtocolDataError, "already recorded"):
            self.store.waive_prelaunch_gap(12, actor="operator", reason="second gap",
                evidence={"review": "not allowed"})
        self.assertTrue(self.store.round_coverage(3, require_credits=False))
        self.assertTrue(self.store.round_coverage(3))
        self.assertEqual(round_credits(self.db, 3), {})
        status = self.store.status()
        self.assertEqual(status["missing_heights"], [9])
        self.assertEqual(status["waived_missing_heights"], [9])
        self.assertEqual(status["unresolved_missing_heights"], [])
        self.assertEqual(self.row("SELECT count(*) AS n FROM observed_blocks WHERE height=9")["n"], 0)
        self.store.record(observation(9), collector="late-backfill")
        self.assertEqual(self.store.status()["missing_heights"], [])
        self.assertEqual(self.store.status()["waived_missing_heights"], [])
        self.assertFalse(self.store.round_coverage(3))  # Real block now needs real credit.
        credit_block(self.db, "block-9", "new-empty-pool")
        self.assertTrue(self.store.round_coverage(3))

    def test_prelaunch_gap_waiver_is_unavailable_after_a_pool_reservation(self):
        for height in (8, 10):
            self.store.record(observation(height), collector="one")
        self.fund()
        benchmarks.reserve(self.db, self.member, "prelaunch-work", creation_round=3, resource="CPU",
            selection={"block_id": "block-8"},
            payload={"track_settings": {"t": {"num_bundles": 2}}},
            fee_limit=0, offer_expires_at=self.expiry)
        controls.set_pause(self.db, True, actor="operator", reason="prelaunch validation",
                           event_key="pause-after-reservation")
        with self.assertRaisesRegex(ProtocolDataError, "after pool reservations exist"):
            self.store.waive_prelaunch_gap(9, actor="operator", reason="prelaunch capture gap",
                evidence={"review": "no pool submissions before launch"})

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
            first = Spool(directory, paged=True)
            data = observation(8)
            path = first.save(data, {"collector": "one"})
            chunks = len(list(Path(directory).glob("chunks/*/*.gz")))
            second_path = first.save(observation(9), {"collector": "one"})
            self.assertEqual(len(list(Path(directory).glob("chunks/*/*.gz"))), chunks)
            resumed = Spool(directory)
            self.assertEqual(set(resumed.pending()), {path, second_path})
            recovered, metadata, error = resumed.read(path)
            self.assertEqual(recovered, canonical_observation(data))
            result = self.store.record(recovered, collector=metadata["collector"], error=error)
            self.assertTrue(result["complete"])
            resumed.recorded(path)
            self.assertEqual(resumed.pending(), [second_path])
            partial = {"start": data["start"]}
            failed = resumed.save(partial, {"collector": "one"}, "API unavailable")
            self.assertEqual(resumed.read(failed), (partial, {"collector": "one"}, "API unavailable"))

    def test_live_fixture_storage_replays_all_protocol_data(self):
        paths = sorted((Path(__file__).parent / "fixtures").glob("block-13511*"))
        data = read_archive(paths[0])["observation"]
        result = self.store.record(data, collector="recorded-live")
        self.assertTrue(result["complete"], result)
        restored, _ = self.store.read(result["block_id"])
        self.assertEqual(restored, canonical_observation(data))
        # The manifest holds page references, not every record digest inline.
        legacy_digests = sum(len(node["records"]) for node in self._nodes(_pack(data, paged=False)[0]) if "records" in node)
        self.assertGreater(legacy_digests, 10000)
        # The row holds one reference; the manifest tree is a compressed chunk.
        size = self.row("SELECT pg_column_size(manifest) AS n FROM capture_attempts")["n"]
        self.assertLess(size, 200, size)
        reference = self.row("SELECT manifest->>'manifest_ref' AS r FROM capture_attempts")["r"]
        tree = self.row("SELECT length(compressed) AS n FROM observation_chunks WHERE digest=%s", (reference,))["n"]
        self.assertLess(tree, 80_000, tree)
        # The next block reuses every unchanged page; only changed records and pages are new.
        before = self.row("SELECT count(*) AS n FROM observation_chunks")["n"]
        following = self.store.record(read_archive(paths[1])["observation"], collector="recorded-live")
        self.assertTrue(following["complete"], following)
        added = self.row("SELECT count(*) AS n FROM observation_chunks")["n"] - before
        self.assertLess(added, 500, added)
        self.assertEqual(self.row("SELECT count(*) AS n FROM observed_blocks")["n"], 2)
        self.assertEqual(self.store.status()["conflicting_heights"], [])

    def _nodes(self, manifest):
        if "dict" in manifest:
            for _, child in manifest["dict"]:
                yield from self._nodes(child)
        else:
            yield manifest

    def test_legacy_inline_manifests_replay_and_match_a_paged_recapture(self):
        data = observation(8)
        manifest, chunks, _ = _pack(data, paged=False)
        attempt = str(uuid.uuid4())
        with self.db.transaction() as cursor:
            execute_values(cursor, "INSERT INTO observation_chunks(digest,compressed) VALUES %s", list(chunks.items()))
            cursor.execute("INSERT INTO capture_attempts(id,collector,block_id,height,manifest,metadata,error) VALUES (%s,%s,%s,%s,%s,%s,NULL)",
                           (attempt, "legacy", "block-8", 8, Json(manifest), Json({})))
            snapshot = validate_snapshot(data)
            cursor.execute("""INSERT INTO observed_blocks(id,height,previous_id,round,timestamp,blocks_per_round,semantic_digest,attempt_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", ("block-8", 8, snapshot.previous_block_id, snapshot.round, snapshot.timestamp, 4,
                _semantic(snapshot, data), attempt))
            cursor.execute("UPDATE observation_stream SET latest_seen_height=8, contiguous_height=8 WHERE name='tig'")
        replay, snapshot = self.store.read("block-8")
        self.assertEqual(replay, data)
        # A recapture of the same block by the paged code is the same block, not a conflict.
        self.assertTrue(self.store.record(data, collector="paged")["complete"])
        self.assertEqual(self.store.status()["conflicting_heights"], [])
        self.assertEqual(self.store.status()["contiguous_height"], 8)

    def test_reordered_api_lists_are_the_same_block(self):
        data = observation(8)
        shuffled = deepcopy(data)
        for player in shuffled["players"].values():
            for field in ("precommits", "benchmarks", "proofs"):
                player[field] = list(reversed(player[field]))
        shuffled["algorithms"]["codes"] = list(reversed(shuffled["algorithms"]["codes"]))
        self.assertTrue(self.store.record(data, collector="one")["complete"])
        self.assertTrue(self.store.record(shuffled, collector="two")["complete"])
        self.assertEqual(self.store.status()["conflicting_heights"], [])
        self.assertEqual(self.row("SELECT count(*) AS n FROM observed_blocks")["n"], 1)
        self.assertEqual(self.store.read("block-8")[0], canonical_observation(shuffled))


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
