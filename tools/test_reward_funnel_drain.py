#!/usr/bin/env python3
"""Unit checks for reward-funnel max_concurrent drain soft-skip."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load_fns():
    path = pathlib.Path(__file__).resolve().parents[1] / "pool_manager" / "pool" / "autopilot.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    wanted = {
        "reward_funnel_max_drain_decision",
        "soft_conversion_drain_grace_timer",
    }
    fns = []
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            fns.append(node)
    if len(fns) != 2:
        raise RuntimeError(f"expected 2 helpers, found {[f.name for f in fns]}")
    ns = {}
    exec(compile(ast.Module(body=fns, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns["reward_funnel_max_drain_decision"], ns["soft_conversion_drain_grace_timer"]


def main() -> int:
    fn, grace_fn = _load_fns()
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
        (
            {
                **base,
                "issues": ["slow_time_to_proof_submission"],
                "proof_conversion_rate": 0.9231,
                "productive_idle_cpu": 0,
                "slot_idle_cpu": 0,
                "min_slot_idle_cpu": 16,
            },
            False,
            "soft latency + healthy conversion never drains (even when busy)",
        ),
        (
            {
                **base,
                "issues": ["slow_time_to_proof_submission"],
                "proof_conversion_rate": 0.80,
                "productive_idle_cpu": 0,
                "slot_idle_cpu": 0,
                "min_slot_idle_cpu": 16,
            },
            True,
            "soft latency + weak conversion still drains when busy",
        ),
        (
            {
                **base,
                "productive_idle_cpu": 0,
                "slot_idle_cpu": 0,
                "min_slot_idle_cpu": 16,
                "soft_conversion_grace_active": True,
            },
            False,
            "marginal + root_ready + grace skips without idle",
        ),
        (
            {
                **base,
                "productive_idle_cpu": 0,
                "slot_idle_cpu": 0,
                "min_slot_idle_cpu": 16,
                "soft_conversion_grace_active": False,
            },
            True,
            "marginal without idle/grace still drains",
        ),
        (
            {
                **base,
                "productive_idle_cpu": 0,
                "slot_idle_cpu": 0,
                "min_slot_idle_cpu": 16,
                "soft_conversion_grace_active": True,
                "root_ready_rate": 0.40,
            },
            True,
            "grace does not skip weak roots",
        ),
        (
            {
                **base,
                "productive_idle_cpu": 0,
                "slot_idle_cpu": 0,
                "min_slot_idle_cpu": 16,
                "soft_conversion_grace_active": True,
                "proof_conversion_rate": 0.79,
            },
            True,
            "below soft floor still hard-drains despite grace",
        ),
    ]
    failed = 0
    for kwargs, expect_drain, label in cases:
        should_drain, meta = fn(**kwargs)
        passed = should_drain is expect_drain
        print(
            f"{'pass' if passed else 'FAIL'}: {label} -> drain={should_drain} "
            f"(marginal={meta.get('marginal_low_proof_conversion')} skip={meta.get('skip_soft_drain')} "
            f"grace_skip={meta.get('skip_marginal_conversion_grace')})"
        )
        if not passed:
            failed += 1

    grace_cases = [
        (True, None, 30 * 60 * 1000, 1000, True, 1000, "starts grace on first soft marginal"),
        (True, 1000, 30 * 60 * 1000, 1000 + 10 * 60 * 1000, True, 1000, "within grace stays active"),
        (True, 1000, 30 * 60 * 1000, 1000 + 30 * 60 * 1000, False, 1000, "at grace limit expires"),
        (True, 1000, 30 * 60 * 1000, 1000 + 45 * 60 * 1000, False, 1000, "expired grace does not restart"),
        (False, 1000, 30 * 60 * 1000, 5000, False, None, "leaving soft marginal clears start"),
    ]
    for in_state, started, limit, now, expect_active, expect_next, label in grace_cases:
        active, next_started, meta = grace_fn(
            in_soft_marginal_state=in_state,
            now_ms=now,
            grace_started_ms=started,
            grace_limit_ms=limit,
        )
        passed = active is expect_active and next_started == expect_next
        print(
            f"{'pass' if passed else 'FAIL'}: timer {label} -> active={active} next={next_started} "
            f"(meta={meta})"
        )
        if not passed:
            failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
