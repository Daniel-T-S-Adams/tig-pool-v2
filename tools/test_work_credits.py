#!/usr/bin/env python3
"""Effort-credit math: VRPTW vs knapsack, cap, conversion floor, split."""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pool_manager"))

from pool.work_credits import (  # noqa: E402
    CAP_MULT,
    CONVERSION_FLOOR,
    blend_seconds_per_nonce,
    conversion_factor,
    credits_for_nonces,
    fractions_from_amounts,
    load_weight_table,
    prior_seconds_per_nonce,
    score_root_group,
)


def main() -> int:
    failed = 0

    def check(ok: bool, label: str) -> None:
        nonlocal failed
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1

    knap = prior_seconds_per_nonce("knapsack")
    vrptw = prior_seconds_per_nonce("vehicle_routing")
    check(vrptw / knap == 5.0, "VRPTW prior is 5x knapsack (3.0 / 0.6)")

    knap_c = credits_for_nonces(32, knap)
    vrptw_c = credits_for_nonces(32, vrptw)
    check(vrptw_c == knap_c * 5.0, f"same 32 nonces: VRPTW {vrptw_c} = 5x knapsack {knap_c}")

    check(credits_for_nonces(0, 2.0) == 0.0, "zero nonces -> 0 credits")
    check(credits_for_nonces(10, 2.0) == 20.0, "10 nonces * 2s = 20 credits")

    # Cap is on the weight, not a higher invented weight: passing a huge
    # sec/nonce that is already the table weight is not increased; the cap
    # only clips a per-batch override above CAP_MULT * weight. credits_for_nonces
    # uses the given weight as both value and cap base, so weight==cap.
    check(
        credits_for_nonces(10, 2.0, cap_mult=CAP_MULT) == 20.0,
        "normal weight is unchanged by the cap",
    )

    check(conversion_factor(stopped=False, proof_submitted=False) == 1.0, "live job converts 1.0")
    check(conversion_factor(stopped=True, proof_submitted=True) == 1.0, "proved job converts 1.0")
    check(
        conversion_factor(stopped=True, proof_submitted=False) == CONVERSION_FLOOR,
        "stopped no-proof uses the floor, not zero",
    )

    scored = score_root_group(
        nonces=32,
        challenge="knapsack",
        stopped=True,
        proof_submitted=False,
    )
    check(
        scored["credits_converted"] == round(scored["credits"] * CONVERSION_FLOOR, 6),
        "junk knapsack keeps half the effort credits",
    )

    check(blend_seconds_per_nonce(None, 2.0, 0) == 2.0, "no EMA -> prior")
    check(blend_seconds_per_nonce(4.0, 2.0, 8) == 4.0, "enough samples -> EMA")
    blended = blend_seconds_per_nonce(4.0, 2.0, 4)
    check(abs(blended - 3.0) < 1e-9, f"4/8 samples blend 50/50 -> {blended}")

    table = load_weight_table(
        [
            {
                "challenge": "knapsack",
                "track_id": "n=100",
                "ema_ms_per_nonce": 400.0,
                "sample_n": 20,
            }
        ]
    )
    check(table[("knapsack", "n=100")]["source"] == "ema", "20 samples -> ema source")
    check(
        abs(table[("knapsack", "n=100")]["sec_per_nonce"] - 0.4) < 1e-9,
        "400ms/nonce -> 0.4s/nonce",
    )
    check(table[("knapsack", "")]["source"] == "prior", "challenge fallback stays prior")

    # Two wallets, same nonce count, different challenges: credit split
    # must favor the slower work. member_share=1.0 for a clean ratio.
    a = score_root_group(nonces=1000, challenge="knapsack")["credits_converted"]
    b = score_root_group(nonces=1000, challenge="vehicle_routing")["credits_converted"]
    frac = fractions_from_amounts({"knap-wallet": a, "vrptw-wallet": b}, 1.0)
    check(abs(sum(frac.values()) - 1.0) < 1e-6, "credit fractions sum to member_share")
    check(frac["vrptw-wallet"] > frac["knap-wallet"], "VRPTW wallet gets the larger slice")
    check(
        abs(frac["vrptw-wallet"] / frac["knap-wallet"] - 5.0) < 1e-3,
        f"VRPTW/knapsack share ratio ~5, got {frac['vrptw-wallet']/frac['knap-wallet']}",
    )

    nonce_frac = fractions_from_amounts({"knap-wallet": 1000, "vrptw-wallet": 1000}, 0.95)
    check(abs(sum(nonce_frac.values()) - 0.95) < 1e-6, "nonce split respects 5% fee")
    check(
        abs(nonce_frac["knap-wallet"] - nonce_frac["vrptw-wallet"]) < 1e-9,
        "same nonces -> same live share (the unfair status quo)",
    )

    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
