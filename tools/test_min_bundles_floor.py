#!/usr/bin/env python3
"""Unit checks: per-challenge / per-track autopilot bundle floors."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load_fns():
    path = pathlib.Path(__file__).resolve().parents[1] / "pool_manager" / "pool" / "autopilot.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    wanted = {
        "_env_track_key",
        "_min_bundles_for_track",
        "_decrease_bundles",
        "_decrease_backlog_bundles",
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
        "WORKLOAD_MAX_BUNDLE_STEP": 1,
        "ROOT_BACKLOG_DRAIN_MIN_BUNDLES": 1,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns


def main() -> int:
    ns = _load_fns()
    env_key = ns["_env_track_key"]
    min_fn = ns["_min_bundles_for_track"]
    decrease = ns["_decrease_bundles"]
    decrease_backlog = ns["_decrease_backlog_bundles"]
    failed = 0

    def check(ok: bool, label: str, detail="") -> None:
        nonlocal failed
        print(f"{'pass' if ok else 'FAIL'}: {label}{(' -> ' + str(detail)) if detail else ''}")
        if not ok:
            failed += 1

    check(env_key("n_queries=7000") == "N_QUERIES_7000", "vector track key")
    check(env_key("n_hidden=14") == "N_HIDDEN_14", "neuralnet track key")
    check(
        env_key("n_vars=10000,ratio=4267") == "N_VARS_10000_RATIO_4267",
        "knapsack track key",
    )

    check(
        min_fn("c004", "n_queries=7000", environ={}, default_floor=4) == 4,
        "global floor when no override",
    )
    check(
        min_fn(
            "c004",
            "n_queries=7000",
            environ={"AUTOPILOT_MIN_BUNDLES_C004": "12"},
            default_floor=4,
        )
        == 12,
        "challenge floor beats global",
    )
    check(
        min_fn(
            "c004_somealgo",
            "n_queries=7000",
            environ={
                "AUTOPILOT_MIN_BUNDLES_C004": "12",
                "AUTOPILOT_MIN_BUNDLES_C004_N_QUERIES_7000": "16",
            },
            default_floor=4,
        )
        == 16,
        "track floor beats challenge",
    )
    check(
        min_fn(
            "c006",
            "n_hidden=18",
            environ={"AUTOPILOT_MIN_BUNDLES_C004": "12"},
            default_floor=4,
        )
        == 4,
        "other challenge is unaffected",
    )
    check(
        min_fn(
            "c005",
            "n_nodes=200000",
            environ={"AUTOPILOT_MIN_BUNDLES_C005": ""},
            default_floor=4,
        )
        == 4,
        "blank challenge override is ignored",
    )

    check(decrease(16, 12) == 15, "decrease steps down toward floor")
    check(decrease(12, 12) == 12, "decrease stops at operator floor")
    check(decrease(8, 12) == 8, "decrease does not raise; enforce path does that")
    check(decrease_backlog(8, 12) == 8, "backlog drain also respects operator floor")
    check(decrease_backlog(20, 16) == 19, "backlog drain can still step down above floor")

    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
