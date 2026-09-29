from unittest.mock import patch
import tempfile
from pathlib import Path

from pool_manager.pool_v2 import report_observer
from pool_manager.pool_v2.block_observer import BlockStore
from pool_manager.pool_v2.spool import Spool
from pool_manager.pool_v2.protocol import ProtocolDataError
from pool_manager.pool_v2.observation import read_archive
from funds_helpers import DatabaseCase
from observer_helpers import observation


class ReportObserverTests(DatabaseCase):
    def test_recorded_public_arbitrations_replay_into_immutable_facts(self):
        fixtures = Path(__file__).parent / "fixtures"
        block = read_archive(next(fixtures.glob("block-1351111*")))["observation"]
        store = BlockStore(self.db); store.initialize(1351111)
        recorded = store.record(block, collector="public-fixture")
        saved = read_archive(fixtures / "reports-129.json.gz")
        from pool_manager.pool_v2 import reports
        result = reports.record(self.db, 129, recorded["block_id"], saved, metadata={"fixture": "reports-129.json.gz"})
        self.assertTrue(result["complete"], result)
        self.assertEqual(self.row("SELECT count(*) AS n FROM confirmed_arbitrations")["n"], 16)

    def test_outage_spool_replays_confirmed_reports_without_live_network_or_duplicate_facts(self):
        data = observation(20)
        payload = {"reports": [{"id": "r", "state": {"block_confirmed": 13}, "details": {
            "benchmark_id": "b", "benchmarker": "pool", "nonce": 1, "round": 3}}],
            "arbitrations": [{"report_id": "r", "state": {"block_confirmed": 19}, "details": {"result": "nonreproducible"}}]}
        class Client:
            records = []
            def get(self, path, params=None):
                if path == "/get-block": return data["start"]
                if path == "/get-reports" and params == {"round": 3}: return payload
                raise AssertionError((path, params))
        with tempfile.TemporaryDirectory() as directory:
            with patch("pool_manager.pool_v2.report_observer.time.time", return_value=1800000020):
                saved, metadata, error = report_observer.capture(Client(), 3)
            self.assertIsNone(error)
            spool = Spool(directory)
            path = spool.save(saved, metadata, error)
            recovered = Spool(directory).read(path)
            with self.assertRaises(ProtocolDataError): report_observer.record(self.db, *recovered)
            store = BlockStore(self.db); store.initialize(20)
            store.record(data, collector="block-fixture")
            first = report_observer.record(self.db, *recovered)
            self.assertTrue(first["complete"], first)
            self.assertEqual(report_observer.record(self.db, *recovered)["id"], first["id"])
            spool.recorded(path)
            self.assertEqual(self.row("SELECT count(*) AS n FROM confirmed_arbitrations")["n"], 1)
            self.assertEqual(self.row("SELECT count(*) AS n FROM collateral_finalizations")["n"], 0)

    def test_fetch_failure_stale_head_and_crossed_boundary_are_retained_as_incomplete(self):
        store = BlockStore(self.db); store.initialize(20)
        data = observation(20); store.record(data, collector="fixture")
        class Client:
            records = []
            def __init__(self, mode): self.mode, self.heads = mode, 0
            def get(self, path, params=None):
                if path == "/get-block":
                    self.heads += 1
                    return observation(21)["start"] if self.mode == "boundary" and self.heads == 2 else data["start"]
                if self.mode == "failure": raise TimeoutError("fixture unavailable")
                return {"reports": [], "arbitrations": []}
        for mode in ("failure", "stale", "boundary"):
            with self.subTest(mode=mode), patch("pool_manager.pool_v2.report_observer.time.time", return_value=1800000300 if mode == "stale" else 1800000020):
                saved, metadata, error = report_observer.capture(Client(mode), 3)
                self.assertIsNotNone(error)
                result = report_observer.record(self.db, saved, metadata, error)
                self.assertFalse(result["complete"], result)
