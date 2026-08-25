#!/usr/bin/env python3
"""get-batches shed + hang circuit breaker."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load_fns(*names: str):
    path = pathlib.Path(__file__).resolve().parents[1] / "master" / "slave_manager.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    keep = []
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            keep.append(node)
    if len(keep) != len(names):
        found = {n.name for n in keep}
        raise RuntimeError(f"missing functions: {set(names) - found}")
    ns = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns


def main() -> int:
    ns = _load_fns(
        "should_shed_get_batches_poll",
        "owner_idle_unlocks_sticky",
        "get_batches_assign_over_deadline",
        "get_batches_watchdog_should_exit",
    )
    should_shed = ns["should_shed_get_batches_poll"]
    owner_idle = ns["owner_idle_unlocks_sticky"]
    over_deadline = ns["get_batches_assign_over_deadline"]
    watchdog = ns["get_batches_watchdog_should_exit"]
    extra = 4
    hard = 16
    cases = [
        (
            should_shed(inflight=8, max_inflight=8, assigned_count=0, idle_extra=extra, hard_inflight=hard) is False,
            "idle slave is not shed at busy inflight cap",
        ),
        (
            should_shed(inflight=11, max_inflight=8, assigned_count=0, idle_extra=extra, hard_inflight=hard) is False,
            "idle slave is not shed inside idle extra window",
        ),
        (
            should_shed(inflight=12, max_inflight=8, assigned_count=0, idle_extra=extra, hard_inflight=hard) is False,
            "idle slave is not shed at cap plus idle extra",
        ),
        (
            should_shed(inflight=15, max_inflight=8, assigned_count=0, idle_extra=extra, hard_inflight=hard) is False,
            "idle slave is not shed just under hard inflight",
        ),
        (
            should_shed(inflight=16, max_inflight=8, assigned_count=0, idle_extra=extra, hard_inflight=hard) is True,
            "idle slave is shed at hard inflight ceiling",
        ),
        (
            should_shed(inflight=32, max_inflight=8, assigned_count=0, idle_extra=extra, hard_inflight=hard) is True,
            "idle slave is shed far above hard inflight",
        ),
        (
            should_shed(inflight=32, max_inflight=8, assigned_count=0, idle_extra=extra, hard_inflight=0) is False,
            "hard inflight 0 keeps legacy idle-never-shed",
        ),
        (
            should_shed(inflight=8, max_inflight=8, assigned_count=1, idle_extra=extra, hard_inflight=hard) is True,
            "busy slave is shed at inflight cap",
        ),
        (
            should_shed(inflight=7, max_inflight=8, assigned_count=3, idle_extra=extra, hard_inflight=hard) is False,
            "busy slave is not shed under inflight cap",
        ),
        (
            should_shed(inflight=0, max_inflight=8, assigned_count=0, idle_extra=extra, hard_inflight=hard) is False,
            "idle slave is not shed when quiet",
        ),
        (
            should_shed(
                inflight=8,
                max_inflight=8,
                assigned_count=0,
                idle_extra=extra,
                hard_inflight=hard,
                last_assign_ms=2000,
                slow_assign_ms=1500,
            )
            is True,
            "slow last assign sheds idle at busy inflight cap",
        ),
        (
            should_shed(
                inflight=7,
                max_inflight=8,
                assigned_count=0,
                idle_extra=extra,
                hard_inflight=hard,
                last_assign_ms=2000,
                slow_assign_ms=1500,
            )
            is False,
            "slow last assign still assigns under busy inflight cap",
        ),
        (
            over_deadline(started_mono=0.0, now_mono=1.5, deadline_ms=1500) is True,
            "assign deadline hits at 1500ms",
        ),
        (
            over_deadline(started_mono=0.0, now_mono=1.499, deadline_ms=1500) is False,
            "assign deadline is not early",
        ),
        (
            over_deadline(started_mono=0.0, now_mono=10.0, deadline_ms=0) is False,
            "assign deadline 0 disables the cutover",
        ),
        (
            watchdog(oldest_start_mono=0.0, now_mono=25.0, watchdog_ms=25000, inflight=1) is True,
            "watchdog exits after 25s wedged poll",
        ),
        (
            watchdog(oldest_start_mono=0.0, now_mono=24.9, watchdog_ms=25000, inflight=1) is False,
            "watchdog does not flap under 25s",
        ),
        (
            watchdog(oldest_start_mono=0.0, now_mono=60.0, watchdog_ms=25000, inflight=0) is False,
            "watchdog ignores idle process",
        ),
        (
            watchdog(oldest_start_mono=None, now_mono=60.0, watchdog_ms=25000, inflight=3) is False,
            "watchdog ignores missing start time",
        ),
        (
            owner_idle(0) is True,
            "zero inflight preferred unlocks sticky leftovers",
        ),
        (
            owner_idle(None) is True,
            "missing inflight preferred unlocks sticky leftovers",
        ),
        (
            owner_idle(1) is False,
            "busy preferred stays sticky",
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
