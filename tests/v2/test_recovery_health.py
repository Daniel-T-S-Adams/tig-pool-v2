"""Recovery checks require real protection while leaving pool services inactive."""
from datetime import timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
import mainnet_backup_health_v2 as health
import mainnet_pending_evidence_v2 as pending
import mainnet_recovery_health_v2 as recovery
from pool_manager.pool_v2.spool import Spool


class RecoveryHealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='recovery-health-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / 'data'
        self.config = self.root / 'config'
        self.config.mkdir()
        self.data.mkdir()
        self.profile = {'evidence_mode': 'pending', 'independent_collectors': False}
        (self.config / 'recovery-protection.json').write_text(json.dumps(self.profile))
        source = self.root / 'source'
        for kind in pending.KINDS:
            Spool(source / kind)
        stream = io.BytesIO()
        pending.export_snapshot(source, stream, lambda: {'system_identifier': '123',
            'source_lsn': '0/10', 'recovery_flush_lsn': '0/10', 'recovery_acknowledged': True})
        stream.seek(0)
        archive = self.data / 'primary-pending-evidence'
        staging = archive / '.receiving-test'
        staging.mkdir(parents=True)
        pending.extract_snapshot(stream, staging)
        self.status = pending.publish_snapshot(archive, staging, system_identifier='123')
        records = {
            'primary-continuous/latest.json': {'system_identifier': '123',
                'completed_at_utc': pending.now().isoformat(),
                'data_checksums_verified': True, 'wal_parse_verified': True},
            'primary-backups/latest-configuration.json': {'created_at_utc': pending.now().isoformat()},
            'primary-backups/continuous-health.json': {'checked_at_utc': pending.now().isoformat(),
                'issues': [], 'primary_commits_wait_for_recovery': True}}
        for name, record in records.items():
            path = self.data / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record))

    def test_fresh_verified_pending_snapshot_is_healthy(self):
        result = health.pending_snapshot_health(self.data, pending.now())
        self.assertTrue(result['durable_wal_barrier_verified'])
        self.assertEqual(result['maximum_age_seconds'], 300)
        self.assertEqual(result['manifests'], dict.fromkeys(pending.KINDS, 0))

    def test_stale_corrupt_or_wrong_cluster_snapshot_is_unhealthy(self):
        with self.assertRaisesRegex(ValueError, 'stale'):
            health.pending_snapshot_health(self.data, pending.now() + timedelta(minutes=6))
        index = self.data / 'primary-pending-evidence' / 'snapshots' / self.status['snapshot'] / 'snapshot.json'
        original = index.read_bytes()
        index.write_bytes(original + b' ')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            health.pending_snapshot_health(self.data, pending.now())
        index.write_bytes(original)
        base = self.data / 'primary-continuous/latest.json'
        base.write_text(json.dumps({'system_identifier': '456', 'data_checksums_verified': True,
            'wal_parse_verified': True}))
        with self.assertRaisesRegex(ValueError, 'cluster'):
            health.pending_snapshot_health(self.data, pending.now())

    def test_pending_mode_checks_its_timer_and_does_not_require_full_copy_jobs(self):
        calls = []
        def local_path(value):
            return {'/etc/innopool-v2-mainnet': self.config,
                '/var/lib/innopool-v2-mainnet': self.data}.get(str(value), Path(value))
        def command(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout='active\n' if 'is-active' in args else 'success\n')
        with patch.object(health, 'Path', side_effect=local_path), \
                patch.object(health.subprocess, 'run', side_effect=command):
            result = health.backup_health('recovery')
        self.assertEqual(result['issues'], [])
        self.assertTrue(result['primary_commits_wait_for_recovery'])
        units = ' '.join(' '.join(args) for args in calls)
        self.assertIn('pending-evidence.timer', units)
        self.assertNotIn('evidence-pull', units)
        self.assertNotIn('evidence-verify', units)

    def test_stale_base_is_reported_even_when_pending_files_are_fresh(self):
        path = self.data / 'primary-continuous/latest.json'
        record = json.loads(path.read_text())
        record['completed_at_utc'] = (pending.now() - timedelta(days=2)).isoformat()
        path.write_text(json.dumps(record))
        def local_path(value):
            return {'/etc/innopool-v2-mainnet': self.config,
                '/var/lib/innopool-v2-mainnet': self.data}.get(str(value), Path(value))
        with patch.object(health, 'Path', side_effect=local_path), \
                patch.object(health.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='active\n')):
            result = health.backup_health('recovery')
        self.assertIn('verified database backup is stale', result['issues'])

    def test_backup_host_allows_inactive_collectors_and_refuses_active_pool(self):
        disk = SimpleNamespace(free=100 * 1024**3)
        with patch.object(recovery.shutil, 'disk_usage', return_value=disk), \
                patch.object(recovery, 'backup_health', return_value={'issues': []}), \
                patch.object(recovery.subprocess, 'run', return_value=SimpleNamespace(stdout='inactive\n')):
            self.assertTrue(recovery.recovery_health(self.config, self.data)['healthy'])
        with patch.object(recovery.shutil, 'disk_usage', return_value=disk), \
                patch.object(recovery, 'backup_health', return_value={'issues': []}), \
                patch.object(recovery.subprocess, 'run', return_value=SimpleNamespace(stdout='active\n')):
            result = recovery.recovery_health(self.config, self.data)
        self.assertFalse(result['healthy'])
        self.assertEqual(len(result['issues']), 6)

    def test_low_disk_reserve_or_invalid_profile_is_unhealthy_without_mutation(self):
        (self.config / 'recovery-protection.json').write_text('{"evidence_mode":"disabled"}')
        with patch.object(recovery.shutil, 'disk_usage', return_value=SimpleNamespace(free=1)), \
                patch.object(recovery, 'backup_health', return_value={'issues': []}), \
                patch.object(recovery.subprocess, 'run', return_value=SimpleNamespace(stdout='inactive\n')) as commands:
            result = recovery.recovery_health(self.config, self.data)
        self.assertFalse(result['healthy'])
        self.assertIn('backup disk reserve is below 35 GiB', result['issues'])
        self.assertIn('recovery protection profile is missing or invalid', result['issues'])
        self.assertTrue(all(call.args[0][1] == 'is-active' for call in commands.call_args_list))
