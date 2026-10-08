"""Expire bounded-life evidence under a recorded policy; derived records are permanent.

Raw block captures, fraud-report captures and wallet/fee captures are kept
only while an obligation can still need them. Ledger, credit, benchmark and
settlement records are never expired. A run advances recorded floors, blanks
payloads under them, removes chunks that only expired captures referenced,
and records what it did. Nothing expires unless a verified backup covers it.
"""

from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import uuid

from psycopg2.extras import Json

from .block_observer import BlockStore, manifest_references
from .database import lock
from .observation import canonical_json, read_archive
from .protocol import ProtocolDataError

POLICY = {"version": "evidence-retention-v2", "raw_block_rounds": 4, "report_rounds": 4,
          "capture_days": 30, "settled_margin_rounds": 3}


def parse_time(value):
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ProtocolDataError("timestamps must carry a timezone")
    return moment


def backup_proof(path, *, now, max_age_hours=36):
    """A verified base backup record; evidence captured before it started is covered."""
    data = json.loads(Path(path).read_text())
    if data.get("data_checksums_verified") is not True or data.get("wal_parse_verified") is not True:
        raise ProtocolDataError("backup proof is not a verified backup")
    completed = parse_time(data["completed_at_utc"])
    started = datetime.strptime(data["snapshot"], "%Y-%m-%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    if not started <= completed <= now:
        raise ProtocolDataError("backup proof times are inconsistent")
    if now - completed > timedelta(hours=max_age_hours):
        raise ProtocolDataError("backup proof is too old to rely on")
    return {"snapshot": data["snapshot"], "completed_at_utc": completed.isoformat(),
            "covered_until": started.isoformat(), "source_host": data.get("source_host"), "files": data.get("files")}


def plan(database, cursor, policy, covered_until, now, notes):
    """Target floors from the policy, settlement, credit coverage and backup coverage."""
    cursor.execute("SELECT * FROM retention_floors WHERE name='evidence' FOR UPDATE")
    current = dict(cursor.fetchone())
    cutoff = min(now - timedelta(days=policy["capture_days"]), covered_until)
    target = {"block_height": current["block_height"], "reporting_round": current["reporting_round"],
              "capture_cutoff": max(current["capture_cutoff"], cutoff)}
    cursor.execute("SELECT height,round,blocks_per_round FROM observed_blocks ORDER BY height DESC LIMIT 1")
    head = cursor.fetchone()
    if not head:
        notes.append("no observed blocks; floors unchanged")
        return current, target, None
    length, current_round = head["blocks_per_round"], head["round"]
    cursor.execute("SELECT launch_height FROM observation_stream WHERE name='tig'")
    launch = cursor.fetchone()["launch_height"]
    cursor.execute("SELECT round FROM round_settlements")
    settled = {row["round"] for row in cursor.fetchall()}
    cursor.execute("SELECT coalesce(max(height),-1) AS height FROM observed_blocks WHERE captured_at<=%s", (covered_until,))
    covered_height = cursor.fetchone()["height"]
    store = BlockStore(database)
    first_round = max(launch // length + 1, (current["block_height"] + 1) // length + 1)
    for round_number in range(first_round, current_round):
        capped = round_number <= current_round - policy["raw_block_rounds"]
        settled_ok = round_number in settled and current_round >= round_number + policy["settled_margin_rounds"]
        if not (capped or settled_ok):
            break
        if not store.round_coverage(round_number, require_credits=True):
            notes.append(f"round {round_number}: incomplete block or credit coverage; raw evidence retained")
            break
        last = round_number * length - 1
        if last > covered_height:
            notes.append(f"round {round_number}: waits for a verified backup covering height {last}")
            break
        target["block_height"] = last
    for round_number in range(current["reporting_round"] + 1, current_round):
        if round_number > current_round - policy["report_rounds"]:
            if current_round < round_number + policy["settled_margin_rounds"]:
                break
            # Early expiry needs every pool benchmark that could still report into
            # this round to have its collateral outcome; otherwise wait for the cap.
            cursor.execute("""SELECT count(*) AS n FROM reservations v
                LEFT JOIN collateral_finalizations f ON f.reservation_id=v.id
                WHERE v.creation_round<=%s AND v.collateral_outcome IS NULL AND f.reservation_id IS NULL""", (round_number,))
            if cursor.fetchone()["n"]:
                notes.append(f"reporting round {round_number}: pool collateral not yet finalized; report captures retained")
                break
        target["reporting_round"] = round_number
    return current, target, dict(head)


def _height(manifest):
    node = manifest
    for key in ("start", "block", "details", "height"):
        if "dict" not in node:
            return None
        children = dict(node["dict"])
        if key not in children:
            return None
        node = children[key]
    return node.get("value") if isinstance(node.get("value"), int) else None


def _spool_references(directory, manifest):
    """Chunk digests a spool archive depends on, expanding digest pages from chunk files."""
    refs, pages = set(), set()

    def collect(node):
        if "pages" in node:
            pages.update(node["pages"])
        elif "items" in node:
            refs.update(node["items"])
        elif "blob" in node:
            refs.add(node["blob"])
        elif "records" in node:
            refs.update(node["records"])
        elif "dict" in node:
            for _, child in node["dict"]:
                collect(child)
    collect(manifest)
    refs.update(pages)
    for page in pages:
        path = directory / "chunks" / page[:2] / (page + ".gz")
        if path.exists():
            raw = gzip.decompress(path.read_bytes())
            refs.update(raw[index:index + 32].hex() for index in range(0, len(raw), 32))
    return refs


def prune_spool(directory, *, block_floor, capture_cutoff, covered_until, removed_digests):
    """Remove recorded archives under the floor and chunk files the database removed.

    Chunks still referenced by a pending (unrecorded) archive are kept so the
    recorder can replay it.
    """
    directory = Path(directory)
    counts = {"archives": 0, "chunks": 0, "kept_for_pending": 0}
    pending = set()
    for path in (directory / "pending").glob("*.json.gz"):
        pending.update(_spool_references(directory, read_archive(path)["manifest"]))
    for path in sorted((directory / "recorded").glob("*.json.gz")):
        saved = read_archive(path)
        captured = saved.get("metadata", {}).get("captured_at")
        captured_at = parse_time(captured) if isinstance(captured, str) else None
        if captured_at is not None and captured_at > covered_until:
            continue
        height = _height(saved["manifest"])
        expired = (height is not None and height <= block_floor) or (
            height is None and captured_at is not None and captured_at <= capture_cutoff)
        if expired:
            path.unlink()
            counts["archives"] += 1
    for digest in removed_digests:
        if digest in pending:
            counts["kept_for_pending"] += 1
            continue
        path = directory / "chunks" / digest[:2] / (digest + ".gz")
        if path.exists():
            path.unlink()
            counts["chunks"] += 1
    return counts


def initialize_chunk_index(database):
    """One-time start of the chunk index: every chunk not referenced by the newest
    block is treated as last referenced at that block's height. Chunks already
    gone from the newest block can then expire only once every block up to it has."""
    with database.transaction() as cursor:
        lock(cursor, "observation-stream")
        cursor.execute("SELECT b.height,a.manifest FROM observed_blocks b JOIN capture_attempts a ON a.id=b.attempt_id ORDER BY b.height DESC LIMIT 1")
        newest = cursor.fetchone()
        if not newest:
            return {"indexed": 0}
        live = manifest_references(cursor, newest["manifest"])
        cursor.execute("SELECT count(*) AS n FROM chunk_last_ref")
        if cursor.fetchone()["n"]:
            raise ProtocolDataError("chunk index already initialized")
        cursor.execute("""INSERT INTO chunk_last_ref(digest,height) SELECT digest,%s FROM observation_chunks
            WHERE NOT (digest=ANY(%s))""", (newest["height"], sorted(live)))
        return {"indexed": cursor.rowcount, "newest_height": newest["height"], "live": len(live)}


def _session(cursor):
    lock(cursor, "evidence-retention")
    cursor.execute("SET LOCAL pool_v2.retention TO 'on'")


def run(database, *, proof, policy=POLICY, now=None, spools=(), dry_run=False, batch=200, max_blocks=5000):
    now = now or datetime.now(timezone.utc)
    covered_until = parse_time(proof["covered_until"])
    run_id, notes, counts = uuid.uuid4(), [], {}
    with database.transaction() as cursor:
        _session(cursor)
        current, target, head = plan(database, cursor, policy, covered_until, now, notes)
        if not dry_run and target != {key: current[key] for key in target}:
            cursor.execute("""UPDATE retention_floors SET block_height=%s,reporting_round=%s,capture_cutoff=%s,
                updated_at=clock_timestamp() WHERE name='evidence'""",
                (target["block_height"], target["reporting_round"], target["capture_cutoff"]))
    floors = {"block_height": target["block_height"], "reporting_round": target["reporting_round"],
              "capture_cutoff": target["capture_cutoff"].isoformat(), "previous": {
                  "block_height": current["block_height"], "reporting_round": current["reporting_round"],
                  "capture_cutoff": current["capture_cutoff"].isoformat()}}
    if dry_run:
        return {"dry_run": True, "floors": floors, "head": head, "notes": notes}

    expired_blocks = 0
    while expired_blocks < max_blocks:
        with database.transaction() as cursor:
            _session(cursor)
            cursor.execute("""SELECT id,height,manifest FROM capture_attempts WHERE NOT (manifest ? 'expired')
                AND ((height IS NOT NULL AND height<=%s) OR (height IS NULL AND created_at<=%s))
                ORDER BY height NULLS LAST, created_at LIMIT %s""",
                (target["block_height"], target["capture_cutoff"], min(batch, max_blocks - expired_blocks)))
            rows = cursor.fetchall()
            if not rows:
                break
            for row in rows:
                manifest = row["manifest"]
                tombstone = {"expired": {"run": str(run_id), "at": now.isoformat(), "policy": policy["version"],
                    "format": "paged" if "manifest_ref" in manifest else "inline",
                    "manifest_sha256": hashlib.sha256(canonical_json(manifest)).hexdigest(),
                    "manifest_ref": manifest.get("manifest_ref")}}
                cursor.execute("UPDATE capture_attempts SET manifest=%s WHERE id=%s", (Json(tombstone), row["id"]))
            expired_blocks += len(rows)
    counts["expired_capture_manifests"] = expired_blocks
    if expired_blocks >= max_blocks:
        notes.append(f"stopped after {max_blocks} capture manifests; run again to continue")

    removed = []
    while True:
        with database.transaction() as cursor:
            _session(cursor)
            cursor.execute("SELECT digest FROM chunk_last_ref WHERE height<=%s ORDER BY digest LIMIT 5000", (target["block_height"],))
            digests = [row["digest"] for row in cursor.fetchall()]
            if not digests:
                break
            cursor.execute("DELETE FROM observation_chunks WHERE digest=ANY(%s)", (digests,))
            cursor.execute("DELETE FROM chunk_last_ref WHERE digest=ANY(%s)", (digests,))
            removed.extend(digests)
    counts["removed_chunks"] = len(removed)

    tombstone = Json({"expired": {"run": str(run_id), "at": now.isoformat(), "policy": policy["version"]}})

    def expire(table, payload, where, params, keep_sql, keep_params=(), metadata=False):
        """Blank payloads in bounded batches; each batch is its own short transaction.

        Report captures also shed their provenance metadata, replaced by a
        tombstone; rows expired before that rule get a second pass.
        """
        with database.transaction() as cursor:
            cursor.execute(keep_sql, keep_params)
            keep = sorted({str(row["id"]) for row in cursor.fetchall()})
        total, collapsed = 0, 0
        while True:
            with database.transaction() as cursor:
                _session(cursor)
                cursor.execute(f"SELECT id FROM {table} WHERE expired_at IS NULL AND {where} AND NOT (id::text=ANY(%s)) "
                               f"ORDER BY created_at LIMIT %s", (*params, keep, batch))
                ids = [row["id"] for row in cursor.fetchall()]
                if not ids:
                    break
                if metadata:
                    cursor.execute(f"UPDATE {table} SET {payload}=''::bytea,expired_at=%s,metadata=%s WHERE id=ANY(%s)", (now, tombstone, ids))
                else:
                    cursor.execute(f"UPDATE {table} SET {payload}=''::bytea,expired_at=%s WHERE id=ANY(%s)", (now, ids))
                total += cursor.rowcount
        while metadata:
            with database.transaction() as cursor:
                _session(cursor)
                cursor.execute(f"SELECT id FROM {table} WHERE expired_at IS NOT NULL AND NOT (metadata ? 'expired') "
                               f"ORDER BY created_at LIMIT %s", (batch,))
                ids = [row["id"] for row in cursor.fetchall()]
                if not ids:
                    break
                cursor.execute(f"UPDATE {table} SET metadata=%s WHERE id=ANY(%s)", (tombstone, ids))
                collapsed += cursor.rowcount
        if metadata:
            counts["collapsed_" + table + "_metadata"] = collapsed
        return total

    counts["expired_report_captures"] = expire(
        "report_captures", "compressed_payload", "reporting_round<=%s AND created_at<=%s",
        (target["reporting_round"], covered_until),
        """SELECT capture_id AS id FROM confirmed_reports UNION SELECT capture_id FROM confirmed_arbitrations
           UNION SELECT unnest(capture_ids) FROM round_report_seals
           UNION (SELECT DISTINCT ON (reporting_round) id FROM report_captures ORDER BY reporting_round,created_at DESC)""",
        metadata=True)
    counts["expired_report_index_captures"] = expire(
        "report_index_captures", "compressed_payload", "reporting_round<=%s AND created_at<=%s",
        (target["reporting_round"], covered_until),
        """SELECT capture_id AS id FROM benchmark_reporting_rounds UNION SELECT unnest(capture_ids) FROM round_report_seals
           UNION (SELECT DISTINCT ON (reporting_round,player_id,challenge_id) id FROM report_index_captures
           ORDER BY reporting_round,player_id,challenge_id,created_at DESC)""",
        metadata=True)
    capture_cutoff = min(target["capture_cutoff"], covered_until)
    counts["expired_funding_captures"] = expire(
        "funding_captures", "payload_gzip", "created_at<=%s", (capture_cutoff,),
        """SELECT policy_capture AS id FROM protocol_topups UNION SELECT capture_id FROM protocol_topup_facts
           UNION SELECT capture_id FROM funding_alerts UNION SELECT capture_id FROM protocol_topup_credits
           UNION SELECT capture_id FROM protocol_opening_credits UNION SELECT capture_id FROM mainnet_protocol_opening_credits
           UNION (SELECT DISTINCT ON (player_id) id FROM funding_captures WHERE complete ORDER BY player_id,created_at DESC)""")
    counts["expired_chain_captures"] = expire(
        "chain_captures", "payload_gzip", "created_at<=%s", (capture_cutoff,),
        """SELECT capture_id AS id FROM chain_alerts UNION SELECT capture_id FROM custody_opening_baselines
           UNION (SELECT id FROM chain_captures ORDER BY created_at DESC LIMIT 1)""")

    for spool in spools:
        counts["spool:" + str(spool)] = prune_spool(spool, block_floor=target["block_height"],
            capture_cutoff=target["capture_cutoff"], covered_until=covered_until, removed_digests=removed)

    record = {"id": str(run_id), "policy": policy, "backup_proof": proof, "floors": floors, "counts": counts,
              "notes": notes, "head": head, "at": now.isoformat()}
    with database.transaction() as cursor:
        cursor.execute("INSERT INTO retention_runs(id,policy,backup_proof,floors,counts,notes) VALUES (%s,%s,%s,%s,%s,%s)",
                       (run_id, Json(policy), Json(proof), Json(floors), Json(counts), Json(notes)))
    return record
