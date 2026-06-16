# InnoPool

A self-hosted mining pool for [The Innovation Game](https://tig.foundation), built on top of the official `tig-benchmarker` master/slave architecture.

## How It Works

```
Pool Members (slave nodes)  ──5115──►  TIG Master (Docker)
                                              │
                                     Pool Manager (Python)
                                       - tracks contributions
                                       - calls /set-coinbase
                                              │
                                     Pool Website (nginx)
                                       - registration
                                       - live stats
```

Slave nodes run on pool **members'** machines, pointing `MASTER_IP` at your pool server.  
Rewards are distributed on-chain using TIG's `/set-coinbase` API every ~50 blocks.

---

## Local Setup (Testing First)

### Prerequisites

- Docker + Docker Compose installed in WSL
- A TIG benchmarker `player_id` and `api_key` (get these from [tig.foundation](https://tig.foundation))

### 1. Configure

```bash
cd /home/kevin/tig-pool
cp .env.example .env
```

Edit `.env`:
- Set a strong `POSTGRES_PASSWORD`
- Set a strong `ADMIN_SECRET` (this protects your admin endpoints)
- Leave `POOL_FEE=0.05` (5%) or adjust as you like

### 2. Build and Start

```bash
docker-compose up --build
```

This starts:
| Service | URL | Purpose |
|---|---|---|
| Pool website | http://localhost:80 | Public-facing pool site |
| Benchmarker UI | http://localhost:80/benchmarker/ | Master admin (your eyes only) |
| Master (slave port) | localhost:5115 | Slave nodes connect here |
| Pool Manager API | http://localhost:80/api/ | REST API |

### 3. Set Your TIG Credentials

1. Open http://localhost:80/benchmarker/
2. Go to **Config**
3. Set your `player_id`, `api_key`, `api_url`, and `algo_selection`
4. Click **Update Config**

### 4. Add Your First Pool Member (Yourself)

Use the admin API to add yourself directly:

```bash
curl -s -X POST http://localhost:80/api/admin/members \
  -H "Content-Type: application/json" \
  -H "X-Admin-Secret: YOUR_ADMIN_SECRET" \
  -d '{"wallet_address": "0xYourWalletAddress"}'
```

This returns your `slave_name` and a ready-to-use `.env` config.

Or create an invite code for others to self-register:

```bash
curl -s -X POST http://localhost:80/api/admin/invite \
  -H "Content-Type: application/json" \
  -H "X-Admin-Secret: YOUR_ADMIN_SECRET" \
  -d '{"count": 5}'
```

### 5. Connect a Slave

In a **separate** directory, clone tig-monorepo and configure it:

```bash
git clone https://github.com/tig-foundation/tig-monorepo.git
cd tig-monorepo/tig-benchmarker
```

Create/edit `.env`:
```
SLAVE_NAME=pool-<your_short_wallet>   # from registration response
MASTER_IP=172.17.0.1                  # use this when master and slave are on same machine
MASTER_PORT=5115
NUM_WORKERS=8
ALGORITHMS_DIR=./algorithms
RESULTS_DIR=./results
TTL=300
```

Start the slave:
```bash
docker-compose -f slave.yml up slave satisfiability vehicle_routing knapsack
```

### 6. Check the Pool Website

Open http://localhost:80 — you should see your slave appear in the stats once it starts completing batches.

---

## Production Deployment

### Hosting Recommendation

- **VPS**: Hetzner Cloud CX22 (~€4/month) — enough for the master + pool manager
- **Domain**: Any registrar (Namecheap, Cloudflare Registrar)
- **SSL**: Add Certbot or use Cloudflare's proxy

### Steps

1. Provision a Ubuntu 22.04 VPS
2. Install Docker: `curl -fsSL https://get.docker.com | sh`
3. `git clone` or `scp` this project to the VPS
4. In `.env`, set `MASTER_PORT=5115` and open that port in your firewall
5. `docker-compose up -d --build`
6. Point a domain at your VPS IP, add SSL via Certbot

### Firewall Rules

```bash
# SSH
ufw allow 22

# Pool website + API
ufw allow 80
ufw allow 443

# Slave node connections (pool members need this)
ufw allow 5115

ufw enable
```

> **Security note**: Port 3336 (benchmarker master internal API) and 5432 (postgres)
> should **never** be exposed publicly. They are internal-only in this Docker setup.

---

## API Reference

All endpoints are prefixed with `/api/`.

### Public

| Method | Endpoint | Description |
|---|---|---|
| GET | `/stats` | Pool overview stats |
| GET | `/leaderboard` | Top 20 contributors (24h) |
| GET | `/member/{wallet}` | Individual member stats |
| POST | `/register` | Register with invite code |

### Admin (requires `X-Admin-Secret` header)

| Method | Endpoint | Description |
|---|---|---|
| POST | `/admin/invite` | Create invite codes |
| POST | `/admin/members` | Add member directly |
| GET | `/admin/members` | List all members |
| DELETE | `/admin/members/{wallet}` | Deactivate member |
| GET | `/admin/coinbase-history` | Distribution history |
| GET | `/admin/invites` | List invite codes |

---

## Architecture

| File | Purpose |
|---|---|
| `docker-compose.yml` | Orchestrates all services |
| `postgres/init.sql` | Combined master + pool schema |
| `nginx/nginx.conf` | Routes `/`, `/api/`, `/benchmarker/` |
| `pool_manager/main.py` | FastAPI server + background loop |
| `pool_manager/pool/tracker.py` | Contribution snapshots every 60s |
| `pool_manager/pool/coinbase.py` | Calls TIG `/set-coinbase` when due |
| `pool_manager/pool/routes.py` | All HTTP endpoints |
| `pool_website/` | Static HTML pool site |

---

## Notes

- The **pool operator** is the on-chain benchmarker. Pool members trust you to call `/set-coinbase` fairly. Your code is open source — they can verify it does exactly that.
- `/set-coinbase` can only be updated once per `coinbase_update_period` blocks (tracked in `pool_settings`). Check the TIG docs for the current value.
- Slave names must match the master config regex `pool-.*`. The default config in `init.sql` sets this up automatically.
- The pool manager reads your `api_key` from the master's `config` table, so you only set it once (via the benchmarker UI).
