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

logger = logging.getLogger(os.path.splitext(os.path.basename(__file__))[0])

CPU_CHALLENGE_IDS = ("c001", "c002", "c003", "c007", "c008")
GPU_CHALLENGE_IDS = ("c004", "c005", "c006")


def _env_bool(name, default="true"):
    return os.environ.get(name, default).lower() in ("1", "true", "yes", "on")


def _governor_settings():
    gov = CONFIG.get("precommit_governor") or {}
    return {
        "enabled": bool(gov["enabled"]) if "enabled" in gov else _env_bool("PRECOMMIT_GOVERNOR_ENABLED", "true"),
        "max_roots_pending": int(
            gov.get(
                "max_roots_pending",
                os.environ.get("PRECOMMIT_GOVERNOR_MAX_ROOTS_PENDING", "256"),
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
        # When CPU has spare slot capacity but no free root batches to assign,
        # do not let a low global root_ready_rate idle the CPU fleet.
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


def should_block_precommit_create(
    roots_pending,
    benchmarks_seen,
    root_ready_benchmarks,
    settings=None,
    idle_cpu_needs_work=False,
):
    """Pure create-gate used by PrecommitManager and unit tests.

    Hard backlog cap always wins. Low root_ready_rate normally blocks creates,
    but idle CPU with no unassigned CPU roots may override that soft gate so
    spare CPU workers are not left empty.
    """
    settings = settings or _governor_settings()
    if not settings.get("enabled", True):
        return False, ""
    max_roots_pending = int(settings.get("max_roots_pending") or 256)
    min_root_ready_rate = float(settings.get("min_root_ready_rate") or 0.50)
    min_samples = int(settings.get("min_samples") or 5)
    roots_pending = int(roots_pending or 0)
    benchmarks_seen = int(benchmarks_seen or 0)
    root_ready_benchmarks = int(root_ready_benchmarks or 0)

    if roots_pending >= max_roots_pending:
        return True, (
            f"root backlog {roots_pending} >= max_roots_pending {max_roots_pending}"
        )
    if roots_pending > 0 and benchmarks_seen >= min_samples:
        root_ready_rate = root_ready_benchmarks / max(1, benchmarks_seen)
        if root_ready_rate < min_root_ready_rate:
            if idle_cpu_needs_work and settings.get("idle_cpu_override", True):
                return False, (
                    f"idle_cpu_override: root_ready_rate {root_ready_rate:.3f} "
                    f"< {min_root_ready_rate:.3f} but CPU has spare capacity "
                    f"and no unassigned CPU roots"
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
        self.algorithm_name_2_id = {}
        self.challenge_name_2_id = {}
        self._governor_cache = None
        self._governor_cache_until_ms = 0

    def on_new_block(self, block: Block, **kwargs):
        self.last_block_id = block.id
        self.num_precommits_submitted = 0
        self.per_challenge_precommits_submitted = {}
        self.challenge_configs = block.config["challenges"]

    def _governor_snapshot(self) -> dict:
        settings = _governor_settings()
        if not settings.get("enabled", True):
            return {"enabled": False, "idle_cpu_needs_work": False}
        now_ms = int(time.time() * 1000)
        cache_ms = max(0, int(settings.get("cache_ms") or 0))
        if (
            self._governor_cache is not None
            and cache_ms > 0
            and now_ms < self._governor_cache_until_ms
        ):
            return self._governor_cache
        try:
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
                          AND j.settings->>'challenge_id' IN %s
                    ) AS gpu_active_jobs,
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
                    ) AS cpu_unassigned_roots
                """,
                (
                    cutoff_ms,
                    cutoff_ms,
                    cutoff_ms,
                    cutoff_ms,
                    cutoff_ms,
                    cutoff_ms,
                    cutoff_ms,
                    cutoff_ms,
                    CPU_CHALLENGE_IDS,
                    GPU_CHALLENGE_IDS,
                    CPU_CHALLENGE_IDS,
                ),
            ) or {}
            cpu_slots = _cpu_slot_target()
            cpu_create_target = _cpu_create_target(cpu_slots)
            cpu_active_jobs = int(row.get("cpu_active_jobs") or 0)
            gpu_active_jobs = int(row.get("gpu_active_jobs") or 0)
            cpu_unassigned_roots = int(row.get("cpu_unassigned_roots") or 0)
            gpu_floor = _gpu_slot_floor_total()
            # Spare CPU create budget + no free CPU root batches => bias toward
            # CPU work, but never treat inflated resource_slots.cpu as a reason
            # to permanently exclude GPU creates.
            idle_cpu_needs_work = (
                settings.get("idle_cpu_override", True)
                and cpu_slots > 0
                and cpu_unassigned_roots == 0
                and cpu_active_jobs < cpu_create_target
            )
            snapshot = {
                "enabled": True,
                "settings": settings,
                "roots_pending": int(row.get("roots_pending") or 0),
                "benchmarks_seen": int(row.get("benchmarks_seen") or 0),
                "root_ready_benchmarks": int(row.get("root_ready_benchmarks") or 0),
                "cpu_slots": cpu_slots,
                "cpu_create_target": cpu_create_target,
                "cpu_active_jobs": cpu_active_jobs,
                "gpu_active_jobs": gpu_active_jobs,
                "gpu_slot_floor": gpu_floor,
                "cpu_unassigned_roots": cpu_unassigned_roots,
                "idle_cpu_needs_work": idle_cpu_needs_work,
            }
        except Exception as exc:
            # Fail open: a transient DB blip must not freeze precommit creation.
            logger.warning("precommit governor query failed; allowing create: %s", exc)
            snapshot = {"enabled": False, "idle_cpu_needs_work": False, "error": str(exc)}
        self._governor_cache = snapshot
        self._governor_cache_until_ms = now_ms + cache_ms
        return snapshot

    def run(self) -> SubmitPrecommitRequest:
        num_pending_jobs = get_db_conn().fetch_one(
            """
            SELECT COUNT(*) 
            FROM job
            WHERE merkle_proofs_ready IS NULL
                AND stopped IS NULL
            """
        )["count"]

        algo_selection = CONFIG["algo_selection"]

        num_pending_benchmarks = num_pending_jobs + self.num_precommits_submitted
        if  num_pending_benchmarks >= CONFIG["max_concurrent_benchmarks"]:
            logger.debug(f"number of pending benchmarks has reached max of {CONFIG['max_concurrent_benchmarks']}")
            return

        governor = self._governor_snapshot()
        idle_cpu_needs_work = bool(governor.get("idle_cpu_needs_work"))
        governor_reason = ""
        if governor.get("enabled"):
            block, governor_reason = should_block_precommit_create(
                governor.get("roots_pending") or 0,
                governor.get("benchmarks_seen") or 0,
                governor.get("root_ready_benchmarks") or 0,
                governor.get("settings"),
                idle_cpu_needs_work=idle_cpu_needs_work,
            )
            if block:
                logger.info("precommit governor blocked create: %s", governor_reason)
                return
            if governor_reason.startswith("idle_cpu_override:"):
                logger.info(
                    "precommit governor allowing create via idle CPU override "
                    "(cpu_active_jobs=%s/%s create_target=%s, cpu_unassigned_roots=%s): %s",
                    governor.get("cpu_active_jobs"),
                    governor.get("cpu_slots"),
                    governor.get("cpu_create_target"),
                    governor.get("cpu_unassigned_roots"),
                    governor_reason,
                )

        # Build per-challenge pending counts keyed by challenge_id (e.g. "c004")
        per_challenge_counts = {}
        rows = get_db_conn().fetch_all(
            """
            SELECT settings->>'challenge_id' AS challenge_id, COUNT(*) AS cnt
            FROM job
            WHERE merkle_proofs_ready IS NULL
                AND stopped IS NULL
            GROUP BY settings->>'challenge_id'
            """
        )
        for row in rows:
            per_challenge_counts[row["challenge_id"]] = row["cnt"]

        per_challenge_max = CONFIG.get("per_challenge_max_benchmarks", {})

        # Filter eligible algorithms (not over their per-challenge limit)
        eligible = [
            x for x in algo_selection
            if per_challenge_max.get(x["algorithm_id"][:4]) is None
            or (
                per_challenge_counts.get(x["algorithm_id"][:4], 0)
                + self.per_challenge_precommits_submitted.get(x["algorithm_id"][:4], 0)
            ) < per_challenge_max[x["algorithm_id"][:4]]
        ]
        if not eligible:
            logger.debug("All algorithms are at their per-challenge max concurrent benchmarks")
            return

        # Idle CPU: bias toward CPU creates, but keep GPU floor filled.
        # A hard CPU-only filter previously starved GPU when resource_slots.cpu
        # was much larger than max_concurrent_benchmarks.
        gpu_floor = int(governor.get("gpu_slot_floor") or _gpu_slot_floor_total())
        gpu_active_jobs = int(governor.get("gpu_active_jobs") or 0)
        if gpu_active_jobs <= 0:
            gpu_active_jobs = sum(
                int(per_challenge_counts.get(cid, 0) or 0) for cid in GPU_CHALLENGE_IDS
            )
        gpu_below_floor = gpu_active_jobs < max(1, gpu_floor)
        # Hard CPU-only only when the rate-gate bypass itself is active.
        force_cpu_only = (
            idle_cpu_needs_work
            and (not gpu_below_floor)
            and governor_reason.startswith("idle_cpu_override:")
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
        for x in eligible:
            weight = int(x.get("weight") or 0)
            if weight <= 0:
                continue
            if idle_cpu_needs_work and not force_cpu_only and not gpu_below_floor:
                if x["algorithm_id"][:4] in CPU_CHALLENGE_IDS:
                    weight = max(1, int(round(weight * idle_mult)))
            elif gpu_below_floor and x["algorithm_id"][:4] in GPU_CHALLENGE_IDS:
                weight = max(1, int(round(weight * idle_mult)))
            weighted_eligible.append(x)
            weights.append(weight)
        if not weighted_eligible:
            logger.debug(
                "All eligible algorithms have zero weight: %s",
                [(x.get("algorithm_id"), x.get("weight")) for x in eligible],
            )
            return

        logger.debug(
            "Selecting algorithm from: %s idle_cpu=%s gpu_below_floor=%s force_cpu_only=%s",
            list(zip([x["algorithm_id"] for x in weighted_eligible], weights)),
            idle_cpu_needs_work,
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
        logger.info(f"Created precommit with algorithm: {a_id}")
        return req
