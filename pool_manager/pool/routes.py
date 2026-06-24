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
from decimal import Decimal
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from . import database as db

logger = logging.getLogger(__name__)
router = APIRouter()

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "changeme")
POOL_FEE = float(os.environ.get("POOL_FEE", "0.05"))


def _json_safe(row):
    if row is None:
        return None
    out = {}
    for key, value in dict(row).items():
        if isinstance(value, Decimal):
            out[key] = float(value)
        else:
            out[key] = value
    return out


# ── helpers ────────────────────────────────────────────────────────────────────

def _check_admin(x_admin_secret: str | None):
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Invalid admin secret")


def _wallet_to_slave_name(wallet: str, worker_type: str = "cpu") -> str:
    """Generate a unique slave name for this wallet+type combination.

    Generates names like pool-cpu-a330c544ec5b or pool-gpu-a330c544ec5b.
    If that name is already taken, appends -2, -3, etc.
    Matches the master's routing regexes:
      ^pool-cpu-.*$  → CPU challenges
      ^pool-gpu-.*$  → GPU challenges
    """
    wtype = "gpu" if worker_type.lower() == "gpu" else "cpu"
    short = wallet.lower().replace("0x", "")[:12]
    base = f"pool-{wtype}-{short}"
    if not db.fetch_one("SELECT 1 FROM pool_members WHERE slave_name = %s", (base,)):
        return base
    i = 2
    while True:
        candidate = f"{base}-{i}"
        if not db.fetch_one("SELECT 1 FROM pool_members WHERE slave_name = %s", (candidate,)):
            return candidate
        i += 1


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
            CASE WHEN pool_total.total_nonces > 0
                 THEN SUM(nonces_computed)::float / pool_total.total_nonces
                 ELSE 0 END AS avg_share
        FROM pool_contributions
        CROSS JOIN (
            SELECT NULLIF(SUM(nonces_computed), 0) AS total_nonces
            FROM pool_contributions
            WHERE snapshot_end_ms > (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT - 86400000
        ) pool_total
        WHERE wallet_address = %s
          AND snapshot_end_ms > (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT - 86400000
        GROUP BY pool_total.total_nonces
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
                SUM(LEAST(j.batch_size, j.num_nonces - rb.batch_idx * j.batch_size)) AS nonces,
                COUNT(*) FILTER (WHERE rb.ready = true) AS completed_batches
            FROM root_batch rb
            JOIN job j ON rb.benchmark_id = j.benchmark_id
            WHERE rb.slave IN ({placeholders})
              AND rb.start_time IS NOT NULL
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
                "completed_batches": int(r["completed_batches"] or 0),
                "nonces": int(r["nonces"] or 0),
                "in_progress": int(r["completed_batches"] or 0) < int(r["batches"] or 0),
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
    two public slave entries (one CPU, one GPU) under a single invite code.
    """
    wallet = req.wallet_address.lower().strip()
    code = req.invite_code.strip()

    if not wallet.startswith("0x") or len(wallet) < 10:
        raise HTTPException(status_code=400, detail="Invalid wallet address")

    wtype = req.worker_type.lower()
    if wtype not in ("cpu", "gpu", "both", "c3"):
        raise HTTPException(status_code=400, detail="worker_type must be 'cpu', 'gpu', 'both', or 'c3'")

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
        "slaves": {t: _slave_payload(sname, t) for t, sname in slave_names.items()},
        # Convenience aliases for single-type registrations
        "slave_name": slave_names.get("cpu") or slave_names.get("gpu"),
        "slave_config": _build_slave_config(list(slave_names.values())[0]),
    }


_POOL_SERVER_IP  = os.environ.get("POOL_SERVER_IP", "YOUR_POOL_SERVER_IP")
_MASTER_PORT     = os.environ.get("MASTER_PORT", "5115")
_PUBLIC_MASTER_HOST = os.environ.get("PUBLIC_MASTER_HOST") or _POOL_SERVER_IP
_PUBLIC_MASTER_PORT = os.environ.get("PUBLIC_MASTER_PORT") or _MASTER_PORT
_POOL_NAME       = os.environ.get("POOL_NAME", "InnoPool")
_TIG_VERSION     = os.environ.get("TIG_VERSION", "0.0.6")
_POOL_PUBLIC_URL = os.environ.get("POOL_PUBLIC_URL", "https://www.innopool.co.uk").rstrip("/")


def _build_slave_config(slave_name: str, num_workers: int = 8) -> str:
    return f"""# {_POOL_NAME} Slave Configuration
# Generated for the tig-benchmarker directory.
# The setup command on the registration page writes absolute paths for you.

VERSION={_TIG_VERSION}
SLAVE_NAME={slave_name}
MASTER_IP={_PUBLIC_MASTER_HOST}
MASTER_PORT={_PUBLIC_MASTER_PORT}
# Adjust NUM_WORKERS for your machine.
# CPU: start around your available CPU threads, then reduce if the machine becomes unstable.
# GPU: normally use 1 worker per GPU.
NUM_WORKERS={num_workers}
ALGORITHMS_DIR=./algorithms
RESULTS_DIR=./results
TTL=300
VERBOSE=
"""


def _build_slave_setup_command(slave_name: str, num_workers: int = 8) -> str:
    """Return a copy-paste setup command to run from tig-benchmarker.

    The official slave uses in-container paths (algorithms/results), while
    docker compose uses ALGORITHMS_DIR/RESULTS_DIR to mount host directories
    into /app. Writing absolute host paths avoids bad mounts from a partial
    or previously-created .env.
    """
    return f"""mkdir -p algorithms results
cat > .env <<EOF
VERSION={_TIG_VERSION}
SLAVE_NAME={slave_name}
MASTER_IP={_PUBLIC_MASTER_HOST}
MASTER_PORT={_PUBLIC_MASTER_PORT}
NUM_WORKERS={num_workers}
ALGORITHMS_DIR=$(pwd)/algorithms
RESULTS_DIR=$(pwd)/results
TTL=300
VERBOSE=
EOF
docker compose -f slave.yml config >/dev/null"""


def _build_slave_preflight_command(services: str) -> str:
    return f"curl -fsSL {_POOL_PUBLIC_URL}/static/preflight.sh | bash -s -- {services}"


def _build_slave_start_command(services: str) -> str:
    return f"docker compose -f slave.yml up -d --force-recreate {services}"


def _slave_payload(slave_name: str, worker_type: str) -> dict:
    is_gpu = worker_type == "gpu"
    services = (
        "slave vector_search hypergraph neuralnet_optimizer"
        if is_gpu
        else "slave satisfiability vehicle_routing knapsack job_scheduling energy_arbitrage"
    )
    num_workers = 1 if is_gpu else 8
    return {
        "slave_name": slave_name,
        "slave_config": _build_slave_config(slave_name, num_workers),
        "setup_command": _build_slave_setup_command(slave_name, num_workers),
        "preflight_command": _build_slave_preflight_command(services),
        "start_command": _build_slave_start_command(services),
        "services": services,
    }


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
        "SELECT 1 FROM pool_members WHERE slave_name = %s", (slave_name,)
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
        "setup_command": _build_slave_setup_command(slave_name),
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


def _member_where(identifier: str) -> tuple[str, tuple]:
    ident = identifier.strip()
    if ident.startswith("pool-"):
        return "slave_name = %s", (ident,)
    return "lower(wallet_address) = lower(%s)", (ident,)


@router.post("/admin/members/{identifier}/activate")
def activate_member(identifier: str, x_admin_secret: str = Header(None)):
    _check_admin(x_admin_secret)
    where, params = _member_where(identifier)
    db.execute(f"UPDATE pool_members SET active = true WHERE {where}", params)
    return {"success": True}


@router.post("/admin/members/{identifier}/deactivate")
def deactivate_member_by_identifier(identifier: str, x_admin_secret: str = Header(None)):
    _check_admin(x_admin_secret)
    where, params = _member_where(identifier)
    db.execute(f"UPDATE pool_members SET active = false WHERE {where}", params)
    return {"success": True}


@router.post("/admin/slaves/{slave_name}/clear")
def clear_slave_assignments(slave_name: str, x_admin_secret: str = Header(None)):
    _check_admin(x_admin_secret)
    db.execute_many(
        (
            """
            UPDATE root_batch
            SET slave = NULL, start_time = NULL, end_time = NULL
            WHERE slave = %s
              AND ready IS NULL
            """,
            (slave_name,),
        ),
        (
            """
            UPDATE proofs_batch
            SET slave = NULL, start_time = NULL, end_time = NULL
            WHERE slave = %s
              AND ready IS NULL
            """,
            (slave_name,),
        ),
    )
    return {"success": True}


@router.get("/admin/slaves/{slave_name}/health")
def slave_health(slave_name: str, x_admin_secret: str = Header(None)):
    _check_admin(x_admin_secret)
    member = db.fetch_one(
        "SELECT wallet_address, slave_name, active, notes FROM pool_members WHERE slave_name = %s",
        (slave_name,),
    )
    root = db.fetch_one(
        """
        SELECT
          COUNT(*) FILTER (WHERE rb.start_time IS NOT NULL) AS assigned_total,
          COUNT(*) FILTER (WHERE rb.ready = true) AS completed_total,
          COUNT(*) FILTER (WHERE rb.ready IS NULL AND rb.start_time IS NOT NULL) AS active_unfinished,
          COUNT(*) FILTER (WHERE rb.start_time > ((EXTRACT(EPOCH FROM NOW()) * 1000) - 300000)) AS assigned_last_5m,
          COUNT(*) FILTER (WHERE rb.ready = true AND rb.end_time > ((EXTRACT(EPOCH FROM NOW()) * 1000) - 300000)) AS completed_last_5m,
          COUNT(*) FILTER (WHERE rb.start_time > ((EXTRACT(EPOCH FROM NOW()) * 1000) - 1800000)) AS assigned_last_30m,
          COUNT(*) FILTER (WHERE rb.ready = true AND rb.end_time > ((EXTRACT(EPOCH FROM NOW()) * 1000) - 1800000)) AS completed_last_30m,
          COUNT(*) FILTER (
            WHERE rb.ready IS NULL
              AND rb.start_time IS NOT NULL
              AND rb.start_time < ((EXTRACT(EPOCH FROM NOW()) * 1000) - 1800000)
          ) AS active_over_30m,
          COUNT(*) FILTER (
            WHERE rb.ready IS NULL
              AND rb.start_time IS NOT NULL
              AND rb.num_attempts >= 3
          ) AS active_high_attempts,
          ROUND(MAX((EXTRACT(EPOCH FROM NOW()) * 1000 - rb.start_time)) FILTER (
            WHERE rb.ready IS NULL AND rb.start_time IS NOT NULL
          ) / 60000.0, 1) AS oldest_active_min,
          COALESCE(SUM(LEAST(j.batch_size, j.num_nonces - rb.batch_idx * j.batch_size)) FILTER (
            WHERE rb.ready = true AND rb.end_time > ((EXTRACT(EPOCH FROM NOW()) * 1000) - 1800000)
          ), 0) AS nonces_last_30m,
          ROUND(AVG(rb.end_time - rb.start_time) FILTER (
            WHERE rb.ready = true AND rb.end_time > ((EXTRACT(EPOCH FROM NOW()) * 1000) - 1800000)
          ) / 1000.0, 1) AS avg_runtime_sec_30m
        FROM root_batch rb
        JOIN job j ON j.benchmark_id = rb.benchmark_id
        WHERE rb.slave = %s
        """,
        (slave_name,),
    )
    proofs = db.fetch_one(
        """
        SELECT
          COUNT(*) FILTER (WHERE start_time IS NOT NULL) AS assigned_total,
          COUNT(*) FILTER (WHERE ready = true) AS completed_total,
          COUNT(*) FILTER (WHERE ready IS NULL AND start_time IS NOT NULL) AS active_unfinished
        FROM proofs_batch
        WHERE slave = %s
        """,
        (slave_name,),
    )
    recent = db.fetch_all(
        """
        SELECT
          left(rb.benchmark_id, 10) AS benchmark,
          j.challenge,
          j.algorithm,
          j.settings->>'track_id' AS track,
          rb.batch_idx,
          rb.ready,
          rb.num_attempts,
          ROUND((EXTRACT(EPOCH FROM NOW()) * 1000 - rb.start_time) / 60000.0, 1) AS assigned_min
        FROM root_batch rb
        JOIN job j ON j.benchmark_id = rb.benchmark_id
        WHERE rb.slave = %s
          AND rb.ready IS NULL
          AND rb.start_time IS NOT NULL
        ORDER BY rb.start_time DESC
        LIMIT 20
        """,
        (slave_name,),
    )
    return {
        "member": _json_safe(member),
        "root_batches": _json_safe(root) or {},
        "proof_batches": _json_safe(proofs) or {},
        "active_root_batches": [_json_safe(r) for r in recent],
    }


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
