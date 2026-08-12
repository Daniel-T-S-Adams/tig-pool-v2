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


def allocate_tig(nonces: int, total_nonces: int, pool_tig: float) -> float:
    """Proportional TIG for one slave. Zero if any input is non-positive."""
    if int(nonces or 0) <= 0 or int(total_nonces or 0) <= 0 or float(pool_tig or 0) <= 0:
        return 0.0
    return round(float(pool_tig) * (int(nonces) / int(total_nonces)), 6)


def round_start_ms() -> int:
    raw = db.get_setting("current_round_start_ms", None) or db.get_setting("current_round_start", None)
    try:
        if raw is not None:
            return int(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid current_round_start value: %r", raw)
    return int(time.time() * 1000) - 7 * 24 * 60 * 60 * 1000


def _slave_work_since(since_ms: int) -> dict[str, dict]:
    rows = db.fetch_all(
        """
        SELECT
            rb.slave AS slave_name,
            COUNT(*) AS batches,
            COALESCE(
                SUM(LEAST(j.batch_size, j.num_nonces - rb.batch_idx * j.batch_size)),
                0
            ) AS nonces
        FROM root_batch rb
        JOIN job j ON rb.benchmark_id = j.benchmark_id
        WHERE rb.ready = true
          AND rb.end_time >= %s
          AND rb.slave IS NOT NULL
        GROUP BY rb.slave
        """,
        (since_ms,),
    )
    return {
        str(r["slave_name"]): {
            "batches": int(r["batches"] or 0),
            "nonces": int(r["nonces"] or 0),
        }
        for r in (rows or [])
        if r.get("slave_name")
    }


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
    round_id_raw = db.get_setting("current_round_id", None)
    try:
        round_num = int(round_id_raw) if round_id_raw is not None else None
    except (TypeError, ValueError):
        round_num = None

    members = db.fetch_all(
        """
        SELECT slave_name, wallet_address, worker_type, active
        FROM pool_members
        """
    ) or []
    work = _slave_work_since(start_ms)

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
    wallet_nonces: dict[str, int] = {}
    for member in members:
        name = member.get("slave_name")
        if not name:
            continue
        stats = work.get(name) or {"batches": 0, "nonces": 0}
        nonces = int(stats["nonces"])
        wallet = (member.get("wallet_address") or "").strip().lower()
        total_nonces += nonces
        if wallet:
            wallet_nonces[wallet] = wallet_nonces.get(wallet, 0) + nonces
        workers.append(
            {
                "slave_name": name,
                "wallet_address": member.get("wallet_address"),
                "worker_type": member.get("worker_type"),
                "active": bool(member.get("active")),
                "batches": int(stats["batches"]),
                "nonces": nonces,
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

    workers.sort(key=lambda r: (-float(r["est_tig"]), -int(r["nonces"]), r["slave_name"] or ""))

    payload = {
        "round": round_num,
        "round_start_ms": start_ms,
        "pool_member_tig": member_tig,
        "pool_fee_pct": round(float(pool_fee or 0) * 100, 1),
        "total_nonces": total_nonces,
        "worker_count": len(workers),
        "note": (
            "Approximate. Payouts are per wallet at round claim. "
            "Each slave's TIG is that wallet's current-round coinbase split by "
            "the slave's share of the wallet's completed root nonces."
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
