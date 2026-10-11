from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from pool_manager.pool_v2 import benchmarks, compaction, report_observer, retention
from pool_manager.pool_v2.block_observer import BlockStore
from pool_manager.pool_v2.observation import read_archive, write_archive
from pool_manager.pool_v2.protocol import ProtocolDataError
from pool_manager.pool_v2.qualifiers import credit_block
from pool_manager.pool_v2.spool import Spool, pruning_lock
from funds_helpers import DatabaseCase, WALLET
from observer_helpers import observation


class SpoolConcurrencyTests(unittest.TestCase):
    def orphan(self, spool, data):
        path = spool.save(data, {})
        path.unlink()  # Simulate old chunks left behind after their manifest retired.
        for chunk in (spool.directory / 'chunks').glob('*/*.gz'):
            past = datetime.now(timezone.utc).timestamp() - 10
            os.utime(chunk, (past, past))

    def test_existing_writing_pin_protects_reused_chunks_before_manifest_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(directory, paged=True)
            self.orphan(spool, observation(9))
            entered, release = threading.Event(), threading.Event()
            original = spool._publish
            result = []

            def publish(*args):
                entered.set()
                if not release.wait(10): raise AssertionError('fixture writer was never released')
                return original(*args)

            def save():
                try: result.append(spool.save(observation(9), {}))
                except Exception as failure: result.append(failure)

            with patch.object(spool, '_publish', side_effect=publish):
                writer = threading.Thread(target=save)
                writer.start()
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertEqual(len(list((spool.directory / 'writing').glob('*.json.gz'))), 1)
                    with pruning_lock(spool.directory):
                        removed = retention._prune_file_chunks(spool.directory, datetime.now(timezone.utc))
                    self.assertEqual(removed, 0)
                finally:
                    release.set()
                    writer.join(5)
            self.assertFalse(writer.is_alive())
            self.assertIsInstance(result[0], Path)
            self.assertEqual(spool.read(result[0])[0]['start']['block']['id'], 'block-9')

    def test_writer_during_reference_scan_is_protected_after_its_pin_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(directory, paged=True)
            first = spool.save(observation(8), {})
            spool.recorded(first)
            self.orphan(spool, observation(9))
            self.orphan(spool, {'unused': [{'id': 'remove-me'}]})
            original = retention._spool_references
            created = []

            def references(path, manifest):
                if not created:
                    self.assertTrue((spool.directory / '.gc-active').exists())
                    created.append(spool.save(observation(9), {}))
                    self.assertEqual(list((spool.directory / 'writing').glob('*.json.gz')), [])
                return original(path, manifest)

            with pruning_lock(spool.directory), patch.object(retention, '_spool_references', side_effect=references):
                self.assertGreater(retention._prune_file_chunks(spool.directory, datetime.now(timezone.utc)), 0)
            self.assertEqual(spool.read(created[0])[0]['start']['block']['id'], 'block-9')
            self.assertFalse((spool.directory / '.gc-active').exists())
            self.assertFalse((spool.directory / '.gc-pins').exists())

    def test_corrupt_writing_pin_aborts_cleanup_and_removes_the_gc_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(directory, paged=True)
            self.orphan(spool, observation(8))
            write_archive(spool.directory / 'writing' / 'corrupt.json.gz', {'digests': ['not-a-digest']})
            before = set((spool.directory / 'chunks').glob('*/*.gz'))
            with pruning_lock(spool.directory), self.assertRaises(ProtocolDataError):
                retention._prune_file_chunks(spool.directory, datetime.now(timezone.utc))
            self.assertEqual(set((spool.directory / 'chunks').glob('*/*.gz')), before)
            self.assertFalse((spool.directory / '.gc-active').exists())
            self.assertFalse((spool.directory / '.gc-pins').exists())

    def test_corrupt_digest_page_aborts_before_deleting_any_chunk(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(directory, paged=True)
            path = spool.save(observation(8), {})
            saved = read_archive(path)

            def page(node):
                if node.get('pages'): return node['pages'][0]
                for _, child in node.get('dict', []):
                    found = page(child)
                    if found: return found
                return None

            digest = page(saved['manifest'])
            self.assertIsNotNone(digest)
            target = spool.directory / 'chunks' / digest[:2] / (digest + '.gz')
            target.write_bytes(b'corrupt')
            before = set((spool.directory / 'chunks').glob('*/*.gz'))
            with pruning_lock(spool.directory), self.assertRaises(Exception):
                retention._prune_file_chunks(spool.directory, datetime.now(timezone.utc))
            self.assertEqual(set((spool.directory / 'chunks').glob('*/*.gz')), before)
            self.assertFalse((spool.directory / '.gc-active').exists())


class RedundantSpoolTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.store = BlockStore(self.db)
        self.store.initialize(8)
        self.data = observation(8)
        self.data['pool_player_id'] = WALLET
        with patch('pool_manager.pool_v2.compact.compact_observation', side_effect=lambda value: value):
            self.assertTrue(self.store.record(self.data, collector='legacy-fixture')['complete'])

    def proof(self):
        return {'covered_until': datetime.now(timezone.utc).isoformat(), 'fixture': 'verified-test-backup'}

    def prepare_compact_copy(self):
        self.fund()
        row = benchmarks.reserve(self.db, self.member, 'owned', creation_round=1, resource='CPU',
            selection={'block_id': 'block-8'}, payload={'track_settings': {'t': {'num_bundles': 2}}},
            fee_limit=0, offer_expires_at=self.expiry)
        benchmarks.mark_submitting(self.db, row['id'])
        row = benchmarks.accept(self.db, row['id'], 'b', {'benchmark_id': 'b'}, actual_fee=0, evidence={'fixture': True})
        benchmarks.acknowledge(self.db, row['id'], self.member, row['assignment_digest'])
        credit_block(self.db, 'block-8', WALLET)
        with self.db.transaction() as cursor:
            cursor.execute('INSERT INTO reconciled_blocks(block_id,player_id) VALUES (%s,%s)', ('block-8', WALLET))
        compaction.backfill(self.db, player_id=WALLET)

    def test_raw_spool_waits_for_pruned_backed_copy_and_preserves_pending_or_conflicting_files(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(directory, paged=True)
            metadata = {'captured_at': datetime.now(timezone.utc).isoformat()}
            path = spool.save(self.data, metadata); spool.recorded(path)
            pending = spool.save(self.data, metadata)
            different = deepcopy(self.data)
            for anchor in ('start', 'end'): different[anchor]['block']['id'] = 'conflicting-eight'
            other = spool.save(different, metadata); spool.recorded(other)
            self.prepare_compact_copy()
            proof = self.proof()
            self.assertEqual(retention.prune_compacted_spool(self.db, directory, proof=proof)['archives'], 0)
            compaction.prune(self.db, proof=proof)
            result = retention.prune_compacted_spool(self.db, directory, proof=proof, collect_chunks=True)
            self.assertEqual(result['archives'], 1)
            self.assertTrue(pending.exists())
            self.assertEqual(spool.read(pending)[0]['start']['block']['id'], 'block-8')
            self.assertEqual(spool.read(spool.directory / 'recorded' / other.name)[0]['start']['block']['id'], 'conflicting-eight')
            self.assertEqual(self.store.read('block-8')[0]['format'], 'pool-network-v1')

    def test_report_file_cleanup_matches_backed_payloads_and_respects_the_batch_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(directory)
            metadata = {'captured_at': datetime.now(timezone.utc).isoformat()}
            saved = {'kind': 'reports', 'reporting_round': 1, 'start': self.data['start'], 'end': self.data['end'],
                     'payload': {'reports': [], 'arbitrations': []}}
            result = report_observer.record(self.db, saved, metadata)
            self.assertTrue(result['complete'], result)
            recorded = []
            for _ in range(2):
                path = spool.save(saved, metadata); spool.recorded(path); recorded.append(path.name)
            pending = spool.save(saved, metadata)
            other = deepcopy(saved); other['reporting_round'] = 2
            unbacked = spool.save(other, metadata); spool.recorded(unbacked)
            proof = self.proof()
            late = spool.save(saved, {'captured_at': (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()})
            spool.recorded(late)
            self.assertEqual(retention.prune_report_spool(self.db, directory, proof=proof, limit=1)['archives'], 1)
            self.assertEqual(sum((spool.directory / 'recorded' / name).exists() for name in recorded), 1)
            self.assertEqual(retention.prune_report_spool(self.db, directory, proof=proof, collect_chunks=True)['archives'], 1)
            self.assertTrue(pending.exists())
            self.assertEqual(spool.read(pending)[0], saved)
            self.assertTrue((spool.directory / 'recorded' / unbacked.name).exists())
            self.assertTrue((spool.directory / 'recorded' / late.name).exists())
