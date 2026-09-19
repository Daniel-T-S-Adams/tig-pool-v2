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


def fetch_benchmark_audits(benchmark_id: str, nonce: int | None = None, with_leaves: bool = False) -> dict:
    """Everything the pool knows about one benchmark, for answering a report.

    Joins the job (settings, batch_size), who computed each batch
    (root_batch), the qualities they posted (batch_data) and the audit
    verdicts. ``nonce`` narrows to the batch containing it and adds the
    posted quality for that exact nonce. ``with_leaves`` attaches the kept
    {nonce}.json payloads so they can be re-verified by hand.
    """
    benchmark_id = str(benchmark_id).strip()
    job = db.fetch_one(
        """
        SELECT benchmark_id, challenge, algorithm, settings, rand_hash, num_nonces, batch_size, start_time
        FROM job WHERE benchmark_id = %s
        """,
        (benchmark_id,),
    )
    batch_size = int(job["batch_size"]) if job and job.get("batch_size") else None
    audits = db.fetch_all(
        """
        SELECT a.id, a.batch_idx, a.slave, a.challenge, a.algorithm, a.settings, a.rand_hash,
               a.status, a.requested_nonces, a.expected_qualities, a.result, a.error,
               a.requested_at, a.leaves_received_at, a.verified_at,
               (SELECT COUNT(*) FROM batch_audit_leaf l WHERE l.audit_id = a.id) AS leaves_kept
        FROM batch_audit a
        WHERE a.benchmark_id = %s
        ORDER BY a.batch_idx
        """,
        (benchmark_id,),
    )
    if audits and not job:
        # Job pruned by retention; the audit rows still carry settings.
        a0 = audits[0]
        job = {
            "benchmark_id": benchmark_id,
            "challenge": a0["challenge"],
            "algorithm": a0["algorithm"],
            "settings": a0["settings"],
            "rand_hash": a0["rand_hash"],
            "num_nonces": None,
            "batch_size": None,
            "start_time": None,
            "note": "job row pruned; from batch_audit",
        }
    batches = {}
    for r in db.fetch_all(
        """
        SELECT rb.batch_idx, rb.slave, rb.start_time, rb.end_time, rb.ready,
               bd.merkle_root, bd.solution_quality, bd.average_quality
        FROM root_batch rb
        LEFT JOIN batch_data bd ON bd.benchmark_id = rb.benchmark_id AND bd.batch_idx = rb.batch_idx
        WHERE rb.benchmark_id = %s
        ORDER BY rb.batch_idx
        """,
        (benchmark_id,),
    ):
        batches[int(r["batch_idx"])] = dict(r)
    for a in audits:
        b = batches.setdefault(int(a["batch_idx"]), {"batch_idx": int(a["batch_idx"]), "slave": a["slave"]})
        b["audit"] = dict(a)
        if with_leaves:
            b["audit"]["leaves"] = [
                dict(l)
                for l in db.fetch_all(
                    "SELECT nonce, leaf FROM batch_audit_leaf WHERE audit_id = %s ORDER BY nonce",
                    (int(a["id"]),),
                )
            ]

    focus = None
    if nonce is not None and batch_size:
        nonce = int(nonce)
        bidx = nonce // batch_size
        b = batches.get(bidx)
        posted = None
        if b and isinstance(b.get("solution_quality"), list):
            off = nonce - bidx * batch_size
            if 0 <= off < len(b["solution_quality"]):
                posted = b["solution_quality"][off]
        audit = (b or {}).get("audit") or {}
        sampled = nonce in [int(n) for n in (audit.get("requested_nonces") or [])]
        verdict = ((audit.get("result") or {}).get(str(nonce))) if sampled else None
        focus = {
            "nonce": nonce,
            "batch_idx": bidx,
            "slave": (b or {}).get("slave"),
            "posted_quality": posted,
            "audited": sampled,
            "audit_id": audit.get("id"),
            "audit_status": audit.get("status"),
            "verifier": verdict,
            "leaf_kept": bool(sampled and audit.get("leaves_kept")),
        }
        batches = {bidx: b} if b else {}

    return {
        "benchmark_id": benchmark_id,
        "job": dict(job) if job else None,
        "batches": [batches[k] for k in sorted(batches)],
        "focus": focus,
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
