#!/usr/bin/env python3
"""Lightweight unit checks for master precommit create-gate pure logic."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load_fns(*names: str, rel: str = "master/precommit_manager.py"):
    path = pathlib.Path(__file__).resolve().parents[1] / rel
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    keep = []
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            keep.append(node)
    if len(keep) != len(names):
        found = {n.name for n in keep}
        raise RuntimeError(f"missing functions in {rel}: {set(names) - found}")
    ns = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns


def main() -> int:
    ns = _load_fns(
        "_clamp_int",
        "compute_profile_root_caps",
        "compute_cpu_unassigned_cap",
        "profile_root_backlog_blocks",
        "should_block_precommit_create",
        "compute_idle_cpu_needs_work",
        "compute_idle_gpu_needs_work",
        "idle_create_burst",
        "challenge_under_create_cap",
        "should_force_cpu_only",
        "should_reserve_idle_gpu_create",
        "has_positive_weight_for_profile",
    )
    should_block = ns["should_block_precommit_create"]
    compute_caps = ns["compute_profile_root_caps"]
    unassigned_cap = ns["compute_cpu_unassigned_cap"]
    profile_blocks = ns["profile_root_backlog_blocks"]
    idle_needs = ns["compute_idle_cpu_needs_work"]
    idle_gpu = ns["compute_idle_gpu_needs_work"]
    burst = ns["idle_create_burst"]
    under_cap = ns["challenge_under_create_cap"]
    force_cpu = ns["should_force_cpu_only"]
    reserve_gpu = ns["should_reserve_idle_gpu_create"]
    has_weighted = ns["has_positive_weight_for_profile"]

    settings = {
        "enabled": True,
        "max_roots_pending": 256,
        "min_root_ready_rate": 0.5,
        "min_samples": 5,
        "idle_cpu_override": True,
    }
    cases = [
        ((100, 20, 18), False, "healthy modest backlog"),
        ((256, 20, 18), False, "combined pending no longer hard-blocks"),
        ((40, 20, 5), True, "low root ready rate with pending"),
        ((0, 20, 0), False, "no pending roots"),
        ((40, 3, 0), False, "below min samples"),
    ]
    failed = 0
    for args, expect_block, label in cases:
        blocked, reason = should_block(*args, settings)
        ok = blocked is expect_block
        status = "pass" if ok else "FAIL"
        print(f"{status}: {label} args={args} blocked={blocked} reason={reason!r}")
        if not ok:
            failed += 1

    blocked, reason = should_block(73, 20, 6, settings, True)
    ok = blocked is False and reason.startswith("idle_cpu_override:")
    print(
        f"{'pass' if ok else 'FAIL'}: idle CPU overrides low root_ready_rate "
        f"blocked={blocked} reason={reason!r}"
    )
    if not ok:
        failed += 1

    no_override = dict(settings, idle_cpu_override=False)
    blocked, _reason = should_block(73, 20, 6, no_override, True)
    ok = blocked is True
    print(f"{'pass' if ok else 'FAIL'}: idle CPU override disabled still blocks")
    if not ok:
        failed += 1

    # Soft-only: large combined pending must not beat idle CPU override.
    blocked, reason = should_block(256, 20, 6, settings, True)
    ok = blocked is False and reason.startswith("idle_cpu_override:")
    print(
        f"{'pass' if ok else 'FAIL'}: large combined pending does not hard-block "
        f"(idle override) blocked={blocked} reason={reason!r}"
    )
    if not ok:
        failed += 1

    disabled = {
        "enabled": False,
        "max_roots_pending": 1,
        "min_root_ready_rate": 0.99,
        "min_samples": 1,
        "idle_cpu_override": True,
    }
    blocked, _reason = should_block(999, 100, 0, disabled)
    ok = blocked is False
    print(f"{'pass' if ok else 'FAIL'}: disabled governor allows create")
    if not ok:
        failed += 1

    # Adaptive caps from create capacity.
    cap_settings = {
        "cpu_roots_per_job_budget": 24,
        "gpu_roots_per_job_budget": 48,
        "min_cpu_roots_pending": 128,
        "max_cpu_roots_pending": 1024,
        "min_gpu_roots_pending": 64,
        "max_gpu_roots_pending": 512,
        "max_cpu_unassigned_roots": 64,
        "max_gpu_unassigned_roots": 32,
    }
    caps = compute_caps(cap_settings, cpu_create_target=10, gpu_slots_total=9)
    ok = caps["cpu_pending_cap"] == 240 and caps["gpu_pending_cap"] == 432
    print(
        f"{'pass' if ok else 'FAIL'}: adaptive caps cpu={caps['cpu_pending_cap']} "
        f"gpu={caps['gpu_pending_cap']}"
    )
    if not ok:
        failed += 1

    caps_min = compute_caps(cap_settings, cpu_create_target=1, gpu_slots_total=1)
    ok = caps_min["cpu_pending_cap"] == 128 and caps_min["gpu_pending_cap"] == 64
    print(
        f"{'pass' if ok else 'FAIL'}: adaptive caps respect profile mins "
        f"cpu={caps_min['cpu_pending_cap']} gpu={caps_min['gpu_pending_cap']}"
    )
    if not ok:
        failed += 1

    # GPU backlog must not block CPU creates (and vice versa).
    blocks = profile_blocks(
        cpu_roots_pending=50,
        gpu_roots_pending=500,
        cpu_unassigned_roots=0,
        gpu_unassigned_roots=0,
        caps={"cpu_pending_cap": 240, "gpu_pending_cap": 432,
              "cpu_unassigned_cap": 64, "gpu_unassigned_cap": 32},
    )
    ok = (not blocks["cpu"]) and blocks["gpu"]
    print(
        f"{'pass' if ok else 'FAIL'}: gpu pending block does not freeze cpu "
        f"blocks={blocks}"
    )
    if not ok:
        failed += 1

    blocks = profile_blocks(
        cpu_roots_pending=10,
        gpu_roots_pending=10,
        cpu_unassigned_roots=80,
        gpu_unassigned_roots=0,
        caps={"cpu_pending_cap": 240, "gpu_pending_cap": 432,
              "cpu_unassigned_cap": 64, "gpu_unassigned_cap": 32},
    )
    ok = blocks["cpu"] and (not blocks["gpu"])
    print(
        f"{'pass' if ok else 'FAIL'}: cpu unassigned block does not freeze gpu "
        f"blocks={blocks}"
    )
    if not ok:
        failed += 1

    # Idle-CPU bias: claimable must cover the idle fleet, not just be non-zero.
    idle_cases = [
        (
            dict(
                idle_cpu_override=True,
                cpu_slots=96,
                cpu_unassigned_claimable=0,
                cpu_jobs_needing_roots=4,
                cpu_create_target=96,
                cpu_profile_blocked=False,
                online_idle_cpu_slaves=21,
            ),
            True,
            "zero claimable + idle CPUs => needs work",
        ),
        (
            dict(
                idle_cpu_override=True,
                cpu_slots=96,
                cpu_unassigned_claimable=5,
                cpu_jobs_needing_roots=4,
                cpu_create_target=96,
                cpu_profile_blocked=False,
                online_idle_cpu_slaves=21,
            ),
            True,
            "claimable 5 < idle 21 => still needs work",
        ),
        (
            dict(
                idle_cpu_override=True,
                cpu_slots=96,
                cpu_unassigned_claimable=25,
                cpu_jobs_needing_roots=4,
                cpu_create_target=96,
                cpu_profile_blocked=False,
                online_idle_cpu_slaves=21,
            ),
            False,
            "claimable 25 >= idle 21 => no boost",
        ),
        (
            dict(
                idle_cpu_override=True,
                cpu_slots=96,
                cpu_unassigned_claimable=5,
                cpu_jobs_needing_roots=4,
                cpu_create_target=96,
                cpu_profile_blocked=False,
                online_idle_cpu_slaves=0,
            ),
            False,
            "no idle CPUs + claimable>0 => legacy off",
        ),
        (
            dict(
                idle_cpu_override=True,
                cpu_slots=96,
                cpu_unassigned_claimable=0,
                cpu_jobs_needing_roots=4,
                cpu_create_target=96,
                cpu_profile_blocked=False,
                online_idle_cpu_slaves=0,
            ),
            True,
            "no idle CPUs + zero claimable => legacy on",
        ),
        (
            dict(
                idle_cpu_override=True,
                cpu_slots=16,
                cpu_unassigned_claimable=0,
                cpu_jobs_needing_roots=16,
                cpu_create_target=16,
                cpu_profile_blocked=False,
                online_idle_cpu_slaves=48,
            ),
            True,
            "new idle boxes still need work after configured slot target is full",
        ),
    ]
    for kwargs, expect, label in idle_cases:
        got = idle_needs(**kwargs)
        ok = got is expect
        print(f"{'pass' if ok else 'FAIL'}: {label} got={got}")
        if not ok:
            failed += 1

    gpu_idle_cases = [
        (dict(gpu_unassigned_claimable=0, online_idle_gpu_slaves=2), True, "idle GPUs and no claimable work"),
        (dict(gpu_unassigned_claimable=4, online_idle_gpu_slaves=2), False, "enough GPU roots to absorb idle"),
        (dict(gpu_unassigned_claimable=0, online_idle_gpu_slaves=0), False, "no idle GPUs"),
        (
            dict(gpu_unassigned_claimable=0, online_idle_gpu_slaves=2, gpu_profile_blocked=True),
            False,
            "blocked GPU profile does not request more creates",
        ),
    ]
    for kwargs, expect, label in gpu_idle_cases:
        got = idle_gpu(**kwargs)
        ok = got is expect
        print(f"{'pass' if ok else 'FAIL'}: {label} got={got}")
        if not ok:
            failed += 1

    cap_cases = [
        (
            under_cap(
                "c004",
                pending_counts={"c004": 3},
                root_phase_counts={"c004": 0},
                submitted={},
                per_challenge_max={"c004": 2},
                idle_gpu_needs_work=False,
            )
            is False,
            "proof-phase GPU jobs block creates when GPUs are busy",
        ),
        (
            under_cap(
                "c004",
                pending_counts={"c004": 3},
                root_phase_counts={"c004": 0},
                submitted={},
                per_challenge_max={"c004": 2},
                idle_gpu_needs_work=True,
            )
            is True,
            "idle GPUs ignore proof-phase jobs for GPU create cap",
        ),
        (
            under_cap(
                "c001",
                pending_counts={"c001": 4},
                root_phase_counts={"c001": 0},
                submitted={},
                per_challenge_max={"c001": 4},
                idle_gpu_needs_work=True,
            )
            is False,
            "idle GPU override does not lift CPU caps",
        ),
    ]
    for ok, label in cap_cases:
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1

    ok = force_cpu(
        idle_cpu_needs_work=True,
        gpu_starved=False,
        idle_gpu_needs_work=False,
        cpu_profile_blocked=False,
    ) is True
    print(f"{'pass' if ok else 'FAIL'}: force CPU when GPUs already have work")
    if not ok:
        failed += 1

    ok = force_cpu(
        idle_cpu_needs_work=True,
        gpu_starved=False,
        idle_gpu_needs_work=True,
        cpu_profile_blocked=False,
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: do not force CPU while idle GPUs need work")
    if not ok:
        failed += 1

    ok = reserve_gpu(idle_gpu_needs_work=True, last_create_ms=0, now_ms=1000) is True
    print(f"{'pass' if ok else 'FAIL'}: reserve first idle-GPU create")
    if not ok:
        failed += 1

    ok = reserve_gpu(
        idle_gpu_needs_work=True,
        last_create_ms=1_000,
        now_ms=10_000,
        cooldown_ms=30_000,
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: idle-GPU create respects cooldown")
    if not ok:
        failed += 1

    gpu_ids = ("c004", "c005", "c006")
    ok = has_weighted(
        [
            {"algorithm_id": "c003_a137", "weight": 1},
            {"algorithm_id": "c004_a100", "weight": 0},
            {"algorithm_id": "c005_a025", "weight": 0},
            {"algorithm_id": "c006_a036", "weight": 0},
        ],
        gpu_ids,
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: GPU reserve has no weighted algo when only CPU has weight")
    if not ok:
        failed += 1

    ok = has_weighted(
        [
            {"algorithm_id": "c003_a137", "weight": 1},
            {"algorithm_id": "c004_a100", "weight": 1},
        ],
        gpu_ids,
    ) is True
    print(f"{'pass' if ok else 'FAIL'}: GPU reserve sees a positive GPU weight")
    if not ok:
        failed += 1

    got = unassigned_cap(
        {
            "max_cpu_unassigned_roots": 256,
            "cpu_unassigned_per_online": 8,
            "max_cpu_unassigned_roots_ceiling": 768,
        },
        61,
    )
    ok = got == 488
    print(f"{'pass' if ok else 'FAIL'}: 61 CPUs raise unassigned cap 256 -> 488 got={got}")
    if not ok:
        failed += 1
    got = unassigned_cap(
        {
            "max_cpu_unassigned_roots": 512,
            "cpu_unassigned_per_online": 8,
            "max_cpu_unassigned_roots_ceiling": 768,
        },
        20,
    )
    ok = got == 512
    print(f"{'pass' if ok else 'FAIL'}: small fleet keeps 512 floor got={got}")
    if not ok:
        failed += 1
    got = unassigned_cap(
        {
            "max_cpu_unassigned_roots": 512,
            "cpu_unassigned_per_online": 8,
            "max_cpu_unassigned_roots_ceiling": 768,
        },
        200,
    )
    ok = got == 768
    print(f"{'pass' if ok else 'FAIL'}: huge fleet hits 768 ceiling got={got}")
    if not ok:
        failed += 1

    ops_cap = _load_fns(
        "compute_cpu_unassigned_cap",
        rel="pool_manager/pool/ops_metrics.py",
    )["compute_cpu_unassigned_cap"]
    settings_256 = {
        "max_cpu_unassigned_roots": 256,
        "cpu_unassigned_per_online": 8,
        "max_cpu_unassigned_roots_ceiling": 768,
    }
    ok = ops_cap(settings_256, 66) == unassigned_cap(settings_256, 66) == 528
    print(
        f"{'pass' if ok else 'FAIL'}: ops unassigned cap matches master "
        f"ops={ops_cap(settings_256, 66)} master={unassigned_cap(settings_256, 66)}"
    )
    if not ok:
        failed += 1

    burst_cases = [
        (
            burst(idle_cpu_needs_work=False, idle_cpu=48, claimable_cpu=0),
            1,
            "no idle flag => single create",
        ),
        (
            burst(
                idle_cpu_needs_work=True,
                idle_cpu=48,
                claimable_cpu=0,
                max_burst=16,
                cpu_unassigned_remaining=256,
            ),
            16,
            "48 idle / 0 claimable => burst to max 16",
        ),
        (
            burst(
                idle_cpu_needs_work=True,
                idle_cpu=3,
                claimable_cpu=0,
                max_burst=16,
                cpu_unassigned_remaining=256,
            ),
            3,
            "small deficit bursts only the deficit",
        ),
        (
            burst(
                idle_cpu_needs_work=True,
                idle_cpu=48,
                claimable_cpu=0,
                max_burst=16,
                cpu_unassigned_remaining=2,
            ),
            2,
            "burst cannot exceed remaining unassigned cap",
        ),
        (
            burst(
                idle_cpu_needs_work=True,
                idle_cpu=21,
                claimable_cpu=25,
                max_burst=16,
            ),
            1,
            "claimable already covers idle => no burst",
        ),
    ]
    for got, expect, label in burst_cases:
        ok = got == expect
        print(f"{'pass' if ok else 'FAIL'}: {label} got={got} expect={expect}")
        if not ok:
            failed += 1

    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
