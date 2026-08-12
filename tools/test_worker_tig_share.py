#!/usr/bin/env python3
"""Unit checks for display-only per-slave TIG allocation."""

from __future__ import annotations

import pathlib
import sys


def main() -> int:
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "pool_manager"
        / "pool"
        / "worker_earnings.py"
    ).read_text(encoding="utf-8")
    start = src.index("def allocate_tig")
    end = src.index("\n\ndef round_start_ms")
    ns: dict = {}
    exec(src[start:end], ns, ns)
    fn = ns["allocate_tig"]

    cases = [
        ((50, 100, 10.0), 5.0, "half the nonces gets half the TIG"),
        ((0, 100, 10.0), 0.0, "zero work earns zero"),
        ((10, 0, 10.0), 0.0, "no pool nonces earns zero"),
        ((10, 100, 0.0), 0.0, "no pool TIG yet earns zero"),
        ((1, 3, 1.0), round(1.0 / 3, 6), "rounding to 6 dp"),
    ]
    failed = 0
    for args, expect, label in cases:
        got = fn(*args)
        ok = got == expect
        print(f"{'pass' if ok else 'FAIL'}: {label} -> {got} (want {expect})")
        failed += 0 if ok else 1
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
