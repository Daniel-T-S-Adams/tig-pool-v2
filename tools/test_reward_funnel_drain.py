#!/usr/bin/env python3
"""Unit checks for reward-funnel max_concurrent drain soft-skip."""

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
        if isinstance(node, ast.FunctionDef) and node.name == "reward_funnel_max_drain_decision":
            fn = node
            break
    if fn is None:
        raise RuntimeError("reward_funnel_max_drain_decision not found")
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(path), "exec"), ns, ns)
    return ns["reward_funnel_max_drain_decision"]


def main() -> int:
    fn = _load_fn()
    base = dict(
        issues=["low_proof_conversion", "slow_time_to_proof_submission"],
        proof_conversion_rate=0.8261,
        root_ready_rate=0.561,
        productive_idle_cpu=4,
        min_idle=3,
        min_root_ready_rate=0.50,
        min_proof_conversion_rate=0.85,
        soft_proof_conversion_floor=0.80,
    )
    cases = [
        (base, False, "live-like 82.6% conversion skips drain"),
        (
            {**base, "proof_conversion_rate": 0.79},
            True,
            "below soft floor still hard-drains",
        ),
        (
            {**base, "productive_idle_cpu": 1, "slot_idle_cpu": 0, "min_slot_idle_cpu": 16},
            True,
            "no idle CPU or free slots => drain",
        ),
        (
            {**base, "productive_idle_cpu": 0, "slot_idle_cpu": 78, "min_slot_idle_cpu": 16},
            False,
            "free CPU slots skip marginal drain",
        ),
        (
            {**base, "root_ready_rate": 0.40},
            True,
            "weak roots => drain",
        ),
        (
            {
                **base,
                "issues": ["high_unexpected_stopped_without_roots_rate"],
                "proof_conversion_rate": 0.95,
            },
            True,
            "other hard issues still drain",
        ),
        (
            {
                **base,
                "issues": ["slow_time_to_proof_submission"],
                "proof_conversion_rate": 0.90,
            },
            False,
            "soft-only with healthy roots/idle skips",
        ),
    ]
    failed = 0
    for kwargs, expect_drain, label in cases:
        should_drain, meta = fn(**kwargs)
        passed = should_drain is expect_drain
        print(
            f"{'pass' if passed else 'FAIL'}: {label} -> drain={should_drain} "
            f"(marginal={meta.get('marginal_low_proof_conversion')} skip={meta.get('skip_soft_drain')})"
        )
        if not passed:
            failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
