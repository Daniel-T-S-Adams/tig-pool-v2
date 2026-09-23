#!/usr/bin/env python3
"""Append an audited CPU test phase; preserve the original total budget and history."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pool_manager.pool_v2 import pilot
from pool_manager.pool_v2.database import Database


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--expected-phase',type=int,required=True)
    parser.add_argument('--actor',required=True)
    parser.add_argument('--reason',required=True)
    args=parser.parse_args(argv)
    database=Database(os.environ.get('POOL_V2_DATABASE_DSN'))
    pilot.extend(database,json.loads(Path(args.config).read_text()),
        expected_phase=args.expected_phase,actor=args.actor,reason=args.reason)
    print(json.dumps(pilot.status(database)))
    return 0


if __name__=='__main__':raise SystemExit(main())
