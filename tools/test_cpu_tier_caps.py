#!/usr/bin/env python3
"""Unit checks for public CPU tier concurrent ceilings + telemetry gates."""

from __future__ import annotations

import ast
import os
import pathlib
import sys


def _load():
    path = pathlib.Path(__file__).resolve().parents[1] / "master" / "cpu_tier_caps.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    ns: dict = {"__name__": "cpu_tier_caps"}
    exec(compile(module, str(path), "exec"), ns, ns)
    return ns


def _load_cap_sched():
    path = pathlib.Path(__file__).resolve().parents[1] / "master" / "capability_scheduler.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    ns: dict = {"__name__": "capability_scheduler"}
    exec(compile(module, str(path), "exec"), ns, ns)
    return ns


def main() -> int:
    ns = _load()
    settings_fn = ns["cpu_tier_cap_settings"]
    parse = ns["parse_slave_telemetry"]
    headroom = ns["telemetry_has_cpu_headroom"]
    shed = ns["telemetry_requires_load_shed"]
    earnable = ns["cpu_earnable_concurrent_ceiling"]
    effective = ns["effective_cpu_adaptive_max_cap"]
    TIER_M = ns["TIER_M"]
    TIER_L = ns["TIER_L"]
    TIER_XL = ns["TIER_XL"]

    saved = {
        k: os.environ.pop(k)
        for k in list(os.environ)
        if k.startswith("CPU_") or k == "CAPABILITY_LIVE_TELEMETRY"
    }
    try:
        os.environ["CPU_CONCURRENT_REQUIRES_TELEMETRY"] = "true"
        settings = settings_fn(
            {
                "adaptive_slave_caps": {
                    "cpu_tier_caps": {"S": 1, "M": 1, "L": 2, "XL": 2},
                    "cpu_concurrent_requires_telemetry": True,
                }
            }
        )
        cases = []

        # Parse telemetry from query + headers
        telem = parse(
            query_params={"cores": "96", "num_workers": "32", "load_1m": "20"},
            headers={"X-InnoPool-Free-Ram-Gb": "64"},
        )
        cases.append((telem.get("cores") == 96, "parse cores"))
        cases.append((telem.get("num_workers") == 32, "parse workers"))
        cases.append((telem.get("free_ram_gb") == 64.0, "parse free ram header"))
        cases.append((parse(query_params={}, headers={}) == {}, "empty telemetry"))

        # Headroom / shed
        cases.append(
            (
                headroom({"cores": 96, "num_workers": 32, "load_1m": 40}, settings),
                "96c/32w low load has headroom",
            )
        )
        cases.append(
            (
                not headroom({"cores": 96, "num_workers": 96, "load_1m": 10}, settings),
                "workers≈cores no headroom",
            )
        )
        cases.append(
            (
                not headroom({"cores": 96, "num_workers": 32, "load_1m": 90}, settings),
                "high load blocks headroom",
            )
        )
        cases.append(
            (
                shed({"cores": 96, "load_1m": 130}, settings),
                "load shed on high load",
            )
        )
        cases.append(
            (
                shed({"free_ram_gb": 2.0}, settings),
                "load shed on low RAM",
            )
        )

        # Earnable ceilings
        cases.append(
            (
                earnable(tier=TIER_M, telemetry=None, settings=settings) == 1,
                "M → 1",
            )
        )
        cases.append(
            (
                earnable(tier=TIER_XL, telemetry=None, settings=settings) == 1,
                "XL no telemetry → 1",
            )
        )
        good = {"cores": 96, "num_workers": 32, "load_1m": 40, "free_ram_gb": 32}
        cases.append(
            (
                earnable(tier=TIER_XL, telemetry=good, settings=settings) == 2,
                "XL with headroom → 2",
            )
        )
        cases.append(
            (
                earnable(
                    tier=TIER_XL,
                    telemetry=good,
                    settings=settings,
                    load_shed_active=True,
                )
                == 1,
                "load-shed cooldown forces 1",
            )
        )
        cases.append(
            (
                earnable(tier=TIER_L, telemetry=good, settings=settings) == 2,
                "L with headroom → 2",
            )
        )

        # Effective max vs fleet cpu_max_cap=1
        cases.append(
            (
                effective(
                    route_cap=8,
                    fleet_cpu_max_cap=1,
                    tier=TIER_M,
                    telemetry=good,
                    settings=settings,
                )
                == 1,
                "Pica/M never exceeds 1 even with telemetry",
            )
        )
        cases.append(
            (
                effective(
                    route_cap=8,
                    fleet_cpu_max_cap=1,
                    tier=TIER_XL,
                    telemetry=None,
                    settings=settings,
                )
                == 1,
                "XL without telemetry stays 1",
            )
        )
        cases.append(
            (
                effective(
                    route_cap=8,
                    fleet_cpu_max_cap=1,
                    tier=TIER_XL,
                    telemetry=good,
                    settings=settings,
                )
                == 2,
                "XL with headroom can earn 2 above fleet max=1",
            )
        )
        cases.append(
            (
                effective(
                    route_cap=8,
                    fleet_cpu_max_cap=4,
                    tier=TIER_M,
                    telemetry=good,
                    settings=settings,
                )
                == 1,
                "M clamps to tier earnable 1 even if fleet max raised",
            )
        )
        cases.append(
            (
                effective(
                    route_cap=1,
                    fleet_cpu_max_cap=1,
                    tier=TIER_XL,
                    telemetry=good,
                    settings=settings,
                )
                == 1,
                "route_cap=1 still bounds XL",
            )
        )

        # Capability rank: XL preferred for hard roots
        cs = _load_cap_sched()
        hard = 0.9
        hard_min = cs["TIER_M"]
        rank_m = cs["assign_rank_tuple"](
            is_proof=False,
            own_proof=False,
            starved_root=False,
            starved_boost=0,
            original_idx=0,
            slave_tier=cs["TIER_M"],
            hardness=hard,
            slave_speed_ratio=1.0,
            job_age_ms=0,
            roots_ready=1,
            hard_hardness=0.65,
            hard_min_tier=hard_min,
        )
        rank_xl = cs["assign_rank_tuple"](
            is_proof=False,
            own_proof=False,
            starved_root=False,
            starved_boost=0,
            original_idx=0,
            slave_tier=cs["TIER_XL"],
            hardness=hard,
            slave_speed_ratio=1.0,
            job_age_ms=0,
            roots_ready=1,
            hard_hardness=0.65,
            hard_min_tier=hard_min,
        )
        # Lower tuple is better; compare score component (index 2 is -score)
        cases.append((rank_xl < rank_m, "XL ranks ahead of M on hard root"))

        failed = [msg for ok, msg in cases if not ok]
        for ok, msg in cases:
            print(("PASS" if ok else "FAIL"), msg)
        if failed:
            print(f"{len(failed)} failed", file=sys.stderr)
            return 1
        print(f"OK {len(cases)} cases")
        return 0
    finally:
        os.environ.update(saved)


if __name__ == "__main__":
    raise SystemExit(main())
