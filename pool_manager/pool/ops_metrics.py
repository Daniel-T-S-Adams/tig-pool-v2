"""
Private ops metrics for the operator dashboard.

Observe-only: does not change create/assign behavior. Requires admin secret
via the /admin/ops/metrics route.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from decimal import Decimal

from pool import autopilot, database as db
from pool.idle_tracker import (
    CPU_IDLE_TRACKER,
    FLEET_IDLE_TRACKER,
    idle_window_settings,
)

logger = logging.getLogger("pool.ops_metrics")

SLAVE_ONLINE_MS = int(os.environ.get("SLAVE_ONLINE_MS", "120000"))
CPU_CHALLENGE_IDS = ("c001", "c002", "c003", "c007", "c008")
GPU_CHALLENGE_IDS = ("c004", "c005", "c006")

# Soft governor defaults (mirror master/precommit_manager.py env fallbacks).
_GOV_DEFAULTS = {
    "enabled": True,
    "max_cpu_unassigned_roots": 512,
    "cpu_unassigned_per_online": 8,
    "max_cpu_unassigned_roots_ceiling": 768,
    "max_gpu_unassigned_roots": 144,
    "gpu_unassigned_per_online": 8,
    "max_gpu_unassigned_roots_ceiling": 768,
    "min_cpu_roots_pending": 128,
    "max_cpu_roots_pending": 1024,
    "min_gpu_roots_pending": 64,
    "max_gpu_roots_pending": 512,
    "cpu_roots_per_job_budget": 24,
    "gpu_roots_per_job_budget": 48,
    "min_root_ready_rate": 0.50,
    "min_samples": 5,
    "window_ms": 30 * 60 * 1000,
    "idle_cpu_override": True,
}


def _json_safe(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _clamp_int(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


def compute_cpu_unassigned_cap(
    settings: dict | None,
    online_cpu: int = 0,
) -> int:
    """Same live CPU unassigned ceiling as master/precommit_manager.py."""
    settings = settings or {}
    configured = max(1, int(settings.get("max_cpu_unassigned_roots") or 512))
    per = max(1, int(settings.get("cpu_unassigned_per_online") or 8))
    ceiling = max(configured, int(settings.get("max_cpu_unassigned_roots_ceiling") or 768))
    adaptive = max(configured, int(online_cpu or 0) * per)
    return min(ceiling, adaptive)


def compute_gpu_unassigned_cap(
    settings: dict | None,
    online_gpu: int = 0,
) -> int:
    """Same live GPU unassigned ceiling as master/precommit_manager.py."""
    settings = settings or {}
    configured = max(1, int(settings.get("max_gpu_unassigned_roots") or 144))
    per = max(1, int(settings.get("gpu_unassigned_per_online") or 8))
    ceiling = max(configured, int(settings.get("max_gpu_unassigned_roots_ceiling") or 768))
    adaptive = max(configured, int(online_gpu or 0) * per)
    return min(ceiling, adaptive)


def _gov_settings(cfg: dict) -> dict:
    gov = (cfg or {}).get("precommit_governor") or {}
    out = dict(_GOV_DEFAULTS)
    for key in out:
        if key in gov:
            out[key] = gov[key]
    # Env overrides (same names as master) when present on pool_manager.
    env_map = {
        "enabled": ("PRECOMMIT_GOVERNOR_ENABLED", lambda v: str(v).lower() in ("1", "true", "yes", "on")),
        "max_cpu_unassigned_roots": ("PRECOMMIT_GOVERNOR_MAX_CPU_UNASSIGNED_ROOTS", int),
        "cpu_unassigned_per_online": ("PRECOMMIT_GOVERNOR_CPU_UNASSIGNED_PER_ONLINE", int),
        "max_cpu_unassigned_roots_ceiling": (
            "PRECOMMIT_GOVERNOR_MAX_CPU_UNASSIGNED_ROOTS_CEILING",
            int,
        ),
        "max_gpu_unassigned_roots": ("PRECOMMIT_GOVERNOR_MAX_GPU_UNASSIGNED_ROOTS", int),
        "gpu_unassigned_per_online": ("PRECOMMIT_GOVERNOR_GPU_UNASSIGNED_PER_ONLINE", int),
        "max_gpu_unassigned_roots_ceiling": (
            "PRECOMMIT_GOVERNOR_MAX_GPU_UNASSIGNED_ROOTS_CEILING",
            int,
        ),
        "min_cpu_roots_pending": ("PRECOMMIT_GOVERNOR_MIN_CPU_ROOTS_PENDING", int),
        "max_cpu_roots_pending": ("PRECOMMIT_GOVERNOR_MAX_CPU_ROOTS_PENDING", int),
        "min_gpu_roots_pending": ("PRECOMMIT_GOVERNOR_MIN_GPU_ROOTS_PENDING", int),
        "max_gpu_roots_pending": ("PRECOMMIT_GOVERNOR_MAX_GPU_ROOTS_PENDING", int),
        "cpu_roots_per_job_budget": ("PRECOMMIT_GOVERNOR_CPU_ROOTS_PER_JOB", int),
        "gpu_roots_per_job_budget": ("PRECOMMIT_GOVERNOR_GPU_ROOTS_PER_JOB", int),
        "min_root_ready_rate": ("PRECOMMIT_GOVERNOR_MIN_ROOT_READY_RATE", float),
        "min_samples": ("PRECOMMIT_GOVERNOR_MIN_SAMPLES", int),
        "window_ms": ("PRECOMMIT_GOVERNOR_WINDOW_MS", int),
        "idle_cpu_override": ("PRECOMMIT_GOVERNOR_IDLE_CPU_OVERRIDE", lambda v: str(v).lower() in ("1", "true", "yes", "on")),
    }
    for key, (env_name, cast) in env_map.items():
        raw = os.environ.get(env_name)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            out[key] = cast(raw)
        except Exception:
            pass
    return out


def slave_telem_is_working(telem_state: str | None, telem_active) -> bool:
    """True when the slave's last get-batches heartbeat says it still has work.

    Dashboard idle used only DB inflight, so a box still running/submitting
    locally looked idle and 'no batches available' looked like a warehouse.
    """
    try:
        if int(telem_active or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return str(telem_state or "").strip().lower() in (
        "running",
        "downloading",
        "submitting",
    )


_slave_telem_columns_ready = False


def _ensure_slave_telem_columns() -> None:
    global _slave_telem_columns_ready
    if _slave_telem_columns_ready:
        return
    try:
        db.execute_many(
            ("ALTER TABLE slave_seen ADD COLUMN IF NOT EXISTS telem_state TEXT", None),
            ("ALTER TABLE slave_seen ADD COLUMN IF NOT EXISTS telem_active INTEGER", None),
            lock_timeout="2s",
        )
        _slave_telem_columns_ready = True
    except Exception as exc:
        logger.debug("slave_seen telem columns: %s", exc)


def slave_display_profile(slave_name: str, worker_type: str | None = None) -> str:
    """CPU vs GPU for ops tiles. Name prefix only, plus an explicit member type.

    ``c3-slave-*`` is leftover AWS/C3 CPU naming, not a GPU. Real GPUs are
    ``pool-gpu-*``. Multi-GPU dispatchers are ``pool-gpu-*-c3-*``.
    """
    explicit = str(worker_type or "").strip().lower()
    if explicit in ("cpu", "gpu"):
        return explicit
    name = str(slave_name or "")
    if name.startswith("pool-gpu-"):
        return "gpu"
    return "cpu"


def machine_fill_rate(busy: int, online: int):
    """Share of online machines that have work. Not inflight / route caps."""
    online_n = max(0, int(online or 0))
    if online_n <= 0:
        return None
    busy_n = min(online_n, max(0, int(busy or 0)))
    return round(busy_n / online_n, 3)


def slave_display_cap(
    *,
    profile: str,
    num_workers: int = 0,
    route_cap: int = 0,
    is_multi_gpu: bool = False,
    adaptive_max: int = 0,
) -> int:
    """Fill-rate denominator: jobs a box can run, not the warehouse route cap.

    CPU stays one job per box. GPU uses reported workers when present,
    otherwise that slave's allowed batch concurrency.
    """
    if str(profile or "") != "gpu":
        return 1
    reported = max(0, int(num_workers or 0))
    allowed = max(0, int(route_cap or 0))
    family_max = max(0, int(adaptive_max or 0))
    if family_max > 0 and allowed > 0:
        allowed = min(allowed, family_max)
    if is_multi_gpu:
        return max(1, reported, allowed)
    if reported > 0:
        return reported
    return max(1, allowed)


def _route_cap_for(slave_name: str, slaves_cfg: list) -> int:
    matched = None
    for route in slaves_cfg or []:
        regex = route.get("name_regex") or route.get("name") or ""
        try:
            if regex and re.search(regex, slave_name):
                matched = route
        except re.error:
            continue
    if not matched:
        return 1
    return max(0, int(matched.get("max_concurrent_batches") or 0))


def _effective_cap(slave_name: str, route_cap: int, adaptive: dict, profile: str) -> int:
    """Best-effort effective cap: min(route_cap, adaptive family max)."""
    if route_cap <= 0:
        return 0
    if profile == "gpu":
        family_max = int(adaptive.get("gpu_max_cap", adaptive.get("max_cap", route_cap)) or route_cap)
    else:
        family_max = int(adaptive.get("cpu_max_cap", adaptive.get("max_cap", route_cap)) or route_cap)
    # Per-name override if present.
    per_slave = adaptive.get("per_slave") or adaptive.get("slaves") or {}
    if isinstance(per_slave, dict) and slave_name in per_slave:
        try:
            family_max = int(per_slave[slave_name])
        except Exception:
            pass
    return max(0, min(route_cap, family_max)) if family_max > 0 else route_cap


def _cpu_create_target(cfg: dict) -> int:
    slots = ((cfg.get("resource_slots") or {}).get("slots") or {})
    cpu_slots = int(slots.get("cpu") or 0)
    aws = cfg.get("aws_batch_capacity") or {}
    if aws.get("enabled"):
        cpu_slots = max(
            cpu_slots,
            int(aws.get("max_concurrent_cpu_jobs") or 0),
            int(aws.get("cpu_instances") or 0),
        )
    max_concurrent = int(cfg.get("max_concurrent_benchmarks") or 0)
    floor = cfg.get("gpu_slot_floor") or {}
    gpu_floor = sum(int(floor.get(k) or 0) for k in ("hypergraph", "vector_search", "neuralnet_optimizer"))
    if max_concurrent > 0:
        cpu_fair = max(1, max_concurrent - max(1, gpu_floor))
        return max(1, min(max(0, cpu_slots), cpu_fair))
    return max(1, cpu_slots)


def _gpu_slots_total(cfg: dict) -> int:
    slots = ((cfg.get("resource_slots") or {}).get("slots") or {})
    total = sum(int(slots.get(k) or 0) for k in ("hypergraph", "vector_search", "neuralnet_optimizer"))
    floor = cfg.get("gpu_slot_floor") or {}
    floor_total = sum(int(floor.get(k) or 0) for k in ("hypergraph", "vector_search", "neuralnet_optimizer"))
    return max(total, floor_total)


def _governor_view(
    cfg: dict,
    now_ms: int,
    online_idle_cpu_slaves: int = 0,
    decision_idle_cpu_slaves: int | None = None,
    online_cpu_slaves: int = 0,
    online_gpu_slaves: int = 0,
) -> dict:
    settings = _gov_settings(cfg)
    if not settings.get("enabled", True):
        return {
            "enabled": False,
            "would_block_global": False,
            "global_reason": "",
            "profile_blocks": {"cpu": False, "gpu": False, "cpu_reasons": [], "gpu_reasons": []},
            "counts": {},
        }

    cutoff_ms = now_ms - int(settings.get("window_ms") or (30 * 60 * 1000))
    online_cutoff = now_ms - SLAVE_ONLINE_MS
    row = db.fetch_one(
        """
        SELECT
            (
                SELECT COUNT(*)
                FROM root_batch rb
                JOIN job j ON j.benchmark_id = rb.benchmark_id
                WHERE rb.ready IS NULL
                  AND COALESCE(j.stopped, false) = false
                  AND j.end_time IS NULL
                  AND j.merkle_root_ready IS NULL
            ) AS roots_pending,
            (
                SELECT COUNT(*)
                FROM root_batch rb
                JOIN job j ON j.benchmark_id = rb.benchmark_id
                WHERE rb.ready IS NULL
                  AND COALESCE(j.stopped, false) = false
                  AND j.end_time IS NULL
                  AND j.merkle_root_ready IS NULL
                  AND j.settings->>'challenge_id' IN %s
            ) AS cpu_roots_pending,
            (
                SELECT COUNT(*)
                FROM root_batch rb
                JOIN job j ON j.benchmark_id = rb.benchmark_id
                WHERE rb.ready IS NULL
                  AND COALESCE(j.stopped, false) = false
                  AND j.end_time IS NULL
                  AND j.merkle_root_ready IS NULL
                  AND j.settings->>'challenge_id' IN %s
            ) AS gpu_roots_pending,
            (
                SELECT COUNT(*)
                FROM job j
                WHERE j.start_time >= %s
                   OR j.benchmark_submit_time >= %s
                   OR j.proof_submit_time >= %s
                   OR j.end_time >= %s
                   OR j.end_time IS NULL
            ) AS benchmarks_seen,
            (
                SELECT COUNT(*)
                FROM job j
                WHERE (
                    j.start_time >= %s
                    OR j.benchmark_submit_time >= %s
                    OR j.proof_submit_time >= %s
                    OR j.end_time >= %s
                    OR j.end_time IS NULL
                )
                  AND j.merkle_root_ready = true
            ) AS root_ready_benchmarks,
            (
                SELECT COUNT(*)
                FROM job j
                WHERE COALESCE(j.stopped, false) = false
                  AND j.end_time IS NULL
                  AND j.merkle_root_ready IS NULL
                  AND j.settings->>'challenge_id' IN %s
            ) AS cpu_jobs_needing_roots,
            (
                SELECT COUNT(*)
                FROM root_batch rb
                JOIN job j ON j.benchmark_id = rb.benchmark_id
                WHERE rb.ready IS NULL
                  AND rb.slave IS NULL
                  AND COALESCE(j.stopped, false) = false
                  AND j.end_time IS NULL
                  AND j.merkle_root_ready IS NULL
                  AND j.settings->>'challenge_id' IN %s
            ) AS cpu_unassigned_roots,
            (
                SELECT COUNT(*)
                FROM root_batch rb
                JOIN job j ON j.benchmark_id = rb.benchmark_id
                WHERE rb.ready IS NULL
                  AND rb.slave IS NULL
                  AND COALESCE(j.stopped, false) = false
                  AND j.end_time IS NULL
                  AND j.merkle_root_ready IS NULL
                  AND j.settings->>'challenge_id' IN %s
            ) AS gpu_unassigned_roots,
            (
                SELECT COUNT(*)
                FROM root_batch rb
                JOIN job j ON j.benchmark_id = rb.benchmark_id
                WHERE rb.ready IS NULL
                  AND rb.slave IS NULL
                  AND COALESCE(j.stopped, false) = false
                  AND j.end_time IS NULL
                  AND j.merkle_root_ready IS NULL
                  AND j.settings->>'challenge_id' IN %s
                  AND NOT EXISTS (
                    SELECT 1
                    FROM root_batch rb2
                    JOIN slave_seen ss ON ss.slave_name = rb2.slave
                    WHERE rb2.benchmark_id = rb.benchmark_id
                      AND rb2.slave IS NOT NULL
                      AND (rb2.ready = true OR rb2.ready IS NULL)
                      AND ss.last_seen >= %s
                  )
            ) AS cpu_unassigned_claimable,
            (
                SELECT COUNT(*)
                FROM root_batch rb
                JOIN job j ON j.benchmark_id = rb.benchmark_id
                WHERE rb.ready IS NULL
                  AND rb.slave IS NULL
                  AND COALESCE(j.stopped, false) = false
                  AND j.end_time IS NULL
                  AND j.merkle_root_ready IS NULL
                  AND j.settings->>'challenge_id' IN %s
                  AND NOT EXISTS (
                    SELECT 1
                    FROM root_batch rb2
                    JOIN slave_seen ss ON ss.slave_name = rb2.slave
                    WHERE rb2.benchmark_id = rb.benchmark_id
                      AND rb2.slave IS NOT NULL
                      AND (rb2.ready = true OR rb2.ready IS NULL)
                      AND ss.last_seen >= %s
                  )
            ) AS gpu_unassigned_claimable
        """,
        (
            CPU_CHALLENGE_IDS,
            GPU_CHALLENGE_IDS,
            cutoff_ms,
            cutoff_ms,
            cutoff_ms,
            cutoff_ms,
            cutoff_ms,
            cutoff_ms,
            cutoff_ms,
            cutoff_ms,
            CPU_CHALLENGE_IDS,
            CPU_CHALLENGE_IDS,
            GPU_CHALLENGE_IDS,
            CPU_CHALLENGE_IDS,
            online_cutoff,
            GPU_CHALLENGE_IDS,
            online_cutoff,
        ),
    ) or {}

    cpu_create_target = _cpu_create_target(cfg)
    gpu_slots = max(1, _gpu_slots_total(cfg))
    cpu_pending_cap = _clamp_int(
        cpu_create_target * max(1, int(settings["cpu_roots_per_job_budget"])),
        int(settings["min_cpu_roots_pending"]),
        int(settings["max_cpu_roots_pending"]),
    )
    gpu_pending_cap = _clamp_int(
        gpu_slots * max(1, int(settings["gpu_roots_per_job_budget"])),
        int(settings["min_gpu_roots_pending"]),
        int(settings["max_gpu_roots_pending"]),
    )
    caps = {
        "cpu_pending_cap": cpu_pending_cap,
        "gpu_pending_cap": gpu_pending_cap,
        "cpu_unassigned_cap": compute_cpu_unassigned_cap(settings, online_cpu_slaves),
        "gpu_unassigned_cap": compute_gpu_unassigned_cap(settings, online_gpu_slaves),
    }

    cpu_roots_pending = int(row.get("cpu_roots_pending") or 0)
    gpu_roots_pending = int(row.get("gpu_roots_pending") or 0)
    cpu_claimable = int(row.get("cpu_unassigned_claimable") or 0)
    gpu_claimable = int(row.get("gpu_unassigned_claimable") or 0)
    cpu_jobs_needing_roots = int(row.get("cpu_jobs_needing_roots") or 0)

    cpu_reasons = []
    gpu_reasons = []
    if cpu_claimable >= caps["cpu_unassigned_cap"]:
        cpu_reasons.append(
            f"cpu claimable_unassigned {cpu_claimable} >= {caps['cpu_unassigned_cap']}"
        )
    if cpu_roots_pending >= caps["cpu_pending_cap"]:
        cpu_reasons.append(
            f"cpu root backlog {cpu_roots_pending} >= adaptive cap {caps['cpu_pending_cap']}"
        )
    if gpu_claimable >= caps["gpu_unassigned_cap"]:
        gpu_reasons.append(
            f"gpu claimable_unassigned {gpu_claimable} >= {caps['gpu_unassigned_cap']}"
        )
    if gpu_roots_pending >= caps["gpu_pending_cap"]:
        gpu_reasons.append(
            f"gpu root backlog {gpu_roots_pending} >= adaptive cap {caps['gpu_pending_cap']}"
        )

    roots_pending = int(row.get("roots_pending") or 0)
    benchmarks_seen = int(row.get("benchmarks_seen") or 0)
    root_ready = int(row.get("root_ready_benchmarks") or 0)
    online_idle_cpu_instant = max(0, int(online_idle_cpu_slaves or 0))
    # Prefer sustained/windowed idle when the caller supplies it (master mirror).
    online_idle_cpu = max(
        0,
        int(
            decision_idle_cpu_slaves
            if decision_idle_cpu_slaves is not None
            else online_idle_cpu_instant
        ),
    )
    # Mirror master/precommit_manager.compute_idle_cpu_needs_work: claimable
    # only suppresses idle bias when it can absorb the idle CPU fleet.
    if not bool(settings.get("idle_cpu_override", True)):
        idle_cpu_needs_work = False
    elif cpu_create_target <= 0 or bool(cpu_reasons):
        idle_cpu_needs_work = False
    elif cpu_jobs_needing_roots >= cpu_create_target:
        idle_cpu_needs_work = False
    elif online_idle_cpu <= 0:
        idle_cpu_needs_work = cpu_claimable == 0
    else:
        idle_cpu_needs_work = cpu_claimable < online_idle_cpu

    global_reason = ""
    would_block_global = False
    min_rate = float(settings["min_root_ready_rate"])
    min_samples = int(settings["min_samples"])
    if roots_pending > 0 and benchmarks_seen >= min_samples:
        rate = root_ready / max(1, benchmarks_seen)
        if rate < min_rate:
            if idle_cpu_needs_work:
                global_reason = (
                    f"idle_cpu_override: root_ready_rate {rate:.3f} < {min_rate:.3f}"
                )
            else:
                would_block_global = True
                global_reason = (
                    f"root_ready_rate {rate:.3f} < {min_rate:.3f} "
                    f"with roots_pending={roots_pending}"
                )

    max_concurrent = int(cfg.get("max_concurrent_benchmarks") or 0)
    open_jobs = db.fetch_one(
        """
        SELECT COUNT(*) AS n
        FROM job
        WHERE merkle_proofs_ready IS NULL
          AND COALESCE(stopped, false) = false
        """
    )
    open_n = int((open_jobs or {}).get("n") or 0)
    at_max_concurrent = max_concurrent > 0 and open_n >= max_concurrent

    block_reasons = []
    if at_max_concurrent:
        block_reasons.append(f"max_concurrent_benchmarks saturated ({open_n}/{max_concurrent})")
    if would_block_global and global_reason:
        block_reasons.append(global_reason)
    block_reasons.extend(cpu_reasons)
    block_reasons.extend(gpu_reasons)

    return {
        "enabled": True,
        "would_block_global": would_block_global,
        "at_max_concurrent": at_max_concurrent,
        "global_reason": global_reason,
        "idle_cpu_needs_work": idle_cpu_needs_work,
        "block_reasons": block_reasons,
        "profile_blocks": {
            "cpu": bool(cpu_reasons),
            "gpu": bool(gpu_reasons),
            "cpu_reasons": cpu_reasons,
            "gpu_reasons": gpu_reasons,
        },
        "caps": caps,
        "counts": {
            "roots_pending": roots_pending,
            "cpu_roots_pending": cpu_roots_pending,
            "gpu_roots_pending": gpu_roots_pending,
            "cpu_unassigned_roots": int(row.get("cpu_unassigned_roots") or 0),
            "gpu_unassigned_roots": int(row.get("gpu_unassigned_roots") or 0),
            "cpu_unassigned_claimable": cpu_claimable,
            "gpu_unassigned_claimable": gpu_claimable,
            "online_idle_cpu_slaves": online_idle_cpu_instant,
            "online_idle_cpu_slaves_instant": online_idle_cpu_instant,
            "sustained_idle_cpu_slaves": online_idle_cpu,
            "benchmarks_seen_window": benchmarks_seen,
            "root_ready_benchmarks_window": root_ready,
            "cpu_jobs_needing_roots": cpu_jobs_needing_roots,
            "open_jobs": open_n,
            "max_concurrent_benchmarks": max_concurrent,
            "cpu_create_target": cpu_create_target,
            "gpu_slots_total": gpu_slots,
        },
    }


def _slave_rows(now_ms: int, cfg: dict) -> list[dict]:
    online_cutoff = now_ms - SLAVE_ONLINE_MS
    _ensure_slave_telem_columns()
    rows = db.fetch_all(
        """
        WITH online AS (
            SELECT slave_name, last_seen, num_workers, telem_state, telem_active
            FROM slave_seen
            WHERE last_seen >= %s
        ),
        inflight AS (
            SELECT slave, COUNT(*) AS root_inflight
            FROM root_batch
            WHERE ready IS NULL AND start_time IS NOT NULL AND slave IS NOT NULL
            GROUP BY slave
        ),
        proof_inflight AS (
            SELECT slave, COUNT(*) AS proof_inflight
            FROM proofs_batch
            WHERE ready IS NULL AND start_time IS NOT NULL AND slave IS NOT NULL
            GROUP BY slave
        ),
        names AS (
            SELECT slave_name FROM online
            UNION
            SELECT slave FROM inflight
            UNION
            SELECT slave FROM proof_inflight
        )
        SELECT
            n.slave_name,
            m.wallet_address,
            m.worker_type,
            COALESCE(m.active, false) AS registered_active,
            o.last_seen,
            o.num_workers,
            o.telem_state,
            o.telem_active,
            COALESCE(i.root_inflight, 0) AS root_inflight,
            COALESCE(p.proof_inflight, 0) AS proof_inflight,
            (o.slave_name IS NOT NULL) AS online
        FROM names n
        LEFT JOIN online o ON o.slave_name = n.slave_name
        LEFT JOIN inflight i ON i.slave = n.slave_name
        LEFT JOIN proof_inflight p ON p.slave = n.slave_name
        LEFT JOIN pool_members m ON m.slave_name = n.slave_name
        """,
        (online_cutoff,),
    )
    slaves_cfg = cfg.get("slaves") or []
    adaptive = cfg.get("adaptive_slave_caps") or {}
    gpu_family_max = int(
        adaptive.get("gpu_max_cap", adaptive.get("max_cap", 0)) or 0
    )
    out = []
    for row in rows:
        name = row["slave_name"]
        profile = slave_display_profile(name, row.get("worker_type"))
        route_cap = _route_cap_for(name, slaves_cfg)
        num_workers = int(row.get("num_workers") or 0)
        cap = slave_display_cap(
            profile=profile,
            num_workers=num_workers,
            route_cap=route_cap,
            is_multi_gpu=autopilot._is_c3_slave(name) and profile == "gpu",
            adaptive_max=gpu_family_max if profile == "gpu" else 0,
        )
        root_inflight = int(row.get("root_inflight") or 0)
        proof_inflight = int(row.get("proof_inflight") or 0)
        inflight = root_inflight + proof_inflight
        telem_working = slave_telem_is_working(
            row.get("telem_state"), row.get("telem_active")
        )
        online = bool(row.get("online"))
        busy = online and (inflight > 0 or telem_working)
        idle = online and not busy
        out.append({
            "slave_name": name,
            "profile": profile,
            "online": online,
            "busy": busy,
            "idle": idle,
            "root_inflight": root_inflight,
            "proof_inflight": proof_inflight,
            "inflight": inflight,
            "route_cap": route_cap,
            "cap": cap,
            "num_workers": num_workers,
            "worker_type": row.get("worker_type"),
            "fill": (1.0 if busy else 0.0) if online else None,
            "last_seen": row.get("last_seen"),
            "telem_state": row.get("telem_state"),
            "telem_active": row.get("telem_active"),
            "registered_active": bool(row.get("registered_active")),
        })
    out.sort(key=lambda s: (-int(s["online"]), -int(s["inflight"]), s["slave_name"] or ""))
    return out


def build_ops_metrics() -> dict:
    now_ms = int(time.time() * 1000)
    cfg, cfg_error = autopilot._fetch_master_config()

    slaves = _slave_rows(now_ms, cfg) if cfg else []
    online = [s for s in slaves if s["online"]]
    busy = [s for s in online if s["busy"]]
    idle = [s for s in online if s["idle"]]

    win = idle_window_settings()
    online_names = [s["slave_name"] for s in online]
    idle_names = [s["slave_name"] for s in idle]
    cpu_online_names = [s["slave_name"] for s in online if s.get("profile") == "cpu"]
    cpu_idle_names = [s["slave_name"] for s in idle if s.get("profile") == "cpu"]
    if win.get("enabled", True):
        FLEET_IDLE_TRACKER.update(
            now_ms, online_names, idle_names, window_ms=int(win.get("window_ms") or 120_000)
        )
        CPU_IDLE_TRACKER.update(
            now_ms,
            cpu_online_names,
            cpu_idle_names,
            window_ms=int(win.get("window_ms") or 120_000),
        )
    fleet_idle_win = FLEET_IDLE_TRACKER.summary(
        online_names=online_names,
        instant_idle_names=idle_names,
        settings=win,
    )
    cpu_idle_win = CPU_IDLE_TRACKER.summary(
        online_names=cpu_online_names,
        instant_idle_names=cpu_idle_names,
        settings=win,
    )
    frac_by_name = {
        r["slave_name"]: r for r in (fleet_idle_win.get("rows") or [])
    }
    for s in slaves:
        meta = frac_by_name.get(s["slave_name"]) or {}
        s["idle_frac_window"] = meta.get("idle_frac_window")
        s["sustained_idle"] = bool(meta.get("sustained_idle"))
        s["continuous_idle_ms"] = int(meta.get("continuous_idle_ms") or 0)
        s["idle_observed_ms"] = int(meta.get("observed_ms") or 0)

    sum_inflight = sum(int(s["inflight"] or 0) for s in online)
    fill_rate = machine_fill_rate(len(busy), len(online))

    by_profile = {}
    for profile in ("cpu", "gpu"):
        prof = [s for s in online if s["profile"] == profile]
        inflight = sum(int(s["inflight"] or 0) for s in prof)
        busy_n = sum(1 for s in prof if s["busy"])
        online_n = len(prof)
        sustained_n = sum(1 for s in prof if s.get("sustained_idle"))
        fracs = [
            float(s["idle_frac_window"])
            for s in prof
            if s.get("idle_frac_window") is not None
        ]
        by_profile[profile] = {
            "online": online_n,
            "busy": busy_n,
            "idle": sum(1 for s in prof if s["idle"]),
            "sustained_idle": sustained_n,
            "mean_idle_frac_window": round(sum(fracs) / len(fracs), 3) if fracs else None,
            "inflight": inflight,
            "sum_caps": online_n,
            "fill_rate": machine_fill_rate(busy_n, online_n),
        }

    online_cutoff = now_ms - SLAVE_ONLINE_MS
    # Sticky-reserved = unassigned root on a job that still has an online
    # sticky owner (someone who already worked that benchmark). Claimable =
    # unassigned with no such online owner — free for idle newcomers.
    sticky_owner_exists = """
        EXISTS (
            SELECT 1
            FROM root_batch rb2
            JOIN slave_seen ss ON ss.slave_name = rb2.slave
            WHERE rb2.benchmark_id = rb.benchmark_id
              AND rb2.slave IS NOT NULL
              AND (rb2.ready = true OR rb2.ready IS NULL)
              AND ss.last_seen >= %s
        )
    """

    unassigned = db.fetch_all(
        f"""
        SELECT
            j.challenge,
            j.settings->>'challenge_id' AS challenge_id,
            j.settings->>'track_id' AS track,
            COUNT(*) AS unassigned_roots,
            COUNT(*) FILTER (WHERE NOT ({sticky_owner_exists})) AS claimable_roots,
            COUNT(*) FILTER (WHERE {sticky_owner_exists}) AS sticky_reserved_roots,
            ROUND(MIN((EXTRACT(EPOCH FROM NOW()) * 1000 - COALESCE(j.start_time, rb.start_time, %s)) / 60000.0), 1)
                AS oldest_job_age_min,
            ROUND(MIN(
                CASE
                  WHEN rb.start_time IS NULL THEN (EXTRACT(EPOCH FROM NOW()) * 1000 - COALESCE(j.start_time, %s)) / 60000.0
                  ELSE NULL
                END
            ), 1) AS oldest_unassigned_age_min
        FROM root_batch rb
        JOIN job j ON j.benchmark_id = rb.benchmark_id
        WHERE rb.ready IS NULL
          AND rb.slave IS NULL
          AND COALESCE(j.stopped, false) = false
          AND j.end_time IS NULL
        GROUP BY j.challenge, j.settings->>'challenge_id', j.settings->>'track_id'
        ORDER BY unassigned_roots DESC
        """,
        (online_cutoff, online_cutoff, now_ms, now_ms),
    )

    oldest_unassigned = db.fetch_one(
        f"""
        SELECT
            ROUND(MAX((EXTRACT(EPOCH FROM NOW()) * 1000 - COALESCE(j.start_time, %s)) / 60000.0), 1)
                AS oldest_unassigned_root_age_min,
            COUNT(*) AS unassigned_root_total,
            COUNT(*) FILTER (WHERE NOT ({sticky_owner_exists})) AS claimable_root_total,
            COUNT(*) FILTER (WHERE {sticky_owner_exists}) AS sticky_reserved_root_total
        FROM root_batch rb
        JOIN job j ON j.benchmark_id = rb.benchmark_id
        WHERE rb.ready IS NULL
          AND rb.slave IS NULL
          AND COALESCE(j.stopped, false) = false
          AND j.end_time IS NULL
        """,
        (now_ms, online_cutoff, online_cutoff),
    ) or {}

    sticky_jobs = db.fetch_all(
        f"""
        SELECT
            left(j.benchmark_id, 12) AS benchmark,
            j.benchmark_id,
            j.challenge,
            j.settings->>'track_id' AS track,
            COUNT(*) AS sticky_reserved_roots,
            (
                SELECT rb2.slave
                FROM root_batch rb2
                JOIN slave_seen ss ON ss.slave_name = rb2.slave
                WHERE rb2.benchmark_id = j.benchmark_id
                  AND rb2.slave IS NOT NULL
                  AND (rb2.ready = true OR rb2.ready IS NULL)
                  AND ss.last_seen >= %s
                ORDER BY rb2.end_time DESC NULLS LAST, rb2.start_time DESC NULLS LAST
                LIMIT 1
            ) AS preferred_slave,
            ROUND((EXTRACT(EPOCH FROM NOW()) * 1000 - COALESCE(j.start_time, %s)) / 60000.0, 1) AS age_min
        FROM root_batch rb
        JOIN job j ON j.benchmark_id = rb.benchmark_id
        WHERE rb.ready IS NULL
          AND rb.slave IS NULL
          AND COALESCE(j.stopped, false) = false
          AND j.end_time IS NULL
          AND {sticky_owner_exists}
        GROUP BY j.benchmark_id, j.challenge, j.settings, j.start_time
        ORDER BY sticky_reserved_roots DESC, age_min DESC
        LIMIT 12
        """,
        (online_cutoff, now_ms, online_cutoff),
    )

    fattest = db.fetch_all(
        """
        SELECT
            left(j.benchmark_id, 12) AS benchmark,
            j.benchmark_id,
            j.challenge,
            j.algorithm,
            j.settings->>'track_id' AS track,
            j.num_nonces,
            j.batch_size,
            NULLIF(j.settings->>'num_bundles', '')::int AS num_bundles,
            CEIL(j.num_nonces::numeric / NULLIF(j.batch_size, 0)) AS root_batches_est,
            COUNT(rb.*) FILTER (WHERE rb.ready IS NULL) AS roots_pending,
            COUNT(rb.*) FILTER (WHERE rb.ready IS NULL AND rb.slave IS NULL) AS roots_unassigned,
            COUNT(rb.*) FILTER (WHERE rb.ready IS NULL AND rb.start_time IS NOT NULL) AS roots_inflight,
            ROUND((EXTRACT(EPOCH FROM NOW()) * 1000 - COALESCE(j.start_time, %s)) / 60000.0, 1) AS age_min,
            j.merkle_root_ready,
            j.merkle_proofs_ready
        FROM job j
        LEFT JOIN root_batch rb ON rb.benchmark_id = j.benchmark_id
        WHERE COALESCE(j.stopped, false) = false
          AND j.end_time IS NULL
        GROUP BY j.benchmark_id, j.challenge, j.algorithm, j.settings, j.num_nonces,
                 j.batch_size, j.start_time, j.merkle_root_ready, j.merkle_proofs_ready
        ORDER BY j.num_nonces DESC NULLS LAST, roots_pending DESC
        LIMIT 12
        """,
        (now_ms,),
    )

    creates = db.fetch_one(
        """
        SELECT
            COUNT(*) FILTER (WHERE start_time >= %s) AS creates_15m,
            COUNT(*) FILTER (WHERE start_time >= %s) AS creates_60m,
            COUNT(*) FILTER (
                WHERE start_time >= %s AND COALESCE(stopped, false) = true
            ) AS created_stopped_15m
        FROM job
        """,
        (now_ms - 15 * 60 * 1000, now_ms - 60 * 60 * 1000, now_ms - 15 * 60 * 1000),
    ) or {}

    finishes = db.fetch_one(
        """
        SELECT
            COUNT(*) FILTER (WHERE ready = true AND end_time >= %s) AS roots_done_15m,
            COUNT(*) FILTER (WHERE ready = true AND end_time >= %s) AS roots_done_60m
        FROM root_batch
        """,
        (now_ms - 15 * 60 * 1000, now_ms - 60 * 60 * 1000),
    ) or {}

    governor = {"enabled": False, "block_reasons": [], "counts": {}}
    if cfg:
        # Instant for display; decision idle is max(sustained, instant) so
        # empty boxes still pull creates (matches master idle_decision_count).
        idle_cpu_n = int((by_profile.get("cpu") or {}).get("idle") or 0)
        online_cpu_n = int(
            cpu_idle_win.get("online")
            or (by_profile.get("cpu") or {}).get("online")
            or 0
        )
        sustained_cpu_n = int(
            cpu_idle_win.get("sustained_idle")
            if win.get("enabled", True)
            else idle_cpu_n
        )
        decision_cpu_n = max(0, sustained_cpu_n, idle_cpu_n)
        online_gpu_n = int((by_profile.get("gpu") or {}).get("online") or 0)
        governor = _governor_view(
            cfg,
            now_ms,
            online_idle_cpu_slaves=idle_cpu_n,
            decision_idle_cpu_slaves=decision_cpu_n,
            online_cpu_slaves=online_cpu_n,
            online_gpu_slaves=online_gpu_n,
        )
        governor["idle_window"] = cpu_idle_win

    return _json_safe({
        "generated_at_ms": now_ms,
        "master_config_error": cfg_error,
        "observe_only": True,
        "slaves": {
            "online": len(online),
            "busy": len(busy),
            "idle": len(idle),
            "sustained_idle": int(fleet_idle_win.get("sustained_idle") or 0),
            "mean_idle_frac_window": fleet_idle_win.get("mean_idle_frac_window"),
            "idle_window": {
                "enabled": bool(win.get("enabled", True)),
                "window_ms": int(win.get("window_ms") or 0),
                "frac_threshold": float(win.get("frac_threshold") or 0.5),
                "min_observed_ms": int(win.get("min_observed_ms") or 0),
                "min_continuous_ms": int(win.get("min_continuous_ms") or 0),
                "fleet": {
                    k: fleet_idle_win.get(k)
                    for k in (
                        "online",
                        "instant_idle",
                        "sustained_idle",
                        "mean_idle_frac_window",
                    )
                },
                "cpu": {
                    k: cpu_idle_win.get(k)
                    for k in (
                        "online",
                        "instant_idle",
                        "sustained_idle",
                        "mean_idle_frac_window",
                    )
                },
            },
            "sum_caps": len(online),
            "inflight": sum_inflight,
            "fill_rate": fill_rate,
            "by_profile": by_profile,
            "rows": slaves,
        },
        "unassigned_by_challenge": unassigned,
        "oldest_unassigned_root_age_min": oldest_unassigned.get("oldest_unassigned_root_age_min"),
        "unassigned_root_total": int(oldest_unassigned.get("unassigned_root_total") or 0),
        "claimable_root_total": int(oldest_unassigned.get("claimable_root_total") or 0),
        "sticky_reserved_root_total": int(oldest_unassigned.get("sticky_reserved_root_total") or 0),
        "sticky_reserved_jobs": sticky_jobs,
        "governor": governor,
        "creates": {
            "creates_15m": int(creates.get("creates_15m") or 0),
            "creates_60m": int(creates.get("creates_60m") or 0),
            "created_stopped_15m": int(creates.get("created_stopped_15m") or 0),
            "http_400s": None,
            "http_400s_note": "TIG/API create 400s are not persisted yet; use governor block_reasons + creates_* as proxy.",
        },
        "finishes": {
            "roots_done_15m": int(finishes.get("roots_done_15m") or 0),
            "roots_done_60m": int(finishes.get("roots_done_60m") or 0),
        },
        "fattest_open_jobs": fattest,
        "config_snapshot": {
            "max_concurrent_benchmarks": (cfg or {}).get("max_concurrent_benchmarks"),
            "adaptive_slave_caps": (cfg or {}).get("adaptive_slave_caps"),
            "per_challenge_max_benchmarks": (cfg or {}).get("per_challenge_max_benchmarks"),
        },
    })
