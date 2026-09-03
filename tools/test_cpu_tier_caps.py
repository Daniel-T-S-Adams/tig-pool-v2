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
                    "cpu_tier_caps": {"S": 1, "M": 1, "L": 3, "XL": 6},
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

        # v1.5 runtime telemetry (0-valued queues must parse)
        runtime = parse(
            query_params={
                "state": "idle",
                "active_batches": "0",
                "pending_batches": "2",
                "last_idle_ms": "1500",
                "slave_version": "innopool-slave/0.1.0",
            }
        )
        cases.append((runtime.get("state") == "idle", "parse state"))
        cases.append((runtime.get("active_batches") == 0, "parse active_batches=0"))
        cases.append((runtime.get("pending_batches") == 2, "parse pending_batches"))
        cases.append((runtime.get("last_idle_ms") == 1500, "parse last_idle_ms"))
        cases.append(
            (runtime.get("slave_version") == "innopool-slave/0.1.0", "parse slave_version")
        )
        cases.append(
            (parse(query_params={"state": "bogus"}).get("state") is None, "reject bad state")
        )
        cases.append(
            (
                parse(headers={"X-InnoPool-State": "SUBMITTING"}).get("state") == "submitting",
                "state header case-insensitive value",
            )
        )

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
                "M no telem → 1",
            )
        )
        pica = {"cores": 32, "num_workers": 32, "load_1m": 12, "free_ram_gb": 16}
        cases.append(
            (
                earnable(tier=TIER_M, telemetry=pica, settings=settings) == 4,
                "M 32 cores load 12 → 4 core-fit seats",
            )
        )
        cases.append(
            (
                earnable(
                    tier=TIER_M,
                    telemetry={**pica, "load_1m": 29},
                    settings=settings,
                )
                == 1,
                "M 32w load 29 → no pack seat",
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
                earnable(tier=TIER_XL, telemetry=good, settings=settings) == 1,
                "XL with only 32 workers → 1 job",
            )
        )
        epyc = {"cores": 192, "num_workers": 153, "load_1m": 28, "free_ram_gb": 64}
        cases.append(
            (
                earnable(tier=TIER_XL, telemetry=epyc, settings=settings) == 4,
                "EPYC 153 workers → 4 jobs",
            )
        )
        cases.append(
            (
                earnable(
                    tier=TIER_XL,
                    telemetry={**epyc, "load_1m": 180},
                    settings=settings,
                )
                == 1,
                "XL high load stays at 1",
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
                == 0,
                "load-shed cooldown forces 0",
            )
        )
        cases.append(
            (
                earnable(
                    tier=TIER_M,
                    telemetry=good,
                    settings=settings,
                    load_shed_active=True,
                )
                == 0,
                "load-shed cooldown forces 0 even for M/tier-ceiling-1",
            )
        )
        overloaded = {"cores": 96, "num_workers": 32, "load_1m": 130, "free_ram_gb": 32}
        cases.append(
            (
                earnable(tier=TIER_M, telemetry=overloaded, settings=settings) == 0,
                "live overload forces 0 even for M",
            )
        )
        cases.append(
            (
                effective(
                    route_cap=8,
                    fleet_cpu_max_cap=1,
                    tier=TIER_M,
                    telemetry=good,
                    settings=settings,
                    load_shed_active=True,
                )
                == 0,
                "effective load-shed → 0 under fleet max=1",
            )
        )
        cases.append(
            (
                earnable(
                    tier=TIER_L,
                    telemetry={"cores": 64, "num_workers": 80, "load_1m": 20, "free_ram_gb": 32},
                    settings=settings,
                )
                == 2,
                "L with 80 workers → 2",
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
                == 8,
                "M 96-core telem core-fit seats cap at 8",
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
                    telemetry=epyc,
                    settings=settings,
                )
                == 4,
                "XL worker scale exceeds fleet max=1",
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
                == 8,
                "M 96-core telem still core-fit capped at 8",
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

        live_tier = ns["live_cpu_tier"]
        empty_seats = ns["cpu_empty_seats"]
        sum_seats = ns["sum_cpu_empty_seats"]
        hold_xl = ns["should_hold_leftover_for_xl"]
        cases.append((live_tier(cores=32) == TIER_M, "32 cores → M"))
        cases.append((live_tier(cores=192) == TIER_XL, "192 cores → XL"))
        cases.append((live_tier(workers=153) == TIER_XL, "153 workers → XL"))
        cases.append(
            (
                empty_seats(workers=25, cores=32, active=0, settings=settings) == 4,
                "Pica empty seats = 4 core-fit",
            )
        )
        cases.append(
            (
                empty_seats(workers=153, cores=192, active=1, settings=settings) == 3,
                "EPYC 153w active=1 → 3 empty seats",
            )
        )
        cases.append(
            (
                empty_seats(
                    workers=153, cores=192, active=0, assigned=1, settings=settings
                )
                == 3,
                "assigned roots count against empty seats",
            )
        )
        cases.append(
            (
                sum_seats(
                    [{"telem_cores": 192, "num_workers": 153, "telem_active": 0}] * 18,
                    settings,
                )
                == 72,
                "18 idle EPYCs → 72 empty seats",
            )
        )
        cases.append(
            (
                hold_xl(poller_earnable=1, hungry_xl_seats=3, sticky_own=False) is False,
                "Pica is not held when XL seats are hungry",
            )
        )
        cases.append(
            (
                hold_xl(
                    poller_earnable=1,
                    hungry_xl_seats=3,
                    leftover_jobs=2,
                )
                is False,
                "scarce leftovers are still claimable by Picas",
            )
        )
        cases.append(
            (
                hold_xl(poller_earnable=1, hungry_xl_seats=3, sticky_own=True) is False,
                "Pica still takes its own sticky job",
            )
        )
        cases.append(
            (
                hold_xl(poller_earnable=4, hungry_xl_seats=3, sticky_own=False) is False,
                "EPYC poller is not held",
            )
        )
        cases.append(
            (
                hold_xl(poller_earnable=1, hungry_xl_seats=0, sticky_own=False) is False,
                "no hungry XL → Picas take leftovers",
            )
        )
        cases.append(
            (
                hold_xl(
                    poller_earnable=1,
                    hungry_xl_seats=4,
                    leftover_is_cpu=False,
                )
                is False,
                "GPU leftovers are never held for XL CPUs",
            )
        )
        cases.append(
            (
                hold_xl(
                    poller_earnable=1,
                    hungry_xl_seats=4,
                    poller_is_cpu=False,
                    leftover_is_cpu=False,
                )
                is False,
                "idle GPU poller is never held for XL CPUs",
            )
        )

        fleet = ns["build_fleet_capacity"]
        hole = ns["fleet_hole_deficit"]
        burst_seats = ns["seat_create_burst"]
        pica4 = fleet(cpu_empty=4, cpu_claimable=0, open_jobs=10, parked_cap=20)
        epyc1 = fleet(cpu_empty=4, cpu_claimable=0, open_jobs=10, parked_cap=20)
        cases.append(
            (
                hole(pica4) == hole(epyc1) == 4,
                "4 Pica seats and 1 EPYC×4 have the same hole",
            )
        )
        cases.append(
            (
                burst_seats(empty_seats=4, claimable=0, remaining_cap_room=10, max_burst=16)
                == burst_seats(empty_seats=4, claimable=0, remaining_cap_room=10, max_burst=16)
                == 4,
                "same empty seats → same create burst",
            )
        )
        cases.append(
            (
                burst_seats(
                    empty_seats=35, claimable=0, remaining_cap_room=0, max_burst=16
                )
                == 0,
                "oversubscribed cap room yields no seat burst",
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
