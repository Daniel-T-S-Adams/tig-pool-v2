# Enriched Slave Telemetry (Phase C)

Stock slaves must keep working when fields are omitted (graceful degrade to
concurrent CPU cap = fleet default, usually 1).

See also [docs/public_cpu_member_capacity.md](../docs/public_cpu_member_capacity.md).

## Goal

Give the master live capacity signals that static preflight + completion EMA
cannot provide: true worker count, instantaneous load, free RAM, GPU util.
Public L/XL CPUs may **earn** concurrent=2 only when telemetry shows
`cores/num_workers >= 2` and healthy load — never from core count alone.

## Protocol (backward compatible)

Extend `GET /get-batches` with optional query parameters (or headers). Missing
fields degrade to DB tier + EMA (current Phase A/B behavior).

Suggested query params:

| Param | Type | Meaning |
|---|---|---|
| `cores` | int | Logical CPUs currently visible |
| `ram_gb` | int | Total RAM GB |
| `num_workers` | int | Slave `NUM_WORKERS` / active workers |
| `load_1m` | float | 1-minute load average |
| `free_ram_gb` | float | Free/available RAM |
| `gpu_util` | float | 0–100 GPU utilization (GPU slaves) |
| `gpu_vram_free_mb` | int | Free VRAM |
| `state` | string | `idle` / `downloading` / `running` / `submitting` (v1.5) |
| `active_batches` | int ≥0 | Batches currently processing (v1.5) |
| `pending_batches` | int ≥0 | Batches queued locally (v1.5) |
| `last_idle_ms` | int ≥0 | Last finished→next-work idle gap, or current idle age (v1.5) |
| `slave_version` | string | e.g. `innopool-slave/0.1.0` (v1.5) |

Headers alternative (if query pollution is a concern):

- `X-InnoPool-Cores`
- `X-InnoPool-Num-Workers`
- `X-InnoPool-Load-1m`
- `X-InnoPool-Free-Ram-Gb`
- `X-InnoPool-Gpu-Util`
- `X-InnoPool-State`
- `X-InnoPool-Active-Batches`
- `X-InnoPool-Pending-Batches`
- `X-InnoPool-Last-Idle-Ms`
- `X-InnoPool-Slave-Version`

Identity remains `User-Agent: <slave_name>`.

v1.5 fields are stored on the master for observability / future scheduling. They do
not by themselves change concurrent caps; capacity gates still use cores/workers/load/RAM.

## Master behavior

Implemented in `master/cpu_tier_caps.py` + `slave_manager._adaptive_max_concurrent`:

1. Parse optional telemetry on each `/get-batches` poll; ignore invalid values.
2. Refresh `HardwareTier` with live `cores`/`ram_gb` when present (override stale preflight).
3. Soft load-shed: if `load_1m > cores * 1.25` or `free_ram_gb < 4`, force CPU
   concurrent cap to 1 for `CPU_LOAD_SHED_COOLDOWN_MS` (default 10m).
4. L/XL earnable concurrent ceiling (default 2) only when
   `CPU_CONCURRENT_REQUIRES_TELEMETRY=true` (default) **and** headroom evidence
   exists; S/M always stay at 1.
5. Never reject stock slaves that omit telemetry.

## Custom slave changes

Minimal patch to stock TIG slave poll loop:

```python
import os, psutil  # or /proc
params = {
    "cores": os.cpu_count() or 1,
    "num_workers": int(os.environ.get("NUM_WORKERS", "1")),
    "load_1m": os.getloadavg()[0],
    "ram_gb": int(psutil.virtual_memory().total / (1024**3)),
    "free_ram_gb": round(psutil.virtual_memory().available / (1024**3), 1),
}
# GET /get-batches?... 
```

GPU slaves additionally sample `nvidia-smi` once per poll (cached ≥5s).

## Rollout

1. Master accepts+logs telemetry (no behavior change).
2. Enable load-shed behind `CAPABILITY_LIVE_TELEMETRY=true`.
3. Deploy custom slave to controlled fleet first; measure SAT p95 + member load.
4. Broaden only if stock-slave members remain fair under graceful degrade.

## Non-goals

- Push assignment (master still pull-based)
- Breaking proof artifact locality
- Requiring all public members to upgrade before Phase A/B value is realized
