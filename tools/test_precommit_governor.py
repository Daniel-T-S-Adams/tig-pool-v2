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
        "keep_ahead_want",
        "compute_idle_gpu_starved",
        "compute_gpu_keep_ahead",
        "compute_idle_gpu_needs_work",
        "concurrent_create_allowed",
        "effective_concurrent_cap",
        "idle_decision_count",
        "idle_create_burst",
        "extra_creates_this_tick",
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
    keep_want = ns["keep_ahead_want"]
    idle_gpu_starved_fn = ns["compute_idle_gpu_starved"]
    gpu_keep_ahead_fn = ns["compute_gpu_keep_ahead"]
    idle_gpu = ns["compute_idle_gpu_needs_work"]
    create_ok = ns["concurrent_create_allowed"]
    eff_cap = ns["effective_concurrent_cap"]
    decision = ns["idle_decision_count"]
    burst = ns["idle_create_burst"]
    extra_tick = ns["extra_creates_this_tick"]
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
        (
            dict(
                idle_cpu_override=True,
                cpu_slots=16,
                cpu_unassigned_claimable=0,
                cpu_jobs_needing_roots=16,
                cpu_create_target=16,
                cpu_profile_blocked=False,
                online_idle_cpu_slaves=0,
                cpu_jobs_in_proof_phase=8,
            ),
            True,
            "proof-phase jobs start CPU replacements before the idle wave",
        ),
        (
            dict(
                idle_cpu_override=True,
                cpu_slots=16,
                cpu_unassigned_claimable=10,
                cpu_jobs_needing_roots=16,
                cpu_create_target=16,
                cpu_profile_blocked=False,
                online_idle_cpu_slaves=0,
                cpu_jobs_in_proof_phase=8,
                unowned_cpu_root_jobs=0,
                online_cpu_slaves=16,
            ),
            True,
            "leftover claimable batches do not count as proving replacements",
        ),
        (
            dict(
                idle_cpu_override=True,
                cpu_slots=16,
                cpu_unassigned_claimable=10,
                cpu_jobs_needing_roots=16,
                cpu_create_target=16,
                cpu_profile_blocked=False,
                online_idle_cpu_slaves=0,
                cpu_jobs_in_proof_phase=8,
                unowned_cpu_root_jobs=8,
                online_cpu_slaves=16,
            ),
            False,
            "unowned replacement jobs cover the proving wave",
        ),
        (
            dict(
                idle_cpu_override=True,
                cpu_slots=4,
                cpu_unassigned_claimable=0,
                cpu_jobs_needing_roots=4,
                cpu_create_target=4,
                cpu_profile_blocked=False,
                online_idle_cpu_slaves=0,
                cpu_jobs_in_proof_phase=1,
                unowned_cpu_root_jobs=1,
                online_cpu_slaves=4,
            ),
            False,
            "tiny CPU fleet keep-ahead is 1 job not a warehouse",
        ),
        (
            dict(
                idle_cpu_override=True,
                cpu_slots=67,
                cpu_unassigned_claimable=10,
                cpu_jobs_needing_roots=40,
                cpu_create_target=67,
                cpu_profile_blocked=False,
                online_idle_cpu_slaves=0,
                cpu_jobs_in_proof_phase=20,
                unowned_cpu_root_jobs=0,
                online_cpu_slaves=67,
            ),
            True,
            "large proving wave requests keep-ahead even when leftovers exist",
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
        (
            dict(gpu_unassigned_claimable=0, online_idle_gpu_slaves=0, gpu_spare_jobs=0),
            False,
            "no idle GPUs and spare disabled",
        ),
        (
            dict(
                gpu_unassigned_claimable=0,
                online_idle_gpu_slaves=0,
                unowned_gpu_root_jobs=0,
                gpu_spare_jobs=2,
            ),
            True,
            "keep-ahead creates spare GPU jobs before anyone is idle",
        ),
        (
            dict(
                gpu_unassigned_claimable=0,
                online_idle_gpu_slaves=0,
                unowned_gpu_root_jobs=2,
                gpu_spare_jobs=2,
            ),
            False,
            "spare GPU jobs already waiting",
        ),
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

    ok = idle_gpu_starved_fn(
        gpu_unassigned_claimable=0, online_idle_gpu_slaves=0
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: keep-ahead is not GPU-card starvation")
    if not ok:
        failed += 1
    ok = idle_gpu_starved_fn(
        gpu_unassigned_claimable=0, online_idle_gpu_slaves=2
    ) is True
    print(f"{'pass' if ok else 'FAIL'}: empty GPU cards are starved")
    if not ok:
        failed += 1
    ok = gpu_keep_ahead_fn(unowned_gpu_root_jobs=0, gpu_spare_jobs=2) is True
    print(f"{'pass' if ok else 'FAIL'}: spare pile short requests keep-ahead")
    if not ok:
        failed += 1
    ok = gpu_keep_ahead_fn(unowned_gpu_root_jobs=2, gpu_spare_jobs=2) is False
    print(f"{'pass' if ok else 'FAIL'}: full spare pile stops keep-ahead")
    if not ok:
        failed += 1
    ok = gpu_keep_ahead_fn(
        unowned_gpu_root_jobs=2,
        gpu_spare_jobs=2,
        gpu_jobs_in_proof_phase=6,
    ) is True
    print(f"{'pass' if ok else 'FAIL'}: proving GPU jobs raise keep-ahead above spare")
    if not ok:
        failed += 1
    ok = gpu_keep_ahead_fn(
        unowned_gpu_root_jobs=6,
        gpu_spare_jobs=2,
        gpu_jobs_in_proof_phase=6,
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: proving GPU keep-ahead stops once replacements exist")
    if not ok:
        failed += 1
    ok = gpu_keep_ahead_fn(
        unowned_gpu_root_jobs=8,
        gpu_spare_jobs=2,
        gpu_jobs_in_proof_phase=18,
        online_gpu_slaves=18,
    ) is True
    print(f"{'pass' if ok else 'FAIL'}: 18 proving GPUs are not capped at 8 replacements")
    if not ok:
        failed += 1
    ok = gpu_keep_ahead_fn(
        unowned_gpu_root_jobs=2,
        gpu_spare_jobs=2,
        gpu_jobs_in_proof_phase=2,
        online_gpu_slaves=2,
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: 2-GPU fleet does not warehouse extra jobs")
    if not ok:
        failed += 1

    want_cases = [
        ((0, 1, 4), 1, "4-box 1-proving wants 1"),
        ((0, 20, 67), 20, "67-box 20-proving wants 20"),
        ((5, 0, 5), 5, "5 idle of 5 online wants 5"),
        ((21, 8, 67), 29, "idle plus proving caps at online"),
        ((0, 30, 18), 18, "proving above online caps at online"),
        ((0, 20, 0), 20, "unknown online does not hide a proving wave"),
    ]
    for args, expect, label in want_cases:
        got = keep_want(idle=args[0], proving=args[1], online=args[2])
        ok = got == expect
        print(f"{'pass' if ok else 'FAIL'}: {label} got={got} expect={expect}")
        if not ok:
            failed += 1

    create_cases = [
        (dict(root_phase_jobs=18, proof_phase_jobs=0, max_concurrent=18), False, "root-phase at cap blocks"),
        (dict(root_phase_jobs=10, proof_phase_jobs=8, max_concurrent=18, overlap_cap=8), True, "proof overlap allows next wave"),
        (dict(root_phase_jobs=18, proof_phase_jobs=8, max_concurrent=18, overlap_cap=8), False, "root-phase at cap still blocks"),
        (dict(root_phase_jobs=12, proof_phase_jobs=20, max_concurrent=18, overlap_cap=8), False, "overlap hard ceiling"),
        (dict(root_phase_jobs=10, proof_phase_jobs=8, submitted=8, max_concurrent=18, overlap_cap=8), False, "in-flight precommits consume root budget"),
        (dict(root_phase_jobs=20, proof_phase_jobs=13, max_concurrent=90, overlap_cap=8), True, "fleet-sized cap lets idle boxes get new jobs"),
    ]
    for kwargs, expect, label in create_cases:
        got = create_ok(**kwargs)
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
        (
            under_cap(
                "c001",
                pending_counts={"c001": 4},
                root_phase_counts={"c001": 0},
                submitted={},
                per_challenge_max={"c001": 4},
                idle_cpu_needs_work=True,
                idle_cpu_slaves=20,
            )
            is True,
            "idle CPUs ignore proof-phase jobs for CPU create cap",
        ),
        (
            under_cap(
                "c001",
                pending_counts={"c001": 6},
                root_phase_counts={"c001": 6},
                submitted={},
                per_challenge_max={"c001": 4},
                idle_cpu_needs_work=True,
                idle_cpu_slaves=20,
            )
            is False,
            "idle CPU cap lift is still bounded",
        ),
        (
            under_cap(
                "c001",
                pending_counts={"c001": 4},
                root_phase_counts={"c001": 4},
                submitted={},
                per_challenge_max={"c001": 4},
                idle_cpu_needs_work=True,
                idle_cpu_slaves=20,
            )
            is True,
            "idle CPUs may exceed cap by at most 2",
        ),
        (
            under_cap(
                "c004",
                pending_counts={"c004": 8},
                root_phase_counts={"c004": 6},
                submitted={},
                per_challenge_max={"c004": 6},
                idle_gpu_needs_work=True,
                gpu_spare_jobs=2,
                idle_gpu_slaves=3,
            )
            is True,
            "idle GPUs lift GPU cap so spare creates are not forced onto CPU",
        ),
        (
            under_cap(
                "c004",
                pending_counts={"c004": 12},
                root_phase_counts={"c004": 10},
                submitted={},
                per_challenge_max={"c004": 6},
                idle_gpu_needs_work=True,
                gpu_spare_jobs=2,
                idle_gpu_slaves=3,
            )
            is False,
            "idle GPU cap lift is still bounded",
        ),
        (
            under_cap(
                "c004",
                pending_counts={"c004": 10},
                root_phase_counts={"c004": 2},
                submitted={},
                per_challenge_max={"c004": 6},
                idle_gpu_starved=False,
                gpu_keep_ahead=True,
                gpu_spare_jobs=2,
                idle_gpu_slaves=0,
            )
            is False,
            "keep-ahead does not ignore proof-phase GPU jobs",
        ),
        (
            under_cap(
                "c004",
                pending_counts={"c004": 7},
                root_phase_counts={"c004": 7},
                submitted={},
                per_challenge_max={"c004": 6},
                idle_gpu_starved=False,
                gpu_keep_ahead=True,
                gpu_spare_jobs=2,
                idle_gpu_slaves=0,
            )
            is True,
            "keep-ahead may exceed cap by the spare count only",
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
        idle_gpu_starved=True,
        cpu_profile_blocked=False,
    ) is True
    print(f"{'pass' if ok else 'FAIL'}: idle GPUs do not unlock CPU burst into GPU lottery")
    if not ok:
        failed += 1

    eff_cases = [
        (dict(max_concurrent=35, online_cpu=67, online_gpu=8, cpu_want_spare=10, gpu_want_spare=3, idle_needs_work=False), 35, "busy fleet keeps autopilot cap"),
        (dict(max_concurrent=35, online_cpu=67, online_gpu=8, cpu_want_spare=10, gpu_want_spare=3, idle_needs_work=True), 88, "idle fleet lifts cap to online plus keep-ahead"),
        (dict(max_concurrent=35, online_cpu=0, online_gpu=0, cpu_want_spare=0, gpu_want_spare=0, idle_needs_work=True), 35, "unknown online does not drop the configured cap"),
        (dict(max_concurrent=120, online_cpu=67, online_gpu=8, cpu_want_spare=10, gpu_want_spare=3, idle_needs_work=True), 120, "already-high cap is not reduced"),
    ]
    for kwargs, expect, label in eff_cases:
        got = eff_cap(**kwargs)
        ok = got == expect
        print(f"{'pass' if ok else 'FAIL'}: {label} got={got} expect={expect}")
        if not ok:
            failed += 1

    ok = force_cpu(
        idle_cpu_needs_work=True,
        gpu_starved=False,
        idle_gpu_starved=False,
        idle_gpu_needs_work=True,
        cpu_profile_blocked=False,
    ) is True
    print(f"{'pass' if ok else 'FAIL'}: keep-ahead spare does not block idle CPU creates")
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

    decision_cases = [
        (decision(0, 23), 23, "instant empty boxes count when sustained is 0"),
        (decision(5, 23), 23, "instant wins when larger than sustained"),
        (decision(8, 3), 8, "sustained wins when larger than instant"),
        (decision(0, 0), 0, "both zero stays zero"),
    ]
    for got, expect, label in decision_cases:
        ok = got == expect
        print(f"{'pass' if ok else 'FAIL'}: {label} got={got} expect={expect}")
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
        (
            burst(
                idle_cpu_needs_work=True,
                idle_cpu=0,
                claimable_cpu=10,
                cpu_want_spare=20,
                cpu_unowned=0,
                max_burst=16,
                cpu_unassigned_remaining=256,
            ),
            16,
            "proving keep-ahead deficit bursts to max 16",
        ),
        (
            burst(
                idle_cpu_needs_work=True,
                idle_cpu=0,
                claimable_cpu=10,
                cpu_want_spare=3,
                cpu_unowned=0,
                max_burst=16,
                cpu_unassigned_remaining=256,
            ),
            3,
            "small proving wave bursts only the spare deficit",
        ),
        (
            burst(
                idle_cpu_needs_work=True,
                idle_cpu=21,
                claimable_cpu=25,
                cpu_want_spare=0,
                cpu_unowned=0,
                max_burst=16,
                cpu_unassigned_remaining=256,
            ),
            1,
            "leftovers covering idle do not burst when no proving spare",
        ),
    ]
    for got, expect, label in burst_cases:
        ok = got == expect
        print(f"{'pass' if ok else 'FAIL'}: {label} got={got} expect={expect}")
        if not ok:
            failed += 1

    extra_cases = [
        (extra_tick(sized_burst=16, first_ok=True, max_burst=16), 15, "first ok => 15 extras"),
        (extra_tick(sized_burst=16, first_ok=False, max_burst=16), 16, "first miss still tries 16"),
        (extra_tick(sized_burst=1, first_ok=True, max_burst=16), 0, "no idle burst => no extras"),
        (extra_tick(sized_burst=1, first_ok=False, max_burst=16), 0, "sized 1 first miss => no extras"),
        (extra_tick(sized_burst=48, first_ok=True, max_burst=16), 16, "extras clamp to max_burst"),
        (extra_tick(sized_burst=0, first_ok=False, max_burst=16), 0, "zero sized => no extras"),
    ]
    for got, expect, label in extra_cases:
        ok = got == expect
        print(f"{'pass' if ok else 'FAIL'}: {label} got={got} expect={expect}")
        if not ok:
            failed += 1

    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
