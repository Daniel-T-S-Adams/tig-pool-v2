"""Actual spool and tar snapshots, without SSH credentials or production data."""
from datetime import timedelta
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import subprocess
import os
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
import mainnet_pending_evidence_v2 as pending
from pool_manager.pool_v2.block_observer import canonical_observation
from pool_manager.pool_v2.spool import Spool
from pool_manager.pool_v2 import ledger, report_observer
from pool_manager.pool_v2.block_observer import BlockStore
from funds_helpers import DatabaseCase
from observer_helpers import observation


def barrier(boundary='0/10'):
    return {'system_identifier': '123', 'source_lsn': boundary,
        'recovery_flush_lsn': boundary, 'recovery_acknowledged': True}


class PendingEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='pending-evidence-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.spools = {kind: Spool(self.source / kind, paged=kind == 'blocks') for kind in pending.KINDS}

    def export(self, **kwargs):
        stream = io.BytesIO()
        record = pending.export_snapshot(self.source, stream, lambda: barrier(), **kwargs)
        stream.seek(0)
        return record, stream

    def stage(self, archive_root, stream):
        archive_root.mkdir(exist_ok=True)
        target = archive_root / ('.receiving-' + pending.uuid.uuid4().hex)
        target.mkdir()
        pending.extract_snapshot(stream, target)
        return target

    def test_roundtrip_copies_all_four_pending_kinds_and_only_referenced_chunks(self):
        inputs = {}
        for kind, spool in self.spools.items():
            data = {'kind': kind, 'records': [{'value': 1}, {'value': 2}],
                'data': {'active_ids': {'players': ['player-b', 'player-a']}}}
            old = spool.save({'records': [{'unused': kind}]}, {})
            spool.recorded(old)
            spool.save(data, {'captured_at': pending.now().isoformat()})
            inputs[kind] = canonical_observation(data) if kind == 'blocks' else data
        record, stream = self.export()
        target = self.stage(self.root / 'archive', stream)
        restored = self.root / 'restored'
        pending.restore_snapshot(target, restored)
        self.assertEqual(record['manifests'], dict.fromkeys(pending.KINDS, 1))
        for kind in pending.KINDS:
            spool = Spool(restored / kind)
            self.assertEqual(spool.read(spool.pending()[0])[0], inputs[kind])
            self.assertEqual(list((restored / kind / 'recorded').iterdir()), [])
            self.assertLess(len(list((restored / kind / 'chunks').glob('*/*.gz'))),
                len(list((self.source / kind / 'chunks').glob('*/*.gz'))))

    def test_compact_blob_and_empty_spools_roundtrip(self):
        data = {'format': 'pool-network-v1', 'start': {'block': {'id': 'block-1', 'details': {'height': 1}}},
            'end': {'block': {'id': 'block-1'}}, 'pool_player_id': 'member'}
        self.spools['blocks'].save(data, {})
        record, stream = self.export()
        target = self.stage(self.root / 'archive', stream)
        restored = self.root / 'restored'
        pending.restore_snapshot(target, restored)
        spool = Spool(restored / 'blocks')
        self.assertEqual(spool.read(spool.pending()[0])[0], data)
        self.assertEqual(record['manifests'], {'blocks': 1, 'reports': 0, 'custody': 0, 'funding': 0})

    def test_pending_to_recorded_rename_after_selection_keeps_complete_snapshot(self):
        spool = self.spools['reports']
        data = {'reports': [{'benchmark_id': 'benchmark-a', 'upheld': False}]}
        path = spool.save(data, {})
        _, stream = self.export(selected_hook=lambda: spool.recorded(path))
        target = self.stage(self.root / 'archive', stream)
        restored = self.root / 'restored'
        pending.restore_snapshot(target, restored)
        spool = Spool(restored / 'reports')
        self.assertEqual(spool.read(spool.pending()[0])[0], data)

    def test_missing_required_spool_is_an_error(self):
        (self.source / 'funding' / 'pending').rmdir()
        with self.assertRaisesRegex(ValueError, 'required protocol spool'):
            self.export()

    def test_corrupt_or_missing_chunk_stops_export(self):
        self.spools['reports'].save({'records': [{'value': 'wanted'}]}, {})
        path = next((self.source / 'reports' / 'chunks').glob('*/*.gz'))
        path.write_bytes(gzip.compress(b'changed'))
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.export()
        path.unlink()
        with self.assertRaises(ValueError):
            self.export()

    def test_export_refuses_total_byte_limit_and_invalid_wal_barrier(self):
        self.spools['reports'].save({'value': 'test'}, {})
        with self.assertRaisesRegex(ValueError, 'transfer limit'):
            self.export(maximum=1)
        for record in (None, {**barrier(), 'recovery_acknowledged': False},
                {**barrier(), 'recovery_flush_lsn': '0/F'}):
            with self.subTest(record=record), self.assertRaises(ValueError):
                pending.export_snapshot(self.source, io.BytesIO(), lambda: record)

    def test_symlink_manifest_or_chunk_directory_stops_export(self):
        spool = self.spools['reports']
        path = spool.save({'value': 'test'}, {})
        original = self.root / 'original'
        path.rename(original)
        path.symlink_to(original)
        with self.assertRaisesRegex(ValueError, 'unsafe evidence'):
            self.export()

    def test_unsafe_duplicate_oversized_and_link_members_are_refused(self):
        for names, kind, size in ((['../escape'], 'file', 1),
                (['snapshot.json', 'snapshot.json'], 'file', 1),
                (['snapshot.json'], 'link', 0),
                (['snapshot.json'], 'file', pending.MAX_FILE + 1)):
            with self.subTest(names=names, kind=kind, size=size):
                stream = io.BytesIO()
                with tarfile.open(fileobj=stream, mode='w') as archive:
                    for name in names:
                        item = tarfile.TarInfo(name)
                        if kind == 'link':
                            item.type, item.linkname = tarfile.SYMTYPE, '/etc/passwd'
                        elif size > pending.MAX_FILE:
                            item.size = size
                            archive.addfile(item)
                            break
                        else:
                            item.size = size
                            archive.addfile(item, io.BytesIO(b'x' * size))
                            continue
                        archive.addfile(item)
                stream.seek(0)
                target = self.root / ('unsafe-' + pending.uuid.uuid4().hex)
                target.mkdir()
                with self.assertRaises(ValueError):
                    pending.extract_snapshot(stream, target)

    def test_manifest_checksum_or_file_set_changes_are_refused(self):
        _, stream = self.export()
        target = self.stage(self.root / 'archive', stream)
        (target / 'extra').write_text('unlisted')
        with self.assertRaisesRegex(ValueError, 'file set'):
            pending.verify_snapshot(target)

    def test_chunk_corruption_and_missing_references_are_refused_after_transfer(self):
        self.spools['reports'].save({'reports': [{'benchmark_id': 'b'}]}, {})
        _, stream = self.export()
        target = self.stage(self.root / 'archive', stream)
        path = next((target / 'reports' / 'chunks').glob('*/*.gz'))
        path.write_bytes(gzip.compress(b'changed'))
        index_path = target / 'snapshot.json'
        index = json.loads(index_path.read_text())
        entry = index['files'][str(path.relative_to(target))]
        entry.update(bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        index['content_bytes'] = sum(row['bytes'] for row in index['files'].values())
        index_path.write_text(json.dumps(index))
        with self.assertRaisesRegex(ValueError, 'chunk checksum'):
            pending.verify_snapshot(target)

    def test_publish_retains_two_and_does_not_touch_primary_or_unrelated_files(self):
        archive = self.root / 'archive'
        self.spools['custody'].save({'transfers': [{'amount': '1'}]}, {})
        published = []
        for _ in range(3):
            record, stream = self.export()
            target = self.stage(archive, stream)
            status = pending.publish_snapshot(archive, target, system_identifier='123')
            published.append(record['snapshot'])
            self.assertTrue(status['all_content_digests_verified'])
        self.assertEqual(sorted(p.name for p in (archive / 'snapshots').iterdir()), sorted(published[1:]))
        self.assertEqual(len(self.spools['custody'].pending()), 1)
        self.assertEqual(json.loads((archive / 'status.json').read_text())['retained_snapshots'], 2)

    def test_failed_replacement_preserves_prior_snapshot_and_status(self):
        archive = self.root / 'archive'
        _, stream = self.export()
        target = self.stage(archive, stream)
        pending.publish_snapshot(archive, target, system_identifier='123')
        status_before = (archive / 'status.json').read_bytes()
        _, stream = self.export()
        target = self.stage(archive, stream)
        for identifier in ('different', '456'):
            with self.assertRaises(ValueError):
                pending.publish_snapshot(archive, target, system_identifier=identifier)
        index_path = target / 'snapshot.json'
        index = json.loads(index_path.read_text())
        index['source_captured_at_utc'] = (pending.now() - timedelta(minutes=11)).isoformat()
        index_path.write_text(json.dumps(index))
        with self.assertRaisesRegex(ValueError, 'stale'):
            pending.publish_snapshot(archive, target, system_identifier='123')
        self.assertEqual((archive / 'status.json').read_bytes(), status_before)
        self.assertEqual(len(list((archive / 'snapshots').iterdir())), 1)

    def test_restore_refuses_existing_target(self):
        _, stream = self.export()
        target = self.stage(self.root / 'archive', stream)
        with self.assertRaises(FileExistsError):
            pending.restore_snapshot(target, self.source)

    def test_low_disk_reserve_does_not_start_ssh(self):
        archive = self.root / 'archive'
        config = {'directory': str(archive), 'minimum_free_gib': 35}
        with patch.object(pending.shutil, 'disk_usage', return_value=pending.shutil._ntuple_diskusage(100, 99, 1)), \
                patch.object(pending.subprocess, 'Popen') as ssh:
            with self.assertRaisesRegex(RuntimeError, 'disk reserve'):
                pending.receive(config)
            ssh.assert_not_called()

    def test_receive_uses_strict_ssh_and_publishes_only_after_success(self):
        _, stream = self.export()
        tar_path = self.root / 'export.tar'
        tar_path.write_bytes(stream.getvalue())
        base = self.root / 'base.json'
        base.write_text(json.dumps({'system_identifier': '123',
            'data_checksums_verified': True, 'wal_parse_verified': True}))
        archive = self.root / 'archive'
        config = {'directory': str(archive), 'minimum_free_gib': 35, 'base_record': str(base),
            'ssh_key': '/private/key', 'known_hosts': '/private/known-hosts', 'source_host': '198.51.100.10'}
        real_popen = pending.subprocess.Popen
        calls = []
        def fixture_ssh(args, **kwargs):
            calls.append(args)
            return real_popen(['/bin/cat', str(tar_path)], **kwargs)
        with patch.object(pending.shutil, 'disk_usage', return_value=pending.shutil._ntuple_diskusage(100*1024**3, 0, 100*1024**3)), \
                patch.object(pending.subprocess, 'Popen', side_effect=fixture_ssh):
            status = pending.receive(config)
        self.assertTrue(status['all_content_digests_verified'])
        self.assertIn('StrictHostKeyChecking=yes', calls[0])
        self.assertIn('ForwardAgent=no', calls[0])
        self.assertEqual(calls[0][-1], 'export-pending-v1')
        self.assertEqual(list(archive.glob('.receiving-*')), [])

    def test_filesystem_allocation_allowance_is_reserved_before_starting_ssh(self):
        archive = self.root / 'archive'
        config = {'directory': str(archive), 'minimum_free_gib': 35}
        free = 35 * 1024**3 + pending.MAX_TOTAL + 1
        with patch.object(pending.shutil, 'disk_usage', return_value=pending.shutil._ntuple_diskusage(free, 0, free)), \
                patch.object(pending.subprocess, 'Popen') as ssh:
            with self.assertRaisesRegex(RuntimeError, 'disk reserve'):
                pending.receive(config)
            ssh.assert_not_called()

    def test_sender_failure_preserves_published_snapshot_and_cleans_owned_staging(self):
        archive = self.root / 'archive'
        _, stream = self.export()
        target = self.stage(archive, stream)
        pending.publish_snapshot(archive, target, system_identifier='123')
        before = (archive / 'status.json').read_bytes()
        _, stream = self.export()
        tar_path = self.root / 'export.tar'
        tar_path.write_bytes(stream.getvalue())
        base = self.root / 'base.json'
        base.write_text(json.dumps({'system_identifier': '123',
            'data_checksums_verified': True, 'wal_parse_verified': True}))
        config = {'directory': str(archive), 'minimum_free_gib': 35, 'base_record': str(base),
            'ssh_key': '/private/key', 'known_hosts': '/private/known-hosts', 'source_host': '198.51.100.10'}
        real_popen = pending.subprocess.Popen
        def fixture_ssh(args, **kwargs):
            return real_popen([sys.executable, '-c',
                'import pathlib,sys;sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes());sys.exit(8)',
                str(tar_path)], **kwargs)
        with patch.object(pending.shutil, 'disk_usage', return_value=pending.shutil._ntuple_diskusage(100*1024**3, 0, 100*1024**3)), \
                patch.object(pending.subprocess, 'Popen', side_effect=fixture_ssh):
            with self.assertRaisesRegex(RuntimeError, 'export failed'):
                pending.receive(config)
        self.assertEqual((archive / 'status.json').read_bytes(), before)
        self.assertEqual(list(archive.glob('.receiving-*')), [])

    def test_invalid_manifest_node_and_digest_pages_fail_closed(self):
        for node in ({'unexpected': []}, {'records': ['../../escape']},
                {'dict': [['x', {'value': 1}], ['x', {'value': 2}]]},
                {'pages': ['a' * 64]}, {'items': ['a' * 64]}):
            with self.subTest(node=node), self.assertRaises(ValueError):
                pending.references(node, lambda digest: b'bad')

    def test_root_exporter_preserves_collector_lock_ownership(self):
        directory = self.source / 'reports'
        uid, gid = os.geteuid(), os.getegid()
        if uid == 0:
            uid, gid = 59999, 59999
            self.root.chmod(0o755)
            self.source.chmod(0o755)
            os.chown(directory, uid, gid)
        for name in ('.pruning.lock', '.spool.lock'):
            with pending.lock(directory / name):
                info = (directory / name).stat()
                self.assertEqual((info.st_uid, info.st_gid, info.st_mode & 0o777), (uid, gid, 0o600))
        code = ('import os,sys,fcntl;uid=int(sys.argv[1]);gid=int(sys.argv[2]);'
            'os.setgroups([]) if os.geteuid()==0 else None;'
            'os.setgid(gid) if os.geteuid()==0 else None;'
            'os.setuid(uid) if os.geteuid()==0 else None;'
            'files=[open(p,"a") for p in sys.argv[3:]];'
            '[fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB) for f in files]')
        subprocess.run(['/usr/bin/python3', '-c', code, str(uid), str(gid),
            str(directory / '.pruning.lock'), str(directory / '.spool.lock')], check=True, timeout=10)

    def test_export_locks_refuse_symlinks_and_hardlinks(self):
        original = self.root / 'original'
        original.write_text('leave unchanged')
        for name, make_link in (('symlink', lambda p: p.symlink_to(original)),
                ('hardlink', lambda p: os.link(original, p))):
            path = self.root / name
            make_link(path)
            with self.assertRaises((OSError, ValueError)):
                with pending.lock(path):
                    self.fail('unsafe lock was accepted')
            self.assertEqual(original.read_text(), 'leave unchanged')


class RestoredPendingDatabaseTests(DatabaseCase):
    def test_restored_files_import_idempotently_without_changing_financial_records(self):
        self.fund()
        self.reserve('held-through-restore')
        def financial_state():
            with self.db.transaction() as cursor:
                state = {}
                for table in ('accounts', 'journals', 'entries', 'members', 'reservations'):
                    cursor.execute('SELECT row_to_json(t) AS value FROM ' + table + ' t')
                    state[table] = sorted(json.dumps(row['value'], sort_keys=True, default=str)
                        for row in cursor.fetchall())
                self.assertEqual(ledger.audit(cursor), [])
                return state
        before = financial_state()
        data = observation(20)
        report = {'start': data['start'], 'end': data['end'], 'reporting_round': 3,
            'kind': 'reports', 'payload': {'reports': [], 'arbitrations': []}}
        with tempfile.TemporaryDirectory(prefix='pending-database-restore-') as temporary:
            root = Path(temporary)
            spools = {kind: Spool(root / 'source' / kind, paged=kind == 'blocks') for kind in pending.KINDS}
            spools['blocks'].save(data, {'collector': 'restore-fixture'})
            spools['reports'].save(report, {'collector': 'restore-fixture'})
            stream = io.BytesIO()
            pending.export_snapshot(root / 'source', stream, lambda: barrier())
            stream.seek(0)
            snapshot = root / 'snapshot'
            snapshot.mkdir()
            pending.extract_snapshot(stream, snapshot)
            pending.restore_snapshot(snapshot, root / 'restored')
            store = BlockStore(self.db)
            store.initialize(20)
            for kind in ('blocks', 'reports'):
                spool = Spool(root / 'restored' / kind)
                path = spool.pending()[0]
                saved, metadata, error = spool.read(path)
                def record():
                    return (store.record(saved, metadata=metadata, error=error, collector='restored')
                        if kind == 'blocks' else report_observer.record(self.db, saved, metadata, error))
                first = record()
                self.assertTrue(first['complete'])
                self.assertTrue(record()['complete'])
                spool.recorded(path)
                self.assertEqual(spool.pending(), [])
        self.assertEqual(financial_state(), before)
        self.assertEqual(self.row('SELECT count(*) AS n FROM observed_blocks')['n'], 1)
        self.assertEqual(self.row('SELECT count(*) AS n FROM report_captures')['n'], 1)
