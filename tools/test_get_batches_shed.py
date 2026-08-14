#!/usr/bin/env python3
"""Idle slaves must never be shed from get-batches assignment."""

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
    ns = _load_fns("should_shed_get_batches_poll", "owner_idle_unlocks_sticky")
    should_shed = ns["should_shed_get_batches_poll"]
    owner_idle = ns["owner_idle_unlocks_sticky"]
    cases = [
        (
            should_shed(inflight=8, max_inflight=8, assigned_count=0) is False,
            "idle slave is never shed at inflight cap",
        ),
        (
            should_shed(inflight=32, max_inflight=8, assigned_count=0) is False,
            "idle slave is never shed above inflight cap",
        ),
        (
            should_shed(inflight=8, max_inflight=8, assigned_count=1) is True,
            "busy slave is shed at inflight cap",
        ),
        (
            should_shed(inflight=7, max_inflight=8, assigned_count=3) is False,
            "busy slave is not shed under inflight cap",
        ),
        (
            should_shed(inflight=0, max_inflight=8, assigned_count=0) is False,
            "idle slave is not shed when quiet",
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
