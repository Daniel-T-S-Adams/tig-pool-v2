#!/usr/bin/env python3
"""Unit checks: AUTOPILOT_WEIGHT_LOCK keeps live-config algo weights."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load_fns():
    path = pathlib.Path(__file__).resolve().parents[1] / "pool_manager" / "pool" / "autopilot.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    wanted = {
        "locked_algo_weight",
        "_find_algo_selection",
        "_apply_workload_target",
        "_workload_cooldown_state",
        "_rollback_last_canary",
    }
    nodes = [
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    found = {n.name for n in nodes}
    if found != wanted:
        raise RuntimeError(f"missing helpers: wanted {wanted}, found {found}")
    ns = {
        "WORKLOAD_MIN_BUNDLES": 4,
        "WORKLOAD_MIN_BATCH_SIZE": 8,
        "WORKLOAD_MIN_WEIGHT": 1,
        "ROOT_BACKLOG_DRAIN_MIN_PER_CHALLENGE": 1,
        "WEIGHT_LOCK": True,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns


def _cfg(weight: int = 5) -> dict:
    return {
        "algo_selection": [
            {
                "algorithm_id": "c003_a137",
                "weight": weight,
                "track_settings": {
                    "n_vars=10000,ratio=4267": {
                        "num_bundles": 8,
                        "batch_size": 32,
                    }
                },
            }
        ],
        "per_challenge_max_benchmarks": {"c003": 16},
    }


def _target(current_weight: int = 5, target_weight: int = 4, bundles: int = 7) -> dict:
    return {
        "algorithm_id": "c003_a137",
        "track": "n_vars=10000,ratio=4267",
        "action": "reduce_workload_until_proofs_convert",
        "reasons": ["proof conversion is below target"],
        "current": {
            "weight": current_weight,
            "num_bundles": 8,
            "effective_batch_size": 32,
            "per_challenge_max_benchmarks": 16,
        },
        "target": {
            "weight": target_weight,
            "num_bundles": bundles,
            "effective_batch_size": 32,
            "per_challenge_max_benchmarks": 16,
        },
        "derived": {"min_bundle_floor": 4, "min_batch_size": 8},
    }


def main() -> int:
    ns = _load_fns()
    locked_fn = ns["locked_algo_weight"]
    apply_fn = ns["_apply_workload_target"]
    rollback_fn = ns["_rollback_last_canary"]
    failed = 0

    def check(ok: bool, label: str, detail="") -> None:
        nonlocal failed
        print(f"{'pass' if ok else 'FAIL'}: {label}{(' -> ' + str(detail)) if detail else ''}")
        if not ok:
            failed += 1

    check(
        locked_fn(5, 4, locked=True) == 5,
        "lock keeps current weight even when proof conversion is poor",
    )
    check(
        locked_fn(5, 4, locked=False) == 4,
        "unlock still allows a weight drop",
    )
    check(locked_fn(1, 1, locked=True) == 1, "SAT weight 1 stays 1")

    ns["WEIGHT_LOCK"] = True
    cfg = _cfg(5)
    change = apply_fn(cfg, _target(5, 4, 7))
    check(cfg["algo_selection"][0]["weight"] == 5, "apply lock does not write weight")
    check(
        change is not None and "weight" not in (change.get("changes") or {}),
        "apply lock omits weight from changes",
        change,
    )
    check(
        (change or {}).get("changes", {}).get("num_bundles", {}).get("next") == 7,
        "apply lock still writes bundle drain",
        change,
    )

    weight_only = _target(5, 4, 8)
    cfg = _cfg(5)
    change = apply_fn(cfg, weight_only)
    check(change is None, "apply lock with only a weight target is a no-op")
    check(cfg["algo_selection"][0]["weight"] == 5, "weight-only apply leaves live weight")

    ns["WEIGHT_LOCK"] = False
    cfg = _cfg(5)
    change = apply_fn(cfg, _target(5, 4, 8))
    check(cfg["algo_selection"][0]["weight"] == 4, "apply unlock writes the weight drop")
    check(
        (change or {}).get("changes", {}).get("weight", {}).get("next") == 4,
        "apply unlock records the weight change",
        change,
    )

    ns["WEIGHT_LOCK"] = True
    cfg = _cfg(4)
    report = {
        "generated_at_ms": 1,
        "reward_funnel": {"summary": {"issues": ["low_proof_conversion"]}},
        "workload_cooldown": {
            "last_change": {
                "canary": True,
                "algorithm_id": "c003_a137",
                "track": "n_vars=10000,ratio=4267",
                "changes": {"weight": {"current": 5, "next": 4}},
            }
        },
    }
    rollback = rollback_fn(
        cfg,
        report,
        {"healthy": False},
        False,
        {"posture": "recovery"},
    )
    check(cfg["algo_selection"][0]["weight"] == 4, "rollback lock does not restore old weight")
    check(
        rollback is None or "weight" not in (rollback.get("changes") or {}),
        "rollback lock omits weight",
        rollback,
    )

    ns["WEIGHT_LOCK"] = False
    cfg = _cfg(4)
    rollback = rollback_fn(
        cfg,
        report,
        {"healthy": False},
        False,
        {"posture": "recovery"},
    )
    check(cfg["algo_selection"][0]["weight"] == 5, "rollback unlock restores previous weight")

    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
