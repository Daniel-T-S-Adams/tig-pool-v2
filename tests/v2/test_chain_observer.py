from copy import deepcopy
from datetime import datetime,timedelta,timezone
import importlib.util
import gzip
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from pool_manager.pool_v2 import chain_observer,controls,deposits,ledger
from pool_manager.pool_v2.chain import TRANSFER_TOPIC
from pool_manager.pool_v2.database import Database
from pool_manager.pool_v2.money import Conflict,FundsError,TIG
from pool_manager.pool_v2.spool import Spool
from funds_helpers import DatabaseCase,NETWORK,WALLET,OTHER,CUSTODY,TOKEN


class CustodyRpc:
    def __init__(self):
        self.latest=120
        self.final=102
        self.logs=[]
        self.omit_logs=False
        self.fail_logs=False
        self.hashes={}
        self.native={}
        self.nonces={}
        self.chain_id=NETWORK.chain_id
        self.code='0x'
        self.now=int(datetime.now(timezone.utc).timestamp())

    def hash(self,height):return self.hashes.get(height,'0x'+f'{height:064x}')

    def header(self,height):
        return {'number':hex(height),'hash':self.hash(height),'parentHash':self.hash(height-1),
            'timestamp':hex(self.now-(self.latest-height)*2)}

    def add(self,*,sender=WALLET,recipient=CUSTODY,amount=100*TIG,height=100):
        row={'address':TOKEN,'transactionHash':'0x'+uuid.uuid4().hex*2,'logIndex':'0x0','blockNumber':hex(height),
            'blockHash':self.hash(height),'removed':False,'data':'0x'+f'{amount:064x}',
            'topics':[TRANSFER_TOPIC,'0x'+'0'*24+sender[2:],'0x'+'0'*24+recipient[2:]]}
        self.logs.append(row)
        return row

    def rpc(self,method,params):
        if method=='eth_chainId':return hex(self.chain_id)
        if method=='eth_call':
            if params[0]['data']=='0x313ce567':return '0x12'
            height=int(params[1],16)
            amount=sum((int(row['data'],16) if row['topics'][2][-40:]==CUSTODY[2:] else 0)
                -(int(row['data'],16) if row['topics'][1][-40:]==CUSTODY[2:] else 0)
                for row in self.logs if int(row['blockNumber'],16)<=height)
            return '0x'+f'{amount:064x}'
        if method=='eth_getBlockByNumber':
            height=self.latest if params[0]=='latest' else self.final if params[0]=='finalized' else int(params[0],16)
            return self.header(height)
        if method=='eth_getLogs':
            if self.fail_logs:raise ConnectionError('fixture outage')
            if self.omit_logs:return []
            rule=params[0]
            return deepcopy([row for row in self.logs if int(rule['fromBlock'],16)<=int(row['blockNumber'],16)<=int(rule['toBlock'],16)
                and all(value is None or row['topics'][index]==value for index,value in enumerate(rule['topics']))])
        if method=='eth_getTransactionReceipt':
            rows=[row for row in self.logs if row['transactionHash']==params[0]]
            first=rows[0]
            return {'transactionHash':params[0],'status':'0x1','blockNumber':first['blockNumber'],
                'blockHash':first['blockHash'],'logs':deepcopy(rows)}
        if method in ('eth_getBalance','eth_getTransactionCount'):
            amounts=self.native if method=='eth_getBalance' else self.nonces
            height=int(params[1],16)
            return hex(amounts[max([key for key in amounts if key<=height])]) if any(key<=height for key in amounts) else '0x0'
        if method=='eth_getCode':return self.code
        raise AssertionError((method,params))


class RecordedCustodyTests(unittest.TestCase):
    def test_public_finalized_tig_transfer_replays_without_an_rpc(self):
        data=json.loads(gzip.decompress((Path(__file__).with_name('fixtures')/'custody-block-51572936.json.gz').read_bytes()))
        value=chain_observer.verify(data)
        self.assertEqual((value['first'],value['last']),(51572936,51572936))
        self.assertEqual(value['network'].chain_id,8453)
        self.assertEqual(len(value['transfers']),1)
        self.assertEqual(value['closing']['tig']-value['opening']['tig'],1151417509939734543)
        self.assertEqual(value['transfers'][0].amount,1151417509939734543)
        changed=deepcopy(data)
        closing=next(call for call in changed['calls'] if call['method']=='eth_call'
            and call['params'][0]['data'].startswith('0x70a08231') and call['params'][1]==hex(51572936))
        closing['result']='0x'+f"{int(closing['result'],16)+1:064x}"
        with self.assertRaisesRegex(Conflict,'balance change'):chain_observer.verify(changed)


class ChainObserverTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.source=CustodyRpc()

    def capture(self,first=100,count=1000):
        return chain_observer.capture(NETWORK,self.source.rpc,first,count=count,source='fixture')

    def test_unknown_delegation_is_observed_but_cannot_make_custody_ready(self):
        self.source.add()
        self.source.code='0xef0100'+'7'*40
        data=self.capture()
        self.assertEqual(data['version'],2)
        self.assertIsNone(data['error'])
        result=chain_observer.record(self.db,data,initialize=True)
        self.assertFalse(result['healthy'])
        self.assertIn('delegation',result['reason'])
        self.assertEqual(chain_observer.status(self.db)['check']['custody_code'],self.source.code)
        self.assertEqual(self.balance()['available'],100*TIG)
        self.assertEqual(controls.blocked(self.db),'custody-reconciliation')
        old=deepcopy(data);old['version']=1
        with self.assertRaises(FundsError):chain_observer.verify(old)

    def test_arbitrary_contract_code_still_cannot_advance_custody_capture(self):
        self.source.add()
        self.source.code='0x6000'
        data=self.capture()
        self.assertIsNotNone(data['error'])
        with self.assertRaises(FundsError):chain_observer.record(self.db,data,initialize=True)
        self.assertFalse(chain_observer.status(self.db)['initialized'])

    def test_finalized_deposits_replay_from_archive_once_and_unknown_sources_stay_unattributed(self):
        self.source.add(amount=100*TIG+1)
        self.source.add(sender='0x'+'8'*40,amount=7*TIG,height=101)
        captured=self.capture()
        self.assertIsNone(captured['error'])
        with tempfile.TemporaryDirectory(prefix='innopool-v2-custody-spool-') as directory:
            spool=Spool(directory)
            path=spool.save(captured,{'fixture':True})
            saved,_,_=spool.read(path)
            result=chain_observer.record(self.db,saved,initialize=True)
            self.assertTrue(result['healthy'],result)
            self.assertEqual(self.balance()['available'],100*TIG+1)
            self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='unattributed:TIG'")['balance'],7*TIG)
            self.assertTrue(chain_observer.record(Database(self.db.dsn),saved)['replayed'])
            self.assertEqual(chain_observer.read(self.db,result['capture_id']),captured)
            spool.recorded(path)
            self.assertEqual(spool.pending(),[])
        self.assertEqual(self.row('SELECT count(*) AS n FROM transfers')['n'],2)
        self.assertEqual(self.row("SELECT count(*) AS n FROM journals WHERE kind='custody_receipt'")['n'],2)
        self.assertTrue(chain_observer.status(self.db)['ready'])
        self.assertIsNone(controls.blocked(self.db))

    def test_incomplete_or_omitted_logs_cannot_advance_or_credit(self):
        self.source.add()
        self.source.omit_logs=True
        missing=self.capture()
        self.assertEqual(missing['error'],'Conflict')
        with self.assertRaises(FundsError):chain_observer.record(self.db,missing,initialize=True)
        self.assertEqual(self.balance()['available'],0)
        self.assertFalse(chain_observer.status(self.db)['initialized'])
        self.source.omit_logs=False
        self.source.fail_logs=True
        failed=self.capture()
        self.assertEqual(failed['error'],'ConnectionError')
        with self.assertRaises(FundsError):chain_observer.record(self.db,failed,initialize=True)
        self.assertEqual(self.row('SELECT count(*) AS n FROM chain_captures')['n'],2)
        self.source.fail_logs=False
        self.assertTrue(chain_observer.record(self.db,self.capture(),initialize=True)['healthy'])
        self.assertEqual(self.balance()['available'],100*TIG)

    def test_crash_after_credit_before_cursor_commit_is_recoverable_without_double_credit(self):
        self.source.add()
        captured=self.capture()
        actual=deposits.receive
        def crashed(database,transfer):
            actual(database,transfer)
            raise RuntimeError('fixture crash after committed receipt')
        with patch.object(deposits,'receive',side_effect=crashed):
            with self.assertRaises(RuntimeError):chain_observer.record(self.db,captured,initialize=True)
        self.assertEqual(self.balance()['available'],100*TIG)
        self.assertEqual(chain_observer.status(self.db)['stream']['last_height'],99)
        self.assertEqual(controls.blocked(self.db),'custody-reconciliation')
        with self.assertRaisesRegex(Conflict,'custody'):self.reserve()
        self.assertTrue(chain_observer.record(self.db,captured)['healthy'])
        self.assertEqual(self.balance()['available'],100*TIG)
        self.assertEqual(self.row("SELECT count(*) AS n FROM journals WHERE kind='custody_receipt'")['n'],1)

    def test_finality_limits_the_range_and_a_fresh_empty_capture_refreshes_health(self):
        self.source.add()
        unfinalized=self.source.add(amount=20*TIG,height=103)
        first=self.capture()
        result=chain_observer.record(self.db,first,initialize=True)
        self.assertEqual(result['height'],102)
        self.assertEqual(self.balance()['available'],100*TIG)
        heartbeat=self.capture(103)
        self.assertIsNone(heartbeat['error'])
        self.assertTrue(chain_observer.record(self.db,heartbeat)['healthy'])
        self.source.final=104
        later=chain_observer.record(self.db,self.capture(103))
        self.assertEqual(later['height'],104)
        self.assertEqual(self.balance()['available'],120*TIG)
        self.assertEqual(self.row('SELECT count(*) AS n FROM transfers WHERE tx_hash=%s',(unfinalized['transactionHash'],))['n'],1)

    def test_unmatched_outgoing_or_native_funding_blocks_work_without_charging_members(self):
        self.source.add()
        chain_observer.record(self.db,self.capture(),initialize=True)
        self.source.add(sender=CUSTODY,recipient=OTHER,amount=20*TIG,height=103)
        self.source.final=104
        self.source.nonces[103]=1
        self.source.native[103]=500
        result=chain_observer.record(self.db,self.capture(103))
        self.assertFalse(result['healthy'])
        self.assertEqual(self.balance()['available'],100*TIG)
        self.assertEqual(self.balance(self.other)['available'],0)
        self.assertEqual(controls.blocked(self.db),'custody-reconciliation')
        controls.set_pause(self.db,False,actor='operator',reason='resume',event_key='resume')
        self.assertEqual(controls.blocked(self.db),'custody-reconciliation')
        with self.assertRaisesRegex(Conflict,'custody'):self.reserve()

    def test_recorded_canonical_conflict_is_latched_even_after_another_capture(self):
        self.source.add()
        chain_observer.record(self.db,self.capture(),initialize=True)
        self.source.final=104
        self.source.hashes[102]='0x'+'f'*64
        changed=self.capture(103)
        with self.assertRaisesRegex(Conflict,'anchor changed'):chain_observer.record(self.db,changed)
        self.source.hashes.pop(102)
        result=chain_observer.record(self.db,self.capture(103))
        self.assertFalse(result['healthy'])
        self.assertEqual(self.row("SELECT count(*) AS n FROM chain_alerts WHERE kind='canonical-conflict'")['n'],1)
        self.assertEqual(chain_observer.status(self.db)['stream']['last_height'],104)

    def test_self_transfer_has_no_receipt_credit_and_archive_tampering_is_rejected(self):
        self.source.add()
        self.source.add(sender=CUSTODY,recipient=CUSTODY,amount=20*TIG,height=101)
        captured=self.capture()
        self.assertTrue(chain_observer.record(self.db,captured,initialize=True)['healthy'])
        self.assertEqual(self.balance()['available'],100*TIG)
        self.assertEqual(self.row("SELECT count(*) AS n FROM journals WHERE kind='custody_receipt'")['n'],1)
        corrupted=deepcopy(captured)
        corrupted['calls'][0]['method']='eth_sendTransaction'
        with self.assertRaisesRegex(FundsError,'reordered'):chain_observer.record(self.db,corrupted)
        self.assertEqual(self.balance()['available'],100*TIG)
        self.assertFalse(chain_observer.status(self.db)['ready'])

    def test_initial_collection_cannot_skip_prior_wallet_funding_or_use_a_different_network(self):
        self.source.add(height=99)
        with self.assertRaisesRegex(Conflict,'before the wallet'):chain_observer.record(self.db,self.capture(),initialize=True)
        self.assertFalse(chain_observer.status(self.db)['initialized'])
        self.source.chain_id=1
        captured=self.capture()
        with self.assertRaises(FundsError):chain_observer.record(self.db,captured,initialize=True)
        self.assertFalse(chain_observer.status(self.db)['initialized'])

    def test_concurrent_capture_replay_and_backfill_never_enable_work_before_catching_up(self):
        self.source.add()
        captures=[self.capture(count=1),self.capture(count=1)]
        results=self.concurrent([lambda data=data:chain_observer.record(self.db,data,initialize=True) for data in captures])
        self.assertTrue(all(isinstance(result,dict) for result in results),results)
        self.assertEqual(self.balance()['available'],100*TIG)
        self.assertEqual(self.row('SELECT count(*) AS n FROM chain_batches')['n'],1)
        self.assertEqual(chain_observer.status(self.db)['stream']['last_height'],100)
        self.assertFalse(chain_observer.status(self.db)['ready'])
        with self.assertRaisesRegex(Conflict,'custody'):self.reserve()
        self.assertTrue(chain_observer.record(self.db,self.capture(101))['healthy'])
        self.assertTrue(chain_observer.status(self.db)['ready'])

    def test_stale_offline_replay_cannot_enable_new_spending(self):
        class Past(datetime):
            @classmethod
            def now(cls,tz=None):return datetime.now(tz)-timedelta(minutes=3)
        self.source.add()
        self.source.now-=180
        with patch.object(chain_observer,'datetime',Past):captured=self.capture()
        result=chain_observer.record(self.db,captured,initialize=True)
        self.assertTrue(result['healthy'])  # Balances reconcile at that historical point.
        self.assertFalse(chain_observer.status(self.db)['ready'])
        self.assertEqual(controls.blocked(self.db),'custody-reconciliation')
        self.source.now+=180
        self.assertTrue(chain_observer.record(self.db,self.capture(103))['healthy'])
        self.assertTrue(chain_observer.status(self.db)['ready'])

    def test_collector_command_replays_saved_rpc_evidence_without_network_calls(self):
        self.source.add()
        captured=self.capture()
        spec=importlib.util.spec_from_file_location('custody_collector_fixture',Path(__file__).resolve().parents[2]/'tools/observe_custody_v2.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='innopool-v2-custody-cli-') as directory:
            spool=Spool(directory);spool.save(captured,{'fixture':True})
            arguments=['--chain-id','8453','--token',TOKEN,'--custody',CUSTODY,'--confirmations','12',
                '--start-height','100','--spool',directory,'--replay-only']
            with patch.dict(os.environ,{'POOL_V2_DATABASE_DSN':self.db.dsn}),patch.object(module,'Rpc') as rpc,patch.object(module.signal,'signal'):
                self.assertEqual(module.main(arguments),0)
                rpc.assert_not_called()
            self.assertEqual(spool.pending(),[])
        self.assertEqual(self.balance()['available'],100*TIG)

    def test_failed_saved_attempt_does_not_starve_a_later_complete_capture(self):
        self.source.add()
        self.source.fail_logs=True
        failed=self.capture()
        self.source.fail_logs=False
        valid=self.capture()
        spec=importlib.util.spec_from_file_location('custody_collector_recovery',Path(__file__).resolve().parents[2]/'tools/observe_custody_v2.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='innopool-v2-custody-recovery-') as directory:
            spool=Spool(directory)
            spool.save(failed,{'fixture':True});spool.save(valid,{'fixture':True})
            arguments=['--chain-id','8453','--token',TOKEN,'--custody',CUSTODY,'--confirmations','12',
                '--start-height','100','--spool',directory,'--replay-only']
            with patch.dict(os.environ,{'POOL_V2_DATABASE_DSN':self.db.dsn}),patch.object(module,'Rpc') as rpc,patch.object(module.signal,'signal'):
                self.assertEqual(module.main(arguments),1)  # Reports the failed attempt as well as making progress.
                rpc.assert_not_called()
            self.assertEqual(spool.pending(),[])
        self.assertEqual(self.balance()['available'],100*TIG)
        self.assertTrue(chain_observer.status(self.db)['ready'])
        self.assertEqual(self.row('SELECT count(*) AS n FROM chain_captures')['n'],2)
