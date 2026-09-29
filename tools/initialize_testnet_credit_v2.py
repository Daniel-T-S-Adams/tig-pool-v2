#!/usr/bin/env python3
"""Record a fresh testnet account's observed free fee credit once, before work."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pool_manager.pool_v2 import funding, starter_credit
from pool_manager.pool_v2.database import Database
from pool_manager.pool_v2.observation import PublicTigClient
from pool_manager.pool_v2.spool import Spool


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-url', required=True, choices=[starter_credit.API_ORIGIN])
    parser.add_argument('--player-id', required=True)
    parser.add_argument('--actor', required=True)
    parser.add_argument('--spool', required=True)
    args = parser.parse_args(argv)
    database = Database(os.environ.get('POOL_V2_DATABASE_DSN'))
    data = funding.capture(PublicTigClient(args.api_url), args.player_id)
    spool = Spool(args.spool)
    path = spool.save(data, {'purpose': 'testnet-starter-credit-setup'}, data['error'])
    result = funding.record(database, data)
    spool.recorded(path)
    if not result['complete']:
        print('Funding observation incomplete; no starter credit initialized.', file=sys.stderr)
        return 1
    credit = starter_credit.initialize(database, result['capture_id'], player_id=args.player_id, actor=args.actor)
    print(json.dumps({'source': credit['source'], 'player_id': credit['player_id'],
        'amount_units': str(credit['amount']), 'capture_id': credit['capture_id'],
        'journal_id': str(credit['journal_id']), 'custody_funds_created': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
