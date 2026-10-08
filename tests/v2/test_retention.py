from copy import deepcopy
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import uuid

from psycopg2.extras import Json

from pool_manager.pool_v2 import benchmarks, chain_observer, funding, retention
from pool_manager.pool_v2.block_observer import BlockStore, canonical_observation
from pool_manager.pool_v2.money import FundsError
from pool_manager.pool_v2.protocol import ProtocolDataError
from pool_manager.pool_v2.qualifiers import credit_block
from pool_manager.pool_v2.spool import Spool
from funds_helpers import DatabaseCase
from observer_helpers import observation

PLAYER = "0x" + "1" * 40


class RetentionTests(DatabaseCase):
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

    def record(self, heights, credit=True):
        for height in heights:
            result = self.store.record(observation(height), collector="one")
            self.assertTrue(result["complete"], result)
            if credit:
                credit_block(self.db, result["block_id"], PLAYER)

    def proof(self, covered_until=None, now=None):
        now = now or datetime.now(timezone.utc)
        started = covered_until or now
        return {"snapshot": started.strftime("%Y-%m-%dT%H%M%SZ"), "completed_at_utc": now.isoformat(),
                "covered_until": started.isoformat(), "source_host": "test", "files": {}}

    def captured_at(self, block_id):
        return self.row("SELECT captured_at FROM observed_blocks WHERE id=%s", (block_id,))["captured_at"]

    def test_backup_proof_must_be_verified_and_fresh(self):
        now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latest.json"
            good = {"snapshot": "2026-10-08T100000Z", "completed_at_utc": "2026-10-08T10:30:00+00:00",
                    "data_checksums_verified": True, "wal_parse_verified": True, "source_host": "primary"}
            path.write_text(json.dumps(good))
            proof = retention.backup_proof(path, now=now)
            self.assertEqual(proof["covered_until"], "2026-10-08T10:00:00+00:00")
            path.write_text(json.dumps({**good, "wal_parse_verified": False}))
            with self.assertRaisesRegex(ProtocolDataError, "verified"):
                retention.backup_proof(path, now=now)
            path.write_text(json.dumps(good))
            with self.assertRaisesRegex(ProtocolDataError, "too old"):
                retention.backup_proof(path, now=now + timedelta(days=3))

    def test_raw_blocks_expire_at_the_round_cap_and_derived_credit_stays(self):
        # Rounds are four blocks long in the fixture: heights 8..31 are rounds 3..8.
        self.record(range(8, 32))
        before = self.row("SELECT count(*) AS n FROM observation_chunks")["n"]
        record = retention.run(self.db, proof=self.proof())
        # Current round 8, cap 4: rounds 3 and 4 (heights 8..15) expire; round 5 stays.
        self.assertEqual(record["floors"]["block_height"], 15)
        self.assertEqual(record["counts"]["expired_capture_manifests"], 8)
        expired = self.row("SELECT count(*) AS n FROM capture_attempts WHERE manifest ? 'expired'")["n"]
        self.assertEqual(expired, 8)
        with self.assertRaisesRegex(ProtocolDataError, "expired under the retention policy"):
            self.store.read("block-8")
        self.assertEqual(self.store.read("block-16")[0], canonical_observation(observation(16)))
        # Only the eight expired blocks' manifest chunks were removed; shared records stay.
        self.assertEqual(record["counts"]["removed_chunks"], 8)
        self.assertEqual(self.row("SELECT count(*) AS n FROM observation_chunks")["n"], before - 8)
        self.assertEqual(self.row("SELECT count(*) AS n FROM credited_blocks")["n"], 24)
        self.assertEqual(self.row("SELECT count(*) AS n FROM observed_blocks")["n"], 24)
        self.assertEqual(self.store.status()["contiguous_height"], 31)
        self.assertEqual(self.row("SELECT count(*) AS n FROM retention_runs")["n"], 1)
        # A second run finds nothing more to do.
        again = retention.run(self.db, proof=self.proof())
        self.assertEqual(again["counts"]["expired_capture_manifests"], 0)
        self.assertEqual(again["counts"]["removed_chunks"], 0)

    def test_backup_coverage_holds_the_floor(self):
        self.record(range(8, 32))
        # A backup that started before block 12 was captured covers only round 3.
        record = retention.run(self.db, proof=self.proof(covered_until=self.captured_at("block-12")))
        self.assertEqual(record["floors"]["block_height"], 11)
        self.assertTrue(any("waits for a verified backup" in note for note in record["notes"]), record["notes"])
        # With everything backed up the cap applies: rounds 3 and 4 expire.
        record = retention.run(self.db, proof=self.proof())
        self.assertEqual(record["floors"]["block_height"], 15)
        self.assertFalse(any("backup" in note or "credit coverage" in note for note in record["notes"]), record["notes"])

    def test_uncredited_round_holds_the_floor_even_under_the_cap(self):
        self.record(range(8, 12))
        self.record(range(12, 16), credit=False)
        self.record(range(16, 32))
        record = retention.run(self.db, proof=self.proof())
        self.assertEqual(record["floors"]["block_height"], 11)
        self.assertTrue(any("incomplete block or credit coverage" in note for note in record["notes"]), record["notes"])
        self.assertEqual(self.row("SELECT count(*) AS n FROM capture_attempts WHERE manifest ? 'expired'")["n"], 4)

    def test_dry_run_changes_nothing(self):
        self.record(range(8, 32))
        record = retention.run(self.db, proof=self.proof(), dry_run=True)
        self.assertTrue(record["dry_run"])
        self.assertEqual(record["floors"]["block_height"], 15)
        self.assertEqual(self.row("SELECT block_height FROM retention_floors")["block_height"], -1)
        self.assertEqual(self.row("SELECT count(*) AS n FROM capture_attempts WHERE manifest ? 'expired'")["n"], 0)

    def test_guard_refuses_changes_outside_a_run_or_above_the_floor(self):
        self.record(range(8, 32))
        with self.assertRaisesRegex(Exception, "append-only"):
            with self.db.transaction() as cursor:
                cursor.execute("DELETE FROM observation_chunks WHERE digest=(SELECT digest FROM observation_chunks LIMIT 1)")
        with self.assertRaisesRegex(Exception, "retention floor"):
            with self.db.transaction() as cursor:
                cursor.execute("SET LOCAL pool_v2.retention TO 'on'")
                cursor.execute("UPDATE capture_attempts SET manifest=%s WHERE height=31", (Json({"expired": {}}),))
        with self.assertRaisesRegex(Exception, "only advance"):
            with self.db.transaction() as cursor:
                cursor.execute("SET LOCAL pool_v2.retention TO 'on'")
                cursor.execute("UPDATE retention_floors SET block_height=-1, reporting_round=0 WHERE name='evidence'")
                cursor.execute("UPDATE retention_floors SET block_height=5 WHERE name='evidence'")
                cursor.execute("UPDATE retention_floors SET block_height=4 WHERE name='evidence'")
        with self.assertRaisesRegex(Exception, "only inside a retention run"):
            with self.db.transaction() as cursor:
                cursor.execute("UPDATE retention_floors SET block_height=5 WHERE name='evidence'")

    EXTRA = {"id": "x", "state": {"banned": False, "round_active": 1}, "details": {"challenge_id": "c1", "name": "fixture_x"},
             "block_data": {"adoption": "1", "num_qualifiers_by_track_by_player": {}}}
    EXTRA_DIGEST = hashlib.sha256(json.dumps(EXTRA, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def with_extra(self, height):
        data = observation(height)
        data["algorithms"]["codes"].append(deepcopy(self.EXTRA))
        data["algorithms"]["binarys"].append({"algorithm_id": "x", "details": {"compile_success": True,
            "download_url": "https://example.invalid/binary/x"}, "state": {"block_confirmed": 1}})
        for block in (data["start"]["block"], data["end"]["block"]):
            block["data"]["active_ids"]["code"].append("x")
            block["details"]["num_active"]["code"] += 1
        return data

    def test_chunk_index_follows_drops_backfills_and_rereferences(self):
        with_extra, digest = self.with_extra, self.EXTRA_DIGEST

        def last_ref():
            row = self.row("SELECT height FROM chunk_last_ref WHERE digest=%s", (digest,))
            return row["height"] if row else None

        self.store.record(with_extra(8), collector="one")
        self.assertIsNone(last_ref())
        self.store.record(observation(9), collector="one")
        self.assertEqual(last_ref(), 8)
        self.store.record(with_extra(11), collector="one")     # re-referenced by the newest block
        self.assertIsNone(last_ref())
        self.store.record(observation(12), collector="one")
        self.assertEqual(last_ref(), 11)
        self.store.record(with_extra(10), collector="replica")  # a recovered older block keeps the later use
        self.assertEqual(last_ref(), 11)
        self.store.record(observation(13), collector="one")
        self.assertEqual(self.store.status()["contiguous_height"], 13)
        # Every expired block's manifest chunk gets a row once the next block arrives.
        reference = self.row("SELECT manifest->>'manifest_ref' AS r FROM capture_attempts WHERE height=12")["r"]
        self.assertEqual(self.row("SELECT height FROM chunk_last_ref WHERE digest=%s", (reference,))["height"], 12)

    def test_chunk_index_initialization_bounds_older_chunks_by_the_newest_block(self):
        self.record(range(8, 12), credit=False)
        with self.db.transaction() as cursor:
            cursor.execute("DELETE FROM chunk_last_ref")
            cursor.execute("INSERT INTO observation_chunks(digest,compressed) VALUES (%s,%s)", ("f" * 64, gzip.compress(b"orphan")))
        result = retention.initialize_chunk_index(self.db)
        self.assertEqual(result["newest_height"], 11)
        self.assertEqual(self.row("SELECT height FROM chunk_last_ref WHERE digest=%s", ("f" * 64,))["height"], 11)
        self.assertIsNone(self.row("SELECT height FROM chunk_last_ref WHERE digest=%s",
            (self.row("SELECT manifest->>'manifest_ref' AS r FROM capture_attempts WHERE height=11")["r"],)))
        with self.assertRaisesRegex(ProtocolDataError, "already initialized"):
            retention.initialize_chunk_index(self.db)

    def test_report_and_capture_payloads_expire_except_cited_and_latest(self):
        self.record(range(8, 32))
        old = datetime.now(timezone.utc) - timedelta(days=40)
        ids = {block: uuid.uuid4() for block in ("block-8", "block-9", "block-12", "block-28")}
        with self.db.transaction() as cursor:
            for round_number, block, when in ((2, "block-8", old), (2, "block-9", old), (3, "block-12", old), (7, "block-28", old)):
                cursor.execute("""INSERT INTO report_captures(id,reporting_round,block_id,payload_sha256,input_digest,compressed_payload,metadata,complete,created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,true,%s)""", (ids[block], round_number, block, "a" * 64, block, gzip.compress(b"{}"), Json({}), when))
            cited = ids["block-8"]
            cursor.execute("""INSERT INTO confirmed_reports(report_id,reporting_round,benchmark_id,benchmarker,nonce,confirmed_height,capture_id)
                VALUES ('r',2,'b','p',1,8,%s)""", (cited,))
            for identity, when in (("old", old), ("new", datetime.now(timezone.utc))):
                raw = json.dumps({"capture": identity}).encode()
                cursor.execute("INSERT INTO funding_captures(id,payload_gzip,player_id,checked_at,complete,created_at) VALUES (%s,%s,%s,%s,true,%s)",
                               (identity, gzip.compress(raw), PLAYER, when, when))
                cursor.execute("INSERT INTO chain_captures(id,payload_gzip,created_at) VALUES (%s,%s,%s)", (identity, gzip.compress(raw), when))
            cursor.execute("INSERT INTO funding_captures(id,payload_gzip,player_id,checked_at,complete,created_at) VALUES ('cited',%s,%s,%s,true,%s)",
                           (gzip.compress(b"{}"), PLAYER, old, old))
            cursor.execute("INSERT INTO funding_alerts(capture_id,details) VALUES ('cited',%s)", (Json({}),))
        record = retention.run(self.db, proof=self.proof())
        # Current round 8, report cap 5: reporting rounds 2 and 3 are under the floor.
        self.assertEqual(record["floors"]["reporting_round"], 3)
        states = {row["block_id"]: row["expired_at"] is not None for row in
                  self.rows("SELECT block_id,expired_at FROM report_captures")}
        self.assertEqual(states, {"block-8": False, "block-9": True, "block-12": False, "block-28": False})
        self.assertEqual(record["counts"]["expired_report_captures"], 1)
        expired_funding = {row["id"] for row in self.rows("SELECT id FROM funding_captures WHERE expired_at IS NOT NULL")}
        self.assertEqual(expired_funding, {"old"})
        expired_chain = {row["id"] for row in self.rows("SELECT id FROM chain_captures WHERE expired_at IS NOT NULL")}
        self.assertEqual(expired_chain, {"old"})
        with self.assertRaisesRegex(FundsError, "expired under the retention policy"):
            funding.read_capture(self.db, "old")
        with self.assertRaisesRegex(FundsError, "expired under the retention policy"):
            chain_observer.read(self.db, "old")

    def rows(self, sql, args=()):
        with self.db.transaction() as cursor:
            cursor.execute(sql, args)
            return cursor.fetchall()

    def test_spool_prune_removes_expired_archives_and_unreferenced_chunks_but_keeps_pending(self):
        # Block 8 alone carries an extra record; it and its page become removable once block 8 expires.
        self.assertTrue(self.store.record(self.with_extra(8), collector="one")["complete"])
        credit_block(self.db, "block-8", PLAYER)
        self.record(range(9, 32))
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(directory, paged=True)
            for height in range(8, 32):
                data = self.with_extra(8) if height == 8 else observation(height)
                path = spool.save(data, {"collector": "one", "captured_at": datetime.now(timezone.utc).isoformat()})
                spool.recorded(path)
            extra_chunk = Path(directory) / "chunks" / self.EXTRA_DIGEST[:2] / (self.EXTRA_DIGEST + ".gz")
            self.assertTrue(extra_chunk.exists())
            pending = spool.save(observation(32), {"collector": "one", "captured_at": datetime.now(timezone.utc).isoformat()})
            record = retention.run(self.db, proof=self.proof(), spools=[directory])
            counts = record["counts"]["spool:" + directory]
            self.assertEqual(counts["archives"], 8)
            self.assertEqual(len(list((Path(directory) / "recorded").glob("*.json.gz"))), 16)
            # The expired blocks' manifest chunks are gone from the spool as well, but
            # nothing the pending archive still needs.
            # The extra record and the page that listed it are gone; nothing the pending archive needs is.
            self.assertGreaterEqual(counts["chunks"], 2)
            self.assertFalse(extra_chunk.exists())
            self.assertEqual(counts["kept_for_pending"], 0)
            self.assertEqual(spool.read(pending)[0], canonical_observation(observation(32)))
            for path in (Path(directory) / "recorded").glob("*.json.gz"):
                spool.read(path)
