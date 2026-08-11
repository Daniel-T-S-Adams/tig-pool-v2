# Idle fleet bug hunt (2026-08-11)

Overnight investigation after chronic `pool-cpu` idleness despite creates flowing.
Adaptive batch sizing was already kill-switched (`ADAPTIVE_CPU_BATCH_SIZE_ENABLED=false`).

## Smoking guns (code bugs)

### 1. FAST get-batches proof-priority regression (PRIMARY)

**File:** `master/slave_manager.py` `_get_batches_fast`

Slow path (correct) only zeros root intake when the slave has **runnable** proof work:

```python
has_proof_work = bool(assigned_proofs or own_proof_work)
```

FAST path (live default `GET_BATCHES_FAST=1`) wrongly also treated `views.awaiting_proofs` as proof work → `root_cap=0` with `SLAVE_PROOF_PRIORITY_MAX_ROOTS=0`.

**Effect:** preferred sticky owner owes proofs but cannot claim proof rows yet → takes **no new roots**. Other CPUs still skip sticky leftovers → warehouse + idle fleet while creates continue.

**Fix shipped:** align FAST `has_proof_work` with slow path.

### 2. Sticky unlock narrower than proof lock

FAST sticky overflow unlocked preferred only when they held an **in-memory assigned proof**, not when they were in `views.awaiting_proofs`.

**Effect:** same warehouse as (1) even after preferred is known proof-owed.

**Fix shipped:** unlock sticky when `preferred in views.awaiting_proofs` (FAST path).
This is intentional and must stay: without it, preferred is proof-locked while
the fleet still skips their leftovers. Do **not** remove this when aligning
`has_proof_work` with the slow path.

### 3. Adaptive cap `0 or route_cap` (load-shed defeated)

```python
max_concurrent = int(views.adaptive_caps.get(slave_name) or route_cap)
# 0 or 32 → 32
```

Aug 10 load-shed→concurrent=0 never applied on the hot path.

**Fix shipped:** honor explicit `0` via `if slave_name in adaptive_caps`.

### 4. Idle-CPU create selection inverted under GPU floor

**File:** `master/precommit_manager.py`

- `force_cpu_only` required `governor_reason.startswith("idle_cpu_override:")` (only when ready-rate soft-gate fires). Healthy ready-rate ⇒ GPU stayed in lottery while `idle_cpu=True`.
- Weight branch used `elif gpu_below_floor` → when **both** idle-CPU and GPU-below-floor, only GPU got the 3× boost.

**Fix shipped:**
- `force_cpu_only` when `idle_cpu_needs_work` and GPU floor met (drop ready-rate string gate).
- Boost CPU on idle independently; boost GPU on floor independently (both can apply).

## Not the main assign bug (but real throughput limits)

| Limit | Notes |
|---|---|
| Master loop ~1 create / 5s | Hard cadence; cannot burst-fill 38 CPUs |
| `PRECOMMIT_GOVERNOR_MAX_CPU_UNASSIGNED_ROOTS` (often 64/256) | Caps claimable inventory |
| Adaptive batch off + large `batch_size` | Low roots/job (~9 finishes per create in live stats) |
| Ops `idle` = online + inflight==0 | Point sample; now also exposes windowed `sustained_idle` / `idle_frac_window` |
| Create burst / idle-CPU bias | Uses **sustained** idle (default 120s window, ≥50% idle frac) so between-job gaps do not look like fleet starvation |

## Deploy (when awake)

```bash
# local
cd ~/tig-pool && git push origin main

# VPS
cd ~/tig-pool && git pull origin main
docker compose up -d --build --force-recreate master
```

Keep `ADAPTIVE_CPU_BATCH_SIZE_ENABLED=false` until a milder fanout design is ready.

## Verify after deploy

```bash
docker logs innopool_master --since 10m 2>&1 | grep -E 'Selecting algorithm|force_cpu|load-shed' | tail -30

set -a; source .env; set +a
curl -sS -H "X-Admin-Secret: $ADMIN_SECRET" http://127.0.0.1:${WEB_PORT:-80}/api/admin/ops/metrics \
  | python3 -c "import sys,json;d=json.load(sys.stdin);s=d.get('slaves') or {};print('fill',s.get('fill_rate'),'idle',s.get('idle'),'claimable',d.get('claimable_root_total'),'sticky',d.get('sticky_reserved_root_total'))"
```

Expect: with `idle_cpu=True` and GPU floor met, selection should be CPU-only more often; sticky should not warehouse while preferred awaits proofs; fill should stay higher for the same create rate.
