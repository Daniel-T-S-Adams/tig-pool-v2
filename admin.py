#!/usr/bin/env python3
"""InnoPool admin CLI — manage invites and members without curl.

Usage:
  python3 admin.py invite                    # create 1 invite code
  python3 admin.py invite 5                  # create 5 invite codes
  python3 admin.py invites                   # list all invite codes
  python3 admin.py add <wallet> [cpu|gpu]    # add member directly (no invite needed)
  python3 admin.py members                   # list all registered members
  python3 admin.py coinbase                  # show last 10 coinbase updates
"""
import json
import os
import sys
import urllib.request
import urllib.error

# ── config ────────────────────────────────────────────────────────────────────
# Reads ADMIN_SECRET from .env if not set in environment
def _load_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    env = {}
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.split("#")[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

_env = _load_env()
ADMIN_SECRET = os.environ.get("ADMIN_SECRET") or _env.get("ADMIN_SECRET", "changeme")
WEB_PORT     = os.environ.get("WEB_PORT")     or _env.get("WEB_PORT", "80")
BASE_URL     = f"http://localhost:{WEB_PORT}/api"

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
    print(f"  http://localhost:{WEB_PORT}/register.html")

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

# ── main ──────────────────────────────────────────────────────────────────────
COMMANDS = {
    "invite":   cmd_invite,
    "invites":  cmd_invites,
    "add":      cmd_add,
    "members":  cmd_members,
    "coinbase": cmd_coinbase,
}

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] not in COMMANDS:
        print(__doc__)
        sys.exit(0)
    COMMANDS[args[0]](args[1:])
