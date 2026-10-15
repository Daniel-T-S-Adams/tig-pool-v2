"""Whole-network selection references, read on demand rather than every block.

A per-block pool capture carries everything member credit needs. Choosing
hyperparameters copies the best bundle per algorithm and track across the
whole network, which needs every player's feed. That read happens here, at
most once per reference age while members are waiting for work, and keeps
only the best references with their parameters.
"""

from dataclasses import asdict
from datetime import datetime, timezone
import gzip
import hashlib
import json
import uuid

from psycopg2.extras import Json

from .block_observer import _semantic
from .compact import metadata_summary
from .observation import canonical_json, capture_snapshot
from .protocol import Bundle, ProtocolDataError, _array, _integer, _object, _text, validate_snapshot
from .selection import ReferenceIndex, references

FORMAT = "network-references-v1"


def capture(client, pool_player_id, *, max_workers=4):
    """Read and validate the whole network once; keep the best bundle per algorithm and track."""
    observation = capture_snapshot(client, pool_player_id=pool_player_id, max_workers=max_workers)
    snapshot = validate_snapshot(observation)
    index = references(snapshot)
    values = [{"bundle": asdict(bundle),
               "hyperparameters": snapshot.precommits[bundle.benchmark_id]["details"]["hyperparameters"]}
              for _, bundle in sorted(index.values.items())]
    payload = {"format": FORMAT, "pool_player_id": pool_player_id,
        "block": {"id": snapshot.block_id, "height": snapshot.height, "round": snapshot.round,
                  "timestamp": snapshot.timestamp},
        "source_semantic_digest": _semantic(snapshot, observation),
        "network": {"players": len(observation["players"]), "active_benchmarks": len(snapshot.precommits),
                    "bundles": len(snapshot.bundles)},
        "references": values}
    metadata = metadata_summary({"captured_at": datetime.now(timezone.utc).isoformat(), "requests": client.records})
    validate_payload(payload)
    return payload, metadata


def validate_payload(payload):
    if _object(payload, "network references").get("format") != FORMAT:
        raise ProtocolDataError("unknown network reference format")
    block = _object(payload["block"], "reference block")
    _text(block["id"], "reference block ID")
    _integer(block["height"], "reference height")
    _integer(block["round"], "reference round", 1)
    _integer(block["timestamp"], "reference timestamp")
    digest = _text(payload["source_semantic_digest"], "reference source digest")
    if len(digest) != 64:
        raise ProtocolDataError("reference source digest must be a SHA-256")
    values, parameters = {}, {}
    for row in _array(payload["references"], "references"):
        try:
            bundle = Bundle(**_object(row["bundle"], "reference bundle"))
        except TypeError as error:
            raise ProtocolDataError("reference bundle has unexpected fields") from error
        for name in ("benchmark_id", "player_id", "challenge_id", "algorithm_id", "track_id"):
            _text(getattr(bundle, name), "reference " + name)
        _integer(bundle.active_index, "reference bundle index")
        if not -(2**31) <= _integer(bundle.quality, "reference quality", -(2**31)) < 2**31:
            raise ProtocolDataError("reference quality exceeds int32")
        key = (bundle.algorithm_id, bundle.track_id)
        if key in values:
            raise ProtocolDataError("duplicate network reference for an algorithm and track")
        if row["hyperparameters"] is not None and not isinstance(row["hyperparameters"], dict):
            raise ProtocolDataError("reference hyperparameters are invalid")
        values[key] = bundle
        parameters[key] = row["hyperparameters"]
    return block, values, parameters


def record(database, payload, metadata):
    """Store one reference capture; an identical payload for the same block is not duplicated."""
    block, _, _ = validate_payload(payload)
    raw = canonical_json(payload)
    digest = hashlib.sha256(raw).hexdigest()
    with database.transaction() as cursor:
        cursor.execute("SELECT id FROM reference_captures WHERE block_id=%s AND payload_sha256=%s", (block["id"], digest))
        previous = cursor.fetchone()
        if previous:
            return {"id": str(previous["id"]), "digest": digest, "new": False, "height": block["height"]}
        identity = uuid.uuid4()
        cursor.execute("""INSERT INTO reference_captures
            (id,block_id,height,round,block_timestamp,source_semantic_digest,payload_sha256,compressed_payload,metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (identity, block["id"], block["height"], block["round"], block["timestamp"],
             payload["source_semantic_digest"], digest, gzip.compress(raw, mtime=0), Json(metadata or {})))
    return {"id": str(identity), "digest": digest, "new": True, "height": block["height"]}


def latest(database, *, now, max_age_seconds):
    """The newest unexpired reference whose block is young enough, as a selector index."""
    _integer(now, "current timestamp")
    _integer(max_age_seconds, "maximum reference age", 1)
    with database.transaction() as cursor:
        cursor.execute("""SELECT id,block_id,height,block_timestamp,payload_sha256,compressed_payload FROM reference_captures
            WHERE expired_at IS NULL AND block_timestamp>=%s ORDER BY height DESC,created_at DESC LIMIT 1""",
            (now - max_age_seconds,))
        row = cursor.fetchone()
    if not row:
        return None
    raw = gzip.decompress(bytes(row["compressed_payload"]))
    if hashlib.sha256(raw).hexdigest() != row["payload_sha256"]:
        raise ProtocolDataError("stored network reference failed its checksum")
    block, values, parameters = validate_payload(json.loads(raw))
    if (block["id"], block["height"], block["timestamp"]) != (row["block_id"], row["height"], row["block_timestamp"]):
        raise ProtocolDataError("stored network reference differs from its recorded block")
    return ReferenceIndex(row["block_id"], values, height=row["height"], timestamp=row["block_timestamp"],
                          capture_id=str(row["id"]), digest=row["payload_sha256"], hyperparameters=parameters)
