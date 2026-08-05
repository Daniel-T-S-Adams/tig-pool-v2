#!/usr/bin/env python3
"""Unit checks for idle-CPU max_concurrent scale gate."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load_fn():
    path = pathlib.Path(__file__).resolve().parents[1] / "pool_manager" / "pool" / "autopilot.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    fn = None
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == "should_idle_cpu_max_scale":
            fn = node
            break
    if fn is None:
        raise RuntimeError("should_idle_cpu_max_scale not found")
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(path), "exec"), ns, ns)
    return ns["should_idle_cpu_max_scale"]


def main() -> int:
    fn = _load_fn()
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
    ]
    failed = 0
    for kwargs, expect, label in cases:
        ok_flag, reason = fn(**kwargs)
        passed = ok_flag is expect
        print(f"{'pass' if passed else 'FAIL'}: {label} -> {ok_flag} ({reason})")
        if not passed:
            failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
