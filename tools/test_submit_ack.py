#!/usr/bin/env python3
"""Submit 200 bodies expose accepted vs duplicate_accepted."""

from __future__ import annotations

import ast
import pathlib


def _load():
    path = pathlib.Path(__file__).resolve().parents[1] / "master" / "slave_manager.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    keep = [
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "submit_ack"
    ]
    if len(keep) != 1:
        raise RuntimeError("submit_ack not found")
    ns = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns["submit_ack"]


def main() -> int:
    ack = _load()
    cases = [
        (
            ack("accepted") == {"status": "OK", "outcome": "accepted"},
            "new write is accepted",
        ),
        (
            ack("duplicate_accepted")
            == {"status": "OK", "outcome": "duplicate_accepted"},
            "already-ready is duplicate_accepted",
        ),
        (
            ack("duplicate_accepted", note="stale_closed_batch")
            == {
                "status": "OK",
                "outcome": "duplicate_accepted",
                "note": "stale_closed_batch",
            },
            "stale-closed keeps note and is duplicate_accepted",
        ),
        (
            ack("duplicate_accepted", note="stale_assignment")
            == {
                "status": "OK",
                "outcome": "duplicate_accepted",
                "note": "stale_assignment",
            },
            "stale-assignment keeps note and is duplicate_accepted",
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
