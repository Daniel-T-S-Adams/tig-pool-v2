#!/usr/bin/env python3
"""Guardrails so extra workers cannot recreate the shm / lock-convoy outage."""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> int:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    sql = (ROOT / "master" / "sql.py").read_text(encoding="utf-8")
    manager = (ROOT / "pool_manager" / "pool" / "database.py").read_text(encoding="utf-8")
    ok = [
        ("max_parallel_workers_per_gather=0" in compose, "compose disables parallel gathers"),
        ("max_parallel_workers=0" in compose, "compose disables parallel workers"),
        ("shm_size: 2g" in compose, "compose gives Postgres 2GB shm"),
        ("statement_timeout=60s" in compose, "compose has a 60s statement backstop"),
        ("statement_timeout=45000" in sql, "master connections time out runaway SQL"),
        ("statement_timeout=10000" in manager, "manager connections fail fast"),
        ("lock_timeout=2000" in manager, "manager does not wait on slave_seen DDL"),
        ("POSTGRES_OPTIONS" in sql, "master honors POSTGRES_OPTIONS"),
    ]
    failed = 0
    for passed, label in ok:
        print(f"{'pass' if passed else 'FAIL'}: {label}")
        if not passed:
            failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
