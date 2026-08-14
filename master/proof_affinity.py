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


def should_skip_root_for_slave(
    slave_name: str,
    preferred_slave: Optional[str],
    online_slaves: Set[str],
    *,
    sticky_enabled: bool = STICKY_ROOTS_ENABLED,
    preferred_at_cap: bool = False,
) -> bool:
    """True when this polling slave must not take a root for a sticky job.

    If the preferred owner is online, only that owner may take more roots.
    If the preferred owner is dark, other live slaves may take over.
    preferred_at_cap means the master released exclusive sticky lock for this
    owner (at-cap overflow and/or aged idle-leftover reclaim). Dark preferred
    owners are handled separately via online_slaves.
    """
    if not sticky_enabled:
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


def touch_slave_seen(
    execute: Callable,
    slave_name: str,
    now_ms: int,
    num_workers: int | None = None,
) -> None:
    workers = None
    if num_workers is not None:
        try:
            workers = int(num_workers) or None
        except (TypeError, ValueError):
            workers = None
    execute(
        """
        INSERT INTO slave_seen (slave_name, last_seen, num_workers)
        VALUES (%s, %s, %s)
        ON CONFLICT (slave_name)
        DO UPDATE SET
            last_seen = EXCLUDED.last_seen,
            num_workers = COALESCE(EXCLUDED.num_workers, slave_seen.num_workers)
        """,
        (slave_name, int(now_ms), workers),
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
