#!/usr/bin/env python3
"""Show or append the reviewed missing-block tolerance; earlier revisions stay recorded."""
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
    parser.add_argument("--max-missing-per-round", type=int,
                        help="missing heights a round may have and still settle (each counts as zero credit)")
    parser.add_argument("--warn-missing-per-round", type=int,
                        help="missing heights above which status warns; at most the settlement limit")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    args = parser.parse_args(argv)
    store = BlockStore(Database(os.environ.get("POOL_V2_DATABASE_DSN")))
    change = [args.max_missing_per_round, args.warn_missing_per_round, args.actor, args.reason]
    if any(value is None for value in change):
        if any(value is not None for value in change):
            parser.error("a policy change needs --max-missing-per-round, --warn-missing-per-round, --actor and --reason")
        result = store.gap_policy()
    else:
        try:
            result = store.set_gap_policy(max_missing_per_round=args.max_missing_per_round,
                warn_missing_per_round=args.warn_missing_per_round, actor=args.actor, reason=args.reason)
        except ValueError as error:
            parser.error(str(error))
    print(json.dumps(result, default=str, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
