"""
Effort-weighted work credits (shadow only until cutover).

Live /set-coinbase still uses raw root nonces. This module scores
completed roots as:

    credits = nonces × seconds_per_nonce(challenge, track) × conversion

seconds_per_nonce is the fleet median from slave_track_ema, falling
back to a per-challenge prior. A single batch cannot exceed CAP_MULT
times that weight (hung box cannot mint hours).

conversion is 1.0 for normal work and CONVERSION_FLOOR when the job
stopped without a proof submit (junk / never-converted).
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

GPU_CHALLENGES = frozenset(
    {"vector_search", "hypergraph", "neuralnet_optimizer"}
)

# Seconds per nonce when no fleet EMA exists yet. Relative, not absolute:
# SAT/VRPTW cost more than knapsack/energy. GPU tracks are separate.
PRIOR_SEC_PER_NONCE: dict[str, float] = {
    "satisfiability": 2.0,
    "vehicle_routing": 3.0,
    "knapsack": 0.6,
    "job_scheduling": 1.2,
    "energy_arbitrage": 0.5,
    "vector_search": 0.4,
    "hypergraph": 2.0,
    "neuralnet_optimizer": 0.8,
}

DEFAULT_SEC_PER_NONCE = 1.0
CAP_MULT = 3.0
CONVERSION_FLOOR = 0.5
MIN_EMA_SAMPLES = 8


def is_gpu_challenge(challenge: str) -> bool:
    return str(challenge or "") in GPU_CHALLENGES


def prior_seconds_per_nonce(challenge: str, track_id: str = "") -> float:
    del track_id  # reserved for track-specific priors
    return float(PRIOR_SEC_PER_NONCE.get(str(challenge or ""), DEFAULT_SEC_PER_NONCE))


def median(values: Iterable[float]) -> Optional[float]:
    nums = sorted(float(v) for v in values if v is not None)
    if not nums:
        return None
    mid = len(nums) // 2
    if len(nums) % 2:
        return nums[mid]
    return (nums[mid - 1] + nums[mid]) / 2.0


def blend_seconds_per_nonce(
    ema_sec: Optional[float],
    prior_sec: float,
    sample_n: int,
    *,
    min_samples: int = MIN_EMA_SAMPLES,
) -> float:
    """Prefer fleet EMA once enough samples exist; otherwise mix with the prior."""
    prior = max(1e-6, float(prior_sec))
    if ema_sec is None or ema_sec <= 0:
        return prior
    ema = float(ema_sec)
    n = max(0, int(sample_n or 0))
    if n >= min_samples:
        return ema
    if n <= 0:
        return prior
    mix = n / float(min_samples)
    return mix * ema + (1.0 - mix) * prior


def credits_for_nonces(
    nonces: float,
    sec_per_nonce: float,
    *,
    cap_mult: float = CAP_MULT,
) -> float:
    """Effort credits for one completed root pile. Cap the per-nonce weight."""
    n = max(0.0, float(nonces or 0))
    if n <= 0:
        return 0.0
    weight = max(0.0, float(sec_per_nonce or 0))
    cap = max(weight, weight * float(cap_mult or CAP_MULT)) if weight > 0 else 0.0
    if cap > 0:
        weight = min(weight, cap)
    return round(n * weight, 6)


def conversion_factor(
    *,
    stopped: bool = False,
    proof_submitted: bool = False,
    floor: float = CONVERSION_FLOOR,
) -> float:
    """1.0 unless the job died without a proof — then the floor, not zero."""
    if stopped and not proof_submitted:
        return max(0.0, min(1.0, float(floor)))
    return 1.0


def fractions_from_amounts(
    amounts: Mapping[str, float],
    member_share: float,
) -> dict[str, float]:
    """Same split math as coinbase._compute_allocation, any positive unit."""
    share = max(0.0, float(member_share))
    cleaned = {
        str(k): float(v)
        for k, v in (amounts or {}).items()
        if k and float(v or 0) > 0
    }
    total = sum(cleaned.values())
    if total <= 0 or share <= 0:
        return {}
    out = {k: round((v / total) * share, 6) for k, v in cleaned.items()}
    summed = sum(out.values())
    if summed > share:
        out = {k: round(v / summed * share, 6) for k, v in out.items()}
    return out


def load_weight_table(ema_rows: Iterable[Mapping[str, Any]] | None) -> dict[tuple[str, str], dict]:
    """Build (challenge, track) -> {sec_per_nonce, source, samples} from EMA rows."""
    grouped: dict[tuple[str, str], list[tuple[float, int]]] = {}
    for row in ema_rows or []:
        challenge = str(row.get("challenge") or "")
        track = str(row.get("track_id") or "")
        mpn = row.get("ema_ms_per_nonce")
        if mpn in (None, ""):
            continue
        try:
            sec = float(mpn) / 1000.0
        except (TypeError, ValueError):
            continue
        if sec <= 0:
            continue
        try:
            n = int(row.get("sample_n") or 0)
        except (TypeError, ValueError):
            n = 0
        grouped.setdefault((challenge, track), []).append((sec, n))

    table: dict[tuple[str, str], dict] = {}
    seen_challenges = {key[0] for key in grouped}
    for challenge in set(seen_challenges) | set(PRIOR_SEC_PER_NONCE):
        # Always expose a challenge-level fallback key ("",).
        prior = prior_seconds_per_nonce(challenge)
        table[(challenge, "")] = {
            "sec_per_nonce": prior,
            "source": "prior",
            "samples": 0,
        }

    for key, pairs in grouped.items():
        challenge, track = key
        prior = prior_seconds_per_nonce(challenge, track)
        secs = [p[0] for p in pairs]
        samples = sum(p[1] for p in pairs)
        ema = median(secs)
        sec = blend_seconds_per_nonce(ema, prior, samples)
        source = "ema" if samples >= MIN_EMA_SAMPLES and ema is not None else "blend"
        if ema is None:
            source = "prior"
        table[key] = {
            "sec_per_nonce": sec,
            "source": source,
            "samples": samples,
        }
    return table


def seconds_for(weights: Mapping[tuple[str, str], Mapping], challenge: str, track_id: str = "") -> float:
    chal = str(challenge or "")
    track = str(track_id or "")
    row = weights.get((chal, track)) or weights.get((chal, ""))
    if row:
        try:
            return float(row["sec_per_nonce"])
        except (KeyError, TypeError, ValueError):
            pass
    return prior_seconds_per_nonce(chal, track)


def ensure_contribution_credit_columns() -> None:
    """Add credit columns on live DBs that predate init.sql."""
    from . import database as db

    db.execute(
        """
        ALTER TABLE pool_contributions
            ADD COLUMN IF NOT EXISTS work_credits DOUBLE PRECISION NOT NULL DEFAULT 0
        """
    )
    db.execute(
        """
        ALTER TABLE pool_contributions
            ADD COLUMN IF NOT EXISTS credit_share_fraction FLOAT NOT NULL DEFAULT 0
        """
    )


def fetch_weight_table():
    """Load (challenge, track) weights from slave_track_ema, else priors."""
    from . import database as db

    try:
        rows = db.fetch_all(
            """
            SELECT challenge, track_id, ema_ms_per_nonce, ema_runtime_ms, sample_n
            FROM slave_track_ema
            """
        )
    except Exception:
        rows = []
    return load_weight_table(rows)


def build_shadow_report(*, pool_fee: float, round_start_ms: int | None, top_n: int = 20) -> dict:
    """Compare nonce split vs effort-credit split. Does not call /set-coinbase."""
    from . import database as db

    ensure_contribution_credit_columns()
    member_share = max(0.0, 1.0 - float(pool_fee or 0))
    weights = fetch_weight_table()

    if round_start_ms is not None:
        nonce_rows = db.fetch_all(
            """
            SELECT wallet_address, SUM(nonces_computed) AS total_nonces
            FROM pool_contributions
            WHERE snapshot_end_ms >= %s
            GROUP BY wallet_address
            """,
            (int(round_start_ms),),
        )
        work_rows = db.fetch_all(
            """
            SELECT
                rb.slave,
                j.challenge,
                COALESCE(j.settings->>'track_id', '') AS track_id,
                j.stopped,
                j.proof_submitted,
                SUM(LEAST(j.batch_size, j.num_nonces - rb.batch_idx * j.batch_size)) AS nonces,
                COUNT(*) AS batches
            FROM root_batch rb
            JOIN job j ON rb.benchmark_id = j.benchmark_id
            WHERE rb.ready = true
              AND rb.end_time >= %s
              AND rb.slave IS NOT NULL
            GROUP BY rb.slave, j.challenge, COALESCE(j.settings->>'track_id', ''),
                     j.benchmark_id, j.stopped, j.proof_submitted
            """,
            (int(round_start_ms),),
        )
    else:
        nonce_rows = db.fetch_all(
            """
            SELECT wallet_address, SUM(nonces_computed) AS total_nonces
            FROM pool_contributions
            GROUP BY wallet_address
            """
        )
        work_rows = db.fetch_all(
            """
            SELECT
                rb.slave,
                j.challenge,
                COALESCE(j.settings->>'track_id', '') AS track_id,
                j.stopped,
                j.proof_submitted,
                SUM(LEAST(j.batch_size, j.num_nonces - rb.batch_idx * j.batch_size)) AS nonces,
                COUNT(*) AS batches
            FROM root_batch rb
            JOIN job j ON rb.benchmark_id = j.benchmark_id
            WHERE rb.ready = true
              AND rb.slave IS NOT NULL
            GROUP BY rb.slave, j.challenge, COALESCE(j.settings->>'track_id', ''),
                     j.benchmark_id, j.stopped, j.proof_submitted
            """
        )

    members = db.fetch_all(
        "SELECT slave_name, wallet_address FROM pool_members WHERE active = true"
    ) or []
    slave_map = {r["slave_name"]: r["wallet_address"] for r in members}

    nonce_amounts = {
        str(r["wallet_address"]): float(r["total_nonces"] or 0)
        for r in (nonce_rows or [])
        if r.get("wallet_address")
    }
    credit_amounts: dict[str, float] = {}
    challenge_nonce: dict[str, float] = {}
    challenge_credit: dict[str, float] = {}
    converted_jobs = 0
    scored_jobs = 0
    for row in work_rows or []:
        wallet = slave_map.get(row.get("slave"))
        if not wallet:
            continue
        scored = score_root_group(
            nonces=float(row.get("nonces") or 0),
            challenge=str(row.get("challenge") or ""),
            track_id=str(row.get("track_id") or ""),
            weights=weights,
            stopped=bool(row.get("stopped")),
            proof_submitted=bool(row.get("proof_submitted")),
        )
        scored_jobs += 1
        if scored["conversion"] < 1.0:
            converted_jobs += 1
        credit_amounts[wallet] = credit_amounts.get(wallet, 0.0) + scored["credits_converted"]
        chal = str(row.get("challenge") or "")
        challenge_nonce[chal] = challenge_nonce.get(chal, 0.0) + scored["nonces"]
        challenge_credit[chal] = challenge_credit.get(chal, 0.0) + scored["credits_converted"]

    nonce_frac = fractions_from_amounts(nonce_amounts, member_share)
    credit_frac = fractions_from_amounts(credit_amounts, member_share)
    wallets = set(nonce_frac) | set(credit_frac)
    compare = []
    for wallet in wallets:
        n = float(nonce_frac.get(wallet, 0.0))
        c = float(credit_frac.get(wallet, 0.0))
        compare.append(
            {
                "wallet_address": wallet,
                "nonce_share": n,
                "credit_share": c,
                "delta": round(c - n, 6),
                "nonces": int(nonce_amounts.get(wallet, 0)),
                "credits": round(credit_amounts.get(wallet, 0.0), 3),
            }
        )
    compare.sort(key=lambda r: (-abs(float(r["delta"])), -float(r["credits"])))

    total_n = sum(challenge_nonce.values()) or 1.0
    total_c = sum(challenge_credit.values()) or 1.0
    challenges = []
    for chal in sorted(set(challenge_nonce) | set(challenge_credit)):
        challenges.append(
            {
                "challenge": chal,
                "profile": "gpu" if is_gpu_challenge(chal) else "cpu",
                "nonce_pct": round(100.0 * challenge_nonce.get(chal, 0.0) / total_n, 2),
                "credit_pct": round(100.0 * challenge_credit.get(chal, 0.0) / total_c, 2),
                "sec_per_nonce": seconds_for(weights, chal, ""),
            }
        )

    weight_rows = [
        {
            "challenge": chal,
            "track_id": track,
            "sec_per_nonce": round(float(meta["sec_per_nonce"]), 4),
            "source": meta.get("source"),
            "samples": int(meta.get("samples") or 0),
        }
        for (chal, track), meta in sorted(weights.items())
        if track == "" or int(meta.get("samples") or 0) > 0
    ]

    return {
        "mode": "shadow",
        "live_payout": "nonces",
        "note": (
            "Live /set-coinbase is still raw root nonces. "
            "credit_share is the effort-weighted would-be split."
        ),
        "member_share": member_share,
        "round_start_ms": round_start_ms,
        "scored_jobs": scored_jobs,
        "conversion_discounted_jobs": converted_jobs,
        "wallets": len(compare),
        "challenges": challenges,
        "weights": weight_rows,
        "top_delta": compare[: max(1, int(top_n))],
    }


def score_root_group(
    *,
    nonces: float,
    challenge: str,
    track_id: str = "",
    weights: Mapping[tuple[str, str], Mapping] | None = None,
    stopped: bool = False,
    proof_submitted: bool = False,
    conversion_floor: float = CONVERSION_FLOOR,
) -> dict[str, float]:
    """Score one (slave, job) or (slave, track) pile of completed root nonces."""
    sec = seconds_for(weights or {}, challenge, track_id)
    raw = credits_for_nonces(nonces, sec)
    conv = conversion_factor(
        stopped=bool(stopped),
        proof_submitted=bool(proof_submitted),
        floor=conversion_floor,
    )
    return {
        "nonces": float(nonces or 0),
        "sec_per_nonce": sec,
        "credits": raw,
        "conversion": conv,
        "credits_converted": round(raw * conv, 6),
    }
