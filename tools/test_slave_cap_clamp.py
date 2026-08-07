#!/usr/bin/env python3
"""Env slave-cap ceilings must clamp, not ratchet upward with live values."""

from __future__ import annotations

import ast
import pathlib


def _load():
    path = pathlib.Path(__file__).resolve().parents[1] / "pool_manager" / "pool" / "autopilot.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    wanted = {
        "_cpu_slave_cap_bounds",
        "_gpu_slave_cap_bounds",
        "_clamp_cpu_slave_cap",
        "_clamp_gpu_slave_cap",
        "_target_adaptive_slave_caps",
    }
    body = [
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    missing = wanted - {n.name for n in body}
    if missing:
        raise RuntimeError(f"missing helpers: {sorted(missing)}")

    ns = {
        "MAX_CPU_SLAVE_CAP": 1,
        "MIN_CPU_SLAVE_CAP": 4,
        "MAX_GPU_SLAVE_CAP": 2,
        "MIN_GPU_SLAVE_CAP": 1,
        "PRODUCTIVE_IDLE_CPU_SCALE_MIN": 5,
        "PRODUCTIVE_IDLE_GPU_SCALE_MIN": 1,
        "CAP_SCALE_COMPLETIONS_PER_STEP": 1,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns


def main() -> int:
    ap = _load()
    cases = [
        (ap["_clamp_cpu_slave_cap"](4) == 1, "cpu clamp 4 -> 1 when max=1"),
        (ap["_clamp_cpu_slave_cap"](1) == 1, "cpu clamp keeps 1"),
        (ap["_cpu_slave_cap_bounds"]() == (1, 1), "cpu floor collapses to ceiling"),
        (ap["_clamp_gpu_slave_cap"](12) == 2, "gpu clamp 12 -> 2"),
    ]
    capacity = {
        "active_cpu": 10,
        "active_gpu": 1,
        "productive_idle_cpu": 10,
        "productive_idle_gpu": 1,
        "cpu_completed_recent": 100,
        "gpu_completed_recent": 100,
        "cpu_pressure": 10,
        "gpu_pressure": 1,
        "current_adaptive_caps": {
            "enabled": True,
            "cpu_max_cap": 4,
            "gpu_max_cap": 12,
            "cpu_min_cap": 1,
            "gpu_min_cap": 1,
        },
    }
    proposed = ap["_target_adaptive_slave_caps"](capacity)
    cases.append(
        (
            proposed.get("cpu_max_cap") == 1,
            f"target cpu over-ceiling -> 1 got {proposed.get('cpu_max_cap')}",
        )
    )
    cases.append(
        (
            proposed.get("gpu_max_cap") == 2,
            f"target gpu over-ceiling -> 2 got {proposed.get('gpu_max_cap')}",
        )
    )

    failed = 0
    for ok, label in cases:
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
