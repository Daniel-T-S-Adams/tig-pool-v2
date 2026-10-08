#!/usr/bin/env python3
"""Expire bounded-life evidence under the recorded retention policy.

Reads the admin DSN from POOL_V2_DATABASE_DSN. Requires a verified backup
record (the recovery host's latest.json) unless --dry-run; nothing captured
after that backup started is expired. See docs/EVIDENCE_RETENTION.md.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pool_manager.pool_v2 import retention
from pool_manager.pool_v2.database import Database


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-proof", help="latest.json of a verified base backup")
    parser.add_argument("--max-proof-age-hours", type=float, default=36)
    parser.add_argument("--policy", help="JSON file overriding retention.POLICY values")
    parser.add_argument("--spool", action="append", default=[], help="a block spool directory on this host to prune too")
    parser.add_argument("--max-blocks", type=int, default=5000, help="capture manifests to expire per run")
    parser.add_argument("--dry-run", action="store_true", help="report the floors a run would set; change nothing")
    parser.add_argument("--initialize-chunk-index", action="store_true",
                        help="one-time start of chunk_last_ref on a database recorded before migration 018")
    args = parser.parse_args(argv)
    database = Database(os.environ.get("POOL_V2_DATABASE_DSN"))
    if args.initialize_chunk_index:
        print(json.dumps(retention.initialize_chunk_index(database)), flush=True)
        return 0
    now = datetime.now(timezone.utc)
    policy = dict(retention.POLICY)
    if args.policy:
        policy.update(json.loads(Path(args.policy).read_text()))
    if args.backup_proof:
        proof = retention.backup_proof(args.backup_proof, now=now, max_age_hours=args.max_proof_age_hours)
    elif args.dry_run:
        proof = {"snapshot": None, "completed_at_utc": None, "covered_until": now.isoformat(), "source_host": None, "files": None}
    else:
        parser.error("--backup-proof is required unless --dry-run")
    record = retention.run(database, proof=proof, policy=policy, now=now, spools=args.spool,
                           dry_run=args.dry_run, max_blocks=args.max_blocks)
    print(json.dumps(record, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
