#!/usr/bin/env python3
"""Ops display: CPU/GPU slave counts must follow real hardware names."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load_fns():
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "pool_manager"
        / "pool"
        / "ops_metrics.py"
    )
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    wanted = {"slave_display_profile", "slave_display_cap"}
    nodes = [
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    found = {n.name for n in nodes}
    if found != wanted:
        raise RuntimeError(f"missing helpers: wanted {wanted}, found {found}")
    ns = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns


def main() -> int:
    ns = _load_fns()
    profile = ns["slave_display_profile"]
    cap = ns["slave_display_cap"]
    failed = 0

    def check(ok: bool, label: str, detail="") -> None:
        nonlocal failed
        print(f"{'pass' if ok else 'FAIL'}: {label}{(' -> ' + str(detail)) if detail else ''}")
        if not ok:
            failed += 1

    check(profile("pool-gpu-abc") == "gpu", "pool-gpu is GPU")
    check(profile("pool-gpu-wallet-c3-1") == "gpu", "multi-GPU dispatcher stays GPU")
    check(profile("pool-cpu-abc") == "cpu", "pool-cpu is CPU")
    check(profile("aws-cpu-slave-1") == "cpu", "aws-cpu-slave is CPU")
    check(profile("c3-slave-9") == "cpu", "c3-slave prefix is CPU, not GPU")
    check(profile("c3-slave-9", "gpu") == "gpu", "explicit worker_type gpu wins")
    check(profile("pool-gpu-abc", "cpu") == "cpu", "explicit worker_type cpu wins")

    check(cap(profile="cpu", num_workers=32, route_cap=512) == 1, "CPU fill cap is one job")
    check(cap(profile="gpu", num_workers=1, route_cap=64) == 1, "reported 1 GPU stays 1")
    check(cap(profile="gpu", num_workers=4, route_cap=64) == 4, "reported GPU workers win")
    check(cap(profile="gpu", num_workers=0, route_cap=8) == 8, "no telemetry uses GPU route cap")
    check(
        cap(profile="gpu", num_workers=0, route_cap=64, adaptive_max=24) == 24,
        "warehouse GPU route cap is clamped",
    )
    check(cap(profile="gpu", num_workers=0, route_cap=0) == 1, "unknown GPU concurrency is 1")
    check(
        cap(profile="gpu", num_workers=12, route_cap=8, is_multi_gpu=True) == 12,
        "multi-GPU dispatcher uses num_workers",
    )
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
