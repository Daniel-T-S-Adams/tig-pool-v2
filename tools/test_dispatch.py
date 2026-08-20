#!/usr/bin/env python3
"""Unit checks for per-profile create-when-short dispatch."""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from master.dispatch import (  # noqa: E402
    extra_create_this_tick,
    next_create_profile,
    pin_expired,
    pin_limit,
    profile_needs_create,
    slave_work_profile,
)


def main() -> int:
    failed = 0

    def check(ok: bool, label: str) -> None:
        nonlocal failed
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1

    check(
        profile_needs_create(idle=28, claimable=0, unowned_jobs=4) is True,
        "idle CPUs with 0 claimable need a create",
    )
    check(
        profile_needs_create(idle=0, claimable=17, unowned_jobs=4) is False,
        "busy CPUs with a warehouse and unowned jobs do not create",
    )
    check(
        profile_needs_create(idle=0, claimable=0, unowned_jobs=0) is True,
        "busy fleet still wants one unowned job in the pipeline",
    )
    check(
        profile_needs_create(idle=4, claimable=0, unowned_jobs=2) is True,
        "empty GPUs need a create even if unowned jobs exist",
    )
    check(
        next_create_profile(cpu_short=True, gpu_short=False) == "cpu",
        "only CPU short → CPU create",
    )
    check(
        next_create_profile(cpu_short=False, gpu_short=True) == "gpu",
        "only GPU short → GPU create",
    )
    check(
        next_create_profile(cpu_short=True, gpu_short=True, last_profile="cpu") == "gpu",
        "both short after CPU → GPU next",
    )
    check(
        next_create_profile(cpu_short=True, gpu_short=True, last_profile="gpu") == "cpu",
        "both short after GPU → CPU next",
    )
    check(
        next_create_profile(cpu_short=False, gpu_short=False) == "",
        "neither short → skip create",
    )
    check(
        extra_create_this_tick(cpu_short=True, gpu_short=True) == 1,
        "both short → one extra create this tick",
    )
    check(
        extra_create_this_tick(cpu_short=True, gpu_short=False) == 0,
        "one profile short → no 59-job burst",
    )
    check(pin_limit(num_batches=12, idle_boxes=40) == 12, "pin at most this job's batches")
    check(pin_limit(num_batches=12, idle_boxes=3) == 3, "pin at most idle boxes")
    check(pin_limit(num_batches=12, idle_boxes=0) == 0, "no idle boxes → no pins")
    check(pin_expired(now_ms=40_000, pinned_at_ms=5_000) is True, "stale pin expires")
    check(pin_expired(now_ms=20_000, pinned_at_ms=5_000) is False, "fresh pin is kept")
    check(slave_work_profile("pool-gpu-abc") == "gpu", "pool-gpu is GPU")
    check(slave_work_profile("pool-cpu-abc") == "cpu", "pool-cpu is CPU")
    check(slave_work_profile("c3-slave-1") == "cpu", "c3-slave leftover name is CPU")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
