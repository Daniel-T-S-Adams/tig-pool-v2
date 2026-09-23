from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from pool_manager.pool_v2 import custody, deposits, ledger, members, sponsored_withdrawals as sponsored, withdrawals
from pool_manager.pool_v2.api import Settings, create_app
from pool_manager.pool_v2.chain import CustodyPreflight, UnsupportedCustodyTransaction
from pool_manager.pool_v2.money import Conflict, FundsError, TIG
from funds_helpers import DatabaseCase, OTHER, CUSTODY, transfer
from test_withdrawals import payment_fixture


def recorded(*, current_time=False):
    fixture = Path(__file__).with_name('fixtures') / 'sponsored-withdrawal-47205354.json.gz'
    data = json.loads(gzip.decompress(fixture.read_bytes()))
    if current_time:
        # Ledger simulation only. The separate recorded-evidence test below
        # replays the unmodified, finalized live receipt and authorization.
        for call in data['calls']:
            if call['method'] == 'eth_getBlockByNumber' and call['params'][0] == hex(47205354):
                call['result']['timestamp'] = hex(int(datetime.now(timezone.utc).timestamp()) + 1)
    return data


def calls(data, method, *, source=None):
    return [c for c in data['calls'] if c['method'] == method and (source is None or c['source'] == source)]


class SponsoredEvidenceTests(unittest.TestCase):
    def test_recorded_finalized_payment_proves_pool_nonce_and_external_gas(self):
        recovery = sponsored.verify_capture(recorded())
        self.assertEqual(recovery.custody_nonce, 0)
        self.assertEqual(recovery.transaction.nonce, 115629)
        self.assertNotEqual(recovery.transaction.sender, recovery.transfer.sender)
        self.assertEqual(recovery.transfer.amount, TIG // 20)
        self.assertEqual(recovery.evidence['custody_fee'], 0)
        self.assertEqual(recovery.transaction.fee, 988216480463)
        self.assertEqual(recovery.delegate, '0x63c0c19a282a1b52b07dd5a65b58948a07dae32b')
        self.assertEqual(recovery.evidence['before']['code'], '0x')

    def test_wrong_authorization_signature_scope_nonce_or_ambiguity_is_rejected(self):
        for field, value in (('nonce', '0x1'), ('chainId', '0x0'), ('r', '0x0'),
                             ('s', '0x'+'f'*64), ('yParity', '0x2'),
                             ('address', '0x'+'7'*40)):
            with self.subTest(field=field):
                data = recorded()
                calls(data, 'eth_getTransactionByHash')[0]['result']['authorizationList'][0][field] = value
                with self.assertRaises(FundsError): sponsored.verify_capture(data)
        data = recorded()
        authorizations = calls(data, 'eth_getTransactionByHash')[0]['result']['authorizationList']
        authorizations.append(deepcopy(authorizations[0]))
        with self.assertRaises(FundsError): sponsored.verify_capture(data)

    def test_unfinalized_changed_chain_and_noncanonical_receipts_are_rejected(self):
        for kind in ('finality', 'chain', 'canonical', 'removed', 'failed'):
            with self.subTest(kind=kind):
                data = recorded()
                if kind == 'finality':
                    for call in calls(data, 'eth_getBlockByNumber'):
                        if call['params'][0] == 'finalized': call['result']['number'] = hex(47205353)
                elif kind == 'chain': calls(data, 'eth_chainId')[0]['result'] = '0x1'
                else:
                    receipt = calls(data, 'eth_getTransactionReceipt')[0]['result']
                    if kind == 'canonical': receipt['blockHash'] = '0x'+'f'*64
                    if kind == 'failed': receipt['status'] = '0x0'
                    if kind == 'removed':
                        # The second receipt is the independent token-event read.
                        for c in calls(data, 'eth_getTransactionReceipt'):
                            c['result']['logs'][1]['removed'] = True
                with self.assertRaises(FundsError): sponsored.verify_capture(data)

    def test_pool_nonce_code_or_balance_changes_cannot_be_assumed(self):
        for method, anchor, result in (
            ('eth_getTransactionCount', 47205353, '0x1'),
            ('eth_getTransactionCount', 47205354, '0x0'),
            ('eth_getCode', 47205353, '0xef0100'+'7'*40),
            ('eth_getCode', 47205354, '0xef0100'+'7'*40),
            ('eth_getBalance', 47205354, '0x0'),
            ('eth_call', 47205354, '0x'+'0'*64),
        ):
            with self.subTest(method=method, anchor=anchor):
                data = recorded()
                matches = [c for c in calls(data, method) if c['params'][-1] == hex(anchor)]
                self.assertEqual(len(matches), 1)
                matches[0]['result'] = result
                with self.assertRaises(FundsError): sponsored.verify_capture(data)

    def test_other_custody_transactions_or_authorizations_in_same_block_block_recovery(self):
        for kind in ('direct', 'authorization', 'identity'):
            with self.subTest(kind=kind):
                data = recorded()
                block = next(c['result'] for c in calls(data, 'eth_getBlockByNumber') if c['params'] == [hex(47205354), True])
                tx = calls(data, 'eth_getTransactionByHash')[0]['result']
                if kind == 'authorization': block['transactions'].append(deepcopy(tx))
                elif kind == 'direct': block['transactions'].append({'from': data['network']['custody']})
                else: block['parentHash'] = '0x'+'f'*64
                with self.assertRaises(FundsError): sponsored.verify_capture(data)

    def test_trace_must_prove_zero_pool_cost_and_only_the_requested_custody_call(self):
        for kind in ('missing', 'extra', 'reverted', 'fee', 'create', 'identity', 'calldata', 'root', 'provider'):
            with self.subTest(kind=kind):
                data = recorded()
                trace = calls(data, 'trace_transaction')[0]['result']
                if kind == 'missing': trace.pop(1)
                elif kind == 'extra': trace.append(deepcopy(trace[-1]))
                elif kind == 'reverted': trace[6]['error'] = 'Reverted'
                elif kind == 'fee': trace[7]['action']['value'] = '0x1'
                elif kind == 'create': trace[7]['type'] = 'create'
                elif kind == 'identity': trace[7]['transactionHash'] = '0x'+'f'*64
                elif kind == 'calldata': trace[7]['action']['input'] = '0x'
                elif kind == 'root': trace[0]['action']['from'] = OTHER
                else: calls(data, 'eth_chainId', source='trace')[0]['result'] = '0x1'
                with self.assertRaises(FundsError): sponsored.verify_capture(data)

    def test_recovery_archive_must_be_complete_and_ordered(self):
        for kind in ('missing', 'extra', 'failed', 'version'):
            data = recorded()
            if kind == 'missing': data['calls'].pop()
            elif kind == 'extra': data['calls'].append(deepcopy(data['calls'][-1]))
            elif kind == 'failed': data['error'] = 'OSError'
            else: data['version'] = 2
            with self.subTest(kind=kind), self.assertRaises(FundsError): sponsored.verify_capture(data)


class SponsoredLedgerTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.recovery = sponsored.verify_capture(recorded(current_time=True))
        self.network = self.recovery.transaction.network
        self.wallet = self.recovery.transfer.recipient
        with self.db.transaction() as cursor:
            self.member = members.register_verified(cursor, self.wallet)['id']
        deposit = replace(transfer(), network=self.network, sender=self.wallet, recipient=self.network.custody)
        deposits.receive(self.db, deposit)
        _, source, identity = payment_fixture(sender=OTHER, to=CUSTODY, value=1000)
        native = replace(source.transaction(identity, fee_model='op-jovian'), network=self.network, recipient=self.network.custody)
        custody.receive_native(self.db, native)
        self.hold = self.reserve()
        self.before_request = self.balance()
        self.withdrawal = withdrawals.request(self.db, self.member, 'sponsored', self.recovery.transfer.amount)
        withdrawals.approve(self.db, self.withdrawal['id'], network=self.network, fee_model='op-jovian',
                            actor='operator', evidence={'review': 'fixture'})
        with self.db.transaction() as cursor:
            preflight = CustodyPreflight(self.network, 0, ledger.backing(cursor), ledger.backing(cursor, 'NATIVE'),
                self.recovery.transaction.block_number-100, datetime.now(timezone.utc), {'code': '0x'})
        self.attempt = withdrawals.begin(self.db, self.withdrawal['id'], 'prepared', preflight, fee_limit=100, actor='operator')

    def recover(self, recovery=None):
        return withdrawals.reconcile_sponsored(self.db, self.attempt['id'], recovery or self.recovery,
            actor='operator', reason='verified already sent sponsored payment')

    def test_recovery_and_replay_pay_once_return_gas_and_preserve_collateral(self):
        result = self.recover()
        self.assertEqual((result['outcome'], int(result['fee'])), ('paid', 0))
        self.assertEqual(self.recover(), result)
        balance = self.balance()
        self.assertEqual(balance['available'], self.before_request['available']-self.recovery.transfer.amount)
        self.assertEqual(balance['pending_withdrawals'], 0)
        self.assertEqual(balance['collateral'], self.before_request['collateral'])
        self.assertEqual(balance['slots'], self.before_request['slots'])
        self.assertEqual(balance['last_paid_at'], self.recovery.transaction.block_timestamp)
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='operator:custody:NATIVE'")['balance'], 1000)
        self.assertEqual(self.row('SELECT count(*) AS n FROM custody_authorization_payments')['n'], 1)
        outer = self.row('SELECT sender,nonce,fee FROM chain_transactions WHERE tx_hash=%s', (self.recovery.transaction.tx_hash,))
        self.assertEqual((outer['sender'], outer['nonce'], outer['fee']),
            (self.recovery.transaction.sender, 115629, self.recovery.transaction.fee))
        with self.assertRaisesRegex(Conflict, 'seven days'):
            withdrawals.request(self.db, self.member, 'too-soon', TIG//100)

    def test_concurrent_recovery_posts_one_payment(self):
        results = self.concurrent([self.recover, self.recover])
        self.assertTrue(all(isinstance(r, dict) and r['outcome']=='paid' for r in results), results)
        self.assertEqual(self.row("SELECT count(*) AS n FROM journals WHERE kind='withdrawal_paid'")['n'], 1)

    def test_wrong_frozen_fields_and_predated_receipts_cannot_pay(self):
        for invalid in (
            replace(self.recovery, custody_nonce=1),
            replace(self.recovery, transfer=replace(self.recovery.transfer, recipient=OTHER)),
            replace(self.recovery, transfer=replace(self.recovery.transfer, amount=1)),
            replace(self.recovery, transaction=replace(self.recovery.transaction, fee_model='ethereum')),
            replace(self.recovery, transaction=replace(self.recovery.transaction, block_number=1)),
            replace(self.recovery, transaction=replace(self.recovery.transaction,
                network=replace(self.network, require_finalized=False))),
        ):
            with self.assertRaises(FundsError): self.recover(invalid)
        self.assertEqual(self.balance()['pending_withdrawals'], self.recovery.transfer.amount)
        self.assertIsNone(self.balance()['last_paid_at'])

    def test_direct_reconciliation_cannot_mischarge_the_relayer_fee(self):
        with self.assertRaises(Conflict):
            withdrawals.reconcile(self.db, self.attempt['id'], self.recovery.transaction, self.recovery.transfer)
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='operator:custody:NATIVE'")['balance'], 900)

    def test_recovery_route_requires_operator_and_complete_verification(self):
        secret = 'test-sponsored-operator'
        app = create_app(Settings(self.db.dsn, 'https://pool.example', self.network.chain_id,
            hashlib.sha256(secret.encode()).hexdigest(), custody_network=self.network,
            custody_rpc_url='https://rpc.example', custody_trace_rpc_url='https://trace.example',
            withdrawal_fee_model='op-jovian'))
        client = TestClient(app)
        endpoint = '/api/v2/operator/withdrawal-attempts/'+str(self.attempt['id'])+'/recover-sponsored'
        body = {'tx_hash': self.recovery.transaction.tx_hash, 'log_index': self.recovery.transfer.log_index,
                'reason': 'verified exact receipt, authorization and sponsored gas'}
        self.assertEqual(client.post(endpoint, json=body).status_code, 401)
        with patch.object(sponsored, 'verify', return_value=self.recovery) as verify:
            denied = client.post(endpoint, json=body, headers={'Authorization': 'Bearer member-token'})
            self.assertEqual(denied.status_code, 403)
            verify.assert_not_called()
            result = client.post(endpoint, json=body, headers={'Authorization': 'Bearer '+secret})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['outcome'], 'paid')
            self.assertEqual(verify.call_args.kwargs['nonce'], 0)

    def test_check_transfer_recovers_sponsored_payment_and_remembers_the_hash(self):
        secret = 'test-sponsored-operator'
        app = create_app(Settings(self.db.dsn, 'https://pool.example', self.network.chain_id,
            hashlib.sha256(secret.encode()).hexdigest(), custody_network=self.network,
            custody_rpc_url='https://rpc.example', custody_trace_rpc_url='https://trace.example',
            withdrawal_fee_model='op-jovian'))
        recovery = self.recovery
        class SimulatedChain:
            def transaction(self, *args, **kwargs): raise UnsupportedCustodyTransaction(4)
            def authorization_transaction(self, *args, **kwargs): return recovery.transaction
            def transfer(self, tx_hash, index):
                if index != recovery.transfer.log_index: raise FundsError('not a matching token event')
                return recovery.transfer
            def find_nonce(self, *args, **kwargs): raise AssertionError('use the saved operator hash')
        app.state.payment_chain = SimulatedChain()
        client = TestClient(app)
        endpoint = '/api/v2/operator/withdrawal-attempts/'+str(self.attempt['id'])+'/reconcile'
        headers = {'Authorization': 'Bearer '+secret}
        with patch.object(sponsored, 'verify', return_value=recovery) as verify:
            first = client.post(endpoint, json={'tx_hash': recovery.transaction.tx_hash}, headers=headers)
            self.assertEqual(first.status_code, 200, first.text)
            second = client.post(endpoint, json={}, headers=headers)
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(first.json(), second.json())
            self.assertEqual(verify.call_count, 2)
        self.assertEqual(withdrawals.instructions(self.db, self.attempt['id'])['claimed_tx_hashes'],
                         [recovery.transaction.tx_hash])
        self.assertEqual(self.row("SELECT count(*) AS n FROM journals WHERE kind='withdrawal_paid'")['n'], 1)
