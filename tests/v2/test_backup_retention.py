from pathlib import Path
import importlib.util
import unittest

SPEC = importlib.util.spec_from_file_location('mainnet_backup_v2',
    Path(__file__).resolve().parents[2] / 'tools/mainnet_backup_v2.py')
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


def record(day, segment, timeline=1, system='123'):
    return {'snapshot': f'2026-10-{day:02}T030000Z', 'system_identifier': system,
            'first_wal_segment': f'{timeline:08X}00000000{segment:08X}'}


class RetentionTests(unittest.TestCase):
    def test_first_two_backups_never_trigger_removal(self):
        self.assertEqual(backup.retention_plan([record(1, 10), record(2, 20)],
            ['000000010000000000000001']), ([], []))

    def test_keeps_three_bases_and_every_segment_needed_by_oldest(self):
        names = ['000000010000000000000001', '000000010000000000000014',
                 '000000010000000000000028', '000000010000000000000001.partial',
                 '000000020000000000000001', '00000001.history', 'unrelated']
        old, wal = backup.retention_plan([record(i, i*10) for i in range(1, 5)], names)
        self.assertEqual(old, ['2026-10-01T030000Z'])
        self.assertEqual(wal, ['000000010000000000000001'])

    def test_different_clusters_or_timelines_require_manual_review(self):
        for changed in (record(3, 30, timeline=2), record(3, 30, system='999')):
            with self.assertRaises(ValueError):
                backup.retention_plan([record(1, 10), record(2, 20), changed], [])

    def test_less_than_three_or_duplicate_records_are_refused(self):
        with self.assertRaises(ValueError): backup.retention_plan([], [], keep=2)
        with self.assertRaises(ValueError): backup.retention_plan([record(1, 10)]*3, [])
