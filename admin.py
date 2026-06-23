#!/usr/bin/env python3
"""InnoPool admin CLI — manage invites and members without curl.

Usage:
  python3 admin.py invite                    # create 1 invite code
  python3 admin.py invite 5                  # create 5 invite codes
  python3 admin.py invites                   # list all invite codes
  python3 admin.py add <wallet> [cpu|gpu]    # add member directly (no invite needed)
  python3 admin.py members                   # list all registered members
  python3 admin.py activate <wallet|slave>   # reactivate a member/slave
  python3 admin.py deactivate <wallet|slave> # deactivate a member/slave
  python3 admin.py clear-slave <slave>       # unassign unfinished batches
  python3 admin.py member-health <slave>     # show slave assignment health
  python3 admin.py coinbase                  # show last 10 coinbase updates
"""
import json
import os
import sys
import urllib.request
import urllib.error

# ── config ────────────────────────────────────────────────────────────────────
# Reads ADMIN_SECRET from .env if not set in environment.
# On the VPS the public HTTP port is normally handled by Caddy, so the CLI should
# talk to the local nginx/pool API port directly instead of following redirects
# through the public domain.
def _load_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    env = {}
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.split("#")[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

_env = _load_env()
ADMIN_SECRET    = os.environ.get("ADMIN_SECRET") or _env.get("ADMIN_SECRET", "changeme")
WEB_PORT        = os.environ.get("WEB_PORT") or _env.get("WEB_PORT", "8088")
ADMIN_BASE_URL  = os.environ.get("ADMIN_BASE_URL") or _env.get("ADMIN_BASE_URL")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL") or _env.get("PUBLIC_BASE_URL", "https://www.innopool.co.uk")
BASE_URL        = (ADMIN_BASE_URL or f"http://127.0.0.1:{WEB_PORT}/api").rstrip("/")

# ── http helpers ──────────────────────────────────────────────────────────────
def _req(method, path, body=None):
    url = BASE_URL + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Content-Type": "application/json",
            "X-Admin-Secret": ADMIN_SECRET,
        },
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Request failed: {e}")
        print(f"Tried: {url}")
        print("If running on the VPS, set ADMIN_BASE_URL=http://127.0.0.1:8088/api or check WEB_PORT in .env.")
        sys.exit(1)

def _get(path):   return _req("GET", path)
def _post(path, body=None): return _req("POST", path, body)

# ── commands ──────────────────────────────────────────────────────────────────
def cmd_invite(args):
    count = int(args[0]) if args else 1
    result = _post("/admin/invite", {"count": count})
    print(f"Created {len(result['codes'])} invite code(s):\n")
    for code in result["codes"]:
        print(f"  {code}")
    print(f"\nShare the registration URL:")
    print(f"  {PUBLIC_BASE_URL.rstrip('/')}/register.html")

def cmd_invites(_):
    rows = _get("/admin/invites")
    if not rows:
        print("No invite codes yet.")
        return
    print(f"{'CODE':<20} {'USED BY':<45} {'CREATED'}")
    print("-" * 80)
    for r in rows:
        used = r["used_by"] or "—"
        from datetime import datetime
        created = datetime.fromtimestamp(r["created_at"] / 1000).strftime("%Y-%m-%d %H:%M")
        print(f"{r['code']:<20} {used:<45} {created}")

def cmd_add(args):
    if not args:
        sys.exit("Usage: python3 admin.py add <wallet_address> [cpu|gpu]")
    wallet = args[0]
    worker_type = args[1] if len(args) > 1 else "cpu"
    result = _post("/admin/members", {"wallet_address": wallet, "worker_type": worker_type})
    print(f"Member added:")
    print(f"  wallet     : {result['wallet_address']}")
    print(f"  slave_name : {result['slave_name']}")
    print(f"\nSlave config to give to the member:")
    print("─" * 40)
    print(result["slave_config"])

def cmd_members(_):
    rows = _get("/admin/members")
    if not rows:
        print("No members registered yet.")
        return
    print(f"{'WALLET':<45} {'SLAVE NAME':<30} {'ACTIVE'}")
    print("-" * 85)
    for r in rows:
        active = "yes" if r["active"] else "no"
        print(f"{r['wallet_address']:<45} {r['slave_name']:<30} {active}")

def cmd_activate(args):
    if not args:
        sys.exit("Usage: python3 admin.py activate <wallet_or_slave_name>")
    ident = args[0]
    _post(f"/admin/members/{ident}/activate", {})
    print(f"Activated: {ident}")

def cmd_deactivate(args):
    if not args:
        sys.exit("Usage: python3 admin.py deactivate <wallet_or_slave_name>")
    ident = args[0]
    _post(f"/admin/members/{ident}/deactivate", {})
    print(f"Deactivated: {ident}")

def cmd_clear_slave(args):
    if not args:
        sys.exit("Usage: python3 admin.py clear-slave <slave_name>")
    slave = args[0]
    _post(f"/admin/slaves/{slave}/clear", {})
    print(f"Cleared unfinished assignments for: {slave}")

def cmd_member_health(args):
    if not args:
        sys.exit("Usage: python3 admin.py member-health <slave_name>")
    slave = args[0]
    result = _get(f"/admin/slaves/{slave}/health")
    member = result.get("member") or {}
    root = result.get("root_batches") or {}
    proofs = result.get("proof_batches") or {}
    print(f"Slave: {slave}")
    if member:
        print(f"  wallet : {member.get('wallet_address')}")
        print(f"  active : {'yes' if member.get('active') else 'no'}")
        if member.get("notes"):
            print(f"  notes  : {member.get('notes')}")
    else:
        print("  member : not registered")
    print("\nRoot batches:")
    for key in ("assigned_total", "completed_total", "active_unfinished", "assigned_last_5m", "completed_last_5m"):
        print(f"  {key:<18}: {root.get(key, 0)}")
    print("\nProof batches:")
    for key in ("assigned_total", "completed_total", "active_unfinished"):
        print(f"  {key:<18}: {proofs.get(key, 0)}")
    active = result.get("active_root_batches") or []
    if active:
        print("\nActive root batches:")
        for r in active[:20]:
            print(
                f"  {r['benchmark']} {r['challenge']} {r['track']} "
                f"batch={r['batch_idx']} attempts={r['num_attempts']} age_min={r['assigned_min']}"
            )

def cmd_coinbase(_):
    rows = _get("/admin/coinbase-history")
    if not rows:
        print("No coinbase updates yet.")
        return
    print(f"Last {len(rows)} coinbase update(s):\n")
    for r in rows[:10]:
        from datetime import datetime
        ts = datetime.fromtimestamp(r["submitted_at"] / 1000).strftime("%Y-%m-%d %H:%M")
        ok = "OK" if r["success"] else "FAIL"
        dist = r.get("distribution", {})
        n = len(dist) if isinstance(dist, dict) else "?"
        print(f"  [{ok}] block={r['block_height']}  members={n}  at={ts}")

def cmd_new_round(_):
    """
    Run this AFTER you have claimed the round on TIG.
    Resets the contribution window so the next round starts fresh.
    Members who only benchmarked in the previous round will not carry
    their contributions forward into the new round.
    """
    result = _post("/admin/new-round", {})
    print(f"New round started.")
    print(f"  Round start: {result['new_round_start']}")
    print(f"  Contributions before this timestamp will no longer affect /set-coinbase.")

# ── main ──────────────────────────────────────────────────────────────────────
COMMANDS = {
    "invite":    cmd_invite,
    "invites":   cmd_invites,
    "add":       cmd_add,
    "members":   cmd_members,
    "activate":  cmd_activate,
    "deactivate": cmd_deactivate,
    "clear-slave": cmd_clear_slave,
    "member-health": cmd_member_health,
    "coinbase":  cmd_coinbase,
    "new-round": cmd_new_round,
}

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] not in COMMANDS:
        print(__doc__)
        sys.exit(0)
    COMMANDS[args[0]](args[1:])
