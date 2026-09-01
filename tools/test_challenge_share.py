#!/usr/bin/env python3
"""Per-challenge pots: GPU work is not priced like knapsack nonces."""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pool_manager"))

from pool.challenge_share import (  # noqa: E402
    pot_per_challenge,
    shares_from_challenge_nonces,
    tig_from_challenge_pots,
)


def main() -> int:
    failed = 0

    def check(ok: bool, label: str) -> None:
        nonlocal failed
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1

    # Same nonces on two challenges -> 50/50 even if one pile is huge.
    shares = shares_from_challenge_nonces(
        {
            ("cpu-wallet", "knapsack"): 14_000_000,
            ("gpu-wallet", "hypergraph"): 273_000,
        },
        scale=0.95,
    )
    check(abs(sum(shares.values()) - 0.95) < 1e-6, "shares sum to member pot")
    check(
        abs(shares["cpu-wallet"] - shares["gpu-wallet"]) < 1e-9,
        f"equal pots: knapsack farm and GPU farm split 50/50, got {shares}",
    )

    mixed = shares_from_challenge_nonces(
        {
            ("cpu-wallet", "knapsack"): 14_000_000,
            ("cpu-wallet", "energy_arbitrage"): 8_900_000,
            ("gpu-wallet", "hypergraph"): 273_000,
            ("gpu-wallet", "vector_search"): 54_000,
            ("gpu-wallet", "neuralnet_optimizer"): 50_000,
        },
        scale=1.0,
    )
    # 5 challenges: CPU owns 2, GPU owns 3 -> GPU 60%.
    check(abs(mixed["gpu-wallet"] - 0.6) < 1e-9, f"GPU 3/5 of pots -> 0.6, got {mixed}")
    check(abs(mixed["cpu-wallet"] - 0.4) < 1e-9, f"CPU 2/5 of pots -> 0.4, got {mixed}")

    check(shares_from_challenge_nonces({}, scale=1.0) == {}, "empty work -> empty shares")
    check(
        shares_from_challenge_nonces({("w", "knapsack"): 0}, scale=1.0) == {},
        "zero nonces -> empty shares",
    )

    # Pica GPU 24h at equal pots. Mixed-nonce rate made this ~0.027.
    pool = {
        "knapsack": 14_000_000,
        "energy_arbitrage": 8_900_000,
        "satisfiability": 400_000,
        "job_scheduling": 300_000,
        "vehicle_routing": 200_000,
        "hypergraph": 273_000,
        "vector_search": 54_000,
        "neuralnet_optimizer": 50_000,
    }
    slice_tig = pot_per_challenge(166.4, 8)
    pica45 = tig_from_challenge_pots(
        {"hypergraph": 2900, "neuralnet_optimizer": 520, "vector_search": 442},
        pool,
        slice_tig,
    )
    mixed_rate = 166.4 * (2900 + 520 + 442) / sum(pool.values())
    check(pica45 > 0.4, f"pica45 GPU 24h is {pica45}, not the mixed-rate {mixed_rate:.4f}")
    check(pica45 > mixed_rate * 10, "per-challenge TIG is >10x the mixed nonce pile")

    one_chal = tig_from_challenge_pots({"hypergraph": 2900}, {"hypergraph": 2900}, 20.0)
    check(one_chal == 20.0, "sole worker on a challenge takes that slice")

    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
