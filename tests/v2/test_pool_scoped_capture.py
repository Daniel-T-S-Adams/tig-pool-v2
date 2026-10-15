"""Pool-scoped block captures and on-demand whole-network selection references."""

from copy import deepcopy
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pool_manager.pool_v2 import benchmarks, compaction, references as network_references, work_requests
from pool_manager.pool_v2.block_observer import BlockStore
from pool_manager.pool_v2.compact import compact_observation
from pool_manager.pool_v2.coordinator import Coordinator
from pool_manager.pool_v2.money import TIG
from pool_manager.pool_v2.observation import canonical_json, capture_pool_snapshot, read_archive
from pool_manager.pool_v2.protocol import ProtocolDataError, SCOPED_FORMAT, equal_bundle_credit, validate_snapshot
from pool_manager.pool_v2.qualifiers import credit_block
from pool_manager.pool_v2.selection import ReferenceIndex, choose, references
from pool_manager.pool_v2.spool import Spool
from funds_helpers import DatabaseCase, WALLET, OTHER
from observer_helpers import observation
from test_compact_storage import network_observation


class FakeClient:
    """Serves one recorded network; remembers which feeds were requested."""

    def __init__(self, data):
        self.data, self.records, self.paths = data, [], []

    def get(self, path, params=None):
        params = dict(params or {})
        self.paths.append((path, params.get("player_id")))
        self.records.append({"path": path, "params": params})
        if path == "/get-block":
            return deepcopy(self.data["start"] if params.get("include_data") else self.data["end"])
        if path == "/get-benchmarks":
            player = params["player_id"]
            if player in self.data["players"]:
                return deepcopy(self.data["players"][player])
            if self.data.get("pool_player_id") == player and "pool_pending" in self.data:
                return deepcopy(self.data["pool_pending"])
            return {"precommits": [], "benchmarks": [], "proofs": [], "frauds": []}
        return deepcopy(self.data[{"/get-algorithms": "algorithms", "/get-challenges": "challenges", "/get-opow": "opow"}[path]])


def scoped(data, player):
    return capture_pool_snapshot(FakeClient(data), player)


def live_fixture():
    data = read_archive(next((Path(__file__).parent / "fixtures").glob("block-1351111*")))["observation"]
    return data, next(iter(data["players"]))


class ScopedCaptureTests(unittest.TestCase):
    def test_capture_requests_only_block_tables_and_the_pool_feed(self):
        data = network_observation()
        client = FakeClient(data)
        result = capture_pool_snapshot(client, WALLET)
        self.assertEqual(sorted(client.paths), sorted([("/get-block", None), ("/get-block", None), ("/get-algorithms", None),
                                                       ("/get-challenges", None), ("/get-opow", None), ("/get-benchmarks", WALLET)]))
        self.assertEqual(result["format"], SCOPED_FORMAT)
        self.assertEqual(set(result["players"]), {WALLET})
        self.assertEqual(result["start"]["block"]["data"]["active_ids"]["benchmark"], ["b", "owned-second"])
        self.assertEqual(result["scope"], {"active_benchmark_count": 3, "active_benchmark_ids_sha256":
            hashlib.sha256(canonical_json(["b", "other-benchmark", "owned-second"])).hexdigest()})
        snapshot = validate_snapshot(result)
        self.assertTrue(snapshot.scoped)
        self.assertEqual({bundle.player_id for bundle in snapshot.bundles}, {WALLET})
        self.assertEqual(snapshot.qualifiers, {(WALLET, "c1", "a1", "t"): 2})
        self.assertEqual(set(snapshot.precommits), {"b", "owned-second"})
        # A pool that is not yet in OPoW keeps its pending feed and has no active credit.
        pending = observation()
        pending["pool_player_id"] = "new-pool"
        pending["pool_pending"] = {"precommits": [], "benchmarks": [], "proofs": [], "frauds": []}
        result = capture_pool_snapshot(FakeClient(pending), "new-pool")
        self.assertEqual((result["players"], result["pool_pending"]), ({}, pending["pool_pending"]))
        self.assertEqual(result["start"]["block"]["data"]["active_ids"]["benchmark"], [])
        self.assertEqual(validate_snapshot(result).bundles, ())

    def test_scoped_credit_matches_the_full_network_validation(self):
        live, live_player = live_fixture()
        for data, player in ((network_observation(), WALLET), (live, live_player)):
            full = validate_snapshot(deepcopy(data))
            snapshot = validate_snapshot(scoped(data, player))
            owned = {bundle.benchmark_id for bundle in full.bundles if bundle.player_id == player}
            self.assertTrue(owned)
            self.assertEqual(equal_bundle_credit(snapshot),
                             {key: value for key, value in equal_bundle_credit(full).items() if key[0] in owned})
            self.assertEqual(snapshot.qualifiers, {key: value for key, value in full.qualifiers.items() if key[0] == player})
            self.assertEqual(snapshot.algorithms, full.algorithms)
            compact = validate_snapshot(compact_observation({**deepcopy(data), "pool_player_id": player}))
            self.assertEqual(equal_bundle_credit(snapshot), equal_bundle_credit(compact))
        # The retained capture is a fraction of the full read.
        self.assertLess(len(canonical_json(scoped(live, live_player))), len(canonical_json(live)) // 5)

    def test_scoped_validation_rejects_inconsistent_network_totals_and_feeds(self):
        base = network_observation()
        good = scoped(base, WALLET)
        validate_snapshot(deepcopy(good))

        def reject(mutate, message):
            data = deepcopy(good)
            mutate(data)
            with self.assertRaisesRegex(ProtocolDataError, message):
                validate_snapshot(data)
        # Another player's totals still have to reconcile, although its feed was never read.
        reject(lambda d: d["opow"]["opow"][1]["block_data"]["num_qualifiers_by_challenge_by_track"]["c1"].__setitem__("t", 5), "OPoW")
        reject(lambda d: d["challenges"]["challenges"][0]["block_data"]["num_qualifiers_by_track"].__setitem__("t", 9), "challenge totals")
        # The pool's own count is checked against the pool's own bundles.
        reject(lambda d: d["algorithms"]["codes"][0]["block_data"]["num_qualifiers_by_track_by_player"]["t"].__setitem__(WALLET, 7), "exceeds")
        reject(lambda d: d["players"].__setitem__(OTHER, deepcopy(base["players"][OTHER])), "exactly the pool")
        reject(lambda d: d["players"][WALLET].__setitem__("proofs", []), "missing 2 active benchmark proofs")
        reject(lambda d: d["end"]["block"].__setitem__("id", "block-9"), "block changed")
        reject(lambda d: d["scope"].__setitem__("active_benchmark_count", 2), "contradict")
        reject(lambda d: d["start"]["block"]["data"]["active_ids"]["benchmark"].append("other-benchmark"), "missing 1 active benchmark precommits")
        reject(lambda d: d.pop("scope"), "malformed")
        reject(lambda d: d["start"]["block"]["data"]["active_ids"]["opow"].remove(WALLET), "opow ID count")

        def deactivate(d):
            d["start"]["block"]["data"]["active_ids"]["opow"].remove(WALLET)
            for anchor in ("start", "end"):
                d[anchor]["block"]["details"]["num_active"]["opow"] -= 1
        # A pool outside OPoW cannot present an active feed or active benchmark credit.
        reject(deactivate, "exactly the pool")

    def test_scoped_snapshot_selects_only_with_a_fresh_external_reference(self):
        data = network_observation()
        full = validate_snapshot(deepcopy(data))
        snapshot = validate_snapshot(scoped(data, WALLET))
        index = references(full)
        external = ReferenceIndex(full.block_id, index.values, height=full.height, timestamp=full.timestamp,
            capture_id="capture-1", digest="d" * 64,
            hyperparameters={key: full.precommits[bundle.benchmark_id]["details"]["hyperparameters"]
                             for key, bundle in index.values.items()})
        binaries = data["algorithms"]["binarys"]

        def select(**kwargs):
            return choose(snapshot, binaries, player_id=WALLET, resource="CPU", compute_type="aws_c7a",
                          now=kwargs.pop("now", snapshot.timestamp), **kwargs)
        with self.assertRaisesRegex(ProtocolDataError, "needs a whole-network reference"):
            select()
        result = select(reference_index=external)
        self.assertEqual(result.payload["track_settings"]["t"]["hyperparameters"], {"network_best": 123})
        self.assertEqual(result.evidence["references"]["t"]["benchmark_id"], "other-benchmark")
        self.assertEqual(result.evidence["reference_capture"], {"id": "capture-1", "block_id": full.block_id,
            "height": full.height, "timestamp": full.timestamp, "digest": "d" * 64})
        self.assertEqual(result.evidence["pool_counts"], {"c1": 2})
        with self.assertRaisesRegex(ProtocolDataError, "stale"):
            select(reference_index=replace(external, timestamp=snapshot.timestamp - 1801))
        select(reference_index=replace(external, timestamp=snapshot.timestamp - 1801), reference_max_age=3600)
        with self.assertRaisesRegex(ProtocolDataError, "after the current snapshot"):
            select(reference_index=replace(external, height=full.height + 1))
        with self.assertRaisesRegex(ProtocolDataError, "lacks its parameters"):
            select(reference_index=replace(external, hyperparameters=None))
        with self.assertRaisesRegex(ProtocolDataError, "another block"):
            select(reference_index=references(validate_snapshot(observation(9))))

    def test_reference_capture_keeps_best_bundles_and_parameters_only(self):
        data = network_observation()
        client = FakeClient(data)
        payload, metadata = network_references.capture(client, WALLET)
        self.assertEqual(payload["format"], "network-references-v1")
        self.assertEqual(payload["block"], {"id": "block-8", "height": 8, "round": 3, "timestamp": 1800000008})
        self.assertEqual([row["bundle"]["benchmark_id"] for row in payload["references"]], ["other-benchmark"])
        self.assertEqual(payload["references"][0]["hyperparameters"], {"network_best": 123})
        self.assertEqual(payload["network"], {"players": 2, "active_benchmarks": 3, "bundles": 7})
        self.assertEqual(client.paths.count(("/get-benchmarks", OTHER)), 1)
        self.assertNotIn("players", payload)
        self.assertNotIn("requests", metadata)
        self.assertEqual(metadata["endpoints"]["/get-benchmarks"], 2)
        broken = deepcopy(payload)
        broken["references"].append(deepcopy(payload["references"][0]))
        with self.assertRaisesRegex(ProtocolDataError, "duplicate"):
            network_references.validate_payload(broken)


class ScopedStorageTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.store = BlockStore(self.db)
        self.store.initialize(8)

    def own(self, benchmark_id):
        row = benchmarks.reserve(self.db, self.member, "owned-" + benchmark_id, creation_round=1, resource="CPU",
            selection={"block_id": "block-1"}, payload={"track_settings": {"t": {"num_bundles": 2}}},
            fee_limit=0, offer_expires_at=self.expiry)
        benchmarks.mark_submitting(self.db, row["id"])
        row = benchmarks.accept(self.db, row["id"], benchmark_id, {"benchmark_id": benchmark_id}, actual_fee=0, evidence={"fixture": True})
        benchmarks.acknowledge(self.db, row["id"], self.member, row["assignment_digest"])

    def test_scoped_blocks_store_one_blob_replay_and_credit_members(self):
        self.fund(amount=200 * TIG)
        self.own("b")
        self.own("owned-second")
        data = scoped(network_observation(), WALLET)
        result = self.store.record(data, collector="primary", metadata={"requests": [{"path": "/get-block"}]})
        self.assertTrue(result["complete"], result)
        attempt = self.row("SELECT manifest,metadata FROM capture_attempts WHERE id=%s", (result["attempt_id"],))
        self.assertEqual(set(attempt["manifest"]), {"compact_ref"})
        self.assertEqual(attempt["metadata"]["request_count"], 1)
        self.assertEqual(self.row("SELECT count(*) AS n FROM observation_chunks")["n"], 1)
        replay, snapshot = self.store.read("block-8")
        self.assertEqual((replay["format"], set(replay["players"]), snapshot.scoped), (SCOPED_FORMAT, {WALLET}, True))
        self.assertEqual(credit_block(self.db, "block-8", WALLET), 2)
        self.assertEqual(self.row("SELECT count(*) AS n FROM benchmark_credits")["n"], 2)
        # An independent collector whose API responses came in another order records the same block.
        again = scoped(network_observation(), WALLET)
        again["players"][WALLET]["benchmarks"].reverse()
        again["algorithms"]["codes"].reverse()
        again["start"]["block"]["data"]["active_ids"]["code"].reverse()
        self.assertTrue(self.store.record(again, collector="recovery")["complete"])
        self.assertEqual(self.row("SELECT count(*) AS n FROM observation_alerts")["n"], 0)
        self.assertEqual(self.row("SELECT count(*) AS n FROM observation_chunks")["n"], 1)
        # Legacy conversion has nothing to do with a scoped capture.
        self.assertEqual(compaction.backfill(self.db, player_id=WALLET)["converted"], 0)
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(directory, paged=True)
            path = spool.save(replay, {"captured_at": "fixture"})
            saved = read_archive(path)
            self.assertEqual((set(saved["manifest"]), saved["metadata"]["block_height"]), ({"blob"}, 8))
            self.assertEqual(spool.read(path)[0], replay)

    def test_collector_in_pool_mode_never_requests_other_feeds(self):
        from tools.observe_tig_v2 import main
        data = network_observation()
        clients = []

        class Client(FakeClient):
            def __init__(self, url):
                super().__init__(data)
                clients.append(self)
        with tempfile.TemporaryDirectory() as directory, patch("tools.observe_tig_v2.PublicTigClient", Client), \
                patch("tools.observe_tig_v2.signal.signal"), patch.dict("os.environ", {"POOL_V2_DATABASE_DSN": self.db.dsn}):
            result = main(["--collector", "test", "--spool", directory, "--launch-height", "8", "--captures", "1",
                           "--poll-seconds", "0.01", "--pool-player-id", WALLET])
            self.assertEqual(result, 0)
            self.assertFalse(any(player == OTHER for client in clients for _, player in client.paths))
            self.assertEqual(len(list((Path(directory) / "chunks").glob("*/*.gz"))), 1)
        replay, snapshot = BlockStore(self.db).read("block-8")
        self.assertTrue(snapshot.scoped)
        self.assertEqual(self.row("SELECT metadata FROM capture_attempts")["metadata"]["endpoints"]["/get-benchmarks"], 1)


class NetworkReferenceTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.fund(amount=200 * TIG)
        self.fees()
        self.data = network_observation()
        self.store = BlockStore(self.db)
        self.store.initialize(8)
        self.assertTrue(self.store.record(scoped(self.data, WALLET), collector="fixture")["complete"])
        self.now = self.data["start"]["block"]["details"]["timestamp"]

    def refresh(self):
        payload, metadata = network_references.capture(FakeClient(self.data), WALLET)
        return network_references.record(self.db, payload, metadata)

    def test_references_are_recorded_once_and_loaded_with_identity_and_age_bound(self):
        first = self.refresh()
        self.assertTrue(first["new"])
        self.assertFalse(self.refresh()["new"])
        self.assertEqual(self.row("SELECT count(*) AS n FROM reference_captures")["n"], 1)
        index = network_references.latest(self.db, now=self.now, max_age_seconds=1800)
        self.assertEqual((index.block_id, index.height, index.timestamp, index.capture_id, index.digest),
                         ("block-8", 8, self.now, first["id"], first["digest"]))
        self.assertEqual(index.values[("a1", "t")].benchmark_id, "other-benchmark")
        self.assertEqual(index.hyperparameters[("a1", "t")], {"network_best": 123})
        self.assertIsNone(network_references.latest(self.db, now=self.now + 1801, max_age_seconds=1800))
        for statement in ("DELETE FROM reference_captures", "UPDATE reference_captures SET height=9"):
            with self.assertRaises(Exception):
                with self.db.transaction() as cursor:
                    cursor.execute(statement)

    def test_reservation_needs_a_fresh_reference_and_reads_one_only_on_demand(self):
        reads = []

        def refresh():
            reads.append(1)
            self.refresh()
        # Nobody waiting: nothing is read and nothing reserved.
        self.assertIsNone(work_requests.reserve_next(self.db, WALLET, now=self.now, refresh_references=refresh))
        self.assertEqual(reads, [])
        work_requests.create(self.db, self.member, "offer", resource="CPU", compute_type="aws_c7a", capacity={"workers": 1})
        with self.assertRaisesRegex(ProtocolDataError, "no fresh whole-network reference"):
            work_requests.reserve_next(self.db, WALLET, now=self.now)
        row = work_requests.reserve_next(self.db, WALLET, now=self.now, refresh_references=refresh)
        self.assertEqual(reads, [1])
        self.assertEqual(row["payload"]["track_settings"]["t"]["hyperparameters"], {"network_best": 123})
        capture = self.row("SELECT id,payload_sha256 FROM reference_captures")
        self.assertEqual(row["selection"]["reference_capture"]["id"], str(capture["id"]))
        self.assertEqual(row["selection"]["reference_capture"]["digest"], capture["payload_sha256"])
        # A second offer inside the reference age reuses the stored reference.
        work_requests.create(self.db, self.member, "second", resource="CPU", compute_type="aws_c7a", capacity={"workers": 1})
        self.assertIsNotNone(work_requests.reserve_next(self.db, WALLET, now=self.now + 60, refresh_references=refresh))
        self.assertEqual(reads, [1])

    def test_coordinator_reads_the_network_only_when_selection_needs_it(self):
        work_requests.create(self.db, self.member, "offer", resource="CPU", compute_type="aws_c7a", capacity={"workers": 1})
        public = FakeClient(self.data)
        writer = Mock()
        writer.enabled = True
        coordinator = Coordinator(self.db, WALLET, public, writer, new_work=True, reference_max_age=1800)
        with patch("pool_manager.pool_v2.coordinator.time.time", return_value=float(self.now)), \
                patch.object(coordinator, "dispatch_one", return_value=None), \
                patch("pool_manager.pool_v2.coordinator.reconcile_block"), \
                patch("pool_manager.pool_v2.coordinator.reconcile_pending", return_value={}), \
                patch("pool_manager.pool_v2.coordinator.controls.blocked", return_value=None):
            result = coordinator.step()
            self.assertIsNotNone(result["reserved"])
            self.assertEqual(public.paths.count(("/get-benchmarks", OTHER)), 1)
            self.assertEqual(coordinator.step()["reserved"], None)
        self.assertEqual(public.paths.count(("/get-benchmarks", OTHER)), 1)
        self.assertEqual(self.row("SELECT count(*) AS n FROM reference_captures")["n"], 1)
        with self.assertRaises(ValueError):
            Coordinator(self.db, WALLET, public, writer, reference_max_age=0)


class ReferenceRetentionTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.store = BlockStore(self.db)
        self.store.initialize(8)
        self.fund(amount=200 * TIG)
        row = benchmarks.reserve(self.db, self.member, "owned", creation_round=1, resource="CPU",
            selection={"block_id": "block-1"}, payload={"track_settings": {"t": {"num_bundles": 2}}},
            fee_limit=0, offer_expires_at=self.expiry)
        benchmarks.mark_submitting(self.db, row["id"])
        row = benchmarks.accept(self.db, row["id"], "b", {"benchmark_id": "b"}, actual_fee=0, evidence={"fixture": True})
        benchmarks.acknowledge(self.db, row["id"], self.member, row["assignment_digest"])

    def proof(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        return {"snapshot": now.strftime("%Y-%m-%dT%H%M%SZ"), "completed_at_utc": now.isoformat(),
                "covered_until": now.isoformat(), "source_host": "test", "files": {}}

    def test_reference_payloads_expire_under_the_block_floor_unless_cited_or_newest(self):
        from pool_manager.pool_v2 import retention
        # Rounds are four blocks long in the fixture: heights 8..31 are rounds 3..8.
        for height in range(8, 32):
            result = self.store.record(scoped(observation(height), WALLET), collector="one")
            self.assertTrue(result["complete"], result)
            credit_block(self.db, result["block_id"], WALLET)

        def reference(height):
            payload, metadata = network_references.capture(FakeClient(observation(height)), WALLET)
            return network_references.record(self.db, payload, metadata)
        old, cited, newest = reference(8), reference(9), reference(30)
        benchmarks.reserve(self.db, self.member, "cites", creation_round=3, resource="CPU",
            selection={"block_id": "block-9", "reference_capture": {"id": cited["id"]}},
            payload={"track_settings": {"t": {"num_bundles": 2}}}, fee_limit=0, offer_expires_at=self.expiry)
        record = retention.run(self.db, proof=self.proof())
        self.assertEqual(record["floors"]["block_height"], 15)
        self.assertEqual(record["counts"]["expired_reference_captures"], 1)
        with self.db.transaction() as cursor:
            cursor.execute("SELECT id,expired_at,compressed_payload FROM reference_captures")
            states = {str(row["id"]): (row["expired_at"] is not None, len(row["compressed_payload"])) for row in cursor.fetchall()}
        self.assertEqual(states[old["id"]], (True, 0))
        self.assertEqual((states[cited["id"]][0], states[newest["id"]][0]), (False, False))
        self.assertGreater(states[cited["id"]][1], 0)
        self.assertEqual(network_references.latest(self.db, now=1800000030, max_age_seconds=10**6).capture_id, newest["id"])
        self.assertEqual(retention.run(self.db, proof=self.proof())["counts"]["expired_reference_captures"], 0)
