#!/usr/bin/env python3
"""InnoPool admin CLI — manage invites and members without curl.

Usage:
  python3 admin.py invite                    # create 1 invite code
  python3 admin.py invite 5                  # create 5 invite codes
  python3 admin.py invites                   # list all invite codes
  python3 admin.py add <wallet> [cpu|gpu]    # add member directly (no invite needed)
  python3 admin.py create-fleet <wallet> <label> [cpu|gpu|mixed] [--cpu N] [--gpu N]
  python3 admin.py fleets                    # list registered fleets
  python3 admin.py members                   # list all registered members
  python3 admin.py activate <wallet|slave>   # reactivate a member/slave
  python3 admin.py deactivate <wallet|slave> # deactivate a member/slave
  python3 admin.py clear-slave <slave>       # unassign unfinished batches
  python3 admin.py member-health <slave>     # show slave assignment health
  python3 admin.py autopilot [--json]        # read-only scheduler report
  python3 admin.py ai-optimizer [--json]     # run read-only DeepSeek analyst
  python3 admin.py ai-decisions [N]          # show recent AI recommendations
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

def cmd_create_fleet(args):
    if len(args) < 2:
        sys.exit("Usage: python3 admin.py create-fleet <wallet> <label> [cpu|gpu|mixed] [--cpu N] [--gpu N] [--cores N] [--gpu-model MODEL]")
    wallet = args[0]
    label = args[1]
    worker_type = args[2] if len(args) > 2 and not args[2].startswith("--") else "mixed"
    opts = args[3:] if len(args) > 2 and not args[2].startswith("--") else args[2:]

    def opt_int(name, default=0):
        if name in opts:
            i = opts.index(name)
            if i + 1 < len(opts):
                return int(opts[i + 1])
        return default

    def opt_str(name, default=None):
        if name in opts:
            i = opts.index(name)
            if i + 1 < len(opts):
                return opts[i + 1]
        return default

    body = {
        "wallet_address": wallet,
        "label": label,
        "worker_type": worker_type,
        "cpu_count": opt_int("--cpu", 0),
        "gpu_count": opt_int("--gpu", 0),
        "cores_per_machine": opt_int("--cores", 0) or None,
        "gpu_model": opt_str("--gpu-model"),
    }
    result = _post("/admin/fleets", body)
    print("Fleet created:")
    print(f"  fleet_id : {result['fleet_id']}")
    print(f"  wallet   : {result['wallet_address']}")
    print(f"  label    : {result['label']}")
    print(f"  token    : {result['fleet_token']}")
    installs = result.get("install_commands") or {}
    if installs:
        print("\nInstall command templates:")
        for kind, cmd in installs.items():
            print(f"\n[{kind}]")
            print(cmd)
    else:
        print("\nUse token with /api/fleet/config to generate machine configs.")

def cmd_fleets(_):
    rows = _get("/admin/fleets")
    if not rows:
        print("No fleets registered yet.")
        return
    print(f"{'FLEET':<34} {'WALLET':<45} {'LABEL':<18} {'TYPE':<6} {'CPU':>4} {'GPU':>4} {'SLAVES':>6} {'ACTIVE'}")
    print("-" * 130)
    for r in rows:
        active = "yes" if r.get("active") else "no"
        print(
            f"{r['fleet_id']:<34} {r['wallet_address']:<45} {r['label']:<18} "
            f"{r['worker_type']:<6} {int(r.get('declared_cpu_machines') or 0):>4} "
            f"{int(r.get('declared_gpu_machines') or 0):>4} {int(r.get('registered_slaves') or 0):>6} {active}"
        )

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
    for key in (
        "assigned_total",
        "completed_total",
        "active_unfinished",
        "assigned_last_5m",
        "completed_last_5m",
        "assigned_last_30m",
        "completed_last_30m",
        "active_over_30m",
        "active_high_attempts",
        "oldest_active_min",
        "nonces_last_30m",
        "avg_runtime_sec_30m",
    ):
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

def cmd_autopilot(args):
    report = _get("/admin/autopilot/report")
    if "--json" in args:
        print(json.dumps(report, indent=2, sort_keys=True))
        return

    windows = report.get("windows", {})
    active = report.get("active_slave_counts", {})
    current = report.get("current_config", {})
    recommendations = report.get("recommendations") or []

    print("Autopilot report (read-only)")
    print(f"  active slaves : CPU={active.get('cpu', 0)} GPU={active.get('gpu', 0)}")
    print(f"  metric window : {int(windows.get('metric_window_ms', 0) / 60000)} min")
    if report.get("master_config_error"):
        print(f"  master config : ERROR {report['master_config_error']}")
    else:
        print(f"  max benchmarks: {current.get('max_concurrent_benchmarks')}")
        print(f"  resource slots: {current.get('resource_slots', {}).get('slots', {})}")

    print("\nTop slaves:")
    print(f"{'SLAVE':<32} {'TYPE':<4} {'ACTIVE':<6} {'DONE':>5} {'LIVE':>5} {'STALE':>5} {'AVG S':>7} {'IDLE M':>7}")
    print("-" * 82)
    for slave in (report.get("slaves") or [])[:20]:
        stale = int(slave.get("stale_roots") or 0) + int(slave.get("stale_proofs") or 0)
        avg = slave.get("avg_runtime_sec")
        idle = slave.get("idle_for_min")
        print(
            f"{slave.get('slave_name', ''):<32} "
            f"{slave.get('profile', ''):<4} "
            f"{'yes' if slave.get('active_now') else 'no':<6} "
            f"{int(slave.get('completed_recent') or 0):>5} "
            f"{int(slave.get('active_unfinished') or 0):>5} "
            f"{stale:>5} "
            f"{str(avg if avg is not None else '-'):>7} "
            f"{str(idle if idle is not None else '-'):>7}"
        )

    print("\nChallenge pressure:")
    print(f"{'CHALLENGE':<22} {'TRACK':<30} {'BENCH':>5} {'ROOT PEND':>9} {'ROOT LIVE':>9} {'STALE':>5}")
    print("-" * 88)
    for row in report.get("challenges") or []:
        stale = int(row.get("stale_roots") or 0) + int(row.get("stale_proofs") or 0)
        print(
            f"{row.get('challenge', ''):<22} "
            f"{str(row.get('track') or ''):<30} "
            f"{int(row.get('active_benchmarks') or 0):>5} "
            f"{int(row.get('roots_pending') or 0):>9} "
            f"{int(row.get('roots_inflight') or 0):>9} "
            f"{stale:>5}"
        )

    print("\nWould-change recommendations:")
    if not recommendations:
        print("  No changes suggested from the current window.")
    for rec in recommendations:
        print(f"  - {rec.get('key')}: {rec.get('current')} -> {rec.get('proposed')}")
        print(f"    {rec.get('reason')}")

def cmd_ai_optimizer(args):
    result = _post("/admin/ai-optimizer/run", {})
    if "--json" in args:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if result.get("status") != "ok":
        print(f"AI optimizer failed/skipped: {result.get('error') or result.get('reason')}")
        return
    rec = result.get("recommendation") or {}
    print("AI optimizer recommendation (read-only)")
    print(f"  category  : {rec.get('decision_category')}")
    print(f"  confidence: {rec.get('confidence')}")
    print(f"  approval  : {'yes' if rec.get('requires_human_approval') else 'no'}")
    print(f"  summary   : {rec.get('summary')}")
    actions = rec.get("recommended_actions") or []
    print("\nRecommended actions:")
    if not actions:
        print("  No actions recommended.")
    for action in actions:
        print(
            f"  - {action.get('action_type')} {action.get('key', '')}: "
            f"{action.get('current')} -> {action.get('proposed')}"
        )
        if action.get("reason"):
            print(f"    {action.get('reason')}")
        if action.get("rollback_condition"):
            print(f"    rollback: {action.get('rollback_condition')}")

def cmd_ai_decisions(args):
    limit = int(args[0]) if args and args[0].isdigit() else 10
    rows = _get(f"/admin/ai-optimizer/decisions?limit={limit}")
    if not rows:
        print("No AI optimizer decisions yet.")
        return
    print(f"{'ID':>4} {'STATUS':<8} {'CATEGORY':<24} {'CONF':>5} SUMMARY")
    print("-" * 100)
    for row in rows:
        conf = row.get("confidence")
        conf_s = f"{float(conf):.2f}" if conf is not None else "-"
        print(
            f"{int(row.get('id') or 0):>4} "
            f"{str(row.get('status') or ''):<8} "
            f"{str(row.get('decision_category') or ''):<24} "
            f"{conf_s:>5} "
            f"{row.get('summary') or row.get('error') or ''}"
        )

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
    "create-fleet": cmd_create_fleet,
    "fleets":    cmd_fleets,
    "members":   cmd_members,
    "activate":  cmd_activate,
    "deactivate": cmd_deactivate,
    "clear-slave": cmd_clear_slave,
    "member-health": cmd_member_health,
    "autopilot": cmd_autopilot,
    "ai-optimizer": cmd_ai_optimizer,
    "ai-decisions": cmd_ai_decisions,
    "coinbase":  cmd_coinbase,
    "new-round": cmd_new_round,
}

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] not in COMMANDS:
        print(__doc__)
        sys.exit(0)
    COMMANDS[args[0]](args[1:])
