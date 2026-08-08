import os
import json
import logging
import re
import time
import random
import math
from threading import Thread, Lock
from dataclasses import dataclass
from fastapi import FastAPI, Request, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
import uvicorn
from common.structs import *
from common.utils import *
from typing import Dict, List, Optional, Set
from master.sql import get_db_conn
from master.client_manager import CONFIG
from master.capability_scheduler import (
    SCHEDULER as CAPABILITY_SCHEDULER,
    assign_rank_tuple,
    capability_settings,
    should_skip_hard_for_weak,
    update_slave_track_ema,
)
from master.cpu_tier_caps import (
    cpu_tier_cap_settings,
    effective_cpu_adaptive_max_cap,
    parse_slave_telemetry,
    telemetry_requires_load_shed,
)
from master.proof_affinity import (

    STICKY_ROOTS_ENABLED,
    ensure_slave_seen_table,
    fetch_online_slaves,
    preferred_root_slave,
    should_skip_root_for_slave,
    should_sticky_idle_overflow,
    touch_slave_seen,
)


logger = logging.getLogger(os.path.splitext(os.path.basename(__file__))[0])

# Sticky lifecycle: while a slave owes proofs (local artifacts), take no new
# roots so proof/root work do not fight on the same box. Default 0 = proof-only.
PROOF_PRIORITY_ENABLED = os.environ.get("SLAVE_PROOF_PRIORITY_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
PROOF_PRIORITY_MAX_ROOTS = max(
    0, int(os.environ.get("SLAVE_PROOF_PRIORITY_MAX_ROOTS", "0"))
)
# Sampling-gap proof-only lock is OFF by default (0). When proofs_batch rows do
# not exist yet, zeroing root_cap fleet-wide left 200+ unassigned roots stranded
# while every CPU logged awaiting_proofs=1 / own_proof_work=0. Only real open
# proofs_batch rows should proof-only lock. Set >0 only as an emergency brake.
SAMPLING_GAP_LOCK_MS = max(
    0, int(os.environ.get("SLAVE_SAMPLING_GAP_LOCK_MS", "0"))
)
# When the sticky preferred owner is online but already at its adaptive cap,
# allow other live CPUs to take unassigned roots. Without this, pending root
# batches sit locked to a full owner while the rest of the fleet idles.
# (Proofs still require local artifacts — only roots overflow.)
STICKY_OVERFLOW_AT_CAP = os.environ.get("SLAVE_STICKY_OVERFLOW_AT_CAP", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# If the sticky preferred owner is online under cap but not working that job,
# unassigned leftovers can sit forever while they take other work. After this
# job age, allow other live CPUs to take those roots (proofs stay sticky).
STICKY_OVERFLOW_IDLE_MS = max(
    0, int(os.environ.get("SLAVE_STICKY_OVERFLOW_IDLE_MS", str(3 * 60 * 1000)))
)


def _is_proof_batch_row(row: dict) -> bool:
    batch = row.get("batch") or {}
    return batch.get("sampled_nonces") is not None


def select_kept_assigned_batches(
    assigned: list,
    max_concurrent: int,
    *,
    proof_priority: bool,
    max_roots_while_proofs: int,
    always_keep_root_benchmarks: Optional[Set[str]] = None,
) -> tuple[list, list]:
    """Prefer keeping proof batches when over capacity / proof-priority mode.

    Roots for always_keep_root_benchmarks (jobs this slave must finish) are
    kept ahead of the proof-only root budget so sticky leftovers are not
    released while the owner is proving something else.

    Returns (kept, excess).
    """
    if max_concurrent < 0:
        max_concurrent = 0
    keep_bids = always_keep_root_benchmarks or set()
    proofs = [b for b in assigned if _is_proof_batch_row(b)]
    roots = [b for b in assigned if not _is_proof_batch_row(b)]
    finish_roots = [b for b in roots if b["batch"]["benchmark_id"] in keep_bids]
    other_roots = [b for b in roots if b["batch"]["benchmark_id"] not in keep_bids]
    kept_proofs = proofs[:max_concurrent]
    room = max(0, max_concurrent - len(kept_proofs))
    kept_finish = finish_roots[:room]
    room_after_finish = max(0, room - len(kept_finish))
    # Apply even with no proof batches yet (awaiting sampling / merkle ready).
    if proof_priority:
        root_budget = min(room_after_finish, max(0, int(max_roots_while_proofs)))
    else:
        root_budget = room_after_finish
    kept_roots = kept_finish + other_roots[:root_budget]
    kept = kept_proofs + kept_roots
    kept_ids = {
        (
            b["batch"]["benchmark_id"],
            b["batch"]["batch_idx"],
            "proof" if _is_proof_batch_row(b) else "root",
        )
        for b in kept
    }
    excess = [
        b
        for b in assigned
        if (
            b["batch"]["benchmark_id"],
            b["batch"]["batch_idx"],
            "proof" if _is_proof_batch_row(b) else "root",
        )
        not in kept_ids
    ]
    return kept, excess


def _batch_retry_time(algorithm_id: str) -> int:
    """Return the retry timeout (ms) for a given algorithm_id.

    Falls back to the global time_before_batch_retry if no per-challenge
    override is set.  per_challenge_time_before_batch_retry is keyed by
    challenge prefix, e.g. {"c005": 2400000, "c004": 600000}.
    """
    challenge_id = algorithm_id.split("_")[0] if "_" in algorithm_id else algorithm_id
    overrides = CONFIG.get("per_challenge_time_before_batch_retry", {})
    return overrides.get(challenge_id, CONFIG["time_before_batch_retry"])


# Challenge retry stays long so slow-but-alive workers are not stolen mid-job.
# Dark reclaim separately frees root batches when the assignee stops heartbeating.
DARK_OWNER_RECLAIM_MS = max(
    0, int(os.environ.get("SLAVE_DARK_OWNER_RECLAIM_MS", str(3 * 60 * 1000)))
)


def batch_owner_stealable(
    *,
    now_ms: int,
    slave: Optional[str],
    start_time: Optional[int],
    algorithm_id: str,
    online_slaves: Set[str],
    is_proof: bool,
    dark_reclaim_ms: int = DARK_OWNER_RECLAIM_MS,
    retry_ms: Optional[int] = None,
) -> bool:
    """True when an assigned batch may be given to another polling slave.

    Proofs are never dark-stolen (local artifacts). Roots may be reclaimed
    from a dark owner after dark_reclaim_ms even if challenge retry is hours.
    """
    if slave is None or start_time is None:
        return True
    age = int(now_ms) - int(start_time)
    effective_retry = (
        int(retry_ms)
        if retry_ms is not None
        else _batch_retry_time(algorithm_id)
    )
    if age > effective_retry:
        return True
    if is_proof or dark_reclaim_ms <= 0:
        return False
    if slave in (online_slaves or set()):
        return False
    return age > int(dark_reclaim_ms)


INFRASTRUCTURE_ERROR_PATTERNS = [
    "cannot open shared object file",
    "no such file or directory",
    "algorithm library",
    "downloading algorithm",
    "challenge container",
    "container not found",
    "permission denied",
    "docker",
    "mount",
]


def _is_infrastructure_error(error: str) -> bool:
    text = (error or "").lower()
    return any(pattern in text for pattern in INFRASTRUCTURE_ERROR_PATTERNS)


def _slave_profile(slave_name: str) -> str:
    if slave_name.startswith("pool-gpu-") or slave_name.startswith("c3-slave-"):
        return "gpu"
    return "cpu"


class SlaveManager:
    def __init__(self):
        self.batches = []
        self.lock = Lock()
        self._slot_table_ready = False
        self._slave_seen_ready = False
        # Short TTL cache so sticky-overflow preferred-cap checks do not spam
        # adaptive-cap DEBUG logs / DB queries on every get-batches poll.
        self._adaptive_cap_cache: Dict[str, tuple[int, int]] = {}
        self._adaptive_cap_cache_ms = max(
            1_000,
            int(os.environ.get("SLAVE_ADAPTIVE_CAP_CACHE_MS", "15000")),
        )
        # Optional get-batches telemetry (Phase C) + load-shed cooldown per slave.
        self._slave_telemetry: Dict[str, dict] = {}
        self._cpu_load_shed_until: Dict[str, int] = {}

    def _ensure_slave_seen_table(self):
        if self._slave_seen_ready:
            return
        ensure_slave_seen_table(get_db_conn().execute)
        self._slave_seen_ready = True

    def _touch_slave_seen(self, slave_name: str, now_ms: int):
        self._ensure_slave_seen_table()
        touch_slave_seen(get_db_conn().execute, slave_name, now_ms)

    def _online_slaves(self, now_ms: int) -> Set[str]:
        self._ensure_slave_seen_table()
        return fetch_online_slaves(get_db_conn().fetch_all, now_ms)

    def _root_affinity_map(self) -> Dict[str, str]:
        """benchmark_id -> preferred root slave for sticky assignment."""
        if not STICKY_ROOTS_ENABLED:
            return {}
        rows = get_db_conn().fetch_all(
            """
            SELECT
                benchmark_id,
                slave,
                COUNT(*) FILTER (WHERE ready = true) AS ready_n,
                COUNT(*) FILTER (WHERE ready IS NULL AND slave IS NOT NULL) AS inflight_n
            FROM root_batch
            WHERE slave IS NOT NULL
              AND (ready = true OR ready IS NULL)
            GROUP BY benchmark_id, slave
            """
        ) or []
        scores: Dict[str, Dict[str, int]] = {}
        for row in rows:
            bid = row.get("benchmark_id")
            slave = row.get("slave")
            if not bid or not slave:
                continue
            # Prefer slaves that already finished roots for this job.
            score = int(row.get("ready_n") or 0) * 100 + int(row.get("inflight_n") or 0)
            scores.setdefault(str(bid), {})[str(slave)] = score
        return {
            bid: owner
            for bid, slave_scores in scores.items()
            if (owner := preferred_root_slave(slave_scores))
        }

    def _is_trusted_slave(self, slave_name: str) -> bool:
        if slave_name in set(CONFIG.get("trusted_slave_names", [])):
            return True
        return any(re.match(pattern, slave_name) for pattern in CONFIG.get("trusted_slave_regexes", []))

    def _is_authorized_slave(self, slave_name: str) -> bool:
        """Return True when a slave name is allowed to use the master.

        Regex routing decides what a slave can work on, but it is not an
        authorization boundary. Pool slave names must be registered and active
        unless the operator has explicitly trusted the exact name or regex.
        """
        if not slave_name.startswith("pool-"):
            return True

        if self._is_trusted_slave(slave_name):
            return True

        row = get_db_conn().fetch_one(
            """
            SELECT 1
            FROM pool_members
            WHERE slave_name = %s
              AND active = true
            LIMIT 1
            """,
            (slave_name,)
        )
        return row is not None

    def _require_authorized_slave(self, slave_name: str):
        if not self._is_authorized_slave(slave_name):
            logger.warning(f"slave {slave_name} is not registered or trusted. rejecting request")
            raise HTTPException(status_code=403, detail="Unregistered slave")

    def _quarantine_slave(self, slave_name: str, reason: str):
        """Deactivate a misconfigured public slave and release its unfinished work."""
        if not slave_name.startswith("pool-") or self._is_trusted_slave(slave_name):
            return

        note = f"auto-quarantined: {reason[:500]}"
        logger.warning(f"quarantining slave {slave_name}: {reason}")
        queries = [
            (
                """
                UPDATE pool_members
                SET active = false,
                    notes = CONCAT_WS(E'\n', NULLIF(notes, ''), %s)
                WHERE slave_name = %s
                """,
                (note, slave_name)
            ),
            (
                """
                UPDATE root_batch
                SET slave = NULL,
                    start_time = NULL,
                    end_time = NULL,
                    num_attempts = 0
                WHERE slave = %s
                  AND ready IS NULL
                """,
                (slave_name,)
            ),
            (
                """
                UPDATE proofs_batch
                SET slave = NULL,
                    start_time = NULL,
                    end_time = NULL,
                    num_attempts = 0
                WHERE slave = %s
                  AND ready IS NULL
                """,
                (slave_name,)
            ),
        ]
        get_db_conn().execute_many(*queries)

    def _ensure_slot_table(self):
        if self._slot_table_ready:
            return
        get_db_conn().execute(
            """
            CREATE TABLE IF NOT EXISTS benchmark_slot (
                slot_id TEXT PRIMARY KEY,
                slot_type TEXT NOT NULL,
                benchmark_id TEXT REFERENCES job(benchmark_id),
                challenge TEXT,
                algorithm_id TEXT,
                track_id TEXT,
                assigned_at BIGINT,
                last_activity_at BIGINT,
                state TEXT NOT NULL DEFAULT 'idle'
            )
            """
        )
        get_db_conn().execute(
            "CREATE INDEX IF NOT EXISTS idx_benchmark_slot_type ON benchmark_slot(slot_type)"
        )
        get_db_conn().execute(
            "CREATE INDEX IF NOT EXISTS idx_benchmark_slot_benchmark_id ON benchmark_slot(benchmark_id)"
        )
        get_db_conn().execute(
            "CREATE INDEX IF NOT EXISTS idx_benchmark_slot_state ON benchmark_slot(state)"
        )
        self._slot_table_ready = True

    def _resource_slot_counts(self) -> Dict[str, int]:
        cfg = CONFIG.get("resource_slots", {})
        if not cfg:
            return {}
        if cfg.get("enabled") is False:
            return {}
        counts = cfg.get("slots", cfg)
        return {
            str(k): int(v)
            for k, v in counts.items()
            if k != "enabled" and isinstance(v, int) and v > 0
        }

    def _slot_types_for_slave(self, slave_name: str) -> List[str]:
        counts = self._resource_slot_counts()
        if not counts:
            return []
        if slave_name.startswith("pool-cpu-") or slave_name.startswith("aws-cpu-slave-"):
            return ["cpu"] if "cpu" in counts else []
        if slave_name.startswith("pool-gpu-") or slave_name.startswith("c3-slave-"):
            return [t for t in ("vector_search", "hypergraph", "neuralnet_optimizer") if t in counts]
        return []

    def _sync_slots(self):
        counts = self._resource_slot_counts()
        if not counts:
            return
        self._ensure_slot_table()
        queries = []
        for slot_type, count in counts.items():
            for i in range(1, count + 1):
                queries.append((
                    """
                    INSERT INTO benchmark_slot (slot_id, slot_type)
                    VALUES (%s, %s)
                    ON CONFLICT (slot_id) DO NOTHING
                    """,
                    (f"{slot_type}_{i:03d}", slot_type)
                ))
        if queries:
            get_db_conn().execute_many(*queries)

    def _release_slots(self):
        """Free slots whose benchmark has finished, stopped, expired, or disappeared."""
        if not self._resource_slot_counts():
            return
        self._ensure_slot_table()
        get_db_conn().execute(
            """
            UPDATE benchmark_slot S
            SET benchmark_id = NULL,
                challenge = NULL,
                algorithm_id = NULL,
                track_id = NULL,
                assigned_at = NULL,
                last_activity_at = NULL,
                state = 'idle'
            FROM job J
            WHERE S.benchmark_id = J.benchmark_id
              AND (
                J.stopped IS NOT NULL
                OR J.end_time IS NOT NULL
                OR J.merkle_proofs_ready IS NOT NULL
              )
            """
        )
        get_db_conn().execute(
            """
            UPDATE benchmark_slot S
            SET benchmark_id = NULL,
                challenge = NULL,
                algorithm_id = NULL,
                track_id = NULL,
                assigned_at = NULL,
                last_activity_at = NULL,
                state = 'idle'
            WHERE S.benchmark_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM job J WHERE J.benchmark_id = S.benchmark_id
              )
            """
        )

    def _challenge_matches_slot(self, slot_type: str) -> str:
        if slot_type == "cpu":
            return "J.challenge NOT IN ('vector_search', 'hypergraph', 'neuralnet_optimizer')"
        return "J.challenge = %s"

    def _assign_idle_slots(self, slot_types: List[str], algorithm_id_regex: str = ""):
        if not slot_types:
            return
        now_ms = int(time.time() * 1000)
        for slot_type in slot_types:
            idle_slots = get_db_conn().fetch_all(
                """
                SELECT slot_id
                FROM benchmark_slot
                WHERE slot_type = %s
                  AND benchmark_id IS NULL
                ORDER BY slot_id
                """,
                (slot_type,)
            )
            for slot in idle_slots:
                if slot_type == "cpu":
                    job = get_db_conn().fetch_one(
                        """
                        SELECT J.benchmark_id, J.challenge, J.settings
                        FROM job J
                        WHERE J.stopped IS NULL
                          AND J.end_time IS NULL
                          AND J.challenge NOT IN ('vector_search', 'hypergraph', 'neuralnet_optimizer')
                          AND (%s = '' OR J.settings->>'algorithm_id' ~ %s)
                          AND NOT EXISTS (
                            SELECT 1 FROM benchmark_slot S WHERE S.benchmark_id = J.benchmark_id
                          )
                          AND (
                            EXISTS (
                              SELECT 1 FROM root_batch R
                              WHERE R.benchmark_id = J.benchmark_id AND R.ready IS NULL
                            )
                            OR EXISTS (
                              SELECT 1 FROM proofs_batch P
                              WHERE P.benchmark_id = J.benchmark_id AND P.ready IS NULL
                            )
                          )
                        ORDER BY J.block_started, J.start_time, J.benchmark_id
                        LIMIT 1
                        """,
                        (algorithm_id_regex, algorithm_id_regex)
                    )
                else:
                    job = get_db_conn().fetch_one(
                        """
                        SELECT J.benchmark_id, J.challenge, J.settings
                        FROM job J
                        WHERE J.stopped IS NULL
                          AND J.end_time IS NULL
                          AND J.challenge = %s
                          AND NOT EXISTS (
                            SELECT 1 FROM benchmark_slot S WHERE S.benchmark_id = J.benchmark_id
                          )
                          AND (
                            EXISTS (
                              SELECT 1 FROM root_batch R
                              WHERE R.benchmark_id = J.benchmark_id AND R.ready IS NULL
                            )
                            OR EXISTS (
                              SELECT 1 FROM proofs_batch P
                              WHERE P.benchmark_id = J.benchmark_id AND P.ready IS NULL
                            )
                          )
                        ORDER BY J.block_started, J.start_time, J.benchmark_id
                        LIMIT 1
                        """,
                        (slot_type,)
                    )
                if job is None:
                    break
                settings = job["settings"] or {}
                get_db_conn().execute(
                    """
                    UPDATE benchmark_slot
                    SET benchmark_id = %s,
                        challenge = %s,
                        algorithm_id = %s,
                        track_id = %s,
                        assigned_at = %s,
                        last_activity_at = %s,
                        state = 'root'
                    WHERE slot_id = %s
                    """,
                    (
                        job["benchmark_id"],
                        job["challenge"],
                        settings.get("algorithm_id"),
                        settings.get("track_id"),
                        now_ms,
                        now_ms,
                        slot["slot_id"],
                    )
                )
                logger.info(
                    f"slot {slot['slot_id']} ({slot_type}) assigned benchmark "
                    f"{job['benchmark_id']} ({job['challenge']}, {settings.get('track_id')})"
                )

    def _slot_benchmark_ids(self, slot_types: List[str]) -> Set[str]:
        if not slot_types:
            return set()
        rows = get_db_conn().fetch_all(
            """
            SELECT benchmark_id
            FROM benchmark_slot
            WHERE slot_type IN %s
              AND benchmark_id IS NOT NULL
            """,
            (tuple(slot_types),)
        )
        return {r["benchmark_id"] for r in rows}

    def _starved_slot_benchmarks(self, slot_types: List[str], now_ms: int) -> Dict[str, float]:
        """Return slotted benchmarks that should be prioritized for root assignment.

        A slot can be occupied by an active benchmark but make no progress if all
        matching slaves stay full on other work. Once the slot has pending roots,
        no assigned roots, and has been idle for long enough, move its root
        batches to the front of the candidate order instead of churning the slot.
        """
        if not slot_types:
            return {}
        threshold_ms = int(CONFIG.get("slot_starvation_priority_ms", 20 * 60 * 1000))
        if threshold_ms <= 0:
            return {}
        rows = get_db_conn().fetch_all(
            """
            SELECT
                S.benchmark_id,
                COALESCE(S.last_activity_at, S.assigned_at, J.start_time, 0) AS last_activity_at,
                COUNT(R.*) FILTER (WHERE R.ready IS NULL) AS pending_roots,
                COUNT(R.*) FILTER (
                    WHERE R.ready IS NULL
                      AND R.slave IS NOT NULL
                      AND R.start_time IS NOT NULL
                ) AS assigned_roots
            FROM benchmark_slot S
            JOIN job J ON J.benchmark_id = S.benchmark_id
            JOIN root_batch R ON R.benchmark_id = S.benchmark_id
            WHERE S.slot_type IN %s
              AND S.benchmark_id IS NOT NULL
              AND S.state = 'root'
              AND J.stopped IS NULL
              AND J.end_time IS NULL
              AND J.merkle_root_ready IS NULL
            GROUP BY S.benchmark_id, S.last_activity_at, S.assigned_at, J.start_time
            HAVING COUNT(R.*) FILTER (WHERE R.ready IS NULL) > 0
               AND COUNT(R.*) FILTER (
                    WHERE R.ready IS NULL
                      AND R.slave IS NOT NULL
                      AND R.start_time IS NOT NULL
               ) = 0
            """,
            (tuple(slot_types),)
        )
        out = {}
        for row in rows:
            last_activity_at = int(row.get("last_activity_at") or 0)
            idle_ms = now_ms - last_activity_at
            if idle_ms >= threshold_ms:
                out[row["benchmark_id"]] = idle_ms
        return out

    def _mark_slot_activity(self, benchmark_id: str, state: str):
        if not self._resource_slot_counts():
            return
        get_db_conn().execute(
            """
            UPDATE benchmark_slot
            SET last_activity_at = (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT,
                state = %s
            WHERE benchmark_id = %s
            """,
            (state, benchmark_id)
        )

    def _route_cap_for_slave(self, slave_name: str) -> int:
        """Configured max_concurrent_batches for the slave route matching name."""
        matched = next(
            (
                row
                for row in (CONFIG.get("slaves") or [])
                if re.match(row.get("name_regex") or r"$^", slave_name)
            ),
            None,
        )
        if not matched:
            return 0
        try:
            return max(0, int(matched.get("max_concurrent_batches") or 0))
        except (TypeError, ValueError):
            return 0

    def _remember_slave_telemetry(self, slave_name: str, telemetry: dict, now_ms: int) -> None:
        """Store optional get-batches telemetry and arm CPU load-shed cooldown."""
        if not telemetry:
            return
        stored = dict(telemetry)
        stored["received_at_ms"] = int(now_ms)
        self._slave_telemetry[slave_name] = stored
        if stored.get("slave_version") or stored.get("state") is not None:
            logger.debug(
                "slave telemetry %s version=%s state=%s active=%s pending=%s last_idle_ms=%s "
                "cores=%s workers=%s load_1m=%s",
                slave_name,
                stored.get("slave_version"),
                stored.get("state"),
                stored.get("active_batches"),
                stored.get("pending_batches"),
                stored.get("last_idle_ms"),
                stored.get("cores"),
                stored.get("num_workers"),
                stored.get("load_1m"),
            )
        tier_settings = cpu_tier_cap_settings(CONFIG)
        if not tier_settings.get("live_telemetry_enabled", True):
            return
        if _slave_profile(slave_name) != "cpu":
            return
        if telemetry_requires_load_shed(telemetry, tier_settings):
            until = now_ms + int(tier_settings.get("load_shed_cooldown_ms") or 0)
            prev = int(self._cpu_load_shed_until.get(slave_name) or 0)
            if until > prev:
                self._cpu_load_shed_until[slave_name] = until
                logger.info(
                    "cpu load-shed armed slave=%s cores=%s load_1m=%s free_ram_gb=%s until_in_ms=%s",
                    slave_name,
                    telemetry.get("cores"),
                    telemetry.get("load_1m"),
                    telemetry.get("free_ram_gb"),
                    int(tier_settings.get("load_shed_cooldown_ms") or 0),
                )

    def _adaptive_max_concurrent(
        self,
        slave_name: str,
        route_cap: int,
        *,
        log: bool = True,
        use_cache: bool = True,
    ) -> int:
        """Return a measured per-slave cap, bounded by the route cap.

        New public miners start with a small cap. As they complete batches in
        the recent window, they earn more in-flight work. Trusted/operator
        slaves keep the route cap so local AWS/C3 tuning remains explicit.

        Public CPU members: fleet cpu_max_cap stays the default (usually 1).
        L/XL may earn a higher concurrent ceiling only with live telemetry
        headroom (see master.cpu_tier_caps); core count alone never raises it.

        Sticky-overflow preferred-owner checks should pass log=False so every
        get-batches poll does not multiply adaptive-cap DEBUG spam.
        """
        now_ms = int(time.time() * 1000)
        cache_key = f"{slave_name}:{int(route_cap)}"
        if use_cache:
            cached = self._adaptive_cap_cache.get(cache_key)
            if cached is not None:
                cached_cap, cached_until = cached
                if now_ms < cached_until:
                    return int(cached_cap)

        cfg = CONFIG.get("adaptive_slave_caps", {})
        if not cfg or cfg.get("enabled") is False:
            return route_cap
        if not slave_name.startswith("pool-") or self._is_trusted_slave(slave_name):
            return route_cap

        profile = _slave_profile(slave_name)
        default_min = 1 if profile == "gpu" else 4
        default_max = route_cap
        min_cap = int(cfg.get(f"{profile}_min_cap", cfg.get("min_cap", default_min)))
        max_cap = int(cfg.get(f"{profile}_max_cap", cfg.get("max_cap", default_max)))
        max_cap = min(route_cap, max(min_cap, max_cap))

        telemetry = self._slave_telemetry.get(slave_name) or {}
        if profile == "cpu":
            tier_settings = cpu_tier_cap_settings(CONFIG)
            load_shed_active = now_ms < int(self._cpu_load_shed_until.get(slave_name) or 0)
            try:
                slave_tier = CAPABILITY_SCHEDULER.slave_tier(
                    slave_name,
                    fetch_one=get_db_conn().fetch_one,
                    config=CONFIG,
                    now_ms=now_ms,
                    live_cores=telemetry.get("cores"),
                    live_ram_gb=telemetry.get("ram_gb"),
                    skip_cache=bool(telemetry.get("cores")),
                )
            except Exception:
                slave_tier = capability_settings(CONFIG).get("default_tier", 1)
            max_cap = effective_cpu_adaptive_max_cap(
                route_cap=route_cap,
                fleet_cpu_max_cap=int(cfg.get("cpu_max_cap", cfg.get("max_cap", 1))),
                tier=int(slave_tier),
                telemetry=telemetry,
                settings=tier_settings,
                load_shed_active=load_shed_active,
            )
            max_cap = max(min_cap, max_cap) if max_cap >= min_cap else max_cap
            # Never let min_cap pull a telemetry-locked CPU above its ceiling.
            min_cap = min(min_cap, max_cap) if max_cap > 0 else min_cap

        window_ms = int(cfg.get("window_ms", 30 * 60 * 1000))
        target_buffer_ms = int(cfg.get("target_buffer_ms", 10 * 60 * 1000))
        warmup_completed = int(cfg.get("warmup_completed_batches", 3))
        since_ms = now_ms - window_ms

        per_family = capability_settings(CONFIG).get("per_family_adaptive_caps", True)
        if per_family:
            stats = get_db_conn().fetch_one(
                """
                WITH recent_roots AS (
                    SELECT
                        R.start_time,
                        R.end_time,
                        R.ready,
                        J.challenge,
                        LEAST(J.batch_size, J.num_nonces - R.batch_idx * J.batch_size) AS nonces,
                        (R.end_time - R.start_time) AS runtime_ms
                    FROM root_batch R
                    JOIN job J ON J.benchmark_id = R.benchmark_id
                    WHERE R.slave = %s
                      AND R.start_time IS NOT NULL
                      AND R.start_time >= %s
                ),
                per_challenge AS (
                    SELECT
                        challenge,
                        AVG(runtime_ms) FILTER (
                            WHERE ready = true AND end_time IS NOT NULL AND start_time IS NOT NULL
                        ) AS avg_runtime_ms
                    FROM recent_roots
                    GROUP BY challenge
                )
                SELECT
                    (SELECT COUNT(*) FROM recent_roots) AS assigned_recent,
                    (SELECT COUNT(*) FROM recent_roots WHERE ready = true) AS completed_recent,
                    (SELECT COUNT(*) FROM recent_roots WHERE ready IS NULL) AS active_unfinished,
                    (SELECT COALESCE(SUM(nonces), 0) FROM recent_roots WHERE ready = true) AS completed_nonces,
                    (SELECT MAX(avg_runtime_ms) FROM per_challenge) AS avg_runtime_ms
                """,
                (slave_name, since_ms)
            ) or {}
        else:
            stats = get_db_conn().fetch_one(
                """
                WITH recent_roots AS (
                    SELECT
                        R.start_time,
                        R.end_time,
                        R.ready,
                        LEAST(J.batch_size, J.num_nonces - R.batch_idx * J.batch_size) AS nonces
                    FROM root_batch R
                    JOIN job J ON J.benchmark_id = R.benchmark_id
                    WHERE R.slave = %s
                      AND R.start_time IS NOT NULL
                      AND R.start_time >= %s
                )
                SELECT
                    COUNT(*) AS assigned_recent,
                    COUNT(*) FILTER (WHERE ready = true) AS completed_recent,
                    COUNT(*) FILTER (WHERE ready IS NULL) AS active_unfinished,
                    COALESCE(SUM(nonces) FILTER (WHERE ready = true), 0) AS completed_nonces,
                    AVG(end_time - start_time) FILTER (WHERE ready = true AND end_time IS NOT NULL) AS avg_runtime_ms
                FROM recent_roots
                """,
                (slave_name, since_ms)
            ) or {}

        completed = int(stats.get("completed_recent") or 0)
        active = int(stats.get("active_unfinished") or 0)
        avg_runtime_ms = float(stats.get("avg_runtime_ms") or 0)

        if completed < warmup_completed:
            cap = min_cap
        elif avg_runtime_ms > 0:
            # Keep roughly target_buffer_ms worth of work in flight. The
            # throughput estimate lets multi-worker machines earn more slots,
            # while runtime keeps very fast single batches from being underfed.
            # Prefer reported num_workers when present (Phase C telemetry).
            workers = None
            try:
                workers = int((telemetry or {}).get("num_workers") or 0) or None
            except (TypeError, ValueError):
                workers = None
            throughput_cap = math.ceil(completed * target_buffer_ms / window_ms)
            runtime_cap = math.ceil(target_buffer_ms / avg_runtime_ms)
            if workers and workers > 0:
                # More workers → higher single-batch burn; do not invent extra
                # concurrent slots from worker count alone (that overloads).
                runtime_cap = max(1, runtime_cap)
            cap = max(min_cap, throughput_cap, runtime_cap)
        else:
            cap = min_cap

        if max_cap <= 0:
            cap = 0
        else:
            cap = max(1, min(max_cap, cap))
        if use_cache:
            self._adaptive_cap_cache[cache_key] = (
                cap,
                now_ms + self._adaptive_cap_cache_ms,
            )
        if log:
            logger.debug(
                f"adaptive cap for {slave_name}: cap={cap}, route_cap={route_cap}, "
                f"completed_recent={completed}, active={active}, avg_runtime_ms={avg_runtime_ms:.0f}"
            )
        return cap

    def _slave_finish_root_benchmarks(self, slave_name: str) -> Set[str]:
        """Jobs this slave should still root even while proof-only.

        If the slave already completed some roots on a job that still has
        unfinished root batches, it must be allowed to finish them — otherwise
        sticky + proof-only strands the leftover unassigned batches forever.
        """
        rows = get_db_conn().fetch_all(
            """
            SELECT DISTINCT r.benchmark_id
            FROM root_batch r
            INNER JOIN job j ON j.benchmark_id = r.benchmark_id
            WHERE j.stopped IS NULL
              AND j.end_time IS NULL
              AND j.merkle_root_ready IS NULL
              AND r.slave = %s
              AND r.ready = true
              AND EXISTS (
                SELECT 1
                FROM root_batch u
                WHERE u.benchmark_id = r.benchmark_id
                  AND u.ready IS NULL
              )
            """,
            (slave_name,),
        )
        return {str(r["benchmark_id"]) for r in (rows or []) if r.get("benchmark_id")}

    def _slave_awaiting_proofs(self, slave_name: str, now_ms: Optional[int] = None) -> bool:
        """True when this slave still owes unfinished proofs_batch work.

        Optional SAMPLING_GAP_LOCK_MS>0 also locks during the pre-sample gap after
        this slave's latest ready root. Default is 0 (disabled): that gap must not
        set root_cap=0 or sticky-unassigned roots stay stranded on idle CPUs.
        """
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        gap_ms = int(SAMPLING_GAP_LOCK_MS)
        gap_cutoff = now_ms - gap_ms if gap_ms > 0 else None
        row = get_db_conn().fetch_one(
            """
            SELECT 1 AS ok
            FROM job j
            WHERE j.stopped IS NULL
              AND j.end_time IS NULL
              AND j.merkle_root_ready = true
              AND j.merkle_proofs_ready IS NULL
              AND EXISTS (
                SELECT 1
                FROM root_batch r
                WHERE r.benchmark_id = j.benchmark_id
                  AND r.slave = %s
                  AND r.ready = true
              )
              AND (
                (
                  %s
                  AND NOT EXISTS (
                    SELECT 1
                    FROM proofs_batch p
                    WHERE p.benchmark_id = j.benchmark_id
                  )
                  AND EXISTS (
                    SELECT 1
                    FROM root_batch r
                    WHERE r.benchmark_id = j.benchmark_id
                      AND r.slave = %s
                      AND r.ready = true
                      AND r.end_time IS NOT NULL
                      AND r.end_time >= %s
                  )
                )
                OR EXISTS (
                  SELECT 1
                  FROM proofs_batch p
                  INNER JOIN root_batch r
                    ON r.benchmark_id = p.benchmark_id
                   AND r.batch_idx = p.batch_idx
                  WHERE p.benchmark_id = j.benchmark_id
                    AND p.ready IS NULL
                    AND r.slave = %s
                )
              )
            LIMIT 1
            """,
            (
                slave_name,
                bool(gap_ms > 0),
                slave_name,
                int(gap_cutoff if gap_cutoff is not None else 0),
                slave_name,
            ),
        )
        return bool(row)

    def _slave_has_root_artifacts(self, slave_name: str, benchmark_id: str, batch_idx: int) -> bool:
        """Proofs must be built by the slave that produced that exact root batch.

        The slave stores root artifacts locally under its cache/results directory.
        Assigning proofs to a different slave burns attempts and can stall a
        benchmark because that slave cannot build Merkle proofs from missing
        local artifacts.
        """
        row = get_db_conn().fetch_one(
            """
            SELECT 1
            FROM root_batch
            WHERE benchmark_id = %s
              AND batch_idx = %s
              AND slave = %s
              AND ready = true
            LIMIT 1
            """,
            (benchmark_id, batch_idx, slave_name)
        )
        return row is not None

    def run(self):
        with self.lock:
            get_db_conn().execute(
                """
                UPDATE proofs_batch P
                SET slave = NULL,
                    start_time = NULL,
                    end_time = NULL
                WHERE P.ready IS NULL
                  AND P.slave IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1
                    FROM root_batch R
                    WHERE R.benchmark_id = P.benchmark_id
                      AND R.batch_idx = P.batch_idx
                      AND R.slave = P.slave
                      AND R.ready = true
                  )
                """
            )
            self.batches = get_db_conn().fetch_all(
                """
                SELECT * FROM (
                    SELECT
                        A.slave,
                        A.start_time,
                        A.end_time,
                        A.num_attempts,
                        JSONB_BUILD_OBJECT(
                            'id', A.benchmark_id || '_' || A.batch_idx,
                            'benchmark_id', A.benchmark_id,
                            'start_nonce', A.batch_idx * B.batch_size,
                            'num_nonces', LEAST(B.batch_size, B.num_nonces - A.batch_idx * B.batch_size),
                            'settings', B.settings,
                            'hyperparameters', B.hyperparameters,
                            'sampled_nonces', A.sampled_nonces,
                            'fuel_budget', B.fuel_budget,
                            'download_url', B.download_url,
                            'rand_hash', B.rand_hash,
                            'batch_size', B.batch_size,
                            'batch_idx', A.batch_idx,
                            'challenge', B.challenge,
                            'algorithm', B.algorithm,
                            'job_start_time', B.start_time
                        ) AS batch
                    FROM proofs_batch A
                    INNER JOIN job B
                        ON A.ready IS NULL
                        AND B.merkle_root_ready
                        AND B.stopped IS NULL
                        AND A.benchmark_id = B.benchmark_id
                    ORDER BY B.block_started, A.benchmark_id, A.batch_idx
                )
                
                UNION ALL
                
                SELECT * FROM (
                    SELECT
                        A.slave,
                        A.start_time,
                        A.end_time,
                        A.num_attempts,
                        JSONB_BUILD_OBJECT(
                            'id', A.benchmark_id || '_' || A.batch_idx,
                            'benchmark_id', A.benchmark_id,
                            'start_nonce', A.batch_idx * B.batch_size,
                            'num_nonces', LEAST(B.batch_size, B.num_nonces - A.batch_idx * B.batch_size),
                            'settings', B.settings,
                            'hyperparameters', B.hyperparameters,
                            'sampled_nonces', NULL,
                            'fuel_budget', B.fuel_budget,
                            'download_url', B.download_url,
                            'rand_hash', B.rand_hash,
                            'batch_size', B.batch_size,
                            'batch_idx', A.batch_idx,
                            'challenge', B.challenge,
                            'algorithm', B.algorithm,
                            'job_start_time', B.start_time
                        ) AS batch
                    FROM root_batch A
                    INNER JOIN job B
                        ON A.ready IS NULL
                        AND B.stopped IS NULL
                        AND A.benchmark_id = B.benchmark_id
                    ORDER BY B.block_started, A.benchmark_id, A.batch_idx
                )
                """
            )
            logger.debug(f"Refreshed pending batches. Got {len(self.batches)}")

    def start(self):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        allowed_exact_paths = {"/get-batches"}
        allowed_prefixes = (
            "/submit-batch-root/",
            "/submit-batch-proofs/",
            "/submit-batch-error/",
        )

        @app.middleware("http")
        async def block_unexpected_paths(request: Request, call_next):
            path = request.url.path
            if path not in allowed_exact_paths and not path.startswith(allowed_prefixes):
                return JSONResponse(status_code=404, content={"detail": "Not Found"})
            return await call_next(request)

        @app.route('/get-batches', methods=['GET'])
        def get_batch(request: Request):
            if (slave_name := request.headers.get('User-Agent', None)) is None:
                return "User-Agent header is required", 403
            if not any(re.match(slave["name_regex"], slave_name) for slave in CONFIG["slaves"]):
                logger.warning(f"slave {slave_name} does not match any regex. rejecting get-batch request")
                raise HTTPException(status_code=403, detail="Unregistered slave")
            self._require_authorized_slave(slave_name)

            slave = next((slave for slave in CONFIG["slaves"] if re.match(slave["name_regex"], slave_name)), None)

            concurrent = []
            updates = []
            now = time.time() * 1000
            # Optional Phase C telemetry (query params and/or X-InnoPool-* headers).
            # Stock slaves that omit fields keep concurrent CPU cap at the fleet default.
            try:
                telemetry = parse_slave_telemetry(
                    query_params=dict(request.query_params),
                    headers=request.headers,
                )
            except Exception:
                telemetry = {}
            if telemetry:
                self._remember_slave_telemetry(slave_name, telemetry, int(now))
            self._touch_slave_seen(slave_name, int(now))
            slot_types = self._slot_types_for_slave(slave_name)
            slot_benchmark_ids = set()
            starved_slot_benchmarks = {}
            if slot_types:
                self._sync_slots()
                self._release_slots()
                self._assign_idle_slots(slot_types, slave["algorithm_id_regex"])
                slot_benchmark_ids = self._slot_benchmark_ids(slot_types)
                starved_slot_benchmarks = self._starved_slot_benchmarks(slot_types, int(now))
            root_affinity = self._root_affinity_map()
            # Always load heartbeats: sticky affinity + dark-owner root reclaim.
            online_slaves = self._online_slaves(int(now))
            # Optional sticky overflow (off by default): only when explicitly enabled.
            preferred_at_cap: Set[str] = set()
            if STICKY_OVERFLOW_AT_CAP and STICKY_ROOTS_ENABLED:
                active_by_slave: Dict[str, int] = {}
                for row in self.batches:
                    owner = row.get("slave")
                    if owner and row.get("end_time") is None:
                        active_by_slave[str(owner)] = active_by_slave.get(str(owner), 0) + 1
                awaiting_cache: Dict[str, bool] = {}
                for preferred in set(root_affinity.values()):
                    if not preferred or preferred not in online_slaves:
                        continue
                    pref_route = self._route_cap_for_slave(preferred)
                    if pref_route <= 0:
                        continue
                    pref_cap = self._adaptive_max_concurrent(
                        preferred, pref_route, log=False, use_cache=True
                    )
                    if pref_cap > 0 and int(active_by_slave.get(preferred) or 0) >= pref_cap:
                        preferred_at_cap.add(preferred)
                        continue
                    # Preferred owner has open proof work → root_cap=0 for them.
                    # Let other live CPUs claim unassigned roots on that job.
                    if PROOF_PRIORITY_ENABLED:
                        if preferred not in awaiting_cache:
                            awaiting_cache[preferred] = self._slave_awaiting_proofs(
                                preferred, int(now)
                            )
                        if awaiting_cache[preferred]:
                            preferred_at_cap.add(preferred)

                # Aged leftovers: preferred online under cap, not inflight on the
                # job, but unassigned roots remain → overflow so the fleet helps.
                if STICKY_OVERFLOW_IDLE_MS > 0:
                    inflight_pref_bids: Set[str] = set()
                    unassigned_job_age: Dict[str, int] = {}
                    unassigned_pref: Dict[str, str] = {}
                    for row in self.batches:
                        batch = row.get("batch") or {}
                        if batch.get("sampled_nonces") is not None:
                            continue
                        if row.get("end_time") is not None:
                            continue
                        bid = str(batch.get("benchmark_id") or "")
                        preferred = root_affinity.get(bid)
                        if not preferred or preferred not in online_slaves:
                            continue
                        if row.get("slave") == preferred:
                            inflight_pref_bids.add(bid)
                            continue
                        if row.get("slave") is not None:
                            continue
                        job_start = batch.get("job_start_time")
                        try:
                            job_age = int(now) - int(job_start)
                        except (TypeError, ValueError):
                            continue
                        prev = unassigned_job_age.get(bid)
                        if prev is None or job_age > prev:
                            unassigned_job_age[bid] = job_age
                            unassigned_pref[bid] = preferred
                    for bid, preferred in unassigned_pref.items():
                        if not should_sticky_idle_overflow(
                            preferred_slave=preferred,
                            preferred_inflight_on_job=bid in inflight_pref_bids,
                            has_unassigned=True,
                            job_age_ms=unassigned_job_age[bid],
                            idle_ms=int(STICKY_OVERFLOW_IDLE_MS),
                            preferred_online=True,
                        ):
                            continue
                        if preferred not in preferred_at_cap:
                            preferred_at_cap.add(preferred)
                            logger.info(
                                "sticky idle overflow preferred=%s bid=%s "
                                "(unassigned leftovers, owner not inflight on job)",
                                preferred,
                                bid[:8],
                            )

            with self.lock:
                route_cap = int(slave["max_concurrent_batches"])
                max_concurrent = self._adaptive_max_concurrent(slave_name, route_cap)
                # Fair-share: cap how many concurrent batches any single benchmark may hold
                # on this slave, so one benchmark can't drain every slot and starve the other
                # challenges (the batches are ordered oldest-precommit-first). Default to a
                # quarter of the slave's capacity when not explicitly configured.
                per_bench_cap = CONFIG.get("max_batches_per_benchmark", 0)
                if not per_bench_cap or per_bench_cap < 1:
                    per_bench_cap = max(1, max_concurrent // 4)

                assigned = [
                    b for b in self.batches
                    if b["slave"] == slave_name and b["end_time"] is None
                ]
                assigned_proofs = [b for b in assigned if _is_proof_batch_row(b)]
                artifact_cache = {}

                def has_artifacts(bid: str, batch_idx: int) -> bool:
                    key = (bid, int(batch_idx))
                    if key not in artifact_cache:
                        artifact_cache[key] = self._slave_has_root_artifacts(
                            slave_name, bid, int(batch_idx)
                        )
                    return artifact_cache[key]

                # Unfinished proofs this slave can actually build (local artifacts).
                own_proof_work = []
                if PROOF_PRIORITY_ENABLED:
                    for b in self.batches:
                        batch = b["batch"]
                        if batch.get("sampled_nonces") is None or b["end_time"] is not None:
                            continue
                        if b["slave"] not in (None, slave_name):
                            continue
                        if has_artifacts(batch["benchmark_id"], batch["batch_idx"]):
                            own_proof_work.append(b)
                awaiting_proofs = (
                    PROOF_PRIORITY_ENABLED and self._slave_awaiting_proofs(slave_name, int(now))
                )
                # root_cap=0 only when this slave can actually run proof batches now.
                # awaiting_proofs alone (esp. sampling gap) was idling CPUs with
                # "no batches available" while hundreds of root batches were pending.
                has_proof_work = bool(assigned_proofs or own_proof_work)
                finish_root_bids = (
                    self._slave_finish_root_benchmarks(slave_name)
                    if PROOF_PRIORITY_ENABLED
                    else set()
                )
                # Preferred owner of jobs that still have unfinished roots in the
                # pending list — same sticky-finish exception without a DB roundtrip.
                if PROOF_PRIORITY_ENABLED and STICKY_ROOTS_ENABLED:
                    pending_unfinished_roots = {
                        b["batch"]["benchmark_id"]
                        for b in self.batches
                        if b.get("end_time") is None
                        and (b.get("batch") or {}).get("sampled_nonces") is None
                    }
                    for bid, preferred in root_affinity.items():
                        if preferred == slave_name and bid in pending_unfinished_roots:
                            finish_root_bids.add(bid)
                root_cap_while_proofs = (
                    PROOF_PRIORITY_MAX_ROOTS if has_proof_work else max_concurrent
                )
                kept_assigned, excess_assigned = select_kept_assigned_batches(
                    assigned,
                    max_concurrent,
                    proof_priority=PROOF_PRIORITY_ENABLED and has_proof_work,
                    max_roots_while_proofs=PROOF_PRIORITY_MAX_ROOTS,
                    always_keep_root_benchmarks=finish_root_bids,
                )
                if excess_assigned:
                    logger.info(
                        f"releasing {len(excess_assigned)} excess batches from {slave_name} "
                        f"(adaptive cap={max_concurrent}"
                        f"{', proof_only_root_cap=' + str(PROOF_PRIORITY_MAX_ROOTS) if (PROOF_PRIORITY_ENABLED and has_proof_work) else ''}"
                        f"{', awaiting_proofs=1' if awaiting_proofs else ''})"
                    )
                    for b in excess_assigned:
                        batch = b["batch"]
                        table = "root_batch" if batch["sampled_nonces"] is None else "proofs_batch"  # nosec B608 — two hardcoded table names, no user input
                        updates.append((
                            f"""
                            UPDATE {table}
                            SET slave = NULL,
                                start_time = NULL,
                                end_time = NULL,
                                num_attempts = GREATEST(num_attempts - 1, 0)
                            WHERE benchmark_id = %s
                              AND batch_idx = %s
                              AND slave = %s
                              AND ready IS NULL
                            """,
                            (batch["benchmark_id"], batch["batch_idx"], slave_name)
                        ))
                        b["slave"] = None
                        b["start_time"] = None
                        b["end_time"] = None
                        b["num_attempts"] = max(0, b["num_attempts"] - 1)

                concurrent = [b["batch"] for b in kept_assigned]
                concurrent_by_bench = {}
                concurrent_roots = sum(
                    1 for batch in concurrent if batch.get("sampled_nonces") is None
                )
                for b in kept_assigned:
                    bid = b["batch"]["benchmark_id"]
                    concurrent_by_bench[bid] = concurrent_by_bench.get(bid, 0) + 1

                # Capability views: tier + track hardness + slave×track speed.
                cap_settings = capability_settings(CONFIG)
                cap_enabled = bool(cap_settings.get("enabled"))
                cap_views = {}
                slave_tier = CAPABILITY_SCHEDULER.settings(CONFIG)["default_tier"]
                job_meta = {}
                if cap_enabled:
                    try:
                        cap_views = CAPABILITY_SCHEDULER.refresh_runtime_views(
                            fetch_all=get_db_conn().fetch_all,
                            execute=get_db_conn().execute,
                            config=CONFIG,
                            now_ms=int(now),
                        )
                        live_telem = self._slave_telemetry.get(slave_name) or {}
                        slave_tier = CAPABILITY_SCHEDULER.slave_tier(
                            slave_name,
                            fetch_one=get_db_conn().fetch_one,
                            config=CONFIG,
                            now_ms=int(now),
                            live_cores=live_telem.get("cores"),
                            live_ram_gb=live_telem.get("ram_gb"),
                            skip_cache=bool(live_telem.get("cores")),
                        )
                        meta_rows = get_db_conn().fetch_all(
                            """
                            SELECT
                                J.benchmark_id,
                                J.challenge,
                                COALESCE(J.settings->>'track_id', '') AS track_id,
                                J.start_time,
                                COUNT(*) FILTER (WHERE R.ready = true) AS roots_ready
                            FROM job J
                            LEFT JOIN root_batch R ON R.benchmark_id = J.benchmark_id
                            WHERE J.stopped IS NULL
                              AND J.end_time IS NULL
                            GROUP BY J.benchmark_id, J.challenge, J.settings, J.start_time
                            """
                        ) or []
                        for row in meta_rows:
                            job_meta[str(row["benchmark_id"])] = row
                    except Exception as exc:
                        logger.warning(
                            "capability scheduler refresh failed; using FIFO assign: %s",
                            exc,
                        )
                        cap_enabled = False

                def _batch_meta(batch):
                    bid = batch["benchmark_id"]
                    meta = job_meta.get(bid) or {}
                    challenge = batch.get("challenge") or meta.get("challenge") or ""
                    settings = batch.get("settings") or {}
                    track_id = settings.get("track_id") or meta.get("track_id") or ""
                    hardness = (
                        CAPABILITY_SCHEDULER.track_hardness(
                            challenge, track_id, views=cap_views
                        )
                        if cap_enabled
                        else 0.0
                    )
                    speed = (
                        CAPABILITY_SCHEDULER.slave_speed_ratio(
                            slave_name, challenge, track_id, views=cap_views
                        )
                        if cap_enabled
                        else 1.0
                    )
                    start_time = meta.get("start_time")
                    job_age_ms = (
                        int(now) - int(start_time)
                        if start_time is not None
                        else 0
                    )
                    roots_ready = int(meta.get("roots_ready") or 0)
                    return challenge, track_id, hardness, speed, job_age_ms, roots_ready

                # Own proofs first, then other proofs, then starved roots, then
                # capability-ranked roots (hard→strong fit).
                def _batch_priority(item):
                    idx, row = item
                    batch = row["batch"]
                    is_proof = batch.get("sampled_nonces") is not None
                    own_proof = is_proof and has_artifacts(
                        batch["benchmark_id"], batch["batch_idx"]
                    )
                    starved_root = (
                        (not is_proof)
                        and batch["benchmark_id"] in starved_slot_benchmarks
                    )
                    sticky_own_unassigned = (
                        (not is_proof)
                        and row.get("slave") is None
                        and row.get("end_time") is None
                        and root_affinity.get(batch["benchmark_id"]) == slave_name
                    )
                    if (
                        not cap_enabled
                        or is_proof
                        or sticky_own_unassigned
                        or starved_root
                    ):
                        return (
                            0
                            if own_proof
                            else 1
                            if is_proof
                            else 2
                            if sticky_own_unassigned
                            else 3
                            if starved_root
                            else 4,
                            -starved_slot_benchmarks.get(batch["benchmark_id"], 0),
                            idx,
                        )
                    _, _, hardness, speed, job_age_ms, roots_ready = _batch_meta(batch)
                    return assign_rank_tuple(
                        is_proof=False,
                        own_proof=False,
                        starved_root=False,
                        starved_boost=starved_slot_benchmarks.get(batch["benchmark_id"], 0),
                        original_idx=idx,
                        slave_tier=slave_tier,
                        hardness=hardness,
                        slave_speed_ratio=speed,
                        job_age_ms=job_age_ms,
                        roots_ready=roots_ready,
                        hard_hardness=cap_settings["hard_hardness"],
                        hard_min_tier=cap_settings["hard_min_tier"],
                    )

                ordered_batches = [
                    b for _, b in sorted(enumerate(self.batches), key=_batch_priority)
                ]

                def _has_easier_claimable(min_hardness: float) -> bool:
                    if not cap_enabled:
                        return False
                    for row in ordered_batches:
                        batch = row["batch"]
                        if batch.get("sampled_nonces") is not None:
                            continue
                        if row.get("end_time") is not None:
                            continue
                        if row.get("slave") not in (None,):
                            continue
                        bid = batch["benchmark_id"]
                        if slot_types and bid not in slot_benchmark_ids:
                            continue
                        if not re.match(
                            slave["algorithm_id_regex"],
                            batch["settings"]["algorithm_id"],
                        ):
                            continue
                        preferred = root_affinity.get(bid)
                        if should_skip_root_for_slave(
                            slave_name,
                            preferred,
                            online_slaves,
                            preferred_at_cap=bool(
                                preferred and preferred in preferred_at_cap
                            ),
                        ):
                            continue
                        _, _, hardness, _, job_age_ms, _ = _batch_meta(batch)
                        if hardness < min_hardness or job_age_ms >= cap_settings["age_out_ms"]:
                            return True
                    return False

                def assign_pass(respect_cap):
                    nonlocal concurrent_roots
                    for b in ordered_batches:
                        batch = b["batch"]
                        bid = batch["benchmark_id"]
                        is_proof = batch.get("sampled_nonces") is not None
                        if len(concurrent) >= max_concurrent:
                            break
                        if (
                            b["slave"] == slave_name or
                            not re.match(slave["algorithm_id_regex"], batch["settings"]["algorithm_id"]) or
                            b["end_time"] is not None
                        ):
                            continue
                        if slot_types and bid not in slot_benchmark_ids:
                            continue
                        if is_proof and not has_artifacts(bid, batch["batch_idx"]):
                            continue
                        preferred = root_affinity.get(bid)
                        if (not is_proof) and should_skip_root_for_slave(
                            slave_name,
                            preferred,
                            online_slaves,
                            preferred_at_cap=bool(
                                preferred and preferred in preferred_at_cap
                            ),
                        ):
                            continue
                        if cap_enabled and (not is_proof):
                            _, _, hardness, _, job_age_ms, _ = _batch_meta(batch)
                            if should_skip_hard_for_weak(
                                slave_tier=slave_tier,
                                hardness=hardness,
                                hard_hardness=cap_settings["hard_hardness"],
                                hard_min_tier=cap_settings["hard_min_tier"],
                                has_easier_claimable=_has_easier_claimable(
                                    cap_settings["hard_hardness"]
                                ),
                                job_age_ms=job_age_ms,
                                age_out_ms=cap_settings["age_out_ms"],
                            ):
                                continue
                        if (
                            PROOF_PRIORITY_ENABLED
                            and has_proof_work
                            and (not is_proof)
                            and bid not in finish_root_bids
                            and concurrent_roots >= root_cap_while_proofs
                        ):
                            continue
                        if not batch_owner_stealable(
                            now_ms=int(now),
                            slave=b.get("slave"),
                            start_time=b.get("start_time"),
                            algorithm_id=batch["settings"]["algorithm_id"],
                            online_slaves=online_slaves,
                            is_proof=is_proof,
                        ):
                            continue
                        if respect_cap and concurrent_by_bench.get(bid, 0) >= per_bench_cap:
                            continue
                        b["slave"] = slave_name
                        b["start_time"] = now
                        b["num_attempts"] += 1
                        concurrent_by_bench[bid] = concurrent_by_bench.get(bid, 0) + 1
                        table = "root_batch" if not is_proof else "proofs_batch"  # nosec B608 — two hardcoded table names, no user input
                        slot_state = "proof" if is_proof else "root"
                        updates.append((
                            f"""
                            UPDATE {table}
                            SET slave = %s,
                                start_time = %s,
                                num_attempts = %s
                            WHERE benchmark_id = %s
                                AND batch_idx = %s
                            """,
                            (slave_name, now, b["num_attempts"], batch["benchmark_id"], batch["batch_idx"])
                        ))
                        if slot_types:
                            updates.append((
                                """
                                UPDATE benchmark_slot
                                SET last_activity_at = %s,
                                    state = %s
                                WHERE benchmark_id = %s
                                """,
                                (now, slot_state, batch["benchmark_id"])
                            ))
                        concurrent.append(batch)
                        if not is_proof:
                            concurrent_roots += 1

                # Pass 1: spread across benchmarks (respect per-benchmark cap) so all
                # challenges advance together. Pass 2: if slots remain because few
                # benchmarks are active, fill them ignoring the cap (use full capacity).
                assign_pass(respect_cap=True)
                assign_pass(respect_cap=False)
                if PROOF_PRIORITY_ENABLED and has_proof_work:
                    logger.info(
                        f"proof_only slave={slave_name} proofs_assigned="
                        f"{sum(1 for batch in concurrent if batch.get('sampled_nonces') is not None)} "
                        f"roots_assigned={concurrent_roots} "
                        f"root_cap={PROOF_PRIORITY_MAX_ROOTS} own_proof_work={len(own_proof_work)} "
                        f"awaiting_proofs={int(awaiting_proofs)} "
                        f"finish_root_jobs={len(finish_root_bids)}"
                    )
                assigned_starved = [
                    batch["id"]
                    for batch in concurrent
                    if batch["sampled_nonces"] is None
                    and batch["benchmark_id"] in starved_slot_benchmarks
                ]
                if assigned_starved:
                    logger.info(
                        f"prioritized {len(assigned_starved)} starved slot batches for "
                        f"{slave_name}: {assigned_starved[:8]}"
                    )
            if len(concurrent) == 0:
                logger.debug(f"no batches available for {slave_name}")
            if len(updates) > 0:
                get_db_conn().execute_many(*updates)
            logger.info(
                f"get-batches slave={slave_name} assigned={len(concurrent)} "
                f"cap={max_concurrent} route_cap={route_cap} adaptive={max_concurrent != route_cap}"
            )
            return JSONResponse(content=jsonable_encoder(concurrent))

        def find_batch(batch_id: str, request: Request):
            if (slave_name := request.headers.get('User-Agent', None)) is None:
                raise HTTPException(status_code=403, detail="User-Agent header is required")
            self._require_authorized_slave(slave_name)
            
            with self.lock:
                b = next((
                    b for b in self.batches
                    if (
                        b["batch"]["id"] == batch_id and 
                        b["slave"] == slave_name and
                        b["end_time"] is None and
                        (
                            'error' in request.url.path or
                            (b["batch"]["sampled_nonces"] is None) == ('root' in request.url.path)
                        )
                    )
                ), None)
                if b is None:
                    raise HTTPException(
                        status_code=408, 
                        detail=f"Slave {slave_name} posted to {request.url.path}, but either took too long, or was not assigned this batch."
                    )
            
            return slave_name, b

        @app.post('/submit-batch-error/{batch_id}')
        async def submit_batch_error(batch_id: str, request: Request):
            slave_name, b = find_batch(batch_id, request)
            result = await request.json()
            error = result.get("error", "")
            logger.warning(f"slave {slave_name} reported failure for {batch_id}: {error}")

            benchmark_id, batch_idx = batch_id.split("_")
            batch_idx = int(batch_idx)
            if _is_infrastructure_error(error):
                self._quarantine_slave(slave_name, error)
                return {"status": "QUARANTINED"}

            if b["num_attempts"] < CONFIG["max_batch_attempts"]:
                table_name = "root_batch" if b["batch"]["sampled_nonces"] is None else "proofs_batch"  # nosec B608 — two hardcoded table names, no user input
                queries = [
                    (
                        f"""
                        UPDATE {table_name}
                        SET slave = NULL
                        WHERE benchmark_id = %s 
                            AND batch_idx = %s
                        """, 
                        (
                            benchmark_id,
                            batch_idx
                        )
                    )
                ]
            else:
                queries = [
                    (
                        """
                        UPDATE job
                        SET stopped = True
                        WHERE benchmark_id = %s 
                        """, 
                        (
                            benchmark_id,
                        )
                    )
                ]
            get_db_conn().execute_many(*queries)

            return {"status": "OK"}

        @app.post('/submit-batch-root/{batch_id}')
        async def submit_batch_root(batch_id: str, request: Request):
            slave_name, b = find_batch(batch_id, request)
            try:
                result = await request.json()
                merkle_root = MerkleHash.from_str(result["merkle_root"])
                solution_quality = result["solution_quality"]
                if not (isinstance(solution_quality, list) and all(isinstance(x, int) for x in solution_quality)):
                    raise ValueError("solution_quality must be a list of integers")
                expected_nonces = int(b["batch"]["num_nonces"])
                if len(solution_quality) != expected_nonces:
                    raise ValueError(
                        f"solution_quality length {len(solution_quality)} != expected {expected_nonces}"
                    )
                logger.debug(f"slave {slave_name} submitted root for {batch_id}")
            except Exception as e:
                logger.error(f"slave {slave_name} submitted INVALID root for {batch_id}: {e}")
                raise HTTPException(status_code=400, detail="INVALID root")
            # Update roots table with merkle root and solution quality
            benchmark_id, batch_idx = batch_id.split("_")
            batch_idx = int(batch_idx)

            # Capability EMA: learn slave×track runtime for affinity ranking.
            try:
                settings_obj = b["batch"].get("settings") or {}
                challenge = b["batch"].get("challenge") or ""
                track_id = settings_obj.get("track_id") or ""
                start_ms = b.get("start_time")
                end_ms = int(time.time() * 1000)
                if start_ms is not None:
                    runtime_ms = max(1.0, float(end_ms) - float(start_ms))
                    nonces = float(b["batch"].get("num_nonces") or 0)
                    mpn = runtime_ms / nonces if nonces > 0 else None
                    update_slave_track_ema(
                        execute=get_db_conn().execute,
                        fetch_one=get_db_conn().fetch_one,
                        slave_name=slave_name,
                        challenge=challenge,
                        track_id=track_id,
                        runtime_ms=runtime_ms,
                        ms_per_nonce=mpn,
                        now_ms=end_ms,
                        alpha=capability_settings(CONFIG).get("ema_alpha", 0.3),
                    )
            except Exception as exc:
                logger.debug("slave_track_ema update failed: %s", exc)

            queries = [
                (
                    """
                    UPDATE root_batch
                    SET ready = true,
                        end_time = (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
                    WHERE benchmark_id = %s 
                        AND batch_idx = %s
                    """, 
                    (
                        benchmark_id,
                        batch_idx
                    )
                ),
                (
                    """
                    UPDATE batch_data
                    SET merkle_root = %s,
                        solution_quality = %s,
                        average_quality = %s
                    WHERE benchmark_id = %s 
                        AND batch_idx = %s                    
                    """,
                    (
                        merkle_root.to_str(),
                        json.dumps(solution_quality),
                        sum(solution_quality) // len(solution_quality),
                        benchmark_id,
                        batch_idx
                    )
                )
            ]
            get_db_conn().execute_many(*queries)
            # Free the in-memory assign slot immediately. DB is updated above, but
            # self.batches is only reloaded on slave_manager.run() (~5s). Until then
            # get-batches still saw end_time=None and kept re-handing this batch,
            # filling concurrent=1 so the slave sat idle ("already processed").
            with self.lock:
                b["end_time"] = int(time.time() * 1000)

            return {"status": "OK"}

        @app.post('/submit-batch-proofs/{batch_id}')
        async def submit_batch_proofs(batch_id: str, request: Request):
            slave_name, b = find_batch(batch_id, request)
            try:
                result = await request.json()
                merkle_proofs = [MerkleProof.from_dict(x) for x in result["merkle_proofs"]]
                logger.debug(f"slave {slave_name} submitted proofs for {batch_id}")
            except Exception as e:
                logger.error(f"slave {slave_name} submitted INVALID proofs for {batch_id}: {e}")
                raise HTTPException(status_code=400, detail="INVALID proofs")
            # Update proofs table with merkle proofs
            benchmark_id, batch_idx = batch_id.split("_")
            batch_idx = int(batch_idx)
            get_db_conn().execute_many(*[
                (
                    """
                    UPDATE proofs_batch
                    SET ready = true,
                        end_time = (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
                    WHERE benchmark_id = %s 
                        AND batch_idx = %s
                    """, 
                    (benchmark_id, batch_idx)
                ),
                (
                    """
                    UPDATE batch_data
                    SET merkle_proofs = %s
                    WHERE benchmark_id = %s 
                        AND batch_idx = %s
                    """, 
                    (
                        json.dumps([x.to_dict() for x in merkle_proofs]), 
                        benchmark_id, 
                        batch_idx
                    )
                )
            ])
            with self.lock:
                b["end_time"] = int(time.time() * 1000)

            return {"status": "OK"}
            
        thread = Thread(target=lambda: uvicorn.run(app, host="0.0.0.0", port=5115, access_log=False))  # nosec B104 — container binds all interfaces; nginx controls external exposure
        thread.daemon = True
        thread.start()

        logger.info(f"webserver started on 0.0.0.0:5115")
