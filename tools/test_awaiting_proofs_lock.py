#!/usr/bin/env python3
"""Logic checks for when proof-only awaiting lock should apply."""

from __future__ import annotations

import ast
import pathlib


def _load_proof_fns(*names: str):
    path = pathlib.Path(__file__).resolve().parents[1] / "master" / "proof_affinity.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    keep = []
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            keep.append(node)
    if {n.name for n in keep} != set(names):
        raise RuntimeError(f"missing: {set(names) - {n.name for n in keep}}")
    ns = {
        "Optional": __import__("typing").Optional,
        "SAMPLING_GAP_RESERVE_MS": 180_000,
    }
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns


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
    gap_ns = _load_proof_fns(
        "job_counts_as_sampling_gap_reserve",
        "sampling_gap_root_intake_cap",
    )
    gap_job = gap_ns["job_counts_as_sampling_gap_reserve"]
    intake = gap_ns["sampling_gap_root_intake_cap"]
    cases.extend(
        [
            (
                gap_job(
                    has_unfinished_roots=False,
                    merkle_proofs_ready=False,
                    proof_batch_count=0,
                    owner_has_ready_root=True,
                    latest_ready_root_age_ms=60_000,
                    reserve_ms=180_000,
                )
                is True,
                "fresh last root reserves a seat for TIG samples",
            ),
            (
                gap_job(
                    has_unfinished_roots=False,
                    merkle_proofs_ready=False,
                    proof_batch_count=0,
                    owner_has_ready_root=True,
                    latest_ready_root_age_ms=10 * 60 * 1000,
                    reserve_ms=180_000,
                )
                is False,
                "stale sampling gap releases the reserved seat",
            ),
            (
                gap_job(
                    has_unfinished_roots=False,
                    merkle_proofs_ready=False,
                    proof_batch_count=2,
                    owner_has_ready_root=True,
                    latest_ready_root_age_ms=10_000,
                    reserve_ms=180_000,
                )
                is False,
                "once proofs_batch exists the reserve ends",
            ),
            (
                intake(max_concurrent=1, assigned=0, gap_jobs=1) == 0,
                "1-seat box holds the seat instead of taking a new job",
            ),
            (
                intake(max_concurrent=6, assigned=5, gap_jobs=1) == 5,
                "XL keeps 5 running jobs and does not refill the finished seat",
            ),
            (
                intake(max_concurrent=6, assigned=0, gap_jobs=1) == 5,
                "XL with one finished job may still pack 5 other seats",
            ),
            (
                intake(max_concurrent=2, assigned=1, gap_jobs=1) == 1,
                "GPU prefetch seat stays reserved for the proof",
            ),
            (
                intake(max_concurrent=1, assigned=0, gap_jobs=1) == 0,
                "1-wide GPU without keep_last_seat parks the only seat",
            ),
            (
                intake(max_concurrent=1, assigned=0, gap_jobs=1, keep_last_seat=True) == 1,
                "idle 1-wide GPU keeps one leftover seat during sampling gap",
            ),
        ]
    )
    failed = 0
    for ok, label in cases:
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
