#!/usr/bin/env python3
"""Logic checks for when proof-only awaiting lock should apply."""

from __future__ import annotations


def should_awaiting_proof_lock(
    *,
    has_ready_roots: bool,
    merkle_root_ready: bool,
    merkle_proofs_ready: bool,
    proof_batch_count: int,
    my_open_proofs: int,
) -> bool:
    """Mirrors master._slave_awaiting_proofs job filter (no DB)."""
    if not has_ready_roots:
        return False
    if not merkle_root_ready or merkle_proofs_ready:
        return False
    if proof_batch_count <= 0:
        return True  # sampling gap
    return my_open_proofs > 0


def main() -> int:
    # pica28 live case: split job, proofs exist, none owed by this slave
    cases = [
        (
            should_awaiting_proof_lock(
                has_ready_roots=True,
                merkle_root_ready=True,
                merkle_proofs_ready=False,
                proof_batch_count=3,
                my_open_proofs=0,
            )
            is False,
            "split contributor with no open proofs is free",
        ),
        (
            should_awaiting_proof_lock(
                has_ready_roots=True,
                merkle_root_ready=True,
                merkle_proofs_ready=False,
                proof_batch_count=3,
                my_open_proofs=1,
            )
            is True,
            "slave with open proofs stays locked",
        ),
        (
            should_awaiting_proof_lock(
                has_ready_roots=True,
                merkle_root_ready=True,
                merkle_proofs_ready=False,
                proof_batch_count=0,
                my_open_proofs=0,
            )
            is True,
            "sampling gap still locks root owner",
        ),
        (
            should_awaiting_proof_lock(
                has_ready_roots=True,
                merkle_root_ready=True,
                merkle_proofs_ready=True,
                proof_batch_count=3,
                my_open_proofs=0,
            )
            is False,
            "proofs done unlocks",
        ),
        (
            should_awaiting_proof_lock(
                has_ready_roots=False,
                merkle_root_ready=True,
                merkle_proofs_ready=False,
                proof_batch_count=0,
                my_open_proofs=0,
            )
            is False,
            "no ready roots -> no lock",
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
