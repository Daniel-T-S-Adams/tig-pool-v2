#!/usr/bin/env python3
"""New job roots must match the in-memory feeder row shape."""

from __future__ import annotations

import ast
import pathlib


def _load_fn():
    path = pathlib.Path(__file__).resolve().parents[1] / "master" / "job_manager.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    keep = []
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == "new_root_memory_rows":
            keep.append(node)
            break
    if not keep:
        raise RuntimeError("missing new_root_memory_rows")
    ns: dict = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns["new_root_memory_rows"]


def main() -> int:
    fn = _load_fn()
    failed = 0
    rows = fn(
        benchmark_id="abc123",
        batch_size=100,
        num_nonces=250,
        num_batches=3,
        settings={"algorithm_id": "c001_a001", "challenge_id": "c001"},
        hyperparameters=None,
        fuel_budget=10,
        download_url="http://x",
        rand_hash="rh",
        challenge="satisfiability",
        algorithm="c001_a001",
        job_start_time=1_700_000_000_000,
    )
    ok = len(rows) == 3
    print(f"{'pass' if ok else 'FAIL'}: 3 root rows for 250/100")
    if not ok:
        failed += 1
    ok = all(row.get("slave") is None and row.get("end_time") is None for row in rows)
    print(f"{'pass' if ok else 'FAIL'}: new roots start unassigned")
    if not ok:
        failed += 1
    batch = rows[2]["batch"]
    ok = (
        batch["id"] == "abc123_2"
        and batch["batch_idx"] == 2
        and batch["start_nonce"] == 200
        and batch["num_nonces"] == 50
        and batch["sampled_nonces"] is None
        and batch["settings"]["algorithm_id"] == "c001_a001"
    )
    print(f"{'pass' if ok else 'FAIL'}: last batch is a 50-nonce leftover crumb")
    if not ok:
        failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
