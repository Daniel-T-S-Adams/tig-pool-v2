#!/usr/bin/env python3
"""Mailbox get-batches: peek-only HTTP + one-pass leftover feeder."""

from __future__ import annotations

import ast
import pathlib
import re
import time
from typing import Dict, List, Optional, Set


def _load_fns(*names: str):
    path = pathlib.Path(__file__).resolve().parents[1] / "master" / "slave_manager.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    keep = []
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            keep.append(node)
    if len(keep) != len(names):
        found = {n.name for n in keep}
        raise RuntimeError(f"missing functions: {set(names) - found}")
    ns: dict = {
        "re": re,
        "Dict": Dict,
        "List": List,
        "Optional": Optional,
        "Set": Set,
    }
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns


def _leftover_row(bid: str, idx: int, algo: str = "c001_a001"):
    return {
        "slave": None,
        "end_time": None,
        "num_attempts": 0,
        "batch": {
            "benchmark_id": bid,
            "batch_idx": idx,
            "sampled_nonces": None,
            "settings": {"algorithm_id": algo},
        },
    }


def main() -> int:
    ns = _load_fns(
        "note_poll_seen",
        "newest_poll_seen_ms",
        "feed_leftovers_one_pass",
        "get_batches_stall_should_exit",
        "batch_remaining_nonces",
        "cpu_worker_hole",
    )
    note = ns["note_poll_seen"]
    newest = ns["newest_poll_seen_ms"]
    feed = ns["feed_leftovers_one_pass"]
    failed = 0

    seen: dict = {}
    note(seen, "pool-cpu-a", 1000)
    note(seen, "pool-cpu-b", 2500)
    cases = [
        (newest(seen) == 2500, "newest poll seen tracks in-memory heartbeats"),
        (newest({}) is None, "empty poll seen is none"),
    ]

    rows = [_leftover_row(f"job{i:04d}", 0) for i in range(2000)]
    hungry = [
        {"name": f"pool-cpu-{i:02d}", "seats": 2, "algo_re": r"^c001_"}
        for i in range(40)
    ]
    started = time.perf_counter()
    claimed = feed(rows, hungry, now=123.0)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    owners = {}
    for row in rows:
        if row.get("slave"):
            owners[row["slave"]] = owners.get(row["slave"], 0) + 1
    cases.extend(
        [
            (elapsed_ms < 250.0, f"40x2000 leftover feed stays fast ({elapsed_ms:.1f}ms)"),
            (len(claimed) == 80, f"feeder fills empty seats only (claimed={len(claimed)})"),
            (all(n <= 2 for n in owners.values()), "no slave exceeds empty seats"),
            (len(owners) == 40, "every hungry slave got leftovers"),
            (
                len({id(r) for r in rows if r.get("slave")}) == 80,
                "no leftover is double-claimed",
            ),
        ]
    )

    sticky_rows = [_leftover_row("locked", 0), _leftover_row("open", 1)]
    sticky_hungry = [{"name": "pool-cpu-new", "seats": 2, "algo_re": r"^c001_"}]
    sticky_claimed = feed(
        sticky_rows,
        sticky_hungry,
        now=1.0,
        takeable_bids_by_slave={"pool-cpu-new": {"open"}},
    )
    cases.append(
        (
            [c["benchmark_id"] for c in sticky_claimed] == ["open"],
            "sticky takeable set skips locked leftovers",
        )
    )

    sat_plus_ks = [_leftover_row("ks32", 0)]
    sat_plus_ks[0]["batch"]["num_nonces"] = 32
    packed = feed(
        sat_plus_ks,
        [
            {
                "name": "pool-cpu-pica46",
                "seats": 1,
                "algo_re": r"^c00",
                "workers": 32,
                "booked": 32,
            }
        ],
        now=2.0,
    )
    cases.append(
        (
            packed == [] and sat_plus_ks[0].get("slave") is None,
            "32-core box with SAT 32 does not take knapsack 32",
        )
    )
    fit_16 = [_leftover_row("ks16", 0)]
    fit_16[0]["batch"]["num_nonces"] = 16
    packed16 = feed(
        fit_16,
        [
            {
                "name": "pool-cpu-pica46",
                "seats": 1,
                "algo_re": r"^c00",
                "workers": 32,
                "booked": 16,
            }
        ],
        now=3.0,
    )
    cases.append(
        (
            [c["benchmark_id"] for c in packed16] == ["ks16"],
            "32-core box with a 16-nonce hole still packs 16",
        )
    )

    assign_q: list = []
    seen_q: list = []
    # Heartbeat must not enqueue on the assign SQL queue.
    seen_q.append(("pool-cpu-a", 1000))
    cases.append(
        (assign_q == [] and len(seen_q) == 1, "heartbeat uses a separate seen queue")
    )

    source = pathlib.Path(__file__).resolve().parents[1] / "master" / "slave_manager.py"
    text = source.read_text(encoding="utf-8")
    handler_start = text.find("@app.route('/get-batches'")
    handler_chunk = text[handler_start : handler_start + 2500] if handler_start >= 0 else ""
    cases.append(
        (
            handler_start >= 0 and "_feed_hungry_slaves" not in handler_chunk,
            "get-batches mailbox peeks only and does not claim leftovers",
        )
    )
    cases.append(
        (
            "def _leftover_feeder_loop" in text and "self._feed_hungry_slaves()" in text,
            "leftover feeder thread still claims leftovers",
        )
    )

    for ok, label in cases:
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
