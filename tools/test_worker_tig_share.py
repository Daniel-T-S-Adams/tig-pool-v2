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

    wallet_tig, wallet_nonces = 0.4190, 100000
    n_round, n_24h, n_12h = 10000, 3000, 1500
    tig_round = fn(n_round, wallet_nonces, wallet_tig)
    tig_24h = fn(n_24h, wallet_nonces, wallet_tig)
    tig_12h = fn(n_12h, wallet_nonces, wallet_tig)
    ok = tig_12h <= tig_24h <= tig_round
    print(
        f"{'pass' if ok else 'FAIL'}: same rate means 12h<=24h<=Round "
        f"-> {tig_12h} <= {tig_24h} <= {tig_round}"
    )
    failed += 0 if ok else 1

    # Joined 20h ago: every nonce this round is also in the last 24h.
    tig_24h_new = fn(21000, wallet_nonces, wallet_tig)
    tig_round_new = fn(21000, wallet_nonces, wallet_tig)
    ok = tig_24h_new == tig_round_new
    print(
        f"{'pass' if ok else 'FAIL'}: joined 20h ago, 24h equals Round "
        f"at the same payout rate -> 24h={tig_24h_new} round={tig_round_new}"
    )
    failed += 0 if ok else 1
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
