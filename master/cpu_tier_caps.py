"""Per-tier CPU concurrent-batch ceilings for public pool members.

Fail safe to concurrent=1 for S/M (32-thread Pica-class). L/XL scale from
live NUM_WORKERS: one job per 32 workers, capped per tier. That feeds an
EPYC-class box several batch-32 jobs without fattening num_bundles and
without raising 7950X boxes above 1.

Load-shed still wins. No telemetry → stay at 1.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Mapping, Optional

# Mirror capability_scheduler tier ints without importing heavy deps in tests.
TIER_S = 0
TIER_M = 1
TIER_L = 2
TIER_XL = 3
TIER_NAMES = {TIER_S: "S", TIER_M: "M", TIER_L: "L", TIER_XL: "XL"}
TIER_FROM_NAME = {v: k for k, v in TIER_NAMES.items()}

DEFAULT_CPU_TIER_CAPS = {"S": 1, "M": 1, "L": 3, "XL": 6}
# Match capability_scheduler.DEFAULT_TIER_CORE_CEILINGS without importing it.
DEFAULT_TIER_CORE_CEILINGS = (32, 64, 96)
DEFAULT_XL_ABSOLUTE_MAX = 8
DEFAULT_WORKERS_PER_CPU_JOB = 32
# cores / num_workers. Kept for tests / legacy headroom helper.
DEFAULT_HEADROOM_RATIO = 1.25
DEFAULT_LOAD_OK_MULT = 0.85
DEFAULT_LOAD_SHED_MULT = 1.25
DEFAULT_MIN_FREE_RAM_GB = 4.0
DEFAULT_LOAD_SHED_COOLDOWN_MS = 10 * 60 * 1000
# After a finish, if load is still over shed threshold, hold assigns this long
# instead of a full 10m lock or an immediate re-feed into a melting box.
DEFAULT_LOAD_SHED_IDLE_COOL_MS = 60 * 1000
# If still idle+hot after this many ms, stop cool-off and allow the next job
# (load average can stick high on Picas long after InnoPool work ends).
DEFAULT_LOAD_SHED_IDLE_MAX_MS = 180 * 1000


def _env_bool(name: str, default: str = "true") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes", "on")


def cpu_tier_cap_settings(config: Optional[Mapping[str, Any]] = None) -> dict:
    """Merge CONFIG.adaptive_slave_caps / capability knobs with env defaults."""
    cfg = dict((config or {}).get("adaptive_slave_caps") or {})
    raw_tiers = cfg.get("cpu_tier_caps") or DEFAULT_CPU_TIER_CAPS
    tier_caps = dict(DEFAULT_CPU_TIER_CAPS)
    if isinstance(raw_tiers, Mapping):
        for key, value in raw_tiers.items():
            name = str(key).upper()
            if name in tier_caps:
                try:
                    tier_caps[name] = max(1, int(value))
                except (TypeError, ValueError):
                    pass
    try:
        tier_caps["L"] = max(1, int(os.environ.get("CPU_TIER_L_MAX", str(tier_caps.get("L", 3)))))
    except (TypeError, ValueError):
        tier_caps["L"] = 3
    try:
        tier_caps["XL"] = max(1, int(os.environ.get("CPU_TIER_XL_MAX", str(tier_caps.get("XL", 6)))))
    except (TypeError, ValueError):
        tier_caps["XL"] = 6
    tier_caps["XL"] = min(int(tier_caps.get("XL", 6)), DEFAULT_XL_ABSOLUTE_MAX)

    requires_telemetry = cfg.get("cpu_concurrent_requires_telemetry")
    if requires_telemetry is None:
        requires_telemetry = _env_bool("CPU_CONCURRENT_REQUIRES_TELEMETRY", "true")
    else:
        requires_telemetry = bool(requires_telemetry)

    return {
        "cpu_tier_caps": tier_caps,
        "workers_per_cpu_job": max(
            8,
            _parse_int(
                cfg.get(
                    "cpu_workers_per_job",
                    os.environ.get(
                        "CPU_WORKERS_PER_JOB", str(DEFAULT_WORKERS_PER_CPU_JOB)
                    ),
                )
            )
            or DEFAULT_WORKERS_PER_CPU_JOB,
        ),
        "cpu_concurrent_requires_telemetry": requires_telemetry,
        "headroom_ratio": float(
            cfg.get(
                "cpu_headroom_ratio",
                os.environ.get("CPU_HEADROOM_RATIO", str(DEFAULT_HEADROOM_RATIO)),
            )
        ),
        "load_ok_mult": float(
            cfg.get(
                "cpu_load_ok_mult",
                os.environ.get("CPU_LOAD_OK_MULT", str(DEFAULT_LOAD_OK_MULT)),
            )
        ),
        "load_shed_mult": float(
            cfg.get(
                "cpu_load_shed_mult",
                os.environ.get("CPU_LOAD_SHED_MULT", str(DEFAULT_LOAD_SHED_MULT)),
            )
        ),
        "min_free_ram_gb": float(
            cfg.get(
                "cpu_min_free_ram_gb",
                os.environ.get("CPU_MIN_FREE_RAM_GB", str(DEFAULT_MIN_FREE_RAM_GB)),
            )
        ),
        "load_shed_cooldown_ms": int(
            cfg.get(
                "cpu_load_shed_cooldown_ms",
                os.environ.get(
                    "CPU_LOAD_SHED_COOLDOWN_MS", str(DEFAULT_LOAD_SHED_COOLDOWN_MS)
                ),
            )
        ),
        # When true (default), load_1m shed arms only if runtime telem shows the
        # slave is processing work. Idle + residual load must not 10m-lock the box.
        "load_shed_require_active": (
            bool(cfg["load_shed_require_active"])
            if "load_shed_require_active" in cfg
            else _env_bool("CPU_LOAD_SHED_REQUIRE_ACTIVE", "true")
        ),
        "load_shed_idle_cool_ms": max(
            0,
            int(
                cfg.get(
                    "cpu_load_shed_idle_cool_ms",
                    os.environ.get(
                        "CPU_LOAD_SHED_IDLE_COOL_MS",
                        str(DEFAULT_LOAD_SHED_IDLE_COOL_MS),
                    ),
                )
            ),
        ),
        "load_shed_idle_max_ms": max(
            0,
            int(
                cfg.get(
                    "cpu_load_shed_idle_max_ms",
                    os.environ.get(
                        "CPU_LOAD_SHED_IDLE_MAX_MS",
                        str(DEFAULT_LOAD_SHED_IDLE_MAX_MS),
                    ),
                )
            ),
        ),
        "live_telemetry_enabled": _env_bool("CAPABILITY_LIVE_TELEMETRY", "true")
        or bool(cfg.get("live_telemetry_enabled")),
    }


def live_cpu_tier(
    cores: Any = None,
    workers: Any = None,
    ceilings: Any = None,
) -> int:
    """S/M/L/XL from live cores (preferred) or workers. Unknown → M."""
    n = _parse_int(cores) or _parse_int(workers)
    if n is None:
        return TIER_M
    bounds = tuple(ceilings or DEFAULT_TIER_CORE_CEILINGS)[:3]
    for idx, ceiling in enumerate(bounds):
        try:
            if int(n) < int(ceiling):
                return idx
        except (TypeError, ValueError):
            continue
    return TIER_XL


def cpu_earnable_from_live(
    *,
    cores: Any = None,
    workers: Any = None,
    load_1m: Any = None,
    settings: Optional[Mapping[str, Any]] = None,
    load_shed_active: bool = False,
) -> int:
    """Concurrent jobs this CPU box may run from live inventory."""
    cfg = settings if settings is not None else cpu_tier_cap_settings()
    telem: Dict[str, Any] = {}
    cores_i = _parse_int(cores)
    workers_i = _parse_int(workers)
    if cores_i is not None:
        telem["cores"] = cores_i
    if workers_i is not None:
        telem["num_workers"] = workers_i
    if load_1m is not None and load_1m != "":
        try:
            telem["load_1m"] = float(load_1m)
        except (TypeError, ValueError):
            pass
    return cpu_earnable_concurrent_ceiling(
        tier=live_cpu_tier(cores_i, workers_i),
        telemetry=telem,
        settings=cfg,
        load_shed_active=load_shed_active,
    )


def cpu_empty_seats(
    *,
    cores: Any = None,
    workers: Any = None,
    active: Any = 0,
    assigned: Any = None,
    load_1m: Any = None,
    settings: Optional[Mapping[str, Any]] = None,
    load_shed_active: bool = False,
) -> int:
    """Empty concurrent seats on one CPU box. Pica=1, 153-worker EPYC=4."""
    try:
        inflight = max(0, int(active or 0))
    except (TypeError, ValueError):
        inflight = 0
    if assigned is not None:
        try:
            inflight = max(inflight, max(0, int(assigned or 0)))
        except (TypeError, ValueError):
            pass
    earnable = cpu_earnable_from_live(
        cores=cores,
        workers=workers,
        load_1m=load_1m,
        settings=settings,
        load_shed_active=load_shed_active,
    )
    return max(0, int(earnable) - inflight)


def sum_cpu_empty_seats(
    rows: Any,
    settings: Optional[Mapping[str, Any]] = None,
) -> int:
    """Sum empty seats across slave_seen / telem rows."""
    cfg = settings if settings is not None else cpu_tier_cap_settings()
    total = 0
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        total += cpu_empty_seats(
            cores=row.get("telem_cores") if "telem_cores" in row else row.get("cores"),
            workers=row.get("num_workers"),
            active=row.get("telem_active") if "telem_active" in row else row.get("active"),
            assigned=row.get("assigned"),
            load_1m=row.get("load_1m"),
            settings=cfg,
        )
    return total


def should_hold_leftover_for_xl(
    *,
    poller_earnable: int = 1,
    hungry_xl_seats: int = 0,
    sticky_own: bool = False,
) -> bool:
    """True when a 1-seat box must not take leftover roots.

    Hungry XL seats (EPYC empty concurrent slots) get the unassigned pile
    first. The small box still keeps its own sticky job.
    """
    if sticky_own:
        return False
    if int(poller_earnable or 0) > 1:
        return False
    return int(hungry_xl_seats or 0) > 0


def tier_concurrent_ceiling(tier: int, settings: Mapping[str, Any]) -> int:
    name = TIER_NAMES.get(int(tier), "M")
    caps = settings.get("cpu_tier_caps") or DEFAULT_CPU_TIER_CAPS
    try:
        return max(1, int(caps.get(name, 1)))
    except (TypeError, ValueError):
        return 1


def _parse_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_nonneg_int(value: Any) -> Optional[int]:
    """Like _parse_int but allows 0 (queue depths, idle timers)."""
    if value is None or value == "":
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _parse_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Runtime state from custom InnoPool slaves (Phase C / v1.5).
SLAVE_RUNTIME_STATES = frozenset({"idle", "downloading", "running", "submitting"})
_SLAVE_VERSION_RE = re.compile(r"^[A-Za-z0-9._+/-]{1,64}$")


def _parse_slave_state(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    state = str(value).strip().lower()
    return state if state in SLAVE_RUNTIME_STATES else None


def _parse_slave_version(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    version = str(value).strip()
    if not _SLAVE_VERSION_RE.match(version):
        return None
    return version


def parse_slave_telemetry(
    *,
    query_params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Parse optional get-batches telemetry from query params and/or headers.

    Missing/invalid fields are omitted. Stock slaves that send nothing get {}.

    Phase C (capacity): cores, num_workers, load_1m, ram_gb, free_ram_gb,
    gpu_util, gpu_vram_free_mb.

    v1.5 (runtime): state, active_batches, pending_batches, last_idle_ms,
    slave_version — stored for scheduling/observability; ignored by stock.
    """
    q = query_params or {}
    h = headers or {}

    def _get(*keys: str) -> Any:
        for key in keys:
            if key in q and q.get(key) not in (None, ""):
                return q.get(key)
            # header maps may be case-insensitive Starlette headers
            try:
                if hasattr(h, "get"):
                    val = h.get(key)
                    if val not in (None, ""):
                        return val
            except Exception:
                pass
        return None

    out: Dict[str, Any] = {}
    cores = _parse_int(
        _get("cores", "X-InnoPool-Cores", "x-innopool-cores")
    )
    num_workers = _parse_int(
        _get("num_workers", "X-InnoPool-Num-Workers", "x-innopool-num-workers")
    )
    load_1m = _parse_float(
        _get("load_1m", "X-InnoPool-Load-1m", "x-innopool-load-1m")
    )
    ram_gb = _parse_int(_get("ram_gb", "X-InnoPool-Ram-Gb", "x-innopool-ram-gb"))
    free_ram_gb = _parse_float(
        _get("free_ram_gb", "X-InnoPool-Free-Ram-Gb", "x-innopool-free-ram-gb")
    )
    gpu_util = _parse_float(
        _get("gpu_util", "X-InnoPool-Gpu-Util", "x-innopool-gpu-util")
    )
    gpu_vram_free_mb = _parse_int(
        _get(
            "gpu_vram_free_mb",
            "X-InnoPool-Gpu-Vram-Free-Mb",
            "x-innopool-gpu-vram-free-mb",
        )
    )
    state = _parse_slave_state(
        _get("state", "X-InnoPool-State", "x-innopool-state")
    )
    active_batches = _parse_nonneg_int(
        _get(
            "active_batches",
            "X-InnoPool-Active-Batches",
            "x-innopool-active-batches",
        )
    )
    pending_batches = _parse_nonneg_int(
        _get(
            "pending_batches",
            "X-InnoPool-Pending-Batches",
            "x-innopool-pending-batches",
        )
    )
    last_idle_ms = _parse_nonneg_int(
        _get(
            "last_idle_ms",
            "X-InnoPool-Last-Idle-Ms",
            "x-innopool-last-idle-ms",
        )
    )
    slave_version = _parse_slave_version(
        _get(
            "slave_version",
            "X-InnoPool-Slave-Version",
            "x-innopool-slave-version",
        )
    )
    if cores is not None:
        out["cores"] = cores
    if num_workers is not None:
        out["num_workers"] = num_workers
    if load_1m is not None:
        out["load_1m"] = load_1m
    if ram_gb is not None:
        out["ram_gb"] = ram_gb
    if free_ram_gb is not None:
        out["free_ram_gb"] = free_ram_gb
    if gpu_util is not None:
        out["gpu_util"] = gpu_util
    if gpu_vram_free_mb is not None:
        out["gpu_vram_free_mb"] = gpu_vram_free_mb
    if state is not None:
        out["state"] = state
    if active_batches is not None:
        out["active_batches"] = active_batches
    if pending_batches is not None:
        out["pending_batches"] = pending_batches
    if last_idle_ms is not None:
        out["last_idle_ms"] = last_idle_ms
    if slave_version is not None:
        out["slave_version"] = slave_version
    return out


def telemetry_has_cpu_headroom(
    telemetry: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> bool:
    """True when live telemetry shows spare parallelism for concurrent > 1."""
    cores = _parse_int(telemetry.get("cores"))
    workers = _parse_int(telemetry.get("num_workers"))
    if cores is None or workers is None or workers <= 0:
        return False
    ratio = float(cores) / float(workers)
    if ratio < float(settings.get("headroom_ratio") or DEFAULT_HEADROOM_RATIO):
        return False
    load_1m = telemetry.get("load_1m")
    if load_1m is not None:
        try:
            if float(load_1m) > float(cores) * float(
                settings.get("load_ok_mult") or DEFAULT_LOAD_OK_MULT
            ):
                return False
        except (TypeError, ValueError):
            return False
    return True


def telem_slave_is_working(telemetry: Mapping[str, Any]) -> Optional[bool]:
    """Whether v1.5 runtime telem says this slave is processing InnoPool work.

    Returns:
      True  — active_batches > 0 or state in downloading/running/submitting
      False — clearly idle (active_batches == 0 and/or state == idle)
      None  — no runtime telem (stock slaves); caller should keep legacy behavior
    """
    if not telemetry:
        return None
    active_raw = telemetry.get("active_batches")
    state = _parse_slave_state(telemetry.get("state"))
    active: Optional[int] = None
    if active_raw is not None:
        try:
            active = int(active_raw)
            if active < 0:
                active = None
        except (TypeError, ValueError):
            active = None

    if active is not None:
        if active > 0:
            return True
        # active == 0: still "working" during download/submit transitions.
        if state in ("downloading", "submitting", "running"):
            return True
        return False
    if state == "idle":
        return False
    if state in ("downloading", "running", "submitting"):
        return True
    return None


def telemetry_ram_critical(
    telemetry: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> bool:
    """True when free RAM is below the safety floor."""
    free_ram = telemetry.get("free_ram_gb")
    if free_ram is None:
        return False
    try:
        return float(free_ram) < float(
            settings.get("min_free_ram_gb") or DEFAULT_MIN_FREE_RAM_GB
        )
    except (TypeError, ValueError):
        return False


def telemetry_load_over_shed(
    telemetry: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> bool:
    """True when load_1m exceeds cores * load_shed_mult."""
    cores = _parse_int(telemetry.get("cores"))
    load_1m = telemetry.get("load_1m")
    if cores is None or load_1m is None:
        return False
    try:
        return float(load_1m) > float(cores) * float(
            settings.get("load_shed_mult") or DEFAULT_LOAD_SHED_MULT
        )
    except (TypeError, ValueError):
        return False


def telemetry_requires_load_shed(
    telemetry: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> bool:
    """True when live telem says stop assigning new CPU work (concurrent 0).

    Load-average shed only applies while the slave is actually processing work
    (or when runtime telem is absent — stock-slave legacy). Idle slaves with a
    lagging high ``load_1m`` after finishing a batch must not be locked out for
    the full cooldown; that created a self-reinforcing idle fleet.

    Low free RAM still sheds even when idle (OOM risk is real regardless of
    active batches).
    """
    if not telemetry:
        return False
    if telemetry_ram_critical(telemetry, settings):
        return True
    if not telemetry_load_over_shed(telemetry, settings):
        return False
    require_active = settings.get("load_shed_require_active")
    if require_active is None:
        require_active = _env_bool("CPU_LOAD_SHED_REQUIRE_ACTIVE", "true")
    if not require_active:
        return True
    working = telem_slave_is_working(telemetry)
    if working is False:
        # Residual 1m load while InnoPool has nothing running — do not shed.
        return False
    # working True → overload while busy; None → no runtime telem → legacy.
    return True


def cpu_worker_scaled_jobs(
    telemetry: Optional[Mapping[str, Any]],
    settings: Mapping[str, Any],
) -> int:
    """How many batch-32 jobs this box can run from live worker count."""
    telem = telemetry or {}
    workers = _parse_int(telem.get("num_workers")) or _parse_int(telem.get("cores"))
    if workers is None or workers <= 0:
        return 1
    per_job = max(
        8,
        int(settings.get("workers_per_cpu_job") or DEFAULT_WORKERS_PER_CPU_JOB),
    )
    return max(1, int(workers) // per_job)


def cpu_earnable_concurrent_ceiling(
    *,
    tier: int,
    telemetry: Optional[Mapping[str, Any]],
    settings: Mapping[str, Any],
    load_shed_active: bool = False,
) -> int:
    """Per-slave CPU concurrent ceiling (1 for S/M or no evidence; 0 while load-shed).

    L/XL scale with live NUM_WORKERS (one job per 32 workers). A 192-thread
    EPYC with 153 workers gets 4 jobs; a 32-thread 7950X stays at 1.
    """
    # Load-shed must win even when tier ceiling is already 1 (fleet/Pica),
    # otherwise cooldown is a no-op and overloaded boxes keep receiving work.
    if load_shed_active or telemetry_requires_load_shed(telemetry or {}, settings):
        return 0
    if int(tier) <= TIER_M:
        return 1
    ceiling = tier_concurrent_ceiling(tier, settings)
    if ceiling <= 1:
        return 1
    telem = telemetry or {}
    if settings.get("cpu_concurrent_requires_telemetry", True) and not telem:
        return 1
    cores = _parse_int(telem.get("cores"))
    load_1m = telem.get("load_1m")
    if cores is not None and load_1m is not None:
        try:
            if float(load_1m) > float(cores) * float(
                settings.get("load_ok_mult") or DEFAULT_LOAD_OK_MULT
            ):
                return 1
        except (TypeError, ValueError):
            pass
    return min(ceiling, cpu_worker_scaled_jobs(telem, settings))


def effective_cpu_adaptive_max_cap(
    *,
    route_cap: int,
    fleet_cpu_max_cap: int,
    tier: int,
    telemetry: Optional[Mapping[str, Any]],
    settings: Mapping[str, Any],
    load_shed_active: bool = False,
) -> int:
    """Bound for adaptive CPU max_cap: fleet default, with L/XL worker scale.

    S/M always stay at 1. L/XL may exceed fleet cpu_max_cap so a large
    public box is not stuck at the 7950X one-job default.
    """
    route = max(0, int(route_cap))
    fleet = max(1, int(fleet_cpu_max_cap))
    earnable = cpu_earnable_concurrent_ceiling(
        tier=tier,
        telemetry=telemetry,
        settings=settings,
        load_shed_active=load_shed_active,
    )
    if earnable > fleet:
        return min(route, earnable)
    return min(route, fleet, earnable)


def xl_hard_root_rank_boost(
    *,
    slave_tier: int,
    hardness: float,
    hard_hardness: float,
) -> float:
    """Extra assign-rank score so L/XL prefer hard roots even at concurrent=1."""
    if float(hardness) < float(hard_hardness):
        return 0.0
    if int(slave_tier) >= TIER_XL:
        return 1.5
    if int(slave_tier) >= TIER_L:
        return 0.75
    return 0.0
