"""Capability-aware scheduling helpers for InnoPool master.

Uses declared/preflight hardware, local root_batch timings, and optional TIG
tracks_data priors to rank slave↔batch fit. Sticky/proof locality rules stay
in slave_manager; this module only scores and filters claimable roots.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("capability_scheduler")

TIER_S = 0
TIER_M = 1
TIER_L = 2
TIER_XL = 3
TIER_NAMES = {TIER_S: "S", TIER_M: "M", TIER_L: "L", TIER_XL: "XL"}
TIER_FROM_NAME = {v: k for k, v in TIER_NAMES.items()}

# Default core thresholds: tier is the first bucket whose ceiling the cores are under.
# <32 → S, <64 → M, <96 → L, else XL.
DEFAULT_TIER_CORE_CEILINGS = (32, 64, 96)

_TRACK_INT_RE = re.compile(
    r"(n_vars|n_nodes|n_queries|n_h_edges|n_hidden|n_items|n_jobs|n)=(\d+)",
    re.IGNORECASE,
)

_CHALLENGE_NAME_TO_ID = {
    "satisfiability": "c001",
    "vehicle_routing": "c002",
    "knapsack": "c003",
    "vector_search": "c004",
    "hypergraph": "c005",
    "neuralnet_optimizer": "c006",
    "job_scheduling": "c007",
    "energy_arbitrage": "c008",
}


def _env_bool(name: str, default: str = "true") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes", "on")


def capability_settings(config: Optional[Mapping[str, Any]] = None) -> dict:
    """Merge CONFIG.capability_scheduler with env defaults."""
    cfg = dict((config or {}).get("capability_scheduler") or {})
    ceilings = cfg.get("tier_core_ceilings") or list(DEFAULT_TIER_CORE_CEILINGS)
    if isinstance(ceilings, dict):
        ceilings = [
            int(ceilings.get("S", 32)),
            int(ceilings.get("M", 64)),
            int(ceilings.get("L", 96)),
        ]
    else:
        ceilings = [int(x) for x in ceilings][:3]
        while len(ceilings) < 3:
            ceilings.append(DEFAULT_TIER_CORE_CEILINGS[len(ceilings)])
    return {
        "enabled": bool(cfg["enabled"])
        if "enabled" in cfg
        else _env_bool("CAPABILITY_SCHEDULER_ENABLED", "true"),
        "tier_core_ceilings": tuple(ceilings),
        "default_tier": int(
            cfg.get(
                "default_tier",
                os.environ.get("CAPABILITY_SCHEDULER_DEFAULT_TIER", str(TIER_M)),
            )
        ),
        "hard_hardness": float(
            cfg.get(
                "hard_hardness",
                os.environ.get("CAPABILITY_SCHEDULER_HARD_HARDNESS", "0.65"),
            )
        ),
        "hard_min_tier": int(
            cfg.get(
                "hard_min_tier",
                os.environ.get("CAPABILITY_SCHEDULER_HARD_MIN_TIER", str(TIER_L)),
            )
        ),
        "age_out_ms": int(
            cfg.get(
                "age_out_ms",
                os.environ.get(
                    "CAPABILITY_SCHEDULER_AGE_OUT_MS", str(20 * 60 * 1000)
                ),
            )
        ),
        "soft_p95_ms": float(
            cfg.get(
                "soft_p95_ms",
                os.environ.get("CAPABILITY_SCHEDULER_SOFT_P95_MS", str(5 * 60 * 1000)),
            )
        ),
        "hard_p95_ms": float(
            cfg.get(
                "hard_p95_ms",
                os.environ.get(
                    "CAPABILITY_SCHEDULER_HARD_P95_MS", str(30 * 60 * 1000)
                ),
            )
        ),
        "local_weight": float(
            cfg.get(
                "local_weight",
                os.environ.get("CAPABILITY_SCHEDULER_LOCAL_WEIGHT", "0.75"),
            )
        ),
        "cache_ms": int(
            cfg.get(
                "cache_ms",
                os.environ.get("CAPABILITY_SCHEDULER_CACHE_MS", "15000"),
            )
        ),
        "ema_alpha": float(
            cfg.get(
                "ema_alpha",
                os.environ.get("CAPABILITY_SCHEDULER_EMA_ALPHA", "0.3"),
            )
        ),
        "per_family_adaptive_caps": bool(cfg["per_family_adaptive_caps"])
        if "per_family_adaptive_caps" in cfg
        else _env_bool("CAPABILITY_PER_FAMILY_ADAPTIVE_CAPS", "true"),
        "strong_tier_min": int(
            cfg.get(
                "strong_tier_min",
                os.environ.get("CAPABILITY_SCHEDULER_STRONG_TIER_MIN", str(TIER_L)),
            )
        ),
        "hard_open_per_strong": float(
            cfg.get(
                "hard_open_per_strong",
                os.environ.get("CAPABILITY_SCHEDULER_HARD_OPEN_PER_STRONG", "8"),
            )
        ),
        "low_spec_tier_penalty": int(
            cfg.get(
                "low_spec_tier_penalty",
                os.environ.get("CAPABILITY_SCHEDULER_LOW_SPEC_PENALTY", "1"),
            )
        ),
    }


def hardware_tier(
    *,
    threads: Optional[int] = None,
    declared_cores: Optional[int] = None,
    ram_gb: Optional[int] = None,
    preflight_status: Optional[str] = None,
    ceilings: Sequence[int] = DEFAULT_TIER_CORE_CEILINGS,
    default_tier: int = TIER_M,
    low_spec_penalty: int = 1,
) -> int:
    """Map measured/declared cores to S/M/L/XL.

    Prefers preflight threads over declared_cores. Unknown hardware → default_tier.
    low_spec_override demotes by low_spec_penalty (floored at S).
    """
    cores = None
    if threads is not None and int(threads) > 0:
        cores = int(threads)
    elif declared_cores is not None and int(declared_cores) > 0:
        cores = int(declared_cores)

    if cores is None:
        tier = max(TIER_S, min(TIER_XL, int(default_tier)))
    else:
        tier = TIER_XL
        for idx, ceiling in enumerate(tuple(ceilings)[:3]):
            if cores < int(ceiling):
                tier = idx
                break

    # Tiny RAM relative to cores → soft demote (e.g. 96c / 16GB is not XL-capable).
    if ram_gb is not None and int(ram_gb) > 0 and cores is not None:
        if int(ram_gb) < 24 and tier > TIER_S:
            tier -= 1
        elif int(ram_gb) < 48 and tier >= TIER_XL:
            tier = TIER_L

    if (preflight_status or "").lower() == "low_spec_override" and low_spec_penalty > 0:
        tier = max(TIER_S, tier - int(low_spec_penalty))
    return tier


def tier_name(tier: int) -> str:
    return TIER_NAMES.get(int(tier), "M")


def parse_track_size(track_id: Optional[str]) -> Dict[str, int]:
    """Extract integer size knobs from a track_id string."""
    out: Dict[str, int] = {}
    if not track_id:
        return out
    for key, val in _TRACK_INT_RE.findall(str(track_id)):
        out[key.lower()] = int(val)
    return out


def heuristic_track_hardness(challenge: Optional[str], track_id: Optional[str]) -> float:
    """Cold-start hardness prior from track_id size knobs (0..1)."""
    sizes = parse_track_size(track_id)
    challenge = (challenge or "").lower()
    score = 0.35
    tid = (track_id or "").lower()

    def _scale(value: float, lo: float, hi: float) -> float:
        if hi <= lo:
            return 0.0
        return max(0.0, min(1.0, (value - lo) / (hi - lo)))

    if "n_vars" in sizes:
        score = max(score, 0.25 + 0.75 * _scale(sizes["n_vars"], 5_000, 100_000))
    if "n_nodes" in sizes:
        score = max(score, 0.2 + 0.7 * _scale(sizes["n_nodes"], 600, 1000))
    if "n_queries" in sizes:
        score = max(score, 0.15 + 0.6 * _scale(sizes["n_queries"], 7000, 15000))
    if "n_h_edges" in sizes:
        score = max(score, 0.2 + 0.7 * _scale(sizes["n_h_edges"], 10_000, 200_000))
    if "n_hidden" in sizes:
        score = max(score, 0.2 + 0.65 * _scale(sizes["n_hidden"], 4, 18))
    if "n_items" in sizes:
        score = max(score, 0.15 + 0.55 * _scale(sizes["n_items"], 1000, 5000))
    if challenge in ("job_scheduling", "c007") or "n_jobs" in sizes or "n=" in tid:
        if "fjsp" in tid or "hybrid" in tid:
            score = max(score, 0.7)
        elif "job_shop" in tid:
            score = max(score, 0.55)
        else:
            score = max(score, 0.4)
    if challenge in ("energy_arbitrage", "c008"):
        if "capstone" in tid or "dense" in tid:
            score = max(score, 0.75)
        elif "congested" in tid:
            score = max(score, 0.55)
        else:
            score = max(score, 0.4)
    return max(0.0, min(1.0, score))


def hardness_from_p95_ms(
    p95_ms: Optional[float],
    *,
    soft_p95_ms: float = 5 * 60 * 1000,
    hard_p95_ms: float = 30 * 60 * 1000,
) -> Optional[float]:
    """Map observed p95 batch runtime to 0..1 hardness."""
    if p95_ms is None:
        return None
    p95 = float(p95_ms)
    if p95 <= 0:
        return 0.0
    if p95 <= soft_p95_ms:
        return 0.5 * (p95 / soft_p95_ms)
    if p95 >= hard_p95_ms:
        return 1.0
    return 0.5 + 0.5 * ((p95 - soft_p95_ms) / max(1.0, hard_p95_ms - soft_p95_ms))


def blend_hardness(
    *,
    local_hardness: Optional[float] = None,
    heuristic: Optional[float] = None,
    tig_prior: Optional[float] = None,
    local_weight: float = 0.75,
) -> float:
    """Blend local runtime hardness with cold priors."""
    priors = [x for x in (heuristic, tig_prior) if x is not None]
    prior = sum(priors) / len(priors) if priors else 0.4
    if local_hardness is None:
        return max(0.0, min(1.0, prior))
    w = max(0.0, min(1.0, float(local_weight)))
    return max(0.0, min(1.0, w * float(local_hardness) + (1.0 - w) * prior))


def tig_track_prior(
    tracks_data: Optional[Mapping[str, Any]],
    challenge_id: Optional[str],
    track_id: Optional[str],
) -> Optional[float]:
    """Derive a weak prior from TIG tracks_data average_quality / num_bundles."""
    if not tracks_data or not challenge_id or not track_id:
        return None
    by_challenge = tracks_data.get(challenge_id) or tracks_data.get(str(challenge_id))
    if not by_challenge:
        return None
    entries = by_challenge.get(track_id)
    if not entries:
        return None
    qualities = []
    bundles = []
    for item in entries:
        if hasattr(item, "average_quality"):
            qualities.append(float(item.average_quality or 0))
            bundles.append(float(getattr(item, "num_bundles", 0) or 0))
        elif isinstance(item, Mapping):
            qualities.append(float(item.get("average_quality") or 0))
            bundles.append(float(item.get("num_bundles") or 0))
    if not qualities:
        return None
    avg_q = sum(qualities) / len(qualities)
    avg_b = sum(bundles) / len(bundles) if bundles else 0.0
    q_part = max(0.0, min(0.6, math.log10(avg_q + 1.0) / 6.0))
    b_part = max(0.0, min(0.4, avg_b / 50.0))
    return max(0.0, min(1.0, 0.3 + q_part + b_part))


def min_tier_for_hardness(hardness: float, hard_hardness: float, hard_min_tier: int) -> int:
    if float(hardness) >= float(hard_hardness):
        return max(TIER_S, min(TIER_XL, int(hard_min_tier)))
    if float(hardness) >= float(hard_hardness) * 0.75:
        return max(TIER_S, min(TIER_XL, int(hard_min_tier) - 1))
    return TIER_S


def should_skip_hard_for_weak(
    *,
    slave_tier: int,
    hardness: float,
    hard_hardness: float,
    hard_min_tier: int,
    has_easier_claimable: bool,
    job_age_ms: int,
    age_out_ms: int,
) -> bool:
    """Weak slaves skip hard work when easier claimable roots exist."""
    if int(job_age_ms) >= int(age_out_ms):
        return False
    need = min_tier_for_hardness(hardness, hard_hardness, hard_min_tier)
    if int(slave_tier) >= need:
        return False
    if float(hardness) < float(hard_hardness):
        return False
    return bool(has_easier_claimable)


def assign_rank_tuple(
    *,
    is_proof: bool,
    own_proof: bool,
    starved_root: bool,
    starved_boost: int,
    original_idx: int,
    slave_tier: int,
    hardness: float,
    slave_speed_ratio: float,
    job_age_ms: int,
    roots_ready: int,
    hard_hardness: float,
    hard_min_tier: int,
) -> tuple:
    """Sort key for ordered_batches (lower is better)."""
    if own_proof:
        bucket = 0
    elif is_proof:
        bucket = 1
    elif starved_root:
        bucket = 2
    else:
        bucket = 3

    need = min_tier_for_hardness(hardness, hard_hardness, hard_min_tier)
    fit = float(slave_tier - need)
    speed = 0.0
    if slave_speed_ratio > 0:
        speed = max(-2.0, min(2.0, 1.0 - float(slave_speed_ratio)))
    first_owner = 0.0
    if roots_ready == 0 and hardness >= hard_hardness:
        first_owner = float(slave_tier) - float(hard_min_tier)
    age_boost = min(2.0, float(job_age_ms) / float(60 * 60 * 1000))
    score = fit * 2.0 + speed + first_owner + age_boost + hardness * 0.25
    return (
        bucket,
        -int(starved_boost),
        -score,
        original_idx,
    )


def precommit_hardness_weight_mult(
    *,
    hardness: float,
    hard_hardness: float,
    strong_online: int,
    hard_open_roots: int,
    hard_open_per_strong: float,
    inventory_known: bool = True,
) -> float:
    """Down-weight hard-track creates when strong census is saturated.

    Fail open (mult=1.0) when the fleet has no measured/declared core inventory:
    strong_online=0 then means "unknown", not "zero strong CPUs".
    """
    if float(hardness) < float(hard_hardness):
        return 1.0
    if not inventory_known:
        return 1.0
    strong = max(0, int(strong_online))
    if strong <= 0:
        return 0.15
    capacity = strong * max(1.0, float(hard_open_per_strong))
    open_roots = max(0, int(hard_open_roots))
    if open_roots <= capacity:
        return 1.0
    over = open_roots / capacity
    return max(0.1, min(1.0, 1.0 / over))


def algo_is_schedulable(
    algorithm_id: str,
    *,
    algorithms: Optional[Iterable[Any]] = None,
    binarys: Optional[Iterable[Any]] = None,
    block_round: Optional[int] = None,
) -> Tuple[bool, str]:
    """Skip banned / inactive / missing-binary algorithms."""
    aid = str(algorithm_id or "")
    if not aid:
        return False, "missing_algorithm_id"

    algo_map = {}
    for code in algorithms or []:
        cid = getattr(code, "id", None) or (
            code.get("id") if isinstance(code, Mapping) else None
        )
        if cid:
            algo_map[str(cid)] = code
    if algo_map and aid in algo_map:
        code = algo_map[aid]
        state = getattr(code, "state", None)
        if state is None and isinstance(code, Mapping):
            state = code.get("state")
        banned = False
        round_active = None
        if state is not None:
            banned = bool(getattr(state, "banned", None))
            if isinstance(state, Mapping):
                banned = bool(state.get("banned"))
            round_active = getattr(state, "round_active", None)
            if isinstance(state, Mapping):
                round_active = state.get("round_active")
        if banned:
            return False, "banned"
        if (
            block_round is not None
            and round_active is not None
            and int(round_active) > int(block_round)
        ):
            return False, "not_active"

    bin_map = {}
    for binary in binarys or []:
        bid = getattr(binary, "algorithm_id", None) or (
            binary.get("algorithm_id") if isinstance(binary, Mapping) else None
        )
        if bid:
            bin_map[str(bid)] = binary
    if bin_map:
        binary = bin_map.get(aid)
        if binary is None:
            return False, "no_binary"
        details = getattr(binary, "details", None)
        if details is None and isinstance(binary, Mapping):
            details = binary.get("details")
        compile_success = True
        download_url = "x"
        if details is not None:
            if isinstance(details, Mapping):
                compile_success = details.get("compile_success", True)
                download_url = details.get("download_url")
            else:
                compile_success = getattr(details, "compile_success", True)
                download_url = getattr(details, "download_url", None)
        if not compile_success:
            return False, "compile_failed"
        if not download_url:
            return False, "no_download_url"
    return True, ""


def ensure_slave_track_ema_table(execute) -> None:
    execute(
        """
        CREATE TABLE IF NOT EXISTS slave_track_ema (
            slave_name TEXT NOT NULL,
            challenge TEXT NOT NULL,
            track_id TEXT NOT NULL,
            ema_runtime_ms DOUBLE PRECISION NOT NULL,
            ema_ms_per_nonce DOUBLE PRECISION,
            sample_n INTEGER NOT NULL DEFAULT 0,
            updated_at BIGINT NOT NULL,
            PRIMARY KEY (slave_name, challenge, track_id)
        )
        """
    )
    execute(
        """
        CREATE INDEX IF NOT EXISTS idx_slave_track_ema_updated
        ON slave_track_ema (updated_at)
        """
    )


def update_slave_track_ema(
    *,
    execute,
    fetch_one,
    slave_name: str,
    challenge: str,
    track_id: str,
    runtime_ms: float,
    ms_per_nonce: Optional[float],
    now_ms: Optional[int] = None,
    alpha: float = 0.3,
) -> None:
    """Upsert EMA row for (slave, challenge, track)."""
    if not slave_name or runtime_ms <= 0:
        return
    ensure_slave_track_ema_table(execute)
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    challenge = str(challenge or "")
    track_id = str(track_id or "")
    row = fetch_one(
        """
        SELECT ema_runtime_ms, ema_ms_per_nonce, sample_n
        FROM slave_track_ema
        WHERE slave_name = %s AND challenge = %s AND track_id = %s
        """,
        (slave_name, challenge, track_id),
    )
    a = max(0.05, min(0.9, float(alpha)))
    if row:
        prev = float(row.get("ema_runtime_ms") or runtime_ms)
        ema = a * float(runtime_ms) + (1.0 - a) * prev
        prev_mpn = row.get("ema_ms_per_nonce")
        if ms_per_nonce is not None and prev_mpn is not None:
            mpn = a * float(ms_per_nonce) + (1.0 - a) * float(prev_mpn)
        elif ms_per_nonce is not None:
            mpn = float(ms_per_nonce)
        else:
            mpn = prev_mpn
        n = int(row.get("sample_n") or 0) + 1
        execute(
            """
            UPDATE slave_track_ema
            SET ema_runtime_ms = %s,
                ema_ms_per_nonce = %s,
                sample_n = %s,
                updated_at = %s
            WHERE slave_name = %s AND challenge = %s AND track_id = %s
            """,
            (ema, mpn, n, now_ms, slave_name, challenge, track_id),
        )
    else:
        execute(
            """
            INSERT INTO slave_track_ema (
                slave_name, challenge, track_id,
                ema_runtime_ms, ema_ms_per_nonce, sample_n, updated_at
            ) VALUES (%s, %s, %s, %s, %s, 1, %s)
            """,
            (
                slave_name,
                challenge,
                track_id,
                float(runtime_ms),
                float(ms_per_nonce) if ms_per_nonce is not None else None,
                now_ms,
            ),
        )


def _parse_report(report: Any) -> Mapping:
    if isinstance(report, Mapping):
        return report
    if isinstance(report, str):
        try:
            parsed = json.loads(report)
            if isinstance(parsed, Mapping):
                return parsed
        except Exception:
            return {}
    return {}


class CapabilityScheduler:
    """Cached DB-backed views for assign/precommit."""

    def __init__(self):
        self._cache = None
        self._cache_until_ms = 0
        self._tier_cache: Dict[str, Tuple[int, int]] = {}
        self._tracks_data = None
        self._algorithms = None
        self._binarys = None
        self._block_round = None

    def set_tig_context(
        self,
        *,
        tracks_data=None,
        algorithms=None,
        binarys=None,
        block_round=None,
    ) -> None:
        self._tracks_data = tracks_data
        self._algorithms = algorithms
        self._binarys = binarys
        self._block_round = block_round

    def settings(self, config: Optional[Mapping[str, Any]] = None) -> dict:
        return capability_settings(config)

    def slave_tier(
        self,
        slave_name: str,
        *,
        fetch_one,
        config: Optional[Mapping[str, Any]] = None,
        now_ms: Optional[int] = None,
    ) -> int:
        settings = self.settings(config)
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        cached = self._tier_cache.get(slave_name)
        if cached and now_ms < cached[1]:
            return cached[0]
        row = None
        try:
            row = fetch_one(
                """
                SELECT declared_cores, preflight_status, preflight_report, worker_type
                FROM pool_members
                WHERE slave_name = %s
                """,
                (slave_name,),
            )
        except Exception as exc:
            logger.debug("slave_tier lookup failed for %s: %s", slave_name, exc)
        threads = None
        ram_gb = None
        declared = None
        status = None
        if row:
            declared = row.get("declared_cores")
            status = row.get("preflight_status")
            report = _parse_report(row.get("preflight_report"))
            threads = report.get("threads")
            ram_gb = report.get("ram_gb")
        tier = hardware_tier(
            threads=int(threads) if threads is not None else None,
            declared_cores=int(declared) if declared is not None else None,
            ram_gb=int(ram_gb) if ram_gb is not None else None,
            preflight_status=status,
            ceilings=settings["tier_core_ceilings"],
            default_tier=settings["default_tier"],
            low_spec_penalty=settings["low_spec_tier_penalty"],
        )
        self._tier_cache[slave_name] = (tier, now_ms + max(1000, settings["cache_ms"]))
        return tier

    def refresh_runtime_views(
        self,
        *,
        fetch_all,
        execute=None,
        config: Optional[Mapping[str, Any]] = None,
        now_ms: Optional[int] = None,
        force: bool = False,
    ) -> dict:
        settings = self.settings(config)
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        if not force and self._cache is not None and now_ms < self._cache_until_ms:
            return self._cache
        if execute is not None:
            try:
                ensure_slave_track_ema_table(execute)
            except Exception as exc:
                logger.debug("ensure slave_track_ema failed: %s", exc)

        window_ms = 2 * 60 * 60 * 1000
        since_ms = now_ms - window_ms
        hardness_rows = []
        ema_rows = []
        census_rows = []
        try:
            hardness_rows = (
                fetch_all(
                    """
                    SELECT
                        J.challenge AS challenge,
                        COALESCE(J.settings->>'track_id', '') AS track_id,
                        PERCENTILE_CONT(0.95) WITHIN GROUP (
                            ORDER BY (R.end_time - R.start_time)
                        ) AS p95_ms,
                        COUNT(*) AS n
                    FROM root_batch R
                    JOIN job J ON J.benchmark_id = R.benchmark_id
                    WHERE R.ready = true
                      AND R.end_time IS NOT NULL
                      AND R.start_time IS NOT NULL
                      AND R.end_time >= %s
                    GROUP BY J.challenge, COALESCE(J.settings->>'track_id', '')
                    """,
                    (since_ms,),
                )
                or []
            )
        except Exception as exc:
            logger.warning("capability hardness query failed: %s", exc)
        try:
            ema_rows = (
                fetch_all(
                    """
                    SELECT slave_name, challenge, track_id, ema_runtime_ms, sample_n
                    FROM slave_track_ema
                    WHERE updated_at >= %s
                    """,
                    (since_ms,),
                )
                or []
            )
        except Exception as exc:
            logger.debug("capability ema query failed: %s", exc)
        try:
            census_rows = (
                fetch_all(
                    """
                    SELECT
                        M.slave_name,
                        M.declared_cores,
                        M.preflight_status,
                        M.preflight_report,
                        S.last_seen
                    FROM pool_members M
                    LEFT JOIN slave_seen S ON S.slave_name = M.slave_name
                    WHERE M.active = true
                      AND M.slave_name LIKE 'pool-cpu-%'
                    """
                )
                or []
            )
        except Exception as exc:
            logger.debug("capability census query failed: %s", exc)

        track_hardness: Dict[Tuple[str, str], float] = {}
        for row in hardness_rows:
            challenge = row.get("challenge") or ""
            track_id = row.get("track_id") or ""
            local = hardness_from_p95_ms(
                row.get("p95_ms"),
                soft_p95_ms=settings["soft_p95_ms"],
                hard_p95_ms=settings["hard_p95_ms"],
            )
            challenge_id = _CHALLENGE_NAME_TO_ID.get(challenge)
            heur = heuristic_track_hardness(challenge, track_id)
            prior = tig_track_prior(self._tracks_data, challenge_id, track_id)
            track_hardness[(challenge, track_id)] = blend_hardness(
                local_hardness=local,
                heuristic=heur,
                tig_prior=prior,
                local_weight=settings["local_weight"],
            )

        track_values: Dict[Tuple[str, str], List[float]] = {}
        slave_track_ema: Dict[Tuple[str, str, str], float] = {}
        for row in ema_rows:
            key = (
                str(row.get("slave_name") or ""),
                str(row.get("challenge") or ""),
                str(row.get("track_id") or ""),
            )
            val = float(row.get("ema_runtime_ms") or 0)
            if val <= 0:
                continue
            slave_track_ema[key] = val
            tkey = (key[1], key[2])
            track_values.setdefault(tkey, []).append(val)
        track_median = {k: sorted(v)[len(v) // 2] for k, v in track_values.items() if v}

        online_ms = int(os.environ.get("SLAVE_ONLINE_MS", str(120000)))
        strong_online = 0
        online_cpu = 0
        online_with_core_info = 0
        for row in census_rows:
            last_seen = row.get("last_seen")
            if last_seen is None or int(now_ms) - int(last_seen) > online_ms:
                continue
            online_cpu += 1
            report = _parse_report(row.get("preflight_report"))
            threads = report.get("threads")
            ram_gb = report.get("ram_gb")
            declared = row.get("declared_cores")
            has_core_info = (
                (threads is not None and int(threads) > 0)
                or (declared is not None and int(declared) > 0)
            )
            if has_core_info:
                online_with_core_info += 1
            else:
                # Unknown hardware cannot count toward the strong census.
                continue
            tier = hardware_tier(
                threads=int(threads) if threads is not None else None,
                declared_cores=int(declared) if declared is not None else None,
                ram_gb=int(ram_gb) if ram_gb is not None else None,
                preflight_status=row.get("preflight_status"),
                ceilings=settings["tier_core_ceilings"],
                default_tier=settings["default_tier"],
                low_spec_penalty=settings["low_spec_tier_penalty"],
            )
            if tier >= settings["strong_tier_min"]:
                strong_online += 1

        hard_open_roots = 0
        try:
            open_rows = (
                fetch_all(
                    """
                    SELECT J.challenge, COALESCE(J.settings->>'track_id','') AS track_id,
                           COUNT(*) AS cnt
                    FROM root_batch R
                    JOIN job J ON J.benchmark_id = R.benchmark_id
                    WHERE R.ready IS NULL AND J.stopped IS NULL
                    GROUP BY J.challenge, COALESCE(J.settings->>'track_id','')
                    """
                )
                or []
            )
            cpu_challenges = {
                "satisfiability",
                "vehicle_routing",
                "knapsack",
                "job_scheduling",
                "energy_arbitrage",
                "c001",
                "c002",
                "c003",
                "c007",
                "c008",
            }
            for row in open_rows:
                challenge = row.get("challenge") or ""
                if challenge not in cpu_challenges:
                    continue
                key = (challenge, row.get("track_id") or "")
                h = track_hardness.get(key)
                if h is None:
                    h = heuristic_track_hardness(key[0], key[1])
                if h >= settings["hard_hardness"]:
                    hard_open_roots += int(row.get("cnt") or 0)
        except Exception as exc:
            logger.debug("hard open roots query failed: %s", exc)

        inventory_known = online_with_core_info > 0
        self._cache = {
            "track_hardness": track_hardness,
            "slave_track_ema": slave_track_ema,
            "track_median": track_median,
            "strong_online": strong_online,
            "hard_open_roots": hard_open_roots,
            "online_cpu": online_cpu,
            "online_with_core_info": online_with_core_info,
            "inventory_known": inventory_known,
            "settings": settings,
        }
        self._cache_until_ms = now_ms + max(1000, settings["cache_ms"])
        return self._cache

    def track_hardness(
        self,
        challenge: str,
        track_id: str,
        *,
        views: Optional[dict] = None,
    ) -> float:
        views = views or self._cache or {}
        key = (challenge or "", track_id or "")
        cached = (views.get("track_hardness") or {}).get(key)
        if cached is not None:
            return float(cached)
        heur = heuristic_track_hardness(challenge, track_id)
        prior = tig_track_prior(
            self._tracks_data, _CHALLENGE_NAME_TO_ID.get(challenge or ""), track_id
        )
        return blend_hardness(heuristic=heur, tig_prior=prior)

    def slave_speed_ratio(
        self,
        slave_name: str,
        challenge: str,
        track_id: str,
        *,
        views: Optional[dict] = None,
    ) -> float:
        """Slave EMA / fleet median; 1.0 = average, <1 faster, >1 slower."""
        views = views or self._cache or {}
        ema = (views.get("slave_track_ema") or {}).get(
            (slave_name, challenge or "", track_id or "")
        )
        med = (views.get("track_median") or {}).get((challenge or "", track_id or ""))
        if not ema or not med or med <= 0:
            return 1.0
        return float(ema) / float(med)

    def max_algo_track_hardness(self, selection: Mapping[str, Any]) -> float:
        """Max hardness across non-empty track_settings for a precommit selection."""
        a_id = str(selection.get("algorithm_id") or "")
        c_id = a_id[:4]
        id_to_name = {v: k for k, v in _CHALLENGE_NAME_TO_ID.items()}
        challenge = id_to_name.get(c_id, c_id)
        best = 0.0
        for track_id, settings in (selection.get("track_settings") or {}).items():
            if not settings:
                continue
            best = max(best, self.track_hardness(challenge, str(track_id)))
        if best <= 0:
            best = heuristic_track_hardness(challenge, None)
        return best


# Module-level singleton used by managers.
SCHEDULER = CapabilityScheduler()
