#!/usr/bin/env python3
"""Convert an exported live autopilot report into a reward-simulator world.

Input is the JSON produced by:

    python3 admin.py autopilot --json

The output is a local synthetic world for reward_simulator.py. This tool does
not connect to the live pool, Postgres, the master, Docker, or TIG APIs.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


CPU_CHALLENGES = {"satisfiability", "vehicle_routing", "knapsack", "job_scheduling", "energy_arbitrage"}
GPU_CHALLENGES = {"vector_search", "hypergraph", "neuralnet_optimizer"}


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return data


def _as_float(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _challenge_profile(challenge_id: str) -> str:
    challenge = str(challenge_id or "").lower()
    if challenge in GPU_CHALLENGES or challenge.startswith(("c004", "c005", "c006")):
        return "gpu"
    if challenge in CPU_CHALLENGES:
        return "cpu"
    return "gpu" if any(name in challenge for name in GPU_CHALLENGES) else "cpu"


def _slave_profile(slave: dict) -> str:
    profile = slave.get("profile")
    if profile in {"cpu", "gpu"}:
        return profile
    name = str(slave.get("slave_name") or "")
    return "gpu" if name.startswith(("pool-gpu-", "c3-slave-")) else "cpu"


def _fleet_units(report: dict, profile: str) -> float:
    units = 0.0
    for slave in report.get("slaves") or []:
        if _slave_profile(slave) != profile:
            continue
        if not (slave.get("active_now") or slave.get("active")):
            continue
        unfinished = _as_float(slave.get("active_unfinished"), 0.0)
        completed = _as_float(slave.get("completed_recent"), 0.0)
        root_capacity = _as_float(slave.get("root_capacity"), 0.0)
        units += max(1.0, unfinished, root_capacity, completed / 20.0)
    return units


def _config_from_report(report: dict) -> dict:
    return (
        report.get("config")
        or report.get("master_config")
        or report.get("current_config")
        or {}
    )


def _track_lookup(rows: list[dict]) -> dict[tuple[str, str], dict]:
    out = {}
    for row in rows or []:
        key = (str(row.get("algorithm_id") or ""), str(row.get("track") or "default"))
        out[key] = row
    return out


def _configured_tracks(cfg: dict) -> list[dict]:
    tracks = []
    for algo in cfg.get("algo_selection") or []:
        algorithm_id = str(algo.get("algorithm_id") or "")
        challenge_id = str(algo.get("challenge_id") or algorithm_id.split("_")[0] or "")
        weight = _as_int(algo.get("weight"), 1)
        algo_batch = _as_int(algo.get("batch_size"), 32)
        track_settings = algo.get("track_settings") or {}
        if not track_settings:
            tracks.append({
                "algorithm_id": algorithm_id,
                "challenge_id": challenge_id,
                "track": "default",
                "weight": weight,
                "batch_size": algo_batch,
                "num_bundles": 8,
            })
            continue
        for track, settings in track_settings.items():
            settings = settings or {}
            tracks.append({
                "algorithm_id": algorithm_id,
                "challenge_id": challenge_id,
                "track": str(track),
                "weight": weight,
                "batch_size": _as_int(settings.get("batch_size"), algo_batch),
                "num_bundles": _as_int(settings.get("num_bundles"), 8),
            })
    return tracks


def _observed_tracks(report: dict) -> list[dict]:
    cfg_tracks = _configured_tracks(_config_from_report(report))
    if cfg_tracks:
        return cfg_tracks

    out = []
    seen = set()
    sources = [
        report.get("track_economics") or [],
        report.get("track_workload") or [],
        (report.get("reward_funnel") or {}).get("by_track") or [],
    ]
    for rows in sources:
        for source in rows:
            if not isinstance(source, dict):
                continue
            algorithm_id = str(source.get("algorithm_id") or "")
            track = str(source.get("track") or "default")
            key = (algorithm_id, track)
            if key in seen:
                continue
            seen.add(key)
            challenge_id = str(source.get("challenge_id") or algorithm_id.split("_")[0] or source.get("challenge") or "")
            configured = source.get("configured") or {}
            out.append({
                "algorithm_id": algorithm_id,
                "challenge_id": challenge_id,
                "track": track,
                "weight": _as_int(configured.get("weight"), 1),
                "batch_size": _as_int(configured.get("effective_batch_size"), 32),
                "num_bundles": _as_int(configured.get("num_bundles"), 8),
            })
    return out


def _track_world_id(row: dict) -> str:
    algorithm = str(row.get("algorithm_id") or row.get("challenge_id") or "track")
    track = str(row.get("track") or "default")
    safe = "".join(ch if ch.isalnum() else "_" for ch in f"{algorithm}_{track}")
    return safe.strip("_") or "track"


def _estimate_solution_rate(profile: str, reward_value: float) -> float:
    base = 0.00000125 if profile == "gpu" else 0.000001
    return base * max(0.25, min(3.0, reward_value))


def _build_tracks(report: dict, reward_scale: float) -> list[dict]:
    workload = _track_lookup(report.get("track_workload") or [])
    economics = _track_lookup(report.get("track_economics") or [])
    funnel = _track_lookup((report.get("reward_funnel") or {}).get("by_track") or [])
    tracks = []

    for row in _observed_tracks(report):
        algorithm_id = str(row.get("algorithm_id") or "")
        track_name = str(row.get("track") or "default")
        key = (algorithm_id, track_name)
        econ = economics.get(key, {})
        observed = workload.get(key) or econ.get("observed") or {}
        configured = econ.get("configured") or {}
        derived = econ.get("derived") or {}
        funnel_row = funnel.get(key, {})
        challenge_id = str(row.get("challenge_id") or econ.get("challenge_id") or algorithm_id.split("_")[0])
        profile = _challenge_profile(challenge_id)

        bundles = _as_int(row.get("num_bundles") or configured.get("num_bundles"), 8)
        batch_size = _as_int(row.get("batch_size") or configured.get("effective_batch_size"), 32)
        avg_nonces = _as_float(observed.get("avg_num_nonces"), 0.0)
        nonces_per_bundle = _as_float(derived.get("estimated_nonces_per_bundle"), 0.0)
        if nonces_per_bundle <= 0 and bundles > 0 and avg_nonces > 0:
            nonces_per_bundle = avg_nonces / bundles
        if nonces_per_bundle <= 0:
            nonces_per_bundle = 1024.0 if profile == "gpu" else 512.0

        root_runtime = _as_float(
            funnel_row.get("p95_root_batch_runtime_sec"),
            _as_float(observed.get("avg_root_runtime_sec"), 120.0 if profile == "gpu" else 180.0),
        )
        runtime_per_bundle = max(10.0, root_runtime * max(1.0, math.ceil(nonces_per_bundle / max(1, batch_size))) / max(1, bundles))
        proof_conversion = _as_float(funnel_row.get("proof_conversion_rate"), 0.92)
        stopped_rate = _as_float(funnel_row.get("stopped_rate"), 0.04)
        proof_time = _as_float(funnel_row.get("avg_time_to_proof_submit_sec"), 900.0)
        reward_value = reward_scale * (1.25 if profile == "gpu" else 1.0)

        tracks.append({
            "id": _track_world_id(row),
            "source_algorithm_id": algorithm_id,
            "source_track": track_name,
            "profile": profile,
            "weight": max(1, _as_int(row.get("weight") or configured.get("weight"), 1)),
            "base_bundles": max(1, bundles),
            "min_bundles": 4,
            "max_bundles": max(8, bundles + 16),
            "base_batch_size": max(1, batch_size),
            "nonces_per_bundle": round(nonces_per_bundle, 2),
            "runtime_sec_per_bundle": round(runtime_per_bundle, 2),
            "solution_rate_per_nonce": _estimate_solution_rate(profile, reward_value),
            "reward_value": round(reward_value, 4),
            "proof_conversion_rate": max(0.0, min(1.0, proof_conversion)),
            "stopped_rate": max(0.0, min(1.0, stopped_rate)),
            "proof_time_sec": round(max(1.0, proof_time), 2),
            "freshness_target_sec": 1800,
            "congestion_tail_factor": 0.4 if profile == "gpu" else 0.5,
        })
    return tracks


def _build_world(report: dict, args: argparse.Namespace) -> dict:
    cpu_units = max(1.0, _fleet_units(report, "cpu")) * args.cpu_scale
    gpu_units = max(0.0, _fleet_units(report, "gpu")) * args.gpu_scale
    tracks = _build_tracks(report, args.reward_scale)
    if not tracks:
        raise SystemExit("report has no track data; export a fuller autopilot report first")

    fleets = []
    if any(track["profile"] == "cpu" for track in tracks):
        fleets.append({
            "name": "live_cpu_scaled",
            "profile": "cpu",
            "machines": max(1, int(round(cpu_units))),
            "units_per_machine": 1,
            "efficiency": 1.0,
        })
    if any(track["profile"] == "gpu" for track in tracks):
        fleets.append({
            "name": "live_gpu_scaled",
            "profile": "gpu",
            "machines": max(1, int(round(gpu_units or args.gpu_scale or 1))),
            "units_per_machine": 1,
            "efficiency": 1.0,
        })

    return {
        "name": args.name,
        "lesson": (
            "Generated from an exported live autopilot report. "
            "Uses live observed timing/funnel data and synthetic fleet scale factors."
        ),
        "source_report": str(args.report),
        "ticks": args.ticks,
        "tick_minutes": args.tick_minutes,
        "precommit_overhang_penalty": args.precommit_overhang_penalty,
        "fleets": fleets,
        "tracks": tracks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert live autopilot report to reward world")
    parser.add_argument("--report", type=Path, required=True, help="exported admin.py autopilot --json report")
    parser.add_argument("--out", type=Path, help="output reward world path; omit for stdout")
    parser.add_argument("--name", default="from_live_scaled", help="world name")
    parser.add_argument("--cpu-scale", type=float, default=1.0, help="multiply observed CPU fleet units")
    parser.add_argument("--gpu-scale", type=float, default=1.0, help="multiply observed GPU fleet units")
    parser.add_argument("--reward-scale", type=float, default=1.0, help="multiply synthetic track reward values")
    parser.add_argument("--ticks", type=int, default=24)
    parser.add_argument("--tick-minutes", type=float, default=5.0)
    parser.add_argument("--precommit-overhang-penalty", type=float, default=0.01)
    args = parser.parse_args()

    world = _build_world(_load_json(args.report), args)
    text = json.dumps(world, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
