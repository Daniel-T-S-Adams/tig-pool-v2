"""
Contribution Tracker
====================
Runs every 60 seconds. Queries the master's root_batch / proofs_batch tables
to find completed work since the last snapshot, maps slave names to registered
pool members, and writes snapshot rows to pool_contributions.

Live /set-coinbase still uses nonces_computed. work_credits is written for
shadow compare and a future cutover.
"""
import time
import logging
from . import database as db
from . import work_credits

logger = logging.getLogger(__name__)

SNAPSHOT_INTERVAL_MS = 60_000  # take a snapshot every 60 seconds


def _get_slave_to_wallet_map() -> dict[str, str]:
    """Return {slave_name: wallet_address} for all active members."""
    rows = db.fetch_all(
        "SELECT slave_name, wallet_address FROM pool_members WHERE active = true"
    )
    return {r["slave_name"]: r["wallet_address"] for r in rows}


def take_snapshot():
    """
    Tally completed root_batch rows since the last snapshot window,
    map slave names to wallets, and write pool_contributions rows.
    """
    now_ms = int(time.time() * 1000)
    last_ms = int(db.get_setting("last_snapshot_ms", "0"))

    if now_ms - last_ms < SNAPSHOT_INTERVAL_MS:
        return  # too soon

    work_credits.ensure_contribution_credit_columns()

    slave_map = _get_slave_to_wallet_map()
    if not slave_map:
        db.set_setting("last_snapshot_ms", str(now_ms))
        return

    rows = db.fetch_all(
        """
        SELECT
            rb.slave,
            j.challenge,
            COALESCE(j.settings->>'track_id', '') AS track_id,
            COUNT(*) AS batches_completed,
            SUM(LEAST(j.batch_size, j.num_nonces - rb.batch_idx * j.batch_size)) AS nonces_computed
        FROM root_batch rb
        JOIN job j ON rb.benchmark_id = j.benchmark_id
        WHERE rb.ready = true
          AND rb.end_time >= %s
          AND rb.end_time < %s
          AND rb.slave IS NOT NULL
        GROUP BY rb.slave, j.challenge, COALESCE(j.settings->>'track_id', '')
        """,
        (last_ms, now_ms),
    )

    if not rows:
        db.set_setting("last_snapshot_ms", str(now_ms))
        return

    weights = work_credits.fetch_weight_table()
    totals: dict[str, dict] = {}
    for r in rows:
        wallet = slave_map.get(r["slave"])
        if wallet is None:
            continue
        scored = work_credits.score_root_group(
            nonces=float(r.get("nonces_computed") or 0),
            challenge=str(r.get("challenge") or ""),
            track_id=str(r.get("track_id") or ""),
            weights=weights,
        )
        entry = totals.setdefault(wallet, {"batches": 0, "nonces": 0, "credits": 0.0})
        entry["batches"] += int(r["batches_completed"] or 0)
        entry["nonces"] += int(r["nonces_computed"] or 0)
        entry["credits"] += float(scored["credits"])

    if not totals:
        db.set_setting("last_snapshot_ms", str(now_ms))
        return

    total_nonces = sum(v["nonces"] for v in totals.values())
    total_credits = sum(v["credits"] for v in totals.values())

    inserts = []
    for wallet, v in totals.items():
        share = v["nonces"] / total_nonces if total_nonces > 0 else 0.0
        credit_share = v["credits"] / total_credits if total_credits > 0 else 0.0
        inserts.append((
            """
            INSERT INTO pool_contributions
                (wallet_address, batches_completed, nonces_computed, work_credits,
                 share_fraction, credit_share_fraction,
                 snapshot_start_ms, snapshot_end_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                wallet,
                v["batches"],
                v["nonces"],
                round(v["credits"], 6),
                share,
                credit_share,
                last_ms,
                now_ms,
            ),
        ))

    db.execute_many(*inserts)
    db.set_setting("last_snapshot_ms", str(now_ms))
    logger.info(
        f"Snapshot [{last_ms} → {now_ms}]: "
        f"{len(totals)} members, {total_nonces:,} nonces, "
        f"{total_credits:,.1f} work-credits (shadow)"
    )
