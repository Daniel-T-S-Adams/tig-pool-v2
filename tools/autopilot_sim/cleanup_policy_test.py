#!/usr/bin/env python3
"""Offline test for autopilot precommit-expiry cleanup policy."""
from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
POOL_MANAGER = ROOT / "pool_manager"


def _forbidden(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError("cleanup policy test must not touch live services")


def _install_optional_psycopg_stub() -> None:
    if "psycopg2" in sys.modules:
        return
    try:
        import psycopg2  # noqa: F401
        return
    except ImportError:
        pass
    psycopg2 = types.ModuleType("psycopg2")
    extras = types.ModuleType("psycopg2.extras")
    extras.RealDictCursor = object
    psycopg2.extras = extras
    psycopg2.connect = _forbidden
    sys.modules["psycopg2"] = psycopg2
    sys.modules["psycopg2.extras"] = extras


def main() -> int:
    os.environ["AUTOPILOT_MODE"] = "apply"
    _install_optional_psycopg_stub()
    sys.path.insert(0, str(POOL_MANAGER))
    autopilot = importlib.import_module("pool.autopilot")
    autopilot.AUTOPILOT_MODE = "apply"
    autopilot.STALE_CLEANUP_ENABLED = False
    autopilot.PRECOMMIT_EXPIRY_CLEANUP_ENABLED = True
    autopilot.PRECOMMIT_ROOT_RECLAIM_AGE_MS = 50 * 60 * 1000
    autopilot.PRECOMMIT_PROOF_RECLAIM_AGE_MS = 20 * 60 * 1000
    autopilot.PRECOMMIT_ABANDON_NO_ROOT_AGE_MS = 75 * 60 * 1000

    now_ms = 100 * 60 * 1000
    rows_by_call = iter([
        [{
            "benchmark_id": "root-expiry-benchmark",
            "batch_idx": 0,
            "slave": "pool-cpu-slow",
            "start_time": 42 * 60 * 1000,
            "num_attempts": 1,
            "job_start_time": 40 * 60 * 1000,
            "challenge": "knapsack",
            "track": "n_items=1000,budget=10",
        }],
        [{
            "benchmark_id": "proof-expiry-benchmark",
            "batch_idx": 1,
            "slave": "pool-cpu-slow",
            "start_time": 75 * 60 * 1000,
            "num_attempts": 1,
            "job_start_time": 60 * 60 * 1000,
            "challenge": "satisfiability",
            "track": "n_vars=5000,ratio=4267",
        }],
        [{
            "benchmark_id": "dead-precommit-benchmark",
            "job_start_time": 20 * 60 * 1000,
            "challenge": "vehicle_routing",
            "algorithm_id": "c002_a110",
            "track": "n_nodes=1000",
            "roots_ready": 0,
            "roots_pending": 8,
        }],
    ])
    executed = []

    def fake_fetch_all(_sql: str, _params: Any = None) -> list[dict]:
        return next(rows_by_call)

    def fake_execute_many(*queries: Any) -> None:
        executed.extend(queries)

    autopilot.db.fetch_one = _forbidden
    autopilot.db.fetch_all = fake_fetch_all
    autopilot.db.execute = _forbidden
    autopilot.db.execute_many = fake_execute_many

    result = autopilot._cleanup_stale_assignments({"time_before_batch_retry": 60000}, now_ms)
    assert len(result["expiry_released_roots"]) == 1, result
    assert len(result["expiry_released_proofs"]) == 1, result
    assert len(result["stopped_precommits"]) == 1, result
    assert len(executed) == 6, executed
    print("cleanup policy test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
