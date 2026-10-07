#!/usr/bin/env python3
"""Bounded replay of already archived reports while a pool is paused.

Uses the observer role. Makes no protocol requests and cannot settle or submit
work. A concurrent normal drainer is safe because capture recording is
idempotent; either recorder may complete the final pending-file rename.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pool_manager.pool_v2 import controls, report_observer
from pool_manager.pool_v2.database import Database
from pool_manager.pool_v2.spool import Spool


def replay(database, spool, limit, seconds):
    if not controls.paused(database):
        raise RuntimeError('maintenance replay requires the recorded work pause')
    paths = []
    for path in spool.pending():
        try:
            paths.append((path.stat().st_mtime, path))
        except FileNotFoundError:
            pass  # The regular drainer completed it.
    paths.sort()
    deadline = time.monotonic() + seconds
    completed = incomplete = raced = 0
    errors = {}
    for _, path in paths[:limit]:
        if time.monotonic() >= deadline:
            break
        if completed % 100 == 0 and not controls.paused(database):
            raise RuntimeError('pool resumed; maintenance replay stopped')
        try:
            saved, metadata, error = spool.read(path)
            if 'start' in saved:
                outcome = report_observer.record(database, saved, metadata, error)
                incomplete += not outcome['complete']
            else:
                incomplete += 1
            try:
                spool.recorded(path)
            except FileNotFoundError:
                pass
            completed += 1
        except FileNotFoundError:
            if path.exists():
                # Missing content is not a successful concurrent rename.
                errors['FileNotFoundError'] = errors.get('FileNotFoundError', 0) + 1
            else:
                raced += 1
        except Exception as error:
            name = type(error).__name__
            errors[name] = errors.get(name, 0) + 1
        if completed and completed % 250 == 0:
            print(json.dumps({'completed': completed, 'incomplete': incomplete, 'errors': errors}), flush=True)
    return {'completed_at_utc': datetime.now(timezone.utc).isoformat(), 'selected': min(len(paths), limit),
            'completed': completed, 'incomplete': incomplete, 'already_recorded_concurrently': raced,
            'errors': errors, 'pending': sum(1 for _ in (spool.directory / 'pending').glob('*.json.gz'))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spool', required=True, type=Path)
    parser.add_argument('--limit', type=int, default=10000)
    parser.add_argument('--seconds', type=int, default=3600)
    args = parser.parse_args()
    if not 1 <= args.limit <= 20000 or not 1 <= args.seconds <= 7200:
        parser.error('maintenance replay must be bounded')
    database = Database(os.environ.get('POOL_V2_DATABASE_DSN'))
    result = replay(database, Spool(args.spool), args.limit, args.seconds)
    print(json.dumps(result), flush=True)
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
