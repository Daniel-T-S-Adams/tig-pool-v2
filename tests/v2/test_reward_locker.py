from copy import deepcopy
from dataclasses import replace
import gzip
import hashlib
import json
from pathlib import Path
import unittest

from pool_manager.pool_v2 import reward_locker
from pool_manager.pool_v2.chain import Chain, Network
from pool_manager.pool_v2.money import FundsError
from funds_helpers import CUSTODY, OTHER, TOKEN, chain_fixture


LOCKER = '0x' + '9' * 40
CODE = '0x1234567890'
PIN = hashlib.sha256(bytes.fromhex(CODE[2:])).hexdigest()


def fixture(kind='claimed', values=(100, 120)):
    data, basic, tx_hash = chain_fixture()
    data['code'] = CODE
    data['finalized'] = {**data['header'], 'number': '0x70'}
    topic = next(key for key, item in reward_locker.TOPICS.items() if item[0] == kind)
    data['receipt']['logs'][0].update(address=LOCKER,
        topics=[topic, '0x' + '0' * 24 + CUSTODY[2:]],
        data='0x' + ''.join(f'{value:064x}' for value in values))
    values = {'token()': int(TOKEN, 16), 'claimable(address)': 100,
        'locked(address)': 120, 'getNumPendingWithdrawals(address)': 2,
        'pendingPeriod()': 28*86400}
    data['values'] = {reward_locker.selector(key): value for key, value in values.items()}
    now = int(data['header']['timestamp'], 16)
    data['pending'] = [(10, now-1), (20, now+1)]
    def rpc(method, params):
        if method == 'eth_getCode': return data['code']
        if method == 'eth_call' and params[0]['to'] == LOCKER:
            call = params[0]['data']
            if call[:10] == reward_locker.selector('pendingWithdrawals(address,uint256)'):
                index = int(call[-64:], 16)
                return '0x' + ''.join(f'{value:064x}' for value in data['pending'][index])
            return f"0x{data['values'][call[:10]]:064x}"
        return basic.rpc(method, params)
    return data, Chain(basic.network, rpc), tx_hash


class RewardLockerTests(unittest.TestCase):
    def test_snapshot_keeps_claimable_locked_and_pending_separate(self):
        _, chain, _ = fixture()
        result = reward_locker.snapshot(chain, LOCKER, PIN)
        self.assertEqual((result['claimable'], result['locked']), ('100', '120'))
        self.assertEqual(result['pending_period_seconds'], 28*86400)
        self.assertEqual([row['ready'] for row in result['pending_withdrawals']], [True, False])
        self.assertTrue(result['custody_receipt_required'])
        self.assertTrue(result['round_attribution_required'])
        self.assertNotIn('available', result)

    def test_claim_event_is_not_a_transfer_or_automatic_round_credit(self):
        _, chain, tx_hash = fixture()
        result = reward_locker.events(chain, LOCKER, PIN, tx_hash)
        self.assertEqual(result['events'][0]['kind'], 'claimed')
        self.assertEqual(result['events'][0]['locked'], '120')
        comparison = reward_locker.compare_distribution(132, {'players': {CUSTODY: {'track': '100'}}}, result)
        self.assertFalse(comparison['all_player_amounts_match'])
        self.assertFalse(comparison['custody_receipt_proven'])

    def test_all_lifecycle_signatures_decode_exact_integer_amounts(self):
        for kind, fields in reward_locker.EVENTS.values():
            with self.subTest(kind=kind):
                _, chain, tx_hash = fixture(kind, tuple(100+i for i in range(len(fields))))
                event = reward_locker.events(chain, LOCKER, PIN, tx_hash)['events'][0]
                self.assertEqual(event['kind'], kind)
                self.assertEqual(event['amount'], '100')
                self.assertEqual(event['user'], CUSTODY)

    def test_wrong_contract_pin_token_or_network_is_rejected(self):
        for mutation in (
            lambda data: data.update(code='0x'),
            lambda data: data.update(code='0x1234'),
            lambda data: data['values'].update({reward_locker.selector('token()'): int(OTHER, 16)}),
            lambda data: data.update(chain_id='0x1'),
        ):
            data, chain, tx_hash = fixture(); mutation(data)
            with self.subTest(mutation=mutation), self.assertRaises(FundsError):
                reward_locker.events(chain, LOCKER, PIN, tx_hash)
        _, chain, _ = fixture()
        with self.assertRaises(FundsError): reward_locker.snapshot(chain, LOCKER, 'latest')

    def test_unfinalized_reverted_or_noncanonical_transactions_are_rejected(self):
        for mutation in (
            lambda data: data['receipt'].update(status='0x0'),
            lambda data: data['finalized'].update(number='0x63'),
            lambda data: data['header'].update(hash='0x'+'b'*64),
        ):
            data, chain, tx_hash = fixture(); mutation(data)
            with self.subTest(mutation=mutation), self.assertRaises(FundsError):
                reward_locker.events(chain, LOCKER, PIN, tx_hash)
        _, chain, tx_hash = fixture()
        chain = Chain(replace(chain.network, require_finalized=False), chain.rpc)
        with self.assertRaises(FundsError): reward_locker.events(chain, LOCKER, PIN, tx_hash)
        with self.assertRaises(FundsError): reward_locker.snapshot(chain, LOCKER, PIN)

    def test_duplicate_mismatched_and_malformed_events_are_rejected(self):
        for mutation in (
            lambda logs: logs.append(deepcopy(logs[0])),
            lambda logs: logs[0].update(removed=True),
            lambda logs: logs[0].update(transactionHash='0x'+'b'*64),
            lambda logs: logs[0].update(blockHash='0x'+'b'*64),
            lambda logs: logs[0].update(blockNumber='0x63'),
            lambda logs: logs[0].update(data='0x01'),
            lambda logs: logs[0]['topics'].append('0x'+'0'*64),
            lambda logs: logs[0]['topics'].__setitem__(1, '0x'+'f'*64),
        ):
            data, chain, tx_hash = fixture(); mutation(data['receipt']['logs'])
            with self.subTest(mutation=mutation), self.assertRaises(FundsError):
                reward_locker.events(chain, LOCKER, PIN, tx_hash)

    def test_pending_withdrawals_are_bounded_and_read_at_one_finalized_height(self):
        data, chain, _ = fixture()
        data['values'][reward_locker.selector('getNumPendingWithdrawals(address)')] = 257
        with self.assertRaisesRegex(FundsError, 'too many pending'):
            reward_locker.snapshot(chain, LOCKER, PIN)
        data['values'][reward_locker.selector('getNumPendingWithdrawals(address)')] = 0
        calls = []
        def rpc(method, params):
            calls.append((method, params)); return chain.rpc(method, params)
        result = reward_locker.snapshot(Chain(chain.network, rpc), LOCKER, PIN)
        self.assertEqual(result['pending_withdrawals'], [])
        for method, params in calls:
            if method == 'eth_getCode' or (method == 'eth_call' and params[0]['to'] == LOCKER):
                self.assertEqual(params[-1], hex(result['block_number']))

    def test_distribution_comparison_requires_exact_nonempty_integer_inputs(self):
        _, chain, tx_hash = fixture('rewarded')
        events = reward_locker.events(chain, LOCKER, PIN, tx_hash)
        for players in ({}, {CUSTODY: {'track': 100}}, {CUSTODY: {'track': '1.0'}},
                        {CUSTODY: {'track': '-1'}}, {CUSTODY: {'track': '0'}}):
            with self.subTest(players=players), self.assertRaises(FundsError):
                reward_locker.compare_distribution(132, {'players': players}, events)
        result = reward_locker.compare_distribution(132, {'players': {CUSTODY: {'track': '100'}}}, events)
        self.assertTrue(result['all_player_amounts_match'])
        self.assertTrue(result['round_attribution_requires_review'])
        self.assertFalse(result['custody_receipt_proven'])

    def test_recorded_mainnet_batch_matches_all_players_without_assuming_receipt_or_round_finality(self):
        path = Path(__file__).parent/'fixtures/mainnet-reward-distribution.json.gz'
        data = json.loads(gzip.decompress(path.read_bytes()))
        calls = iter(data['calls'])
        def rpc(method, params):
            entry = next(calls)
            self.assertEqual((method, params), (entry['method'], entry['params']))
            return entry['result']
        chain = Chain(Network(**data['network']), rpc)
        snapshot = reward_locker.snapshot(chain, data['locker'], data['code_sha256'])
        events = reward_locker.events(chain, data['locker'], data['code_sha256'], data['tx_hash'])
        self.assertIsNone(next(calls, None))
        self.assertEqual(snapshot['pending_period_seconds'], 28*86400)
        self.assertEqual(events['block_number'], 51476049)
        result = reward_locker.compare_distribution(data['round'], data['emissions']['payload'], events)
        self.assertEqual(result['expected_players'], 338)
        self.assertEqual(len(events['events']), 339)
        self.assertTrue(result['all_player_amounts_match'])
        self.assertEqual(sum(map(int, result['unlisted_allocations'].values())),
                         int(data['emissions']['payload']['totals']['bootstrap']))
        self.assertNotIn('penalty', data['emissions']['payload']['totals'])
        self.assertTrue(result['round_attribution_requires_review'])
        self.assertFalse(result['custody_receipt_proven'])
