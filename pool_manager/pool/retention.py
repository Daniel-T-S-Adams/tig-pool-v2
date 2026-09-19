"""Rolling retention for operational history.

What is trimmed and what is never touched
-----------------------------------------
Trimmed (older than RETENTION_DAYS, default 14):
  * job + job_data + root_batch + proofs_batch + batch_data
      A TIG precommit expires ~2h after creation, so a job older than a day
      is dead by protocol. This family was 71 GB of a 74 GB database.
  * autopilot_decisions, ai_optimizer_decisions
      Debug logs with a full JSONB report per row (~3 GB / few months).

Never touched here:
  * pool_coinbase_history   append-only payout ledger
  * pool_contributions      per-member earnings history, small
  * pool_members / fleets   the roster
  * batch_audit             the auditor owns its own retention and keeps
                            failed audits (evidence) forever
  * benchmark_slot          persistent slot entities; only unlinked from
                            deleted jobs

Runs from the pool_manager background loop, once per RETENTION_INTERVAL_S,
deleting at most RETENTION_JOB_BATCH jobs per pass so it never holds locks
for long while slaves are active.
"""

from __future__ import annotations

import logging
import os
import time

from . import database as db

logger = logging.getLogger(__name__)

RETENTION_ENABLED = os.environ.get("RETENTION_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
RETENTION_DAYS = max(1, int(os.environ.get("RETENTION_DAYS", "14")))
RETENTION_INTERVAL_S = max(60, int(os.environ.get("RETENTION_INTERVAL_S", "3600")))
RETENTION_JOB_BATCH = max(10, int(os.environ.get("RETENTION_JOB_BATCH", "200")))

_last_run = 0.0


def _cutoff_ms(now_ms: int | None = None) -> int:
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    return now_ms - RETENTION_DAYS * 86400 * 1000


def prune_jobs(cutoff_ms: int, limit: int = RETENTION_JOB_BATCH) -> int:
    """Delete up to ``limit`` jobs created before ``cutoff_ms`` with all their
    batch rows. Returns the number of jobs removed."""
    rows = db.fetch_all(
        """
        SELECT benchmark_id
        FROM job
        WHERE start_time IS NOT NULL
          AND start_time < %s
        ORDER BY start_time ASC
        LIMIT %s
        """,
        (int(cutoff_ms), int(limit)),
    )
    ids = [r["benchmark_id"] for r in rows]
    if not ids:
        return 0
    # The manager's session default is a 10s statement timeout (dashboard
    # safety). A batch of jobs with large JSONB proofs can take longer.
    db.execute_many(
        ("SET LOCAL statement_timeout = '120s'", None),
        ("DELETE FROM batch_data     WHERE benchmark_id = ANY(%s)", (ids,)),
        ("DELETE FROM proofs_batch   WHERE benchmark_id = ANY(%s)", (ids,)),
        ("DELETE FROM root_batch     WHERE benchmark_id = ANY(%s)", (ids,)),
        ("DELETE FROM job_data       WHERE benchmark_id = ANY(%s)", (ids,)),
        (
            """
            UPDATE benchmark_slot
            SET benchmark_id = NULL, state = 'idle'
            WHERE benchmark_id = ANY(%s)
            """,
            (ids,),
        ),
        ("DELETE FROM job            WHERE benchmark_id = ANY(%s)", (ids,)),
        lock_timeout="5s",
    )
    return len(ids)


def prune_decisions(cutoff_ms: int) -> dict:
    out = {}
    for table in ("autopilot_decisions", "ai_optimizer_decisions"):
        if not db.table_exists(table):
            continue
        try:
            if db.has_columns(table, "generated_at_ms"):
                sql = f"DELETE FROM {table} WHERE generated_at_ms < %s"  # nosec B608 — table from fixed tuple
            elif db.has_columns(table, "created_at"):
                sql = f"DELETE FROM {table} WHERE created_at < to_timestamp(%s / 1000.0)"  # nosec B608
            else:
                continue
            with db.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (int(cutoff_ms),))
                    out[table] = cur.rowcount
        except Exception as exc:
            logger.warning("retention: %s prune failed: %s", table, exc)
    return out


def run_once(now_ms: int | None = None) -> dict:
    cutoff = _cutoff_ms(now_ms)
    jobs = prune_jobs(cutoff)
    decisions = prune_decisions(cutoff)
    if jobs or any(decisions.values()):
        logger.info(
            "retention: removed %s job(s) and %s older than %sd",
            jobs,
            ", ".join(f"{v} {k}" for k, v in decisions.items()) or "no decision rows",
            RETENTION_DAYS,
        )
    return {"jobs": jobs, **decisions}


def maybe_run() -> None:
    """Call from the background loop; cheap no-op between intervals."""
    global _last_run
    if not RETENTION_ENABLED:
        return
    now = time.time()
    if now - _last_run < RETENTION_INTERVAL_S:
        return
    _last_run = now
    run_once()
