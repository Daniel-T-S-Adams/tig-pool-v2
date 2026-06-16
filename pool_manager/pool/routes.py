"""
Pool Manager HTTP Routes
========================
Public routes:  /stats, /members, /member/{wallet}, /leaderboard
Admin routes:   /admin/invite, /admin/members (require X-Admin-Secret header)
Registration:   /register (requires valid invite code)
"""
import os
import time
import secrets
import logging
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from . import database as db

logger = logging.getLogger(__name__)
router = APIRouter()

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "changeme")
POOL_FEE = float(os.environ.get("POOL_FEE", "0.05"))


# ── helpers ────────────────────────────────────────────────────────────────────

def _check_admin(x_admin_secret: str | None):
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Invalid admin secret")


def _wallet_to_slave_name(wallet: str, worker_type: str = "cpu") -> str:
    """Deterministic slave name from wallet address and worker type.

    worker_type must be one of: cpu, gpu
    Generates names like pool-cpu-a330c544ec5b or pool-gpu-a330c544ec5b
    which match the master's routing regexes:
      ^pool-(aws|cpu)-.*$  → CPU challenges
      ^pool-(c3|gpu)-.*$   → GPU challenges
    """
    wtype = "gpu" if worker_type.lower() in ("gpu", "c3") else "cpu"
    short = wallet.lower().replace("0x", "")[:12]
    return f"pool-{wtype}-{short}"


# ── public stats ───────────────────────────────────────────────────────────────

@router.get("/stats")
def get_pool_stats():
    """Overall pool statistics for the landing page."""
    members = db.fetch_one(
        "SELECT COUNT(*) AS total FROM pool_members WHERE active = true"
    )
    contributions = db.fetch_one(
        """
        SELECT
            SUM(nonces_computed) AS total_nonces,
            SUM(batches_completed) AS total_batches
        FROM pool_contributions
        WHERE snapshot_end_ms > (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT - 86400000
        """
    )
    coinbase_count = db.fetch_one(
        "SELECT COUNT(*) AS total FROM pool_coinbase_history WHERE success = true"
    )
    last_distribution = db.fetch_one(
        """
        SELECT distribution, block_height, submitted_at
        FROM pool_coinbase_history
        WHERE success = true
        ORDER BY submitted_at DESC
        LIMIT 1
        """
    )
    return {
        "active_members": members["total"] if members else 0,
        "pool_fee_pct": round(POOL_FEE * 100, 1),
        "nonces_last_24h": int(contributions["total_nonces"] or 0) if contributions else 0,
        "batches_last_24h": int(contributions["total_batches"] or 0) if contributions else 0,
        "total_coinbase_updates": coinbase_count["total"] if coinbase_count else 0,
        "last_distribution": dict(last_distribution) if last_distribution else None,
    }


@router.get("/leaderboard")
def get_leaderboard():
    """Top contributors over the last 24 hours."""
    rows = db.fetch_all(
        """
        SELECT
            wallet_address,
            SUM(nonces_computed) AS nonces,
            SUM(batches_completed) AS batches
        FROM pool_contributions
        WHERE snapshot_end_ms > (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT - 86400000
        GROUP BY wallet_address
        ORDER BY nonces DESC
        LIMIT 20
        """
    )
    total = sum(r["nonces"] or 0 for r in rows)
    return [
        {
            "wallet_address": r["wallet_address"],
            "nonces_24h": int(r["nonces"] or 0),
            "batches_24h": int(r["batches"] or 0),
            "share_pct": round((r["nonces"] / total * 100), 2) if total > 0 else 0,
        }
        for r in rows
    ]


@router.get("/member/{wallet_address}")
def get_member_stats(wallet_address: str):
    """Stats for a specific pool member."""
    wallet_address = wallet_address.lower()
    member = db.fetch_one(
        "SELECT * FROM pool_members WHERE wallet_address = %s",
        (wallet_address,),
    )
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    contributions = db.fetch_all(
        """
        SELECT
            nonces_computed, batches_completed, share_fraction,
            snapshot_start_ms, snapshot_end_ms
        FROM pool_contributions
        WHERE wallet_address = %s
        ORDER BY snapshot_end_ms DESC
        LIMIT 100
        """,
        (wallet_address,),
    )

    stats_24h = db.fetch_one(
        """
        SELECT
            SUM(nonces_computed) AS nonces,
            SUM(batches_completed) AS batches,
            AVG(share_fraction) AS avg_share
        FROM pool_contributions
        WHERE wallet_address = %s
          AND snapshot_end_ms > (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT - 86400000
        """,
        (wallet_address,),
    )

    # Algorithm breakdown — what challenges/algorithms this wallet's slaves have worked on
    slave_names = db.fetch_all(
        "SELECT slave_name FROM pool_members WHERE wallet_address = %s",
        (wallet_address,),
    )
    slave_list = [r["slave_name"] for r in slave_names]
    algo_stats = []
    if slave_list:
        placeholders = ",".join(["%s"] * len(slave_list))
        algo_stats = db.fetch_all(
            f"""
            SELECT
                j.challenge,
                j.algorithm,
                COUNT(*) AS batches,
                SUM(LEAST(j.batch_size, j.num_nonces - rb.batch_idx * j.batch_size)) AS nonces
            FROM root_batch rb
            JOIN job j ON rb.benchmark_id = j.benchmark_id
            WHERE rb.slave IN ({placeholders})
              AND rb.ready = true
            GROUP BY j.challenge, j.algorithm
            ORDER BY nonces DESC
            """,
            tuple(slave_list),
        )

    return {
        "wallet_address": member["wallet_address"],
        "slave_name": member["slave_name"],
        "registered_at": member["registered_at"],
        "active": member["active"],
        "stats_24h": {
            "nonces": int(stats_24h["nonces"] or 0),
            "batches": int(stats_24h["batches"] or 0),
            "avg_share_pct": round(float(stats_24h["avg_share"] or 0) * 100, 2),
        } if stats_24h else None,
        "recent_contributions": [dict(c) for c in contributions],
        "algo_stats": [
            {
                "challenge": r["challenge"],
                "algorithm": r["algorithm"],
                "batches": int(r["batches"] or 0),
                "nonces": int(r["nonces"] or 0),
            }
            for r in algo_stats
        ],
    }


# ── registration ───────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    wallet_address: str
    invite_code: str
    worker_type: str = "cpu"  # "cpu", "gpu", or "both"


@router.post("/register")
def register_member(req: RegisterRequest):
    """Register a pool member using an invite code.

    worker_type can be "cpu", "gpu", or "both". Using "both" registers
    two slave entries (one CPU, one GPU) under a single invite code.
    """
    wallet = req.wallet_address.lower().strip()
    code = req.invite_code.strip()

    if not wallet.startswith("0x") or len(wallet) < 10:
        raise HTTPException(status_code=400, detail="Invalid wallet address")

    wtype = req.worker_type.lower()
    if wtype not in ("cpu", "gpu", "both", "c3"):
        raise HTTPException(status_code=400, detail="worker_type must be 'cpu', 'gpu', or 'both'")

    # Check invite code
    invite = db.fetch_one(
        """
        SELECT * FROM pool_invites
        WHERE code = %s
          AND used_by IS NULL
          AND (expires_at IS NULL OR expires_at > (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT)
        """,
        (code,),
    )
    if not invite:
        raise HTTPException(status_code=400, detail="Invalid or expired invite code")

    # Determine which slave names to create
    types_to_register = ["cpu", "gpu"] if wtype == "both" else [wtype]
    slave_names = {t: _wallet_to_slave_name(wallet, t) for t in types_to_register}

    # Check none already exist
    for t, sname in slave_names.items():
        existing = db.fetch_one("SELECT 1 FROM pool_members WHERE slave_name = %s", (sname,))
        if existing:
            raise HTTPException(status_code=409, detail=f"Already registered as {t} worker")

    now_ms = int(time.time() * 1000)
    inserts = [
        (
            "UPDATE pool_invites SET used_by = %s, used_at = %s WHERE code = %s",
            (wallet, now_ms, code),
        )
    ] + [
        (
            "INSERT INTO pool_members (wallet_address, slave_name, invite_code, registered_at) VALUES (%s, %s, %s, %s)",
            (wallet, sname, code, now_ms),
        )
        for sname in slave_names.values()
    ]
    db.execute_many(*inserts)

    logger.info(f"New member registered: {wallet} → {list(slave_names.values())}")
    return {
        "success": True,
        "wallet_address": wallet,
        "worker_type": wtype,
        "slaves": {t: {"slave_name": sname, "slave_config": _build_slave_config(sname)}
                   for t, sname in slave_names.items()},
        # Convenience aliases for single-type registrations
        "slave_name": slave_names.get("cpu") or slave_names.get("gpu"),
        "slave_config": _build_slave_config(list(slave_names.values())[0]),
    }


_POOL_SERVER_IP  = os.environ.get("POOL_SERVER_IP", "YOUR_POOL_SERVER_IP")
_MASTER_PORT     = os.environ.get("MASTER_PORT", "5115")
_POOL_NAME       = os.environ.get("POOL_NAME", "InnoPool")


def _build_slave_config(slave_name: str) -> str:
    return f"""# {_POOL_NAME} Slave Configuration
# Save this as your .env file in the tig-benchmarker directory

SLAVE_NAME={slave_name}
MASTER_IP={_POOL_SERVER_IP}
MASTER_PORT={_MASTER_PORT}
NUM_WORKERS=8
ALGORITHMS_DIR=./algorithms
RESULTS_DIR=./results
TTL=300
VERBOSE=
"""


# ── admin endpoints ────────────────────────────────────────────────────────────

class CreateInviteRequest(BaseModel):
    count: int = 1
    expires_in_hours: int | None = None


@router.post("/admin/invite")
def create_invites(req: CreateInviteRequest, x_admin_secret: str = Header(None)):
    _check_admin(x_admin_secret)
    now_ms = int(time.time() * 1000)
    expires_at = None
    if req.expires_in_hours:
        expires_at = now_ms + req.expires_in_hours * 3_600_000

    codes = [secrets.token_urlsafe(12) for _ in range(req.count)]
    db.execute_many(*[
        (
            "INSERT INTO pool_invites (code, expires_at) VALUES (%s, %s)",
            (code, expires_at),
        )
        for code in codes
    ])
    return {"codes": codes}


class AddMemberDirectRequest(BaseModel):
    wallet_address: str
    worker_type: str = "cpu"  # "cpu" or "gpu"
    notes: str = ""


@router.post("/admin/members")
def add_member_direct(req: AddMemberDirectRequest, x_admin_secret: str = Header(None)):
    """Add a member directly without an invite code (admin bypass)."""
    _check_admin(x_admin_secret)
    wallet = req.wallet_address.lower().strip()
    slave_name = _wallet_to_slave_name(wallet, req.worker_type)
    now_ms = int(time.time() * 1000)

    existing = db.fetch_one(
        "SELECT 1 FROM pool_members WHERE wallet_address = %s", (wallet,)
    )
    if existing:
        raise HTTPException(status_code=409, detail="Already registered")

    db.execute(
        """
        INSERT INTO pool_members (wallet_address, slave_name, registered_at, notes)
        VALUES (%s, %s, %s, %s)
        """,
        (wallet, slave_name, now_ms, req.notes),
    )
    return {
        "wallet_address": wallet,
        "slave_name": slave_name,
        "slave_config": _build_slave_config(slave_name),
    }


@router.get("/admin/members")
def list_members(x_admin_secret: str = Header(None)):
    _check_admin(x_admin_secret)
    return db.fetch_all(
        "SELECT wallet_address, slave_name, registered_at, active, notes FROM pool_members ORDER BY registered_at DESC"
    )


@router.delete("/admin/members/{wallet_address}")
def deactivate_member(wallet_address: str, x_admin_secret: str = Header(None)):
    _check_admin(x_admin_secret)
    db.execute(
        "UPDATE pool_members SET active = false WHERE wallet_address = %s",
        (wallet_address.lower(),),
    )
    return {"success": True}


@router.get("/admin/coinbase-history")
def coinbase_history(x_admin_secret: str = Header(None)):
    _check_admin(x_admin_secret)
    rows = db.fetch_all(
        """
        SELECT id, block_height, distribution, success, api_response, submitted_at
        FROM pool_coinbase_history
        ORDER BY submitted_at DESC
        LIMIT 50
        """
    )
    return [dict(r) for r in rows]


@router.get("/admin/invites")
def list_invites(x_admin_secret: str = Header(None)):
    _check_admin(x_admin_secret)
    return db.fetch_all(
        "SELECT code, used_by, used_at, created_at, expires_at FROM pool_invites ORDER BY created_at DESC"
    )


@router.post("/admin/new-round")
def new_round(x_admin_secret: str = Header(None)):
    """
    Call this after you have claimed the round on TIG.
    Resets the round start timestamp so the next round's contributions
    are tracked fresh — members who only benchmarked in the previous
    round will no longer affect future allocations.
    """
    _check_admin(x_admin_secret)
    now = __import__("datetime").datetime.utcnow().isoformat()
    db.set_setting("current_round_start", now)
    return {"success": True, "new_round_start": now}
