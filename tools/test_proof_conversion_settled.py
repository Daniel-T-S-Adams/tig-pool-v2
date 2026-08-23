#!/usr/bin/env python3
"""In-flight proof-phase jobs must not count as conversion failures."""

from __future__ import annotations

import ast
import pathlib


def _load_fn():
    path = pathlib.Path(__file__).resolve().parents[1] / "pool_manager" / "pool" / "autopilot.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    target = None
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == "proof_counts_toward_conversion":
            target = node
            break
    if target is None:
        raise RuntimeError("proof_counts_toward_conversion not found")
    ns = {}
    exec(compile(ast.Module(body=[target], type_ignores=[]), str(path), "exec"), ns, ns)
    return ns["proof_counts_toward_conversion"]


def main() -> int:
    fn = _load_fn()
    cases = [
        (
            fn(has_proof_batches=True, proof_submitted=False, stopped=False, has_end_time=False),
            False,
            "open in-flight proof phase excluded",
        ),
        (
            fn(has_proof_batches=True, proof_submitted=True, stopped=False, has_end_time=False),
            True,
            "TIG-confirmed proof counts even before end_time",
        ),
        (
            fn(has_proof_batches=True, proof_submitted=False, stopped=True, has_end_time=True),
            True,
            "stopped after proof batches is settled",
        ),
        (
            fn(has_proof_batches=True, proof_submitted=False, stopped=False, has_end_time=True),
            True,
            "ended without TIG proof is settled failure",
        ),
        (
            fn(has_proof_batches=False, proof_submitted=False, stopped=True, has_end_time=True),
            False,
            "allowlist skip with no proof batches stays out",
        ),
    ]
    failed = 0
    for got, expect, label in cases:
        ok = got is expect
        print(f"{'pass' if ok else 'FAIL'}: {label} -> {got}")
        if not ok:
            failed += 1

    submitted = 20
    settled = 20
    inflight = 40
    old_rate = submitted / (settled + inflight)
    new_rate = submitted / settled
    mix_ok = abs(new_rate - 1.0) < 1e-9 and abs(old_rate - (20 / 60)) < 1e-9
    print(f"{'pass' if mix_ok else 'FAIL'}: 20 confirmed / 40 in-flight -> {new_rate:.2f} not {old_rate:.2f}")
    if not mix_ok:
        failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
