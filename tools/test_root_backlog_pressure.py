#!/usr/bin/env python3
"""Unit checks for GPU-aware root backlog pressure on max_concurrent."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load_fns():
    path = pathlib.Path(__file__).resolve().parents[1] / "pool_manager" / "pool" / "autopilot.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    wanted = {"_profile_roots_pending", "_root_backlog_pressure"}
    nodes = [
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    if {n.name for n in nodes} != wanted:
        raise RuntimeError(f"missing helpers: wanted {wanted}, found {[n.name for n in nodes]}")
    ns = {
        "ROOT_PENDING_MAX_CONCURRENT_DRAIN": 396,
        "ROOT_READY_RATE_MIN_FOR_UPSCALE": 0.50,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns["_profile_roots_pending"], ns["_root_backlog_pressure"]


def main() -> int:
    _profile, pressure = _load_fns()
    cases = [
        (
            {
                "roots_pending": 550,
                "cpu_roots_pending": 500,
                "gpu_roots_pending": 50,
                "root_ready_rate": 0.66,
                "benchmarks_seen": 80,
            },
            None,
            "cpu-heavy backlog does not drain max_concurrent",
        ),
        (
            {
                "roots_pending": 450,
                "cpu_roots_pending": 50,
                "gpu_roots_pending": 400,
                "root_ready_rate": 0.66,
                "benchmarks_seen": 80,
            },
            "gpu_roots_pending_above_drain_threshold",
            "gpu pending above threshold drains",
        ),
        (
            {
                "roots_pending": 20,
                "cpu_roots_pending": 10,
                "gpu_roots_pending": 10,
                "root_ready_rate": 0.31,
                "benchmarks_seen": 80,
            },
            None,
            "low global ready rate with mixed pending does not drain",
        ),
        (
            {
                "roots_pending": 153,
                "cpu_roots_pending": 140,
                "gpu_roots_pending": 13,
                "root_ready_rate": 0.275,
                "benchmarks_seen": 80,
            },
            None,
            "cpu 137 pile plus a few GPU roots does not drain max_concurrent",
        ),
        (
            {
                "roots_pending": 90,
                "cpu_roots_pending": 10,
                "gpu_roots_pending": 80,
                "root_ready_rate": 0.31,
                "benchmarks_seen": 80,
            },
            "low_root_ready_rate_with_pending_gpu_roots",
            "low ready rate with GPU-majority pending drains",
        ),
        (
            {
                "roots_pending": 10,
                "cpu_roots_pending": 10,
                "gpu_roots_pending": 0,
                "root_ready_rate": 0.31,
                "benchmarks_seen": 80,
            },
            None,
            "low ready rate with cpu-only pending does not drain",
        ),
        (
            {
                "roots_pending": 500,
                "root_ready_rate": 0.66,
                "benchmarks_seen": 80,
            },
            "gpu_roots_pending_above_drain_threshold",
            "legacy total-only summary still drains (compat)",
        ),
    ]
    failed = 0
    for funnel, expect_reason, label in cases:
        total, cpu, gpu = _profile(funnel)
        result = pressure(funnel)
        if expect_reason is None:
            ok = result is None
            detail = "None" if result is None else result.get("reasons")
        else:
            reasons = (result or {}).get("reasons") or []
            ok = result is not None and expect_reason in reasons
            detail = reasons
            if ok:
                ok = (
                    result.get("cpu_roots_pending") == cpu
                    and result.get("gpu_roots_pending") == gpu
                    and result.get("roots_pending") == total
                )
        print(f"{'pass' if ok else 'FAIL'}: {label} -> {detail}")
        if not ok:
            failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
