#!/usr/bin/env python3
"""Convert legacy full snapshots; optionally prune backed compact replacements.

Apply migrations 020–022 and the updated grants first. Backfill is additive.
Before --prune, take the next verified database backup so it contains those
compact copies. This tool does not waive gaps or move financial balances.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pool_manager.pool_v2 import compaction, retention
from pool_manager.pool_v2.database import Database
from pool_manager.pool_v2.members import address


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--player-id', required=True)
    parser.add_argument('--limit', type=int, default=200)
    parser.add_argument('--after-height', type=int, default=-1, help='resume backfill past a reviewed batch; held blocks retain their raw data')
    parser.add_argument('--backup-proof')
    parser.add_argument('--prune', action='store_true')
    parser.add_argument('--collect-unused-chunks', action='store_true')
    parser.add_argument('--chunk-limit', type=int, default=5000, help='maximum database chunk deletions per run')
    parser.add_argument('--block-spool', action='append', default=[])
    parser.add_argument('--report-spool', action='append', default=[])
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 2000:
        parser.error('limit must be between 1 and 2000')
    if not 1 <= args.chunk_limit <= 50000:
        parser.error('chunk-limit must be between 1 and 50000')
    if (args.prune or args.collect_unused_chunks or args.block_spool or args.report_spool) and not args.backup_proof:
        parser.error('pruning requires a verified --backup-proof')
    database = Database(os.environ.get('POOL_V2_DATABASE_DSN'))
    record = {'backfill': compaction.backfill(database, player_id=address(args.player_id), limit=args.limit,
        after_height=args.after_height)}
    if args.backup_proof:
        proof = retention.backup_proof(args.backup_proof, now=datetime.now(timezone.utc))
        if args.prune:
            record['prune'] = compaction.prune(database, proof=proof, limit=args.limit)
        if args.collect_unused_chunks:
            record['chunks'] = compaction.collect_unused_chunks(database, proof=proof, limit=args.chunk_limit)
        for spool in args.block_spool:
            record['block_spool:' + spool] = retention.prune_compacted_spool(database, spool, proof=proof,
                limit=args.limit, collect_chunks=args.collect_unused_chunks)
        for spool in args.report_spool:
            record['report_spool:' + spool] = retention.prune_report_spool(database, spool, proof=proof,
                limit=args.limit, collect_chunks=args.collect_unused_chunks)
    print(json.dumps(record), flush=True)
    return 1 if record['backfill']['held'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
