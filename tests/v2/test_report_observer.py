from unittest.mock import patch
from copy import deepcopy
from datetime import datetime, timezone
import tempfile
import time
import unittest
from pathlib import Path

from pool_manager.pool_v2 import benchmarks, report_observer, reports, settlement
from pool_manager.pool_v2.block_observer import BlockStore
from pool_manager.pool_v2.spool import Spool
from pool_manager.pool_v2.protocol import ProtocolDataError
from pool_manager.pool_v2.observation import read_archive
from pool_manager.pool_v2.money import Conflict
from funds_helpers import DatabaseCase, TIG
from observer_helpers import observation


class ReportObserverTests(DatabaseCase):
    def test_report_import_uses_validated_header_without_replaying_global_benchmarks(self):
        data = observation(20)
        BlockStore(self.db).initialize(20)
        BlockStore(self.db).record(data, collector='anchor-fixture')
        saved = {'start': data['start'], 'end': data['end'], 'reporting_round': 3,
                 'kind': 'reports', 'payload': {'reports': [], 'arbitrations': []}}
        with patch.object(BlockStore, 'read', side_effect=AssertionError('global replay is unnecessary')):
            result = report_observer.record(self.db, saved, {})
        self.assertTrue(result['complete'])
        with self.db.transaction() as cursor:
            from psycopg2.extras import Json
            cursor.execute("INSERT INTO observation_alerts(kind,height,details) VALUES ('conflicting-block',20,%s)",
                           (Json({'fixture': True}),))
        from pool_manager.pool_v2.money import Conflict
        with self.assertRaises(Conflict):
            report_observer.record(self.db, saved, {})

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
            # Provenance keeps the block identity and request records, not the block headers.
            stored = self.row("SELECT metadata FROM report_captures WHERE id=%s", (first["id"],))["metadata"]
            self.assertEqual(stored["start_block"], {"id": data["start"]["block"]["id"], "height": 20})
            self.assertNotIn("start", stored)
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


class FinalReportCaptureTests(unittest.TestCase):
    def capture(self, payload, *, observed_round=6, edge_age='0', started_at=1800000020, final_not_before=None):
        data = observation(20)
        data['start']['block']['details']['round'] = observed_round
        data['start']['block']['config']['reports'] = {'submission_period': 2}

        class Client:
            def __init__(self): self.records = []
            def get(self, path, params=None):
                self.records.append({'path': path,
                    'started_at': datetime.fromtimestamp(started_at, timezone.utc).isoformat(),
                    'response_headers': {'age': edge_age}})
                if path == '/get-block': return deepcopy(data['start'])
                if path == '/get-reports': return deepcopy(payload)
                raise AssertionError(path)

        with patch('pool_manager.pool_v2.report_observer.time.time', return_value=1800000020):
            return report_observer.capture(Client(), 3, benchmark_ids={'owned'},
                known_report_ids={'legacy'}, final_not_before=final_not_before)

    def payload(self, *, pending=False):
        payload = {'reports': [], 'arbitrations': []}
        for identity, benchmark in (('our-report', 'owned'), ('unrelated', 'foreign'), ('legacy', 'previously-known')):
            payload['reports'].append({'id': identity, 'state': {'block_confirmed': 13}, 'details': {
                'benchmark_id': benchmark, 'benchmarker': 'pool', 'nonce': 1, 'round': 3}})
            if identity != 'our-report' or not pending:
                payload['arbitrations'].append({'report_id': identity, 'state': {'block_confirmed': 19},
                    'details': {'result': 'nonreproducible'}})
        return payload

    def test_final_capture_keeps_owned_reports_and_known_facts_with_source_hash(self):
        saved, metadata, error = self.capture(self.payload())
        self.assertIsNone(error)
        self.assertEqual({row['id'] for row in saved['payload']['reports']}, {'our-report', 'legacy'})
        self.assertEqual({row['report_id'] for row in saved['payload']['arbitrations']}, {'our-report', 'legacy'})
        self.assertEqual(saved['scope_benchmark_ids'], ['owned'])
        self.assertEqual(saved['report_submission_period'], 2)
        self.assertEqual(len(saved['source_payload_sha256']), 64)
        self.assertNotIn('config', saved['start']['block'])
        self.assertNotIn('data', saved['start']['block'])

    def test_open_reporting_window_and_owned_pending_arbitration_retry(self):
        self.assertIn('still open', self.capture(self.payload(), observed_round=5)[2])
        self.assertIn('still pending', self.capture(self.payload(pending=True))[2])
        payload = self.payload()
        payload['arbitrations'] = [row for row in payload['arbitrations'] if row['report_id'] != 'unrelated']
        self.assertIsNone(self.capture(payload)[2])

    def test_cached_response_older_than_boundary_grace_cannot_be_final(self):
        self.assertIn('cache grace', self.capture(self.payload(), final_not_before=1800000010, edge_age='20')[2])
        self.assertIn('cache grace', self.capture(self.payload(), final_not_before=1800000030)[2])
        saved, _, error = self.capture(self.payload(), final_not_before=1800000010, edge_age='5')
        self.assertIsNone(error)
        self.assertIn('final_not_before_utc', saved)
        self.assertIsNotNone(self.capture(self.payload(), final_not_before=1800000010, edge_age='bad')[2])

    def test_invalid_unrelated_report_invalidates_the_full_source_before_filtering(self):
        payload = self.payload()
        payload['reports'][1]['details']['nonce'] = -1
        self.assertIsNotNone(self.capture(payload)[2])


class LeanReportQueryTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.player = '0x' + '6' * 40
        self.store = BlockStore(self.db)
        self.store.initialize(8)
        self.fund(amount=100 * TIG)
        self.block(8)

    def block(self, height):
        data = observation(height)
        data['start']['block']['config']['reports'] = {'submission_period': 2}
        data['end']['block']['config']['reports'] = {'submission_period': 2}
        self.assertTrue(self.store.record(data, collector='fixture')['complete'])

    def owner(self):
        row = benchmarks.reserve(self.db, self.member, 'reporting-owner', creation_round=3, resource='CPU',
            selection={'block_id': 'block-8'}, payload={'track_settings': {'t': {'num_bundles': 2}}},
            fee_limit=0, offer_expires_at=self.expiry)
        benchmarks.mark_submitting(self.db, row['id'])
        return benchmarks.accept(self.db, row['id'], 'a' * 32,
            {'benchmark_id': 'a' * 32, 'settings': {'player_id': self.player, 'challenge_id': 'c1'}, 'num_nonces': 4},
            actual_fee=0, evidence={'fixture': 'accepted'})

    def needed(self, current_round, *, after_grace=True, **options):
        moment = time.time() + 70 if after_grace else time.time()
        with patch('pool_manager.pool_v2.report_observer.time.time', return_value=moment):
            return report_observer.needed_queries(self.db, current_round, player_id=self.player,
                challenge_ids=['c1'], **options)

    def test_empty_pool_without_pending_rewards_does_not_poll_global_reports(self):
        self.block(20)
        needed = self.needed(6)
        self.assertEqual(needed['reports'], [])
        self.assertEqual(needed['indices'], [])

    def test_confirmation_round_mapping_uses_actual_header_and_is_saved_once(self):
        row = self.owner()
        self.assertIsNone(reports.record_confirmation_round(self.db, row['benchmark_id'], 12, 'block-8'))
        self.assertEqual(self.row('SELECT count(*) AS n FROM benchmark_reporting_rounds')['n'], 0)
        self.block(12)
        first = reports.record_confirmation_round(self.db, row['benchmark_id'], 12, 'block-12')
        self.assertTrue(first['complete'], first)
        self.assertEqual(self.row('SELECT reporting_round FROM benchmark_reporting_rounds')['reporting_round'], 4)
        reports.record_confirmation_round(self.db, row['benchmark_id'], 12, 'block-12')
        self.assertEqual(self.row('SELECT count(*) AS n FROM report_index_captures')['n'], 1)
        with self.assertRaises(Conflict):
            reports.record_confirmation_round(self.db, row['benchmark_id'], 8, 'block-12')

    def test_unknown_mapping_requests_only_owned_challenge_indices(self):
        row = self.owner()
        self.block(20)
        needed = self.needed(6)
        self.assertEqual(needed['mapping_benchmark_ids'], {row['benchmark_id']})
        self.assertEqual(needed['indices'], [(number, self.player, 'c1') for number in range(3, 7)])
        self.assertEqual(needed['reports'], [3])  # Reporting round 4's window remains open.
        self.assertEqual(self.needed(5)['reports'], [])

    def test_cross_round_confirmation_waits_for_later_reporting_window_and_cache_grace(self):
        row = self.owner()
        self.block(12)
        reports.record_confirmation_round(self.db, row['benchmark_id'], 12, 'block-12')
        self.block(20)
        self.assertEqual(self.needed(6)['reports'], [])
        self.block(24)
        self.assertEqual(self.needed(7, after_grace=False)['reports'], [])
        ready = self.needed(7)
        self.assertEqual(ready['reports'], [4])
        self.assertEqual(ready['indices'], [])
        self.assertEqual(ready['deadlines'][4], 7)
        final = reports.record(self.db, 4, 'block-24', {'reports': [], 'arbitrations': []},
            metadata={'final_for_pool': True, 'report_submission_period': 2})
        self.assertTrue(final['complete'], final)
        self.assertEqual(self.needed(7)['reports'], [])
        self.block(28)
        self.assertEqual(self.needed(8, submission_period=3)['reports'], [4])

    def test_pending_round_earnings_keep_final_queries_after_execution_obligations_end(self):
        self.block(20)
        settlement.declare_earnings(self.db, 3, expected_received_net=1, withheld_operating_cost=0,
            block_id='block-20', evidence={'fixture': 'final emissions'})
        needed = self.needed(6)
        self.assertEqual(needed['reports'], [3])
        self.assertEqual(needed['benchmark_ids'], set())
        self.assertEqual(needed['indices'], [])
