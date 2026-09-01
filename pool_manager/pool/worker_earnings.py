"""
Display-only per-slave TIG estimates.

Payouts stay per wallet via /set-coinbase. Each slave's estimate is that
wallet's current-round coinbase TIG, split by the slave's share of the
wallet's completed root nonces. A member's slave rows therefore sum to
that member's coinbase, not to a pool-wide slice of it.
"""
from __future__ import annotations

import logging
import time

from . import database as db

logger = logging.getLogger("pool.worker_earnings")

CACHE_TTL_S = 60
_cache: dict = {"data": None, "ts": 0.0}


HOUR_MS = 60 * 60 * 1000
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


_NONCE_EXPR = "LEAST(j.batch_size, j.num_nonces - rb.batch_idx * j.batch_size)"


def _slave_work_windows(since_ms: int, now_ms: int) -> dict[str, dict]:
    h1 = int(now_ms) - HOUR_MS
    h24 = int(now_ms) - DAY_MS
    rows = db.fetch_all(
        f"""
        SELECT
            rb.slave AS slave_name,
            COUNT(*) AS batches,
            COALESCE(SUM({_NONCE_EXPR}), 0) AS nonces,
            COALESCE(SUM({_NONCE_EXPR}) FILTER (WHERE rb.end_time >= %s), 0) AS nonces_1h,
            COALESCE(SUM({_NONCE_EXPR}) FILTER (WHERE rb.end_time >= %s), 0) AS nonces_24h,
            MIN(rb.end_time) AS first_ms
        FROM root_batch rb
        JOIN job j ON rb.benchmark_id = j.benchmark_id
        WHERE rb.ready = true
          AND rb.end_time >= %s
          AND rb.slave IS NOT NULL
        GROUP BY rb.slave
        """,
        (h1, h24, since_ms),
    )
    return {
        str(r["slave_name"]): {
            "batches": int(r["batches"] or 0),
            "nonces": int(r["nonces"] or 0),
            "nonces_1h": int(r["nonces_1h"] or 0),
            "nonces_24h": int(r["nonces_24h"] or 0),
            "first_ms": _to_ms(r.get("first_ms")),
        }
        for r in (rows or [])
        if r.get("slave_name")
    }


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
    elapsed_ms = max(1, now_ms - int(start_ms))
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
    pool_nonces_1h = 0
    pool_nonces_24h = 0
    wallet_nonces: dict[str, int] = {}
    for member in members:
        name = member.get("slave_name")
        if not name:
            continue
        stats = work.get(name) or {
            "batches": 0,
            "nonces": 0,
            "nonces_1h": 0,
            "nonces_24h": 0,
            "first_ms": None,
        }
        nonces = int(stats["nonces"])
        nonces_1h = int(stats.get("nonces_1h") or 0)
        nonces_24h = int(stats.get("nonces_24h") or 0)
        wallet = (member.get("wallet_address") or "").strip().lower()
        total_nonces += nonces
        pool_nonces_1h += nonces_1h
        pool_nonces_24h += nonces_24h
        if wallet:
            wallet_nonces[wallet] = wallet_nonces.get(wallet, 0) + nonces
        workers.append(
            {
                "slave_name": name,
                "wallet_address": member.get("wallet_address"),
                "worker_type": member.get("worker_type"),
                "active": bool(member.get("active")),
                "registered_at": member.get("registered_at"),
                "joined_ms": _to_ms(member.get("registered_at")) or stats.get("first_ms") or start_ms,
                "batches": int(stats["batches"]),
                "nonces": nonces,
                "nonces_1h": nonces_1h,
                "nonces_24h": nonces_24h,
            }
        )

    # Payouts are per wallet. Each slave's estimate is that wallet's coinbase
    # TIG split by the slave's share of the wallet's completed root nonces.
    for row in workers:
        wallet = (row.get("wallet_address") or "").strip().lower()
        w_nonces = int(wallet_nonces.get(wallet, 0) or 0)
        wallet_tig = float(coinbase_map.get(wallet, 0) or 0)
        if wallet_tig <= 0 and w_nonces > 0 and member_tig > 0:
            wallet_tig = allocate_tig(w_nonces, total_nonces, member_tig)
        row["share_pct"] = (
            round((row["nonces"] / total_nonces) * 100, 4) if total_nonces > 0 else 0.0
        )
        row["wallet_share_pct"] = (
            round((row["nonces"] / w_nonces) * 100, 4) if w_nonces > 0 else 0.0
        )
        row["est_tig"] = allocate_tig(row["nonces"], w_nonces, wallet_tig)

    late_joiners = any(int(row["joined_ms"]) > start_ms for row in workers)
    buckets = _pool_nonce_buckets(start_ms) if late_joiners else []

    for row in workers:
        row["est_tig_1h"] = estimate_window_tig(
            row["nonces_1h"],
            pool_nonces_1h,
            member_tig,
            min(HOUR_MS, elapsed_ms),
            elapsed_ms,
        )
        row["est_tig_24h"] = estimate_window_tig(
            row["nonces_24h"],
            pool_nonces_24h,
            member_tig,
            min(DAY_MS, elapsed_ms),
            elapsed_ms,
        )
        join_ms = int(row["joined_ms"])
        pool_since_join = _sum_buckets_since(buckets, join_ms)
        if pool_since_join <= 0 and total_nonces > 0 and join_ms > start_ms:
            window_ms = max(0, now_ms - join_ms)
            pool_since_join = int(round(total_nonces * min(1.0, window_ms / elapsed_ms)))
        row["est_tig_since_join"] = estimate_since_join_tig(
            est_tig_week=row["est_tig"],
            join_ms=join_ms,
            round_start=start_ms,
            now_ms=now_ms,
            slave_nonces=row["nonces"],
            pool_since_join_nonces=pool_since_join,
            round_tig=member_tig,
        )

    workers.sort(key=lambda r: (-float(r["est_tig"]), -int(r["nonces"]), r["slave_name"] or ""))

    payload = {
        "round": round_num,
        "round_start_ms": start_ms,
        "now_ms": now_ms,
        "pool_member_tig": member_tig,
        "pool_fee_pct": round(float(pool_fee or 0) * 100, 1),
        "total_nonces": total_nonces,
        "worker_count": len(workers),
        "note": (
            "Approximate. This week is the real wallet payout split. "
            "Since joined / 24h / 1h estimate TIG accrued in that window only — "
            "TIG does not pay hourly."
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
