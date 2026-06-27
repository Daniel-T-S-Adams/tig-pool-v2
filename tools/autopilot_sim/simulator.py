#!/usr/bin/env python3
"""Offline simulator for InnoPool autopilot decisions.

This script is deliberately read-only. It loads exported autopilot reports or
synthetic scenarios from JSON, calls the existing in-memory decision planner,
and prints what autopilot would do. Any accidental DB/write path fails closed.
"""
from __future__ import annotations

import argparse
import copy
import importlib
import json
import os
import sys
import types
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
POOL_MANAGER = ROOT / "pool_manager"


class OfflineSafetyError(RuntimeError):
    pass


def _forbidden(*_args: Any, **_kwargs: Any) -> None:
    raise OfflineSafetyError("autopilot simulator cannot access live services")


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return data


def _install_optional_psycopg_stub() -> None:
    """Allow importing pool.database on machines without psycopg2 installed."""
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


def _load_autopilot():
    os.environ["AUTOPILOT_MODE"] = "apply"
    _install_optional_psycopg_stub()
    sys.path.insert(0, str(POOL_MANAGER))
    autopilot = importlib.import_module("pool.autopilot")
    autopilot.AUTOPILOT_MODE = "apply"

    # Hard guard all live read/write paths. The planner should not need these
    # for exported reports; if it does, the simulator must fail rather than
    # touch the live pool.
    autopilot.db.fetch_one = _forbidden
    autopilot.db.fetch_all = _forbidden
    autopilot.db.execute = _forbidden
    autopilot.db.execute_many = _forbidden
    autopilot.db.get_setting = _forbidden
    autopilot.db.set_setting = _forbidden
    autopilot._fetch_master_config = _forbidden
    autopilot._push_config = _forbidden
    autopilot._save_decision = _forbidden
    autopilot._cleanup_stale_assignments = _forbidden
    return autopilot


def _config_from_report(report: dict) -> dict:
    config = copy.deepcopy(report.get("config") or report.get("master_config") or {})
    current = report.get("current_config") or {}
    if not config:
        config = copy.deepcopy(current)

    # Exported reports contain a summary config. That is enough for the current
    # planner paths, but normalize missing containers so synthetic reports can be
    # compact.
    config.setdefault("max_concurrent_benchmarks", current.get("max_concurrent_benchmarks", 0))
    config.setdefault("max_job_batches", current.get("max_job_batches"))
    config.setdefault("max_batches_per_benchmark", current.get("max_batches_per_benchmark"))
    config.setdefault("resource_slots", current.get("resource_slots") or {"slots": {}})
    config.setdefault("per_challenge_max_benchmarks", current.get("per_challenge_max_benchmarks") or {})
    config.setdefault("adaptive_slave_caps", current.get("adaptive_slave_caps") or {})
    config.setdefault("slaves", current.get("slaves") or [])
    return config


def _active_jobs_from_report(report: dict) -> int:
    funnel = (report.get("reward_funnel") or {}).get("summary") or {}
    if funnel.get("active_benchmarks") is not None:
        return int(funnel.get("active_benchmarks") or 0)
    challenges = report.get("challenges") or []
    return sum(int(row.get("active_benchmarks") or 0) for row in challenges)


def _scenario_to_report_and_config(data: dict) -> tuple[dict, dict, int]:
    if "report" in data:
        report = copy.deepcopy(data["report"])
    else:
        report = copy.deepcopy(data)
    config = copy.deepcopy(data.get("config") or _config_from_report(report))
    clean_windows = int(data.get("clean_windows", report.get("clean_windows", 0)) or 0)
    return report, config, clean_windows


def _enrich_workload_targets(autopilot: Any, report: dict, config: dict, clean_windows: int) -> None:
    if not config or not report.get("track_workload"):
        return
    if not report.get("policy_posture"):
        health = autopilot._health_summary(report)
        report["policy_posture"] = autopilot._policy_posture(
            report,
            health,
            report.get("capacity_model") or {},
            clean_windows,
        )
    if not report.get("track_economics"):
        report["track_economics"] = autopilot._track_config_economics(
            config,
            report.get("track_workload") or [],
        )
    if not report.get("workload_targets"):
        report["workload_targets"] = autopilot._workload_controller_targets(
            config,
            report.get("track_economics") or [],
            report.get("reward_funnel") or {},
            report.get("policy_posture") or {},
        )


def _slots_from_report(report: dict) -> dict:
    slots = report.get("slots") or {}
    if isinstance(slots, dict):
        return slots
    if isinstance(slots, list):
        return {"summary": slots}
    return {"summary": []}


def _enrich_recommendations(autopilot: Any, report: dict, config: dict) -> None:
    if report.get("recommendations") is not None:
        return
    report["recommendations"] = autopilot._recommendations(
        config,
        report.get("slaves") or [],
        report.get("challenges") or [],
        _slots_from_report(report),
        report.get("track_economics") or [],
        report.get("stale_totals") or {},
        report.get("reward_funnel") or {},
        report.get("workload_targets") or {},
    )


def _changed_max(decision: dict) -> tuple[int | None, int | None]:
    change = (decision.get("changes") or {}).get("max_concurrent_benchmarks") or {}
    current = change.get("current")
    next_value = change.get("next")
    return (
        int(current) if current is not None else None,
        int(next_value) if next_value is not None else None,
    )


def _workload_target(report: dict, algorithm_id: str, track: str) -> dict:
    targets = ((report.get("workload_targets") or {}).get("targets") or [])
    for row in targets:
        if row.get("algorithm_id") == algorithm_id and row.get("track") == track:
            return row
    return {}


def _direction(current: int | None, target: int | None) -> str:
    if current is None or target is None or target == current:
        return "flat"
    return "up" if target > current else "down"


def _sum_slots(slots: dict | None, slot_types: list[str]) -> int:
    slots = slots or {}
    return sum(int(slots.get(slot_type) or 0) for slot_type in slot_types)


def _algo_from_config(config: dict, algorithm_id: str) -> dict:
    for algo in config.get("algo_selection") or []:
        if algo.get("algorithm_id") == algorithm_id:
            return algo
    return {}


def _validate_decision(data: dict, report: dict, decision: dict) -> list[dict]:
    requested = list(data.get("assertions") or [])
    funnel_safe = bool(((report.get("reward_funnel") or {}).get("summary") or {}).get("safe_to_scale_workload", True))
    if not funnel_safe and "must_not_scale_when_funnel_unsafe" not in requested:
        requested.append("must_not_scale_when_funnel_unsafe")
    if "must_not_apply" not in requested:
        requested.append("must_not_apply")

    results = []
    for assertion in requested:
        if assertion == "must_not_apply":
            ok = not bool(decision.get("applied"))
            detail = "offline simulator decisions must not be marked applied"
        elif assertion == "must_not_scale_when_funnel_unsafe":
            current, next_value = _changed_max(decision)
            ok = next_value is None or current is None or next_value <= current
            detail = f"max_concurrent_benchmarks current={current} next={next_value}"
        elif isinstance(assertion, dict) and assertion.get("expect_reason"):
            expected = assertion["expect_reason"]
            ok = decision.get("reason") == expected
            detail = f"expected={expected} actual={decision.get('reason')}"
        elif isinstance(assertion, dict) and assertion.get("expect_reason_in"):
            expected = assertion["expect_reason_in"]
            ok = decision.get("reason") in expected
            detail = f"expected_one_of={expected} actual={decision.get('reason')}"
        elif isinstance(assertion, dict) and assertion.get("expect_change_key"):
            key = assertion["expect_change_key"]
            ok = key in (decision.get("changes") or {})
            detail = f"changes={list((decision.get('changes') or {}).keys())}"
        elif isinstance(assertion, dict) and assertion.get("expect_no_change_key"):
            key = assertion["expect_no_change_key"]
            ok = key not in (decision.get("changes") or {})
            detail = f"changes={list((decision.get('changes') or {}).keys())}"
        elif isinstance(assertion, dict) and assertion.get("expect_max_direction"):
            expected = assertion["expect_max_direction"]
            current, next_value = _changed_max(decision)
            if expected == "up":
                ok = current is not None and next_value is not None and next_value > current
            elif expected == "down":
                ok = current is not None and next_value is not None and next_value < current
            elif expected == "flat":
                ok = next_value is None or current is None or next_value == current
            else:
                ok = False
            detail = f"direction={expected} current={current} next={next_value}"
        elif isinstance(assertion, dict) and assertion.get("expect_max_step_lte") is not None:
            limit = int(assertion["expect_max_step_lte"])
            current, next_value = _changed_max(decision)
            ok = next_value is None or current is None or abs(next_value - current) <= limit
            detail = f"limit={limit} current={current} next={next_value}"
        elif isinstance(assertion, dict) and assertion.get("expect_workload_action"):
            spec = assertion["expect_workload_action"]
            row = _workload_target(report, spec["algorithm_id"], spec["track"])
            ok = row.get("action") == spec["action"]
            detail = f"expected={spec['action']} actual={row.get('action')} row_found={bool(row)}"
        elif isinstance(assertion, dict) and assertion.get("expect_workload_target_direction"):
            spec = assertion["expect_workload_target_direction"]
            row = _workload_target(report, spec["algorithm_id"], spec["track"])
            field = spec["field"]
            current = (row.get("current") or {}).get(field)
            target = (row.get("target") or {}).get(field)
            actual = _direction(
                int(current) if current is not None else None,
                int(target) if target is not None else None,
            )
            ok = actual == spec["direction"]
            detail = f"field={field} expected={spec['direction']} actual={actual} current={current} target={target}"
        elif isinstance(assertion, dict) and assertion.get("expect_resource_slot_sum_direction"):
            spec = assertion["expect_resource_slot_sum_direction"]
            change = (decision.get("changes") or {}).get("resource_slots.slots") or {}
            current = _sum_slots(change.get("current"), spec["slot_types"])
            next_value = _sum_slots(change.get("next"), spec["slot_types"])
            actual = _direction(current, next_value)
            ok = actual == spec["direction"]
            detail = f"slot_types={spec['slot_types']} expected={spec['direction']} actual={actual} current={current} next={next_value}"
        elif isinstance(assertion, dict) and assertion.get("expect_config_track_setting"):
            spec = assertion["expect_config_track_setting"]
            config = decision.get("config") or {}
            algo = _algo_from_config(config, spec["algorithm_id"])
            track_settings = (algo.get("track_settings") or {}).get(spec["track"]) or {}
            actual = track_settings.get(spec["field"])
            ok = actual == spec["value"]
            detail = f"field={spec['field']} expected={spec['value']} actual={actual}"
        elif isinstance(assertion, dict) and assertion.get("expect_config_algo_weight"):
            spec = assertion["expect_config_algo_weight"]
            config = decision.get("config") or {}
            algo = _algo_from_config(config, spec["algorithm_id"])
            actual = algo.get("weight")
            ok = actual == spec["value"]
            detail = f"expected={spec['value']} actual={actual}"
        elif isinstance(assertion, dict) and assertion.get("expect_unserved_stranded_fields"):
            spec = assertion["expect_unserved_stranded_fields"]
            health = decision.get("health") or {}
            rows = health.get("unserved_stranded_benchmarks") or []
            row = next(
                (
                    item
                    for item in rows
                    if item.get("benchmark_id") == spec.get("benchmark_id")
                    or item.get("benchmark") == spec.get("benchmark")
                ),
                {},
            )
            mismatches = {
                key: {"expected": value, "actual": row.get(key)}
                for key, value in (spec.get("fields") or {}).items()
                if row.get(key) != value
            }
            ok = bool(row) and not mismatches
            detail = f"row_found={bool(row)} mismatches={mismatches}"
        else:
            ok = False
            detail = f"unknown assertion: {assertion!r}"
        results.append({"assertion": assertion, "passed": ok, "detail": detail})
    return results


def run_simulation(data: dict) -> dict:
    autopilot = _load_autopilot()
    report, config, clean_windows = _scenario_to_report_and_config(data)
    _enrich_workload_targets(autopilot, report, config, clean_windows)
    _enrich_recommendations(autopilot, report, config)
    active_jobs = _active_jobs_from_report(report)
    autopilot._active_unfinished_jobs = lambda: active_jobs
    decision = autopilot._plan_config_change(report, config, clean_windows)
    assertions = _validate_decision(data, report, decision)
    decision["simulator"] = {
        "offline": True,
        "active_jobs_from_report": active_jobs,
        "clean_windows": clean_windows,
        "assertions": assertions,
        "workload_targets": report.get("workload_targets"),
    }
    return decision


def _print_summary(name: str, decision: dict) -> None:
    health = decision.get("health") or {}
    print(f"Scenario: {name}")
    print(f"  healthy: {decision.get('healthy')}")
    print(f"  reward_funnel_safe: {decision.get('reward_funnel_safe')}")
    print(f"  reason: {decision.get('reason')}")
    print(f"  clean_windows: {(decision.get('simulator') or {}).get('clean_windows')}")
    print(f"  active_jobs: {(decision.get('simulator') or {}).get('active_jobs_from_report')}")
    print(f"  unserved_stranded: {len(health.get('unserved_stranded_benchmarks') or [])}")
    print(f"  capacity_waiting: {len(health.get('capacity_waiting_benchmarks') or [])}")

    changes = decision.get("changes") or {}
    if not changes:
        print("  changes: none")
    else:
        print("  changes:")
        for key, value in changes.items():
            print(f"    - {key}: {json.dumps(value, sort_keys=True)}")

    guardrails = decision.get("guardrails") or {}
    if guardrails:
        print("  guardrails:")
        for key, value in guardrails.items():
            print(f"    - {key}: {json.dumps(value, sort_keys=True)}")

    workload_targets = (((decision.get("simulator") or {}).get("workload_targets") or {}).get("actionable") or [])
    if workload_targets:
        print("  workload_actions:")
        for row in workload_targets:
            print(
                "    - "
                f"{row.get('algorithm_id')}:{row.get('track')} "
                f"{row.get('action')} "
                f"current={json.dumps(row.get('current'), sort_keys=True)} "
                f"target={json.dumps(row.get('target'), sort_keys=True)}"
            )

    assertions = (decision.get("simulator") or {}).get("assertions") or []
    if assertions:
        print("  assertions:")
        for result in assertions:
            status = "pass" if result.get("passed") else "FAIL"
            print(f"    - {status}: {result.get('assertion')} ({result.get('detail')})")


def _run_one(path: Path, emit_json: bool) -> tuple[dict, list[dict]]:
    data = _load_json(path)
    decision = run_simulation(data)
    if data.get("lesson") and not emit_json:
        print(f"Lesson: {data['lesson']}")
    if emit_json:
        print(json.dumps(decision, indent=2, sort_keys=True))
    else:
        _print_summary(data.get("name") or str(path), decision)
    failed = [
        result
        for result in (decision.get("simulator") or {}).get("assertions") or []
        if not result.get("passed")
    ]
    return decision, failed


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline InnoPool autopilot simulator")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--report", type=Path, help="exported admin.py autopilot --json report")
    source.add_argument("--scenario", type=Path, help="synthetic scenario JSON")
    source.add_argument("--all", action="store_true", help="run every scenario JSON in the scenarios directory")
    parser.add_argument(
        "--scenarios-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "scenarios",
        help="directory used by --all",
    )
    parser.add_argument("--json", action="store_true", help="print full decision JSON")
    args = parser.parse_args()

    if args.all:
        paths = sorted(args.scenarios_dir.glob("*.json"))
        if not paths:
            raise SystemExit(f"no scenarios found in {args.scenarios_dir}")
        failures = []
        for index, path in enumerate(paths):
            if index and not args.json:
                print()
            _decision, failed = _run_one(path, args.json)
            if failed:
                failures.append({"path": str(path), "failed": failed})
        if not args.json:
            print()
            print(f"Batch summary: {len(paths) - len(failures)}/{len(paths)} scenarios passed")
            if failures:
                print("Failed scenarios:")
                for failure in failures:
                    print(f"  - {failure['path']}")
                    for result in failure["failed"]:
                        print(f"    * {result.get('assertion')}: {result.get('detail')}")
        if failures:
            return 2
        return 0

    path = args.report or args.scenario
    _decision, failed = _run_one(path, args.json)
    if failed:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
