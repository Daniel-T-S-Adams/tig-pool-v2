#!/usr/bin/env python3
"""Unit checks for display-only per-slave TIG allocation."""

from __future__ import annotations

import pathlib
import sys


def main() -> int:
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "pool_manager"
        / "pool"
        / "worker_earnings.py"
    ).read_text(encoding="utf-8")
    start = src.index("def allocate_tig")
    end = src.index("\n\ndef round_start_ms")
    ns: dict = {}
    exec(src[start:end], ns, ns)
    fn = ns["allocate_tig"]
    prorate = ns["prorate_pot"]
    since_join = ns["estimate_since_join_tig"]

    cases = [
        ((50, 100, 10.0), 5.0, "half the nonces gets half the TIG"),
        ((0, 100, 10.0), 0.0, "zero work earns zero"),
        ((10, 0, 10.0), 0.0, "no pool nonces earns zero"),
        ((10, 100, 0.0), 0.0, "no pool TIG yet earns zero"),
        ((1, 3, 1.0), round(1.0 / 3, 6), "rounding to 6 dp"),
    ]
    failed = 0
    for args, expect, label in cases:
        got = fn(*args)
        ok = got == expect
        print(f"{'pass' if ok else 'FAIL'}: {label} -> {got} (want {expect})")
        failed += 0 if ok else 1

    # Wallet-split: each slave gets a slice of *that wallet's* coinbase,
    # not of the whole pool. Two wallets, same pool nonce pile.
    wallet_a_tig, wallet_a_nonces = 37.42, 1000
    a1 = fn(600, wallet_a_nonces, wallet_a_tig)
    a2 = fn(400, wallet_a_nonces, wallet_a_tig)
    wallet_b_tig, wallet_b_nonces = 10.0, 500
    b1 = fn(500, wallet_b_nonces, wallet_b_tig)
    pool_wrong_a1 = fn(600, 1500, wallet_a_tig + wallet_b_tig)
    ok = (
        abs((a1 + a2) - wallet_a_tig) < 1e-9
        and abs(b1 - wallet_b_tig) < 1e-9
        and a1 != pool_wrong_a1
    )
    print(
        f"{'pass' if ok else 'FAIL'}: wallet rows sum to that wallet's TIG "
        f"-> a={a1}+{a2}={a1+a2} (want {wallet_a_tig}), b={b1}"
    )
    failed += 0 if ok else 1

    pot = prorate(70.0, 2 * 24 * 60 * 60 * 1000, 7 * 24 * 60 * 60 * 1000)
    ok = abs(pot - 20.0) < 1e-9
    print(f"{'pass' if ok else 'FAIL'}: 2 of 7 days prorates the pot -> {pot} (want 20.0)")
    failed += 0 if ok else 1

    same_day = fn(21000, 21000, 0.0313)
    ok = same_day == 0.0313
    print(
        f"{'pass' if ok else 'FAIL'}: all work in the last 24h means 24h equals Round "
        f"-> {same_day} (want 0.0313)"
    )
    failed += 0 if ok else 1

    week_tig = 0.07
    early = since_join(
        est_tig_week=week_tig,
        join_ms=1,
        round_start=10,
        now_ms=100,
        slave_nonces=21000,
        pool_since_join_nonces=21000,
        round_tig=10.0,
    )
    late = since_join(
        est_tig_week=week_tig,
        join_ms=50,
        round_start=10,
        now_ms=100,
        slave_nonces=21000,
        pool_since_join_nonces=42000,
        round_tig=10.0,
    )
    ok = early == week_tig and late > week_tig
    print(
        f"{'pass' if ok else 'FAIL'}: early joiner keeps week TIG, late joiner "
        f"uses the window -> early={early} late={late}"
    )
    failed += 0 if ok else 1
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
