# Public CPU member capacity (large machines)

InnoPool keeps a **fleet-safe default of 1 concurrent batch** for public
`pool-cpu-*` members (Pica-class). Larger machines are utilised mainly by
**job fit** (hard roots prefer L/XL) and sticky absorption, not by assuming
core count means more concurrent batches.

## Why core count alone is not enough

Stock TIG slaves often set `NUM_WORKERS ≈ nproc`. A 96-thread box running one
batch already saturates the machine. Giving it `cap=2` recreates the Pica
overload failure mode.

## Optional telemetry (earn concurrent=2 on L/XL)

Send optional fields on `GET /get-batches` (query params or headers). Stock
slaves that omit them keep `cap=1`.

| Field | Meaning |
|---|---|
| `cores` | Logical CPUs |
| `num_workers` | Active `NUM_WORKERS` |
| `load_1m` | 1-minute load average |
| `free_ram_gb` | Available RAM |
| `ram_gb` | Total RAM (optional) |
| `state` | `idle` / `downloading` / `running` / `submitting` (custom slave) |
| `active_batches` / `pending_batches` | Local queue depths (custom slave) |
| `last_idle_ms` | Idle gap / current idle age (custom slave) |
| `slave_version` | e.g. `innopool-slave/0.1.0` (custom slave) |

Headers: `X-InnoPool-Cores`, `X-InnoPool-Num-Workers`, `X-InnoPool-Load-1m`,
`X-InnoPool-Free-Ram-Gb`, plus `X-InnoPool-State`, `X-InnoPool-Active-Batches`,
`X-InnoPool-Pending-Batches`, `X-InnoPool-Last-Idle-Ms`, `X-InnoPool-Slave-Version`.

Runtime (v1.5) fields drive load-shed gating: high `load_1m` hard-sheds while
`active_batches > 0` / working `state` (timer not reset every poll). Idle with
load still hot gets a short cool-off (`CPU_LOAD_SHED_IDLE_COOL_MS`); clear when
load drops. Low `free_ram_gb` still sheds when idle.

**Headroom rule:** `cores / num_workers >= 1.25` (~80% workers) and
`load_1m <= cores * 0.85`. Then L/XL may earn concurrent **2**. While a slave is
**working**, load spikes (`load_1m > cores * 1.25`) or `free_ram_gb < 4` force
concurrent **0** for a cooldown — including fleet/Pica boxes already at cap 1.

## Member recommendations

- Leave ~10–20% CPU headroom in `NUM_WORKERS` (e.g. 80 on a 96-thread box).
- Report telemetry if you want the master to consider concurrent > 1.
- Do not expect name tricks alone to raise caps; evidence is required.
