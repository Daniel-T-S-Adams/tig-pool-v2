#!/usr/bin/env python3
"""Configure InnoPool's master by transplanting the live tig-master config.

Loads ~/tig-master/saved_config.json (your exact production algo_selection,
track_settings, track_allowlist, fuel_budgets, weights, etc.) and pushes it
to InnoPool's master — with only the slave routing changed to pool-.*

Connects directly to the master container's ClientManager API on port 3336
(bypasses nginx auth — port is mapped to localhost in docker-compose.yml).

Usage (simplest — uses everything from saved_config.json as-is):

    python3 configure_innopool.py

Override the slave mode if testing with your own named slaves:

    SLAVE_MODE=hybrid python3 configure_innopool.py
      → routes c3-slave-.* to GPU, aws-cpu-slave-.* to CPU

    SLAVE_MODE=pool python3 configure_innopool.py   (default)
      → routes pool-.* to all algorithms (pool members + renamed test slaves)

Override player_id if needed (default: reuses what's in saved_config.json):

    PLAYER_ID=0xYOURWALLET python3 configure_innopool.py

API key is always read from ~/.tig_api_key (same convention as tig-master).
"""
import json
import os
import re
import sys
import urllib.request

MASTER = os.environ.get("MASTER_CONFIG_URL", "http://localhost:3336")

SAVED_CONFIG_PATH = os.path.expanduser(
    os.environ.get("SAVED_CONFIG", "~/tig-master/saved_config.json")
)


def _load_base_config() -> dict:
    if not os.path.exists(SAVED_CONFIG_PATH):
        sys.exit(
            f"Saved config not found at {SAVED_CONFIG_PATH}\n"
            "Run from tig-master to regenerate it:\n"
            "  docker compose -f master.yml up -d db\n"
            "  docker compose -f master.yml exec db psql -U postgres -d postgres "
            "-t -c 'SELECT config FROM config LIMIT 1;' \\\n"
            "    | python3 -c 'import sys,json; print(json.dumps(json.loads"
            "(sys.stdin.read().strip()), indent=2))' > saved_config.json\n"
            "  docker compose -f master.yml down"
        )
    with open(SAVED_CONFIG_PATH) as f:
        cfg = json.load(f)
    print(f"  base config loaded from: {SAVED_CONFIG_PATH}")
    print(f"  {len(cfg.get('algo_selection', []))} algorithms, "
          f"{len(cfg.get('track_allowlist', {}))} challenge allowlists")
    return cfg


def _prefix_regex(algo_ids: list) -> str:
    prefixes = sorted({aid.split("_", 1)[0] for aid in algo_ids if "_" in aid})
    if not prefixes:
        return r"^$a"
    return r"^(" + "|".join(re.escape(p) for p in prefixes) + r")_"


def main():
    # ── credentials ───────────────────────────────────────────────────────────
    key_path = os.path.expanduser("~/.tig_api_key")
    if not os.path.exists(key_path):
        sys.exit(
            f"Missing {key_path}. Create it with:\n"
            "  echo 'YOUR_TIG_API_KEY' > ~/.tig_api_key && chmod 600 ~/.tig_api_key"
        )
    api_key = open(key_path).read().strip()

    # ── base config (all algo_selection, track_settings, allowlists, etc.) ────
    cfg = _load_base_config()

    # ── credentials — keep player_id from saved config unless overridden ──────
    player_id = os.environ.get("PLAYER_ID", "").strip() or cfg.get("player_id", "")
    if not player_id or player_id.startswith("0x000"):
        sys.exit("No player_id found. Set PLAYER_ID=0x... or check saved_config.json")
    cfg["player_id"] = player_id.lower()
    cfg["api_key"] = api_key
    cfg["api_url"] = "https://mainnet-api.tig.foundation"

    # ── fetch current master config (to confirm it's reachable) ───────────────
    print(f"\nFetching current InnoPool master config from {MASTER}/get-config ...")
    try:
        urllib.request.urlopen(f"{MASTER}/get-config")
    except Exception as e:
        sys.exit(
            f"Cannot reach InnoPool master at {MASTER}: {e}\n"
            "Make sure InnoPool is running: cd ~/tig-pool && docker compose up -d"
        )

    # ── slave routing ─────────────────────────────────────────────────────────
    slave_mode = os.environ.get("SLAVE_MODE", "pool").lower()

    # Determine which algo IDs are GPU vs CPU based on challenge prefix
    # c004=vector_search, c005=hypergraph, c006=neuralnet_optimizer → GPU
    # c001=satisfiability, c002=vehicle_routing, c003=knapsack,
    # c007=job_scheduling, c008=energy_arbitrage → CPU
    GPU_CHALLENGES = {"c004", "c005", "c006"}
    gpu_ids = [s["algorithm_id"] for s in cfg["algo_selection"]
               if s["algorithm_id"].split("_")[0] in GPU_CHALLENGES]
    cpu_ids = [s["algorithm_id"] for s in cfg["algo_selection"]
               if s["algorithm_id"].split("_")[0] not in GPU_CHALLENGES]

    # ── max_concurrent_batches — read from saved_config slaves if present ────────
    saved_slaves = {s["name_regex"]: s for s in cfg.get("slaves", [])}
    _gpu_slave = next((s for s in saved_slaves.values() if "gpu" in s["name_regex"]), {})
    _cpu_slave = next((s for s in saved_slaves.values() if "cpu" in s["name_regex"]), {})
    GPU_MAX_CONCURRENT_BATCHES = int(os.environ.get(
        "GPU_MAX_CONCURRENT_BATCHES",
        str(_gpu_slave.get("max_concurrent_batches", 24))
    ))
    CPU_MAX_CONCURRENT_BATCHES = int(os.environ.get(
        "CPU_MAX_CONCURRENT_BATCHES",
        str(_cpu_slave.get("max_concurrent_batches", 48))
    ))

    # ── batch_size overrides ───────────────────────────────────────────────────
    # Larger batch_size = fewer batches per benchmark = less backlog buildup.
    # CPU: 64 nonces/batch (7 batches for a 400-nonce job vs 50 at batch_size=8)
    # GPU: keep 8 — each GPU nonce takes minutes, so batches stay manageable.
    CPU_BATCH_SIZE = int(os.environ.get("CPU_BATCH_SIZE", "64"))
    GPU_BATCH_SIZE = int(os.environ.get("GPU_BATCH_SIZE", "8"))
    gpu_id_set = set(gpu_ids)
    for s in cfg["algo_selection"]:
        if s["algorithm_id"] in gpu_id_set:
            s["batch_size"] = GPU_BATCH_SIZE
        else:
            s["batch_size"] = CPU_BATCH_SIZE

    if slave_mode == "hybrid":
        # Your own C3/AWS slaves using their original names
        slaves = []
        if gpu_ids:
            slaves.append({
                "name_regex": "^c3-slave-.*$",
                "algorithm_id_regex": _prefix_regex(gpu_ids),
                "max_concurrent_batches": GPU_MAX_CONCURRENT_BATCHES,
            })
        if cpu_ids:
            slaves.append({
                "name_regex": "^aws-cpu-slave-.*$",
                "algorithm_id_regex": _prefix_regex(cpu_ids),
                "max_concurrent_batches": CPU_MAX_CONCURRENT_BATCHES,
            })
    else:
        # Default pool mode — split GPU/CPU by slave name prefix:
        #   pool-gpu-*  → GPU challenges (c004, c005, c006)
        #   pool-cpu-*  → CPU challenges (c001, c002, c003, c007, c008)
        slaves = []
        if gpu_ids:
            slaves.append({
                "name_regex": "^pool-gpu-.*$",
                "algorithm_id_regex": _prefix_regex(gpu_ids),
                "max_concurrent_batches": GPU_MAX_CONCURRENT_BATCHES,
            })
        if cpu_ids:
            slaves.append({
                "name_regex": "^pool-cpu-.*$",
                "algorithm_id_regex": _prefix_regex(cpu_ids),
                "max_concurrent_batches": CPU_MAX_CONCURRENT_BATCHES,
            })

    cfg["slaves"] = slaves

    # ── push to InnoPool master ───────────────────────────────────────────────
    data = json.dumps(cfg).encode()
    req = urllib.request.Request(
        f"{MASTER}/update-config", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    resp = urllib.request.urlopen(req)
    print(f"update-config: {resp.status} {resp.read().decode()}")

    masked = api_key[:4] + "..." + api_key[-2:] if len(api_key) > 6 else "***"
    print(f"\nInnoPool master configured:")
    print(f"  player_id  = {cfg['player_id']}")
    print(f"  api_key    = {masked}")
    print(f"  slave_mode = {slave_mode}")
    print(f"  max_concurrent_benchmarks = {cfg.get('max_concurrent_benchmarks')}")
    for s in slaves:
        print(f"  route '{s['name_regex']}' -> {s['algorithm_id_regex']} "
              f"(max_concurrent_batches={s['max_concurrent_batches']})")
    algos = [(s["algorithm_id"], s["weight"]) for s in cfg["algo_selection"]]
    print(f"  algorithms ({len(algos)}): " +
          ", ".join(f"{aid}(w={w})" for aid, w in algos))
    if cfg.get("track_allowlist"):
        print(f"  track_allowlist: {list(cfg['track_allowlist'].keys())}")
    if cfg.get("per_challenge_max_benchmarks"):
        print(f"  per_challenge_max: {cfg['per_challenge_max_benchmarks']}")


if __name__ == "__main__":
    main()
