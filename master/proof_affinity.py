"""Sticky root affinity + slave liveness helpers for reliable proof completion.

Proofs can only be built by the slave that holds local root artifacts. Spreading
one job's roots across many pool CPUs makes proofs fail when a member box goes
dark. These helpers:

1. Prefer keeping a job's roots on one live slave (sticky affinity).
2. Track slave liveness via get-batches heartbeats (slave_seen).
3. Support pre-submit redo of roots owned by offline slaves.
4. Support stopping jobs whose proofs are stranded on offline artifact owners.
"""

from __future__ import annotations

import os
from typing import Callable, Dict, Iterable, Optional, Set


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
    """Unlock exclusive sticky when leftovers sit idle while peers are empty.

    Do not compare leftovers to the owner's remaining cap. A 32-slot owner
    with 3 in flight still "has room" for 29, so a 8–20 root job stayed
    exclusive and the rest of the fleet got "no batches available".
    Keep a small leftover exclusive; fan the rest out only when same-profile
    peers are idle. Proofs stay on whoever actually ran each root.
    """
    del preferred_inflight_total, preferred_cap
    unassigned = max(0, int(unassigned_on_job or 0))
    keep = max(0, int(leftover_keep or 0))
    if unassigned <= keep:
        return False
    return int(idle_peers or 0) > 0


def should_sticky_idle_overflow(
    *,
    preferred_slave: Optional[str],
    preferred_inflight_on_job: bool,
    has_unassigned: bool,
    job_age_ms: int,
    idle_ms: int,
    preferred_online: bool,
) -> bool:
    """True when aged sticky leftovers should overflow to other live CPUs.

    Preferred is online and under normal sticky protection, but is not working
    this job while unassigned roots remain past idle_ms.
    """
    if idle_ms <= 0:
        return False
    if not preferred_slave or not preferred_online:
        return False
    if not has_unassigned or preferred_inflight_on_job:
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
) -> bool:
    """True when this polling slave must not take a root for a sticky job.

    If the preferred owner is online, only that owner may take more roots.
    If the preferred owner is dark, other live slaves may take over.
    preferred_at_cap means the master released exclusive sticky lock for this
    owner (at-cap overflow and/or aged idle-leftover reclaim). Dark preferred
    owners are handled separately via online_slaves.
    An idle poller must never be told 'no batches' while leftovers sit on a
    busy owner — proofs stay with whoever actually ran each root.
    """
    if not sticky_enabled:
        return False
    if poller_idle:
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


def ensure_slave_seen_table(execute: Callable) -> None:
    execute(
        """
        CREATE TABLE IF NOT EXISTS slave_seen (
            slave_name TEXT PRIMARY KEY,
            last_seen BIGINT NOT NULL
        )
        """
    )
    execute(
        "CREATE INDEX IF NOT EXISTS idx_slave_seen_last_seen ON slave_seen(last_seen)"
    )
    execute("ALTER TABLE slave_seen ADD COLUMN IF NOT EXISTS num_workers INTEGER")
    execute("ALTER TABLE slave_seen ADD COLUMN IF NOT EXISTS telem_state TEXT")
    execute("ALTER TABLE slave_seen ADD COLUMN IF NOT EXISTS telem_active INTEGER")


def touch_slave_seen(
    execute: Callable,
    slave_name: str,
    now_ms: int,
    num_workers: int | None = None,
    telem_state: str | None = None,
    telem_active: int | None = None,
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
    execute(
        """
        INSERT INTO slave_seen (
            slave_name, last_seen, num_workers, telem_state, telem_active
        )
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (slave_name)
        DO UPDATE SET
            last_seen = EXCLUDED.last_seen,
            num_workers = COALESCE(EXCLUDED.num_workers, slave_seen.num_workers),
            telem_state = COALESCE(EXCLUDED.telem_state, slave_seen.telem_state),
            telem_active = COALESCE(EXCLUDED.telem_active, slave_seen.telem_active)
        """,
        (slave_name, int(now_ms), workers, state, active),
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
