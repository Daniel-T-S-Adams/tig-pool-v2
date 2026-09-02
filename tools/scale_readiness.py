#!/usr/bin/env python3
"""Scale readiness checks for larger InnoPool compute rollouts.

The tool is intentionally read-only. It can capture a baseline from a live VPS,
analyze an exported autopilot report, and compare before/after reports for a
fleet rollout wave.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_DIR = ROOT / "tools" / "autopilot_sim" / "reports"

RECOMMENDED_FIRST_WAVE_ENV = {
    "AUTOPILOT_MAX_MAX_BENCHMARKS": "192",
    "AUTOPILOT_MAX_CPU_SLOTS": "128",
    "AUTOPILOT_MAX_CPU_CHALLENGE_BENCHMARKS": "32",
    "AUTOPILOT_MAX_GPU_SLOTS_PER_TYPE": "16",
    "AUTOPILOT_GPU_UNITS_PER_JOB": "4",
    "AUTOPILOT_GPU_JOB_SPARE": "4",
    "PRECOMMIT_GOVERNOR_GPU_SPARE_JOBS": "4",
    "PRECOMMIT_GOVERNOR_GPU_FLEET_SPARE": "4",
    "AUTOPILOT_MAX_GPU_CHALLENGE_BENCHMARKS": "12",
    "AUTOPILOT_MAX_BENCHMARK_UP_STEP": "4",
    "AUTOPILOT_SLOT_UP_STEP": "3",
    "AUTOPILOT_WORKLOAD_MAX_BUNDLE_STEP": "1",
}


def _run(cmd: list[str], timeout: int = 30) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        return {"ok": False, "cmd": cmd, "error": str(exc)}
    return {
        "ok": proc.returncode == 0,
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _json_cmd(cmd: list[str], timeout: int = 30) -> Any:
    result = _run(cmd, timeout=timeout)
    if not result["ok"]:
        return result
    try:
        return json.loads(result["stdout"] or "{}")
    except json.JSONDecodeError as exc:
        result["ok"] = False
        result["error"] = f"invalid JSON: {exc}"
        return result


def _compose_ps() -> Any:
    result = _run(["docker", "compose", "ps", "--format", "json"])
    if not result["ok"]:
        return result
    rows = []
    for line in result["stdout"].splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            return {"ok": False, "error": "invalid docker compose ps JSON", "stdout": result["stdout"]}
    return rows


def _db_snapshot() -> Any:
    sql = """
    SELECT jsonb_build_object(
      'database_size', pg_size_pretty(pg_database_size(current_database())),
      'job_rows', (SELECT COUNT(*) FROM job),
      'active_jobs', (SELECT COUNT(*) FROM job WHERE end_time IS NULL AND stopped IS NULL),
      'root_rows', (SELECT COUNT(*) FROM root_batch),
      'active_root_batches', (SELECT COUNT(*) FROM root_batch WHERE ready IS NULL AND start_time IS NOT NULL),
      'proof_rows', (SELECT COUNT(*) FROM proofs_batch),
      'active_proof_batches', (SELECT COUNT(*) FROM proofs_batch WHERE ready IS NULL AND start_time IS NOT NULL),
      'slot_rows', (SELECT COUNT(*) FROM benchmark_slot),
      'members', (SELECT COUNT(*) FROM pool_members WHERE active = true)
    );
    """
    result = _run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "db",
            "sh",
            "-lc",
            'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$1"',
            "sh",
            sql,
        ],
        timeout=20,
    )
    if not result["ok"]:
        return result
    raw = result["stdout"].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid DB JSON", "stdout": raw, "stderr": result["stderr"]}


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    posture = report.get("policy_posture") or {}
    funnel = (report.get("reward_funnel") or {}).get("summary") or {}
    stranded = report.get("stranded_classification") or {}
    stale = report.get("stale_totals") or {}
    current = report.get("current_config") or {}
    capacity = report.get("capacity_model") or {}
    targets = report.get("capacity_targets") or {}
    health = report.get("health") or {}

    unserved = stranded.get("unserved") or []
    active_unregistered = health.get("active_unregistered")
    if active_unregistered is None:
        active_unregistered = (posture.get("signals") or {}).get("active_unregistered", 0)

    blockers = []
    if report.get("master_config_error"):
        blockers.append(f"master_config_error: {report['master_config_error']}")
    if int(stale.get("combined") or 0) or int(stale.get("roots") or 0) or int(stale.get("proofs") or 0):
        blockers.append(f"stale work roots={stale.get('roots', 0)} proofs={stale.get('proofs', 0)}")
    if active_unregistered:
        blockers.append(f"active unregistered workers={active_unregistered}")
    if unserved:
        blockers.append(f"unserved stranded benchmarks={len(unserved)}")
    if not funnel.get("safe_to_scale_workload", True):
        blockers.append("reward funnel unsafe: " + ",".join(funnel.get("issues") or []))
    if posture.get("posture") == "recovery":
        blockers.append("policy posture is recovery")

    current_slots = ((current.get("resource_slots") or {}).get("slots") or {})
    target_slots = (targets.get("resource_slots") or {})
    current_max = current.get("max_concurrent_benchmarks")
    target_max = targets.get("max_concurrent_benchmarks")

    if blockers:
        gate = "blocked"
    elif posture.get("posture") in {"conservative", "balanced"}:
        gate = "caution"
    else:
        gate = "ready"

    remediation = []
    if int(stale.get("roots") or 0) or int(stale.get("proofs") or 0):
        remediation.append(
            "Clear stale assignments before scaling; inspect affected slaves with "
            "`python3 admin.py member-health <slave>` and enable stale cleanup only after confirming work is dead."
        )
    if unserved:
        remediation.append(
            "Do not raise workload while unserved stranded benchmarks exist; restore matching CPU/GPU capacity or stop/cleanup the orphaned benchmark."
        )
    if active_unregistered:
        remediation.append(
            "Register or deactivate active public workers that are not in pool_members before allowing capacity increases."
        )
    if not funnel.get("safe_to_scale_workload", True):
        remediation.append(
            "Wait for root work to convert into benchmark/proof submissions before workload canaries are allowed."
        )

    return {
        "gate": gate,
        "blockers": blockers,
        "remediation": remediation,
        "posture": posture.get("posture"),
        "posture_reasons": posture.get("reasons") or [],
        "active_slave_counts": report.get("active_slave_counts") or {},
        "capacity_model": {
            "active_cpu": capacity.get("active_cpu"),
            "active_gpu": capacity.get("active_gpu"),
            "productive_idle_cpu": capacity.get("productive_idle_cpu"),
            "productive_idle_gpu": capacity.get("productive_idle_gpu"),
            "cpu_pressure": capacity.get("cpu_pressure"),
            "gpu_pressure": capacity.get("gpu_pressure"),
        },
        "current": {
            "max_concurrent_benchmarks": current_max,
            "resource_slots": current_slots,
            "per_challenge_max_benchmarks": current.get("per_challenge_max_benchmarks") or {},
            "adaptive_slave_caps": current.get("adaptive_slave_caps") or {},
        },
        "targets": {
            "max_concurrent_benchmarks": target_max,
            "resource_slots": target_slots,
            "per_challenge_max_benchmarks": targets.get("per_challenge_max_benchmarks") or {},
            "adaptive_slave_caps": targets.get("adaptive_slave_caps") or {},
        },
        "headroom": {
            "max_concurrent_benchmarks": (
                int(target_max) - int(current_max)
                if target_max is not None and current_max is not None
                else None
            ),
            "cpu_slots": (
                int(target_slots.get("cpu") or 0) - int(current_slots.get("cpu") or 0)
                if target_slots
                else None
            ),
        },
        "reward_funnel": {
            "safe_to_scale_workload": funnel.get("safe_to_scale_workload"),
            "issues": funnel.get("issues") or [],
            "proof_conversion_rate": funnel.get("proof_conversion_rate"),
            "stopped_rate": funnel.get("stopped_rate"),
            "avg_time_to_proof_submit_sec": funnel.get("avg_time_to_proof_submit_sec"),
            "active_benchmarks": funnel.get("active_benchmarks"),
        },
        "stranded": {
            "unserved": len(unserved),
            "capacity_waiting": len(stranded.get("capacity_waiting") or []),
            "live_by_profile": stranded.get("live_by_profile") or {},
            "slot_capacity": stranded.get("slot_capacity") or {},
        },
        "stale_totals": stale,
    }


def _load_report(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    if "report" in data and isinstance(data["report"], dict):
        return data["report"]
    return data


def _print_summary(summary: dict[str, Any]) -> None:
    print(f"Scale gate: {summary['gate']}")
    print(f"Posture: {summary.get('posture')}")
    print(f"Active slaves: {summary.get('active_slave_counts')}")
    print(f"Reward funnel: {summary.get('reward_funnel')}")
    print(f"Stranded: {summary.get('stranded')}")
    print(f"Stale totals: {summary.get('stale_totals')}")
    print(f"Headroom: {summary.get('headroom')}")
    blockers = summary.get("blockers") or []
    if blockers:
        print("\nBlockers:")
        for blocker in blockers:
            print(f"  - {blocker}")
    else:
        print("\nBlockers: none")
    remediation = summary.get("remediation") or []
    if remediation:
        print("\nRemediation:")
        for item in remediation:
            print(f"  - {item}")


def cmd_baseline(args: argparse.Namespace) -> int:
    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    autopilot = _json_cmd([sys.executable, "admin.py", "autopilot", "--json"], timeout=30)
    baseline = {
        "captured_at_ms": int(time.time() * 1000),
        "autopilot": autopilot,
        "readiness": _summary(autopilot) if isinstance(autopilot, dict) and "generated_at_ms" in autopilot else None,
        "compose_ps": _compose_ps() if not args.skip_docker else None,
        "db": _db_snapshot() if not args.skip_docker else None,
    }
    path = out_dir / f"scale_baseline_{stamp}.json"
    path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(path)
    if baseline["readiness"]:
        print()
        _print_summary(baseline["readiness"])
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    report = _load_report(args.report)
    summary = _summary(report)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        _print_summary(summary)
    return 2 if args.fail_on_blockers and summary["gate"] == "blocked" else 0


def cmd_wave_check(args: argparse.Namespace) -> int:
    before = _summary(_load_report(args.before))
    after = _summary(_load_report(args.after))
    before_counts = before.get("active_slave_counts") or {}
    after_counts = after.get("active_slave_counts") or {}
    delta = {
        "cpu": int(after_counts.get("cpu") or 0) - int(before_counts.get("cpu") or 0),
        "gpu": int(after_counts.get("gpu") or 0) - int(before_counts.get("gpu") or 0),
    }
    result = {
        "gate": after["gate"],
        "worker_delta": delta,
        "before": before,
        "after": after,
        "regressions": [],
    }
    if after["gate"] == "blocked":
        result["regressions"].extend(after["blockers"])
    if before["reward_funnel"].get("safe_to_scale_workload") and not after["reward_funnel"].get("safe_to_scale_workload"):
        result["regressions"].append("reward funnel became unsafe after this wave")
    if int(after["stranded"].get("unserved") or 0) > int(before["stranded"].get("unserved") or 0):
        result["regressions"].append("unserved stranded benchmarks increased")

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Wave gate: {result['gate']}")
        print(f"Worker delta: CPU={delta['cpu']} GPU={delta['gpu']}")
        if result["regressions"]:
            print("Regressions:")
            for item in result["regressions"]:
                print(f"  - {item}")
        else:
            print("Regressions: none")
        print("\nAfter wave:")
        _print_summary(after)
    return 2 if result["regressions"] else 0


def cmd_print_env(args: argparse.Namespace) -> int:
    for key, value in RECOMMENDED_FIRST_WAVE_ENV.items():
        if args.export:
            print(f"export {key}={value}")
        else:
            print(f"{key}={value}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only scale readiness tooling")
    sub = parser.add_subparsers(dest="cmd", required=True)

    baseline = sub.add_parser("baseline", help="capture live autopilot/docker/DB baseline")
    baseline.add_argument("--output-dir", type=Path, default=DEFAULT_REPORT_DIR)
    baseline.add_argument("--skip-docker", action="store_true")
    baseline.set_defaults(func=cmd_baseline)

    analyze = sub.add_parser("analyze", help="summarize one autopilot JSON report")
    analyze.add_argument("report", type=Path)
    analyze.add_argument("--json", action="store_true")
    analyze.add_argument("--fail-on-blockers", action="store_true")
    analyze.set_defaults(func=cmd_analyze)

    wave = sub.add_parser("wave-check", help="compare before/after autopilot reports")
    wave.add_argument("--before", type=Path, required=True)
    wave.add_argument("--after", type=Path, required=True)
    wave.add_argument("--json", action="store_true")
    wave.set_defaults(func=cmd_wave_check)

    env = sub.add_parser("print-env", help="print conservative first-wave scale env")
    env.add_argument("--export", action="store_true")
    env.set_defaults(func=cmd_print_env)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
