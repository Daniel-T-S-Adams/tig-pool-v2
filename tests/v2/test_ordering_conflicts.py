from copy import deepcopy
import unittest
from unittest.mock import patch

import psycopg2

from pool_manager.pool_v2 import compaction
from pool_manager.pool_v2.block_observer import _semantic, canonical_observation
from pool_manager.pool_v2.compact import compact_observation
from pool_manager.pool_v2.ordering_conflicts import ordering_proof, resolve
from pool_manager.pool_v2.protocol import ProtocolDataError, validate_snapshot
import test_compact_storage as storage
from test_compact_storage import network_observation
from observer_helpers import observation


def scheduled(data):
    for anchor in ('start', 'end'):
        data[anchor]['block']['config']['rewards'] = {'schedule': [
            {'round_start': 1, 'reward': '100'}, {'round_start': 27, 'reward': '50'},
            {'round_start': 79, 'reward': '25'}]}
    return data


class OrderingProjectionTests(unittest.TestCase):
    def test_header_record_reordering_has_stable_identity_and_original_header(self):
        data = scheduled(network_observation())
        other = deepcopy(data)
        for anchor in ('start', 'end'):
            other[anchor]['block']['config']['rewards']['schedule'].reverse()
        other['algorithms']['binarys'].reverse()
        first, second = compact_observation(data), compact_observation(other)
        self.assertEqual(first['start']['block'], {key: data['start']['block'][key] for key in ('id', 'details', 'config')})
        self.assertEqual(first['source_semantic_digest'], second['source_semantic_digest'])
        self.assertEqual(_semantic(validate_snapshot(first), first), _semantic(validate_snapshot(second), second))
        self.assertIn(_semantic(validate_snapshot(compact_observation(data, identity_version=1)), compact_observation(data, identity_version=1)), first['compatible_projection_digests'])
        changed = deepcopy(data)
        for anchor in ('start', 'end'):
            changed[anchor]['block']['config']['rewards']['schedule'][0]['reward'] = '101'
        self.assertNotEqual(compact_observation(changed)['source_semantic_digest'], first['source_semantic_digest'])

    def test_full_network_replay_rejects_changed_or_missing_records(self):
        raw = scheduled(network_observation())
        legacy = canonical_observation(raw)
        compact = compact_observation(raw, identity_version=1)
        digest = _semantic(validate_snapshot(legacy), legacy)
        self.assertTrue(ordering_proof(legacy, compact, digest)['complete_network_replay_matches'])
        for path in ('reward', 'binary', 'reference', 'source'):
            other = deepcopy(compact)
            if path == 'reward':
                for anchor in ('start', 'end'):
                    other[anchor]['block']['config']['rewards']['schedule'][0]['reward'] = '101'
            elif path == 'binary':
                other['algorithms']['binarys'].pop()
            elif path == 'reference':
                other['selection_references'][0]['hyperparameters'] = {'changed': True}
            else:
                other['source_semantic_digests'] = ['0' * 64]
            with self.subTest(path=path), self.assertRaises(ProtocolDataError):
                ordering_proof(legacy, other, digest)


class OrderingStorageTests(storage.CompactStorageTests):
    def conflict(self):
        raw = scheduled(observation())
        raw['pool_player_id'] = storage.WALLET
        with patch('pool_manager.pool_v2.compact.compact_observation', side_effect=lambda value: value):
            first = self.store.record(raw, collector='legacy-fixture')
        with patch('pool_manager.pool_v2.compact.compact_observation', side_effect=lambda value: compact_observation(value, identity_version=1)):
            second = self.store.record(raw, collector='transition-fixture')
        self.assertTrue(first['complete'])
        self.assertFalse(second['complete'])
        alert = self.row("SELECT id FROM observation_alerts WHERE kind='conflicting-block'")['id']
        return raw, first, second, alert

    def resolution(self, alert, second):
        return resolve(self.db, alert_id=alert, conflicting_attempt_id=second['attempt_id'],
                       actor='test-operator', reason='Verified transport list ordering only')

    def test_resolution_is_audited_idempotent_and_new_conflicts_still_block(self):
        raw, first, second, alert = self.conflict()
        before = self.row('SELECT * FROM observed_blocks')
        with self.assertRaises(ProtocolDataError):
            self.store.read('block-8')
        old_backup = self.proof()
        result = self.resolution(alert, second)
        self.assertEqual(self.resolution(alert, second), result)
        self.assertEqual(self.row('SELECT * FROM observed_blocks'), before)
        self.assertEqual(self.row("SELECT count(*) AS n FROM observation_alerts WHERE kind='conflicting-block'")['n'], 1)
        self.assertEqual(self.store.status()['conflicting_heights'], [])
        self.assertEqual(compaction.backfill(self.db, player_id=raw['pool_player_id'])['converted'], 1)
        self.ready('block-8')
        self.assertEqual(compaction.prune(self.db, proof=old_backup)['expired_raw_blocks'], 0)
        self.assertEqual(compaction.prune(self.db, proof=self.proof())['expired_raw_blocks'], 1)
        self.store.read('block-8')
        for statement in ('UPDATE observation_conflict_resolutions SET reason=\'changed\'',
                          'DELETE FROM observation_conflict_resolutions', 'TRUNCATE observation_conflict_resolutions'):
            with self.assertRaises(psycopg2.Error), self.db.transaction() as cursor:
                cursor.execute(statement)
        for anchor in ('start', 'end'):
            raw[anchor]['block']['config']['rewards']['schedule'][0]['reward'] = '999'
        self.assertFalse(self.store.record(raw, collector='genuine-change')['complete'])
        self.assertEqual(self.store.status()['conflicting_heights'], [8])
        with self.assertRaises(ProtocolDataError):
            self.store.read('block-8')

    def test_wrong_capture_and_missing_operator_are_rejected(self):
        _, first, second, alert = self.conflict()
        with self.assertRaises(ProtocolDataError):
            self.resolution(alert, first)
        with self.assertRaises(ValueError):
            resolve(self.db, alert_id=alert, conflicting_attempt_id=second['attempt_id'], actor='', reason='test')
        self.assertEqual(self.row('SELECT count(*) AS n FROM observation_conflict_resolutions')['n'], 0)

    def test_upgrade_recapture_keeps_legacy_and_v1_compact_blocks_usable(self):
        raw = scheduled(network_observation())
        with patch('pool_manager.pool_v2.compact.compact_observation', side_effect=lambda value: compact_observation(value, identity_version=1)):
            self.assertTrue(self.store.record(raw, collector='v1')['complete'])
        self.assertTrue(self.store.record(raw, collector='v2')['complete'])
        for anchor in ('start', 'end'):
            raw[anchor]['block']['config']['rewards']['schedule'].reverse()
        self.assertTrue(self.store.record(raw, collector='v2-reordered')['complete'])
