"""Spot-check audit of slave-submitted qualities.

Why this exists
---------------
A root submit carries ``merkle_root`` and a ``solution_quality`` list. The
merkle root commits to the *solutions*; it does not commit to the quality
numbers. A hostile member could post real solutions with inflated qualities
and the master would forward them to TIG unchanged. TIG only re-checks a
handful of nonces per benchmark, and when its re-run disagrees the clawback
lands on the pool, not the member.

Protocol (slave push, two phases)
---------------------------------
1. Slave POSTs ``/submit-batch-root`` exactly as before.
2. Master picks the audit nonces *after* it has seen the quality list, so the
   slave cannot know in advance which nonces will be checked. The 200 ack
   carries ``audit_nonces``.
3. Slave POSTs ``/submit-batch-audit/{batch_id}`` with the original
   ``{nonce}.json`` leaves for those nonces (a few KB each).
4. The ``auditor`` service (separate container, has docker.sock) runs
   ``tig-verifier`` only — never ``tig-runtime`` — on each leaf and compares
   the result to the quality the slave posted. Mismatch => strike.

Fetch (kind='fetch')
--------------------
Slaves >= 0.1.23 archive every leaf of every batch for AUDIT_TTL. When TIG
reports a nonce we did not sample, the operator creates a ``fetch`` row; the
master advertises it in the ``X-Innopool-Audit-Fetch`` header of that slave's
next get-batches reply; the slave pushes the leaf plus its merkle branch; the
master checks the branch against the ``merkle_root`` the slave committed at
root-submit time (``merkle_ok``) before the auditor re-scores it.

Everything here is pure helpers + SQL so ``slave_manager`` stays small and
the logic is unit-testable without a DB.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(os.path.splitext(os.path.basename(__file__))[0])

LEAF_REQUIRED_KEYS = ("nonce", "runtime_signature", "fuel_consumed", "solution", "cpu_arch")
# Optional per-leaf merkle branch (hex, 66 chars per stem) from slaves >= 0.1.23.
LEAF_BRANCH_KEY = "branch"
AUDIT_FETCH_HEADER = "X-Innopool-Audit-Fetch"
# Fetch rows advertised per get-batches reply; header stays small.
AUDIT_FETCH_MAX_PER_POLL = 5

# Master-side defaults. Operator can override any key under CONFIG["audit"]
# via /update-config without a restart.
DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    # Random leaves requested per root batch (plus the max-quality nonce).
    "leaves_per_batch": 2,
    # Always request the nonce with the highest posted quality. That is the
    # one an inflater cares about most.
    "include_max_quality": True,
    # Per-leaf JSON size cap. Real leaves are ~1-10 KB; energy can be larger.
    "max_leaf_bytes": 262144,
    # How long a slave has to deliver leaves after the root ack.
    "request_ttl_ms": 30 * 60 * 1000,
}


def audit_settings(config: Optional[dict]) -> Dict[str, Any]:
    out = dict(DEFAULTS)
    raw = (config or {}).get("audit") if isinstance(config, dict) else None
    if isinstance(raw, dict):
        for key in DEFAULTS:
            if key in raw and raw[key] is not None:
                out[key] = raw[key]
    try:
        out["leaves_per_batch"] = max(0, int(out["leaves_per_batch"]))
    except Exception:
        out["leaves_per_batch"] = DEFAULTS["leaves_per_batch"]
    try:
        out["max_leaf_bytes"] = max(1024, int(out["max_leaf_bytes"]))
    except Exception:
        out["max_leaf_bytes"] = DEFAULTS["max_leaf_bytes"]
    try:
        out["request_ttl_ms"] = max(60_000, int(out["request_ttl_ms"]))
    except Exception:
        out["request_ttl_ms"] = DEFAULTS["request_ttl_ms"]
    out["enabled"] = bool(out["enabled"])
    out["include_max_quality"] = bool(out["include_max_quality"])
    return out


def choose_audit_nonces(
    start_nonce: int,
    qualities: Sequence[int],
    *,
    leaves_per_batch: int,
    include_max_quality: bool = True,
    rng: Optional[random.Random] = None,
) -> List[int]:
    """Pick which nonces of a just-submitted root to audit.

    Chosen *after* the slave has committed to ``qualities`` so it cannot
    steer the sample. Returns absolute nonces, sorted.
    """
    n = len(qualities)
    if n <= 0:
        return []
    rng = rng or random.SystemRandom()
    picked: set = set()
    if include_max_quality:
        best_idx = max(range(n), key=lambda i: qualities[i])
        picked.add(best_idx)
    want = min(n, max(0, int(leaves_per_batch)) + len(picked))
    remaining = [i for i in range(n) if i not in picked]
    if want > len(picked) and remaining:
        picked.update(rng.sample(remaining, min(len(remaining), want - len(picked))))
    return sorted(int(start_nonce) + i for i in picked)


def validate_audit_leaves(
    payload: Any,
    *,
    requested_nonces: Iterable[int],
    max_leaf_bytes: int,
) -> List[dict]:
    """Validate a ``/submit-batch-audit`` body.

    Returns the accepted leaves (one per requested nonce that was supplied).
    Raises ``ValueError`` on anything malformed. Missing nonces are allowed
    here (partial delivery is recorded as such); duplicates are not.
    """
    if not isinstance(payload, dict):
        raise ValueError("body must be an object")
    leaves = payload.get("leaves")
    if not isinstance(leaves, list):
        raise ValueError("leaves must be a list")
    requested = {int(n) for n in requested_nonces}
    if len(leaves) > len(requested):
        raise ValueError(f"{len(leaves)} leaves for {len(requested)} requested nonces")
    seen: set = set()
    accepted: List[dict] = []
    for leaf in leaves:
        if not isinstance(leaf, dict):
            raise ValueError("leaf must be an object")
        for key in LEAF_REQUIRED_KEYS:
            if key not in leaf:
                raise ValueError(f"leaf missing {key}")
        try:
            nonce = int(leaf["nonce"])
        except Exception as exc:
            raise ValueError("leaf nonce must be an int") from exc
        if nonce not in requested:
            raise ValueError(f"nonce {nonce} was not requested")
        if nonce in seen:
            raise ValueError(f"duplicate nonce {nonce}")
        if not isinstance(leaf["runtime_signature"], int) or not isinstance(leaf["fuel_consumed"], int):
            raise ValueError("runtime_signature / fuel_consumed must be ints")
        if not isinstance(leaf["solution"], str) or not isinstance(leaf["cpu_arch"], str):
            raise ValueError("solution / cpu_arch must be strings")
        # The slave's own quality field must not leak into the stored leaf;
        # the auditor recomputes it and compares to what was posted.
        clean = {k: leaf[k] for k in LEAF_REQUIRED_KEYS}
        clean["nonce"] = nonce
        size = len(json.dumps(clean, separators=(",", ":")))
        if size > int(max_leaf_bytes):
            raise ValueError(f"leaf {nonce} is {size} bytes > {max_leaf_bytes}")
        branch = leaf.get(LEAF_BRANCH_KEY)
        if branch is not None:
            if not isinstance(branch, str) or len(branch) % 66 != 0 or len(branch) > 66 * 64:
                raise ValueError(f"leaf {nonce} has a malformed merkle branch")
            try:
                bytes.fromhex(branch)
            except ValueError as exc:
                raise ValueError(f"leaf {nonce} merkle branch is not hex") from exc
            clean[LEAF_BRANCH_KEY] = branch
        seen.add(nonce)
        accepted.append(clean)
    return accepted


def verify_leaf_branch(leaf: dict, *, merkle_root: str, start_nonce: int) -> Optional[bool]:
    """Check a delivered leaf against the root the slave committed at submit.

    Returns True/False, or None when the leaf carries no branch (slave < 0.1.23)
    or no root is known. Import is local so the pure helpers stay importable
    in tests without the tig ``common`` package.
    """
    branch = leaf.get(LEAF_BRANCH_KEY)
    if not branch or not merkle_root:
        return None
    try:
        from common.merkle_tree import MerkleBranch, MerkleHash
        from common.structs import OutputData

        data = {k: leaf[k] for k in LEAF_REQUIRED_KEYS}
        leaf_hash = OutputData.from_dict(data).to_merkle_hash()
        root = MerkleBranch.from_str(branch).calc_merkle_root(leaf_hash, int(leaf["nonce"]) - int(start_nonce))
        return root == MerkleHash.from_str(merkle_root)
    except Exception as exc:
        logger.warning("merkle branch check failed for nonce %s: %s", leaf.get("nonce"), exc)
        return False


def group_nonces_by_batch(nonces: Iterable[int], *, batch_size: int) -> Dict[int, List[int]]:
    """Absolute nonces -> {batch_idx: [nonces]} for a job's batch size."""
    out: Dict[int, List[int]] = {}
    for n in sorted({int(x) for x in nonces}):
        out.setdefault(n // int(batch_size), []).append(n)
    return out


def fetch_header_value(items: Sequence[dict]) -> str:
    """Compact JSON for the get-batches header. Empty string when nothing to ask."""
    if not items:
        return ""
    return json.dumps(
        [{"audit_id": int(i["id"]), "batch_id": f"{i['benchmark_id']}_{int(i['batch_idx'])}", "nonces": [int(n) for n in i["requested_nonces"]]} for i in items],
        separators=(",", ":"),
    )


# ── schema ───────────────────────────────────────────────────────────────────

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS batch_audit (
        id BIGSERIAL PRIMARY KEY,
        benchmark_id TEXT NOT NULL,
        batch_idx INTEGER NOT NULL,
        slave TEXT NOT NULL,
        challenge TEXT NOT NULL,
        algorithm TEXT,
        settings JSONB NOT NULL,
        rand_hash TEXT NOT NULL,
        -- absolute nonces the master asked for, and the qualities the slave
        -- posted for them (index-aligned)
        requested_nonces JSONB NOT NULL,
        expected_qualities JSONB NOT NULL,
        -- requested -> pending -> (passed | failed | error | skipped) ; requested -> missing
        status TEXT NOT NULL DEFAULT 'requested',
        requested_at BIGINT NOT NULL,
        leaves_received_at BIGINT,
        verified_at BIGINT,
        attempts INTEGER NOT NULL DEFAULT 0,
        -- per-nonce verifier output: {nonce: {"expected":..,"actual":..,"ok":bool,"error":..}}
        result JSONB,
        error TEXT,
        -- 'sample' (picked at root ack) or 'fetch' (operator asked for specific nonces later)
        kind TEXT NOT NULL DEFAULT 'sample',
        -- free text for fetches: who asked / which TIG report
        requested_by TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS batch_audit_leaf (
        audit_id BIGINT NOT NULL REFERENCES batch_audit(id) ON DELETE CASCADE,
        nonce BIGINT NOT NULL,
        leaf JSONB NOT NULL,
        -- merkle branch delivered with the leaf and whether it reproduces the committed root
        branch TEXT,
        merkle_ok BOOLEAN,
        PRIMARY KEY (audit_id, nonce)
    )
    """,
    # Upgrades for DBs created before kind/fetch existed.
    "ALTER TABLE batch_audit ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'sample'",
    "ALTER TABLE batch_audit ADD COLUMN IF NOT EXISTS requested_by TEXT",
    "ALTER TABLE batch_audit DROP CONSTRAINT IF EXISTS batch_audit_benchmark_id_batch_idx_key",
    "ALTER TABLE batch_audit_leaf ADD COLUMN IF NOT EXISTS branch TEXT",
    "ALTER TABLE batch_audit_leaf ADD COLUMN IF NOT EXISTS merkle_ok BOOLEAN",
    # One sample per batch; any number of fetches.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_batch_audit_sample ON batch_audit(benchmark_id, batch_idx) WHERE kind = 'sample'",
    "CREATE INDEX IF NOT EXISTS idx_batch_audit_status ON batch_audit(status)",
    "CREATE INDEX IF NOT EXISTS idx_batch_audit_slave ON batch_audit(slave)",
    "CREATE INDEX IF NOT EXISTS idx_batch_audit_requested_at ON batch_audit(requested_at)",
    "CREATE INDEX IF NOT EXISTS idx_batch_audit_kind_status ON batch_audit(kind, status)",
    "CREATE INDEX IF NOT EXISTS idx_batch_audit_benchmark ON batch_audit(benchmark_id)",
)

_schema_ready = False


def ensure_audit_schema(db) -> None:
    """Idempotent. Safe on an existing DB that predates init.sql changes."""
    global _schema_ready
    if _schema_ready:
        return
    try:
        db.execute_many(*[(stmt, None) for stmt in SCHEMA_STATEMENTS])
        _schema_ready = True
    except Exception as exc:
        logger.warning("batch_audit schema ensure failed: %s", exc)


# ── SQL helpers used by slave_manager ────────────────────────────────────────

def record_audit_request(
    db,
    *,
    benchmark_id: str,
    batch_idx: int,
    slave: str,
    challenge: str,
    algorithm: Optional[str],
    settings: dict,
    rand_hash: str,
    nonces: List[int],
    expected_qualities: List[int],
    now_ms: Optional[int] = None,
) -> None:
    if not nonces:
        return
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    db.execute(
        """
        INSERT INTO batch_audit (
            benchmark_id, batch_idx, slave, challenge, algorithm, settings, rand_hash,
            requested_nonces, expected_qualities, status, requested_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'requested', %s)
        ON CONFLICT (benchmark_id, batch_idx) WHERE kind = 'sample' DO NOTHING
        """,
        (
            benchmark_id,
            int(batch_idx),
            slave,
            challenge,
            algorithm,
            json.dumps(settings),
            rand_hash,
            json.dumps([int(n) for n in nonces]),
            json.dumps([int(q) for q in expected_qualities]),
            now_ms,
        ),
    )


def fetch_audit_request(db, *, benchmark_id: str, batch_idx: int, audit_id: Optional[int] = None) -> Optional[dict]:
    """The row a ``/submit-batch-audit`` push is answering.

    Slaves >= 0.1.23 name the ``audit_id``; older ones do not, and for them
    the only row that can exist is the batch's sample.
    """
    if audit_id is not None:
        return db.fetch_one(
            """
            SELECT id, slave, status, kind, requested_nonces, requested_at
            FROM batch_audit
            WHERE id = %s AND benchmark_id = %s AND batch_idx = %s
            """,
            (int(audit_id), benchmark_id, int(batch_idx)),
        )
    return db.fetch_one(
        """
        SELECT id, slave, status, kind, requested_nonces, requested_at
        FROM batch_audit
        WHERE benchmark_id = %s AND batch_idx = %s AND kind = 'sample'
        """,
        (benchmark_id, int(batch_idx)),
    )


def fetch_batch_root(db, *, benchmark_id: str, batch_idx: int) -> Optional[dict]:
    """merkle_root the slave committed for this batch plus the job's batch_size."""
    return db.fetch_one(
        """
        SELECT bd.merkle_root, j.batch_size
        FROM batch_data bd
        JOIN job j ON j.benchmark_id = bd.benchmark_id
        WHERE bd.benchmark_id = %s AND bd.batch_idx = %s
        """,
        (benchmark_id, int(batch_idx)),
    )


def store_audit_leaves(
    db,
    *,
    audit_id: int,
    leaves: List[dict],
    merkle_ok: Optional[Dict[int, Optional[bool]]] = None,
    now_ms: Optional[int] = None,
) -> None:
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    merkle_ok = merkle_ok or {}
    queries = []
    for leaf in leaves:
        nonce = int(leaf["nonce"])
        branch = leaf.get(LEAF_BRANCH_KEY)
        stored = {k: leaf[k] for k in LEAF_REQUIRED_KEYS}
        queries.append(
            (
                """
                INSERT INTO batch_audit_leaf (audit_id, nonce, leaf, branch, merkle_ok)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (audit_id, nonce) DO UPDATE
                SET leaf = EXCLUDED.leaf, branch = EXCLUDED.branch, merkle_ok = EXCLUDED.merkle_ok
                """,
                (int(audit_id), nonce, json.dumps(stored, separators=(",", ":")), branch, merkle_ok.get(nonce)),
            )
        )
    queries.append(
        (
            """
            UPDATE batch_audit
            SET status = 'pending',
                leaves_received_at = %s
            WHERE id = %s
              AND status = 'requested'
            """,
            (now_ms, int(audit_id)),
        )
    )
    db.execute_many(*queries)


def mark_audit_unavailable(db, *, audit_id: int, reason: str, now_ms: Optional[int] = None) -> None:
    """Slave answered a fetch with 'I do not have it'. Record immediately as
    missing instead of waiting for the request TTL."""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    db.execute(
        """
        UPDATE batch_audit
        SET status = 'missing', verified_at = %s, error = %s
        WHERE id = %s AND status = 'requested'
        """,
        (now_ms, str(reason)[:500], int(audit_id)),
    )


def pending_fetch_requests(db, *, limit: int = 500) -> List[dict]:
    """All fetch rows still waiting for leaves; master caches these per slave."""
    return db.fetch_all(
        """
        SELECT id, benchmark_id, batch_idx, slave, requested_nonces
        FROM batch_audit
        WHERE kind = 'fetch' AND status = 'requested'
        ORDER BY requested_at
        LIMIT %s
        """,
        (int(limit),),
    ) or []
