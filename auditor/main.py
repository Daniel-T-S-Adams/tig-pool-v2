"""InnoPool quality auditor.

Separate container so the master hot path never waits on a verifier and so
docker.sock is not mounted into the master. Loop:

  1. expire  : 'requested' rows past request_ttl_ms -> 'missing'
  2. verify  : 'pending' rows -> tig-verifier (docker exec into the CPU
               challenge containers on this box) -> passed / failed / error
  3. strikes : failed (and optionally missing) counts per slave inside the
               window -> deactivate + trust_state='quarantined' + release
               that slave's unfinished batches
  3b. trust  : probation members with a clean window of enough passed
               audits -> trust_state='trusted' (trust_source='auditor');
               auditor-promoted members with a failed audit -> probation.
  4. prune   : leaves of passed/skipped audits older than retention_days.
               Failed audits and their leaves are kept forever.

Only tig-verifier is ever run. Nothing here re-solves a nonce.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import psycopg2
import psycopg2.extras

from audit_core import (
    AuditOutcome,
    AuditorSettings,
    LeafVerdict,
    build_verifier_cmd,
    classify_verifier_failure,
    judge,
    parse_verifier_quality,
    promotion_decision,
    should_verify,
)

logging.basicConfig(
    format="%(levelname)s - [auditor] - %(message)s",
    level=logging.DEBUG if os.environ.get("VERBOSE") else logging.INFO,
)
logger = logging.getLogger("auditor")

# Shared with the challenge containers (same host path mounted at /app/audit
# in every runtime container). Leaves are written here so tig-verifier can
# open them; removed again after the verdict.
AUDIT_DIR = os.environ.get("AUDIT_DIR_IN_CONTAINER", "/app/audit")
SCRATCH_SUBDIR = "scratch"

_conn_params = {
    "host": os.environ.get("POSTGRES_HOST", "db"),
    "dbname": os.environ.get("POSTGRES_DB", "innopool"),
    "user": os.environ.get("POSTGRES_USER", "postgres"),
    "password": os.environ.get("POSTGRES_PASSWORD", ""),
    "options": os.environ.get(
        "AUDITOR_POSTGRES_OPTIONS",
        "-c statement_timeout=30000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=20000",
    ),
}


def now_ms() -> int:
    return int(time.time() * 1000)


class DB:
    def __init__(self):
        self._conn = None
        self._lock = threading.Lock()

    def _get(self):
        if self._conn is None or getattr(self._conn, "closed", 1):
            self._conn = psycopg2.connect(**_conn_params)
        return self._conn

    def fetch_all(self, sql, params=None) -> List[dict]:
        with self._lock:
            conn = self._get()
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
                conn.commit()
                return rows
            except Exception:
                conn.rollback()
                raise

    def fetch_one(self, sql, params=None) -> Optional[dict]:
        rows = self.fetch_all(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=None) -> int:
        with self._lock:
            conn = self._get()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    n = cur.rowcount
                conn.commit()
                return n
            except Exception:
                conn.rollback()
                raise

    def execute_many(self, *queries) -> None:
        with self._lock:
            conn = self._get()
            try:
                with conn.cursor() as cur:
                    for sql, params in queries:
                        cur.execute(sql, params)
                conn.commit()
            except Exception:
                conn.rollback()
                raise


def wait_for_schema(db: DB) -> None:
    while True:
        try:
            row = db.fetch_one(
                "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='batch_audit'"
            )
            if row:
                break
            logger.info("waiting for batch_audit table (master creates it on start)")
        except Exception as exc:
            logger.warning("db not ready: %s", exc)
        time.sleep(5)
    # Columns added for fetch audits; the master migrates them at start. Add
    # them here too so an auditor upgraded before the master keeps working.
    for stmt in (
        "ALTER TABLE batch_audit ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'sample'",
        "ALTER TABLE batch_audit ADD COLUMN IF NOT EXISTS requested_by TEXT",
        "ALTER TABLE batch_audit_leaf ADD COLUMN IF NOT EXISTS branch TEXT",
        "ALTER TABLE batch_audit_leaf ADD COLUMN IF NOT EXISTS merkle_ok BOOLEAN",
    ):
        try:
            db.execute(stmt)
        except Exception as exc:
            logger.warning("schema upgrade failed (%s): %s", stmt[:60], exc)


def ensure_trust_columns(db: DB) -> None:
    """pool_manager owns trust_state/trusted_at; trust_source is ours and
    marks promotions the auditor made so it never undoes an operator's."""
    db.execute("ALTER TABLE pool_members ADD COLUMN IF NOT EXISTS trust_state TEXT NOT NULL DEFAULT 'probation'")
    db.execute("ALTER TABLE pool_members ADD COLUMN IF NOT EXISTS trusted_at BIGINT")
    db.execute("ALTER TABLE pool_members ADD COLUMN IF NOT EXISTS trust_source TEXT")


# ── phase 1: expire undelivered requests ─────────────────────────────────────

def expire_requests(db: DB, s: AuditorSettings) -> int:
    # Samples must arrive within minutes of the root ack. Fetches wait longer:
    # the slave may be offline, and it answers on its next poll.
    n = db.execute(
        """
        UPDATE batch_audit
        SET status = 'missing',
            verified_at = %s,
            error = CASE WHEN kind = 'fetch'
                         THEN 'archived leaves not delivered within fetch ttl'
                         ELSE 'leaves not delivered within request ttl' END
        WHERE status = 'requested'
          AND ((kind = 'fetch' AND requested_at < %s)
               OR (kind <> 'fetch' AND requested_at < %s))
        """,
        (now_ms(), now_ms() - int(s.fetch_ttl_ms), now_ms() - int(s.request_ttl_ms)),
    )
    if n:
        logger.info("marked %s audit request(s) missing", n)
    return n


# ── phase 2: verify pending leaves ───────────────────────────────────────────

def _trust_state(db: DB, slave: str) -> str:
    row = db.fetch_one(
        "SELECT trust_state, active FROM pool_members WHERE slave_name = %s",
        (slave,),
    )
    if row is None:
        return "unknown"
    return str(row.get("trust_state") or "probation").lower()


def _container_running(name: str) -> bool:
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True, text=True, timeout=15,
        )
        return out.returncode == 0 and out.stdout.strip() == "true"
    except Exception:
        return False


def verify_leaf(row: dict, nonce: int, leaf: dict, expected: Optional[int], s: AuditorSettings) -> LeafVerdict:
    scratch = os.path.join(AUDIT_DIR, SCRATCH_SUBDIR)
    os.makedirs(scratch, exist_ok=True)
    fname = f"{row['benchmark_id']}_{row['batch_idx']}_{nonce}.json"
    host_path = os.path.join(scratch, fname)
    # Same mount inside the challenge container.
    container_path = f"/app/audit/{SCRATCH_SUBDIR}/{fname}"
    try:
        with open(host_path, "w") as f:
            json.dump(leaf, f, separators=(",", ":"))
        cmd = build_verifier_cmd(row["challenge"], row["settings"], row["rand_hash"], nonce, container_path)
        logger.debug("verifying %s nonce %s: %s", row["benchmark_id"], nonce, " ".join(cmd[:4]))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=s.verifier_timeout_s)
        except subprocess.TimeoutExpired:
            return LeafVerdict(nonce, expected, None, False, error="infra: verifier timeout")
        if proc.returncode != 0:
            kind = classify_verifier_failure(proc.returncode, proc.stderr)
            msg = (proc.stderr or proc.stdout or "").strip()[-400:]
            if kind == "infra":
                return LeafVerdict(nonce, expected, None, False, error=f"infra: {msg}")
            return LeafVerdict(nonce, expected, None, False, error=f"invalid solution: {msg}")
        actual = parse_verifier_quality(proc.stdout)
        if actual is None:
            return LeafVerdict(nonce, expected, None, False, error="no quality line in verifier output")
        return LeafVerdict(nonce, expected, actual, actual == expected)
    finally:
        try:
            os.remove(host_path)
        except OSError:
            pass


def verify_one(db: DB, row: dict, s: AuditorSettings) -> AuditOutcome:
    nonces = [int(n) for n in row["requested_nonces"]]
    expected = [int(q) for q in row["expected_qualities"]]
    exp_by_nonce = dict(zip(nonces, expected))
    leaves = db.fetch_all(
        "SELECT nonce, leaf, merkle_ok FROM batch_audit_leaf WHERE audit_id = %s ORDER BY nonce",
        (int(row["id"]),),
    )
    delivered = [int(l["nonce"]) for l in leaves]
    if row["challenge"] not in s.challenges:
        return AuditOutcome("skipped", {}, error=f"challenge {row['challenge']} not verified on this box")
    if not _container_running(row["challenge"]):
        return AuditOutcome("error", {}, error=f"infra: container {row['challenge']} not running")
    verdicts: Dict[int, LeafVerdict] = {}
    for l in leaves:
        nonce = int(l["nonce"])
        # The master checked the leaf's merkle branch against the root the
        # slave committed at submit. False means this is not that leaf —
        # a fail regardless of what the verifier would say about it.
        if l.get("merkle_ok") is False:
            verdicts[nonce] = LeafVerdict(
                nonce, exp_by_nonce.get(nonce), None, False,
                error="merkle: leaf does not match the root committed at submit",
            )
            continue
        verdicts[nonce] = verify_leaf(row, nonce, l["leaf"], exp_by_nonce.get(nonce), s)
    return judge(expected, nonces, verdicts, delivered=delivered)


def process_pending(db: DB, s: AuditorSettings) -> int:
    rows = db.fetch_all(
        """
        SELECT id, benchmark_id, batch_idx, slave, challenge, settings, rand_hash,
               requested_nonces, expected_qualities, attempts, kind
        FROM batch_audit
        WHERE status = 'pending'
          AND attempts < %s
        ORDER BY (kind = 'fetch') DESC, leaves_received_at ASC NULLS LAST, id ASC
        LIMIT %s
        """,
        (int(s.max_attempts), int(s.batch_limit)),
    )
    done = 0
    to_verify = []
    for row in rows:
        trust = _trust_state(db, row["slave"])
        # Fetches were asked for by the operator: always verify them.
        if row.get("kind") != "fetch" and not should_verify(trust, s):
            db.execute(
                """
                UPDATE batch_audit
                SET status = 'skipped', verified_at = %s, error = %s
                WHERE id = %s AND status = 'pending'
                """,
                (now_ms(), f"not sampled (trust_state={trust})", int(row["id"])),
            )
            done += 1
            continue
        to_verify.append(row)

    def _safe_verify(row):
        try:
            return verify_one(db, row, s)
        except Exception as exc:
            logger.exception("audit %s crashed", row["id"])
            return AuditOutcome("error", {}, error=f"infra: {exc}")

    # Verifier runs are docker execs; a few in parallel keeps energy leaves
    # (~7s each) from building a backlog. Small pool so the VPS stays quiet.
    if to_verify:
        with ThreadPoolExecutor(max_workers=max(1, s.workers)) as ex:
            outcomes = list(ex.map(_safe_verify, to_verify))
    else:
        outcomes = []

    for row, outcome in zip(to_verify, outcomes):
        attempts = int(row["attempts"] or 0) + 1
        if outcome.status == "error" and attempts < s.max_attempts:
            # stay pending, retry later
            db.execute(
                "UPDATE batch_audit SET attempts = %s, error = %s WHERE id = %s",
                (attempts, (outcome.error or "")[:1000], int(row["id"])),
            )
            logger.warning(
                "audit %s (%s_%s from %s) infra error, attempt %s/%s: %s",
                row["id"], row["benchmark_id"], row["batch_idx"], row["slave"], attempts, s.max_attempts, outcome.error,
            )
            continue
        db.execute(
            """
            UPDATE batch_audit
            SET status = %s, attempts = %s, verified_at = %s, result = %s, error = %s
            WHERE id = %s
            """,
            (
                outcome.status,
                attempts,
                now_ms(),
                json.dumps(outcome.result_json()),
                (outcome.error or None),
                int(row["id"]),
            ),
        )
        done += 1
        tag = f"fetch #{row['id']}" if row.get("kind") == "fetch" else f"audit #{row['id']}"
        if outcome.status == "failed":
            logger.error(
                "AUDIT FAILED %s %s_%s slave=%s challenge=%s result=%s",
                tag, row["benchmark_id"], row["batch_idx"], row["slave"], row["challenge"], json.dumps(outcome.result_json()),
            )
        else:
            logger.info(
                "%s %s_%s slave=%s %s (%s leaves)",
                tag, row["benchmark_id"], row["batch_idx"], row["slave"], outcome.status, len(outcome.verdicts),
            )
    return done


# ── phase 3: strikes → quarantine ────────────────────────────────────────────

def _strike_counts(db: DB, s: AuditorSettings) -> List[dict]:
    return db.fetch_all(
        """
        SELECT slave,
               COUNT(*) FILTER (WHERE status = 'failed')  AS failed,
               COUNT(*) FILTER (WHERE status = 'missing') AS missing
        FROM batch_audit
        WHERE requested_at >= %s
          AND status IN ('failed', 'missing')
        GROUP BY slave
        """,
        (now_ms() - int(s.strike_window_ms),),
    )


def quarantine_slave(db: DB, slave: str, reason: str) -> None:
    row = db.fetch_one(
        "SELECT active, trust_state FROM pool_members WHERE slave_name = %s",
        (slave,),
    )
    if row is None:
        logger.warning("cannot quarantine %s: not a registered member", slave)
        return
    if str(row.get("trust_state") or "").lower() == "operator":
        # Operator's own machines: never auto-lock the pool out of itself. Log loudly.
        logger.error("operator slave %s failed audit (%s) — NOT auto-quarantined", slave, reason)
        return
    if not row.get("active") and str(row.get("trust_state") or "").lower() == "quarantined":
        return
    note = f"auto-quarantined by auditor: {reason[:400]}"
    logger.error("QUARANTINE %s: %s", slave, reason)
    db.execute_many(
        (
            """
            UPDATE pool_members
            SET active = false,
                trust_state = 'quarantined',
                notes = CONCAT_WS(E'\n', NULLIF(notes, ''), %s)
            WHERE slave_name = %s
            """,
            (note, slave),
        ),
        (
            """
            UPDATE root_batch
            SET slave = NULL, start_time = NULL, end_time = NULL, num_attempts = 0
            WHERE slave = %s AND ready IS NULL
            """,
            (slave,),
        ),
        (
            """
            UPDATE proofs_batch
            SET slave = NULL, start_time = NULL, end_time = NULL, num_attempts = 0
            WHERE slave = %s AND ready IS NULL
            """,
            (slave,),
        ),
    )


def apply_strikes(db: DB, s: AuditorSettings) -> None:
    for row in _strike_counts(db, s):
        failed = int(row["failed"] or 0)
        missing = int(row["missing"] or 0)
        if s.fail_quarantine_threshold and failed >= s.fail_quarantine_threshold:
            quarantine_slave(db, row["slave"], f"{failed} failed quality audit(s) in window")
        elif s.missing_quarantine_threshold and missing >= s.missing_quarantine_threshold:
            quarantine_slave(db, row["slave"], f"{missing} undelivered audit request(s) in window")


# ── phase 3b: probation -> trusted (and back) ───────────────────────────────

def _trust_candidates(db: DB, s: AuditorSettings) -> List[dict]:
    """Active probation/trusted members with their audit tallies."""
    window_start = now_ms() - int(s.promote_window_ms)
    return db.fetch_all(
        """
        SELECT m.slave_name,
               LOWER(COALESCE(m.trust_state, 'probation')) AS trust_state,
               m.trust_source,
               COALESCE(a.passed, 0)  AS passed,
               COALESCE(a.failed, 0)  AS failed,
               COALESCE(a.missing, 0) AS missing,
               f.first_audit_at
        FROM pool_members m
        LEFT JOIN (
            SELECT slave,
                   COUNT(*) FILTER (WHERE status = 'passed')  AS passed,
                   COUNT(*) FILTER (WHERE status = 'failed')  AS failed,
                   COUNT(*) FILTER (WHERE status = 'missing') AS missing
            FROM batch_audit
            WHERE requested_at >= %s
            GROUP BY slave
        ) a ON a.slave = m.slave_name
        LEFT JOIN (
            SELECT slave, MIN(requested_at) AS first_audit_at
            FROM batch_audit
            GROUP BY slave
        ) f ON f.slave = m.slave_name
        WHERE m.active = true
          AND LOWER(COALESCE(m.trust_state, 'probation')) IN ('probation', 'trusted')
        """,
        (window_start,),
    )


def promote_slave(db: DB, slave: str, passed: int, s: AuditorSettings) -> None:
    note = (
        f"auto-promoted to trusted by auditor: {passed} passed quality audits, "
        f"0 failed/missing in {s.promote_window_ms // 3600000}h"
    )
    logger.info("TRUST %s: %s", slave, note)
    db.execute(
        """
        UPDATE pool_members
        SET trust_state = 'trusted',
            trusted_at = %s,
            trust_source = 'auditor',
            notes = CONCAT_WS(E'\n', NULLIF(notes, ''), %s)
        WHERE slave_name = %s AND LOWER(COALESCE(trust_state, 'probation')) = 'probation'
        """,
        (now_ms(), note, slave),
    )


def demote_slave(db: DB, slave: str, failed: int) -> None:
    note = f"auto-demoted to probation by auditor: {failed} failed quality audit(s)"
    logger.warning("DEMOTE %s: %s", slave, note)
    db.execute(
        """
        UPDATE pool_members
        SET trust_state = 'probation',
            trusted_at = NULL,
            trust_source = NULL,
            notes = CONCAT_WS(E'\n', NULLIF(notes, ''), %s)
        WHERE slave_name = %s AND trust_source = 'auditor' AND LOWER(trust_state) = 'trusted'
        """,
        (note, slave),
    )


def apply_promotions(db: DB, s: AuditorSettings) -> None:
    if s.promote_min_passed <= 0:
        return
    t = now_ms()
    for row in _trust_candidates(db, s):
        decision = promotion_decision(
            row["trust_state"],
            row.get("trust_source"),
            passed_in_window=int(row["passed"] or 0),
            failed_in_window=int(row["failed"] or 0),
            missing_in_window=int(row["missing"] or 0),
            first_audit_at_ms=row.get("first_audit_at"),
            settings=s,
            now_ms=t,
        )
        if decision == "promote":
            promote_slave(db, row["slave_name"], int(row["passed"] or 0), s)
        elif decision == "demote":
            demote_slave(db, row["slave_name"], int(row["failed"] or 0))


# ── phase 4: retention ───────────────────────────────────────────────────────

def prune(db: DB, s: AuditorSettings) -> None:
    cutoff = now_ms() - int(s.retention_days) * 86400 * 1000
    # Leaves of passed/skipped audits: drop the payload, keep the verdict row.
    n = db.execute(
        """
        DELETE FROM batch_audit_leaf
        WHERE audit_id IN (
            SELECT id FROM batch_audit
            WHERE status IN ('passed', 'skipped')
              AND requested_at < %s
        )
        """,
        (cutoff,),
    )
    if n:
        logger.info("pruned %s audit leaf row(s) older than %s days", n, s.retention_days)
    # Verdict rows for passed/skipped/missing beyond 4x retention.
    n = db.execute(
        """
        DELETE FROM batch_audit
        WHERE status IN ('passed', 'skipped', 'missing')
          AND requested_at < %s
        """,
        (now_ms() - 4 * int(s.retention_days) * 86400 * 1000,),
    )
    if n:
        logger.info("pruned %s old audit verdict row(s)", n)


# ── main loop ────────────────────────────────────────────────────────────────

def main() -> None:
    s = AuditorSettings.from_env()
    logger.info(
        "auditor start: challenges=%s trusted_sample_rate=%s fail_threshold=%s missing_threshold=%s "
        "retention_days=%s promote=%s passed/%sh fetch_ttl=%sh",
        ",".join(s.challenges), s.trusted_sample_rate, s.fail_quarantine_threshold,
        s.missing_quarantine_threshold, s.retention_days,
        s.promote_min_passed or "off", s.promote_window_ms // 3600000,
        s.fetch_ttl_ms // 3600000,
    )
    db = DB()
    wait_for_schema(db)
    ensure_trust_columns(db)
    for c in s.challenges:
        if not _container_running(c):
            logger.warning("challenge container %s is not running; audits for it will retry", c)
    last_prune = 0.0
    last_promote = 0.0
    while True:
        t0 = time.time()
        try:
            expire_requests(db, s)
            process_pending(db, s)
            apply_strikes(db, s)
            if t0 - last_promote > 60:
                apply_promotions(db, s)
                last_promote = t0
            if t0 - last_prune > 3600:
                prune(db, s)
                last_prune = t0
        except Exception as exc:
            logger.exception("auditor loop error: %s", exc)
            time.sleep(5)
        time.sleep(max(0.2, s.poll_interval_s - (time.time() - t0)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
