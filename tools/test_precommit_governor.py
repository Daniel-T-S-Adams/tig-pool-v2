#!/usr/bin/env python3
"""Lightweight unit checks for master precommit create-gate pure logic."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load_should_block():
    path = pathlib.Path(__file__).resolve().parents[1] / "master" / "precommit_manager.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    fn = None
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == "should_block_precommit_create":
            fn = node
            break
    if fn is None:
        raise RuntimeError("should_block_precommit_create not found")
    # Also need helpers referenced? Function is self-contained aside from settings default.
    # Replace settings-or path by requiring explicit settings in tests.
    code = ast.Module(body=[fn], type_ignores=[])
    ns = {}
    exec(compile(code, str(path), "exec"), ns, ns)
    return ns["should_block_precommit_create"]


def main() -> int:
    should_block = _load_should_block()
    settings = {
        "enabled": True,
        "max_roots_pending": 256,
        "min_root_ready_rate": 0.5,
        "min_samples": 5,
    }
    cases = [
        ((100, 20, 18), False, "healthy modest backlog"),
        ((256, 20, 18), True, "roots pending at threshold"),
        ((40, 20, 5), True, "low root ready rate with pending"),
        ((0, 20, 0), False, "no pending roots"),
        ((40, 3, 0), False, "below min samples"),
    ]
    failed = 0
    for args, expect_block, label in cases:
        blocked, reason = should_block(*args, settings)
        ok = blocked is expect_block
        status = "pass" if ok else "FAIL"
        print(f"{status}: {label} args={args} blocked={blocked} reason={reason!r}")
        if not ok:
            failed += 1
    disabled = {
        "enabled": False,
        "max_roots_pending": 1,
        "min_root_ready_rate": 0.99,
        "min_samples": 1,
    }
    blocked, _reason = should_block(999, 100, 0, disabled)
    ok = blocked is False
    print(f"{'pass' if ok else 'FAIL'}: disabled governor allows create")
    if not ok:
        failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
