#!/usr/bin/env python3
"""Offline reward what-if simulator for InnoPool autopilot policies.

The regular simulator validates one autopilot decision. This script simulates
many control ticks over synthetic pool worlds and compares policy outcomes.
It is intentionally self-contained: no DB, no network, no Docker, no imports
from the live pool manager.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
from typing import Any


DEFAULT_POLICIES = {
    "conservative": {
        "max_concurrent": 12,
        "max_step": 1,
        "bundle_bias": -1,
        "scale_threshold": 0.96,
        "proof_time_target_sec": 900,
        "stopped_limit": 0.06,
        "funnel_guard": True,
    },
    "current_safe": {
        "max_concurrent": 18,
        "max_step": 2,
        "bundle_bias": 0,
        "scale_threshold": 0.95,
        "proof_time_target_sec": 1200,
        "stopped_limit": 0.10,
        "funnel_guard": True,
    },
    "aggressive": {
        "max_concurrent": 36,
        "max_step": 6,
        "bundle_bias": 2,
        "scale_threshold": 0.90,
        "proof_time_target_sec": 1800,
        "stopped_limit": 0.18,
        "funnel_guard": False,
    },
    "adaptive_reward": {
        "max_concurrent": 18,
        "max_step": 2,
        "bundle_bias": 0,
        "scale_threshold": 0.95,
        "proof_time_target_sec": 900,
        "stopped_limit": 0.08,
        "funnel_guard": True,
        "optimize_bundles": True,
    },
}


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return data


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _fleet_capacity(world: dict, tick: int) -> dict[str, float]:
    capacity = {"cpu": 0.0, "gpu": 0.0}
    for fleet in world.get("fleets") or []:
        start = int(fleet.get("start_tick", 0))
        end = fleet.get("end_tick")
        if tick < start or (end is not None and tick >= int(end)):
            continue
        profile = fleet.get("profile", "cpu")
        machines = float(fleet.get("machines", 1))
        units = float(fleet.get("units_per_machine", 1))
        efficiency = float(fleet.get("efficiency", 1.0))
        capacity[profile] = capacity.get(profile, 0.0) + machines * units * efficiency
    return capacity


def _track_state(track: dict, tick: int) -> dict:
    state = dict(track)
    for event in track.get("events") or []:
        start = int(event.get("start_tick", 0))
        end = event.get("end_tick")
        if tick < start or (end is not None and tick >= int(end)):
            continue
        state.update(event.get("set") or {})
    return state


def _policy_track_config(track: dict, policy: dict, prior: dict | None = None) -> dict:
    min_bundles = int(track.get("min_bundles", 4))
    max_bundles = int(track.get("max_bundles", 32))
    base_bundles = int(track.get("base_bundles", 8))
    bundles = int((prior or {}).get("bundles", base_bundles + int(policy.get("bundle_bias", 0))))
    bundles = _clamp(bundles, min_bundles, max_bundles)
    return {
        "bundles": bundles,
        "batch_size": int(track.get("base_batch_size", 32)),
    }


def _runtime_sec(track: dict, bundles: int, pressure: float) -> float:
    base = float(track.get("runtime_sec_per_bundle", 120))
    tail = 1.0 + max(0.0, pressure - 1.0) * float(track.get("congestion_tail_factor", 0.35))
    return max(1.0, base * bundles * tail)


def _freshness(track: dict, runtime_sec: float, proof_time_sec: float) -> float:
    target = float(track.get("freshness_target_sec", 1800))
    total = runtime_sec + proof_time_sec
    if total <= target:
        return 1.0
    return max(0.25, target / total)


def _score_track(
    track: dict,
    tick_sec: float,
    capacity_units: float,
    max_concurrent_share: float,
    cfg: dict,
) -> dict:
    bundles = int(cfg["bundles"])
    nonces = float(track.get("nonces_per_bundle", 512)) * bundles
    pressure = _safe_div(max_concurrent_share, max(1.0, capacity_units))
    runtime = _runtime_sec(track, bundles, pressure)
    possible_by_time = capacity_units * tick_sec / runtime
    benchmarks = min(max_concurrent_share, possible_by_time)

    stopped_rate = float(track.get("stopped_rate", 0.02))
    proof_conversion = float(track.get("proof_conversion_rate", 0.95))
    proof_time = float(track.get("proof_time_sec", 600))
    completed = benchmarks * max(0.0, 1.0 - stopped_rate) * proof_conversion
    stale = max(0.0, benchmarks - completed)

    solution_rate = float(track.get("solution_rate_per_nonce", 0.000001))
    reward_value = float(track.get("reward_value", 1.0))
    freshness = _freshness(track, runtime, proof_time)
    reward = completed * nonces * solution_rate * reward_value * freshness

    stale_penalty = stale * float(track.get("stale_penalty", 0.02))
    stopped_penalty = benchmarks * stopped_rate * float(track.get("stopped_penalty", 0.04))
    return {
        "benchmarks": benchmarks,
        "completed": completed,
        "stale": stale,
        "runtime_sec": runtime,
        "proof_time_sec": proof_time,
        "nonces": completed * nonces,
        "reward": reward,
        "penalty": stale_penalty + stopped_penalty,
        "net_reward": reward - stale_penalty - stopped_penalty,
        "proof_conversion_rate": proof_conversion,
        "stopped_rate": stopped_rate,
        "freshness": freshness,
    }


def _allocate_slots(tracks: list[dict], capacity: dict[str, float], max_concurrent: int) -> dict[str, float]:
    by_profile: dict[str, list[dict]] = {}
    for track in tracks:
        by_profile.setdefault(track.get("profile", "cpu"), []).append(track)

    out = {}
    total_capacity = sum(capacity.values()) or 1.0
    for profile, rows in by_profile.items():
        profile_capacity = capacity.get(profile, 0.0)
        profile_budget = max_concurrent * profile_capacity / total_capacity
        total_weight = sum(float(row.get("weight", 1)) for row in rows) or 1.0
        for row in rows:
            out[row["id"]] = profile_budget * float(row.get("weight", 1)) / total_weight
    return out


def _adjust_policy(policy: dict, max_concurrent: int, track_cfg: dict, tick_result: dict) -> tuple[int, dict]:
    if not policy.get("funnel_guard"):
        return _clamp(max_concurrent + int(policy.get("max_step", 1)), 3, 128), track_cfg

    conversion = (
        tick_result["proof_conversion_weighted"] / tick_result["benchmarks"]
        if tick_result["benchmarks"]
        else 1.0
    )
    stopped = tick_result["stopped"] / tick_result["benchmarks"] if tick_result["benchmarks"] else 0.0
    avg_proof = tick_result["proof_time_weighted"] / tick_result["completed"] if tick_result["completed"] else 0.0
    safe = (
        conversion >= float(policy.get("scale_threshold", 0.95))
        and stopped <= float(policy.get("stopped_limit", 0.10))
        and avg_proof <= float(policy.get("proof_time_target_sec", 1200))
    )
    step = int(policy.get("max_step", 1))
    if safe and tick_result["utilization"] > 0.75:
        max_concurrent += step
    elif not safe:
        max_concurrent -= step
    max_concurrent = _clamp(max_concurrent, 3, 128)

    if policy.get("optimize_bundles"):
        next_cfg = copy.deepcopy(track_cfg)
        for track_id, row in tick_result["tracks"].items():
            cfg = next_cfg[track_id]
            if row["proof_conversion_rate"] >= 0.95 and row["proof_time_sec"] <= 600 and row["freshness"] >= 0.95:
                cfg["bundles"] = min(cfg["bundles"] + 1, row["max_bundles"])
            elif row["proof_conversion_rate"] < 0.85 or row["proof_time_sec"] > 1200 or row["freshness"] < 0.75:
                cfg["bundles"] = max(cfg["bundles"] - 1, row["min_bundles"])
        track_cfg = next_cfg
    return max_concurrent, track_cfg


def simulate_policy(world: dict, policy_name: str, policy: dict) -> dict:
    ticks = int(world.get("ticks", 24))
    tick_sec = float(world.get("tick_minutes", 5)) * 60.0
    max_concurrent = int(policy.get("max_concurrent", 18))
    track_cfg = {
        track["id"]: _policy_track_config(track, policy)
        for track in world.get("tracks") or []
    }
    totals = {
        "policy": policy_name,
        "expected_reward": 0.0,
        "gross_reward": 0.0,
        "penalty": 0.0,
        "benchmarks": 0.0,
        "completed": 0.0,
        "stale": 0.0,
        "stopped": 0.0,
        "nonces": 0.0,
        "avg_utilization": 0.0,
        "final_max_concurrent": max_concurrent,
        "final_track_bundles": {},
        "ticks": [],
    }

    for tick in range(ticks):
        capacity = _fleet_capacity(world, tick)
        active_tracks = [_track_state(track, tick) for track in world.get("tracks") or []]
        shares = _allocate_slots(active_tracks, capacity, max_concurrent)
        tick_result = {
            "tick": tick,
            "capacity": capacity,
            "max_concurrent": max_concurrent,
            "benchmarks": 0.0,
            "completed": 0.0,
            "stale": 0.0,
            "stopped": 0.0,
            "nonces": 0.0,
            "gross_reward": 0.0,
            "penalty": 0.0,
            "expected_reward": 0.0,
            "proof_time_weighted": 0.0,
            "proof_conversion_weighted": 0.0,
            "tracks": {},
        }

        for track in active_tracks:
            track_id = track["id"]
            share = shares.get(track_id, 0.0)
            score = _score_track(
                track,
                tick_sec,
                capacity.get(track.get("profile", "cpu"), 0.0),
                share,
                track_cfg[track_id],
            )
            stopped = score["benchmarks"] * score["stopped_rate"]
            tick_result["benchmarks"] += score["benchmarks"]
            tick_result["completed"] += score["completed"]
            tick_result["stale"] += score["stale"]
            tick_result["stopped"] += stopped
            tick_result["nonces"] += score["nonces"]
            tick_result["gross_reward"] += score["reward"]
            tick_result["penalty"] += score["penalty"]
            tick_result["expected_reward"] += score["net_reward"]
            tick_result["proof_time_weighted"] += score["proof_time_sec"] * score["completed"]
            tick_result["proof_conversion_weighted"] += score["proof_conversion_rate"] * score["benchmarks"]
            tick_result["tracks"][track_id] = {
                **score,
                "bundles": track_cfg[track_id]["bundles"],
                "min_bundles": int(track.get("min_bundles", 4)),
                "max_bundles": int(track.get("max_bundles", 32)),
            }

        tick_result["utilization"] = _safe_div(tick_result["benchmarks"], max(1.0, max_concurrent))
        overhang = max(0.0, max_concurrent - tick_result["benchmarks"])
        overhang_penalty = overhang * float(world.get("precommit_overhang_penalty", 0.01))
        tick_result["penalty"] += overhang_penalty
        tick_result["expected_reward"] -= overhang_penalty
        tick_result["precommit_overhang"] = overhang
        totals["ticks"].append(copy.deepcopy(tick_result))
        for key in ("benchmarks", "completed", "stale", "stopped", "nonces", "gross_reward", "penalty", "expected_reward"):
            totals[key] += tick_result[key]
        totals["avg_utilization"] += tick_result["utilization"]
        max_concurrent, track_cfg = _adjust_policy(policy, max_concurrent, track_cfg, tick_result)

    totals["avg_utilization"] = round(totals["avg_utilization"] / max(1, ticks), 4)
    totals["final_max_concurrent"] = max_concurrent
    totals["final_track_bundles"] = {key: value["bundles"] for key, value in track_cfg.items()}
    for key in ("expected_reward", "gross_reward", "penalty", "benchmarks", "completed", "stale", "stopped", "nonces"):
        totals[key] = round(totals[key], 6)
    return totals


def run_world(world: dict) -> dict:
    policies = copy.deepcopy(DEFAULT_POLICIES)
    policies.update(world.get("policies") or {})
    results = [simulate_policy(world, name, policy) for name, policy in policies.items()]
    results.sort(key=lambda row: row["expected_reward"], reverse=True)
    winner = results[0]["policy"] if results else None
    assertions = []
    expected = world.get("expected_winner")
    if expected:
        assertions.append({
            "assertion": "expected_winner",
            "passed": winner == expected,
            "detail": f"expected={expected} actual={winner}",
        })
    for policy_name in world.get("expected_not_winner") or []:
        assertions.append({
            "assertion": "expected_not_winner",
            "passed": winner != policy_name,
            "detail": f"not_expected={policy_name} actual={winner}",
        })
    return {
        "world": world.get("name", "unnamed"),
        "lesson": world.get("lesson"),
        "winner": winner,
        "results": results,
        "assertions": assertions,
    }


def _print_world(result: dict) -> None:
    if result.get("lesson"):
        print(f"Lesson: {result['lesson']}")
    print(f"World: {result['world']}")
    print(f"  winner: {result['winner']}")
    for row in result["results"]:
        print(
            "  - "
            f"{row['policy']}: reward={row['expected_reward']:.6f} "
            f"gross={row['gross_reward']:.6f} penalty={row['penalty']:.6f} "
            f"completed={row['completed']:.2f} stale={row['stale']:.2f} "
            f"util={row['avg_utilization']:.2f} "
            f"final_max={row['final_max_concurrent']} "
            f"bundles={json.dumps(row['final_track_bundles'], sort_keys=True)}"
        )
    if result.get("assertions"):
        print("  assertions:")
        for assertion in result["assertions"]:
            status = "pass" if assertion["passed"] else "FAIL"
            print(f"    - {status}: {assertion['assertion']} ({assertion['detail']})")


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline reward what-if simulator")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--world", type=Path, help="reward world JSON")
    source.add_argument("--all", action="store_true", help="run every reward world")
    parser.add_argument(
        "--worlds-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "reward_worlds",
    )
    parser.add_argument("--json", action="store_true", help="print full JSON")
    args = parser.parse_args()

    paths = sorted(args.worlds_dir.glob("*.json")) if args.all else [args.world]
    if not paths:
        raise SystemExit(f"no reward worlds found in {args.worlds_dir}")

    failures = []
    outputs = []
    for index, path in enumerate(paths):
        result = run_world(_load_json(path))
        outputs.append(result)
        if args.json:
            continue
        if index:
            print()
        _print_world(result)
        for assertion in result.get("assertions") or []:
            if not assertion.get("passed"):
                failures.append({"path": str(path), "assertion": assertion})

    if args.json:
        print(json.dumps(outputs, indent=2, sort_keys=True))
    else:
        print()
        print(f"Reward batch summary: {len(paths) - len(failures)}/{len(paths)} worlds passed")
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
