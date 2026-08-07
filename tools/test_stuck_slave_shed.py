#!/usr/bin/env python3
"""Unit checks for stuck/dark/overload root-owner shedding."""

from __future__ import annotations

import ast
import pathlib


def _load_fn():
    path = pathlib.Path(__file__).resolve().parents[1] / "master" / "job_manager.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    target = None
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == "should_shed_slave_roots":
            target = node
            break
    if target is None:
        raise RuntimeError("should_shed_slave_roots not found")
    ns = {
        "Optional": __import__("typing").Optional,
        "STUCK_SLAVE_SHED_MIN_INFLIGHT": 2,
        "STUCK_SLAVE_SHED_MIN_AGE_MS": 12 * 60 * 1000,
        "STUCK_SLAVE_SHED_MAX_COMPLETES": 1,
        "OVERLOAD_SLAVE_SHED_MIN_INFLIGHT": 2,
        "OVERLOAD_SLAVE_SHED_MIN_AGE_MS": 12 * 60 * 1000,
        "OVERLOAD_SLAVE_SHED_MAX_COMPLETES": 2,
        "DARK_ROOT_SHED_MS": 180_000,
    }
    exec(compile(ast.Module(body=[target], type_ignores=[]), str(path), "exec"), ns, ns)
    return ns["should_shed_slave_roots"]


def main() -> int:
    fn = _load_fn()
    twelve_min = 12 * 60 * 1000
    cases = [
        (
            fn(
                inflight=16,
                oldest_age_ms=200_000,
                completes_in_window=0,
                owner_online=False,
            )
            == "dark_owner",
            "dark owner shed after reclaim grace",
        ),
        (
            fn(
                inflight=16,
                oldest_age_ms=60_000,
                completes_in_window=0,
                owner_online=False,
            )
            is None,
            "dark owner grace under 3m",
        ),
        (
            fn(
                inflight=3,
                oldest_age_ms=twelve_min,
                completes_in_window=0,
                owner_online=True,
            )
            == "stuck_no_progress",
            "online stuck warehouse shed at 12m",
        ),
        (
            fn(
                inflight=3,
                oldest_age_ms=twelve_min,
                completes_in_window=2,
                owner_online=True,
            )
            == "overloaded_slow",
            "online overloaded slow shed",
        ),
        (
            fn(
                inflight=3,
                oldest_age_ms=twelve_min,
                completes_in_window=5,
                owner_online=True,
            )
            is None,
            "healthy busy worker kept",
        ),
        (
            fn(
                inflight=1,
                oldest_age_ms=twelve_min,
                completes_in_window=0,
                owner_online=True,
            )
            is None,
            "below min inflight kept",
        ),
        (
            fn(
                inflight=3,
                oldest_age_ms=10 * 60 * 1000,
                completes_in_window=0,
                owner_online=True,
            )
            is None,
            "under 12m age kept",
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
