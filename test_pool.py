#!/usr/bin/env python3
"""
InnoPool end-to-end health check.
Run from the tig-pool directory: python3 test_pool.py
"""
import json, urllib.request, urllib.error, urllib.parse, sys, time, base64, os

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
WARN = "\033[33m⚠\033[0m"
HEAD = "\033[1;36m"
END  = "\033[0m"

errors = 0

def section(title):
    print(f"\n{HEAD}{'─'*52}{END}")
    print(f"{HEAD}  {title}{END}")
    print(f"{HEAD}{'─'*52}{END}")

def ok(msg, detail=""):
    print(f"  {PASS} {msg}" + (f"  →  {detail}" if detail else ""))

def fail(msg, detail=""):
    global errors
    errors += 1
    print(f"  {FAIL} {msg}" + (f"  →  {detail}" if detail else ""))

def warn(msg, detail=""):
    print(f"  {WARN} {msg}" + (f"  →  {detail}" if detail else ""))

def http(method, url, body=None, headers=None, timeout=12):
    h = headers or {}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in h.items():
        req.add_header(k, v)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        raw = r.read()
        try:
            return r.status, json.loads(raw)
        except Exception:
            return r.status, raw.decode(errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {"http_error": e.reason}
        return e.code, body
    except Exception as e:
        return 0, {"connection_error": str(e)}

def get(url, headers=None, **kw):  return http("GET",  url, headers=headers, **kw)
def post(url, body, headers=None, **kw): return http("POST", url, body=body, headers=headers, **kw)

# ── Load .env ──────────────────────────────────────────────────────────────
env = {}
try:
    for line in open(".env"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            # Strip inline comments (docker-compose does this; Python's open() doesn't)
            v = v.split(" #")[0].split("\t#")[0]
            env[k.strip()] = v.strip()
except FileNotFoundError:
    print("Run this from the /home/kevin/tig-pool directory")
    sys.exit(1)

OP_USER  = env.get("OPERATOR_USER", "admin")
OP_PASS  = env.get("OPERATOR_PASSWORD", "")
ADM_SEC  = env.get("ADMIN_SECRET", "")
WEB_PORT = env.get("WEB_PORT", "80")
OP_PORT  = env.get("OPERATOR_PORT", "8888")
MST_PORT = env.get("MASTER_PORT", "5115")

PUBLIC   = f"http://localhost:{WEB_PORT}"
OPERATOR = f"http://localhost:{OP_PORT}"
MASTER   = f"http://localhost:{MST_PORT}"
op_creds = base64.b64encode(f"{OP_USER}:{OP_PASS}".encode()).decode()
op_auth  = {"Authorization": f"Basic {op_creds}"}
adm_hdr  = {"X-Admin-Secret": ADM_SEC}

# ═══════════════════════════════════════════════════════════════════════════
section("1. Master API  (config + live blockchain data)")
# ═══════════════════════════════════════════════════════════════════════════

status, data = get(f"{OPERATOR}/get-config", headers=op_auth)
if status == 200 and isinstance(data, dict):
    ok("GET /get-config")
    pid = data.get("player_id", "")
    if pid.startswith("0x"):
        ok("  player_id", pid)
    else:
        fail("  player_id not set — open admin UI and enter it")
    if data.get("api_key"):
        ok("  api_key is set")
    else:
        fail("  api_key not set — open admin UI and enter it")
    slaves = data.get("slaves", [])
    if slaves:
        ok("  slave regex", slaves[0].get("name_regex","?"))
    else:
        warn("  no slave regex configured")
elif status == 401:
    fail("GET /get-config → 401 Unauthorized",
         "OPERATOR_PASSWORD in .env may not match nginx image password")
elif status == 200:
    fail("GET /get-config → 200 but returned HTML (routing issue)", str(data)[:80])
else:
    fail(f"GET /get-config → {status}", str(data))

status, data = get(f"{OPERATOR}/get-latest-data", headers=op_auth, timeout=20)
if status == 200 and isinstance(data, dict):
    block = data.get("block", {})
    height = block.get("details", {}).get("height", "?")
    ok("GET /get-latest-data", f"block height={height}")
    jobs = data.get("jobs", {})
    ok(f"  active algorithm challenges", str(len(jobs)))
elif status == 401:
    fail("GET /get-latest-data → 401 (same credential issue)")
else:
    fail(f"GET /get-latest-data → {status}", str(data))

# ═══════════════════════════════════════════════════════════════════════════
section("2. Pool Manager  (public stats)")
# ═══════════════════════════════════════════════════════════════════════════

status, data = get(f"{PUBLIC}/api/stats")
if status == 200 and isinstance(data, dict):
    ok("GET /api/stats")
    ok("  active members", str(data.get("active_members", 0)))
    ok("  pool fee",       f"{data.get('pool_fee_pct', 0)}%")
else:
    fail(f"GET /api/stats → {status}", str(data)[:200])

status, data = get(f"{PUBLIC}/api/leaderboard")
if status == 200 and isinstance(data, list):
    ok("GET /api/leaderboard", f"{len(data)} member(s) shown")
else:
    fail(f"GET /api/leaderboard → {status}", str(data)[:200])

# ═══════════════════════════════════════════════════════════════════════════
section("3. Invite & Registration flow")
# ═══════════════════════════════════════════════════════════════════════════

if not ADM_SEC or ADM_SEC == "changeme":
    warn("ADMIN_SECRET not set in .env — skipping")
else:
    # Create invite via header auth
    status, data = post(f"{PUBLIC}/api/admin/invite",
                        {"count": 1},
                        headers=adm_hdr)
    if status == 200:
        codes = data.get("codes", [])
        if codes:
            invite_code = codes[0]
            ok("POST /api/admin/invite", f"code={invite_code}")

            # Register test wallet
            test_wallet = "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
            status2, data2 = post(f"{PUBLIC}/api/register",
                                  {"wallet_address": test_wallet,
                                   "invite_code": invite_code})
            if status2 == 200:
                ok("POST /api/register", f"slave={data2.get('slave_name','?')}")

                # Duplicate should be rejected
                status3, _ = post(f"{PUBLIC}/api/register",
                                  {"wallet_address": test_wallet,
                                   "invite_code": invite_code})
                if status3 in (400, 409):
                    ok("Duplicate registration rejected correctly", str(status3))
                else:
                    warn(f"Duplicate allowed? Got {status3}")

                # Member lookup
                status4, data4 = get(f"{PUBLIC}/api/member/{test_wallet}")
                if status4 == 200:
                    ok("GET /api/member/<wallet>",
                       f"slave={data4.get('slave_name','?')}")
                else:
                    fail(f"GET /api/member → {status4}", str(data4)[:200])
            elif status2 == 409:
                ok("Test wallet already registered (prior run)")
            else:
                fail(f"POST /api/register → {status2}", str(data2)[:200])
    elif status == 401:
        fail("POST /api/admin/invite → 401  (ADMIN_SECRET wrong?)")
    else:
        fail(f"POST /api/admin/invite → {status}", str(data)[:200])

# ═══════════════════════════════════════════════════════════════════════════
section("4. Admin read endpoints")
# ═══════════════════════════════════════════════════════════════════════════

if ADM_SEC and ADM_SEC != "changeme":
    status, data = get(f"{PUBLIC}/api/admin/members", headers=adm_hdr)
    if status == 200 and isinstance(data, list):
        ok("GET /api/admin/members", f"{len(data)} member(s)")
        for m in data:
            print(f"      wallet={m.get('wallet_address','?')[:20]}...  "
                  f"slave={m.get('slave_name','?')}  "
                  f"active={m.get('active','?')}")
    else:
        fail(f"GET /api/admin/members → {status}", str(data)[:200])

    status, data = get(f"{PUBLIC}/api/admin/invites", headers=adm_hdr)
    if status == 200 and isinstance(data, list):
        ok("GET /api/admin/invites", f"{len(data)} invite(s)")
    else:
        fail(f"GET /api/admin/invites → {status}", str(data)[:200])

    status, data = get(f"{PUBLIC}/api/admin/coinbase-history", headers=adm_hdr)
    if status == 200 and isinstance(data, list):
        ok("GET /api/admin/coinbase-history", f"{len(data)} entries")
    else:
        fail(f"GET /api/admin/coinbase-history → {status}", str(data)[:200])

# ═══════════════════════════════════════════════════════════════════════════
section("5. Slave connectivity")
# ═══════════════════════════════════════════════════════════════════════════

# Master slave port (5115) is accessible locally for testing
status, data = get(f"{MASTER}/", timeout=5)
if status in (200, 404, 405, 422):
    ok(f"Master slave port {MST_PORT} reachable (HTTP {status})")
else:
    fail(f"Master slave port {MST_PORT} → {status}", str(data)[:100])

# Confirm the slave regex
status2, cfg = get(f"{OPERATOR}/get-config", headers=op_auth)
if status2 == 200:
    for s in cfg.get("slaves", []):
        ok(f"Slave name pattern", s.get("name_regex","?"))
        print(f"      Slaves must be named:  pool-<something>")
        print(f"      Connect to:            <server-ip>:{MST_PORT}")

# ═══════════════════════════════════════════════════════════════════════════
section("Summary")
# ═══════════════════════════════════════════════════════════════════════════
if errors == 0:
    print(f"\n  {PASS} All checks passed — InnoPool is ready!\n")
else:
    print(f"\n  {FAIL} {errors} check(s) failed — see above\n")

print(f"  Public website   →  {PUBLIC}")
print(f"  Register page    →  {PUBLIC}/register.html")
print(f"  Admin UI         →  {OPERATOR}  (login required)")
print(f"  Slave master IP  →  localhost:{MST_PORT}  (use server IP in production)")
print()
