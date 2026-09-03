"""
Effort-weighted work credits.

Live /set-coinbase (PAY_MODE=effort, the default) splits the member pot by
completed-root effort, not a fixed GPU/CPU family pot:

    credits = nonces × seconds_per_nonce(challenge, track) × conversion

seconds_per_nonce is the fleet median from slave_track_ema, falling
back to a per-challenge prior. A single batch cannot exceed CAP_MULT
times that weight (hung box cannot mint hours).

conversion is 1.0 for normal work and CONVERSION_FLOOR when the job
stopped without a proof submit (junk / never-converted).

PAY_MODE=family restores the old 27/73 challenge pots.
"""
from __future__ import annotations

import os
import time
from typing import Any, Iterable, Mapping, Optional

_SHADOW_TTL_S = 60.0
_shadow_cache: dict[str, Any] = {"ts": 0.0, "data": None}

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


def pay_mode() -> str:
    """Live coinbase mode. effort = work credits; family = GPU/CPU pots."""
    raw = (os.environ.get("PAY_MODE") or "effort").strip().lower()
    if raw in {"family", "pots", "gpu_frac"}:
        return "family"
    return "effort"


def challenge_effort_pots(
    pool_tig: float,
    pool_nonces: Mapping[str, float],
    *,
    weights: Mapping[tuple[str, str], Mapping] | None = None,
) -> dict[str, float]:
    """Split pool TIG by challenge effort (nonces × sec/nonce), not 27/73."""
    table = weights if weights is not None else {}
    credits: dict[str, float] = {}
    for challenge, raw in (pool_nonces or {}).items():
        n = float(raw or 0)
        if n <= 0 or not challenge:
            continue
        sec = seconds_for(table, str(challenge))
        cr = credits_for_nonces(n, sec)
        if cr > 0:
            credits[str(challenge)] = cr
    return fractions_from_amounts(credits, max(0.0, float(pool_tig or 0)))


def wallet_shares(
    since_ms: int | None,
    *,
    scale: float = 1.0,
    force: bool = False,
) -> dict[str, float]:
    """Member-pot fractions from effort credits for the current round."""
    report = build_shadow_report(
        pool_fee=max(0.0, 1.0 - float(scale or 0)),
        round_start_ms=since_ms,
        force=force,
    )
    out: dict[str, float] = {}
    for row in report.get("wallets_combined") or []:
        wallet = str(row.get("wallet_address") or "").strip()
        share = float(row.get("weight_share") or 0)
        if wallet and share > 0:
            out[wallet] = share
    return out


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
    cap_ref_sec: Optional[float] = None,
) -> float:
    """Effort credits for one completed root pile.

    ``cap_ref_sec`` defaults to ``sec_per_nonce``. Wall-clock scoring should
    pass the table weight as the ref so a hung box cannot exceed CAP_MULT
    times the fleet/prior rate.
    """
    n = max(0.0, float(nonces or 0))
    if n <= 0:
        return 0.0
    weight = max(0.0, float(sec_per_nonce or 0))
    try:
        ref = float(cap_ref_sec) if cap_ref_sec not in (None, "") else weight
    except (TypeError, ValueError):
        ref = weight
    if ref > 0 and cap_mult:
        weight = min(weight, ref * float(cap_mult))
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


def weight_meta(
    weights: Mapping[tuple[str, str], Mapping],
    challenge: str,
    track_id: str = "",
) -> dict:
    chal = str(challenge or "")
    track = str(track_id or "")
    row = (weights or {}).get((chal, track)) or (weights or {}).get((chal, "")) or {}
    try:
        sec = float(row.get("sec_per_nonce"))
    except (TypeError, ValueError):
        sec = 0.0
    if sec <= 0:
        sec = prior_seconds_per_nonce(chal, track)
        return {"sec_per_nonce": sec, "source": "prior", "samples": 0}
    return {
        "sec_per_nonce": sec,
        "source": str(row.get("source") or "prior"),
        "samples": int(row.get("samples") or 0),
    }


def seconds_for(weights: Mapping[tuple[str, str], Mapping], challenge: str, track_id: str = "") -> float:
    return float(weight_meta(weights, challenge, track_id)["sec_per_nonce"])


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


def _work_sql(since_ms: int | None) -> tuple[str, tuple]:
    """One row per slave × track × junk-bucket, with wall-clock ms."""
    where = """
        rb.ready = true
        AND rb.slave IS NOT NULL
    """
    params: tuple = ()
    if since_ms is not None:
        where += " AND rb.end_time >= %s"
        params = (int(since_ms),)
    sql = f"""
        SELECT
            rb.slave,
            j.challenge,
            COALESCE(j.settings->>'track_id', '') AS track_id,
            (j.stopped IS TRUE AND j.proof_submitted IS NOT TRUE) AS junk,
            SUM(LEAST(j.batch_size, j.num_nonces - rb.batch_idx * j.batch_size)) AS nonces,
            COUNT(*) AS batches,
            COUNT(DISTINCT j.benchmark_id) AS jobs,
            SUM(
                CASE
                    WHEN rb.start_time IS NOT NULL
                     AND rb.end_time IS NOT NULL
                     AND rb.end_time > rb.start_time
                    THEN rb.end_time - rb.start_time
                    ELSE 0
                END
            ) AS runtime_ms
        FROM root_batch rb
        JOIN job j ON rb.benchmark_id = j.benchmark_id
        WHERE {where}
        GROUP BY rb.slave, j.challenge, COALESCE(j.settings->>'track_id', ''),
                 (j.stopped IS TRUE AND j.proof_submitted IS NOT TRUE)
    """
    return sql, params


def _pct(part: float, whole: float) -> float:
    if whole <= 0:
        return 0.0
    return round(100.0 * float(part) / float(whole), 2)


def _share_table(
    amounts: Mapping[str, float],
    nonce_frac: Mapping[str, float],
    member_share: float,
    *,
    extra: Mapping[str, Mapping[str, float]] | None = None,
) -> list[dict]:
    frac = fractions_from_amounts(amounts, member_share)
    keys = set(nonce_frac) | set(frac)
    rows = []
    for key in keys:
        n = float(nonce_frac.get(key, 0.0))
        c = float(frac.get(key, 0.0))
        row = {
            "id": key,
            "nonce_share": n,
            "credit_share": c,
            "delta": round(c - n, 6),
            "amount": round(float(amounts.get(key, 0.0)), 3),
        }
        if extra and key in extra:
            row.update(extra[key])
        rows.append(row)
    rows.sort(key=lambda r: (-abs(float(r["delta"])), -float(r["amount"])))
    return rows


def _verdict(payload: dict) -> dict:
    """Machine-readable judgement so a pasted report is enough."""
    wallets = payload.get("wallets_combined") or []
    challenges = payload.get("challenges") or []
    biggest = wallets[0] if wallets else {}
    gpu_nonce = sum(float(r.get("nonce_pct") or 0) for r in challenges if r.get("profile") == "gpu")
    gpu_wt = sum(float(r.get("wt_pct") or 0) for r in challenges if r.get("profile") == "gpu")
    gpu_act = sum(float(r.get("act_pct") or 0) for r in challenges if r.get("profile") == "gpu")
    wt_hours = float(payload.get("weight_hours") or 0)
    act_hours = float(payload.get("actual_hours") or 0)
    ratio = (wt_hours / act_hours) if act_hours > 0 else None
    notes = []
    max_swing = abs(float(biggest.get("delta_wt") or biggest.get("delta") or 0))
    if ratio is not None and ratio > 1.5:
        notes.append(
            f"Table weights pay {ratio:.2f}x actual wall hours — EMA/prior is hot "
            "(GPU likely overpaid vs real card time)."
        )
    elif ratio is not None and ratio < 0.7:
        notes.append(
            f"Table weights pay {ratio:.2f}x actual wall hours — weights are cold "
            "(slow SAT/VRPTW still underpaid)."
        )
    else:
        notes.append(
            "Weight hours and actual hours agree closely — the table is calibrated "
            "enough to judge mix luck."
        )
    if gpu_wt - gpu_nonce > 15:
        notes.append(
            f"GPU is {gpu_nonce:.1f}% of nonces but {gpu_wt:.1f}% of weight-credits "
            f"({gpu_act:.1f}% of actual-credits). Combined-pot effort-pay moves TIG "
            "from CPU nonce farms to GPU hours."
        )
    if max_swing >= 0.15:
        notes.append(
            f"Largest wallet swing is {max_swing:.1%} — too big to cut over without "
            "agreeing CPU and GPU should share one pot."
        )
    sat = next((r for r in challenges if r.get("challenge") == "satisfiability"), None)
    knap = next((r for r in challenges if r.get("challenge") == "knapsack"), None)
    if sat and knap and float(sat.get("act_sec") or 0) > float(knap.get("act_sec") or 0) * 2:
        notes.append(
            "Actual s/nonce: SAT is much slower than knapsack. Raw nonces underpay "
            "SAT/VRPTW mix luck. Effort-pay is fairer on CPU if WT≈ACT."
        )
    agree = True
    for row in wallets:
        if abs(float(row.get("delta_wt") or 0) - float(row.get("delta_act") or 0)) > 0.05:
            agree = False
            break
    if wallets:
        notes.append(
            "WT and ACT wallet shares "
            + ("agree — method is stable." if agree else "disagree — do not cut over; fix weights first.")
        )
    return {
        "largest_wallet": biggest.get("wallet_address"),
        "largest_swing": round(max_swing, 4),
        "gpu_nonce_pct": round(gpu_nonce, 2),
        "gpu_weight_credit_pct": round(gpu_wt, 2),
        "gpu_actual_credit_pct": round(gpu_act, 2),
        "weight_over_actual": round(ratio, 3) if ratio is not None else None,
        "notes": notes,
    }


def build_shadow_report(
    *,
    pool_fee: float,
    round_start_ms: int | None,
    top_n: int = 20,
    force: bool = False,
) -> dict:
    """Compare nonce vs weight/prior/actual credits. Does not call /set-coinbase."""
    now = time.time()
    cached = _shadow_cache.get("data")
    if (
        not force
        and cached is not None
        and now - float(_shadow_cache.get("ts") or 0) < _SHADOW_TTL_S
        and cached.get("round_start_ms") == round_start_ms
    ):
        return cached

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
    else:
        nonce_rows = db.fetch_all(
            """
            SELECT wallet_address, SUM(nonces_computed) AS total_nonces
            FROM pool_contributions
            GROUP BY wallet_address
            """
        )
    work_sql, work_params = _work_sql(round_start_ms)
    work_rows = db.fetch_all(work_sql, work_params)

    members = db.fetch_all(
        "SELECT slave_name, wallet_address FROM pool_members WHERE active = true"
    ) or []
    slave_map = {r["slave_name"]: r["wallet_address"] for r in members}

    nonce_amounts = {
        str(r["wallet_address"]): float(r["total_nonces"] or 0)
        for r in (nonce_rows or [])
        if r.get("wallet_address")
    }

    wallet_wt: dict[str, float] = {}
    wallet_pri: dict[str, float] = {}
    wallet_act: dict[str, float] = {}
    slave_nonce: dict[str, float] = {}
    slave_wt: dict[str, float] = {}
    slave_act: dict[str, float] = {}
    slave_wallet: dict[str, str] = {}
    cpu_nonce: dict[str, float] = {}
    cpu_wt: dict[str, float] = {}
    gpu_nonce: dict[str, float] = {}
    gpu_wt: dict[str, float] = {}
    chal: dict[str, dict] = {}
    track: dict[tuple[str, str], dict] = {}

    scored_groups = 0
    discounted_jobs = 0
    total_jobs = 0
    total_batches = 0
    total_runtime_s = 0.0

    for row in work_rows or []:
        slave = str(row.get("slave") or "")
        wallet = slave_map.get(slave)
        if not wallet:
            continue
        challenge = str(row.get("challenge") or "")
        track_id = str(row.get("track_id") or "")
        nonces = float(row.get("nonces") or 0)
        runtime_ms = float(row.get("runtime_ms") or 0)
        jobs = int(row.get("jobs") or 0)
        batches = int(row.get("batches") or 0)
        junk = bool(row.get("junk"))
        scored = score_root_group(
            nonces=nonces,
            challenge=challenge,
            track_id=track_id,
            weights=weights,
            runtime_ms=runtime_ms,
            stopped=junk,
            proof_submitted=not junk,
        )
        scored_groups += 1
        total_jobs += jobs
        total_batches += batches
        total_runtime_s += runtime_ms / 1000.0
        if scored["conversion"] < 1.0:
            discounted_jobs += jobs

        wt = scored["credits_weight"]
        pri = scored["credits_prior"]
        act = scored["credits_actual"]
        wallet_wt[wallet] = wallet_wt.get(wallet, 0.0) + wt
        wallet_pri[wallet] = wallet_pri.get(wallet, 0.0) + pri
        wallet_act[wallet] = wallet_act.get(wallet, 0.0) + act
        slave_nonce[slave] = slave_nonce.get(slave, 0.0) + nonces
        slave_wt[slave] = slave_wt.get(slave, 0.0) + wt
        slave_act[slave] = slave_act.get(slave, 0.0) + act
        slave_wallet[slave] = wallet
        pot_n, pot_w = (gpu_nonce, gpu_wt) if is_gpu_challenge(challenge) else (cpu_nonce, cpu_wt)
        pot_n[wallet] = pot_n.get(wallet, 0.0) + nonces
        pot_w[wallet] = pot_w.get(wallet, 0.0) + wt

        c = chal.setdefault(
            challenge,
            {
                "nonces": 0.0,
                "runtime_s": 0.0,
                "wt": 0.0,
                "pri": 0.0,
                "act": 0.0,
                "source": scored["source"],
                "samples": scored["samples"],
                "prior_sec": scored["prior_sec"],
            },
        )
        c["nonces"] += nonces
        c["runtime_s"] += runtime_ms / 1000.0
        c["wt"] += wt
        c["pri"] += pri
        c["act"] += act

        t = track.setdefault(
            (challenge, track_id),
            {
                "nonces": 0.0,
                "runtime_s": 0.0,
                "wt": 0.0,
                "act": 0.0,
                "weight_sec": scored["weight_sec"],
                "prior_sec": scored["prior_sec"],
                "source": scored["source"],
                "samples": scored["samples"],
            },
        )
        t["nonces"] += nonces
        t["runtime_s"] += runtime_ms / 1000.0
        t["wt"] += wt
        t["act"] += act

    nonce_frac = fractions_from_amounts(nonce_amounts, member_share)
    wt_frac = fractions_from_amounts(wallet_wt, member_share)
    pri_frac = fractions_from_amounts(wallet_pri, member_share)
    act_frac = fractions_from_amounts(wallet_act, member_share)

    wallets_combined = []
    for wallet in set(nonce_frac) | set(wt_frac) | set(act_frac) | set(pri_frac):
        n = float(nonce_frac.get(wallet, 0.0))
        w = float(wt_frac.get(wallet, 0.0))
        a = float(act_frac.get(wallet, 0.0))
        p = float(pri_frac.get(wallet, 0.0))
        wallets_combined.append(
            {
                "wallet_address": wallet,
                "nonce_share": n,
                "weight_share": w,
                "actual_share": a,
                "prior_share": p,
                "delta_wt": round(w - n, 6),
                "delta_act": round(a - n, 6),
                "nonces": int(nonce_amounts.get(wallet, 0)),
                "weight_credits": round(wallet_wt.get(wallet, 0.0), 1),
                "actual_credits": round(wallet_act.get(wallet, 0.0), 1),
            }
        )
    wallets_combined.sort(key=lambda r: (-abs(float(r["delta_wt"])), -float(r["weight_credits"])))

    slave_nonce_frac = fractions_from_amounts(slave_nonce, member_share)
    slave_wt_frac = fractions_from_amounts(slave_wt, member_share)
    slave_act_frac = fractions_from_amounts(slave_act, member_share)
    slaves = []
    for name in slave_nonce:
        n = float(slave_nonce_frac.get(name, 0.0))
        w = float(slave_wt_frac.get(name, 0.0))
        a = float(slave_act_frac.get(name, 0.0))
        slaves.append(
            {
                "slave_name": name,
                "wallet_address": slave_wallet.get(name, ""),
                "profile": "gpu" if str(name).startswith("pool-gpu-") else "cpu",
                "nonces": int(slave_nonce.get(name, 0)),
                "nonce_share": n,
                "weight_share": w,
                "actual_share": a,
                "delta_wt": round(w - n, 6),
                "delta_act": round(a - n, 6),
            }
        )
    slaves.sort(key=lambda r: (-abs(float(r["delta_wt"])), -int(r["nonces"])))

    total_n = sum(v["nonces"] for v in chal.values()) or 1.0
    total_wt = sum(v["wt"] for v in chal.values()) or 1.0
    total_act = sum(v["act"] for v in chal.values()) or 1.0
    total_pri = sum(v["pri"] for v in chal.values()) or 1.0
    challenges = []
    for name, v in sorted(chal.items()):
        act_sec = (v["runtime_s"] / v["nonces"]) if v["nonces"] else 0.0
        wt_sec = (v["wt"] / v["nonces"]) if v["nonces"] else 0.0
        challenges.append(
            {
                "challenge": name,
                "profile": "gpu" if is_gpu_challenge(name) else "cpu",
                "nonce_pct": _pct(v["nonces"], total_n),
                "wt_pct": _pct(v["wt"], total_wt),
                "act_pct": _pct(v["act"], total_act),
                "pri_pct": _pct(v["pri"], total_pri),
                "prior_sec": round(v["prior_sec"], 4),
                "effective_sec": round(wt_sec, 4),
                "act_sec": round(act_sec, 4),
                "source": v["source"],
                "samples": v["samples"],
            }
        )

    tracks = []
    for (name, tid), v in sorted(track.items(), key=lambda kv: -kv[1]["wt"]):
        if v["nonces"] <= 0:
            continue
        act_sec = (v["runtime_s"] / v["nonces"]) if v["nonces"] else 0.0
        tracks.append(
            {
                "challenge": name,
                "track_id": tid,
                "profile": "gpu" if is_gpu_challenge(name) else "cpu",
                "nonce_pct": _pct(v["nonces"], total_n),
                "wt_pct": _pct(v["wt"], total_wt),
                "act_pct": _pct(v["act"], total_act),
                "prior_sec": round(v["prior_sec"], 4),
                "weight_sec": round(v["weight_sec"], 4),
                "act_sec": round(act_sec, 4),
                "source": v["source"],
                "samples": v["samples"],
            }
        )

    cpu_nonce_frac = fractions_from_amounts(cpu_nonce, member_share)
    gpu_nonce_frac = fractions_from_amounts(gpu_nonce, member_share)
    limit = max(1, int(top_n))
    live = pay_mode()
    payload = {
        "mode": live,
        "live_payout": "effort" if live == "effort" else "challenge_share",
        "note": (
            "Live /set-coinbase splits the member pot by effort credits "
            "(nonces × fleet seconds/nonce) when PAY_MODE=effort. "
            "PAY_MODE=family restores GPU 27% / CPU 73% challenge pots. "
            "WT=table weight (EMA/prior). ACT=capped wall-clock. "
            "PRI=hardcoded prior only. If WT≈ACT, weights are calibrated."
        ),
        "member_share": member_share,
        "round_start_ms": round_start_ms,
        "scored_groups": scored_groups,
        "jobs": total_jobs,
        "batches": total_batches,
        "discounted_jobs": discounted_jobs,
        "actual_hours": round(total_runtime_s / 3600.0, 2),
        "weight_hours": round(sum(wallet_wt.values()) / 3600.0, 2),
        "prior_hours": round(sum(wallet_pri.values()) / 3600.0, 2),
        "wallets": len(wallets_combined),
        "challenges": challenges,
        "tracks": tracks[:40],
        "wallets_combined": wallets_combined,
        "wallets_cpu": _share_table(cpu_wt, cpu_nonce_frac, member_share)[:limit],
        "wallets_gpu": _share_table(gpu_wt, gpu_nonce_frac, member_share)[:limit],
        "top_slaves": slaves[:limit],
        "top_delta": wallets_combined[:limit],
    }
    payload["verdict"] = _verdict(payload)
    _shadow_cache["data"] = payload
    _shadow_cache["ts"] = now
    return payload


def score_root_group(
    *,
    nonces: float,
    challenge: str,
    track_id: str = "",
    weights: Mapping[tuple[str, str], Mapping] | None = None,
    runtime_ms: float = 0.0,
    stopped: bool = False,
    proof_submitted: bool = False,
    conversion_floor: float = CONVERSION_FLOOR,
) -> dict[str, float]:
    """Score one slave×track pile. Returns weight, prior, and capped-actual credits."""
    n = float(nonces or 0)
    meta = weight_meta(weights or {}, challenge, track_id)
    weight_sec = float(meta["sec_per_nonce"])
    prior_sec = prior_seconds_per_nonce(challenge, track_id)
    actual_sec = 0.0
    if n > 0 and runtime_ms and float(runtime_ms) > 0:
        actual_sec = float(runtime_ms) / 1000.0 / n
    conv = conversion_factor(
        stopped=bool(stopped),
        proof_submitted=bool(proof_submitted),
        floor=conversion_floor,
    )
    wt = credits_for_nonces(n, weight_sec) * conv
    pri = credits_for_nonces(n, prior_sec) * conv
    act = credits_for_nonces(n, actual_sec, cap_ref_sec=weight_sec or prior_sec) * conv
    raw = credits_for_nonces(n, weight_sec)
    return {
        "nonces": n,
        "sec_per_nonce": weight_sec,
        "weight_sec": weight_sec,
        "prior_sec": prior_sec,
        "actual_sec": actual_sec,
        "source": meta["source"],
        "samples": meta["samples"],
        "credits": raw,
        "conversion": conv,
        "credits_converted": round(wt, 6),
        "credits_weight": round(wt, 6),
        "credits_prior": round(pri, 6),
        "credits_actual": round(act, 6),
    }
