#!/usr/bin/env python3
"""Unit checks: GPU slots/challenge caps follow live eligible GPU headcount down."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load_fns():
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "pool_manager"
        / "pool"
        / "autopilot.py"
    )
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    wanted = {
        "_gpu_job_target",
        "_target_resource_slots",
        "_target_per_challenge_caps",
        "_challenge_ids_by_profile",
    }
    funcs = []
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            funcs.append(node)
    names = {f.name for f in funcs}
    missing = wanted - names
    if missing:
        raise RuntimeError(f"missing helpers: {sorted(missing)}")

    ns = {
        "math": __import__("math"),
        "CPU_SLOT_TYPE": "vehicle_routing",
        "GPU_SLOT_TYPES": ("vector_search", "hypergraph", "neuralnet_optimizer"),
        "MAX_CPU_SLOTS": 96,
        "MAX_GPU_SLOTS_PER_TYPE": 16,
        "GPU_UNITS_PER_JOB": 4,
        "UPSTREAM_SAFE_MAX_BENCHMARKS": 192,
        "BENCHMARK_BUFFER": 2,
        "PRODUCTIVE_IDLE_CPU_SCALE_MIN": 5,
        "PRODUCTIVE_IDLE_CPU_PER_SLOT": 4,
        "PRODUCTIVE_IDLE_GPU_SCALE_MIN": 1,
        "PRODUCTIVE_IDLE_GPU_PER_SLOT": 1,
        "PRODUCTIVE_IDLE_STALE_ROOT_TOLERANCE": 5,
        "WORKLOAD_MIN_BATCH_SIZE": 1,
        "_max_challenge_benchmarks": lambda challenge_id: 32,
    }
    exec(
        compile(ast.Module(body=funcs, type_ignores=[]), str(path), "exec"),
        ns,
        ns,
    )
    return ns


def _capacity(**overrides):
    base = {
        "active_cpu": 30,
        "active_gpu": 4,
        "productive_idle_cpu": 0,
        "productive_idle_gpu": 0,
        "cpu_pressure": 10,
        "gpu_pressure": 4,
        "stale_total": 0,
        "stale_roots": 0,
        "stale_proofs": 0,
        "stale_challenge_ids": [],
        "slot_counts": {
            "vehicle_routing": 40,
            "vector_search": 4,
            "hypergraph": 4,
            "neuralnet_optimizer": 4,
        },
        "slot_idle": {
            "vehicle_routing": 5,
            "vector_search": 2,
            "hypergraph": 2,
            "neuralnet_optimizer": 2,
        },
        "slot_busy": {
            "vehicle_routing": 35,
            "vector_search": 2,
            "hypergraph": 1,
            "neuralnet_optimizer": 1,
        },
        "current_slots": {
            "vehicle_routing": 40,
            "vector_search": 4,
            "hypergraph": 4,
            "neuralnet_optimizer": 4,
        },
        "gpu_slot_floor": {
            "vector_search": 1,
            "hypergraph": 1,
            "neuralnet_optimizer": 1,
        },
        "current_adaptive_caps": {"gpu_max_cap": 4, "cpu_max_cap": 2},
        "aws_cpu_jobs": 0,
    }
    base.update(overrides)
    return base


def main() -> int:
    ns = _load_fns()
    target_slots = ns["_target_resource_slots"]
    target_per = ns["_target_per_challenge_caps"]
    failed = 0

    # 12 configured GPU slots, only 4 eligible GPUs, 4 busy → target ~4 total.
    proposed = target_slots(_capacity(active_gpu=4))
    gpu_total = (
        int(proposed["vector_search"])
        + int(proposed["hypergraph"])
        + int(proposed["neuralnet_optimizer"])
    )
    ok = gpu_total == 4 and gpu_total < 12
    print(
        f"{'pass' if ok else 'FAIL'}: downscale GPU slots with headcount "
        f"-> total={gpu_total} slots={ {k: proposed[k] for k in ('vector_search','hypergraph','neuralnet_optimizer')} }"
    )
    failed += 0 if ok else 1

    # Busy occupancy floors the target (8 busy > 4 active).
    proposed_busy = target_slots(
        _capacity(
            active_gpu=4,
            slot_busy={
                "vehicle_routing": 35,
                "vector_search": 3,
                "hypergraph": 3,
                "neuralnet_optimizer": 2,
            },
        )
    )
    busy_total = (
        int(proposed_busy["vector_search"])
        + int(proposed_busy["hypergraph"])
        + int(proposed_busy["neuralnet_optimizer"])
    )
    ok = busy_total >= 8
    print(f"{'pass' if ok else 'FAIL'}: busy occupancy floors GPU target -> total={busy_total}")
    failed += 0 if ok else 1

    # No GPUs → operator floor only.
    proposed_none = target_slots(_capacity(active_gpu=0))
    ok = (
        proposed_none["vector_search"] == 1
        and proposed_none["hypergraph"] == 1
        and proposed_none["neuralnet_optimizer"] == 1
    )
    print(
        f"{'pass' if ok else 'FAIL'}: zero GPUs collapses to floor "
        f"-> { {k: proposed_none[k] for k in ('vector_search','hypergraph','neuralnet_optimizer')} }"
    )
    failed += 0 if ok else 1

    # Challenge caps follow proposed slots downward (no ratchet on current=6).
    cfg = {
        "algo_selection": [
            {"algorithm_id": "c004_a1"},
            {"algorithm_id": "c005_a1"},
            {"algorithm_id": "c006_a1"},
        ],
        "per_challenge_max_benchmarks": {"c004": 6, "c005": 6, "c006": 6},
    }
    caps = target_per(
        cfg,
        _capacity(active_gpu=4),
        {
            "vector_search": 2,
            "hypergraph": 1,
            "neuralnet_optimizer": 1,
            "vehicle_routing": 40,
        },
    )
    ok = caps["c004"] == 2 and caps["c005"] == 1 and caps["c006"] == 1
    print(f"{'pass' if ok else 'FAIL'}: challenge caps follow slots down -> {caps}")
    failed += 0 if ok else 1

    keys = ("vector_search", "hypergraph", "neuralnet_optimizer")

    def _gpu_sum(proposed):
        return sum(int(proposed[k]) for k in keys)

    # C3 worker count opens a few shared jobs, not one benchmark per GPU.
    headcount = target_slots(_capacity(active_gpu=2, active_gpu_units=2, sizing_gpu_units=2))
    with_units = target_slots(
        _capacity(active_gpu=2, active_gpu_units=13, sizing_gpu_units=13)
    )
    headcount_total = _gpu_sum(headcount)
    units_total = _gpu_sum(with_units)
    ok = units_total > headcount_total and units_total < 13
    print(
        f"{'pass' if ok else 'FAIL'}: C3 units raise GPU jobs without 1:1 "
        f"-> headcount={headcount_total} units={units_total}"
    )
    failed += 0 if ok else 1

    # 50 GPU workers → ~13 jobs (units/4), not 50, and not stuck at the old 6*3 cap.
    fifty = target_slots(
        _capacity(
            active_gpu=50,
            active_gpu_units=50,
            sizing_gpu_units=50,
            current_slots={
                "vehicle_routing": 40,
                "vector_search": 4,
                "hypergraph": 4,
                "neuralnet_optimizer": 4,
            },
            slot_busy={
                "vehicle_routing": 35,
                "vector_search": 2,
                "hypergraph": 1,
                "neuralnet_optimizer": 1,
            },
        )
    )
    fifty_total = _gpu_sum(fifty)
    ok = 12 <= fifty_total <= 16
    print(f"{'pass' if ok else 'FAIL'}: 50 GPU units fan-out to ~13 jobs -> total={fifty_total}")
    failed += 0 if ok else 1

    # CPU slots follow headcount down instead of ratcheting at the old high.
    cpu_down = target_slots(
        _capacity(
            active_cpu=8,
            cpu_pressure=8,
            productive_idle_cpu=0,
            slot_busy={
                "vehicle_routing": 8,
                "vector_search": 2,
                "hypergraph": 1,
                "neuralnet_optimizer": 1,
            },
        )
    )
    ok = int(cpu_down["vehicle_routing"]) <= 12
    print(f"{'pass' if ok else 'FAIL'}: CPU slots follow fleet down -> cpu={cpu_down['vehicle_routing']}")
    failed += 0 if ok else 1

    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
