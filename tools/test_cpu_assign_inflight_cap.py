#!/usr/bin/env python3
"""CPU assign cap is 2 on S/M Picas with worker telem. L/XL keep earnable seats."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load():
    path = pathlib.Path(__file__).resolve().parents[1] / "master" / "cpu_tier_caps.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    ns: dict = {"__name__": "cpu_tier_caps"}
    exec(compile(module, str(path), "exec"), ns, ns)
    return ns


def main() -> int:
    ns = _load()
    cap = ns["cpu_assign_inflight_cap"]
    failed = 0
    cases = [
        (cap(32, cores=32, workers=32, route_cap=32) == 2, "32-core Pica warehouses 32 -> 2"),
        (cap(8, cores=32, workers=32, route_cap=32) == 2, "32-core Pica warehouses 8 -> 2"),
        (cap(32, cores=None, workers=None, route_cap=32) == 1, "unknown CPU size fail-safes to 1"),
        (
            cap(32, cores=None, workers=None, route_cap=32, trusted_without_telem=True) == 32,
            "trusted coordinator without telem keeps 32",
        ),
        (cap(32, cores=192, workers=190, route_cap=32) == 5, "192-core XL keeps 190/32 = 5"),
        (cap(6, cores=192, workers=190, route_cap=32) == 5, "XL proposed 6 still capped at earnable 5"),
        (cap(0, cores=32, workers=32, route_cap=32) == 0, "load-shed 0 stays 0"),
        (
            cap(4, cores=32, workers=32, route_cap=32, load_shed_active=True) == 0,
            "load-shed active yields 0",
        ),
        (cap(32, cores=48, workers=48, route_cap=32) == 2, "48-core M pack cap is 2"),
        (
            cap(32, cores=32, workers=32, route_cap=32, load_1m=29) == 1,
            "hot 32-core Pica stays at 1",
        ),
        (cap(32, cores=80, workers=80, route_cap=32) == 2, "80-core L scales to 80/32 = 2"),
        (cap(32, cores=96, workers=96, route_cap=32) == 3, "96-core XL scales to 96/32 = 3"),
    ]
    for ok, label in cases:
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
