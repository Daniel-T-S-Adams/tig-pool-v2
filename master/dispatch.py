"""Per-profile create-when-short + pin policy.

TIG lands ~2.4 precommits/min. Keep any fleet busy by covering online boxes
with matching-profile batches, not by bursting creates or warehousing the
other profile.
"""

from __future__ import annotations

# Confirmed jobs still in flight to cover TIG confirm lag (~1-2 min at ~2.4/min).
NEXT_JOB_BUFFER = 1
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
    one unowned job in the pipeline so the next batch is ready after TIG
    confirms. Proving-job keep-ahead is not a create lock.
    """
    idle_n = max(0, int(idle or 0))
    claimable_n = max(0, int(claimable or 0))
    if idle_n > 0 and claimable_n < idle_n:
        return True
    return int(unowned_jobs or 0) < max(0, int(next_job_buffer or 0))


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


def extra_create_this_tick(*, cpu_short: bool, gpu_short: bool) -> int:
    """At most one extra create, and only when both profiles are short."""
    return 1 if (cpu_short and gpu_short) else 0


def pin_limit(*, num_batches: int, idle_boxes: int) -> int:
    """Pin new batches onto idle boxes; leave leftovers claimable.

    Never invent extra batches. Never pin more names than idle boxes.
    """
    return max(0, min(int(num_batches or 0), int(idle_boxes or 0)))


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
