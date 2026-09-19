# InnoPool

A self-hosted, open-source mining pool for [The Innovation Game (TIG)](https://tig.foundation), built on the official `tig-benchmarker` master/slave architecture.

Pool members run TIG slave nodes pointing at your server. The pool manager tracks contributions, distributes rewards on-chain via `/set-coinbase`, and automatically tunes pool capacity through an autopilot system.

---

## Features

- **Fleet-based registration** — members register with invite codes, receive a fleet token and auto-generated slave name
- **Live pool website** — public stats, leaderboard, per-member dashboard, and fleet install scripts
- **Autopilot** — continuously tunes benchmark slots, capacities, and workload based on live pool health
- **Worker trust system** — probation/trust states gate capacity contributions from community miners
- **Quality audit** — master spot-checks the qualities every slave posts by re-scoring original solutions with `tig-verifier`; mismatches auto-quarantine the member and keep the evidence
- **AI co-pilot** — optional DeepSeek-backed advisor that reviews pool health and surfaces recommendations (read-only, operator-approved)
- **Scheduler** — optional benchmark pre-seeding to keep the master active during quiet periods
- **Admin CLI** — `admin.py` for all operator tasks without needing to call the API directly
- **Security audit tool** — `tools/security_audit.py` for pre-release static analysis

---

## Architecture

```
Community Miners (slave nodes)
        │ :5115
        ▼
  TIG Master (Docker)          ← manages jobs, roots, proofs
        │ :3336 (internal)
        ▼
  Pool Manager (FastAPI)       ← tracks contributions, autopilot, coinbase
        │
  Auditor (Docker)             ← re-scores sampled leaves with tig-verifier
        │   └─ CPU challenge runtimes (knapsack, energy_arbitrage, …)
  PostgreSQL (Docker)          ← shared schema for master + pool
        │
  Nginx (Docker)               ← serves pool website, proxies /api/ and /benchmarker/
        │ :80 / :443
        ▼
  Public Internet
```

| Service | Internal port | Public port | Purpose |
|---|---|---|---|
| Nginx | 80 | 80 / 443 | Pool website + API proxy |
| Master (slave) | 5115 | 5115 | Slave node connections |
| Benchmarker UI | 7777 | 8081 | Operator-only master admin |
| Pool Manager | 8080 | — | Internal only (proxied via nginx) |
| Auditor | — | — | No ports; talks to Postgres and the host Docker socket |
| PostgreSQL | 5432 | — | Internal only |

> Port 3336 (master internal API) and 5432 (postgres) must **never** be exposed publicly.

---

## Prerequisites

- Ubuntu 22.04+ (or any Linux host with Docker)
- Docker + Docker Compose V2 (`docker compose`, not `docker-compose`)
- A TIG benchmarker `player_id` and `api_key` from [tig.foundation](https://tig.foundation)
- (Optional) A domain name + SSL certificate for production

---

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/your-org/innopool.git
cd innopool
cp .env.example .env
```

Edit `.env` and set at minimum:

```env
POSTGRES_PASSWORD=<strong-random-password>
ADMIN_SECRET=<strong-random-secret>
POOL_NAME=MyPool
POOL_PUBLIC_URL=http://localhost   # or your domain
POOL_FEE=0.05                      # 5% operator fee
```

See [Master Configuration](#master-configuration) below for how to seed your TIG credentials and algo selection into the pool master.

### 2. Build and start

```bash
docker compose up -d --build
```

### 3. Set your TIG credentials

Open the benchmarker UI at `http://localhost:8081/benchmarker/`, go to **Config**, and set your `player_id`, `api_key`, `api_url`, and `algo_selection`. Click **Update Config**.

### 4. Register yourself as the first member

```bash
python3 admin.py add 0xYourWalletAddress cpu
```

This prints a `slave_name`, fleet token, and a ready-to-paste slave `.env` block.

Or generate invite codes for others to self-register:

```bash
python3 admin.py invite 5
```

### 5. Connect a slave

The pool website serves a one-liner install script at `/static/fleet-install.sh`. Members run:

```bash
curl -s http://your-pool-domain/static/fleet-install.sh | bash
```

Or follow the manual steps on the pool website registration page.

### 6. Verify

```bash
python3 admin.py autopilot
```

---

## Production Deployment

### Recommended hosting

- **VPS**: Hetzner CX22 (~€4/month) is sufficient for the master + pool manager alone
- Larger fleets (50+ slaves) benefit from a CX32 or CX42
- **Domain**: Any registrar; Cloudflare proxy or Certbot for SSL

### Firewall

```bash
ufw allow 22    # SSH
ufw allow 80    # Pool website + API
ufw allow 443   # HTTPS (if using SSL)
ufw allow 5115  # Slave node connections
ufw enable
```

### Deploy

```bash
git clone https://github.com/your-org/innopool.git
cd innopool
cp .env.example .env
# edit .env with production values
docker compose up -d --build
```

### SSL with Certbot

```bash
apt install certbot python3-certbot-nginx
certbot --nginx -d yourpool.example.com
```

Then update `POOL_PUBLIC_URL` in `.env` and restart nginx:

```bash
docker compose restart nginx
```

---

## Autopilot

The autopilot runs every `AUTOPILOT_INTERVAL_S` seconds (default 300) and automatically adjusts:

- **Benchmark slots** — scales CPU and GPU slot counts based on connected worker capacity
- **Max concurrent benchmarks** — tunes the master's workload cap to match proof throughput
- **Stale work cleanup** — reclaims abandoned roots and proofs

Enable it by setting `AUTOPILOT_MODE=on` in `.env`, then restart `pool_manager`:

```bash
docker compose up -d pool_manager
```

Key tuning parameters (all set in `.env`):

| Variable | Default | Description |
|---|---|---|
| `AUTOPILOT_MODE` | `off` | `on` to enable, `off` to disable |
| `AUTOPILOT_INTERVAL_S` | `300` | Seconds between autopilot runs |
| `AUTOPILOT_MAX_CPU_SLOTS` | `128` | Hard cap on CPU benchmark slots |
| `AUTOPILOT_MAX_GPU_SLOTS_PER_TYPE` | `6` | Hard cap on GPU slots per challenge type |
| `AUTOPILOT_FUNNEL_MIN_PROOF_CONVERSION_RATE` | `0.85` | Minimum proof conversion before scaling is blocked |

---

## AI Co-pilot

An optional DeepSeek-backed advisor that analyses pool state and surfaces recommendations for operator review. It is **read-only** — it never applies changes automatically.

Enable by setting in `.env`:

```env
DEEPSEEK_API_KEY=sk-...
AI_OPTIMIZER_ENABLED=true
AI_OPTIMIZER_MODE=on
AI_OPTIMIZER_INTERVAL_S=1800
```

View the latest recommendation:

```bash
python3 admin.py ai-optimizer
```

View decision history:

```bash
python3 admin.py ai-decisions
```

Recommendations are categorised as `observe_only`, `investigate`, or `act`. All proposed config changes are listed under `blocked_actions` until the operator manually applies them.

---

## Worker Trust System

New community miners start on **probation**. The autopilot only counts their capacity once they have demonstrated consistent proof conversion and passed preflight checks.

Trust states:

| State | Meaning |
|---|---|
| `probation` | New miner, limited capacity contribution; every audit request is verified |
| `trusted` | Verified miner, full capacity counted; audits are sampled (`AUDIT_TRUSTED_SAMPLE_RATE`) |
| `operator` | Pool operator's own machines; never auto-quarantined |
| `quarantined` | Failed a quality audit — deactivated, unfinished batches released |
| `suspended` | Removed from capacity calculations |

View trust state for all members:

```bash
python3 admin.py members
```

Trust state is managed via the pool database or admin API. The `members` command shows `trust_state` and `preflight_status` columns for every registered slave.

---

## Quality Audit

A root submit commits to the *solutions* (merkle root) but not to the *quality numbers* the slave posts alongside them. A hostile member could post real solutions with inflated qualities; TIG only re-checks a few nonces per benchmark, and a disagreement is clawed back from the pool. The audit closes that gap without re-solving anything:

1. Slave POSTs `/submit-batch-root` as normal.
2. Master picks the audit nonces **after** seeing the quality list — the highest-quality nonce plus `leaves_per_batch` random ones — and returns them in the ack as `audit_nonces`. The slave cannot know the sample before committing.
3. Slave (≥ 0.1.22) POSTs the original `{nonce}.json` leaves to `/submit-batch-audit/{batch_id}` and keeps its own copy for 30 days.
4. The `auditor` container runs **`tig-verifier` only** on each leaf inside the matching challenge container and compares the result to the posted quality.

Outcomes per batch: `passed`, `failed`, `skipped` (not sampled / GPU challenge), `missing` (slave never delivered), `error` (verifier infrastructure problem, retried). A `failed` audit deactivates the member, sets `trust_state = quarantined` and releases its unfinished work. Failed audits and their leaves are kept **forever** as evidence; passed ones are pruned after `AUDIT_RETENTION_DAYS`.

```bash
python3 admin.py audit              # per-slave pass/fail/missing, backlog
python3 admin.py audit --failures   # expected vs verifier quality per nonce
python3 admin.py audit <id>         # one audit with its kept leaves
```

Tuning lives in `.env` (`AUDIT_*`, see `.env.example`) and, for the master side, under `"audit"` in the master config (`enabled`, `leaves_per_batch`, `include_max_quality`, `max_leaf_bytes`, `request_ttl_ms`). Leave `AUDIT_MISSING_QUARANTINE_THRESHOLD=0` until every member runs a slave ≥ 0.1.22 — older slaves never answer audit requests. GPU challenges are stored but not verified (the VPS has no GPU).

---

## Admin CLI

`admin.py` provides operator access to all pool management functions without needing to call the API directly.

```bash
python3 admin.py --help
```

Common commands:

```bash
python3 admin.py autopilot                            # Pool health + scale readiness report
python3 admin.py autopilot --json                     # Same, machine-readable
python3 admin.py hit-rate                             # Quality vs TIG qualifier floor, bundles, time
python3 admin.py hit-rate --json                      # Same, machine-readable
python3 admin.py audit [--json]                       # Quality spot-check: per-slave pass/fail, backlog
python3 admin.py audit --failures                     # Failed audits with expected vs verifier quality
python3 admin.py audit <id>                           # One audit incl. kept leaves (dispute evidence)

python3 admin.py members                              # List all registered members (with trust/preflight state)
python3 admin.py fleets                               # List registered fleets

python3 admin.py add <wallet> [cpu|gpu]               # Add a member directly (no invite needed)
python3 admin.py create-fleet <wallet> <label> [cpu|gpu|mixed] [--cpu N] [--gpu N]
python3 admin.py invite [N]                           # Generate N invite codes (default 1)
python3 admin.py invites                              # List all invite codes

python3 admin.py activate <wallet|slave>              # Re-activate a member or slave
python3 admin.py deactivate <wallet|slave>            # Deactivate a member or slave
python3 admin.py clear-slave <slave>                  # Unassign stale batches from a slave
python3 admin.py member-health <slave>                # Detailed assignment health for a slave

python3 admin.py ai-optimizer                         # Run AI co-pilot manually (read-only)
python3 admin.py ai-decisions [N]                     # Show last N AI recommendations (default 10)

python3 admin.py coinbase                             # Show last 10 coinbase distribution events
python3 admin.py compute-types [--apply]              # Validate/fix TIG compute_type on algo_selection
```

---

## Master Configuration

### New operators (no prior TIG setup)

Configure your `player_id`, `api_key`, and `algo_selection` via the benchmarker UI at `http://your-server:8081/benchmarker/`. This is the simplest path.

### Migrating from an existing tig-benchmarker setup

If you already run a standalone `tig-benchmarker` (tig-master) and want to transplant its full config into InnoPool, use `configure_innopool.py`:

```bash
# 1. Export your existing master config
docker compose -f ~/tig-master/master.yml exec db \
  psql -U postgres -d postgres -t \
  -c 'SELECT config FROM config LIMIT 1;' \
  | python3 -c 'import sys,json; print(json.dumps(json.loads(sys.stdin.read().strip()), indent=2))' \
  > ~/tig-master/saved_config.json

# 2. Create your API key file
echo 'YOUR_TIG_API_KEY' > ~/.tig_api_key && chmod 600 ~/.tig_api_key

# 3. Push config to InnoPool master (with pool-* slave routing)
python3 configure_innopool.py
```

This script reads `~/tig-master/saved_config.json`, rewrites slave routing to `pool-gpu-.*` / `pool-cpu-.*`, sets pool-appropriate batch sizes, validates TIG 0.0.7 `compute_type` values, and pushes the full config to InnoPool's master.

**Environment overrides:**

| Variable | Default | Description |
|---|---|---|
| `SLAVE_MODE` | `pool` | `pool` (pool-* routing) or `hybrid` (your own named slaves) |
| `CPU_BATCH_SIZE` | `64` | Nonces per CPU batch |
| `GPU_BATCH_SIZE` | `8` | Nonces per GPU batch |
| `CPU_COMPUTE_TYPE` | `aws_c7a` | Default compute_type for CPU algorithms |
| `GPU_COMPUTE_TYPE` | `aws_g4dn` | Default compute_type for GPU algorithms |
| `SAVED_CONFIG` | `~/tig-master/saved_config.json` | Path to exported config |

---

## API Reference

All endpoints are prefixed with `/api/`.

### Public

| Method | Endpoint | Description |
|---|---|---|
| GET | `/stats` | Pool overview (workers, benchmarks, slots) |
| GET | `/leaderboard` | Top contributors (24h) |
| GET | `/member/{wallet}` | Individual member stats |
| GET | `/health` | Pool health indicators |
| POST | `/register` | Register with invite code |
| POST | `/preflight` | Submit preflight check result |

### Admin (requires `X-Admin-Secret` header)

| Method | Endpoint | Description |
|---|---|---|
| POST | `/admin/invite` | Create invite codes |
| POST | `/admin/members` | Add member directly |
| GET | `/admin/members` | List all members |
| POST | `/admin/members/{id}/activate` | Re-activate member |
| POST | `/admin/members/{id}/deactivate` | Deactivate member |
| GET | `/admin/coinbase-history` | Reward distribution history |
| GET | `/admin/invites` | List invite codes |
| GET | `/admin/pool-settings` | View pool configuration |
| POST | `/admin/pool-settings` | Update pool configuration |
| GET | `/admin/ops/audit` | Quality-audit report: per-slave totals, backlog, recent failures |
| GET | `/admin/ops/audit/{id}` | One audit row with its kept leaves |

---

## Codebase Overview

| Path | Purpose |
|---|---|
| `docker-compose.yml` | Orchestrates all services |
| `postgres/init.sql` | Combined master + pool database schema |
| `nginx/nginx.conf` | Routes `/`, `/api/`, `/benchmarker/`, `/static/` |
| `pool_manager/main.py` | FastAPI server + background loop |
| `pool_manager/pool/routes.py` | All HTTP endpoints |
| `pool_manager/pool/tracker.py` | Contribution snapshots (every 60s) |
| `pool_manager/pool/coinbase.py` | Calls TIG `/set-coinbase` when due |
| `pool_manager/pool/autopilot.py` | Autopilot capacity and workload tuning |
| `pool_manager/pool/ai_optimizer.py` | AI co-pilot advisor |
| `pool_manager/pool/scheduler.py` | Lightweight dynamic tuner — adjusts `max_concurrent_benchmarks` based on active slave count (superseded by autopilot) |
| `master/slave_manager.py` | Slave connection and batch assignment |
| `master/batch_audit.py` | Audit nonce sampling, leaf validation, `batch_audit` schema |
| `master/precommit_manager.py` | Precommit selection and submission |
| `auditor/` | Auditor service: `tig-verifier`-only re-scoring, strikes, retention |
| `pool_manager/pool/audit_report.py` | Observe-only audit report for `/admin/ops/audit` |
| `pool_website/` | Static HTML/CSS/JS pool site |
| `admin.py` | Operator CLI |
| `configure_innopool.py` | Migrates an existing tig-master config into InnoPool with pool slave routing |
| `tools/security_audit.py` | Read-only static security audit |
| `tools/autopilot_sim/` | Autopilot scenario simulation and testing |
| `docs/` | Operator documentation and AI context |

---

## Notes

- The pool operator is the on-chain benchmarker. Members trust you to call `/set-coinbase` proportionally to their contributions. The code is open source — they can verify it does exactly that.
- `/set-coinbase` is rate-limited by TIG to once per `coinbase_update_period` blocks. The pool manager tracks this automatically.
- Fleet labels (the display name shown on the website) are cosmetic only. The pool identifies workers by their fleet token hash, which cannot be changed after registration.
- The benchmarker UI (port 8081) should **not** be exposed publicly. Nginx restricts it to requests with valid `OPERATOR_USER`/`OPERATOR_PASSWORD` HTTP basic auth.
