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
    latest_ready_root_age_ms: int | None = None,
    sampling_gap_lock_ms: int = 0,
) -> bool:
    """Mirrors master._slave_awaiting_proofs job filter (no DB)."""
    if not has_ready_roots:
        return False
    if not merkle_root_ready or merkle_proofs_ready:
        return False
    if proof_batch_count > 0:
        return my_open_proofs > 0
    # Sampling gap lock disabled unless sampling_gap_lock_ms > 0.
    if sampling_gap_lock_ms <= 0:
        return False
    if latest_ready_root_age_ms is None:
        return True
    return latest_ready_root_age_ms <= sampling_gap_lock_ms


def has_proof_work_for_root_cap(
    *,
    assigned_proofs: int,
    own_proof_work: int,
    awaiting_proofs: bool,
) -> bool:
    """root_cap reservation ignores awaiting-only (mirrors slave_manager)."""
    return bool(assigned_proofs or own_proof_work)


def main() -> int:
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
                latest_ready_root_age_ms=60_000,
            )
            is False,
            "default sampling gap does not lock",
        ),
        (
            should_awaiting_proof_lock(
                has_ready_roots=True,
                merkle_root_ready=True,
                merkle_proofs_ready=False,
                proof_batch_count=0,
                my_open_proofs=0,
                latest_ready_root_age_ms=60_000,
                sampling_gap_lock_ms=5 * 60 * 1000,
            )
            is True,
            "optional fresh sampling gap still locks when enabled",
        ),
        (
            should_awaiting_proof_lock(
                has_ready_roots=True,
                merkle_root_ready=True,
                merkle_proofs_ready=False,
                proof_batch_count=0,
                my_open_proofs=0,
                latest_ready_root_age_ms=10 * 60 * 1000,
                sampling_gap_lock_ms=5 * 60 * 1000,
            )
            is False,
            "stale sampling gap unlocks when optional lock enabled",
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
        (
            has_proof_work_for_root_cap(
                assigned_proofs=0, own_proof_work=0, awaiting_proofs=True
            )
            is False,
            "awaiting-only does not zero root_cap",
        ),
        (
            has_proof_work_for_root_cap(
                assigned_proofs=0, own_proof_work=1, awaiting_proofs=True
            )
            is True,
            "own proof work still zeros root_cap",
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
