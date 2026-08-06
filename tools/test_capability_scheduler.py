#!/usr/bin/env python3
"""Unit checks for capability_scheduler pure helpers."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load():
    path = pathlib.Path(__file__).resolve().parents[1] / "master" / "capability_scheduler.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    # Execute whole module (stdlib-only imports).
    ns: dict = {"__name__": "capability_scheduler"}
    exec(compile(module, str(path), "exec"), ns, ns)
    return ns


def main() -> int:
    ns = _load()
    hardware_tier = ns["hardware_tier"]
    heuristic_track_hardness = ns["heuristic_track_hardness"]
    hardness_from_p95_ms = ns["hardness_from_p95_ms"]
    blend_hardness = ns["blend_hardness"]
    should_skip_hard_for_weak = ns["should_skip_hard_for_weak"]
    assign_rank_tuple = ns["assign_rank_tuple"]
    precommit_hardness_weight_mult = ns["precommit_hardness_weight_mult"]
    algo_is_schedulable = ns["algo_is_schedulable"]
    min_tier_for_hardness = ns["min_tier_for_hardness"]
    TIER_S = ns["TIER_S"]
    TIER_M = ns["TIER_M"]
    TIER_L = ns["TIER_L"]
    TIER_XL = ns["TIER_XL"]

    cases = []

    cases.append((hardware_tier(threads=16) == TIER_S, "16c → S"))
    cases.append((hardware_tier(threads=32) == TIER_M, "32c → M"))
    cases.append((hardware_tier(threads=64) == TIER_L, "64c → L"))
    cases.append((hardware_tier(threads=96) == TIER_XL, "96c → XL"))
    cases.append((hardware_tier(declared_cores=48) == TIER_M, "declared 48 → M"))
    cases.append(
        (
            hardware_tier(threads=96, ram_gb=16) < TIER_XL,
            "96c low RAM demoted from XL",
        )
    )
    cases.append(
        (
            hardware_tier(threads=64, preflight_status="low_spec_override") == TIER_M,
            "low_spec demotes L→M",
        )
    )
    cases.append((hardware_tier() == TIER_M, "unknown → default M"))

    sat_hard = heuristic_track_hardness("satisfiability", "n_vars=100000,ratio=4200")
    sat_easy = heuristic_track_hardness("satisfiability", "n_vars=5000,ratio=4267")
    cases.append((sat_hard > 0.85, f"SAT 100k hard got {sat_hard:.2f}"))
    cases.append((sat_easy < sat_hard, "SAT 5k easier than 100k"))
    cases.append(
        (
            heuristic_track_hardness("job_scheduling", "n=50,s=fjsp_medium") >= 0.7,
            "fjsp hard",
        )
    )

    soft = hardness_from_p95_ms(60_000, soft_p95_ms=300_000, hard_p95_ms=1_800_000)
    hard = hardness_from_p95_ms(1_800_000, soft_p95_ms=300_000, hard_p95_ms=1_800_000)
    cases.append((soft is not None and soft < 0.5, f"1m p95 soft got {soft}"))
    cases.append((hard == 1.0, f"hard p95=1 got {hard}"))

    blended = blend_hardness(local_hardness=0.9, heuristic=0.2, local_weight=0.75)
    cases.append((0.7 < blended < 0.8, f"blend ~0.725 got {blended:.3f}"))

    cases.append(
        (
            min_tier_for_hardness(0.9, 0.65, TIER_L) == TIER_L,
            "hard work needs L",
        )
    )
    cases.append(
        (
            should_skip_hard_for_weak(
                slave_tier=TIER_M,
                hardness=0.9,
                hard_hardness=0.65,
                hard_min_tier=TIER_L,
                has_easier_claimable=True,
                job_age_ms=60_000,
                age_out_ms=20 * 60 * 1000,
            ),
            "M skips hard when easier exists",
        )
    )
    cases.append(
        (
            not should_skip_hard_for_weak(
                slave_tier=TIER_M,
                hardness=0.9,
                hard_hardness=0.65,
                hard_min_tier=TIER_L,
                has_easier_claimable=True,
                job_age_ms=25 * 60 * 1000,
                age_out_ms=20 * 60 * 1000,
            ),
            "age-out allows weak on hard",
        )
    )
    cases.append(
        (
            not should_skip_hard_for_weak(
                slave_tier=TIER_XL,
                hardness=0.9,
                hard_hardness=0.65,
                hard_min_tier=TIER_L,
                has_easier_claimable=True,
                job_age_ms=60_000,
                age_out_ms=20 * 60 * 1000,
            ),
            "XL does not skip hard",
        )
    )

    weak_key = assign_rank_tuple(
        is_proof=False,
        own_proof=False,
        starved_root=False,
        starved_boost=0,
        original_idx=0,
        slave_tier=TIER_S,
        hardness=0.9,
        slave_speed_ratio=1.0,
        job_age_ms=0,
        roots_ready=0,
        hard_hardness=0.65,
        hard_min_tier=TIER_L,
    )
    strong_key = assign_rank_tuple(
        is_proof=False,
        own_proof=False,
        starved_root=False,
        starved_boost=0,
        original_idx=1,
        slave_tier=TIER_XL,
        hardness=0.9,
        slave_speed_ratio=0.8,
        job_age_ms=0,
        roots_ready=0,
        hard_hardness=0.65,
        hard_min_tier=TIER_L,
    )
    cases.append((strong_key < weak_key, "XL ranks ahead of S on hard first-owner"))

    mult_ok = precommit_hardness_weight_mult(
        hardness=0.9,
        hard_hardness=0.65,
        strong_online=10,
        hard_open_roots=40,
        hard_open_per_strong=8,
        inventory_known=True,
    )
    mult_sat = precommit_hardness_weight_mult(
        hardness=0.9,
        hard_hardness=0.65,
        strong_online=2,
        hard_open_roots=80,
        hard_open_per_strong=8,
        inventory_known=True,
    )
    mult_unknown = precommit_hardness_weight_mult(
        hardness=0.9,
        hard_hardness=0.65,
        strong_online=0,
        hard_open_roots=80,
        hard_open_per_strong=8,
        inventory_known=False,
    )
    mult_zero_strong = precommit_hardness_weight_mult(
        hardness=0.9,
        hard_hardness=0.65,
        strong_online=0,
        hard_open_roots=80,
        hard_open_per_strong=8,
        inventory_known=True,
    )
    cases.append((mult_ok == 1.0, f"under capacity weight 1 got {mult_ok}"))
    cases.append((mult_sat < 0.5, f"over capacity down-weight got {mult_sat}"))
    cases.append((mult_unknown == 1.0, f"fail-open unknown inventory got {mult_unknown}"))
    cases.append((mult_zero_strong == 0.15, f"known empty strong census got {mult_zero_strong}"))

    ok, reason = algo_is_schedulable(
        "c001_a098",
        algorithms=[
            {
                "id": "c001_a098",
                "state": {"banned": True, "round_active": 1},
            }
        ],
        binarys=[{"algorithm_id": "c001_a098", "details": {"compile_success": True, "download_url": "x"}}],
    )
    cases.append((not ok and reason == "banned", f"banned skip got {ok}/{reason}"))

    ok2, reason2 = algo_is_schedulable(
        "c001_a098",
        algorithms=[
            {
                "id": "c001_a098",
                "state": {"banned": False, "round_active": 1},
            }
        ],
        binarys=[],
        block_round=10,
    )
    cases.append((ok2, f"no binary map still ok got {ok2}/{reason2}"))

    ok3, reason3 = algo_is_schedulable(
        "c001_a098",
        algorithms=[
            {
                "id": "c001_a098",
                "state": {"banned": False, "round_active": 1},
            }
        ],
        binarys=[
            {
                "algorithm_id": "c001_a098",
                "details": {"compile_success": False, "download_url": "x"},
            }
        ],
    )
    cases.append((not ok3 and reason3 == "compile_failed", f"compile fail {ok3}/{reason3}"))

    failed = 0
    for okc, label in cases:
        print(f"{'pass' if okc else 'FAIL'}: {label}")
        if not okc:
            failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
