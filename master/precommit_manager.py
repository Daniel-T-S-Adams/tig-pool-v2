import copy
import os
import logging
import random
import time
from dataclasses import dataclass
from master.submissions_manager import SubmitPrecommitRequest
from common.structs import *
from common.utils import FromDict
from typing import Dict, List, Optional, Set, Tuple
from master.sql import get_db_conn
from master.client_manager import CONFIG
from master.proof_affinity import SLAVE_ONLINE_MS, ensure_slave_seen_table
from master.idle_tracker import CPU_IDLE_TRACKER, idle_window_settings
from master.capability_scheduler import (
    SCHEDULER as CAPABILITY_SCHEDULER,
    algo_is_schedulable,
    capability_settings,
    heuristic_track_hardness,
    precommit_hardness_weight_mult,
)

logger = logging.getLogger(os.path.splitext(os.path.basename(__file__))[0])

CPU_CHALLENGE_IDS = ("c001", "c002", "c003", "c007", "c008")
GPU_CHALLENGE_IDS = ("c004", "c005", "c006")


def _env_bool(name, default="true"):
    return os.environ.get(name, default).lower() in ("1", "true", "yes", "on")


def _governor_settings():
    gov = CONFIG.get("precommit_governor") or {}
    # Legacy combined ceiling — kept as the default upper bound for each profile.
    legacy_max = int(
        gov.get(
            "max_roots_pending",
            os.environ.get("PRECOMMIT_GOVERNOR_MAX_ROOTS_PENDING", "1024"),
        )
    )
    return {
        "enabled": bool(gov["enabled"]) if "enabled" in gov else _env_bool("PRECOMMIT_GOVERNOR_ENABLED", "true"),
        "max_roots_pending": legacy_max,
        "min_cpu_roots_pending": int(
            gov.get(
                "min_cpu_roots_pending",
                os.environ.get("PRECOMMIT_GOVERNOR_MIN_CPU_ROOTS_PENDING", "128"),
            )
        ),
        "max_cpu_roots_pending": int(
            gov.get(
                "max_cpu_roots_pending",
                os.environ.get("PRECOMMIT_GOVERNOR_MAX_CPU_ROOTS_PENDING", str(legacy_max)),
            )
        ),
        "min_gpu_roots_pending": int(
            gov.get(
                "min_gpu_roots_pending",
                os.environ.get("PRECOMMIT_GOVERNOR_MIN_GPU_ROOTS_PENDING", "64"),
            )
        ),
        "max_gpu_roots_pending": int(
            gov.get(
                "max_gpu_roots_pending",
                os.environ.get("PRECOMMIT_GOVERNOR_MAX_GPU_ROOTS_PENDING", str(min(legacy_max, 512))),
            )
        ),
        "cpu_roots_per_job_budget": int(
            gov.get(
                "cpu_roots_per_job_budget",
                os.environ.get("PRECOMMIT_GOVERNOR_CPU_ROOTS_PER_JOB", "24"),
            )
        ),
        "gpu_roots_per_job_budget": int(
            gov.get(
                "gpu_roots_per_job_budget",
                os.environ.get("PRECOMMIT_GOVERNOR_GPU_ROOTS_PER_JOB", "48"),
            )
        ),
        "max_cpu_unassigned_roots": int(
            gov.get(
                "max_cpu_unassigned_roots",
                os.environ.get("PRECOMMIT_GOVERNOR_MAX_CPU_UNASSIGNED_ROOTS", "512"),
            )
        ),
        "cpu_unassigned_per_online": int(
            gov.get(
                "cpu_unassigned_per_online",
                os.environ.get("PRECOMMIT_GOVERNOR_CPU_UNASSIGNED_PER_ONLINE", "8"),
            )
        ),
        "max_cpu_unassigned_roots_ceiling": int(
            gov.get(
                "max_cpu_unassigned_roots_ceiling",
                os.environ.get("PRECOMMIT_GOVERNOR_MAX_CPU_UNASSIGNED_ROOTS_CEILING", "768"),
            )
        ),
        "max_gpu_unassigned_roots": int(
            gov.get(
                "max_gpu_unassigned_roots",
                os.environ.get("PRECOMMIT_GOVERNOR_MAX_GPU_UNASSIGNED_ROOTS", "32"),
            )
        ),
        # Keep this many unowned GPU root jobs ready so a finishing GPU does
        # not wait a full TIG precommit (~2 min) before the next job.
        "gpu_spare_jobs": int(
            gov.get(
                "gpu_spare_jobs",
                os.environ.get("PRECOMMIT_GOVERNOR_GPU_SPARE_JOBS", "2"),
            )
        ),
        "min_root_ready_rate": float(
            gov.get(
                "min_root_ready_rate",
                os.environ.get("PRECOMMIT_GOVERNOR_MIN_ROOT_READY_RATE", "0.50"),
            )
        ),
        "min_samples": int(
            gov.get(
                "min_samples",
                os.environ.get("PRECOMMIT_GOVERNOR_MIN_SAMPLES", "5"),
            )
        ),
        "window_ms": int(
            gov.get(
                "window_ms",
                os.environ.get("PRECOMMIT_GOVERNOR_WINDOW_MS", str(30 * 60 * 1000)),
            )
        ),
        "cache_ms": int(
            gov.get(
                "cache_ms",
                os.environ.get("PRECOMMIT_GOVERNOR_CACHE_MS", "15000"),
            )
        ),
        # When CPU has spare capacity and claimable roots can't feed idle
        # workers, do not let a low global root_ready_rate idle the CPU fleet.
        "idle_cpu_override": (
            bool(gov["idle_cpu_override"])
            if "idle_cpu_override" in gov
            else _env_bool("PRECOMMIT_GOVERNOR_IDLE_CPU_OVERRIDE", "true")
        ),
        "idle_cpu_weight_mult": float(
            gov.get(
                "idle_cpu_weight_mult",
                os.environ.get("PRECOMMIT_GOVERNOR_IDLE_CPU_WEIGHT_MULT", "3"),
            )
        ),
    }


def _clamp_int(value: int, lo: int, hi: int) -> int:
    return max(int(lo), min(int(hi), int(value)))


def compute_cpu_unassigned_cap(
    settings: dict | None,
    online_cpu: int = 0,
) -> int:
    """Claimable-unassigned ceiling: at least the configured floor, grows with fleet.

    A create burst can emit many root rows before assign catches up. The old
    256 cap stopped minting while new boxes were still empty. Ceiling stops
    another leftover pile.
    """
    settings = settings or {}
    configured = max(1, int(settings.get("max_cpu_unassigned_roots") or 512))
    per = max(1, int(settings.get("cpu_unassigned_per_online") or 8))
    ceiling = max(configured, int(settings.get("max_cpu_unassigned_roots_ceiling") or 768))
    adaptive = max(configured, int(online_cpu or 0) * per)
    return min(ceiling, adaptive)


def compute_profile_root_caps(
    settings: dict | None,
    cpu_create_target: int,
    gpu_slots_total: int,
    online_cpu: int = 0,
) -> dict:
    """Adaptive per-profile pending-root ceilings from live create capacity."""
    settings = settings or _governor_settings()
    cpu_target = max(1, int(cpu_create_target or 1))
    gpu_slots = max(1, int(gpu_slots_total or 1))
    cpu_cap = _clamp_int(
        cpu_target * max(1, int(settings.get("cpu_roots_per_job_budget") or 24)),
        int(settings.get("min_cpu_roots_pending") or 128),
        int(settings.get("max_cpu_roots_pending") or 1024),
    )
    gpu_cap = _clamp_int(
        gpu_slots * max(1, int(settings.get("gpu_roots_per_job_budget") or 48)),
        int(settings.get("min_gpu_roots_pending") or 64),
        int(settings.get("max_gpu_roots_pending") or 512),
    )
    return {
        "cpu_pending_cap": cpu_cap,
        "gpu_pending_cap": gpu_cap,
        "cpu_unassigned_cap": compute_cpu_unassigned_cap(settings, online_cpu),
        "gpu_unassigned_cap": max(1, int(settings.get("max_gpu_unassigned_roots") or 32)),
    }


def profile_root_backlog_blocks(
    cpu_roots_pending: int,
    gpu_roots_pending: int,
    cpu_unassigned_roots: int,
    gpu_unassigned_roots: int,
    caps: dict,
) -> dict:
    """Which challenge profiles must not receive new precommits right now."""
    cpu_pending = int(cpu_roots_pending or 0)
    gpu_pending = int(gpu_roots_pending or 0)
    cpu_unassigned = int(cpu_unassigned_roots or 0)
    gpu_unassigned = int(gpu_unassigned_roots or 0)
    cpu_pending_cap = int(caps.get("cpu_pending_cap") or 0)
    gpu_pending_cap = int(caps.get("gpu_pending_cap") or 0)
    cpu_unassigned_cap = int(caps.get("cpu_unassigned_cap") or 0)
    gpu_unassigned_cap = int(caps.get("gpu_unassigned_cap") or 0)

    cpu_reasons = []
    gpu_reasons = []
    if cpu_unassigned_cap and cpu_unassigned >= cpu_unassigned_cap:
        cpu_reasons.append(
            f"cpu unassigned roots {cpu_unassigned} >= {cpu_unassigned_cap}"
        )
    if cpu_pending_cap and cpu_pending >= cpu_pending_cap:
        cpu_reasons.append(
            f"cpu root backlog {cpu_pending} >= adaptive cap {cpu_pending_cap}"
        )
    if gpu_unassigned_cap and gpu_unassigned >= gpu_unassigned_cap:
        gpu_reasons.append(
            f"gpu unassigned roots {gpu_unassigned} >= {gpu_unassigned_cap}"
        )
    if gpu_pending_cap and gpu_pending >= gpu_pending_cap:
        gpu_reasons.append(
            f"gpu root backlog {gpu_pending} >= adaptive cap {gpu_pending_cap}"
        )
    return {
        "cpu": bool(cpu_reasons),
        "gpu": bool(gpu_reasons),
        "cpu_reasons": cpu_reasons,
        "gpu_reasons": gpu_reasons,
    }


def _cpu_slot_target() -> int:
    slots = ((CONFIG.get("resource_slots") or {}).get("slots") or {})
    cpu_slots = int(slots.get("cpu") or 0)
    aws = CONFIG.get("aws_batch_capacity") or {}
    if aws.get("enabled"):
        cpu_slots = max(
            cpu_slots,
            int(aws.get("max_concurrent_cpu_jobs") or 0),
            int(aws.get("cpu_instances") or 0),
        )
    return max(0, cpu_slots)


def _gpu_slot_floor_total() -> int:
    floor = CONFIG.get("gpu_slot_floor") or {}
    return max(
        0,
        int(floor.get("hypergraph") or 0)
        + int(floor.get("vector_search") or 0)
        + int(floor.get("neuralnet_optimizer") or 0),
    )


def _gpu_slot_total() -> int:
    slots = ((CONFIG.get("resource_slots") or {}).get("slots") or {})
    total = sum(
        int(slots.get(k) or 0)
        for k in ("hypergraph", "vector_search", "neuralnet_optimizer")
    )
    return max(total, _gpu_slot_floor_total())


def _cpu_create_target(cpu_slots: int) -> int:
    """Bound idle-CPU pressure by the live concurrent budget, not raw slot count.

    resource_slots.cpu can be far above max_concurrent_benchmarks (e.g. 96 vs 13).
    Using the raw slot count made idle-CPU mode permanent and starved GPU creates.
    """
    max_concurrent = int(CONFIG.get("max_concurrent_benchmarks") or 0)
    gpu_floor = _gpu_slot_floor_total()
    if max_concurrent > 0:
        cpu_fair_share = max(1, max_concurrent - max(1, gpu_floor))
        return max(1, min(cpu_slots, cpu_fair_share))
    return max(1, cpu_slots)


def keep_ahead_want(
    *,
    idle: int = 0,
    proving: int = 0,
    online: int = 0,
) -> int:
    """How many unowned root jobs should already be sitting ready.

    Scales with the live fleet: want = idle + proving, never more than
    online boxes. Unknown online (0) does not cap, so small tests and a
    vanished ``slave_seen`` row still request replacements for a proving wave.
    """
    raw = max(0, int(idle or 0)) + max(0, int(proving or 0))
    online_n = max(0, int(online or 0))
    if online_n > 0:
        return min(raw, online_n)
    return raw


def compute_idle_cpu_needs_work(
    *,
    idle_cpu_override: bool = True,
    cpu_slots: int = 0,
    cpu_unassigned_claimable: int = 0,
    cpu_jobs_needing_roots: int = 0,
    cpu_create_target: int = 0,
    cpu_profile_blocked: bool = False,
    online_idle_cpu_slaves: int = 0,
    cpu_jobs_in_proof_phase: int = 0,
    unowned_cpu_root_jobs: int = 0,
    online_cpu_slaves: int = 0,
) -> bool:
    """True when precommit should bias toward CPU work for an underfed fleet.

    Claimable leftover roots may feed boxes that are already idle. They do
    not count as the next job for a box that is still proving. Keep-ahead
    uses unowned root-phase jobs vs ``keep_ahead_want(proving, online)``.
    """
    if not idle_cpu_override:
        return False
    if int(cpu_slots or 0) <= 0:
        return False
    if cpu_profile_blocked:
        return False
    claimable = max(0, int(cpu_unassigned_claimable or 0))
    idle = max(0, int(online_idle_cpu_slaves or 0))
    proving = max(0, int(cpu_jobs_in_proof_phase or 0))
    if idle > 0 and claimable < idle:
        return True
    want = keep_ahead_want(idle=0, proving=proving, online=online_cpu_slaves)
    if want > 0 and int(unowned_cpu_root_jobs or 0) < want:
        return True
    if idle > 0:
        return False
    if int(cpu_jobs_needing_roots or 0) >= max(1, int(cpu_create_target or 0)):
        return False
    return claimable == 0


def idle_decision_count(sustained: int = 0, instant: int = 0) -> int:
    """Idle boxes that should drive create burst and CPU override.

    Sustained-only ignored machines that just finished a wave (dashboard
    idle=23, sustained=0). Instant-only would flicker. Use the larger so
    empty boxes get work without waiting out the 2-minute window.
    """
    return max(0, int(sustained or 0), int(instant or 0))


def scaled_idle_burst_max(
    *,
    base_burst: int = 4,
    max_burst: int = 16,
    online: int = 0,
    want: int = 0,
) -> int:
    """Per-tick create cap. Grows with fleet so 200 boxes are not stuck at 16.

    ``max_burst`` is the small-fleet floor. Grow with idle/keep-ahead want
    and about one extra create per 8 online boxes, never above 64.
    Unassigned room still caps the wave.
    """
    base = max(1, int(base_burst or 1))
    configured = max(base, int(max_burst or base))
    fleet = max(0, int(online or 0))
    need = max(0, int(want or 0))
    if fleet <= 0 and need <= 0:
        return configured
    grown = max(configured, need, (fleet + 7) // 8)
    return min(64, grown)


def empty_claimable_wave(
    *,
    base_burst: int = 4,
    hi: int = 16,
    want: int = 0,
) -> int:
    """Replacement jobs when claimable is empty. Scales with keep-ahead want."""
    return min(max(1, int(hi or 1)), max(int(base_burst or 4), int(want or 0)))


def idle_create_burst(
    *,
    idle_cpu_needs_work: bool = False,
    idle_gpu_needs_work: bool = False,
    idle_cpu: int = 0,
    claimable_cpu: int = 0,
    idle_gpu: int = 0,
    claimable_gpu: int = 0,
    cpu_want_spare: int = 0,
    cpu_unowned: int = 0,
    gpu_want_spare: int = 0,
    gpu_unowned: int = 0,
    base_burst: int = 4,
    max_burst: int = 16,
    cpu_unassigned_remaining: int = 256,
    gpu_unassigned_remaining: int = 32,
    cpu_online: int = 0,
    gpu_online: int = 0,
) -> int:
    """How many precommits to attempt this tick (including the first).

    1 = normal single create. More while idle machines have fewer
    claimable roots than they can absorb, or when claimable is empty
    during a proving wave (unowned job counts do not feed finishers).
    Caps at remaining unassigned room so this cannot rebuild a leftover
    pile. Empty-claimable replacements scale with fleet/want, not a
    fixed 4.
    """
    if not idle_cpu_needs_work and not idle_gpu_needs_work:
        return 1
    want_for_cap = 0
    online_for_cap = 0
    if idle_cpu_needs_work:
        want_for_cap += max(0, int(cpu_want_spare or 0), int(idle_cpu or 0))
        online_for_cap += max(0, int(cpu_online or 0))
    if idle_gpu_needs_work:
        want_for_cap += max(0, int(gpu_want_spare or 0))
        online_for_cap += max(0, int(gpu_online or 0))
    hi = scaled_idle_burst_max(
        base_burst=base_burst,
        max_burst=max_burst,
        online=online_for_cap,
        want=want_for_cap,
    )
    # Sitting leftovers do not feed idle boxes. Subtracting them sized a
    # 16-job burst while 38 CPUs stayed empty next to 22 unassigned roots.
    cpu_idle_def = max(0, int(idle_cpu or 0)) if idle_cpu_needs_work else 0
    cpu_keep_def = max(0, int(cpu_want_spare or 0) - int(cpu_unowned or 0))
    # Unowned jobs with no claimable roots do not feed a finishing box.
    if idle_cpu_needs_work and int(claimable_cpu or 0) <= 0:
        cpu_keep_def = max(
            cpu_keep_def,
            empty_claimable_wave(
                base_burst=base_burst,
                hi=hi,
                want=cpu_want_spare,
            ),
        )
    cpu_def = max(cpu_idle_def, cpu_keep_def) if idle_cpu_needs_work else 0
    gpu_idle_def = max(0, int(idle_gpu or 0) - int(claimable_gpu or 0))
    gpu_keep_def = max(0, int(gpu_want_spare or 0) - int(gpu_unowned or 0))
    if idle_gpu_needs_work and int(claimable_gpu or 0) <= 0:
        gpu_keep_def = max(
            gpu_keep_def,
            empty_claimable_wave(
                base_burst=base_burst,
                hi=hi,
                want=gpu_want_spare,
            ),
        )
    gpu_def = max(gpu_idle_def, gpu_keep_def) if idle_gpu_needs_work else 0
    deficit = cpu_def + gpu_def
    if deficit <= 0:
        return 1
    room = 0
    if idle_cpu_needs_work:
        room += max(0, int(cpu_unassigned_remaining or 0))
    if idle_gpu_needs_work:
        room += max(0, int(gpu_unassigned_remaining or 0))
    if room <= 0:
        return 1
    return max(1, min(hi, deficit, room))


def extra_creates_this_tick(
    *,
    sized_burst: int = 1,
    first_ok: bool = False,
    max_burst: int = 16,
) -> int:
    """Extra precommit attempts after the first one this tick.

    Uses the burst sized at the start of the first attempt. Later abort
    paths must not shrink this. If the first attempt failed, still try
    the full sized burst.
    """
    sized = max(0, int(sized_burst or 0))
    hi = max(0, int(max_burst or 0))
    if sized <= 1:
        return 0
    used = 1 if first_ok else 0
    return min(hi, max(0, sized - used))


def compute_idle_gpu_starved(
    *,
    gpu_unassigned_claimable: int = 0,
    online_idle_gpu_slaves: int = 0,
    gpu_profile_blocked: bool = False,
) -> bool:
    """True only when live GPU cards are empty and have nothing to claim."""
    if gpu_profile_blocked:
        return False
    idle = max(0, int(online_idle_gpu_slaves or 0))
    return idle > 0 and int(gpu_unassigned_claimable or 0) < idle


def compute_gpu_keep_ahead(
    *,
    unowned_gpu_root_jobs: int = 0,
    gpu_spare_jobs: int = 0,
    gpu_profile_blocked: bool = False,
    gpu_jobs_in_proof_phase: int = 0,
    keep_ahead_cap: int = 8,
    online_idle_gpu_slaves: int = 0,
    online_gpu_slaves: int = 0,
    gpu_unassigned_claimable: int = 0,
) -> bool:
    """True when the unowned GPU spare pile is short. GPUs may all be busy.

    Spare target scales with idle + proving, capped by live GPU count.
    ``gpu_spare_jobs`` remains an operator floor, also capped by online.
    ``keep_ahead_cap`` is only the fallback cap when online is unknown.
    Unowned jobs with no claimable roots do not feed a finishing card.
    """
    if gpu_profile_blocked:
        return False
    proving = max(0, int(gpu_jobs_in_proof_phase or 0))
    online = max(0, int(online_gpu_slaves or 0))
    want = keep_ahead_want(idle=0, proving=proving, online=online)
    if online <= 0 and proving > 0:
        want = min(want, max(0, int(keep_ahead_cap or 0)) or want)
    spare = max(0, int(gpu_spare_jobs or 0))
    if spare > 0:
        spare_cap = online if online > 0 else spare
        want = max(want, min(spare, spare_cap))
    usable = int(unowned_gpu_root_jobs or 0)
    if int(gpu_unassigned_claimable or 0) <= 0:
        usable = 0
    return want > 0 and usable < want


def compute_idle_gpu_needs_work(
    *,
    gpu_unassigned_claimable: int = 0,
    online_idle_gpu_slaves: int = 0,
    gpu_profile_blocked: bool = False,
    unowned_gpu_root_jobs: int = 0,
    gpu_spare_jobs: int = 0,
    gpu_jobs_in_proof_phase: int = 0,
    online_gpu_slaves: int = 0,
) -> bool:
    """True when GPUs need more claimable work, including a keep-ahead spare.

    Reactive: idle GPUs and not enough unowned roots.
    Keep-ahead: create spare unowned GPU jobs *before* anyone goes idle so
    the next card does not wait for a TIG precommit.
    Keep-ahead must not lock the create lottery to GPU — that turns an idle
    CPU burst into a pile of extra GPU jobs.
    """
    return compute_idle_gpu_starved(
        gpu_unassigned_claimable=gpu_unassigned_claimable,
        online_idle_gpu_slaves=online_idle_gpu_slaves,
        gpu_profile_blocked=gpu_profile_blocked,
    ) or compute_gpu_keep_ahead(
        unowned_gpu_root_jobs=unowned_gpu_root_jobs,
        gpu_spare_jobs=gpu_spare_jobs,
        gpu_profile_blocked=gpu_profile_blocked,
        gpu_jobs_in_proof_phase=gpu_jobs_in_proof_phase,
        online_idle_gpu_slaves=online_idle_gpu_slaves,
        online_gpu_slaves=online_gpu_slaves,
        gpu_unassigned_claimable=gpu_unassigned_claimable,
    )


def effective_concurrent_cap(
    *,
    max_concurrent: int = 0,
    online_cpu: int = 0,
    online_gpu: int = 0,
    cpu_want_spare: int = 0,
    gpu_want_spare: int = 0,
    idle_needs_work: bool = False,
) -> int:
    """Create ceiling while idle boxes have nothing to claim.

    ``max_concurrent_benchmarks`` is the configured leftover brake.
    Live values are often far above the fleet (e.g. 276). This lift only
    matters when that setting is below online boxes plus keep-ahead.
    One job per box needs the live fleet, plus keep-ahead replacements for
    boxes that are already proving. Proof-phase jobs do not feed idle boxes.
    """
    cap = max(0, int(max_concurrent or 0))
    if not idle_needs_work:
        return cap
    fleet = max(0, int(online_cpu or 0)) + max(0, int(online_gpu or 0))
    spare = max(0, int(cpu_want_spare or 0)) + max(0, int(gpu_want_spare or 0))
    return max(cap, fleet + spare)


def concurrent_create_allowed(
    *,
    root_phase_jobs: int = 0,
    proof_phase_jobs: int = 0,
    submitted: int = 0,
    max_concurrent: int = 0,
    overlap_cap: int = 8,
) -> bool:
    """True when another precommit may start.

    Proof-phase jobs still occupy runtime, but they no longer feed idle root
    workers. Allow a bounded overlap so the next wave is already submitting
    while the current wave proves — otherwise idle spikes for the ~1-2 min
    TIG precommit after proofs finish.
    """
    cap = int(max_concurrent or 0)
    if cap <= 0:
        return True
    root = max(0, int(root_phase_jobs or 0))
    proof = max(0, int(proof_phase_jobs or 0))
    inflight = max(0, int(submitted or 0))
    overlap = min(proof, max(0, int(overlap_cap or 0)))
    if root + proof + inflight >= cap + overlap:
        return False
    return root + inflight < cap


def challenge_under_create_cap(
    challenge_id: str,
    *,
    pending_counts: dict,
    root_phase_counts: dict,
    submitted: dict,
    per_challenge_max: dict,
    idle_gpu_needs_work: bool = False,
    idle_gpu_starved: bool = False,
    gpu_keep_ahead: bool = False,
    gpu_ids: tuple = ("c004", "c005", "c006"),
    gpu_spare_jobs: int = 0,
    idle_gpu_slaves: int = 0,
    idle_cpu_needs_work: bool = False,
    idle_cpu_slaves: int = 0,
    cpu_ids: tuple = ("c001", "c002", "c003", "c007", "c008"),
) -> bool:
    """True when this challenge may receive another precommit.

    Proof-phase jobs do not feed idle root workers. When a profile is idle
    with no claimable roots, count only root-phase jobs against that
    profile's per-challenge cap so a new root job can start.
    GPU idle lift grows with empty cards. CPU idle lift grows with idle
    boxes — a +2 lift left dozens of CPUs empty against autopilot caps of 7.
    Unassigned remaining still caps the leftover pile.
    """
    cid = str(challenge_id or "")[:4]
    cap = per_challenge_max.get(cid)
    if cap is None:
        return True
    starved = bool(idle_gpu_starved) or (
        bool(idle_gpu_needs_work) and not bool(gpu_keep_ahead)
    )
    cpu_idle = bool(idle_cpu_needs_work) and cid in cpu_ids
    gpu_idle = starved and cid in gpu_ids
    counts = root_phase_counts if (cpu_idle or gpu_idle) else pending_counts
    used = int((counts or {}).get(cid, 0) or 0) + int((submitted or {}).get(cid, 0) or 0)
    extra = 0
    if gpu_idle:
        extra = max(int(gpu_spare_jobs or 0), int(idle_gpu_slaves or 0), 1)
    elif gpu_keep_ahead and cid in gpu_ids:
        extra = max(int(gpu_spare_jobs or 0), 1)
    elif cpu_idle:
        extra = max(1, int(idle_cpu_slaves or 0))
    return used < int(cap) + extra


def should_force_cpu_only(
    *,
    idle_cpu_needs_work: bool,
    gpu_starved: bool,
    idle_gpu_starved: bool = False,
    idle_gpu_needs_work: bool = False,
    cpu_profile_blocked: bool,
) -> bool:
    """Hard CPU filter once GPUs already have jobs.

    Empty GPU cards must still receive reserved creates. This filter only
    blocks the mixed lottery; ``should_reserve_idle_gpu_create`` can override
    it for a fleet-scaled GPU wave.
    """
    del idle_gpu_needs_work
    if idle_gpu_starved:
        return False
    return bool(
        idle_cpu_needs_work
        and (not gpu_starved)
        and (not cpu_profile_blocked)
    )


def should_reserve_idle_gpu_create(
    *,
    idle_gpu_needs_work: bool,
    last_create_ms: int = 0,
    now_ms: int = 0,
    cooldown_ms: int = 30_000,
    skip_cooldown: bool = False,
) -> bool:
    """Reserve a GPU create while cards need work.

    Keep-ahead uses a cooldown so a CPU burst cannot flood GPU. Empty
    cards skip that cooldown so idle GPUs are not stuck for 30s each.
    """
    if not idle_gpu_needs_work:
        return False
    if skip_cooldown:
        return True
    last = int(last_create_ms or 0)
    now = int(now_ms or 0)
    if last > 0 and now > 0 and (now - last) < int(cooldown_ms):
        return False
    return True


def has_positive_weight_for_profile(eligible, challenge_ids) -> bool:
    """True when at least one eligible algo in this profile has weight > 0."""
    ids = set(challenge_ids or ())
    for item in eligible or []:
        cid = str((item or {}).get("algorithm_id") or "")[:4]
        if cid in ids and int((item or {}).get("weight") or 0) > 0:
            return True
    return False


def should_block_precommit_create(
    roots_pending,
    benchmarks_seen,
    root_ready_benchmarks,
    settings=None,
    idle_cpu_needs_work=False,
):
    """Soft create-gate used by PrecommitManager and unit tests.

    Per-profile pending/unassigned caps are enforced separately via
    profile_root_backlog_blocks (filter eligible algos). This function only
    applies the soft root_ready_rate drain. Idle CPU with claimable roots below
    idle capacity may override that soft gate so spare CPU workers are not left empty.

    Legacy max_roots_pending is retained in settings for adaptive ceiling
    defaults only; it is not a global hard create-block anymore.
    """
    settings = settings or _governor_settings()
    if not settings.get("enabled", True):
        return False, ""
    min_root_ready_rate = float(settings.get("min_root_ready_rate") or 0.50)
    min_samples = int(settings.get("min_samples") or 5)
    roots_pending = int(roots_pending or 0)
    benchmarks_seen = int(benchmarks_seen or 0)
    root_ready_benchmarks = int(root_ready_benchmarks or 0)

    if roots_pending > 0 and benchmarks_seen >= min_samples:
        root_ready_rate = root_ready_benchmarks / max(1, benchmarks_seen)
        if root_ready_rate < min_root_ready_rate:
            if idle_cpu_needs_work and settings.get("idle_cpu_override", True):
                return False, (
                    f"idle_cpu_override: root_ready_rate {root_ready_rate:.3f} "
                    f"< {min_root_ready_rate:.3f} but CPU has spare capacity "
                    f"and claimable roots below idle fleet size"
                )
            return True, (
                f"root_ready_rate {root_ready_rate:.3f} < {min_root_ready_rate:.3f} "
                f"with roots_pending={roots_pending}"
            )
    return False, ""


class PrecommitManager:
    def __init__(self):
        self.last_block_id = None
        self.num_precommits_submitted = 0
        self.per_challenge_precommits_submitted = {}
        self.algorithm_name_2_id = {}
        self.challenge_name_2_id = {}
        self._governor_cache = None
        self._governor_cache_until_ms = 0
        # Read by master/main.py idle-burst loop.
        self.last_idle_cpu_needs_work = False
        self.last_idle_gpu_needs_work = False
        self.last_idle_burst = 1
        self.last_sized_burst = 1
        self.last_idle_window = {}
        self._idle_gpu_create_ms = 0
        self._idle_gpu_reserved_count = 0
        self._idle_gpu_reserve_tick_ms = 0

    def _refresh_cpu_idle_window(self, now_ms: Optional[int] = None) -> dict:
        """Sample online/idle CPU names and update the sustained-idle window."""
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        win = idle_window_settings()
        try:
            ensure_slave_seen_table(get_db_conn().execute)
            online_cutoff = now_ms - int(SLAVE_ONLINE_MS)
            online_rows = get_db_conn().fetch_all(
                """
                SELECT ss.slave_name
                FROM slave_seen ss
                WHERE ss.last_seen >= %s
                  AND ss.slave_name LIKE 'pool-cpu-%%'
                """,
                (online_cutoff,),
            ) or []
            idle_rows = get_db_conn().fetch_all(
                """
                SELECT ss.slave_name
                FROM slave_seen ss
                WHERE ss.last_seen >= %s
                  AND ss.slave_name LIKE 'pool-cpu-%%'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM root_batch rb
                    WHERE rb.slave = ss.slave_name
                      AND rb.ready IS NULL
                      AND rb.start_time IS NOT NULL
                  )
                  AND COALESCE(ss.telem_state, 'idle') NOT IN
                      ('running', 'downloading', 'submitting')
                  AND COALESCE(ss.telem_active, 0) <= 0
                """,
                (online_cutoff,),
            ) or []
            online_names = [r["slave_name"] for r in online_rows if r.get("slave_name")]
            idle_names = [r["slave_name"] for r in idle_rows if r.get("slave_name")]
        except Exception as exc:
            logger.warning("cpu idle window sample failed: %s", exc)
            summary = {
                "enabled": bool(win.get("enabled", True)),
                "window_ms": int(win.get("window_ms") or 0),
                "frac_threshold": float(win.get("frac_threshold") or 0.5),
                "min_observed_ms": int(win.get("min_observed_ms") or 0),
                "min_continuous_ms": int(win.get("min_continuous_ms") or 0),
                "online": 0,
                "instant_idle": 0,
                "sustained_idle": 0,
                "mean_idle_frac_window": None,
                "rows": [],
                "error": str(exc),
            }
            self.last_idle_window = summary
            return summary

        if win.get("enabled", True):
            CPU_IDLE_TRACKER.update(
                now_ms,
                online_names,
                idle_names,
                window_ms=int(win.get("window_ms") or 120_000),
            )
            summary = CPU_IDLE_TRACKER.summary(
                online_names=online_names,
                instant_idle_names=idle_names,
                settings=win,
            )
        else:
            summary = {
                "enabled": False,
                "window_ms": int(win.get("window_ms") or 0),
                "frac_threshold": float(win.get("frac_threshold") or 0.5),
                "min_observed_ms": int(win.get("min_observed_ms") or 0),
                "min_continuous_ms": int(win.get("min_continuous_ms") or 0),
                "online": len(online_names),
                "instant_idle": len(idle_names),
                "sustained_idle": len(idle_names),
                "mean_idle_frac_window": None,
                "rows": [],
            }
        self.last_idle_window = summary
        return summary

    def on_new_block(self, block: Block, **kwargs):
        self.last_block_id = block.id
        self.num_precommits_submitted = 0
        self.per_challenge_precommits_submitted = {}
        self.challenge_configs = block.config["challenges"]
        self._algorithms = kwargs.get("algorithms")
        self._binarys = kwargs.get("binarys")
        self._tracks_data = kwargs.get("tracks_data")
        self._block_round = getattr(getattr(block, "details", None), "round", None)
        try:
            CAPABILITY_SCHEDULER.set_tig_context(
                tracks_data=self._tracks_data,
                algorithms=self._algorithms,
                binarys=self._binarys,
                block_round=self._block_round,
            )
        except Exception:
            pass

    def _apply_idle_window_to_snapshot(self, snapshot: dict, idle_win: dict) -> dict:
        """Overlay sustained idle onto a governor snapshot for create decisions."""
        snap = dict(snapshot or {})
        settings = snap.get("settings") or _governor_settings()
        instant = int(
            idle_win.get("instant_idle")
            if idle_win.get("instant_idle") is not None
            else snap.get("online_idle_cpu_slaves")
            or 0
        )
        sustained = int(
            idle_win.get("sustained_idle")
            if idle_win.get("enabled", True)
            else instant
        )
        if not idle_win.get("enabled", True):
            sustained = instant
        decision_idle = idle_decision_count(sustained, instant)
        online_cpu = int(idle_win.get("online") or 0)
        cpu_slots = max(int(snap.get("cpu_slots") or 0), online_cpu)
        cpu_create_target = (
            _cpu_create_target(cpu_slots)
            if cpu_slots > 0
            else int(snap.get("cpu_create_target") or 0)
        )
        snap["cpu_slots"] = cpu_slots
        snap["cpu_create_target"] = cpu_create_target
        snap["online_cpu_slaves"] = online_cpu
        caps = dict(snap.get("profile_caps") or {})
        if caps:
            caps["cpu_unassigned_cap"] = compute_cpu_unassigned_cap(settings, online_cpu)
            snap["profile_caps"] = caps
            snap["profile_blocks"] = profile_root_backlog_blocks(
                int(snap.get("cpu_roots_pending") or 0),
                int(snap.get("gpu_roots_pending") or 0),
                int(snap.get("cpu_unassigned_claimable") or 0),
                int(snap.get("gpu_unassigned_claimable") or 0),
                caps,
            )
        profile_blocks = snap.get("profile_blocks") or {"cpu": False, "gpu": False}
        idle_cpu_needs_work = compute_idle_cpu_needs_work(
            idle_cpu_override=bool(settings.get("idle_cpu_override", True)),
            cpu_slots=cpu_slots,
            cpu_unassigned_claimable=int(snap.get("cpu_unassigned_claimable") or 0),
            cpu_jobs_needing_roots=int(snap.get("cpu_jobs_needing_roots") or 0),
            cpu_create_target=cpu_create_target,
            cpu_profile_blocked=bool(profile_blocks.get("cpu")),
            online_idle_cpu_slaves=decision_idle,
            cpu_jobs_in_proof_phase=int(snap.get("cpu_jobs_in_proof_phase") or 0),
            unowned_cpu_root_jobs=int(snap.get("unowned_cpu_root_jobs") or 0),
            online_cpu_slaves=max(online_cpu, int(snap.get("online_cpu_slaves") or 0)),
        )
        snap["online_idle_cpu_slaves"] = instant
        snap["online_idle_cpu_slaves_instant"] = instant
        snap["sustained_idle_cpu_slaves"] = sustained
        snap["decision_idle_cpu_slaves"] = decision_idle
        snap["idle_window"] = idle_win
        snap["idle_cpu_needs_work"] = idle_cpu_needs_work
        return snap

    def _governor_snapshot(self) -> dict:
        settings = _governor_settings()
        if not settings.get("enabled", True):
            return {"enabled": False, "idle_cpu_needs_work": False}
        now_ms = int(time.time() * 1000)
        # Always refresh the idle window (cheap) so burst/create sees sustained
        # idle even while heavier governor counts are cached.
        idle_win = self._refresh_cpu_idle_window(now_ms)
        cache_ms = max(0, int(settings.get("cache_ms") or 0))
        if (
            self._governor_cache is not None
            and cache_ms > 0
            and now_ms < self._governor_cache_until_ms
        ):
            return self._apply_idle_window_to_snapshot(self._governor_cache, idle_win)
        try:
            ensure_slave_seen_table(get_db_conn().execute)
            cutoff_ms = now_ms - int(settings.get("window_ms") or (30 * 60 * 1000))
            row = get_db_conn().fetch_one(
                """
                SELECT
                    (
                        SELECT COUNT(*)
                        FROM root_batch rb
                        JOIN job j ON j.benchmark_id = rb.benchmark_id
                        WHERE rb.ready IS NULL
                          AND j.stopped IS NULL
                          AND j.end_time IS NULL
                          AND j.merkle_root_ready IS NULL
                    ) AS roots_pending,
                    (
                        SELECT COUNT(*)
                        FROM root_batch rb
                        JOIN job j ON j.benchmark_id = rb.benchmark_id
                        WHERE rb.ready IS NULL
                          AND j.stopped IS NULL
                          AND j.end_time IS NULL
                          AND j.merkle_root_ready IS NULL
                          AND j.settings->>'challenge_id' IN %s
                    ) AS cpu_roots_pending,
                    (
                        SELECT COUNT(*)
                        FROM root_batch rb
                        JOIN job j ON j.benchmark_id = rb.benchmark_id
                        WHERE rb.ready IS NULL
                          AND j.stopped IS NULL
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
                        WHERE j.stopped IS NULL
                          AND j.end_time IS NULL
                          AND j.settings->>'challenge_id' IN %s
                    ) AS cpu_active_jobs,
                    (
                        SELECT COUNT(*)
                        FROM job j
                        WHERE j.stopped IS NULL
                          AND j.end_time IS NULL
                          AND j.merkle_root_ready IS NULL
                          AND j.settings->>'challenge_id' IN %s
                    ) AS cpu_jobs_needing_roots,
                    (
                        SELECT COUNT(*)
                        FROM job j
                        WHERE j.stopped IS NULL
                          AND j.end_time IS NULL
                          AND j.merkle_root_ready = true
                          AND j.merkle_proofs_ready IS NULL
                          AND j.settings->>'challenge_id' IN %s
                    ) AS cpu_jobs_in_proof_phase,
                    (
                        SELECT COUNT(*)
                        FROM job j
                        WHERE j.stopped IS NULL
                          AND j.end_time IS NULL
                          AND j.settings->>'challenge_id' IN %s
                    ) AS gpu_active_jobs,
                    (
                        SELECT COUNT(*)
                        FROM job j
                        WHERE j.stopped IS NULL
                          AND j.end_time IS NULL
                          AND j.merkle_root_ready = true
                          AND j.merkle_proofs_ready IS NULL
                          AND j.settings->>'challenge_id' IN %s
                    ) AS gpu_jobs_in_proof_phase,
                    (
                        -- Root-phase GPU jobs that no slave has touched yet.
                        -- These are the keep-ahead buffer idle GPUs can take.
                        SELECT COUNT(*)
                        FROM job j
                        WHERE j.stopped IS NULL
                          AND j.end_time IS NULL
                          AND j.merkle_root_ready IS NULL
                          AND j.settings->>'challenge_id' IN %s
                          AND NOT EXISTS (
                            SELECT 1
                            FROM root_batch rb
                            WHERE rb.benchmark_id = j.benchmark_id
                              AND rb.slave IS NOT NULL
                          )
                    ) AS unowned_gpu_root_jobs,
                    (
                        SELECT COUNT(*)
                        FROM job j
                        WHERE j.stopped IS NULL
                          AND j.end_time IS NULL
                          AND j.merkle_root_ready IS NULL
                          AND j.settings->>'challenge_id' IN %s
                          AND NOT EXISTS (
                            SELECT 1
                            FROM root_batch rb
                            WHERE rb.benchmark_id = j.benchmark_id
                              AND rb.slave IS NOT NULL
                          )
                    ) AS unowned_cpu_root_jobs,
                    (
                        SELECT COUNT(*)
                        FROM slave_seen ss
                        WHERE ss.last_seen >= %s
                          AND ss.slave_name LIKE 'pool-cpu-%%'
                    ) AS online_cpu_slaves,
                    (
                        SELECT COUNT(*)
                        FROM slave_seen ss
                        WHERE ss.last_seen >= %s
                          AND (
                            ss.slave_name LIKE 'pool-gpu-%%'
                            OR ss.slave_name LIKE 'c3-slave-%%'
                          )
                    ) AS online_gpu_slaves,
                    (
                        SELECT COUNT(*)
                        FROM root_batch rb
                        JOIN job j ON j.benchmark_id = rb.benchmark_id
                        WHERE rb.ready IS NULL
                          AND rb.slave IS NULL
                          AND j.stopped IS NULL
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
                          AND j.stopped IS NULL
                          AND j.end_time IS NULL
                          AND j.merkle_root_ready IS NULL
                          AND j.settings->>'challenge_id' IN %s
                    ) AS gpu_unassigned_roots,
                    (
                        -- Claimable = unassigned and not reserved for an online
                        -- sticky owner. Sticky-warehoused roots must not freeze
                        -- CPU creates for idle newcomers.
                        SELECT COUNT(*)
                        FROM root_batch rb
                        JOIN job j ON j.benchmark_id = rb.benchmark_id
                        WHERE rb.ready IS NULL
                          AND rb.slave IS NULL
                          AND j.stopped IS NULL
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
                          AND j.stopped IS NULL
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
                    ) AS gpu_unassigned_claimable,
                    (
                        -- Online CPU slaves with no assigned unfinished root work
                        -- and no live slave telemetry saying they are still running.
                        SELECT COUNT(*)
                        FROM slave_seen ss
                        WHERE ss.last_seen >= %s
                          AND ss.slave_name LIKE 'pool-cpu-%%'
                          AND NOT EXISTS (
                            SELECT 1
                            FROM root_batch rb
                            WHERE rb.slave = ss.slave_name
                              AND rb.ready IS NULL
                              AND rb.start_time IS NOT NULL
                          )
                          AND COALESCE(ss.telem_state, 'idle') NOT IN
                              ('running', 'downloading', 'submitting')
                          AND COALESCE(ss.telem_active, 0) <= 0
                    ) AS online_idle_cpu_slaves,
                    (
                        SELECT COUNT(*)
                        FROM slave_seen ss
                        WHERE ss.last_seen >= %s
                          AND (
                            ss.slave_name LIKE 'pool-gpu-%%'
                            OR ss.slave_name LIKE 'c3-slave-%%'
                          )
                          AND NOT EXISTS (
                            SELECT 1
                            FROM root_batch rb
                            WHERE rb.slave = ss.slave_name
                              AND rb.ready IS NULL
                              AND rb.start_time IS NOT NULL
                          )
                    ) AS online_idle_gpu_slaves
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
                    CPU_CHALLENGE_IDS,
                    GPU_CHALLENGE_IDS,
                    GPU_CHALLENGE_IDS,
                    GPU_CHALLENGE_IDS,
                    CPU_CHALLENGE_IDS,
                    now_ms - int(SLAVE_ONLINE_MS),
                    now_ms - int(SLAVE_ONLINE_MS),
                    CPU_CHALLENGE_IDS,
                    GPU_CHALLENGE_IDS,
                    CPU_CHALLENGE_IDS,
                    now_ms - int(SLAVE_ONLINE_MS),
                    GPU_CHALLENGE_IDS,
                    now_ms - int(SLAVE_ONLINE_MS),
                    now_ms - int(SLAVE_ONLINE_MS),
                    now_ms - int(SLAVE_ONLINE_MS),
                ),
            ) or {}
            cpu_slots = _cpu_slot_target()
            cpu_create_target = _cpu_create_target(cpu_slots)
            gpu_slots_total = _gpu_slot_total()
            cpu_active_jobs = int(row.get("cpu_active_jobs") or 0)
            cpu_jobs_needing_roots = int(row.get("cpu_jobs_needing_roots") or 0)
            cpu_jobs_in_proof_phase = int(row.get("cpu_jobs_in_proof_phase") or 0)
            gpu_active_jobs = int(row.get("gpu_active_jobs") or 0)
            gpu_jobs_in_proof_phase = int(row.get("gpu_jobs_in_proof_phase") or 0)
            unowned_gpu_root_jobs = int(row.get("unowned_gpu_root_jobs") or 0)
            unowned_cpu_root_jobs = int(row.get("unowned_cpu_root_jobs") or 0)
            online_cpu_slaves = int(row.get("online_cpu_slaves") or 0)
            online_gpu_slaves = int(row.get("online_gpu_slaves") or 0)
            cpu_roots_pending = int(row.get("cpu_roots_pending") or 0)
            gpu_roots_pending = int(row.get("gpu_roots_pending") or 0)
            cpu_unassigned_roots = int(row.get("cpu_unassigned_roots") or 0)
            gpu_unassigned_roots = int(row.get("gpu_unassigned_roots") or 0)
            cpu_unassigned_claimable = int(row.get("cpu_unassigned_claimable") or 0)
            gpu_unassigned_claimable = int(row.get("gpu_unassigned_claimable") or 0)
            online_idle_cpu_slaves = int(row.get("online_idle_cpu_slaves") or 0)
            online_idle_gpu_slaves = int(row.get("online_idle_gpu_slaves") or 0)
            gpu_floor = _gpu_slot_floor_total()
            profile_caps = compute_profile_root_caps(
                settings,
                cpu_create_target,
                gpu_slots_total,
                online_cpu=int(idle_win.get("online") or 0),
            )
            profile_blocks = profile_root_backlog_blocks(
                cpu_roots_pending,
                gpu_roots_pending,
                cpu_unassigned_claimable,
                gpu_unassigned_claimable,
                profile_caps,
            )
            # Instant idle from SQL; sustained overlay applied below.
            snapshot = {
                "enabled": True,
                "settings": settings,
                "roots_pending": int(row.get("roots_pending") or 0),
                "cpu_roots_pending": cpu_roots_pending,
                "gpu_roots_pending": gpu_roots_pending,
                "benchmarks_seen": int(row.get("benchmarks_seen") or 0),
                "root_ready_benchmarks": int(row.get("root_ready_benchmarks") or 0),
                "cpu_slots": cpu_slots,
                "cpu_create_target": cpu_create_target,
                "gpu_slots_total": gpu_slots_total,
                "cpu_active_jobs": cpu_active_jobs,
                "cpu_jobs_needing_roots": cpu_jobs_needing_roots,
                "cpu_jobs_in_proof_phase": cpu_jobs_in_proof_phase,
                "gpu_active_jobs": gpu_active_jobs,
                "gpu_jobs_in_proof_phase": gpu_jobs_in_proof_phase,
                "unowned_gpu_root_jobs": unowned_gpu_root_jobs,
                "unowned_cpu_root_jobs": unowned_cpu_root_jobs,
                "online_cpu_slaves": online_cpu_slaves,
                "online_gpu_slaves": online_gpu_slaves,
                "gpu_slot_floor": gpu_floor,
                "cpu_unassigned_roots": cpu_unassigned_roots,
                "gpu_unassigned_roots": gpu_unassigned_roots,
                "cpu_unassigned_claimable": cpu_unassigned_claimable,
                "gpu_unassigned_claimable": gpu_unassigned_claimable,
                "online_idle_cpu_slaves": online_idle_cpu_slaves,
                "online_idle_gpu_slaves": online_idle_gpu_slaves,
                "profile_caps": profile_caps,
                "profile_blocks": profile_blocks,
                "idle_cpu_needs_work": False,
                "idle_gpu_starved": compute_idle_gpu_starved(
                    gpu_unassigned_claimable=gpu_unassigned_claimable,
                    online_idle_gpu_slaves=online_idle_gpu_slaves,
                    gpu_profile_blocked=bool(profile_blocks.get("gpu")),
                ),
                "gpu_keep_ahead": compute_gpu_keep_ahead(
                    unowned_gpu_root_jobs=unowned_gpu_root_jobs,
                    gpu_spare_jobs=int(settings.get("gpu_spare_jobs") or 0),
                    gpu_profile_blocked=bool(profile_blocks.get("gpu")),
                    gpu_jobs_in_proof_phase=gpu_jobs_in_proof_phase,
                    online_idle_gpu_slaves=online_idle_gpu_slaves,
                    online_gpu_slaves=online_gpu_slaves,
                    gpu_unassigned_claimable=gpu_unassigned_claimable,
                ),
                "idle_gpu_needs_work": compute_idle_gpu_needs_work(
                    gpu_unassigned_claimable=gpu_unassigned_claimable,
                    online_idle_gpu_slaves=online_idle_gpu_slaves,
                    gpu_profile_blocked=bool(profile_blocks.get("gpu")),
                    unowned_gpu_root_jobs=unowned_gpu_root_jobs,
                    gpu_spare_jobs=int(settings.get("gpu_spare_jobs") or 0),
                    gpu_jobs_in_proof_phase=gpu_jobs_in_proof_phase,
                    online_gpu_slaves=online_gpu_slaves,
                ),
            }
        except Exception as exc:
            # Fail open: a transient DB blip must not freeze precommit creation.
            logger.warning("precommit governor query failed; allowing create: %s", exc)
            snapshot = {"enabled": False, "idle_cpu_needs_work": False, "error": str(exc)}
        snapshot = self._apply_idle_window_to_snapshot(snapshot, idle_win)
        # Cache the heavy counts without pinning a stale idle decision.
        self._governor_cache = {
            k: v for k, v in snapshot.items()
            if k not in ("idle_cpu_needs_work", "idle_window", "sustained_idle_cpu_slaves")
        }
        self._governor_cache_until_ms = now_ms + cache_ms
        return snapshot

    def run(self) -> SubmitPrecommitRequest:
        pending_row = get_db_conn().fetch_one(
            """
            SELECT
                COUNT(*) FILTER (WHERE merkle_root_ready IS NULL) AS root_phase,
                COUNT(*) FILTER (WHERE merkle_root_ready IS NOT NULL) AS proof_phase,
                COUNT(*) AS pending
            FROM job
            WHERE merkle_proofs_ready IS NULL
                AND stopped IS NULL
            """
        ) or {}
        num_pending_jobs = int(pending_row.get("pending") or 0)
        root_phase_jobs = int(pending_row.get("root_phase") or 0)
        proof_phase_jobs = int(pending_row.get("proof_phase") or 0)

        algo_selection = CONFIG["algo_selection"]

        governor = self._governor_snapshot()
        idle_cpu_needs_work = bool(governor.get("idle_cpu_needs_work"))
        idle_gpu_needs_work = bool(governor.get("idle_gpu_needs_work"))
        self.last_idle_cpu_needs_work = idle_cpu_needs_work
        self.last_idle_gpu_needs_work = idle_gpu_needs_work
        caps = governor.get("profile_caps") or {}
        cpu_want_spare = keep_ahead_want(
            idle=0,
            proving=int(governor.get("cpu_jobs_in_proof_phase") or 0),
            online=int(governor.get("online_cpu_slaves") or 0),
        )
        gpu_want_spare = keep_ahead_want(
            idle=0,
            proving=int(governor.get("gpu_jobs_in_proof_phase") or 0),
            online=int(governor.get("online_gpu_slaves") or 0),
        )
        configured_cap = int(CONFIG.get("max_concurrent_benchmarks") or 0)
        create_cap = effective_concurrent_cap(
            max_concurrent=configured_cap,
            online_cpu=int(governor.get("online_cpu_slaves") or 0),
            online_gpu=int(governor.get("online_gpu_slaves") or 0),
            cpu_want_spare=cpu_want_spare,
            gpu_want_spare=gpu_want_spare,
            idle_needs_work=idle_cpu_needs_work or idle_gpu_needs_work,
        )
        overlap_cap = int(os.environ.get("PRECOMMIT_PROOF_OVERLAP", "8"))
        # Size the idle burst before any gate so extras can still run this
        # tick even if the first attempt aborts.
        self.last_idle_burst = idle_create_burst(
            idle_cpu_needs_work=idle_cpu_needs_work,
            idle_gpu_needs_work=idle_gpu_needs_work,
            idle_cpu=int(
                governor.get("decision_idle_cpu_slaves")
                or idle_decision_count(
                    governor.get("sustained_idle_cpu_slaves"),
                    governor.get("online_idle_cpu_slaves"),
                )
            ),
            claimable_cpu=int(governor.get("cpu_unassigned_claimable") or 0),
            idle_gpu=int(governor.get("online_idle_gpu_slaves") or 0),
            claimable_gpu=int(governor.get("gpu_unassigned_claimable") or 0),
            cpu_want_spare=cpu_want_spare,
            cpu_unowned=int(governor.get("unowned_cpu_root_jobs") or 0),
            gpu_want_spare=gpu_want_spare,
            gpu_unowned=int(governor.get("unowned_gpu_root_jobs") or 0),
            base_burst=int(os.environ.get("PRECOMMIT_IDLE_BURST", "4")),
            max_burst=int(os.environ.get("PRECOMMIT_IDLE_BURST_MAX", "16")),
            cpu_unassigned_remaining=max(
                0,
                int(caps.get("cpu_unassigned_cap") or 0)
                - int(governor.get("cpu_unassigned_claimable") or 0),
            ),
            gpu_unassigned_remaining=max(
                0,
                int(caps.get("gpu_unassigned_cap") or 0)
                - int(governor.get("gpu_unassigned_claimable") or 0),
            ),
            cpu_online=int(governor.get("online_cpu_slaves") or 0),
            gpu_online=int(governor.get("online_gpu_slaves") or 0),
        )
        self.last_sized_burst = self.last_idle_burst
        if self.last_idle_burst > 1:
            logger.info(
                "idle create sized burst=%s instant_cpu=%s sustained_cpu=%s "
                "decision_cpu=%s claimable_cpu=%s unowned_cpu=%s want_cpu=%s "
                "idle_gpu=%s claimable_gpu=%s unowned_gpu=%s want_gpu=%s "
                "cap=%s effective=%s root=%s proof=%s",
                self.last_idle_burst,
                governor.get("online_idle_cpu_slaves_instant")
                or governor.get("online_idle_cpu_slaves"),
                governor.get("sustained_idle_cpu_slaves"),
                governor.get("decision_idle_cpu_slaves"),
                governor.get("cpu_unassigned_claimable"),
                governor.get("unowned_cpu_root_jobs"),
                cpu_want_spare,
                governor.get("online_idle_gpu_slaves"),
                governor.get("gpu_unassigned_claimable"),
                governor.get("unowned_gpu_root_jobs"),
                gpu_want_spare,
                configured_cap,
                create_cap,
                root_phase_jobs,
                proof_phase_jobs,
            )
        if not concurrent_create_allowed(
            root_phase_jobs=root_phase_jobs,
            proof_phase_jobs=proof_phase_jobs,
            submitted=self.num_precommits_submitted,
            max_concurrent=create_cap,
            overlap_cap=overlap_cap,
        ):
            logger.info(
                "pending benchmarks at cap (pending=%s root=%s proof=%s "
                "submitted=%s max=%s effective=%s overlap=%s idle_cpu=%s idle_gpu=%s)",
                num_pending_jobs,
                root_phase_jobs,
                proof_phase_jobs,
                self.num_precommits_submitted,
                configured_cap,
                create_cap,
                overlap_cap,
                idle_cpu_needs_work,
                idle_gpu_needs_work,
            )
            if not (idle_cpu_needs_work or idle_gpu_needs_work):
                self.last_idle_cpu_needs_work = False
                self.last_idle_gpu_needs_work = False
            return
        governor_reason = ""
        profile_blocks = governor.get("profile_blocks") or {"cpu": False, "gpu": False}
        if governor.get("enabled"):
            block, governor_reason = should_block_precommit_create(
                governor.get("roots_pending") or 0,
                governor.get("benchmarks_seen") or 0,
                governor.get("root_ready_benchmarks") or 0,
                governor.get("settings"),
                idle_cpu_needs_work=idle_cpu_needs_work,
            )
            if block:
                if idle_cpu_needs_work or idle_gpu_needs_work:
                    logger.info(
                        "precommit governor would block (%s); idle override continuing",
                        governor_reason,
                    )
                else:
                    logger.info("precommit governor blocked create: %s", governor_reason)
                    self.last_idle_cpu_needs_work = False
                    self.last_idle_gpu_needs_work = False
                    return
            if governor_reason.startswith("idle_cpu_override:"):
                logger.info(
                    "precommit governor allowing create via idle CPU override "
                    "(cpu_jobs_needing_roots=%s/%s create_target=%s, "
                    "cpu_active_jobs=%s proof_phase=%s, cpu_unassigned_roots=%s): %s",
                    governor.get("cpu_jobs_needing_roots"),
                    governor.get("cpu_slots"),
                    governor.get("cpu_create_target"),
                    governor.get("cpu_active_jobs"),
                    governor.get("cpu_jobs_in_proof_phase"),
                    governor.get("cpu_unassigned_roots"),
                    governor_reason,
                )
            caps = governor.get("profile_caps") or {}
            if profile_blocks.get("cpu") or profile_blocks.get("gpu"):
                logger.info(
                    "precommit governor profile backlog "
                    "(cpu_pending=%s/%s claimable_unassigned=%s/%s raw_unassigned=%s "
                    "block=%s reasons=%s; "
                    "gpu_pending=%s/%s claimable_unassigned=%s/%s raw_unassigned=%s "
                    "block=%s reasons=%s)",
                    governor.get("cpu_roots_pending"),
                    caps.get("cpu_pending_cap"),
                    governor.get("cpu_unassigned_claimable"),
                    caps.get("cpu_unassigned_cap"),
                    governor.get("cpu_unassigned_roots"),
                    profile_blocks.get("cpu"),
                    profile_blocks.get("cpu_reasons"),
                    governor.get("gpu_roots_pending"),
                    caps.get("gpu_pending_cap"),
                    governor.get("gpu_unassigned_claimable"),
                    caps.get("gpu_unassigned_cap"),
                    governor.get("gpu_unassigned_roots"),
                    profile_blocks.get("gpu"),
                    profile_blocks.get("gpu_reasons"),
                )

        # Build per-challenge pending counts keyed by challenge_id (e.g. "c004")
        per_challenge_counts = {}
        root_phase_counts = {}
        rows = get_db_conn().fetch_all(
            """
            SELECT
                settings->>'challenge_id' AS challenge_id,
                COUNT(*) AS cnt,
                COUNT(*) FILTER (WHERE merkle_root_ready IS NULL) AS root_cnt
            FROM job
            WHERE merkle_proofs_ready IS NULL
                AND stopped IS NULL
            GROUP BY settings->>'challenge_id'
            """
        )
        for row in rows:
            per_challenge_counts[row["challenge_id"]] = row["cnt"]
            root_phase_counts[row["challenge_id"]] = row["root_cnt"]

        per_challenge_max = CONFIG.get("per_challenge_max_benchmarks", {})
        idle_gpu_needs_work = bool(governor.get("idle_gpu_needs_work"))
        idle_gpu_starved = bool(governor.get("idle_gpu_starved"))
        gpu_keep_ahead = bool(governor.get("gpu_keep_ahead"))
        idle_gpu_slaves = int(governor.get("online_idle_gpu_slaves") or 0)
        gpu_spare_jobs = int((governor.get("settings") or {}).get("gpu_spare_jobs") or 0)
        idle_cpu_slaves = int(
            governor.get("decision_idle_cpu_slaves")
            or idle_decision_count(
                governor.get("sustained_idle_cpu_slaves"),
                governor.get("online_idle_cpu_slaves"),
            )
        )

        # Filter eligible algorithms (not over their per-challenge limit).
        # Empty GPU cards: proof-phase jobs do not count against GPU caps, and
        # the cap lifts so those cards are not starved onto CPU. Keep-ahead
        # only adds the spare count — it must not copy the idle-card lift.
        # Idle CPUs: same root-phase rule, plus a +1..2 lift so a drained
        # autopilot per-challenge cap cannot freeze the fleet.
        eligible = [
            x for x in algo_selection
            if challenge_under_create_cap(
                x["algorithm_id"][:4],
                pending_counts=per_challenge_counts,
                root_phase_counts=root_phase_counts,
                submitted=self.per_challenge_precommits_submitted,
                per_challenge_max=per_challenge_max,
                idle_gpu_starved=idle_gpu_starved,
                gpu_keep_ahead=gpu_keep_ahead,
                gpu_spare_jobs=gpu_spare_jobs,
                idle_gpu_slaves=idle_gpu_slaves,
                idle_cpu_needs_work=idle_cpu_needs_work,
                idle_cpu_slaves=idle_cpu_slaves,
            )
        ]
        # TIG hygiene: skip banned / not-yet-active / failed-binary algorithms.
        algorithms = getattr(self, "_algorithms", None)
        binarys = getattr(self, "_binarys", None)
        block_round = getattr(self, "_block_round", None)
        if algorithms is not None or binarys is not None:
            kept = []
            for x in eligible:
                ok, reason = algo_is_schedulable(
                    x["algorithm_id"],
                    algorithms=algorithms,
                    binarys=binarys,
                    block_round=block_round,
                )
                if ok:
                    kept.append(x)
                else:
                    logger.info(
                        "skipping algorithm %s for precommit: %s",
                        x.get("algorithm_id"),
                        reason,
                    )
            eligible = kept
        if not eligible:
            logger.info(
                "all algorithms at per-challenge max "
                "(pending=%s root=%s submitted=%s caps=%s "
                "idle_cpu=%s idle_cpu_slaves=%s idle_gpu=%s)",
                per_challenge_counts,
                root_phase_counts,
                dict(self.per_challenge_precommits_submitted or {}),
                per_challenge_max,
                idle_cpu_needs_work,
                idle_cpu_slaves,
                idle_gpu_needs_work,
            )
            return

        # Profile backlog: block only the saturated profile so fat GPU/CPU root
        # piles cannot freeze creates for the other profile.
        if profile_blocks.get("cpu") or profile_blocks.get("gpu"):
            filtered = []
            for x in eligible:
                cid = x["algorithm_id"][:4]
                if cid in CPU_CHALLENGE_IDS and profile_blocks.get("cpu"):
                    continue
                if cid in GPU_CHALLENGE_IDS and profile_blocks.get("gpu"):
                    continue
                filtered.append(x)
            if not filtered:
                logger.info(
                    "precommit governor: both profiles blocked by root backlog "
                    "(cpu=%s gpu=%s)",
                    profile_blocks.get("cpu_reasons"),
                    profile_blocks.get("gpu_reasons"),
                )
                return
            eligible = filtered

        # Idle CPU: bias toward CPU creates. Do NOT treat gpu_slot_floor as
        # "need N GPU precommits". Proof-phase GPU jobs also do not feed idle
        # GPUs — those need a new root job, not leftovers from a proving box.
        gpu_floor = int(governor.get("gpu_slot_floor") or _gpu_slot_floor_total())
        gpu_active_jobs = int(governor.get("gpu_active_jobs") or 0)
        if gpu_active_jobs <= 0:
            gpu_active_jobs = sum(
                int(per_challenge_counts.get(cid, 0) or 0) for cid in GPU_CHALLENGE_IDS
            )
        gpu_below_floor = gpu_active_jobs < max(1, gpu_floor)
        gpu_starved = gpu_active_jobs <= 0
        now_ms = int(time.time() * 1000)
        if now_ms - int(getattr(self, "_idle_gpu_reserve_tick_ms", 0) or 0) > 4000:
            self._idle_gpu_reserved_count = 0
            self._idle_gpu_reserve_tick_ms = now_ms
        gpu_claimable = int(governor.get("gpu_unassigned_claimable") or 0)
        gpu_wave = 0
        if idle_gpu_starved:
            gpu_wave = empty_claimable_wave(
                base_burst=int(os.environ.get("PRECOMMIT_IDLE_BURST", "4")),
                hi=scaled_idle_burst_max(
                    base_burst=int(os.environ.get("PRECOMMIT_IDLE_BURST", "4")),
                    max_burst=int(os.environ.get("PRECOMMIT_IDLE_BURST_MAX", "16")),
                    online=int(governor.get("online_gpu_slaves") or idle_gpu_slaves or 0),
                    want=max(
                        0,
                        idle_gpu_slaves - gpu_claimable,
                        gpu_want_spare,
                    ),
                ),
                want=max(0, idle_gpu_slaves - gpu_claimable, gpu_want_spare),
            )
        reserved_so_far = int(getattr(self, "_idle_gpu_reserved_count", 0) or 0)
        reserve_gpu = should_reserve_idle_gpu_create(
            idle_gpu_needs_work=bool(idle_gpu_starved or idle_gpu_needs_work),
            last_create_ms=int(getattr(self, "_idle_gpu_create_ms", 0) or 0),
            now_ms=now_ms,
            skip_cooldown=bool(idle_gpu_starved and reserved_so_far < max(1, gpu_wave)),
        )
        if idle_gpu_starved and reserved_so_far >= max(1, gpu_wave):
            reserve_gpu = False
        force_cpu_only = should_force_cpu_only(
            idle_cpu_needs_work=idle_cpu_needs_work,
            gpu_starved=gpu_starved,
            idle_gpu_starved=idle_gpu_starved,
            cpu_profile_blocked=bool(profile_blocks.get("cpu")),
        )
        eligible_before_reserve = list(eligible)
        if reserve_gpu:
            gpu_eligible = [
                x for x in eligible
                if x["algorithm_id"][:4] in GPU_CHALLENGE_IDS
            ]
            if gpu_eligible and has_positive_weight_for_profile(
                gpu_eligible, GPU_CHALLENGE_IDS
            ):
                eligible = gpu_eligible
                force_cpu_only = False
                logger.info(
                    "precommit governor reserving GPU create for idle GPUs "
                    "(root_phase=%s pending=%s)",
                    {cid: root_phase_counts.get(cid, 0) for cid in GPU_CHALLENGE_IDS},
                    {cid: per_challenge_counts.get(cid, 0) for cid in GPU_CHALLENGE_IDS},
                )
            else:
                if gpu_eligible:
                    logger.info(
                        "precommit governor skipped GPU reserve: idle GPUs need "
                        "work but GPU algorithm weights are 0; keeping CPU algorithms"
                    )
                reserve_gpu = False
        # Empty GPU cards: one create per cooldown is reserve_gpu above.
        # Never abort the CPU burst because a GPU challenge is at cap.
        if idle_gpu_starved:
            gpu_left = [
                x for x in eligible
                if x["algorithm_id"][:4] in GPU_CHALLENGE_IDS
            ]
            if not gpu_left or not has_positive_weight_for_profile(
                gpu_left, GPU_CHALLENGE_IDS
            ):
                logger.info(
                    "idle GPUs need work but no GPU algorithm is eligible; "
                    "continuing CPU create (root_phase=%s pending=%s)",
                    {cid: root_phase_counts.get(cid, 0) for cid in GPU_CHALLENGE_IDS},
                    {cid: per_challenge_counts.get(cid, 0) for cid in GPU_CHALLENGE_IDS},
                )
        if force_cpu_only:
            cpu_eligible = [
                x for x in eligible
                if x["algorithm_id"][:4] in CPU_CHALLENGE_IDS
            ]
            if cpu_eligible:
                eligible = cpu_eligible
            else:
                force_cpu_only = False
                logger.info(
                    "idle CPU override active but no CPU algorithms eligible; "
                    "allowing normal selection"
                )

        weighted_eligible = []
        weights = []
        idle_mult = float((governor.get("settings") or {}).get("idle_cpu_weight_mult") or 3)
        cap_settings = capability_settings(CONFIG)
        cap_views = {}
        if cap_settings.get("enabled"):
            try:
                CAPABILITY_SCHEDULER.set_tig_context(
                    tracks_data=getattr(self, "_tracks_data", None),
                    algorithms=getattr(self, "_algorithms", None),
                    binarys=getattr(self, "_binarys", None),
                    block_round=getattr(self, "_block_round", None),
                )
                cap_views = CAPABILITY_SCHEDULER.refresh_runtime_views(
                    fetch_all=get_db_conn().fetch_all,
                    execute=get_db_conn().execute,
                    config=CONFIG,
                )
            except Exception as exc:
                logger.debug("capability views for precommit failed: %s", exc)
        for x in eligible:
            weight = int(x.get("weight") or 0)
            if weight <= 0:
                continue
            # Boost CPU whenever the idle fleet needs work. Do not use elif with
            # gpu_below_floor — that inverted intent and boosted only GPU when
            # both pressures were active (common at low open_jobs).
            if idle_cpu_needs_work and not force_cpu_only:
                if x["algorithm_id"][:4] in CPU_CHALLENGE_IDS:
                    weight = max(1, int(round(weight * idle_mult)))
            if (gpu_starved or idle_gpu_starved) and x["algorithm_id"][:4] in GPU_CHALLENGE_IDS:
                weight = max(1, int(round(weight * idle_mult)))
            if cap_settings.get("enabled"):
                cid = x["algorithm_id"][:4]
                # GPU creates are governed by GPU slots/floor, not CPU L/XL census.
                if cid in GPU_CHALLENGE_IDS:
                    pass
                else:
                    try:
                        hardness = CAPABILITY_SCHEDULER.max_algo_track_hardness(x)
                    except Exception:
                        hardness = heuristic_track_hardness(cid, None)
                    inventory_known = bool((cap_views or {}).get("inventory_known"))
                    mult = precommit_hardness_weight_mult(
                        hardness=hardness,
                        hard_hardness=cap_settings["hard_hardness"],
                        strong_online=int((cap_views or {}).get("strong_online") or 0),
                        hard_open_roots=int((cap_views or {}).get("hard_open_roots") or 0),
                        hard_open_per_strong=cap_settings["hard_open_per_strong"],
                        inventory_known=inventory_known,
                    )
                    if not inventory_known:
                        logger.info(
                            "capability throttle fail-open algo=%s "
                            "(no measured/declared cores on online CPUs; "
                            "online_cpu=%s core_info=%s)",
                            x.get("algorithm_id"),
                            (cap_views or {}).get("online_cpu"),
                            (cap_views or {}).get("online_with_core_info"),
                        )
                    elif mult < 1.0:
                        logger.info(
                            "capability throttle algo=%s hardness=%.2f mult=%.2f "
                            "strong_online=%s hard_open=%s inventory_known=%s",
                            x.get("algorithm_id"),
                            hardness,
                            mult,
                            (cap_views or {}).get("strong_online"),
                            (cap_views or {}).get("hard_open_roots"),
                            inventory_known,
                        )
                    weight = max(1, int(round(weight * mult))) if mult > 0 else 0
                    if weight <= 0:
                        continue
            weighted_eligible.append(x)
            weights.append(weight)
        if not weighted_eligible and reserve_gpu:
            logger.info(
                "GPU reserve produced no creatable algorithm; falling back to CPU "
                "(root_phase=%s pending=%s)",
                {cid: root_phase_counts.get(cid, 0) for cid in GPU_CHALLENGE_IDS},
                {cid: per_challenge_counts.get(cid, 0) for cid in GPU_CHALLENGE_IDS},
            )
            self._idle_gpu_reserved_count = max(
                int(getattr(self, "_idle_gpu_reserved_count", 0) or 0) + 1,
                int(gpu_wave or 0),
            )
            reserve_gpu = False
            force_cpu_only = False
            eligible = [
                x for x in eligible_before_reserve
                if x["algorithm_id"][:4] in CPU_CHALLENGE_IDS
            ] or list(eligible_before_reserve)
            for x in eligible:
                weight = int(x.get("weight") or 0)
                if weight <= 0:
                    continue
                if idle_cpu_needs_work and x["algorithm_id"][:4] in CPU_CHALLENGE_IDS:
                    weight = max(1, int(round(weight * idle_mult)))
                weighted_eligible.append(x)
                weights.append(weight)
        if not weighted_eligible:
            logger.info(
                "precommit create skipped: no positive-weight algorithms after "
                "filters (eligible=%s reserve_gpu=%s force_cpu_only=%s)",
                [(x.get("algorithm_id"), x.get("weight")) for x in eligible],
                reserve_gpu,
                force_cpu_only,
            )
            return

        logger.debug(
            "Selecting algorithm from: %s idle_cpu=%s idle_gpu=%s gpu_below_floor=%s force_cpu_only=%s",
            list(zip([x["algorithm_id"] for x in weighted_eligible], weights)),
            idle_cpu_needs_work,
            idle_gpu_needs_work,
            gpu_below_floor,
            force_cpu_only,
        )
        # Deep copy so mutations below (stripping unknown keys, filling defaults)
        # don't corrupt the live CONFIG["algo_selection"] — especially batch_size
        # which lives in track_settings but must not be sent to mainnet.
        selection = copy.deepcopy(random.choices(weighted_eligible, weights=weights)[0])  # nosec B311 — weighted algorithm selection, not cryptographic
        a_id = selection["algorithm_id"]
        c_id = a_id[:4]
        compute_type = selection.get("compute_type")
        if not compute_type:
            logger.error(f"Selected algorithm '{a_id}' is missing required compute_type")
            return
        if c_id not in self.challenge_configs:
            logger.error(f"Invalid selected challenge_id '{c_id}'. Valid challenge_ids: {sorted(self.challenge_configs)}")
            return
        challenge_config = self.challenge_configs[c_id]
        _CHALLENGE_NAMES = {
            "c001": "satisfiability", "c002": "vehicle_routing", "c003": "knapsack",
            "c004": "vector_search",  "c005": "hypergraph",      "c006": "neuralnet_optimizer",
            "c007": "job_scheduling", "c008": "energy_arbitrage",
        }
        _allowlist = CONFIG.get("track_allowlist", {})
        _allowed = _allowlist.get(_CHALLENGE_NAMES.get(c_id, ""), None)

        _track_algo_map = CONFIG.get("track_algorithm_map", {}).get(_CHALLENGE_NAMES.get(c_id, ""), {})

        # Remove tracks no longer active on mainnet
        for t_id in set(selection["track_settings"]) - set(challenge_config["active_tracks"]):
            selection["track_settings"].pop(t_id)
        # ALL active tracks must be in the precommit (TIG API requirement).
        # Tracks not in the allowlist get {} so master uses min_num_bundles (minimal compute).
        # Tracks in the allowlist keep their configured settings.
        # Tracks pinned (via track_algorithm_map) to a DIFFERENT algorithm also get {}:
        # job_manager will stop this job anyway if it lands there, so there's no point
        # requesting anything beyond the on-chain minimum.
        for t_id in challenge_config["active_tracks"]:
            if t_id not in selection["track_settings"]:
                selection["track_settings"][t_id] = {}
            if _allowed is not None and t_id not in _allowed:
                selection["track_settings"][t_id] = {}
            _pinned = _track_algo_map.get(t_id)
            if _pinned is not None and _pinned != a_id:
                selection["track_settings"][t_id] = {}

        for t_id in set(challenge_config["active_tracks"]):
            for k in set(selection["track_settings"][t_id]) - {"num_bundles", "hyperparameters", "fuel_budget"}:
                selection["track_settings"][t_id].pop(k)
            if selection["track_settings"][t_id].get("num_bundles", 0) < challenge_config["min_num_bundles"]:
                selection["track_settings"][t_id]["num_bundles"] = challenge_config["min_num_bundles"]
            if (
                selection["track_settings"][t_id].get("fuel_budget") is None or 
                selection["track_settings"][t_id]["fuel_budget"] < 0 or
                selection["track_settings"][t_id]["fuel_budget"] > challenge_config["max_fuel_budget"]
            ):
                selection["track_settings"][t_id]["fuel_budget"] = challenge_config["max_fuel_budget"]
            if "hyperparameters" not in selection["track_settings"][t_id]:
                selection["track_settings"][t_id]["hyperparameters"] = None

        self.num_precommits_submitted += 1
        self.per_challenge_precommits_submitted[c_id] = self.per_challenge_precommits_submitted.get(c_id, 0) + 1
        req = SubmitPrecommitRequest(
            settings=BenchmarkSettings(
                challenge_id=c_id,
                algorithm_id=a_id,
                player_id=CONFIG["player_id"],
                block_id=self.last_block_id,
                track_id="",
            ),
            track_settings=selection["track_settings"],
            compute_type=compute_type,
        )
        if reserve_gpu and c_id in GPU_CHALLENGE_IDS:
            self._idle_gpu_create_ms = int(time.time() * 1000)
            self._idle_gpu_reserved_count = int(
                getattr(self, "_idle_gpu_reserved_count", 0) or 0
            ) + 1
        logger.info(f"Created precommit with algorithm: {a_id}")
        return req
