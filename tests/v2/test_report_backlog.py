import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pool_manager.pool_v2.spool import Spool

SPEC = importlib.util.spec_from_file_location('replay_reports_v2',
    Path(__file__).resolve().parents[2] / 'tools/replay_reports_v2.py')
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


class ReportBacklogTests(unittest.TestCase):
    def test_unpaused_pool_refuses_replay_without_consuming_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(directory)
            path = spool.save({'start': {'block': {'id': 'example'}}}, {'collector': 'test'})
            with patch.object(replay.controls, 'paused', return_value=False):
                with self.assertRaises(RuntimeError): replay.replay(object(), spool, 100, 30)
            self.assertTrue(path.exists())

    def test_missing_content_remains_pending_and_is_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(directory)
            path = spool.save({'start': {'block': {'id': 'example'}}}, {'collector': 'test'})
            with (patch.object(replay.controls, 'paused', return_value=True),
                  patch.object(spool, 'read', side_effect=FileNotFoundError('missing chunk'))):
                result = replay.replay(object(), spool, 100, 30)
            self.assertEqual(result['errors'], {'FileNotFoundError': 1})
            self.assertEqual(result['already_recorded_concurrently'], 0)
            self.assertTrue(path.exists())
