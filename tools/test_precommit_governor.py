#!/usr/bin/env python3
"""Lightweight unit checks for master precommit create-gate pure logic."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load_fns(*names: str):
    path = pathlib.Path(__file__).resolve().parents[1] / "master" / "precommit_manager.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    keep = []
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            keep.append(node)
    if len(keep) != len(names):
        found = {n.name for n in keep}
        raise RuntimeError(f"missing functions: {set(names) - found}")
    ns = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns


def main() -> int:
    ns = _load_fns(
        "_clamp_int",
        "compute_profile_root_caps",
        "profile_root_backlog_blocks",
        "should_block_precommit_create",
    )
    should_block = ns["should_block_precommit_create"]
    compute_caps = ns["compute_profile_root_caps"]
    profile_blocks = ns["profile_root_backlog_blocks"]

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

    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
