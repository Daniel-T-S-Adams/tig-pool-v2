#!/usr/bin/env python3
"""Capture public TIG fee balances and confirm already-paid operator top-ups."""

import argparse
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from pool_manager.pool_v2 import funding,topups
from pool_manager.pool_v2.database import Database
from pool_manager.pool_v2.money import FundsError
from pool_manager.pool_v2.observation import PublicTigClient
from pool_manager.pool_v2.spool import Spool


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-url',default='https://mainnet-api.tig.foundation')
    parser.add_argument('--player-id',required=True)
    parser.add_argument('--spool',required=True)
    parser.add_argument('--poll-seconds',type=float,default=10)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--replay-only',action='store_true')
    args=parser.parse_args(argv)
    if not 0<args.poll_seconds<=60:parser.error('polling interval must be greater than zero and at most 60 seconds')
    database=Database(os.environ.get('POOL_V2_DATABASE_DSN'));spool=Spool(args.spool)
    stop=threading.Event()
    for name in (signal.SIGINT,signal.SIGTERM):signal.signal(name,lambda *_:stop.set())
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    logger=logging.getLogger('innopool-v2-funding')
    def record_pending():
        complete=True
        for path in spool.pending():
            if stop.is_set():return False
            try:
                data,_,_=spool.read(path)
                if data['player_id']!=args.player_id.lower():raise FundsError('spool belongs to another benchmarker')
                result=funding.record(database,data)
                if result['complete']:
                    with database.transaction() as cursor:
                        cursor.execute("SELECT id FROM protocol_topups WHERE state='awaiting_protocol' ORDER BY sent_at")
                        pending=cursor.fetchall()
                    for row in pending:
                        try:topups.credit(database,row['id'],result['capture_id'])
                        except FundsError:continue  # Keep waiting for this exact protocol confirmation.
                else:complete=False
                spool.recorded(path)
            except Exception as failure:
                complete=False
                logger.warning('funding evidence retained for replay: %s',type(failure).__name__)
        return complete
    if args.replay_only:return 0 if record_pending() else 1
    def drain():
        while not stop.is_set():
            record_pending()
            stop.wait(2)
    drainer=threading.Thread(target=drain,name='funding-recorder',daemon=True)
    drainer.start()
    try:
        while not stop.is_set():
            data=funding.capture(PublicTigClient(args.api_url),args.player_id)
            spool.save(data,{'purpose':'public-protocol-funding'},data['error'])
            if args.once:
                deadline=time.monotonic()+30
                while spool.pending() and not stop.is_set() and time.monotonic()<deadline:stop.wait(.1)
                return 0 if not spool.pending() and funding.status(database)['ready'] else 1
            stop.wait(args.poll_seconds)
    finally:
        stop.set();drainer.join(timeout=1)
    return 0


if __name__=='__main__':raise SystemExit(main())
