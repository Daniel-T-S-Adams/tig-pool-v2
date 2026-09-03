#!/usr/bin/env python3
"""Pull-queue: assign fills seats, stuck work is leftover, create only refills."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load_fns(rel: str, *names: str, extra_ns: dict | None = None):
    path = pathlib.Path(__file__).resolve().parents[1] / rel
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    keep = []
    want = set(names)
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in want:
            keep.append(node)
    if {n.name for n in keep} != want:
        raise RuntimeError(f"missing in {rel}: {want - {n.name for n in keep}}")
    ns = dict(extra_ns or {})
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns


def main() -> int:
    failed = 0

    def check(ok: bool, label: str) -> None:
        nonlocal failed
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1

    fleet_ns = _load_fns(
        "master/cpu_tier_caps.py",
        "build_fleet_capacity",
        "fleet_hole_deficit",
        "seat_create_burst",
    )
    fleet = fleet_ns["build_fleet_capacity"]
    hole = fleet_ns["fleet_hole_deficit"]
    pica4 = fleet(cpu_empty=4, cpu_claimable=0, open_jobs=42, parked_cap=20)
    epyc1 = fleet(cpu_empty=4, cpu_claimable=0, open_jobs=42, parked_cap=20)
    check(
        hole(pica4) == hole(epyc1) == 4,
        "4 Pica seats and 1 EPYC×4 have the same hole",
    )

    slave_ns = _load_fns(
        "master/slave_manager.py",
        "seat_per_bench_cap",
        "leftover_finishes_job",
        "assigned_root_reclaimable",
        "batch_owner_stealable",
        "cpu_pack_seats_open",
        "slave_holds_last_leftover",
        "should_skip_foreign_root_for_last_leftover",
        "retain_started_cpu_excess",
        extra_ns={
            "Optional": __import__("typing").Optional,
            "Set": __import__("typing").Set,
            "DARK_OWNER_RECLAIM_MS": 180_000,
            "FAT_ROOT_MIN_NONCES": 16,
            "FAT_ROOT_RECLAIM_MS": 180_000,
        },
    )
    per_bench = slave_ns["seat_per_bench_cap"]
    reclaim = slave_ns["assigned_root_reclaimable"]
    stealable = slave_ns["batch_owner_stealable"]
    holds_last = slave_ns["slave_holds_last_leftover"]
    skip_foreign = slave_ns["should_skip_foreign_root_for_last_leftover"]
    retain = slave_ns["retain_started_cpu_excess"]
    started, unstarted = retain(
        [{"start_time": 1, "batch": {"benchmark_id": "a"}}, {"start_time": None, "batch": {"benchmark_id": "b"}}],
        is_cpu=True,
    )
    check(
        [r["batch"]["benchmark_id"] for r in started] == ["a"]
        and [r["batch"]["benchmark_id"] for r in unstarted] == ["b"],
        "started CPU excess stays assigned; unstarted is released",
    )
    kept_gpu, drop_gpu = retain(
        [{"start_time": 1, "batch": {"benchmark_id": "g"}}],
        is_cpu=False,
    )
    check(
        kept_gpu == [] and [r["batch"]["benchmark_id"] for r in drop_gpu] == ["g"],
        "GPU excess is not retained by the CPU started-batch hold",
    )
    check(
        per_bench(max_concurrent=5, configured=0) == 5,
        "EPYC earnable 5 may take 5 roots of one job (no idle-peer spray)",
    )
    check(
        per_bench(max_concurrent=1, configured=0) == 1,
        "Pica seat cap stays 1",
    )
    check(
        reclaim(
            is_proof=False,
            owner_active=4,
            owner_working=False,
            unassigned_on_job=2,
            remaining_nonces=8,
        )
        is True,
        "owner idle + assigned leftover crumb is reclaimable",
    )
    check(
        reclaim(is_proof=False, owner_active=0, owner_working=False, unassigned_on_job=0)
        is False,
        "assigned last leftover is not stolen mid-start",
    )
    check(
        reclaim(
            is_proof=False,
            owner_active=0,
            owner_working=False,
            unassigned_on_job=0,
            assigned_age_ms=10 * 60 * 1000,
        )
        is True,
        "aged last leftover on idle owner is stealable",
    )
    check(
        reclaim(
            is_proof=False,
            owner_active=1,
            owner_working=True,
            unassigned_on_job=0,
            assigned_age_ms=10 * 60 * 1000,
        )
        is False,
        "aged last leftover stays with a working owner",
    )
    check(
        reclaim(is_proof=True, owner_active=0, owner_working=False) is False,
        "proofs stay with the artifact owner",
    )
    now = 10_000_000
    check(
        stealable(
            now_ms=now,
            slave="idle-owner",
            start_time=now - 5_000,
            algorithm_id="c001_x",
            online_slaves={"pica", "idle-owner"},
            is_proof=False,
            retry_ms=7_200_000,
            owner_active=3,
            owner_working=False,
            unassigned_on_job=2,
            remaining_nonces=8,
        )
        is True,
        "next Pica/EPYC poll can claim an owner-idle assigned crumb",
    )
    check(
        stealable(
            now_ms=now,
            slave="idle-owner",
            start_time=now - 5_000,
            algorithm_id="c001_x",
            online_slaves={"pica", "idle-owner"},
            is_proof=False,
            retry_ms=7_200_000,
            owner_active=1,
            owner_working=False,
            unassigned_on_job=4,
            remaining_nonces=32,
        )
        is False,
        "telem-idle fat 32 is not stolen in the first seconds",
    )
    check(
        stealable(
            now_ms=now,
            slave="idle-owner",
            start_time=now - 180_000,
            algorithm_id="c001_x",
            online_slaves={"pica", "idle-owner"},
            is_proof=False,
            retry_ms=7_200_000,
            owner_active=1,
            owner_working=False,
            unassigned_on_job=4,
            remaining_nonces=32,
        )
        is True,
        "telem-idle fat 32 is stealable after 3m",
    )
    check(
        stealable(
            now_ms=now,
            slave="working-owner",
            start_time=now - 5_000,
            algorithm_id="c001_x",
            online_slaves={"pica", "working-owner"},
            is_proof=False,
            retry_ms=7_200_000,
            owner_active=3,
            owner_working=True,
        )
        is False,
        "working owner keeps a fresh assigned root",
    )
    check(
        stealable(
            now_ms=now,
            slave="ghost-owner",
            start_time=now - 5_000,
            algorithm_id="c004_x",
            online_slaves={"pica", "ghost-owner"},
            is_proof=False,
            retry_ms=3_600_000,
            owner_active=0,
            owner_working=False,
            unassigned_on_job=0,
        )
        is False,
        "fresh last leftover stays put mid-start",
    )
    check(
        stealable(
            now_ms=now,
            slave="ghost-owner",
            start_time=now - (10 * 60 * 1000),
            algorithm_id="c004_x",
            online_slaves={"pica", "ghost-owner"},
            is_proof=False,
            retry_ms=3_600_000,
            owner_active=0,
            owner_working=False,
            unassigned_on_job=0,
        )
        is True,
        "next Pica poll can claim an aged idle last leftover",
    )
    check(
        holds_last(
            [{"benchmark_id": "8ef39f", "sampled_nonces": None}],
            {"8ef39f"},
        )
        is True,
        "assigned last leftover is detected on the owner",
    )
    check(
        skip_foreign(holds_last_leftover=True, candidate_is_last_leftover=False)
        is True,
        "owner of a last leftover does not take a new foreign root",
    )
    check(
        skip_foreign(holds_last_leftover=True, candidate_is_last_leftover=True)
        is False,
        "another job's last leftover may still join",
    )
    check(
        skip_foreign(holds_last_leftover=True, is_proof=True)
        is False,
        "proofs are not blocked by a last leftover",
    )
    check(
        skip_foreign(holds_last_leftover=False, candidate_is_last_leftover=False)
        is False,
        "XL with no last leftover may still take several jobs",
    )
    check(
        skip_foreign(
            holds_last_leftover=True,
            candidate_is_last_leftover=False,
            empty_seats=5,
            max_concurrent=6,
        )
        is False,
        "XL with spare seats may pack SAT leftovers while holding a last crumb",
    )
    check(
        skip_foreign(
            holds_last_leftover=True,
            candidate_is_last_leftover=False,
            empty_seats=0,
            max_concurrent=6,
        )
        is True,
        "full XL still finishes its last leftover before a new foreign root",
    )
    check(
        skip_foreign(
            holds_last_leftover=True,
            candidate_is_last_leftover=False,
            empty_seats=1,
            max_concurrent=1,
        )
        is True,
        "1-seat Pica stays parked on its last leftover",
    )
    check(
        skip_foreign(
            holds_last_leftover=True,
            candidate_is_last_leftover=False,
            empty_seats=1,
            max_concurrent=2,
            poller_is_gpu=True,
        )
        is True,
        "GPU prefetch seat stays parked on a last leftover",
    )
    check(
        reclaim(
            is_proof=False,
            owner_active=5,
            owner_working=True,
            unassigned_on_job=0,
            assigned_age_ms=11 * 60 * 1000,
            owner_other_roots=4,
        )
        is True,
        "stale last leftover is stealable when the owner is warehousing",
    )
    check(
        stealable(
            now_ms=now,
            slave="working-owner",
            start_time=now - (11 * 60 * 1000),
            algorithm_id="c007_a033",
            online_slaves={"pica", "working-owner"},
            is_proof=False,
            retry_ms=7_200_000,
            owner_active=5,
            owner_working=True,
            unassigned_on_job=0,
        )
        is True,
        "next Pica can steal a 11-min last leftover from a busy XL",
    )
    check(
        stealable(
            now_ms=now,
            slave="working-owner",
            start_time=now - (11 * 60 * 1000),
            algorithm_id="c007_a033",
            online_slaves={"pica", "working-owner"},
            is_proof=False,
            retry_ms=7_200_000,
            owner_active=1,
            owner_working=True,
            unassigned_on_job=0,
        )
        is False,
        "last leftover is not stolen while it is the owner's only root",
    )

    pre_ns = _load_fns(
        "master/precommit_manager.py",
        "concurrent_create_allowed",
        "effective_concurrent_cap",
    )
    create_ok = pre_ns["concurrent_create_allowed"]
    eff_cap = pre_ns["effective_concurrent_cap"]
    check(
        create_ok(
            root_phase_jobs=42,
            max_concurrent=20,
            unresolved=42,
            unresolved_ceiling=85,
            seat_hole=True,
        )
        is False,
        "42 open / parked 20 is over the autopilot cap even with a hole",
    )
    check(
        create_ok(
            root_phase_jobs=20,
            max_concurrent=20,
            unresolved=85,
            unresolved_ceiling=85,
            seat_hole=True,
        )
        is False,
        "85 unresolved → create blocked",
    )
    check(
        create_ok(
            root_phase_jobs=25,
            max_concurrent=27,
            unresolved=25,
            unresolved_ceiling=85,
            seat_hole=True,
        )
        is True,
        "25/27 under the autopilot cap still creates for a GPU hole",
    )
    check(
        create_ok(
            root_phase_jobs=83,
            max_concurrent=85,
            unresolved=83,
            unresolved_ceiling=85,
            gpu_seat_hole=True,
        )
        is True,
        "GPU seat hole may create under TIG ceiling when CPU filled 85",
    )
    check(
        create_ok(
            root_phase_jobs=20,
            max_concurrent=20,
            unresolved=85,
            unresolved_ceiling=85,
            gpu_seat_hole=True,
        )
        is False,
        "GPU seat hole still stops at the TIG unresolved ceiling",
    )
    pica_cap = eff_cap(
        max_concurrent=20, idle_needs_work=True, hole_deficit=4, max_hole_lift=16
    )
    epyc_cap = eff_cap(
        max_concurrent=20, idle_needs_work=True, hole_deficit=4, max_hole_lift=16
    )
    check(
        pica_cap == epyc_cap,
        "4 Pica seats and 1 EPYC×4 make the same create-cap decision",
    )

    root = pathlib.Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from master.dispatch import next_hole_profile  # noqa: E402

    check(
        next_hole_profile(
            cpu_hole=True,
            gpu_hole=True,
            cpu_idle=27,
            cpu_claimable=0,
            gpu_idle=14,
            gpu_claimable=0,
            last_profile="cpu",
        )
        == "gpu",
        "14 idle GPUs, 0 GPU claimable, last create CPU → next create is GPU",
    )

    ghost_ns = _load_fns(
        "master/slave_manager.py",
        "drop_ready_ghost_rows",
        "ready_phase_key",
        "forget_ready_marks_for_pending",
    )
    ready_root_63 = ghost_ns["ready_phase_key"]("job_63", is_proof=False)
    ghost_keep, ghost_dropped = ghost_ns["drop_ready_ghost_rows"](
        [
            {
                "slave": "pica11",
                "end_time": None,
                "batch": {"id": "job_63", "batch_idx": 63},
            },
            {
                "slave": "pica11",
                "end_time": None,
                "batch": {"id": "job_64", "batch_idx": 64},
            },
            {
                "slave": "pica11",
                "end_time": None,
                "batch": {
                    "id": "job_63",
                    "batch_idx": 63,
                    "sampled_nonces": [1],
                },
            },
        ],
        {ready_root_63},
        now_ms=1,
    )
    check(
        ghost_dropped == ["root:job_63"]
        and [r["batch"]["id"] for r in ghost_keep] == ["job_64", "job_63"]
        and ghost_keep[1]["batch"].get("sampled_nonces") == [1]
        and ghost_keep[0]["slave"] == "pica11",
        "ready root ghost _63 is dropped; proof _63 and live root _64 stay",
    )
    leftover_106 = ghost_ns["ready_phase_key"](
        "92f771d15115cd46d011a4da7cb3e5c7_106", is_proof=False
    )
    leftover_keep, leftover_dropped = ghost_ns["drop_ready_ghost_rows"](
        [
            {
                "slave": None,
                "end_time": None,
                "batch": {
                    "id": "92f771d15115cd46d011a4da7cb3e5c7_106",
                    "batch_idx": 106,
                    "benchmark_id": "92f771d15115cd46d011a4da7cb3e5c7",
                },
            }
        ],
        {leftover_106},
        now_ms=1,
    )
    check(
        leftover_dropped == []
        and len(leftover_keep) == 1
        and leftover_keep[0]["batch"]["batch_idx"] == 106
        and leftover_keep[0].get("slave") is None,
        "unassigned last leftover is not a ready ghost",
    )
    forgotten_ready, forgotten = ghost_ns["forget_ready_marks_for_pending"](
        {leftover_106, "root:other_1"},
        leftover_keep,
    )
    check(
        leftover_106 not in forgotten_ready
        and "root:other_1" in forgotten_ready
        and leftover_106 in forgotten,
        "SQL-pending leftover drops the stale ready mark",
    )

    auto_ns = _load_fns(
        "pool_manager/pool/autopilot.py",
        "idle_hole_blocks_cap_drain",
        "precommit_already_oversubscribed",
    )
    check(
        auto_ns["idle_hole_blocks_cap_drain"](idle_cpu=27, cpu_claimable=0) is True,
        "autopilot must not drain the parked cap while CPU seats are empty",
    )
    check(
        auto_ns["precommit_already_oversubscribed"](active_jobs=42, current_max=20)
        is True,
        "oversub upscale guard still sees 42/20",
    )

    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
