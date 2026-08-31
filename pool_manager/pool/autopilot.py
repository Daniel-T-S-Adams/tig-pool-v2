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
import re
import threading
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
MAX_MAX_BENCHMARKS = int(os.environ.get("AUTOPILOT_MAX_MAX_BENCHMARKS", "192"))
# Not a TIG protocol constant. Added 2026-07-18 as an InnoPool safety guess
# after a suspected upstream reject. Live pools have run above 100 (e.g. 116).
# Honor it only when the operator sets the env explicitly.
_TIG_UNRESOLVED_LIMIT_RAW = os.environ.get("AUTOPILOT_TIG_UNRESOLVED_BENCHMARK_LIMIT")
TIG_UNRESOLVED_BENCHMARK_LIMIT = (
    int(_TIG_UNRESOLVED_LIMIT_RAW) if _TIG_UNRESOLVED_LIMIT_RAW not in (None, "") else 0
)
TIG_UNRESOLVED_BENCHMARK_HEADROOM = int(os.environ.get("AUTOPILOT_TIG_UNRESOLVED_BENCHMARK_HEADROOM", "10"))
_UPSTREAM_SAFE_RAW = os.environ.get("AUTOPILOT_UPSTREAM_SAFE_MAX_BENCHMARKS")
if _UPSTREAM_SAFE_RAW not in (None, ""):
    _UPSTREAM_SAFE_DEFAULT = int(_UPSTREAM_SAFE_RAW)
elif TIG_UNRESOLVED_BENCHMARK_LIMIT > 0:
    _UPSTREAM_SAFE_DEFAULT = TIG_UNRESOLVED_BENCHMARK_LIMIT - TIG_UNRESOLVED_BENCHMARK_HEADROOM
else:
    _UPSTREAM_SAFE_DEFAULT = MAX_MAX_BENCHMARKS
UPSTREAM_SAFE_MAX_BENCHMARKS = max(
    MIN_MAX_BENCHMARKS,
    min(MAX_MAX_BENCHMARKS, _UPSTREAM_SAFE_DEFAULT),
)
APPLY_MIN_CLEAN_WINDOWS = int(os.environ.get("AUTOPILOT_APPLY_MIN_CLEAN_WINDOWS", "2"))
MAX_BENCHMARK_STEP = int(os.environ.get("AUTOPILOT_MAX_BENCHMARK_STEP", "2"))
MAX_BENCHMARK_UP_STEP = int(os.environ.get("AUTOPILOT_MAX_BENCHMARK_UP_STEP", str(MAX_BENCHMARK_STEP)))
MAX_BENCHMARK_DOWN_STEP = int(os.environ.get("AUTOPILOT_MAX_BENCHMARK_DOWN_STEP", str(MAX_BENCHMARK_STEP)))
SLOT_STEP = int(os.environ.get("AUTOPILOT_SLOT_STEP", "1"))
SLOT_UP_STEP = int(os.environ.get("AUTOPILOT_SLOT_UP_STEP", str(SLOT_STEP)))
SLOT_DOWN_STEP = int(os.environ.get("AUTOPILOT_SLOT_DOWN_STEP", str(SLOT_STEP)))
MAX_CPU_SLOTS = int(os.environ.get("AUTOPILOT_MAX_CPU_SLOTS", "128"))
MAX_GPU_SLOTS_PER_TYPE = int(os.environ.get("AUTOPILOT_MAX_GPU_SLOTS_PER_TYPE", "16"))
# How many GPU workers one open GPU benchmark should feed via root-batch fan-out.
# 50 GPUs / 4 = 13 jobs, not 50. A 12-GPU C3 box is 3 jobs, not 12.
GPU_UNITS_PER_JOB = max(1, int(os.environ.get("AUTOPILOT_GPU_UNITS_PER_JOB", "4")))
# Extra GPU slots/jobs kept unowned so a finishing GPU has work waiting.
# Sticky blocks fan-out, so this buffer hides TIG precommit latency.
GPU_JOB_SPARE = max(0, int(os.environ.get("AUTOPILOT_GPU_JOB_SPARE", "2")))
MAX_CPU_CHALLENGE_BENCHMARKS = int(os.environ.get("AUTOPILOT_MAX_CPU_CHALLENGE_BENCHMARKS", "16"))
MAX_GPU_CHALLENGE_BENCHMARKS = int(os.environ.get("AUTOPILOT_MAX_GPU_CHALLENGE_BENCHMARKS", "12"))
# Optional per-challenge ceilings. Unset keys fall back to the CPU/GPU family max.
# Example: AUTOPILOT_MAX_CHALLENGE_BENCHMARKS_C005=1 keeps hypergraph at 1 while
# AUTOPILOT_MAX_CHALLENGE_BENCHMARKS_C006=3 allows more neuralnet jobs.
_CHALLENGE_MAX_BENCHMARK_ENV = {
    "c001": "AUTOPILOT_MAX_CHALLENGE_BENCHMARKS_C001",
    "c002": "AUTOPILOT_MAX_CHALLENGE_BENCHMARKS_C002",
    "c003": "AUTOPILOT_MAX_CHALLENGE_BENCHMARKS_C003",
    "c004": "AUTOPILOT_MAX_CHALLENGE_BENCHMARKS_C004",
    "c005": "AUTOPILOT_MAX_CHALLENGE_BENCHMARKS_C005",
    "c006": "AUTOPILOT_MAX_CHALLENGE_BENCHMARKS_C006",
    "c007": "AUTOPILOT_MAX_CHALLENGE_BENCHMARKS_C007",
    "c008": "AUTOPILOT_MAX_CHALLENGE_BENCHMARKS_C008",
}
_GPU_CHALLENGE_IDS = frozenset({"c004", "c005", "c006"})


def _env_track_key(track: str) -> str:
    """n_queries=7000 -> N_QUERIES_7000; n_vars=10000,ratio=4267 -> N_VARS_10000_RATIO_4267."""
    out = []
    for ch in str(track or "").strip().upper():
        out.append(ch if ch.isalnum() else "_")
    key = "".join(out)
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


def _min_bundles_for_track(
    challenge_id: str,
    track: str | None = None,
    environ: dict | None = None,
    default_floor: int | None = None,
) -> int:
    """Operator bundle floor: track env, then challenge env, then global min.

    AUTOPILOT_MIN_BUNDLES_C004=12
    AUTOPILOT_MIN_BUNDLES_C004_N_QUERIES_7000=16
    """
    env = environ if environ is not None else os.environ
    floor = int(default_floor if default_floor is not None else WORKLOAD_MIN_BUNDLES)
    cid = str(challenge_id or "").split("_", 1)[0].upper()
    if cid:
        raw = env.get(f"AUTOPILOT_MIN_BUNDLES_{cid}")
        if raw is not None and str(raw).strip() != "":
            floor = max(floor, int(raw))
        if track:
            raw = env.get(f"AUTOPILOT_MIN_BUNDLES_{cid}_{_env_track_key(track)}")
            if raw is not None and str(raw).strip() != "":
                floor = max(floor, int(raw))
    return max(1, floor)


def locked_algo_weight(
    current_weight: int,
    proposed_weight: int,
    locked: bool | None = None,
) -> int:
    """Keep live-config weight when AUTOPILOT_WEIGHT_LOCK is on."""
    if locked if locked is not None else WEIGHT_LOCK:
        return int(current_weight or 0)
    return int(proposed_weight or 0)


def _max_challenge_benchmarks(challenge_id: str) -> int:
    """Autopilot ceiling for one challenge's per_challenge_max_benchmarks entry."""
    cid = str(challenge_id or "").split("_", 1)[0]
    env_name = _CHALLENGE_MAX_BENCHMARK_ENV.get(cid)
    if env_name:
        raw = os.environ.get(env_name)
        if raw is not None and str(raw).strip() != "":
            return max(1, int(raw))
    if cid in _GPU_CHALLENGE_IDS:
        return max(1, int(MAX_GPU_CHALLENGE_BENCHMARKS))
    return max(1, int(MAX_CPU_CHALLENGE_BENCHMARKS))


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
# Small max_concurrent step-ups for idle proven CPU workers even when the global
# reward funnel is still "unsafe" due to slow GPU proof tails / recovery posture.
IDLE_CPU_MAX_SCALE_ENABLED = os.environ.get(
    "AUTOPILOT_IDLE_CPU_MAX_SCALE_ENABLED", "true"
).lower() in ("1", "true", "yes", "on")
IDLE_CPU_MAX_SCALE_MIN = int(os.environ.get("AUTOPILOT_IDLE_CPU_MAX_SCALE_MIN", "3"))
# When capacity-eligible workers are all busy, still allow a slow max climb if
# free CPU slots exist and root/conversion health clears the soft floor.
IDLE_CPU_MAX_SCALE_MIN_SLOT_IDLE = int(
    os.environ.get("AUTOPILOT_IDLE_CPU_MAX_SCALE_MIN_SLOT_IDLE", "16")
)
IDLE_CPU_MAX_SCALE_UP_STEP = int(os.environ.get("AUTOPILOT_IDLE_CPU_MAX_SCALE_UP_STEP", "2"))
CAP_SCALE_COMPLETIONS_PER_STEP = int(os.environ.get("AUTOPILOT_CAP_SCALE_COMPLETIONS_PER_STEP", "20"))
ROUTE_CPU_UP_STEP = int(os.environ.get("AUTOPILOT_ROUTE_CPU_UP_STEP", "8"))
ROUTE_GPU_UP_STEP = int(os.environ.get("AUTOPILOT_ROUTE_GPU_UP_STEP", "1"))
ROUTE_MIN_SATURATED_FRACTION = float(os.environ.get("AUTOPILOT_ROUTE_MIN_SATURATED_FRACTION", "0.25"))
ROUTE_GPU_MIN_COMPLETIONS_PER_SLAVE = int(os.environ.get("AUTOPILOT_ROUTE_GPU_MIN_COMPLETIONS_PER_SLAVE", "2"))
ROUTE_CPU_MIN_COMPLETIONS_PER_SLAVE = int(os.environ.get("AUTOPILOT_ROUTE_CPU_MIN_COMPLETIONS_PER_SLAVE", "2"))
BUNDLE_TARGET_MIN_ROOT_BATCHES = int(os.environ.get("AUTOPILOT_BUNDLE_TARGET_MIN_ROOT_BATCHES", "8"))
BUNDLE_TARGET_MAX_ROOT_BATCHES = int(os.environ.get("AUTOPILOT_BUNDLE_TARGET_MAX_ROOT_BATCHES", "192"))
BUNDLE_TARGET_ROOT_RUNTIME_SEC = int(os.environ.get("AUTOPILOT_BUNDLE_TARGET_ROOT_RUNTIME_SEC", "900"))
FUNNEL_TARGET_PROOF_SUBMIT_SEC = int(os.environ.get("AUTOPILOT_FUNNEL_TARGET_PROOF_SUBMIT_SEC", "1200"))
FUNNEL_MIN_PROOF_CONVERSION_RATE = float(os.environ.get("AUTOPILOT_FUNNEL_MIN_PROOF_CONVERSION_RATE", "0.85"))
# Near-threshold conversion (e.g. 80-85%) can still be soft-skipped when roots
# are healthy and CPU is idle — avoids sawtoothing max_concurrent to the drain floor.
FUNNEL_SOFT_PROOF_CONVERSION_FLOOR = float(
    os.environ.get("AUTOPILOT_FUNNEL_SOFT_PROOF_CONVERSION_FLOOR", "0.80")
)
# How long soft/marginal proof-conversion (soft-floor..target) may skip max drain
# without proven idle CPU. Default = one funnel metric window so diluted samples
# from newly joined workers can roll out before drain resumes.
SOFT_CONVERSION_DRAIN_GRACE_MS = int(
    os.environ.get("AUTOPILOT_SOFT_CONVERSION_DRAIN_GRACE_MS") or METRIC_WINDOW_MS
)
SOFT_CONVERSION_GRACE_SETTING = "autopilot_soft_conversion_grace_started_ms"
FUNNEL_MAX_STOPPED_OR_EXPIRED_RATE = float(os.environ.get("AUTOPILOT_FUNNEL_MAX_STOPPED_OR_EXPIRED_RATE", "0.10"))
FUNNEL_DRAIN_MIN_MAX_BENCHMARKS = int(os.environ.get("AUTOPILOT_FUNNEL_DRAIN_MIN_MAX_BENCHMARKS", "12"))
WORKLOAD_MIN_BUNDLES = int(os.environ.get("AUTOPILOT_WORKLOAD_MIN_BUNDLES", "4"))
# Optional tighter floors: AUTOPILOT_MIN_BUNDLES_C004=12 and
# AUTOPILOT_MIN_BUNDLES_C004_N_QUERIES_7000=16. See _min_bundles_for_track.
WORKLOAD_MIN_BATCH_SIZE = int(os.environ.get("AUTOPILOT_WORKLOAD_MIN_BATCH_SIZE", "8"))
WORKLOAD_MIN_WEIGHT = int(os.environ.get("AUTOPILOT_WORKLOAD_MIN_WEIGHT", "1"))
# Live-config algo_selection[].weight is operator-owned while this is on.
# Workload still tunes bundles / batch_size / per-challenge caps.
WEIGHT_LOCK = os.environ.get("AUTOPILOT_WEIGHT_LOCK", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
WORKLOAD_MAX_BUNDLE_STEP = int(os.environ.get("AUTOPILOT_WORKLOAD_MAX_BUNDLE_STEP", "1"))
WORKLOAD_SAFETY_COOLDOWN_MS = int(os.environ.get("AUTOPILOT_WORKLOAD_SAFETY_COOLDOWN_MS", str(METRIC_WINDOW_MS)))
WORKLOAD_CANARY_COOLDOWN_MS = int(os.environ.get("AUTOPILOT_WORKLOAD_CANARY_COOLDOWN_MS", str(METRIC_WINDOW_MS)))
WORKLOAD_FAST_PROOF_FACTOR = float(os.environ.get("AUTOPILOT_WORKLOAD_FAST_PROOF_FACTOR", "0.50"))
WORKLOAD_HIGH_PROOF_CONVERSION_RATE = float(os.environ.get("AUTOPILOT_WORKLOAD_HIGH_PROOF_CONVERSION_RATE", "0.95"))
ROOT_BACKLOG_DRAIN_MIN_NOT_STARTED = int(os.environ.get("AUTOPILOT_ROOT_BACKLOG_DRAIN_MIN_NOT_STARTED", "128"))
ROOT_BACKLOG_DRAIN_MIN_AGE_MS = int(os.environ.get("AUTOPILOT_ROOT_BACKLOG_DRAIN_MIN_AGE_MS", str(20 * 60 * 1000)))
ROOT_BACKLOG_DRAIN_MIN_BUNDLES = int(os.environ.get("AUTOPILOT_ROOT_BACKLOG_DRAIN_MIN_BUNDLES", "1"))
ROOT_BACKLOG_DRAIN_MIN_PER_CHALLENGE = int(os.environ.get("AUTOPILOT_ROOT_BACKLOG_DRAIN_MIN_PER_CHALLENGE", "1"))
# Pipeline-healthy tracks keep their current num_bundles. Backlog still drains
# via fewer new jobs (per_challenge_max_benchmarks), not smaller jobs.
HOLD_HEALTHY_TRACK_BUNDLES = os.environ.get(
    "AUTOPILOT_HOLD_HEALTHY_TRACK_BUNDLES", "true"
).lower() in ("1", "true", "yes", "on")
BUNDLE_HOLD_ACTIONS = frozenset({
    "drain_root_backlog_pressure",
    "reduce_tail_time",
    "reduce_workload_until_proofs_convert",
    "reduce_workload_until_stopped_rate_recovers",
})
# Drain/hold global max_concurrent when too many *GPU* roots sit unfinished.
# CPU backlog (knapsack/energy/etc.) must not yank max_concurrent — that is
# handled by per-challenge workload drain + the master's precommit governor.
ROOT_PENDING_MAX_CONCURRENT_DRAIN = int(
    os.environ.get("AUTOPILOT_ROOT_PENDING_MAX_CONCURRENT_DRAIN", "256")
)
ROOT_READY_RATE_MIN_FOR_UPSCALE = float(
    os.environ.get("AUTOPILOT_ROOT_READY_RATE_MIN_FOR_UPSCALE", "0.50")
)
# When recommended max_concurrent jumps far above live config, climb slowly so
# proof conversion can prove out before another surge of precommits.
SURGE_TARGET_GAP = int(os.environ.get("AUTOPILOT_SURGE_TARGET_GAP", "16"))
SURGE_MAX_BENCHMARK_UP_STEP = int(os.environ.get("AUTOPILOT_SURGE_MAX_BENCHMARK_UP_STEP", "1"))
# Match max_concurrent to recent finishes so creates scale with compute that is
# actually converting, instead of only slot-sum capacity.
COMPLETION_MATCH_ENABLED = os.environ.get("AUTOPILOT_COMPLETION_MATCH_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
COMPLETION_MATCH_MIN_SAMPLES = int(os.environ.get("AUTOPILOT_COMPLETION_MATCH_MIN_SAMPLES", "5"))
COMPLETION_MATCH_BUFFER = int(os.environ.get("AUTOPILOT_COMPLETION_MATCH_BUFFER", "8"))
COMPLETION_INFLIGHT_MULT = float(os.environ.get("AUTOPILOT_COMPLETION_INFLIGHT_MULT", "2.0"))
COMPLETION_WARMUP_HEADROOM = int(os.environ.get("AUTOPILOT_COMPLETION_WARMUP_HEADROOM", "8"))
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
BENCHMARK_MAX_AGE_CLEANUP_ENABLED = os.environ.get(
    "AUTOPILOT_BENCHMARK_MAX_AGE_CLEANUP_ENABLED",
    "true",
).lower() in ("1", "true", "yes", "on")
BENCHMARK_MAX_AGE_MS = int(
    os.environ.get("AUTOPILOT_BENCHMARK_MAX_AGE_MS", str(90 * 60 * 1000))
)
# Pending root/proof rows on stopped/ended jobs inflate backlog metrics and can
# keep governors in permanent drain. Close them out in apply mode.
ZOMBIE_PENDING_CLEANUP_ENABLED = os.environ.get(
    "AUTOPILOT_ZOMBIE_PENDING_CLEANUP_ENABLED",
    "true",
).lower() in ("1", "true", "yes", "on")
ZOMBIE_PENDING_CLEANUP_MAX_ROWS = int(
    os.environ.get("AUTOPILOT_ZOMBIE_PENDING_CLEANUP_MAX_ROWS", "5000")
)
TRUSTED_CPU_COMPLETIONS = int(os.environ.get("AUTOPILOT_TRUSTED_CPU_COMPLETIONS", "10"))
TRUSTED_GPU_COMPLETIONS = int(os.environ.get("AUTOPILOT_TRUSTED_GPU_COMPLETIONS", "2"))
TRUSTED_MAX_FAILED_RECENT = int(os.environ.get("AUTOPILOT_TRUSTED_MAX_FAILED_RECENT", "0"))
# Live workers with some finishes still count toward capacity even below the
# full "trusted" completion bar — otherwise one slow VRPTW batch (stale) made
# almost the entire public CPU fleet disappear from active_cpu.
CAPACITY_LIVE_MIN_COMPLETIONS = int(
    os.environ.get("AUTOPILOT_CAPACITY_LIVE_MIN_COMPLETIONS", "3")
)
CAPACITY_STUCK_MIN_INFLIGHT = int(
    os.environ.get("AUTOPILOT_CAPACITY_STUCK_MIN_INFLIGHT", "4")
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
CPU_CHALLENGE_IDS = {"c001", "c002", "c003", "c007", "c008"}
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
_member_hardening_schema_ready = False


def _aws_batch_capacity(cfg: dict | None) -> dict:
    raw = ((cfg or {}).get("aws_batch_capacity") or {})
    enabled = str(raw.get("enabled", "false")).lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return {
            "enabled": False,
            "cpu_instances": 0,
            "cpu_threads_per_instance": 0,
            "max_concurrent_cpu_jobs": 0,
            "cpu_batch_size_floor": WORKLOAD_MIN_BATCH_SIZE,
        }

    instances = max(0, int(raw.get("cpu_instances") or raw.get("instances") or 0))
    threads = max(1, int(raw.get("cpu_threads_per_instance") or raw.get("threads_per_instance") or 1))
    max_jobs = int(raw.get("max_concurrent_cpu_jobs") or raw.get("max_concurrent_jobs") or instances or 0)
    max_jobs = max(0, max_jobs)
    batch_floor = int(raw.get("cpu_batch_size_floor") or raw.get("batch_size_floor") or threads)
    batch_floor = max(WORKLOAD_MIN_BATCH_SIZE, batch_floor)
    return {
        "enabled": True,
        "cpu_instances": instances,
        "cpu_threads_per_instance": threads,
        "max_concurrent_cpu_jobs": max_jobs,
        "cpu_batch_size_floor": batch_floor,
    }


def _route_is_cpu(slave: dict) -> bool:
    regex = str(slave.get("algorithm_id_regex") or "")
    return any(challenge_id in regex for challenge_id in CPU_CHALLENGE_IDS)


def _route_is_gpu(route: dict) -> bool:
    regex = str(route.get("algorithm_id_regex") or "")
    return any(challenge_id in regex for challenge_id in GPU_CHALLENGE_ID_TO_SLOT)


def _route_profile(route: dict) -> str | None:
    if _route_is_gpu(route):
        return "gpu"
    if _route_is_cpu(route):
        return "cpu"
    return None


def _route_is_manual_gpu(route: dict) -> bool:
    """Return True for GPU routes where a static operator cap is intentional.

    Local GPU routes stay static. C3 routes are still skipped by generic fleet
    step-ups, but `_target_slave_route_caps` may raise a C3 route toward
    reported `num_workers` so a 12-GPU dispatcher is not stuck at 1 or 8.
    """
    name_regex = str(route.get("name_regex") or "").lower()
    return (
        "pool-gpu-local" in name_regex
        or "^local" in name_regex
        or "c3" in name_regex
        or name_regex.startswith("^pool-gpu-a330c544ec5b-2$")
    )


def _route_matches_slave(route: dict, slave_name: str) -> bool:
    pattern = str(route.get("name_regex") or "")
    if not pattern:
        return False
    try:
        return re.match(pattern, slave_name) is not None
    except re.error:
        return False


def _capacity_eligible(slave: dict) -> bool:
    if slave.get("capacity_eligible") is not None:
        return bool(slave.get("capacity_eligible"))
    return _counts_for_capacity(slave)


def _gpu_slot_floor(cfg: dict | None) -> dict[str, int]:
    """Minimum GPU slots the operator expects autopilot to preserve.

    A previous single-GPU recovery path could write GPU slots to zero. Using a
    floor lets autopilot repair that state instead of treating zero as intent.
    """
    raw = ((cfg or {}).get("gpu_slot_floor") or {})
    return {
        slot_type: max(0, int(raw.get(slot_type, 1)))
        for slot_type in GPU_SLOT_TYPES
    }


def _benchmark_max_age_cleanup(cfg: dict | None) -> dict:
    raw = ((cfg or {}).get("benchmark_max_age_cleanup") or {})
    enabled = raw.get("enabled", BENCHMARK_MAX_AGE_CLEANUP_ENABLED)
    if isinstance(enabled, str):
        enabled = enabled.lower() in {"1", "true", "yes", "on"}
    max_age_ms = int(raw.get("max_age_ms") or raw.get("age_ms") or BENCHMARK_MAX_AGE_MS)
    return {
        "enabled": bool(enabled),
        "max_age_ms": max_age_ms,
    }


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


def _is_c3_slave(slave_name: str) -> bool:
    name = str(slave_name or "").lower()
    return "-c3-" in name or name.startswith("c3-slave-")


def _gpu_units(slave: dict, cfg: dict | None = None) -> int:
    """How many parallel GPU workers one slave name represents.

    Used for route/batch concurrency and for GPU *job* fan-out
    (`ceil(units / GPU_UNITS_PER_JOB)`), not one benchmark per GPU.
    """
    name = str(slave.get("slave_name") or "")
    if (slave.get("profile") or _slave_profile(name)) != "gpu":
        return 0
    reported = 0
    try:
        reported = int(slave.get("num_workers") or 0)
    except (TypeError, ValueError):
        reported = 0
    route_cap = 0
    for route in (cfg or {}).get("slaves") or []:
        if _route_matches_slave(route, name):
            try:
                route_cap = int(route.get("max_concurrent_batches") or 0)
            except (TypeError, ValueError):
                route_cap = 0
            break
    if _is_c3_slave(name):
        return max(1, reported, route_cap)
    return max(1, reported) if reported else 1


def _is_public_member_slave(slave_name: str) -> bool:
    return slave_name.startswith(("pool-cpu-", "pool-gpu-"))


_member_hardening_retry_after = 0.0


def _ensure_member_hardening_schema():
    global _member_hardening_schema_ready, _member_hardening_retry_after
    if _member_hardening_schema_ready:
        return
    if time.monotonic() < _member_hardening_retry_after:
        return
    try:
        if (
            db.has_columns(
                "pool_members",
                "trust_state",
                "preflight_status",
                "preflight_report",
                "trusted_at",
            )
            and db.has_index("idx_pool_members_trust_state")
            and db.table_exists("slave_seen")
            and db.has_columns("slave_seen", "num_workers")
        ):
            _member_hardening_schema_ready = True
            return
        db.execute_many(
            ("ALTER TABLE pool_members ADD COLUMN IF NOT EXISTS trust_state TEXT NOT NULL DEFAULT 'probation'", None),
            ("ALTER TABLE pool_members ADD COLUMN IF NOT EXISTS preflight_status TEXT", None),
            ("ALTER TABLE pool_members ADD COLUMN IF NOT EXISTS preflight_report JSONB", None),
            ("ALTER TABLE pool_members ADD COLUMN IF NOT EXISTS trusted_at BIGINT", None),
            ("CREATE INDEX IF NOT EXISTS idx_pool_members_trust_state ON pool_members(trust_state)", None),
            (
                """
                CREATE TABLE IF NOT EXISTS slave_seen (
                    slave_name TEXT PRIMARY KEY,
                    last_seen BIGINT NOT NULL
                )
                """,
                None,
            ),
            ("ALTER TABLE slave_seen ADD COLUMN IF NOT EXISTS num_workers INTEGER", None),
            lock_timeout="2s",
        )
        _member_hardening_schema_ready = True
    except Exception as exc:
        _member_hardening_retry_after = time.monotonic() + 60
        logger.warning("member hardening schema check failed: %s", exc)


def _gpu_trust_blocked(slave: dict) -> bool:
    trust_state = str(slave.get("trust_state") or "probation").lower()
    return trust_state in {"disabled", "quarantined", "blocked"}


def _gpu_online_registered(slave: dict) -> bool:
    """Online GPU that may receive work, including public probation joiners."""
    name = slave.get("slave_name") or ""
    profile = slave.get("profile") or _slave_profile(name)
    if profile != "gpu":
        return False
    if not slave.get("active_now"):
        return False
    if _gpu_trust_blocked(slave):
        return False
    if _is_public_member_slave(name) and not slave.get("registered_active"):
        return False
    return True


def _counts_for_capacity(slave: dict) -> bool:
    name = slave.get("slave_name") or ""
    if not slave.get("active_now"):
        return False
    if not _is_public_member_slave(name):
        return True
    if not slave.get("registered_active"):
        return False

    trust_state = str(slave.get("trust_state") or "probation").lower()
    if trust_state in {"trusted", "operator"}:
        return True
    if trust_state in {"disabled", "quarantined", "blocked"}:
        return False

    profile = slave.get("profile") or _slave_profile(name)
    required_completed = TRUSTED_GPU_COMPLETIONS if profile == "gpu" else TRUSTED_CPU_COMPLETIONS
    completed = int(slave.get("completed_recent") or 0)
    stale = int(slave.get("stale_roots") or 0) + int(slave.get("stale_proofs") or 0)
    failed = int(slave.get("failed_recent") or 0)
    active_unfinished = int(slave.get("active_unfinished") or 0)
    # Holding old work with zero finishes — do not scale creates on this box.
    if (
        completed == 0
        and stale >= 1
        and active_unfinished >= CAPACITY_STUCK_MIN_INFLIGHT
    ):
        return False
    if failed > TRUSTED_MAX_FAILED_RECENT:
        return False
    # Full trusted bar (no longer requires stale == 0 — one long batch was
    # zeroing out ~20 CPUs from the capacity model).
    if completed >= required_completed:
        return True
    # Actively working public CPUs with some recent finishes still count.
    live_min = min(CAPACITY_LIVE_MIN_COMPLETIONS, required_completed)
    if active_unfinished > 0 and completed >= live_min:
        return True
    return False


def _capacity_reason(slave: dict) -> str:
    name = slave.get("slave_name") or ""
    if not slave.get("active_now"):
        return "inactive"
    if not _is_public_member_slave(name):
        return "operator"
    if not slave.get("registered_active"):
        return "not_registered_active"
    trust_state = str(slave.get("trust_state") or "probation").lower()
    if trust_state in {"trusted", "operator"}:
        return trust_state
    if trust_state in {"disabled", "quarantined", "blocked"}:
        return trust_state
    return "proven_recent" if _counts_for_capacity(slave) else "probation"


def _fetch_master_config() -> tuple[dict, str | None]:
    try:
        with urllib.request.urlopen(f"{MASTER_URL}/get-config", timeout=5) as resp:  # nosec B310 — MASTER_URL is an internal Docker env var, never user-controlled
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
    urllib.request.urlopen(req, timeout=5)  # nosec B310 — MASTER_URL is an internal Docker env var, never user-controlled


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
    max_age_cleanup = _benchmark_max_age_cleanup(cfg)
    result = {
        "enabled": STALE_CLEANUP_ENABLED,
        "precommit_expiry_enabled": PRECOMMIT_EXPIRY_CLEANUP_ENABLED,
        "benchmark_max_age_enabled": max_age_cleanup["enabled"],
        "zombie_pending_cleanup_enabled": ZOMBIE_PENDING_CLEANUP_ENABLED,
        "released_roots": [],
        "released_orphan_roots": [],
        "released_proofs": [],
        "expiry_released_roots": [],
        "expiry_released_proofs": [],
        "stopped_precommits": [],
        "stopped_old_benchmarks": [],
        "closed_zombie_roots": 0,
        "closed_zombie_proofs": 0,
        "thresholds_ms": {
            "stale_root_cleanup": STALE_ROOT_CLEANUP_MIN_AGE_MS,
            "stale_proof_cleanup": STALE_PROOF_CLEANUP_MIN_AGE_MS,
            "precommit_root_reclaim": PRECOMMIT_ROOT_RECLAIM_AGE_MS,
            "precommit_proof_reclaim": PRECOMMIT_PROOF_RECLAIM_AGE_MS,
            "precommit_abandon_no_root": PRECOMMIT_ABANDON_NO_ROOT_AGE_MS,
            "benchmark_max_age": max_age_cleanup["max_age_ms"],
        },
        "skipped": "",
    }
    if AUTOPILOT_MODE != "apply":
        result["skipped"] = "report_only"
        return result
    if (
        not STALE_CLEANUP_ENABLED
        and not PRECOMMIT_EXPIRY_CLEANUP_ENABLED
        and not max_age_cleanup["enabled"]
        and not ZOMBIE_PENDING_CLEANUP_ENABLED
    ):
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
    old_benchmarks_to_stop = []
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

    if max_age_cleanup["enabled"] and max_age_cleanup["max_age_ms"] > 0:
        old_benchmarks_to_stop = _fetch_all(
            """
            SELECT
                j.benchmark_id,
                j.start_time AS job_start_time,
                j.challenge,
                j.settings->>'algorithm_id' AS algorithm_id,
                j.settings->>'track_id' AS track,
                j.merkle_root_ready,
                COUNT(rb.*) FILTER (WHERE rb.ready = true) AS roots_ready,
                COUNT(rb.*) FILTER (WHERE rb.ready IS NULL) AS roots_pending,
                COUNT(pb.*) FILTER (WHERE pb.ready = true) AS proofs_ready,
                COUNT(pb.*) FILTER (WHERE pb.ready IS NULL) AS proofs_pending
            FROM job j
            LEFT JOIN root_batch rb ON rb.benchmark_id = j.benchmark_id
            LEFT JOIN proofs_batch pb ON pb.benchmark_id = j.benchmark_id
            WHERE j.stopped IS NULL
              AND j.end_time IS NULL
              AND j.start_time IS NOT NULL
              AND j.start_time < %s
            GROUP BY j.benchmark_id, j.start_time, j.challenge, j.settings, j.merkle_root_ready
            ORDER BY j.start_time
            LIMIT %s
            """,
            (now_ms - int(max_age_cleanup["max_age_ms"]), STALE_CLEANUP_MAX_ROWS),
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
                    end_time = COALESCE(end_time, %s),
                    ready = false
                WHERE benchmark_id = %s
                  AND ready IS NULL
                """,
                (now_ms, row["benchmark_id"]),
            ),
            (
                """
                UPDATE proofs_batch
                SET slave = NULL,
                    start_time = NULL,
                    end_time = COALESCE(end_time, %s),
                    ready = false
                WHERE benchmark_id = %s
                  AND ready IS NULL
                """,
                (now_ms, row["benchmark_id"]),
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

    stopped_precommit_ids = {row["benchmark_id"] for row in precommits_to_stop}
    for row in old_benchmarks_to_stop:
        if row["benchmark_id"] in stopped_precommit_ids:
            continue
        queries.extend([
            (
                """
                UPDATE job
                SET stopped = true,
                    end_time = %s
                WHERE benchmark_id = %s
                  AND stopped IS NULL
                  AND end_time IS NULL
                """,
                (now_ms, row["benchmark_id"]),
            ),
            (
                """
                UPDATE root_batch
                SET slave = NULL,
                    start_time = NULL,
                    end_time = COALESCE(end_time, %s),
                    ready = false
                WHERE benchmark_id = %s
                  AND ready IS NULL
                """,
                (now_ms, row["benchmark_id"]),
            ),
            (
                """
                UPDATE proofs_batch
                SET slave = NULL,
                    start_time = NULL,
                    end_time = COALESCE(end_time, %s),
                    ready = false
                WHERE benchmark_id = %s
                  AND ready IS NULL
                """,
                (now_ms, row["benchmark_id"]),
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
        result["stopped_old_benchmarks"].append({
            "benchmark": str(row["benchmark_id"])[:10],
            "challenge": row["challenge"],
            "algorithm_id": row["algorithm_id"],
            "track": row["track"],
            "job_age_min": round((now_ms - int(row["job_start_time"])) / 60000.0, 1),
            "merkle_root_ready": bool(row.get("merkle_root_ready")),
            "roots_ready": int(row.get("roots_ready") or 0),
            "roots_pending": int(row.get("roots_pending") or 0),
            "proofs_ready": int(row.get("proofs_ready") or 0),
            "proofs_pending": int(row.get("proofs_pending") or 0),
            "reason": "benchmark_max_age_exceeded",
        })

    if ZOMBIE_PENDING_CLEANUP_ENABLED:
        zombie_roots = _fetch_all(
            """
            SELECT rb.benchmark_id, rb.batch_idx
            FROM root_batch rb
            JOIN job j ON j.benchmark_id = rb.benchmark_id
            WHERE rb.ready IS NULL
              AND (j.stopped IS TRUE OR j.end_time IS NOT NULL)
            ORDER BY rb.benchmark_id, rb.batch_idx
            LIMIT %s
            """,
            (ZOMBIE_PENDING_CLEANUP_MAX_ROWS,),
        )
        for row in zombie_roots:
            queries.append((
                """
                UPDATE root_batch
                SET slave = NULL,
                    start_time = NULL,
                    end_time = COALESCE(end_time, %s),
                    ready = false
                WHERE benchmark_id = %s
                  AND batch_idx = %s
                  AND ready IS NULL
                """,
                (now_ms, row["benchmark_id"], row["batch_idx"]),
            ))
        result["closed_zombie_roots"] = len(zombie_roots)

        zombie_proofs = _fetch_all(
            """
            SELECT pb.benchmark_id, pb.batch_idx
            FROM proofs_batch pb
            JOIN job j ON j.benchmark_id = pb.benchmark_id
            WHERE pb.ready IS NULL
              AND (j.stopped IS TRUE OR j.end_time IS NOT NULL)
            ORDER BY pb.benchmark_id, pb.batch_idx
            LIMIT %s
            """,
            (ZOMBIE_PENDING_CLEANUP_MAX_ROWS,),
        )
        for row in zombie_proofs:
            queries.append((
                """
                UPDATE proofs_batch
                SET slave = NULL,
                    start_time = NULL,
                    end_time = COALESCE(end_time, %s),
                    ready = false
                WHERE benchmark_id = %s
                  AND batch_idx = %s
                  AND ready IS NULL
                """,
                (now_ms, row["benchmark_id"], row["batch_idx"]),
            ))
        result["closed_zombie_proofs"] = len(zombie_proofs)

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
        "gpu_slot_floor": cfg.get("gpu_slot_floor", {}),
        "adaptive_slave_caps": cfg.get("adaptive_slave_caps", {}),
        "aws_batch_capacity": cfg.get("aws_batch_capacity", {}),
        "benchmark_max_age_cleanup": cfg.get("benchmark_max_age_cleanup", {}),
        "slaves": cfg.get("slaves", []),
    }


def _slave_metrics(now_ms: int) -> list[dict]:
    _ensure_member_hardening_schema()
    cutoff_active = now_ms - ACTIVE_WINDOW_MS
    cutoff_metrics = now_ms - METRIC_WINDOW_MS
    rows = _fetch_all(
        """
        WITH registered AS (
            SELECT
                slave_name,
                wallet_address,
                active,
                trust_state,
                preflight_status,
                trusted_at
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
            COALESCE(r.trust_state, 'probation') AS trust_state,
            r.preflight_status,
            r.trusted_at,
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
            rs.last_completed_at,
            ss.num_workers
        FROM registered r
        FULL OUTER JOIN root_stats rs ON rs.slave_name = r.slave_name
        FULL OUTER JOIN proof_stats ps ON ps.slave_name = COALESCE(r.slave_name, rs.slave_name)
        LEFT JOIN slave_seen ss ON ss.slave_name = COALESCE(r.slave_name, rs.slave_name, ps.slave_name)
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
        row["capacity_eligible"] = _counts_for_capacity(row)
        row["capacity_reason"] = _capacity_reason(row)
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


def proof_counts_toward_conversion(
    *,
    has_proof_batches: bool,
    proof_submitted: bool = False,
    stopped: bool = False,
    has_end_time: bool = False,
) -> bool:
    """True when a job belongs in proof_conversion_rate's denominator.

    Open proof-phase work (and local-done waiting on TIG confirm) must not
    look like a failed conversion. Only settled jobs count: TIG-confirmed
    proofs, stopped, or ended.
    """
    if not has_proof_batches:
        return False
    return bool(proof_submitted) or bool(stopped) or bool(has_end_time)


def _reward_funnel_summary(now_ms: int) -> dict:
    cutoff_metrics = now_ms - METRIC_WINDOW_MS
    cfg, _cfg_error = _fetch_master_config()
    track_allowlist = cfg.get("track_allowlist", {}) if cfg else {}
    track_algorithm_map = cfg.get("track_algorithm_map", {}) if cfg else {}
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
            COUNT(*) FILTER (
                WHERE COALESCE(pa.proof_batches, 0) > 0
                  AND (
                      jb.proof_submitted = true
                      OR jb.stopped = true
                      OR jb.end_time IS NOT NULL
                  )
            ) AS proof_required_benchmarks,
            COUNT(*) FILTER (
                WHERE COALESCE(pa.proof_batches, 0) > 0
                  AND jb.proof_submitted IS NOT TRUE
                  AND jb.stopped IS NULL
                  AND jb.end_time IS NULL
            ) AS proof_inflight_benchmarks,
            COUNT(*) FILTER (WHERE jb.merkle_proofs_ready = true) AS proof_ready_benchmarks,
            COUNT(*) FILTER (WHERE jb.proof_submit_time IS NOT NULL) AS proof_submit_attempted,
            COUNT(*) FILTER (WHERE jb.proof_submitted = true) AS proof_submitted_confirmed,
            COALESCE(SUM(jb.num_nonces), 0) AS nonces_seen,
            COALESCE(SUM(jb.num_batches), 0) AS root_batches_expected,
            COALESCE(SUM(ra.root_batches), 0) AS root_batches_seen,
            COALESCE(SUM(ra.roots_ready), 0) AS roots_ready,
            COALESCE(SUM(ra.roots_pending) FILTER (
                WHERE jb.stopped IS NULL AND jb.end_time IS NULL
            ), 0) AS roots_pending,
            COALESCE(SUM(ra.roots_pending) FILTER (
                WHERE jb.stopped IS NULL AND jb.end_time IS NULL
                  AND jb.challenge IN (
                      'satisfiability', 'vehicle_routing', 'knapsack',
                      'job_scheduling', 'energy_arbitrage',
                      'c001', 'c002', 'c003', 'c007', 'c008'
                  )
            ), 0) AS cpu_roots_pending,
            COALESCE(SUM(ra.roots_pending) FILTER (
                WHERE jb.stopped IS NULL AND jb.end_time IS NULL
                  AND jb.challenge IN (
                      'vector_search', 'hypergraph', 'neuralnet_optimizer',
                      'c004', 'c005', 'c006'
                  )
            ), 0) AS gpu_roots_pending,
            COALESCE(SUM(ra.roots_pending) FILTER (
                WHERE jb.stopped IS TRUE OR jb.end_time IS NOT NULL
            ), 0) AS roots_pending_on_dead_jobs,
            COALESCE(SUM(ra.roots_failed), 0) AS roots_failed,
            COALESCE(SUM(pa.proof_batches), 0) AS proof_batches_seen,
            COALESCE(SUM(pa.proofs_ready), 0) AS proofs_ready,
            COALESCE(SUM(pa.proofs_pending) FILTER (
                WHERE jb.stopped IS NULL AND jb.end_time IS NULL
            ), 0) AS proofs_pending,
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
            COUNT(*) FILTER (
                WHERE COALESCE(pa.proof_batches, 0) > 0
                  AND (
                      jb.proof_submitted = true
                      OR jb.stopped = true
                      OR jb.end_time IS NOT NULL
                  )
            ) AS proof_required_benchmarks,
            COUNT(*) FILTER (
                WHERE COALESCE(pa.proof_batches, 0) > 0
                  AND jb.proof_submitted IS NOT TRUE
                  AND jb.stopped IS NULL
                  AND jb.end_time IS NULL
            ) AS proof_inflight_benchmarks,
            COUNT(*) FILTER (WHERE jb.merkle_proofs_ready = true) AS proof_ready_benchmarks,
            COUNT(*) FILTER (WHERE jb.proof_submitted = true) AS proof_submitted_confirmed,
            ROUND(AVG(jb.num_nonces)::numeric, 1) AS avg_num_nonces,
            ROUND(AVG(jb.num_batches)::numeric, 1) AS avg_num_batches,
            ROUND(AVG(jb.batch_size)::numeric, 1) AS avg_batch_size,
            COALESCE(SUM(ra.roots_ready), 0) AS roots_ready,
            COALESCE(SUM(ra.roots_pending) FILTER (
                WHERE jb.stopped IS NULL AND jb.end_time IS NULL
            ), 0) AS roots_pending,
            COALESCE(SUM(ra.roots_pending) FILTER (
                WHERE jb.stopped IS TRUE OR jb.end_time IS NOT NULL
            ), 0) AS roots_pending_on_dead_jobs,
            COALESCE(SUM(pa.proofs_ready), 0) AS proofs_ready,
            COALESCE(SUM(pa.proofs_pending) FILTER (
                WHERE jb.stopped IS NULL AND jb.end_time IS NULL
            ), 0) AS proofs_pending,
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
        pinned_algorithm = (track_algorithm_map.get(row.get("challenge")) or {}).get(row.get("track"))
        algorithm_pin_blocked = bool(pinned_algorithm) and pinned_algorithm != row.get("algorithm_id")
        intentionally_stopped = allowlist_blocked or algorithm_pin_blocked
        row["allowlist_blocked"] = allowlist_blocked
        row["algorithm_pin_blocked"] = algorithm_pin_blocked
        row["intentional_stopped_without_roots"] = stopped_without_roots if intentionally_stopped else 0
        row["unexpected_stopped_without_roots"] = 0 if intentionally_stopped else stopped_without_roots
        row["unexpected_stopped_without_roots_rate"] = 0.0 if intentionally_stopped else _safe_div(stopped_without_roots, seen)
        row["unexpected_stopped_rate"] = 0.0 if intentionally_stopped else _safe_div(stopped, seen)
        row["root_ready_rate"] = _safe_div(root_ready, seen)
        row["proof_conversion_rate"] = _safe_div(proof_submitted, proof_required)
        row["stopped_rate"] = _safe_div(stopped, seen)
    total = dict(total or {})
    seen = int(total.get("benchmarks_seen") or 0)
    root_ready = int(total.get("root_ready_benchmarks") or 0)
    proof_required = int(total.get("proof_required_benchmarks") or 0)
    proof_submitted = int(total.get("proof_submitted_confirmed") or 0)
    proof_attempted = int(total.get("proof_submit_attempted") or 0)
    proof_inflight = int(total.get("proof_inflight_benchmarks") or 0)
    stopped = int(total.get("stopped_benchmarks") or 0)
    stopped_without_roots = int(total.get("stopped_without_roots") or 0)
    intentional_stopped_without_roots = sum(
        int(row.get("intentional_stopped_without_roots") or 0)
        for row in by_track
    )
    unexpected_stopped_without_roots = max(0, stopped_without_roots - intentional_stopped_without_roots)
    unexpected_stopped = max(0, stopped - intentional_stopped_without_roots)
    roots_pending = int(float(total.get("roots_pending") or 0))
    cpu_roots_pending = int(float(total.get("cpu_roots_pending") or 0))
    gpu_roots_pending = int(float(total.get("gpu_roots_pending") or 0))
    avg_time_to_proof = total.get("avg_time_to_proof_submit_sec")
    # Settled jobs only. In-flight proof-phase rows stay out of the denominator
    # so a full warehouse does not print 70% conversion and drain max_concurrent.
    proof_conversion = _safe_div(proof_submitted, proof_required)
    proof_attempt_rate = _safe_div(proof_attempted, proof_required)
    stopped_rate = _safe_div(stopped, seen)
    unexpected_stopped_rate = _safe_div(unexpected_stopped, seen)
    unexpected_stopped_without_roots_rate = _safe_div(unexpected_stopped_without_roots, seen)
    issues = []
    if seen >= 5 and proof_required == 0:
        issues.append("warming_up_no_proof_samples")
    if seen >= 5 and root_ready == 0 and roots_pending > 0:
        issues.append("root_phase_not_complete")
    if proof_required and (proof_conversion or 0.0) < FUNNEL_MIN_PROOF_CONVERSION_RATE:
        issues.append("low_proof_conversion")
    if (
        unexpected_stopped_without_roots_rate is not None
        and unexpected_stopped_without_roots_rate > FUNNEL_MAX_STOPPED_OR_EXPIRED_RATE
    ):
        issues.append("high_unexpected_stopped_without_roots_rate")
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
            "roots_pending": roots_pending,
            "cpu_roots_pending": cpu_roots_pending,
            "gpu_roots_pending": gpu_roots_pending,
            "root_ready_rate": _safe_div(root_ready, seen),
            "proof_conversion_rate": proof_conversion,
            "proof_inflight_benchmarks": proof_inflight,
            "proof_submit_attempt_rate": proof_attempt_rate,
            "stopped_rate": stopped_rate,
            "unexpected_stopped_rate": unexpected_stopped_rate,
            "unexpected_stopped_without_roots_rate": unexpected_stopped_without_roots_rate,
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
            COUNT(rb.*) FILTER (
                WHERE j.stopped IS NULL AND j.end_time IS NULL
            ) AS root_batches_seen,
            COUNT(rb.*) FILTER (
                WHERE rb.ready = true
                  AND j.stopped IS NULL
                  AND j.end_time IS NULL
            ) AS roots_ready,
            COUNT(rb.*) FILTER (
                WHERE rb.ready IS NULL
                  AND j.stopped IS NULL
                  AND j.end_time IS NULL
            ) AS roots_pending,
            COUNT(rb.*) FILTER (
                WHERE rb.ready IS NULL
                  AND rb.start_time IS NULL
                  AND j.stopped IS NULL
                  AND j.end_time IS NULL
            ) AS roots_not_started,
            COUNT(rb.*) FILTER (
                WHERE rb.ready IS NULL
                  AND rb.start_time IS NULL
                  AND j.stopped IS NULL
                  AND j.end_time IS NULL
                  AND j.start_time < %s
            ) AS old_roots_not_started,
            ROUND(MAX(%s - j.start_time) FILTER (
                WHERE rb.ready IS NULL
                  AND rb.start_time IS NULL
                  AND j.stopped IS NULL
                  AND j.end_time IS NULL
                  AND j.start_time IS NOT NULL
            ) / 60000.0, 1) AS oldest_not_started_root_age_min,
            COUNT(rb.*) FILTER (
                WHERE rb.ready IS NULL
                  AND rb.start_time IS NOT NULL
                  AND rb.start_time < %s
                  AND j.stopped IS NULL
                  AND j.end_time IS NULL
            ) AS stale_roots,
            COUNT(pb.*) FILTER (
                WHERE j.stopped IS NULL AND j.end_time IS NULL
            ) AS proof_batches_seen,
            COUNT(pb.*) FILTER (
                WHERE pb.ready = true
                  AND j.stopped IS NULL
                  AND j.end_time IS NULL
            ) AS proofs_ready,
            COUNT(pb.*) FILTER (
                WHERE pb.ready IS NULL
                  AND j.stopped IS NULL
                  AND j.end_time IS NULL
            ) AS proofs_pending,
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
        (
            now_ms - ROOT_BACKLOG_DRAIN_MIN_AGE_MS,
            now_ms,
            now_ms - STALE_ROOT_MS,
            cutoff_metrics,
            cutoff_metrics,
            cutoff_metrics,
        ),
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


def _effective_stopped_rate(funnel: dict):
    return funnel.get(
        "unexpected_stopped_without_roots_rate",
        funnel.get("unexpected_stopped_rate", funnel.get("stopped_rate")),
    )


def _workload_confidence(funnel: dict, observed: dict) -> dict:
    samples = int(funnel.get("benchmarks_seen") or observed.get("benchmarks_seen") or 0)
    proof_required = int(funnel.get("proof_required_benchmarks") or 0)
    root_batches = int(observed.get("root_batches_seen") or 0)
    has_proof_rate = funnel.get("proof_conversion_rate") is not None
    has_stopped_rate = _effective_stopped_rate(funnel) is not None
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


def _decrease_bundles(current: int, floor: int | None = None) -> int:
    floor = max(1, int(floor if floor is not None else WORKLOAD_MIN_BUNDLES))
    current = int(current or 0)
    if current <= floor:
        return current
    return max(floor, current - WORKLOAD_MAX_BUNDLE_STEP)


def _decrease_backlog_bundles(current: int, floor: int | None = None) -> int:
    current = int(current or 0)
    floor = max(1, int(floor if floor is not None else ROOT_BACKLOG_DRAIN_MIN_BUNDLES))
    if current <= floor:
        return current
    return max(floor, current - WORKLOAD_MAX_BUNDLE_STEP)


def _pipeline_healthy_track(
    funnel: dict,
    min_proof_conversion: float,
    max_stopped_rate: float,
) -> bool:
    """True when a track converts proofs and is not failing to start work.

    Time-to-proof and root backlog are not part of this predicate. Those used
    to shrink job size on otherwise healthy tracks and undo bundle experiments.
    """
    if int(funnel.get("unexpected_stopped_without_roots") or 0) > 0:
        return False
    if funnel.get("allowlist_blocked") or funnel.get("algorithm_pin_blocked"):
        return False
    proof = funnel.get("proof_conversion_rate")
    if proof is not None and float(proof) < float(min_proof_conversion):
        return False
    stopped = funnel.get(
        "unexpected_stopped_without_roots_rate",
        funnel.get("unexpected_stopped_rate", funnel.get("stopped_rate")),
    )
    if stopped is not None and float(stopped) > float(max_stopped_rate):
        return False
    return True


def _hold_healthy_track_bundles(
    *,
    enabled: bool,
    pipeline_healthy: bool,
    action: str,
    current_bundles: int,
    target_bundles: int,
) -> tuple[int, bool, str | None]:
    """Keep current num_bundles on a pipeline-healthy track.

    Returns (bundles, held, reason).
    """
    if not enabled or not pipeline_healthy:
        return int(target_bundles), False, None
    if action not in BUNDLE_HOLD_ACTIONS:
        return int(target_bundles), False, None
    if int(target_bundles) >= int(current_bundles):
        return int(target_bundles), False, None
    return (
        int(current_bundles),
        True,
        "pipeline-healthy track: hold num_bundles; drain via fewer new jobs if needed",
    )


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
    aws_capacity = _aws_batch_capacity(cfg)
    cpu_batch_size_floor = int(aws_capacity.get("cpu_batch_size_floor") or WORKLOAD_MIN_BATCH_SIZE)
    targets = []
    for row in track_economics:
        algorithm_id = row.get("algorithm_id")
        challenge_id = str(algorithm_id or "").split("_", 1)[0]
        track = row.get("track")
        configured = row.get("configured") or {}
        derived = row.get("derived") or {}
        observed = row.get("observed") or {}
        funnel = dict(funnel_by_track.get((algorithm_id, track), {}))
        current_bundles = int(configured.get("num_bundles") or 0)
        current_batch_size = int(configured.get("effective_batch_size") or 1)
        min_batch_size = WORKLOAD_MIN_BATCH_SIZE
        is_cpu_challenge = challenge_id not in {"c004", "c005", "c006"}
        if is_cpu_challenge:
            min_batch_size = max(min_batch_size, cpu_batch_size_floor)
        current_weight = int(configured.get("weight") or current_weights.get(algorithm_id) or 0)
        target_bundles = current_bundles
        target_batch_size = current_batch_size
        target_weight = current_weight
        action = "observe"
        reasons = []

        proof_required = int(funnel.get("proof_required_benchmarks") or 0)
        proof_conversion = funnel.get("proof_conversion_rate")
        stopped_rate = funnel.get("stopped_rate")
        effective_stopped_rate = _effective_stopped_rate(funnel)
        stopped_without_roots = int(funnel.get("stopped_without_roots") or 0)
        intentional_stopped_without_roots = int(funnel.get("intentional_stopped_without_roots") or 0)
        unexpected_stopped_without_roots = int(funnel.get("unexpected_stopped_without_roots") or 0)
        allowlist_blocked = bool(funnel.get("allowlist_blocked"))
        algorithm_pin_blocked = bool(funnel.get("algorithm_pin_blocked"))
        avg_time_to_proof = funnel.get("avg_time_to_proof_submit_sec")
        p95_root_runtime = funnel.get("p95_root_batch_runtime_sec")
        estimated_root_batches = derived.get("estimated_root_batches")
        estimated_nonces_per_bundle = derived.get("estimated_nonces_per_bundle")
        confidence = _workload_confidence(funnel, observed)
        roots_not_started = int(observed.get("roots_not_started") or 0)
        old_roots_not_started = int(observed.get("old_roots_not_started") or 0)
        oldest_not_started_root_age_min = observed.get("oldest_not_started_root_age_min")
        current_per_challenge_cap = int((cfg.get("per_challenge_max_benchmarks") or {}).get(challenge_id, 0) or 0)
        target_per_challenge_cap = current_per_challenge_cap
        bundle_floor = _min_bundles_for_track(challenge_id, track)
        pipeline_healthy = False
        held_healthy_track_bundles = False

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
            stopped_unhealthy = effective_stopped_rate is not None and float(effective_stopped_rate) > FUNNEL_MAX_STOPPED_OR_EXPIRED_RATE
            slow_to_proof = (
                avg_time_to_proof is not None
                and float(avg_time_to_proof) > FUNNEL_TARGET_PROOF_SUBMIT_SEC
            )
            fast_clean = (
                proof_required > 0
                and proof_conversion is not None
                and float(proof_conversion) >= WORKLOAD_HIGH_PROOF_CONVERSION_RATE
                and (effective_stopped_rate is None or float(effective_stopped_rate) <= FUNNEL_MAX_STOPPED_OR_EXPIRED_RATE / 2)
                and avg_time_to_proof is not None
                and float(avg_time_to_proof) <= FUNNEL_TARGET_PROOF_SUBMIT_SEC * WORKLOAD_FAST_PROOF_FACTOR
            )
            backlog_pressure = old_roots_not_started >= ROOT_BACKLOG_DRAIN_MIN_NOT_STARTED

            if allowlist_blocked:
                action = "intentional_allowlist_stop"
                reasons.append("track is outside track_allowlist and was intentionally not benchmarked")
            elif algorithm_pin_blocked:
                action = "intentional_algorithm_pin_stop"
                reasons.append("track is pinned (via track_algorithm_map) to a different algorithm and was intentionally not benchmarked")
            elif unexpected_stopped_without_roots:
                action = "reduce_or_fix_unrunnable_track"
                reasons.append("recent jobs stopped before root work; check max_job_batches/allowlist/TIG debt")
                target_bundles = _decrease_bundles(current_bundles, bundle_floor)
            elif backlog_pressure:
                action = "drain_root_backlog_pressure"
                reasons.append("old not-started root batches are accumulating faster than workers can drain them")
                target_bundles = _decrease_bundles(current_bundles, bundle_floor)
                if current_per_challenge_cap > ROOT_BACKLOG_DRAIN_MIN_PER_CHALLENGE:
                    target_per_challenge_cap = max(
                        ROOT_BACKLOG_DRAIN_MIN_PER_CHALLENGE,
                        current_per_challenge_cap - 1,
                    )
            elif proof_unhealthy:
                action = "reduce_workload_until_proofs_convert"
                reasons.append("proof conversion is below target")
                target_bundles = _decrease_bundles(current_bundles, bundle_floor)
                proposed_weight = max(1, current_weight - 1) if current_weight > 1 else current_weight
                target_weight = locked_algo_weight(current_weight, proposed_weight)
                if WEIGHT_LOCK and proposed_weight != current_weight:
                    reasons.append("algo weight is locked; live-config value is kept")
            elif stopped_unhealthy:
                action = "reduce_workload_until_stopped_rate_recovers"
                reasons.append("stopped/expired benchmark rate is above target")
                target_bundles = _decrease_bundles(current_bundles, bundle_floor)
            elif slow_to_proof:
                action = "reduce_tail_time"
                reasons.append("time-to-proof-submission is above target")
                target_bundles = _decrease_bundles(current_bundles, bundle_floor)
                if p95_root_runtime is not None and float(p95_root_runtime) > BUNDLE_TARGET_ROOT_RUNTIME_SEC:
                    target_batch_size = max(min_batch_size, _previous_power_of_two(current_batch_size // 2))
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

            pipeline_healthy = _pipeline_healthy_track(
                funnel,
                FUNNEL_MIN_PROOF_CONVERSION_RATE,
                FUNNEL_MAX_STOPPED_OR_EXPIRED_RATE,
            )
            target_bundles, held_healthy_track_bundles, hold_reason = _hold_healthy_track_bundles(
                enabled=HOLD_HEALTHY_TRACK_BUNDLES,
                pipeline_healthy=pipeline_healthy,
                action=action,
                current_bundles=current_bundles,
                target_bundles=target_bundles,
            )
            if held_healthy_track_bundles and hold_reason:
                reasons.append(hold_reason)
                cap_still_drains = target_per_challenge_cap < current_per_challenge_cap
                batch_still_shrinks = target_batch_size < current_batch_size
                if action == "drain_root_backlog_pressure" and cap_still_drains:
                    pass
                elif action == "reduce_tail_time" and batch_still_shrinks:
                    pass
                else:
                    action = "hold_bundles_on_healthy_track"

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
        if is_cpu_challenge and current_batch_size < min_batch_size:
            action = "enforce_aws_cpu_batch_size_floor"
            target_batch_size = min_batch_size
            reasons.append(f"batch_size floor keeps AWS CPU jobs busy up to {min_batch_size} workers")
        elif target_batch_size < min_batch_size:
            target_batch_size = min_batch_size
            reasons.append(f"batch_size floor keeps AWS CPU jobs busy up to {min_batch_size} workers")
        blocked_from_floor = action in {
            "intentional_allowlist_stop",
            "intentional_algorithm_pin_stop",
            "missing_bundle_config",
        }
        if current_bundles > 0 and current_bundles < bundle_floor and not blocked_from_floor:
            action = "enforce_min_bundles"
            target_bundles = bundle_floor
            reasons.append(
                f"num_bundles {current_bundles} is below operator floor {bundle_floor}"
            )
        elif target_bundles > 0 and target_bundles < bundle_floor and not blocked_from_floor:
            target_bundles = bundle_floor
        target_weight = locked_algo_weight(current_weight, target_weight)

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
                "per_challenge_max_benchmarks": current_per_challenge_cap,
            },
            "target": {
                "weight": target_weight,
                "num_bundles": target_bundles,
                "effective_batch_size": target_batch_size,
                "per_challenge_max_benchmarks": target_per_challenge_cap,
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
                "roots_not_started": roots_not_started,
                "old_roots_not_started": old_roots_not_started,
                "oldest_not_started_root_age_min": oldest_not_started_root_age_min,
            },
            "derived": {
                "estimated_nonces_per_bundle": estimated_nonces_per_bundle,
                "current_estimated_root_batches": estimated_root_batches,
                "target_estimated_root_batches": estimated_target_batches,
                "min_batch_size": min_batch_size,
                "min_bundle_floor": bundle_floor,
                "max_job_batches_margin_ok": max_job_batches_margin_ok,
                "policy_posture": posture,
                "pipeline_healthy": pipeline_healthy,
                "held_healthy_track_bundles": held_healthy_track_bundles,
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
    online_gpu = [s for s in slaves if _gpu_online_registered(s)]
    active_gpu = [s for s in online_gpu if _counts_for_capacity(s)]
    warmup_gpu = [s for s in online_gpu if not _counts_for_capacity(s)]
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
    aws_capacity = _aws_batch_capacity(cfg)
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
    active_gpu_units = sum(_gpu_units(s, cfg) for s in active_gpu)
    warmup_gpu_units = sum(_gpu_units(s, cfg) for s in warmup_gpu)
    sizing_gpu_units = max(len(online_gpu), int(active_gpu_units or 0) + int(warmup_gpu_units or 0))
    largest_gpu_units = 0
    for row in online_gpu:
        largest_gpu_units = max(largest_gpu_units, _gpu_units(row, cfg))
    productive_idle_gpu_units = sum(_gpu_units(s, cfg) for s in productive_idle_gpu)
    return {
        "active_cpu": len(active_cpu),
        "active_gpu": len(active_gpu),
        "active_gpu_units": max(len(active_gpu), int(active_gpu_units or 0)),
        "warmup_gpu_units": int(warmup_gpu_units or 0),
        "sizing_gpu_units": int(sizing_gpu_units or 0),
        "largest_gpu_units": int(largest_gpu_units or 0),
        "productive_idle_cpu": len(productive_idle_cpu),
        "productive_idle_gpu": len(productive_idle_gpu),
        "productive_idle_gpu_units": int(productive_idle_gpu_units or 0),
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
        "gpu_slot_floor": _gpu_slot_floor(cfg),
        "current_adaptive_caps": cfg.get("adaptive_slave_caps") or {},
        "aws_batch_capacity": aws_capacity,
        "aws_cpu_jobs": aws_capacity.get("max_concurrent_cpu_jobs", 0),
        "aws_cpu_threads_per_instance": aws_capacity.get("cpu_threads_per_instance", 0),
        "aws_cpu_batch_size_floor": aws_capacity.get("cpu_batch_size_floor", WORKLOAD_MIN_BATCH_SIZE),
    }


def _gpu_job_target(capacity: dict) -> int:
    """Open GPU benchmarks from connected GPU *units*, not slave names.

    One job feeds several GPUs through root-batch fan-out. Hold the current
    job count while the live fleet still covers it; drop only after units
    leave. Never go below busy occupancy or the operator floor.
    """
    units = int(capacity.get("sizing_gpu_units") or capacity.get("active_gpu_units") or 0)
    names = int(capacity.get("active_gpu") or 0)
    warmup = int(capacity.get("warmup_gpu_units") or 0)
    units = max(units, names, warmup)
    if units <= 0 and names <= 0:
        return 0

    per = max(1, int(GPU_UNITS_PER_JOB))
    from_units = (units + per - 1) // per
    min_parallel = 1 if units <= 1 else min(len(GPU_SLOT_TYPES), units)
    current_total = sum(
        int((capacity.get("current_slots") or {}).get(slot_type, 0) or 0)
        for slot_type in GPU_SLOT_TYPES
    )
    busy_total = sum(
        int((capacity.get("slot_busy") or {}).get(slot_type, 0) or 0)
        for slot_type in GPU_SLOT_TYPES
    )
    floor_total = sum(
        int((capacity.get("gpu_slot_floor") or {}).get(slot_type, 0) or 0)
        for slot_type in GPU_SLOT_TYPES
    )
    have_slot_telemetry = busy_total > 0 or any(
        int((capacity.get("slot_counts") or {}).get(slot_type, 0) or 0)
        for slot_type in GPU_SLOT_TYPES
    )
    # Empty slot tables must not ghost-downscale a live GPU fleet.
    hold = current_total if (units > 0 and not have_slot_telemetry) else min(current_total, units)
    pressure = int(capacity.get("gpu_pressure") or 0)
    spare = max(0, int(GPU_JOB_SPARE))
    target = max(
        min_parallel,
        from_units,
        hold,
        busy_total + spare,
        floor_total,
        pressure,
        1,
    )

    cpu_slots = int((capacity.get("current_slots") or {}).get(CPU_SLOT_TYPE, 0) or 0)
    busy_cpu = int((capacity.get("slot_busy") or {}).get(CPU_SLOT_TYPE, 0) or 0)
    cpu_need = max(
        int(capacity.get("aws_cpu_jobs") or 0),
        min(cpu_slots, max(int(capacity.get("active_cpu") or 0), busy_cpu)),
    )
    room = UPSTREAM_SAFE_MAX_BENCHMARKS - cpu_need - BENCHMARK_BUFFER
    hard_cap = MAX_GPU_SLOTS_PER_TYPE * len(GPU_SLOT_TYPES)
    target = min(target, hard_cap)
    if room > 0:
        target = min(target, max(floor_total, busy_total, 1, room))
    return max(1, int(target))


def _target_resource_slots(capacity: dict) -> dict:
    current_slots = capacity["current_slots"]
    slot_counts = capacity["slot_counts"]
    slot_idle = capacity["slot_idle"]
    slot_busy = capacity["slot_busy"]
    proposed = dict(current_slots)

    current_cpu = int(current_slots.get(CPU_SLOT_TYPE, slot_counts.get(CPU_SLOT_TYPE, 0)) or 0)
    aws_jobs = int(capacity.get("aws_cpu_jobs") or 0)
    busy_cpu = int(slot_busy.get(CPU_SLOT_TYPE, 0) or 0)
    if aws_jobs > 0 or capacity["active_cpu"]:
        active_floor = 0
        if capacity["active_cpu"]:
            active_floor = max(
                2,
                (capacity["active_cpu"] + PRODUCTIVE_IDLE_CPU_PER_SLOT - 1)
                // max(1, PRODUCTIVE_IDLE_CPU_PER_SLOT),
            )
        cpu_pressure = int(capacity.get("cpu_pressure") or 0)
        have_cpu_telemetry = busy_cpu > 0 or int(slot_counts.get(CPU_SLOT_TYPE, 0) or 0) > 0
        target_cpu = max(aws_jobs, busy_cpu, active_floor, cpu_pressure)
        if not have_cpu_telemetry and capacity["active_cpu"]:
            target_cpu = max(target_cpu, min(current_cpu, max(cpu_pressure, active_floor)))
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
            target_cpu = max(aws_jobs, busy_cpu, 2, current_cpu - 1)
        elif slot_idle.get(CPU_SLOT_TYPE, 0) == 0 and capacity["cpu_pressure"] >= max(1, current_cpu):
            target_cpu = max(target_cpu, current_cpu + 2)
        proposed[CPU_SLOT_TYPE] = min(target_cpu, MAX_CPU_SLOTS)
    else:
        proposed[CPU_SLOT_TYPE] = 0

    current_gpu_slots = {
        slot_type: int(current_slots.get(slot_type, slot_counts.get(slot_type, 0)) or 0)
        for slot_type in GPU_SLOT_TYPES
    }
    gpu_slot_floor = capacity.get("gpu_slot_floor") or {}
    adaptive_caps = capacity.get("current_adaptive_caps") or {}
    gpu_floor_total = sum(int(gpu_slot_floor.get(slot_type, 0) or 0) for slot_type in GPU_SLOT_TYPES)
    busy_gpu_slots = {
        slot_type: int(slot_busy.get(slot_type, 0) or 0)
        for slot_type in GPU_SLOT_TYPES
    }
    # Single-GPU / broken adaptive-cap recovery: honor operator floor only.
    # Do not pin a multi-GPU join to the floor just because gpu_max_cap is still 1.
    if (
        gpu_floor_total > 0
        and int(adaptive_caps.get("gpu_max_cap") or 0) <= 1
        and int(capacity.get("sizing_gpu_units") or capacity.get("active_gpu_units") or 0) <= 1
        and int(capacity.get("active_gpu") or 0) <= 1
    ):
        for slot_type in GPU_SLOT_TYPES:
            proposed[slot_type] = max(
                int(gpu_slot_floor.get(slot_type, 0) or 0),
                int(busy_gpu_slots.get(slot_type, 0) or 0),
            )
        return proposed

    # GPU jobs follow connected GPU units (join/leave). Slave names are not
    # 1:1 with jobs — a 12-GPU box shares a few benchmarks.
    sizing_units = int(capacity.get("sizing_gpu_units") or capacity.get("active_gpu_units") or 0)
    if not capacity["active_gpu"] and sizing_units <= 0:
        for slot_type in GPU_SLOT_TYPES:
            proposed[slot_type] = int(gpu_slot_floor.get(slot_type, 0) or 0)
        return proposed

    gpu_target_total = _gpu_job_target(capacity)
    if (
        capacity.get("gpu_pressure", 0) > gpu_target_total
        and sum(busy_gpu_slots.values()) >= gpu_target_total
    ):
        gpu_target_total += 1

    busy_total = sum(busy_gpu_slots.values())
    floor_total = sum(int(gpu_slot_floor.get(slot_type, 0) or 0) for slot_type in GPU_SLOT_TYPES)
    gpu_target_total = max(gpu_target_total, busy_total, floor_total, 1)
    gpu_target_total = min(gpu_target_total, MAX_GPU_SLOTS_PER_TYPE * len(GPU_SLOT_TYPES))

    proposed_gpu_slots = {
        slot_type: int(gpu_slot_floor.get(slot_type, 0) or 0)
        for slot_type in GPU_SLOT_TYPES
    }
    remaining = max(0, gpu_target_total - sum(proposed_gpu_slots.values()))
    type_order = sorted(
        GPU_SLOT_TYPES,
        key=lambda key: (
            -busy_gpu_slots.get(key, 0),
            -current_gpu_slots.get(key, 0),
            key,
        ),
    )

    # Cover busy demand first so downscales do not undercut live GPU work.
    for slot_type in type_order:
        if remaining <= 0:
            break
        room = MAX_GPU_SLOTS_PER_TYPE - proposed_gpu_slots[slot_type]
        if room <= 0:
            continue
        need = max(0, busy_gpu_slots.get(slot_type, 0) - proposed_gpu_slots[slot_type])
        add = min(room, remaining, need)
        if add > 0:
            proposed_gpu_slots[slot_type] += add
            remaining -= add

    # Spread leftover capacity across types (still may be below prior highs).
    while remaining > 0:
        progressed = False
        for slot_type in type_order:
            if remaining <= 0:
                break
            room = MAX_GPU_SLOTS_PER_TYPE - proposed_gpu_slots[slot_type]
            if room <= 0:
                continue
            proposed_gpu_slots[slot_type] += 1
            remaining -= 1
            progressed = True
        if not progressed:
            break

    proposed.update(proposed_gpu_slots)
    return proposed


def _slot_capacity_max_concurrent(capacity: dict, proposed_slots: dict) -> int:
    active_gpu = capacity["active_gpu"] > 0
    active_cpu = capacity["active_cpu"] > 0 or int(capacity.get("aws_cpu_jobs") or 0) > 0
    cpu_slot_total = int(proposed_slots.get(CPU_SLOT_TYPE, 0) or 0) if active_cpu else 0
    gpu_slot_total = (
        sum(int(proposed_slots.get(k, 0) or 0) for k in GPU_SLOT_TYPES)
        if active_gpu
        else 0
    )
    buffer = BENCHMARK_BUFFER if (active_cpu or active_gpu) else 0
    return _clamp(cpu_slot_total + gpu_slot_total + buffer, MIN_MAX_BENCHMARKS, UPSTREAM_SAFE_MAX_BENCHMARKS)


def _live_worker_floor_max_concurrent(capacity: dict) -> int:
    """Minimum concurrent room for currently online compute (not configured slots).

    Using configured slots as a floor would make completion-matching a no-op
    whenever resource_slots were already oversized.
    """
    aws_cpu_jobs = int(capacity.get("aws_cpu_jobs") or 0)
    active_cpu = int(capacity.get("active_cpu") or 0)
    active_gpu = int(capacity.get("active_gpu") or 0)
    cpu_pressure = int(capacity.get("cpu_pressure") or 0)
    gpu_pressure = int(capacity.get("gpu_pressure") or 0)
    # Prefer observed in-flight work / AWS batch jobs over raw slave counts so a
    # single multi-machine CPU slave still opens enough precommits to stay busy.
    cpu_floor = max(aws_cpu_jobs, min(cpu_pressure, aws_cpu_jobs or cpu_pressure), active_cpu)
    gpu_jobs = _gpu_job_target(capacity) if (active_gpu or int(capacity.get("sizing_gpu_units") or 0)) else 0
    gpu_floor = min(gpu_jobs, max(active_gpu, gpu_pressure, gpu_jobs))
    buffer = BENCHMARK_BUFFER if (cpu_floor or gpu_floor) else 0
    return _clamp(cpu_floor + gpu_floor + buffer, MIN_MAX_BENCHMARKS, UPSTREAM_SAFE_MAX_BENCHMARKS)


def _completion_matched_max_concurrent(
    capacity: dict,
    proposed_slots: dict,
    reward_funnel: dict | None = None,
) -> tuple[int, dict]:
    """Target concurrent precommits from live capacity + recent finish quality.

    Scales up as compute joins (live floor / slot capacity rise) but caps the
    open-precommit budget when finishes/conversion lag so workers stay on
    finishable work instead of an ever-growing root backlog.
    """
    slot_target = _slot_capacity_max_concurrent(capacity, proposed_slots)
    live_floor = min(slot_target, _live_worker_floor_max_concurrent(capacity))
    funnel = (reward_funnel or {}).get("summary") or {}
    seen = int(funnel.get("benchmarks_seen") or 0)
    root_ready = int(funnel.get("root_ready_benchmarks") or 0)
    proof_submitted = int(funnel.get("proof_submitted_confirmed") or 0)
    finished = max(proof_submitted, root_ready)
    conversion = funnel.get("proof_conversion_rate")
    root_ready_rate = funnel.get("root_ready_rate")
    funnel_safe = bool(funnel.get("safe_to_scale_workload", True))
    backlog = _root_backlog_pressure(funnel)
    finish_based = _clamp(
        int(round(finished * COMPLETION_INFLIGHT_MULT)) + COMPLETION_MATCH_BUFFER,
        MIN_MAX_BENCHMARKS,
        UPSTREAM_SAFE_MAX_BENCHMARKS,
    )
    quality = 1.0
    if conversion is not None:
        quality = min(quality, max(0.25, float(conversion)))
    if root_ready_rate is not None:
        quality = min(quality, max(0.25, float(root_ready_rate)))

    signals = {
        "slot_target": slot_target,
        "live_floor": live_floor,
        "benchmarks_seen": seen,
        "root_ready_benchmarks": root_ready,
        "proof_submitted_confirmed": proof_submitted,
        "finished": finished,
        "finish_based": finish_based,
        "quality": round(quality, 4),
        "funnel_safe": funnel_safe,
        "backlog_reasons": (backlog or {}).get("reasons") or [],
        "completion_match_enabled": COMPLETION_MATCH_ENABLED,
    }

    if not COMPLETION_MATCH_ENABLED:
        signals["mode"] = "slot_capacity_only"
        return slot_target, signals

    if seen < COMPLETION_MATCH_MIN_SAMPLES:
        target = max(live_floor, min(slot_target, live_floor + COMPLETION_WARMUP_HEADROOM))
        signals["mode"] = "warmup_live_floor"
    elif backlog or not funnel_safe:
        target = max(live_floor, min(slot_target, finish_based))
        signals["mode"] = "finish_matched_constrained"
    else:
        high_quality = (
            (conversion is None or float(conversion) >= WORKLOAD_HIGH_PROOF_CONVERSION_RATE)
            and (
                root_ready_rate is None
                or float(root_ready_rate) >= ROOT_READY_RATE_MIN_FOR_UPSCALE
            )
        )
        if high_quality and finish_based >= max(live_floor, max(1, slot_target // 2)):
            target = slot_target
            signals["mode"] = "healthy_slot_capacity"
        else:
            blended = live_floor + int(round((slot_target - live_floor) * quality))
            target = max(live_floor, min(slot_target, max(finish_based, blended)))
            signals["mode"] = "healthy_ramping" if high_quality else "quality_blended"

    target = _clamp(target, MIN_MAX_BENCHMARKS, UPSTREAM_SAFE_MAX_BENCHMARKS)
    signals["target"] = target
    return target, signals


def _target_max_concurrent_benchmarks(
    capacity: dict,
    proposed_slots: dict,
    reward_funnel: dict | None = None,
) -> int:
    target, _signals = _completion_matched_max_concurrent(capacity, proposed_slots, reward_funnel)
    return target


def _capacity_floor_max_concurrent(capacity: dict, proposed_slots: dict | None = None) -> int:
    proposed_slots = proposed_slots or {}
    aws_cpu_jobs = int(capacity.get("aws_cpu_jobs") or 0)
    cpu_slots = int(proposed_slots.get(CPU_SLOT_TYPE, 0) or 0)
    active_cpu = capacity.get("active_cpu", 0) > 0 or aws_cpu_jobs > 0
    cpu_floor = max(aws_cpu_jobs, cpu_slots if active_cpu else 0)

    gpu_slots = sum(int(proposed_slots.get(k, 0) or 0) for k in GPU_SLOT_TYPES)
    gpu_jobs = _gpu_job_target(capacity) if (
        int(capacity.get("active_gpu") or 0) or int(capacity.get("sizing_gpu_units") or 0)
    ) else 0
    gpu_floor = max(gpu_jobs, min(gpu_slots, gpu_jobs or gpu_slots))

    buffer = BENCHMARK_BUFFER if (cpu_floor or gpu_floor) else 0
    return _clamp(cpu_floor + gpu_floor + buffer, MIN_MAX_BENCHMARKS, UPSTREAM_SAFE_MAX_BENCHMARKS)


def _funnel_drain_floor(capacity_model: dict | None = None) -> int:
    """Soft floor while draining unhealthy/flooded precommit capacity.

    Do not pin to the full CPU capacity floor — that is what left live pools
    stuck near the upstream ceiling while the reward funnel was already failing.
    Keep a small GPU reserve so focused GPU work is not starved during CPU drain.
    """
    floor = max(MIN_MAX_BENCHMARKS, FUNNEL_DRAIN_MIN_MAX_BENCHMARKS)
    gpu_jobs = 0
    if capacity_model:
        if int(capacity_model.get("active_gpu") or 0) or int(capacity_model.get("sizing_gpu_units") or 0):
            gpu_jobs = _gpu_job_target(capacity_model)
    if gpu_jobs > 0:
        floor = max(floor, min(gpu_jobs + BENCHMARK_BUFFER, UPSTREAM_SAFE_MAX_BENCHMARKS))
    return floor


def _profile_roots_pending(funnel_summary: dict | None) -> tuple[int, int, int]:
    """Return (total, cpu, gpu) pending roots for active jobs.

    When the funnel summary includes cpu/gpu splits, use them so CPU backlog
    (knapsack/energy) cannot drive global max_concurrent drain. Legacy summaries
    without splits fall back to treating total pending as GPU-affecting.
    """
    funnel_summary = funnel_summary or {}
    roots_pending = int(funnel_summary.get("roots_pending") or 0)
    has_split = (
        funnel_summary.get("gpu_roots_pending") is not None
        or funnel_summary.get("cpu_roots_pending") is not None
    )
    if has_split:
        cpu_roots_pending = int(funnel_summary.get("cpu_roots_pending") or 0)
        gpu_roots_pending = int(funnel_summary.get("gpu_roots_pending") or 0)
    else:
        # Legacy summaries: unknown mix — keep prior total-based safety for both
        # GPU max_concurrent drain and idle-CPU hard-cap gates.
        cpu_roots_pending = roots_pending
        gpu_roots_pending = roots_pending
    return roots_pending, cpu_roots_pending, gpu_roots_pending


def _root_backlog_pressure(funnel_summary: dict | None) -> dict | None:
    """Detect GPU root backlog that should block or reverse max_concurrent upscales.

    CPU-only pending roots do not trigger this pressure — those are drained via
    per-challenge workload actions and the master's precommit governor.
    """
    funnel_summary = funnel_summary or {}
    if (
        funnel_summary.get("roots_pending") is None
        and funnel_summary.get("gpu_roots_pending") is None
        and funnel_summary.get("root_ready_rate") is None
    ):
        return None
    roots_pending, cpu_roots_pending, gpu_roots_pending = _profile_roots_pending(
        funnel_summary
    )
    root_ready_rate = funnel_summary.get("root_ready_rate")
    seen = int(funnel_summary.get("benchmarks_seen") or 0)
    reasons = []
    if gpu_roots_pending >= ROOT_PENDING_MAX_CONCURRENT_DRAIN:
        reasons.append("gpu_roots_pending_above_drain_threshold")
    # Global root_ready_rate is CPU+GPU. A knapsack 137 pile can drop it to
    # 0.27 while a handful of GPU roots are still pending — that must not
    # yank max_concurrent to the drain floor (12).
    if (
        gpu_roots_pending > 0
        and gpu_roots_pending > cpu_roots_pending
        and seen >= 5
        and root_ready_rate is not None
        and float(root_ready_rate) < ROOT_READY_RATE_MIN_FOR_UPSCALE
    ):
        reasons.append("low_root_ready_rate_with_pending_gpu_roots")
    if not reasons:
        return None
    return {
        "roots_pending": roots_pending,
        "cpu_roots_pending": cpu_roots_pending,
        "gpu_roots_pending": gpu_roots_pending,
        "root_ready_rate": root_ready_rate,
        "benchmarks_seen": seen,
        "reasons": reasons,
        "threshold": ROOT_PENDING_MAX_CONCURRENT_DRAIN,
        "min_root_ready_rate": ROOT_READY_RATE_MIN_FOR_UPSCALE,
    }


def precommit_already_oversubscribed(
    *,
    active_jobs: int = 0,
    current_max: int = 0,
) -> bool:
    """True when open jobs already exceed the parked create ceiling.

    Drain may lower the cap under in-flight work. Raising it again while
    oversubscribed is the 78/20 yo-yo.
    """
    return int(active_jobs or 0) > int(current_max or 0)


def oversub_upscale_allowed(
    *,
    active_jobs: int = 0,
    current_max: int = 0,
    proposed_max: int = 0,
) -> bool:
    """Allow a parked-cap climb toward proven live jobs.

    34/20 with idle-CPU override is already absorbed. Blocking every raise
    pins the floor at 20 forever. 78/20 is a flood and must stay blocked.
    """
    jobs = int(active_jobs or 0)
    cap = int(current_max or 0)
    proposed = int(proposed_max or 0)
    if jobs <= cap:
        return True
    return jobs <= max(proposed, cap * 2)


def health_block_reasons(health: dict | None = None) -> dict:
    """Name the dirt that keeps healthy=False / clean_windows=0."""
    health = health or {}
    unregistered = [
        str(name) for name in (health.get("active_unregistered") or []) if name
    ]
    stranded = health.get("unserved_stranded_benchmarks") or []
    stale_roots = int(health.get("stale_roots") or 0)
    stale_proofs = int(health.get("stale_proofs") or 0)
    reasons = []
    if stale_roots > PRODUCTIVE_IDLE_STALE_ROOT_TOLERANCE:
        reasons.append("stale_roots")
    if stale_proofs > 0:
        reasons.append("stale_proofs")
    if unregistered:
        reasons.append("active_unregistered")
    if stranded:
        reasons.append("unserved_stranded")
    return {
        "reasons": reasons,
        "stale_roots": stale_roots,
        "stale_proofs": stale_proofs,
        "active_unregistered": unregistered,
        "unserved_stranded": len(stranded),
    }


def leftover_stranded_blocks_ratchet(unserved_stranded) -> bool:
    """Block only fat jobs nobody is working.

    Leftover crumbs on live jobs are normal pull-queue food. Treating
    them as stranded pinned max_concurrent at 20 while 32 jobs already
    ran. Unassigned fat jobs (no owner, more than a leftover batch)
    still block.
    """
    for item in list(unserved_stranded or []):
        pending = int(item.get("pending_roots") or 0)
        assigned = int(item.get("assigned_roots") or 0)
        if assigned <= 0 and pending > 4:
            return True
    return False


def should_raise_cap_for_seat_hole(
    *,
    idle_cpu: int = 0,
    cpu_claimable: int = 0,
    idle_gpu: int = 0,
    gpu_claimable: int = 0,
    current_max: int = 0,
    active_jobs: int = 0,
    has_unregistered: bool = False,
    unresolved_ceiling: int = 85,
) -> tuple[bool, str]:
    """Raise max_concurrent when idle seats have nothing claimable.

    Master honors the cap again. Autopilot must move it, or empty GPUs
    sit idle while the badge says saturated.
    """
    if has_unregistered:
        return False, "unregistered_active_work"
    cpu_hole = int(idle_cpu or 0) > 0 and int(cpu_claimable or 0) < int(idle_cpu or 0)
    gpu_hole = int(idle_gpu or 0) > 0 and int(gpu_claimable or 0) < int(idle_gpu or 0)
    if not (cpu_hole or gpu_hole):
        return False, "no_seat_hole"
    ceiling = int(unresolved_ceiling or 0)
    if ceiling > 0 and int(current_max or 0) >= ceiling:
        return False, "tig_ceiling"
    if int(active_jobs or 0) < int(current_max or 0):
        return False, "cap_has_room"
    return True, "seat_hole_at_cap"


def should_ratchet_parked_cap_to_live(
    *,
    active_jobs: int,
    current_max: int,
    proposed_max: int,
    has_unregistered: bool,
    unserved_stranded=None,
) -> tuple[bool, str]:
    """Raise the parked floor toward jobs already running.

    Idle-CPU scale never fires when the fleet is full at 34/20. The main
    apply path then dies on leftover stranded work, so clean_windows stays 0
    and the floor never moves. This path only records proven live work. It
    does not need idle workers, clean windows, stale-root health, or a high
    root-ready rate (leftovers tank that). Unregistered workers still block.
    A last leftover (1-2 crumbs) does not.
    """
    if not precommit_already_oversubscribed(
        active_jobs=active_jobs, current_max=current_max
    ):
        return False, "not_oversubscribed"
    if not oversub_upscale_allowed(
        active_jobs=active_jobs,
        current_max=current_max,
        proposed_max=proposed_max,
    ):
        return False, "oversub_flood"
    if leftover_stranded_blocks_ratchet(unserved_stranded):
        return False, "stranded_benchmarks_present"
    if has_unregistered:
        return False, "unregistered_active_work"
    if int(proposed_max or 0) <= int(current_max or 0):
        return False, "proposed_max_not_higher"
    return True, "ratchet_parked_cap_to_proven_live"


def idle_hole_blocks_cap_drain(
    *,
    idle_cpu: int = 0,
    cpu_claimable: int = 0,
    idle_gpu: int = 0,
    gpu_claimable: int = 0,
) -> bool:
    """Do not shrink max_concurrent while empty seats have nothing to pull.

    The 42/20 stall was idle CPUs + 0 claimable while autopilot drained the
    parked cap and precommit refused to refill.
    """
    if int(idle_cpu or 0) > 0 and int(cpu_claimable or 0) <= 0:
        return True
    if int(idle_gpu or 0) > 0 and int(gpu_claimable or 0) <= 0:
        return True
    return False


def should_idle_cpu_max_scale(
    *,
    enabled: bool,
    productive_idle_cpu: int,
    min_idle: int,
    slot_idle_cpu: int = 0,
    min_slot_idle_cpu: int = 0,
    proof_conversion_rate=None,
    soft_proof_conversion_floor: float = 0.80,
    root_ready_rate,
    min_root_ready_rate: float,
    roots_pending: int,
    max_roots_pending: int,
    benchmarks_seen: int,
    current_max: int,
    proposed_max: int,
    active_jobs: int,
    stale_proofs: int,
    has_stranded: bool,
    has_unregistered: bool,
) -> tuple[bool, str]:
    """Pure gate: allow a small max_concurrent bump for idle/free CPU capacity.

    Used when global funnel_safe is false because of slow GPU proof tails, but
    root completion is healthy. Prefer proven idle workers; if those are busy,
    free CPU slots + soft conversion floor can still justify a slow climb off
    the drain floor.
    """
    if not enabled:
        return False, "idle_cpu_max_scale_disabled"
    if has_stranded:
        return False, "stranded_benchmarks_present"
    if has_unregistered:
        return False, "unregistered_active_work"
    if int(stale_proofs or 0) > 0:
        return False, "stale_proofs_present"
    productive_idle_ok = int(productive_idle_cpu or 0) >= int(min_idle or 0)
    slot_idle_ok = (
        int(min_slot_idle_cpu or 0) > 0
        and int(slot_idle_cpu or 0) >= int(min_slot_idle_cpu or 0)
    )
    if not productive_idle_ok and not slot_idle_ok:
        return False, "productive_idle_cpu_below_min"
    if int(roots_pending or 0) >= int(max_roots_pending or 0):
        return False, "roots_pending_at_hard_cap"
    if int(benchmarks_seen or 0) >= 5:
        if root_ready_rate is None:
            return False, "root_ready_rate_missing"
        if float(root_ready_rate) < float(min_root_ready_rate):
            return False, "root_ready_rate_below_min"
        # Slot-idle path (no proven idle workers) needs conversion above soft floor.
        if not productive_idle_ok and slot_idle_ok:
            if proof_conversion_rate is None:
                return False, "proof_conversion_missing_for_slot_idle_scale"
            if float(proof_conversion_rate) < float(soft_proof_conversion_floor):
                return False, "proof_conversion_below_soft_floor_for_slot_idle_scale"
    if int(proposed_max or 0) <= int(current_max or 0):
        return False, "proposed_max_not_higher"
    if precommit_already_oversubscribed(
        active_jobs=active_jobs, current_max=current_max
    ) and not oversub_upscale_allowed(
        active_jobs=active_jobs,
        current_max=current_max,
        proposed_max=proposed_max,
    ):
        return False, "precommit_already_oversubscribed"
    # Only bump when the current ceiling is actually binding.
    if int(active_jobs or 0) < max(1, int(current_max or 0) - 1):
        return False, "precommit_capacity_not_saturated"
    if productive_idle_ok:
        return True, "idle_cpu_needs_max_headroom"
    return True, "free_cpu_slots_need_max_headroom"


def soft_conversion_drain_grace_timer(
    *,
    in_soft_marginal_state: bool,
    now_ms: int,
    grace_started_ms: int | None,
    grace_limit_ms: int,
) -> tuple[bool, int | None, dict]:
    """Track a one-shot grace window for soft/marginal proof-conversion drain skip.

    Returns (grace_active, next_started_ms, details).
    next_started_ms is None when the stored start should be cleared (left soft
    marginal state). While still marginal, the start is kept even after expiry so
    grace cannot restart until conversion recovers above the target.
    """
    limit = max(0, int(grace_limit_ms or 0))
    if not in_soft_marginal_state:
        return False, None, {
            "grace_active": False,
            "grace_limit_ms": limit,
            "cleared": grace_started_ms is not None,
        }
    started = int(now_ms) if grace_started_ms is None else int(grace_started_ms)
    elapsed = max(0, int(now_ms) - started)
    active = elapsed < limit
    return active, started, {
        "grace_active": active,
        "grace_started_ms": started,
        "grace_elapsed_ms": elapsed,
        "grace_limit_ms": limit,
        "grace_remaining_ms": max(0, limit - elapsed),
        "grace_expired": (not active) and limit > 0,
    }


def reward_funnel_max_drain_decision(
    *,
    issues,
    proof_conversion_rate,
    root_ready_rate,
    productive_idle_cpu: int,
    min_idle: int,
    slot_idle_cpu: int = 0,
    min_slot_idle_cpu: int = 0,
    min_root_ready_rate: float,
    min_proof_conversion_rate: float,
    soft_proof_conversion_floor: float,
    soft_conversion_grace_active: bool = False,
) -> tuple[bool, dict]:
    """Decide whether unhealthy reward-funnel issues should drain max_concurrent.

    Returns (should_drain, details). Soft latency noise, and near-threshold
    low_proof_conversion (>= soft floor), can skip drain when roots are healthy
    and free/idle CPU capacity exists. Truly hard conversion/stop issues still drain.

    Special case: soft-only slow_time_to_proof_submission with healthy proof
    conversion (>= min_proof_conversion_rate) never drains max_concurrent — it
    should only block upscale via funnel_safe / recovery posture. Busy CPU fleets
    were sawtoothing max down to the drain floor on long GPU/JS proof tails.

    Soft/marginal low_proof_conversion may also skip drain for a bounded grace
    window (soft_conversion_grace_active) while root_ready is healthy — covering
    fleet-join dilution before new workers' proofs enter the rolling funnel window.
    After grace expires, idle_ok is required again to skip.
    """
    hard_drain_issues = {
        "low_proof_conversion",
        "high_stopped_or_expired_rate",
        "high_unexpected_stopped_or_expired_rate",
        "high_unexpected_stopped_rate",
        "high_unexpected_stopped_without_roots_rate",
        "root_phase_not_complete",
    }
    soft_drain_issues = {
        "slow_time_to_proof_submission",
        "stopped_without_root_work",
    }
    active_issues = set(issues or [])
    hard_hits = active_issues.intersection(hard_drain_issues)
    soft_hits = active_issues.intersection(soft_drain_issues)
    conversion = None if proof_conversion_rate is None else float(proof_conversion_rate)
    floor = float(soft_proof_conversion_floor)
    target = float(min_proof_conversion_rate)
    marginal_conversion = (
        "low_proof_conversion" in hard_hits
        and conversion is not None
        and conversion >= floor
        and conversion < target
    )
    if marginal_conversion:
        hard_hits = hard_hits - {"low_proof_conversion"}
        soft_hits = soft_hits | {"low_proof_conversion"}
    root_ready_ok = (
        root_ready_rate is not None
        and float(root_ready_rate) >= float(min_root_ready_rate)
    )
    productive_idle_ok = int(productive_idle_cpu or 0) >= int(min_idle or 0)
    slot_idle_ok = (
        int(min_slot_idle_cpu or 0) > 0
        and int(slot_idle_cpu or 0) >= int(min_slot_idle_cpu or 0)
    )
    idle_ok = productive_idle_ok or slot_idle_ok
    has_hard = bool(hard_hits)
    has_soft = bool(soft_hits)
    conversion_ok = conversion is not None and conversion >= target
    # Soft latency alone must not yank max while conversion is healthy — even
    # when the fleet is busy (no idle skip). Upscale remains gated elsewhere.
    soft_latency_only = (
        (not has_hard)
        and has_soft
        and soft_hits <= {"slow_time_to_proof_submission"}
    )
    skip_soft_latency = soft_latency_only and conversion_ok
    # Soft hits that are safe to cover with the fleet-join grace window.
    grace_soft_issues = {"low_proof_conversion", "slow_time_to_proof_submission"}
    skip_marginal_grace = (
        bool(soft_conversion_grace_active)
        and marginal_conversion
        and root_ready_ok
        and (not has_hard)
        and has_soft
        and soft_hits <= grace_soft_issues
    )
    skip_soft = (not has_hard) and has_soft and (
        skip_soft_latency
        or skip_marginal_grace
        or (root_ready_ok and idle_ok)
    )
    should_drain = has_hard or (has_soft and not skip_soft)
    details = {
        "has_hard_drain": has_hard,
        "has_soft_drain": has_soft,
        "hard_issues": sorted(hard_hits),
        "soft_issues": sorted(soft_hits),
        "marginal_low_proof_conversion": marginal_conversion,
        "skip_soft_drain": skip_soft,
        "skip_soft_latency_healthy_conversion": skip_soft_latency,
        "skip_marginal_conversion_grace": skip_marginal_grace,
        "soft_conversion_grace_active": bool(soft_conversion_grace_active),
        "root_ready_ok": root_ready_ok,
        "idle_ok": idle_ok,
        "productive_idle_ok": productive_idle_ok,
        "slot_idle_ok": slot_idle_ok,
        "slot_idle_cpu": int(slot_idle_cpu or 0),
        "proof_conversion_rate": conversion,
        "soft_proof_conversion_floor": floor,
        "conversion_ok": conversion_ok,
    }
    return should_drain, details


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
    if (capacity["active_cpu"] or int(capacity.get("aws_cpu_jobs") or 0) > 0) and cpu_ids:
        cpu_slots = int(proposed_slots.get(CPU_SLOT_TYPE, 0) or 0)
        per_cpu_target = max(1, math.ceil(cpu_slots / max(1, len(cpu_ids))))
        for challenge_id in cpu_ids:
            if stale_blocking and challenge_id in stale_challenge_ids:
                continue
            current = int(current_per.get(challenge_id, 1) or 1)
            proposed[challenge_id] = min(
                max(current, per_cpu_target),
                _max_challenge_benchmarks(challenge_id),
            )
    if capacity["active_gpu"]:
        # Follow proposed GPU slots both up and down (no historical ratchet).
        # Stale tracks keep their current challenge cap until cleaned.
        current_c004 = int(current_per.get("c004", 1) or 1)
        current_c005 = int(current_per.get("c005", 1) or 1)
        current_c006 = int(current_per.get("c006", 1) or 1)
        slot_floor = capacity.get("gpu_slot_floor") or {}

        def _gpu_challenge_target(challenge_id: str, slot_type: str, current: int) -> int:
            if stale_blocking and challenge_id in stale_challenge_ids:
                return current
            slot_target = max(
                int(slot_floor.get(slot_type, 0) or 0),
                int(proposed_slots.get(slot_type, 0) or 0),
            )
            return min(max(1, slot_target), _max_challenge_benchmarks(challenge_id))

        proposed.update({
            "c004": _gpu_challenge_target("c004", "vector_search", current_c004),
            "c005": _gpu_challenge_target("c005", "hypergraph", current_c005),
            "c006": _gpu_challenge_target("c006", "neuralnet_optimizer", current_c006),
        })
    else:
        proposed.update({"c004": 1, "c005": 1, "c006": 1})
    return proposed



def _cpu_slave_cap_bounds() -> tuple[int, int]:
    """Return (floor, ceiling) with ceiling winning when env min > max."""
    ceiling = max(1, int(MAX_CPU_SLAVE_CAP))
    floor = min(max(1, int(MIN_CPU_SLAVE_CAP)), ceiling)
    return floor, ceiling


def _gpu_slave_cap_bounds() -> tuple[int, int]:
    ceiling = max(1, int(MAX_GPU_SLAVE_CAP))
    floor = min(max(1, int(MIN_GPU_SLAVE_CAP)), ceiling)
    return floor, ceiling


def _clamp_cpu_slave_cap(value: int) -> int:
    floor, ceiling = _cpu_slave_cap_bounds()
    return max(floor, min(int(value), ceiling))


def _clamp_gpu_slave_cap(value: int) -> int:
    floor, ceiling = _gpu_slave_cap_bounds()
    return max(floor, min(int(value), ceiling))


def _target_adaptive_slave_caps(capacity: dict) -> dict:
    current = capacity.get("current_adaptive_caps") or {}
    if not current:
        return {}
    proposed = dict(current)
    cpu_max = int(current.get("cpu_max_cap", 0) or 0)
    gpu_max = int(current.get("gpu_max_cap", 0) or 0)
    _, cpu_ceiling = _cpu_slave_cap_bounds()
    _, gpu_ceiling = _gpu_slave_cap_bounds()

    # Always honor env ceilings: never treat an already-high live cap as permission
    # to keep climbing past AUTOPILOT_MAX_*_SLAVE_CAP.
    if cpu_max > cpu_ceiling:
        proposed["cpu_max_cap"] = cpu_ceiling
    elif capacity["active_cpu"] and cpu_max:
        if capacity["productive_idle_cpu"] >= PRODUCTIVE_IDLE_CPU_SCALE_MIN:
            proposed["cpu_max_cap"] = _clamp_cpu_slave_cap(cpu_max + 1)
        elif (
            capacity["cpu_completed_recent"] >= CAP_SCALE_COMPLETIONS_PER_STEP
            and capacity["cpu_pressure"] >= capacity["active_cpu"]
        ):
            proposed["cpu_max_cap"] = _clamp_cpu_slave_cap(cpu_max + 1)

    if gpu_max > gpu_ceiling:
        proposed["gpu_max_cap"] = gpu_ceiling
    elif (capacity["active_gpu"] or int(capacity.get("sizing_gpu_units") or 0)) and gpu_max:
        largest = int(capacity.get("largest_gpu_units") or 0)
        if largest > gpu_max:
            proposed["gpu_max_cap"] = _clamp_gpu_slave_cap(max(gpu_max + 1, min(largest, gpu_max + 4)))
        elif capacity["productive_idle_gpu"] >= PRODUCTIVE_IDLE_GPU_SCALE_MIN:
            proposed["gpu_max_cap"] = _clamp_gpu_slave_cap(gpu_max + 1)
        elif (
            capacity["gpu_completed_recent"] >= CAP_SCALE_COMPLETIONS_PER_STEP
            and capacity["gpu_pressure"] >= capacity["active_gpu"]
        ):
            proposed["gpu_max_cap"] = _clamp_gpu_slave_cap(gpu_max + 1)
    return proposed


def _route_cap_signals(route: dict, slaves: list[dict], current: int) -> dict:
    matched = [
        row
        for row in (slaves or [])
        if _route_matches_slave(route, str(row.get("slave_name") or ""))
    ]
    eligible = [row for row in matched if _capacity_eligible(row)]
    stale_roots = sum(int(row.get("stale_roots") or 0) for row in eligible)
    stale_proofs = sum(int(row.get("stale_proofs") or 0) for row in eligible)
    completed_recent = sum(int(row.get("completed_recent") or 0) for row in eligible)
    active_unfinished = sum(int(row.get("active_unfinished") or 0) for row in eligible)
    active_now = sum(1 for row in eligible if row.get("active_now"))
    saturated = [
        row
        for row in eligible
        if current > 0 and int(row.get("active_unfinished") or 0) >= current
    ]
    adaptive_limited = [
        row
        for row in eligible
        if current > 1
        and row.get("active_now")
        and int(row.get("active_unfinished") or 0) < current
        and int(row.get("completed_recent") or 0) > 0
    ]
    runtime_values = [
        float(row.get("avg_runtime_sec") or 0)
        for row in eligible
        if row.get("avg_runtime_sec") is not None
    ]
    return {
        "matched_slaves": len(matched),
        "eligible_slaves": len(eligible),
        "active_now": active_now,
        "active_unfinished": active_unfinished,
        "completed_recent": completed_recent,
        "stale_roots": stale_roots,
        "stale_proofs": stale_proofs,
        "saturated_slaves": len(saturated),
        "adaptive_limited_slaves": len(adaptive_limited),
        "avg_runtime_sec": round(sum(runtime_values) / len(runtime_values), 1) if runtime_values else None,
        "pressure": round(active_unfinished / max(1, current * max(1, len(eligible))), 3) if current > 0 else 0.0,
    }


def _route_cap_ready(profile: str, current: int, signals: dict) -> tuple[bool, str]:
    if current <= 0:
        return False, "missing_current_route_cap"
    eligible = int(signals.get("eligible_slaves") or 0)
    if eligible <= 0:
        return False, "no_capacity_eligible_slaves"
    if int(signals.get("stale_roots") or 0) > 0 or int(signals.get("stale_proofs") or 0) > 0:
        return False, "route_has_stale_work"
    min_saturated = max(1, math.ceil(eligible * ROUTE_MIN_SATURATED_FRACTION))
    if int(signals.get("saturated_slaves") or 0) < min_saturated:
        return False, "route_not_saturated"
    adaptive_limited = int(signals.get("adaptive_limited_slaves") or 0)
    if adaptive_limited > int(signals.get("saturated_slaves") or 0):
        return False, "adaptive_caps_are_limiter"
    completions_per_slave = ROUTE_GPU_MIN_COMPLETIONS_PER_SLAVE if profile == "gpu" else ROUTE_CPU_MIN_COMPLETIONS_PER_SLAVE
    min_completions = max(1, eligible * completions_per_slave)
    if int(signals.get("completed_recent") or 0) < min_completions:
        return False, "low_recent_completions"
    return True, "route_cap_saturated_with_clean_completions"


def _target_slave_route_caps(cfg: dict, capacity: dict, slaves: list[dict] | None = None) -> list[dict]:
    aws_jobs = int(capacity.get("aws_cpu_jobs") or 0)
    targets = []
    for idx, route in enumerate(cfg.get("slaves") or []):
        profile = _route_profile(route)
        if profile not in {"cpu", "gpu"}:
            continue
        current = int(route.get("max_concurrent_batches") or 0)
        target = current
        reasons = []
        signals = _route_cap_signals(route, slaves or [], current)

        if profile == "cpu" and aws_jobs > 0 and current < aws_jobs:
            target = max(target, aws_jobs)
            reasons.append("aws_cpu_jobs_floor")

        if profile == "gpu" and _route_is_manual_gpu(route):
            name_regex = str(route.get("name_regex") or "").lower()
            local_static = "pool-gpu-local" in name_regex or "^local" in name_regex
            if local_static:
                signals["skipped"] = "manual_gpu_route"
            else:
                route_units = max(
                    (
                        _gpu_units(row, cfg)
                        for row in (slaves or [])
                        if _route_matches_slave(route, str(row.get("slave_name") or ""))
                        and (_counts_for_capacity(row) or _gpu_online_registered(row))
                    ),
                    default=0,
                )
                if route_units > current:
                    target = max(target, min(int(MAX_GPU_SLAVE_CAP), int(route_units)))
                    reasons.append("gpu_worker_units")
                else:
                    signals["skipped"] = "manual_gpu_route"
        else:
            if profile == "gpu":
                route_units = max(
                    (
                        _gpu_units(row, cfg)
                        for row in (slaves or [])
                        if _route_matches_slave(route, str(row.get("slave_name") or ""))
                        and (_counts_for_capacity(row) or _gpu_online_registered(row))
                    ),
                    default=0,
                )
                if route_units > current:
                    target = max(target, min(int(MAX_GPU_SLAVE_CAP), int(route_units)))
                    reasons.append("gpu_worker_units")
            ready, reason = _route_cap_ready(profile, current, signals)
            signals["route_cap_ready"] = ready
            signals["route_cap_reason"] = reason
            if ready:
                step = ROUTE_GPU_UP_STEP if profile == "gpu" else ROUTE_CPU_UP_STEP
                ceiling = MAX_GPU_SLAVE_CAP if profile == "gpu" else MAX_CPU_SLAVE_CAP
                target = max(target, min(current + max(1, step), ceiling))
                reasons.append(reason)

        ceiling = MAX_GPU_SLAVE_CAP if profile == "gpu" else MAX_CPU_SLAVE_CAP
        if current > ceiling:
            target = ceiling
            reasons.append("env_slave_cap_ceiling")
        else:
            target = min(target, ceiling)
        if target != current:
            step = ROUTE_GPU_UP_STEP if profile == "gpu" else ROUTE_CPU_UP_STEP
            if target < current:
                next_value = target  # hard clamp down to env ceiling immediately
            else:
                next_value = _next_value_bounded(current, target, max(1, step), 1)
            targets.append({
                "index": idx,
                "name_regex": route.get("name_regex"),
                "algorithm_id_regex": route.get("algorithm_id_regex"),
                "profile": profile,
                "current": current,
                "target": target,
                "next": next_value,
                "reasons": reasons,
                "signals": signals,
            })
    return targets


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
                    "Resource slots follow connected fleet capacity: CPU headcount up and down, "
                    "GPU jobs from live GPU units (fan-out, not one job per GPU), busy/floor "
                    "guards, and stale-work guardrails. Slots step down when workers leave."
                ),
                "signals": capacity,
                "apply_now": False,
            })

    proposed_max, completion_match = _completion_matched_max_concurrent(
        capacity, proposed_slots, reward_funnel
    )
    current_max = cfg.get("max_concurrent_benchmarks")
    if current_max is not None:
        recommendations.append({
            "key": "max_concurrent_benchmarks",
            "current": current_max,
            "proposed": proposed_max,
            "reason": (
                "Concurrent benchmark target scales with live worker/slot capacity, then "
                "completion-matches to recent root/proof finishes and conversion quality "
                "so precommit creation tracks finishable work."
            ),
            "signals": {
                **capacity,
                "completion_match": completion_match,
            },
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

    route_cap_targets = _target_slave_route_caps(cfg, capacity, slaves)
    if route_cap_targets:
        recommendations.append({
            "key": "slaves.max_concurrent_batches",
            "current": [
                {
                    "name_regex": row.get("name_regex"),
                    "max_concurrent_batches": row.get("current"),
                }
                for row in route_cap_targets
            ],
            "proposed": route_cap_targets,
            "reason": (
                "Slave route caps clamp adaptive assignment. Autopilot raises them only when "
                "matching workers are capacity-eligible, route-saturated, recently productive, "
                "and clean of stale work; AWS CPU routes also honor max_concurrent_cpu_jobs."
            ),
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
                "unexpected_stopped_rate": funnel_summary.get("unexpected_stopped_rate"),
                "unexpected_stopped_without_roots_rate": funnel_summary.get("unexpected_stopped_without_roots_rate"),
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
            stale_roots <= PRODUCTIVE_IDLE_STALE_ROOT_TOLERANCE
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
    effective_stopped_rate = _effective_stopped_rate(funnel_summary)
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
            effective_stopped_rate is None
            or float(effective_stopped_rate) <= FUNNEL_MAX_STOPPED_OR_EXPIRED_RATE / 2
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
            "unexpected_stopped_rate": funnel_summary.get("unexpected_stopped_rate"),
            "unexpected_stopped_without_roots_rate": funnel_summary.get("unexpected_stopped_without_roots_rate"),
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


def _active_gpu_units(report: dict, cfg: dict | None = None) -> int:
    units = 0
    for slave in report.get("slaves") or []:
        if slave.get("profile") == "gpu" and _counts_for_capacity(slave):
            units += _gpu_units(slave, cfg)
    return max(_active_gpu_slave_count(report), units)


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
    challenge_id = str(algorithm_id).split("_", 1)[0]
    algo = _find_algo_selection(new_cfg, algorithm_id)
    if not algo:
        return None
    track_settings = algo.get("track_settings") or {}
    if track not in track_settings:
        return None
    settings = track_settings.get(track) or {}
    current = target.get("current") or {}
    desired = target.get("target") or {}
    derived = target.get("derived") or {}
    next_settings = dict(settings)
    changed = {}

    for field, config_key, floor in (
        ("num_bundles", "num_bundles", WORKLOAD_MIN_BUNDLES),
        ("effective_batch_size", "batch_size", WORKLOAD_MIN_BATCH_SIZE),
    ):
        current_value = int(current.get(field) or next_settings.get(config_key) or 0)
        target_value = int(desired.get(field) or current_value)
        if config_key == "num_bundles":
            floor = max(1, int(derived.get("min_bundle_floor") or floor))
        if config_key == "batch_size":
            floor = max(floor, int(derived.get("min_batch_size") or 0))
        if target_value < current_value:
            target_value = max(floor, target_value)
        elif target_value < floor:
            target_value = floor
        if target_value != current_value:
            next_settings[config_key] = target_value
            changed[config_key] = {
                "current": current_value,
                "target": target_value,
                "next": target_value,
            }

    current_weight = int(current.get("weight") or algo.get("weight") or 0)
    target_weight = locked_algo_weight(
        current_weight,
        int(desired.get("weight") or current_weight),
    )
    if target_weight < current_weight:
        target_weight = max(WORKLOAD_MIN_WEIGHT, target_weight)
    if target_weight != current_weight:
        algo["weight"] = target_weight
        changed["weight"] = {
            "current": current_weight,
            "target": target_weight,
            "next": target_weight,
        }

    current_per_challenge = int(current.get("per_challenge_max_benchmarks") or 0)
    target_per_challenge = int(desired.get("per_challenge_max_benchmarks") or current_per_challenge)
    if current_per_challenge and target_per_challenge < current_per_challenge:
        target_per_challenge = max(ROOT_BACKLOG_DRAIN_MIN_PER_CHALLENGE, target_per_challenge)
        next_per = dict(new_cfg.get("per_challenge_max_benchmarks") or {})
        if int(next_per.get(challenge_id, current_per_challenge) or 0) == current_per_challenge:
            next_per[challenge_id] = target_per_challenge
            new_cfg["per_challenge_max_benchmarks"] = next_per
            changed["per_challenge_max_benchmarks"] = {
                "challenge_id": challenge_id,
                "current": current_per_challenge,
                "target": target_per_challenge,
                "next": target_per_challenge,
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
        if field == "num_bundles":
            previous_value = max(
                previous_value,
                _min_bundles_for_track(str(algorithm_id).split("_", 1)[0], track),
            )
        if int(current_value or 0) != previous_value:
            settings[field] = previous_value
            rollback_changes[field] = {
                "current": current_value,
                "next": previous_value,
                "canary_next": change.get("next"),
            }

    weight_change = (last.get("changes") or {}).get("weight") or {}
    if not WEIGHT_LOCK and weight_change.get("current") is not None:
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

    floor_actions = {
        "enforce_aws_cpu_batch_size_floor",
        "enforce_min_bundles",
    }
    floor_changes = []
    for row in actionable:
        if row.get("action") not in floor_actions:
            continue
        change = _apply_workload_target(new_cfg, row)
        if change:
            floor_changes.append(change)
    if floor_changes:
        actions = {change.get("action") for change in floor_changes}
        if actions == {"enforce_min_bundles"}:
            action = "enforce_min_bundles"
            reasons = ["num_bundles values were below the configured operator floor"]
        elif actions == {"enforce_aws_cpu_batch_size_floor"}:
            action = "enforce_aws_cpu_batch_size_floor"
            reasons = ["CPU track batch_size values were below the configured AWS CPU batch-size floor"]
        else:
            action = "enforce_operator_floors"
            reasons = ["configured operator floors were below current track settings"]
        return {
            "action": action,
            "changes": floor_changes,
            "reasons": reasons,
        }, None

    safety_actions = {
        "drain_root_backlog_pressure",
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
        derived = row.get("derived") or {}
        if (
            HOLD_HEALTHY_TRACK_BUNDLES
            and derived.get("pipeline_healthy")
            and int(target.get("num_bundles") or current.get("num_bundles") or 0)
            < int(current.get("num_bundles") or 0)
        ):
            target = dict(target)
            target["num_bundles"] = current.get("num_bundles")
            row = dict(row)
            row["target"] = target
        reduces_work = (
            int(target.get("num_bundles") or current.get("num_bundles") or 0)
            < int(current.get("num_bundles") or 0)
            or int(target.get("effective_batch_size") or current.get("effective_batch_size") or 0)
            < int(current.get("effective_batch_size") or 0)
            or int(target.get("weight") or current.get("weight") or 0)
            < int(current.get("weight") or 0)
            or int(target.get("per_challenge_max_benchmarks") or current.get("per_challenge_max_benchmarks") or 0)
            < int(current.get("per_challenge_max_benchmarks") or 0)
        )
        if not reduces_work and row.get("action") != "enforce_aws_cpu_batch_size_floor":
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


def _apply_route_cap_targets(new_cfg: dict, route_rec: dict) -> dict | None:
    current_routes = new_cfg.get("slaves") or []
    route_targets = route_rec.get("proposed") or []
    if not route_targets or not current_routes:
        return None

    route_changes = []
    for target in route_targets:
        idx = int(target.get("index"))
        if idx < 0 or idx >= len(current_routes):
            continue
        current = int(current_routes[idx].get("max_concurrent_batches") or 0)
        proposed = int(target.get("target") or current)
        next_value = int(target.get("next") or proposed)
        if next_value == current:
            continue
        # Allow immediate clamp-down when over env ceiling; keep stepwise ups.
        if next_value > current:
            next_value = min(next_value, proposed)
        else:
            next_value = min(next_value, proposed)
        current_routes[idx]["max_concurrent_batches"] = next_value
        route_changes.append({
            "name_regex": current_routes[idx].get("name_regex"),
            "algorithm_id_regex": current_routes[idx].get("algorithm_id_regex"),
            "current": current,
            "target": proposed,
            "next": next_value,
            "profile": target.get("profile"),
            "reasons": target.get("reasons") or [],
            "signals": target.get("signals") or {},
        })

    if not route_changes:
        return None
    new_cfg["slaves"] = current_routes
    return {
        "changes": route_changes,
        "signals": route_rec.get("signals") or {},
    }


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
    route_rec = recommendations.get("slaves.max_concurrent_batches") or {}
    slot_signals = slots_rec.get("signals") or {}
    current_slots_for_gate = ((cfg.get("resource_slots") or {}).get("slots") or {})
    proposed_slots_for_gate = slots_rec.get("proposed") or {}
    productive_idle_cpu = int(slot_signals.get("productive_idle_cpu") or 0)
    productive_idle_gpu = int(slot_signals.get("productive_idle_gpu") or 0)
    hole_blocks_drain = idle_hole_blocks_cap_drain(
        idle_cpu=productive_idle_cpu,
        cpu_claimable=int(funnel_summary.get("cpu_unassigned_claimable") or 0),
        idle_gpu=productive_idle_gpu,
        gpu_claimable=int(funnel_summary.get("gpu_unassigned_claimable") or 0),
    )
    slot_idle_map = slot_signals.get("slot_idle") or {}
    slot_idle_cpu = int(slot_idle_map.get("cpu") or slot_idle_map.get(CPU_SLOT_TYPE) or 0)
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
    route_cap_change_allowed = (
        health["healthy"]
        and funnel_safe
        and posture != "recovery"
        and clean_windows >= APPLY_MIN_CLEAN_WINDOWS
    )
    current_max_for_upstream = cfg.get("max_concurrent_benchmarks")
    if current_max_for_upstream is not None:
        current_max_for_upstream = int(current_max_for_upstream or 0)
        if current_max_for_upstream > UPSTREAM_SAFE_MAX_BENCHMARKS:
            new_cfg = json.loads(json.dumps(cfg))
            new_cfg["max_concurrent_benchmarks"] = UPSTREAM_SAFE_MAX_BENCHMARKS
            decision["reason"] = "clamp_upstream_benchmark_ceiling"
            decision["changes"] = {
                "max_concurrent_benchmarks": {
                    "current": current_max_for_upstream,
                    "target": UPSTREAM_SAFE_MAX_BENCHMARKS,
                    "next": UPSTREAM_SAFE_MAX_BENCHMARKS,
                    "upstream_unresolved_benchmark_limit": TIG_UNRESOLVED_BENCHMARK_LIMIT or None,
                    "operator_ceiling": MAX_MAX_BENCHMARKS,
                    "headroom": TIG_UNRESOLVED_BENCHMARK_HEADROOM,
                    "issues": funnel_summary.get("issues", []),
                    "safe_to_scale_workload": funnel_safe,
                    "policy_posture": posture,
                }
            }
            decision["config"] = new_cfg
            return decision
    floor_targets = [
        row for row in ((report.get("workload_targets") or {}).get("actionable") or [])
        if row.get("action") in {"enforce_min_bundles", "enforce_aws_cpu_batch_size_floor"}
    ]
    if floor_targets:
        floor_cfg = json.loads(json.dumps(cfg))
        floor_change, _ = _next_workload_change(
            floor_cfg,
            report,
            {"actionable": floor_targets},
            health,
            funnel_safe,
            policy_posture,
            clean_windows,
            allow_canary=False,
        )
        if floor_change:
            decision["reason"] = "workload_safety_adjustment"
            decision["changes"] = {"workload_controller": floor_change}
            decision["config"] = floor_cfg
            return decision
    backlog_drain_targets = [
        row for row in ((report.get("workload_targets") or {}).get("actionable") or [])
        if row.get("action") == "drain_root_backlog_pressure"
    ]
    if backlog_drain_targets:
        backlog_cfg = json.loads(json.dumps(cfg))
        backlog_change, backlog_guard = _next_workload_change(
            backlog_cfg,
            report,
            {"actionable": backlog_drain_targets},
            health,
            funnel_safe,
            policy_posture,
            clean_windows,
            allow_canary=False,
        )
        if backlog_change:
            decision["reason"] = "workload_safety_adjustment"
            decision["changes"] = {"workload_controller": backlog_change}
            decision["config"] = backlog_cfg
            return decision
        if backlog_guard:
            decision.setdefault("guardrails", {})["workload_backlog_drain"] = backlog_guard

    if not funnel_safe:
        decision.setdefault("guardrails", {})["reward_funnel"] = {
            "skipped": "funnel_unhealthy_blocks_workload_scale",
            "issues": funnel_summary.get("issues", []),
            "proof_conversion_rate": funnel_summary.get("proof_conversion_rate"),
            "stopped_rate": funnel_summary.get("stopped_rate"),
            "unexpected_stopped_rate": funnel_summary.get("unexpected_stopped_rate"),
            "unexpected_stopped_without_roots_rate": funnel_summary.get("unexpected_stopped_without_roots_rate"),
            "avg_time_to_proof_submit_sec": funnel_summary.get("avg_time_to_proof_submit_sec"),
        }
        # Soft latency / near-threshold conversion should not pin max to the
        # drain floor while roots are converting and CPU workers are idle.
        # Fleet-join dilution also gets a bounded grace window (one metric window
        # by default) before soft/marginal conversion can drain without idle_ok.
        conv_for_grace = funnel_summary.get("proof_conversion_rate")
        root_ready_for_grace = funnel_summary.get("root_ready_rate")
        issues_for_grace = set(funnel_summary.get("issues") or [])
        in_soft_marginal_state = (
            "low_proof_conversion" in issues_for_grace
            and conv_for_grace is not None
            and float(conv_for_grace) >= FUNNEL_SOFT_PROOF_CONVERSION_FLOOR
            and float(conv_for_grace) < FUNNEL_MIN_PROOF_CONVERSION_RATE
            and root_ready_for_grace is not None
            and float(root_ready_for_grace) >= ROOT_READY_RATE_MIN_FOR_UPSCALE
        )
        try:
            raw_grace_started = db.get_setting(SOFT_CONVERSION_GRACE_SETTING, "") or ""
            grace_started_ms = int(raw_grace_started) if str(raw_grace_started).strip() else None
        except Exception:
            grace_started_ms = None
        now_for_grace = int(report.get("generated_at_ms") or time.time() * 1000)
        soft_grace_active, next_grace_started, soft_grace_meta = soft_conversion_drain_grace_timer(
            in_soft_marginal_state=in_soft_marginal_state,
            now_ms=now_for_grace,
            grace_started_ms=grace_started_ms,
            grace_limit_ms=SOFT_CONVERSION_DRAIN_GRACE_MS,
        )
        try:
            if next_grace_started is None:
                if grace_started_ms is not None:
                    db.set_setting(SOFT_CONVERSION_GRACE_SETTING, "")
            elif next_grace_started != grace_started_ms:
                db.set_setting(SOFT_CONVERSION_GRACE_SETTING, str(next_grace_started))
        except Exception:
            pass
        funnel_should_drain, funnel_drain_meta = reward_funnel_max_drain_decision(
            issues=funnel_summary.get("issues") or [],
            proof_conversion_rate=funnel_summary.get("proof_conversion_rate"),
            root_ready_rate=funnel_summary.get("root_ready_rate"),
            productive_idle_cpu=productive_idle_cpu,
            min_idle=IDLE_CPU_MAX_SCALE_MIN,
            slot_idle_cpu=slot_idle_cpu,
            min_slot_idle_cpu=IDLE_CPU_MAX_SCALE_MIN_SLOT_IDLE,
            min_root_ready_rate=ROOT_READY_RATE_MIN_FOR_UPSCALE,
            min_proof_conversion_rate=FUNNEL_MIN_PROOF_CONVERSION_RATE,
            soft_proof_conversion_floor=FUNNEL_SOFT_PROOF_CONVERSION_FLOOR,
            soft_conversion_grace_active=soft_grace_active,
        )
        funnel_drain_meta.update(soft_grace_meta)
        current = int(cfg.get("max_concurrent_benchmarks") or 0)
        capacity_model = report.get("capacity_model") or {}
        drain_floor = _funnel_drain_floor(capacity_model)
        should_drain = (
            current > drain_floor and funnel_should_drain and not hole_blocks_drain
        )
        if hole_blocks_drain:
            decision.setdefault("guardrails", {})["idle_hole_cap_drain"] = {
                "skipped": "empty_seats_with_no_claimable",
                "productive_idle_cpu": productive_idle_cpu,
                "productive_idle_gpu": productive_idle_gpu,
            }
        if should_drain:
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
                    "unexpected_stopped_without_roots_rate": funnel_summary.get("unexpected_stopped_without_roots_rate"),
                    "avg_time_to_proof_submit_sec": funnel_summary.get("avg_time_to_proof_submit_sec"),
                    "roots_pending": funnel_summary.get("roots_pending"),
                    "root_ready_rate": funnel_summary.get("root_ready_rate"),
                    "drain_floor": drain_floor,
                    **funnel_drain_meta,
                }
            }
            decision["config"] = new_cfg
            return decision
        if funnel_drain_meta.get("skip_soft_drain"):
            if funnel_drain_meta.get("skip_marginal_conversion_grace"):
                skip_reason = "marginal_proof_conversion_within_soft_grace"
            elif funnel_drain_meta.get("marginal_low_proof_conversion"):
                skip_reason = "marginal_proof_conversion_with_healthy_roots_and_idle_cpu"
            else:
                skip_reason = "soft_funnel_issues_with_healthy_roots_and_idle_cpu"
            decision.setdefault("guardrails", {})["reward_funnel_drain"] = {
                "skipped": skip_reason,
                "issues": funnel_summary.get("issues", []),
                "root_ready_rate": funnel_summary.get("root_ready_rate"),
                "productive_idle_cpu": productive_idle_cpu,
                **funnel_drain_meta,
            }

    backlog_pressure = _root_backlog_pressure(funnel_summary)
    if backlog_pressure:
        current = int(cfg.get("max_concurrent_benchmarks") or 0)
        capacity_model = report.get("capacity_model") or {}
        drain_floor = _funnel_drain_floor(capacity_model)
        if hole_blocks_drain:
            decision.setdefault("guardrails", {})["root_backlog_max_concurrent"] = {
                **backlog_pressure,
                "current": current,
                "drain_floor": drain_floor,
                "skipped": "empty_seats_with_no_claimable",
            }
        elif current > drain_floor:
            next_max = max(drain_floor, current - max(1, MAX_BENCHMARK_DOWN_STEP))
            new_cfg = json.loads(json.dumps(cfg))
            new_cfg["max_concurrent_benchmarks"] = next_max
            decision["reason"] = "drain_root_backlog_max_concurrent"
            decision["changes"] = {
                "max_concurrent_benchmarks": {
                    "current": current,
                    "next": next_max,
                    "drain_floor": drain_floor,
                    **backlog_pressure,
                }
            }
            decision["config"] = new_cfg
            return decision
        elif not hole_blocks_drain:
            decision.setdefault("guardrails", {})["root_backlog_max_concurrent"] = {
                **backlog_pressure,
                "current": current,
                "drain_floor": drain_floor,
                "skipped": "already_at_or_below_drain_floor",
            }

    if health.get("unserved_stranded_benchmarks"):
        current = int(cfg.get("max_concurrent_benchmarks") or 0)
        active_jobs = _active_unfinished_jobs()
        stranded_count = len(health["unserved_stranded_benchmarks"])
        productive_jobs = max(0, active_jobs - stranded_count)
        gpu_slot_total, _ = _gpu_slot_counts(report)
        capacity_model = report.get("capacity_model") or {}
        gpu_job_reserve = 0
        if capacity_model and (
            int(capacity_model.get("active_gpu") or 0)
            or int(capacity_model.get("sizing_gpu_units") or 0)
        ):
            gpu_job_reserve = _gpu_job_target(capacity_model)
        live_gpu = int((health.get("live_by_profile") or {}).get("gpu") or 0)
        active_gpu_reserve = gpu_job_reserve or min(live_gpu, gpu_slot_total or live_gpu)
        gpu_reserve = min(gpu_slot_total, active_gpu_reserve) if gpu_slot_total else active_gpu_reserve
        target_slots = _target_resource_slots(capacity_model) if capacity_model else {}
        capacity_floor = _capacity_floor_max_concurrent(capacity_model, target_slots) if capacity_model else MIN_MAX_BENCHMARKS
        drain_target = _clamp(
            max(
                productive_jobs + STRANDED_BUFFER_BENCHMARKS + gpu_reserve,
                capacity_floor,
            ),
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
            "capacity_floor": capacity_floor,
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

    # Prefer feeding idle proven CPU over another GPU tail trim when root-ready
    # is healthy. Otherwise workload_safety_adjustment monopolizes every cycle
    # while max_concurrent stays pinned in recovery.
    max_rec_for_idle = recommendations.get("max_concurrent_benchmarks") or {}
    current_max_for_idle = int(cfg.get("max_concurrent_benchmarks") or 0)
    proposed_max_for_idle = int(max_rec_for_idle.get("proposed") or current_max_for_idle)
    active_jobs_for_idle = _active_unfinished_jobs()
    if oversub_upscale_allowed(
        active_jobs=active_jobs_for_idle,
        current_max=current_max_for_idle,
        proposed_max=max(proposed_max_for_idle, active_jobs_for_idle),
    ) and precommit_already_oversubscribed(
        active_jobs=active_jobs_for_idle, current_max=current_max_for_idle
    ):
        proposed_max_for_idle = max(proposed_max_for_idle, active_jobs_for_idle)
    allow_hole_raise, hole_raise_reason = should_raise_cap_for_seat_hole(
        idle_cpu=productive_idle_cpu,
        cpu_claimable=int(funnel_summary.get("cpu_unassigned_claimable") or 0),
        idle_gpu=productive_idle_gpu,
        gpu_claimable=int(funnel_summary.get("gpu_unassigned_claimable") or 0),
        current_max=current_max_for_idle,
        active_jobs=active_jobs_for_idle,
        has_unregistered=bool(health.get("active_unregistered")),
        unresolved_ceiling=UPSTREAM_SAFE_MAX_BENCHMARKS,
    )
    if allow_hole_raise:
        next_max = min(
            int(UPSTREAM_SAFE_MAX_BENCHMARKS),
            current_max_for_idle + max(1, IDLE_CPU_MAX_SCALE_UP_STEP),
        )
        if next_max > current_max_for_idle:
            new_cfg = json.loads(json.dumps(cfg))
            new_cfg["max_concurrent_benchmarks"] = next_max
            decision["reason"] = "seat_hole_needs_cap"
            decision["changes"] = {
                "max_concurrent_benchmarks": {
                    "current": current_max_for_idle,
                    "target": next_max,
                    "next": next_max,
                    "active_jobs": active_jobs_for_idle,
                    "productive_idle_cpu": productive_idle_cpu,
                    "productive_idle_gpu": productive_idle_gpu,
                    "gate_reason": hole_raise_reason,
                }
            }
            decision["config"] = new_cfg
            return decision
    root_ready_rate = funnel_summary.get("root_ready_rate")
    _roots_pending_total, cpu_roots_pending_for_idle, _gpu_roots_pending = (
        _profile_roots_pending(funnel_summary)
    )
    allow_idle_cpu_max, idle_cpu_reason = should_idle_cpu_max_scale(
        enabled=IDLE_CPU_MAX_SCALE_ENABLED,
        productive_idle_cpu=productive_idle_cpu,
        min_idle=IDLE_CPU_MAX_SCALE_MIN,
        slot_idle_cpu=slot_idle_cpu,
        min_slot_idle_cpu=IDLE_CPU_MAX_SCALE_MIN_SLOT_IDLE,
        proof_conversion_rate=funnel_summary.get("proof_conversion_rate"),
        soft_proof_conversion_floor=FUNNEL_SOFT_PROOF_CONVERSION_FLOOR,
        root_ready_rate=root_ready_rate,
        min_root_ready_rate=ROOT_READY_RATE_MIN_FOR_UPSCALE,
        # Hard-cap idle-CPU max_concurrent climbs on CPU root backlog only.
        roots_pending=cpu_roots_pending_for_idle,
        max_roots_pending=ROOT_PENDING_MAX_CONCURRENT_DRAIN,
        benchmarks_seen=int(funnel_summary.get("benchmarks_seen") or 0),
        current_max=current_max_for_idle,
        proposed_max=proposed_max_for_idle,
        active_jobs=active_jobs_for_idle,
        stale_proofs=stale_proofs,
        has_stranded=bool(health.get("unserved_stranded_benchmarks")),
        has_unregistered=bool(health.get("active_unregistered")),
    )
    if allow_idle_cpu_max:
        up_step = max(1, min(IDLE_CPU_MAX_SCALE_UP_STEP, SURGE_MAX_BENCHMARK_UP_STEP if posture == "recovery" else IDLE_CPU_MAX_SCALE_UP_STEP))
        if (proposed_max_for_idle - current_max_for_idle) >= SURGE_TARGET_GAP:
            up_step = min(up_step, SURGE_MAX_BENCHMARK_UP_STEP)
        next_max = _next_value_bounded(
            current_max_for_idle,
            proposed_max_for_idle,
            up_step,
            MAX_BENCHMARK_DOWN_STEP,
        )
        if next_max > current_max_for_idle:
            new_cfg = json.loads(json.dumps(cfg))
            new_cfg["max_concurrent_benchmarks"] = next_max
            decision["reason"] = "idle_cpu_max_concurrent_scale"
            decision["changes"] = {
                "max_concurrent_benchmarks": {
                    "current": current_max_for_idle,
                    "target": proposed_max_for_idle,
                    "next": next_max,
                    "up_step": up_step,
                    "active_jobs": active_jobs_for_idle,
                    "productive_idle_cpu": productive_idle_cpu,
                    "slot_idle_cpu": slot_idle_cpu,
                    "root_ready_rate": root_ready_rate,
                    "proof_conversion_rate": funnel_summary.get("proof_conversion_rate"),
                    "roots_pending": _roots_pending_total,
                    "cpu_roots_pending": cpu_roots_pending_for_idle,
                    "gpu_roots_pending": _gpu_roots_pending,
                    "policy_posture": posture,
                    "funnel_safe": funnel_safe,
                    "gate_reason": idle_cpu_reason,
                    "signals": max_rec_for_idle.get("signals") or {},
                }
            }
            decision["config"] = new_cfg
            return decision
    else:
        decision.setdefault("guardrails", {})["idle_cpu_max_concurrent_scale"] = {
            "skipped": idle_cpu_reason,
            "productive_idle_cpu": productive_idle_cpu,
            "min_idle": IDLE_CPU_MAX_SCALE_MIN,
            "slot_idle_cpu": slot_idle_cpu,
            "min_slot_idle_cpu": IDLE_CPU_MAX_SCALE_MIN_SLOT_IDLE,
            "cpu_roots_pending": cpu_roots_pending_for_idle,
            "gpu_roots_pending": _gpu_roots_pending,
            "current": current_max_for_idle,
            "proposed": proposed_max_for_idle,
            "active_jobs": active_jobs_for_idle,
            "root_ready_rate": root_ready_rate,
            "proof_conversion_rate": funnel_summary.get("proof_conversion_rate"),
        }

    allow_parked_ratchet, parked_ratchet_reason = should_ratchet_parked_cap_to_live(
        active_jobs=active_jobs_for_idle,
        current_max=current_max_for_idle,
        proposed_max=proposed_max_for_idle,
        has_unregistered=bool(health.get("active_unregistered")),
        unserved_stranded=health.get("unserved_stranded_benchmarks"),
    )
    if allow_parked_ratchet:
        up_step = max(1, min(IDLE_CPU_MAX_SCALE_UP_STEP, SURGE_MAX_BENCHMARK_UP_STEP if posture == "recovery" else IDLE_CPU_MAX_SCALE_UP_STEP))
        if (proposed_max_for_idle - current_max_for_idle) >= SURGE_TARGET_GAP:
            up_step = min(up_step, SURGE_MAX_BENCHMARK_UP_STEP)
        next_max = _next_value_bounded(
            current_max_for_idle,
            proposed_max_for_idle,
            up_step,
            MAX_BENCHMARK_DOWN_STEP,
        )
        if next_max > current_max_for_idle:
            new_cfg = json.loads(json.dumps(cfg))
            new_cfg["max_concurrent_benchmarks"] = next_max
            decision["reason"] = "ratchet_parked_cap_to_proven_live"
            decision["changes"] = {
                "max_concurrent_benchmarks": {
                    "current": current_max_for_idle,
                    "target": proposed_max_for_idle,
                    "next": next_max,
                    "up_step": up_step,
                    "active_jobs": active_jobs_for_idle,
                    "productive_idle_cpu": productive_idle_cpu,
                    "slot_idle_cpu": slot_idle_cpu,
                    "root_ready_rate": root_ready_rate,
                    "proof_conversion_rate": funnel_summary.get("proof_conversion_rate"),
                    "roots_pending": _roots_pending_total,
                    "cpu_roots_pending": cpu_roots_pending_for_idle,
                    "gpu_roots_pending": _gpu_roots_pending,
                    "policy_posture": posture,
                    "funnel_safe": funnel_safe,
                    "gate_reason": parked_ratchet_reason,
                    "health_blockers": health_block_reasons(health),
                    "signals": max_rec_for_idle.get("signals") or {},
                }
            }
            decision["config"] = new_cfg
            return decision
    else:
        decision.setdefault("guardrails", {})["parked_cap_ratchet"] = {
            "skipped": parked_ratchet_reason,
            "current": current_max_for_idle,
            "proposed": proposed_max_for_idle,
            "active_jobs": active_jobs_for_idle,
            "root_ready_rate": root_ready_rate,
        }

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

    if not health["healthy"] and not productive_capacity_scale and not safe_per_challenge_scale:
        decision["reason"] = "blocked_by_stale_or_unregistered_work"
        decision["health_blockers"] = health_block_reasons(health)
        return decision
    if clean_windows < APPLY_MIN_CLEAN_WINDOWS and not productive_capacity_scale and not safe_per_challenge_scale:
        decision["reason"] = "waiting_for_clean_windows"
        return decision

    new_cfg = json.loads(json.dumps(cfg))
    changes: dict[str, dict] = {}

    current_slots = ((new_cfg.get("resource_slots") or {}).get("slots") or {})
    if slots_rec and current_slots and capacity_change_allowed:
        proposed_slots = slots_rec.get("proposed") or {}
        next_slots = dict(current_slots)
        for key, target in proposed_slots.items():
            current = int(current_slots.get(key, 0) or 0)
            target = int(target or 0)
            # GPU slots may step down with live eligible GPU headcount (parity
            # with CPU slot sizing). Apply still uses SLOT_DOWN_STEP.
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
        capacity_model = report.get("capacity_model") or {}
        capacity_floor = _capacity_floor_max_concurrent(capacity_model, current_slots) if capacity_model else MIN_MAX_BENCHMARKS
        target = max(target, capacity_floor)
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
            and precommit_already_oversubscribed(
                active_jobs=active_jobs, current_max=current
            )
            and not oversub_upscale_allowed(
                active_jobs=active_jobs,
                current_max=current,
                proposed_max=target,
            )
        ):
            decision.setdefault("guardrails", {})["max_concurrent_benchmarks"] = {
                "skipped": "precommit_already_oversubscribed",
                "current": current,
                "target": target,
                "active_jobs": active_jobs,
                "signals": max_rec.get("signals") or {},
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
        elif target > current:
            upscale_backlog = _root_backlog_pressure(funnel_summary)
            if upscale_backlog:
                decision.setdefault("guardrails", {})["max_concurrent_benchmarks"] = {
                    "skipped": "upscale_blocked_by_root_backlog",
                    "current": current,
                    "target": target,
                    "active_jobs": active_jobs,
                    **upscale_backlog,
                    "signals": max_rec.get("signals") or {},
                }
            else:
                max_up_step = 1 if posture == "conservative" else MAX_BENCHMARK_UP_STEP
                surge_limited = False
                if (target - current) >= SURGE_TARGET_GAP:
                    surge_limited = max_up_step > SURGE_MAX_BENCHMARK_UP_STEP
                    max_up_step = min(max_up_step, SURGE_MAX_BENCHMARK_UP_STEP)
                next_max = _next_value_bounded(current, target, max_up_step, MAX_BENCHMARK_DOWN_STEP)
                if next_max != current:
                    new_cfg["max_concurrent_benchmarks"] = next_max
                    changes["max_concurrent_benchmarks"] = {
                        "current": current,
                        "target": target,
                        "next": next_max,
                        "active_jobs": active_jobs,
                        "capacity_floor": capacity_floor,
                        "gpu_capacity_needs_room": gpu_needs_room,
                        "productive_capacity_scale": productive_capacity_scale,
                        "policy_posture": posture,
                        "up_step": max_up_step,
                        "surge_limited": surge_limited,
                        "surge_target_gap": SURGE_TARGET_GAP,
                        "signals": max_rec.get("signals") or {},
                    }

    current_per = new_cfg.get("per_challenge_max_benchmarks") or {}
    if per_rec and current_per and (capacity_change_allowed or safe_per_challenge_scale):
        proposed_per = per_rec.get("proposed") or {}
        next_per = dict(current_per)
        per_changes = {}
        for key, proposed_value in proposed_per.items():
            current = int(current_per.get(key, 0) or 0)
            target = int(current if proposed_value is None else proposed_value)
            # Clamp to per-challenge env ceiling (may lower GPU caps that were
            # previously ratcheted above the new operator max).
            target = min(target, _max_challenge_benchmarks(key))
            if target != current:
                next_value = _next_value_bounded(current, target, 1, 1)
                if next_value != current:
                    next_per[key] = next_value
                    per_changes[key] = {
                        "current": current,
                        "target": target,
                        "next": next_value,
                    }
        if per_changes:
            new_cfg["per_challenge_max_benchmarks"] = next_per
            changes["per_challenge_max_benchmarks"] = per_changes

    # Always enforce per-challenge env ceilings, even when upscales are gated.
    current_per_ceiling = new_cfg.get("per_challenge_max_benchmarks") or {}
    if current_per_ceiling:
        next_per_ceiling = dict(current_per_ceiling)
        ceiling_changes = {}
        for key, value in current_per_ceiling.items():
            current = int(value or 0)
            if current <= 0:
                continue
            ceiling = _max_challenge_benchmarks(str(key))
            if current > ceiling:
                next_value = _next_value_bounded(current, ceiling, 1, 1)
                next_per_ceiling[key] = next_value
                ceiling_changes[key] = {
                    "current": current,
                    "target": ceiling,
                    "next": next_value,
                    "reason": "per_challenge_env_ceiling",
                }
        if ceiling_changes:
            new_cfg["per_challenge_max_benchmarks"] = next_per_ceiling
            changes["per_challenge_max_benchmarks"] = {
                **(changes.get("per_challenge_max_benchmarks") or {}),
                **ceiling_changes,
            }

    # Always enforce env slave-cap ceilings, even when capacity upscales are gated.
    current_caps = new_cfg.get("adaptive_slave_caps") or {}
    if current_caps:
        next_caps = dict(current_caps)
        clamp_changes = {}
        for key, clamp_fn in (
            ("cpu_max_cap", _clamp_cpu_slave_cap),
            ("gpu_max_cap", _clamp_gpu_slave_cap),
        ):
            current = int(current_caps.get(key, 0) or 0)
            if current <= 0:
                continue
            clamped = clamp_fn(current)
            if clamped != current:
                next_caps[key] = clamped
                clamp_changes[key] = {
                    "current": current,
                    "target": clamped,
                    "next": clamped,
                    "reason": "env_slave_cap_ceiling",
                }
        if clamp_changes:
            new_cfg["adaptive_slave_caps"] = next_caps
            changes["adaptive_slave_caps"] = {
                "changes": clamp_changes,
                "signals": {"enforced": "AUTOPILOT_MAX_*_SLAVE_CAP"},
            }
            current_caps = next_caps

    caps_rec = recommendations.get("adaptive_slave_caps")
    current_caps = new_cfg.get("adaptive_slave_caps") or current_caps
    if caps_rec and current_caps and capacity_change_allowed:
        proposed_caps = caps_rec.get("proposed") or {}
        next_caps = dict(current_caps)
        cap_changes = {}
        for key, clamp_fn in (
            ("cpu_max_cap", _clamp_cpu_slave_cap),
            ("gpu_max_cap", _clamp_gpu_slave_cap),
        ):
            current = int(current_caps.get(key, 0) or 0)
            target = clamp_fn(int(proposed_caps.get(key, current) or current))
            if target == current:
                continue
            if target > current:
                next_value = clamp_fn(current + 1)
            else:
                next_value = target
            if next_value == current:
                continue
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

    # Env ceiling clamp-downs for routes should not wait on clean-window upscale gates.
    if route_rec:
        proposed_routes = route_rec.get("proposed") or []
        clamp_only = {
            **route_rec,
            "proposed": [
                row for row in proposed_routes
                if int(row.get("next") or row.get("target") or 0)
                < int(row.get("current") or 0)
            ],
        }
        if clamp_only["proposed"]:
            route_change = _apply_route_cap_targets(new_cfg, clamp_only)
            if route_change:
                changes["slaves.max_concurrent_batches"] = route_change
    if route_rec and route_cap_change_allowed:
        route_change = _apply_route_cap_targets(new_cfg, route_rec)
        if route_change:
            changes["slaves.max_concurrent_batches"] = route_change
    elif route_rec:
        decision.setdefault("guardrails", {})["slaves.max_concurrent_batches"] = {
            "skipped": "route_cap_requires_healthy_funnel_and_clean_windows",
            "healthy": health["healthy"],
            "reward_funnel_safe": funnel_safe,
            "policy_posture": posture,
            "clean_windows": clean_windows,
            "required_clean_windows": APPLY_MIN_CLEAN_WINDOWS,
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
        _target_max_concurrent_benchmarks(capacity, target_slots, reward_funnel)
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
        "active_gpu_units": capacity.get("active_gpu_units"),
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
            "unexpected_stopped_rate": funnel_summary.get("unexpected_stopped_rate"),
            "unexpected_stopped_without_roots_rate": funnel_summary.get("unexpected_stopped_without_roots_rate"),
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
    report = build_report(force=True)
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
        or cleanup.get("stopped_old_benchmarks")
    ):
        decision.setdefault("changes", {})["stale_cleanup"] = {
            "released_roots": cleanup.get("released_roots", []),
            "released_orphan_roots": cleanup.get("released_orphan_roots", []),
            "released_proofs": cleanup.get("released_proofs", []),
            "expiry_released_roots": cleanup.get("expiry_released_roots", []),
            "expiry_released_proofs": cleanup.get("expiry_released_proofs", []),
            "stopped_precommits": cleanup.get("stopped_precommits", []),
            "stopped_old_benchmarks": cleanup.get("stopped_old_benchmarks", []),
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
        "autopilot mode=%s healthy=%s clean_windows=%s applied=%s reason=%s changes=%s blockers=%s stale_roots=%s stale_proofs=%s unregistered=%s stranded=%s",
        AUTOPILOT_MODE,
        decision.get("healthy"),
        clean_windows,
        decision.get("applied"),
        decision.get("reason"),
        list((decision.get("changes") or {}).keys()),
        (decision.get("health_blockers") or health_block_reasons(decision.get("health") or {})).get("reasons"),
        (decision.get("health") or {}).get("stale_roots"),
        (decision.get("health") or {}).get("stale_proofs"),
        (decision.get("health") or {}).get("active_unregistered"),
        len((decision.get("health") or {}).get("unserved_stranded_benchmarks") or []),
    )
    return decision


_REPORT_CACHE_MS = int(os.environ.get("AUTOPILOT_REPORT_CACHE_MS", "8000"))
_report_lock = threading.Lock()
_report_cache: dict[str, Any] = {"ts": 0, "report": None}


def _degraded_report(now_ms: int) -> dict:
    return {
        "generated_at_ms": now_ms,
        "scale_readiness": {"gate": "unknown", "posture": "degraded"},
        "stale_totals": {"roots": 0, "proofs": 0},
        "active_slave_counts": {"cpu": 0, "gpu": 0},
        "current_config": {},
        "reward_funnel": {"summary": {}},
        "slaves": [],
        "challenges": [],
        "degraded": True,
    }


def build_report(*, force: bool = False) -> dict:
    now_ms = int(time.time() * 1000)
    cached = _report_cache.get("report")
    ts = int(_report_cache.get("ts") or 0)
    if not force and cached is not None and now_ms - ts < _REPORT_CACHE_MS:
        return cached
    if not _report_lock.acquire(blocking=False):
        if cached is not None:
            return cached
        return _degraded_report(now_ms)
    try:
        now_ms = int(time.time() * 1000)
        if not force:
            cached = _report_cache.get("report")
            ts = int(_report_cache.get("ts") or 0)
            if cached is not None and now_ms - ts < _REPORT_CACHE_MS:
                return cached
        report = _build_report_uncached()
        _report_cache["report"] = report
        _report_cache["ts"] = int(time.time() * 1000)
        return report
    finally:
        _report_lock.release()


def _build_report_uncached() -> dict:
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
            "aws_batch_capacity": capacity.get("aws_batch_capacity") if capacity else {},
            "gpu_slot_floor": capacity.get("gpu_slot_floor") if capacity else {},
            "resource_slots": target_slots,
            "max_concurrent_benchmarks": (
                _target_max_concurrent_benchmarks(capacity, target_slots, reward_funnel)
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
            "slave_route_caps": (
                _target_slave_route_caps(cfg, capacity)
                if capacity
                else []
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
