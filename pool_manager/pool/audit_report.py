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
    kind_col = "a.kind" if db.has_columns("batch_audit", "kind") else "'sample' AS kind"
    merkle_col = (
        "(SELECT bool_and(l.merkle_ok) FROM batch_audit_leaf l WHERE l.audit_id = a.id AND l.merkle_ok IS NOT NULL) AS merkle_ok"
        if db.has_columns("batch_audit_leaf", "merkle_ok")
        else "NULL::boolean AS merkle_ok"
    )
    audits = db.fetch_all(
        f"""
        SELECT a.id, a.batch_idx, a.slave, a.challenge, a.algorithm, a.settings, a.rand_hash,
               a.status, a.requested_nonces, a.expected_qualities, a.result, a.error,
               a.requested_at, a.leaves_received_at, a.verified_at, {kind_col},
               (SELECT COUNT(*) FROM batch_audit_leaf l WHERE l.audit_id = a.id) AS leaves_kept,
               {merkle_col}
        FROM batch_audit a
        WHERE a.benchmark_id = %s
        ORDER BY a.batch_idx, a.requested_at
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
    leaf_cols = "nonce, leaf" + (", branch, merkle_ok" if db.has_columns("batch_audit_leaf", "merkle_ok") else "")
    for a in audits:
        b = batches.setdefault(int(a["batch_idx"]), {"batch_idx": int(a["batch_idx"]), "slave": a["slave"]})
        row = dict(a)
        if with_leaves:
            row["leaves"] = [
                dict(l)
                for l in db.fetch_all(
                    f"SELECT {leaf_cols} FROM batch_audit_leaf WHERE audit_id = %s ORDER BY nonce",
                    (int(a["id"]),),
                )
            ]
        # One sample per batch plus any number of fetches. ``audit`` stays the
        # sample for older callers; ``audits`` has everything.
        b.setdefault("audits", []).append(row)
        if row.get("kind", "sample") == "sample" or "audit" not in b:
            b["audit"] = row

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
        # Prefer the newest audit that actually covers this nonce.
        covering = [
            a for a in ((b or {}).get("audits") or [])
            if nonce in [int(n) for n in (a.get("requested_nonces") or [])]
        ]
        audit = covering[-1] if covering else {}
        sampled = bool(covering)
        verdict = ((audit.get("result") or {}).get(str(nonce))) if sampled else None
        focus = {
            "nonce": nonce,
            "batch_idx": bidx,
            "slave": (b or {}).get("slave"),
            "posted_quality": posted,
            "audited": sampled,
            "audit_id": audit.get("id"),
            "audit_kind": audit.get("kind"),
            "audit_status": audit.get("status"),
            "merkle_ok": audit.get("merkle_ok"),
            "verifier": verdict,
            "leaf_kept": bool(sampled and audit.get("leaves_kept")),
            "root_available": bool((b or {}).get("merkle_root")),
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
    leaf_cols = "nonce, leaf" + (", branch, merkle_ok" if db.has_columns("batch_audit_leaf", "merkle_ok") else "")
    leaves = db.fetch_all(
        f"SELECT {leaf_cols} FROM batch_audit_leaf WHERE audit_id = %s ORDER BY nonce",
        (int(audit_id),),
    )
    out = dict(row)
    out["leaves"] = [dict(l) for l in leaves]
    return out


def create_fetch_requests(benchmark_id: str, nonces: list[int], requested_by: str | None = None) -> dict:
    """Ask the slaves that computed these nonces for their archived leaves.

    One ``batch_audit`` row (kind='fetch') per batch touched. The master
    advertises it on the slave's next get-batches poll; the slave answers
    with leaf + merkle branch; the auditor re-scores. Needs the job and the
    batch's root (batch_data) to still be in the DB, i.e. inside retention.
    """
    benchmark_id = str(benchmark_id).strip()
    wanted = sorted({int(n) for n in nonces})
    if not wanted:
        raise ValueError("no nonces")
    if not db.has_columns("batch_audit", "kind"):
        raise RuntimeError("batch_audit has no 'kind' column yet — restart the master to migrate")
    job = db.fetch_one(
        """
        SELECT benchmark_id, challenge, algorithm, settings, rand_hash, num_nonces, batch_size
        FROM job WHERE benchmark_id = %s
        """,
        (benchmark_id,),
    )
    if job is None:
        raise LookupError(f"job {benchmark_id} not found (outside retention?)")
    batch_size = int(job["batch_size"])
    num_nonces = int(job["num_nonces"] or 0)
    created, errors = [], []
    now_ms = _now_ms()
    by_batch: dict[int, list[int]] = {}
    for n in wanted:
        if num_nonces and not (0 <= n < num_nonces):
            errors.append({"nonce": n, "error": f"outside job range 0..{num_nonces - 1}"})
            continue
        by_batch.setdefault(n // batch_size, []).append(n)
    for bidx, ns in sorted(by_batch.items()):
        b = db.fetch_one(
            """
            SELECT rb.slave, bd.merkle_root, bd.solution_quality
            FROM root_batch rb
            LEFT JOIN batch_data bd ON bd.benchmark_id = rb.benchmark_id AND bd.batch_idx = rb.batch_idx
            WHERE rb.benchmark_id = %s AND rb.batch_idx = %s
            """,
            (benchmark_id, bidx),
        )
        if b is None or not b.get("slave"):
            errors.append({"batch_idx": bidx, "nonces": ns, "error": "no root_batch row (never assigned or pruned)"})
            continue
        qualities = b.get("solution_quality")
        if not isinstance(qualities, list) or not b.get("merkle_root"):
            errors.append({"batch_idx": bidx, "nonces": ns, "slave": b["slave"], "error": "root never submitted for this batch"})
            continue
        expected = []
        for n in ns:
            off = n - bidx * batch_size
            expected.append(int(qualities[off]) if 0 <= off < len(qualities) else None)
        if any(q is None for q in expected):
            errors.append({"batch_idx": bidx, "nonces": ns, "slave": b["slave"], "error": "posted quality list shorter than batch (corrupt batch_data)"})
            continue
        row = db.fetch_one(
            """
            INSERT INTO batch_audit (
                benchmark_id, batch_idx, slave, challenge, algorithm, settings, rand_hash,
                requested_nonces, expected_qualities, status, requested_at, kind, requested_by
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, 'requested', %s, 'fetch', %s)
            RETURNING id
            """,
            (
                benchmark_id,
                bidx,
                b["slave"],
                job["challenge"],
                job.get("algorithm"),
                _json(job["settings"]),
                job["rand_hash"],
                _json(ns),
                _json(expected),
                now_ms,
                (requested_by or "admin")[:200],
            ),
        )
        member = db.fetch_one(
            "SELECT active, trust_state FROM pool_members WHERE slave_name = %s",
            (b["slave"],),
        ) or {}
        seen = (
            db.fetch_one("SELECT last_seen FROM slave_seen WHERE slave_name = %s", (b["slave"],))
            if db.table_exists("slave_seen")
            else None
        ) or {}
        created.append(
            {
                "audit_id": int(row["id"]),
                "batch_idx": bidx,
                "slave": b["slave"],
                "nonces": ns,
                "posted_qualities": expected,
                "slave_active": bool(member.get("active")),
                "slave_trust": member.get("trust_state"),
                "slave_last_seen": seen.get("last_seen"),
            }
        )
    return {"benchmark_id": benchmark_id, "created": created, "errors": errors}


def _json(value) -> str:
    import json

    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))
