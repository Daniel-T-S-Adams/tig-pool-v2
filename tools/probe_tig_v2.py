#!/usr/bin/env python3
"""Capture and validate consecutive TIG blocks without submitting any work."""

import argparse
from collections import Counter
from datetime import datetime, timezone
from fractions import Fraction
import json
import math
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pool_manager.pool_v2.observation import (  # noqa: E402
    CaptureError, PublicTigClient, capture_snapshot, write_archive,
)
from pool_manager.pool_v2.protocol import (  # noqa: E402
    ProtocolDataError, equal_bundle_credit, validate_reports, validate_snapshot,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="https://mainnet-api.tig.foundation")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--snapshots", type=int, default=2)
    parser.add_argument("--max-wait", type=float, default=240)
    parser.add_argument("--poll-interval", type=float, default=3)
    parser.add_argument("--max-block-age", type=float, default=180)
    parser.add_argument("--reports-round", type=int, action="append", default=[])
    args = parser.parse_args(argv)
    timing = (args.max_wait, args.poll_interval, args.max_block_age)
    if args.snapshots < 1 or any(not math.isfinite(value) or value <= 0 for value in timing):
        parser.error("counts and timing limits must be positive")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("use an empty output directory to preserve previous probe evidence")
    args.output.mkdir(parents=True, exist_ok=True)
    client = PublicTigClient(args.api_url)
    accepted = []
    attempts = 0
    deadline = time.monotonic() + args.max_wait
    last = None
    had_gap = False
    while len(accepted) < args.snapshots and time.monotonic() < deadline:
        observation = {}
        started_at = datetime.now(timezone.utc).isoformat()
        record_offset = len(client.records)
        try:
            start = client.get("/get-block", {"include_data": "true"})
            if last and start["block"]["id"] == last.block_id:
                time.sleep(args.poll_interval)
                continue
            attempts += 1
            observation = capture_snapshot(client, start=start)
            snapshot = validate_snapshot(observation)
            age = time.time() - snapshot.timestamp
            if age > args.max_block_age or age < -30:
                raise ProtocolDataError(f"block timestamp is not fresh (age {age:.1f}s)")
            credits = equal_bundle_credit(snapshot)
            gap = bool(last and (snapshot.height != last.height + 1 or snapshot.previous_block_id != last.block_id))
            had_gap |= gap
            metadata = {
                "schema": 1, "api_url": args.api_url, "started_at": started_at,
                "valid_snapshot": True, "gap_before": gap,
                "height": snapshot.height, "round": snapshot.round, "block_id": snapshot.block_id,
                "active_benchmarks": len(snapshot.precommits), "eligible_bundles": len(snapshot.bundles),
                "qualifying_credit": str(sum(credits.values(), Fraction())),
                "requests": client.records[record_offset:],
            }
            path = args.output / f"block-{snapshot.height}-{snapshot.block_id}.json.gz"
            write_archive(path, {"metadata": metadata, "observation": observation})
            accepted.append({key: value for key, value in metadata.items() if key != "requests"})
            print(json.dumps(accepted[-1]), flush=True)
            last = snapshot
        except (OSError, ValueError, KeyError) as error:
            if isinstance(error, CaptureError):
                observation = error.observation
            stamp = time.time_ns()
            write_archive(args.output / f"incomplete-{stamp}.json.gz", {
                "metadata": {"schema": 1, "valid_snapshot": False, "error": str(error),
                             "started_at": started_at, "requests": client.records[record_offset:]},
                "observation": observation,
            })
            print(json.dumps({"incomplete": True, "error": str(error)}), flush=True)
        if len(accepted) < args.snapshots:
            time.sleep(args.poll_interval)
    reports = {}
    report_errors = []
    for round_number in sorted(set(args.reports_round)):
        try:
            payload = client.get("/get-reports", {"round": round_number})
            outcomes = validate_reports(payload)
            reports[str(round_number)] = dict(Counter(v["result"] or "pending" for v in outcomes.values()))
            write_archive(args.output / f"reports-{round_number}.json.gz", payload)
        except Exception as error:
            report_errors.append({"round": round_number, "error": str(error)})
    summary = {
        "snapshots": accepted, "requested_snapshots": args.snapshots, "attempts": attempts,
        "consecutive": len(accepted) == args.snapshots and not had_gap,
        "reports": reports, "report_errors": report_errors,
        "scope": "read-only protocol inputs; no member ownership or funded settlement is established",
    }
    write_archive(args.output / "summary.json.gz", summary)
    print(json.dumps(summary), flush=True)
    return 0 if summary["consecutive"] and not report_errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
