#!/usr/bin/env python3
"""Run the isolated v2 coordinator against an already migrated v2 database."""

import argparse
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from pool_manager.pool_v2.coordinator import Coordinator
from pool_manager.pool_v2.database import Database,lock
from pool_manager.pool_v2.members import address
from pool_manager.pool_v2.observation import PublicTigClient
from pool_manager.pool_v2.tig_transport import TigSubmissionClient


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args(argv)
    config=json.loads(Path(args.config).read_text())
    database=Database(os.environ.get('POOL_V2_DATABASE_DSN'))
    origin=config['tig_api_url']
    enabled=config.get('submissions_enabled',False)
    new_work=config.get('new_work_enabled',False)
    if type(enabled) is not bool or type(new_work) is not bool:
        parser.error('submission and new-work flags must be explicit booleans')
    key=os.environ.get('POOL_V2_TIG_API_KEY')
    if enabled and not key:parser.error('enabled submissions require POOL_V2_TIG_API_KEY in the service environment')
    if new_work and not enabled:parser.error('new work requires an enabled, configured submission service')
    if new_work and not config.get('public_origin'):parser.error('new work requires the HTTPS member API public_origin for saved artifacts')
    coordinator=Coordinator(database,address(config['pool_player_id']),PublicTigClient(origin),
        TigSubmissionClient(origin,key,enabled=enabled),new_work=new_work,
        binary_hosts=config.get('binary_hosts'),max_age=config.get('max_block_age',120),artifact_origin=config.get('public_origin'))
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    log=logging.getLogger('innopool-v2-coordinator')
    stop=threading.Event()
    for number in (signal.SIGINT,signal.SIGTERM):signal.signal(number,lambda *_:stop.set())
    while not stop.is_set():
        try:
            # Only this service uses this lock. All financial transactions and
            # potentially-sent fences remain durable in their own transactions.
            with database.transaction() as cursor:
                lock(cursor,'coordinator-cycle')
                result=coordinator.step()
            log.info('coordinator progress: %s',json.dumps(result))
            if args.once:return 0
        except Exception as error:
            log.error('coordinator interrupted (%s); durable submissions remain available for reconciliation',type(error).__name__)
            if args.once:return 1
        stop.wait(3)
    return 0


if __name__=='__main__':raise SystemExit(main())
