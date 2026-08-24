"""Per-profile create-when-short + pin policy.

TIG lands ~2.4 precommits/min. Keep any fleet busy by covering online boxes
with matching-profile batches, not by bursting creates or warehousing the
other profile.
"""

from __future__ import annotations

from master.cpu_tier_caps import should_hold_leftover_for_xl  # noqa: F401

# Confirmed jobs still in flight to cover TIG confirm lag (~1-2 min at ~2.4/min).
# One unowned job is ~one box-wave, not 37 idle CPUs. Two ready is enough
# to keep the next box busy without doubling into TIG's 100-cap.
NEXT_JOB_BUFFER = 2
PIN_EXPIRE_MS = 30_000


def profile_needs_create(
    *,
    idle: int = 0,
    claimable: int = 0,
    unowned_jobs: int = 0,
    next_job_buffer: int = NEXT_JOB_BUFFER,
) -> bool:
    """True when this profile should receive the next precommit.

    Empty boxes with nothing to claim are short. A busy fleet still wants
    a few unowned jobs in the pipeline so the next wave can pull after TIG
    confirms. Proving-job keep-ahead is not a create lock.
    """
    idle_n = max(0, int(idle or 0))
    claimable_n = max(0, int(claimable or 0))
    buf = max(0, int(next_job_buffer or 0))
    if idle_n > 0 and claimable_n < idle_n:
        return True
    if leftovers_cover_spare(claimable=claimable_n, spare=buf):
        return False
    return int(unowned_jobs or 0) < buf


def profile_has_hole(*, idle: int = 0, claimable: int = 0) -> bool:
    """True when idle boxes of this profile have nothing they can pull."""
    idle_n = max(0, int(idle or 0))
    return idle_n > 0 and max(0, int(claimable or 0)) < idle_n


def leftover_food(*, claimable: int = 0, unassigned: int = 0) -> int:
    """Sticky leftovers still feed boxes. Claimable-only hid that pile."""
    return max(0, int(claimable or 0), int(unassigned or 0))


def leftovers_cover_spare(*, claimable: int = 0, spare: int = NEXT_JOB_BUFFER) -> bool:
    """True when sitting leftovers already are the keep-ahead pile.

    Keep-ahead counted unowned jobs only. Sticky leftovers then looked
    like an empty warehouse, so creates kept minting into 1600 unassigned.
    """
    return max(0, int(claimable or 0)) > max(0, int(spare or 0))


def dispatch_shorts(
    *,
    cpu_idle: int = 0,
    cpu_claimable: int = 0,
    cpu_unowned: int = 0,
    gpu_idle: int = 0,
    gpu_claimable: int = 0,
    gpu_unowned: int = 0,
    next_job_buffer: int = NEXT_JOB_BUFFER,
    allow_keep_ahead: bool = True,
) -> tuple[bool, bool]:
    """CPU/GPU short flags for this tick.

    An idle hole always wins. Keep-ahead (unowned < buffer) only runs on a
    busy profile when the other profile has no hole. Otherwise 100% busy
    CPUs keep taking creates while GPUs sit empty.

    When the parked job cap is already exceeded, keep-ahead is off so the
    warehouse can drain.
    """
    cpu_hole = profile_has_hole(idle=cpu_idle, claimable=cpu_claimable)
    gpu_hole = profile_has_hole(idle=gpu_idle, claimable=gpu_claimable)
    if not allow_keep_ahead:
        return cpu_hole, gpu_hole
    buf = max(0, int(next_job_buffer or 0))
    cpu_ahead = (
        (not cpu_hole)
        and int(cpu_unowned or 0) < buf
        and not leftovers_cover_spare(claimable=cpu_claimable, spare=buf)
    )
    gpu_ahead = (
        (not gpu_hole)
        and int(gpu_unowned or 0) < buf
        and not leftovers_cover_spare(claimable=gpu_claimable, spare=buf)
    )
    cpu_short = cpu_hole or (cpu_ahead and not gpu_hole)
    gpu_short = gpu_hole or (gpu_ahead and not cpu_hole)
    return cpu_short, gpu_short


def next_create_profile(
    *,
    cpu_short: bool,
    gpu_short: bool,
    last_profile: str = "",
) -> str:
    """Which profile gets this create, or '' to skip.

    Both short → alternate so neither starves. Last GPU → CPU next, and
    the other way around. Empty last defaults to CPU when both are short.
    """
    cpu = bool(cpu_short)
    gpu = bool(gpu_short)
    if cpu and gpu:
        if str(last_profile or "") == "cpu":
            return "gpu"
        return "cpu"
    if cpu:
        return "cpu"
    if gpu:
        return "gpu"
    return ""


def next_hole_profile(
    *,
    cpu_hole: bool,
    gpu_hole: bool,
    cpu_idle: int = 0,
    cpu_claimable: int = 0,
    gpu_idle: int = 0,
    gpu_claimable: int = 0,
    cpu_short: bool = False,
    gpu_short: bool = False,
    last_profile: str = "",
) -> str:
    """One TIG precommit slot when a profile has a hole.

    Do not compare CPU empty seats to GPU cards. After XL boxes joined,
    65 CPU seats always beat 16 idle T4s and every create locked to CPU.
    Both holes alternate, same as both-short.
    """
    del cpu_idle, cpu_claimable, gpu_idle, gpu_claimable
    if cpu_hole and gpu_hole:
        return next_create_profile(
            cpu_short=True,
            gpu_short=True,
            last_profile=last_profile,
        )
    if cpu_hole:
        return "cpu"
    if gpu_hole:
        return "gpu"
    return next_create_profile(
        cpu_short=cpu_short,
        gpu_short=gpu_short,
        last_profile=last_profile,
    )


def extra_create_this_tick(*, cpu_short: bool, gpu_short: bool) -> int:
    """At most one extra create, and only when both profiles are short."""
    return 1 if (cpu_short and gpu_short) else 0


def creates_for_profile(
    *,
    has_hole: bool,
    is_short: bool,
    idle: int = 0,
    claimable: int = 0,
    n_algos: int = 1,
) -> int:
    """How many distinct-algorithm creates this tick for one profile.

    A hole is covered with one create per algorithm, capped by the idle
    deficit. Keep-ahead without a hole stays a single create. Repeating
    the same algorithm in the same block 400s; this does not burst clones.
    """
    if has_hole:
        deficit = max(1, int(idle or 0) - max(0, int(claimable or 0)))
        return max(1, min(max(1, int(n_algos or 1)), deficit))
    if is_short:
        return 1
    return 0


def lock_eligible_algorithms(eligible, *, profile: str, cpu_ids, gpu_ids):
    """Keep only algorithms for a forced create profile.

    Returns [] when nothing matches so the caller can fail open.
    """
    wanted = str(profile or "")
    if wanted == "gpu":
        ids = set(gpu_ids or ())
    elif wanted == "cpu":
        ids = set(cpu_ids or ())
    else:
        return list(eligible or [])
    return [
        x
        for x in (eligible or [])
        if str((x or {}).get("algorithm_id") or "")[:4] in ids
    ]


def pin_limit(*, num_batches: int, idle_boxes: int = 0, empty_seats: int | None = None) -> int:
    """Pin new batches onto empty seats; leave leftovers claimable.

    Never invent extra batches. Seat count wins over hostname count so
    one idle EPYC (4 seats) pins like four idle Picas.
    """
    seats = int(idle_boxes or 0) if empty_seats is None else int(empty_seats or 0)
    return max(0, min(int(num_batches or 0), seats))


def pin_targets(*, boxes, num_batches: int):
    """Expand ``(slave, seats)`` into ``(batch_idx, slave)`` pins.

    Same seat total → same pin count, whether those seats live on one
    XL box or many S/M boxes.
    """
    out = []
    idx = 0
    limit = max(0, int(num_batches or 0))
    for item in boxes or []:
        if idx >= limit:
            break
        if isinstance(item, dict):
            name = str(item.get("slave_name") or item.get("name") or "")
            try:
                seats = int(item.get("empty_seats") or item.get("seats") or 1)
            except (TypeError, ValueError):
                seats = 1
        else:
            try:
                name = str(item[0])
                seats = int(item[1]) if len(item) > 1 else 1
            except (TypeError, ValueError, IndexError):
                continue
        if not name:
            continue
        take = min(max(0, seats), limit - idx)
        for _ in range(take):
            out.append((idx, name))
            idx += 1
    return out


def pin_expired(*, now_ms: int, pinned_at_ms: int, expire_ms: int = PIN_EXPIRE_MS) -> bool:
    """True when a pin was not picked up in time and must move."""
    now = int(now_ms or 0)
    pinned = int(pinned_at_ms or 0)
    if now <= 0 or pinned <= 0:
        return False
    return (now - pinned) >= max(1, int(expire_ms or PIN_EXPIRE_MS))


def slave_work_profile(slave_name: str) -> str:
    """CPU vs GPU from live naming. Real GPUs are pool-gpu-* only."""
    name = str(slave_name or "")
    if name.startswith("pool-gpu-"):
        return "gpu"
    if name.startswith("pool-cpu-") or name.startswith("aws-cpu-slave-") or name.startswith("c3-slave-"):
        return "cpu"
    if name:
        return "cpu"
    return ""
