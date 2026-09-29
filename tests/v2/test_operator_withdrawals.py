from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import unittest

from fastapi.testclient import TestClient
from eth_account import Account
from eth_account.messages import encode_defunct
import psycopg2

from pool_manager.pool_v2 import custody, deposits, funding, ledger, topups, withdrawals
from pool_manager.pool_v2.api import Settings, create_app
from pool_manager.pool_v2.chain import CustodyPreflight
from pool_manager.pool_v2.money import Conflict, FundsError, InsufficientFunds, TIG
from funds_helpers import DatabaseCase, NETWORK, CUSTODY, OTHER, transfer
from test_funding import funding_capture
from test_withdrawals import payment_fixture


INCOME = '0x' + '9' * 40


class OperatorWithdrawalTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.fund(amount=100*TIG)
        self.fees(20*TIG)
        receipt = transfer(sender='0x'+'8'*40, amount=50*TIG)
        deposits.receive(self.db, receipt)
        deposits.attribute_reviewed(self.db, receipt, operator=True, actor='operator', evidence={'own_funds': True})
        _, chain, tx_hash = payment_fixture(sender='0x'+'7'*40, to=CUSTODY, value=1000)
        custody.receive_native(self.db, chain.transaction(tx_hash, fee_model='op-jovian'))

    def operator_balance(self):
        return int(self.row("SELECT balance FROM accounts WHERE id='operator:custody:TIG'")['balance'])

    def request(self, key='income', amount=40*TIG, reserve=10*TIG, recipient=INCOME):
        return withdrawals.request_operator(self.db, key, amount, NETWORK, recipient=recipient,
            reserve=reserve, actor='operator', reason='Settled operator funds')

    def preflight(self, nonce=1):
        with self.db.transaction() as cursor:
            return CustodyPreflight(NETWORK, nonce, ledger.backing(cursor), ledger.backing(cursor, 'NATIVE'),
                90, datetime.now(timezone.utc), {'fixture': True})

    def approve(self, row):
        withdrawals.approve(self.db, row['id'], NETWORK, fee_model='op-jovian', actor='operator', evidence={'checked': True})

    def begin(self, row, nonce=1, key='send'):
        return withdrawals.begin(self.db, row['id'], key, self.preflight(nonce), fee_limit=100, actor='operator')

    def test_operator_reserve_excludes_member_and_prepaid_protocol_funds(self):
        for amount in (40*TIG+1, 51*TIG, 150*TIG):
            with self.subTest(amount=amount), self.assertRaises(InsufficientFunds):
                self.request(amount=amount)
        self.assertEqual(self.row('SELECT count(*) AS n FROM withdrawals')['n'], 0)
        row = self.request(amount=40*TIG-1)
        self.assertEqual(self.operator_balance(), 10*TIG+1)
        self.assertEqual(self.balance()['available'], 100*TIG)
        self.assertEqual(self.balance()['pending_withdrawals'], 0)
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='operator:protocol:TIG'")['balance'], 20*TIG)
        for change in ({'recipient': OTHER}, {'reserve': 0}, {'amount': 40*TIG}):
            with self.assertRaises(Conflict):
                self.request(**{'amount': 40*TIG-1, **change})
        for column, value in (('recipient', OTHER), ('operator_reserve', 0), ('kind', 'member'), ('member_id', self.member)):
            with self.subTest(column=column), self.assertRaises(psycopg2.Error), self.db.transaction() as cursor:
                cursor.execute('UPDATE withdrawals SET '+column+'=%s WHERE id=%s', (value, row['id']))

    def test_concurrent_requests_and_retries_reserve_once_and_release_to_operator(self):
        rows = self.concurrent([lambda: self.request() for _ in range(3)])
        self.assertTrue(all(isinstance(row, dict) for row in rows), rows)
        self.assertEqual(len({row['id'] for row in rows}), 1)
        row = rows[0]
        with self.assertRaises(Conflict):
            self.request(key='second', amount=1, reserve=0)
        with self.assertRaises(FundsError):
            withdrawals.release(self.db, row['id'], member_id=self.member, actor='member', reason='not mine', event_key='attack')
        self.approve(row)
        for _ in range(2):
            withdrawals.release(self.db, row['id'], actor='operator', reason='defer payout', event_key='reject')
        self.assertEqual(self.operator_balance(), 50*TIG)
        self.assertEqual(self.balance()['available'], 100*TIG)
        self.assertEqual(self.request()['state'], 'rejected')

    def test_payment_recovery_is_idempotent_and_does_not_change_member_cooldown(self):
        row = self.request()
        self.approve(row)
        attempt = self.begin(row)
        self.assertEqual(self.begin(row)['id'], attempt['id'])
        instructions = withdrawals.instructions(self.db, attempt['id'])
        self.assertEqual((instructions['kind'], instructions['recipient']), ('operator', INCOME))
        with self.assertRaises(Conflict):
            withdrawals.release(self.db, row['id'], actor='operator', reason='lost response', event_key='unsafe')
        _, chain, tx_hash = payment_fixture(recipient=INCOME)
        tx, token = chain.transaction(tx_hash, fee_model='op-jovian'), chain.transfer(tx_hash, 2)
        for wrong in (replace(token, recipient=OTHER), replace(token, amount=40*TIG-1)):
            with self.assertRaises(Conflict):
                withdrawals.reconcile(self.db, attempt['id'], tx, wrong)
        results = self.concurrent([lambda: withdrawals.reconcile(self.db, attempt['id'], tx, token) for _ in range(3)])
        self.assertTrue(all(isinstance(result, dict) and result['outcome']=='paid' for result in results), results)
        self.assertEqual(self.operator_balance(), 10*TIG)
        self.assertEqual(self.balance()['available'], 100*TIG)
        self.assertIsNone(self.balance()['last_paid_at'])
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='operator:custody:NATIVE'")['balance'], 940)
        self.assertEqual(self.row('SELECT count(*) AS n FROM custody_payments')['n'], 1)
        with self.db.transaction() as cursor:
            self.assertEqual(ledger.backing(cursor), 110*TIG)
        # Operator requests have no member cooldown.
        self.assertEqual(self.request(key='next', amount=1, reserve=0)['kind'], 'operator')

    def test_failed_send_requires_final_evidence_before_retry_or_release(self):
        row = self.request()
        self.approve(row)
        attempt = self.begin(row)
        _, chain, tx_hash = payment_fixture(status=0, recipient=INCOME)
        outcome = withdrawals.reconcile(self.db, attempt['id'], chain.transaction(tx_hash, fee_model='op-jovian'))
        self.assertEqual(outcome['outcome'], 'failed')
        self.assertEqual(self.operator_balance(), 10*TIG)
        self.assertEqual(self.row('SELECT state FROM withdrawals WHERE id=%s', (row['id'],))['state'], 'approved')
        withdrawals.release(self.db, row['id'], actor='operator', reason='failed payout deferred', event_key='defer')
        self.assertEqual(self.operator_balance(), 50*TIG)

    def test_member_payment_and_operator_payout_share_nonce_and_gas_reservations(self):
        operator = self.request()
        member = withdrawals.request(self.db, self.member, 'member', 10*TIG)
        for row in (operator, member): self.approve(row)
        snapshot = self.preflight()
        results = self.concurrent([lambda row=row: withdrawals.begin(self.db, row['id'], 'send', snapshot,
            fee_limit=100, actor='operator') for row in (operator, member)])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1, results)
        self.assertEqual(sum(isinstance(result, Conflict) for result in results), 1, results)
        self.assertEqual(self.row('SELECT count(*) AS n FROM custody_sends')['n'], 1)

    def test_topup_and_payout_cannot_reserve_the_same_operator_tokens(self):
        # Top-up is separately entitled to use the retained operating budget.
        funding.record(self.db, funding_capture(20*TIG))
        policy = funding.status(self.db)['observation']['id']
        snapshot = self.preflight()
        results = self.concurrent([
            lambda: self.request(amount=40*TIG, reserve=0),
            lambda: topups.begin(self.db, 'topup', snapshot, policy, amount=40*TIG, fee_limit=100,
                fee_model='op-jovian', actor='operator'),
        ])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1, results)
        self.assertEqual(sum(isinstance(result, InsufficientFunds) for result in results), 1, results)
        self.assertEqual(self.operator_balance(), 10*TIG)
        self.assertEqual(self.balance()['available'], 100*TIG)

    def test_operating_reserve_is_rechecked_before_payment(self):
        row = self.request()
        self.approve(row)
        with self.db.transaction() as cursor:
            ledger.account(cursor, 'operator:test-expense', 'operator_commitment')
            ledger.post(cursor, 'expense-reserved', 'fixture',
                [('operator:custody:TIG', -1), ('operator:test-expense', 1)])
        with self.assertRaises(InsufficientFunds): self.begin(row)
        self.assertEqual(self.row('SELECT count(*) AS n FROM withdrawal_attempts')['n'], 0)
        with self.db.transaction() as cursor:
            ledger.post(cursor, 'expense-returned', 'fixture',
                [('operator:test-expense', -1), ('operator:custody:TIG', 1)])
        with self.assertRaises(InsufficientFunds):
            withdrawals.begin(self.db, row['id'], 'send', self.preflight(), fee_limit=100, actor='operator', operator_reserve=11*TIG)
        self.assertEqual(self.begin(row)['withdrawal_id'], row['id'])


class OperatorWithdrawalApiTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.secret = 'operator-only'
        self.headers = {'Authorization': 'Bearer '+self.secret}
        self.settings = Settings(self.db.dsn, 'https://pool.example', 8453, hashlib.sha256(self.secret.encode()).hexdigest(),
            funds_enabled=True, custody_network=NETWORK, custody_rpc_url='https://rpc.example', withdrawal_fee_model='op-jovian',
            operator_income_wallet=INCOME, operator_tig_reserve_units=str(10*TIG))
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)
        self.body = {'amount': str(40*TIG), 'request_key': 'income', 'reason': 'Monthly operator payout'}
        self.fund()
        receipt = transfer(sender='0x'+'8'*40, amount=50*TIG)
        deposits.receive(self.db, receipt)
        deposits.attribute_reviewed(self.db, receipt, operator=True, actor='operator', evidence={'own_funds': True})
        self.path = '/api/v2/operator/income/withdrawals'

    def test_only_operator_can_request_and_destination_cannot_be_supplied(self):
        signer = Account.create()
        challenge = self.client.post('/api/v2/auth/challenges', json={'wallet': signer.address}).json()
        signature = Account.sign_message(encode_defunct(text=challenge['message']), signer.key).signature.hex()
        session = self.client.post('/api/v2/auth/sessions', json={'challenge_id': challenge['id'], 'signature': signature}).json()
        member_headers = {'Authorization': 'Bearer '+session['token']}
        worker = self.client.post('/api/v2/auth/execution-tokens', headers=member_headers).json()
        worker_headers = {'Authorization': 'Bearer '+worker['token']}
        for headers in (member_headers, worker_headers):
            self.assertEqual(self.client.get('/api/v2/operator/income', headers=headers).status_code, 403)
            self.assertEqual(self.client.post(self.path, json=self.body, headers=headers).status_code, 403)
        self.assertEqual(self.client.post(self.path, json=self.body).status_code, 401)
        self.assertEqual(self.client.post(self.path, json={**self.body, 'recipient': OTHER}, headers=self.headers).status_code, 422)
        before = self.client.get('/api/v2/operator/income', headers=self.headers).json()
        self.assertEqual((before['withdrawable'], before['reserve']), (str(40*TIG), str(10*TIG)))
        row = self.client.post(self.path, json=self.body, headers=self.headers)
        self.assertEqual(row.status_code, 200, row.text)
        self.assertEqual(row.json()['recipient'], INCOME)
        self.assertIsNone(row.json()['member_id'])
        listed = self.client.get('/api/v2/operator/withdrawals', headers=self.headers).json()['withdrawals']
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]['kind'], 'operator')
        after = self.client.get('/api/v2/operator/income', headers=self.headers)
        self.assertEqual(after.headers['cache-control'], 'no-store')
        self.assertEqual(after.json()['pending'], str(40*TIG))
        self.assertEqual(after.json()['withdrawable'], '0')
        self.assertEqual(self.balance()['available'], 100*TIG)
        self.assertEqual(self.client.get('/api/v2/member/withdrawals', headers=member_headers).json()['withdrawals'], [])
        cancelled = self.client.post('/api/v2/withdrawals/'+row.json()['id']+'/cancel',
            json={'reason': 'not my funds', 'event_key': 'attack'}, headers=member_headers)
        self.assertEqual(cancelled.status_code, 400)

    def test_full_api_payment_can_be_recovered_after_payouts_are_disabled(self):
        _, native_chain, native_hash = payment_fixture(sender='0x'+'7'*40, to=CUSTODY, value=1000)
        custody.receive_native(self.db, native_chain.transaction(native_hash, fee_model='op-jovian'))
        _, chain, tx_hash = payment_fixture(recipient=INCOME)
        test = self
        class SimulatedChain:
            network = NETWORK
            def preflight(self):
                with test.db.transaction() as cursor:
                    return CustodyPreflight(NETWORK, 1, ledger.backing(cursor), ledger.backing(cursor, 'NATIVE'),
                        90, datetime.now(timezone.utc), {'fixture': True})
            def find_nonce(self, nonce, *, after_height):
                test.assertEqual((nonce, after_height), (1, 90))
                return tx_hash
            def transaction(self, *args, **kwargs): return chain.transaction(*args, **kwargs)
            def transfer(self, *args, **kwargs): return chain.transfer(*args, **kwargs)
        self.app.state.payment_chain = SimulatedChain()
        row = self.client.post(self.path, json=self.body, headers=self.headers).json()
        base = '/api/v2/operator/withdrawals/'+row['id']
        self.assertEqual(self.client.post(base+'/approve', json={'reason': 'checked'}, headers=self.headers).status_code, 200)
        body = {'request_key': 'send', 'fee_limit': '100'}
        begun = self.client.post(base+'/begin', json=body, headers=self.headers)
        self.assertEqual(begun.status_code, 200, begun.text)
        attempt = begun.json()
        self.assertEqual(attempt['kind'], 'operator')
        self.assertEqual(self.client.post(base+'/begin', json=body, headers=self.headers).json()['id'], attempt['id'])
        paused_app = create_app(replace(self.settings, funds_enabled=False, operator_income_wallet=None, operator_tig_reserve_units=None))
        paused_app.state.payment_chain = SimulatedChain()
        paused = TestClient(paused_app)
        path = '/api/v2/operator/withdrawal-attempts/'+attempt['id']
        self.assertEqual(paused.get(path, headers=self.headers).json()['recipient'], INCOME)
        for _ in range(2):
            result = paused.post(path+'/reconcile', json={}, headers=self.headers)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['outcome'], 'paid')
        self.assertEqual(self.balance()['available'], 100*TIG)
        self.assertEqual(self.row('SELECT count(*) AS n FROM custody_payments')['n'], 1)

    def test_unconfigured_and_paused_payouts_are_closed(self):
        for settings in (replace(self.settings, funds_enabled=False),
                         replace(self.settings, operator_income_wallet=None, operator_tig_reserve_units=None)):
            client = TestClient(create_app(settings))
            self.assertFalse(client.get('/api/v2/operator/income', headers=self.headers).json()['enabled'])
            self.assertEqual(client.post(self.path, json=self.body, headers=self.headers).status_code, 503)
        for values in ({'operator_income_wallet': None}, {'operator_tig_reserve_units': None},
                       {'operator_tig_reserve_units': '-1'}, {'operator_tig_reserve_units': 0},
                       {'operator_income_wallet': CUSTODY}):
            with self.subTest(values=values), self.assertRaises(ValueError): create_app(replace(self.settings, **values))

    def test_config_change_never_redirects_pending_request_and_release_works_while_paused(self):
        row = self.client.post(self.path, json=self.body, headers=self.headers).json()
        changed = TestClient(create_app(replace(self.settings, operator_income_wallet=OTHER)))
        base = '/api/v2/operator/withdrawals/'+row['id']
        result = changed.post(base+'/approve', json={'reason': 'checked'}, headers=self.headers)
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(self.row('SELECT recipient FROM withdrawals WHERE id=%s', (row['id'],))['recipient'], INCOME)
        paused = TestClient(create_app(replace(self.settings, funds_enabled=False, operator_income_wallet=None, operator_tig_reserve_units=None)))
        released = paused.post(base+'/reject', json={'reason': 'choose new wallet', 'event_key': 'changed'}, headers=self.headers)
        self.assertEqual(released.status_code, 200, released.text)
        self.assertEqual(self.client.get('/api/v2/operator/income', headers=self.headers).json()['available'], str(50*TIG))


if __name__ == '__main__':
    unittest.main()
