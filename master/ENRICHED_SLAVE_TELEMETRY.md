# Enriched Slave Telemetry (Phase C)

Ship only after Phase A/B metrics plateau. Stock slaves must keep working.

## Goal

Give the master live capacity signals that static preflight + completion EMA
cannot provide: true worker count, instantaneous load, free RAM, GPU util.

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

Headers alternative (if query pollution is a concern):

- `X-InnoPool-Cores`
- `X-InnoPool-Num-Workers`
- `X-InnoPool-Load-1m`
- `X-InnoPool-Free-Ram-Gb`
- `X-InnoPool-Gpu-Util`

Identity remains `User-Agent: <slave_name>`.

## Master behavior

1. Parse optional telemetry on each poll; ignore invalid values.
2. Refresh `HardwareTier` with live `cores`/`ram_gb` when present (override stale preflight).
3. Soft load-shed: if `load_1m > cores * 1.5` or `free_ram_gb < 4`, temporarily
   treat the slave as one tier lower and/or reduce adaptive cap by 1.
4. Prefer `num_workers` when computing runtime_cap in adaptive caps.
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
