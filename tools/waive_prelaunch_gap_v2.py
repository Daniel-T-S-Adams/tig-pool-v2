#!/usr/bin/env python3
"""Record the one-time, auditable prelaunch missing-block credit waiver."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pool_manager.pool_v2.block_observer import BlockStore
from pool_manager.pool_v2.database import Database


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--height", required=True, type=int)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--evidence-json", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        evidence = json.loads(args.evidence_json.read_text())
    except (OSError, json.JSONDecodeError) as error:
        parser.error(f"cannot read evidence JSON: {type(error).__name__}")
    if not isinstance(evidence, dict) or not evidence:
        parser.error("evidence JSON must contain a nonempty object")
    database = Database(os.environ.get("POOL_V2_DATABASE_DSN"))
    result = BlockStore(database).waive_prelaunch_gap(args.height, actor=args.actor,
        reason=args.reason, evidence=evidence)
    print(json.dumps(result, default=str, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
