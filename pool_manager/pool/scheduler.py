"""
Adaptive Scheduler
==================
Adjusts master max_concurrent_benchmarks and per_challenge_max_benchmarks
dynamically based on how many slaves are currently active.

Why this matters:
  The TIG master creates benchmarks up to max_concurrent_benchmarks regardless
  of whether any slaves are polling. When slaves go offline, benchmarks pile up
  with 0 in-progress batches, wasting precommit slots and creating backlog.

  This scheduler watches the root_batch table to detect live slaves, calculates
  how many benchmarks they can realistically consume, and adjusts the master
  config every 60 seconds.

Active slave detection:
  A slave is "active" if it dispatched a batch within the last ACTIVE_WINDOW_MS.
  GPU slaves: pool-gpu-* prefix
  CPU slaves: pool-cpu-* prefix

Capacity formula:
  GPU:  n_gpu  × (GPU_MAX_CONCURRENT_BATCHES  // GPU_MIN_BUNDLES)
  CPU:  n_cpu  × (CPU_MAX_CONCURRENT_BATCHES  // CPU_MIN_BUNDLES)
  total = gpu_capacity + cpu_capacity, clamped to [MIN, MAX]
"""

import json
import logging
import os
import time
import urllib.request

from pool import database as db

logger = logging.getLogger("pool.scheduler")

MASTER_URL = os.environ.get("MASTER_INTERNAL_URL", "http://master:3336")

# Slave is considered active if it dispatched a batch in this window
ACTIVE_WINDOW_MS = int(os.environ.get("SCHEDULER_ACTIVE_WINDOW_MS", str(5 * 60 * 1000)))

# Must match the routing rules in saved_config.json / configure_innopool.py
GPU_MAX_CONCURRENT_BATCHES = int(os.environ.get("GPU_MAX_CONCURRENT_BATCHES", "12"))
CPU_MAX_CONCURRENT_BATCHES = int(os.environ.get("CPU_MAX_CONCURRENT_BATCHES", "48"))

# Minimum bundles per benchmark for each type (drives batches-per-benchmark)
# GPU: hypergraph has 4 bundles (our smallest/slowest GPU challenge)
# CPU: most CPU challenges use 4 bundles minimum
GPU_MIN_BUNDLES = 4
CPU_MIN_BUNDLES = 4

# Hard bounds
MIN_BENCHMARKS = 3   # always keep some running so master doesn't idle
MAX_BENCHMARKS = 50  # safety cap

_last_run_ts = 0.0
RUN_INTERVAL_S = 60


def _active_slaves() -> dict[str, str]:
    """Return {slave_name: 'gpu'|'cpu'} for slaves active in last ACTIVE_WINDOW_MS."""
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - ACTIVE_WINDOW_MS
    rows = db.fetch_all(
        """
        SELECT DISTINCT slave
        FROM root_batch
        WHERE start_time > %s
          AND slave IS NOT NULL
        """,
        (cutoff_ms,),
    )
    result = {}
    for row in rows:
        name = row["slave"] or ""
        if name.startswith("pool-gpu-"):
            result[name] = "gpu"
        elif name.startswith("pool-cpu-"):
            result[name] = "cpu"
    return result


def _fetch_config() -> dict:
    resp = urllib.request.urlopen(f"{MASTER_URL}/get-config", timeout=5)
    return json.loads(resp.read())


def _push_config(cfg: dict):
    data = json.dumps(cfg).encode()
    req = urllib.request.Request(
        f"{MASTER_URL}/update-config",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=5)


def maybe_update_schedule():
    """Called from background loop. Throttled to once every RUN_INTERVAL_S seconds."""
    global _last_run_ts
    now = time.time()
    if now - _last_run_ts < RUN_INTERVAL_S:
        return
    _last_run_ts = now

    try:
        _run()
    except Exception as e:
        logger.error(f"Scheduler error: {e}")


def _run():
    active = _active_slaves()
    n_gpu = sum(1 for t in active.values() if t == "gpu")
    n_cpu = sum(1 for t in active.values() if t == "cpu")

    # Benchmark capacity per slave type
    gpu_cap = n_gpu * (GPU_MAX_CONCURRENT_BATCHES // GPU_MIN_BUNDLES)
    cpu_cap = n_cpu * (CPU_MAX_CONCURRENT_BATCHES // CPU_MIN_BUNDLES)
    new_max = max(MIN_BENCHMARKS, min(gpu_cap + cpu_cap, MAX_BENCHMARKS))

    # GPU per-challenge limits scale with active GPU slave count
    #   hypergraph (c005): 3 benchmarks/slave × 4 bundles = 12 batches → fills 12 C3 workers
    #   vector_search (c004): 1 benchmark/slave (10-40 bundles, plenty of batches)
    #   neuralnet (c006): 2 benchmarks/slave × 8 bundles = 16 batches
    new_per = {
        "c004": max(1, n_gpu),
        "c005": max(1, n_gpu * 3),
        "c006": max(1, n_gpu * 2),
    }

    try:
        cfg = _fetch_config()
    except Exception as e:
        logger.warning(f"Scheduler: cannot reach master: {e}")
        return

    old_max = cfg.get("max_concurrent_benchmarks")
    old_per = cfg.get("per_challenge_max_benchmarks", {})

    if old_max == new_max and old_per == new_per:
        return  # nothing to change

    cfg["max_concurrent_benchmarks"] = new_max
    cfg["per_challenge_max_benchmarks"] = new_per

    _push_config(cfg)
    logger.info(
        f"Scheduler updated: {n_gpu} GPU + {n_cpu} CPU active → "
        f"max_concurrent_benchmarks={new_max} (was {old_max}), "
        f"per_challenge={new_per}"
    )
