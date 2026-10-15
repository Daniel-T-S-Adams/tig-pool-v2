"""Resolving a same-block capture that only changed format, and not recapturing on restart."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import psycopg2
from psycopg2.extras import Json

from pool_manager.pool_v2 import dashboard, reports
from pool_manager.pool_v2.block_observer import BlockStore, _semantic, canonical_observation
from pool_manager.pool_v2.boundary_conflicts import POLICY, projection_proof
from pool_manager.pool_v2.compact import compact_observation
from pool_manager.pool_v2.money import Conflict
from pool_manager.pool_v2.ordering_conflicts import POLICY as ORDERING_POLICY, resolve
from pool_manager.pool_v2.protocol import ProtocolDataError, validate_snapshot
import test_compact_storage as storage
from test_compact_storage import network_observation
from test_pool_scoped_capture import FakeClient, scoped
from funds_helpers import WALLET


def pair(data=None):
    data = data or network_observation()
    original = compact_observation(deepcopy(data))
    conflicting = canonical_observation(scoped(data, WALLET))
    return original, conflicting, _semantic(validate_snapshot(original), original)


class BoundaryProjectionTests(unittest.TestCase):
    def test_scoped_recapture_of_the_same_block_is_proven_equivalent(self):
        for version in (2, 1):
            data = network_observation()
            original = compact_observation(deepcopy(data), identity_version=version)
            conflicting = canonical_observation(scoped(data, WALLET))
            # The live API also lists inactive algorithms and challenges without block data;
            # the compact projection stores only the block's active records.
            conflicting['algorithms']['codes'].append({'id': 'retired', 'state': {'banned': True, 'round_active': 1},
                                                       'details': {'challenge_id': 'c1', 'name': 'retired'}, 'block_data': None})
            conflicting['challenges']['challenges'].append({'id': 'c9', 'config': {'type': 'cpu', 'active_tracks': {}},
                                                            'state': {'round_active': 1}, 'block_data': None})
            digest = _semantic(validate_snapshot(original), original)
            proof = projection_proof(original, conflicting, digest)
            self.assertEqual(proof['policy'], POLICY)
            self.assertEqual((proof['block_id'], proof['height'], proof['original_semantic_digest']), ('block-8', 8, digest))
            self.assertEqual(proof['compared'], ['block', 'block_config', 'block_details', 'pool_feed', 'pool_precommits',
                                                 'pool_bundles', 'pool_qualifiers', 'pool_credit', 'algorithms', 'binaries',
                                                 'challenges', 'pool_opow', 'pool_active_benchmark_ids', 'scope_count'])
            self.assertTrue(proof['pool_credit_matches'])
            self.assertNotEqual(proof['conflicting_semantic_digest'], digest)
            self.assertEqual(proof['conflicting_scope'], conflicting['scope'])

    def test_changed_credit_inputs_formats_or_identity_are_refused(self):
        original, conflicting, digest = pair()
        cases = {}
        changed = deepcopy(conflicting)
        changed['players'][WALLET]['benchmarks'][0]['details']['average_quality_by_bundle'] = [1, 1]
        cases['quality'] = (original, changed, digest)
        changed = deepcopy(conflicting)
        changed['players'][WALLET]['precommits'][0]['details']['hyperparameters'] = {'tune': 'other'}
        cases['parameters'] = (original, changed, digest)
        changed = deepcopy(conflicting)
        for anchor in ('start', 'end'):
            changed[anchor]['block']['config']['rounds']['blocks_per_round'] = 5
        cases['config'] = (original, changed, digest)
        changed = deepcopy(conflicting)
        changed['algorithms']['binarys'].pop()
        cases['binaries'] = (original, changed, digest)
        changed = deepcopy(conflicting)
        for row in changed['challenges']['challenges']:
            row['state'] = {'round_active': 2}
        cases['challenges'] = (original, changed, digest)
        changed = deepcopy(conflicting)
        changed['scope']['active_benchmark_count'] = 99
        cases['scope'] = (original, changed, digest)
        other_block = canonical_observation(scoped(network_observation(9), WALLET))
        cases['identity'] = (original, other_block, digest)
        cases['digest'] = (original, conflicting, '0' * 64)
        cases['formats'] = (conflicting, original, digest)
        cases['legacy'] = (canonical_observation(network_observation()), conflicting, digest)
        foreign = deepcopy(conflicting)
        foreign['pool_player_id'] = '0x' + 'f' * 40
        cases['pool'] = (original, foreign, digest)
        for name, arguments in cases.items():
            with self.subTest(name=name), self.assertRaises(ProtocolDataError):
                projection_proof(*arguments)


class BoundaryStorageTests(storage.CompactStorageTests):
    def conflict(self):
        data = network_observation()
        first = self.store.record(deepcopy(data), collector='compact-fixture')
        second = self.store.record(scoped(data, WALLET), collector='scoped-fixture')
        self.assertTrue(first['complete'])
        self.assertFalse(second['complete'])
        self.assertEqual(self.store.status()['conflicting_heights'], [8])
        alert = self.row("SELECT id FROM observation_alerts WHERE kind='conflicting-block'")['id']
        return data, first, second, alert

    def resolution(self, alert, second, policy=POLICY):
        return resolve(self.db, alert_id=alert, conflicting_attempt_id=second['attempt_id'],
                       actor='test-operator', reason='Scoped recapture of the recorded block at the format boundary', policy=policy)

    def test_boundary_resolution_is_audited_idempotent_and_keeps_later_conflicts(self):
        data, first, second, alert = self.conflict()
        before = self.row('SELECT * FROM observed_blocks')
        with self.assertRaises(ProtocolDataError):
            self.store.read('block-8')
        with self.assertRaises(ProtocolDataError):
            self.resolution(alert, second, policy=ORDERING_POLICY)
        with self.assertRaises(ValueError):
            self.resolution(alert, second, policy='unknown-policy')
        result = self.resolution(alert, second)
        self.assertEqual(result['policy'], POLICY)
        self.assertEqual(self.resolution(alert, second), result)
        with self.assertRaises(ProtocolDataError):
            self.resolution(alert, second, policy=ORDERING_POLICY)
        self.assertEqual(self.row('SELECT * FROM observed_blocks'), before)
        self.assertEqual(self.row("SELECT count(*) AS n FROM observation_alerts WHERE kind='conflicting-block'")['n'], 1)
        self.assertEqual(self.store.status()['conflicting_heights'], [])
        self.assertTrue(next(row for row in dashboard.operator(self.db)['alerts'] if row['kind'] == 'conflicting-block')['resolved'])
        with self.db.transaction() as cursor:
            reports.require_block(cursor, 'block-8')
        replay, snapshot = self.store.read('block-8')
        self.assertEqual(replay['format'], 'pool-network-v1')
        self.assertFalse(snapshot.scoped)
        for statement in ("UPDATE observation_conflict_resolutions SET reason='changed'",
                          'DELETE FROM observation_conflict_resolutions', 'TRUNCATE observation_conflict_resolutions'):
            with self.assertRaises(psycopg2.Error), self.db.transaction() as cursor:
                cursor.execute(statement)
        changed = scoped(data, WALLET)
        changed['players'][WALLET]['benchmarks'][0]['details']['average_quality_by_bundle'] = [1, 1]
        self.assertFalse(self.store.record(changed, collector='genuine-change')['complete'])
        self.assertEqual(self.store.status()['conflicting_heights'], [8])
        with self.assertRaises(ProtocolDataError):
            self.store.read('block-8')
        with self.assertRaises(Conflict), self.db.transaction() as cursor:
            reports.require_block(cursor, 'block-8')

    def test_wrong_capture_and_tampered_proof_are_rejected(self):
        _, first, second, alert = self.conflict()
        with self.assertRaises(ProtocolDataError):
            self.resolution(alert, first)
        with self.assertRaises(ValueError):
            resolve(self.db, alert_id=alert, conflicting_attempt_id=second['attempt_id'], actor='', reason='test', policy=POLICY)
        self.assertEqual(self.row('SELECT count(*) AS n FROM observation_conflict_resolutions')['n'], 0)
        block = self.row("SELECT id,attempt_id,semantic_digest FROM observed_blocks WHERE height=8")
        proof = {'policy': POLICY, 'block_id': block['id'], 'height': 8, 'original_format': 'pool-network-v1',
                 'conflicting_format': 'pool-scoped-v2', 'original_semantic_digest': block['semantic_digest'],
                 'conflicting_semantic_digest': '1' * 64, 'original_payload_sha256': '2' * 64,
                 'conflicting_payload_sha256': '3' * 64, 'compared': ['block'], 'pool_credit_matches': True}
        for tamper in ({'pool_credit_matches': False}, {'original_semantic_digest': '0' * 64}, {'height': 9},
                       {'conflicting_format': 'pool-network-v1'}, {'compared': []}, {'policy': ORDERING_POLICY}):
            with self.subTest(tamper=tamper), self.assertRaises(psycopg2.Error), self.db.transaction() as cursor:
                cursor.execute('''INSERT INTO observation_conflict_resolutions
                    (alert_id,block_id,original_attempt_id,conflicting_attempt_id,policy,actor,reason,proof)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''', (alert, block['id'], block['attempt_id'], second['attempt_id'],
                    tamper.get('policy', POLICY), 'direct', 'direct', Json({**proof, **tamper})))

    def test_restarted_collector_resumes_after_the_recorded_head(self):
        from tools.observe_tig_v2 import main
        first, second = network_observation(), network_observation(9)
        self.assertTrue(self.store.record(deepcopy(first), collector='compact-fixture')['complete'])
        polls = []

        class Client(FakeClient):
            def __init__(self, url):
                super().__init__(first)

            def get(self, path, params=None):
                if path == '/get-block':
                    polls.append(path)
                    # The first poll still sees the recorded head; later polls see the next block.
                    self.data = first if len(polls) == 1 else second
                return super().get(path, params)
        with tempfile.TemporaryDirectory() as directory, patch('tools.observe_tig_v2.PublicTigClient', Client), \
                patch('tools.observe_tig_v2.signal.signal'), patch.dict('os.environ', {'POOL_V2_DATABASE_DSN': self.db.dsn}):
            self.assertEqual(main(['--collector', 'restart', '--spool', directory, '--launch-height', '8', '--captures', '1',
                                   '--poll-seconds', '0.01', '--pool-player-id', WALLET]), 0)
            self.assertEqual(len(list((Path(directory) / 'chunks').glob('*/*.gz'))), 1)
        with self.db.transaction() as cursor:
            cursor.execute('SELECT height FROM capture_attempts ORDER BY created_at')
            self.assertEqual([row['height'] for row in cursor.fetchall()], [8, 9])
        self.assertEqual(self.store.status()['conflicting_heights'], [])
        self.assertTrue(self.store.read('block-9')[1].scoped)


if __name__ == '__main__':
    unittest.main()
