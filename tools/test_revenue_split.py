#!/usr/bin/env python3
"""Revenue attribution math: block factor, round pots, per-owner shares, blend."""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pool_manager"))

from pool.revenue_split import (  # noqa: E402
    attribute_block,
    blend_shares,
    effort_blend,
    pot_fractions,
    revenue_shares,
)


def _close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(float(a) - float(b)) <= tol


def main() -> int:
    failed = 0

    def check(ok: bool, label: str) -> None:
        nonlocal failed
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1

    # ── attribute_block: TIG /get-opow + /get-challenges shapes ─────────────
    pool_bd = {
        "num_qualifiers_by_challenge_by_track": {
            "c001": {"t1": 50, "t2": 0},        # 50 of 200 → 0.25
            "c004": {"a": 100},                 # 100 of 100, mult 1.1 → 1.1
            "c009": {"x": 3},                   # not in challenge list → ignored
        },
        "legacy_multiplier_by_challenge_by_track": {"c004": {"a": 1.1}},
        "reward": str(2 * 10**18),
    }
    challenges = [
        {"id": "c001", "config": {"name": "satisfiability", "type": "cpu"},
         "block_data": {"num_qualifiers_by_track": {"t1": 100, "t2": 100}}},
        {"id": "c004", "config": {"name": "vector_search", "type": "gpu"},
         "block_data": {"num_qualifiers_by_track": {"a": 100}}},
        {"id": "c005", "config": {"name": "hypergraph", "type": "gpu"},
         "block_data": {"num_qualifiers_by_track": {"h": 100}}},
    ]
    attr = attribute_block(pool_bd, challenges)
    check(set(attr) == {"satisfiability", "vector_search", "hypergraph"}, "one row per active challenge, named")
    check(_close(attr["satisfiability"]["factor"], 0.25), "factor = pool q / total q across tracks")
    check(_close(attr["vector_search"]["factor"], 1.1), "legacy multiplier applied per track")
    check(attr["hypergraph"]["factor"] == 0 and attr["hypergraph"]["attribution"] == 0, "no qualifiers → zero, still listed")
    check(_close(sum(v["attribution"] for v in attr.values()), 1.0), "attribution sums to 1")
    check(_close(attr["vector_search"]["attribution"], 1.1 / 1.35), "attribution = factor / Σ factor")
    check(attr["vector_search"]["type"] == "gpu" and attr["satisfiability"]["type"] == "cpu", "type from TIG config")
    check(attribute_block({}, challenges)["satisfiability"]["attribution"] == 0, "empty block data → all zero, no crash")

    # ── pot_fractions: weight samples by reward × blocks ────────────────────
    samples = [
        {"reward_tig": 1.0, "blocks_covered": 1, "attribution": {"a": {"attribution": 1.0}}},
        {"reward_tig": 1.0, "blocks_covered": 3, "attribution": {"b": {"attribution": 1.0}}},
    ]
    pots = pot_fractions(samples)
    check(_close(pots["a"], 0.25) and _close(pots["b"], 0.75), "sample covering 3 blocks weighs 3x")
    pots = pot_fractions([
        {"reward_tig": 0.0, "blocks_covered": 5, "attribution": {"a": {"attribution": 1.0}}},
        {"reward_tig": 2.0, "blocks_covered": 1, "attribution": '{"b": {"attribution": 0.5}, "c": {"attribution": 0.5}}'},
    ])
    check("a" not in pots and _close(pots["b"], 0.5), "zero-reward block ignored; JSON string attribution parsed")
    check(pot_fractions([]) == {}, "no samples → empty (caller falls back to effort)")

    # ── revenue_shares: effort within challenge, pot between challenges ─────
    credits = {
        ("w1", "sat"): 100.0,
        ("w2", "sat"): 100.0,
        ("w2", "vec"): 50.0,
        ("w3", "dead"): 999.0,   # pool earns nothing here
    }
    pots = {"sat": 0.4, "vec": 0.6, "unworked": 0.0}
    rev = revenue_shares(credits, pots)
    check(_close(rev["w1"], 0.2), "w1: half of sat pot (0.4)")
    check(_close(rev["w2"], 0.2 + 0.6), "w2: half of sat + all of vec")
    check("w3" not in rev, "work on zero-revenue challenge earns nothing in pure attribution")
    check(_close(sum(rev.values()), 1.0), "revenue shares sum to 1")
    rev2 = revenue_shares(credits, {"sat": 0.2, "vec": 0.3, "ghost": 0.5})
    check(_close(rev2["w1"], 0.2), "pot on a challenge nobody worked is renormalised away")
    check(revenue_shares(credits, {}) == {}, "no pots → empty")

    # ── blend_shares: insurance for dead-challenge work ─────────────────────
    effort = {"w1": 100.0, "w2": 150.0, "w3": 999.0}
    bl = blend_shares(rev, effort, 0.2)
    check(_close(sum(bl.values()), 1.0), "blend sums to 1")
    check(_close(bl["w3"], 0.2 * 999.0 / 1249.0), "w3 only paid via the effort slice")
    check(_close(bl["w2"], 0.8 * 0.8 + 0.2 * 150.0 / 1249.0), "w2 = 0.8·revenue + 0.2·effort")
    check(blend_shares(rev, effort, 1.0) == {k: v / 1249.0 for k, v in effort.items()}, "β=1 is pure effort")
    check(blend_shares({}, effort, 0.2) == {k: v / 1249.0 for k, v in effort.items()}, "no revenue → effort")
    check(_close(blend_shares(rev, effort, 0.0)["w2"], 0.8), "β=0 is pure attribution")

    import os
    os.environ["PAY_EFFORT_BLEND"] = "0.35"
    check(effort_blend() == 0.35, "PAY_EFFORT_BLEND read from env")
    check(effort_blend(2.0) == 1.0 and effort_blend(-1) == 0.0, "blend clamped to [0,1]")
    os.environ["PAY_EFFORT_BLEND"] = "junk"
    check(effort_blend() == 0.2, "bad env value → default 0.2")

    print()
    print("all passed" if not failed else f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
