#!/usr/bin/env python3
"""Continuously record finalized custody transfers; never sign or send tokens."""

import argparse
import hashlib
import logging
import os
from pathlib import Path
import signal
import sys
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from pool_manager.pool_v2 import chain_observer
from pool_manager.pool_v2.chain import Network,Rpc
from pool_manager.pool_v2.database import Database
from pool_manager.pool_v2.money import FundsError
from pool_manager.pool_v2.spool import Spool


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chain-id',type=int,required=True)
    parser.add_argument('--token',required=True)
    parser.add_argument('--custody',required=True)
    parser.add_argument('--confirmations',type=int,required=True)
    parser.add_argument('--start-height',type=int,help='first custody block; must precede initial wallet funding/use')
    parser.add_argument('--batch-size',type=int,default=1000)
    parser.add_argument('--spool',required=True)
    parser.add_argument('--poll-seconds',type=float,default=10)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--replay-only',action='store_true')
    args=parser.parse_args(argv)
    if not 1<=args.batch_size<=1000 or not 0<args.poll_seconds<=60 or (args.start_height is not None and args.start_height<1):
        parser.error('invalid batch size, starting height or polling interval')
    url=os.environ.get('POOL_V2_CUSTODY_RPC_URL')
    if not url and not args.replay_only:parser.error('POOL_V2_CUSTODY_RPC_URL is required for capture')
    network=Network(args.chain_id,args.token,args.custody,args.confirmations)
    database=Database(os.environ.get('POOL_V2_DATABASE_DSN'))
    spool=Spool(args.spool)
    stop=threading.Event()
    for name in (signal.SIGINT,signal.SIGTERM):signal.signal(name,lambda *_:stop.set())
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    logger=logging.getLogger('innopool-v2-custody')
    def drain():
        pending=[]
        complete=True
        for path in spool.pending():
            data,_,_=spool.read(path)
            pending.append((data['first'],data['checked_at'],path,data))
        for _,_,path,data in sorted(pending):
            try:
                result=chain_observer.record(database,data,initialize=args.start_height==data['first'])
                spool.recorded(path)
                logger.info('custody capture recorded: %s',result)
            except Exception as failure:
                logger.warning('custody evidence retained: %s',type(failure).__name__)
                # A validated failure is already archived in the database. Keep
                # collecting fresh ranges while reconciliation remains blocked.
                if isinstance(failure,FundsError):
                    spool.recorded(path)
                    complete=False
                else:raise
        return complete
    while not stop.is_set():
        try:
            complete=drain()
            if args.replay_only:return 0 if complete and not spool.pending() else 1
            state=chain_observer.status(database)
            if state['initialized']:
                if state['stream']['network']!=chain_observer.asdict(network):raise ValueError('configured custody network differs from recorded stream')
                first=state['stream']['last_height']+1
            elif args.start_height is not None:first=args.start_height
            else:raise ValueError('initial custody capture needs --start-height before first wallet funding')
            data=chain_observer.capture(network,Rpc(url),first,count=args.batch_size,
                source='rpc:'+hashlib.sha256(url.encode()).hexdigest()[:16])
            spool.save(data,{'purpose':'finalized-custody-capture'},data['error'])
            drain()
            state=chain_observer.status(database)
            if args.once:return 0 if state['ready'] and not spool.pending() else 1
        except Exception as failure:
            logger.warning('custody collection will retry without advancing: %s',type(failure).__name__)
            if args.once or args.replay_only:return 1
        stop.wait(args.poll_seconds)
    return 0


if __name__=='__main__':raise SystemExit(main())
