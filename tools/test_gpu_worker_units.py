#!/usr/bin/env python3
"""Unit checks: C3 / multi-GPU slaves count as worker units, not one laptop."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load_fns():
    path = pathlib.Path(__file__).resolve().parents[1] / "pool_manager" / "pool" / "autopilot.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    wanted = {"_is_c3_slave", "_gpu_units", "_route_matches_slave", "_slave_profile"}
    nodes = [
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    found = {n.name for n in nodes}
    if found != wanted:
        raise RuntimeError(f"missing helpers: wanted {wanted}, found {found}")
    ns = {"re": __import__("re")}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns


def main() -> int:
    ns = _load_fns()
    is_c3 = ns["_is_c3_slave"]
    units = ns["_gpu_units"]
    failed = 0

    def check(ok: bool, label: str, detail="") -> None:
        nonlocal failed
        print(f"{'pass' if ok else 'FAIL'}: {label}{(' -> ' + str(detail)) if detail else ''}")
        if not ok:
            failed += 1

    check(is_c3("pool-gpu-abc-c3-dispatcher") is True, "c3 name is detected")
    check(is_c3("pool-gpu-a330c544ec5b-2") is False, "local GPU is not c3")

    cfg = {
        "slaves": [
            {
                "name_regex": "^pool-gpu-.*-c3-.*$",
                "max_concurrent_batches": 8,
            },
            {
                "name_regex": "^pool-gpu-a330c544ec5b-2$",
                "max_concurrent_batches": 12,
            },
        ]
    }
    c3 = {
        "slave_name": "pool-gpu-wallet-c3-1",
        "profile": "gpu",
        "num_workers": 12,
    }
    local = {
        "slave_name": "pool-gpu-a330c544ec5b-2",
        "profile": "gpu",
        "num_workers": 1,
    }
    check(units(c3, cfg) == 12, "C3 uses reported num_workers", units(c3, cfg))
    c3_no_telem = {"slave_name": "pool-gpu-wallet-c3-1", "profile": "gpu"}
    check(units(c3_no_telem, cfg) == 8, "C3 without telemetry uses route cap", units(c3_no_telem, cfg))
    check(units(local, cfg) == 1, "local GPU stays one unit", units(local, cfg))
    check(units({"slave_name": "pool-cpu-1", "profile": "cpu"}, cfg) == 0, "CPU is zero GPU units")
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
