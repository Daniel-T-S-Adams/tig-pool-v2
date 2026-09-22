from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import Mock, patch

import psycopg2
from fastapi.testclient import TestClient

from pool_manager.pool_v2 import benchmarks, controls, custody, deposits, funding, members, pilot, starter_credit, submissions, work_requests
from pool_manager.pool_v2.coordinator import Coordinator
from pool_manager.pool_v2.database import Database
from pool_manager.pool_v2.money import Conflict, FundsError, TIG
from funds_helpers import DatabaseCase, CUSTODY, OTHER, WALLET, transfer
from test_starter_credit import TESTNET, starter_capture


def configuration():
    return {'version': 1, 'api_origin': starter_credit.API_ORIGIN, 'chain_id': TESTNET.chain_id,
            'token': TESTNET.token, 'pool_wallet': CUSTODY, 'maximum_total_tig_units': str(5*TIG),
            'max_fee_per_attempt_units': str(TIG//100),
            'members': [{'wallet': WALLET, 'funding_units': str(TIG)}, {'wallet': OTHER, 'funding_units': str(TIG)}]}


class PilotTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        with self.db.transaction() as cursor: custody.bind(cursor, TESTNET)
        capture = funding.record(self.db, starter_capture())['capture_id']
        starter_credit.initialize(self.db, capture, player_id=CUSTODY, actor='operator')
        self.config = configuration()
        pilot.initialize(self.db, self.config, actor='operator')
        for member, wallet in ((self.member, WALLET), (self.other, OTHER)):
            deposits.receive(self.db, replace(transfer(wallet, amount=TIG), network=TESTNET))
            members.set_multiplier(self.db, member, '0.02', actor='operator', reason='bounded test', event_key=wallet)
        self.custody_status = self.enterContext(patch('pool_manager.pool_v2.chain_observer.status',
            return_value={'initialized': True, 'ready': True}))

    def reserve(self, key='one', member=None, resource='CPU', fee=TIG//1000, player=CUSTODY):
        row = benchmarks.reserve(self.db, member or self.member, key, creation_round=10, resource=resource,
            selection={'height': 8, 'binary': {'details': {'download_url': 'https://testnet-api.tig.foundation/fixture.tar.gz'}}},
            payload={'settings': {'block_id': 'block-8', 'player_id': player}, 'compute_type': 'aws_c7g',
                     'track_settings': {'a': {'num_bundles': 5}}}, fee_limit=fee, offer_expires_at=self.expiry)
        with self.db.transaction() as cursor: work_requests.enqueue(cursor, row['id'], 'precommit', row['payload'])
        submissions.prepare_archive(self.db, row['id'], row['selection']['binary']['details']['download_url'], b'fixture')
        return row

    def send(self, row):
        intent = self.row("SELECT id FROM protocol_outbox WHERE reservation_id=%s AND kind='precommit'", (row['id'],))
        return submissions.begin(self.db, intent['id'],
            preflight={'block_id': 'block-8', 'height': 8, 'seen_benchmarks': [], 'observed_at': int(time.time())})

    def accept(self, row, identity='a'*32):
        intent = self.row("SELECT id FROM protocol_outbox WHERE reservation_id=%s AND kind='precommit'", (row['id'],))
        submissions.record_response(self.db, intent['id'], {'status': 200, 'body': {'benchmark_id': identity}})
        accepted = benchmarks.accept(self.db, row['id'], identity, {'fixture': True},
            actual_fee=int(row['fee_limit']), evidence={'fixture': True})
        benchmarks.acknowledge(self.db, row['id'], row['member_id'], accepted['assignment_digest'])
        with self.db.transaction() as cursor: remaining = funding.balance(cursor)
        funding.record(self.db, starter_capture(available=remaining))
        return accepted

    def active(self, row, identity='a'*32):
        self.accept(row, identity)
        benchmarks.record_outcome(self.db, row['id'], 'active', height=20, evidence={'fixture': True})

    def test_two_sequential_attempts_then_restart_and_resume_cannot_extend_pilot(self):
        with self.assertRaises(Conflict): self.reserve('b-too-early', member=self.other)
        first = self.reserve()
        self.assertEqual(int(first['amount']), TIG)
        self.assertEqual(self.reserve()['id'], first['id'])
        with self.assertRaises(Conflict): self.reserve('parallel-a')
        with self.assertRaises(Conflict): self.reserve('parallel-b', member=self.other)
        self.send(first)
        with self.assertRaises(Conflict): self.reserve('uncertain-b', member=self.other)
        self.active(first)
        with self.assertRaises(Conflict): self.reserve('extra-a')
        second = self.reserve('member-b', member=self.other)
        self.send(second); self.active(second, 'b'*32)
        # Reopening connections, replaying setup and toggling operator controls
        # must not erase the permanently consumed attempt authorizations.
        self.db = Database(self.db.dsn)
        pilot.initialize(self.db, self.config, actor='operator')
        controls.set_pause(self.db, True, actor='operator', reason='test', event_key='pause')
        controls.set_pause(self.db, False, actor='operator', reason='test', event_key='resume')
        for member in (self.member, self.other):
            with self.assertRaisesRegex(Conflict, 'both precommit attempts'):
                self.reserve('after-restart', member=member)
        self.assertEqual(pilot.status(self.db)['attempts_used'], 2)
        self.assertEqual(pilot.status(self.db)['committed_fee_units'], str(2*TIG//1000))

    def test_concurrent_requests_and_submissions_cannot_consume_multiple_attempts(self):
        rows = self.concurrent([lambda: self.reserve('race-a'), lambda: self.reserve('race-b')])
        self.assertEqual(sum(isinstance(row, dict) for row in rows), 1, rows)
        row = next(row for row in rows if isinstance(row, dict))
        outcomes = self.concurrent([lambda: self.send(row), lambda: self.send(row)])
        self.assertEqual(sum(isinstance(value, dict) for value in outcomes), 1, outcomes)
        self.assertEqual(pilot.status(self.db)['attempts_used'], 1)

    def test_proven_unsent_cancellation_can_be_replaced_without_using_an_attempt(self):
        first = self.reserve()
        intent = self.row('SELECT id FROM protocol_outbox WHERE reservation_id=%s', (first['id'],))
        submissions.cancel_unsent(self.db, intent['id'], evidence={'fixture': 'block changed'})
        self.assertEqual(pilot.status(self.db)['attempts_used'], 0)
        second = self.reserve('new-current-block')
        self.send(second)
        self.assertEqual(pilot.status(self.db)['attempts_used'], 1)

    def test_rejection_counts_even_with_full_refund_and_stops_the_next_member(self):
        row = self.reserve(); self.send(row)
        benchmarks.release_unstarted(self.db, row['id'], rejected=True, actual_fee=0, evidence={'fixture': 'definitive rejection'})
        self.assertEqual(self.balance()['available'], TIG)
        self.assertEqual(pilot.status(self.db)['attempts_used'], 1)
        for member in (self.member, self.other):
            with self.assertRaisesRegex(Conflict, 'failure or uncertainty'):
                self.reserve('after-rejection', member=member)

    def test_verification_failure_stops_later_work_without_changing_collateral_rules(self):
        row = self.reserve(); self.send(row); self.accept(row)
        benchmarks.record_outcome(self.db, row['id'], 'verification_failed', height=20, evidence={'fixture': True})
        self.assertEqual(self.balance()['collateral'], TIG)
        self.assertEqual(self.balance()['slots'], 0)
        with self.assertRaisesRegex(Conflict, 'failure or uncertainty'):
            self.reserve('after-failure', member=self.other)

    def test_gpu_wrong_player_excess_fee_zero_or_excess_collateral_are_refused(self):
        for kwargs in ({'resource': 'GPU'}, {'player': OTHER}, {'fee': TIG//100+1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(Conflict): self.reserve(**kwargs)
        for value in ('0', '1'):
            members.set_multiplier(self.db, self.member, value, actor='operator', reason='fixture', event_key=value)
            with self.assertRaisesRegex(Conflict, 'positive collateral'):
                self.reserve('invalid-collateral-'+value)
        self.assertEqual(pilot.status(self.db)['attempts_used'], 0)

    def test_uninitialized_or_stale_monitors_block_both_reservation_and_send(self):
        for state in ({'initialized': False, 'ready': False}, {'initialized': True, 'ready': False}):
            self.custody_status.return_value = state
            with self.assertRaises(Conflict): self.reserve()
        self.custody_status.return_value = {'initialized': True, 'ready': True}
        row = self.reserve()
        self.custody_status.return_value = {'initialized': False, 'ready': False}
        with self.assertRaises(Conflict): self.send(row)
        self.custody_status.return_value = {'initialized': True, 'ready': True}
        with patch('pool_manager.pool_v2.funding.status', return_value={'initialized': False, 'ready': False}):
            with self.assertRaises(Conflict): self.send(row)
        self.assertEqual(pilot.status(self.db)['attempts_used'], 0)

    def test_additional_custody_receipts_stop_send_and_topups_are_disabled(self):
        row = self.reserve()
        deposits.receive(self.db, replace(transfer(amount=1), network=TESTNET))
        with self.assertRaisesRegex(Conflict, 'receipts exceed'): self.send(row)
        with self.assertRaisesRegex(Conflict, 'top-ups are disabled'):
            with self.db.transaction() as cursor: pilot.prohibit_topup(cursor)

    def test_result_submission_remains_available_when_attempt_limit_is_used(self):
        first = self.reserve(); self.send(first); self.active(first)
        second = self.reserve('b', member=self.other); self.send(second); self.accept(second, 'b'*32)
        with self.db.transaction() as cursor:
            work_requests.enqueue(cursor, second['id'], 'results', {'fixture': True})
        result = self.row("SELECT id FROM protocol_outbox WHERE reservation_id=%s AND kind='results'", (second['id'],))
        self.assertEqual(submissions.begin(self.db, result['id'])['state'], 'uncertain')
        self.assertEqual(pilot.status(self.db)['attempts_used'], 2)

    def test_policy_cannot_be_changed_deleted_or_reset_and_requires_exact_network(self):
        wrong = deepcopy(self.config); wrong['members'].reverse()
        with self.assertRaises(Conflict): pilot.initialize(self.db, wrong, actor='operator')
        for sql in ('DELETE FROM pilot_limits', 'TRUNCATE pilot_limits', "UPDATE pilot_limits SET actor='someone'"):
            with self.assertRaises(psycopg2.Error):
                with self.db.transaction() as cursor: cursor.execute(sql)
        pilot.require_service(self.db, api_origin=starter_credit.API_ORIGIN, player_id=CUSTODY, required=True)
        for origin, player in (('https://mainnet-api.tig.foundation', CUSTODY), (starter_credit.API_ORIGIN, OTHER)):
            with self.assertRaises(Conflict): pilot.require_service(self.db, api_origin=origin, player_id=player)
        writer = Mock(origin='https://mainnet-api.tig.foundation', enabled=True)
        coordinator = Coordinator(self.db, CUSTODY, Mock(), writer, new_work=True)
        with self.assertRaises(Conflict): coordinator.dispatch_one()
        writer.post.assert_not_called()

    def test_malformed_or_over_budget_configuration_cannot_be_installed(self):
        for field, value in (('maximum_total_tig_units', str(5*TIG+1)), ('maximum_total_tig_units', str(2*TIG)),
                             ('max_fee_per_attempt_units', 0.001), ('version', True),
                             ('chain_id', 8453), ('token', OTHER), ('api_origin', 'https://mainnet-api.tig.foundation')):
            config = deepcopy(self.config); config[field] = value
            with self.subTest(field=field), self.assertRaises(FundsError): pilot.validate(config)
        config = deepcopy(self.config); config['members'][1]['wallet'] = WALLET
        with self.assertRaises(FundsError): pilot.validate(config)

    def test_private_api_factory_starts_closed_and_operator_can_inspect_limits(self):
        from pool_manager.pool_v2.service import application
        config = {'origin': 'https://localhost:18443', 'chain_id': TESTNET.chain_id,
            'pool_player_id': CUSTODY, 'tig_api_url': starter_credit.API_ORIGIN, 'require_pilot_limits': True}
        token = 'test-only-operator-token-' + 'x'*32
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'service.json'; path.write_text(json.dumps(config))
            with patch.dict(os.environ, {'POOL_V2_SERVICE_CONFIG': str(path), 'POOL_V2_DATABASE_DSN': self.db.dsn,
                                         'POOL_V2_OPERATOR_TOKEN': token}):
                with TestClient(application()) as client:
                    self.assertEqual(client.get('/api/v2/operator/pilot').status_code, 401)
                    result = client.get('/api/v2/operator/pilot', headers={'Authorization': 'Bearer '+token})
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(result.json()['attempts_used'], 0)
                    self.assertEqual(result.json()['config'], self.config)
                for field, value in (('work_enabled', 'false'), ('chain_id', 8453), ('database_dsn', 'unsafe')):
                    path.write_text(json.dumps({**config, field: value}))
                    with self.assertRaises(ValueError): application()
