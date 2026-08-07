#!/usr/bin/env python3
"""Unit checks for per-challenge autopilot benchmark ceilings."""

from __future__ import annotations

import ast
import os
import pathlib
import sys


def _load_fn():
    path = pathlib.Path(__file__).resolve().parents[1] / "pool_manager" / "pool" / "autopilot.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    wanted = {"_max_challenge_benchmarks"}
    # Pull the helper plus the constants/maps it closes over by exec'ing the
    # assignment block that defines the env table and family defaults.
    assigns = []
    fn = None
    for node in module.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in {
                    "MAX_CPU_CHALLENGE_BENCHMARKS",
                    "MAX_GPU_CHALLENGE_BENCHMARKS",
                    "_CHALLENGE_MAX_BENCHMARK_ENV",
                    "_GPU_CHALLENGE_IDS",
                }:
                    assigns.append(node)
        if isinstance(node, ast.FunctionDef) and node.name == "_max_challenge_benchmarks":
            fn = node
    if fn is None:
        raise RuntimeError("_max_challenge_benchmarks not found")
    ns: dict = {"os": os, "frozenset": frozenset}
    exec(compile(ast.Module(body=assigns + [fn], type_ignores=[]), str(path), "exec"), ns, ns)
    return ns["_max_challenge_benchmarks"]


def main() -> int:
    fn = _load_fn()
    os.environ["AUTOPILOT_MAX_CPU_CHALLENGE_BENCHMARKS"] = "16"
    os.environ["AUTOPILOT_MAX_GPU_CHALLENGE_BENCHMARKS"] = "12"
    for key in list(os.environ):
        if key.startswith("AUTOPILOT_MAX_CHALLENGE_BENCHMARKS_C"):
            del os.environ[key]

    # Re-load after env mutate: function reads os.environ at call time for
    # per-challenge keys, but family defaults were baked at exec — re-exec.
    fn = _load_fn()
    cases = [
        ("c005", None, 12, "gpu family fallback"),
        ("c001", None, 16, "cpu family fallback"),
        ("c005", "1", 1, "hypergraph override"),
        ("c006", "3", 3, "neuralnet override"),
        ("c004", "2", 2, "vector_search override"),
    ]
    failed = 0
    for cid, override, expect, label in cases:
        for key in list(os.environ):
            if key.startswith("AUTOPILOT_MAX_CHALLENGE_BENCHMARKS_C"):
                del os.environ[key]
        if override is not None:
            os.environ[f"AUTOPILOT_MAX_CHALLENGE_BENCHMARKS_{cid.upper()}"] = override
        got = fn(cid)
        ok = got == expect
        print(f"{'pass' if ok else 'FAIL'}: {label} -> {got} (want {expect})")
        if not ok:
            failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
