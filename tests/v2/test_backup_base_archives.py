import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
from mainnet_backup_v2 import base_catalogue, locate_base, maintain_bases


class BaseArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'volume'
        self.bases = self.root / 'base';self.bases.mkdir(parents=True)
        self.wal = self.root / 'wal';self.wal.mkdir()
        self.archive = Path(self.temporary.name) / 'older';self.archive.mkdir()

    def save(self, stamp, number, *, parent=None, suffix=''):
        path=(parent or self.bases)/stamp;path.mkdir()
        files={}
        for name in ('base.tar.gz','pg_wal.tar.gz','backup_manifest'):
            raw=(stamp+name+suffix).encode();(path/name).write_bytes(raw)
            files[name]=hashlib.sha256(raw).hexdigest()
        record={'snapshot':stamp,'data_checksums_verified':True,'wal_parse_verified':True,
            'system_identifier':'one','first_wal_segment':f'{1:08X}{0:08X}{number:08X}',
            'archive_bytes':sum((path/name).stat().st_size for name in files),'files':files}
        (path/'verified.json').write_text(json.dumps(record))
        return record

    def test_identical_copies_are_one_snapshot_and_restore_lookup_finds_older(self):
        stamp='2026-10-13T100000Z'
        record=self.save(stamp,1)
        self.save(stamp,1,parent=self.archive)
        records,locations=base_catalogue(self.bases,self.archive)
        self.assertEqual(records,[record]);self.assertEqual(len(locations[stamp]),2)
        import shutil
        shutil.rmtree(self.bases/stamp)
        self.assertEqual(locate_base(self.bases,stamp,self.archive),self.archive/stamp)

    def test_three_bases_keep_newest_on_volume_and_required_wal(self):
        stamps=[f'2026-10-{day:02d}T100000Z' for day in range(11,15)]
        records=[self.save(stamp,n+1) for n,stamp in enumerate(stamps)]
        for number in range(1,6):(self.wal/f'{1:08X}{0:08X}{number:08X}').write_bytes(b'wal')
        with patch('mainnet_backup_v2.shutil.disk_usage',return_value=SimpleNamespace(free=10**12)):
            result=maintain_bases(self.root,self.archive)
        self.assertEqual(result['removed_base_snapshots'],[stamps[0]])
        self.assertEqual([p.name for p in self.bases.iterdir()],[stamps[-1]])
        self.assertEqual(sorted(p.name for p in self.archive.iterdir()),stamps[1:3])
        kept,_=base_catalogue(self.bases,self.archive)
        self.assertEqual(kept,records[1:])
        self.assertFalse((self.wal/f'{1:08X}{0:08X}{1:08X}').exists())
        self.assertTrue((self.wal/f'{1:08X}{0:08X}{2:08X}').exists())

    def test_low_root_reserve_preserves_originals_and_wal(self):
        stamps=[f'2026-10-{day:02d}T100000Z' for day in range(12,15)]
        for n,stamp in enumerate(stamps):self.save(stamp,n+1)
        segment=self.wal/f'{1:08X}{0:08X}{0:08X}';segment.write_bytes(b'wal')
        with patch('mainnet_backup_v2.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(RuntimeError,'reserve'):
                maintain_bases(self.root,self.archive)
        self.assertTrue(all((self.bases/stamp).exists() for stamp in stamps))
        self.assertTrue(segment.exists())

    def test_conflicting_or_corrupt_copies_are_rejected(self):
        stamp='2026-10-13T100000Z'
        self.save(stamp,1);self.save(stamp,1,parent=self.archive,suffix='changed')
        with self.assertRaisesRegex(ValueError,'conflicting'):
            base_catalogue(self.bases,self.archive)
        (self.archive/stamp/'base.tar.gz').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'checksum'):
            base_catalogue(self.bases,self.archive)
