#!/usr/bin/env python3
"""Per-challenge pots: GPU work is not priced like knapsack nonces."""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pool_manager"))

from pool.challenge_share import (  # noqa: E402
    pots_by_challenge,
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

    # Same nonces on two families: GPU pot 27%, CPU pot 73% of the member share.
    shares = shares_from_challenge_nonces(
        {
            ("cpu-wallet", "knapsack"): 14_000_000,
            ("gpu-wallet", "hypergraph"): 273_000,
        },
        scale=0.95,
        gpu_frac=0.27,
    )
    check(abs(sum(shares.values()) - 0.95) < 1e-6, "shares sum to member pot")
    check(
        abs(shares["gpu-wallet"] - 0.95 * 0.27) < 1e-9,
        f"GPU family 27% of 0.95, got {shares}",
    )
    check(
        abs(shares["cpu-wallet"] - 0.95 * 0.73) < 1e-9,
        f"CPU family 73% of 0.95, got {shares}",
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
        gpu_frac=0.27,
    )
    check(abs(mixed["gpu-wallet"] - 0.27) < 1e-9, f"3 GPU challenges still 0.27, got {mixed}")
    check(abs(mixed["cpu-wallet"] - 0.73) < 1e-9, f"2 CPU challenges still 0.73, got {mixed}")

    cpu_only = shares_from_challenge_nonces(
        {("cpu-wallet", "knapsack"): 100, ("other", "energy_arbitrage"): 100},
        scale=1.0,
        gpu_frac=0.27,
    )
    check(abs(sum(cpu_only.values()) - 1.0) < 1e-9, "CPU-only work renormalizes to 100%")
    check(abs(cpu_only["cpu-wallet"] - 0.5) < 1e-9, f"CPU-only equal pots, got {cpu_only}")

    restored = shares_from_challenge_nonces(
        {
            ("cpu-wallet", "knapsack"): 1,
            ("cpu-wallet", "energy_arbitrage"): 1,
            ("cpu-wallet", "satisfiability"): 1,
            ("cpu-wallet", "job_scheduling"): 1,
            ("cpu-wallet", "vehicle_routing"): 1,
            ("gpu-wallet", "hypergraph"): 1,
            ("gpu-wallet", "vector_search"): 1,
            ("gpu-wallet", "neuralnet_optimizer"): 1,
        },
        scale=1.0,
        gpu_frac=0.375,
    )
    check(abs(restored["gpu-wallet"] - 0.375) < 1e-9, f"0.375 GPU frac restores 3/8, got {restored}")
    check(abs(restored["cpu-wallet"] - 0.625) < 1e-9, f"0.375 GPU frac restores 5/8, got {restored}")

    check(shares_from_challenge_nonces({}, scale=1.0) == {}, "empty work -> empty shares")
    check(
        shares_from_challenge_nonces({("w", "knapsack"): 0}, scale=1.0) == {},
        "zero nonces -> empty shares",
    )

    # Pica GPU 24h at 27/73 family pots. Mixed-nonce rate made this ~0.027.
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
    pots = pots_by_challenge(166.4, pool, gpu_frac=0.27)
    check(abs(sum(pots[c] for c in ("hypergraph", "vector_search", "neuralnet_optimizer")) - 166.4 * 0.27) < 1e-9, "GPU pots sum to 27%")
    pica45 = tig_from_challenge_pots(
        {"hypergraph": 2900, "neuralnet_optimizer": 520, "vector_search": 442},
        pool,
        pots,
    )
    mixed_rate = 166.4 * (2900 + 520 + 442) / sum(pool.values())
    check(pica45 > 0.4, f"pica45 GPU 24h is {pica45}, not the mixed-rate {mixed_rate:.4f}")
    check(pica45 > mixed_rate * 10, "per-challenge TIG is >10x the mixed nonce pile")
    equal = tig_from_challenge_pots(
        {"hypergraph": 2900, "neuralnet_optimizer": 520, "vector_search": 442},
        pool,
        pots_by_challenge(166.4, pool, gpu_frac=0.375),
    )
    check(pica45 < equal, f"27% GPU pot pays less than equal 8-pots: {pica45} vs {equal}")

    one_chal = tig_from_challenge_pots({"hypergraph": 2900}, {"hypergraph": 2900}, 20.0)
    check(one_chal == 20.0, "sole worker on a challenge takes that slice")

    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
