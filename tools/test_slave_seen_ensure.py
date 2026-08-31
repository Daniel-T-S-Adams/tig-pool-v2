#!/usr/bin/env python3
"""ensure_slave_seen_table is a one-shot and does not raise on lock failure."""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from master.proof_affinity import (  # noqa: E402
    ensure_slave_seen_table,
    reset_slave_seen_ready_for_tests,
)


def main() -> int:
    reset_slave_seen_ready_for_tests()
    calls: list[str] = []

    def execute(sql, *args, **kwargs):
        calls.append(str(sql))

    ensure_slave_seen_table(execute)
    first = len(calls)
    ensure_slave_seen_table(execute)
    ok = [
        (first >= 1, "first ensure runs DDL"),
        (len(calls) == first, "second ensure is a no-op"),
    ]

    reset_slave_seen_ready_for_tests()

    def boom(sql, *args, **kwargs):
        raise RuntimeError("lock timeout")

    ensure_slave_seen_table(boom)
    retried: list[str] = []

    def execute2(sql, *args, **kwargs):
        retried.append(str(sql))

    ensure_slave_seen_table(execute2)
    ok.append((len(retried) >= 1, "failed ensure is retried later"))

    failed = 0
    for passed, label in ok:
        print(f"{'pass' if passed else 'FAIL'}: {label}")
        if not passed:
            failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
