#!/usr/bin/env python3
"""Last slave proof batch kicks job merkle assembly."""

from __future__ import annotations

import ast
import pathlib


def _load_fns(*names: str):
    path = pathlib.Path(__file__).resolve().parents[1] / "master" / "job_manager.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    keep = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name in names]
    if {n.name for n in keep} != set(names):
        raise RuntimeError(f"missing: {set(names) - {n.name for n in keep}}")
    ns: dict = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns


def main() -> int:
    failed = 0

    def check(ok: bool, label: str) -> None:
        nonlocal failed
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1

    kick = _load_fns("should_kick_proof_assembly")["should_kick_proof_assembly"]
    check(
        kick(
            merkle_root_ready=True,
            merkle_proofs_ready=False,
            stopped=False,
            all_batches_ready=True,
        )
        is True,
        "last batch with root ready kicks assembly",
    )
    check(
        kick(
            merkle_root_ready=True,
            merkle_proofs_ready=False,
            stopped=False,
            all_batches_ready=False,
        )
        is False,
        "missing proof batch does not kick",
    )
    check(
        kick(
            merkle_root_ready=False,
            merkle_proofs_ready=False,
            stopped=False,
            all_batches_ready=True,
        )
        is False,
        "no merkle root yet: wait for fallback run()",
    )
    check(
        kick(
            merkle_root_ready=True,
            merkle_proofs_ready=True,
            stopped=False,
            all_batches_ready=True,
        )
        is False,
        "already assembled does not kick again",
    )

    root = pathlib.Path(__file__).resolve().parents[1]
    slave = (root / "master" / "slave_manager.py").read_text(encoding="utf-8")
    job = (root / "master" / "job_manager.py").read_text(encoding="utf-8")
    check(
        "assemble_ready_proofs" in slave
        and "target=assemble_ready_proofs" in slave,
        "submit-batch-proofs kicks assemble_ready_proofs",
    )
    check(
        "assemble_ready_proofs()" in job
        and "def assemble_ready_proofs" in job,
        "job_manager.run still assembles as fallback",
    )
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
