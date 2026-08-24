#!/usr/bin/env python3
"""Unit checks for idle-CPU max_concurrent scale gate."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load_fns():
    path = pathlib.Path(__file__).resolve().parents[1] / "pool_manager" / "pool" / "autopilot.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    keep = []
    want = {
        "should_idle_cpu_max_scale",
        "precommit_already_oversubscribed",
        "oversub_upscale_allowed",
        "idle_hole_blocks_cap_drain",
        "should_ratchet_parked_cap_to_live",
        "should_raise_cap_for_seat_hole",
        "leftover_stranded_blocks_ratchet",
        "health_block_reasons",
    }
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in want:
            keep.append(node)
    if {n.name for n in keep} != want:
        raise RuntimeError(f"missing autopilot helpers: {want - {n.name for n in keep}}")
    ns = {"PRODUCTIVE_IDLE_STALE_ROOT_TOLERANCE": 5}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns


def main() -> int:
    ns = _load_fns()
    fn = ns["should_idle_cpu_max_scale"]
    oversub = ns["precommit_already_oversubscribed"]
    ratchet = ns["oversub_upscale_allowed"]
    hole_drain = ns["idle_hole_blocks_cap_drain"]
    hole_raise = ns["should_raise_cap_for_seat_hole"]
    base = dict(
        enabled=True,
        productive_idle_cpu=4,
        min_idle=3,
        slot_idle_cpu=0,
        min_slot_idle_cpu=16,
        proof_conversion_rate=0.84,
        soft_proof_conversion_floor=0.80,
        root_ready_rate=0.58,
        min_root_ready_rate=0.50,
        roots_pending=59,
        max_roots_pending=256,
        benchmarks_seen=31,
        current_max=12,
        proposed_max=44,
        active_jobs=12,
        stale_proofs=0,
        has_stranded=False,
        has_unregistered=False,
    )
    cases = [
        (base, True, "current live-like pool allows bump"),
        ({**base, "productive_idle_cpu": 2}, False, "below idle min and no free slots"),
        (
            {**base, "productive_idle_cpu": 0, "slot_idle_cpu": 78},
            True,
            "free CPU slots allow bump with soft conversion",
        ),
        (
            {
                **base,
                "productive_idle_cpu": 0,
                "slot_idle_cpu": 78,
                "proof_conversion_rate": 0.75,
            },
            False,
            "slot-idle path blocked below soft conversion floor",
        ),
        ({**base, "root_ready_rate": 0.31}, False, "root ready too low"),
        ({**base, "active_jobs": 8}, False, "ceiling not saturated"),
        ({**base, "proposed_max": 12}, False, "no higher proposal"),
        ({**base, "roots_pending": 256}, False, "hard root backlog"),
        ({**base, "enabled": False}, False, "disabled"),
        (
            {**base, "current_max": 20, "proposed_max": 44, "active_jobs": 78},
            False,
            "78 open on parked 20 must not idle-upscale",
        ),
        (
            {**base, "current_max": 20, "proposed_max": 44, "active_jobs": 34},
            True,
            "34/20 healthy override may ratchet the parked floor",
        ),
    ]
    failed = 0
    for kwargs, expect, label in cases:
        ok_flag, reason = fn(**kwargs)
        passed = ok_flag is expect
        print(f"{'pass' if passed else 'FAIL'}: {label} -> {ok_flag} ({reason})")
        if not passed:
            failed += 1
    helper_cases = [
        ((78, 20), True, "78/20 is oversubscribed"),
        ((20, 20), False, "at cap is saturated, not over"),
        ((12, 20), False, "under cap is not oversubscribed"),
    ]
    for (jobs, cap), expect, label in helper_cases:
        got = oversub(active_jobs=jobs, current_max=cap)
        passed = got is expect
        print(f"{'pass' if passed else 'FAIL'}: {label} -> {got}")
        if not passed:
            failed += 1
    ratchet_cases = [
        (dict(active_jobs=34, current_max=20, proposed_max=44), True, "34/20 may ratchet"),
        (dict(active_jobs=78, current_max=20, proposed_max=44), False, "78/20 flood stays blocked"),
        (dict(active_jobs=12, current_max=20, proposed_max=44), True, "under cap is not a flood"),
    ]
    for kwargs, expect, label in ratchet_cases:
        got = ratchet(**kwargs)
        passed = got is expect
        print(f"{'pass' if passed else 'FAIL'}: {label} -> {got}")
        if not passed:
            failed += 1
    drain_cases = [
        (dict(idle_cpu=27, cpu_claimable=0), True, "idle CPU + 0 claimable blocks cap drain"),
        (dict(idle_gpu=14, gpu_claimable=0), True, "idle GPU + 0 claimable blocks cap drain"),
        (dict(idle_cpu=27, cpu_claimable=30), False, "claimable covers idle CPU → drain ok"),
        (dict(idle_cpu=0, idle_gpu=0), False, "no empty seats → drain ok"),
    ]
    for kwargs, expect, label in drain_cases:
        got = hole_drain(**kwargs)
        passed = got is expect
        print(f"{'pass' if passed else 'FAIL'}: {label} -> {got}")
        if not passed:
            failed += 1
    ratchet_live = ns["should_ratchet_parked_cap_to_live"]
    leftover_stranded = ns["leftover_stranded_blocks_ratchet"]
    health_block = ns["health_block_reasons"]
    live_base = dict(
        active_jobs=34,
        current_max=20,
        proposed_max=34,
        has_unregistered=False,
        unserved_stranded=[],
    )
    energy_leftover = [{"benchmark_id": "6ad444c362", "pending_roots": 2, "assigned_roots": 0}]
    live_cases = [
        (live_base, True, "full fleet 34/20 ratchets without idle CPUs"),
        (
            {**live_base, "unserved_stranded": energy_leftover},
            True,
            "one energy leftover does not pin the parked floor",
        ),
        ({**live_base, "active_jobs": 20}, False, "at parked cap is not a ratchet"),
        ({**live_base, "active_jobs": 78, "proposed_max": 44}, False, "78/20 flood stays blocked"),
        ({**live_base, "has_unregistered": True}, False, "unregistered still blocks ratchet"),
        (
            {
                **live_base,
                "unserved_stranded": [
                    {"pending_roots": 80, "assigned_roots": 0},
                    {"pending_roots": 45, "assigned_roots": 0},
                    {"pending_roots": 40, "assigned_roots": 0},
                ],
            },
            False,
            "a pile of fat unserved jobs still blocks ratchet",
        ),
    ]
    for kwargs, expect, label in live_cases:
        ok_flag, reason = ratchet_live(**kwargs)
        passed = ok_flag is expect
        print(f"{'pass' if passed else 'FAIL'}: {label} -> {ok_flag} ({reason})")
        if not passed:
            failed += 1
    blockers = health_block(
        {
            "stale_roots": 0,
            "stale_proofs": 0,
            "active_unregistered": [],
            "unserved_stranded_benchmarks": energy_leftover,
        }
    )
    passed = blockers["reasons"] == ["unserved_stranded"] and blockers["unserved_stranded"] == 1
    print(f"{'pass' if passed else 'FAIL'}: leftover stranded names the health block -> {blockers}")
    if not passed:
        failed += 1
    leftover_cases = [
        ([], False, "no stranded"),
        (energy_leftover, False, "2-root energy leftover is not a capacity hole"),
        ([{"pending_roots": 80, "assigned_roots": 0}], True, "fat unserved job blocks"),
        (
            [
                {"pending_roots": 2, "assigned_roots": 0},
                {"pending_roots": 2, "assigned_roots": 0},
                {"pending_roots": 2, "assigned_roots": 0},
            ],
            False,
            "three leftover crumbs are not a capacity hole",
        ),
    ]
    hole_raise_cases = [
        (
            dict(idle_gpu=10, gpu_claimable=0, current_max=23, active_jobs=32),
            True,
            "idle GPUs at a full cap raise max_concurrent",
        ),
        (
            dict(idle_gpu=1, gpu_claimable=0, current_max=27, active_jobs=25),
            False,
            "cap still has room — master creates under it",
        ),
        (
            dict(idle_gpu=0, gpu_claimable=0, current_max=27, active_jobs=27),
            False,
            "no idle hole does not raise the cap",
        ),
    ]
    for kwargs, expect, label in hole_raise_cases:
        ok_flag, reason = hole_raise(**kwargs)
        passed = ok_flag is expect
        print(f"{'pass' if passed else 'FAIL'}: {label} -> {ok_flag} ({reason})")
        if not passed:
            failed += 1
    for items, expect, label in leftover_cases:
        got = leftover_stranded(items)
        passed = got is expect
        print(f"{'pass' if passed else 'FAIL'}: {label} -> {got}")
        if not passed:
            failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
