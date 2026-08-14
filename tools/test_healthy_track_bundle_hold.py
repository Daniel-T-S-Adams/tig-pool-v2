#!/usr/bin/env python3
"""Unit checks: pipeline-healthy tracks keep num_bundles."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load_fns():
    path = pathlib.Path(__file__).resolve().parents[1] / "pool_manager" / "pool" / "autopilot.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    wanted = {"_pipeline_healthy_track", "_hold_healthy_track_bundles"}
    nodes = [
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    found = {n.name for n in nodes}
    if found != wanted:
        raise RuntimeError(f"missing helpers: wanted {wanted}, found {found}")
    ns = {
        "BUNDLE_HOLD_ACTIONS": {
            "drain_root_backlog_pressure",
            "reduce_tail_time",
            "reduce_workload_until_proofs_convert",
            "reduce_workload_until_stopped_rate_recovers",
        }
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns["_pipeline_healthy_track"], ns["_hold_healthy_track_bundles"]


def main() -> int:
    healthy_fn, hold_fn = _load_fns()
    failed = 0

    def check(ok: bool, label: str, detail="") -> None:
        nonlocal failed
        print(f"{'pass' if ok else 'FAIL'}: {label}{(' -> ' + str(detail)) if detail else ''}")
        if not ok:
            failed += 1

    converting = {
        "proof_conversion_rate": 0.96,
        "stopped_rate": 0.02,
        "unexpected_stopped_without_roots": 0,
    }
    check(healthy_fn(converting, 0.85, 0.10) is True, "converting track is pipeline-healthy")
    check(
        healthy_fn({**converting, "proof_conversion_rate": 0.70}, 0.85, 0.10) is False,
        "low conversion is not healthy",
    )
    check(
        healthy_fn({**converting, "unexpected_stopped_without_roots": 3}, 0.85, 0.10) is False,
        "unrunnable track is not healthy",
    )
    check(
        healthy_fn({**converting, "allowlist_blocked": True}, 0.85, 0.10) is False,
        "allowlist-blocked is not healthy",
    )

    bundles, held, reason = hold_fn(
        enabled=True,
        pipeline_healthy=True,
        action="drain_root_backlog_pressure",
        current_bundles=8,
        target_bundles=7,
    )
    check(bundles == 8 and held is True, "backlog does not shrink healthy-track bundles", (bundles, held, reason))

    bundles, held, _ = hold_fn(
        enabled=True,
        pipeline_healthy=True,
        action="reduce_tail_time",
        current_bundles=6,
        target_bundles=5,
    )
    check(bundles == 6 and held is True, "slow-to-proof does not shrink healthy-track bundles")

    bundles, held, _ = hold_fn(
        enabled=True,
        pipeline_healthy=False,
        action="reduce_workload_until_proofs_convert",
        current_bundles=8,
        target_bundles=7,
    )
    check(bundles == 7 and held is False, "unhealthy conversion still allows a bundle cut")

    bundles, held, _ = hold_fn(
        enabled=True,
        pipeline_healthy=True,
        action="reduce_or_fix_unrunnable_track",
        current_bundles=8,
        target_bundles=7,
    )
    check(bundles == 7 and held is False, "unrunnable action is not held")

    bundles, held, _ = hold_fn(
        enabled=False,
        pipeline_healthy=True,
        action="drain_root_backlog_pressure",
        current_bundles=8,
        target_bundles=7,
    )
    check(bundles == 7 and held is False, "hold can be disabled")

    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
