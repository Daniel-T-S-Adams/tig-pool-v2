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
        "compute_gpu_unassigned_cap",
        "profile_root_backlog_blocks",
        "should_block_precommit_create",
        "compute_idle_cpu_needs_work",
        "keep_ahead_want",
        "tig_unresolved_ceiling",
        "compute_idle_gpu_starved",
        "compute_gpu_keep_ahead",
        "compute_idle_gpu_needs_work",
        "concurrent_create_allowed",
        "effective_concurrent_cap",
        "idle_decision_count",
        "idle_create_burst",
        "scaled_idle_burst_max",
        "empty_claimable_wave",
        "extra_creates_this_tick",
        "resolve_tick_burst",
        "challenge_under_create_cap",
        "should_force_cpu_only",
        "cpu_idle_blocks_gpu_reserve",
        "cpu_idle_hole",
        "profile_burst_lock",
        "should_reserve_idle_gpu_create",
        "has_positive_weight_for_profile",
        "gpu_inflight_root_ceiling",
        "gpu_fleet_create_allowed",
    )
    should_block = ns["should_block_precommit_create"]
    compute_caps = ns["compute_profile_root_caps"]
    unassigned_cap = ns["compute_cpu_unassigned_cap"]
    gpu_unassigned_cap = ns["compute_gpu_unassigned_cap"]
    profile_blocks = ns["profile_root_backlog_blocks"]
    idle_needs = ns["compute_idle_cpu_needs_work"]
    keep_want = ns["keep_ahead_want"]
    tig_ceiling = ns["tig_unresolved_ceiling"]
    idle_gpu_starved_fn = ns["compute_idle_gpu_starved"]
    gpu_keep_ahead_fn = ns["compute_gpu_keep_ahead"]
    idle_gpu = ns["compute_idle_gpu_needs_work"]
    create_ok = ns["concurrent_create_allowed"]
    eff_cap = ns["effective_concurrent_cap"]
    decision = ns["idle_decision_count"]
    burst = ns["idle_create_burst"]
    scaled_hi = ns["scaled_idle_burst_max"]
    empty_wave = ns["empty_claimable_wave"]
    extra_tick = ns["extra_creates_this_tick"]
    resolve_burst = ns["resolve_tick_burst"]
    under_cap = ns["challenge_under_create_cap"]
    force_cpu = ns["should_force_cpu_only"]
    cpu_blocks_gpu = ns["cpu_idle_blocks_gpu_reserve"]
    cpu_hole_fn = ns["cpu_idle_hole"]
    burst_lock = ns["profile_burst_lock"]
    reserve_gpu = ns["should_reserve_idle_gpu_create"]
    has_weighted = ns["has_positive_weight_for_profile"]
    gpu_ceiling = ns["gpu_inflight_root_ceiling"]
    gpu_fleet_ok = ns["gpu_fleet_create_allowed"]

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

    blocked, reason = should_block(73, 20, 6, settings, False, True)
    ok = blocked is False and reason.startswith("idle_gpu_override:")
    print(
        f"{'pass' if ok else 'FAIL'}: idle GPUs override low root_ready_rate "
        f"blocked={blocked} reason={reason!r}"
    )
    if not ok:
        failed += 1

    blocked, reason = should_block(746, 173, 60, settings, False, True)
    ok = blocked is False and reason.startswith("idle_gpu_override:")
    print(
        f"{'pass' if ok else 'FAIL'}: 0.347 ready-rate still creates for empty GPUs "
        f"blocked={blocked} reason={reason!r}"
    )
    if not ok:
        failed += 1

    blocked, reason = should_block(
        73, 20, 6, settings, False, False, True
    )
    ok = blocked is False and reason.startswith("ready_buffer:")
    print(
        f"{'pass' if ok else 'FAIL'}: empty ready-job buffer overrides low "
        f"root_ready_rate blocked={blocked} reason={reason!r}"
    )
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
            False,
            "no idle CPUs + zero claimable is keep-ahead, not an idle hole",
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
                online_cpu_slaves=16,
            ),
            False,
            "busy proving fleet keep-ahead is not an idle-hole override",
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
            False,
            "leftover claimable batches already are the spare pile",
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
                unowned_cpu_root_jobs=2,
                online_cpu_slaves=4,
            ),
            False,
            "tiny CPU fleet 2-job spare is covered",
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
            False,
            "10 leftover claimable roots cover keep-ahead",
        ),
        (
            dict(
                idle_cpu_override=True,
                cpu_slots=43,
                cpu_unassigned_claimable=572,
                cpu_jobs_needing_roots=40,
                cpu_create_target=43,
                cpu_profile_blocked=False,
                online_idle_cpu_slaves=2,
                cpu_jobs_in_proof_phase=20,
                unowned_cpu_root_jobs=0,
                online_cpu_slaves=43,
            ),
            False,
            "572 claimable / 2 idle must not mint more jobs",
        ),
        (
            dict(
                idle_cpu_override=True,
                cpu_slots=18,
                cpu_unassigned_claimable=1,
                cpu_jobs_needing_roots=10,
                cpu_create_target=18,
                cpu_profile_blocked=False,
                online_idle_cpu_slaves=3,
                cpu_jobs_in_proof_phase=10,
                unowned_cpu_root_jobs=8,
                online_cpu_slaves=18,
            ),
            True,
            "EPYC empty seats (3) with 1 claimable still need work",
        ),
        (
            dict(
                idle_cpu_override=True,
                cpu_slots=18,
                cpu_unassigned_claimable=4,
                cpu_jobs_needing_roots=10,
                cpu_create_target=18,
                cpu_profile_blocked=False,
                online_idle_cpu_slaves=3,
                cpu_jobs_in_proof_phase=10,
                unowned_cpu_root_jobs=8,
                online_cpu_slaves=18,
            ),
            False,
            "claimable covering empty seats is not a hole",
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
                gpu_unassigned_claimable=4,
                online_idle_gpu_slaves=0,
                unowned_gpu_root_jobs=2,
                gpu_spare_jobs=2,
            ),
            False,
            "spare GPU jobs already waiting",
        ),
        (
            dict(
                gpu_unassigned_claimable=0,
                online_idle_gpu_slaves=0,
                unowned_gpu_root_jobs=2,
                gpu_spare_jobs=2,
            ),
            True,
            "unowned GPU jobs with no claimable roots still need work",
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
    ok = gpu_ceiling(online_gpu=15, spare=1) == 16
    print(f"{'pass' if ok else 'FAIL'}: GPU fleet ceiling is cards + 1 spare")
    if not ok:
        failed += 1
    ok = gpu_fleet_ok(gpu_root_jobs=16, online_gpu=15) is False
    print(f"{'pass' if ok else 'FAIL'}: 16 GPU root jobs on 15 cards blocks creates")
    if not ok:
        failed += 1
    ok = gpu_fleet_ok(gpu_root_jobs=15, online_gpu=15) is True
    print(f"{'pass' if ok else 'FAIL'}: 15 GPU root jobs on 15 cards allows the spare")
    if not ok:
        failed += 1
    ok = gpu_fleet_ok(gpu_root_jobs=25, online_gpu=0) is True
    print(f"{'pass' if ok else 'FAIL'}: missing GPU headcount does not invent a block")
    if not ok:
        failed += 1
    ok = gpu_keep_ahead_fn(
        unowned_gpu_root_jobs=0,
        gpu_spare_jobs=2,
        online_gpu_slaves=15,
        gpu_root_jobs=16,
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: keep-ahead stops at the GPU fleet ceiling")
    if not ok:
        failed += 1
    ok = gpu_keep_ahead_fn(unowned_gpu_root_jobs=0, gpu_spare_jobs=2) is True
    print(f"{'pass' if ok else 'FAIL'}: spare pile short requests keep-ahead")
    if not ok:
        failed += 1
    ok = gpu_keep_ahead_fn(
        unowned_gpu_root_jobs=0,
        gpu_spare_jobs=2,
        gpu_unassigned_claimable=32,
        leftover_jobs=1,
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: leftover GPU roots are the next poll")
    if not ok:
        failed += 1
    ok = gpu_keep_ahead_fn(
        unowned_gpu_root_jobs=0,
        gpu_spare_jobs=2,
        gpu_unassigned_claimable=381,
        leftover_jobs=8,
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: leftover GPU jobs already are the spare pile")
    if not ok:
        failed += 1
    ok = gpu_keep_ahead_fn(
        unowned_gpu_root_jobs=0,
        gpu_spare_jobs=2,
        gpu_unassigned_claimable=0,
        leftover_jobs=8,
    ) is True
    print(
        f"{'pass' if ok else 'FAIL'}: sticky leftover crumbs on live GPU jobs "
        f"do not hide keep-ahead when claimable_gpu=0"
    )
    if not ok:
        failed += 1
    ok = gpu_keep_ahead_fn(
        unowned_gpu_root_jobs=0,
        gpu_spare_jobs=2,
        gpu_unassigned_claimable=381,
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: GPU leftover warehouse covers keep-ahead")
    if not ok:
        failed += 1
    ok = gpu_keep_ahead_fn(
        unowned_gpu_root_jobs=2,
        gpu_spare_jobs=2,
        gpu_unassigned_claimable=4,
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: full spare pile stops keep-ahead")
    ok = gpu_keep_ahead_fn(
        unowned_gpu_root_jobs=2,
        gpu_spare_jobs=2,
        gpu_unassigned_claimable=0,
    ) is True
    print(f"{'pass' if ok else 'FAIL'}: unowned GPU jobs without claimable do not satisfy keep-ahead")
    if not ok:
        failed += 1
    ok = gpu_keep_ahead_fn(
        unowned_gpu_root_jobs=2,
        gpu_spare_jobs=2,
        gpu_jobs_in_proof_phase=6,
        gpu_unassigned_claimable=4,
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: proving does not raise GPU keep-ahead above spare")
    if not ok:
        failed += 1
    ok = gpu_keep_ahead_fn(
        unowned_gpu_root_jobs=6,
        gpu_spare_jobs=2,
        gpu_jobs_in_proof_phase=6,
        gpu_unassigned_claimable=8,
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: proving GPU keep-ahead stops once replacements exist")
    if not ok:
        failed += 1
    ok = gpu_keep_ahead_fn(
        unowned_gpu_root_jobs=2,
        gpu_spare_jobs=2,
        gpu_jobs_in_proof_phase=18,
        online_gpu_slaves=18,
        gpu_unassigned_claimable=8,
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: 2-job spare covers proving GPUs")
    if not ok:
        failed += 1
    ok = gpu_keep_ahead_fn(
        unowned_gpu_root_jobs=2,
        gpu_spare_jobs=2,
        gpu_jobs_in_proof_phase=2,
        online_gpu_slaves=2,
        gpu_unassigned_claimable=4,
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: 2-GPU fleet does not warehouse extra jobs")
    if not ok:
        failed += 1

    want_cases = [
        ((0, 1, 4), 2, "4-box fleet wants 2 spare, not one per proving"),
        ((0, 20, 67), 2, "67-box busy fleet wants 2 spare not 20"),
        ((5, 0, 5), 5, "5 idle of 5 online wants 5"),
        ((21, 8, 67), 23, "idle plus spare caps at online"),
        ((0, 30, 18), 2, "proving does not warehouse past spare"),
        ((0, 20, 0), 0, "unknown online does not invent a proving warehouse"),
    ]
    for args, expect, label in want_cases:
        got = keep_want(idle=args[0], proving=args[1], online=args[2])
        ok = got == expect
        print(f"{'pass' if ok else 'FAIL'}: {label} got={got} expect={expect}")
        if not ok:
            failed += 1

    ok = tig_ceiling(100, 15) == 85
    print(f"{'pass' if ok else 'FAIL'}: TIG ceiling is 100 minus headroom")
    if not ok:
        failed += 1
    ok = tig_ceiling(100, 0) == 100
    print(f"{'pass' if ok else 'FAIL'}: zero headroom keeps the TIG limit")
    if not ok:
        failed += 1

    create_cases = [
        (dict(root_phase_jobs=18, proof_phase_jobs=0, max_concurrent=18), False, "root-phase at cap blocks"),
        (dict(root_phase_jobs=10, proof_phase_jobs=8, max_concurrent=18, overlap_cap=8), True, "proof overlap allows next wave"),
        (dict(root_phase_jobs=18, proof_phase_jobs=8, max_concurrent=18, overlap_cap=8), False, "root-phase at cap still blocks"),
        (dict(root_phase_jobs=12, proof_phase_jobs=20, max_concurrent=18, overlap_cap=8), False, "overlap hard ceiling"),
        (dict(root_phase_jobs=10, proof_phase_jobs=8, submitted=8, max_concurrent=18, overlap_cap=8), False, "in-flight precommits consume root budget"),
        (dict(root_phase_jobs=20, proof_phase_jobs=13, max_concurrent=90, overlap_cap=8), True, "fleet-sized cap lets idle boxes get new jobs"),
        (dict(root_phase_jobs=10, proof_phase_jobs=0, max_concurrent=90, unresolved=85, unresolved_ceiling=85), False, "TIG unresolved ceiling blocks creates"),
        (dict(root_phase_jobs=10, proof_phase_jobs=0, max_concurrent=90, unresolved=84, unresolved_ceiling=85), True, "one slot under TIG ceiling still creates"),
        (dict(root_phase_jobs=42, proof_phase_jobs=0, max_concurrent=20, unresolved=42, unresolved_ceiling=85, seat_hole=True), False, "seat hole does not walk around max_concurrent"),
        (dict(root_phase_jobs=42, proof_phase_jobs=0, max_concurrent=20, unresolved=85, unresolved_ceiling=85, seat_hole=True), False, "TIG 85 blocks even with a seat hole"),
        (dict(root_phase_jobs=42, proof_phase_jobs=0, max_concurrent=20, unresolved=42, unresolved_ceiling=85, seat_hole=False), False, "42/20 without a hole stays blocked"),
        (dict(root_phase_jobs=35, proof_phase_jobs=0, max_concurrent=21, unresolved=35, unresolved_ceiling=85, spare_short=True), False, "spare short does not walk around max_concurrent"),
        (dict(root_phase_jobs=35, proof_phase_jobs=0, max_concurrent=21, unresolved=85, unresolved_ceiling=85, spare_short=True), False, "TIG 85 blocks spare top-up"),
        (dict(root_phase_jobs=24, proof_phase_jobs=0, max_concurrent=27, unresolved=24, unresolved_ceiling=85), True, "under the autopilot cap still creates"),
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
                pending_counts={"c001": 25},
                root_phase_counts={"c001": 24},
                submitted={},
                per_challenge_max={"c001": 4},
                idle_cpu_needs_work=True,
                idle_cpu_slaves=20,
            )
            is False,
            "idle CPU cap lift is bounded by idle box count",
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
            "idle CPUs lift cap by the idle box count",
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
        (
            under_cap(
                "c004",
                pending_counts={"c004": 3, "c005": 3, "c006": 3},
                root_phase_counts={"c004": 6, "c005": 5, "c006": 5},
                submitted={},
                per_challenge_max={"c004": 6, "c005": 6, "c006": 6},
                gpu_root_jobs=16,
                online_gpu=15,
            )
            is False,
            "GPU fleet ceiling blocks creates at 15 cards + 1 spare",
        ),
        (
            under_cap(
                "c004",
                pending_counts={"c004": 3},
                root_phase_counts={"c004": 10},
                submitted={},
                per_challenge_max={"c004": 6},
                idle_gpu_needs_work=True,
                idle_gpu_slaves=6,
                gpu_root_jobs=16,
                online_gpu=15,
            )
            is False,
            "idle GPU lift does not walk around the fleet ceiling",
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
                gpu_root_jobs=16,
                online_gpu=15,
            )
            is True,
            "GPU fleet ceiling does not lift or block CPU creates",
        ),
        (
            under_cap(
                "c004",
                pending_counts={"c004": 2},
                root_phase_counts={"c004": 2, "c005": 2, "c006": 2},
                submitted={},
                per_challenge_max={"c004": 6},
                gpu_root_jobs=6,
                online_gpu=15,
            )
            is True,
            "GPU creates still allowed under the fleet ceiling",
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
        cpu_idle_hole=True,
    ) is True
    print(f"{'pass' if ok else 'FAIL'}: force CPU when empty CPUs have nothing to claim")
    if not ok:
        failed += 1
    ok = force_cpu(
        idle_cpu_needs_work=True,
        gpu_starved=False,
        idle_gpu_needs_work=False,
        cpu_profile_blocked=False,
        cpu_idle_hole=False,
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: CPU keep-ahead does not lock extras off GPU")
    if not ok:
        failed += 1

    ok = force_cpu(
        idle_cpu_needs_work=True,
        gpu_starved=False,
        idle_gpu_starved=True,
        cpu_profile_blocked=False,
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: empty GPU cards are not locked out of creates")
    if not ok:
        failed += 1

    eff_cases = [
        (dict(max_concurrent=24, online_cpu=67, online_gpu=16, cpu_want_spare=2, gpu_want_spare=2, idle_needs_work=False), 24, "busy fleet honors a parked autopilot cap"),
        (dict(max_concurrent=24, online_cpu=67, online_gpu=16, idle_needs_work=True, hole_deficit=0), 24, "keep-ahead idle flag does not lift to fleet size"),
        (dict(max_concurrent=24, online_cpu=67, online_gpu=16, idle_needs_work=True, hole_deficit=17, max_hole_lift=16), 24, "hole does not lift the autopilot cap"),
        (dict(max_concurrent=20, online_cpu=66, online_gpu=18, idle_needs_work=True, hole_deficit=35, max_hole_lift=16), 20, "empty XL seats do not walk around the cap"),
        (dict(max_concurrent=35, online_cpu=0, online_gpu=0, cpu_want_spare=0, gpu_want_spare=0, idle_needs_work=True), 35, "unknown online does not drop the configured cap"),
        (dict(max_concurrent=120, online_cpu=67, online_gpu=16, idle_needs_work=True, hole_deficit=50, unresolved_ceiling=85), 85, "TIG ceiling still clamps a high configured cap"),
        (dict(max_concurrent=24, online_cpu=67, online_gpu=16, unresolved_ceiling=85), 24, "no idle hole keeps the parked cap under TIG ceiling"),
    ]
    for kwargs, expect, label in eff_cases:
        got = eff_cap(**kwargs)
        ok = got == expect
        print(f"{'pass' if ok else 'FAIL'}: {label} got={got} expect={expect}")
        if not ok:
            failed += 1

    pica_cap = eff_cap(
        max_concurrent=20, idle_needs_work=True, hole_deficit=4, max_hole_lift=16
    )
    epyc_cap = eff_cap(
        max_concurrent=20, idle_needs_work=True, hole_deficit=4, max_hole_lift=16
    )
    ok = pica_cap == epyc_cap == 20
    print(
        f"{'pass' if ok else 'FAIL'}: 4 Pica seats and 1 EPYC×4 honor the same cap "
        f"pica={pica_cap} epyc={epyc_cap}"
    )
    if not ok:
        failed += 1

    ok = force_cpu(
        idle_cpu_needs_work=True,
        gpu_starved=False,
        idle_gpu_starved=False,
        idle_gpu_needs_work=True,
        cpu_profile_blocked=False,
        cpu_idle_hole=True,
    ) is True
    print(f"{'pass' if ok else 'FAIL'}: GPU keep-ahead spare does not block empty CPU creates")
    if not ok:
        failed += 1

    ok = cpu_hole_fn(idle=46, claimable=0) is True
    print(f"{'pass' if ok else 'FAIL'}: 46 idle CPUs with 0 claimable is a CPU hole")
    if not ok:
        failed += 1
    ok = cpu_hole_fn(idle=0, claimable=114) is False
    print(f"{'pass' if ok else 'FAIL'}: busy CPUs with a warehouse are not a CPU hole")
    if not ok:
        failed += 1
    ok = cpu_hole_fn(idle=3, claimable=1) is True
    print(f"{'pass' if ok else 'FAIL'}: 3 empty EPYC seats with 1 leftover is a hole")
    if not ok:
        failed += 1
    ok = burst_lock(cpu_hole=False, gpu_starved=True) == "gpu"
    print(f"{'pass' if ok else 'FAIL'}: empty GPUs lock extras to GPU even if CPU wants keep-ahead")
    if not ok:
        failed += 1
    ok = burst_lock(cpu_hole=True, gpu_starved=False) == "cpu"
    print(f"{'pass' if ok else 'FAIL'}: empty CPUs lock extras to CPU when GPUs are fed")
    if not ok:
        failed += 1
    ok = burst_lock(cpu_hole=True, gpu_starved=True) == "gpu"
    print(f"{'pass' if ok else 'FAIL'}: empty GPUs beat empty CPUs for extra lock")
    if not ok:
        failed += 1
    ok = burst_lock(cpu_hole=False, gpu_starved=False) == ""
    print(f"{'pass' if ok else 'FAIL'}: no lock when both profiles have work")
    if not ok:
        failed += 1

    ok = cpu_blocks_gpu(
        idle_cpu_needs_work=True,
        idle_gpu_starved=False,
        cpu_idle_hole=True,
    ) is True
    print(f"{'pass' if ok else 'FAIL'}: empty CPUs block GPU keep-ahead reserve")
    if not ok:
        failed += 1
    ok = cpu_blocks_gpu(
        idle_cpu_needs_work=True,
        idle_gpu_starved=False,
        cpu_idle_hole=False,
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: CPU keep-ahead does not freeze GPU creates")
    if not ok:
        failed += 1
    ok = cpu_blocks_gpu(
        idle_cpu_needs_work=True,
        idle_gpu_starved=True,
        cpu_idle_hole=True,
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: empty GPU cards can still reserve")
    if not ok:
        failed += 1
    ok = cpu_blocks_gpu(
        idle_cpu_needs_work=False,
        idle_gpu_starved=False,
    ) is False
    print(f"{'pass' if ok else 'FAIL'}: fed CPUs allow GPU keep-ahead reserve")
    if not ok:
        failed += 1
    ok = extra_tick(sized_burst=41, first_ok=True, max_burst=41) == 40
    print(f"{'pass' if ok else 'FAIL'}: 41-job CPU idle tick still extras 40")
    if not ok:
        failed += 1
    ok = resolve_burst(1, 41) == 41
    print(f"{'pass' if ok else 'FAIL'}: last_sized init 1 does not hide idle burst 41")
    if not ok:
        failed += 1
    ok = resolve_burst(1, 0, 1) == 1
    print(f"{'pass' if ok else 'FAIL'}: all ones stays a single create")
    if not ok:
        failed += 1
    ok = extra_tick(
        sized_burst=resolve_burst(1, 41),
        first_ok=True,
        max_burst=41,
    ) == 40
    print(f"{'pass' if ok else 'FAIL'}: resolving sized=1 + idle=41 still extras 40")
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
    ok = reserve_gpu(
        idle_gpu_needs_work=True,
        last_create_ms=1_000,
        now_ms=10_000,
        cooldown_ms=30_000,
        skip_cooldown=True,
    ) is True
    print(f"{'pass' if ok else 'FAIL'}: empty GPU cards skip reserve cooldown")
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

    gpu_cap_settings = {
        "max_gpu_unassigned_roots": 144,
        "gpu_unassigned_per_online": 8,
        "max_gpu_unassigned_roots_ceiling": 768,
    }
    got = gpu_unassigned_cap(gpu_cap_settings, 18)
    ok = got == 144
    print(f"{'pass' if ok else 'FAIL'}: 18 GPUs keep 144 floor got={got}")
    if not ok:
        failed += 1
    got = gpu_unassigned_cap(gpu_cap_settings, 40)
    ok = got == 320
    print(f"{'pass' if ok else 'FAIL'}: 40 GPUs raise unassigned cap 144 -> 320 got={got}")
    if not ok:
        failed += 1
    got = gpu_unassigned_cap(gpu_cap_settings, 80)
    ok = got == 640
    print(f"{'pass' if ok else 'FAIL'}: 80 GPUs raise unassigned cap 144 -> 640 got={got}")
    if not ok:
        failed += 1
    got = gpu_unassigned_cap(gpu_cap_settings, 200)
    ok = got == 768
    print(f"{'pass' if ok else 'FAIL'}: huge GPU fleet hits 768 ceiling got={got}")
    if not ok:
        failed += 1
    got = gpu_unassigned_cap(
        {
            "max_gpu_unassigned_roots": 32,
            "gpu_unassigned_per_online": 8,
            "max_gpu_unassigned_roots_ceiling": 768,
        },
        0,
    )
    ok = got == 32
    print(f"{'pass' if ok else 'FAIL'}: online GPU 0 keeps configured floor 32 got={got}")
    if not ok:
        failed += 1

    gpu_caps = compute_caps(
        {
            **cap_settings,
            "max_gpu_unassigned_roots": 144,
            "gpu_unassigned_per_online": 8,
            "max_gpu_unassigned_roots_ceiling": 768,
        },
        cpu_create_target=10,
        gpu_slots_total=9,
        online_gpu=40,
    )
    ok = gpu_caps["gpu_unassigned_cap"] == 320
    print(
        f"{'pass' if ok else 'FAIL'}: profile caps use online GPUs not slots "
        f"gpu_unassigned={gpu_caps['gpu_unassigned_cap']}"
    )
    if not ok:
        failed += 1

    ops_cap = _load_fns(
        "compute_cpu_unassigned_cap",
        "compute_gpu_unassigned_cap",
        rel="pool_manager/pool/ops_metrics.py",
    )
    settings_256 = {
        "max_cpu_unassigned_roots": 256,
        "cpu_unassigned_per_online": 8,
        "max_cpu_unassigned_roots_ceiling": 768,
    }
    ok = (
        ops_cap["compute_cpu_unassigned_cap"](settings_256, 66)
        == unassigned_cap(settings_256, 66)
        == 528
    )
    print(
        f"{'pass' if ok else 'FAIL'}: ops unassigned cap matches master "
        f"ops={ops_cap['compute_cpu_unassigned_cap'](settings_256, 66)} "
        f"master={unassigned_cap(settings_256, 66)}"
    )
    if not ok:
        failed += 1
    ok = (
        ops_cap["compute_gpu_unassigned_cap"](gpu_cap_settings, 40)
        == gpu_unassigned_cap(gpu_cap_settings, 40)
        == 320
    )
    print(
        f"{'pass' if ok else 'FAIL'}: ops GPU unassigned cap matches master "
        f"ops={ops_cap['compute_gpu_unassigned_cap'](gpu_cap_settings, 40)} "
        f"master={gpu_unassigned_cap(gpu_cap_settings, 40)}"
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
            2,
            "48 idle / 0 claimable is a 2-job trickle, not a 16-job dump",
        ),
        (
            burst(
                idle_cpu_needs_work=True,
                idle_cpu=3,
                claimable_cpu=0,
                max_burst=16,
                cpu_unassigned_remaining=256,
            ),
            2,
            "small idle + empty claimable still trickles 2",
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
                cpu_unassigned_remaining=256,
            ),
            2,
            "sitting leftovers still trickle at most 2",
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
            2,
            "proving keep-ahead is a 2-job trickle",
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
            2,
            "small proving wave still trickles 2",
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
            2,
            "leftovers covering idle do not dump 16 jobs",
        ),
        (
            burst(
                idle_cpu_needs_work=True,
                idle_cpu=0,
                claimable_cpu=0,
                cpu_want_spare=20,
                cpu_unowned=20,
                base_burst=4,
                max_burst=16,
                cpu_unassigned_remaining=256,
                cpu_online=77,
            ),
            2,
            "claimable 0 uses want but trickles 2",
        ),
        (
            burst(
                idle_cpu_needs_work=True,
                idle_cpu=0,
                claimable_cpu=0,
                cpu_want_spare=80,
                cpu_unowned=80,
                base_burst=4,
                max_burst=16,
                cpu_unassigned_remaining=256,
                cpu_online=200,
            ),
            2,
            "200-box fleet empty claimable still trickles 2",
        ),
        (
            burst(
                idle_cpu_needs_work=True,
                idle_cpu=0,
                claimable_cpu=0,
                cpu_want_spare=0,
                cpu_unowned=12,
                base_burst=4,
                max_burst=16,
                cpu_unassigned_remaining=256,
            ),
            2,
            "busy fleet + empty claimable still trickles 2",
        ),
        (
            burst(
                idle_cpu_needs_work=True,
                idle_gpu_needs_work=True,
                idle_cpu=6,
                claimable_cpu=0,
                idle_gpu=13,
                claimable_gpu=0,
                max_burst=16,
                cpu_unassigned_remaining=256,
                gpu_unassigned_remaining=32,
            ),
            2,
            "19 empty seats after a drain do not dump a 16-job refill",
        ),
    ]
    for got, expect, label in burst_cases:
        ok = got == expect
        print(f"{'pass' if ok else 'FAIL'}: {label} got={got} expect={expect}")
        if not ok:
            failed += 1

    scale_cases = [
        (scaled_hi(base_burst=4, max_burst=16, online=0, want=0), 16, "unknown fleet keeps configured max"),
        (scaled_hi(base_burst=4, max_burst=16, online=77, want=20), 20, "77-box fleet burst follows idle want"),
        (scaled_hi(base_burst=4, max_burst=16, online=200, want=80), 64, "want 80 hits 64 ceiling"),
        (scaled_hi(base_burst=4, max_burst=16, online=512, want=200), 64, "huge fleet hits 64 ceiling"),
        (empty_wave(base_burst=4, hi=16, want=20), 16, "empty claimable wave follows want up to hi"),
        (empty_wave(base_burst=4, hi=25, want=80), 25, "empty claimable wave uses scaled hi"),
        (empty_wave(base_burst=4, hi=16, want=0), 4, "no want still keeps base wave"),
        (
            burst(
                idle_cpu_needs_work=True,
                idle_cpu=0,
                claimable_cpu=10,
                cpu_want_spare=2,
                cpu_unowned=8,
                max_burst=16,
                cpu_unassigned_remaining=256,
            ),
            1,
            "name-busy EPYCs with leftovers do not burst 72 seat-creates",
        ),
        (
            burst(
                idle_cpu_needs_work=True,
                idle_cpu=35,
                claimable_cpu=0,
                max_burst=16,
                cpu_unassigned_remaining=256,
                remaining_cap_room=0,
            ),
            1,
            "oversubscribed parked cap does not burst empty seats",
        ),
        (
            burst(
                idle_cpu_needs_work=True,
                idle_cpu=4,
                claimable_cpu=0,
                max_burst=16,
                cpu_unassigned_remaining=256,
                remaining_cap_room=10,
            ),
            2,
            "4 empty seats still trickle 2",
        ),
    ]
    for got, expect, label in scale_cases:
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
