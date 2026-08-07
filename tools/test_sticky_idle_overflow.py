#!/usr/bin/env python3
"""Checks for aged sticky leftover overflow."""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from master.proof_affinity import should_sticky_idle_overflow  # noqa: E402


def main() -> int:
    cases = [
        (
            should_sticky_idle_overflow(
                preferred_slave="a",
                preferred_inflight_on_job=False,
                has_unassigned=True,
                job_age_ms=5 * 60 * 1000,
                idle_ms=3 * 60 * 1000,
                preferred_online=True,
            )
            is True,
            "aged leftover with idle owner overflows",
        ),
        (
            should_sticky_idle_overflow(
                preferred_slave="a",
                preferred_inflight_on_job=True,
                has_unassigned=True,
                job_age_ms=5 * 60 * 1000,
                idle_ms=3 * 60 * 1000,
                preferred_online=True,
            )
            is False,
            "no overflow while preferred still working the job",
        ),
        (
            should_sticky_idle_overflow(
                preferred_slave="a",
                preferred_inflight_on_job=False,
                has_unassigned=True,
                job_age_ms=60_000,
                idle_ms=3 * 60 * 1000,
                preferred_online=True,
            )
            is False,
            "fresh jobs stay sticky",
        ),
        (
            should_sticky_idle_overflow(
                preferred_slave="a",
                preferred_inflight_on_job=False,
                has_unassigned=True,
                job_age_ms=5 * 60 * 1000,
                idle_ms=0,
                preferred_online=True,
            )
            is False,
            "idle overflow disabled when idle_ms=0",
        ),
        (
            should_sticky_idle_overflow(
                preferred_slave="a",
                preferred_inflight_on_job=False,
                has_unassigned=True,
                job_age_ms=5 * 60 * 1000,
                idle_ms=3 * 60 * 1000,
                preferred_online=False,
            )
            is False,
            "dark preferred uses normal dark takeover instead",
        ),
    ]
    failed = 0
    for ok, label in cases:
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
