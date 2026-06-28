"""
Pool autopilot report and guarded controller.

The report path is always read-only. The background controller can optionally
apply small config changes when AUTOPILOT_MODE=apply and the pool has been clean
for enough consecutive windows.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any

from pool import database as db

logger = logging.getLogger("pool.autopilot")

MASTER_URL = os.environ.get("MASTER_INTERNAL_URL", "http://master:3336")
AUTOPILOT_MODE = os.environ.get("AUTOPILOT_MODE", "off").lower()
RUN_INTERVAL_S = int(os.environ.get("AUTOPILOT_INTERVAL_S", "300"))
ACTIVE_WINDOW_MS = int(os.environ.get("AUTOPILOT_ACTIVE_WINDOW_MS", str(10 * 60 * 1000)))
METRIC_WINDOW_MS = int(os.environ.get("AUTOPILOT_METRIC_WINDOW_MS", str(30 * 60 * 1000)))
STALE_ROOT_MS = int(os.environ.get("AUTOPILOT_STALE_ROOT_MS", str(45 * 60 * 1000)))
STALE_PROOF_MS = int(os.environ.get("AUTOPILOT_STALE_PROOF_MS", str(20 * 60 * 1000)))
MIN_MAX_BENCHMARKS = int(os.environ.get("AUTOPILOT_MIN_MAX_BENCHMARKS", "3"))
MAX_MAX_BENCHMARKS = int(os.environ.get("AUTOPILOT_MAX_MAX_BENCHMARKS", "96"))
APPLY_MIN_CLEAN_WINDOWS = int(os.environ.get("AUTOPILOT_APPLY_MIN_CLEAN_WINDOWS", "2"))
MAX_BENCHMARK_STEP = int(os.environ.get("AUTOPILOT_MAX_BENCHMARK_STEP", "2"))
MAX_BENCHMARK_UP_STEP = int(os.environ.get("AUTOPILOT_MAX_BENCHMARK_UP_STEP", str(MAX_BENCHMARK_STEP)))
MAX_BENCHMARK_DOWN_STEP = int(os.environ.get("AUTOPILOT_MAX_BENCHMARK_DOWN_STEP", str(MAX_BENCHMARK_STEP)))
SLOT_STEP = int(os.environ.get("AUTOPILOT_SLOT_STEP", "1"))
SLOT_UP_STEP = int(os.environ.get("AUTOPILOT_SLOT_UP_STEP", str(SLOT_STEP)))
SLOT_DOWN_STEP = int(os.environ.get("AUTOPILOT_SLOT_DOWN_STEP", str(SLOT_STEP)))
MAX_CPU_SLOTS = int(os.environ.get("AUTOPILOT_MAX_CPU_SLOTS", "64"))
MAX_GPU_SLOTS_PER_TYPE = int(os.environ.get("AUTOPILOT_MAX_GPU_SLOTS_PER_TYPE", "6"))
MAX_CPU_CHALLENGE_BENCHMARKS = int(os.environ.get("AUTOPILOT_MAX_CPU_CHALLENGE_BENCHMARKS", "16"))
MAX_GPU_CHALLENGE_BENCHMARKS = int(os.environ.get("AUTOPILOT_MAX_GPU_CHALLENGE_BENCHMARKS", "12"))
MAX_CPU_SLAVE_CAP = int(os.environ.get("AUTOPILOT_MAX_CPU_SLAVE_CAP", "256"))
MAX_GPU_SLAVE_CAP = int(os.environ.get("AUTOPILOT_MAX_GPU_SLAVE_CAP", "24"))
MIN_CPU_SLAVE_CAP = int(os.environ.get("AUTOPILOT_MIN_CPU_SLAVE_CAP", "4"))
MIN_GPU_SLAVE_CAP = int(os.environ.get("AUTOPILOT_MIN_GPU_SLAVE_CAP", "1"))
BENCHMARK_BUFFER = int(os.environ.get("AUTOPILOT_BENCHMARK_BUFFER", "2"))
PRODUCTIVE_IDLE_CPU_SCALE_MIN = int(os.environ.get("AUTOPILOT_PRODUCTIVE_IDLE_CPU_SCALE_MIN", "5"))
PRODUCTIVE_IDLE_CPU_PER_SLOT = int(os.environ.get("AUTOPILOT_PRODUCTIVE_IDLE_CPU_PER_SLOT", "4"))
PRODUCTIVE_IDLE_STALE_ROOT_TOLERANCE = int(
    os.environ.get("AUTOPILOT_PRODUCTIVE_IDLE_STALE_ROOT_TOLERANCE", "5")
)
PRODUCTIVE_IDLE_GPU_SCALE_MIN = int(os.environ.get("AUTOPILOT_PRODUCTIVE_IDLE_GPU_SCALE_MIN", "1"))
PRODUCTIVE_IDLE_GPU_PER_SLOT = int(os.environ.get("AUTOPILOT_PRODUCTIVE_IDLE_GPU_PER_SLOT", "1"))
CAP_SCALE_COMPLETIONS_PER_STEP = int(os.environ.get("AUTOPILOT_CAP_SCALE_COMPLETIONS_PER_STEP", "20"))
BUNDLE_TARGET_MIN_ROOT_BATCHES = int(os.environ.get("AUTOPILOT_BUNDLE_TARGET_MIN_ROOT_BATCHES", "8"))
BUNDLE_TARGET_MAX_ROOT_BATCHES = int(os.environ.get("AUTOPILOT_BUNDLE_TARGET_MAX_ROOT_BATCHES", "192"))
BUNDLE_TARGET_ROOT_RUNTIME_SEC = int(os.environ.get("AUTOPILOT_BUNDLE_TARGET_ROOT_RUNTIME_SEC", "900"))
FUNNEL_TARGET_PROOF_SUBMIT_SEC = int(os.environ.get("AUTOPILOT_FUNNEL_TARGET_PROOF_SUBMIT_SEC", "1200"))
FUNNEL_MIN_PROOF_CONVERSION_RATE = float(os.environ.get("AUTOPILOT_FUNNEL_MIN_PROOF_CONVERSION_RATE", "0.85"))
FUNNEL_MAX_STOPPED_OR_EXPIRED_RATE = float(os.environ.get("AUTOPILOT_FUNNEL_MAX_STOPPED_OR_EXPIRED_RATE", "0.10"))
FUNNEL_DRAIN_MIN_MAX_BENCHMARKS = int(os.environ.get("AUTOPILOT_FUNNEL_DRAIN_MIN_MAX_BENCHMARKS", "12"))
WORKLOAD_MIN_BUNDLES = int(os.environ.get("AUTOPILOT_WORKLOAD_MIN_BUNDLES", "4"))
WORKLOAD_MIN_BATCH_SIZE = int(os.environ.get("AUTOPILOT_WORKLOAD_MIN_BATCH_SIZE", "8"))
WORKLOAD_MIN_WEIGHT = int(os.environ.get("AUTOPILOT_WORKLOAD_MIN_WEIGHT", "1"))
WORKLOAD_MAX_BUNDLE_STEP = int(os.environ.get("AUTOPILOT_WORKLOAD_MAX_BUNDLE_STEP", "1"))
WORKLOAD_SAFETY_COOLDOWN_MS = int(os.environ.get("AUTOPILOT_WORKLOAD_SAFETY_COOLDOWN_MS", str(METRIC_WINDOW_MS)))
WORKLOAD_CANARY_COOLDOWN_MS = int(os.environ.get("AUTOPILOT_WORKLOAD_CANARY_COOLDOWN_MS", str(METRIC_WINDOW_MS)))
WORKLOAD_FAST_PROOF_FACTOR = float(os.environ.get("AUTOPILOT_WORKLOAD_FAST_PROOF_FACTOR", "0.50"))
WORKLOAD_HIGH_PROOF_CONVERSION_RATE = float(os.environ.get("AUTOPILOT_WORKLOAD_HIGH_PROOF_CONVERSION_RATE", "0.95"))
STRANDED_BENCHMARK_MS = int(os.environ.get("AUTOPILOT_STRANDED_BENCHMARK_MS", str(30 * 60 * 1000)))
STRANDED_DOWNSCALE_STEP = int(os.environ.get("AUTOPILOT_STRANDED_DOWNSCALE_STEP", "2"))
STRANDED_BUFFER_BENCHMARKS = int(os.environ.get("AUTOPILOT_STRANDED_BUFFER_BENCHMARKS", "2"))
STALE_CLEANUP_ENABLED = os.environ.get("AUTOPILOT_STALE_CLEANUP_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
STALE_CLEANUP_MIN_AGE_MS = int(os.environ.get("AUTOPILOT_STALE_CLEANUP_MIN_AGE_MS", str(60 * 60 * 1000)))
STALE_ROOT_CLEANUP_MIN_AGE_MS = int(
    os.environ.get("AUTOPILOT_STALE_ROOT_CLEANUP_MIN_AGE_MS", str(STALE_CLEANUP_MIN_AGE_MS))
)
STALE_PROOF_CLEANUP_MIN_AGE_MS = int(
    os.environ.get("AUTOPILOT_STALE_PROOF_CLEANUP_MIN_AGE_MS", str(20 * 60 * 1000))
)
STALE_CLEANUP_MAX_ROWS = int(os.environ.get("AUTOPILOT_STALE_CLEANUP_MAX_ROWS", "50"))
PRECOMMIT_EXPIRY_CLEANUP_ENABLED = os.environ.get(
    "AUTOPILOT_PRECOMMIT_EXPIRY_CLEANUP_ENABLED",
    "true",
).lower() in ("1", "true", "yes", "on")
PRECOMMIT_ROOT_RECLAIM_AGE_MS = int(
    os.environ.get("AUTOPILOT_PRECOMMIT_ROOT_RECLAIM_AGE_MS", str(50 * 60 * 1000))
)
PRECOMMIT_PROOF_RECLAIM_AGE_MS = int(
    os.environ.get("AUTOPILOT_PRECOMMIT_PROOF_RECLAIM_AGE_MS", str(20 * 60 * 1000))
)
PRECOMMIT_ABANDON_NO_ROOT_AGE_MS = int(
    os.environ.get("AUTOPILOT_PRECOMMIT_ABANDON_NO_ROOT_AGE_MS", str(75 * 60 * 1000))
)

GPU_CHALLENGES = {"vector_search", "hypergraph", "neuralnet_optimizer"}
GPU_SLOT_TYPES = ("vector_search", "hypergraph", "neuralnet_optimizer")
GPU_CHALLENGE_ID_TO_SLOT = {
    "c004": "vector_search",
    "c005": "hypergraph",
    "c006": "neuralnet_optimizer",
    "vector_search": "vector_search",
    "hypergraph": "hypergraph",
    "neuralnet_optimizer": "neuralnet_optimizer",
}
CPU_SLOT_TYPE = "cpu"
CHALLENGE_NAME_TO_ID = {
    "satisfiability": "c001",
    "vehicle_routing": "c002",
    "knapsack": "c003",
    "vector_search": "c004",
    "hypergraph": "c005",
    "neuralnet_optimizer": "c006",
    "job_scheduling": "c007",
    "energy_arbitrage": "c008",
}
_last_run_ts = 0.0
_decision_table_ready = False


def _capacity_profile_for_work(item: dict) -> str:
    challenge = item.get("challenge")
    challenge_id = item.get("challenge_id")
    slot_type = item.get("slot_type")
    if challenge in GPU_CHALLENGES or challenge_id in GPU_CHALLENGE_ID_TO_SLOT:
        return "gpu"
    return "gpu" if slot_type in GPU_SLOT_TYPES else "cpu"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _fetch_all(sql: str, params=None) -> list[dict]:
    try:
        return [_json_safe(r) for r in db.fetch_all(sql, params)]
    except Exception as exc:
        logger.warning("autopilot query failed: %s", exc)
        return []


def _fetch_one(sql: str, params=None) -> dict:
    try:
        return _json_safe(db.fetch_one(sql, params) or {})
    except Exception as exc:
        logger.warning("autopilot query failed: %s", exc)
        return {}


def _slave_profile(slave_name: str) -> str:
    if slave_name.startswith(("pool-gpu-", "c3-slave-")):
        return "gpu"
    return "cpu"


def _is_public_member_slave(slave_name: str) -> bool:
    return slave_name.startswith(("pool-cpu-", "pool-gpu-"))


def _counts_for_capacity(slave: dict) -> bool:
    name = slave.get("slave_name") or ""
    return bool(
        slave.get("active_now")
        and (
            slave.get("registered_active")
            or not _is_public_member_slave(name)
        )
    )


def _fetch_master_config() -> tuple[dict, str | None]:
    try:
        with urllib.request.urlopen(f"{MASTER_URL}/get-config", timeout=5) as resp:
            return json.loads(resp.read()), None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {}, str(exc)


def _push_config(cfg: dict):
    data = json.dumps(cfg).encode()
    req = urllib.request.Request(
        f"{MASTER_URL}/update-config",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=5)


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _active_unfinished_jobs() -> int:
    row = _fetch_one(
        """
        SELECT COUNT(*) AS count
        FROM job
        WHERE stopped IS NULL
          AND end_time IS NULL
          AND merkle_proofs_ready IS NULL
        """
    )
    return int(row.get("count") or 0)


def _stale_totals(now_ms: int) -> dict:
    roots = _fetch_one(
        """
        SELECT COUNT(*) AS count
        FROM root_batch rb
        JOIN job j ON j.benchmark_id = rb.benchmark_id
        WHERE rb.ready IS NULL
          AND rb.start_time IS NOT NULL
          AND rb.start_time < %s
          AND j.stopped IS NULL
          AND j.end_time IS NULL
        """,
        (now_ms - STALE_ROOT_MS,),
    )
    proofs = _fetch_one(
        """
        SELECT COUNT(*) AS count
        FROM proofs_batch pb
        JOIN job j ON j.benchmark_id = pb.benchmark_id
        WHERE pb.ready IS NULL
          AND pb.start_time IS NOT NULL
          AND pb.start_time < %s
          AND j.stopped IS NULL
          AND j.end_time IS NULL
        """,
        (now_ms - STALE_PROOF_MS,),
    )
    stale_roots = int(roots.get("count") or 0)
    stale_proofs = int(proofs.get("count") or 0)
    return {
        "roots": stale_roots,
        "proofs": stale_proofs,
        "combined": stale_roots + stale_proofs,
    }


def _ensure_decision_table():
    global _decision_table_ready
    if _decision_table_ready:
        return
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS autopilot_decisions (
            id BIGSERIAL PRIMARY KEY,
            mode TEXT NOT NULL,
            generated_at_ms BIGINT NOT NULL,
            clean_windows INTEGER NOT NULL,
            healthy BOOLEAN NOT NULL,
            applied BOOLEAN NOT NULL,
            reason TEXT,
            changes JSONB NOT NULL DEFAULT '{}'::JSONB,
            report JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    _decision_table_ready = True


def _save_decision(report: dict, decision: dict):
    _ensure_decision_table()
    db.execute(
        """
        INSERT INTO autopilot_decisions (
            mode, generated_at_ms, clean_windows, healthy, applied,
            reason, changes, report
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s::JSONB, %s::JSONB)
        """,
        (
            decision.get("mode", AUTOPILOT_MODE),
            report.get("generated_at_ms"),
            int(decision.get("clean_windows") or 0),
            bool(decision.get("healthy")),
            bool(decision.get("applied")),
            decision.get("reason"),
            json.dumps(decision.get("changes") or {}),
            json.dumps(report),
        ),
    )


def _retry_timeout_ms(cfg: dict, challenge: str) -> int:
    overrides = cfg.get("per_challenge_time_before_batch_retry", {}) or {}
    global_timeout = int(cfg.get("time_before_batch_retry") or STALE_CLEANUP_MIN_AGE_MS)
    challenge_id = str(challenge or "").split("_")[0]
    return int(overrides.get(challenge_id, global_timeout) or global_timeout)


def _cleanup_stale_assignments(cfg: dict, now_ms: int) -> dict:
    """Release stale assignments so eligible work can be retried.

    Cleanup uses the master's per-challenge retry timeout when available and a
    separate minimum age floor. This avoids cancelling slow-but-valid work too
    early on weaker CPUs.
    """
    result = {
        "enabled": STALE_CLEANUP_ENABLED,
        "precommit_expiry_enabled": PRECOMMIT_EXPIRY_CLEANUP_ENABLED,
        "released_roots": [],
        "released_orphan_roots": [],
        "released_proofs": [],
        "expiry_released_roots": [],
        "expiry_released_proofs": [],
        "stopped_precommits": [],
        "thresholds_ms": {
            "stale_root_cleanup": STALE_ROOT_CLEANUP_MIN_AGE_MS,
            "stale_proof_cleanup": STALE_PROOF_CLEANUP_MIN_AGE_MS,
            "precommit_root_reclaim": PRECOMMIT_ROOT_RECLAIM_AGE_MS,
            "precommit_proof_reclaim": PRECOMMIT_PROOF_RECLAIM_AGE_MS,
            "precommit_abandon_no_root": PRECOMMIT_ABANDON_NO_ROOT_AGE_MS,
        },
        "skipped": "",
    }
    if AUTOPILOT_MODE != "apply":
        result["skipped"] = "report_only"
        return result
    if not STALE_CLEANUP_ENABLED and not PRECOMMIT_EXPIRY_CLEANUP_ENABLED:
        result["skipped"] = "disabled"
        return result
    if not cfg:
        result["skipped"] = "missing_config"
        return result

    root_candidates = []
    if STALE_CLEANUP_ENABLED:
        root_candidates = _fetch_all(
            """
            SELECT
                rb.benchmark_id,
                rb.batch_idx,
                rb.slave,
                rb.start_time,
                rb.num_attempts,
                j.challenge,
                j.settings->>'track_id' AS track
            FROM root_batch rb
            JOIN job j ON j.benchmark_id = rb.benchmark_id
            WHERE rb.ready IS NULL
              AND rb.slave IS NOT NULL
              AND rb.start_time IS NOT NULL
              AND rb.start_time < %s
              AND j.stopped IS NULL
              AND j.end_time IS NULL
            ORDER BY rb.start_time
            LIMIT %s
            """,
            (now_ms - STALE_ROOT_CLEANUP_MIN_AGE_MS, STALE_CLEANUP_MAX_ROWS),
        )
    roots_to_release = []
    for row in root_candidates:
        age_ms = now_ms - int(row.get("start_time") or now_ms)
        timeout_ms = max(STALE_ROOT_CLEANUP_MIN_AGE_MS, _retry_timeout_ms(cfg, row.get("challenge")))
        if age_ms >= timeout_ms:
            roots_to_release.append(row)

    orphan_root_candidates = []
    if STALE_CLEANUP_ENABLED:
        orphan_root_candidates = _fetch_all(
            """
            SELECT
                rb.benchmark_id,
                rb.batch_idx,
                rb.start_time,
                rb.num_attempts,
                j.challenge,
                j.settings->>'track_id' AS track
            FROM root_batch rb
            JOIN job j ON j.benchmark_id = rb.benchmark_id
            WHERE rb.ready IS NULL
              AND rb.slave IS NULL
              AND rb.start_time IS NOT NULL
              AND rb.start_time < %s
              AND j.stopped IS NULL
              AND j.end_time IS NULL
            ORDER BY rb.start_time
            LIMIT %s
            """,
            (now_ms - STALE_ROOT_CLEANUP_MIN_AGE_MS, STALE_CLEANUP_MAX_ROWS),
        )
    orphan_roots_to_release = []
    for row in orphan_root_candidates:
        age_ms = now_ms - int(row.get("start_time") or now_ms)
        timeout_ms = max(STALE_ROOT_CLEANUP_MIN_AGE_MS, _retry_timeout_ms(cfg, row.get("challenge")))
        if age_ms >= timeout_ms:
            orphan_roots_to_release.append(row)

    proof_candidates = []
    if STALE_CLEANUP_ENABLED:
        proof_candidates = _fetch_all(
            """
            SELECT
                pb.benchmark_id,
                pb.batch_idx,
                pb.slave,
                pb.start_time,
                pb.num_attempts,
                j.challenge,
                j.settings->>'track_id' AS track
            FROM proofs_batch pb
            JOIN job j ON j.benchmark_id = pb.benchmark_id
            WHERE pb.ready IS NULL
              AND pb.start_time IS NOT NULL
              AND pb.start_time < %s
              AND j.stopped IS NULL
              AND j.end_time IS NULL
            ORDER BY pb.start_time
            LIMIT %s
            """,
            (now_ms - STALE_PROOF_CLEANUP_MIN_AGE_MS, STALE_CLEANUP_MAX_ROWS),
        )
    proofs_to_release = []
    for row in proof_candidates:
        age_ms = now_ms - int(row.get("start_time") or now_ms)
        timeout_ms = STALE_PROOF_CLEANUP_MIN_AGE_MS
        if age_ms >= timeout_ms:
            proofs_to_release.append(row)

    expiry_roots_to_release = []
    expiry_proofs_to_release = []
    precommits_to_stop = []
    if PRECOMMIT_EXPIRY_CLEANUP_ENABLED:
        expiry_root_candidates = _fetch_all(
            """
            SELECT
                rb.benchmark_id,
                rb.batch_idx,
                rb.slave,
                rb.start_time,
                rb.num_attempts,
                j.start_time AS job_start_time,
                j.challenge,
                j.settings->>'track_id' AS track
            FROM root_batch rb
            JOIN job j ON j.benchmark_id = rb.benchmark_id
            WHERE rb.ready IS NULL
              AND rb.slave IS NOT NULL
              AND rb.start_time IS NOT NULL
              AND j.stopped IS NULL
              AND j.end_time IS NULL
              AND j.merkle_root_ready IS NULL
              AND j.start_time < %s
            ORDER BY j.start_time, rb.start_time
            LIMIT %s
            """,
            (now_ms - PRECOMMIT_ROOT_RECLAIM_AGE_MS, STALE_CLEANUP_MAX_ROWS),
        )
        stale_root_keys = {(row["benchmark_id"], row["batch_idx"]) for row in roots_to_release}
        for row in expiry_root_candidates:
            key = (row["benchmark_id"], row["batch_idx"])
            if key not in stale_root_keys:
                expiry_roots_to_release.append(row)

        expiry_proof_candidates = _fetch_all(
            """
            SELECT
                pb.benchmark_id,
                pb.batch_idx,
                pb.slave,
                pb.start_time,
                pb.num_attempts,
                j.start_time AS job_start_time,
                j.challenge,
                j.settings->>'track_id' AS track
            FROM proofs_batch pb
            JOIN job j ON j.benchmark_id = pb.benchmark_id
            WHERE pb.ready IS NULL
              AND pb.slave IS NOT NULL
              AND pb.start_time IS NOT NULL
              AND j.stopped IS NULL
              AND j.end_time IS NULL
              AND j.start_time < %s
            ORDER BY j.start_time, pb.start_time
            LIMIT %s
            """,
            (now_ms - PRECOMMIT_PROOF_RECLAIM_AGE_MS, STALE_CLEANUP_MAX_ROWS),
        )
        stale_proof_keys = {(row["benchmark_id"], row["batch_idx"]) for row in proofs_to_release}
        for row in expiry_proof_candidates:
            key = (row["benchmark_id"], row["batch_idx"])
            if key not in stale_proof_keys:
                expiry_proofs_to_release.append(row)

        precommits_to_stop = _fetch_all(
            """
            SELECT
                j.benchmark_id,
                j.start_time AS job_start_time,
                j.challenge,
                j.settings->>'algorithm_id' AS algorithm_id,
                j.settings->>'track_id' AS track,
                COUNT(rb.*) FILTER (WHERE rb.ready = true) AS roots_ready,
                COUNT(rb.*) FILTER (WHERE rb.ready IS NULL) AS roots_pending
            FROM job j
            JOIN root_batch rb ON rb.benchmark_id = j.benchmark_id
            WHERE j.stopped IS NULL
              AND j.end_time IS NULL
              AND j.merkle_root_ready IS NULL
              AND j.start_time < %s
            GROUP BY j.benchmark_id, j.start_time, j.challenge, j.settings
            HAVING COUNT(rb.*) FILTER (WHERE rb.ready = true) = 0
            ORDER BY j.start_time
            LIMIT %s
            """,
            (now_ms - PRECOMMIT_ABANDON_NO_ROOT_AGE_MS, STALE_CLEANUP_MAX_ROWS),
        )

    queries = []
    for row in roots_to_release:
        queries.append((
            """
            UPDATE root_batch
            SET slave = NULL,
                start_time = NULL,
                end_time = NULL
            WHERE benchmark_id = %s
              AND batch_idx = %s
              AND ready IS NULL
            """,
            (row["benchmark_id"], row["batch_idx"]),
        ))
        result["released_roots"].append({
            "benchmark": str(row["benchmark_id"])[:10],
            "batch_idx": row["batch_idx"],
            "slave": row["slave"],
            "challenge": row["challenge"],
            "track": row["track"],
            "age_min": round((now_ms - int(row["start_time"])) / 60000.0, 1),
        })

    for row in orphan_roots_to_release:
        queries.append((
            """
            UPDATE root_batch
            SET start_time = NULL,
                end_time = NULL
            WHERE benchmark_id = %s
              AND batch_idx = %s
              AND ready IS NULL
              AND slave IS NULL
            """,
            (row["benchmark_id"], row["batch_idx"]),
        ))
        result["released_orphan_roots"].append({
            "benchmark": str(row["benchmark_id"])[:10],
            "batch_idx": row["batch_idx"],
            "slave": None,
            "challenge": row["challenge"],
            "track": row["track"],
            "age_min": round((now_ms - int(row["start_time"])) / 60000.0, 1),
        })

    for row in expiry_roots_to_release:
        queries.append((
            """
            UPDATE root_batch
            SET slave = NULL,
                start_time = NULL,
                end_time = NULL
            WHERE benchmark_id = %s
              AND batch_idx = %s
              AND ready IS NULL
            """,
            (row["benchmark_id"], row["batch_idx"]),
        ))
        result["expiry_released_roots"].append({
            "benchmark": str(row["benchmark_id"])[:10],
            "batch_idx": row["batch_idx"],
            "slave": row["slave"],
            "challenge": row["challenge"],
            "track": row["track"],
            "job_age_min": round((now_ms - int(row["job_start_time"])) / 60000.0, 1),
            "assignment_age_min": round((now_ms - int(row["start_time"])) / 60000.0, 1),
            "reason": "precommit_root_reclaim_age_exceeded",
        })

    for row in proofs_to_release:
        queries.append((
            """
            UPDATE proofs_batch
            SET slave = NULL,
                start_time = NULL,
                end_time = NULL
            WHERE benchmark_id = %s
              AND batch_idx = %s
              AND ready IS NULL
            """,
            (row["benchmark_id"], row["batch_idx"]),
        ))
        result["released_proofs"].append({
            "benchmark": str(row["benchmark_id"])[:10],
            "batch_idx": row["batch_idx"],
            "slave": row["slave"],
            "challenge": row["challenge"],
            "track": row["track"],
            "age_min": round((now_ms - int(row["start_time"])) / 60000.0, 1),
        })

    for row in expiry_proofs_to_release:
        queries.append((
            """
            UPDATE proofs_batch
            SET slave = NULL,
                start_time = NULL,
                end_time = NULL
            WHERE benchmark_id = %s
              AND batch_idx = %s
              AND ready IS NULL
            """,
            (row["benchmark_id"], row["batch_idx"]),
        ))
        result["expiry_released_proofs"].append({
            "benchmark": str(row["benchmark_id"])[:10],
            "batch_idx": row["batch_idx"],
            "slave": row["slave"],
            "challenge": row["challenge"],
            "track": row["track"],
            "job_age_min": round((now_ms - int(row["job_start_time"])) / 60000.0, 1),
            "assignment_age_min": round((now_ms - int(row["start_time"])) / 60000.0, 1),
            "reason": "precommit_proof_reclaim_age_exceeded",
        })

    for row in precommits_to_stop:
        queries.extend([
            (
                """
                UPDATE job
                SET stopped = true,
                    end_time = %s
                WHERE benchmark_id = %s
                  AND stopped IS NULL
                  AND end_time IS NULL
                  AND merkle_root_ready IS NULL
                """,
                (now_ms, row["benchmark_id"]),
            ),
            (
                """
                UPDATE root_batch
                SET slave = NULL,
                    start_time = NULL,
                    end_time = NULL
                WHERE benchmark_id = %s
                  AND ready IS NULL
                """,
                (row["benchmark_id"],),
            ),
            (
                """
                UPDATE proofs_batch
                SET slave = NULL,
                    start_time = NULL,
                    end_time = NULL
                WHERE benchmark_id = %s
                  AND ready IS NULL
                """,
                (row["benchmark_id"],),
            ),
            (
                """
                UPDATE benchmark_slot
                SET benchmark_id = NULL,
                    challenge = NULL,
                    algorithm_id = NULL,
                    track_id = NULL,
                    assigned_at = NULL,
                    last_activity_at = NULL,
                    state = 'idle'
                WHERE benchmark_id = %s
                """,
                (row["benchmark_id"],),
            ),
        ])
        result["stopped_precommits"].append({
            "benchmark": str(row["benchmark_id"])[:10],
            "challenge": row["challenge"],
            "algorithm_id": row["algorithm_id"],
            "track": row["track"],
            "job_age_min": round((now_ms - int(row["job_start_time"])) / 60000.0, 1),
            "roots_ready": int(row.get("roots_ready") or 0),
            "roots_pending": int(row.get("roots_pending") or 0),
            "reason": "precommit_no_root_progress_abandon_age_exceeded",
        })

    if queries:
        db.execute_many(*queries)
    return result


def _current_config_summary(cfg: dict) -> dict:
    return {
        "max_concurrent_benchmarks": cfg.get("max_concurrent_benchmarks"),
        "max_job_batches": cfg.get("max_job_batches"),
        "max_batches_per_benchmark": cfg.get("max_batches_per_benchmark"),
        "per_challenge_max_benchmarks": cfg.get("per_challenge_max_benchmarks", {}),
        "resource_slots": cfg.get("resource_slots", {}),
        "adaptive_slave_caps": cfg.get("adaptive_slave_caps", {}),
        "slaves": cfg.get("slaves", []),
    }


def _slave_metrics(now_ms: int) -> list[dict]:
    cutoff_active = now_ms - ACTIVE_WINDOW_MS
    cutoff_metrics = now_ms - METRIC_WINDOW_MS
    rows = _fetch_all(
        """
        WITH registered AS (
            SELECT slave_name, wallet_address, active
            FROM pool_members
        ),
        root_stats AS (
            SELECT
                rb.slave AS slave_name,
                MAX(rb.start_time) AS last_assigned_at,
                MAX(rb.end_time) FILTER (WHERE rb.ready = true) AS last_completed_at,
                COUNT(*) FILTER (WHERE rb.start_time >= %s) AS assigned_recent,
                COUNT(*) FILTER (WHERE rb.ready = true AND rb.end_time >= %s) AS completed_recent,
                COUNT(*) FILTER (WHERE rb.ready IS NULL AND rb.start_time IS NOT NULL) AS active_unfinished,
                COUNT(*) FILTER (
                    WHERE rb.ready IS NULL
                      AND rb.start_time IS NOT NULL
                      AND rb.start_time < %s
                ) AS stale_roots,
                COUNT(*) FILTER (WHERE rb.ready = false AND rb.end_time >= %s) AS failed_recent,
                COALESCE(SUM(LEAST(j.batch_size, j.num_nonces - rb.batch_idx * j.batch_size)) FILTER (
                    WHERE rb.ready = true AND rb.end_time >= %s
                ), 0) AS nonces_recent,
                ROUND(AVG(rb.end_time - rb.start_time) FILTER (
                    WHERE rb.ready = true AND rb.end_time >= %s AND rb.end_time IS NOT NULL
                ) / 1000.0, 1) AS avg_runtime_sec
            FROM root_batch rb
            JOIN job j ON j.benchmark_id = rb.benchmark_id
            WHERE rb.slave IS NOT NULL
            GROUP BY rb.slave
        ),
        proof_stats AS (
            SELECT
                slave AS slave_name,
                COUNT(*) FILTER (WHERE ready IS NULL AND start_time IS NOT NULL) AS active_proofs,
                COUNT(*) FILTER (WHERE ready = true AND end_time >= %s) AS proofs_completed_recent,
                COUNT(*) FILTER (
                    WHERE ready IS NULL
                      AND start_time IS NOT NULL
                      AND start_time < %s
                ) AS stale_proofs,
                ROUND(AVG(end_time - start_time) FILTER (
                    WHERE ready = true AND end_time >= %s AND end_time IS NOT NULL
                ) / 1000.0, 1) AS avg_proof_runtime_sec
            FROM proofs_batch
            WHERE slave IS NOT NULL
            GROUP BY slave
        )
        SELECT
            COALESCE(r.slave_name, rs.slave_name, ps.slave_name) AS slave_name,
            r.wallet_address,
            r.slave_name IS NOT NULL AS registered,
            COALESCE(r.active, false) AS registered_active,
            COALESCE(rs.assigned_recent, 0) AS assigned_recent,
            COALESCE(rs.completed_recent, 0) AS completed_recent,
            COALESCE(rs.active_unfinished, 0) AS active_unfinished,
            COALESCE(ps.active_proofs, 0) AS active_proofs,
            COALESCE(ps.proofs_completed_recent, 0) AS proofs_completed_recent,
            COALESCE(rs.stale_roots, 0) AS stale_roots,
            COALESCE(ps.stale_proofs, 0) AS stale_proofs,
            COALESCE(rs.failed_recent, 0) AS failed_recent,
            COALESCE(rs.nonces_recent, 0) AS nonces_recent,
            rs.avg_runtime_sec,
            ps.avg_proof_runtime_sec,
            rs.last_assigned_at,
            rs.last_completed_at
        FROM registered r
        FULL OUTER JOIN root_stats rs ON rs.slave_name = r.slave_name
        FULL OUTER JOIN proof_stats ps ON ps.slave_name = COALESCE(r.slave_name, rs.slave_name)
        WHERE COALESCE(r.slave_name, rs.slave_name, ps.slave_name) IS NOT NULL
        ORDER BY COALESCE(rs.nonces_recent, 0) DESC, COALESCE(rs.last_assigned_at, 0) DESC
        """,
        (
            cutoff_metrics,
            cutoff_metrics,
            now_ms - STALE_ROOT_MS,
            cutoff_metrics,
            cutoff_metrics,
            cutoff_metrics,
            cutoff_metrics,
            now_ms - STALE_PROOF_MS,
            cutoff_metrics,
        ),
    )

    out = []
    for row in rows:
        last_activity = max(
            int(row.get("last_assigned_at") or 0),
            int(row.get("last_completed_at") or 0),
        )
        row["profile"] = _slave_profile(row["slave_name"])
        row["active_now"] = last_activity >= cutoff_active or int(row.get("active_unfinished") or 0) > 0
        row["idle_for_min"] = round((now_ms - last_activity) / 60000.0, 1) if last_activity else None
        out.append(row)
    return out


def _challenge_metrics(now_ms: int) -> list[dict]:
    cutoff_metrics = now_ms - METRIC_WINDOW_MS
    return _fetch_all(
        """
        SELECT
            j.challenge,
            j.algorithm,
            j.settings->>'track_id' AS track,
            COUNT(DISTINCT j.benchmark_id) AS active_benchmarks,
            COUNT(rb.*) AS root_batches,
            COUNT(rb.*) FILTER (WHERE rb.ready = true) AS roots_ready,
            COUNT(rb.*) FILTER (WHERE rb.ready IS NULL) AS roots_pending,
            COUNT(rb.*) FILTER (WHERE rb.ready IS NULL AND rb.start_time IS NOT NULL) AS roots_inflight,
            COUNT(rb.*) FILTER (
                WHERE rb.ready IS NULL
                  AND rb.start_time IS NOT NULL
                  AND rb.start_time < %s
            ) AS stale_roots,
            COUNT(pb.*) AS proof_batches,
            COUNT(pb.*) FILTER (WHERE pb.ready = true) AS proofs_ready,
            COUNT(pb.*) FILTER (WHERE pb.ready IS NULL) AS proofs_pending,
            COUNT(pb.*) FILTER (WHERE pb.ready = true AND pb.end_time >= %s) AS proofs_done_recent,
            COUNT(pb.*) FILTER (
                WHERE pb.ready IS NULL
                  AND pb.start_time IS NOT NULL
                  AND pb.start_time < %s
            ) AS stale_proofs,
            COUNT(rb.*) FILTER (WHERE rb.ready = true AND rb.end_time >= %s) AS roots_done_recent,
            ROUND(AVG(rb.end_time - rb.start_time) FILTER (
                WHERE rb.ready = true AND rb.end_time >= %s AND rb.end_time IS NOT NULL
            ) / 1000.0, 1) AS avg_root_runtime_sec,
            ROUND(AVG(pb.end_time - pb.start_time) FILTER (
                WHERE pb.ready = true AND pb.end_time >= %s AND pb.end_time IS NOT NULL
            ) / 1000.0, 1) AS avg_proof_runtime_sec
        FROM job j
        LEFT JOIN root_batch rb ON rb.benchmark_id = j.benchmark_id
        LEFT JOIN proofs_batch pb
          ON pb.benchmark_id = j.benchmark_id
         AND pb.batch_idx = rb.batch_idx
        WHERE j.stopped IS NULL
          AND j.end_time IS NULL
        GROUP BY j.challenge, j.algorithm, j.settings
        ORDER BY j.challenge, track
        """,
        (
            now_ms - STALE_ROOT_MS,
            cutoff_metrics,
            now_ms - STALE_PROOF_MS,
            cutoff_metrics,
            cutoff_metrics,
            cutoff_metrics,
        ),
    )


def _safe_div(numerator: int | float | None, denominator: int | float | None) -> float | None:
    if denominator in (None, 0):
        return None
    return round(float(numerator or 0) / float(denominator), 4)


def _reward_funnel_summary(now_ms: int) -> dict:
    cutoff_metrics = now_ms - METRIC_WINDOW_MS
    cfg, _cfg_error = _fetch_master_config()
    track_allowlist = cfg.get("track_allowlist", {}) if cfg else {}
    total = _fetch_one(
        """
        WITH job_base AS (
            SELECT
                j.*,
                j.settings->>'algorithm_id' AS algorithm_id,
                j.settings->>'track_id' AS track
            FROM job j
            WHERE j.start_time >= %s
               OR j.benchmark_submit_time >= %s
               OR j.proof_submit_time >= %s
               OR j.end_time >= %s
               OR j.end_time IS NULL
        ),
        root_agg AS (
            SELECT
                benchmark_id,
                COUNT(*) AS root_batches,
                COUNT(*) FILTER (WHERE ready = true) AS roots_ready,
                COUNT(*) FILTER (WHERE ready IS NULL) AS roots_pending,
                COUNT(*) FILTER (WHERE ready = false) AS roots_failed,
                CASE
                    WHEN COUNT(*) > 0 AND COUNT(*) = COUNT(*) FILTER (WHERE ready = true)
                    THEN MAX(end_time) FILTER (WHERE ready = true)
                    ELSE NULL
                END AS all_roots_ready_at,
                PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY end_time - start_time)
                    FILTER (WHERE ready = true AND end_time >= %s AND end_time IS NOT NULL) AS root_runtime_p95_ms
            FROM root_batch
            GROUP BY benchmark_id
        ),
        proof_agg AS (
            SELECT
                benchmark_id,
                COUNT(*) AS proof_batches,
                COUNT(*) FILTER (WHERE ready = true) AS proofs_ready,
                COUNT(*) FILTER (WHERE ready IS NULL) AS proofs_pending,
                COUNT(*) FILTER (WHERE ready = false) AS proofs_failed,
                MAX(end_time) FILTER (WHERE ready = true) AS all_proofs_ready_at,
                PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY end_time - start_time)
                    FILTER (WHERE ready = true AND end_time >= %s AND end_time IS NOT NULL) AS proof_runtime_p95_ms
            FROM proofs_batch
            GROUP BY benchmark_id
        )
        SELECT
            COUNT(*) AS benchmarks_seen,
            COUNT(*) FILTER (WHERE jb.stopped IS NULL AND jb.end_time IS NULL) AS active_benchmarks,
            COUNT(*) FILTER (WHERE jb.stopped = true) AS stopped_benchmarks,
            COUNT(*) FILTER (WHERE jb.stopped = true AND COALESCE(ra.root_batches, 0) = 0) AS stopped_without_roots,
            COUNT(*) FILTER (WHERE jb.merkle_root_ready = true) AS root_ready_benchmarks,
            COUNT(*) FILTER (WHERE jb.benchmark_submit_time IS NOT NULL) AS benchmark_submit_attempted,
            COUNT(*) FILTER (WHERE jb.benchmark_submitted = true) AS benchmark_submitted_confirmed,
            COUNT(*) FILTER (WHERE jb.sampled_nonces IS NOT NULL) AS sampled_benchmarks,
            COUNT(*) FILTER (WHERE COALESCE(pa.proof_batches, 0) > 0) AS proof_required_benchmarks,
            COUNT(*) FILTER (WHERE jb.merkle_proofs_ready = true) AS proof_ready_benchmarks,
            COUNT(*) FILTER (WHERE jb.proof_submit_time IS NOT NULL) AS proof_submit_attempted,
            COUNT(*) FILTER (WHERE jb.proof_submitted = true) AS proof_submitted_confirmed,
            COALESCE(SUM(jb.num_nonces), 0) AS nonces_seen,
            COALESCE(SUM(jb.num_batches), 0) AS root_batches_expected,
            COALESCE(SUM(ra.root_batches), 0) AS root_batches_seen,
            COALESCE(SUM(ra.roots_ready), 0) AS roots_ready,
            COALESCE(SUM(ra.roots_pending), 0) AS roots_pending,
            COALESCE(SUM(ra.roots_failed), 0) AS roots_failed,
            COALESCE(SUM(pa.proof_batches), 0) AS proof_batches_seen,
            COALESCE(SUM(pa.proofs_ready), 0) AS proofs_ready,
            COALESCE(SUM(pa.proofs_pending), 0) AS proofs_pending,
            COALESCE(SUM(pa.proofs_failed), 0) AS proofs_failed,
            ROUND(AVG(ra.all_roots_ready_at - jb.start_time) FILTER (
                WHERE ra.all_roots_ready_at IS NOT NULL AND jb.start_time IS NOT NULL
            ) / 1000.0, 1) AS avg_root_phase_sec,
            ROUND(AVG(pa.all_proofs_ready_at - jb.benchmark_submit_time) FILTER (
                WHERE pa.all_proofs_ready_at IS NOT NULL AND jb.benchmark_submit_time IS NOT NULL
            ) / 1000.0, 1) AS avg_proof_phase_sec,
            ROUND(AVG(jb.proof_submit_time - jb.start_time) FILTER (
                WHERE jb.proof_submit_time IS NOT NULL AND jb.start_time IS NOT NULL
            ) / 1000.0, 1) AS avg_time_to_proof_submit_sec,
            ROUND((AVG(ra.root_runtime_p95_ms) / 1000.0)::numeric, 1) AS p95_root_batch_runtime_sec,
            ROUND((AVG(pa.proof_runtime_p95_ms) / 1000.0)::numeric, 1) AS p95_proof_batch_runtime_sec
        FROM job_base jb
        LEFT JOIN root_agg ra ON ra.benchmark_id = jb.benchmark_id
        LEFT JOIN proof_agg pa ON pa.benchmark_id = jb.benchmark_id
        """,
        (
            cutoff_metrics,
            cutoff_metrics,
            cutoff_metrics,
            cutoff_metrics,
            cutoff_metrics,
            cutoff_metrics,
        ),
    )
    by_track = _fetch_all(
        """
        WITH job_base AS (
            SELECT
                j.*,
                j.settings->>'algorithm_id' AS algorithm_id,
                j.settings->>'track_id' AS track
            FROM job j
            WHERE j.start_time >= %s
               OR j.benchmark_submit_time >= %s
               OR j.proof_submit_time >= %s
               OR j.end_time >= %s
               OR j.end_time IS NULL
        ),
        root_agg AS (
            SELECT
                benchmark_id,
                COUNT(*) AS root_batches,
                COUNT(*) FILTER (WHERE ready = true) AS roots_ready,
                COUNT(*) FILTER (WHERE ready IS NULL) AS roots_pending,
                CASE
                    WHEN COUNT(*) > 0 AND COUNT(*) = COUNT(*) FILTER (WHERE ready = true)
                    THEN MAX(end_time) FILTER (WHERE ready = true)
                    ELSE NULL
                END AS all_roots_ready_at,
                PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY end_time - start_time)
                    FILTER (WHERE ready = true AND end_time >= %s AND end_time IS NOT NULL) AS root_runtime_p95_ms
            FROM root_batch
            GROUP BY benchmark_id
        ),
        proof_agg AS (
            SELECT
                benchmark_id,
                COUNT(*) AS proof_batches,
                COUNT(*) FILTER (WHERE ready = true) AS proofs_ready,
                COUNT(*) FILTER (WHERE ready IS NULL) AS proofs_pending,
                MAX(end_time) FILTER (WHERE ready = true) AS all_proofs_ready_at
            FROM proofs_batch
            GROUP BY benchmark_id
        )
        SELECT
            jb.challenge,
            jb.algorithm_id,
            jb.track,
            COUNT(*) AS benchmarks_seen,
            COUNT(*) FILTER (WHERE jb.stopped IS NULL AND jb.end_time IS NULL) AS active_benchmarks,
            COUNT(*) FILTER (WHERE jb.stopped = true) AS stopped_benchmarks,
            COUNT(*) FILTER (WHERE jb.stopped = true AND COALESCE(ra.root_batches, 0) = 0) AS stopped_without_roots,
            COUNT(*) FILTER (WHERE jb.merkle_root_ready = true) AS root_ready_benchmarks,
            COUNT(*) FILTER (WHERE jb.benchmark_submitted = true) AS benchmark_submitted_confirmed,
            COUNT(*) FILTER (WHERE jb.sampled_nonces IS NOT NULL) AS sampled_benchmarks,
            COUNT(*) FILTER (WHERE COALESCE(pa.proof_batches, 0) > 0) AS proof_required_benchmarks,
            COUNT(*) FILTER (WHERE jb.merkle_proofs_ready = true) AS proof_ready_benchmarks,
            COUNT(*) FILTER (WHERE jb.proof_submitted = true) AS proof_submitted_confirmed,
            ROUND(AVG(jb.num_nonces)::numeric, 1) AS avg_num_nonces,
            ROUND(AVG(jb.num_batches)::numeric, 1) AS avg_num_batches,
            ROUND(AVG(jb.batch_size)::numeric, 1) AS avg_batch_size,
            COALESCE(SUM(ra.roots_ready), 0) AS roots_ready,
            COALESCE(SUM(ra.roots_pending), 0) AS roots_pending,
            COALESCE(SUM(pa.proofs_ready), 0) AS proofs_ready,
            COALESCE(SUM(pa.proofs_pending), 0) AS proofs_pending,
            ROUND(AVG(ra.all_roots_ready_at - jb.start_time) FILTER (
                WHERE ra.all_roots_ready_at IS NOT NULL AND jb.start_time IS NOT NULL
            ) / 1000.0, 1) AS avg_root_phase_sec,
            ROUND(AVG(pa.all_proofs_ready_at - jb.benchmark_submit_time) FILTER (
                WHERE pa.all_proofs_ready_at IS NOT NULL AND jb.benchmark_submit_time IS NOT NULL
            ) / 1000.0, 1) AS avg_proof_phase_sec,
            ROUND(AVG(jb.proof_submit_time - jb.start_time) FILTER (
                WHERE jb.proof_submit_time IS NOT NULL AND jb.start_time IS NOT NULL
            ) / 1000.0, 1) AS avg_time_to_proof_submit_sec,
            ROUND((AVG(ra.root_runtime_p95_ms) / 1000.0)::numeric, 1) AS p95_root_batch_runtime_sec
        FROM job_base jb
        LEFT JOIN root_agg ra ON ra.benchmark_id = jb.benchmark_id
        LEFT JOIN proof_agg pa ON pa.benchmark_id = jb.benchmark_id
        GROUP BY jb.challenge, jb.algorithm_id, jb.track
        ORDER BY jb.challenge, jb.algorithm_id, jb.track
        """,
        (
            cutoff_metrics,
            cutoff_metrics,
            cutoff_metrics,
            cutoff_metrics,
            cutoff_metrics,
        ),
    )
    for row in by_track:
        seen = int(row.get("benchmarks_seen") or 0)
        root_ready = int(row.get("root_ready_benchmarks") or 0)
        proof_required = int(row.get("proof_required_benchmarks") or 0)
        proof_submitted = int(row.get("proof_submitted_confirmed") or 0)
        stopped = int(row.get("stopped_benchmarks") or 0)
        stopped_without_roots = int(row.get("stopped_without_roots") or 0)
        allowed_tracks = track_allowlist.get(row.get("challenge"))
        allowlist_blocked = bool(allowed_tracks) and row.get("track") not in allowed_tracks
        row["allowlist_blocked"] = allowlist_blocked
        row["intentional_stopped_without_roots"] = stopped_without_roots if allowlist_blocked else 0
        row["unexpected_stopped_without_roots"] = 0 if allowlist_blocked else stopped_without_roots
        row["root_ready_rate"] = _safe_div(root_ready, seen)
        row["proof_conversion_rate"] = _safe_div(proof_submitted, proof_required)
        row["stopped_rate"] = _safe_div(stopped, seen)
    total = dict(total or {})
    seen = int(total.get("benchmarks_seen") or 0)
    root_ready = int(total.get("root_ready_benchmarks") or 0)
    proof_required = int(total.get("proof_required_benchmarks") or 0)
    proof_submitted = int(total.get("proof_submitted_confirmed") or 0)
    proof_attempted = int(total.get("proof_submit_attempted") or 0)
    stopped = int(total.get("stopped_benchmarks") or 0)
    stopped_without_roots = int(total.get("stopped_without_roots") or 0)
    intentional_stopped_without_roots = sum(
        int(row.get("intentional_stopped_without_roots") or 0)
        for row in by_track
    )
    unexpected_stopped_without_roots = max(0, stopped_without_roots - intentional_stopped_without_roots)
    unexpected_stopped = max(0, stopped - intentional_stopped_without_roots)
    roots_pending = int(float(total.get("roots_pending") or 0))
    avg_time_to_proof = total.get("avg_time_to_proof_submit_sec")
    proof_conversion = _safe_div(proof_submitted, proof_required)
    proof_attempt_rate = _safe_div(proof_attempted, proof_required)
    stopped_rate = _safe_div(stopped, seen)
    unexpected_stopped_rate = _safe_div(unexpected_stopped, seen)
    issues = []
    if seen >= 5 and proof_required == 0:
        issues.append("warming_up_no_proof_samples")
    if seen >= 5 and root_ready == 0 and roots_pending > 0:
        issues.append("root_phase_not_complete")
    if proof_required and (proof_conversion or 0.0) < FUNNEL_MIN_PROOF_CONVERSION_RATE:
        issues.append("low_proof_conversion")
    if unexpected_stopped_rate is not None and unexpected_stopped_rate > FUNNEL_MAX_STOPPED_OR_EXPIRED_RATE:
        issues.append("high_stopped_or_expired_rate")
    if unexpected_stopped_without_roots:
        issues.append("stopped_without_root_work")
    if avg_time_to_proof is not None and float(avg_time_to_proof) > FUNNEL_TARGET_PROOF_SUBMIT_SEC:
        issues.append("slow_time_to_proof_submission")
    return {
        "window_ms": METRIC_WINDOW_MS,
        "targets": {
            "proof_submit_sec": FUNNEL_TARGET_PROOF_SUBMIT_SEC,
            "min_proof_conversion_rate": FUNNEL_MIN_PROOF_CONVERSION_RATE,
            "max_stopped_or_expired_rate": FUNNEL_MAX_STOPPED_OR_EXPIRED_RATE,
        },
        "summary": {
            **total,
            "root_ready_rate": _safe_div(root_ready, seen),
            "proof_conversion_rate": proof_conversion,
            "proof_submit_attempt_rate": proof_attempt_rate,
            "stopped_rate": stopped_rate,
            "unexpected_stopped_rate": unexpected_stopped_rate,
            "intentional_stopped_without_roots": intentional_stopped_without_roots,
            "unexpected_stopped_without_roots": unexpected_stopped_without_roots,
            "safe_to_scale_workload": not issues,
            "issues": issues,
        },
        "by_track": by_track,
    }


def _track_workload_metrics(now_ms: int) -> list[dict]:
    cutoff_metrics = now_ms - METRIC_WINDOW_MS
    return _fetch_all(
        """
        SELECT
            j.challenge,
            j.settings->>'algorithm_id' AS algorithm_id,
            j.settings->>'track_id' AS track,
            COUNT(DISTINCT j.benchmark_id) AS benchmarks_seen,
            COUNT(DISTINCT j.benchmark_id) FILTER (
                WHERE j.stopped IS NULL AND j.end_time IS NULL
            ) AS active_benchmarks,
            ROUND(AVG(j.num_nonces)::numeric, 1) AS avg_num_nonces,
            ROUND(AVG(j.num_batches)::numeric, 1) AS avg_num_batches,
            ROUND(AVG(j.batch_size)::numeric, 1) AS avg_batch_size,
            COUNT(rb.*) AS root_batches_seen,
            COUNT(rb.*) FILTER (WHERE rb.ready = true) AS roots_ready,
            COUNT(rb.*) FILTER (WHERE rb.ready IS NULL) AS roots_pending,
            COUNT(rb.*) FILTER (
                WHERE rb.ready IS NULL
                  AND rb.start_time IS NOT NULL
                  AND rb.start_time < %s
            ) AS stale_roots,
            COUNT(pb.*) AS proof_batches_seen,
            COUNT(pb.*) FILTER (WHERE pb.ready = true) AS proofs_ready,
            COUNT(pb.*) FILTER (WHERE pb.ready IS NULL) AS proofs_pending,
            ROUND(AVG(rb.end_time - rb.start_time) FILTER (
                WHERE rb.ready = true
                  AND rb.end_time >= %s
                  AND rb.end_time IS NOT NULL
            ) / 1000.0, 1) AS avg_root_runtime_sec,
            ROUND(AVG(j.end_time - j.start_time) FILTER (
                WHERE j.end_time >= %s
                  AND j.end_time IS NOT NULL
                  AND j.start_time IS NOT NULL
            ) / 1000.0, 1) AS avg_benchmark_wall_sec
        FROM job j
        LEFT JOIN root_batch rb ON rb.benchmark_id = j.benchmark_id
        LEFT JOIN proofs_batch pb
          ON pb.benchmark_id = j.benchmark_id
         AND pb.batch_idx = rb.batch_idx
        WHERE j.start_time >= %s
           OR j.end_time IS NULL
        GROUP BY j.challenge, j.settings->>'algorithm_id', j.settings->>'track_id'
        ORDER BY j.challenge, algorithm_id, track
        """,
        (now_ms - STALE_ROOT_MS, cutoff_metrics, cutoff_metrics, cutoff_metrics),
    )


def _track_config_economics(cfg: dict, workload: list[dict]) -> list[dict]:
    metrics = {
        (row.get("algorithm_id"), row.get("track")): row
        for row in workload
    }
    out = []
    for algo in cfg.get("algo_selection") or []:
        algorithm_id = algo.get("algorithm_id")
        challenge_id = str(algorithm_id or "").split("_")[0]
        algo_batch_size = int(algo.get("batch_size") or 1)
        for track, settings in (algo.get("track_settings") or {}).items():
            settings = settings or {}
            configured_batch_size = int(settings.get("batch_size") or algo_batch_size or 1)
            configured_bundles = int(settings.get("num_bundles") or 0)
            row = dict(metrics.get((algorithm_id, track), {}))
            avg_num_nonces = row.get("avg_num_nonces")
            avg_num_batches = row.get("avg_num_batches")
            avg_root_runtime = row.get("avg_root_runtime_sec")
            nonces_per_bundle = None
            estimated_root_batches = None
            estimated_benchmark_root_runtime_sec = None
            if configured_bundles > 0 and avg_num_nonces is not None:
                nonces_per_bundle = round(float(avg_num_nonces) / configured_bundles, 1)
            if nonces_per_bundle is not None and configured_batch_size > 0:
                estimated_root_batches = int(math.ceil((nonces_per_bundle * configured_bundles) / configured_batch_size))
            elif avg_num_batches is not None:
                estimated_root_batches = int(math.ceil(float(avg_num_batches)))
            if estimated_root_batches is not None and avg_root_runtime is not None:
                estimated_benchmark_root_runtime_sec = round(estimated_root_batches * float(avg_root_runtime), 1)

            notes = []
            if estimated_root_batches is not None:
                if estimated_root_batches > BUNDLE_TARGET_MAX_ROOT_BATCHES:
                    notes.append("too_many_root_batches_for_single_benchmark")
                elif estimated_root_batches < BUNDLE_TARGET_MIN_ROOT_BATCHES and configured_bundles > 1:
                    notes.append("coarse_root_batch_granularity_check_runtime_before_changing_bundles")
            if avg_root_runtime is not None:
                if float(avg_root_runtime) > BUNDLE_TARGET_ROOT_RUNTIME_SEC:
                    notes.append("root_batch_runtime_ties_worker_too_long")
                elif float(avg_root_runtime) < max(30, BUNDLE_TARGET_ROOT_RUNTIME_SEC // 6):
                    notes.append("root_batch_runtime_short_enough_for_larger_batches")

            out.append({
                "challenge_id": challenge_id,
                "algorithm_id": algorithm_id,
                "track": track,
                "configured": {
                    "weight": algo.get("weight"),
                    "algo_batch_size": algo_batch_size,
                    "track_batch_size": settings.get("batch_size"),
                    "effective_batch_size": configured_batch_size,
                    "num_bundles": configured_bundles,
                    "fuel_budget": settings.get("fuel_budget"),
                    "hyperparameters": settings.get("hyperparameters"),
                },
                "observed": row,
                "derived": {
                    "estimated_nonces_per_bundle": nonces_per_bundle,
                    "estimated_root_batches": estimated_root_batches,
                    "estimated_benchmark_root_runtime_sec": estimated_benchmark_root_runtime_sec,
                    "root_batches_per_bundle": (
                        round(estimated_root_batches / configured_bundles, 2)
                        if estimated_root_batches is not None and configured_bundles > 0
                        else None
                    ),
                },
                "efficiency_notes": notes,
            })
    return out


def _previous_power_of_two(value: int) -> int:
    value = max(1, int(value or 1))
    return 1 << (value.bit_length() - 1)


def _next_power_of_two(value: int) -> int:
    value = max(1, int(value or 1))
    if value & (value - 1) == 0:
        return value
    return 1 << value.bit_length()


def _workload_confidence(funnel: dict, observed: dict) -> dict:
    samples = int(funnel.get("benchmarks_seen") or observed.get("benchmarks_seen") or 0)
    proof_required = int(funnel.get("proof_required_benchmarks") or 0)
    root_batches = int(observed.get("root_batches_seen") or 0)
    has_proof_rate = funnel.get("proof_conversion_rate") is not None
    has_stopped_rate = funnel.get("stopped_rate") is not None
    has_time_to_proof = funnel.get("avg_time_to_proof_submit_sec") is not None

    score = 0.0
    score += min(0.30, samples / 50.0)
    score += min(0.25, proof_required / 40.0)
    score += min(0.15, root_batches / 500.0)
    if has_proof_rate:
        score += 0.15
    if has_stopped_rate:
        score += 0.10
    if has_time_to_proof:
        score += 0.05
    score = round(min(1.0, score), 2)
    if score >= 0.75:
        level = "high"
    elif score >= 0.45:
        level = "medium"
    else:
        level = "low"
    return {
        "score": score,
        "level": level,
        "benchmarks_seen": samples,
        "proof_required_benchmarks": proof_required,
        "root_batches_seen": root_batches,
    }


def _decrease_bundles(current: int) -> int:
    current = int(current or 0)
    if current <= WORKLOAD_MIN_BUNDLES:
        return current
    return max(WORKLOAD_MIN_BUNDLES, current - WORKLOAD_MAX_BUNDLE_STEP)


def _workload_controller_targets(
    cfg: dict,
    track_economics: list[dict],
    reward_funnel: dict,
    policy_posture: dict | None = None,
) -> dict:
    """Read-only high-risk workload controller.

    This does not apply changes. It translates observed reward-funnel health into
    conservative per-track targets for num_bundles, batch_size, and weight so the
    operator can see how the pool would adapt to changing fleet capacity.
    """
    funnel_by_track = {
        (row.get("algorithm_id"), row.get("track")): row
        for row in (reward_funnel.get("by_track") or [])
    }
    current_weights = {
        algo.get("algorithm_id"): int(algo.get("weight") or 0)
        for algo in (cfg.get("algo_selection") or [])
    }
    posture = (policy_posture or {}).get("posture") or "balanced"
    funnel_summary = (reward_funnel or {}).get("summary") or {}
    global_funnel_safe = bool(funnel_summary.get("safe_to_scale_workload", True))
    posture_blocks_increase = posture in {"recovery", "conservative"} or not global_funnel_safe
    targets = []
    for row in track_economics:
        algorithm_id = row.get("algorithm_id")
        track = row.get("track")
        configured = row.get("configured") or {}
        derived = row.get("derived") or {}
        observed = row.get("observed") or {}
        funnel = dict(funnel_by_track.get((algorithm_id, track), {}))
        current_bundles = int(configured.get("num_bundles") or 0)
        current_batch_size = int(configured.get("effective_batch_size") or 1)
        current_weight = int(configured.get("weight") or current_weights.get(algorithm_id) or 0)
        target_bundles = current_bundles
        target_batch_size = current_batch_size
        target_weight = current_weight
        action = "observe"
        reasons = []

        proof_required = int(funnel.get("proof_required_benchmarks") or 0)
        proof_conversion = funnel.get("proof_conversion_rate")
        stopped_rate = funnel.get("stopped_rate")
        stopped_without_roots = int(funnel.get("stopped_without_roots") or 0)
        intentional_stopped_without_roots = int(funnel.get("intentional_stopped_without_roots") or 0)
        unexpected_stopped_without_roots = int(funnel.get("unexpected_stopped_without_roots") or 0)
        allowlist_blocked = bool(funnel.get("allowlist_blocked"))
        avg_time_to_proof = funnel.get("avg_time_to_proof_submit_sec")
        p95_root_runtime = funnel.get("p95_root_batch_runtime_sec")
        estimated_root_batches = derived.get("estimated_root_batches")
        estimated_nonces_per_bundle = derived.get("estimated_nonces_per_bundle")
        confidence = _workload_confidence(funnel, observed)

        if current_bundles <= 0:
            action = "missing_bundle_config"
            reasons.append("track has no configured num_bundles")
        elif not funnel:
            action = "observe_until_track_has_funnel_data"
            reasons.append("no recent reward-funnel data for this track")
        else:
            proof_unhealthy = (
                proof_required > 0
                and proof_conversion is not None
                and float(proof_conversion) < FUNNEL_MIN_PROOF_CONVERSION_RATE
            )
            stopped_unhealthy = stopped_rate is not None and float(stopped_rate) > FUNNEL_MAX_STOPPED_OR_EXPIRED_RATE
            slow_to_proof = (
                avg_time_to_proof is not None
                and float(avg_time_to_proof) > FUNNEL_TARGET_PROOF_SUBMIT_SEC
            )
            fast_clean = (
                proof_required > 0
                and proof_conversion is not None
                and float(proof_conversion) >= WORKLOAD_HIGH_PROOF_CONVERSION_RATE
                and (stopped_rate is None or float(stopped_rate) <= FUNNEL_MAX_STOPPED_OR_EXPIRED_RATE / 2)
                and avg_time_to_proof is not None
                and float(avg_time_to_proof) <= FUNNEL_TARGET_PROOF_SUBMIT_SEC * WORKLOAD_FAST_PROOF_FACTOR
            )

            if allowlist_blocked:
                action = "intentional_allowlist_stop"
                reasons.append("track is outside track_allowlist and was intentionally not benchmarked")
            elif unexpected_stopped_without_roots:
                action = "reduce_or_fix_unrunnable_track"
                reasons.append("recent jobs stopped before root work; check max_job_batches/allowlist/TIG debt")
                target_bundles = _decrease_bundles(current_bundles)
            elif proof_unhealthy:
                action = "reduce_workload_until_proofs_convert"
                reasons.append("proof conversion is below target")
                target_bundles = _decrease_bundles(current_bundles)
                target_weight = max(1, current_weight - 1) if current_weight > 1 else current_weight
            elif stopped_unhealthy:
                action = "reduce_workload_until_stopped_rate_recovers"
                reasons.append("stopped/expired benchmark rate is above target")
                target_bundles = _decrease_bundles(current_bundles)
            elif slow_to_proof:
                action = "reduce_tail_time"
                reasons.append("time-to-proof-submission is above target")
                target_bundles = _decrease_bundles(current_bundles)
                if p95_root_runtime is not None and float(p95_root_runtime) > BUNDLE_TARGET_ROOT_RUNTIME_SEC:
                    target_batch_size = max(1, _previous_power_of_two(current_batch_size // 2))
                    reasons.append("p95 root batch runtime is too high; smaller batches may reduce tail latency")
            elif fast_clean:
                if posture_blocks_increase:
                    action = "hold_workload_until_policy_posture_improves"
                    if not global_funnel_safe:
                        reasons.append("global reward funnel is unsafe; clean track increases stay read-only")
                    else:
                        reasons.append(f"policy posture is {posture}; clean track increases stay read-only")
                else:
                    action = "consider_small_workload_increase"
                    target_bundles = current_bundles + WORKLOAD_MAX_BUNDLE_STEP
                    reasons.append("proof conversion is strong and time-to-proof is well below target")
                    if (
                        estimated_root_batches is not None
                        and int(estimated_root_batches) > BUNDLE_TARGET_MAX_ROOT_BATCHES
                        and p95_root_runtime is not None
                        and float(p95_root_runtime) < BUNDLE_TARGET_ROOT_RUNTIME_SEC / 3
                    ):
                        target_batch_size = _next_power_of_two(current_batch_size + 1)
                        reasons.append("many root batches with short p95 runtime; larger batch_size may reduce scheduling overhead")
            else:
                reasons.append("track is not clearly constrained or underloaded yet")

        estimated_target_batches = None
        if estimated_nonces_per_bundle is not None and target_batch_size > 0 and target_bundles > 0:
            estimated_target_batches = int(math.ceil(float(estimated_nonces_per_bundle) * target_bundles / target_batch_size))
        max_job_batches = int(cfg.get("max_job_batches") or 256)
        max_job_batches_margin_ok = (
            estimated_target_batches is None
            or not max_job_batches
            or estimated_target_batches <= max(1, int(max_job_batches * 0.90))
        )
        if not max_job_batches_margin_ok:
            action = "do_not_increase_exceeds_max_job_batches_margin"
            target_bundles = current_bundles
            target_batch_size = current_batch_size
            reasons.append("target would exceed max_job_batches safety margin")

        targets.append({
            "challenge_id": row.get("challenge_id"),
            "algorithm_id": algorithm_id,
            "track": track,
            "action": action,
            "reasons": reasons,
            "current": {
                "weight": current_weight,
                "num_bundles": current_bundles,
                "effective_batch_size": current_batch_size,
            },
            "target": {
                "weight": target_weight,
                "num_bundles": target_bundles,
                "effective_batch_size": target_batch_size,
            },
            "observed": {
                "proof_required_benchmarks": proof_required,
                "proof_conversion_rate": proof_conversion,
                "stopped_rate": stopped_rate,
                "stopped_without_roots": stopped_without_roots,
                "intentional_stopped_without_roots": intentional_stopped_without_roots,
                "unexpected_stopped_without_roots": unexpected_stopped_without_roots,
                "allowlist_blocked": allowlist_blocked,
                "avg_time_to_proof_submit_sec": avg_time_to_proof,
                "p95_root_batch_runtime_sec": p95_root_runtime,
                "avg_num_batches": observed.get("avg_num_batches"),
                "avg_root_runtime_sec": observed.get("avg_root_runtime_sec"),
            },
            "derived": {
                "estimated_nonces_per_bundle": estimated_nonces_per_bundle,
                "current_estimated_root_batches": estimated_root_batches,
                "target_estimated_root_batches": estimated_target_batches,
                "max_job_batches_margin_ok": max_job_batches_margin_ok,
                "policy_posture": posture,
            },
            "confidence": confidence,
            "apply_now": False,
        })

    actionable = [
        row for row in targets
        if row.get("action") not in {"observe", "observe_until_track_has_funnel_data"}
    ]
    return {
        "mode": "read_only",
        "policy_posture": policy_posture or {"posture": posture},
        "targets": targets,
        "actionable": actionable,
    }


def _slot_metrics(now_ms: int) -> dict:
    summary = _fetch_all(
        """
        SELECT slot_type, state, COUNT(*) AS count
        FROM benchmark_slot
        GROUP BY slot_type, state
        ORDER BY slot_type, state
        """
    )
    detail = _fetch_all(
        """
        SELECT
            slot_id,
            slot_type,
            state,
            left(benchmark_id, 10) AS benchmark,
            challenge,
            track_id,
            ROUND((%s - COALESCE(last_activity_at, assigned_at)) / 60000.0, 1) AS idle_min
        FROM benchmark_slot
        ORDER BY slot_type, slot_id
        """,
        (now_ms,),
    )
    return {"summary": summary, "detail": detail}


def _stranded_benchmarks(now_ms: int) -> list[dict]:
    """Find old active benchmarks with pending roots but no assigned root work."""
    return _fetch_all(
        """
        SELECT
            left(j.benchmark_id, 10) AS benchmark,
            j.benchmark_id,
            j.challenge,
            j.algorithm,
            j.settings->>'algorithm_id' AS algorithm_id,
            j.settings->>'track_id' AS track,
            ROUND((%s - j.start_time) / 60000.0, 1) AS age_min,
            COUNT(rb.*) FILTER (WHERE rb.ready IS NULL) AS pending_roots,
            COUNT(rb.*) FILTER (
                WHERE rb.ready IS NULL
                  AND rb.slave IS NOT NULL
                  AND rb.start_time IS NOT NULL
            ) AS assigned_roots,
            COUNT(pb.*) FILTER (WHERE pb.ready IS NULL) AS pending_proofs,
            bs.slot_id,
            bs.slot_type,
            bs.state AS slot_state
        FROM job j
        JOIN root_batch rb ON rb.benchmark_id = j.benchmark_id
        LEFT JOIN proofs_batch pb
          ON pb.benchmark_id = j.benchmark_id
         AND pb.batch_idx = rb.batch_idx
        LEFT JOIN benchmark_slot bs ON bs.benchmark_id = j.benchmark_id
        WHERE j.stopped IS NULL
          AND j.end_time IS NULL
          AND j.merkle_root_ready IS NULL
          AND j.start_time IS NOT NULL
          AND j.start_time < %s
        GROUP BY j.benchmark_id, j.challenge, j.algorithm, j.settings, bs.slot_id, bs.slot_type, bs.state
        HAVING COUNT(rb.*) FILTER (WHERE rb.ready IS NULL) > 0
           AND COUNT(rb.*) FILTER (
                WHERE rb.ready IS NULL
                  AND rb.slave IS NOT NULL
                  AND rb.start_time IS NOT NULL
           ) = 0
        ORDER BY j.start_time
        LIMIT 25
        """,
        (now_ms, now_ms - STRANDED_BENCHMARK_MS),
    )


def _slot_state_summary(slots: dict) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    counts: dict[str, int] = {}
    idle: dict[str, int] = {}
    busy: dict[str, int] = {}
    for row in slots.get("summary", []):
        slot_type = row.get("slot_type")
        count = int(row.get("count") or 0)
        counts[slot_type] = counts.get(slot_type, 0) + count
        if row.get("state") == "idle":
            idle[slot_type] = idle.get(slot_type, 0) + count
        else:
            busy[slot_type] = busy.get(slot_type, 0) + count
    return counts, idle, busy


def _avg_runtime(slaves: list[dict]) -> float | None:
    runtimes = [
        float(s.get("avg_runtime_sec"))
        for s in slaves
        if s.get("avg_runtime_sec") is not None
    ]
    if not runtimes:
        return None
    return round(sum(runtimes) / len(runtimes), 1)


def _fleet_capacity(
    cfg: dict,
    slaves: list[dict],
    challenges: list[dict],
    slots: dict,
    stale_totals: dict | None = None,
) -> dict:
    active_cpu = [s for s in slaves if s["profile"] == "cpu" and _counts_for_capacity(s)]
    active_gpu = [s for s in slaves if s["profile"] == "gpu" and _counts_for_capacity(s)]
    if stale_totals is None:
        stale_roots = max(
            sum(int(s.get("stale_roots") or 0) for s in slaves),
            sum(int(c.get("stale_roots") or 0) for c in challenges),
        )
        stale_proofs = max(
            sum(int(s.get("stale_proofs") or 0) for s in slaves),
            sum(int(c.get("stale_proofs") or 0) for c in challenges),
        )
    else:
        stale_roots = int(stale_totals.get("roots") or 0)
        stale_proofs = int(stale_totals.get("proofs") or 0)
    stale_total = stale_roots + stale_proofs
    stale_challenge_ids = sorted({
        CHALLENGE_NAME_TO_ID.get(str(c.get("challenge") or ""))
        for c in challenges
        if int(c.get("stale_roots") or 0) or int(c.get("stale_proofs") or 0)
    } - {None})

    current_slots = (cfg.get("resource_slots") or {}).get("slots", {})
    slot_counts, slot_idle, slot_busy = _slot_state_summary(slots)
    active_gpu_slot_types = []
    for challenge in challenges:
        if int(challenge.get("active_benchmarks") or 0) <= 0:
            continue
        slot_type = GPU_CHALLENGE_ID_TO_SLOT.get(str(challenge.get("challenge_id") or challenge.get("challenge") or ""))
        if slot_type and slot_type not in active_gpu_slot_types:
            active_gpu_slot_types.append(slot_type)

    cpu_pressure = sum(int(s.get("active_unfinished") or 0) for s in active_cpu)
    gpu_pressure = sum(int(s.get("active_unfinished") or 0) for s in active_gpu)
    productive_idle_cpu = [
        s for s in active_cpu
        if int(s.get("completed_recent") or 0) > 0 and int(s.get("active_unfinished") or 0) == 0
    ]
    productive_idle_gpu = [
        s for s in active_gpu
        if int(s.get("completed_recent") or 0) > 0 and int(s.get("active_unfinished") or 0) == 0
    ]
    return {
        "active_cpu": len(active_cpu),
        "active_gpu": len(active_gpu),
        "productive_idle_cpu": len(productive_idle_cpu),
        "productive_idle_gpu": len(productive_idle_gpu),
        "cpu_pressure": cpu_pressure,
        "gpu_pressure": gpu_pressure,
        "stale_roots": stale_roots,
        "stale_proofs": stale_proofs,
        "stale_total": stale_total,
        "stale_challenge_ids": stale_challenge_ids,
        "cpu_completed_recent": sum(int(s.get("completed_recent") or 0) for s in active_cpu),
        "gpu_completed_recent": sum(int(s.get("completed_recent") or 0) for s in active_gpu),
        "cpu_nonces_recent": sum(int(s.get("nonces_recent") or 0) for s in active_cpu),
        "gpu_nonces_recent": sum(int(s.get("nonces_recent") or 0) for s in active_gpu),
        "cpu_avg_runtime_sec": _avg_runtime(active_cpu),
        "gpu_avg_runtime_sec": _avg_runtime(active_gpu),
        "slot_counts": slot_counts,
        "slot_idle": slot_idle,
        "slot_busy": slot_busy,
        "active_gpu_slot_types": active_gpu_slot_types,
        "current_slots": current_slots,
        "current_adaptive_caps": cfg.get("adaptive_slave_caps") or {},
    }


def _target_resource_slots(capacity: dict) -> dict:
    current_slots = capacity["current_slots"]
    slot_counts = capacity["slot_counts"]
    slot_idle = capacity["slot_idle"]
    slot_busy = capacity["slot_busy"]
    proposed = dict(current_slots)

    current_cpu = int(current_slots.get(CPU_SLOT_TYPE, slot_counts.get(CPU_SLOT_TYPE, 0)) or 0)
    if capacity["active_cpu"]:
        active_floor = max(2, (capacity["active_cpu"] + PRODUCTIVE_IDLE_CPU_PER_SLOT - 1) // max(1, PRODUCTIVE_IDLE_CPU_PER_SLOT))
        target_cpu = max(current_cpu, min(MAX_CPU_SLOTS, active_floor))
        if capacity["productive_idle_cpu"] >= PRODUCTIVE_IDLE_CPU_SCALE_MIN:
            extra_slots = max(
                1,
                (capacity["productive_idle_cpu"] + PRODUCTIVE_IDLE_CPU_PER_SLOT - 1)
                // max(1, PRODUCTIVE_IDLE_CPU_PER_SLOT),
            )
            target_cpu = max(target_cpu, current_cpu + extra_slots)
        elif (
            capacity["stale_total"]
            and capacity["productive_idle_cpu"] == 0
            and slot_idle.get(CPU_SLOT_TYPE, 0) > 0
        ):
            target_cpu = max(2, current_cpu - 1)
        elif slot_idle.get(CPU_SLOT_TYPE, 0) == 0 and capacity["cpu_pressure"] >= max(1, current_cpu):
            target_cpu = max(target_cpu, current_cpu + 2)
        proposed[CPU_SLOT_TYPE] = min(target_cpu, MAX_CPU_SLOTS)
    else:
        proposed[CPU_SLOT_TYPE] = 0

    if not capacity["active_gpu"]:
        for slot_type in GPU_SLOT_TYPES:
            proposed[slot_type] = 0
    else:
        current_gpu_slots = {
            slot_type: int(current_slots.get(slot_type, slot_counts.get(slot_type, 0)) or 0)
            for slot_type in GPU_SLOT_TYPES
        }
        busy_gpu_slots = {
            slot_type: int(slot_busy.get(slot_type, 0) or 0)
            for slot_type in GPU_SLOT_TYPES
        }
        if int(capacity["active_gpu"] or 0) <= 1:
            focus_order = list(capacity.get("active_gpu_slot_types") or [])
            if not focus_order:
                focus_order = sorted(
                    GPU_SLOT_TYPES,
                    key=lambda key: (-busy_gpu_slots.get(key, 0), -current_gpu_slots.get(key, 0), key),
                )
            focus_slot = next((slot_type for slot_type in focus_order if slot_type in GPU_SLOT_TYPES), GPU_SLOT_TYPES[0])
            proposed_gpu_slots = {slot_type: 0 for slot_type in GPU_SLOT_TYPES}
            proposed_gpu_slots[focus_slot] = 1
            proposed.update(proposed_gpu_slots)
            return proposed

        gpu_target_total = max(1, int(capacity["active_gpu"] or 0))
        if capacity["productive_idle_gpu"] >= PRODUCTIVE_IDLE_GPU_SCALE_MIN:
            extra_slots = max(
                1,
                (capacity["productive_idle_gpu"] + PRODUCTIVE_IDLE_GPU_PER_SLOT - 1)
                // max(1, PRODUCTIVE_IDLE_GPU_PER_SLOT),
            )
            gpu_target_total += extra_slots
        elif capacity["gpu_pressure"] > gpu_target_total and sum(busy_gpu_slots.values()) >= gpu_target_total:
            gpu_target_total += 1
        gpu_target_total = min(gpu_target_total, MAX_GPU_SLOTS_PER_TYPE * len(GPU_SLOT_TYPES))

        proposed_gpu_slots = {slot_type: 0 for slot_type in GPU_SLOT_TYPES}
        for slot_type in sorted(GPU_SLOT_TYPES, key=lambda key: (-busy_gpu_slots.get(key, 0), -current_gpu_slots.get(key, 0), key)):
            if gpu_target_total <= 0:
                break
            target = min(
                MAX_GPU_SLOTS_PER_TYPE,
                max(1 if current_gpu_slots.get(slot_type, 0) or busy_gpu_slots.get(slot_type, 0) else 0, busy_gpu_slots.get(slot_type, 0)),
                gpu_target_total,
            )
            proposed_gpu_slots[slot_type] = target
            gpu_target_total -= target
        if gpu_target_total > 0:
            for slot_type in GPU_SLOT_TYPES:
                if gpu_target_total <= 0:
                    break
                room = MAX_GPU_SLOTS_PER_TYPE - proposed_gpu_slots[slot_type]
                if room <= 0:
                    continue
                add = min(room, gpu_target_total)
                proposed_gpu_slots[slot_type] += add
                gpu_target_total -= add

        proposed.update(proposed_gpu_slots)
    return proposed


def _target_max_concurrent_benchmarks(capacity: dict, proposed_slots: dict) -> int:
    active_gpu = capacity["active_gpu"] > 0
    active_cpu = capacity["active_cpu"] > 0
    cpu_slot_total = int(proposed_slots.get(CPU_SLOT_TYPE, 0) or 0) if active_cpu else 0
    gpu_slot_total = (
        sum(int(proposed_slots.get(k, 0) or 0) for k in GPU_SLOT_TYPES)
        if active_gpu
        else 0
    )
    buffer = BENCHMARK_BUFFER if (active_cpu or active_gpu) else 0
    return _clamp(cpu_slot_total + gpu_slot_total + buffer, MIN_MAX_BENCHMARKS, MAX_MAX_BENCHMARKS)


def _challenge_ids_by_profile(cfg: dict) -> tuple[list[str], list[str]]:
    cpu_ids = []
    gpu_ids = []
    for algo in cfg.get("algo_selection") or []:
        challenge_id = str(algo.get("algorithm_id") or "").split("_")[0]
        if not challenge_id:
            continue
        if challenge_id in {"c004", "c005", "c006"}:
            if challenge_id not in gpu_ids:
                gpu_ids.append(challenge_id)
        elif challenge_id not in cpu_ids:
            cpu_ids.append(challenge_id)
    return cpu_ids, gpu_ids


def _target_per_challenge_caps(cfg: dict, capacity: dict, proposed_slots: dict) -> dict:
    current_per = cfg.get("per_challenge_max_benchmarks", {}) or {}
    proposed = dict(current_per)
    cpu_ids, _gpu_ids = _challenge_ids_by_profile(cfg)
    stale_blocking = (
        int(capacity.get("stale_roots") or 0) > PRODUCTIVE_IDLE_STALE_ROOT_TOLERANCE
        or int(capacity.get("stale_proofs") or 0) > 0
    )
    stale_challenge_ids = set(capacity.get("stale_challenge_ids") or [])
    if capacity["active_cpu"] and cpu_ids:
        cpu_slots = int(proposed_slots.get(CPU_SLOT_TYPE, 0) or 0)
        per_cpu_target = max(1, math.ceil(cpu_slots / max(1, len(cpu_ids))))
        for challenge_id in cpu_ids:
            if stale_blocking and challenge_id in stale_challenge_ids:
                continue
            current = int(current_per.get(challenge_id, 1) or 1)
            proposed[challenge_id] = min(
                max(current, per_cpu_target),
                MAX_CPU_CHALLENGE_BENCHMARKS,
            )
    if capacity["active_gpu"]:
        current_c004 = int(current_per.get("c004", 1) or 1)
        proposed.update({
            "c004": min(
                max(0, int(proposed_slots.get("vector_search", 0) or 0))
                if not (stale_blocking and "c004" in stale_challenge_ids)
                else current_c004,
                MAX_GPU_CHALLENGE_BENCHMARKS,
            ),
            "c005": min(
                max(0, int(proposed_slots.get("hypergraph", 0) or 0))
                if not (stale_blocking and "c005" in stale_challenge_ids)
                else int(current_per.get("c005", 1) or 1),
                MAX_GPU_CHALLENGE_BENCHMARKS,
            ),
            "c006": min(
                max(0, int(proposed_slots.get("neuralnet_optimizer", 0) or 0))
                if not (stale_blocking and "c006" in stale_challenge_ids)
                else int(current_per.get("c006", 1) or 1),
                MAX_GPU_CHALLENGE_BENCHMARKS,
            ),
        })
    else:
        proposed.update({"c004": 1, "c005": 1, "c006": 1})
    return proposed


def _target_adaptive_slave_caps(capacity: dict) -> dict:
    current = capacity.get("current_adaptive_caps") or {}
    if not current:
        return {}
    proposed = dict(current)
    cpu_max = int(current.get("cpu_max_cap", 0) or 0)
    gpu_max = int(current.get("gpu_max_cap", 0) or 0)
    if capacity["active_cpu"] and cpu_max:
        cpu_ceiling = max(cpu_max, MAX_CPU_SLAVE_CAP)
        if capacity["productive_idle_cpu"] >= PRODUCTIVE_IDLE_CPU_SCALE_MIN:
            proposed["cpu_max_cap"] = max(MIN_CPU_SLAVE_CAP, min(cpu_max + 1, cpu_ceiling))
        elif capacity["cpu_completed_recent"] >= CAP_SCALE_COMPLETIONS_PER_STEP and capacity["cpu_pressure"] >= capacity["active_cpu"]:
            proposed["cpu_max_cap"] = max(MIN_CPU_SLAVE_CAP, min(cpu_max + 1, cpu_ceiling))
    if capacity["active_gpu"] and gpu_max:
        gpu_ceiling = max(gpu_max, MAX_GPU_SLAVE_CAP)
        if capacity["productive_idle_gpu"] >= PRODUCTIVE_IDLE_GPU_SCALE_MIN:
            proposed["gpu_max_cap"] = max(MIN_GPU_SLAVE_CAP, min(gpu_max + 1, gpu_ceiling))
        elif capacity["gpu_completed_recent"] >= CAP_SCALE_COMPLETIONS_PER_STEP and capacity["gpu_pressure"] >= capacity["active_gpu"]:
            proposed["gpu_max_cap"] = max(MIN_GPU_SLAVE_CAP, min(gpu_max + 1, gpu_ceiling))
    return proposed


def _track_economics_recommendations(track_economics: list[dict]) -> list[dict]:
    recs = []
    flagged = [
        row for row in track_economics
        if row.get("efficiency_notes")
    ]
    if flagged:
        recs.append({
            "key": "track_settings.bundle_runtime_economics",
            "current": [
                {
                    "algorithm_id": row.get("algorithm_id"),
                    "track": row.get("track"),
                    "configured": row.get("configured"),
                    "derived": row.get("derived"),
                    "notes": row.get("efficiency_notes"),
                }
                for row in flagged[:25]
            ],
            "proposed": "review_num_bundles_batch_size_and_hyperparameters",
            "reason": (
                "num_bundles controls total nonces and reward-ticket count, while batch_size controls "
                "root-batch granularity. Track settings should be balanced using observed root runtime, "
                "root batches per benchmark, stale/proof pressure, and reward evidence before automatic "
                "algo_selection changes are safe."
            ),
            "apply_now": False,
        })
    return recs


def _recommendations(
    cfg: dict,
    slaves: list[dict],
    challenges: list[dict],
    slots: dict,
    track_economics: list[dict] | None = None,
    stale_totals: dict | None = None,
    reward_funnel: dict | None = None,
    workload_targets: dict | None = None,
) -> list[dict]:
    capacity = _fleet_capacity(cfg, slaves, challenges, slots, stale_totals)
    current_slots = capacity["current_slots"]
    proposed_slots = _target_resource_slots(capacity)
    recommendations = []
    if current_slots:
        if proposed_slots != current_slots:
            recommendations.append({
                "key": "resource_slots.slots",
                "current": current_slots,
                "proposed": proposed_slots,
                "reason": (
                    "Resource slots are sized from active fleet capacity, productive idle workers, "
                    "slot pressure, GPU reserve needs, and stale-work guardrails."
                ),
                "signals": capacity,
                "apply_now": False,
            })

    proposed_max = _target_max_concurrent_benchmarks(capacity, proposed_slots)
    current_max = cfg.get("max_concurrent_benchmarks")
    if current_max is not None and proposed_max != int(current_max):
        recommendations.append({
            "key": "max_concurrent_benchmarks",
            "current": current_max,
            "proposed": proposed_max,
            "reason": "Concurrent benchmark target reserves room for active CPU slots, GPU slots, and a small precommit buffer.",
            "signals": capacity,
            "apply_now": False,
        })

    current_per = cfg.get("per_challenge_max_benchmarks", {}) or {}
    proposed_per = _target_per_challenge_caps(cfg, capacity, proposed_slots)
    if proposed_per != current_per:
        recommendations.append({
            "key": "per_challenge_max_benchmarks",
            "current": current_per,
            "proposed": proposed_per,
            "reason": "Per-challenge benchmark caps should scale with CPU/GPU slot capacity so precommit creation does not starve active workers.",
            "signals": capacity,
            "apply_now": False,
        })

    current_caps = cfg.get("adaptive_slave_caps", {}) or {}
    proposed_caps = _target_adaptive_slave_caps(capacity)
    if proposed_caps and proposed_caps != current_caps:
        recommendations.append({
            "key": "adaptive_slave_caps",
            "current": current_caps,
            "proposed": proposed_caps,
            "reason": "Adaptive slave cap ceilings should rise when productive workers prove they can carry more concurrent batches.",
            "signals": capacity,
            "apply_now": False,
        })

    for challenge in challenges:
        if int(challenge.get("stale_roots") or 0) or int(challenge.get("stale_proofs") or 0):
            recommendations.append({
                "key": f"challenge_health.{challenge.get('challenge')}.{challenge.get('track')}",
                "current": {
                    "stale_roots": challenge.get("stale_roots"),
                    "stale_proofs": challenge.get("stale_proofs"),
                },
                "proposed": "investigate_or_reduce_capacity",
                "reason": "This active track has stale unfinished work and may be over-assigned or assigned to unhealthy slaves.",
                "apply_now": False,
            })

    if capacity["stale_proofs"]:
        recommendations.append({
            "key": "proof_queue",
            "current": {"stale_proofs": capacity["stale_proofs"]},
            "proposed": "check root-artifact ownership and proof slave logs",
            "reason": "Proof batches should normally clear quickly once roots are ready.",
            "apply_now": False,
        })

    funnel_summary = (reward_funnel or {}).get("summary") or {}
    if funnel_summary.get("issues"):
        recommendations.append({
            "key": "reward_funnel",
            "current": {
                "issues": funnel_summary.get("issues"),
                "proof_conversion_rate": funnel_summary.get("proof_conversion_rate"),
                "stopped_rate": funnel_summary.get("stopped_rate"),
                "avg_time_to_proof_submit_sec": funnel_summary.get("avg_time_to_proof_submit_sec"),
                "stopped_without_roots": funnel_summary.get("stopped_without_roots"),
            },
            "proposed": "stabilize_proof_submission_before_scaling_workload",
            "reason": (
                "The pool should scale work only when root work converts into benchmark submissions, "
                "proof submissions, and clean finalized jobs. Root throughput alone is not a reward signal."
            ),
            "apply_now": False,
        })

    workload_actionable = (workload_targets or {}).get("actionable") or []
    if workload_actionable:
        recommendations.append({
            "key": "workload_controller",
            "current": [
                {
                    "algorithm_id": row.get("algorithm_id"),
                    "track": row.get("track"),
                    "action": row.get("action"),
                    "current": row.get("current"),
                    "target": row.get("target"),
                    "reasons": row.get("reasons"),
                    "observed": row.get("observed"),
                }
                for row in workload_actionable[:25]
            ],
            "proposed": "review_read_only_workload_targets",
            "reason": (
                "High-risk workload settings should follow measured proof conversion, "
                "time-to-proof, stopped/no-proof debt, p95 batch runtime, and max_job_batches "
                "margin. These targets are read-only until validated over multiple clean windows."
            ),
            "apply_now": False,
        })

    recommendations.extend(_track_economics_recommendations(track_economics or []))
    return recommendations


def _health_summary(report: dict) -> dict:
    slaves = report.get("slaves") or []
    challenges = report.get("challenges") or []
    slots = report.get("slots") or {}
    stale_totals = report.get("stale_totals") or {}
    if stale_totals:
        stale_roots = int(stale_totals.get("roots") or 0)
        stale_proofs = int(stale_totals.get("proofs") or 0)
    else:
        stale_roots = max(
            sum(int(s.get("stale_roots") or 0) for s in slaves),
            sum(int(c.get("stale_roots") or 0) for c in challenges),
        )
        stale_proofs = max(
            sum(int(s.get("stale_proofs") or 0) for s in slaves),
            sum(int(c.get("stale_proofs") or 0) for c in challenges),
        )
    active_unregistered = [
        s["slave_name"]
        for s in slaves
        if s.get("active_now")
        and _is_public_member_slave(str(s.get("slave_name") or ""))
        and not s.get("registered")
    ]
    stranded = report.get("stranded_benchmarks") or []
    slot_capacity: dict[str, int] = {}
    for row in slots.get("summary", []):
        slot_type = row.get("slot_type")
        slot_capacity[slot_type] = slot_capacity.get(slot_type, 0) + int(row.get("count") or 0)

    live_by_profile = {
        "cpu": sum(
            int(s.get("active_unfinished") or 0)
            for s in slaves
            if s.get("profile") == "cpu" and _counts_for_capacity(s)
        ),
        "gpu": sum(
            int(s.get("active_unfinished") or 0)
            for s in slaves
            if s.get("profile") == "gpu" and _counts_for_capacity(s)
        ),
    }
    live_by_slot_type = {
        CPU_SLOT_TYPE: live_by_profile["cpu"],
        "gpu": live_by_profile["gpu"],
    }
    gpu_slot_capacity = sum(int(slot_capacity.get(k, 0) or 0) for k in GPU_SLOT_TYPES)
    capacity_waiting = []
    unserved_stranded = []
    for item in stranded:
        profile = _capacity_profile_for_work(item)
        capacity = gpu_slot_capacity if profile == "gpu" else int(slot_capacity.get(CPU_SLOT_TYPE, 0) or 0)
        live = live_by_profile[profile]
        enriched = dict(item)
        enriched["capacity_profile"] = profile
        enriched["matching_live_roots"] = live
        enriched["matching_slot_capacity"] = capacity
        # Slot capacity is a benchmark budget, not always the exact effective
        # slave assignment capacity. Treat near-full GPU capacity as normal
        # waiting so a few idle slot-equivalents do not become a hard blocker
        # while C3/local GPU route caps are already busy.
        if profile == "gpu":
            near_capacity_threshold = max(
                1,
                min(
                    capacity - _active_gpu_slave_count(report),
                    math.floor(capacity * 0.60),
                ),
            )
        else:
            near_capacity_threshold = max(1, math.floor(capacity * 0.60))
        if capacity > 0 and live >= near_capacity_threshold:
            enriched["classification"] = "capacity_waiting"
            capacity_waiting.append(enriched)
        else:
            enriched["classification"] = "unserved"
            unserved_stranded.append(enriched)
    return {
        "stale_roots": stale_roots,
        "stale_proofs": stale_proofs,
        "active_unregistered": active_unregistered,
        "stranded_benchmarks": stranded,
        "unserved_stranded_benchmarks": unserved_stranded,
        "capacity_waiting_benchmarks": capacity_waiting,
        "live_by_profile": live_by_profile,
        "slot_capacity": slot_capacity,
        "healthy": (
            stale_roots == 0
            and stale_proofs == 0
            and not active_unregistered
            and not unserved_stranded
        ),
    }


def _policy_posture(
    report: dict,
    health: dict,
    capacity: dict | None = None,
    clean_windows: int | None = None,
) -> dict:
    """Classify the pool state before deciding how ambitious tuning can be.

    Posture is deliberately conservative. It tells the controller whether it is
    looking at a recovery, low-compute, normal, or proven high-throughput world.
    """
    funnel_summary = (report.get("reward_funnel") or {}).get("summary") or {}
    issues = list(funnel_summary.get("issues") or [])
    proof_conversion = funnel_summary.get("proof_conversion_rate")
    stopped_rate = funnel_summary.get("stopped_rate")
    avg_time_to_proof = funnel_summary.get("avg_time_to_proof_submit_sec")
    funnel_safe = bool(funnel_summary.get("safe_to_scale_workload", True))
    capacity = capacity or {}
    active_cpu = int(capacity.get("active_cpu") or 0)
    active_gpu = int(capacity.get("active_gpu") or 0)
    cpu_pressure = int(capacity.get("cpu_pressure") or 0)
    gpu_pressure = int(capacity.get("gpu_pressure") or 0)
    completions = int(capacity.get("cpu_completed_recent") or 0) + int(capacity.get("gpu_completed_recent") or 0)
    active_profiles = active_cpu + active_gpu
    active_work = cpu_pressure + gpu_pressure
    reasons = []

    if not health.get("healthy"):
        reasons.append("health_has_stale_unregistered_or_unserved_work")
    if not funnel_safe:
        reasons.append("reward_funnel_not_safe")
    if issues:
        reasons.append("reward_funnel_has_issues")
    if health.get("stale_proofs"):
        reasons.append("stale_proof_debt")
    if health.get("unserved_stranded_benchmarks"):
        reasons.append("unserved_stranded_precommits")
    if health.get("active_unregistered"):
        reasons.append("active_unregistered_slaves")

    if reasons:
        posture = "recovery"
    else:
        high_conversion = (
            proof_conversion is not None
            and float(proof_conversion) >= WORKLOAD_HIGH_PROOF_CONVERSION_RATE
        )
        low_stopped = (
            stopped_rate is None
            or float(stopped_rate) <= FUNNEL_MAX_STOPPED_OR_EXPIRED_RATE / 2
        )
        fast_proof = (
            avg_time_to_proof is not None
            and float(avg_time_to_proof) <= FUNNEL_TARGET_PROOF_SUBMIT_SEC * WORKLOAD_FAST_PROOF_FACTOR
        )
        enough_clean_windows = clean_windows is None or clean_windows >= APPLY_MIN_CLEAN_WINDOWS
        low_compute = active_profiles <= 1 and active_work < 4 and completions < 20
        high_compute = active_work >= 16 or completions >= 100
        if low_compute:
            posture = "conservative"
            reasons.append("low_observed_compute")
        elif high_compute and high_conversion and low_stopped and fast_proof and enough_clean_windows:
            posture = "aggressive"
            reasons.append("high_compute_clean_fast_reward_funnel")
        else:
            posture = "balanced"
            reasons.append("normal_guarded_operation")

    return {
        "posture": posture,
        "reasons": reasons,
        "signals": {
            "funnel_safe": funnel_safe,
            "issues": issues,
            "proof_conversion_rate": proof_conversion,
            "stopped_rate": stopped_rate,
            "avg_time_to_proof_submit_sec": avg_time_to_proof,
            "clean_windows": clean_windows,
            "active_cpu": active_cpu,
            "active_gpu": active_gpu,
            "active_work": active_work,
            "completed_recent": completions,
            "stale_roots": health.get("stale_roots"),
            "stale_proofs": health.get("stale_proofs"),
            "unserved_stranded": len(health.get("unserved_stranded_benchmarks") or []),
            "active_unregistered": len(health.get("active_unregistered") or []),
        },
        "workload_auto_apply_allowed": posture == "aggressive",
    }


def _gpu_slot_counts(report: dict) -> tuple[int, int]:
    total = 0
    idle = 0
    for row in (report.get("slots") or {}).get("summary", []):
        if row.get("slot_type") not in GPU_SLOT_TYPES:
            continue
        count = int(row.get("count") or 0)
        total += count
        if row.get("state") == "idle":
            idle += count
    return total, idle


def _active_gpu_slave_count(report: dict) -> int:
    return sum(
        1
        for slave in report.get("slaves") or []
        if slave.get("profile") == "gpu" and _counts_for_capacity(slave)
    )


def _gpu_capacity_needs_benchmark_room(report: dict) -> bool:
    gpu_slots, idle_gpu_slots = _gpu_slot_counts(report)
    return _active_gpu_slave_count(report) > 0 and gpu_slots > 0 and idle_gpu_slots > 0


def _next_value(current: int, target: int, step: int) -> int:
    if target > current:
        return min(target, current + step)
    if target < current:
        return max(target, current - step)
    return current


def _next_value_bounded(current: int, target: int, up_step: int, down_step: int) -> int:
    if target > current:
        return min(target, current + max(1, up_step))
    if target < current:
        return max(target, current - max(1, down_step))
    return current


def _find_algo_selection(cfg: dict, algorithm_id: str) -> dict | None:
    for algo in cfg.get("algo_selection") or []:
        if algo.get("algorithm_id") == algorithm_id:
            return algo
    return None


def _apply_workload_target(new_cfg: dict, target: dict) -> dict | None:
    algorithm_id = target.get("algorithm_id")
    track = target.get("track")
    if not algorithm_id or not track:
        return None
    algo = _find_algo_selection(new_cfg, algorithm_id)
    if not algo:
        return None
    track_settings = algo.get("track_settings") or {}
    if track not in track_settings:
        return None
    settings = track_settings.get(track) or {}
    current = target.get("current") or {}
    desired = target.get("target") or {}
    next_settings = dict(settings)
    changed = {}

    for field, config_key, floor in (
        ("num_bundles", "num_bundles", WORKLOAD_MIN_BUNDLES),
        ("effective_batch_size", "batch_size", WORKLOAD_MIN_BATCH_SIZE),
    ):
        current_value = int(current.get(field) or next_settings.get(config_key) or 0)
        target_value = int(desired.get(field) or current_value)
        if target_value < current_value:
            target_value = max(floor, target_value)
        if target_value != current_value:
            next_settings[config_key] = target_value
            changed[config_key] = {
                "current": current_value,
                "target": target_value,
                "next": target_value,
            }

    current_weight = int(current.get("weight") or algo.get("weight") or 0)
    target_weight = int(desired.get("weight") or current_weight)
    if target_weight < current_weight:
        target_weight = max(WORKLOAD_MIN_WEIGHT, target_weight)
    if target_weight != current_weight:
        algo["weight"] = target_weight
        changed["weight"] = {
            "current": current_weight,
            "target": target_weight,
            "next": target_weight,
        }

    if not changed:
        return None
    algo.setdefault("track_settings", {})[track] = next_settings
    return {
        "algorithm_id": algorithm_id,
        "track": track,
        "action": target.get("action"),
        "changes": changed,
        "reasons": target.get("reasons") or [],
        "observed": target.get("observed") or {},
        "derived": target.get("derived") or {},
    }


def _workload_cooldown_state(report: dict) -> dict:
    state = report.get("workload_cooldown") or {}
    if isinstance(state, dict):
        return state
    return {}


def _workload_cooldown_guard(report: dict, action_kind: str) -> dict | None:
    now_ms = int(report.get("generated_at_ms") or int(time.time() * 1000))
    state = _workload_cooldown_state(report)
    if action_kind == "safety":
        last_ms = int(state.get("last_safety_change_ms") or 0)
        cooldown_ms = WORKLOAD_SAFETY_COOLDOWN_MS
    else:
        last_ms = int(state.get("last_canary_change_ms") or 0)
        cooldown_ms = WORKLOAD_CANARY_COOLDOWN_MS
    if last_ms and now_ms - last_ms < cooldown_ms:
        return {
            "skipped": f"workload_{action_kind}_cooldown_active",
            "last_change_ms": last_ms,
            "elapsed_ms": max(0, now_ms - last_ms),
            "cooldown_ms": cooldown_ms,
            "last_change": state.get("last_change") or {},
        }
    return None


def _rollback_last_canary(
    new_cfg: dict,
    report: dict,
    health: dict,
    funnel_safe: bool,
    policy_posture: dict,
) -> dict | None:
    state = _workload_cooldown_state(report)
    last = state.get("last_change") or {}
    if not last.get("canary"):
        return None
    if funnel_safe and health.get("healthy") and policy_posture.get("posture") != "recovery":
        return None

    algorithm_id = last.get("algorithm_id")
    track = last.get("track")
    if not algorithm_id or not track:
        return None
    algo = _find_algo_selection(new_cfg, algorithm_id)
    if not algo:
        return None
    track_settings = algo.get("track_settings") or {}
    if track not in track_settings:
        return None

    settings = dict(track_settings.get(track) or {})
    rollback_changes = {}
    for field in ("num_bundles", "batch_size"):
        change = (last.get("changes") or {}).get(field) or {}
        if change.get("current") is None:
            continue
        current_value = settings.get(field)
        previous_value = int(change.get("current") or 0)
        if int(current_value or 0) != previous_value:
            settings[field] = previous_value
            rollback_changes[field] = {
                "current": current_value,
                "next": previous_value,
                "canary_next": change.get("next"),
            }

    weight_change = (last.get("changes") or {}).get("weight") or {}
    if weight_change.get("current") is not None:
        current_weight = algo.get("weight")
        previous_weight = int(weight_change.get("current") or 0)
        if int(current_weight or 0) != previous_weight:
            algo["weight"] = previous_weight
            rollback_changes["weight"] = {
                "current": current_weight,
                "next": previous_weight,
                "canary_next": weight_change.get("next"),
            }

    if not rollback_changes:
        return None

    algo.setdefault("track_settings", {})[track] = settings
    funnel_summary = (report.get("reward_funnel") or {}).get("summary") or {}
    return {
        "algorithm_id": algorithm_id,
        "track": track,
        "action": "rollback_failed_workload_canary",
        "canary_rollback": True,
        "changes": rollback_changes,
        "rollback_of": last,
        "reasons": [
            "previous workload canary is being reverted because pool health or reward funnel regressed"
        ],
        "observed": {
            "funnel_safe": funnel_safe,
            "funnel_issues": funnel_summary.get("issues") or [],
            "healthy": bool(health.get("healthy")),
            "policy_posture": policy_posture.get("posture"),
        },
    }


def _next_workload_change(
    new_cfg: dict,
    report: dict,
    workload_targets: dict,
    health: dict,
    funnel_safe: bool,
    policy_posture: dict,
    clean_windows: int,
    allow_canary: bool = True,
) -> tuple[dict | None, dict | None]:
    actionable = list((workload_targets or {}).get("actionable") or [])
    if not actionable:
        return None, None

    safety_actions = {
        "reduce_or_fix_unrunnable_track",
        "reduce_workload_until_proofs_convert",
        "reduce_workload_until_stopped_rate_recovers",
        "reduce_tail_time",
    }
    safety_cooldown_guard = _workload_cooldown_guard(report, "safety")
    if safety_cooldown_guard:
        return None, safety_cooldown_guard
    for row in actionable:
        if row.get("action") not in safety_actions:
            continue
        current = row.get("current") or {}
        target = row.get("target") or {}
        reduces_work = (
            int(target.get("num_bundles") or current.get("num_bundles") or 0)
            < int(current.get("num_bundles") or 0)
            or int(target.get("effective_batch_size") or current.get("effective_batch_size") or 0)
            < int(current.get("effective_batch_size") or 0)
            or int(target.get("weight") or current.get("weight") or 0)
            < int(current.get("weight") or 0)
        )
        if not reduces_work:
            continue
        change = _apply_workload_target(new_cfg, row)
        if change:
            return change, None

    if not allow_canary:
        return None, None

    cooldown_guard = _workload_cooldown_guard(report, "canary")
    canary_guard = {
        "required": "aggressive_posture_clean_global_funnel_and_no_health_debt",
        "posture": policy_posture.get("posture"),
        "workload_auto_apply_allowed": bool(policy_posture.get("workload_auto_apply_allowed")),
        "funnel_safe": funnel_safe,
        "healthy": bool(health.get("healthy")),
        "clean_windows": clean_windows,
    }
    if cooldown_guard:
        canary_guard.update(cooldown_guard)
        return None, canary_guard
    canary_allowed = (
        policy_posture.get("posture") == "aggressive"
        and bool(policy_posture.get("workload_auto_apply_allowed"))
        and funnel_safe
        and health.get("healthy")
        and clean_windows >= APPLY_MIN_CLEAN_WINDOWS
    )
    if not canary_allowed:
        return None, canary_guard

    for row in actionable:
        if row.get("action") != "consider_small_workload_increase":
            continue
        current = row.get("current") or {}
        target = row.get("target") or {}
        bundle_step = int(target.get("num_bundles") or 0) - int(current.get("num_bundles") or 0)
        batch_step = int(target.get("effective_batch_size") or 0) - int(current.get("effective_batch_size") or 0)
        weight_step = int(target.get("weight") or 0) - int(current.get("weight") or 0)
        if bundle_step != 1 or batch_step > 0 or weight_step > 0:
            continue
        change = _apply_workload_target(new_cfg, row)
        if change:
            change["canary"] = True
            return change, None
    return None, canary_guard


def _plan_config_change(report: dict, cfg: dict, clean_windows: int) -> dict:
    health = _health_summary(report)
    funnel_summary = (report.get("reward_funnel") or {}).get("summary") or {}
    funnel_safe = bool(funnel_summary.get("safe_to_scale_workload", True))
    policy_posture = report.get("policy_posture") or _policy_posture(
        report,
        health,
        report.get("capacity_model") or {},
        clean_windows,
    )
    posture = policy_posture.get("posture", "balanced")
    decision = {
        "mode": AUTOPILOT_MODE,
        "healthy": health["healthy"],
        "clean_windows": clean_windows,
        "applied": False,
        "reason": "report_only",
        "changes": {},
        "health": health,
        "reward_funnel_safe": funnel_safe,
        "policy_posture": policy_posture,
    }

    if AUTOPILOT_MODE != "apply":
        return decision
    if report.get("master_config_error"):
        decision["reason"] = f"master_config_unavailable: {report['master_config_error']}"
        return decision
    recommendations = {r.get("key"): r for r in report.get("recommendations") or []}
    slots_rec = recommendations.get("resource_slots.slots") or {}
    per_rec = recommendations.get("per_challenge_max_benchmarks") or {}
    slot_signals = slots_rec.get("signals") or {}
    current_slots_for_gate = ((cfg.get("resource_slots") or {}).get("slots") or {})
    proposed_slots_for_gate = slots_rec.get("proposed") or {}
    single_gpu_serialization = (
        int(slot_signals.get("active_gpu") or 0) == 1
        and bool(proposed_slots_for_gate)
        and sum(int(proposed_slots_for_gate.get(key, 0) or 0) for key in GPU_SLOT_TYPES) == 1
        and any(
            int(current_slots_for_gate.get(key, 0) or 0) != int(proposed_slots_for_gate.get(key, 0) or 0)
            for key in GPU_SLOT_TYPES
        )
    )
    productive_idle_cpu = int(slot_signals.get("productive_idle_cpu") or 0)
    productive_idle_gpu = int(slot_signals.get("productive_idle_gpu") or 0)
    stale_roots = int(slot_signals.get("stale_roots") or health.get("stale_roots") or 0)
    stale_proofs = int(slot_signals.get("stale_proofs") or health.get("stale_proofs") or 0)
    productive_idle_cpu_scale = (
        productive_idle_cpu >= PRODUCTIVE_IDLE_CPU_SCALE_MIN
        and stale_roots <= PRODUCTIVE_IDLE_STALE_ROOT_TOLERANCE
        and stale_proofs == 0
        and funnel_safe
        and not health.get("active_unregistered")
        and not health.get("unserved_stranded_benchmarks")
    )
    productive_idle_gpu_scale = (
        productive_idle_gpu >= PRODUCTIVE_IDLE_GPU_SCALE_MIN
        and stale_proofs == 0
        and funnel_safe
        and not health.get("active_unregistered")
        and not health.get("unserved_stranded_benchmarks")
    )
    productive_capacity_scale = productive_idle_cpu_scale or productive_idle_gpu_scale
    current_per_for_gate = (cfg.get("per_challenge_max_benchmarks") or {})
    proposed_per_for_gate = per_rec.get("proposed") or {}
    safe_per_challenge_scale = (
        stale_proofs == 0
        and funnel_safe
        and not health.get("active_unregistered")
        and not health.get("unserved_stranded_benchmarks")
        and any(
            int(proposed_per_for_gate.get(key, current) or 0) > int(current or 0)
            for key, current in current_per_for_gate.items()
        )
    )
    if posture == "recovery":
        productive_capacity_scale = False
        safe_per_challenge_scale = False
    capacity_change_allowed = (
        ((health["healthy"] and funnel_safe) or productive_capacity_scale)
        and posture != "recovery"
    )
    if not funnel_safe:
        decision.setdefault("guardrails", {})["reward_funnel"] = {
            "skipped": "funnel_unhealthy_blocks_workload_scale",
            "issues": funnel_summary.get("issues", []),
            "proof_conversion_rate": funnel_summary.get("proof_conversion_rate"),
            "stopped_rate": funnel_summary.get("stopped_rate"),
            "avg_time_to_proof_submit_sec": funnel_summary.get("avg_time_to_proof_submit_sec"),
        }
        drain_issues = {
            "slow_time_to_proof_submission",
            "low_proof_conversion",
            "high_stopped_or_expired_rate",
            "high_unexpected_stopped_rate",
        }
        active_issues = set(funnel_summary.get("issues") or [])
        current = int(cfg.get("max_concurrent_benchmarks") or 0)
        drain_floor = max(MIN_MAX_BENCHMARKS, FUNNEL_DRAIN_MIN_MAX_BENCHMARKS)
        if current > drain_floor and active_issues.intersection(drain_issues):
            next_max = max(drain_floor, current - max(1, MAX_BENCHMARK_DOWN_STEP))
            new_cfg = json.loads(json.dumps(cfg))
            new_cfg["max_concurrent_benchmarks"] = next_max
            decision["reason"] = "drain_unhealthy_reward_funnel"
            decision["changes"] = {
                "max_concurrent_benchmarks": {
                    "current": current,
                    "next": next_max,
                    "issues": funnel_summary.get("issues", []),
                    "proof_conversion_rate": funnel_summary.get("proof_conversion_rate"),
                    "unexpected_stopped_rate": funnel_summary.get("unexpected_stopped_rate"),
                    "avg_time_to_proof_submit_sec": funnel_summary.get("avg_time_to_proof_submit_sec"),
                    "drain_floor": drain_floor,
                }
            }
            decision["config"] = new_cfg
            return decision
    if health.get("unserved_stranded_benchmarks"):
        current = int(cfg.get("max_concurrent_benchmarks") or 0)
        active_jobs = _active_unfinished_jobs()
        stranded_count = len(health["unserved_stranded_benchmarks"])
        productive_jobs = max(0, active_jobs - stranded_count)
        gpu_slot_total, _ = _gpu_slot_counts(report)
        active_gpu_reserve = max(
            _active_gpu_slave_count(report),
            int((health.get("live_by_profile") or {}).get("gpu") or 0),
        )
        gpu_reserve = min(gpu_slot_total, active_gpu_reserve) if gpu_slot_total else active_gpu_reserve
        drain_target = _clamp(
            productive_jobs + STRANDED_BUFFER_BENCHMARKS + gpu_reserve,
            MIN_MAX_BENCHMARKS,
            MAX_MAX_BENCHMARKS,
        )
        next_max = current
        if current > drain_target:
            next_max = max(drain_target, current - STRANDED_DOWNSCALE_STEP)
        stranded_plan = {
            "current": current,
            "target": drain_target,
            "next": next_max,
            "active_jobs": active_jobs,
            "productive_jobs": productive_jobs,
            "buffer": STRANDED_BUFFER_BENCHMARKS,
            "gpu_reserve": gpu_reserve,
            "configured_gpu_slots": gpu_slot_total,
            "active_gpu_reserve": active_gpu_reserve,
            "stranded": health["unserved_stranded_benchmarks"],
            "capacity_waiting": health.get("capacity_waiting_benchmarks", []),
        }
        if next_max != current:
            new_cfg = json.loads(json.dumps(cfg))
            new_cfg["max_concurrent_benchmarks"] = next_max
            decision["reason"] = "drain_stranded_benchmarks"
            decision["changes"] = {
                "max_concurrent_benchmarks": stranded_plan
            }
            decision["config"] = new_cfg
            return decision
        decision.setdefault("guardrails", {})["stranded_benchmarks"] = {
            **stranded_plan,
            "skipped": "already_at_or_below_drain_target",
        }

    rollback_cfg = json.loads(json.dumps(cfg))
    canary_rollback = _rollback_last_canary(
        rollback_cfg,
        report,
        health,
        funnel_safe,
        policy_posture,
    )
    if canary_rollback:
        decision["reason"] = "workload_canary_rollback"
        decision["changes"] = {"workload_controller": canary_rollback}
        decision["config"] = rollback_cfg
        return decision

    safety_cfg = json.loads(json.dumps(cfg))
    workload_safety_change, workload_safety_guard = _next_workload_change(
        safety_cfg,
        report,
        report.get("workload_targets") or {},
        health,
        funnel_safe,
        policy_posture,
        clean_windows,
        allow_canary=False,
    )
    if workload_safety_change:
        decision["reason"] = "workload_safety_adjustment"
        decision["changes"] = {"workload_controller": workload_safety_change}
        decision["config"] = safety_cfg
        return decision
    if workload_safety_guard:
        decision.setdefault("guardrails", {})["workload_controller"] = workload_safety_guard

    if not health["healthy"] and not productive_capacity_scale and not safe_per_challenge_scale and not single_gpu_serialization:
        decision["reason"] = "blocked_by_stale_or_unregistered_work"
        return decision
    if clean_windows < APPLY_MIN_CLEAN_WINDOWS and not productive_capacity_scale and not safe_per_challenge_scale and not single_gpu_serialization:
        decision["reason"] = "waiting_for_clean_windows"
        return decision

    new_cfg = json.loads(json.dumps(cfg))
    changes: dict[str, dict] = {}

    current_slots = ((new_cfg.get("resource_slots") or {}).get("slots") or {})
    if slots_rec and current_slots and (capacity_change_allowed or single_gpu_serialization):
        proposed_slots = slots_rec.get("proposed") or {}
        next_slots = dict(current_slots)
        for key, target in proposed_slots.items():
            if single_gpu_serialization and not capacity_change_allowed and key not in GPU_SLOT_TYPES:
                continue
            current = int(current_slots.get(key, 0) or 0)
            target = int(target or 0)
            next_slots[key] = _next_value_bounded(current, target, SLOT_UP_STEP, SLOT_DOWN_STEP)
        if next_slots != current_slots:
            new_cfg.setdefault("resource_slots", {})["slots"] = next_slots
            changes["resource_slots.slots"] = {
                "current": current_slots,
                "target": proposed_slots,
                "next": next_slots,
                "signals": slot_signals,
            }

    max_rec = recommendations.get("max_concurrent_benchmarks")
    if max_rec and new_cfg.get("max_concurrent_benchmarks") is not None and capacity_change_allowed:
        current = int(new_cfg.get("max_concurrent_benchmarks") or 0)
        target = int(max_rec.get("proposed") or current)
        active_jobs = _active_unfinished_jobs()
        gpu_needs_room = _gpu_capacity_needs_benchmark_room(report)
        if target < current:
            # Healthy slot-capacity changes can fluctuate when slaves appear or
            # go quiet briefly. Only stranded-benchmark drain mode is allowed to
            # lower max_concurrent_benchmarks automatically.
            decision.setdefault("guardrails", {})["max_concurrent_benchmarks"] = {
                "skipped": "downscale_requires_stranded_benchmarks",
                "current": current,
                "target": target,
                "active_jobs": active_jobs,
            }
        elif (
            target > current
            and active_jobs < max(1, current - 1)
            and not gpu_needs_room
            and not productive_capacity_scale
        ):
            decision.setdefault("guardrails", {})["max_concurrent_benchmarks"] = {
                "skipped": "upscale_requires_saturated_precommit_capacity",
                "current": current,
                "target": target,
                "active_jobs": active_jobs,
                "signals": max_rec.get("signals") or {},
            }
        else:
            max_up_step = 1 if posture == "conservative" else MAX_BENCHMARK_UP_STEP
            next_max = _next_value_bounded(current, target, max_up_step, MAX_BENCHMARK_DOWN_STEP)
            if next_max != current:
                new_cfg["max_concurrent_benchmarks"] = next_max
                changes["max_concurrent_benchmarks"] = {
                    "current": current,
                    "target": target,
                    "next": next_max,
                    "active_jobs": active_jobs,
                    "gpu_capacity_needs_room": gpu_needs_room,
                    "productive_capacity_scale": productive_capacity_scale,
                    "policy_posture": posture,
                    "signals": max_rec.get("signals") or {},
                }

    current_per = new_cfg.get("per_challenge_max_benchmarks") or {}
    if per_rec and current_per and (capacity_change_allowed or safe_per_challenge_scale or single_gpu_serialization):
        proposed_per = per_rec.get("proposed") or {}
        next_per = dict(current_per)
        per_changes = {}
        for key, proposed_value in proposed_per.items():
            current = int(current_per.get(key, 0) or 0)
            target = int(current if proposed_value is None else proposed_value)
            if target > current or (single_gpu_serialization and key in {"c004", "c005", "c006"} and target < current):
                next_value = _next_value_bounded(current, target, 1, 1)
                next_per[key] = next_value
                per_changes[key] = {
                    "current": current,
                    "target": target,
                    "next": next_value,
                }
        if per_changes:
            new_cfg["per_challenge_max_benchmarks"] = next_per
            changes["per_challenge_max_benchmarks"] = per_changes

    caps_rec = recommendations.get("adaptive_slave_caps")
    current_caps = new_cfg.get("adaptive_slave_caps") or {}
    if caps_rec and current_caps and capacity_change_allowed:
        proposed_caps = caps_rec.get("proposed") or {}
        next_caps = dict(current_caps)
        cap_changes = {}
        for key in ("cpu_max_cap", "gpu_max_cap"):
            current = int(current_caps.get(key, 0) or 0)
            target = int(proposed_caps.get(key, current) or current)
            if target > current:
                next_value = current + 1
                next_caps[key] = next_value
                cap_changes[key] = {
                    "current": current,
                    "target": target,
                    "next": next_value,
                }
        if cap_changes:
            new_cfg["adaptive_slave_caps"] = next_caps
            changes["adaptive_slave_caps"] = {
                "changes": cap_changes,
                "signals": caps_rec.get("signals") or {},
            }

    if not changes:
        workload_canary_change, workload_guard = _next_workload_change(
            new_cfg,
            report,
            report.get("workload_targets") or {},
            health,
            funnel_safe,
            policy_posture,
            clean_windows,
            allow_canary=True,
        )
        if workload_canary_change:
            changes["workload_controller"] = workload_canary_change
        elif workload_guard:
            decision.setdefault("guardrails", {})["workload_controller"] = workload_guard

    if not changes:
        decision["reason"] = "no_safe_changes"
        return decision

    decision["reason"] = "ready_to_apply"
    decision["changes"] = changes
    decision["config"] = new_cfg
    return decision


def _scale_readiness_summary(
    cfg: dict,
    health: dict,
    policy_posture: dict,
    capacity: dict,
    reward_funnel: dict,
    stale_totals: dict,
    target_slots: dict,
) -> dict:
    funnel_summary = (reward_funnel or {}).get("summary") or {}
    current_slots = ((cfg.get("resource_slots") or {}).get("slots") or {}) if cfg else {}
    current_max = cfg.get("max_concurrent_benchmarks") if cfg else None
    target_max = (
        _target_max_concurrent_benchmarks(capacity, target_slots)
        if capacity and target_slots
        else None
    )
    blockers = []
    if int(stale_totals.get("roots") or 0) or int(stale_totals.get("proofs") or 0):
        blockers.append("stale_work")
    if health.get("active_unregistered"):
        blockers.append("active_unregistered_workers")
    if health.get("unserved_stranded_benchmarks"):
        blockers.append("unserved_stranded_precommits")
    if not funnel_summary.get("safe_to_scale_workload", True):
        blockers.append("reward_funnel_unsafe")
    if policy_posture.get("posture") == "recovery":
        blockers.append("policy_posture_recovery")

    if blockers:
        gate = "blocked"
    elif policy_posture.get("posture") in {"aggressive", "balanced"}:
        gate = "ready"
    else:
        gate = "caution"

    remediation = []
    if "stale_work" in blockers:
        remediation.append("clear stale root/proof assignments before raising workload")
    if "unserved_stranded_precommits" in blockers:
        remediation.append("restore matching capacity or cleanup orphaned stranded benchmarks")
    if "active_unregistered_workers" in blockers:
        remediation.append("register or deactivate active unregistered public workers")
    if "reward_funnel_unsafe" in blockers:
        remediation.append("wait for roots to convert into benchmark/proof submissions")

    return {
        "gate": gate,
        "blockers": blockers,
        "remediation": remediation,
        "posture": policy_posture.get("posture"),
        "active_cpu": capacity.get("active_cpu"),
        "active_gpu": capacity.get("active_gpu"),
        "productive_idle_cpu": capacity.get("productive_idle_cpu"),
        "productive_idle_gpu": capacity.get("productive_idle_gpu"),
        "current": {
            "max_concurrent_benchmarks": current_max,
            "cpu_slots": current_slots.get(CPU_SLOT_TYPE),
            "gpu_slots_total": sum(int(current_slots.get(key, 0) or 0) for key in GPU_SLOT_TYPES),
        },
        "targets": {
            "max_concurrent_benchmarks": target_max,
            "cpu_slots": target_slots.get(CPU_SLOT_TYPE),
            "gpu_slots_total": sum(int(target_slots.get(key, 0) or 0) for key in GPU_SLOT_TYPES),
        },
        "reward_funnel": {
            "safe_to_scale_workload": funnel_summary.get("safe_to_scale_workload"),
            "issues": funnel_summary.get("issues") or [],
            "proof_conversion_rate": funnel_summary.get("proof_conversion_rate"),
            "stopped_rate": funnel_summary.get("stopped_rate"),
            "avg_time_to_proof_submit_sec": funnel_summary.get("avg_time_to_proof_submit_sec"),
        },
    }


def maybe_run():
    """Run the autopilot background loop when AUTOPILOT_MODE is report/apply."""
    global _last_run_ts
    if AUTOPILOT_MODE not in {"report", "apply"}:
        return None
    now = time.time()
    if now - _last_run_ts < RUN_INTERVAL_S:
        return None
    _last_run_ts = now

    now_ms = int(time.time() * 1000)
    cfg, cfg_error = _fetch_master_config()
    cleanup = _cleanup_stale_assignments(cfg, now_ms)
    report = build_report()
    if cfg_error and not report.get("master_config_error"):
        report["master_config_error"] = cfg_error

    health = _health_summary(report)
    clean_windows = int(db.get_setting("autopilot_clean_windows", "0") or 0)
    clean_windows = clean_windows + 1 if health["healthy"] else 0
    db.set_setting("autopilot_clean_windows", str(clean_windows))

    decision = _plan_config_change(report, cfg, clean_windows)
    decision["cleanup"] = cleanup
    if (
        cleanup.get("released_roots")
        or cleanup.get("released_orphan_roots")
        or cleanup.get("released_proofs")
        or cleanup.get("expiry_released_roots")
        or cleanup.get("expiry_released_proofs")
        or cleanup.get("stopped_precommits")
    ):
        decision.setdefault("changes", {})["stale_cleanup"] = {
            "released_roots": cleanup.get("released_roots", []),
            "released_orphan_roots": cleanup.get("released_orphan_roots", []),
            "released_proofs": cleanup.get("released_proofs", []),
            "expiry_released_roots": cleanup.get("expiry_released_roots", []),
            "expiry_released_proofs": cleanup.get("expiry_released_proofs", []),
            "stopped_precommits": cleanup.get("stopped_precommits", []),
        }
    if decision.get("config"):
        _push_config(decision["config"])
        decision["applied"] = True
        workload_change = (decision.get("changes") or {}).get("workload_controller")
        if workload_change:
            cooldown_state = dict(report.get("workload_cooldown") or {})
            cooldown_state["last_change_ms"] = now_ms
            cooldown_state["last_change"] = {
                "algorithm_id": workload_change.get("algorithm_id"),
                "track": workload_change.get("track"),
                "action": workload_change.get("action"),
                "changes": workload_change.get("changes"),
                "canary": bool(workload_change.get("canary")),
                "canary_rollback": bool(workload_change.get("canary_rollback")),
            }
            if workload_change.get("canary"):
                cooldown_state["last_canary_change_ms"] = now_ms
            elif workload_change.get("canary_rollback"):
                cooldown_state["last_canary_rollback_ms"] = now_ms
            else:
                cooldown_state["last_safety_change_ms"] = now_ms
            db.set_setting("autopilot_workload_cooldown", json.dumps(cooldown_state, separators=(",", ":")))
            decision["workload_cooldown"] = cooldown_state
        if decision.get("reason") == "ready_to_apply":
            decision["reason"] = "applied"
        elif decision.get("reason") == "drain_stranded_benchmarks":
            decision["reason"] = "applied_drain_stranded_benchmarks"

    _save_decision(report, decision)
    logger.info(
        "autopilot mode=%s healthy=%s clean_windows=%s applied=%s reason=%s changes=%s",
        AUTOPILOT_MODE,
        decision.get("healthy"),
        clean_windows,
        decision.get("applied"),
        decision.get("reason"),
        list((decision.get("changes") or {}).keys()),
    )
    return decision


def build_report() -> dict:
    now_ms = int(time.time() * 1000)
    cfg, cfg_error = _fetch_master_config()
    slaves = _slave_metrics(now_ms)
    challenges = _challenge_metrics(now_ms)
    slots = _slot_metrics(now_ms)
    stranded = _stranded_benchmarks(now_ms)
    stale_totals = _stale_totals(now_ms)
    workload = _track_workload_metrics(now_ms)
    reward_funnel = _reward_funnel_summary(now_ms)
    track_economics = _track_config_economics(cfg, workload) if cfg else []
    capacity = _fleet_capacity(cfg, slaves, challenges, slots, stale_totals) if cfg else {}
    health = _health_summary({
        "slaves": slaves,
        "challenges": challenges,
        "slots": slots,
        "stale_totals": stale_totals,
        "stranded_benchmarks": stranded,
        "reward_funnel": reward_funnel,
    })
    try:
        clean_windows = int(db.get_setting("autopilot_clean_windows", "0") or 0)
    except Exception:
        clean_windows = None
    try:
        workload_cooldown = json.loads(db.get_setting("autopilot_workload_cooldown", "{}") or "{}")
    except Exception:
        workload_cooldown = {}
    policy_posture = _policy_posture(
        {
            "slaves": slaves,
            "challenges": challenges,
            "slots": slots,
            "stale_totals": stale_totals,
            "stranded_benchmarks": stranded,
            "reward_funnel": reward_funnel,
        },
        health,
        capacity,
        clean_windows,
    ) if cfg else {}
    workload_targets = (
        _workload_controller_targets(cfg, track_economics, reward_funnel, policy_posture)
        if cfg
        else {}
    )
    target_slots = _target_resource_slots(capacity) if capacity else {}
    recommendations = (
        _recommendations(
            cfg,
            slaves,
            challenges,
            slots,
            track_economics,
            stale_totals,
            reward_funnel,
            workload_targets,
        )
        if cfg
        else []
    )

    active_counts = {
        "cpu": sum(1 for s in slaves if s["profile"] == "cpu" and _counts_for_capacity(s)),
        "gpu": sum(1 for s in slaves if s["profile"] == "gpu" and _counts_for_capacity(s)),
    }
    scale_readiness = (
        _scale_readiness_summary(
            cfg,
            health,
            policy_posture,
            capacity,
            reward_funnel,
            stale_totals,
            target_slots,
        )
        if cfg
        else {}
    )

    return {
        "mode": "read_only",
        "generated_at_ms": now_ms,
        "windows": {
            "active_window_ms": ACTIVE_WINDOW_MS,
            "metric_window_ms": METRIC_WINDOW_MS,
            "stale_root_ms": STALE_ROOT_MS,
            "stale_proof_ms": STALE_PROOF_MS,
        },
        "master_config_error": cfg_error,
        "current_config": _current_config_summary(cfg),
        "active_slave_counts": active_counts,
        "slaves": slaves,
        "slots": slots,
        "challenges": challenges,
        "stale_totals": stale_totals,
        "track_workload": workload,
        "reward_funnel": reward_funnel,
        "policy_posture": policy_posture,
        "scale_readiness": scale_readiness,
        "workload_cooldown": workload_cooldown,
        "workload_targets": workload_targets,
        "track_economics": track_economics,
        "capacity_model": capacity,
        "capacity_targets": {
            "resource_slots": target_slots,
            "max_concurrent_benchmarks": (
                _target_max_concurrent_benchmarks(capacity, target_slots)
                if capacity
                else None
            ),
            "per_challenge_max_benchmarks": (
                _target_per_challenge_caps(cfg, capacity, target_slots)
                if capacity
                else {}
            ),
            "adaptive_slave_caps": (
                _target_adaptive_slave_caps(capacity)
                if capacity
                else {}
            ),
        },
        "stranded_benchmarks": stranded,
        "stranded_classification": {
            "unserved": health.get("unserved_stranded_benchmarks", []),
            "capacity_waiting": health.get("capacity_waiting_benchmarks", []),
            "live_by_profile": health.get("live_by_profile", {}),
            "slot_capacity": health.get("slot_capacity", {}),
        },
        "recommendations": recommendations,
    }
