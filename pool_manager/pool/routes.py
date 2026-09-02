"""
Pool Manager HTTP Routes
========================
Public routes:  /stats, /members, /member/{wallet}, /leaderboard, /worker-earnings
Admin routes:   /admin/invite, /admin/members, /admin/ops/metrics, /admin/ops/hit-rate (require X-Admin-Secret header)
Registration:   /register (requires valid invite code)
"""
import os
import time
import secrets
import logging
import hashlib
import re
import json
import requests as _requests
from decimal import Decimal
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from . import database as db
from . import autopilot, ai_optimizer, challenge_share, hit_rate_report, ops_metrics, worker_earnings, work_credits

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


_fleet_schema_ready = False


def _ensure_fleet_schema():
    global _fleet_schema_ready
    if _fleet_schema_ready:
        return
    if (
        db.table_exists("pool_fleets")
        and db.has_index("idx_pool_fleets_wallet")
        and db.has_index("idx_pool_members_fleet_id")
        and db.has_index("idx_pool_members_trust_state")
        and db.has_columns(
            "pool_members",
            "fleet_id",
            "worker_type",
            "machine_index",
            "declared_cores",
            "declared_gpu_model",
            "trust_state",
            "preflight_status",
            "preflight_report",
            "trusted_at",
        )
    ):
        _fleet_schema_ready = True
        return
    db.execute_many(
        (
            """
            CREATE TABLE IF NOT EXISTS pool_fleets (
                fleet_id TEXT PRIMARY KEY,
                wallet_address TEXT NOT NULL,
                label TEXT NOT NULL,
                fleet_token_hash TEXT NOT NULL UNIQUE,
                worker_type TEXT NOT NULL DEFAULT 'mixed',
                declared_cpu_machines INTEGER NOT NULL DEFAULT 0,
                declared_gpu_machines INTEGER NOT NULL DEFAULT 0,
                declared_cores_per_machine INTEGER,
                declared_gpu_model TEXT,
                active BOOLEAN NOT NULL DEFAULT true,
                created_at BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT,
                notes TEXT
            )
            """,
            None,
        ),
        ("ALTER TABLE pool_members ADD COLUMN IF NOT EXISTS fleet_id TEXT", None),
        ("ALTER TABLE pool_members ADD COLUMN IF NOT EXISTS worker_type TEXT", None),
        ("ALTER TABLE pool_members ADD COLUMN IF NOT EXISTS machine_index TEXT", None),
        ("ALTER TABLE pool_members ADD COLUMN IF NOT EXISTS declared_cores INTEGER", None),
        ("ALTER TABLE pool_members ADD COLUMN IF NOT EXISTS declared_gpu_model TEXT", None),
        ("ALTER TABLE pool_members ADD COLUMN IF NOT EXISTS trust_state TEXT NOT NULL DEFAULT 'probation'", None),
        ("ALTER TABLE pool_members ADD COLUMN IF NOT EXISTS preflight_status TEXT", None),
        ("ALTER TABLE pool_members ADD COLUMN IF NOT EXISTS preflight_report JSONB", None),
        ("ALTER TABLE pool_members ADD COLUMN IF NOT EXISTS trusted_at BIGINT", None),
        ("CREATE INDEX IF NOT EXISTS idx_pool_fleets_wallet ON pool_fleets(wallet_address)", None),
        ("CREATE INDEX IF NOT EXISTS idx_pool_members_fleet_id ON pool_members(fleet_id)", None),
        ("CREATE INDEX IF NOT EXISTS idx_pool_members_trust_state ON pool_members(trust_state)", None),
        lock_timeout="2s",
    )
    _fleet_schema_ready = True


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _slug(value: str, fallback: str = "fleet") -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", (value or "").lower()).strip("-")
    return slug[:32] or fallback


def _wallet_prefix(wallet: str) -> str:
    return wallet.lower().replace("0x", "")[:12]


def _infer_worker_type(slave_name: str, worker_type: str | None = None) -> str:
    explicit = (worker_type or "").lower().strip()
    if explicit in {"cpu", "gpu"}:
        return explicit
    name = slave_name or ""
    if name.startswith("pool-gpu-"):
        return "gpu"
    return "cpu"


def _normalise_machine_index(value: str) -> str:
    raw = (value or "").strip().lower()
    raw = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")
    if not raw:
        raise HTTPException(status_code=400, detail="machine_index is required")
    return raw[:48]


def _fleet_slave_name(wallet: str, worker_type: str, label: str, machine_index: str) -> str:
    wtype = "gpu" if worker_type == "gpu" else "cpu"
    return f"pool-{wtype}-{_wallet_prefix(wallet)}-{_slug(label)}-{_normalise_machine_index(machine_index)}"


def _create_fleet(wallet: str, label: str, worker_type: str, cpu_count: int = 0, gpu_count: int = 0,
                  cores_per_machine: int | None = None, gpu_model: str | None = None,
                  notes: str = "") -> dict:
    _ensure_fleet_schema()
    token = secrets.token_urlsafe(24)
    fleet_id = f"fleet-{_wallet_prefix(wallet)}-{_slug(label)}-{secrets.token_hex(3)}"
    now_ms = int(time.time() * 1000)
    db.execute(
        """
        INSERT INTO pool_fleets (
            fleet_id, wallet_address, label, fleet_token_hash, worker_type,
            declared_cpu_machines, declared_gpu_machines, declared_cores_per_machine,
            declared_gpu_model, created_at, notes
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            fleet_id,
            wallet,
            label,
            _hash_token(token),
            worker_type,
            max(0, int(cpu_count or 0)),
            max(0, int(gpu_count or 0)),
            cores_per_machine,
            gpu_model,
            now_ms,
            notes,
        ),
    )
    return {
        "fleet_id": fleet_id,
        "wallet_address": wallet,
        "label": label,
        "worker_type": worker_type,
        "fleet_token": token,
        "cpu_count": max(0, int(cpu_count or 0)),
        "gpu_count": max(0, int(gpu_count or 0)),
    }


def _fleet_services(worker_type: str) -> str:
    return (
        "slave vector_search hypergraph neuralnet_optimizer"
        if worker_type == "gpu"
        else "slave satisfiability vehicle_routing knapsack job_scheduling energy_arbitrage"
    )


def _fleet_install_command(token: str, worker_type: str, machine_index: str = "AUTO") -> str:
    """One-liner that clones innopool-slave, configures, and starts containers."""
    return (
        f"curl -fsSL \"{_POOL_PUBLIC_URL}/static/install.sh?cachebust=$(date +%s)\" | bash -s -- "
        f"--fleet-token {token} --worker-type {worker_type} --machine-index {machine_index}"
    )


def _compose_restart_policy_command(services: str, sudo: str = "") -> str:
    prefix = f"{sudo} " if sudo else ""
    return (
        f"{prefix}docker compose ps -q {services} "
        f"| xargs -r {prefix}docker update --restart unless-stopped"
    )


def _fleet_linux_install_script(token: str, worker_type: str) -> str:
    return f"""#!/usr/bin/env bash
set -euxo pipefail
# InnoPool custom slave install (honors Join page worker_type={worker_type})
curl -fsSL "{_POOL_PUBLIC_URL}/static/install.sh?cachebust=$(date +%s)" | bash -s -- \\
  --fleet-token "{token}" \\
  --worker-type {worker_type} \\
  --machine-index AUTO
"""


def _fleet_aws_user_data_script(token: str, worker_type: str) -> str:
    if worker_type == "gpu":
        return f"""#!/bin/bash
set -euxo pipefail
exec > >(tee -a /var/log/innopool-gpu-userdata.log) 2>&1

# Install under /home/ubuntu so SSH users can manage the slave (not /root).
export HOME=/home/ubuntu
export USER=ubuntu
export INNOPOOL_INSTALL_USER=ubuntu
export INNOPOOL_INSTALL_ROOT=/home/ubuntu
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y curl git ca-certificates gnupg python3
if curl -fsSL --connect-timeout 20 --retry 2 https://get.docker.com -o /tmp/get-docker.sh; then
  apt-get remove -y docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc || true
  sh /tmp/get-docker.sh
  apt-get install -y docker-compose-plugin || true
else
  echo "get.docker.com unreachable (TLS/network). Installing Docker from Ubuntu apt..."
  apt-get install -y docker.io docker-compose-v2 \
    || apt-get install -y docker.io docker-compose-plugin
fi
systemctl enable --now docker
usermod -aG docker ubuntu || true

apt-get install -y \\
  "linux-headers-$(uname -r)" \\
  ubuntu-drivers-common \\
  dkms \\
  build-essential
apt-get install -y "linux-modules-extra-$(uname -r)" || true
ubuntu-drivers devices || true
apt-get install -y nvidia-driver-595-open nvidia-utils-595 \\
  || apt-get install -y nvidia-driver-580-open nvidia-utils-580 \\
  || apt-get install -y nvidia-driver-580-server nvidia-utils-580-server nvidia-dkms-580-server \\
  || ubuntu-drivers install
dkms autoinstall || true
depmod -a
modprobe nvidia
nvidia-smi

curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \\
  | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \\
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \\
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update
apt-get install -y nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi

curl -fsSL "{_POOL_PUBLIC_URL}/static/install.sh?cachebust=$(date +%s)" | bash -s -- \\
  --fleet-token "{token}" \\
  --worker-type gpu \\
  --machine-index AUTO \\
  --install-root /home/ubuntu \\
  --skip-docker-install \\
  --skip-nvidia-install
chown -R ubuntu:ubuntu /home/ubuntu/innopool-slave-gpu || true

echo "INNOPOOL_CUSTOM_SLAVE_GPU_SETUP_DONE"
"""
    return f"""#!/bin/bash
set -euxo pipefail
exec > >(tee -a /var/log/innopool-userdata.log) 2>&1

# Install under /home/ubuntu so SSH users can manage the slave (not /root).
export HOME=/home/ubuntu
export USER=ubuntu
export INNOPOOL_INSTALL_USER=ubuntu
export INNOPOOL_INSTALL_ROOT=/home/ubuntu

curl -fsSL "{_POOL_PUBLIC_URL}/static/install.sh?cachebust=$(date +%s)" | bash -s -- \\
  --fleet-token "{token}" \\
  --worker-type {worker_type} \\
  --machine-index AUTO \\
  --install-root /home/ubuntu
chown -R ubuntu:ubuntu /home/ubuntu/innopool-slave-cpu /home/ubuntu/innopool-slave-gpu 2>/dev/null || true
usermod -aG docker ubuntu || true

echo "INNOPOOL_CUSTOM_SLAVE_SETUP_DONE"
"""


def _fleet_onboarding_payload(token: str, worker_type: str) -> dict:
    return {
        "install_command": _fleet_install_command(token, worker_type, "AUTO"),
        "quick_install_command": _fleet_install_command(token, worker_type, "AUTO"),
        "linux_install_script": _fleet_linux_install_script(token, worker_type),
        "aws_user_data": _fleet_aws_user_data_script(token, worker_type),
        "services": _fleet_services(worker_type),
        "slave_package": "innopool-slave",
    }


# ── public stats ───────────────────────────────────────────────────────────────

_STATS_CACHE = db.SingleFlightCache(float(os.environ.get("POOL_STATS_CACHE_S", "10")))
_HEALTH_CACHE = db.SingleFlightCache(float(os.environ.get("POOL_HEALTH_CACHE_S", "8")))


def _health_unavailable():
    return {
        "generated_at_ms": int(time.time() * 1000),
        "status": "caution",
        "gate": "unknown",
        "posture": "degraded",
        "active_slave_counts": {"cpu": 0, "gpu": 0},
        "worker_trust": {
            "cpu": {"capacity_eligible": 0, "probation": 0, "low_spec_override": 0},
            "gpu": {"capacity_eligible": 0, "probation": 0, "low_spec_override": 0},
        },
        "current": {
            "max_concurrent_benchmarks": None,
            "cpu_slots": 0,
            "gpu_slots_total": 0,
        },
        "stale_totals": {"roots": 0, "proofs": 0},
        "reward_funnel": {},
        "latest_coinbase": None,
        "challenges": [],
        "degraded": True,
        "warming": True,
    }


@router.get("/stats")
def get_pool_stats():
    """Overall pool statistics for the landing page."""
    return _STATS_CACHE.get(_get_pool_stats_uncached)


def _get_pool_stats_uncached():
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


_earnings_cache: dict = {"data": None, "ts": 0.0}
_EARNINGS_CACHE_TTL = 300   # 5 minutes for round totals
_BLOCK_REWARD_CACHE: dict = {"data": None, "ts": 0.0}
_BLOCK_REWARD_CACHE_TTL = 60  # 1 minute — updates every block (~60s)


def _fetch_round_emissions(api_url: str, player_id: str, round_num: int):
    """Fetch benchmarker earnings for one round. Returns (total, benchmarker, shared) in TIG."""
    try:
        resp = _requests.get(f"{api_url}/get-round-emissions?round={round_num}", timeout=10)
        if resp.status_code != 200:
            return None, None, None
        opow = resp.json().get("opow") or {}
        player = opow.get(player_id) or {}
        total_tig = round(int(player.get("total") or "0") / 1e18, 4)
        benchmarker_tig = round(sum(int(v) for v in (player.get("coinbase") or {}).values()) / 1e18, 4)
        shared_tig = round(int(player.get("shared") or "0") / 1e18, 4)
        return total_tig, benchmarker_tig, shared_tig
    except Exception as exc:
        logger.warning(f"Could not fetch round {round_num} emissions: {exc}")
        return None, None, None


def _fetch_block_reward(api_url: str, player_id: str):
    """Fetch live per-block TIG reward for the pool's player_id."""
    global _BLOCK_REWARD_CACHE
    now = time.time()
    if _BLOCK_REWARD_CACHE["data"] is not None and now - _BLOCK_REWARD_CACHE["ts"] < _BLOCK_REWARD_CACHE_TTL:
        return _BLOCK_REWARD_CACHE["data"]
    try:
        block_resp = _requests.get(f"{api_url}/get-block", timeout=10)
        if block_resp.status_code != 200:
            return None
        block_id = (block_resp.json().get("block") or {}).get("id")
        if not block_id:
            return None
        opow_resp = _requests.get(f"{api_url}/get-opow?block_id={block_id}", timeout=10)
        if opow_resp.status_code != 200:
            return None
        opow_data = opow_resp.json()
        # Current API: {"opow": [{"player_id", "block_data"}, ...], ...}
        # Older shapes: bare list, or dict keyed by player_id.
        if isinstance(opow_data, dict) and "opow" in opow_data:
            opow_data = opow_data.get("opow")
        entry = None
        if isinstance(opow_data, list):
            entry = next((x for x in opow_data if (x.get("player_id") or "").lower() == player_id), None)
            block_data = (entry or {}).get("block_data") or {}
        elif isinstance(opow_data, dict):
            raw = opow_data.get(player_id) or next(
                (v for k, v in opow_data.items() if k.lower() == player_id), None
            )
            block_data = (raw or {}).get("block_data") or raw or {}
        else:
            return None
        reward_raw = block_data.get("reward") or "0"
        reward_tig = round(int(reward_raw) / 1e18, 6)
        _BLOCK_REWARD_CACHE["data"] = reward_tig
        _BLOCK_REWARD_CACHE["ts"] = now
        return reward_tig
    except Exception as exc:
        logger.warning(f"Could not fetch block reward: {exc}")
        return None


_member_round_cache: dict = {}   # (round_num) -> {"ts": float, "player_total_tig": float, "coinbase": dict}
_MEMBER_ROUND_CACHE_TTL = 60      # only applies to the in-progress round; finalized rounds cache forever


def _fetch_round_coinbase_map(api_url: str, player_id: str, round_num: int, is_final: bool):
    """
    Fetch the full coinbase distribution map for one round:
    { recipient_wallet: tig_amount }, plus the pool's total TIG for that round.
    Finalized rounds are cached indefinitely (the data never changes once final);
    the current in-progress round is cached briefly since it updates every block.
    """
    cached = _member_round_cache.get(round_num)
    if cached is not None:
        if is_final or (time.time() - cached["ts"] < _MEMBER_ROUND_CACHE_TTL):
            return cached["player_total_tig"], cached["coinbase"]

    try:
        resp = _requests.get(f"{api_url}/get-round-emissions?round={round_num}", timeout=10)
        if resp.status_code != 200:
            return None, None
        opow = resp.json().get("opow") or {}
        player = opow.get(player_id) or {}
        total_tig = round(int(player.get("total") or "0") / 1e18, 4)
        coinbase_raw = player.get("coinbase") or {}
        coinbase_tig = {addr.lower(): round(int(amt) / 1e18, 6) for addr, amt in coinbase_raw.items()}
        _member_round_cache[round_num] = {
            "ts": time.time(),
            "player_total_tig": total_tig,
            "coinbase": coinbase_tig,
        }
        return total_tig, coinbase_tig
    except Exception as exc:
        logger.warning(f"Could not fetch round {round_num} coinbase map: {exc}")
        return None, None


def _current_round_start_ms() -> int | None:
    start_raw = db.get_setting("current_round_start_ms", None) or db.get_setting("current_round_start", None)
    try:
        return int(start_raw) if start_raw is not None else None
    except (TypeError, ValueError):
        return None


def _current_round_work_share(wallet: str) -> tuple[float, int, int]:
    """Challenge-weighted work share (0-1), wallet nonces, pool nonces."""
    start_ms = _current_round_start_ms()
    if start_ms is None:
        return 0.0, 0, 0
    table = challenge_share.build_round_challenge_table(int(start_ms))
    shares = challenge_share.shares_from_challenge_nonces(table["owner_challenge"], scale=1.0)
    shares_lc = {str(k).lower(): v for k, v in shares.items()}
    nonces = table.get("wallet_nonces") or {}
    nonces_lc = {str(k).lower(): int(v) for k, v in nonces.items()}
    wallet_nonces = int(nonces_lc.get(wallet, 0) or 0)
    pool_nonces = int(sum(nonces_lc.values()))
    return float(shares_lc.get(wallet, 0.0) or 0.0), wallet_nonces, pool_nonces


def _member_earnings(wallet: str, rounds: int) -> dict:
    """
    Round-by-round earnings. The in-progress round uses the same
    per-challenge pots as /set-coinbase. Finished rounds stay on
    TIG's recorded coinbase.
    """
    wallet = (wallet or "").strip().lower()
    if not wallet.startswith("0x") or len(wallet) < 10:
        return {"error": "invalid wallet address"}

    row = db.fetch_one("SELECT config FROM config LIMIT 1")
    if not row or not row["config"]:
        return {"error": "master config not available"}
    cfg = row["config"]
    player_id = (cfg.get("player_id") or "").lower()
    api_url = (cfg.get("api_url") or "https://mainnet-api.tig.foundation").rstrip("/")
    if not player_id or player_id.startswith("0x000000"):
        return {"error": "player_id not configured"}

    current_round_str = db.get_setting("current_round_id", None)
    if not current_round_str:
        return {"error": "current round not yet detected"}
    try:
        current_round = int(current_round_str)
    except (TypeError, ValueError):
        return {"error": "invalid current_round_id in pool settings"}

    rounds = max(1, min(int(rounds or 8), 26))
    history = []
    total_wallet_tig = 0.0
    for round_num in range(current_round, current_round - rounds, -1):
        if round_num < 0:
            break
        is_final = round_num < current_round
        pool_total_tig, coinbase_map = _fetch_round_coinbase_map(api_url, player_id, round_num, is_final)
        if coinbase_map is None:
            continue
        pool_tig = round(sum(coinbase_map.values()), 6) if coinbase_map else round(float(pool_total_tig or 0), 6)
        if is_final:
            wallet_tig = float(coinbase_map.get(wallet, 0.0) or 0)
            pct = round((wallet_tig / pool_tig) * 100, 2) if pool_tig else 0.0
        else:
            work_share, _wallet_nonces, pool_nonces = _current_round_work_share(wallet)
            wallet_tig = round(pool_tig * work_share, 6) if pool_tig > 0 else 0.0
            pct = round(work_share * 100, 2) if pool_nonces else 0.0
        total_wallet_tig += wallet_tig
        history.append(
            {
                "round": round_num,
                "final": is_final,
                "wallet_tig": round(wallet_tig, 6),
                "pool_coinbase_total_tig": pool_tig,
                "wallet_pct_of_coinbase": pct,
            }
        )

    return {
        "wallet": wallet,
        "rounds_checked": len(history),
        "total_tig_across_rounds": round(total_wallet_tig, 6),
        "history": history,
    }


@router.get("/worker-earnings")
def get_worker_earnings():
    """
    Per-slave TIG for the current round. Same per-challenge pots as /set-coinbase.
    """
    payload = worker_earnings.build_worker_earnings(
        pool_fee=POOL_FEE,
        fetch_round_coinbase=_fetch_round_coinbase_map,
    )
    public_workers = []
    for row in payload.get("workers") or []:
        if not row.get("active"):
            continue
        public_workers.append(
            {
                "slave_name": row.get("slave_name"),
                "worker_type": _infer_worker_type(row.get("slave_name") or "", row.get("worker_type")),
                "active": True,
                "batches": int(row.get("batches") or 0),
                "nonces": int(row.get("nonces") or 0),
                "nonces_1h": int(row.get("nonces_1h") or 0),
                "nonces_24h": int(row.get("nonces_24h") or 0),
                "joined_ms": int(row.get("joined_ms") or 0) or None,
                "est_tig": float(row.get("est_tig") or 0),
                "est_tig_since_join": float(row.get("est_tig_since_join") or 0),
                "est_tig_24h": float(row.get("est_tig_24h") or 0),
                "est_tig_1h": float(row.get("est_tig_1h") or 0),
            }
        )
    return {
        "round": payload.get("round"),
        "round_start_ms": payload.get("round_start_ms"),
        "pool_member_tig": payload.get("pool_member_tig"),
        "pool_fee_pct": payload.get("pool_fee_pct"),
        "total_nonces": payload.get("total_nonces"),
        "challenge_count": payload.get("challenge_count"),
        "tig_per_challenge": payload.get("tig_per_challenge"),
        "gpu_pot_frac": payload.get("gpu_pot_frac"),
        "gpu_family_tig": payload.get("gpu_family_tig"),
        "cpu_family_tig": payload.get("cpu_family_tig"),
        "worker_count": len(public_workers),
        "note": payload.get("note"),
        "workers": public_workers,
    }


@router.get("/member-earnings")
def get_member_earnings(wallet: str, rounds: int = 8):
    """
    Public lookup: paste a wallet to see round-by-round TIG. The in-progress
    round uses the same per-challenge split as /set-coinbase. Finished rounds
    use TIG's recorded coinbase from /get-round-emissions.
    """
    return _member_earnings(wallet, rounds)


@router.get("/earnings")
def get_pool_earnings():
    """Pool TIG earnings including live per-block reward rate."""
    global _earnings_cache
    now = time.time()

    row = db.fetch_one("SELECT config FROM config LIMIT 1")
    if not row or not row["config"]:
        return {"error": "master config not available"}
    cfg = row["config"]
    player_id = (cfg.get("player_id") or "").lower()
    api_url = (cfg.get("api_url") or "https://mainnet-api.tig.foundation").rstrip("/")
    if not player_id or player_id.startswith("0x000000"):
        return {"error": "player_id not configured"}

    # Always fetch live block reward (has its own 60s cache)
    block_reward = _fetch_block_reward(api_url, player_id)

    # Return cached round totals + fresh block reward if cache is still valid
    if _earnings_cache["data"] is not None and now - _earnings_cache["ts"] < _EARNINGS_CACHE_TTL:
        return {**_earnings_cache["data"], "block_reward_tig": block_reward}

    current_round_str = db.get_setting("current_round_id", None)
    if not current_round_str:
        return {"error": "current round not yet detected"}
    try:
        current_round = int(current_round_str)
    except (TypeError, ValueError):
        return {"error": "invalid current_round_id in pool settings"}

    cur_total, cur_benchmarker, cur_shared = _fetch_round_emissions(api_url, player_id, current_round)
    prev_round = current_round - 1 if current_round > 0 else None
    prev_total, prev_benchmarker, prev_shared = (
        _fetch_round_emissions(api_url, player_id, prev_round) if prev_round is not None else (None, None, None)
    )

    round_data = {
        "current_round": current_round,
        "current_round_tig": cur_total,
        "current_round_benchmarker_tig": cur_benchmarker,
        "current_round_shared_tig": cur_shared,
        "prev_round": prev_round,
        "prev_round_tig": prev_total,
        "prev_round_benchmarker_tig": prev_benchmarker,
        "prev_round_shared_tig": prev_shared,
    }
    _earnings_cache = {"data": round_data, "ts": now}
    return {**round_data, "block_reward_tig": block_reward}


@router.get("/health")
def get_pool_health():
    """Public, sanitized pool health summary for the dashboard."""
    return _HEALTH_CACHE.get(_get_pool_health_uncached, placeholder=_health_unavailable())


def prewarm_health_cache() -> None:
    """Build /api/health off the request path so the first public GET is real."""
    try:
        _HEALTH_CACHE.get(_get_pool_health_uncached, force=True)
        logger.info("health cache prewarmed")
    except Exception:
        logger.warning("health cache prewarm failed", exc_info=True)


def _get_pool_health_uncached():
    report = autopilot.build_report()
    readiness = report.get("scale_readiness") or {}
    stale_totals = report.get("stale_totals") or {}
    active_counts = report.get("active_slave_counts") or {}
    current_config = report.get("current_config") or {}
    resource_slots = ((current_config.get("resource_slots") or {}).get("slots") or {})
    reward_funnel = ((report.get("reward_funnel") or {}).get("summary") or {})
    worker_trust = {
        "cpu": {"capacity_eligible": 0, "probation": 0, "low_spec_override": 0},
        "gpu": {"capacity_eligible": 0, "probation": 0, "low_spec_override": 0},
    }
    for slave in report.get("slaves") or []:
        if not slave.get("active_now"):
            continue
        profile = slave.get("profile") if slave.get("profile") in ("cpu", "gpu") else "cpu"
        if slave.get("capacity_eligible"):
            worker_trust[profile]["capacity_eligible"] += 1
        elif slave.get("registered_active"):
            worker_trust[profile]["probation"] += 1
        if slave.get("preflight_status") == "low_spec_override":
            worker_trust[profile]["low_spec_override"] += 1

    stale_roots = int(stale_totals.get("roots") or 0)
    stale_proofs = int(stale_totals.get("proofs") or 0)
    gate = readiness.get("gate") or "unknown"
    if stale_roots or stale_proofs:
        status = "blocked"
    elif gate == "ready":
        status = "healthy"
    elif gate == "blocked":
        status = "caution"
    else:
        status = "caution"

    latest_coinbase = db.fetch_one(
        """
        SELECT block_height, submitted_at
        FROM pool_coinbase_history
        WHERE success = true
        ORDER BY submitted_at DESC
        LIMIT 1
        """
    )

    challenges = []
    for row in report.get("challenges") or []:
        stale_total = int(row.get("stale_roots") or 0) + int(row.get("stale_proofs") or 0)
        challenges.append(
            {
                "challenge": row.get("challenge") or "",
                "track": row.get("track") or "",
                "active_benchmarks": int(row.get("active_benchmarks") or 0),
                "roots_pending": int(row.get("roots_pending") or 0),
                "roots_inflight": int(row.get("roots_inflight") or 0),
                "stale": stale_total,
            }
        )

    return {
        "generated_at_ms": report.get("generated_at_ms"),
        "status": status,
        "gate": gate,
        "posture": readiness.get("posture"),
        "active_slave_counts": {
            "cpu": int(active_counts.get("cpu") or 0),
            "gpu": int(active_counts.get("gpu") or 0),
        },
        "worker_trust": worker_trust,
        "current": {
            "max_concurrent_benchmarks": current_config.get("max_concurrent_benchmarks"),
            "cpu_slots": int(resource_slots.get("cpu") or 0),
            "gpu_slots_total": sum(
                int(resource_slots.get(key, 0) or 0)
                for key in ("vector_search", "hypergraph", "neuralnet_optimizer")
            ),
        },
        "stale_totals": {
            "roots": stale_roots,
            "proofs": stale_proofs,
        },
        "reward_funnel": {
            "safe_to_scale_workload": reward_funnel.get("safe_to_scale_workload"),
            "proof_conversion_rate": reward_funnel.get("proof_conversion_rate"),
            "avg_time_to_proof_submit_sec": reward_funnel.get("avg_time_to_proof_submit_sec"),
        },
        "latest_coinbase": dict(latest_coinbase) if latest_coinbase else None,
        "challenges": challenges,
    }


@router.get("/leaderboard")
def get_leaderboard():
    """Top contributors for the current TIG round, matching coinbase allocation."""
    round_start = db.get_setting("current_round_start_ms", None) or db.get_setting("current_round_start", None)
    try:
        since_ms = int(round_start) if round_start else None
    except (TypeError, ValueError):
        since_ms = None
    if since_ms is None:
        since_ms = int(time.time() * 1000) - 86400000

    rows = db.fetch_all(
        """
        SELECT
            wallet_address,
            SUM(nonces_computed) AS nonces,
            SUM(batches_completed) AS batches
        FROM pool_contributions
        WHERE snapshot_end_ms >= %s
        GROUP BY wallet_address
        """,
        (since_ms,),
    ) or []
    nonce_by_wallet = {
        str(r["wallet_address"]).strip(): int(r["nonces"] or 0)
        for r in rows
        if r.get("wallet_address")
    }
    batches_by_wallet = {
        str(r["wallet_address"]).strip(): int(r["batches"] or 0)
        for r in rows
        if r.get("wallet_address")
    }
    shares = challenge_share.wallet_shares(int(since_ms), scale=1.0)
    by_lc: dict[str, str] = {}
    for wallet in list(shares) + list(nonce_by_wallet):
        by_lc.setdefault(wallet.lower(), wallet)
    ranked = sorted(
        by_lc.values(),
        key=lambda w: (
            -float(shares.get(w, shares.get(w.lower(), 0.0))),
            -int(nonce_by_wallet.get(w, nonce_by_wallet.get(w.lower(), 0))),
        ),
    )[:20]
    return [
        {
            "wallet_address": wallet,
            "nonces_round": int(nonce_by_wallet.get(wallet, nonce_by_wallet.get(wallet.lower(), 0))),
            "batches_round": int(batches_by_wallet.get(wallet, batches_by_wallet.get(wallet.lower(), 0))),
            "nonces_24h": int(nonce_by_wallet.get(wallet, nonce_by_wallet.get(wallet.lower(), 0))),
            "batches_24h": int(nonce_by_wallet.get(wallet, nonce_by_wallet.get(wallet.lower(), 0))),
            "share_pct": round(
                float(shares.get(wallet, shares.get(wallet.lower(), 0.0))) * 100, 2
            ),
        }
        for wallet in ranked
    ]


@router.get("/member/{wallet_address}")
def get_member_stats(wallet_address: str):
    """Stats for a specific pool member."""
    _ensure_fleet_schema()
    wallet_address = wallet_address.lower()
    members = db.fetch_all(
        "SELECT * FROM pool_members WHERE wallet_address = %s ORDER BY slave_name",
        (wallet_address,),
    )
    if not members:
        raise HTTPException(status_code=404, detail="Member not found")
    member = members[0]

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
    slave_list = [r["slave_name"] for r in members]
    algo_stats = []
    slave_activity = {}
    if slave_list:
        placeholders = ",".join(["%s"] * len(slave_list))  # nosec B608 — generates parameterized %s markers only, no user input interpolated
        try:
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
            cutoff_metrics = int(time.time() * 1000) - int(autopilot.METRIC_WINDOW_MS)
            activity_rows = db.fetch_all(
                f"""
            WITH root_activity AS (
                SELECT
                    slave,
                    COUNT(*) FILTER (WHERE ready IS NULL AND start_time IS NOT NULL) AS active_roots,
                    COUNT(*) FILTER (WHERE ready = true AND end_time >= %s) AS completed_recent,
                    COUNT(*) FILTER (
                        WHERE ready IS NULL
                          AND start_time IS NOT NULL
                          AND start_time < %s
                    ) AS stale_roots,
                    MAX(GREATEST(COALESCE(start_time, 0), COALESCE(end_time, 0))) AS last_root_ms
                FROM root_batch
                WHERE slave IN ({placeholders})
                GROUP BY slave
            ),
            proof_activity AS (
                SELECT
                    slave,
                    COUNT(*) FILTER (WHERE ready IS NULL AND start_time IS NOT NULL) AS active_proofs,
                    MAX(GREATEST(COALESCE(start_time, 0), COALESCE(end_time, 0))) AS last_proof_ms
                FROM proofs_batch
                WHERE slave IN ({placeholders})
                GROUP BY slave
            )
            SELECT
                COALESCE(r.slave, p.slave) AS slave_name,
                COALESCE(r.active_roots, 0) AS active_roots,
                COALESCE(r.completed_recent, 0) AS completed_recent,
                COALESCE(r.stale_roots, 0) AS stale_roots,
                COALESCE(p.active_proofs, 0) AS active_proofs,
                GREATEST(COALESCE(r.last_root_ms, 0), COALESCE(p.last_proof_ms, 0)) AS last_activity_ms
            FROM root_activity r
            FULL OUTER JOIN proof_activity p ON p.slave = r.slave
            """,
                (cutoff_metrics, cutoff_metrics) + tuple(slave_list) + tuple(slave_list),
            )
            slave_activity = {r["slave_name"]: r for r in activity_rows}
        except Exception as exc:
            logger.warning("member stats warehouse skipped: %s", exc)
            algo_stats = []
            slave_activity = {}

    earnings_payload = worker_earnings.build_worker_earnings(
        pool_fee=POOL_FEE,
        fetch_round_coinbase=_fetch_round_coinbase_map,
    )
    earnings_by_slave = worker_earnings.earnings_by_slave(earnings_payload)
    cached_slaves = autopilot.cached_slave_by_name()

    def _member_trust_state(row) -> str:
        name = row["slave_name"]
        cached = cached_slaves.get(name)
        activity = slave_activity.get(name) or {}
        fallback = {
            "slave_name": name,
            "registered_active": bool(row.get("active")),
            "completed_recent": int(activity.get("completed_recent") or 0),
            "active_unfinished": int(activity.get("active_roots") or 0),
            "stale_roots": int(activity.get("stale_roots") or 0),
            "stale_proofs": 0,
            "failed_recent": 0,
            "profile": _infer_worker_type(name, row.get("worker_type")),
        }
        return autopilot.member_display_trust_state(
            row.get("trust_state") or "probation",
            cached or fallback,
        )

    return {
        "wallet_address": member["wallet_address"],
        "slave_name": member["slave_name"],
        "round_earnings": {
            "round": earnings_payload.get("round"),
            "pool_member_tig": earnings_payload.get("pool_member_tig"),
            "pool_fee_pct": earnings_payload.get("pool_fee_pct"),
            "note": earnings_payload.get("note"),
        },
        "slaves": [
            {
                "slave_name": r["slave_name"],
                "active": r["active"],
                "registered_at": r["registered_at"],
                "worker_type": _infer_worker_type(r["slave_name"], r.get("worker_type")),
                "fleet_id": r.get("fleet_id"),
                "machine_index": r.get("machine_index"),
                "trust_state": _member_trust_state(r),
                "preflight_status": r.get("preflight_status"),
                "active_roots": int((slave_activity.get(r["slave_name"]) or {}).get("active_roots") or 0),
                "active_proofs": int((slave_activity.get(r["slave_name"]) or {}).get("active_proofs") or 0),
                "last_activity_ms": int((slave_activity.get(r["slave_name"]) or {}).get("last_activity_ms") or 0),
                "nonces_round": int((earnings_by_slave.get(r["slave_name"]) or {}).get("nonces") or 0),
                "batches_round": int((earnings_by_slave.get(r["slave_name"]) or {}).get("batches") or 0),
                "share_pct_round": float((earnings_by_slave.get(r["slave_name"]) or {}).get("wallet_share_pct") or 0),
                "est_tig_round": float((earnings_by_slave.get(r["slave_name"]) or {}).get("est_tig") or 0),
                "wallet_address": r.get("wallet_address") or member["wallet_address"],
                "est_tig_since_join": float((earnings_by_slave.get(r["slave_name"]) or {}).get("est_tig_since_join") or 0),
                "est_tig_24h": float((earnings_by_slave.get(r["slave_name"]) or {}).get("est_tig_24h") or 0),
                "est_tig_12h": float((earnings_by_slave.get(r["slave_name"]) or {}).get("est_tig_12h") or 0),
                "est_tig_1h": float((earnings_by_slave.get(r["slave_name"]) or {}).get("est_tig_1h") or 0),
                "joined_ms": int((earnings_by_slave.get(r["slave_name"]) or {}).get("joined_ms") or 0) or None,
            }
            for r in members
        ],
        "registered_at": member["registered_at"],
        "active": any(r["active"] for r in members),
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
    setup_type: str = "single"  # "single", "single_both", "fleet", or "cloud"
    fleet_label: str = "fleet"
    cpu_machines: int = 0
    gpu_machines: int = 0
    cores_per_machine: int | None = None
    gpu_model: str | None = None


@router.post("/register")
def register_member(req: RegisterRequest):
    """Register a pool member using an invite code.

    worker_type can be "cpu", "gpu", or "both". Using "both" registers
    two public slave entries (one CPU, one GPU) under a single invite code.
    """
    _ensure_fleet_schema()
    wallet = req.wallet_address.lower().strip()
    code = req.invite_code.strip()

    if not wallet.startswith("0x") or len(wallet) < 10:
        raise HTTPException(status_code=400, detail="Invalid wallet address")

    wtype = req.worker_type.lower()
    if wtype not in ("cpu", "gpu", "both", "c3"):
        raise HTTPException(status_code=400, detail="worker_type must be 'cpu', 'gpu', 'both', or 'c3'")
    setup_type = req.setup_type.lower()
    if setup_type not in ("single", "single_both", "fleet", "cloud"):
        raise HTTPException(status_code=400, detail="Invalid setup_type")

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

    # All public registrants get fleet-token onboarding (1 machine or many).
    # Join-page worker_type is the source of truth for CPU / GPU / BOTH.
    cpu_count = max(0, int(req.cpu_machines or 0))
    gpu_count = max(0, int(req.gpu_machines or 0))
    if wtype == "gpu":
        cpu_count = 0
        gpu_count = max(1, gpu_count)
    elif wtype == "both":
        cpu_count = max(1, cpu_count)
        gpu_count = max(1, gpu_count)
    else:
        # cpu / c3
        gpu_count = 0
        cpu_count = max(1, cpu_count)

    fleet_type = "mixed" if cpu_count and gpu_count else ("gpu" if gpu_count else "cpu")
    now_ms = int(time.time() * 1000)
    default_label = "home" if setup_type in ("single", "single_both") else (
        "cloud" if setup_type == "cloud" else "fleet"
    )
    fleet = _create_fleet(
        wallet,
        req.fleet_label or default_label,
        fleet_type,
        cpu_count,
        gpu_count,
        req.cores_per_machine,
        req.gpu_model,
        notes=f"self-registered via {setup_type}",
    )
    db.execute(
        "UPDATE pool_invites SET used_by = %s, used_at = %s WHERE code = %s",
        (wallet, now_ms, code),
    )
    install = {}
    onboarding = {}
    if cpu_count and gpu_count:
        # ONE command for BOTH — install.sh starts CPU + GPU in separate folders.
        both_cmd = _fleet_install_command(fleet["fleet_token"], "both", "AUTO")
        install["both"] = both_cmd
        onboarding["both"] = {
            "install_command": both_cmd,
            "quick_install_command": both_cmd,
            "linux_install_script": _fleet_linux_install_script(fleet["fleet_token"], "both"),
            "aws_user_data": (
                _fleet_aws_user_data_script(fleet["fleet_token"], "gpu").replace(
                    "--worker-type gpu",
                    "--worker-type both",
                )
            ),
            "services": "cpu+gpu",
            "slave_package": "innopool-slave",
        }
    else:
        if cpu_count:
            install["cpu"] = _fleet_install_command(fleet["fleet_token"], "cpu", "AUTO")
            onboarding["cpu"] = _fleet_onboarding_payload(fleet["fleet_token"], "cpu")
        if gpu_count:
            install["gpu"] = _fleet_install_command(fleet["fleet_token"], "gpu", "AUTO")
            onboarding["gpu"] = _fleet_onboarding_payload(fleet["fleet_token"], "gpu")

    logger.info(
        "New member registered (fleet onboarding): %s type=%s cpu=%s gpu=%s fleet=%s",
        wallet,
        fleet_type,
        cpu_count,
        gpu_count,
        fleet["fleet_id"],
    )
    return {
        "success": True,
        "wallet_address": wallet,
        "setup_type": setup_type,
        "worker_type": fleet_type if wtype != "both" else "both",
        "fleet": {
            "fleet_id": fleet["fleet_id"],
            "label": fleet["label"],
            "fleet_token": fleet["fleet_token"],
            "cpu_count": cpu_count,
            "gpu_count": gpu_count,
            "install_commands": install,
            "onboarding": onboarding,
            "config_url_example": (
                f"{_POOL_PUBLIC_URL}/api/fleet/config?token={fleet['fleet_token']}"
                f"&worker_type=cpu&machine_index=001"
            ),
        },
        "slaves": {},
    }


_POOL_SERVER_IP  = os.environ.get("POOL_SERVER_IP", "YOUR_POOL_SERVER_IP")
_MASTER_PORT     = os.environ.get("MASTER_PORT", "5115")
_PUBLIC_MASTER_HOST = os.environ.get("PUBLIC_MASTER_HOST") or _POOL_SERVER_IP
_PUBLIC_MASTER_PORT = os.environ.get("PUBLIC_MASTER_PORT") or _MASTER_PORT
_POOL_NAME       = os.environ.get("POOL_NAME", "InnoPool")
_TIG_VERSION     = os.environ.get("TIG_VERSION", "0.0.7")
_POOL_PUBLIC_URL = os.environ.get("POOL_PUBLIC_URL", "https://www.innopool.co.uk").rstrip("/")


def _build_slave_config(slave_name: str, num_workers: int = 8) -> str:
    return f"""# {_POOL_NAME} Slave Configuration (innopool-slave)
# Prefer the Join-page install.sh one-liner; this .env is written automatically.

TIG_VERSION={_TIG_VERSION}
VERSION={_TIG_VERSION}
SLAVE_NAME={slave_name}
MASTER_IP={_PUBLIC_MASTER_HOST}
MASTER_PORT={_PUBLIC_MASTER_PORT}
# CPU: installer defaults to detected logical threads.
# GPU: normally 1 worker per GPU.
NUM_WORKERS={num_workers}
INNOPOOL_IDLE_POLL_SEC=5
ALGORITHMS_DIR=$(pwd)/data/algorithms
RESULTS_DIR=$(pwd)/data/results
DASHBOARD_HOST_PORT=8787
TTL=3600
VERBOSE=
"""


def _build_slave_setup_command(slave_name: str, num_workers: int = 8, worker_type: str = "cpu") -> str:
    """Return a setup snippet that writes innopool-slave .env (used by install.sh)."""
    worker_setup = (
        'DETECTED_NUM_WORKERS="${NUM_WORKERS:-1}"'
        if worker_type == "gpu"
        else """DETECTED_NUM_WORKERS="${NUM_WORKERS:-$(python3 - <<'PY'
import os
cores = max(1, os.cpu_count() or 1)
# ~80% of cores (10–20% headroom) for telemetry earn-cap
print(max(1, (cores * 4) // 5))
PY
)}"
"""
    )
    return f"""{worker_setup}
mkdir -p data/algorithms data/results
cat > .env <<EOF
TIG_VERSION={_TIG_VERSION}
VERSION={_TIG_VERSION}
SLAVE_NAME={slave_name}
MASTER_IP={_PUBLIC_MASTER_HOST}
MASTER_PORT={_PUBLIC_MASTER_PORT}
NUM_WORKERS=$DETECTED_NUM_WORKERS
INNOPOOL_IDLE_POLL_SEC=5
ALGORITHMS_DIR=$(pwd)/data/algorithms
RESULTS_DIR=$(pwd)/data/results
DASHBOARD_HOST_PORT=8787
TTL=3600
VERBOSE=
EOF
docker compose config >/dev/null"""


def _build_slave_preflight_command(services: str, worker_type: str) -> str:
    return (
        f"# Optional legacy preflight; prefer install.sh which starts the stack directly.\n"
        f"curl -fsSL {_POOL_PUBLIC_URL}/static/preflight.sh | bash -s -- --worker-type {worker_type} {services}"
    )


def _build_slave_start_command(services: str) -> str:
    return (
        f"docker compose up -d --build --force-recreate {services}\n"
        f"{_compose_restart_policy_command(services)}"
    )


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
        "worker_type": worker_type,
        "master_ip": _PUBLIC_MASTER_HOST,
        "master_port": _PUBLIC_MASTER_PORT,
        "tig_version": _TIG_VERSION,
        "version": _TIG_VERSION,
        "slave_package": "innopool-slave",
        "slave_config": _build_slave_config(slave_name, num_workers),
        "setup_command": _build_slave_setup_command(slave_name, num_workers, worker_type),
        "preflight_command": _build_slave_preflight_command(services, worker_type),
        "start_command": _build_slave_start_command(services),
        "services": services,
    }


@router.get("/fleet/config")
def fleet_config(token: str, worker_type: str = "cpu", machine_index: str = "001"):
    """Return/create the correct slave config for one machine in a fleet."""
    _ensure_fleet_schema()
    wtype = worker_type.lower().strip()
    if wtype not in ("cpu", "gpu"):
        raise HTTPException(status_code=400, detail="worker_type must be cpu or gpu")
    index = _normalise_machine_index(machine_index)
    fleet = db.fetch_one(
        """
        SELECT *
        FROM pool_fleets
        WHERE fleet_token_hash = %s
          AND active = true
        """,
        (_hash_token(token),),
    )
    if not fleet:
        raise HTTPException(status_code=404, detail="Invalid or inactive fleet token")

    slave_name = _fleet_slave_name(fleet["wallet_address"], wtype, fleet["label"], index)
    now_ms = int(time.time() * 1000)
    db.execute(
        """
        INSERT INTO pool_members (
            wallet_address, slave_name, registered_at, notes,
            fleet_id, worker_type, machine_index, declared_cores, declared_gpu_model
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (slave_name) DO UPDATE
        SET active = true,
            fleet_id = EXCLUDED.fleet_id,
            worker_type = EXCLUDED.worker_type,
            machine_index = EXCLUDED.machine_index,
            declared_cores = EXCLUDED.declared_cores,
            declared_gpu_model = EXCLUDED.declared_gpu_model
        """,
        (
            fleet["wallet_address"],
            slave_name,
            now_ms,
            f"fleet {fleet['label']} machine {index}",
            fleet["fleet_id"],
            wtype,
            index,
            fleet.get("declared_cores_per_machine") if wtype == "cpu" else None,
            fleet.get("declared_gpu_model") if wtype == "gpu" else None,
        ),
    )
    payload = _slave_payload(slave_name, wtype)
    return {
        "success": True,
        "fleet_id": fleet["fleet_id"],
        "wallet_address": fleet["wallet_address"],
        "worker_type": wtype,
        "machine_index": index,
        **payload,
    }


class PreflightReportRequest(BaseModel):
    slave_name: str
    worker_type: str = "cpu"
    status: str
    report: dict = {}


@router.post("/slave/preflight")
def record_slave_preflight(req: PreflightReportRequest):
    """Record a public slave preflight result without granting trust."""
    _ensure_fleet_schema()
    slave_name = req.slave_name.strip()
    if not slave_name.startswith("pool-"):
        raise HTTPException(status_code=400, detail="Only public pool slaves can report preflight")
    status = req.status.lower().strip()
    if status not in {"passed", "low_spec_override", "failed"}:
        raise HTTPException(status_code=400, detail="Invalid preflight status")
    row = db.fetch_one(
        "SELECT 1 FROM pool_members WHERE slave_name = %s",
        (slave_name,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Unknown slave")
    report = dict(req.report or {})
    report["worker_type"] = req.worker_type.lower().strip()
    report["reported_at"] = int(time.time() * 1000)
    gpu_name = (report.get("gpu_name") or "").strip() or None
    db.execute(
        """
        UPDATE pool_members
        SET preflight_status = %s,
            preflight_report = %s::jsonb,
            worker_type = COALESCE(NULLIF(worker_type, ''), %s),
            declared_gpu_model = COALESCE(%s, declared_gpu_model)
        WHERE slave_name = %s
        """,
        (status, json.dumps(report), report["worker_type"], gpu_name, slave_name),
    )
    return {"success": True}


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
    _ensure_fleet_schema()
    wallet = req.wallet_address.lower().strip()
    worker_type = req.worker_type.lower().strip()
    if worker_type not in ("cpu", "gpu"):
        raise HTTPException(status_code=400, detail="worker_type must be cpu or gpu")
    slave_name = _wallet_to_slave_name(wallet, worker_type)
    now_ms = int(time.time() * 1000)

    existing = db.fetch_one(
        "SELECT 1 FROM pool_members WHERE slave_name = %s", (slave_name,)
    )
    if existing:
        raise HTTPException(status_code=409, detail="Already registered")

    db.execute(
        """
        INSERT INTO pool_members (wallet_address, slave_name, registered_at, notes, worker_type)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (wallet, slave_name, now_ms, req.notes, worker_type),
    )
    return {
        "wallet_address": wallet,
        "slave_name": slave_name,
        "slave_config": _build_slave_config(slave_name, 1 if worker_type == "gpu" else 8),
        "setup_command": _build_slave_setup_command(
            slave_name,
            1 if worker_type == "gpu" else 8,
            worker_type,
        ),
    }


class CreateFleetRequest(BaseModel):
    wallet_address: str
    label: str = "fleet"
    worker_type: str = "mixed"
    cpu_count: int = 0
    gpu_count: int = 0
    cores_per_machine: int | None = None
    gpu_model: str | None = None
    notes: str = ""


@router.post("/admin/fleets")
def create_fleet(req: CreateFleetRequest, x_admin_secret: str = Header(None)):
    _check_admin(x_admin_secret)
    wallet = req.wallet_address.lower().strip()
    if not wallet.startswith("0x") or len(wallet) < 10:
        raise HTTPException(status_code=400, detail="Invalid wallet address")
    worker_type = req.worker_type.lower().strip()
    if worker_type not in ("cpu", "gpu", "mixed"):
        raise HTTPException(status_code=400, detail="worker_type must be cpu, gpu, or mixed")
    fleet = _create_fleet(
        wallet,
        req.label,
        worker_type,
        req.cpu_count,
        req.gpu_count,
        req.cores_per_machine,
        req.gpu_model,
        req.notes,
    )
    install = {}
    onboarding = {}
    if req.cpu_count:
        install["cpu"] = _fleet_install_command(fleet["fleet_token"], "cpu", "001")
        onboarding["cpu"] = _fleet_onboarding_payload(fleet["fleet_token"], "cpu")
    if req.gpu_count:
        install["gpu"] = _fleet_install_command(fleet["fleet_token"], "gpu", "001")
        onboarding["gpu"] = _fleet_onboarding_payload(fleet["fleet_token"], "gpu")
    return {**fleet, "install_commands": install, "onboarding": onboarding}


@router.get("/admin/fleets")
def list_fleets(x_admin_secret: str = Header(None)):
    _check_admin(x_admin_secret)
    _ensure_fleet_schema()
    rows = db.fetch_all(
        """
        SELECT
            f.fleet_id,
            f.wallet_address,
            f.label,
            f.worker_type,
            f.declared_cpu_machines,
            f.declared_gpu_machines,
            f.active,
            f.created_at,
            COUNT(pm.slave_name) AS registered_slaves
        FROM pool_fleets f
        LEFT JOIN pool_members pm ON pm.fleet_id = f.fleet_id
        GROUP BY f.fleet_id
        ORDER BY f.created_at DESC
        """
    )
    return [_json_safe(r) for r in rows]


@router.get("/admin/members")
def list_members(x_admin_secret: str = Header(None)):
    _check_admin(x_admin_secret)
    _ensure_fleet_schema()
    return db.fetch_all(
        """
        SELECT
            wallet_address,
            slave_name,
            registered_at,
            active,
            COALESCE(
                NULLIF(worker_type, ''),
                CASE WHEN slave_name LIKE 'pool-gpu-%' THEN 'gpu' ELSE 'cpu' END
            ) AS worker_type,
            trust_state,
            preflight_status,
            trusted_at,
            notes
        FROM pool_members
        ORDER BY registered_at DESC
        """
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
    db.execute(f"UPDATE pool_members SET active = true WHERE {where}", params)  # nosec B608 — where clause is a hardcoded internal fragment from _member_where()
    return {"success": True}


@router.post("/admin/members/{identifier}/deactivate")
def deactivate_member_by_identifier(identifier: str, x_admin_secret: str = Header(None)):
    _check_admin(x_admin_secret)
    where, params = _member_where(identifier)
    db.execute(f"UPDATE pool_members SET active = false WHERE {where}", params)  # nosec B608 — where clause is a hardcoded internal fragment from _member_where()
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
def coinbase_history(
    x_admin_secret: str = Header(None),
    round_id: int | None = None,
    limit: int = 50,
):
    """
    Append-only audit ledger of every /set-coinbase call ever made.
    Pass ?round_id=N to see the full history for one round (for auditing
    exactly what split was on-chain at every point during that round).
    """
    _check_admin(x_admin_secret)
    limit = max(1, min(limit, 1000))
    if round_id is not None:
        rows = db.fetch_all(
            """
            SELECT id, block_height, round_id, distribution, success, api_response, submitted_at
            FROM pool_coinbase_history
            WHERE round_id = %s
            ORDER BY submitted_at ASC
            """,
            (round_id,),
        )
    else:
        rows = db.fetch_all(
            """
            SELECT id, block_height, round_id, distribution, success, api_response, submitted_at
            FROM pool_coinbase_history
            ORDER BY submitted_at DESC
            LIMIT %s
            """,
            (limit,),
        )
    return [dict(r) for r in rows]


@router.get("/admin/member-earnings")
def admin_member_earnings(
    wallet: str,
    x_admin_secret: str = Header(None),
    rounds: int = 8,
):
    """Admin variant of /member-earnings (no rate limiting) for debugging."""
    _check_admin(x_admin_secret)
    return _member_earnings(wallet, rounds)


@router.get("/admin/invites")
def list_invites(x_admin_secret: str = Header(None)):
    _check_admin(x_admin_secret)
    return db.fetch_all(
        "SELECT code, used_by, used_at, created_at, expires_at FROM pool_invites ORDER BY created_at DESC"
    )


@router.get("/admin/autopilot/report")
def autopilot_report(x_admin_secret: str = Header(None)):
    """Read-only scheduler report showing current pressure and would-change recommendations."""
    _check_admin(x_admin_secret)
    return autopilot.build_report()


@router.get("/admin/ops/metrics")
def admin_ops_metrics(x_admin_secret: str = Header(None)):
    """Private observe-only ops metrics for the operator dashboard."""
    _check_admin(x_admin_secret)
    return ops_metrics.build_ops_metrics()


@router.get("/admin/ops/hit-rate")
def admin_ops_hit_rate(x_admin_secret: str = Header(None)):
    """Observe-only per-track quality vs TIG qualifier floor, bundles, and time."""
    _check_admin(x_admin_secret)
    return hit_rate_report.build_hit_rate_report()


@router.get("/admin/payout-shadow")
def admin_payout_shadow(force: bool = False, x_admin_secret: str = Header(None)):
    """Compare nonce vs effort-credit shares. Does not change /set-coinbase."""
    _check_admin(x_admin_secret)
    return work_credits.build_shadow_report(
        pool_fee=POOL_FEE,
        round_start_ms=worker_earnings.round_start_ms(),
        force=bool(force),
    )


@router.post("/admin/ai-optimizer/run")
def ai_optimizer_run(x_admin_secret: str = Header(None)):
    """Run one read-only AI optimizer recommendation cycle."""
    _check_admin(x_admin_secret)
    return ai_optimizer.run_once(force=True)


@router.get("/admin/ai-optimizer/decisions")
def ai_optimizer_decisions(limit: int = 10, x_admin_secret: str = Header(None)):
    """List recent AI optimizer recommendations."""
    _check_admin(x_admin_secret)
    return ai_optimizer.latest(limit=limit)


@router.post("/admin/new-round")
def new_round(x_admin_secret: str = Header(None)):
    """
    Call this after you have claimed the round on TIG.
    Resets the round start timestamp so the next round's contributions
    are tracked fresh — members who only benchmarked in the previous
    round will no longer affect future allocations.
    """
    _check_admin(x_admin_secret)
    now_ms = int(__import__("time").time() * 1000)
    db.set_setting("current_round_start_ms", str(now_ms))
    db.set_setting("current_round_start", str(now_ms))
    return {"success": True, "new_round_start": now_ms}
