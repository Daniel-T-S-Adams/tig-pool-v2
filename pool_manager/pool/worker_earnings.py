"""
Per-slave TIG estimates. Same per-challenge pots as /set-coinbase:

    GPU challenges share PAY_GPU_POT_FRAC of pool TIG (default 0.27)
    CPU challenges share the rest; equal split within each family
    24h = sum_c (machine_nonces_24h_c / pool_nonces_round_c) × pot_c
    1h  = sum_c (machine_nonces_1h_c  / pool_nonces_round_c) × pot_c
"""
from __future__ import annotations

import logging
import time

from . import challenge_share
from . import database as db

logger = logging.getLogger("pool.worker_earnings")

CACHE_TTL_S = 60
_cache: dict = {"data": None, "ts": 0.0}


HOUR_MS = 60 * 60 * 1000
TWELVE_MS = 12 * HOUR_MS
DAY_MS = 24 * HOUR_MS


def allocate_tig(nonces: int, total_nonces: int, pool_tig: float) -> float:
    """Proportional TIG for one slave. Zero if any input is non-positive."""
    if int(nonces or 0) <= 0 or int(total_nonces or 0) <= 0 or float(pool_tig or 0) <= 0:
        return 0.0
    return round(float(pool_tig) * (int(nonces) / int(total_nonces)), 6)


def prorate_pot(round_tig: float, window_ms: int, round_elapsed_ms: int) -> float:
    """Slice of the current-round pot that accrued during a time window."""
    if float(round_tig or 0) <= 0 or int(window_ms or 0) <= 0 or int(round_elapsed_ms or 0) <= 0:
        return 0.0
    frac = min(1.0, int(window_ms) / int(round_elapsed_ms))
    return round(float(round_tig) * frac, 6)


def _to_ms(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        raw = int(value)
        # Seconds vs milliseconds.
        return raw * 1000 if raw < 10_000_000_000 else raw
    if hasattr(value, "timestamp"):
        return int(value.timestamp() * 1000)
    return None


def _sum_buckets_since(buckets: list[tuple[int, int]], since_ms: int) -> int:
    cutoff = int(since_ms or 0)
    return sum(int(nonces) for bucket_ms, nonces in buckets if int(bucket_ms) >= cutoff)


def estimate_window_tig(
    slave_nonces: int,
    pool_window_nonces: int,
    round_tig: float,
    window_ms: int,
    round_elapsed_ms: int,
) -> float:
    """Estimated TIG for work done inside a window of the current round."""
    return allocate_tig(
        slave_nonces,
        pool_window_nonces,
        prorate_pot(round_tig, window_ms, round_elapsed_ms),
    )


def estimate_since_join_tig(
    *,
    est_tig_week: float,
    join_ms: int | None,
    round_start: int,
    now_ms: int,
    slave_nonces: int,
    pool_since_join_nonces: int,
    round_tig: float,
) -> float:
    """
    Week-to-date payout if the worker was here at round start.
    Otherwise an estimate of TIG accrued after they joined.
    """
    joined = int(join_ms or round_start)
    if joined <= int(round_start):
        return round(float(est_tig_week or 0), 6)
    window_ms = max(0, int(now_ms) - joined)
    elapsed = max(0, int(now_ms) - int(round_start))
    return estimate_window_tig(
        slave_nonces,
        pool_since_join_nonces,
        round_tig,
        window_ms,
        elapsed,
    )


def round_start_ms() -> int:
    raw = db.get_setting("current_round_start_ms", None) or db.get_setting("current_round_start", None)
    try:
        if raw is not None:
            return int(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid current_round_start value: %r", raw)
    return int(time.time() * 1000) - 7 * 24 * 60 * 60 * 1000


def _empty_stats() -> dict:
    return {
        "batches": 0,
        "nonces": 0,
        "nonces_1h": 0,
        "nonces_12h": 0,
        "nonces_24h": 0,
        "first_ms": None,
        "by_challenge": {},
    }


def _slave_work_windows(since_ms: int, now_ms: int) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in challenge_share.fetch_slave_challenge_work(int(since_ms), int(now_ms)):
        name = str(row["slave_name"])
        challenge = str(row["challenge"])
        stats = out.setdefault(name, _empty_stats())
        nonces = int(row.get("nonces") or 0)
        n1h = int(row.get("nonces_1h") or 0)
        n12h = int(row.get("nonces_12h") or 0)
        n24h = int(row.get("nonces_24h") or 0)
        batches = int(row.get("batches") or 0)
        stats["batches"] += batches
        stats["nonces"] += nonces
        stats["nonces_1h"] += n1h
        stats["nonces_12h"] += n12h
        stats["nonces_24h"] += n24h
        first_ms = _to_ms(row.get("first_ms"))
        if first_ms is not None:
            prev = stats["first_ms"]
            stats["first_ms"] = first_ms if prev is None else min(prev, first_ms)
        stats["by_challenge"][challenge] = {
            "nonces": nonces,
            "nonces_1h": n1h,
            "nonces_12h": n12h,
            "nonces_24h": n24h,
        }
    return out


def _pool_nonce_buckets(since_ms: int) -> list[tuple[int, int]]:
    try:
        rows = db.fetch_all(
            f"""
            SELECT (rb.end_time / 60000) * 60000 AS bucket_ms,
                   COALESCE(SUM({_NONCE_EXPR}), 0) AS nonces
            FROM root_batch rb
            JOIN job j ON rb.benchmark_id = j.benchmark_id
            WHERE rb.ready = true
              AND rb.end_time >= %s
              AND rb.slave IS NOT NULL
            GROUP BY 1
            ORDER BY 1
            """,
            (since_ms,),
        )
    except Exception:
        logger.exception("Failed to load pool nonce buckets for windowed TIG")
        return []
    return [
        (int(r["bucket_ms"]), int(r["nonces"] or 0))
        for r in (rows or [])
        if r.get("bucket_ms") is not None
    ]


def build_worker_earnings(
    *,
    pool_fee: float,
    fetch_round_coinbase,
) -> dict:
    """
    fetch_round_coinbase(api_url, player_id, round_num, is_final)
      -> (player_total_tig, coinbase_map) or (None, None)
    """
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < CACHE_TTL_S:
        return _cache["data"]

    start_ms = round_start_ms()
    now_ms = int(now * 1000)
    round_id_raw = db.get_setting("current_round_id", None)
    try:
        round_num = int(round_id_raw) if round_id_raw is not None else None
    except (TypeError, ValueError):
        round_num = None

    members = db.fetch_all(
        """
        SELECT slave_name, wallet_address, worker_type, active, registered_at
        FROM pool_members
        """
    ) or []
    work = _slave_work_windows(start_ms, now_ms)

    cfg_row = db.fetch_one("SELECT config FROM config LIMIT 1")
    cfg = (cfg_row or {}).get("config") or {}
    player_id = (cfg.get("player_id") or "").lower()
    api_url = (cfg.get("api_url") or "https://mainnet-api.tig.foundation").rstrip("/")

    member_tig = 0.0
    coinbase_map: dict[str, float] = {}
    if round_num is not None and player_id and not player_id.startswith("0x000000"):
        _total, fetched_map = fetch_round_coinbase(api_url, player_id, round_num, False)
        if fetched_map:
            coinbase_map = {
                str(addr).lower(): float(amt or 0)
                for addr, amt in fetched_map.items()
            }
            member_tig = round(sum(coinbase_map.values()), 6)
        elif _total:
            member_tig = round(float(_total) * max(0.0, 1.0 - float(pool_fee or 0)), 6)

    workers = []
    total_nonces = 0
    pool_challenge: dict[str, float] = {}
    for member in members:
        name = member.get("slave_name")
        if not name:
            continue
        stats = work.get(name) or _empty_stats()
        nonces = int(stats["nonces"])
        nonces_1h = int(stats.get("nonces_1h") or 0)
        nonces_12h = int(stats.get("nonces_12h") or 0)
        nonces_24h = int(stats.get("nonces_24h") or 0)
        total_nonces += nonces
        for challenge, pile in (stats.get("by_challenge") or {}).items():
            pool_challenge[challenge] = pool_challenge.get(challenge, 0.0) + float(
                pile.get("nonces") or 0
            )
        workers.append(
            {
                "slave_name": name,
                "wallet_address": member.get("wallet_address"),
                "worker_type": member.get("worker_type"),
                "active": bool(member.get("active")),
                "registered_at": member.get("registered_at"),
                # First completed work this round, not wallet/fleet signup.
                # Fleet rows share an old registered_at, so pica45 looking
                # "joined in August" made Joined collapse to Round.
                "joined_ms": stats.get("first_ms") or _to_ms(member.get("registered_at")) or start_ms,
                "batches": int(stats["batches"]),
                "nonces": nonces,
                "nonces_1h": nonces_1h,
                "nonces_12h": nonces_12h,
                "nonces_24h": nonces_24h,
                "_by_challenge": stats.get("by_challenge") or {},
            }
        )

    n_active = sum(1 for n in pool_challenge.values() if n > 0)
    pots = challenge_share.pots_by_challenge(member_tig, pool_challenge)
    gpu_family_tig = sum(
        v for c, v in pots.items() if challenge_share.is_gpu_challenge(c)
    )
    cpu_family_tig = sum(
        v for c, v in pots.items() if not challenge_share.is_gpu_challenge(c)
    )
    wallet_tig_est: dict[str, float] = {}

    for row in workers:
        by_chal = row.pop("_by_challenge") or {}
        row["est_tig"] = challenge_share.tig_from_challenge_pots(
            {c: pile.get("nonces") or 0 for c, pile in by_chal.items()},
            pool_challenge,
            pots,
        )
        row["est_tig_1h"] = challenge_share.tig_from_challenge_pots(
            {c: pile.get("nonces_1h") or 0 for c, pile in by_chal.items()},
            pool_challenge,
            pots,
        )
        row["est_tig_12h"] = challenge_share.tig_from_challenge_pots(
            {c: pile.get("nonces_12h") or 0 for c, pile in by_chal.items()},
            pool_challenge,
            pots,
        )
        row["est_tig_24h"] = challenge_share.tig_from_challenge_pots(
            {c: pile.get("nonces_24h") or 0 for c, pile in by_chal.items()},
            pool_challenge,
            pots,
        )
        row["est_tig_since_join"] = row["est_tig"]
        row["share_pct"] = (
            round((row["est_tig"] / member_tig) * 100, 4) if member_tig > 0 else 0.0
        )
        wallet = (row.get("wallet_address") or "").strip().lower()
        if wallet:
            wallet_tig_est[wallet] = wallet_tig_est.get(wallet, 0.0) + float(row["est_tig"])

    for row in workers:
        wallet = (row.get("wallet_address") or "").strip().lower()
        w_tig = float(wallet_tig_est.get(wallet, 0) or 0)
        row["wallet_share_pct"] = (
            round((row["est_tig"] / w_tig) * 100, 4) if w_tig > 0 else 0.0
        )

    workers.sort(key=lambda r: (-float(r["est_tig"]), -int(r["nonces"]), r["slave_name"] or ""))

    payload = {
        "round": round_num,
        "round_start_ms": start_ms,
        "now_ms": now_ms,
        "pool_member_tig": member_tig,
        "pool_fee_pct": round(float(pool_fee or 0) * 100, 1),
        "total_nonces": total_nonces,
        "challenge_count": n_active,
        "tig_per_challenge": {c: round(v, 6) for c, v in pots.items()},
        "gpu_pot_frac": challenge_share.gpu_pot_frac(),
        "gpu_family_tig": round(gpu_family_tig, 6),
        "cpu_family_tig": round(cpu_family_tig, 6),
        "worker_count": len(workers),
        "note": (
            f"GPU challenges share {challenge_share.gpu_pot_frac():.0%} of pool TIG "
            f"and CPU challenges share {1.0 - challenge_share.gpu_pot_frac():.0%}, "
            "then each family splits equally across the challenges it worked. "
            "24h and 1h are this machine's nonces on each challenge times "
            "that challenge's pot / pool nonces on it this round. "
            "Same split as /set-coinbase."
        ),
        "workers": workers,
    }
    _cache["data"] = payload
    _cache["ts"] = now
    return payload


def earnings_by_slave(payload: dict | None) -> dict[str, dict]:
    out = {}
    for row in (payload or {}).get("workers") or []:
        name = row.get("slave_name")
        if name:
            out[name] = row
    return out
