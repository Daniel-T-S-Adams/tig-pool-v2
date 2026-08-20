import os
import json
import logging
import time
import requests
from common.merkle_tree import MerkleHash, MerkleBranch, MerkleTree
from common.structs import *
from common.utils import *
from typing import Dict, List, Optional, Set, Tuple
from master.sql import get_db_conn
from master.client_manager import CONFIG
from master.proof_affinity import (
    PRE_SUBMIT_OWNER_ONLINE_MS,
    SLAVE_ONLINE_MS,
    STRANDED_PROOF_STOP_ENABLED,
    STRANDED_PROOF_STOP_MS,
    ensure_slave_seen_table,
    fetch_online_slaves,
    offline_owners,
)
from master.dispatch import pin_limit, slave_work_profile
import math

logger = logging.getLogger(os.path.splitext(os.path.basename(__file__))[0])

# Mirrored from precommit_manager — keep local to avoid import cycles.
CPU_CHALLENGE_IDS = ("c001", "c002", "c003", "c007", "c008")
GPU_CHALLENGE_IDS = ("c004", "c005", "c006")


def _job_profile(challenge_id: str) -> str:
    cid = str(challenge_id or "")[:4]
    return "gpu" if cid in GPU_CHALLENGE_IDS else "cpu"


def pin_new_job_batches(benchmark_id: str, challenge_id: str, num_batches: int) -> int:
    """Reserve new root rows for idle boxes of the matching profile.

    Leaves leftover batches unassigned so they stay claimable. Pins expire
    via existing zombie/idle shed if the box never polls.
    """
    profile = _job_profile(challenge_id)
    if int(num_batches or 0) <= 0 or not benchmark_id:
        return 0
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - int(SLAVE_ONLINE_MS)
    try:
        ensure_slave_seen_table(get_db_conn().execute)
        rows = get_db_conn().fetch_all(
            """
            SELECT ss.slave_name
            FROM slave_seen ss
            WHERE ss.last_seen >= %s
              AND COALESCE(ss.telem_state, 'idle') NOT IN
                  ('running', 'downloading', 'submitting')
              AND COALESCE(ss.telem_active, 0) <= 0
              AND NOT EXISTS (
                SELECT 1 FROM root_batch rb
                WHERE rb.slave = ss.slave_name
                  AND rb.ready IS NULL
                  AND rb.start_time IS NOT NULL
              )
              AND NOT EXISTS (
                SELECT 1 FROM proofs_batch pb
                WHERE pb.slave = ss.slave_name
                  AND pb.ready IS NULL
                  AND pb.start_time IS NOT NULL
              )
            ORDER BY ss.last_seen ASC
            """,
            (cutoff,),
        ) or []
    except Exception as exc:
        logger.warning("pin idle-slave query failed: %s", exc)
        return 0
    idle = [
        r["slave_name"]
        for r in rows
        if slave_work_profile(r.get("slave_name")) == profile
    ]
    take = pin_limit(num_batches=num_batches, idle_boxes=len(idle))
    if take <= 0:
        return 0
    updates = []
    for batch_idx, slave_name in enumerate(idle[:take]):
        updates.append(
            (
                """
                UPDATE root_batch
                SET slave = %s
                WHERE benchmark_id = %s
                  AND batch_idx = %s
                  AND slave IS NULL
                """,
                (slave_name, benchmark_id, batch_idx),
            )
        )
    if not updates:
        return 0
    try:
        get_db_conn().execute_many(*updates)
    except Exception as exc:
        logger.warning("pin batches failed job=%s: %s", benchmark_id, exc)
        return 0
    logger.info(
        "dispatch pinned %s/%s root batches on %s job %s idle=%s",
        take,
        num_batches,
        profile,
        benchmark_id,
        len(idle),
    )
    return take

# Adaptive CPU batch_size at job-create time (kill-switched off by default).
# Smaller batch_size → more root_batch rows → better fill when claimable << idle.
# Larger batch_size → fewer rows → less unassigned flood when claimable already covers idle.
ADAPTIVE_CPU_BATCH_SIZE_ENABLED = os.environ.get(
    "ADAPTIVE_CPU_BATCH_SIZE_ENABLED", "false"
).lower() in ("1", "true", "yes", "on")
ADAPTIVE_CPU_BATCH_SIZE_MIN = max(
    1, int(os.environ.get("ADAPTIVE_CPU_BATCH_SIZE_MIN", "8"))
)
ADAPTIVE_CPU_BATCH_SIZE_MAX = max(
    ADAPTIVE_CPU_BATCH_SIZE_MIN,
    int(os.environ.get("ADAPTIVE_CPU_BATCH_SIZE_MAX", "128")),
)
# When underfed, aim for this many new root batches per idle-CPU deficit unit.
ADAPTIVE_CPU_BATCH_TARGET_PER_IDLE = max(
    0.25, float(os.environ.get("ADAPTIVE_CPU_BATCH_TARGET_PER_IDLE", "1.0"))
)


def choose_adaptive_cpu_batch_size(
    configured_batch_size: int,
    num_nonces: int,
    *,
    cpu_unassigned_claimable: int = 0,
    online_idle_cpu_slaves: int = 0,
    enabled: bool = False,
    min_batch: int = 8,
    max_batch: int = 128,
    max_job_batches: int = 256,
    target_batches_per_idle: float = 1.0,
) -> Tuple[int, Dict]:
    """Pick an effective CPU job batch_size from idle/claimable telemetry.

    Pure function — safe for unit tests. Never returns a size that would push
    num_batches above max_job_batches (those jobs are created already-stopped).
    """
    configured = max(1, int(configured_batch_size or 1))
    nonces = max(1, int(num_nonces or 1))
    claimable = max(0, int(cpu_unassigned_claimable or 0))
    idle = max(0, int(online_idle_cpu_slaves or 0))
    max_batches = max(1, int(max_job_batches or 1))
    floor_for_cap = int(math.ceil(nonces / max_batches))
    lo = max(1, int(min_batch or 1), floor_for_cap)
    hi = max(lo, int(max_batch or lo))

    meta: Dict = {
        "enabled": bool(enabled),
        "configured": configured,
        "claimable": claimable,
        "idle": idle,
        "mode": "disabled",
        "target_batches": None,
    }
    if not enabled:
        return configured, meta

    configured_batches = int(math.ceil(nonces / configured))
    deficit = max(0, idle - claimable)

    if deficit > 0:
        # Underfed fleet: shrink batch_size so this job fans out more root rows.
        want = int(math.ceil(deficit * max(0.25, float(target_batches_per_idle or 1.0))))
        target_batches = min(max_batches, max(configured_batches, want, 1))
        batch_size = int(math.ceil(nonces / target_batches))
        # Only shrink vs config when underfed (never grow past configured here).
        batch_size = min(configured, batch_size)
        batch_size = max(lo, min(hi, batch_size))
        meta.update(
            {
                "mode": "shrink_for_idle",
                "deficit": deficit,
                "target_batches": target_batches,
                "effective": batch_size,
                "effective_batches": int(math.ceil(nonces / batch_size)),
            }
        )
        return batch_size, meta

    if claimable >= max(idle, 1) * 2 and configured < hi:
        # Claimable already covers idle with headroom: grow toward max to
        # reduce future unassigned floods (still clamped by max_job_batches).
        grown = min(hi, max(configured, configured * 2))
        grown = max(lo, min(hi, grown))
        # Refuse growth that would leave fewer than 1 batch.
        grown = min(grown, nonces)
        meta.update(
            {
                "mode": "grow_for_surplus",
                "deficit": 0,
                "target_batches": int(math.ceil(nonces / grown)),
                "effective": grown,
                "effective_batches": int(math.ceil(nonces / grown)),
            }
        )
        return grown, meta

    kept = max(lo, min(hi, configured))
    meta.update(
        {
            "mode": "keep_configured",
            "deficit": 0,
            "target_batches": int(math.ceil(nonces / kept)),
            "effective": kept,
            "effective_batches": int(math.ceil(nonces / kept)),
        }
    )
    return kept, meta


def fetch_cpu_adaptive_batch_telemetry(now_ms: Optional[int] = None) -> Dict:
    """Light claimable/idle snapshot for adaptive batch sizing at job create."""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    online_cutoff = now - int(SLAVE_ONLINE_MS)
    try:
        row = get_db_conn().fetch_one(
            """
            SELECT
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
                ) AS cpu_unassigned_claimable,
                (
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
                ) AS online_idle_cpu_slaves
            """,
            (CPU_CHALLENGE_IDS, online_cutoff, online_cutoff),
        ) or {}
        return {
            "cpu_unassigned_claimable": int(row.get("cpu_unassigned_claimable") or 0),
            "online_idle_cpu_slaves": int(row.get("online_idle_cpu_slaves") or 0),
            "error": None,
        }
    except Exception as exc:
        logger.warning("adaptive CPU batch telemetry failed; using configured batch_size: %s", exc)
        return {
            "cpu_unassigned_claimable": 0,
            "online_idle_cpu_slaves": 0,
            "error": str(exc),
        }

# Free root batches from dark / stuck / overloaded assignees so healthy CPUs
# can finish jobs inside the ~120 minute on-chain precommit lifetime.
# Proofs are never shed (local artifacts).
STUCK_SLAVE_SHED_ENABLED = os.environ.get("SLAVE_STUCK_SHED_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# Hard warehouse: almost no finishes in the window.
STUCK_SLAVE_SHED_MIN_INFLIGHT = max(
    1, int(os.environ.get("SLAVE_STUCK_SHED_MIN_INFLIGHT", "2"))
)
STUCK_SLAVE_SHED_MIN_AGE_MS = max(
    60_000, int(os.environ.get("SLAVE_STUCK_SHED_MIN_AGE_MS", str(12 * 60 * 1000)))
)
STUCK_SLAVE_SHED_WINDOW_MS = max(
    60_000, int(os.environ.get("SLAVE_STUCK_SHED_WINDOW_MS", str(30 * 60 * 1000)))
)
STUCK_SLAVE_SHED_MAX_COMPLETES = max(
    0, int(os.environ.get("SLAVE_STUCK_SHED_MAX_COMPLETES", "1"))
)
# Soft overload: stacked old roots with weak throughput — release only aged roots.
OVERLOAD_SLAVE_SHED_MIN_INFLIGHT = max(
    1, int(os.environ.get("SLAVE_OVERLOAD_SHED_MIN_INFLIGHT", "2"))
)
OVERLOAD_SLAVE_SHED_MIN_AGE_MS = max(
    60_000, int(os.environ.get("SLAVE_OVERLOAD_SHED_MIN_AGE_MS", str(12 * 60 * 1000)))
)
OVERLOAD_SLAVE_SHED_MAX_COMPLETES = max(
    0, int(os.environ.get("SLAVE_OVERLOAD_SHED_MAX_COMPLETES", "2"))
)
DARK_ROOT_SHED_MS = max(
    0, int(os.environ.get("SLAVE_DARK_OWNER_RECLAIM_MS", str(3 * 60 * 1000)))
)
# Heartbeating but not working: telem says active_batches=0 while holding roots.
ZOMBIE_IDLE_AGE_MS = max(
    60_000, int(os.environ.get("SLAVE_ZOMBIE_IDLE_AGE_MS", str(5 * 60 * 1000)))
)
# Single-batch backstop when telem is unavailable (old slaves / no telemetry).
ZOMBIE_SINGLE_AGE_MS = max(
    60_000, int(os.environ.get("SLAVE_ZOMBIE_SINGLE_AGE_MS", str(45 * 60 * 1000)))
)


def should_shed_slave_roots(
    *,
    inflight: int,
    oldest_age_ms: int,
    completes_in_window: int,
    owner_online: bool,
    min_inflight: int = STUCK_SLAVE_SHED_MIN_INFLIGHT,
    min_age_ms: int = STUCK_SLAVE_SHED_MIN_AGE_MS,
    max_completes: int = STUCK_SLAVE_SHED_MAX_COMPLETES,
    dark_reclaim_ms: int = DARK_ROOT_SHED_MS,
    overload_min_inflight: int = OVERLOAD_SLAVE_SHED_MIN_INFLIGHT,
    overload_min_age_ms: int = OVERLOAD_SLAVE_SHED_MIN_AGE_MS,
    overload_max_completes: int = OVERLOAD_SLAVE_SHED_MAX_COMPLETES,
    telem_active_batches: Optional[int] = None,
    zombie_idle_age_ms: int = ZOMBIE_IDLE_AGE_MS,
    zombie_single_age_ms: int = ZOMBIE_SINGLE_AGE_MS,
) -> Optional[str]:
    """Return shed reason, or None if the assignee should keep its roots.

    Reasons:
      dark_owner          — offline longer than reclaim grace
      zombie_idle         — online, telem active_batches=0, aged assigned roots
      stuck_no_progress   — online warehouse with little/no finishes
      zombie_no_progress  — online single-batch stall (no telem required)
      overloaded_slow     — online, stacked aged roots, weak throughput
    """
    if inflight <= 0:
        return None
    if not owner_online:
        if dark_reclaim_ms > 0 and oldest_age_ms > dark_reclaim_ms:
            return "dark_owner"
        return None
    # Prefer telemetry contradiction over heartbeat: slave is polling but not
    # processing the roots master still has assigned to it.
    if (
        telem_active_batches is not None
        and int(telem_active_batches) <= 0
        and oldest_age_ms >= int(zombie_idle_age_ms)
        and int(completes_in_window) <= 0
    ):
        return "zombie_idle"
    if (
        inflight >= min_inflight
        and oldest_age_ms >= min_age_ms
        and completes_in_window <= max_completes
    ):
        return "stuck_no_progress"
    if (
        inflight >= 1
        and int(completes_in_window) <= 0
        and oldest_age_ms >= int(zombie_single_age_ms)
    ):
        return "zombie_no_progress"
    if (
        inflight >= overload_min_inflight
        and oldest_age_ms >= overload_min_age_ms
        and completes_in_window <= overload_max_completes
    ):
        return "overloaded_slow"
    return None

class JobManager:
    def on_new_block(
        self,
        block: Block,
        precommits: Dict[str, Precommit],
        benchmarks: Dict[str, Benchmark],
        proofs: Dict[str, Proof],
        challenges: Dict[str, Challenge],
        algorithms: Dict[str, Code],
        binarys: Dict[str, Binary],
        **kwargs
    ):
        algo_selection = CONFIG["algo_selection"]
        # create jobs from confirmed precommits
        challenge_id_2_name = {
            c.id: c.config["name"]
            for c in challenges.values()
        }
        algorithm_id_2_name = {
            a.id: a.details.name
            for a in algorithms.values()
        }
        for benchmark_id, x in precommits.items():
            if (
                benchmark_id in proofs or
                get_db_conn().fetch_one( # check if job is already created
                    """
                    SELECT 1 
                    FROM job
                    WHERE benchmark_id = %s
                    """,
                    (benchmark_id,)
                )
            ):
                continue
                
            if block.details.height - x.details.block_started >= 60:
                logger.info(f"skipping precommit {benchmark_id} as it is over 60 blocks old")
                continue

            logger.info(f"creating job from confirmed precommit {benchmark_id}")
            c_name = challenge_id_2_name[x.settings.challenge_id]
            a_name = algorithm_id_2_name[x.settings.algorithm_id]

            bin = binarys.get(x.settings.algorithm_id, None)
            if bin is None:
                logger.error(f"batch {x.benchmark_id}: no binary-blob found for {x.settings.algorithm_id}. skipping job")
                continue
            if bin.details.download_url is None:
                logger.error(f"batch {x.benchmark_id}: no download_url found for {bin.algorithm_id}. skipping job")
                continue
            algo_sel = next(
                (s for s in algo_selection if s["algorithm_id"] == x.settings.algorithm_id),
                None
            )
            if algo_sel is None:
                logger.error(f"batch {x.benchmark_id}: no batch size found for {x.settings.algorithm_id}. skipping job")
                continue
            # Resolve track_id first — used by both batch_size and allowlist logic.
            track_id = getattr(x.settings, "track_id", None)

            # Per-track batch_size override: check track_settings[track_id]["batch_size"]
            # Falls back to algo-level batch_size. batch_size is a master-side config only
            # (stripped from precommit before submission to mainnet).
            track_batch_size = None
            if track_id:
                track_batch_size = algo_sel.get("track_settings", {}).get(track_id, {}).get("batch_size")
            batch_size = track_batch_size or algo_sel["batch_size"]
            if track_batch_size:
                logger.debug(f"job {benchmark_id}: using per-track batch_size={batch_size} for track '{track_id}'")
            max_job_batches = CONFIG.get("max_job_batches", 256)
            # Adaptive CPU batch_size: more root fanout when claimable cannot feed
            # idle CPUs; optional growth when claimable already has surplus.
            # Kill-switched via ADAPTIVE_CPU_BATCH_SIZE_ENABLED (default off).
            if (
                ADAPTIVE_CPU_BATCH_SIZE_ENABLED
                and getattr(x.settings, "challenge_id", None) in CPU_CHALLENGE_IDS
            ):
                telem = fetch_cpu_adaptive_batch_telemetry()
                adapted, adapt_meta = choose_adaptive_cpu_batch_size(
                    batch_size,
                    x.details.num_nonces,
                    cpu_unassigned_claimable=telem["cpu_unassigned_claimable"],
                    online_idle_cpu_slaves=telem["online_idle_cpu_slaves"],
                    enabled=True,
                    min_batch=ADAPTIVE_CPU_BATCH_SIZE_MIN,
                    max_batch=ADAPTIVE_CPU_BATCH_SIZE_MAX,
                    max_job_batches=int(max_job_batches or 256),
                    target_batches_per_idle=ADAPTIVE_CPU_BATCH_TARGET_PER_IDLE,
                )
                if adapted != batch_size:
                    logger.info(
                        "job %s adaptive CPU batch_size %s -> %s mode=%s "
                        "nonces=%s batches~%s claimable=%s idle_cpu=%s deficit=%s",
                        benchmark_id,
                        batch_size,
                        adapted,
                        adapt_meta.get("mode"),
                        x.details.num_nonces,
                        adapt_meta.get("effective_batches"),
                        adapt_meta.get("claimable"),
                        adapt_meta.get("idle"),
                        adapt_meta.get("deficit"),
                    )
                else:
                    logger.debug(
                        "job %s adaptive CPU batch_size kept=%s mode=%s "
                        "claimable=%s idle_cpu=%s",
                        benchmark_id,
                        batch_size,
                        adapt_meta.get("mode"),
                        adapt_meta.get("claimable"),
                        adapt_meta.get("idle"),
                    )
                batch_size = adapted
            num_batches = math.ceil(x.details.num_nonces / batch_size)
            oversized = bool(max_job_batches) and num_batches > max_job_batches

            # Per-challenge track allowlist. Tracks are assigned on-chain at random, so
            # we can't avoid precommitting to slow tracks, but we can refuse to spend
            # compute on them: if an allowlist is configured for this challenge and the
            # assigned track isn't on it, create the job already-stopped.
            track_allowlist = CONFIG.get("track_allowlist", {})
            allowed_tracks = track_allowlist.get(c_name)
            blocked_track = bool(allowed_tracks) and track_id not in allowed_tracks

            # Per-track algorithm pinning. Algorithm and track are chosen independently
            # on-chain (algorithm is locked in at precommit time, track is randomly
            # rolled by the protocol afterwards) — there is no way to submit a precommit
            # only once you know its track. So to guarantee "track X is always computed
            # by algorithm Y" (e.g. because algorithm Z hangs/scores poorly on track X),
            # we let every algorithm precommit to every track as required, but only ever
            # spend compute on the (algorithm_id, track_id) pairs we've explicitly pinned.
            # Precommits that land on a track pinned to a *different* algorithm are
            # created already-stopped, same as a blocked track above.
            track_algorithm_map = CONFIG.get("track_algorithm_map", {})
            pinned_algorithm = (track_algorithm_map.get(c_name) or {}).get(track_id)
            mismatched_algorithm = bool(pinned_algorithm) and pinned_algorithm != x.settings.algorithm_id

            skip = oversized or blocked_track or mismatched_algorithm
            if oversized:
                logger.info(
                    f"job {benchmark_id} ({c_name}): {num_batches} batches exceeds "
                    f"max_job_batches={max_job_batches}; creating as stopped (won't be benchmarked)"
                )
            if blocked_track:
                logger.info(
                    f"job {benchmark_id} ({c_name}): track '{track_id}' not in allowlist "
                    f"{allowed_tracks}; creating as stopped (won't be benchmarked)"
                )
            if mismatched_algorithm:
                logger.info(
                    f"job {benchmark_id} ({c_name}): track '{track_id}' is pinned to algorithm "
                    f"'{pinned_algorithm}', not '{x.settings.algorithm_id}'; creating as stopped "
                    f"(won't be benchmarked)"
                )
            atomic_inserts = [
                (
                    """
                    INSERT INTO job 
                    (
                        benchmark_id, 
                        settings,
                        hyperparameters,
                        num_nonces, 
                        num_batches, 
                        rand_hash, 
                        fuel_budget, 
                        batch_size, 
                        challenge,
                        algorithm,
                        download_url,
                        block_started,
                        start_time
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT)
                    ON CONFLICT (benchmark_id) DO NOTHING;
                    """,
                    (
                        benchmark_id,
                        json.dumps(asdict(x.settings)),
                        None if x.details.hyperparameters is None else json.dumps(x.details.hyperparameters),
                        x.details.num_nonces,
                        num_batches,
                        x.details.rand_hash,
                        x.details.fuel_budget,
                        batch_size,
                        c_name,
                        a_name,
                        bin.details.download_url,
                        x.details.block_started,
                    )
                ),
                (
                    """
                    INSERT INTO job_data (benchmark_id) VALUES (%s)
                    ON CONFLICT (benchmark_id) DO NOTHING;
                    """,
                    (benchmark_id,)
                )
            ]

            if skip:
                # Mark unwinnable jobs (too large, or a blocked track) stopped on arrival
                # and skip per-batch rows entirely: the slave never schedules them, and the
                # job row's presence stops re-creation from the still-live precommit.
                atomic_inserts.append((
                    """
                    UPDATE job
                    SET stopped = true,
                        end_time = (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
                    WHERE benchmark_id = %s
                    """,
                    (benchmark_id,)
                ))
            else:
                for batch_idx in range(num_batches):
                    atomic_inserts += [
                        (
                            """
                            INSERT INTO root_batch (benchmark_id, batch_idx) VALUES (%s, %s)
                            """,
                            (benchmark_id, batch_idx)
                        ),
                        (
                            """
                            INSERT INTO batch_data (benchmark_id, batch_idx) VALUES (%s, %s)
                            """,
                            (benchmark_id, batch_idx)
                        )
                    ]
            
            get_db_conn().execute_many(*atomic_inserts)
            # Pins made claimable=0: named boxes held rows other idle boxes
            # could not pull. Slaves poll ~1s; unassigned leftovers are the queue.
            if not skip and os.environ.get("PIN_NEW_JOB_BATCHES", "false").lower() in (
                "1",
                "true",
                "yes",
                "on",
            ):
                try:
                    pin_new_job_batches(
                        benchmark_id,
                        getattr(x.settings, "challenge_id", "") or "",
                        num_batches,
                    )
                except Exception as exc:
                    logger.warning("dispatch pin after create failed %s: %s", benchmark_id, exc)


        # update jobs from confirmed benchmarks
        for benchmark_id, x in benchmarks.items():
            if (
                benchmark_id in proofs or
                (result := get_db_conn().fetch_one(
                    """
                    SELECT num_batches, batch_size
                    FROM job
                    WHERE benchmark_id = %s
                        AND sampled_nonces IS NULL
                        AND stopped IS NULL
                    """,
                    (benchmark_id,)
                )) is None
            ):
                continue

            logger.info(f"updating job from confirmed benchmark {benchmark_id}")
            if x.details.stopped:
                atomic_update = [
                    (
                        """
                        UPDATE job
                        SET stopped = true,
                            end_time = (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
                        WHERE benchmark_id = %s
                        """,
                        (benchmark_id,)
                    )
                ]
            else:
                atomic_update = [
                    (
                        """
                        UPDATE job
                        SET sampled_nonces = %s
                        WHERE benchmark_id = %s
                        """,
                        (json.dumps(x.details.sampled_nonces), benchmark_id)
                    ),
                    (
                        """
                        UPDATE job_data
                        SET average_quality = %s
                        WHERE benchmark_id = %s AND average_quality IS NULL
                        """,
                        (sum(x.details.average_quality_by_bundle) // len(x.details.average_quality_by_bundle), benchmark_id)
                    )
                ]

                batch_sampled_nonces = {}
                for nonce in x.details.sampled_nonces:
                    batch_idx = nonce // result["batch_size"]
                    batch_sampled_nonces.setdefault(batch_idx, []).append(nonce)

                for batch_idx, sampled_nonces in batch_sampled_nonces.items():
                    atomic_update += [
                        (
                            """
                            INSERT INTO proofs_batch (sampled_nonces, benchmark_id, batch_idx, slave, start_time)
                            SELECT %s, A.benchmark_id, A.batch_idx, A.slave, (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
                            FROM root_batch A
                            WHERE A.benchmark_id = %s AND A.batch_idx = %s
                            """,
                            (json.dumps(sampled_nonces), benchmark_id, batch_idx)
                        )
                    ]
            
            get_db_conn().execute_many(*atomic_update)

        # update jobs from confirmed proofs
        if len(proofs) > 0:
            get_db_conn().execute(
                """
                UPDATE job
                SET end_time = (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
                WHERE end_time IS NULL
                    AND benchmark_id IN %s
                """,
                (tuple(proofs),)
            )

        # stop any expired jobs
        get_db_conn().execute(
            """
            UPDATE job
            SET stopped = true,
                end_time = (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
            WHERE (stopped IS NULL OR end_time IS NULL)
                AND %s >= block_started + 120
            """,
            (block.details.height,)
        )
        
                
    def _invalidate_roots_for_offline_owners(self, benchmark_id: str, offline: List[str]) -> int:
        """Clear ready roots owned by dark slaves so live slaves can redo them."""
        if not offline:
            return 0
        rows = get_db_conn().fetch_all(
            """
            SELECT batch_idx, slave
            FROM root_batch
            WHERE benchmark_id = %s
              AND ready = true
              AND slave IN %s
            """,
            (benchmark_id, tuple(offline)),
        ) or []
        if not rows:
            return 0
        queries = []
        for row in rows:
            queries.extend([
                (
                    """
                    UPDATE root_batch
                    SET ready = NULL,
                        slave = NULL,
                        start_time = NULL,
                        end_time = NULL,
                        num_attempts = 0
                    WHERE benchmark_id = %s
                      AND batch_idx = %s
                      AND ready = true
                    """,
                    (benchmark_id, row["batch_idx"]),
                ),
                (
                    """
                    UPDATE batch_data
                    SET merkle_root = NULL,
                        solution_quality = NULL,
                        average_quality = NULL
                    WHERE benchmark_id = %s
                      AND batch_idx = %s
                    """,
                    (benchmark_id, row["batch_idx"]),
                ),
            ])
        get_db_conn().execute_many(*queries)
        logger.warning(
            f"job {benchmark_id}: invalidated {len(rows)} root batch(es) owned by "
            f"offline slave(s) {sorted(set(r['slave'] for r in rows))} before merkle submit"
        )
        return len(rows)

    def _root_owners_online(self, benchmark_id: str, now_ms: int) -> bool:
        """False if any ready-root owner has not heartbeated recently."""
        ensure_slave_seen_table(get_db_conn().execute)
        owners = get_db_conn().fetch_all(
            """
            SELECT DISTINCT slave
            FROM root_batch
            WHERE benchmark_id = %s
              AND ready = true
              AND slave IS NOT NULL
            """,
            (benchmark_id,),
        ) or []
        owner_names = [r["slave"] for r in owners if r.get("slave")]
        if not owner_names:
            return True
        online = fetch_online_slaves(
            get_db_conn().fetch_all,
            now_ms,
            PRE_SUBMIT_OWNER_ONLINE_MS,
        )
        dark = offline_owners(owner_names, online)
        if not dark:
            return True
        self._invalidate_roots_for_offline_owners(benchmark_id, dark)
        return False

    def _shed_stuck_root_owners(self, now_ms: int):
        """Unassign unfinished roots from dark / stuck / zombie / overloaded owners.

        Does not touch proofs (local artifacts). Overload sheds only aged roots
        so freshly assigned work is not yanked; stuck/dark/zombie shed all open
        roots. Telemetry-aware zombie_idle shedding also runs from SlaveManager
        on the get-batches path.
        """
        if not STUCK_SLAVE_SHED_ENABLED:
            return
        ensure_slave_seen_table(get_db_conn().execute)
        online = fetch_online_slaves(get_db_conn().fetch_all, now_ms)
        since_ms = now_ms - STUCK_SLAVE_SHED_WINDOW_MS
        rows = get_db_conn().fetch_all(
            """
            SELECT
                r.slave AS slave_name,
                COUNT(*) FILTER (
                    WHERE r.ready IS NULL
                      AND r.end_time IS NULL
                      AND r.start_time IS NOT NULL
                ) AS inflight,
                COALESCE(
                    MAX(
                        CASE
                            WHEN r.ready IS NULL
                             AND r.end_time IS NULL
                             AND r.start_time IS NOT NULL
                            THEN %s - r.start_time
                            ELSE NULL
                        END
                    ),
                    0
                ) AS oldest_age_ms,
                COUNT(*) FILTER (
                    WHERE r.ready = true
                      AND r.end_time IS NOT NULL
                      AND r.end_time >= %s
                ) AS completes_in_window
            FROM root_batch r
            WHERE r.slave IS NOT NULL
            GROUP BY r.slave
            """,
            (now_ms, since_ms),
        ) or []
        to_shed = []
        for row in rows:
            slave = str(row.get("slave_name") or "")
            if not slave:
                continue
            reason = should_shed_slave_roots(
                inflight=int(row.get("inflight") or 0),
                oldest_age_ms=int(row.get("oldest_age_ms") or 0),
                completes_in_window=int(row.get("completes_in_window") or 0),
                owner_online=slave in online,
            )
            if reason:
                to_shed.append((slave, reason, int(row.get("inflight") or 0)))
        if not to_shed:
            return
        queries = []
        for slave, reason, inflight in to_shed:
            if reason == "overloaded_slow":
                logger.warning(
                    f"shedding aged unfinished root batch(es) from {slave} "
                    f"(reason={reason}, inflight={inflight}, "
                    f"min_age_ms={OVERLOAD_SLAVE_SHED_MIN_AGE_MS})"
                )
                queries.append((
                    """
                    UPDATE root_batch
                    SET slave = NULL,
                        start_time = NULL,
                        end_time = NULL
                    WHERE slave = %s
                      AND ready IS NULL
                      AND start_time IS NOT NULL
                      AND (%s - start_time) >= %s
                    """,
                    (slave, now_ms, OVERLOAD_SLAVE_SHED_MIN_AGE_MS),
                ))
            else:
                logger.warning(
                    f"shedding {inflight} unfinished root batch(es) from {slave} "
                    f"(reason={reason})"
                )
                queries.append((
                    """
                    UPDATE root_batch
                    SET slave = NULL,
                        start_time = NULL,
                        end_time = NULL
                    WHERE slave = %s
                      AND ready IS NULL
                    """,
                    (slave,),
                ))
        if queries:
            get_db_conn().execute_many(*queries)

    def _stop_stranded_proof_jobs(self, now_ms: int):
        """Stop jobs whose remaining proofs are stuck on offline artifact owners."""
        if not STRANDED_PROOF_STOP_ENABLED:
            return
        ensure_slave_seen_table(get_db_conn().execute)
        online = fetch_online_slaves(get_db_conn().fetch_all, now_ms)
        cutoff = now_ms - STRANDED_PROOF_STOP_MS
        rows = get_db_conn().fetch_all(
            """
            SELECT
                p.benchmark_id,
                p.batch_idx,
                r.slave AS root_slave,
                p.slave AS proof_slave,
                COALESCE(j.benchmark_submit_time, j.start_time) AS stuck_since
            FROM proofs_batch p
            JOIN root_batch r
              ON r.benchmark_id = p.benchmark_id
             AND r.batch_idx = p.batch_idx
             AND r.ready = true
            JOIN job j ON j.benchmark_id = p.benchmark_id
            WHERE p.ready IS NULL
              AND j.stopped IS NULL
              AND j.end_time IS NULL
              AND j.merkle_root_ready = true
              AND COALESCE(j.benchmark_submit_time, j.start_time) IS NOT NULL
              AND COALESCE(j.benchmark_submit_time, j.start_time) < %s
            """,
            (cutoff,),
        ) or []
        to_stop = []
        for row in rows:
            owner = row.get("root_slave")
            if not owner or owner in online:
                continue
            proof_slave = row.get("proof_slave")
            # Unassigned, or assigned to a slave that is also dark (usually the owner).
            if proof_slave and proof_slave in online:
                continue
            to_stop.append(row)
        if not to_stop:
            return
        by_job: Dict[str, list] = {}
        for row in to_stop:
            by_job.setdefault(str(row["benchmark_id"]), []).append(row)
        queries = []
        for benchmark_id, items in by_job.items():
            owners = sorted({str(i["root_slave"]) for i in items})
            logger.warning(
                f"job {benchmark_id}: stopping — stranded proof batch(es) "
                f"{[i['batch_idx'] for i in items]} owned by offline slave(s) {owners}"
            )
            queries.append((
                """
                UPDATE job
                SET stopped = true,
                    end_time = (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
                WHERE benchmark_id = %s
                  AND stopped IS NULL
                  AND end_time IS NULL
                """,
                (benchmark_id,),
            ))
            # Mark unfinished proofs/roots closed so slots and metrics settle.
            queries.append((
                """
                UPDATE proofs_batch
                SET ready = false,
                    slave = NULL,
                    end_time = (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
                WHERE benchmark_id = %s
                  AND ready IS NULL
                """,
                (benchmark_id,),
            ))
        if queries:
            get_db_conn().execute_many(*queries)

    def run(self):
        now = int(time.time() * 1000)

        # Find jobs where all root_batchs are ready
        # NOTE: COUNT(A.ready) only checks non-NULL; ready=false (abandon/stop)
        # must NOT count as complete. Require every batch ready=true.
        rows = get_db_conn().fetch_all(
            """
            WITH ready AS (
                SELECT A.benchmark_id
                FROM root_batch A
                INNER JOIN job B
                    ON B.merkle_root_ready IS NULL
                    AND B.stopped IS NULL
                    AND B.end_time IS NULL
                    AND A.benchmark_id = B.benchmark_id
                GROUP BY A.benchmark_id
                HAVING BOOL_AND(A.ready IS TRUE)
            )
            SELECT 
                A.benchmark_id, 
                JSONB_AGG(B.merkle_root ORDER BY B.batch_idx) AS batch_merkle_roots,
                JSONB_AGG(B.solution_quality) AS solution_quality
            FROM ready A
            INNER JOIN batch_data B
                ON A.benchmark_id = B.benchmark_id
            GROUP BY A.benchmark_id
            """
        )

        # Calculate merkle roots for completed jobs
        for row in rows:
            benchmark_id = row['benchmark_id']
            if (
                row['batch_merkle_roots'] is None
                or row['solution_quality'] is None
                or any(x is None for x in row['batch_merkle_roots'])
                or any(x is None for x in row['solution_quality'])
            ):
                logger.warning(
                    f"job {benchmark_id}: skipping merkle root assembly "
                    f"(null batch merkle_root/solution_quality)"
                )
                continue
            # Do not submit merkle while any root owner is dark — invalidate those
            # roots so a live slave can redo them and proofs stay completable.
            if not self._root_owners_online(benchmark_id, now):
                continue
            solution_quality = [x for y in row['solution_quality'] for x in y]
            if not solution_quality:
                logger.warning(
                    f"job {benchmark_id}: skipping merkle root assembly "
                    f"(empty solution_quality)"
                )
                continue
            average_quality = sum(solution_quality) // len(solution_quality)

            batch_merkle_roots = [MerkleHash.from_str(root) for root in row['batch_merkle_roots']]
            num_batches = len(batch_merkle_roots)
            
            logger.info(f"job {benchmark_id}: (benchmark ready, average_solution_nonces: {average_quality}")

            tree = MerkleTree(
                batch_merkle_roots,
                1 << (num_batches - 1).bit_length()
            )
            merkle_root = tree.calc_merkle_root()

            # Update the database with calculated merkle root
            get_db_conn().execute_many(*[
                (
                    """
                    UPDATE job_data
                    SET merkle_root = %s, 
                        solution_quality = %s,
                        average_quality = %s
                    WHERE benchmark_id = %s
                    """, 
                    (
                        merkle_root.to_str(), 
                        json.dumps(solution_quality),
                        average_quality,
                        benchmark_id
                    )
                ),
                (
                    """
                    UPDATE job
                    SET merkle_root_ready = true
                    WHERE benchmark_id = %s
                    """,
                    (benchmark_id,)
                )
            ])
            
        # Find jobs where all proofs_batchs are ready
        # Same ready=false trap as roots: abandon/stop/zombie cleanup marks
        # unfinished proofs ready=false with null merkle_proofs. Also skip
        # stopped/ended jobs (proof CTE previously lacked that filter).
        rows = get_db_conn().fetch_all(
            """
            WITH ready AS (
                SELECT A.benchmark_id
                FROM proofs_batch A
                INNER JOIN job B
                    ON B.merkle_root_ready
                    AND B.merkle_proofs_ready IS NULL
                    AND B.stopped IS NULL
                    AND B.end_time IS NULL
                    AND A.benchmark_id = B.benchmark_id
                GROUP BY A.benchmark_id
                HAVING BOOL_AND(A.ready IS TRUE)
            )
            SELECT 
                A.benchmark_id, 
                JSONB_AGG(D.merkle_proofs ORDER BY D.batch_idx) AS batch_merkle_proofs,
                B.batch_size, 
                B.num_batches
            FROM ready A
            INNER JOIN job B 
                ON A.benchmark_id = B.benchmark_id
            INNER JOIN proofs_batch C
                ON A.benchmark_id = C.benchmark_id
            INNER JOIN batch_data D
                ON C.benchmark_id = D.benchmark_id 
                AND C.batch_idx = D.batch_idx
            GROUP BY 
                A.benchmark_id, 
                B.batch_size, 
                B.num_batches
            """
        )

        for row in rows:
            benchmark_id = row["benchmark_id"]
            raw_batch_proofs = row["batch_merkle_proofs"]
            if raw_batch_proofs is None or any(y is None for y in raw_batch_proofs):
                logger.warning(
                    f"job {benchmark_id}: skipping proof assembly "
                    f"(null batch merkle_proofs)"
                )
                continue
            batch_merkle_proofs = [
                MerkleProof.from_dict(x) 
                for y in raw_batch_proofs 
                for x in y
            ]

            batch_merkle_roots = get_db_conn().fetch_one(
                """
                SELECT JSONB_AGG(merkle_root ORDER BY batch_idx) as batch_merkle_roots
                FROM batch_data
                WHERE benchmark_id = %s
                """,
                (benchmark_id,)
            )

            batch_merkle_roots = batch_merkle_roots["batch_merkle_roots"]
            if batch_merkle_roots is None or any(r is None for r in batch_merkle_roots):
                logger.warning(
                    f"job {benchmark_id}: skipping proof assembly "
                    f"(null batch merkle_roots)"
                )
                continue
            
            logger.info(f"job {benchmark_id}: (proof ready)")
            
            depth_offset = (row["batch_size"] - 1).bit_length()
            tree = MerkleTree(
                [MerkleHash.from_str(root) for root in batch_merkle_roots],
                1 << (row["num_batches"] - 1).bit_length()
            )
            
            merkle_proofs = []       
            for proof in batch_merkle_proofs:
                batch_idx = proof.leaf.nonce // row["batch_size"]
                upper_stems = [
                    (d + depth_offset, h)
                    for d, h in tree.calc_merkle_branch(batch_idx).stems
                ]
                
                merkle_proofs.append(
                    MerkleProof(
                        leaf=proof.leaf,
                        branch=MerkleBranch(proof.branch.stems + upper_stems)
                    )
                )
                    
            # Update database with calculated merkle proofs
            get_db_conn().execute_many(*[
                (
                    """
                    UPDATE job_data
                    SET merkle_proofs = %s
                    WHERE benchmark_id = %s
                    """, 
                    (
                        json.dumps([x.to_dict() for x in merkle_proofs]), 
                        benchmark_id
                    )
                ),
                (
                    """
                    UPDATE job
                    SET merkle_proofs_ready = true
                    WHERE benchmark_id = %s
                    """,
                    (benchmark_id,)
                )
            ])

        self._shed_stuck_root_owners(now)
        self._stop_stranded_proof_jobs(now)
