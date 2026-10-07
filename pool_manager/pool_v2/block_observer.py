"""Durable, deduplicated block history and independent collection progress."""

from collections import defaultdict
import gzip
import hashlib
import json
import uuid

from psycopg2.extras import Json, execute_values

from .database import lock
from .ledger import fingerprint
from .observation import canonical_json
from .protocol import ProtocolDataError, validate_snapshot


def _pack(observation):
    chunks = {}

    def encode(value):
        if isinstance(value, dict):
            return {"dict": [[key, encode(item)] for key, item in sorted(value.items())]}
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            digests = []
            for item in value:
                raw = canonical_json(item)
                digest = hashlib.sha256(raw).hexdigest()
                chunks[digest] = gzip.compress(raw, mtime=0)
                digests.append(digest)
            return {"records": digests}
        return {"value": value}

    return encode(observation), chunks


def _unpack(cursor, manifest):
    wanted = set()

    def collect(value):
        if "records" in value:
            wanted.update(value["records"])
        elif "dict" in value:
            for _, child in value["dict"]:
                collect(child)
    collect(manifest)
    cursor.execute("SELECT digest,compressed FROM observation_chunks WHERE digest=ANY(%s)", (sorted(wanted),))
    chunks = {}
    for row in cursor.fetchall():
        raw = gzip.decompress(bytes(row["compressed"]))
        if hashlib.sha256(raw).hexdigest() != row["digest"]:
            raise ProtocolDataError("stored observation chunk failed its checksum")
        chunks[row["digest"]] = json.loads(raw)
    if chunks.keys() != wanted:
        raise ProtocolDataError("stored observation is missing raw evidence")

    def decode(value):
        if "records" in value:
            return [chunks[key] for key in value["records"]]
        if "dict" in value:
            return {key: decode(child) for key, child in value["dict"]}
        return value["value"]
    return decode(manifest)


def _semantic(snapshot, observation):
    scores = defaultdict(list)
    for bundle in snapshot.bundles:
        scores[bundle.benchmark_id].append(bundle.quality)
    data={"block_id": snapshot.block_id, "previous": snapshot.previous_block_id,
        "height": snapshot.height, "round": snapshot.round, "timestamp": snapshot.timestamp,
        "config": observation["start"]["block"]["config"], "scores": scores,
        "precommits": snapshot.precommits, "algorithms": snapshot.algorithms, "challenges": snapshot.challenges,
        "qualifiers": sorted((*key, value) for key, value in snapshot.qualifiers.items())}
    if observation.get("pool_player_id"):
        player=observation["pool_player_id"]
        data["pool_player_id"]=player
        data["pool_feed"]=observation["players"].get(player,observation.get("pool_pending"))
    return fingerprint(data)


class BlockStore:
    def __init__(self, database):
        self.database = database

    def initialize(self, launch_height):
        if type(launch_height) is not int or launch_height < 0:
            raise ValueError("explicit nonnegative collection start height required")
        with self.database.transaction() as cursor:
            lock(cursor, "observation-stream")
            cursor.execute("SELECT launch_height FROM observation_stream WHERE name='tig'")
            row = cursor.fetchone()
            if row:
                if row["launch_height"] != launch_height:
                    raise ProtocolDataError("collection start cannot change after initialization")
                return
            cursor.execute("""INSERT INTO observation_stream VALUES ('tig',%s,%s,%s)""",
                           (launch_height, launch_height-1, launch_height-1))

    def record(self, observation, *, collector, metadata=None, error=None):
        """Store available evidence even when it cannot establish a complete block."""
        if not collector:
            raise ValueError("collector identity is required")
        snapshot = None
        try:
            snapshot = validate_snapshot(observation)
            blocks_per_round = observation["start"]["block"]["config"]["rounds"]["blocks_per_round"]
            if type(blocks_per_round) is not int or blocks_per_round <= 0:
                raise ProtocolDataError("round length is unavailable")
            if snapshot.height // blocks_per_round + 1 != snapshot.round:
                raise ProtocolDataError("block/round boundary mapping differs from the pinned adapter")
        except (ProtocolDataError, KeyError, TypeError) as exc:
            error = error or str(exc)
        block = observation.get("start", {}).get("block", {})
        height, block_id = block.get("details", {}).get("height"), block.get("id")
        if type(height) is not int or height < 0:
            height = None
        if not isinstance(block_id, str):
            block_id = None
        manifest, chunks = _pack(observation)
        identity = uuid.uuid4()
        with self.database.transaction() as cursor:
            lock(cursor, "observation-stream")
            cursor.execute("SELECT * FROM observation_stream WHERE name='tig' FOR UPDATE")
            stream = cursor.fetchone()
            if not stream:
                raise ProtocolDataError("initialize collection start before recording blocks")
            cursor.execute("SELECT digest FROM observation_chunks WHERE digest=ANY(%s)", (list(chunks),))
            present = {row["digest"] for row in cursor.fetchall()}
            missing = [(key, data) for key, data in chunks.items() if key not in present]
            if missing:
                execute_values(cursor, "INSERT INTO observation_chunks(digest,compressed) VALUES %s ON CONFLICT DO NOTHING", missing, page_size=300)
            if snapshot and not error:
                semantic = _semantic(snapshot, observation)
                cursor.execute("SELECT id,semantic_digest FROM observed_blocks WHERE height=%s OR id=%s", (height, block_id))
                previous = cursor.fetchall()
                if any(row["id"] != block_id or row["semantic_digest"] != semantic for row in previous):
                    error = "conflicting block identity or accounting evidence at a captured height"
                    cursor.execute("INSERT INTO observation_alerts(kind,height,details) VALUES ('conflicting-block',%s,%s)",
                                   (height, Json({"block_id": block_id, "collector": collector})))
                # Check both neighbors, including a later block recovered first.
                cursor.execute("SELECT id,height,previous_id FROM observed_blocks WHERE height IN (%s,%s)", (height-1, height+1))
                for neighbor in cursor.fetchall():
                    if ((neighbor["height"] == height-1 and neighbor["id"] != snapshot.previous_block_id)
                            or (neighbor["height"] == height+1 and neighbor["previous_id"] != block_id)):
                        error = "captured block does not link to its stored neighbor"
                        cursor.execute("INSERT INTO observation_alerts(kind,height,details) VALUES ('conflicting-block',%s,%s)",
                                       (height, Json({"block_id": block_id, "neighbor": neighbor["id"]})))
            cursor.execute("""INSERT INTO capture_attempts(id,collector,block_id,height,manifest,metadata,error)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""", (identity, collector, block_id, height,
                Json(manifest), Json(metadata or {}), error))
            if height is not None and height > stream["latest_seen_height"]:
                cursor.execute("UPDATE observation_stream SET latest_seen_height=%s WHERE name='tig'", (height,))
            cursor.execute("SELECT min(height) AS height FROM observation_alerts WHERE kind='conflicting-block' AND height >= %s",
                           (stream["launch_height"],))
            conflict_height = cursor.fetchone()["height"]
            if conflict_height is not None and conflict_height <= stream["contiguous_height"]:
                stream["contiguous_height"] = conflict_height - 1
                cursor.execute("UPDATE observation_stream SET contiguous_height=%s WHERE name='tig'", (conflict_height-1,))
            if snapshot and not error:
                cursor.execute("""INSERT INTO observed_blocks
                    (id,height,previous_id,round,timestamp,blocks_per_round,semantic_digest,attempt_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (block_id, height, snapshot.previous_block_id, snapshot.round, snapshot.timestamp,
                     blocks_per_round, semantic, identity))
                self._advance(cursor, stream["contiguous_height"])
            else:
                cursor.execute("INSERT INTO observation_alerts(kind,height,details) VALUES ('incomplete-capture',%s,%s)",
                               (height, Json({"attempt_id": str(identity), "error": error})))
        return {"attempt_id": str(identity), "complete": snapshot is not None and not error,
                "block_id": block_id, "height": height, "error": error}

    def _advance(self, cursor, contiguous):
        cursor.execute("SELECT id FROM observed_blocks WHERE height=%s", (contiguous,))
        row = cursor.fetchone()
        previous_id = row["id"] if row else None
        cursor.execute("""SELECT id,height,previous_id FROM observed_blocks b WHERE height>%s
            AND NOT EXISTS (SELECT 1 FROM observation_alerts a WHERE a.kind='conflicting-block' AND a.height=b.height)
            ORDER BY height""", (contiguous,))
        for row in cursor.fetchall():
            if row["height"] != contiguous + 1 or (previous_id and row["previous_id"] != previous_id):
                break
            contiguous, previous_id = row["height"], row["id"]
        cursor.execute("UPDATE observation_stream SET contiguous_height=%s WHERE name='tig'", (contiguous,))

    def read(self, block_id):
        with self.database.transaction() as cursor:
            cursor.execute("""SELECT a.manifest,b.semantic_digest,b.height FROM observed_blocks b
                JOIN capture_attempts a ON a.id=b.attempt_id WHERE b.id=%s""", (block_id,))
            row = cursor.fetchone()
            if not row:
                raise ProtocolDataError("complete block is not available in the pool archive")
            cursor.execute("SELECT 1 FROM observation_alerts WHERE kind='conflicting-block' AND height=%s LIMIT 1", (row["height"],))
            if cursor.fetchone():
                raise ProtocolDataError("conflicting observation requires investigation before use")
            observation = _unpack(cursor, row["manifest"])
        snapshot = validate_snapshot(observation)
        if _semantic(snapshot, observation) != row["semantic_digest"]:
            raise ProtocolDataError("stored block replay differs from its validated evidence")
        return observation, snapshot

    def status(self):
        with self.database.transaction() as cursor:
            cursor.execute("SELECT * FROM observation_stream WHERE name='tig'")
            result = dict(cursor.fetchone() or {})
            if not result:
                return {"initialized": False}
            cursor.execute("""SELECT expected.height FROM generate_series(%s,%s) AS expected(height)
                LEFT JOIN observed_blocks b ON b.height=expected.height
                WHERE b.id IS NULL ORDER BY expected.height LIMIT 100""",
                (result["contiguous_height"]+1, result["latest_seen_height"]))
            missing = [row["height"] for row in cursor.fetchall()]
            cursor.execute("""SELECT w.height FROM observation_gap_waivers w
                LEFT JOIN observed_blocks b ON b.height=w.height
                WHERE b.id IS NULL ORDER BY w.height""")
            waived = {row["height"] for row in cursor.fetchall()}
            result["missing_heights"] = missing
            result["waived_missing_heights"] = [height for height in missing if height in waived]
            result["unresolved_missing_heights"] = [height for height in missing if height not in waived]
            cursor.execute("SELECT DISTINCT height FROM observation_alerts WHERE kind='conflicting-block' ORDER BY height LIMIT 100")
            result["conflicting_heights"] = [row["height"] for row in cursor.fetchall()]
            cursor.execute("SELECT max(timestamp) AS latest_timestamp FROM observed_blocks")
            result["latest_timestamp"] = cursor.fetchone()["latest_timestamp"]
            result["initialized"] = True
            return result

    def waive_prelaunch_gap(self, height, *, actor, reason, evidence):
        """Record the single prelaunch missing-height exception without fabricating a block.

        This is only allowed before any pool reservation exists. The settlement
        rule treats this one height as having zero pool credit; the raw gap
        remains visible in observation status and the immutable waiver record.
        """
        if type(height) is not int or height < 0:
            raise ValueError("a nonnegative missing block height is required")
        if not isinstance(actor, str) or not actor.strip() or not isinstance(reason, str) or not reason.strip():
            raise ValueError("gap waiver requires an actor and reason")
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError("gap waiver requires nonempty evidence")
        policy = "one-time-prelaunch-zero-pool-credit-v1"
        with self.database.transaction() as cursor:
            # Match the lock order in reserve_next: observation stream, then
            # protocol-budget. This prevents a new reservation racing the waiver.
            lock(cursor, "observation-stream")
            lock(cursor, "operator:protocol-budget")
            cursor.execute("SELECT * FROM observation_gap_waivers WHERE height=%s OR policy=%s", (height, policy))
            previous = cursor.fetchone()
            if previous:
                if (previous["height"], previous["actor"], previous["reason"], previous["evidence"]) == (
                        height, actor.strip(), reason.strip(), evidence):
                    return dict(previous)
                raise ProtocolDataError("the one-time prelaunch gap waiver is already recorded")
            cursor.execute("SELECT value FROM runtime_controls WHERE name='new_work_paused'")
            paused = cursor.fetchone()
            if not paused or not paused["value"]:
                raise ProtocolDataError("prelaunch gap waiver requires new work to remain paused")
            cursor.execute("SELECT 1 FROM reservations LIMIT 1")
            if cursor.fetchone():
                raise ProtocolDataError("prelaunch gap waiver is unavailable after pool reservations exist")
            cursor.execute("SELECT * FROM observation_stream WHERE name='tig' FOR UPDATE")
            stream = cursor.fetchone()
            if not stream or height < stream["launch_height"] or height >= stream["latest_seen_height"]:
                raise ProtocolDataError("waiver height is outside the observed stream")
            cursor.execute("SELECT * FROM observed_blocks WHERE height=ANY(%s)", ([height-1, height, height+1],))
            neighbors = {row["height"]: row for row in cursor.fetchall()}
            if height in neighbors or height-1 not in neighbors or height+1 not in neighbors:
                raise ProtocolDataError("waiver requires one missing height between two captured blocks")
            previous_block, next_block = neighbors[height-1], neighbors[height+1]
            length = previous_block["blocks_per_round"]
            round_number = height // length + 1 if length else 0
            if (length <= 0 or next_block["blocks_per_round"] != length
                    or previous_block["round"] != round_number or next_block["round"] != round_number):
                raise ProtocolDataError("waiver height must be bracketed within one verified round")
            cursor.execute("SELECT height FROM observed_blocks WHERE id=%s", (next_block["previous_id"],))
            if cursor.fetchone():
                raise ProtocolDataError("the successor points to an already recorded block at another height")
            cursor.execute("""SELECT 1 FROM observation_alerts WHERE kind='conflicting-block'
                AND height=ANY(%s) LIMIT 1""", ([height-1, height, height+1],))
            if cursor.fetchone():
                raise ProtocolDataError("conflicting neighbor evidence prevents a gap waiver")
            cursor.execute("""INSERT INTO observation_gap_waivers
                (height,round_number,assumed_block_id,previous_observed_block_id,
                 next_observed_block_id,policy,actor,reason,evidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (height, round_number, next_block["previous_id"], previous_block["id"],
                 next_block["id"], policy, actor.strip(), reason.strip(), Json(evidence)))
            return dict(cursor.fetchone())

    def round_coverage(self, round_number, *, require_credits=True):
        with self.database.transaction() as cursor:
            cursor.execute("SELECT DISTINCT blocks_per_round FROM observed_blocks WHERE round=%s", (round_number,))
            lengths = [row["blocks_per_round"] for row in cursor.fetchall()]
            if len(lengths) != 1:
                return False
            length = lengths[0]
            first, last = (round_number-1)*length, round_number*length-1
            cursor.execute("""SELECT
                    count(*) FILTER (WHERE b.id IS NOT NULL OR w.height IS NOT NULL) AS captured,
                    count(*) FILTER (WHERE c.block_id IS NOT NULL OR (b.id IS NULL AND w.height IS NOT NULL)) AS credited
                FROM generate_series(%s,%s) AS expected(height)
                LEFT JOIN observed_blocks b ON b.height=expected.height AND b.round=%s
                LEFT JOIN credited_blocks c ON c.block_id=b.id
                LEFT JOIN observation_gap_waivers w ON w.height=expected.height AND w.round_number=%s""",
                (first, last, round_number, round_number))
            counts = cursor.fetchone()
            cursor.execute("SELECT 1 FROM observation_alerts WHERE kind='conflicting-block' AND height BETWEEN %s AND %s LIMIT 1", (first, last))
            return not cursor.fetchone() and counts["captured"] == length and (not require_credits or counts["credited"] == length)
