#!/usr/bin/env python3
"""GPU assign cap is 1-2 in-flight batches per card. CPU uses cpu_assign_inflight_cap."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load():
    path = pathlib.Path(__file__).resolve().parents[1] / "master" / "slave_manager.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    keep = [
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name
        in {
            "gpu_assign_inflight_cap",
            "_slave_work_profile",
            "_is_c3_dispatcher_slave",
            "_is_proof_batch_row",
            "select_gpu_kept_assigned",
        }
    ]
    names = {n.name for n in keep}
    want = {
        "gpu_assign_inflight_cap",
        "_slave_work_profile",
        "_is_c3_dispatcher_slave",
        "_is_proof_batch_row",
        "select_gpu_kept_assigned",
    }
    if names != want:
        raise RuntimeError(f"missing helpers: {want - names}")
    ns = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns


def main() -> int:
    ns = _load()
    cap = ns["gpu_assign_inflight_cap"]
    profile = ns["_slave_work_profile"]
    is_c3 = ns["_is_c3_dispatcher_slave"]
    failed = 0

    cases = [
        (cap(8, workers=1, route_cap=8) == 2, "default is run + prefetch"),
        (cap(13, workers=1, route_cap=16) == 2, "throughput warehouse 13 -> 2"),
        (cap(8, workers=4, route_cap=8) == 2, "advertised 4 workers still 2"),
        (cap(8, workers=4, route_cap=8, cfg={"gpu_inflight_per_worker": 1}) == 1, "can pin a card to 1"),
        (cap(8, workers=1, route_cap=8, cfg={"gpu_inflight_per_worker": 2}) == 2, "prefetch 2 is allowed"),
        (cap(8, workers=1, route_cap=8, cfg={"gpu_inflight_per_worker": 9}) == 2, "per-card cannot exceed 2"),
        (cap(4, workers=2, route_cap=8) == 2, "2 advertised workers still 2"),
        (cap(0, workers=1, route_cap=8) == 0, "load-shed 0 stays 0"),
        (cap(8, workers=None, route_cap=8) == 2, "missing workers still 2"),
        (cap(2, workers=6, route_cap=2, dispatcher=True) == 6, "C3 uses 6 workers not home clamp"),
        (cap(8, workers=6, route_cap=8, dispatcher=True) == 6, "C3 width is worker count"),
        (cap(8, workers=None, route_cap=8, dispatcher=True) == 2, "C3 without telem stays 2"),
        (cap(0, workers=6, route_cap=8, dispatcher=True) == 0, "C3 load-shed 0 stays 0"),
        (cap(8, workers=99, route_cap=8, dispatcher=True) == 16, "C3 workers hard-max 16"),
        (is_c3("pool-gpu-a330c544ec5b-c3-001"), "live C3 name is dispatcher"),
        (not is_c3("pool-gpu-9ffb87dc69ee-home-pica"), "home Pica is not C3"),
        (not is_c3("pool-gpu-a330c544ec5b-home-kevin-strix"), "home Strix is not C3"),
        (profile("pool-gpu-abc") == "gpu", "pool-gpu is GPU"),
        (profile("pool-cpu-abc") == "cpu", "pool-cpu is CPU"),
        (profile("c3-slave-1") == "cpu", "c3 leftover name is CPU"),
    ]
    keep_fn = ns["select_gpu_kept_assigned"]
    pending = {"start_time": None, "batch": {"sampled_nonces": None}}
    started = {"start_time": 1, "batch": {"sampled_nonces": None}}
    proof = {"start_time": 1, "batch": {"sampled_nonces": [1]}}
    kept, excess = keep_fn([pending, pending, started, proof], 2)
    cases.extend(
        [
            (len(kept) == 2 and len(excess) == 2, "GPU shed keeps exactly cap rows"),
            (
                proof in kept and started in kept,
                "GPU shed keeps started proof and started root first",
            ),
            (
                all(row.get("start_time") is None for row in excess),
                "GPU shed releases not-started prefetch first",
            ),
        ]
    )
    for ok, label in cases:
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
