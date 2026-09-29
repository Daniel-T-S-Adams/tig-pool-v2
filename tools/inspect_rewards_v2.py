#!/usr/bin/env python3
"""Read finalized TokenLocker evidence; never sign, transfer or credit balances."""
import argparse
import gzip
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pool_manager.pool_v2.chain import Chain, Network, Rpc
from pool_manager.pool_v2 import reward_locker


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rpc', required=True)
    parser.add_argument('--chain-id', type=int, required=True)
    parser.add_argument('--token', required=True)
    parser.add_argument('--wallet', required=True)
    parser.add_argument('--locker', required=True)
    parser.add_argument('--code-sha256', required=True)
    parser.add_argument('--transaction')
    parser.add_argument('--round', type=int)
    parser.add_argument('--emissions', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--request-interval', type=float, default=2,
                        help='minimum seconds between RPC reads (default: 2)')
    args = parser.parse_args(argv)
    if not 0 <= args.request_interval <= 60:
        parser.error('--request-interval must be between 0 and 60 seconds')
    if bool(args.round) != bool(args.emissions) or (args.emissions and not args.transaction):
        parser.error('--round and --emissions require each other and --transaction')
    if args.output.exists():
        parser.error('use a new output path to preserve previous evidence')
    provider = Rpc(args.rpc)
    previous = 0
    def rpc(method, params):
        nonlocal previous
        time.sleep(max(0, args.request_interval - (time.monotonic() - previous)))
        previous = time.monotonic()
        return provider(method, params)
    chain = Chain(Network(args.chain_id, args.token, args.wallet, 1), rpc)
    result = {'snapshot': reward_locker.snapshot(chain, args.locker, args.code_sha256)}
    if args.transaction:
        result['distribution'] = reward_locker.events(chain, args.locker, args.code_sha256, args.transaction)
    if args.emissions:
        opener = gzip.open if args.emissions.suffix == '.gz' else open
        with opener(args.emissions, 'rt') as source:
            payload = json.load(source)
        result['comparison'] = reward_locker.compare_distribution(
            args.round, payload.get('payload', payload), result['distribution'])
    with args.output.open('x') as output:
        json.dump(result, output, indent=2)
        output.write('\n')
    print(json.dumps({'evidence': str(args.output), 'snapshot': result['snapshot'],
                     'comparison': result.get('comparison')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
