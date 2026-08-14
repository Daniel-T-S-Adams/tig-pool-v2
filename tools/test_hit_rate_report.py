#!/usr/bin/env python3
"""Unit checks for hit-rate aggregation helpers."""

from __future__ import annotations

import ast
import pathlib
import sys


def _load_fns():
    path = pathlib.Path(__file__).resolve().parents[1] / "pool_manager" / "pool" / "hit_rate_report.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    wanted = {
        "_max_quality",
        "_qualifier_floors",
        "annotate_job",
        "aggregate_rows",
        "_median",
        "_p90",
        "_as_list",
        "_configured_bundles",
        "_index_by_benchmark_id",
    }
    nodes = [
        node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted
    ]
    found = {n.name for n in nodes}
    if found != wanted:
        raise RuntimeError(f"missing helpers: wanted {wanted}, found {found}")
    ns = {
        "defaultdict": __import__("collections").defaultdict,
        "json": __import__("json"),
        "statistics": __import__("statistics"),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns


def main() -> int:
    ns = _load_fns()
    failed = 0

    def check(ok: bool, label: str, detail="") -> None:
        nonlocal failed
        print(f"{'pass' if ok else 'FAIL'}: {label}{(' -> ' + str(detail)) if detail else ''}")
        if not ok:
            failed += 1

    check(ns["_max_quality"]([10, 20, 15]) == 20, "max quality from list")
    check(ns["_max_quality"]("[1, 9, 3]") == 9, "max quality from json string")

    floors = ns["_qualifier_floors"](
        [
            {
                "id": "c004",
                "block_data": {
                    "qualifier_qualities_by_track": {
                        "n_queries=15000": [77649, 77675, 77712],
                    }
                },
            }
        ]
    )
    check(floors[("c004", "n_queries=15000")] == 77649, "floor is min qualifier quality", floors)

    job = ns["annotate_job"](
        {
            "benchmark_id": "abc",
            "challenge": "c004",
            "algorithm_id": "c004_a100",
            "track": "n_queries=15000",
            "solution_quality": [77000, 77507, 77400],
            "block_started": 100,
            "start_time": 1_000_000,
            "end_time": 1_042_000,
            "proof_submit_time": 1_050_000,
        },
        floors=floors,
        configured={("c004_a100", "n_queries=15000"): 4},
        precommits={"abc": {"details": {"num_bundles": 6}}},
        proofs={"abc": {"state": {"block_confirmed": 114}, "details": {"submission_delay": 14}}},
    )
    check(job["num_bundles"] == 6, "prefers TIG precommit bundles", job["num_bundles"])
    check(job["max_nonce_quality"] == 77507, "max nonce quality")
    check(job["qualifier_floor"] == 77649, "joins live floor")
    check(job["hit"] is False and job["gap"] == 77507 - 77649, "below-floor job is a miss")
    check(job["blocks_to_proof"] == 14, "blocks to proof from confirmed-start")
    check(abs(job["wall_clock_sec"] - 42.0) < 0.01, "wall clock from start/end")

    over = dict(job)
    over["max_nonce_quality"] = 78000
    over["hit"] = True
    over["gap"] = 78000 - 77649
    rows = ns["aggregate_rows"]([job, over])
    check(len(rows) == 1, "groups same track+bundles")
    check(rows[0]["hits"] == 1 and rows[0]["jobs_vs_floor"] == 2, "hit rate 1/2", rows[0])
    check(rows[0]["max_nonce_quality_best"] == 78000, "best max quality")

    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
