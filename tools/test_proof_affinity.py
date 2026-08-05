#!/usr/bin/env python3
"""Unit checks for sticky root affinity / offline-owner helpers."""

from __future__ import annotations

import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from master.proof_affinity import (  # noqa: E402
    offline_owners,
    preferred_root_slave,
    should_skip_root_for_slave,
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
    cases.append((preferred_root_slave({"": 5, None: 9}) is None, "blank slaves ignored"))

    online = {"pool-cpu-a", "pool-cpu-b"}
    cases.append(
        (
            should_skip_root_for_slave("pool-cpu-b", "pool-cpu-a", online) is True,
            "skip non-owner while owner online",
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

    failed = 0
    for ok, label in cases:
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
