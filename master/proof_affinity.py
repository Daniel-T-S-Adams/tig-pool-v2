"""Sticky root affinity + slave liveness helpers for reliable proof completion.

Proofs can only be built by the slave that holds local root artifacts. Spreading
one job's roots across many pool CPUs makes proofs fail when a member box goes
dark. These helpers:

1. Prefer keeping a job's roots on one live slave (sticky affinity).
2. Track slave liveness via get-batches / submit heartbeats (slave_seen).
3. Reclaim unfinished roots from dark owners. Never wipe ready roots —
   merkle is assembled from batch_data already in Postgres.
4. Support stopping jobs whose proofs are stranded on offline artifact owners.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Callable, Dict, Iterable, Optional, Set

logger = logging.getLogger(os.path.splitext(os.path.basename(__file__))[0])
_SLAVE_SEEN_READY = False
_SLAVE_SEEN_LOCK = threading.Lock()
_SLAVE_SEEN_STATEMENTS = (
    """
        CREATE TABLE IF NOT EXISTS slave_seen (
            slave_name TEXT PRIMARY KEY,
            last_seen BIGINT NOT NULL
        )
        """,
    "CREATE INDEX IF NOT EXISTS idx_slave_seen_last_seen ON slave_seen(last_seen)",
    "ALTER TABLE slave_seen ADD COLUMN IF NOT EXISTS num_workers INTEGER",
    "ALTER TABLE slave_seen ADD COLUMN IF NOT EXISTS telem_state TEXT",
    "ALTER TABLE slave_seen ADD COLUMN IF NOT EXISTS telem_active INTEGER",
    "ALTER TABLE slave_seen ADD COLUMN IF NOT EXISTS telem_cores INTEGER",
)

# pool-cpu6a10… / pool-gpu6a10… (missing hyphen after cpu|gpu).
_POOL_NAME_MISSING_HYPHEN = re.compile(
    r"^(pool-(?:cpu|gpu))([0-9a-f]{12}\b.*)$",
    re.IGNORECASE,
)


def canonicalize_pool_slave_name(name: Optional[str]) -> Optional[str]:
    """Insert the hyphen in ``pool-cpu<hex>…`` / ``pool-gpu<hex>…`` typos."""
    if name is None:
        return None
    raw = str(name).strip()
    if not raw:
        return raw
    match = _POOL_NAME_MISSING_HYPHEN.match(raw)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return raw


def _env_bool(name: str, default: str = "true") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes", "on")


STICKY_ROOTS_ENABLED = _env_bool("SLAVE_STICKY_ROOTS_ENABLED", "true")
SLAVE_ONLINE_MS = max(5_000, int(os.environ.get("SLAVE_ONLINE_MS", str(2 * 60 * 1000))))
PRE_SUBMIT_OWNER_ONLINE_MS = max(
    SLAVE_ONLINE_MS,
    int(os.environ.get("SLAVE_PRE_SUBMIT_OWNER_ONLINE_MS", str(SLAVE_ONLINE_MS))),
)
STRANDED_PROOF_STOP_ENABLED = _env_bool("SLAVE_STRANDED_PROOF_STOP_ENABLED", "true")
STRANDED_PROOF_STOP_MS = max(
    60_000,
    int(os.environ.get("SLAVE_STRANDED_PROOF_STOP_MS", str(15 * 60 * 1000))),
)
# OFF by default. A 3-minute reserve parked every finished 32-thread box
# empty while TIG sampled, which dropped fleet throughput. Set >0 only as
# an emergency brake. This is not SAMPLING_GAP_LOCK.
SAMPLING_GAP_RESERVE_MS = max(
    0, int(os.environ.get("SLAVE_SAMPLING_GAP_RESERVE_MS", "0"))
)


def job_counts_as_sampling_gap_reserve(
    *,
    has_unfinished_roots: bool = False,
    merkle_proofs_ready: bool = False,
    proof_batch_count: int = 0,
    owner_has_ready_root: bool = False,
    latest_ready_root_age_ms: Optional[int] = None,
    reserve_ms: int = SAMPLING_GAP_RESERVE_MS,
    stopped: bool = False,
) -> bool:
    """True when this owned job still owes a seat for TIG sampling."""
    if stopped or merkle_proofs_ready or has_unfinished_roots:
        return False
    if int(proof_batch_count or 0) > 0:
        return False
    if not owner_has_ready_root:
        return False
    if int(reserve_ms or 0) <= 0:
        return False
    if latest_ready_root_age_ms is None:
        return True
    try:
        return int(latest_ready_root_age_ms) <= int(reserve_ms)
    except (TypeError, ValueError):
        return True


def sampling_gap_root_intake_cap(
    *,
    max_concurrent: int,
    assigned: int,
    gap_jobs: int,
    keep_last_seat: bool = False,
) -> int:
    """Max in-flight rows allowed for new roots while seats wait for samples.

    Other in-flight jobs keep running. Proofs may still fill up to max_concurrent.
    An idle GPU (assigned=0) must keep one leftover seat: 1-wide cards
    otherwise reserve their only slot for TIG sampling and sit dark next
    to claimable GPU leftovers.
    """
    try:
        cap = max(0, int(max_concurrent or 0))
        used = max(0, int(assigned or 0))
        gap = max(0, int(gap_jobs or 0))
    except (TypeError, ValueError):
        return 0
    reserved = min(gap, max(0, cap - used))
    intake = max(0, cap - reserved)
    if keep_last_seat and cap > 0 and used <= 0:
        return max(1, intake)
    return intake


def preferred_root_slave(slave_scores: Dict[str, int]) -> Optional[str]:
    """Return the sticky owner for a benchmark from per-slave root scores.

    Higher score wins. Callers typically score ready roots higher than
    in-flight/unfinished roots so a slave that already finished work stays
    preferred.
    """
    best_slave = None
    best_score = 0
    for slave, score in (slave_scores or {}).items():
        if not slave:
            continue
        try:
            value = int(score)
        except (TypeError, ValueError):
            continue
        if value > best_score:
            best_score = value
            best_slave = str(slave)
    return best_slave


def should_sticky_leftover_fanout(
    *,
    unassigned_on_job: int,
    leftover_keep: int = 4,
    idle_peers: int = 0,
    preferred_inflight_total: int = 0,
    preferred_cap: int = 0,
) -> bool:
    """Unlock exclusive sticky so leftover roots stay a shared CPU queue."""
    del leftover_keep, preferred_inflight_total, preferred_cap, idle_peers
    return max(0, int(unassigned_on_job or 0)) > 0


def should_sticky_idle_overflow(
    *,
    preferred_slave: Optional[str],
    preferred_inflight_on_job: bool,
    has_unassigned: bool,
    job_age_ms: int,
    idle_ms: int,
    preferred_online: bool,
    cpu_shared_queue: bool = False,
) -> bool:
    """True when sticky leftovers should overflow to other live CPUs.

    GPU stays exclusive while the preferred card is still working the job.
    CPU leftovers are a shared queue: sibling roots must fan out even when
    the owner is computing another batch of the same job.
    """
    if not preferred_slave or not preferred_online:
        return False
    if not has_unassigned:
        return False
    if cpu_shared_queue:
        return True
    if idle_ms <= 0:
        return False
    if preferred_inflight_on_job:
        return False
    return int(job_age_ms) >= int(idle_ms)


GPU_CHALLENGE_PREFIXES = frozenset({"c004", "c005", "c006"})


def should_hold_unowned_gpu_for_idle(
    *,
    algorithm_id: str,
    preferred_slave: Optional[str],
    slave_inflight: int,
) -> bool:
    """True when a busy GPU must not take a spare unowned GPU job.

    Sticky keeps owned jobs on their owner. A new GPU job has no owner; if a
    busy card takes the first root, idle cards wait for another TIG precommit
    (often ~2 min). Hold unowned GPU work for a slave with no inflight work.
    """
    cid = str(algorithm_id or "")[:4]
    if cid not in GPU_CHALLENGE_PREFIXES:
        return False
    if preferred_slave:
        return False
    return int(slave_inflight or 0) > 0


def should_skip_root_for_slave(
    slave_name: str,
    preferred_slave: Optional[str],
    online_slaves: Set[str],
    *,
    sticky_enabled: bool = STICKY_ROOTS_ENABLED,
    preferred_at_cap: bool = False,
    poller_idle: bool = False,
    preferred_working: bool | None = None,
    poller_is_gpu: bool = False,
) -> bool:
    """True when this polling slave must not take a root for a sticky job.

    CPU leftovers are a shared queue: a free CPU seat must take the next
    root. GPU still respects sticky so a busy card does not steal. Dark or
    telem-idle owners do not warehouse. Proofs stay with the root owner.
    """
    if not sticky_enabled:
        return False
    if not poller_is_gpu:
        return False
    if poller_idle:
        return False
    if preferred_working is False:
        return False
    if not preferred_slave:
        return False
    if preferred_slave == slave_name:
        return False
    if preferred_slave not in (online_slaves or set()):
        return False
    if preferred_at_cap:
        return False
    return True


def reset_slave_seen_ready_for_tests() -> None:
    global _SLAVE_SEEN_READY
    _SLAVE_SEEN_READY = False


def ensure_slave_seen_table(execute: Callable) -> None:
    """Create slave_seen once. Never sit on ACCESS EXCLUSIVE on the hot path.

    job_manager / precommit / get-batches used to ALTER this table every loop.
    With 100+ 1Hz heartbeats that lock convoy freezes assigns and the dashboard.
    """
    global _SLAVE_SEEN_READY
    if _SLAVE_SEEN_READY:
        return
    with _SLAVE_SEEN_LOCK:
        if _SLAVE_SEEN_READY:
            return
        try:
            db = getattr(execute, "__self__", None)
            if db is not None and hasattr(db, "execute_many"):
                db.execute_many(
                    *[(sql,) for sql in _SLAVE_SEEN_STATEMENTS],
                    lock_timeout="2s",
                )
            else:
                for sql in _SLAVE_SEEN_STATEMENTS:
                    execute(sql)
            _SLAVE_SEEN_READY = True
        except Exception as exc:
            logger.warning("slave_seen schema ensure deferred: %s", exc)


def touch_slave_seen(
    execute: Callable,
    slave_name: str,
    now_ms: int,
    num_workers: int | None = None,
    telem_state: str | None = None,
    telem_active: int | None = None,
    telem_cores: int | None = None,
) -> None:
    workers = None
    if num_workers is not None:
        try:
            workers = int(num_workers) or None
        except (TypeError, ValueError):
            workers = None
    state = None
    if telem_state:
        cleaned = str(telem_state).strip().lower()
        if cleaned in {"idle", "downloading", "running", "submitting"}:
            state = cleaned
    active = None
    if telem_active is not None:
        try:
            active = max(0, int(telem_active))
        except (TypeError, ValueError):
            active = None
    cores = None
    if telem_cores is not None:
        try:
            cores = int(telem_cores) or None
        except (TypeError, ValueError):
            cores = None
    execute(
        """
        INSERT INTO slave_seen (
            slave_name, last_seen, num_workers, telem_state, telem_active, telem_cores
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (slave_name)
        DO UPDATE SET
            last_seen = EXCLUDED.last_seen,
            num_workers = COALESCE(EXCLUDED.num_workers, slave_seen.num_workers),
            telem_state = COALESCE(EXCLUDED.telem_state, slave_seen.telem_state),
            telem_active = COALESCE(EXCLUDED.telem_active, slave_seen.telem_active),
            telem_cores = COALESCE(EXCLUDED.telem_cores, slave_seen.telem_cores)
        """,
        (slave_name, int(now_ms), workers, state, active, cores),
    )


def fetch_online_slaves(
    fetch_all: Callable,
    now_ms: int,
    online_ms: int = SLAVE_ONLINE_MS,
) -> Set[str]:
    rows = fetch_all(
        """
        SELECT slave_name
        FROM slave_seen
        WHERE last_seen >= %s
        """,
        (int(now_ms) - int(online_ms),),
    ) or []
    return {str(r["slave_name"]) for r in rows if r.get("slave_name")}


def offline_owners(
    owners: Iterable[str],
    online_slaves: Set[str],
) -> list[str]:
    out = []
    for owner in owners:
        if not owner:
            continue
        name = str(owner)
        if name not in online_slaves:
            out.append(name)
    return out
