"""
Coinbase Allocation Updater
============================
TIG Reward Flow (important — read before editing)
-------------------------------------------------
1. Benchmarking happens over a weekly ROUND.
2. At the end of the round the operator CLAIMS the earned TIG to their wallet.
3. After claiming there is a 28-day lock before the tokens can be WITHDRAWN.

/set-coinbase does NOT transfer tokens immediately.  It tells TIG how to
SPLIT the operator's upcoming round earnings across wallet addresses.  The
actual token flow to member wallets only happens when the operator claims
at round end.

This module keeps that split accurate by recalculating each member's share
of the current round's contributions and calling /set-coinbase to keep the
on-chain allocation current.  We update it every `coinbase_update_period`
blocks so the proportions stay fresh throughout the round.

Members receive their share directly into their own wallet from TIG at
round end — the pool never holds or transfers tokens on behalf of members.
"""
import os
import time
import logging
import requests
from . import database as db

logger = logging.getLogger(__name__)

POOL_FEE = float(os.environ.get("POOL_FEE", "0.05"))
MASTER_URL = os.environ.get("MASTER_INTERNAL_URL", "http://master:3336")


def _get_current_block() -> int | None:
    """Fetch current block height from the master's latest-data endpoint."""
    try:
        resp = requests.get(f"{MASTER_URL}/get-latest-data", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            block = data.get("block") or data.get("latest_block")
            if block:
                return block.get("details", {}).get("height") or block.get("height")
    except Exception as e:
        logger.warning(f"Could not fetch block height from master: {e}")
    return None


def _get_tig_credentials() -> tuple[str, str] | tuple[None, None]:
    """Read api_key and api_url from the master's config table."""
    row = db.fetch_one("SELECT config FROM config LIMIT 1")
    if row and row["config"]:
        cfg = row["config"]
        return cfg.get("api_key"), cfg.get("api_url")
    return None, None


def _compute_allocation() -> dict[str, float]:
    """
    Compute each member's share of the round earnings based on ALL contributions
    since the current round started (set when operator marks a round as claimed).

    Returns a dict {wallet_address: fraction} where:
      - fractions are proportional to total nonces computed this round
      - the POOL_FEE fraction is NOT included (it stays with the operator wallet)
      - all returned fractions sum to (1.0 - POOL_FEE)

    Members who benchmarked early in the week and then stopped still receive
    their fair share — contributions accumulate for the full round.
    """
    round_start = db.get_setting("current_round_start", None)
    if round_start:
        rows = db.fetch_all(
            """
            SELECT wallet_address, SUM(nonces_computed) AS total_nonces
            FROM pool_contributions
            WHERE created_at >= %s
            GROUP BY wallet_address
            """,
            (round_start,),
        )
    else:
        # No round start recorded yet — sum all contributions ever
        rows = db.fetch_all(
            """
            SELECT wallet_address, SUM(nonces_computed) AS total_nonces
            FROM pool_contributions
            GROUP BY wallet_address
            """
        )

    if not rows:
        return {}

    total = sum(int(r["total_nonces"] or 0) for r in rows)
    if total == 0:
        return {}

    member_share = 1.0 - POOL_FEE
    allocation = {
        r["wallet_address"]: round((int(r["total_nonces"]) / total) * member_share, 6)
        for r in rows
        if (r["total_nonces"] or 0) > 0
    }

    # Sanity check: sum must not exceed member_share
    total_alloc = sum(allocation.values())
    if total_alloc > member_share:
        allocation = {k: round(v / total_alloc * member_share, 6) for k, v in allocation.items()}

    return allocation


def maybe_update_coinbase():
    """
    Recalculate member allocations and push an updated /set-coinbase to TIG
    if enough blocks have passed since the last update.

    This keeps the on-chain split accurate throughout the round.  The actual
    token allocation to member wallets only materialises when the operator
    claims at the end of the round.
    """
    current_block = _get_current_block()
    if current_block is None:
        return

    last_block = int(db.get_setting("last_coinbase_block", "0"))
    update_period = int(db.get_setting("coinbase_update_period", "50"))

    if current_block - last_block < update_period:
        logger.debug(
            f"Allocation update not due yet: "
            f"current={current_block}, last={last_block}, period={update_period}"
        )
        return

    api_key, api_url = _get_tig_credentials()
    if not api_key or api_key == "00000000000000000000000000000000":
        logger.warning(
            "TIG API key not configured — skipping /set-coinbase. "
            "Set your api_key via the benchmarker admin UI."
        )
        return

    allocation = _compute_allocation()
    if not allocation:
        logger.info("No contribution data yet — skipping coinbase update.")
        return

    logger.info(
        f"Updating /set-coinbase at block {current_block} "
        f"(last update: block {last_block}).  "
        f"{len(allocation)} member(s), pool fee={POOL_FEE:.0%}"
    )
    for wallet, weight in allocation.items():
        logger.info(f"  {wallet}: {weight * 100:.2f}% of round earnings")
    logger.info(
        f"  Operator retains {POOL_FEE * 100:.0f}% — "
        "paid to operator wallet when round is claimed on TIG"
    )

    success = False
    api_response = ""
    try:
        resp = requests.post(
            f"{api_url}/set-coinbase",
            json={"coinbase": allocation},
            headers={
                "X-Api-Key": api_key,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        api_response = resp.text
        success = resp.status_code == 200
        if success:
            logger.info(
                "✓ /set-coinbase updated.  Allocation will take effect "
                "when the operator claims at round end."
            )
        else:
            logger.error(
                f"✗ /set-coinbase failed ({resp.status_code}): {resp.text}"
            )
    except Exception as e:
        api_response = str(e)
        logger.error(f"✗ /set-coinbase request error: {e}")

    db.execute(
        """
        INSERT INTO pool_coinbase_history
            (distribution, block_height, api_response, success)
        VALUES (%s, %s, %s, %s)
        """,
        (
            __import__("json").dumps(allocation),
            current_block,
            api_response,
            success,
        ),
    )

    if success:
        db.set_setting("last_coinbase_block", str(current_block))
