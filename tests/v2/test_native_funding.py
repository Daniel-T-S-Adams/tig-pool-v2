from copy import deepcopy
from dataclasses import replace
import gzip
import hashlib
import json
from pathlib import Path
import unittest

from fastapi.testclient import TestClient
from pool_manager.pool_v2 import custody, native_funding
from pool_manager.pool_v2.api import Settings, create_app
from pool_manager.pool_v2.chain import Chain, Network
from pool_manager.pool_v2.database import Database
from pool_manager.pool_v2.money import Conflict, FundsError, TIG
from funds_helpers import CUSTODY, NETWORK, OTHER, WALLET, DatabaseCase
from test_withdrawals import payment_fixture


def fixture():
    data, basic, tx_hash = payment_fixture(sender=WALLET, to=OTHER)
    data['receipt']['transactionIndex'] = '0x1'
    data['code'] = '0x'
    data['trace_chain'] = hex(NETWORK.chain_id)
    data['trace_header'] = deepcopy(data['header'])
    def frame(location, sender, recipient, value, children):
        return {'type': 'call', 'traceAddress': location, 'subtraces': children,
            'transactionHash': tx_hash, 'transactionPosition': 1,
            'blockNumber': 100, 'blockHash': data['header']['hash'],
            'action': {'callType': 'call', 'from': sender, 'to': recipient,
                       'value': hex(value), 'input': data['transaction']['input'] if not location else '0x'},
            'result': {'gasUsed': '0x1', 'output': '0x'}}
    data['trace'] = [frame([], WALLET, OTHER, 0, 1), frame([0], OTHER, WALLET, 0, 1),
                     frame([0, 0], WALLET, CUSTODY, 500, 0)]
    def rpc(method, params):
        if method == 'eth_getCode': return data['code']
        return basic.rpc(method, params)
    def traced(method, params):
        if method == 'eth_chainId': return data['trace_chain']
        if method == 'eth_getBlockByNumber': return deepcopy(data['trace_header'])
        if method == 'trace_transaction': return deepcopy(data['trace'])
        raise AssertionError((method, params))
    return data, Chain(NETWORK, rpc), traced, tx_hash


def verified(chain, traced, tx_hash, location=(0, 0)):
    return native_funding.verify(chain, traced, tx_hash, location, fee_model='op-jovian')


class NativeTraceTests(unittest.TestCase):
    def test_nested_native_call_is_distinct_from_outer_zero_value_transaction(self):
        _, chain, traced, tx_hash = fixture()
        result = verified(chain, traced, tx_hash)
        self.assertEqual((result.sender, result.recipient, result.amount), (WALLET, CUSTODY, 500))
        self.assertEqual(result.transaction.value, 0)
        self.assertEqual(result.transaction.recipient, OTHER)
        self.assertEqual(result.trace_address, (0, 0))

    def test_finality_and_trace_provider_are_required(self):
        data, chain, traced, tx_hash = fixture()
        with self.assertRaises(FundsError): verified(chain, None, tx_hash)
        with self.assertRaises(FundsError): verified(Chain(replace(NETWORK, require_finalized=False), chain.rpc), traced, tx_hash)
        data['finalized']['number'] = '0x63'
        with self.assertRaisesRegex(FundsError, 'finalized'): verified(chain, traced, tx_hash)

    def test_reverted_ancestor_child_and_outer_failure_cannot_credit(self):
        for index in (0, 1, 2):
            with self.subTest(frame=index):
                data, chain, traced, tx_hash = fixture()
                data['trace'][index]['error'] = 'Reverted'
                with self.assertRaises(FundsError): verified(chain, traced, tx_hash)
        data, chain, traced, tx_hash = fixture()
        data['receipt']['status'] = '0x0'
        with self.assertRaises(FundsError): verified(chain, traced, tx_hash)

    def test_delegatecall_callcode_staticcall_and_wrong_recipient_are_not_funding(self):
        for kind in ('delegatecall', 'callcode', 'staticcall'):
            data, chain, traced, tx_hash = fixture()
            data['trace'][-1]['action']['callType'] = kind
            with self.subTest(kind=kind), self.assertRaises(FundsError): verified(chain, traced, tx_hash)
        for field, value in (('to', OTHER), ('from', CUSTODY), ('value', '0x0')):
            data, chain, traced, tx_hash = fixture()
            data['trace'][-1]['action'][field] = value
            with self.subTest(field=field), self.assertRaises(FundsError): verified(chain, traced, tx_hash)

    def test_canonical_network_root_and_frame_mismatches_fail(self):
        changes = [lambda d: d.update(trace_chain='0x1'),
            lambda d: d['trace_header'].update(hash='0x'+'b'*64),
            lambda d: d['trace'][-1].update(blockHash='0x'+'b'*64),
            lambda d: d['trace'][-1].update(transactionHash='0x'+'b'*64),
            lambda d: d['trace'][-1].update(transactionPosition=2),
            lambda d: d['trace'][0]['action'].update(input='0x00'),
            lambda d: d['trace'][0]['action'].update(value='0x1'),
            lambda d: d.update(code='0xef0100'+'1'*40)]
        for change in changes:
            data, chain, traced, tx_hash = fixture(); change(data)
            with self.subTest(change=change), self.assertRaises(FundsError): verified(chain, traced, tx_hash)

    def test_missing_duplicate_or_invalid_paths_fail(self):
        for location in ([], [True], [-1], [2**31], [0]*65, [9]):
            _, chain, traced, tx_hash = fixture()
            with self.subTest(location=location), self.assertRaises(FundsError): verified(chain, traced, tx_hash, location)
        for mutate in (lambda d: d['trace'].pop(1), lambda d: d['trace'].append(deepcopy(d['trace'][-1])),
                       lambda d: d['trace'][0].update(subtraces=10000)):
            data, chain, traced, tx_hash = fixture(); mutate(data)
            with self.assertRaises(FundsError): verified(chain, traced, tx_hash)

    def test_recorded_testnet_receipt_replays_all_rpc_evidence(self):
        data = json.loads(gzip.decompress((Path(__file__).parent/'fixtures/base-sepolia-internal-native.json.gz').read_bytes()))
        calls = iter(data['calls'])
        def rpc(source):
            def request(method, params):
                entry = next(calls)
                self.assertEqual((source, method, params), (entry['source'], entry['method'], entry['params']))
                return entry['result']
            return request
        result = native_funding.verify(Chain(Network(**data['network']), rpc('custody')), rpc('trace'),
            data['tx_hash'], data['trace_address'], fee_model=data['fee_model'])
        self.assertEqual(result.amount, 10**17)
        self.assertEqual(result.trace_address, (5, 0))
        self.assertIsNone(next(calls, None))


class NativeFundingTests(DatabaseCase):
    def test_concurrent_replay_and_restart_credit_operator_once_without_charging_sender_gas(self):
        self.fund(amount=TIG)
        _, chain, traced, tx_hash = fixture(); receipt = verified(chain, traced, tx_hash)
        results = self.concurrent([lambda: native_funding.receive(self.db, receipt, actor='operator')] * 3)
        self.assertEqual(results, [None]*3)
        native_funding.receive(Database(self.db.dsn), receipt, actor='another-operator')
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='operator:custody:NATIVE'")['balance'], 500)
        self.assertEqual(self.balance()['available'], TIG)
        self.assertEqual(self.row('SELECT count(*) AS n FROM native_internal_receipts')['n'], 1)
        self.assertEqual(self.row("SELECT count(*) AS n FROM journals WHERE kind='operator_native_funding'")['n'], 1)
        with self.assertRaises(FundsError): custody.receive_native(self.db, receipt.transaction)

    def test_conflicting_amount_and_network_cannot_recredit(self):
        _, chain, traced, tx_hash = fixture(); receipt = verified(chain, traced, tx_hash)
        native_funding.receive(self.db, receipt, actor='operator')
        for changed in (replace(receipt, amount=501),
                replace(receipt, transaction=replace(receipt.transaction, network=replace(NETWORK, chain_id=1)))):
            with self.assertRaises(Conflict): native_funding.receive(self.db, changed, actor='operator')
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='operator:custody:NATIVE'")['balance'], 500)

    def test_operator_endpoint_requires_authority_exact_path_and_retains_direct_receipts(self):
        operator='native-funding-test-operator'
        app=create_app(Settings(self.db.dsn,'https://pool.example',8453,hashlib.sha256(operator.encode()).hexdigest(),
            custody_network=NETWORK,custody_rpc_url='https://unused.example',withdrawal_fee_model='op-jovian'))
        _,chain,traced,tx_hash=fixture()
        app.state.payment_chain=chain;app.state.custody_trace_rpc=traced
        client=TestClient(app);route='/api/v2/operator/custody/receive-native'
        body={'tx_hash':tx_hash,'trace_address':[0,0]};headers={'Authorization':'Bearer '+operator}
        self.assertEqual(client.post(route,json=body).status_code,401)
        self.assertEqual(client.post(route,json=body,headers={'Authorization':'Bearer '+'member-token'}).status_code,403)
        for location in ([],[True],['0']):
            self.assertEqual(client.post(route,json={**body,'trace_address':location},headers=headers).status_code,422)
        self.assertEqual(client.post(route,json={**body,'trace_address':[-1]},headers=headers).status_code,400)
        result=client.post(route,json=body,headers=headers)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(client.post(route,json=body,headers=headers).status_code,200)
        _,direct,direct_hash=payment_fixture(sender=WALLET,to=CUSTODY,value=100,nonce=2)
        app.state.payment_chain=direct
        self.assertEqual(client.post(route,json={'tx_hash':direct_hash},headers=headers).status_code,200)
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='operator:custody:NATIVE'")['balance'],600)
