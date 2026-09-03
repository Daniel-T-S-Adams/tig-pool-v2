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
of the current round and calling /set-coinbase. Default PAY_MODE=effort
splits the member pot by work credits (nonces × seconds/nonce). PAY_MODE=family
restores GPU PAY_GPU_POT_FRAC / CPU pots. We update every
`coinbase_update_period` blocks.

Members receive their share directly into their own wallet from TIG at
round end — the pool never holds or transfers tokens on behalf of members.
"""
import os
import time
import logging
import requests
from . import challenge_share, work_credits
from . import database as db

logger = logging.getLogger(__name__)

POOL_FEE = float(os.environ.get("POOL_FEE", "0.05"))
MASTER_URL = os.environ.get("MASTER_INTERNAL_URL", "http://master:3336")


def _get_current_block_info() -> dict | None:
    """Fetch current block metadata from the master's latest-data endpoint."""
    try:
        resp = requests.get(f"{MASTER_URL}/get-latest-data", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            block = data.get("block") or data.get("latest_block")
            if block:
                details = block.get("details") or {}
                return {
                    "height": details.get("height") or block.get("height"),
                    "round": details.get("round") or block.get("round"),
                    "id": block.get("id"),
                }
    except Exception as e:
        logger.warning(f"Could not fetch block metadata from master: {e}")
    return None


def _get_current_block() -> int | None:
    """Fetch current block height from the master's latest-data endpoint."""
    info = _get_current_block_info()
    return info.get("height") if info else None


def _get_tig_config() -> dict:
    """Read api_key and api_url from the master's config table."""
    row = db.fetch_one("SELECT config FROM config LIMIT 1")
    if row and row["config"]:
        return row["config"]
    return {}


def _get_tig_credentials() -> tuple[str, str] | tuple[None, None]:
    """Read api_key and api_url from the master's config table."""
    cfg = _get_tig_config()
    return cfg.get("api_key"), cfg.get("api_url")


def _ensure_current_round_window(current_block: int, current_round: int | None) -> bool:
    """Reset the contribution window automatically when TIG enters a new round."""
    if current_round is None:
        logger.warning(
            "Latest block did not include a round number; keeping existing coinbase contribution window."
        )
        return False

    round_id = int(current_round)
    stored_round_id = db.get_setting("current_round_id", None)
    if stored_round_id == str(round_id):
        return False

    now_ms = int(time.time() * 1000)
    db.set_setting("current_round_id", str(round_id))
    db.set_setting("current_round_start_ms", str(now_ms))
    # Keep the legacy key in sync for older admin/reporting code.
    db.set_setting("current_round_start", str(now_ms))
    logger.info(
        "Detected TIG round rollover. Starting fresh contribution window: "
        f"round_id={round_id}, block={current_block}, start_ms={now_ms}"
    )
    return True


def _current_round_start_ms() -> int | None:
    raw = db.get_setting("current_round_start_ms", None) or db.get_setting("current_round_start", None)
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid current_round_start value: %r", raw)
        return None


def _nonce_pile_allocation(round_start_ms: int | None, member_share: float) -> dict[str, float]:
    """Fallback: one pot, raw contribution nonces. Used only if challenge rows are empty."""
    if round_start_ms is not None:
        rows = db.fetch_all(
            """
            SELECT wallet_address, SUM(nonces_computed) AS total_nonces
            FROM pool_contributions
            WHERE snapshot_end_ms >= %s
            GROUP BY wallet_address
            """,
            (round_start_ms,),
        )
    else:
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

    allocation = {
        r["wallet_address"]: round((int(r["total_nonces"]) / total) * member_share, 6)
        for r in rows
        if (r["total_nonces"] or 0) > 0
    }
    total_alloc = sum(allocation.values())
    if total_alloc > member_share:
        allocation = {k: round(v / total_alloc * member_share, 6) for k, v in allocation.items()}
    return allocation


def _compute_allocation() -> dict[str, float]:
    """
    Member share of the in-progress round, matching the dashboard.

    PAY_MODE=effort (default): wallet share is effort credits / pool
    credits. PAY_MODE=family: GPU PAY_GPU_POT_FRAC, CPU the rest.
    Members who worked early and then stopped still keep that work.
    """
    member_share = 1.0 - POOL_FEE
    round_start_ms = _current_round_start_ms()
    if round_start_ms is not None:
        mode = work_credits.pay_mode()
        try:
            if mode == "effort":
                allocation = work_credits.wallet_shares(
                    int(round_start_ms),
                    scale=member_share,
                )
            else:
                allocation = challenge_share.wallet_shares(
                    int(round_start_ms),
                    scale=member_share,
                )
            if allocation:
                return allocation
            logger.warning(
                "%s allocation was empty; falling back to contribution nonces.",
                mode,
            )
        except Exception:
            logger.exception(
                "%s allocation failed; falling back to contribution nonces.",
                work_credits.pay_mode(),
            )
    return _nonce_pile_allocation(round_start_ms, member_share)


def maybe_update_coinbase():
    """
    Recalculate member allocations and push an updated /set-coinbase to TIG
    if enough blocks have passed since the last update.

    This keeps the on-chain split accurate throughout the round.  The actual
    token allocation to member wallets only materialises when the operator
    claims at the end of the round.
    """
    block_info = _get_current_block_info()
    if block_info is None or block_info.get("height") is None:
        return
    current_block = int(block_info["height"])
    current_round = block_info.get("round")

    round_changed = _ensure_current_round_window(current_block, current_round)
    round_label = current_round if current_round is not None else "unknown"

    # At round rollover: snapshot the final allocation so it survives the grace
    # period even if the contribution query starts returning new-round data first.
    if round_changed:
        db.set_setting("round_rollover_block", str(current_block))
        last_row = db.fetch_one(
            "SELECT distribution FROM pool_coinbase_history WHERE success = true ORDER BY submitted_at DESC LIMIT 1"
        )
        if last_row and last_row.get("distribution"):
            db.set_setting("prev_round_final_allocation", last_row["distribution"])
            logger.info("Locked previous round final allocation for claim grace window.")

    last_block = int(db.get_setting("last_coinbase_block", "0"))
    # TIG enforces a hard minimum of 60 blocks between /set-coinbase calls
    # (rejects with "Can only update coinbase every 60 blocks" otherwise).
    # Default here stays a couple of blocks above that floor as a safety margin.
    update_period = int(db.get_setting("coinbase_update_period", "62"))
    # Hard floor: never allow a configured value below TIG's actual on-chain
    # minimum (60 blocks) — that exact misconfiguration (period=50) caused a
    # multi-day storm of rejected /set-coinbase calls in the ledger (documented
    # 2026-06-24 through 2026-06-29). Clamp instead of trusting the stored setting.
    if update_period < 62:
        update_period = 62
    # Grace period (blocks) after round rollover during which the locked previous-
    # round allocation is kept on-chain so the operator can claim correctly.
    # Default 30 blocks (~30 min).  Configurable via coinbase_claim_grace_blocks.
    claim_grace_blocks = int(db.get_setting("coinbase_claim_grace_blocks", "30"))

    rollover_block_str = db.get_setting("round_rollover_block", None)
    if rollover_block_str:
        blocks_since_rollover = current_block - int(rollover_block_str)
        if blocks_since_rollover < claim_grace_blocks:
            logger.info(
                f"Claim grace period active ({blocks_since_rollover}/{claim_grace_blocks} blocks) — "
                f"preserving previous round allocation."
            )
            return

    if not round_changed and current_block - last_block < update_period:
        logger.debug(
            f"Allocation update not due yet: "
            f"current={current_block}, last={last_block}, period={update_period}"
        )
        return

    tig_cfg = _get_tig_config()
    api_key, api_url = tig_cfg.get("api_key"), tig_cfg.get("api_url")
    if not api_key or api_key == "00000000000000000000000000000000":
        logger.warning(
            "TIG API key not configured — skipping /set-coinbase. "
            "Set your api_key via the benchmarker admin UI."
        )
        return

    allocation = _compute_allocation()
    if not allocation:
        # At round rollover there is no contribution data for the new round yet.
        # Do NOT reset to 100% operator — that would wipe the previous round's
        # carefully calculated split and cause the wrong distribution when the
        # operator claims the previous round.  Instead, keep the last valid
        # allocation from the coinbase history so the on-chain split stays intact
        # until real new-round data arrives.
        last_row = db.fetch_one(
            """
            SELECT distribution FROM pool_coinbase_history
            WHERE success = true
            ORDER BY submitted_at DESC
            LIMIT 1
            """
        )
        if last_row and last_row.get("distribution"):
            try:
                import json as _json
                allocation = _json.loads(last_row["distribution"])
                logger.info(
                    "No current-round contribution data yet — "
                    "maintaining last valid allocation from previous round."
                )
            except Exception:
                allocation = None

        if not allocation:
            operator_wallet = tig_cfg.get("player_id")
            if not operator_wallet:
                logger.warning("No contribution data yet and player_id unavailable — skipping coinbase update.")
                return
            allocation = {operator_wallet: 1.0}
            logger.info(
                "No contribution data and no prior allocation — "
                "defaulting 100% coinbase to operator wallet."
            )

    logger.info(
        f"Updating /set-coinbase for round {round_label} at block {current_block} "
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
            (distribution, block_height, round_id, api_response, success)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            __import__("json").dumps(allocation),
            current_block,
            int(current_round) if current_round is not None else None,
            api_response,
            success,
        ),
    )
    logger.info(
        "Coinbase history recorded: success=%s block=%s members=%s response=%s",
        success,
        current_block,
        len(allocation),
        api_response[:200],
    )

    if success:
        db.set_setting("last_coinbase_block", str(current_block))
