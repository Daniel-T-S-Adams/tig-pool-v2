#!/usr/bin/env python3
"""Unit checks for dark-owner root reclaim vs long challenge retry."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load_fn():
    path = pathlib.Path(__file__).resolve().parents[1] / "master" / "slave_manager.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    keep = []
    want = {
        "batch_owner_stealable",
        "assigned_root_reclaimable",
        "leftover_finishes_job",
        "reclaim_idle_assigned_roots",
    }
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in want:
            keep.append(node)
    if {n.name for n in keep} != want:
        raise RuntimeError(f"missing slave_manager helpers: {want - {n.name for n in keep}}")
    ns: dict = {
        "Optional": __import__("typing").Optional,
        "Set": __import__("typing").Set,
        "Dict": __import__("typing").Dict,
    }
    # Default arg DARK_OWNER_RECLAIM_MS is a Name in the function signature —
    # provide it in ns before exec.
    ns["DARK_OWNER_RECLAIM_MS"] = 180_000
    ns["FAT_ROOT_MIN_NONCES"] = 16
    ns["FAT_ROOT_RECLAIM_MS"] = 180_000
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns["batch_owner_stealable"], ns["reclaim_idle_assigned_roots"]


def main() -> int:
    fn, reclaim = _load_fn()
    now = 10_000_000
    online = {"alive"}
    cases = [
        (
            fn(
                now_ms=now,
                slave=None,
                start_time=None,
                algorithm_id="c001_x",
                online_slaves=online,
                is_proof=False,
                retry_ms=7_200_000,
                dark_reclaim_ms=180_000,
            )
            is True,
            "unassigned is stealable",
        ),
        (
            fn(
                now_ms=now,
                slave="alive",
                start_time=now - 600_000,
                algorithm_id="c001_x",
                online_slaves=online,
                is_proof=False,
                retry_ms=7_200_000,
                dark_reclaim_ms=180_000,
            )
            is False,
            "alive slow owner kept under long retry",
        ),
        (
            fn(
                now_ms=now,
                slave="dark",
                start_time=now - 200_000,
                algorithm_id="c001_x",
                online_slaves=online,
                is_proof=False,
                retry_ms=7_200_000,
                dark_reclaim_ms=180_000,
            )
            is True,
            "dark root owner reclaimed after 3m",
        ),
        (
            fn(
                now_ms=now,
                slave="dark",
                start_time=now - 60_000,
                algorithm_id="c001_x",
                online_slaves=online,
                is_proof=False,
                retry_ms=7_200_000,
                dark_reclaim_ms=180_000,
            )
            is False,
            "dark root owner grace under 3m",
        ),
        (
            fn(
                now_ms=now,
                slave="dark",
                start_time=now - 200_000,
                algorithm_id="c001_x",
                online_slaves=online,
                is_proof=True,
                retry_ms=7_200_000,
                dark_reclaim_ms=180_000,
            )
            is False,
            "proofs never dark-stolen",
        ),
        (
            fn(
                now_ms=now,
                slave="alive",
                start_time=now - 7_300_000,
                algorithm_id="c001_x",
                online_slaves=online,
                is_proof=False,
                retry_ms=7_200_000,
                dark_reclaim_ms=180_000,
            )
            is True,
            "challenge retry still steals after timeout",
        ),
    ]
    failed = 0
    for ok, label in cases:
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1

    def _row(bid, idx, slave, start, proof=False, nonces=8):
        return {
            "slave": slave,
            "start_time": start,
            "end_time": None,
            "batch": {
                "benchmark_id": bid,
                "batch_idx": idx,
                "num_nonces": nonces,
                "sampled_nonces": [1] if proof else None,
            },
        }

    idle_owner = [
        _row("job-a", 0, "idle-cpu", now - 30_000),
        _row("job-a", 1, "idle-cpu", now - 30_000),
        _row("job-a", 2, None, None),
    ]
    released = reclaim(
        idle_owner,
        now_ms=now,
        working_by_slave={"idle-cpu": False},
        unassigned_by_bid={"job-a": 1},
    )
    ok = (
        len(released) == 2
        and idle_owner[0]["slave"] is None
        and idle_owner[1]["slave"] is None
        and idle_owner[2]["slave"] is None
    )
    print(f"{'pass' if ok else 'FAIL'}: telem-idle owner releases assigned roots")
    if not ok:
        failed += 1

    busy = [_row("job-b", 0, "busy-cpu", now - 30_000)]
    released = reclaim(
        busy,
        now_ms=now,
        working_by_slave={"busy-cpu": True},
        unassigned_by_bid={"job-b": 11},
    )
    ok = len(released) == 0 and busy[0]["slave"] == "busy-cpu"
    print(f"{'pass' if ok else 'FAIL'}: working owner keeps assigned roots")
    if not ok:
        failed += 1

    last = [_row("job-c", 0, "idle-cpu", now - 30_000)]
    released = reclaim(
        last,
        now_ms=now,
        working_by_slave={"idle-cpu": False},
        unassigned_by_bid={"job-c": 0},
    )
    ok = len(released) == 0 and last[0]["slave"] == "idle-cpu"
    print(f"{'pass' if ok else 'FAIL'}: last leftover stays under 10m steal grace")
    if not ok:
        failed += 1

    proof = [_row("job-d", 0, "idle-cpu", now - 30_000, proof=True)]
    released = reclaim(
        proof,
        now_ms=now,
        working_by_slave={"idle-cpu": False},
        unassigned_by_bid={},
    )
    ok = len(released) == 0 and proof[0]["slave"] == "idle-cpu"
    print(f"{'pass' if ok else 'FAIL'}: proofs are never reclaimed from idle owner")
    if not ok:
        failed += 1

    fat_fresh = [_row("job-e", 0, "idle-cpu", now - 30_000, nonces=32)]
    released = reclaim(
        fat_fresh,
        now_ms=now,
        working_by_slave={"idle-cpu": False},
        unassigned_by_bid={"job-e": 4},
    )
    ok = len(released) == 0 and fat_fresh[0]["slave"] == "idle-cpu"
    print(f"{'pass' if ok else 'FAIL'}: telem-idle fat 32 stays under 3m grace")
    if not ok:
        failed += 1

    fat_aged = [_row("job-f", 0, "idle-cpu", now - 180_000, nonces=32)]
    released = reclaim(
        fat_aged,
        now_ms=now,
        working_by_slave={"idle-cpu": False},
        unassigned_by_bid={"job-f": 4},
    )
    ok = len(released) == 1 and fat_aged[0]["slave"] is None
    print(f"{'pass' if ok else 'FAIL'}: telem-idle fat 32 releases after 3m")
    if not ok:
        failed += 1

    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
