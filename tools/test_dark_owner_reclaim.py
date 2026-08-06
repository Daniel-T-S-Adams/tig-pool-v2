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
    target = None
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == "batch_owner_stealable":
            target = node
            break
    if target is None:
        raise RuntimeError("batch_owner_stealable not found")
    ns: dict = {"Optional": __import__("typing").Optional, "Set": __import__("typing").Set}
    # Default arg DARK_OWNER_RECLAIM_MS is a Name in the function signature —
    # provide it in ns before exec.
    ns["DARK_OWNER_RECLAIM_MS"] = 180_000
    exec(compile(ast.Module(body=[target], type_ignores=[]), str(path), "exec"), ns, ns)
    return ns["batch_owner_stealable"]


def main() -> int:
    fn = _load_fn()
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
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
