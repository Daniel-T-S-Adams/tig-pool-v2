#!/usr/bin/env python3
"""Unit checks for stuck/dark/overload/zombie root-owner shedding."""

from __future__ import annotations

import ast
import pathlib


def _load_fn():
    path = pathlib.Path(__file__).resolve().parents[1] / "master" / "job_manager.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    keep = []
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in (
            "should_shed_slave_roots",
            "sibling_root_stalled",
        ):
            keep.append(node)
    if {n.name for n in keep} != {"should_shed_slave_roots", "sibling_root_stalled"}:
        raise RuntimeError("missing shed helpers")
    ns = {
        "Optional": __import__("typing").Optional,
        "STUCK_SLAVE_SHED_MIN_INFLIGHT": 2,
        "STUCK_SLAVE_SHED_MIN_AGE_MS": 12 * 60 * 1000,
        "STUCK_SLAVE_SHED_MAX_COMPLETES": 1,
        "OVERLOAD_SLAVE_SHED_MIN_INFLIGHT": 2,
        "OVERLOAD_SLAVE_SHED_MIN_AGE_MS": 12 * 60 * 1000,
        "OVERLOAD_SLAVE_SHED_MAX_COMPLETES": 2,
        "DARK_ROOT_SHED_MS": 180_000,
        "ZOMBIE_IDLE_AGE_MS": 5 * 60 * 1000,
        "ZOMBIE_SINGLE_AGE_MS": 45 * 60 * 1000,
        "SIBLING_STALL_MIN_READY": 2,
        "SIBLING_STALL_MULT": 2.5,
        "SIBLING_STALL_MIN_AGE_MS": 15 * 60 * 1000,
    }
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns["should_shed_slave_roots"], ns["sibling_root_stalled"]


def main() -> int:
    fn, sibling = _load_fn()
    twelve_min = 12 * 60 * 1000
    five_min = 5 * 60 * 1000
    forty_five_min = 45 * 60 * 1000
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
            "below min inflight at 12m without telem kept",
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
        (
            fn(
                inflight=1,
                oldest_age_ms=five_min,
                completes_in_window=0,
                owner_online=True,
                telem_active_batches=0,
            )
            == "zombie_idle",
            "telem idle + aged single root → zombie_idle",
        ),
        (
            fn(
                inflight=1,
                oldest_age_ms=five_min,
                completes_in_window=0,
                owner_online=True,
                telem_active_batches=1,
            )
            is None,
            "telem active worker kept at 5m",
        ),
        (
            fn(
                inflight=1,
                oldest_age_ms=4 * 60 * 1000,
                completes_in_window=0,
                owner_online=True,
                telem_active_batches=0,
            )
            is None,
            "telem idle under zombie idle age kept",
        ),
        (
            fn(
                inflight=1,
                oldest_age_ms=forty_five_min,
                completes_in_window=0,
                owner_online=True,
            )
            == "zombie_no_progress",
            "single-batch 45m no-telem backstop",
        ),
        (
            fn(
                inflight=1,
                oldest_age_ms=forty_five_min,
                completes_in_window=1,
                owner_online=True,
            )
            is None,
            "single-batch with a recent complete kept",
        ),
        (
            fn(
                inflight=1,
                oldest_age_ms=five_min,
                completes_in_window=1,
                owner_online=True,
                telem_active_batches=0,
            )
            is None,
            "telem idle but recent complete kept",
        ),
        (
            sibling(
                assigned_age_ms=1_778_541,
                sibling_ready_n=4,
                sibling_median_ms=578_000,
            )
            is True,
            "hive33 30m vs 10m siblings is stalled",
        ),
        (
            sibling(
                assigned_age_ms=700_000,
                sibling_ready_n=4,
                sibling_median_ms=578_000,
            )
            is False,
            "same job still under 2.5x sibling median is kept",
        ),
        (
            sibling(
                assigned_age_ms=1_778_541,
                sibling_ready_n=1,
                sibling_median_ms=578_000,
            )
            is False,
            "one finished sibling is not enough",
        ),
        (
            sibling(
                is_proof=True,
                assigned_age_ms=1_778_541,
                sibling_ready_n=4,
                sibling_median_ms=578_000,
            )
            is False,
            "proofs are never sibling-stolen",
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
