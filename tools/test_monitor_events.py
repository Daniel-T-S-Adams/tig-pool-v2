#!/usr/bin/env python3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from monitor_pool_health import detect_events, keep_ahead_want


def main() -> int:
    failed = 0
    if keep_ahead_want(20, 67) != 20:
        print("FAIL want 20")
        failed += 1
    if keep_ahead_want(30, 18) != 18:
        print("FAIL want cap 18")
        failed += 1

    active: set = set()
    hole = {
        "ts": "2026-08-19T03:04:00Z",
        "cpu_online": 61,
        "cpu_idle": 32,
        "cpu_busy": 29,
        "gpu_online": 18,
        "gpu_idle": 0,
        "claimable": 0,
        "sticky": 10,
        "unassigned": 10,
        "open_jobs": 29,
        "max_concurrent": 276,
        "sql_ok": True,
        "cpu_root_phase": 12,
        "cpu_proof_phase": 17,
        "gpu_root_phase": 6,
        "gpu_proof_phase": 4,
        "unowned_cpu": 0,
        "want_cpu": 17,
        "unowned_gpu": 1,
        "want_gpu": 4,
        "idle_cpu_needs_work": True,
        "cpu_profile_blocked": False,
        "at_max_concurrent": False,
        "block_reasons": [],
        "cpu_reasons": [],
        "gpu_reasons": [],
        "creates_15m": 42,
        "roots_done_15m": 1077,
        "stopped_15m": 5,
        "proved_1h_n": 80,
        "proved_1h_p50_min": 15.2,
        "proved_1h_p90_min": 24.0,
        "open_p50_age_min": 8.0,
        "open_max_age_min": 22.0,
    }
    ev = detect_events(None, hole, active)
    kinds = [e["kind"] for e in ev]
    ok = "cpu_idle_hole_no_work" in kinds
    print(("pass" if ok else "FAIL") + f": hole event kinds={kinds}")
    if not ok:
        failed += 1
    if ev:
        print(ev[0]["text"][:240] + "...")
    ev2 = detect_events(hole, hole, active)
    ok = ev2 == []
    print(("pass" if ok else "FAIL") + ": no duplicate hole while still idle")
    if not ok:
        failed += 1
    cleared = dict(hole, cpu_idle=3, cpu_busy=58, ts="2026-08-19T03:07:00Z")
    ev3 = detect_events(hole, cleared, active)
    kinds3 = [e["kind"] for e in ev3]
    ok = "cpu_idle_hole_cleared" in kinds3
    print(("pass" if ok else "FAIL") + f": cleared kinds={kinds3}")
    if not ok:
        failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
