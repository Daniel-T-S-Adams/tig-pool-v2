from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
import uuid

from pool_manager.pool_v2 import custody, deposits, ledger, withdrawals
from pool_manager.pool_v2.chain import Chain, CustodyPreflight, Network, transaction_fee
from pool_manager.pool_v2.money import Conflict, FundsError, InsufficientFunds, TIG
from funds_helpers import DatabaseCase, WALLET, OTHER, CUSTODY, TOKEN, NETWORK, chain_fixture, transfer


def payment_fixture(*, amount=40*TIG, recipient=WALLET, sender=CUSTODY, nonce=1,
                    status=1, value=0, to=TOKEN, fee=60, cancellation=False):
    data, basic, tx_hash = chain_fixture(sender, recipient, amount)
    data['receipt'].update(status=hex(status), gasUsed='0xa', effectiveGasPrice='0x5', l1Fee=hex(fee-50),
                           daFootprintGasScalar='0x94', blobGasUsed='0x39d0', **{'from': sender, 'to': to})
    if status == 0 or cancellation: data['receipt']['logs'] = []
    data['transaction'] = {'hash': tx_hash, 'chainId': '0x2105', 'type': '0x2', 'from': sender, 'to': to,
        'nonce': hex(nonce), 'value': hex(value), 'blockHash': data['receipt']['blockHash'],
        'blockNumber': data['receipt']['blockNumber'], 'input': '0x' if cancellation else '0xa9059cbb'}
    def rpc(method, params):
        if method == 'eth_getTransactionByHash': return deepcopy(data['transaction'])
        return basic.rpc(method, params)
    return data, Chain(NETWORK, rpc), tx_hash


class WithdrawalTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.fund()
        self.fund(OTHER)

    def fund_gas(self, amount=1000, nonce=1):
        _, chain, tx_hash = payment_fixture(sender='0x'+'7'*40, to=CUSTODY, value=amount, nonce=nonce)
        tx = chain.transaction(tx_hash, fee_model='op-jovian')
        custody.receive_native(self.db, tx)
        return tx

    def preflight(self, nonce=1):
        with self.db.transaction() as cursor:
            return CustodyPreflight(NETWORK, nonce, ledger.backing(cursor), ledger.backing(cursor, 'NATIVE'),
                                    90, datetime.now(timezone.utc), {'fixture': True})

    def approved(self, member=None, key='withdraw', amount=40*TIG):
        row = withdrawals.request(self.db, member or self.member, key, amount)
        withdrawals.approve(self.db, row['id'], NETWORK, fee_model='op-jovian', actor='operator', evidence={'reviewed': True})
        return row

    def begin(self, row, nonce=1, key='attempt', fee_limit=100):
        return withdrawals.begin(self.db, row['id'], key, self.preflight(nonce), fee_limit=fee_limit, actor='operator')

    def test_full_payment_and_operator_fee_are_atomic_and_replay_does_not_restart_cooldown(self):
        self.fund_gas()
        row = self.approved()
        attempt = self.begin(row)
        _, chain, tx_hash = payment_fixture()
        tx, token = chain.transaction(tx_hash, fee_model='op-jovian'), chain.transfer(tx_hash, 2)
        results = self.concurrent([lambda: withdrawals.reconcile(self.db, attempt['id'], tx, token) for _ in range(3)])
        self.assertTrue(all(isinstance(value, dict) and value['outcome']=='paid' for value in results), results)
        self.assertEqual(self.balance()['available'], 60*TIG)
        self.assertEqual(self.balance()['pending_withdrawals'], 0)
        self.assertEqual(self.balance()['last_paid_at'], tx.block_timestamp)
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='operator:custody:NATIVE'")['balance'], 940)
        self.assertEqual(self.row('SELECT balance FROM accounts WHERE id=%s', (withdrawals.gas_hold(attempt['id']),))['balance'], 0)
        with self.assertRaisesRegex(Conflict, 'seven days'):
            withdrawals.request(self.db, self.member, 'next', TIG)
        from pool_manager.pool_v2.database import Database
        withdrawals.reconcile(Database(self.db.dsn), attempt['id'], tx, token)
        self.assertEqual(self.balance()['last_paid_at'], tx.block_timestamp)
        self.assertEqual(self.row('SELECT count(*) AS n FROM withdrawal_attempt_outcomes')['n'], 1)

    def test_unknown_send_stays_held_and_recovery_works_without_a_recorded_hash(self):
        self.fund_gas()
        row = self.approved(); attempt = self.begin(row)
        # Preparing another attempt, cancelling, and rejecting cannot refund an uncertain send.
        with self.assertRaises(Conflict): self.begin(row, nonce=2, key='new')
        with self.assertRaises(Conflict):
            withdrawals.release(self.db, row['id'], actor='operator', reason='timeout', event_key='reject')
        with self.assertRaises(Conflict):
            withdrawals.release(self.db, row['id'], member_id=self.member, actor='wallet', reason='cancel', event_key='cancel')
        self.assertEqual(self.begin(row)['id'], attempt['id'])
        self.assertEqual(self.row('SELECT count(*) AS n FROM withdrawal_transaction_claims')['n'], 0)
        _, chain, tx_hash = payment_fixture()
        result = withdrawals.reconcile(self.db, attempt['id'], chain.transaction(tx_hash, fee_model='op-jovian'), chain.transfer(tx_hash, 2))
        self.assertEqual(result['outcome'], 'paid')

    def test_confirmed_failure_charges_only_operator_and_can_retry_same_request(self):
        self.fund_gas(); row = self.approved(); first = self.begin(row)
        _, failed, hash1 = payment_fixture(status=0)
        with self.assertRaises(FundsError): failed.transfer(hash1, 2)
        tx = failed.transaction(hash1, fee_model='op-jovian')
        self.assertEqual(withdrawals.reconcile(self.db, first['id'], tx)['outcome'], 'failed')
        self.assertEqual(self.balance()['pending_withdrawals'], 40*TIG)
        self.assertIsNone(self.balance()['last_paid_at'])
        second = self.begin(row, nonce=2, key='retry')
        _, success, hash2 = payment_fixture(nonce=2)
        withdrawals.reconcile(self.db, second['id'], success.transaction(hash2, fee_model='op-jovian'), success.transfer(hash2, 2))
        self.assertEqual(self.balance()['available'], 60*TIG)
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='operator:custody:NATIVE'")['balance'], 880)

    def test_empty_finalized_nonce_replacement_can_release_after_explicit_reconciliation(self):
        self.fund_gas(); row = self.approved(); attempt = self.begin(row)
        _, chain, tx_hash = payment_fixture(to=CUSTODY, cancellation=True)
        tx = chain.transaction(tx_hash, fee_model='op-jovian')
        self.assertEqual(withdrawals.reconcile(self.db, attempt['id'], tx)['outcome'], 'cancelled')
        withdrawals.release(self.db, row['id'], actor='operator', reason='member cancelled', event_key='cancelled-after-reconciliation')
        self.assertEqual(self.balance()['available'], 100*TIG)
        self.assertIsNone(self.balance()['last_paid_at'])

    def test_wrong_nonce_amount_recipient_token_network_or_unmatched_success_cannot_pay(self):
        self.fund_gas(); row = self.approved(); attempt = self.begin(row)
        _, chain, tx_hash = payment_fixture()
        tx, token = chain.transaction(tx_hash, fee_model='op-jovian'), chain.transfer(tx_hash, 2)
        wrong_network = replace(NETWORK, chain_id=1)
        for wrong_tx, wrong_token in (
            (replace(tx, nonce=2), token), (replace(tx, sender=OTHER), token),
            (replace(tx, network=wrong_network), replace(token, network=wrong_network)),
            (tx, replace(token, amount=39*TIG)), (tx, replace(token, recipient=OTHER)),
            (tx, replace(token, network=replace(NETWORK, token=OTHER))),
            (tx, None),
        ):
            with self.subTest(tx=wrong_tx.tx_hash, token=wrong_token), self.assertRaises(Conflict):
                withdrawals.reconcile(self.db, attempt['id'], wrong_tx, wrong_token)
        self.assertEqual(self.balance()['pending_withdrawals'], 40*TIG)
        self.assertIsNone(self.balance()['last_paid_at'])
        self.assertEqual(withdrawals.reconcile(self.db, attempt['id'], tx, token)['outcome'], 'paid')

    def test_operator_fee_budget_and_chain_nonce_are_shared_across_members(self):
        self.fund_gas(amount=100)
        first, second = self.approved(), self.approved(member=self.other)
        snapshot = self.preflight()
        results = self.concurrent([lambda row=row: withdrawals.begin(self.db, row['id'], 'send', snapshot, fee_limit=100, actor='operator')
                                   for row in (first, second)])
        self.assertEqual(sum(isinstance(value, dict) for value in results), 1, results)
        self.assertEqual(sum(isinstance(value, Conflict) for value in results), 1, results)
        unstarted = next(row for row in (first, second) if not self.row('SELECT id FROM withdrawal_attempts WHERE withdrawal_id=%s', (row['id'],)))
        with self.assertRaises(InsufficientFunds): self.begin(unstarted, nonce=2)

    def test_actual_fee_over_budget_keeps_evidence_until_operator_tops_up(self):
        self.fund_gas(amount=100)
        row = self.approved(); attempt = self.begin(row)
        _, chain, tx_hash = payment_fixture(fee=150)
        tx, token = chain.transaction(tx_hash, fee_model='op-jovian'), chain.transfer(tx_hash, 2)
        with self.assertRaises(InsufficientFunds): withdrawals.reconcile(self.db, attempt['id'], tx, token)
        self.assertEqual(self.row('SELECT count(*) AS n FROM chain_transactions WHERE tx_hash=%s', (tx_hash,))['n'], 1)
        self.assertEqual(self.balance()['pending_withdrawals'], 40*TIG)
        self.fund_gas(amount=100, nonce=2)
        withdrawals.reconcile(self.db, attempt['id'], tx, token)
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='operator:custody:NATIVE'")['balance'], 50)
        self.assertEqual(self.balance()['available'], 60*TIG)

    def test_stale_backing_mismatch_and_weaker_finality_block_new_send(self):
        self.fund_gas(); row = self.approved(); snapshot = self.preflight()
        for invalid in (replace(snapshot, checked_at=snapshot.checked_at-timedelta(seconds=21)),
                        replace(snapshot, token_balance=snapshot.token_balance-1),
                        replace(snapshot, native_balance=snapshot.native_balance+1),
                        replace(snapshot, network=replace(NETWORK, require_finalized=False)),
                        replace(snapshot, network=replace(NETWORK, confirmations=1))):
            with self.assertRaises(Conflict):
                withdrawals.begin(self.db, row['id'], 'attempt', invalid, fee_limit=100, actor='operator')
        self.assertEqual(self.row('SELECT count(*) AS n FROM withdrawal_attempts')['n'], 0)

    def test_frozen_instructions_and_rejection_release_only_the_requested_amount(self):
        self.fund_gas(); row = self.approved()
        with self.db.transaction() as cursor:
            cursor.execute('UPDATE members SET withdrawal_wallet=%s WHERE id=%s', (OTHER, self.member))
        attempt = self.begin(row)
        instructions = withdrawals.instructions(self.db, attempt['id'])
        self.assertEqual(instructions['recipient'], WALLET)
        self.assertEqual(instructions['transaction']['nonce'], '0x1')
        self.assertEqual(instructions['transaction']['to'], TOKEN)
        self.assertEqual(instructions['transaction']['data'], '0xa9059cbb'+'0'*24+WALLET[2:]+f'{40*TIG:064x}')
        other = self.approved(member=self.other)
        withdrawals.release(self.db, other['id'], actor='operator', reason='declined', event_key='declined')
        self.assertEqual(self.balance(self.other)['available'], 100*TIG)

    def test_zero_token_transfer_records_once_without_credit_and_wrong_custody_is_rejected(self):
        zero = transfer(amount=0)
        self.assertEqual(deposits.receive(self.db, zero), deposits.receive(self.db, zero))
        self.assertEqual(self.balance()['available'], 100*TIG)
        self.assertEqual(self.row('SELECT count(*) AS n FROM transfers WHERE event_id=%s', (zero.event_id,))['n'], 1)
        wrong = replace(transfer(), network=replace(NETWORK, token=OTHER))
        with self.assertRaises(Conflict): deposits.receive(self.db, wrong)


class TransactionFeeTests(unittest.TestCase):
    def test_nonce_recovery_finds_a_finalized_transaction_without_its_saved_hash(self):
        data, basic, tx_hash = payment_fixture()
        counts = []
        def rpc(method, params):
            if method == 'eth_getTransactionCount':
                height = int(params[1],16); counts.append(height)
                return '0x2' if height >= 100 else '0x1'
            if method == 'eth_getBlockByNumber' and params == ['0x64', True]:
                return {'number': '0x64', 'transactions': [data['transaction']]}
            return basic.rpc(method, params)
        chain = Chain(NETWORK,rpc)
        self.assertEqual(chain.find_nonce(1,after_height=90),tx_hash)
        self.assertLess(len(counts),10)
        self.assertIsNone(chain.find_nonce(2,after_height=90))
        with self.assertRaises(FundsError): chain.find_nonce(1,after_height=100)

    def test_preflight_reads_finalized_balances_nonce_and_rejects_custody_code(self):
        settings = {'code': '0x', 'changed': False}
        header = {'number': '0x64', 'hash': '0x'+'a'*64}
        def rpc(method, params):
            if method == 'eth_chainId': return '0x2105'
            if method == 'eth_call': return '0x12' if params[0]['data']=='0x313ce567' else '0x'+f'{100*TIG:064x}'
            if method == 'eth_getBalance': return '0x3e8'
            if method == 'eth_getCode': return settings['code']
            if method == 'eth_getTransactionCount': return '0x5'
            if method == 'eth_getBlockByNumber':
                if params[0]=='latest': return {'number': '0x80'}
                if params[0]=='0x64' and settings['changed']: return {**header,'hash':'0x'+'b'*64}
                return dict(header)
            raise AssertionError((method,params))
        chain = Chain(NETWORK,rpc)
        checked = chain.preflight()
        self.assertEqual((checked.nonce,checked.token_balance,checked.native_balance),(5,100*TIG,1000))
        settings['code']='0xef0100'
        with self.assertRaises(FundsError): chain.preflight()
        settings['code']='0x';settings['changed']=True
        with self.assertRaises(FundsError): chain.preflight()

    def test_jovian_operator_fee_does_not_double_count_da_footprint(self):
        receipt = {'gasUsed': '0x64', 'effectiveGasPrice': '0xa', 'l1Fee': '0x14',
            'operatorFeeScalar': '0x2', 'operatorFeeConstant': '0x3', 'daFootprintGasScalar': '0x94', 'blobGasUsed': '0xffffff'}
        self.assertEqual(transaction_fee(receipt, 'op-jovian'), 21023)
        with self.assertRaises(FundsError): transaction_fee(receipt, 'op-isthmus')
        with self.assertRaises(FundsError): transaction_fee(receipt, 'ethereum')
        del receipt['daFootprintGasScalar']
        self.assertEqual(transaction_fee(receipt, 'op-isthmus'), 1023)

    def test_public_base_receipt_matches_execution_plus_actual_l1_fee(self):
        fixture = json.loads((Path(__file__).parent/'fixtures/base-transaction.json').read_text())
        receipt = fixture['receipt']
        expected = int(receipt['gasUsed'],16)*int(receipt['effectiveGasPrice'],16)+int(receipt['l1Fee'],16)
        self.assertEqual(transaction_fee(receipt, 'op-jovian'), expected)

    def test_transaction_verifier_rejects_unconfirmed_wrong_chain_and_unsupported_type(self):
        for key in ('failed-finality', 'wrong-chain', 'delegated-type', 'missing-fee'):
            data, chain, tx_hash = payment_fixture()
            if key == 'failed-finality': data['finalized']['number'] = '0x63'
            elif key == 'wrong-chain': data['transaction']['chainId'] = '0x1'
            elif key == 'delegated-type': data['transaction']['type'] = '0x4'
            else: del data['receipt']['l1Fee']
            with self.subTest(key=key), self.assertRaises(FundsError): chain.transaction(tx_hash, fee_model='op-jovian')
