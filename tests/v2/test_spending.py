"""Software-enforced spending caps: no cap means no fee commitment; ceilings count per attempt and per window."""

from datetime import datetime, timedelta, timezone
import hashlib
import unittest

from fastapi.testclient import TestClient

from pool_manager.pool_v2 import benchmarks, controls, custody, ledger, spending, withdrawals
from pool_manager.pool_v2.api import Settings, create_app
from pool_manager.pool_v2.chain import CustodyPreflight
from pool_manager.pool_v2.money import Conflict, FundsError, TIG
from funds_helpers import DatabaseCase, CUSTODY, NETWORK, OTHER
from test_withdrawals import payment_fixture


def cap(database, **overrides):
    values = dict(window_seconds=86400, tig_fee_window_units=3*TIG, tig_fee_attempt_units=TIG,
                  native_fee_window_units=700, native_fee_attempt_units=400,
                  actor='operator', reason='fixture cap', event_key='cap-1')
    values.update(overrides)
    return spending.set_cap(database, **values)


class FeeCapTests(DatabaseCase):
    spending_cap = False

    def setUp(self):
        super().setUp()
        self.fund()
        self.fund(OTHER)
        self.fees(10*TIG)

    def test_without_a_cap_nothing_is_committed_and_the_reason_is_visible(self):
        self.assertEqual(controls.blocked(self.db), spending.UNSET)
        with self.assertRaises(Conflict) as caught:
            self.reserve('one', fee=0)
        self.assertIn(spending.UNSET, str(caught.exception))
        status = spending.status(self.db)
        self.assertEqual((status['configured'], status['cap'], status['block_reason']), (False, None, spending.UNSET))
        self.assertEqual(self.row('SELECT count(*) AS n FROM reservations')['n'], 0)

    def test_caps_are_validated_append_only_and_idempotent_by_event_key(self):
        for bad in (dict(window_seconds=10), dict(window_seconds='86400'), dict(tig_fee_attempt_units=4*TIG),
                    dict(native_fee_attempt_units=701), dict(tig_fee_window_units=-1), dict(actor=''), dict(reason=''),
                    dict(event_key=''), dict(tig_fee_window_units=2**128)):
            with self.subTest(bad=bad), self.assertRaises(FundsError):
                cap(self.db, **bad)
        first = cap(self.db)
        self.assertEqual(cap(self.db)['id'], first['id'])
        with self.assertRaises(Conflict):
            cap(self.db, reason='different')
        second = cap(self.db, event_key='cap-2', tig_fee_window_units=5*TIG)
        with self.db.transaction() as cursor:
            self.assertEqual(spending.current(cursor)['id'], second['id'])
        self.assertEqual(self.row('SELECT count(*) AS n FROM spending_caps')['n'], 2)
        with self.assertRaises(Exception):
            with self.db.transaction() as cursor:
                cursor.execute('DELETE FROM spending_caps')
        self.assertEqual(self.row('SELECT count(*) AS n FROM spending_caps')['n'], 2)
        self.assertEqual(controls.blocked(self.db), None)

    def test_fee_ceilings_are_capped_per_attempt_and_per_rolling_window(self):
        cap(self.db)
        with self.assertRaises(Conflict) as caught:
            self.reserve('big', fee=2*TIG)
        self.assertIn('per-attempt', str(caught.exception))
        first = self.reserve('a', fee=TIG)
        self.reserve('b', member=self.other, fee=TIG)
        self.assertEqual(spending.status(self.db)['window']['tig_fee_units'], 2*TIG)
        # Sending counts the reservation once: its pending ceiling becomes its sent ceiling.
        benchmarks.mark_submitting(self.db, first['id'])
        self.assertEqual(spending.status(self.db)['window']['tig_fee_units'], 2*TIG)
        self.reserve('c', fee=TIG)
        status = spending.status(self.db)
        self.assertEqual((status['window']['tig_fee_units'], status['window']['remaining']['tig_fee_units'], status['block_reason']),
                         (3*TIG, 0, spending.EXHAUSTED))
        self.assertEqual(controls.blocked(self.db), spending.EXHAUSTED)
        with self.assertRaises(Conflict):
            self.reserve('d', member=self.other, fee=TIG)
        with self.assertRaises(Conflict):
            self.reserve('free', member=self.other, fee=0)
        later = datetime.now(timezone.utc) + timedelta(days=2)
        self.assertEqual(spending.status(self.db, now=later)['window']['remaining']['tig_fee_units'], 3*TIG)
        # A refund or rejection never recycles a committed ceiling inside the window.
        benchmarks.release_unstarted(self.db, first['id'], rejected=True, actual_fee=0, evidence={'fixture': 'rejected'})
        self.assertEqual(spending.status(self.db)['window']['tig_fee_units'], 3*TIG)

    def test_a_zero_cap_stops_new_fees_without_touching_existing_reservations(self):
        cap(self.db)
        row = self.reserve('a', fee=TIG)
        cap(self.db, event_key='stop', tig_fee_window_units=0, tig_fee_attempt_units=0)
        self.assertEqual(controls.blocked(self.db), spending.EXHAUSTED)
        with self.assertRaises(Conflict):
            benchmarks.mark_submitting(self.db, row['id'])
        self.assertEqual(self.row('SELECT state FROM reservations WHERE id=%s', (row['id'],))['state'], 'reserved')


class GasCapTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.fund()
        self.fund(OTHER)
        cap(self.db, event_key='gas-cap')
        _, chain, tx_hash = payment_fixture(sender='0x'+'7'*40, to=CUSTODY, value=5000, nonce=1)
        custody.receive_native(self.db, chain.transaction(tx_hash, fee_model='op-jovian'))

    def preflight(self, nonce):
        with self.db.transaction() as cursor:
            return CustodyPreflight(NETWORK, nonce, ledger.backing(cursor), ledger.backing(cursor, 'NATIVE'),
                                    90, datetime.now(timezone.utc), {'fixture': True})

    def approved(self, member, key):
        row = withdrawals.request(self.db, member, key, 40*TIG)
        withdrawals.approve(self.db, row['id'], NETWORK, fee_model='op-jovian', actor='operator', evidence={'reviewed': True})
        return row

    def test_gas_ceilings_are_capped_per_send_and_per_window(self):
        first = self.approved(self.member, 'w1')
        with self.assertRaises(Conflict) as caught:
            withdrawals.begin(self.db, first['id'], 'big', self.preflight(1), fee_limit=500, actor='operator')
        self.assertIn('per-send', str(caught.exception))
        withdrawals.begin(self.db, first['id'], 'ok', self.preflight(1), fee_limit=400, actor='operator')
        self.assertEqual(spending.status(self.db)['window']['remaining']['native_fee_units'], 300)
        second = self.approved(self.other, 'w2')
        with self.assertRaises(Conflict) as caught:
            withdrawals.begin(self.db, second['id'], 'ok', self.preflight(2), fee_limit=400, actor='operator')
        self.assertIn('window', str(caught.exception))
        self.assertEqual(self.row('SELECT count(*) AS n FROM withdrawal_attempts')['n'], 1)
        attempt = withdrawals.begin(self.db, second['id'], 'ok', self.preflight(2), fee_limit=300, actor='operator')
        self.assertEqual(int(attempt['fee_limit']), 300)
        self.assertEqual(spending.status(self.db)['window']['remaining']['native_fee_units'], 0)


class SpendingApiTests(DatabaseCase):
    spending_cap = False

    def test_operator_records_caps_and_reads_the_window(self):
        secret = 'operator-only'
        settings = Settings(self.db.dsn, 'https://pool.example', 8453, hashlib.sha256(secret.encode()).hexdigest(),
                            funds_enabled=True, work_enabled=True, pool_player_id=CUSTODY)
        client = TestClient(create_app(settings))
        headers = {'Authorization': 'Bearer ' + secret}
        before = client.get('/api/v2/capabilities').json()
        self.assertEqual((before['work_enabled'], before['work_block_reason']), (False, spending.UNSET))
        self.assertEqual(client.get('/api/v2/operator/spending').status_code, 401)
        body = {'event_key': 'cap-api', 'reason': 'launch budget', 'window_seconds': 30*86400,
                'tig_fee_window_units': str(10*TIG), 'tig_fee_attempt_units': str(TIG//100),
                'native_fee_window_units': '500000000000000', 'native_fee_attempt_units': '100000000000000'}
        recorded = client.post('/api/v2/operator/spending-cap', json=body, headers=headers)
        self.assertEqual(recorded.status_code, 200, recorded.text)
        self.assertEqual(client.post('/api/v2/operator/spending-cap', json=body, headers=headers).json()['id'], recorded.json()['id'])
        self.assertEqual(client.post('/api/v2/operator/spending-cap', json={**body, 'reason': 'changed'}, headers=headers).status_code, 409)
        self.assertEqual(client.post('/api/v2/operator/spending-cap', json={**body, 'event_key': 'bad', 'window_seconds': 60}, headers=headers).status_code, 422)
        status = client.get('/api/v2/operator/spending', headers=headers).json()
        self.assertTrue(status['configured'])
        self.assertEqual(status['window']['remaining']['tig_fee_units'], 10*TIG)
        self.assertTrue(status['cap']['actor'].startswith('operator'))
        after = client.get('/api/v2/capabilities').json()
        self.assertEqual((after['work_enabled'], after['work_block_reason']), (True, None))


if __name__ == '__main__':
    unittest.main()
