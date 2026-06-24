"""
Read-only pool autopilot report.

This module deliberately does not mutate master config. It collects the signals
needed to tune the pool safely, then returns "would change" recommendations for
the operator to review.
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
ACTIVE_WINDOW_MS = int(os.environ.get("AUTOPILOT_ACTIVE_WINDOW_MS", str(10 * 60 * 1000)))
METRIC_WINDOW_MS = int(os.environ.get("AUTOPILOT_METRIC_WINDOW_MS", str(30 * 60 * 1000)))
STALE_ROOT_MS = int(os.environ.get("AUTOPILOT_STALE_ROOT_MS", str(45 * 60 * 1000)))
STALE_PROOF_MS = int(os.environ.get("AUTOPILOT_STALE_PROOF_MS", str(20 * 60 * 1000)))
MIN_MAX_BENCHMARKS = int(os.environ.get("AUTOPILOT_MIN_MAX_BENCHMARKS", "3"))
MAX_MAX_BENCHMARKS = int(os.environ.get("AUTOPILOT_MAX_MAX_BENCHMARKS", "32"))

GPU_CHALLENGES = {"vector_search", "hypergraph", "neuralnet_optimizer"}
GPU_SLOT_TYPES = ("vector_search", "hypergraph", "neuralnet_optimizer")
CPU_SLOT_TYPE = "cpu"


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


def _fetch_master_config() -> tuple[dict, str | None]:
    try:
        with urllib.request.urlopen(f"{MASTER_URL}/get-config", timeout=5) as resp:
            return json.loads(resp.read()), None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {}, str(exc)


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


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
            COALESCE(r.active, true) AS registered_active,
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


def _recommendations(cfg: dict, slaves: list[dict], challenges: list[dict], slots: dict) -> list[dict]:
    active_cpu = [s for s in slaves if s["profile"] == "cpu" and s["active_now"] and s.get("registered_active")]
    active_gpu = [s for s in slaves if s["profile"] == "gpu" and s["active_now"] and s.get("registered_active")]
    stale_roots = sum(int(s.get("stale_roots") or 0) for s in slaves)
    stale_proofs = sum(int(s.get("stale_proofs") or 0) for s in slaves)

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
        if stale_roots:
            recommended_cpu_slots = max(2, recommended_cpu_slots - 1)
        elif slot_idle.get(CPU_SLOT_TYPE, 0) == 0 and cpu_pressure >= max(1, recommended_cpu_slots):
            recommended_cpu_slots = min(recommended_cpu_slots + 2, 24)
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
            if active_gpu and stale_roots == 0 and slot_idle.get(slot_type, 0) == 0 and gpu_pressure:
                proposed_slots[slot_type] = min(current + 1, 6)
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
        proposed_per.update({
            "c004": max(1, int(current_slots.get("vector_search", 1) or 1)),
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


def build_report() -> dict:
    now_ms = int(time.time() * 1000)
    cfg, cfg_error = _fetch_master_config()
    slaves = _slave_metrics(now_ms)
    challenges = _challenge_metrics(now_ms)
    slots = _slot_metrics(now_ms)
    recommendations = _recommendations(cfg, slaves, challenges, slots) if cfg else []

    active_counts = {
        "cpu": sum(1 for s in slaves if s["profile"] == "cpu" and s["active_now"] and s.get("registered_active")),
        "gpu": sum(1 for s in slaves if s["profile"] == "gpu" and s["active_now"] and s.get("registered_active")),
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
        "recommendations": recommendations,
    }
