from copy import deepcopy
from datetime import datetime, timezone
from fractions import Fraction
import gzip
import hashlib
import random
import tempfile
import unittest
from unittest.mock import patch

from psycopg2.extras import Json

from pool_manager.pool_v2 import benchmarks, compaction
from pool_manager.pool_v2.block_observer import BlockStore, _semantic
from pool_manager.pool_v2.compact import FORMAT, compact_observation, metadata_summary
from pool_manager.pool_v2.observation import read_archive
from pool_manager.pool_v2.protocol import ProtocolDataError, equal_bundle_credit, validate_snapshot
from pool_manager.pool_v2.qualifiers import credit_block
from pool_manager.pool_v2.selection import choose, references
from pool_manager.pool_v2.spool import Spool
from funds_helpers import DatabaseCase, WALLET, OTHER
from observer_helpers import observation


def network_observation(height=8):
    data = observation(height)
    data['pool_player_id'] = WALLET
    own = data['players'][WALLET]
    second = deepcopy(own)
    second['precommits'][0]['benchmark_id'] = 'owned-second'
    second['benchmarks'][0]['id'] = 'owned-second'
    second['proofs'][0]['benchmark_id'] = 'owned-second'
    for key in ('precommits', 'benchmarks', 'proofs'):
        own[key].extend(second[key])
    other = deepcopy(second)
    other['precommits'][0]['benchmark_id'] = 'other-benchmark'
    other['precommits'][0]['settings']['player_id'] = OTHER
    other['precommits'][0]['details'].update(num_bundles=3, hyperparameters={'network_best': 123})
    other['benchmarks'][0]['id'] = 'other-benchmark'
    other['benchmarks'][0]['details'].update(num_active_bundles=3, average_quality_by_bundle=[12, 12, 5])
    other['proofs'][0]['benchmark_id'] = 'other-benchmark'
    data['players'][OTHER] = other
    for anchor in ('start', 'end'):
        block = data[anchor]['block']
        block['details']['num_active'].update(benchmark=3, opow=2)
        block['data']['active_ids'].update(benchmark=['b', 'owned-second', 'other-benchmark'], opow=[WALLET, OTHER])
    data['algorithms']['codes'][0]['block_data']['num_qualifiers_by_track_by_player']['t'] = {WALLET: 2, OTHER: 2}
    data['challenges']['challenges'][0]['block_data']['num_qualifiers_by_track']['t'] = 4
    data['challenges']['challenges'][1]['config']['type'] = 'gpu'  # CPU selection exercises the external best reference.
    data['opow']['opow'][0]['block_data']['num_qualifiers_by_challenge_by_track']['c1']['t'] = 2
    data['opow']['opow'].append({'player_id': OTHER, 'block_data': {
        'num_qualifiers_by_challenge_by_track': {'c1': {'t': 2}}}})
    return data


class CompactProjectionTests(unittest.TestCase):
    def assert_equivalent(self, data):
        full = validate_snapshot(data)
        projection = compact_observation(data)
        compact = validate_snapshot(projection)
        owned = {identity for identity, row in full.precommits.items() if row['settings']['player_id'] == data['pool_player_id']}
        expected = {key: value for key, value in equal_bundle_credit(full).items() if key[0] in owned}
        self.assertEqual(equal_bundle_credit(compact), expected)
        self.assertEqual(references(compact), references(full))
        for resource, compute in (('CPU', 'aws_c7a'), ('GPU', 'aws_g4dn')):
            options = dict(player_id=data['pool_player_id'], resource=resource, compute_type=compute,
                           now=full.timestamp)
            self.assertEqual(choose(full, data['algorithms']['binarys'], rng=random.Random(1), **options),
                             choose(compact, projection['algorithms']['binarys'], rng=random.Random(1), **options))
        return projection, compact

    def test_exact_fractional_credit_and_external_best_selection_reference(self):
        data = network_observation()
        before = deepcopy(data)
        projection, snapshot = self.assert_equivalent(data)
        self.assertEqual(data, before)
        self.assertEqual(set(projection['players']), {WALLET})
        self.assertNotIn('data', projection['start']['block'])
        self.assertEqual(projection['start']['block']['details']['num_active']['benchmark'], 3)
        self.assertEqual(set(snapshot.precommits), {'b', 'owned-second'})
        self.assertEqual(set(equal_bundle_credit(snapshot).values()), {Fraction(1, 2)})
        self.assertEqual(snapshot.selection_references['a1', 't']['hyperparameters'], {'network_best': 123})
        self.assertLess(len(str(projection)), len(str(data)))

    def test_inactive_pool_with_complete_pending_feed_keeps_external_selection(self):
        data = observation()
        data['pool_player_id'] = 'new-pool'
        data['pool_pending'] = {'precommits': [], 'benchmarks': [], 'proofs': [], 'frauds': []}
        projection, snapshot = self.assert_equivalent(data)
        self.assertFalse(projection['pool_active'])
        self.assertEqual(snapshot.bundles, ())
        self.assertEqual(projection['players'], {})
        self.assertEqual(projection['pool_pending'], data['pool_pending'])

    def test_retired_benchmarks_and_explicit_default_parameters_replay(self):
        data = network_observation()
        for precommit in data['players'][WALLET]['precommits']:
            precommit['details']['hyperparameters'] = None
        other = data['players'][OTHER]
        other['benchmarks'][0]['details'].update(num_active_bundles=0, average_quality_by_bundle=[])
        for anchor in ('start', 'end'):
            block = data[anchor]['block']
            block['details']['num_active']['benchmark'] = 2
            block['data']['active_ids']['benchmark'] = ['b', 'owned-second']
        data['algorithms']['codes'][0]['block_data']['num_qualifiers_by_track_by_player']['t'][OTHER] = 0
        data['challenges']['challenges'][0]['block_data']['num_qualifiers_by_track']['t'] = 2
        data['opow']['opow'][1]['block_data']['num_qualifiers_by_challenge_by_track']['c1']['t'] = 0
        _, snapshot = self.assert_equivalent(data)
        self.assertIsNone(snapshot.selection_references['a1', 't']['hyperparameters'])

    def test_reordering_pool_records_preserves_canonical_block_identity(self):
        data = network_observation()
        reordered = deepcopy(data)
        for feed in reordered['players'].values():
            for records in feed.values():
                records.reverse()
        for anchor in ('start', 'end'):
            for identities in reordered[anchor]['block']['data']['active_ids'].values():
                identities.reverse()
        first, second = compact_observation(data), compact_observation(reordered)
        self.assertEqual(_semantic(validate_snapshot(first), first), _semantic(validate_snapshot(second), second))
        self.assertEqual(first['source_semantic_digest'], second['source_semantic_digest'])

    def test_missing_pool_feed_and_incomplete_network_are_not_compacted(self):
        data = observation()
        data['pool_player_id'] = 'new-pool'
        with self.assertRaises(ProtocolDataError):
            compact_observation(data)
        data = network_observation()
        del data['players'][OTHER]
        with self.assertRaises(ProtocolDataError):
            compact_observation(data)

    def test_scoped_validation_refuses_wrong_owner_counts_and_references(self):
        original = compact_observation(network_observation())
        corruptions = []
        value = deepcopy(original); value['players'][WALLET]['precommits'][0]['settings']['player_id'] = OTHER; corruptions.append(value)
        value = deepcopy(original); value['pool_active_benchmark_ids'].append('missing'); corruptions.append(value)
        value = deepcopy(original); value['selection_references'] = []; corruptions.append(value)
        value = deepcopy(original); value['selection_references'][0]['bundle']['quality'] = -1; corruptions.append(value)
        value = deepcopy(original); value['algorithms']['codes'][0]['block_data']['num_qualifiers_by_track_by_player']['t'][OTHER] = 1; corruptions.append(value)
        value = deepcopy(original); value['source_semantic_digest'] = 'bad'; corruptions.append(value)
        for index, value in enumerate(corruptions):
            with self.subTest(index=index), self.assertRaises(ProtocolDataError):
                validate_snapshot(value)

    def test_live_fixture_projection_preserves_pool_credit_and_network_references(self):
        from pathlib import Path
        source = next((Path(__file__).parent / 'fixtures').glob('block-1351111*'))
        data = read_archive(source)['observation']
        data['pool_player_id'] = next(iter(data['players']))
        full = validate_snapshot(data)
        projection = compact_observation(data)
        compact = validate_snapshot(projection)
        owned = {row.benchmark_id for row in full.bundles if row.player_id == data['pool_player_id']}
        self.assertEqual(equal_bundle_credit(compact), {key: value for key, value in equal_bundle_credit(full).items() if key[0] in owned})
        self.assertEqual(references(compact), references(full))

    def test_request_metadata_keeps_provenance_without_full_request_lists(self):
        metadata = {'captured_at': 'fixture', 'requests': [{'path': '/get-benchmarks', 'url': 'one'},
                    {'path': '/get-benchmarks', 'url': 'two'}, {'path': '/get-block'}]}
        result = metadata_summary(metadata)
        self.assertNotIn('requests', result)
        self.assertEqual(result['request_count'], 3)
        self.assertEqual(result['endpoints'], {'/get-benchmarks': 2, '/get-block': 1})
        self.assertEqual(len(result['requests_sha256']), 64)
        self.assertEqual(result['captured_at'], 'fixture')


class CompactStorageTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.store = BlockStore(self.db)
        self.store.initialize(8)
        self.fund()
        row = benchmarks.reserve(self.db, self.member, 'owned', creation_round=1, resource='CPU',
            selection={'block_id': 'block-1'}, payload={'track_settings': {'t': {'num_bundles': 2}}},
            fee_limit=0, offer_expires_at=self.expiry)
        benchmarks.mark_submitting(self.db, row['id'])
        row = benchmarks.accept(self.db, row['id'], 'b', {'benchmark_id': 'b'}, actual_fee=0, evidence={'fixture': True})
        benchmarks.acknowledge(self.db, row['id'], self.member, row['assignment_digest'])

    def record(self, height=8, *, legacy=False, bound=True):
        data = observation(height)
        if bound:
            data['pool_player_id'] = WALLET
        if legacy:
            with patch('pool_manager.pool_v2.compact.compact_observation', side_effect=lambda value: value):
                result = self.store.record(data, collector='legacy-fixture')
        else:
            result = self.store.record(data, collector='compact-fixture')
        self.assertTrue(result['complete'], result)
        return data, result

    def ready(self, block_id):
        credit_block(self.db, block_id, WALLET)
        with self.db.transaction() as cursor:
            cursor.execute('INSERT INTO reconciled_blocks(block_id,player_id) VALUES (%s,%s)', (block_id, WALLET))

    def proof(self, when=None):
        return {'covered_until': (when or datetime.now(timezone.utc)).isoformat(), 'fixture': 'verified-test-backup'}

    def test_new_block_and_outage_spool_use_one_compact_blob(self):
        data, result = self.record()
        manifest = self.row('SELECT manifest FROM capture_attempts WHERE id=%s', (result['attempt_id'],))['manifest']
        self.assertEqual(set(manifest), {'compact_ref'})
        self.assertEqual(self.row('SELECT count(*) AS n FROM observation_chunks')['n'], 1)
        replay, _ = self.store.read('block-8')
        self.assertEqual(replay['format'], FORMAT)
        self.assertEqual(credit_block(self.db, 'block-8', WALLET), 1)
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(directory, paged=True)
            path = spool.save(replay, {'fixture': True})
            saved = read_archive(path)
            self.assertEqual(set(saved['manifest']), {'blob'})
            self.assertEqual(saved['metadata']['block_height'], 8)
            self.assertEqual(spool.read(path)[0], replay)
            self.assertEqual(len(list((spool.directory / 'chunks').glob('*/*.gz'))), 1)
            self.assertEqual(list((spool.directory / 'writing').glob('*.json.gz')), [])

    def test_additive_conversion_requires_bound_feed_and_preserves_identity(self):
        _, result = self.record(legacy=True)
        before = self.row("SELECT semantic_digest,attempt_id FROM observed_blocks WHERE id='block-8'")
        raw_manifest = self.row('SELECT manifest FROM capture_attempts WHERE id=%s', (result['attempt_id'],))['manifest']
        self.assertEqual(compaction.backfill(self.db, player_id=WALLET)['converted'], 1)
        self.assertEqual(self.row("SELECT semantic_digest,attempt_id FROM observed_blocks WHERE id='block-8'"), before)
        self.assertEqual(self.row('SELECT manifest FROM capture_attempts WHERE id=%s', (result['attempt_id'],))['manifest'], raw_manifest)
        self.assertEqual(self.store.read('block-8')[0]['format'], FORMAT)
        self.assertEqual(compaction.backfill(self.db, player_id=WALLET)['converted'], 0)
        self.record(9, legacy=True, bound=False)
        held = compaction.backfill(self.db, player_id=WALLET)
        self.assertEqual(held['converted'], 0)
        self.assertEqual(len(held['held']), 1)
        self.assertEqual(held['last_height'], 9)
        self.assertNotIn('format', self.store.read('block-9')[0])

    def test_pruning_waits_for_completed_processing_and_backup_after_conversion(self):
        _, result = self.record(legacy=True)
        old_backup = self.proof()
        self.assertEqual(compaction.backfill(self.db, player_id=WALLET)['converted'], 1)
        self.assertEqual(compaction.prune(self.db, proof=self.proof())['expired_raw_blocks'], 0)
        self.ready('block-8')
        self.assertEqual(compaction.prune(self.db, proof=old_backup)['expired_raw_blocks'], 0)
        before = self.balance()
        self.assertEqual(compaction.prune(self.db, proof=self.proof())['expired_raw_blocks'], 1)
        self.assertIn('expired', self.row('SELECT manifest FROM capture_attempts WHERE id=%s', (result['attempt_id'],))['manifest'])
        self.assertEqual(self.store.read('block-8')[0]['format'], FORMAT)
        self.assertEqual(credit_block(self.db, 'block-8', WALLET), 1)
        self.assertEqual(self.balance(), before)
        self.assertEqual(self.store.status()['contiguous_height'], 8)
        self.assertEqual(compaction.prune(self.db, proof=self.proof())['expired_raw_blocks'], 0)

    def test_chunk_collection_preserves_other_live_captures_and_financial_records(self):
        self.record(legacy=True)
        self.record(9, legacy=True, bound=False)
        compaction.backfill(self.db, player_id=WALLET)
        self.ready('block-8')
        compaction.prune(self.db, proof=self.proof())
        count = self.row('SELECT count(*) AS n FROM observation_chunks')['n']
        result = compaction.collect_unused_chunks(self.db, proof=self.proof())
        self.assertGreater(result['removed_chunks'], 0)
        self.assertLess(self.row('SELECT count(*) AS n FROM observation_chunks')['n'], count)
        self.assertEqual(self.store.read('block-8')[0]['format'], FORMAT)
        self.assertEqual(self.store.read('block-9')[1].height, 9)
        self.assertEqual(self.row('SELECT count(*) AS n FROM benchmark_credits')['n'], 1)

    def test_unused_chunk_deletion_is_bounded_and_audited(self):
        self.record()
        with self.db.transaction() as cursor:
            for index in range(12):
                raw = ('unused-fixture-' + str(index)).encode()
                cursor.execute('INSERT INTO observation_chunks(digest,compressed) VALUES (%s,%s)',
                               (hashlib.sha256(raw).hexdigest(), gzip.compress(raw)))
        first = compaction.collect_unused_chunks(self.db, proof=self.proof(), limit=5)
        self.assertEqual(first['removed_chunks'], 5)
        self.assertTrue(first['more_chunks'])
        audit = self.row('SELECT counts FROM retention_runs WHERE id=%s', (first['run'],))
        self.assertEqual(audit['counts']['removed_chunks'], 5)
        self.assertEqual(compaction.collect_unused_chunks(self.db, proof=self.proof(), limit=5)['removed_chunks'], 5)
        last = compaction.collect_unused_chunks(self.db, proof=self.proof(), limit=5)
        self.assertEqual(last['removed_chunks'], 2)
        self.assertFalse(last['more_chunks'])
        self.assertFalse(last['more_reference_rows'])
        with self.db.transaction() as cursor:
            for index in range(12):
                cursor.execute('INSERT INTO chunk_last_ref(digest,height) VALUES (%s,%s)',
                    (hashlib.sha256(('orphan-reference-' + str(index)).encode()).hexdigest(), 1))
        reference_runs = [compaction.collect_unused_chunks(self.db, proof=self.proof(), limit=5) for _ in range(3)]
        self.assertEqual([item['removed_reference_rows'] for item in reference_runs], [5, 5, 2])
        self.assertEqual([item['more_reference_rows'] for item in reference_runs], [True, True, False])
        self.assertEqual([item['removed_chunks'] for item in reference_runs], [0, 0, 0])
        self.assertEqual(self.store.read('block-8')[0]['format'], FORMAT)

    def test_gap_is_not_waived_by_per_block_conversion_or_pruning(self):
        self.record(legacy=True)
        self.record(10, legacy=True)
        compaction.backfill(self.db, player_id=WALLET)
        for identity in ('block-8', 'block-10'):
            self.ready(identity)
        self.assertEqual(compaction.prune(self.db, proof=self.proof())['expired_raw_blocks'], 2)
        self.assertEqual(self.store.status()['unresolved_missing_heights'], [9])
        self.assertFalse(self.store.round_coverage(3))
        self.assertEqual(self.row('SELECT block_height FROM retention_floors')['block_height'], -1)

    def test_compact_replacement_is_immutable_and_checksum_validated(self):
        self.record(legacy=True)
        compaction.backfill(self.db, player_id=WALLET)
        with self.assertRaises(Exception):
            with self.db.transaction() as cursor:
                cursor.execute("UPDATE compact_block_copies SET payload_sha256=%s", ('0' * 64,))
        with self.assertRaises(Exception):
            with self.db.transaction() as cursor:
                cursor.execute('DELETE FROM compact_block_copies')
        with self.db.transaction() as cursor:
            cursor.execute('ALTER TABLE compact_block_copies DISABLE TRIGGER retention')
            cursor.execute("UPDATE compact_block_copies SET compressed_payload=%s", (gzip.compress(b'corrupt'),))
            cursor.execute('ALTER TABLE compact_block_copies ENABLE TRIGGER retention')
        with self.assertRaises(ProtocolDataError):
            self.store.read('block-8')
