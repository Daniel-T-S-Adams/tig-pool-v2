#!/usr/bin/env python3
"""Read-only arbitration collector; no settlement, credentials or TIG submissions."""

import argparse
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pool_manager.pool_v2 import report_observer
from pool_manager.pool_v2.observation import canonical_json
import hashlib
from pool_manager.pool_v2.database import Database
from pool_manager.pool_v2.members import address
from pool_manager.pool_v2.observation import PublicTigClient
from pool_manager.pool_v2.spool import Spool


log = logging.getLogger("innopool-v2-reports")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="https://mainnet-api.tig.foundation")
    parser.add_argument("--spool", required=True)
    parser.add_argument("--collector", required=True)
    parser.add_argument("--recent-rounds", type=int, default=4, help="legacy compatibility option; collection now follows open obligations")
    parser.add_argument("--reporting-round", type=int, action="append", default=[], help="also poll an older unresolved protocol round")
    parser.add_argument("--player-id", help="public benchmarker address, for positive reporting-round associations")
    parser.add_argument("--challenge-id", action="append", default=[])
    parser.add_argument("--max-block-age", type=int, default=180)
    parser.add_argument("--poll-seconds", type=float, default=30)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.recent_rounds <= 10 or any(value <= 0 for value in args.reporting_round) or not 0 < args.poll_seconds <= 60 or args.max_block_age <= 0:
        parser.error("invalid observation range, freshness, or polling interval")
    if bool(args.player_id) != bool(args.challenge_id):
        parser.error("reporting index capture needs both player-id and challenge-id")
    if not args.player_id:
        parser.error("lean report collection requires the pool player-id and supported challenges")
    if args.player_id:
        args.player_id = address(args.player_id)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    database, spool = Database(os.environ.get("POOL_V2_DATABASE_DSN")), Spool(args.spool)
    stop = threading.Event()
    incomplete = threading.Event()
    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, lambda *_: stop.set())

    def drain():
        while not stop.is_set():
            # An unavailable old block must not prevent newer evidence recording.
            for path in spool.pending():
                if stop.is_set(): break
                try:
                    saved, metadata, error = spool.read(path)
                    if "start" not in saved:
                        # No block was available. Preserve the failed attempt in
                        # the local archive; it cannot support financial actions.
                        spool.recorded(path)
                        incomplete.set()
                        continue
                    outcome = report_observer.record(database, saved, metadata, error)
                    spool.recorded(path)
                    if not outcome["complete"]:
                        incomplete.set()
                        log.warning("incomplete reporting evidence: %s", outcome["error"])
                except Exception as failure:
                    log.warning("report evidence retained for replay: %s", type(failure).__name__)
            stop.wait(3)

    drainer = threading.Thread(target=drain, name="report-recorder", daemon=True)
    drainer.start()
    saved_inputs = {}
    try:
        while not stop.is_set():
            try:
                head = PublicTigClient(args.api_url).get("/get-block")['block']
                current = head['details']['round']
                needed = report_observer.needed_queries(database, current, player_id=args.player_id,
                    challenge_ids=args.challenge_id, extra_rounds=args.reporting_round,
                    submission_period=head['config']['reports']['submission_period'])
                with database.transaction() as cursor:
                    cursor.execute('SELECT report_id FROM confirmed_reports')
                    known_ids = {row['report_id'] for row in cursor.fetchall()}
                queries = [(number, None, None) for number in needed['reports']] + needed['indices']
                for number, player, challenge in queries:
                    if stop.is_set(): break
                    saved, metadata, error = report_observer.capture(PublicTigClient(args.api_url), number,
                        player_id=player, challenge_id=challenge, max_block_age=args.max_block_age,
                        benchmark_ids=needed['benchmark_ids'], known_report_ids=known_ids,
                        final_not_before=needed['not_before'].get(number) if not player else None)
                    if player and not error:
                        # Absence establishes nothing. Store only new positive
                        # membership for pool benchmarks, not repeated empties.
                        identities = saved.get('payload', {}).get('benchmark_ids', [])
                        if not isinstance(identities, list):
                            raise ValueError('reportable benchmark response is malformed')
                        saved['payload'] = {'benchmark_ids': sorted(set(identities) & needed['mapping_benchmark_ids'])}
                        if not saved['payload']['benchmark_ids']:
                            continue
                    key = number, player, challenge
                    # A successful final response must meet the newest required
                    # deadline. Failures deduplicate by payload/error, and retry
                    # requests continue even while an earlier file is pending.
                    signature = hashlib.sha256(canonical_json({'payload': saved.get('payload'), 'error': error,
                        'deadline': needed['deadlines'].get(number) if not player and not error else None})).hexdigest()
                    if saved_inputs.get(key) == signature:
                        continue
                    spool.save(saved, {**metadata, "collector": args.collector}, error)
                    saved_inputs[key] = signature
                    if error: incomplete.set()
            except Exception as failure:
                log.warning("report capture will retry: %s", type(failure).__name__)
                if args.once: return 1
            if args.once:
                deadline = time.monotonic()+30
                while spool.pending() and not stop.is_set() and time.monotonic() < deadline:
                    stop.wait(0.2)
                return 1 if spool.pending() or incomplete.is_set() else 0
            stop.wait(args.poll_seconds)
    finally:
        stop.set()
        drainer.join(timeout=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
