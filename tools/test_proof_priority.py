#!/usr/bin/env python3
"""Unit checks for proof-priority kept-assignment selection."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load_fn():
    path = pathlib.Path(__file__).resolve().parents[1] / "master" / "slave_manager.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    helpers = []
    target = None
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in {
            "_is_proof_batch_row",
            "select_kept_assigned_batches",
        }:
            if node.name == "select_kept_assigned_batches":
                target = node
            helpers.append(node)
    if target is None:
        raise RuntimeError("select_kept_assigned_batches not found")
    ns = {}
    exec(compile(ast.Module(body=helpers, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns["select_kept_assigned_batches"]


def _row(bid: str, idx: int, proof: bool) -> dict:
    return {
        "batch": {
            "benchmark_id": bid,
            "batch_idx": idx,
            "sampled_nonces": [1] if proof else None,
        }
    }


def main() -> int:
    fn = _load_fn()
    assigned = [
        _row("a", 0, False),
        _row("a", 1, False),
        _row("a", 2, False),
        _row("p", 0, True),
        _row("p", 1, True),
    ]
    kept, excess = fn(
        assigned,
        4,
        proof_priority=True,
        max_roots_while_proofs=2,
    )
    kept_proofs = sum(1 for b in kept if b["batch"]["sampled_nonces"] is not None)
    kept_roots = len(kept) - kept_proofs
    cases = [
        (len(kept) == 4, f"kept size 4 got {len(kept)}"),
        (kept_proofs == 2, f"keep both proofs got {kept_proofs}"),
        (kept_roots == 2, f"root cap 2 got {kept_roots}"),
        (len(excess) == 1, f"one excess root got {len(excess)}"),
        (excess[0]["batch"]["sampled_nonces"] is None, "excess should be a root"),
    ]
    # Sticky lifecycle default: proof-only (0 roots) while proofs owed.
    kept0, excess0 = fn(
        assigned,
        4,
        proof_priority=True,
        max_roots_while_proofs=0,
    )
    cases.append(
        (
            sum(1 for b in kept0 if b["batch"]["sampled_nonces"] is not None) == 2,
            "proof-only keeps both proofs",
        )
    )
    cases.append(
        (
            sum(1 for b in kept0 if b["batch"]["sampled_nonces"] is None) == 0,
            "proof-only keeps zero roots",
        )
    )
    cases.append((len(excess0) == 3, f"proof-only excess 3 roots got {len(excess0)}"))

    # Awaiting-proofs gap: no proof batches yet, still apply root cap.
    roots_only = [_row("a", 0, False), _row("a", 1, False), _row("a", 2, False)]
    kept_gap, excess_gap = fn(
        roots_only,
        3,
        proof_priority=True,
        max_roots_while_proofs=0,
    )
    cases.append((len(kept_gap) == 0, f"awaiting gap keeps 0 got {len(kept_gap)}"))
    cases.append(
        (len(excess_gap) == 3, f"awaiting gap releases all roots got {len(excess_gap)}")
    )

    # Finish-own-job exception: keep sticky leftover roots while proof-only.
    kept_fin, excess_fin = fn(
        assigned,
        4,
        proof_priority=True,
        max_roots_while_proofs=0,
        always_keep_root_benchmarks={"a"},
    )
    cases.append(
        (
            sum(1 for b in kept_fin if b["batch"]["sampled_nonces"] is not None) == 1,
            "finish-keep keeps one proof after last leftovers",
        )
    )
    cases.append(
        (
            sum(1 for b in kept_fin if b["batch"]["benchmark_id"] == "a") == 3,
            "finish-keep retains all own-job last leftovers",
        )
    )
    cases.append((len(excess_fin) == 1, f"finish-keep excess 1 got {len(excess_fin)}"))

    kept_one, excess_one = fn(
        [_row("last", 0, False), _row("p", 0, True)],
        1,
        proof_priority=True,
        max_roots_while_proofs=0,
        always_keep_root_benchmarks={"last"},
    )
    cases.append(
        (
            [b["batch"]["benchmark_id"] for b in kept_one] == ["last"]
            and [b["batch"]["benchmark_id"] for b in excess_one] == ["p"],
            "1-seat box keeps the last leftover and releases the proof",
        )
    )

    # Without proof priority, fill capacity preferring proofs first.
    kept2, excess2 = fn(assigned, 3, proof_priority=False, max_roots_while_proofs=2)
    cases.append((len(kept2) == 3, f"no-priority kept 3 got {len(kept2)}"))
    cases.append(
        (
            sum(1 for b in kept2 if b["batch"]["sampled_nonces"] is not None) == 2,
            "no-priority still keeps proofs first",
        )
    )

    failed = 0
    for ok, label in cases:
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
