from dataclasses import replace
import importlib.util
import io
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import psycopg2

from pool_manager.pool_v2 import benchmarks, custody, deposits, funding, ledger, starter_credit, withdrawals
from pool_manager.pool_v2.chain import Network
from pool_manager.pool_v2.money import Conflict, FundsError, InsufficientFunds, TIG
from funds_helpers import DatabaseCase, CUSTODY, OTHER, transfer
from test_funding import funding_capture, protocol_topup


TESTNET = Network(starter_credit.CHAIN_ID, starter_credit.TOKEN, CUSTODY, 12)


def starter_capture(**kwargs):
    data = funding_capture(kwargs.pop('available', starter_credit.AMOUNT), **kwargs)
    data['api_origin'] = starter_credit.API_ORIGIN
    for end in ('start', 'end'):
        data[end]['block']['config']['erc20'] = {
            'chain_id': hex(TESTNET.chain_id), 'token_address': TESTNET.token,
            'rpc_url': 'https://sepolia.base.org'}
    return data


class StarterCreditTests(DatabaseCase):
    def bind(self, network=TESTNET):
        with self.db.transaction() as cursor:
            custody.bind(cursor, network)

    def capture(self, data=None):
        result = funding.record(self.db, starter_capture() if data is None else data)
        self.assertTrue(result['complete'])
        return result['capture_id']

    def initialize(self, capture_id, **kwargs):
        return starter_credit.initialize(self.db, capture_id,
            player_id=kwargs.get('player_id', CUSTODY), actor=kwargs.get('actor', 'pilot-operator'))

    def test_concurrent_opening_and_lost_response_recovery_credit_once(self):
        self.bind()
        proof = self.capture()
        self.assertFalse(funding.status(self.db)['ready'])
        results = self.concurrent([lambda: self.initialize(proof) for _ in range(3)])
        self.assertTrue(all(isinstance(result, dict) for result in results), results)
        self.assertEqual(len({result['journal_id'] for result in results}), 1)
        self.assertEqual(self.row('SELECT count(*) AS n FROM protocol_opening_credits')['n'], 1)
        self.assertTrue(funding.status(self.db)['ready'])
        with self.db.transaction() as cursor:
            self.assertEqual(funding.balance(cursor), 10*TIG)
            self.assertEqual(ledger.backing(cursor), 0)
            self.assertEqual(ledger.backing(cursor, 'NATIVE'), 0)
        self.assertEqual(self.balance()['available'], 0)
        with self.assertRaises(InsufficientFunds):
            withdrawals.request(self.db, self.member, 'no-cash-from-grant', TIG)
        # Subsequent fee consumption must not allow an opening balance reset.
        with self.db.transaction() as cursor:
            ledger.post(cursor, 'fixture-fee', 'fixture',
                [(benchmarks.OPERATOR_FEES, -TIG), ('external:protocol:TIG', TIG)])
        later = self.capture(starter_capture(available=9*TIG))
        self.assertEqual(self.initialize(later)['journal_id'], results[0]['journal_id'])
        self.assertEqual(self.row('SELECT balance FROM accounts WHERE id=%s', (benchmarks.OPERATOR_FEES,))['balance'], 9*TIG)
        self.assertTrue(funding.status(self.db)['ready'])
        with self.assertRaises(Conflict):
            self.initialize(later, player_id=OTHER)

    def test_credit_can_pay_operator_submission_fee_without_changing_member_cash(self):
        self.bind()
        self.initialize(self.capture())
        deposits.receive(self.db, replace(transfer(), network=TESTNET))
        reservation = self.reserve(fee=TIG//1000)
        benchmarks.mark_submitting(self.db, reservation['id'])
        benchmarks.accept(self.db, reservation['id'], 'fixture-benchmark', {'fixture': True},
            actual_fee=TIG//1000, evidence={'fixture': True})
        self.capture(starter_capture(available=10*TIG-TIG//1000))
        self.assertTrue(funding.status(self.db)['ready'])
        self.assertEqual(self.balance()['available'], 50*TIG)
        with self.db.transaction() as cursor:
            self.assertEqual(ledger.backing(cursor), 100*TIG)
        self.assertEqual(self.row('SELECT balance FROM accounts WHERE id=%s', (benchmarks.OPERATOR_FEES,))['balance'], 10*TIG-TIG//1000)

    def test_missing_or_wrong_custody_network_is_rejected(self):
        proof = self.capture()
        with self.assertRaises(Conflict): self.initialize(proof)
        self.bind(replace(TESTNET, chain_id=8453))
        with self.assertRaises(Conflict): self.initialize(proof)
        self.assertEqual(self.row('SELECT count(*) AS n FROM protocol_opening_credits')['n'], 0)

    def test_wrong_source_token_balance_player_and_topups_are_not_opening_credit(self):
        self.bind()
        cases = []
        wrong = starter_capture(); wrong['api_origin'] = 'https://mainnet-api.tig.foundation'; cases.append(wrong)
        wrong = starter_capture(); del wrong['api_origin']; cases.append(wrong)
        for field, value in (('chain_id', '0x2105'), ('token_address', OTHER)):
            wrong = starter_capture()
            wrong['start']['block']['config']['erc20'][field] = value
            cases.append(wrong)
        wrong = starter_capture(); wrong['end']['block']['config']['erc20']['chain_id'] = '0x2105'; cases.append(wrong)
        cases += [starter_capture(available=0), starter_capture(available=10*TIG-1), starter_capture(available=11*TIG)]
        for data in cases:
            with self.subTest(source=data.get('api_origin'), balance=data['player_data']['player']['state']):
                with self.assertRaises(FundsError): self.initialize(self.capture(data))
        proof = self.capture()
        with self.assertRaises(Conflict): self.initialize(proof, player_id=OTHER)
        with self.assertRaises(FundsError): self.initialize(proof, actor=' ')
        wrong = starter_capture(topup=protocol_topup('0x'+'a'*64, confirmed=False))
        with self.assertRaises(FundsError): self.initialize(self.capture(wrong))
        self.assertEqual(self.row('SELECT count(*) AS n FROM protocol_opening_credits')['n'], 0)

    def test_stale_superseded_and_incomplete_captures_are_rejected(self):
        self.bind()
        with self.assertRaises(Conflict): self.initialize(self.capture(starter_capture(age=121)))
        old = self.capture()
        self.capture(starter_capture(available=9*TIG))
        with self.assertRaises(Conflict): self.initialize(old)
        current = self.capture()
        failed = starter_capture(); failed['error'] = 'ConnectionError'
        self.assertFalse(funding.record(self.db, failed)['complete'])
        with self.assertRaises(Conflict): self.initialize(current)
        self.assertEqual(self.row('SELECT count(*) AS n FROM protocol_opening_credits')['n'], 0)

    def test_prior_protocol_activity_cannot_be_disguised_by_zero_balance(self):
        self.bind()
        proof = self.capture()
        self.fees(TIG)
        with self.db.transaction() as cursor:
            ledger.post(cursor, 'spent-prior-fees', 'fixture',
                [(benchmarks.OPERATOR_FEES, -TIG), ('external:protocol:TIG', TIG)])
        with self.assertRaises(Conflict): self.initialize(proof)
        self.assertEqual(self.row('SELECT count(*) AS n FROM protocol_opening_credits')['n'], 0)

    def test_prior_reservations_and_alerted_history_prevent_initialization(self):
        self.bind()
        deposits.receive(self.db, replace(transfer(), network=TESTNET))
        self.reserve()
        with self.assertRaises(Conflict): self.initialize(self.capture())
        # Even an otherwise acceptable current balance cannot dismiss a
        # collector's persistent conflict record.
        proof = self.capture()
        with self.db.transaction() as cursor:
            cursor.execute("INSERT INTO funding_alerts(capture_id,details) VALUES (%s,'{}')", (proof,))
        with self.assertRaises(Conflict): self.initialize(self.capture())

    def test_opening_record_cannot_be_edited_deleted_or_truncated(self):
        self.bind()
        self.initialize(self.capture())
        for sql in ("UPDATE protocol_opening_credits SET amount=amount+1", 'DELETE FROM protocol_opening_credits',
                    'TRUNCATE protocol_opening_credits'):
            with self.assertRaises(psycopg2.Error):
                with self.db.transaction() as cursor: cursor.execute(sql)

    def test_setup_command_archives_public_evidence_and_is_safe_to_rerun(self):
        self.bind()
        path = Path(__file__).resolve().parents[2] / 'tools' / 'initialize_testnet_credit_v2.py'
        spec = importlib.util.spec_from_file_location('initialize_testnet_credit_v2', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'POOL_V2_DATABASE_DSN': self.db.dsn}), \
                patch.object(module, 'PublicTigClient') as client, \
                patch.object(module.funding, 'capture', side_effect=lambda *_: starter_capture()), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            args = ['--api-url', starter_credit.API_ORIGIN, '--player-id', CUSTODY,
                    '--actor', 'pilot-operator', '--spool', directory]
            self.assertEqual(module.main(args), 0)
            self.assertEqual(module.main(args), 0)
            self.assertIn('"custody_funds_created": false', output.getvalue())
            self.assertEqual(len(list((Path(directory) / 'recorded').glob('*.json.gz'))), 2)
            self.assertEqual(len(list((Path(directory) / 'pending').glob('*.json.gz'))), 0)
            self.assertEqual(self.row('SELECT count(*) AS n FROM protocol_opening_credits')['n'], 1)
            client.assert_called_with(starter_credit.API_ORIGIN)
