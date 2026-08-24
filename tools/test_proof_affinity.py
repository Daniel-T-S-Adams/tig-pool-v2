#!/usr/bin/env python3
"""Unit checks for sticky root affinity / offline-owner helpers."""

from __future__ import annotations

import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from master.proof_affinity import (  # noqa: E402
    canonicalize_pool_slave_name,
    offline_owners,
    preferred_root_slave,
    should_hold_unowned_gpu_for_idle,
    should_skip_root_for_slave,
    should_sticky_leftover_fanout,
)


def main() -> int:
    cases = []

    cases.append(
        (
            preferred_root_slave({"a": 1, "b": 3, "c": 2}) == "b",
            "preferred picks highest score",
        )
    )
    cases.append(
        (
            preferred_root_slave({"a": 100, "b": 1}) == "a",
            "ready-heavy score preferred",
        )
    )
    cases.append((preferred_root_slave({}) is None, "empty scores -> None"))
    cases.append(
        (
            canonicalize_pool_slave_name("pool-cpu6a10bcc3bb93-home-9654-14")
            == "pool-cpu-6a10bcc3bb93-home-9654-14",
            "insert hyphen after pool-cpu",
        )
    )
    cases.append(
        (
            canonicalize_pool_slave_name("pool-cpu-6a10bcc3bb93-home-9654-14")
            == "pool-cpu-6a10bcc3bb93-home-9654-14",
            "correct pool-cpu name unchanged",
        )
    )
    cases.append(
        (
            canonicalize_pool_slave_name("pool-gpu6a10bcc3bb93-aws-1")
            == "pool-gpu-6a10bcc3bb93-aws-1",
            "insert hyphen after pool-gpu",
        )
    )
    cases.append((canonicalize_pool_slave_name(None) is None, "None UA stays None"))
    cases.append((preferred_root_slave({"": 5, None: 9}) is None, "blank slaves ignored"))

    online = {"pool-cpu-a", "pool-cpu-b"}
    cases.append(
        (
            should_skip_root_for_slave("pool-cpu-b", "pool-cpu-a", online) is True,
            "skip non-owner while owner online under cap",
        )
    )
    cases.append(
        (
            should_skip_root_for_slave("pool-cpu-a", "pool-cpu-a", online) is False,
            "owner may take own roots",
        )
    )
    cases.append(
        (
            should_skip_root_for_slave("pool-cpu-b", "pool-cpu-a", {"pool-cpu-b"}) is False,
            "allow takeover when owner dark",
        )
    )
    cases.append(
        (
            should_skip_root_for_slave(
                "pool-cpu-b",
                "pool-cpu-a",
                online,
                preferred_at_cap=True,
            )
            is False,
            "allow overflow when owner online but at adaptive cap",
        )
    )
    cases.append(
        (
            should_skip_root_for_slave(
                "pool-cpu-b",
                "pool-cpu-a",
                online,
                preferred_at_cap=False,
            )
            is True,
            "still sticky when owner online under cap",
        )
    )
    cases.append(
        (
            should_skip_root_for_slave(
                "pool-cpu-b", "pool-cpu-a", online, sticky_enabled=False
            )
            is False,
            "sticky off allows any slave",
        )
    )
    cases.append(
        (
            should_skip_root_for_slave("pool-cpu-b", None, online) is False,
            "no preference -> no skip",
        )
    )
    cases.append(
        (
            should_skip_root_for_slave(
                "pool-cpu-b",
                "pool-cpu-a",
                online,
                poller_idle=True,
            )
            is False,
            "idle poller may take leftovers instead of no-batches",
        )
    )
    cases.append(
        (
            should_sticky_leftover_fanout(
                unassigned_on_job=98,
                leftover_keep=4,
                idle_peers=10,
            )
            is True,
            "large leftover pile fans out when peers are idle",
        )
    )
    cases.append(
        (
            should_sticky_leftover_fanout(
                unassigned_on_job=8,
                leftover_keep=4,
                idle_peers=10,
                preferred_inflight_total=3,
                preferred_cap=32,
            )
            is True,
            "typical job fans out even when owner still has remaining cap",
        )
    )
    cases.append(
        (
            should_sticky_leftover_fanout(
                unassigned_on_job=8,
                leftover_keep=4,
                idle_peers=0,
                preferred_inflight_total=3,
                preferred_cap=32,
            )
            is False,
            "leftovers stay sticky when the fleet is already busy",
        )
    )
    cases.append(
        (
            should_sticky_leftover_fanout(
                unassigned_on_job=4,
                leftover_keep=4,
                idle_peers=10,
            )
            is False,
            "keep-count leftovers stay exclusive",
        )
    )
    cases.append(
        (
            should_sticky_leftover_fanout(
                unassigned_on_job=1,
                leftover_keep=4,
                idle_peers=0,
            )
            is True,
            "last leftover unlocks so any CPU can finish the job",
        )
    )

    cases.append(
        (
            offline_owners(["pool-cpu-a", "pool-cpu-z"], online) == ["pool-cpu-z"],
            "offline_owners lists dark only",
        )
    )
    cases.append(
        (
            offline_owners(["pool-cpu-a", ""], online) == [],
            "blank owners ignored",
        )
    )
    cases.append(
        (
            should_hold_unowned_gpu_for_idle(
                algorithm_id="c006_a1",
                preferred_slave=None,
                slave_inflight=3,
            )
            is True,
            "busy GPU cannot take unowned GPU job",
        )
    )
    cases.append(
        (
            should_hold_unowned_gpu_for_idle(
                algorithm_id="c006_a1",
                preferred_slave=None,
                slave_inflight=0,
            )
            is False,
            "idle GPU may take unowned GPU job",
        )
    )
    cases.append(
        (
            should_hold_unowned_gpu_for_idle(
                algorithm_id="c006_a1",
                preferred_slave="pool-gpu-a",
                slave_inflight=3,
            )
            is False,
            "owned GPU jobs stay on sticky path",
        )
    )
    cases.append(
        (
            should_hold_unowned_gpu_for_idle(
                algorithm_id="c001_a1",
                preferred_slave=None,
                slave_inflight=3,
            )
            is False,
            "CPU jobs are not held for idle GPUs",
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
