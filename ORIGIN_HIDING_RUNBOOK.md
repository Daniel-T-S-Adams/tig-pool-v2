# InnoPool Origin Hiding Runbook

This runbook migrates InnoPool to a fresh VPS/IP behind Cloudflare Tunnel without
losing Postgres data. The new origin should not expose public web/master ports.

## 1. Cloudflare Tunnel

Create a named tunnel in Cloudflare:

```bash
cloudflared tunnel login
cloudflared tunnel create innopool-origin
```

Copy `cloudflared-config.example.yml` to `/etc/cloudflared/config.yml` on the new
VPS and update the `credentials-file` path to the JSON created by Cloudflare.

Create DNS routes:

```bash
cloudflared tunnel route dns innopool-origin innopool.co.uk
cloudflared tunnel route dns innopool-origin www.innopool.co.uk
cloudflared tunnel route dns innopool-origin master.innopool.co.uk
cloudflared tunnel route dns innopool-origin operator.innopool.co.uk
```

Protect `operator.innopool.co.uk` with Cloudflare Access.

## 2. New VPS Preparation

Install Docker, Compose, git, and cloudflared on the fresh VPS.

```bash
apt update
apt install -y ca-certificates curl gnupg git ufw
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
```

Clone the repo:

```bash
cd /root
git clone <YOUR_REPO_URL> tig-pool
cd /root/tig-pool
```

Copy the old VPS runtime files to the new VPS:

```bash
scp root@OLD_VPS_IP:/root/tig-pool/.env /root/tig-pool/.env
scp root@OLD_VPS_IP:/root/tig-pool/saved_config.json /root/tig-pool/saved_config.json
```

Merge these hidden-origin values into `/root/tig-pool/.env`:

```dotenv
BIND_ADDR=127.0.0.1
POOL_PUBLIC_URL=https://www.innopool.co.uk
PUBLIC_MASTER_HOST=master.innopool.co.uk
PUBLIC_MASTER_PORT=80
WEB_PORT=80
OPERATOR_PORT=8888
MASTER_PORT=5115
MASTER_TUNNEL_PORT=8081
```

Do not put the fresh VPS IP in miner-facing config.

## 3. Database Dry Run

On the old VPS:

```bash
cd /root/tig-pool
docker compose exec -T db pg_dump -U postgres -d innopool -Fc > /root/innopool.dryrun.dump
scp /root/innopool.dryrun.dump root@NEW_VPS_IP:/root/
```

On the new VPS:

```bash
cd /root/tig-pool
docker compose up -d db
docker compose exec -T db dropdb -U postgres --if-exists innopool
docker compose exec -T db createdb -U postgres innopool
docker compose exec -T db pg_restore -U postgres -d innopool < /root/innopool.dryrun.dump
docker compose up -d --build
```

Start cloudflared:

```bash
cloudflared service install
systemctl enable --now cloudflared
```

Verify:

```bash
curl -I https://www.innopool.co.uk
curl -i http://master.innopool.co.uk/get-batches
curl -I https://operator.innopool.co.uk
```

The master test may return an auth/member-related response depending on
`User-Agent`; it should not expose admin endpoints.

## 4. Final Cutover

Use a short downtime window.

On the old VPS:

```bash
cd /root/tig-pool
docker compose stop master pool_manager nginx benchmarker_ui
docker compose exec -T db pg_dump -U postgres -d innopool -Fc > /root/innopool.final.dump
scp /root/innopool.final.dump root@NEW_VPS_IP:/root/
```

On the new VPS:

```bash
cd /root/tig-pool
docker compose down
docker compose up -d db
docker compose exec -T db dropdb -U postgres --if-exists innopool
docker compose exec -T db createdb -U postgres innopool
docker compose exec -T db pg_restore -U postgres -d innopool < /root/innopool.final.dump
docker compose up -d --build
systemctl restart cloudflared
```

## 5. Firewall

After the tunnel works, lock down the new VPS:

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow from YOUR_HOME_IP to any port 22 proto tcp
ufw enable
ufw status verbose
```

Do not allow public inbound `80`, `443`, `5115`, `3336`, `7777`, `8088`, or
`8888`. Cloudflare Tunnel uses outbound connections.

## 6. Existing Miner Rotation

Existing miners need to replace only their master endpoint and restart:

```bash
cd ~/tig-monorepo/tig-benchmarker
perl -pi -e 's/^MASTER_IP=.*/MASTER_IP=master.innopool.co.uk/; s/^MASTER_PORT=.*/MASTER_PORT=80/' .env
docker compose -f slave.yml up -d --force-recreate slave
```

If they want to fully refresh all services:

```bash
docker compose -f slave.yml up -d --force-recreate
```

## 7. Verification

On the new VPS:

```bash
cd /root/tig-pool
docker compose ps
python3 admin.py members
docker compose logs --tail=100 master
```

From a miner machine:

```bash
cd ~/tig-monorepo/tig-benchmarker
docker compose -f slave.yml logs -f slave
```

Confirm new registrations generate:

```dotenv
MASTER_IP=master.innopool.co.uk
MASTER_PORT=80
```

## 8. Rollback

Keep the old VPS stopped, not deleted, until the new origin has been stable. To
roll back, stop the new stack, restart the old stack, and point the Cloudflare
tunnel/DNS routes back to the old host temporarily.
