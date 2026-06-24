"""
Pool autopilot report and guarded controller.

The report path is always read-only. The background controller can optionally
apply small config changes when AUTOPILOT_MODE=apply and the pool has been clean
for enough consecutive windows.
"""
from __future__ import annotations

import json
import logging
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
MAX_MAX_BENCHMARKS = int(os.environ.get("AUTOPILOT_MAX_MAX_BENCHMARKS", "32"))
APPLY_MIN_CLEAN_WINDOWS = int(os.environ.get("AUTOPILOT_APPLY_MIN_CLEAN_WINDOWS", "2"))
MAX_BENCHMARK_STEP = int(os.environ.get("AUTOPILOT_MAX_BENCHMARK_STEP", "2"))
SLOT_STEP = int(os.environ.get("AUTOPILOT_SLOT_STEP", "1"))
MAX_CPU_SLOTS = int(os.environ.get("AUTOPILOT_MAX_CPU_SLOTS", "64"))
MAX_GPU_SLOTS_PER_TYPE = int(os.environ.get("AUTOPILOT_MAX_GPU_SLOTS_PER_TYPE", "6"))
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

GPU_CHALLENGES = {"vector_search", "hypergraph", "neuralnet_optimizer"}
GPU_SLOT_TYPES = ("vector_search", "hypergraph", "neuralnet_optimizer")
CPU_SLOT_TYPE = "cpu"
_last_run_ts = 0.0
_decision_table_ready = False


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
        "released_roots": [],
        "released_proofs": [],
        "skipped": "",
    }
    if not STALE_CLEANUP_ENABLED:
        result["skipped"] = "disabled"
        return result
    if not cfg:
        result["skipped"] = "missing_config"
        return result

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
                COUNT(*) FILTER (
                    WHERE ready IS NULL
                      AND start_time IS NOT NULL
                      AND start_time < %s
                ) AS stale_proofs
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
            COALESCE(rs.stale_roots, 0) AS stale_roots,
            COALESCE(ps.stale_proofs, 0) AS stale_proofs,
            COALESCE(rs.failed_recent, 0) AS failed_recent,
            COALESCE(rs.nonces_recent, 0) AS nonces_recent,
            rs.avg_runtime_sec,
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
            now_ms - STALE_PROOF_MS,
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
            COUNT(pb.*) FILTER (
                WHERE pb.ready IS NULL
                  AND pb.start_time IS NOT NULL
                  AND pb.start_time < %s
            ) AS stale_proofs,
            COUNT(rb.*) FILTER (WHERE rb.ready = true AND rb.end_time >= %s) AS roots_done_recent,
            ROUND(AVG(rb.end_time - rb.start_time) FILTER (
                WHERE rb.ready = true AND rb.end_time >= %s AND rb.end_time IS NOT NULL
            ) / 1000.0, 1) AS avg_root_runtime_sec
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
        (now_ms - STALE_ROOT_MS, now_ms - STALE_PROOF_MS, cutoff_metrics, cutoff_metrics),
    )


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


def _recommendations(cfg: dict, slaves: list[dict], challenges: list[dict], slots: dict) -> list[dict]:
    active_cpu = [s for s in slaves if s["profile"] == "cpu" and _counts_for_capacity(s)]
    active_gpu = [s for s in slaves if s["profile"] == "gpu" and _counts_for_capacity(s)]
    stale_roots = sum(int(s.get("stale_roots") or 0) for s in slaves) + sum(
        int(c.get("stale_roots") or 0) for c in challenges
    )
    stale_proofs = sum(int(s.get("stale_proofs") or 0) for s in slaves) + sum(
        int(c.get("stale_proofs") or 0) for c in challenges
    )
    stale_total = stale_roots + stale_proofs

    current_slots = (cfg.get("resource_slots") or {}).get("slots", {})
    slot_counts: dict[str, int] = {}
    for row in slots.get("summary", []):
        slot_type = row.get("slot_type")
        slot_counts.setdefault(slot_type, 0)
        slot_counts[slot_type] += int(row.get("count") or 0)

    cpu_pressure = sum(int(s.get("active_unfinished") or 0) for s in active_cpu)
    gpu_pressure = sum(int(s.get("active_unfinished") or 0) for s in active_gpu)
    slot_idle = {
        row.get("slot_type"): int(row.get("count") or 0)
        for row in slots.get("summary", [])
        if row.get("state") == "idle"
    }

    recommended_cpu_slots = int(current_slots.get(CPU_SLOT_TYPE, slot_counts.get(CPU_SLOT_TYPE, 0)) or 0)
    if active_cpu:
        if stale_total:
            recommended_cpu_slots = max(2, recommended_cpu_slots - 1)
        elif slot_idle.get(CPU_SLOT_TYPE, 0) == 0 and cpu_pressure >= max(1, recommended_cpu_slots):
            recommended_cpu_slots = min(recommended_cpu_slots + 2, MAX_CPU_SLOTS)
        else:
            recommended_cpu_slots = max(recommended_cpu_slots, min(12, max(2, len(active_cpu) * 2)))
    else:
        recommended_cpu_slots = 0

    recommendations = []
    if current_slots:
        proposed_slots = dict(current_slots)
        proposed_slots[CPU_SLOT_TYPE] = recommended_cpu_slots
        for slot_type in GPU_SLOT_TYPES:
            current = int(current_slots.get(slot_type, slot_counts.get(slot_type, 0)) or 0)
            if active_gpu and stale_total == 0 and slot_idle.get(slot_type, 0) == 0 and gpu_pressure:
                proposed_slots[slot_type] = min(current + 1, MAX_GPU_SLOTS_PER_TYPE)
            elif not active_gpu:
                proposed_slots[slot_type] = 0
            else:
                proposed_slots[slot_type] = current
        if proposed_slots != current_slots:
            recommendations.append({
                "key": "resource_slots.slots",
                "current": current_slots,
                "proposed": proposed_slots,
                "reason": "Slot pressure is inferred from active slaves, idle slots, and stale unfinished work.",
                "apply_now": False,
            })

    gpu_slot_total = sum(int(current_slots.get(k, 0) or 0) for k in GPU_SLOT_TYPES) if active_gpu else 0
    cpu_slot_total = int(current_slots.get(CPU_SLOT_TYPE, 0) or 0) if active_cpu else 0
    proposed_max = _clamp(cpu_slot_total + gpu_slot_total, MIN_MAX_BENCHMARKS, MAX_MAX_BENCHMARKS)
    current_max = cfg.get("max_concurrent_benchmarks")
    if current_max is not None and proposed_max != int(current_max):
        recommendations.append({
            "key": "max_concurrent_benchmarks",
            "current": current_max,
            "proposed": proposed_max,
            "reason": "Concurrent benchmark target should follow active CPU/GPU slot capacity.",
            "apply_now": False,
        })

    current_per = cfg.get("per_challenge_max_benchmarks", {}) or {}
    proposed_per = dict(current_per)
    if active_gpu:
        # c004/vector-search is very fast, so keep extra benchmark buffer instead
        # of mapping it strictly one-for-one to vector slots.
        current_c004 = int(current_per.get("c004", 1) or 1)
        proposed_per.update({
            "c004": max(current_c004, int(current_slots.get("vector_search", 1) or 1)),
            "c005": max(1, int(current_slots.get("hypergraph", 1) or 1)),
            "c006": max(1, int(current_slots.get("neuralnet_optimizer", 1) or 1)),
        })
    else:
        proposed_per.update({"c004": 1, "c005": 1, "c006": 1})
    if proposed_per != current_per:
        recommendations.append({
            "key": "per_challenge_max_benchmarks",
            "current": current_per,
            "proposed": proposed_per,
            "reason": "GPU challenge benchmark caps should match currently available GPU slot types.",
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

    if stale_proofs:
        recommendations.append({
            "key": "proof_queue",
            "current": {"stale_proofs": stale_proofs},
            "proposed": "check root-artifact ownership and proof slave logs",
            "reason": "Proof batches should normally clear quickly once roots are ready.",
            "apply_now": False,
        })

    return recommendations


def _health_summary(report: dict) -> dict:
    slaves = report.get("slaves") or []
    challenges = report.get("challenges") or []
    stale_roots = sum(int(s.get("stale_roots") or 0) for s in slaves) + sum(
        int(c.get("stale_roots") or 0) for c in challenges
    )
    stale_proofs = sum(int(s.get("stale_proofs") or 0) for s in slaves) + sum(
        int(c.get("stale_proofs") or 0) for c in challenges
    )
    active_unregistered = [
        s["slave_name"]
        for s in slaves
        if s.get("active_now")
        and _is_public_member_slave(str(s.get("slave_name") or ""))
        and not s.get("registered")
    ]
    stranded = report.get("stranded_benchmarks") or []
    return {
        "stale_roots": stale_roots,
        "stale_proofs": stale_proofs,
        "active_unregistered": active_unregistered,
        "stranded_benchmarks": stranded,
        "healthy": stale_roots == 0 and stale_proofs == 0 and not active_unregistered and not stranded,
    }


def _next_value(current: int, target: int, step: int) -> int:
    if target > current:
        return min(target, current + step)
    if target < current:
        return max(target, current - step)
    return current


def _plan_config_change(report: dict, cfg: dict, clean_windows: int) -> dict:
    health = _health_summary(report)
    decision = {
        "mode": AUTOPILOT_MODE,
        "healthy": health["healthy"],
        "clean_windows": clean_windows,
        "applied": False,
        "reason": "report_only",
        "changes": {},
        "health": health,
    }

    if AUTOPILOT_MODE != "apply":
        return decision
    if report.get("master_config_error"):
        decision["reason"] = f"master_config_unavailable: {report['master_config_error']}"
        return decision
    if health.get("stranded_benchmarks"):
        current = int(cfg.get("max_concurrent_benchmarks") or 0)
        active_jobs = _active_unfinished_jobs()
        stranded_count = len(health["stranded_benchmarks"])
        productive_jobs = max(0, active_jobs - stranded_count)
        drain_target = _clamp(
            productive_jobs + STRANDED_BUFFER_BENCHMARKS,
            MIN_MAX_BENCHMARKS,
            MAX_MAX_BENCHMARKS,
        )
        next_max = current
        if current > drain_target:
            next_max = max(drain_target, current - STRANDED_DOWNSCALE_STEP)
        decision["reason"] = "drain_stranded_benchmarks"
        decision["changes"] = {
            "max_concurrent_benchmarks": {
                "current": current,
                "target": drain_target,
                "next": next_max,
                "active_jobs": active_jobs,
                "productive_jobs": productive_jobs,
                "buffer": STRANDED_BUFFER_BENCHMARKS,
                "stranded": health["stranded_benchmarks"],
            }
        }
        if next_max != current:
            new_cfg = json.loads(json.dumps(cfg))
            new_cfg["max_concurrent_benchmarks"] = next_max
            decision["config"] = new_cfg
        else:
            decision["reason"] = "stranded_benchmarks_at_drain_target"
        return decision
    if not health["healthy"]:
        decision["reason"] = "blocked_by_stale_or_unregistered_work"
        return decision
    if clean_windows < APPLY_MIN_CLEAN_WINDOWS:
        decision["reason"] = "waiting_for_clean_windows"
        return decision

    new_cfg = json.loads(json.dumps(cfg))
    changes: dict[str, dict] = {}
    recommendations = {r.get("key"): r for r in report.get("recommendations") or []}

    slots_rec = recommendations.get("resource_slots.slots")
    current_slots = ((new_cfg.get("resource_slots") or {}).get("slots") or {})
    if slots_rec and current_slots:
        proposed_slots = slots_rec.get("proposed") or {}
        next_slots = dict(current_slots)
        for key, target in proposed_slots.items():
            current = int(current_slots.get(key, 0) or 0)
            target = int(target or 0)
            next_slots[key] = _next_value(current, target, SLOT_STEP)
        if next_slots != current_slots:
            new_cfg.setdefault("resource_slots", {})["slots"] = next_slots
            changes["resource_slots.slots"] = {
                "current": current_slots,
                "target": proposed_slots,
                "next": next_slots,
            }

    max_rec = recommendations.get("max_concurrent_benchmarks")
    if max_rec and new_cfg.get("max_concurrent_benchmarks") is not None:
        current = int(new_cfg.get("max_concurrent_benchmarks") or 0)
        target = int(max_rec.get("proposed") or current)
        active_jobs = _active_unfinished_jobs()
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
        elif target > current and active_jobs < max(1, current - 1):
            decision.setdefault("guardrails", {})["max_concurrent_benchmarks"] = {
                "skipped": "upscale_requires_saturated_precommit_capacity",
                "current": current,
                "target": target,
                "active_jobs": active_jobs,
            }
        else:
            next_max = _next_value(current, target, MAX_BENCHMARK_STEP)
            if next_max != current:
                new_cfg["max_concurrent_benchmarks"] = next_max
                changes["max_concurrent_benchmarks"] = {
                    "current": current,
                    "target": target,
                    "next": next_max,
                    "active_jobs": active_jobs,
                }

    if not changes:
        decision["reason"] = "no_safe_changes"
        return decision

    decision["reason"] = "ready_to_apply"
    decision["changes"] = changes
    decision["config"] = new_cfg
    return decision


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
    if cleanup.get("released_roots") or cleanup.get("released_proofs"):
        decision.setdefault("changes", {})["stale_cleanup"] = {
            "released_roots": cleanup.get("released_roots", []),
            "released_proofs": cleanup.get("released_proofs", []),
        }
    if decision.get("config"):
        _push_config(decision["config"])
        decision["applied"] = True
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
    recommendations = _recommendations(cfg, slaves, challenges, slots) if cfg else []

    active_counts = {
        "cpu": sum(1 for s in slaves if s["profile"] == "cpu" and _counts_for_capacity(s)),
        "gpu": sum(1 for s in slaves if s["profile"] == "gpu" and _counts_for_capacity(s)),
    }

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
        "stranded_benchmarks": stranded,
        "recommendations": recommendations,
    }
