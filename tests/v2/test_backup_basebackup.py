"""The daily base backup must hand the older-base archive directory, not the WAL tarball, to base maintenance."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
import mainnet_backup_v2 as backup


class BaseBackupArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'volume'
        self.bases = self.root / 'base'; self.bases.mkdir(parents=True)
        self.wal = self.root / 'wal'; self.wal.mkdir()
        self.archive = Path(self.temporary.name) / 'older'; self.archive.mkdir()
        pgpass = Path(self.temporary.name) / 'pgpass'; pgpass.write_text('fixture')
        self.config = {'directory': str(self.root), 'base_archive_directory': str(self.archive), 'minimum_free_gib': 35,
                       'pgpass': str(pgpass), 'tunnel_port': 55440, 'source_host': '198.51.100.10',
                       'postgres_image': 'postgres@sha256:' + '0' * 64, 'wal_segment_bytes': 16 * 1024 ** 2}

    def save(self, stamp, number, *, parent=None):
        path = (parent or self.bases) / stamp; path.mkdir()
        files = {}
        for name in ('base.tar.gz', 'pg_wal.tar.gz', 'backup_manifest'):
            raw = (stamp + name).encode(); (path / name).write_bytes(raw)
            files[name] = hashlib.sha256(raw).hexdigest()
        record = {'snapshot': stamp, 'data_checksums_verified': True, 'wal_parse_verified': True,
                  'system_identifier': 'one', 'first_wal_segment': f'{1:08X}{0:08X}{number:08X}',
                  'archive_bytes': sum((path / name).stat().st_size for name in files), 'files': files}
        (path / 'verified.json').write_text(json.dumps(record))
        return record

    def fake_backup(self, mounts, args):
        """Stand in for the pinned PostgreSQL container: write a tiny verified-looking backup."""
        if 'pg_basebackup' in args:
            staging = Path(mounts[0][0])
            (staging / 'base.tar.gz').write_bytes(b'base')
            with tarfile.open(staging / 'pg_wal.tar.gz', 'w:gz') as tarball:
                segment = Path(self.temporary.name) / '000000010000000000000004'; segment.write_bytes(b'wal')
                tarball.add(segment, arcname=segment.name)
            (staging / 'backup_manifest').write_text(json.dumps({'System-Identifier': 'one',
                'WAL-Ranges': [{'Timeline': 1, 'Start-LSN': '0/4000028', 'End-LSN': '0/4000100'}]}))

    def test_basebackup_relocates_bases_through_the_archive_directory_not_the_wal_tarball(self):
        self.save('2026-10-13T104305Z', 1, parent=self.archive)
        self.save('2026-10-14T031353Z', 2, parent=self.archive)
        self.save('2026-10-14T134951Z', 3)
        for number in range(0, 5):
            (self.wal / f'{1:08X}{0:08X}{number:08X}').write_bytes(b'segment')
        calls = []
        with patch.object(backup, 'docker', side_effect=lambda config, mounts, args, **options: (mounts, args)), \
                patch.object(backup, 'checked', side_effect=lambda command, timeout: (calls.append(command[1][0]), self.fake_backup(*command))), \
                patch.object(backup.shutil, 'disk_usage', return_value=SimpleNamespace(free=10 ** 12)), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            backup.basebackup(self.config)
        record = json.loads(output.getvalue().strip().splitlines()[-1])
        self.assertEqual(calls, ['pg_basebackup', '/usr/lib/postgresql/18/bin/pg_verifybackup', '/usr/lib/postgresql/18/bin/pg_waldump'])
        self.assertEqual(record['first_wal_segment'], f'{1:08X}{0:08X}{4:08X}')
        retention = json.loads((self.root / 'retention.json').read_text())
        self.assertEqual(retention['older_base_archive'], str(self.archive))
        self.assertEqual(retention['kept_base_snapshots'], ['2026-10-14T031353Z', '2026-10-14T134951Z', record['snapshot']])
        self.assertEqual(retention['removed_base_snapshots'], ['2026-10-13T104305Z'])
        self.assertEqual(retention['newest_base_on_volume'], record['snapshot'])
        self.assertEqual(sorted(path.name for path in self.bases.iterdir()), [record['snapshot']])
        self.assertEqual(sorted(path.name for path in self.archive.iterdir()), ['2026-10-14T031353Z', '2026-10-14T134951Z'])
        self.assertEqual(json.loads((self.archive / '2026-10-14T134951Z' / 'verified.json').read_text())['snapshot'], '2026-10-14T134951Z')
        self.assertEqual(json.loads((self.root / 'latest.json').read_text())['snapshot'], record['snapshot'])
        self.assertEqual(sorted(path.name for path in self.wal.iterdir()), [f'{1:08X}{0:08X}{number:08X}' for number in range(2, 5)])


if __name__ == '__main__':
    unittest.main()
