#!/usr/bin/env python3
"""Unit checks: SingleFlightCache serves last snapshot while refreshing."""

from __future__ import annotations

import ast
import logging
import pathlib
import threading
import time


def _load_cache():
    path = pathlib.Path(__file__).resolve().parents[1] / "pool_manager" / "pool" / "database.py"
    module = ast.parse(path.read_text(encoding="utf-8"))
    node = next(
        n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "SingleFlightCache"
    )
    ns = {
        "logging": logging,
        "threading": threading,
        "time": time,
        "logger": logging.getLogger("test_singleflight"),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), ns, ns)
    return ns["SingleFlightCache"]


def main() -> int:
    failed = 0

    def check(ok: bool, label: str) -> None:
        nonlocal failed
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1

    Cache = _load_cache()

    cache = Cache(10)
    check(cache.get(lambda: "fresh") == "fresh", "cold build returns value")
    calls = {"n": 0}

    def once():
        calls["n"] += 1
        return "again"

    check(cache.get(once) == "fresh", "ttl hit skips builder")
    check(calls["n"] == 0, "ttl hit does not call builder")

    stale = Cache(0.05)
    stale.get(lambda: "v1")
    time.sleep(0.06)
    gate = threading.Event()

    def slow():
        gate.wait(2)
        return "v2"

    started = time.monotonic()
    got = stale.get(slow)
    elapsed = time.monotonic() - started
    check(got == "v1", "expired read returns last snapshot")
    check(elapsed < 0.2, f"expired read does not wait on rebuild ({elapsed:.3f}s)")

    gate.set()
    deadline = time.time() + 1
    landed = False
    while time.time() < deadline:
        if stale.get(lambda: "no") == "v2":
            landed = True
            break
        time.sleep(0.01)
    check(landed, "background refresh updates snapshot")

    concurrent = Cache(0.05)
    concurrent.get(lambda: "a")
    time.sleep(0.06)
    builds = {"n": 0}

    def counted():
        builds["n"] += 1
        time.sleep(0.15)
        return builds["n"]

    threads = [threading.Thread(target=lambda: concurrent.get(counted)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    time.sleep(0.2)
    check(builds["n"] == 1, f"one refresh for concurrent expired reads (n={builds['n']})")

    broken = Cache(0.05)
    broken.get(lambda: "ok")
    time.sleep(0.06)

    def boom():
        raise RuntimeError("rebuild failed")

    check(broken.get(boom) == "ok", "failed refresh still returns last snapshot")

    src = (
        pathlib.Path(__file__).resolve().parents[1] / "pool_manager" / "pool" / "ops_metrics.py"
    ).read_text(encoding="utf-8")
    check("STICKY_ONLINE_OWNERS_CTE" in src, "ops metrics defines sticky CTE")
    check("AND NOT EXISTS (" not in src, "ops metrics dropped correlated NOT EXISTS")
    check(src.count("LEFT JOIN sticky_online_owners") >= 4, "dashboard + governor use sticky join")

    html = (
        pathlib.Path(__file__).resolve().parents[1] / "pool_website" / "ops.html"
    ).read_text(encoding="utf-8")
    check("sanitizeApiError" in html, "ops page sanitizes HTML API errors")
    check("showing last good snapshot" in html, "ops page keeps last good snapshot")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
