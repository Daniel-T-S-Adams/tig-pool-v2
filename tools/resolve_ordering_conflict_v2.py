#!/usr/bin/env python3
"""Append a verified same-block ordering resolution; never rewrite an alert."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pool_manager.pool_v2.database import Database
from pool_manager.pool_v2.ordering_conflicts import resolve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--alert-id', type=int, required=True)
    parser.add_argument('--conflicting-attempt-id', required=True)
    parser.add_argument('--actor', required=True)
    parser.add_argument('--reason', required=True)
    args = parser.parse_args()
    result = resolve(Database(os.environ['POOL_V2_DATABASE_DSN']), **vars(args))
    print(json.dumps(result, default=str))


if __name__ == '__main__':
    main()
