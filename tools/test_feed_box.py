#!/usr/bin/env python3
"""Feed-the-box: take fat leftovers, skip crumbs on an empty seat."""

from __future__ import annotations

import ast
import pathlib


def _load_fns(rel: str, *names: str, extra_ns: dict | None = None):
    path = pathlib.Path(__file__).resolve().parents[1] / rel
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    keep = []
    want = set(names)
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in want:
            keep.append(node)
    if {n.name for n in keep} != want:
        raise RuntimeError(f"missing in {rel}: {want - {n.name for n in keep}}")
    ns = dict(extra_ns or {})
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns, ns)
    return ns


def main() -> int:
    failed = 0

    def check(ok: bool, label: str) -> None:
        nonlocal failed
        print(f"{'pass' if ok else 'FAIL'}: {label}")
        if not ok:
            failed += 1

    ns = _load_fns(
        "master/slave_manager.py",
        "batch_remaining_nonces",
        "poller_worker_count",
        "leftover_feeds_box",
        "leftover_is_crumb",
        "leftover_finishes_job",
        "leftover_finish_bids",
        "unfinished_roots_by_job",
        "cpu_pack_seats_open",
        "leftover_takeable_by_poller",
        "owner_idle_unlocks_sticky",
        "should_skip_crumb_for_empty_seat",
        "takeable_unassigned_by_bid",
        "claimable_has_fat_leftover",
        "feed_leftover_rank",
        "same_job_fill_allows",
        "leftover_nonces_by_job",
        "unassigned_roots_by_job",
        "pick_fill_bid",
        "assigned_crumb_should_release",
        "split_assigned_crumbs",
        extra_ns={
            "Optional": __import__("typing").Optional,
            "Dict": __import__("typing").Dict,
        },
    )
    feeds = ns["leftover_feeds_box"]
    crumb = ns["leftover_is_crumb"]
    skip = ns["should_skip_crumb_for_empty_seat"]
    finish_bids = ns["leftover_finish_bids"]
    has_fat = ns["claimable_has_fat_leftover"]
    rank = ns["feed_leftover_rank"]
    same_job = ns["same_job_fill_allows"]
    takeable = ns["leftover_takeable_by_poller"]
    takeable_map = ns["takeable_unassigned_by_bid"]
    pick = ns["pick_fill_bid"]
    leftover_nonces = ns["leftover_nonces_by_job"]
    unassigned = ns["unassigned_roots_by_job"]
    workers = ns["poller_worker_count"]
    release = ns["assigned_crumb_should_release"]
    split_crumbs = ns["split_assigned_crumbs"]

    check(
        workers({"num_workers": 25}, is_gpu=False) == 25,
        "Pica telem workers stay 25",
    )
    check(
        workers({"num_workers": 190}, is_gpu=False) == 190,
        "EPYC telem workers stay 190",
    )
    check(
        workers({}, is_gpu=True) == 1,
        "GPU with no telem defaults to 1 worker",
    )

    check(
        feeds(remaining_nonces=12, unassigned_on_job=1, workers=25, empty_seats=1)
        is False,
        "Pica 25w + 12-nonce leftover is a crumb",
    )
    check(
        feeds(remaining_nonces=64, unassigned_on_job=1, workers=25, empty_seats=1)
        is True,
        "Pica 25w + 64-nonce leftover is fat",
    )
    check(
        feeds(remaining_nonces=80, unassigned_on_job=1, workers=25, empty_seats=1)
        is True,
        "Pica 25w + 80-nonce leftover is fat",
    )
    check(
        feeds(remaining_nonces=64, unassigned_on_job=5, workers=190, empty_seats=5)
        is True,
        "EPYC 190w + 5 unassigned x 64 stacks to fat",
    )
    check(
        feeds(remaining_nonces=64, unassigned_on_job=1, workers=190, empty_seats=5)
        is False,
        "EPYC 190w + one 64-nonce leftover is a crumb",
    )
    check(
        feeds(remaining_nonces=190, unassigned_on_job=1, workers=190, empty_seats=5)
        is True,
        "EPYC 190w + 190-nonce leftover is fat",
    )

    fat_jobs = {"fat": 80, "crumb": 1}
    fat_nonces = {"fat": 64, "crumb": 12}
    check(
        has_fat(
            unassigned_by_bid=fat_jobs,
            leftover_nonces_by_bid=fat_nonces,
            workers=25,
            empty_seats=1,
        )
        is True,
        "80-root knapsack counts as fat claimable for a Pica",
    )
    check(
        skip(
            remaining_nonces=12,
            unassigned_on_job=3,
            workers=25,
            empty_seats=1,
            poller_assigned=0,
            taking_this_poll=0,
            has_fat_claimable=True,
            sticky_own=False,
        )
        is True,
        "empty Pica skips a mid-job 12-nonce crumb when fat leftovers exist",
    )
    check(
        skip(
            remaining_nonces=24,
            unassigned_on_job=1,
            workers=25,
            empty_seats=1,
            poller_assigned=0,
            taking_this_poll=0,
            has_fat_claimable=True,
            sticky_own=False,
        )
        is False,
        "empty Pica takes the last leftover even when it is a crumb",
    )
    check(
        skip(
            remaining_nonces=16,
            unassigned_on_job=1,
            workers=25,
            empty_seats=1,
            poller_assigned=0,
            taking_this_poll=0,
            has_fat_claimable=True,
            sticky_own=False,
        )
        is False,
        "empty Pica takes a 16-nonce last leftover",
    )
    check(
        skip(
            remaining_nonces=64,
            unassigned_on_job=1,
            workers=25,
            empty_seats=1,
            poller_assigned=0,
            taking_this_poll=0,
            has_fat_claimable=True,
            sticky_own=False,
        )
        is False,
        "empty Pica takes a 64-nonce leftover",
    )
    check(
        skip(
            remaining_nonces=64,
            unassigned_on_job=3,
            workers=190,
            empty_seats=5,
            poller_assigned=0,
            taking_this_poll=0,
            has_fat_claimable=True,
            sticky_own=False,
        )
        is True,
        "empty EPYC skips a mid-job 64-nonce leftover when a fat job exists",
    )
    check(
        skip(
            remaining_nonces=64,
            unassigned_on_job=1,
            workers=190,
            empty_seats=5,
            poller_assigned=0,
            taking_this_poll=0,
            has_fat_claimable=True,
            sticky_own=False,
        )
        is False,
        "empty EPYC takes the last leftover of a job",
    )
    check(
        skip(
            remaining_nonces=12,
            unassigned_on_job=1,
            workers=25,
            empty_seats=1,
            poller_assigned=0,
            taking_this_poll=0,
            has_fat_claimable=False,
            sticky_own=False,
        )
        is False,
        "no fat claimable: take the crumb",
    )
    check(
        skip(
            remaining_nonces=12,
            unassigned_on_job=1,
            workers=25,
            empty_seats=1,
            poller_assigned=0,
            taking_this_poll=0,
            has_fat_claimable=True,
            sticky_own=True,
        )
        is False,
        "sticky Pica takes the last leftover even when fat leftovers exist",
    )
    check(
        skip(
            remaining_nonces=12,
            unassigned_on_job=1,
            workers=190,
            empty_seats=5,
            poller_assigned=0,
            taking_this_poll=0,
            has_fat_claimable=True,
            sticky_own=True,
        )
        is False,
        "sticky EPYC may finish a scrap when other seats exist",
    )
    check(
        skip(
            remaining_nonces=12,
            unassigned_on_job=1,
            workers=25,
            empty_seats=1,
            is_proof=True,
            has_fat_claimable=True,
        )
        is False,
        "proofs are never skipped as crumbs",
    )

    fat_rank = rank(unassigned_on_job=80, remaining_nonces=64, original_idx=9)
    last_rank = rank(unassigned_on_job=1, remaining_nonces=32, original_idx=0)
    proof_rank = rank(unassigned_on_job=0, remaining_nonces=32, original_idx=1, is_proof=True)
    check(
        last_rank < fat_rank,
        "last leftover ranks before a fatter stranger job",
    )
    check(
        last_rank < proof_rank,
        "last leftover ranks before proofs",
    )
    check(
        finish_bids({"last": 1, "fat": 80, "done": 0}) == {"last"},
        "only the last leftover bid is a finish leftover",
    )
    check(
        finish_bids({}, {"held": 1, "fat": 3}) == {"held"},
        "assigned last leftover stays a finish leftover so it is not released",
    )
    own_rank = rank(
        unassigned_on_job=1, remaining_nonces=12, original_idx=0, sticky_own=True
    )
    check(own_rank < fat_rank, "own last leftover ranks before a fatter stranger job")

    check(
        same_job(fill_bid="A", bid="A", sticky_own=False) is True,
        "same-job fill allows the locked bid",
    )
    check(
        same_job(fill_bid="A", bid="B", sticky_own=False, unassigned_on_job=3)
        is False,
        "after fill_bid=A, reject a mid-job stranger leftover",
    )
    check(
        same_job(fill_bid="A", bid="B", sticky_own=False, unassigned_on_job=1)
        is True,
        "last leftover may join even after fill_bid=A",
    )
    check(
        same_job(fill_bid="A", bid="B", sticky_own=True) is True,
        "sticky own may leave the fill lock",
    )
    check(
        same_job(fill_bid="", bid="B", sticky_own=False) is True,
        "no fill lock yet: any job is allowed",
    )

    check(
        takeable(
            "fat",
            slave_name="pica",
            root_affinity={"fat": "other"},
            overflow_benchmark_ids=set(),
            unassigned_on_job=80,
        )
        is False,
        "fat leftover locked to another owner is not takeable",
    )
    check(
        takeable(
            "fat",
            slave_name="pica",
            root_affinity={"fat": "other"},
            overflow_benchmark_ids=set(),
            unassigned_on_job=80,
            preferred_online=False,
        )
        is True,
        "fat leftover on an offline owner is takeable",
    )
    check(
        takeable(
            "fat",
            slave_name="pica",
            root_affinity={"fat": "other"},
            overflow_benchmark_ids=set(),
            unassigned_on_job=80,
            preferred_releases=True,
        )
        is True,
        "fat leftover on a telem-idle owner is takeable",
    )
    check(
        takeable(
            "last",
            slave_name="pica",
            root_affinity={"last": "other"},
            overflow_benchmark_ids=set(),
            unassigned_on_job=1,
        )
        is True,
        "last leftover is takeable even when sticky-locked",
    )
    check(
        takeable(
            "fat",
            slave_name="pica",
            root_affinity={"fat": "other"},
            overflow_benchmark_ids={"fat"},
        )
        is True,
        "overflow-unlocked leftover is takeable",
    )
    check(
        takeable(
            "fat",
            slave_name="pool-gpu-idle",
            root_affinity={"fat": "pool-gpu-owner"},
            overflow_benchmark_ids=set(),
            unassigned_on_job=55,
        )
        is False,
        "GPU leftover stays sticky for a busy stranger card",
    )
    check(
        takeable(
            "fat",
            slave_name="pool-gpu-idle",
            root_affinity={"fat": "pool-gpu-owner"},
            overflow_benchmark_ids=set(),
            unassigned_on_job=55,
            poller_idle=True,
            poller_is_gpu=True,
        )
        is True,
        "empty GPU card may take a sticky leftover pile",
    )
    check(
        takeable(
            "fat",
            slave_name="pool-gpu-idle",
            root_affinity={"fat": "pool-gpu-owner"},
            overflow_benchmark_ids=set(),
            unassigned_on_job=55,
            preferred_at_cap=True,
        )
        is True,
        "leftover is takeable when the sticky owner is at cap",
    )
    check(
        takeable(
            "fat",
            slave_name="pica",
            root_affinity={"fat": "other"},
            overflow_benchmark_ids=set(),
            unassigned_on_job=80,
            poller_idle=True,
            poller_is_gpu=False,
        )
        is True,
        "empty CPU box may take a sticky leftover pile",
    )
    check(
        takeable(
            "fat",
            slave_name="pool-cpu-home-s02",
            root_affinity={"fat": "pool-cpu-home-s05"},
            overflow_benchmark_ids=set(),
            unassigned_on_job=80,
            poller_idle=False,
            poller_is_gpu=False,
            poller_empty_seats=5,
            poller_max_concurrent=6,
        )
        is True,
        "XL with spare seats may take a sticky leftover pile",
    )
    check(
        takeable(
            "fat",
            slave_name="pica",
            root_affinity={"fat": "other"},
            overflow_benchmark_ids=set(),
            unassigned_on_job=80,
            poller_idle=False,
            poller_is_gpu=False,
            poller_empty_seats=0,
            poller_max_concurrent=1,
        )
        is False,
        "1-seat Pica holding work does not steal a sticky leftover",
    )
    check(
        takeable(
            "fat",
            slave_name="pool-gpu-busy",
            root_affinity={"fat": "pool-gpu-owner"},
            overflow_benchmark_ids=set(),
            unassigned_on_job=55,
            poller_idle=False,
            poller_is_gpu=True,
            poller_empty_seats=1,
            poller_max_concurrent=2,
        )
        is False,
        "GPU prefetch seat does not steal sticky leftovers",
    )
    idle_gpu_map = takeable_map(
        {"fat": 55, "crumb": 1},
        slave_name="pool-gpu-idle",
        root_affinity={"fat": "pool-gpu-owner"},
        overflow_benchmark_ids=set(),
        preferred_at_cap={"pool-gpu-owner"},
        poller_idle=True,
        poller_is_gpu=True,
    )
    check(
        "fat" in idle_gpu_map,
        "takeable map unlocks GPU leftovers for an empty card when owner is at cap",
    )
    locked_fat = takeable_map(
        {"fat": 80, "crumb": 1},
        slave_name="pica",
        root_affinity={"fat": "other"},
        overflow_benchmark_ids=set(),
    )
    check(
        has_fat(
            unassigned_by_bid=locked_fat,
            leftover_nonces_by_bid=fat_nonces,
            workers=25,
            empty_seats=1,
        )
        is False,
        "sticky-locked fat does not count as fat claimable for this poller",
    )

    check(
        pick(["crumb"], {"crumb": 1, "fat": 80}, {"crumb": 12, "fat": 64})
        == "crumb",
        "held crumb stays the fill lock when no fat-claimable flag",
    )
    check(
        pick(
            ["crumb"],
            {"crumb": 3, "fat": 80},
            {"crumb": 12, "fat": 64},
            workers=25,
            empty_seats=1,
            has_fat_claimable=True,
        )
        == "",
        "do not lock remaining seats onto a mid-job crumb when fat leftovers exist",
    )
    check(
        pick(
            ["last"],
            {"last": 1, "fat": 80},
            {"last": 32, "fat": 64},
            workers=25,
            empty_seats=1,
            has_fat_claimable=True,
        )
        == "last",
        "held last leftover stays the fill lock so the job can finish",
    )
    check(
        pick(
            ["fat"],
            {"crumb": 1, "fat": 80},
            {"crumb": 12, "fat": 64},
            workers=25,
            empty_seats=1,
            has_fat_claimable=True,
        )
        == "fat",
        "held fat job stays the fill lock",
    )

    rows = [
        {
            "slave": None,
            "end_time": None,
            "batch": {
                "benchmark_id": "fat",
                "num_nonces": 64,
                "sampled_nonces": None,
            },
        },
        {
            "slave": None,
            "end_time": None,
            "batch": {
                "benchmark_id": "fat",
                "num_nonces": 64,
                "sampled_nonces": None,
            },
        },
        {
            "slave": None,
            "end_time": None,
            "batch": {
                "benchmark_id": "crumb",
                "num_nonces": 12,
                "sampled_nonces": None,
            },
        },
        {
            "slave": "busy",
            "end_time": None,
            "batch": {
                "benchmark_id": "assigned",
                "num_nonces": 64,
                "sampled_nonces": None,
            },
        },
    ]
    check(
        leftover_nonces(rows) == {"fat": 64, "crumb": 12},
        "leftover nonce map ignores assigned roots",
    )
    check(
        unassigned(rows) == {"fat": 2, "crumb": 1},
        "unassigned root counts ignore assigned roots",
    )
    check(crumb(remaining_nonces=12, workers=25, empty_seats=1) is True, "12 < 25 is crumb")
    check(
        release(
            remaining_nonces=12,
            unassigned_on_job=2,
            workers=25,
            empty_seats=1,
            has_fat_claimable=True,
        )
        is True,
        "assigned mid-job 12-nonce crumb is released when fat leftovers exist",
    )
    check(
        release(
            remaining_nonces=24,
            unassigned_on_job=0,
            workers=25,
            empty_seats=1,
            has_fat_claimable=True,
        )
        is False,
        "assigned last leftover stays on the box so the job can finish",
    )
    check(
        release(
            remaining_nonces=64,
            unassigned_on_job=1,
            workers=25,
            empty_seats=1,
            has_fat_claimable=True,
        )
        is False,
        "assigned 64-nonce leftover stays on a Pica",
    )
    check(
        release(
            remaining_nonces=12,
            unassigned_on_job=1,
            workers=25,
            empty_seats=1,
            has_fat_claimable=False,
        )
        is False,
        "keep the crumb when nothing fat is claimable",
    )
    check(
        release(
            is_proof=True,
            remaining_nonces=12,
            workers=25,
            empty_seats=1,
            has_fat_claimable=True,
        )
        is False,
        "never release a proof as a crumb",
    )
    kept, dropped = split_crumbs(
        [
            {"batch": {"benchmark_id": "crumb", "num_nonces": 12, "sampled_nonces": None}},
            {"batch": {"benchmark_id": "fat", "num_nonces": 64, "sampled_nonces": None}},
        ],
        workers=25,
        max_concurrent=1,
        unassigned_by_bid={"crumb": 2, "fat": 80},
        leftover_nonces_by_bid={"crumb": 12, "fat": 64},
        has_fat_claimable=True,
    )
    check(
        [r["batch"]["benchmark_id"] for r in dropped] == ["crumb"]
        and [r["batch"]["benchmark_id"] for r in kept] == ["fat"],
        "split drops the assigned crumb and keeps the fat root",
    )

    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
