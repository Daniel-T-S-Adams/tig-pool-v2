#!/usr/bin/env python3
"""Cold SingleFlightCache must return a placeholder instead of blocking."""

from __future__ import annotations

import ast
import logging
import pathlib
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_cache():
    path = ROOT / "pool_manager" / "pool" / "database.py"
    module = ast.parse(path.read_text(encoding="utf-8"))
    keep = [node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "SingleFlightCache"]
    if len(keep) != 1:
        raise RuntimeError("SingleFlightCache not found")
    ns = {
        "logging": logging,
        "logger": logging.getLogger("test_single_flight"),
        "threading": __import__("threading"),
        "time": time,
    }
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns["SingleFlightCache"]


def main() -> int:
    cache = _load_cache()(10)
    started = []

    def builder():
        started.append(1)
        time.sleep(0.2)
        return {"ok": True}

    first = cache.get(builder, placeholder={"degraded": True})
    second = cache.get(builder, placeholder={"degraded": True})
    time.sleep(0.3)
    third = cache.get(builder, placeholder={"degraded": True})
    ok = [
        (first == {"degraded": True}, "cold get returns placeholder"),
        (second == {"degraded": True} or second == {"ok": True}, "waiter is not blocked on SQL"),
        (third == {"ok": True}, "background build becomes the live snapshot"),
        (len(started) == 1, "only one builder runs"),
    ]
    failed = 0
    for passed, label in ok:
        print(f"{'pass' if passed else 'FAIL'}: {label}")
        if not passed:
            failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
