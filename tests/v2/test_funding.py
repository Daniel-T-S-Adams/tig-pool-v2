from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient

from pool_manager.pool_v2 import benchmarks,controls,custody,deposits,funding,ledger,topups,withdrawals
from pool_manager.pool_v2.api import Settings,create_app
from pool_manager.pool_v2.chain import Chain,CustodyPreflight,Network
from pool_manager.pool_v2.money import Conflict,FundsError,InsufficientFunds,TIG
from pool_manager.pool_v2.spool import Spool
from funds_helpers import DatabaseCase,NETWORK,CUSTODY,TOKEN,OTHER,transfer
from test_withdrawals import payment_fixture


TOPUP='0x'+'0'*39+'1'


def funding_capture(available=0,topup=None,*,age=0,player=CUSTODY):
    checked=datetime.now(timezone.utc)-timedelta(seconds=age)
    block={'id':uuid.uuid4().hex,'details':{'height':200,'timestamp':int(checked.timestamp())},
        'config':{'topups':{'topup_address':TOPUP,'min_topup_amount':str(5*TIG)}}}
    return {'version':1,'player_id':player,'checked_at':checked.isoformat(),'error':None,
        'start':{'block':block},'end':{'block':deepcopy(block)},
        'player_data':{'player':{'id':player,'state':{'available_fee_balance':str(available)}},
            'topups':[] if topup is None else [topup]}}


def protocol_topup(tx_hash,amount=30*TIG,*,index=2,confirmed=True):
    return {'id':uuid.uuid4().hex,'details':{'player_id':CUSTODY,'tx_hash':tx_hash,'log_idx':index,'amount':str(amount)},
        'state':{'block_confirmed':199} if confirmed else None}


class FundingTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.fund()
        receipt=transfer('0x'+'7'*40,amount=80*TIG)
        deposits.receive(self.db,receipt)
        deposits.attribute_reviewed(self.db,receipt,actor='operator',evidence={'fixture':True},operator=True)
        self.native(1000)
        self.initial=funding.record(self.db,funding_capture())['capture_id']

    def native(self,amount,nonce=1):
        _,chain,tx_hash=payment_fixture(sender='0x'+'8'*40,to=CUSTODY,value=amount,nonce=nonce)
        custody.receive_native(self.db,chain.transaction(tx_hash,fee_model='op-jovian'))

    def preflight(self,nonce=1):
        with self.db.transaction() as cursor:
            return CustodyPreflight(NETWORK,nonce,ledger.backing(cursor),ledger.backing(cursor,'NATIVE'),90,
                datetime.now(timezone.utc),{'fixture':True})

    def begin(self,**kwargs):
        return topups.begin(self.db,kwargs.pop('key','fund-once'),kwargs.pop('preflight',self.preflight()),
            kwargs.pop('policy',self.initial),amount=kwargs.pop('amount',30*TIG),fee_limit=kwargs.pop('fee_limit',100),
            fee_model='op-jovian',actor='operator',**kwargs)

    def operator_balance(self,asset='TIG',location='custody'):
        account=benchmarks.OPERATOR_FEES if location=='protocol' else f'operator:custody:{asset}'
        return int(self.row('SELECT balance FROM accounts WHERE id=%s',(account,))['balance'])

    def test_chain_cash_and_protocol_credit_are_separate_and_replay_once(self):
        attempt=self.begin()
        self.assertEqual(self.operator_balance(),50*TIG)
        self.assertEqual(self.operator_balance('NATIVE'),900)
        self.assertEqual(self.balance()['available'],100*TIG)
        instructions=topups.instructions(self.db,attempt['id'])
        self.assertEqual(instructions['transaction']['data'],'0xa9059cbb'+'0'*24+TOPUP[2:]+f'{30*TIG:064x}')
        self.assertEqual(self.begin()['id'],attempt['id'])
        with self.assertRaises(Conflict):self.begin(amount=31*TIG)
        _,chain,tx_hash=payment_fixture(amount=30*TIG,recipient=TOPUP)
        tx,event=chain.transaction(tx_hash,fee_model='op-jovian'),chain.transfer(tx_hash,2)
        results=self.concurrent([lambda:topups.reconcile(self.db,attempt['id'],tx,event) for _ in range(3)])
        self.assertTrue(all(isinstance(row,dict) and row['state']=='awaiting_protocol' for row in results),results)
        self.assertEqual(self.operator_balance('NATIVE'),940)
        self.assertEqual(self.operator_balance(location='protocol'),0)
        observed=funding.record(self.db,funding_capture(30*TIG,protocol_topup(tx_hash)))['capture_id']
        self.assertFalse(funding.status(self.db)['ready'])
        results=self.concurrent([lambda:topups.credit(self.db,attempt['id'],observed) for _ in range(3)])
        self.assertTrue(all(isinstance(row,dict) for row in results),results)
        self.assertTrue(funding.status(self.db)['ready'])
        self.assertEqual(self.operator_balance(location='protocol'),30*TIG)
        self.assertEqual(self.row('SELECT count(*) AS n FROM custody_payments')['n'],1)
        self.assertEqual(self.row('SELECT count(*) AS n FROM protocol_topup_credits')['n'],1)
        self.assertEqual(self.balance()['available'],100*TIG)

    def test_unconfirmed_wrong_event_and_duplicate_protocol_facts_cannot_create_credit(self):
        attempt=self.begin()
        _,chain,tx_hash=payment_fixture(amount=30*TIG,recipient=TOPUP)
        tx,event=chain.transaction(tx_hash,fee_model='op-jovian'),chain.transfer(tx_hash,2)
        confirmed=funding_capture(30*TIG,protocol_topup(tx_hash))
        proof=funding.record(self.db,confirmed)['capture_id']
        with self.assertRaises(Conflict):topups.credit(self.db,attempt['id'],proof)
        for invalid in (replace(event,amount=event.amount-1),replace(event,recipient=OTHER),
                        replace(event,network=replace(NETWORK,confirmations=1)),replace(event,tx_hash='0x'+'1'*64)):
            with self.assertRaises(Conflict):topups.reconcile(self.db,attempt['id'],tx,invalid)
        self.assertEqual(self.row('SELECT count(*) AS n FROM chain_transactions WHERE tx_hash=%s',(tx_hash,))['n'],1)
        topups.reconcile(self.db,attempt['id'],tx,event)
        for wrong in (protocol_topup(tx_hash,confirmed=False),protocol_topup(tx_hash,index=3),protocol_topup(tx_hash,amount=29*TIG)):
            captured=funding.record(self.db,funding_capture(0,wrong))['capture_id']
            with self.assertRaises(Conflict):topups.credit(self.db,attempt['id'],captured)
        repeated=deepcopy(confirmed)
        duplicate=deepcopy(repeated['player_data']['topups'][0]);duplicate['id']=uuid.uuid4().hex
        repeated['player_data']['topups'].append(duplicate)
        captured=funding.record(self.db,repeated)['capture_id']
        with self.assertRaises(Conflict):topups.credit(self.db,attempt['id'],captured)
        self.assertEqual(self.operator_balance(location='protocol'),0)
        # Conflicting positive protocol facts remain held even if an older proof is replayed.
        with self.assertRaises(Conflict):topups.credit(self.db,attempt['id'],proof)

    def test_changed_confirmed_topup_is_latched_even_after_a_later_balanced_capture(self):
        receipt=protocol_topup('0x'+'a'*64)
        self.assertTrue(funding.record(self.db,funding_capture(topup=receipt))['complete'])
        self.assertTrue(funding.status(self.db)['ready'])
        changed=deepcopy(receipt);changed['details']['amount']=str(31*TIG)
        self.assertFalse(funding.record(self.db,funding_capture(topup=changed))['complete'])
        self.assertEqual(controls.blocked(self.db),'protocol-fee-reconciliation')
        funding.record(self.db,funding_capture(topup=receipt))
        self.assertFalse(funding.status(self.db)['ready'])
        self.assertEqual(len(funding.status(self.db)['conflicts']),1)

    def test_unmatched_success_keeps_the_topup_reserved_for_reconciliation(self):
        attempt=self.begin()
        _,wrong,wrong_hash=payment_fixture(amount=30*TIG,recipient=OTHER)
        with self.assertRaises(Conflict):topups.reconcile(self.db,attempt['id'],wrong.transaction(wrong_hash,fee_model='op-jovian'))
        self.assertEqual(self.operator_balance(),50*TIG)
        # Another finalized transaction at this same nonce would contradict saved evidence.
        self.assertEqual(self.row('SELECT state FROM protocol_topups WHERE id=%s',(attempt['id'],))['state'],'uncertain')

    def test_failed_transaction_refunds_operator_tig_and_charges_actual_native_fee(self):
        attempt=self.begin()
        _,chain,tx_hash=payment_fixture(status=0)
        outcome=topups.reconcile(self.db,attempt['id'],chain.transaction(tx_hash,fee_model='op-jovian'))
        self.assertEqual(outcome['state'],'failed')
        self.assertEqual(self.operator_balance(),80*TIG)
        self.assertEqual(self.operator_balance('NATIVE'),940)
        self.assertEqual(self.balance()['available'],100*TIG)
        second=self.begin(key='second',preflight=self.preflight(nonce=2))
        _,chain,tx_hash=payment_fixture(nonce=2,to=CUSTODY,cancellation=True)
        self.assertEqual(topups.reconcile(self.db,second['id'],chain.transaction(tx_hash,fee_model='op-jovian'))['state'],'cancelled')
        self.assertEqual(self.operator_balance(),80*TIG)
        self.assertEqual(self.operator_balance('NATIVE'),880)

    def test_fee_overrun_retains_raw_evidence_until_operator_funds_it(self):
        attempt=self.begin()
        _,chain,tx_hash=payment_fixture(amount=30*TIG,recipient=TOPUP,fee=1100)
        tx,event=chain.transaction(tx_hash,fee_model='op-jovian'),chain.transfer(tx_hash,2)
        with self.assertRaises(InsufficientFunds):topups.reconcile(self.db,attempt['id'],tx,event)
        self.assertEqual(self.row('SELECT count(*) AS n FROM chain_transactions WHERE tx_hash=%s',(tx_hash,))['n'],1)
        self.assertEqual(self.row('SELECT count(*) AS n FROM custody_payments')['n'],0)
        self.native(200,nonce=2)
        topups.reconcile(self.db,attempt['id'],tx,event)
        self.assertEqual(self.operator_balance('NATIVE'),100)
        self.assertEqual(self.balance()['available'],100*TIG)

    def test_topup_and_withdrawal_cannot_reserve_the_same_wallet_nonce(self):
        withdrawal=withdrawals.request(self.db,self.member,'withdraw',40*TIG)
        withdrawals.approve(self.db,withdrawal['id'],NETWORK,fee_model='op-jovian',actor='operator',evidence={'fixture':True})
        preflight=self.preflight()
        results=self.concurrent([lambda:self.begin(preflight=preflight),
            lambda:withdrawals.begin(self.db,withdrawal['id'],'withdraw-once',preflight,fee_limit=100,actor='operator')])
        self.assertEqual(sum(isinstance(row,dict) for row in results),1,results)
        self.assertEqual(sum(isinstance(row,Conflict) for row in results),1,results)
        self.assertEqual(self.row('SELECT count(*) AS n FROM custody_sends')['n'],1)

    def test_topup_cannot_borrow_member_funds_or_use_stale_policy_or_weaker_backing(self):
        with self.assertRaises(InsufficientFunds):self.begin(amount=100*TIG)
        with self.assertRaises(FundsError):self.begin(amount=4*TIG)
        now=self.preflight()
        for invalid in (replace(now,token_balance=now.token_balance-1),replace(now,native_balance=now.native_balance+1),
                        replace(now,checked_at=now.checked_at-timedelta(seconds=21)),
                        replace(now,network=replace(NETWORK,require_finalized=False))):
            with self.assertRaises(Conflict):self.begin(preflight=invalid)
        newer=funding.record(self.db,funding_capture())['capture_id']
        with self.assertRaises(Conflict):self.begin()
        self.begin(policy=newer)
        self.assertEqual(self.row('SELECT count(*) AS n FROM protocol_topups')['n'],1)

    def test_fee_balance_mismatch_pauses_new_work_and_old_replay_cannot_clear_it(self):
        initial=funding_capture(age=2)
        funding.record(self.db,initial)
        mismatched=funding_capture(10*TIG)
        self.assertTrue(funding.record(self.db,mismatched)['complete'])
        self.assertEqual(controls.blocked(self.db),'protocol-fee-reconciliation')
        with self.assertRaises(Conflict):self.reserve()
        funding.record(self.db,initial)
        self.assertFalse(funding.status(self.db)['ready'])
        self.fees(10*TIG)
        self.assertTrue(funding.status(self.db)['ready'])
        self.reserve(fee=TIG)
        # Committed submission funds remain part of the observed prepaid balance.
        self.assertTrue(funding.status(self.db)['ready'])
        partial=funding_capture(10*TIG);partial['error']='ConnectionError'
        self.assertFalse(funding.record(self.db,partial)['complete'])
        self.assertFalse(funding.status(self.db)['ready'])
        funding.record(self.db,funding_capture(10*TIG))
        self.assertTrue(funding.status(self.db)['ready'])

    def test_protocol_identity_is_bound_to_custody_and_archived_capture_is_immutable(self):
        with self.assertRaises(Conflict):funding.record(self.db,funding_capture(player=OTHER))
        with self.assertRaises(Conflict),self.db.transaction() as cursor:custody.bind(cursor,replace(NETWORK,custody=OTHER))
        import psycopg2
        with self.assertRaises(psycopg2.Error),self.db.transaction() as cursor:
            cursor.execute('UPDATE funding_captures SET available=1')
        self.assertEqual(funding.read(self.db,self.initial)['available'],0)

    def test_operator_api_recovers_manual_intent_without_rpc_and_confirms_credit(self):
        secret='test-funding-operator'
        settings=Settings(self.db.dsn,'https://pool.example',8453,hashlib.sha256(secret.encode()).hexdigest(),
            funds_enabled=True,pool_player_id=CUSTODY,custody_network=NETWORK,custody_rpc_url='https://rpc.example',
            withdrawal_fee_model='op-jovian')
        app=create_app(settings);client=TestClient(app);headers={'Authorization':'Bearer '+secret}
        _,chain,tx_hash=payment_fixture(amount=30*TIG,recipient=TOPUP)
        test=self
        class SimulatedChain:
            network=NETWORK
            def preflight(self):return test.preflight()
            def find_nonce(self,nonce,*,after_height):
                test.assertEqual((nonce,after_height),(1,90));return tx_hash
            def transaction(self,*args,**kwargs):return chain.transaction(*args,**kwargs)
            def transfer(self,*args,**kwargs):return chain.transfer(*args,**kwargs)
        app.state.payment_chain=SimulatedChain()
        body={'request_key':'api-topup','amount':str(30*TIG),'fee_limit':'100'}
        self.assertEqual(client.post('/api/v2/operator/topups',json=body).status_code,401)
        self.assertEqual(client.post('/api/v2/operator/topups',json=body,headers={'Authorization':'Bearer not-operator'}).status_code,403)
        begun=client.post('/api/v2/operator/topups',json=body,headers=headers)
        self.assertEqual(begun.status_code,200,begun.text)
        identity=begun.json()['id'];path='/api/v2/operator/topups/'+identity
        with patch.object(app.state.payment_chain,'preflight',side_effect=ConnectionError):
            self.assertEqual(client.post('/api/v2/operator/topups',json=body,headers=headers).json()['id'],identity)
        paid=client.post(path+'/reconcile',json={},headers=headers)
        self.assertEqual(paid.status_code,200,paid.text)
        self.assertEqual(paid.json()['state'],'awaiting_protocol')
        self.assertEqual(client.post(path+'/confirm',json={},headers=headers).status_code,409)
        funding.record(self.db,funding_capture(30*TIG,protocol_topup(tx_hash)))
        credited=client.post(path+'/confirm',json={},headers=headers)
        self.assertEqual(credited.status_code,200,credited.text)
        shown=client.get('/api/v2/operator/funding',headers=headers)
        self.assertTrue(shown.json()['observation']['ready'])
        self.assertEqual(shown.json()['topups'][0]['state'],'credited')

    def test_funding_collector_replays_pending_evidence_and_confirms_only_paid_topup(self):
        attempt=self.begin()
        _,chain,tx_hash=payment_fixture(amount=30*TIG,recipient=TOPUP)
        topups.reconcile(self.db,attempt['id'],chain.transaction(tx_hash,fee_model='op-jovian'),chain.transfer(tx_hash,2))
        data=funding_capture(30*TIG,protocol_topup(tx_hash))
        spec=importlib.util.spec_from_file_location('funding_collector_fixture',Path(__file__).resolve().parents[2]/'tools/observe_funding_v2.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='innopool-v2-funding-cli-') as directory:
            spool=Spool(directory);spool.save(data,{'fixture':True})
            original=funding.record
            attempts=[]
            def outage(*args):
                attempts.append(True)
                if len(attempts)==1:raise ConnectionError('fixture database outage')
                return original(*args)
            with patch.dict(os.environ,{'POOL_V2_DATABASE_DSN':self.db.dsn}),patch.object(module.signal,'signal'),\
                    patch.object(module.funding,'capture',return_value=data),patch.object(module,'PublicTigClient'),\
                    patch.object(module.funding,'record',side_effect=outage),patch.object(module.logging,'basicConfig'),\
                    self.assertLogs('innopool-v2-funding',level='WARNING'):
                self.assertEqual(module.main(['--player-id',CUSTODY,'--spool',directory,'--once']),0)
            self.assertEqual(spool.pending(),[])
            self.assertGreaterEqual(len(attempts),3)
            spool.save(data,{'fixture':'offline restore'})
            with patch.dict(os.environ,{'POOL_V2_DATABASE_DSN':self.db.dsn}),patch.object(module.signal,'signal'),\
                    patch.object(module,'PublicTigClient') as client,patch.object(module.logging,'basicConfig'):
                self.assertEqual(module.main(['--player-id',CUSTODY,'--spool',directory,'--replay-only']),0)
                client.assert_not_called()
        self.assertEqual(self.operator_balance(location='protocol'),30*TIG)
        self.assertEqual(self.row('SELECT count(*) AS n FROM protocol_topup_credits')['n'],1)


class FundingEvidenceTests(unittest.TestCase):
    def test_unknown_player_is_zero_and_block_mismatch_or_numeric_amount_is_rejected(self):
        data=funding_capture();data['player_data']['player']=None
        self.assertEqual(funding.verify(data)['available'],0)
        mismatch=deepcopy(data);mismatch['end']['block']['id']='changed'
        with self.assertRaises(Conflict):funding.verify(mismatch)
        numeric=funding_capture();numeric['player_data']['player']['state']['available_fee_balance']=1.0
        with self.assertRaises(FundsError):funding.verify(numeric)
        missing=deepcopy(data);del missing['player_data']['topups']
        with self.assertRaises((FundsError,KeyError)):funding.verify(missing)

    def test_public_tig_topup_and_base_receipt_agree_on_the_exact_transfer(self):
        fixture=json.loads(gzip.decompress((Path(__file__).with_name('fixtures')/'protocol-topup.json.gz').read_bytes()))
        verified=funding.verify(fixture['funding'])
        transaction=fixture['chain']['transaction']
        token='0x0c03ce270b4826ec62e7dd007f0b716068639f7b'
        network=Network(8453,token,verified['player_id'],12)
        def rpc(method,params):
            if method=='eth_chainId':return '0x2105'
            if method=='eth_call':return '0x12'
            if method=='eth_getTransactionReceipt':return transaction['receipt']
            if method=='eth_getTransactionByHash':return transaction['transaction']
            if method=='eth_getBlockByNumber':return transaction[params[0] if params[0] in ('latest','finalized') else 'header']
            raise AssertionError((method,params))
        chain=Chain(network,rpc)
        identity=fixture['chain']['topup']['id'];topup=verified['topups'][identity]
        received=chain.transfer(topup['tx_hash'],topup['log_index'])
        tx=chain.transaction(topup['tx_hash'],fee_model='op-jovian')
        self.assertEqual((received.sender,received.recipient,received.amount),(verified['player_id'],verified['recipient'],30*TIG))
        self.assertEqual(topup['amount'],received.amount)
        self.assertEqual(tx.fee,279580507888)


class FundingInitializationTests(DatabaseCase):
    def test_initial_stale_capture_cannot_enable_work_and_first_bound_player_controls_custody(self):
        data=funding_capture(age=180);data['player_data']['player']=None
        self.assertTrue(funding.record(self.db,data)['complete'])
        self.assertFalse(funding.status(self.db)['ready'])
        with self.assertRaises(Conflict),self.db.transaction() as cursor:custody.bind(cursor,replace(NETWORK,custody=OTHER))
        funding.record(self.db,funding_capture())
        self.assertTrue(funding.status(self.db)['ready'])
        with self.db.transaction() as cursor:custody.bind(cursor,NETWORK)

    def test_custody_observer_recognizes_the_topup_cash_and_fee_without_spending_member_funds(self):
        from pool_manager.pool_v2 import chain_observer
        from test_chain_observer import CustodyRpc
        source=CustodyRpc()
        # Consistent generated block timestamps keep this accelerated cycle within the intent window.
        source.header=lambda height:{'number':hex(height),'hash':source.hash(height),
            'parentHash':source.hash(height-1),'timestamp':hex(source.now)}
        capture=lambda first:chain_observer.capture(NETWORK,source.rpc,first,count=1000,source='fixture')
        chain_observer.record(self.db,capture(100),initialize=True)
        source.add(height=103)
        operator=source.add(sender='0x'+'7'*40,amount=80*TIG,height=103)
        source.native[103]=1000;source.final=104
        self.assertFalse(chain_observer.record(self.db,capture(103))['healthy'])
        deposits.attribute_reviewed(self.db,Chain(NETWORK,source.rpc).transfer(operator['transactionHash'],0),
            actor='operator',evidence={'fixture':True},operator=True)
        def receipt(height,*,log=None,**kwargs):
            data,chain,tx_hash=payment_fixture(**kwargs)
            tx_hash=log['transactionHash'] if log else tx_hash
            block=source.header(height)
            data['header']=block;data['latest']=source.header(source.latest);data['finalized']=source.header(source.final)
            data['receipt'].update(transactionHash=tx_hash,blockNumber=hex(height),blockHash=block['hash'],logs=[log] if log else [])
            data['transaction'].update(hash=tx_hash,blockNumber=hex(height),blockHash=block['hash'])
            return chain,tx_hash
        native,native_hash=receipt(103,sender='0x'+'8'*40,to=CUSTODY,value=1000)
        custody.receive_native(self.db,native.transaction(native_hash,fee_model='op-jovian'))
        self.assertTrue(chain_observer.record(self.db,capture(105))['healthy'])
        policy=funding.record(self.db,funding_capture())['capture_id']
        preflight=CustodyPreflight(NETWORK,0,180*TIG,1000,104,datetime.now(timezone.utc),{'fixture':True})
        attempt=topups.begin(self.db,'observed-send',preflight,policy,amount=30*TIG,fee_limit=100,
            fee_model='op-jovian',actor='operator')
        outgoing=source.add(sender=CUSTODY,recipient=TOPUP,amount=30*TIG,height=105)
        source.native[105]=940;source.nonces[105]=1;source.final=106
        self.assertFalse(chain_observer.record(self.db,capture(105))['healthy'])
        chain,tx_hash=receipt(105,log=outgoing,nonce=0,amount=30*TIG,recipient=TOPUP)
        topups.reconcile(self.db,attempt['id'],chain.transaction(tx_hash,fee_model='op-jovian'),chain.transfer(tx_hash,0))
        self.assertTrue(chain_observer.record(self.db,capture(107))['healthy'])
        self.assertTrue(chain_observer.status(self.db)['ready'])
        self.assertEqual(self.balance()['available'],100*TIG)
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='operator:protocol:TIG'")['balance'],0)
        observed=funding.record(self.db,funding_capture(30*TIG,protocol_topup(tx_hash,index=0)))['capture_id']
        topups.credit(self.db,attempt['id'],observed)
        self.assertIsNone(controls.blocked(self.db))
