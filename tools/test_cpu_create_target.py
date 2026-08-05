#!/usr/bin/env python3
"""Checks that idle-CPU create target is bounded by max_concurrent, not raw slots."""

from __future__ import annotations

import ast
import pathlib
import sys
import types


def _load():
    path = pathlib.Path(__file__).resolve().parents[1] / "master" / "precommit_manager.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    keep = []
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.Assign)):
            names = []
            if isinstance(node, ast.FunctionDef):
                names = [node.name]
            else:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.append(t.id)
            if any(
                n in {
                    "CPU_CHALLENGE_IDS",
                    "GPU_CHALLENGE_IDS",
                    "_gpu_slot_floor_total",
                    "_cpu_create_target",
                }
                for n in names
            ):
                keep.append(node)
    ns = {"CONFIG": {}}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns


def main() -> int:
    ns = _load()
    ns["CONFIG"] = {
        "max_concurrent_benchmarks": 13,
        "gpu_slot_floor": {"hypergraph": 1, "vector_search": 1, "neuralnet_optimizer": 1},
    }
    target = ns["_cpu_create_target"](96)
    ok = target == 10  # 13 - 3 gpu floor
    print(f"{'pass' if ok else 'FAIL'}: create_target from slots=96/max=13/gpu_floor=3 -> {target}")
    failed = 0 if ok else 1

    ns["CONFIG"] = {"max_concurrent_benchmarks": 13, "gpu_slot_floor": {}}
    target = ns["_cpu_create_target"](96)
    # gpu floor defaults to 0, then max(1, gpu_floor)=1 reserved => 12
    ok = target == 12
    print(f"{'pass' if ok else 'FAIL'}: create_target with empty gpu floor -> {target}")
    if not ok:
        failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
