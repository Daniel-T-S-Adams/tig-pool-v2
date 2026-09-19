"""Observe-only report over batch_audit for /admin/ops/audit and admin.py.

Nothing here changes state. Quarantine decisions live in the auditor
container; this is how the operator sees what it did and why.
"""

from __future__ import annotations

import time

from . import database as db

DEFAULT_WINDOW_MS = 7 * 24 * 3600 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


def build_audit_report(window_ms: int | None = None, failures_limit: int = 25) -> dict:
    window_ms = int(window_ms or DEFAULT_WINDOW_MS)
    since = _now_ms() - window_ms
    if not db.table_exists("batch_audit"):
        return {
            "window_ms": window_ms,
            "available": False,
            "note": "batch_audit table missing — master has not started with audit support yet",
            "totals": {},
            "per_slave": [],
            "recent_failures": [],
        }

    totals_rows = db.fetch_all(
        """
        SELECT status, COUNT(*) AS n
        FROM batch_audit
        WHERE requested_at >= %s
        GROUP BY status
        """,
        (since,),
    )
    totals = {str(r["status"]): int(r["n"]) for r in totals_rows}

    per_slave = db.fetch_all(
        """
        SELECT a.slave,
               COALESCE(m.trust_state, 'unregistered') AS trust_state,
               COALESCE(m.active, false) AS active,
               COUNT(*) AS requested,
               COUNT(*) FILTER (WHERE a.status = 'passed')  AS passed,
               COUNT(*) FILTER (WHERE a.status = 'failed')  AS failed,
               COUNT(*) FILTER (WHERE a.status = 'missing') AS missing,
               COUNT(*) FILTER (WHERE a.status = 'skipped') AS skipped,
               COUNT(*) FILTER (WHERE a.status IN ('pending', 'requested', 'error')) AS open,
               MAX(a.verified_at) AS last_verified_at
        FROM batch_audit a
        LEFT JOIN pool_members m ON m.slave_name = a.slave
        WHERE a.requested_at >= %s
        GROUP BY a.slave, m.trust_state, m.active
        ORDER BY failed DESC, missing DESC, requested DESC
        """,
        (since,),
    )

    recent_failures = db.fetch_all(
        """
        SELECT id, benchmark_id, batch_idx, slave, challenge, algorithm,
               requested_nonces, expected_qualities, result, error, requested_at, verified_at,
               (SELECT COUNT(*) FROM batch_audit_leaf l WHERE l.audit_id = a.id) AS leaves_kept
        FROM batch_audit a
        WHERE status = 'failed'
        ORDER BY verified_at DESC NULLS LAST
        LIMIT %s
        """,
        (int(failures_limit),),
    )

    backlog = db.fetch_one(
        """
        SELECT COUNT(*) FILTER (WHERE status = 'pending')   AS pending,
               COUNT(*) FILTER (WHERE status = 'requested') AS awaiting_leaves,
               COUNT(*) FILTER (WHERE status = 'error')     AS error,
               MIN(leaves_received_at) FILTER (WHERE status = 'pending') AS oldest_pending_at
        FROM batch_audit
        """
    ) or {}

    return {
        "window_ms": window_ms,
        "available": True,
        "totals": totals,
        "backlog": {k: (int(v) if v is not None else None) for k, v in dict(backlog).items()},
        "per_slave": [dict(r) for r in per_slave],
        "recent_failures": [dict(r) for r in recent_failures],
    }


def fetch_audit_detail(audit_id: int) -> dict | None:
    row = db.fetch_one("SELECT * FROM batch_audit WHERE id = %s", (int(audit_id),))
    if row is None:
        return None
    leaves = db.fetch_all(
        "SELECT nonce, leaf FROM batch_audit_leaf WHERE audit_id = %s ORDER BY nonce",
        (int(audit_id),),
    )
    out = dict(row)
    out["leaves"] = [dict(l) for l in leaves]
    return out
