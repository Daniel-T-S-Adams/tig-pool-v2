#!/usr/bin/env python3
"""Unit checks for adaptive CPU batch_size chooser (pure logic)."""

from __future__ import annotations

import ast
import math
import pathlib
import sys


def _load_fn(name: str):
    path = pathlib.Path(__file__).resolve().parents[1] / "master" / "job_manager.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    keep = []
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            keep.append(node)
    if not keep:
        raise RuntimeError(f"missing function: {name}")
    ns = {"math": math, "Dict": dict, "Tuple": tuple}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns[name]


def main() -> int:
    choose = _load_fn("choose_adaptive_cpu_batch_size")
    fit = _load_fn("fit_batch_size_to_job_cap")
    failed = 0

    def check(ok: bool, label: str) -> None:
        nonlocal failed
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1

    # Kill switch off → always configured
    bs, meta = choose(128, 2048, cpu_unassigned_claimable=0, online_idle_cpu_slaves=20, enabled=False)
    check(bs == 128 and meta["mode"] == "disabled", "disabled keeps configured")

    # Underfed: claimable 0, idle 20 → shrink to fan out ~20 batches
    # 2048/20 = 102.4 → 103, but min with configured 128 → 103; lo may be 8
    bs, meta = choose(
        128,
        2048,
        cpu_unassigned_claimable=0,
        online_idle_cpu_slaves=20,
        enabled=True,
        min_batch=8,
        max_batch=128,
        max_job_batches=256,
        target_batches_per_idle=1.0,
    )
    check(meta["mode"] == "shrink_for_idle", f"underfed mode ({meta['mode']})")
    check(bs < 128, f"underfed shrinks batch_size ({bs})")
    check(math.ceil(2048 / bs) >= 20, f"underfed fans out enough batches ({math.ceil(2048 / bs)})")

    # Never exceed max_job_batches (job would be created stopped)
    bs, meta = choose(
        128,
        10000,
        cpu_unassigned_claimable=0,
        online_idle_cpu_slaves=500,
        enabled=True,
        min_batch=8,
        max_batch=128,
        max_job_batches=64,
        target_batches_per_idle=1.0,
    )
    batches = math.ceil(10000 / bs)
    check(batches <= 64, f"respects max_job_batches ({batches} <= 64, bs={bs})")
    check(bs >= math.ceil(10000 / 64), f"floor from max_job_batches (bs={bs})")

    # Surplus claimable → grow toward max
    bs, meta = choose(
        32,
        2048,
        cpu_unassigned_claimable=40,
        online_idle_cpu_slaves=10,
        enabled=True,
        min_batch=8,
        max_batch=128,
        max_job_batches=256,
    )
    check(meta["mode"] == "grow_for_surplus", f"surplus mode ({meta['mode']})")
    check(bs > 32, f"surplus grows batch_size ({bs})")

    # Balanced → keep configured (within clamps)
    bs, meta = choose(
        64,
        2048,
        cpu_unassigned_claimable=10,
        online_idle_cpu_slaves=10,
        enabled=True,
        min_batch=8,
        max_batch=128,
        max_job_batches=256,
    )
    check(meta["mode"] == "keep_configured", f"balanced mode ({meta['mode']})")
    check(bs == 64, f"balanced keeps configured ({bs})")

    # Allowlisted SAT 100k must fit 256 batches instead of being born stopped.
    sat_bs = fit(32, 100000, 256)
    sat_batches = math.ceil(100000 / sat_bs)
    check(sat_bs >= math.ceil(100000 / 256), f"SAT 100k floor ({sat_bs})")
    check(sat_batches <= 256, f"SAT 100k batches {sat_batches} <= 256")
    check(fit(32, 2048, 256) == 32, "small jobs keep configured batch_size")

    # GPU path not tested here — caller gates on CPU challenge ids.

    print(f"\n{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
