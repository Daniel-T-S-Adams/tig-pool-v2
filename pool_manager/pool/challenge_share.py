"""
Per-challenge TIG share (live pay and display).

TIG influence uses one challenge factor per challenge, then averages them.
A knapsack nonce is not worth a hypergraph nonce. The accurate pool split is:

    each challenge with pool work gets an equal slice of the member pot
    wallet/machine TIG = sum_c (their nonces_c / pool nonces_c) × slice

/set-coinbase and the dashboard both use this. Finished rounds still show
TIG's recorded coinbase; this module is for the in-progress round.
"""
from __future__ import annotations

import logging
import time
from typing import Mapping

logger = logging.getLogger("pool.challenge_share")

_NONCE_EXPR = "LEAST(j.batch_size, j.num_nonces - rb.batch_idx * j.batch_size)"

_TABLE_TTL_S = 30.0
_table_cache: dict = {"ts": 0.0, "since_ms": None, "data": None}


def shares_from_challenge_nonces(
    owner_challenge_nonces: Mapping[tuple[str, str], float],
    *,
    scale: float = 1.0,
) -> dict[str, float]:
    """
    owner_challenge_nonces: (owner_id, challenge) -> nonces.

    Each challenge with a positive pool total gets an equal pot.
    Owner share = mean over those challenges of (owner / pool) × scale.
    """
    share = max(0.0, float(scale or 0))
    pool: dict[str, float] = {}
    owners: set[str] = set()
    cleaned: dict[tuple[str, str], float] = {}
    for key, raw in (owner_challenge_nonces or {}).items():
        if not key or len(key) != 2:
            continue
        owner, challenge = str(key[0] or ""), str(key[1] or "")
        n = float(raw or 0)
        if not owner or not challenge or n <= 0:
            continue
        cleaned[(owner, challenge)] = cleaned.get((owner, challenge), 0.0) + n
        pool[challenge] = pool.get(challenge, 0.0) + n
        owners.add(owner)

    active = [c for c, total in pool.items() if total > 0]
    n_active = len(active)
    if n_active <= 0 or share <= 0 or not owners:
        return {}

    out: dict[str, float] = {}
    for owner in owners:
        acc = 0.0
        for challenge in active:
            total = pool[challenge]
            mine = cleaned.get((owner, challenge), 0.0)
            acc += mine / total
        out[owner] = round((acc / n_active) * share, 6)

    summed = sum(out.values())
    if summed > share and summed > 0:
        out = {k: round(v / summed * share, 6) for k, v in out.items()}
    return out


def pot_per_challenge(pool_tig: float, n_active: int) -> float:
    if int(n_active or 0) <= 0 or float(pool_tig or 0) <= 0:
        return 0.0
    return float(pool_tig) / int(n_active)


def tig_from_challenge_pots(
    owner_nonces: Mapping[str, float],
    pool_nonces: Mapping[str, float],
    slice_tig: float,
) -> float:
    """TIG for one owner from their per-challenge nonces at the current slice."""
    if float(slice_tig or 0) <= 0:
        return 0.0
    total = 0.0
    for challenge, raw in (owner_nonces or {}).items():
        n = float(raw or 0)
        pool = float((pool_nonces or {}).get(challenge) or 0)
        if n <= 0 or pool <= 0:
            continue
        total += (n / pool) * float(slice_tig)
    return round(total, 6)


def _renormalize(allocation: dict[str, float], scale: float) -> dict[str, float]:
    share = max(0.0, float(scale or 0))
    summed = sum(allocation.values())
    if summed > share and summed > 0:
        return {k: round(v / summed * share, 6) for k, v in allocation.items()}
    return allocation


def fetch_slave_challenge_work(
    since_ms: int,
    now_ms: int | None = None,
) -> list[dict]:
    """Completed ready roots since ``since_ms``, one row per slave × challenge."""
    from . import database as db

    params: list = [int(since_ms)]
    windows = ""
    if now_ms is not None:
        h1 = int(now_ms) - 60 * 60 * 1000
        h12 = int(now_ms) - 12 * 60 * 60 * 1000
        h24 = int(now_ms) - 24 * 60 * 60 * 1000
        windows = f"""
            , COALESCE(SUM({_NONCE_EXPR}) FILTER (WHERE rb.end_time >= %s), 0) AS nonces_1h
            , COALESCE(SUM({_NONCE_EXPR}) FILTER (WHERE rb.end_time >= %s), 0) AS nonces_12h
            , COALESCE(SUM({_NONCE_EXPR}) FILTER (WHERE rb.end_time >= %s), 0) AS nonces_24h
        """
        params = [h1, h12, h24, int(since_ms)]

    rows = db.fetch_all(
        f"""
        SELECT
            rb.slave AS slave_name,
            j.challenge AS challenge,
            COUNT(*) AS batches,
            COALESCE(SUM({_NONCE_EXPR}), 0) AS nonces,
            MIN(rb.end_time) AS first_ms
            {windows}
        FROM root_batch rb
        JOIN job j ON rb.benchmark_id = j.benchmark_id
        WHERE rb.ready = true
          AND rb.end_time >= %s
          AND rb.slave IS NOT NULL
        GROUP BY rb.slave, j.challenge
        """,
        tuple(params),
    )
    return [dict(r) for r in (rows or []) if r.get("slave_name") and r.get("challenge")]


def _active_slave_wallets() -> dict[str, str]:
    from . import database as db

    rows = db.fetch_all(
        "SELECT slave_name, wallet_address FROM pool_members WHERE active = true"
    ) or []
    out: dict[str, str] = {}
    for r in rows:
        name = r.get("slave_name")
        wallet = str(r.get("wallet_address") or "").strip()
        if name and wallet:
            out[str(name)] = wallet
    return out


def build_round_challenge_table(since_ms: int, *, force: bool = False) -> dict:
    """Wallet × challenge nonces for the current round (cached briefly)."""
    now = time.time()
    cached = _table_cache.get("data")
    if (
        not force
        and cached is not None
        and _table_cache.get("since_ms") == int(since_ms)
        and now - float(_table_cache.get("ts") or 0) < _TABLE_TTL_S
    ):
        return cached

    slave_wallet = _active_slave_wallets()
    owner_challenge: dict[tuple[str, str], float] = {}
    pool_challenge: dict[str, float] = {}
    wallet_nonces: dict[str, int] = {}
    for row in fetch_slave_challenge_work(int(since_ms)):
        wallet = slave_wallet.get(str(row["slave_name"]))
        if not wallet:
            continue
        challenge = str(row["challenge"])
        nonces = int(row.get("nonces") or 0)
        if nonces <= 0:
            continue
        key = (wallet, challenge)
        owner_challenge[key] = owner_challenge.get(key, 0.0) + float(nonces)
        pool_challenge[challenge] = pool_challenge.get(challenge, 0.0) + float(nonces)
        wallet_nonces[wallet] = wallet_nonces.get(wallet, 0) + nonces

    n_active = sum(1 for n in pool_challenge.values() if n > 0)
    payload = {
        "owner_challenge": owner_challenge,
        "pool_challenge": pool_challenge,
        "wallet_nonces": wallet_nonces,
        "n_active": n_active,
    }
    _table_cache["data"] = payload
    _table_cache["since_ms"] = int(since_ms)
    _table_cache["ts"] = now
    return payload


def wallet_shares(
    since_ms: int,
    *,
    scale: float = 1.0,
    force: bool = False,
) -> dict[str, float]:
    table = build_round_challenge_table(int(since_ms), force=force)
    return _renormalize(
        shares_from_challenge_nonces(table["owner_challenge"], scale=scale),
        scale,
    )
