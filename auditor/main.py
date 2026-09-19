"""InnoPool quality auditor.

Separate container so the master hot path never waits on a verifier and so
docker.sock is not mounted into the master. Loop:

  1. expire  : 'requested' rows past request_ttl_ms -> 'missing'
  2. verify  : 'pending' rows -> tig-verifier (docker exec into the CPU
               challenge containers on this box) -> passed / failed / error
  3. strikes : failed (and optionally missing) counts per slave inside the
               window -> deactivate + trust_state='quarantined' + release
               that slave's unfinished batches
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
                return
            logger.info("waiting for batch_audit table (master creates it on start)")
        except Exception as exc:
            logger.warning("db not ready: %s", exc)
        time.sleep(5)


# ── phase 1: expire undelivered requests ─────────────────────────────────────

def expire_requests(db: DB, s: AuditorSettings) -> int:
    n = db.execute(
        """
        UPDATE batch_audit
        SET status = 'missing',
            verified_at = %s,
            error = 'leaves not delivered within request ttl'
        WHERE status = 'requested'
          AND requested_at < %s
        """,
        (now_ms(), now_ms() - int(s.request_ttl_ms)),
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
        "SELECT nonce, leaf FROM batch_audit_leaf WHERE audit_id = %s ORDER BY nonce",
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
        verdicts[nonce] = verify_leaf(row, nonce, l["leaf"], exp_by_nonce.get(nonce), s)
    return judge(expected, nonces, verdicts, delivered=delivered)


def process_pending(db: DB, s: AuditorSettings) -> int:
    rows = db.fetch_all(
        """
        SELECT id, benchmark_id, batch_idx, slave, challenge, settings, rand_hash,
               requested_nonces, expected_qualities, attempts
        FROM batch_audit
        WHERE status = 'pending'
          AND attempts < %s
        ORDER BY leaves_received_at ASC NULLS LAST, id ASC
        LIMIT %s
        """,
        (int(s.max_attempts), int(s.batch_limit)),
    )
    done = 0
    to_verify = []
    for row in rows:
        trust = _trust_state(db, row["slave"])
        if not should_verify(trust, s):
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
        if outcome.status == "failed":
            logger.error(
                "AUDIT FAILED %s_%s slave=%s challenge=%s result=%s",
                row["benchmark_id"], row["batch_idx"], row["slave"], row["challenge"], json.dumps(outcome.result_json()),
            )
        else:
            logger.info(
                "audit %s_%s slave=%s %s (%s leaves)",
                row["benchmark_id"], row["batch_idx"], row["slave"], outcome.status, len(outcome.verdicts),
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
        "auditor start: challenges=%s trusted_sample_rate=%s fail_threshold=%s missing_threshold=%s retention_days=%s",
        ",".join(s.challenges), s.trusted_sample_rate, s.fail_quarantine_threshold,
        s.missing_quarantine_threshold, s.retention_days,
    )
    db = DB()
    wait_for_schema(db)
    for c in s.challenges:
        if not _container_running(c):
            logger.warning("challenge container %s is not running; audits for it will retry", c)
    last_prune = 0.0
    while True:
        t0 = time.time()
        try:
            expire_requests(db, s)
            process_pending(db, s)
            apply_strikes(db, s)
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
