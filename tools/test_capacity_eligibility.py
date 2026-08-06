#!/usr/bin/env python3
"""Unit checks for autopilot capacity eligibility with stale tolerance."""

from __future__ import annotations

import ast
import pathlib


def _load_fn():
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "pool_manager"
        / "pool"
        / "autopilot.py"
    )
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    helpers = []
    target = None
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in {
            "_counts_for_capacity",
            "_is_public_member_slave",
            "_slave_profile",
        }:
            helpers.append(node)
            if node.name == "_counts_for_capacity":
                target = node
    if target is None:
        raise RuntimeError("_counts_for_capacity not found")
    ns = {
        "TRUSTED_CPU_COMPLETIONS": 10,
        "TRUSTED_GPU_COMPLETIONS": 2,
        "TRUSTED_MAX_FAILED_RECENT": 0,
        "CAPACITY_LIVE_MIN_COMPLETIONS": 3,
        "CAPACITY_STUCK_MIN_INFLIGHT": 4,
    }
    exec(compile(ast.Module(body=helpers, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns["_counts_for_capacity"]


def _slave(**kwargs):
    base = {
        "slave_name": "pool-cpu-abc-pica01",
        "active_now": True,
        "registered_active": True,
        "trust_state": "probation",
        "profile": "cpu",
        "completed_recent": 0,
        "stale_roots": 0,
        "stale_proofs": 0,
        "failed_recent": 0,
        "active_unfinished": 0,
    }
    base.update(kwargs)
    return base


def main() -> int:
    fn = _load_fn()
    cases = [
        (
            fn(_slave(completed_recent=12, stale_roots=2)) is True,
            "completing worker with stale still counts",
        ),
        (
            fn(
                _slave(
                    completed_recent=0,
                    stale_roots=8,
                    active_unfinished=16,
                )
            )
            is False,
            "stuck warehouse excluded",
        ),
        (
            fn(
                _slave(
                    completed_recent=5,
                    stale_roots=1,
                    active_unfinished=6,
                )
            )
            is True,
            "live worker with some finishes counts",
        ),
        (
            fn(_slave(completed_recent=2, active_unfinished=0)) is False,
            "idle low-complete probation excluded",
        ),
        (
            fn(
                _slave(
                    trust_state="trusted",
                    completed_recent=0,
                    stale_roots=3,
                )
            )
            is True,
            "trusted always counts when active",
        ),
    ]
    failed = 0
    for ok, label in cases:
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
