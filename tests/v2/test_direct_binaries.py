import hashlib
import io
import time
import unittest
from unittest.mock import patch
from urllib.request import Request

from pool_manager.pool_v2 import submissions, work_requests
from pool_manager.pool_v2.artifacts import ArtifactRedirect, DEFAULT_HOSTS, check_url, read_archive
from pool_manager.pool_v2.block_observer import BlockStore
from pool_manager.pool_v2.coordinator import download_archive
from pool_manager.pool_v2.money import Conflict, FundsError, TIG
from funds_helpers import DatabaseCase
from observer_helpers import observation


MEDIA = 'https://media.githubusercontent.com/media/tig-foundation/tig-monorepo/main/algorithms/fixture.tar.gz'


class PublicArtifactTests(unittest.TestCase):
    def test_download_and_redirect_share_the_same_source_validation(self):
        original = Request('https://mainnet-api.tig.foundation/get-binary-blob', headers={
            'Authorization': 'Bearer fixture-secret', 'Cookie': 'fixture-cookie'})
        redirected = ArtifactRedirect(DEFAULT_HOSTS).redirect_request(original, None, 302, 'found', {}, MEDIA)
        self.assertEqual(dict(redirected.header_items()), {'User-agent': 'innopool-v2-artifact/2.0'})
        for url in (
            MEDIA.replace('https:', 'http:'),
            MEDIA.replace('media.githubusercontent.com', 'attacker.example'),
            MEDIA.replace('media.githubusercontent.com', 'user:password@media.githubusercontent.com'),
            MEDIA + '#fragment',
            MEDIA.replace('media.githubusercontent.com', 'media.githubusercontent.com:444'),
            MEDIA.replace('media.githubusercontent.com', 'media.githubusercontent.com:bad'),
            MEDIA.replace('/tig-foundation/tig-monorepo/', '/someone/another/'),
            MEDIA.replace('/main/', '/../../someone/another/'),
            MEDIA.replace('/main/', '/%2e%2e/%2e%2e/someone/another/'),
            MEDIA.replace('/main/', '/%2e%2e%2fsomeone/'),
        ):
            with self.subTest(url=url), patch('pool_manager.pool_v2.coordinator.build_opener') as opener:
                with self.assertRaises(FundsError):
                    download_archive(url, allowed_hosts=DEFAULT_HOSTS)
                opener.assert_not_called()
                with self.assertRaises(FundsError):
                    ArtifactRedirect(DEFAULT_HOSTS).redirect_request(original, None, 302, 'found', {}, url)

    def test_public_archive_download_has_no_execution_credentials(self):
        check_url(MEDIA, DEFAULT_HOSTS)
        with patch('pool_manager.pool_v2.coordinator.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(b'fixture-archive')
            self.assertEqual(download_archive(MEDIA, allowed_hosts=DEFAULT_HOSTS), b'fixture-archive')
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, MEDIA)
            self.assertNotIn('Authorization', request.headers)
            self.assertIsInstance(opener.call_args.args[0], ArtifactRedirect)


class DirectBinaryTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.player = '0x' + '6' * 40
        self.fund(amount=200 * TIG)
        self.fees()
        self.data = observation(8)
        self.data['pool_player_id'] = self.player
        self.data['pool_pending'] = {'precommits': [], 'benchmarks': [], 'proofs': [], 'frauds': []}
        for row in self.data['algorithms']['binarys']:
            row['details']['download_url'] = MEDIA
        self.store = BlockStore(self.db)
        self.store.initialize(8)
        self.assertTrue(self.store.record(self.data, collector='fixture')['complete'])

    def reserve_offer(self):
        work_requests.create(self.db, self.member, 'direct', resource='CPU', compute_type='aws_c7a',
                             capacity={'workers': 12})
        return work_requests.reserve_next(self.db, self.player, now=self.data['start']['block']['details']['timestamp'])

    def precommit(self, row):
        track = sorted(row['payload']['track_settings'])[0]
        chosen = row['payload']['track_settings'][track]
        return {'benchmark_id': 'b' * 32, 'settings': {**row['payload']['settings'], 'track_id': track},
                'state': {'block_confirmed': 9}, 'details': {**chosen,
                    'num_nonces': chosen['num_bundles'] * 2, 'rand_hash': 'a' * 32,
                    'block_started': 8, 'fee_paid': str(row['fee_limit']), 'compute_type': 'aws_c7a'}}

    def test_new_preparation_keeps_metadata_without_persisting_archive_bytes(self):
        row = self.reserve_offer()
        payload = b'public-fixture-archive'
        submissions.prepare_archive(self.db, row['id'], MEDIA, payload)
        saved = self.row('SELECT * FROM reservation_binaries WHERE reservation_id=%s', (row['id'],))
        self.assertEqual((saved['sha256'], saved['source_url'], saved['size_bytes']),
                         (hashlib.sha256(payload).hexdigest(), MEDIA, len(payload)))
        self.assertEqual(self.row('SELECT count(*) AS n FROM algorithm_archives')['n'], 0)
        self.assertEqual(self.row('SELECT count(*) AS n FROM reservation_archives')['n'], 0)
        submissions.prepare_archive(self.db, row['id'], MEDIA, payload)
        with self.assertRaises(Conflict):
            submissions.prepare_archive(self.db, row['id'], MEDIA, b'different')
        with self.assertRaises(Conflict):
            submissions.prepare_archive(self.db, row['id'], MEDIA + '?different=1', payload)
        with self.assertRaises(Exception):
            with self.db.transaction() as cursor:
                cursor.execute('DELETE FROM reservation_binaries WHERE reservation_id=%s', (row['id'],))

    def test_precommit_fence_requires_prepared_binary_metadata(self):
        row = self.reserve_offer()
        intent = self.row("SELECT id FROM protocol_outbox WHERE reservation_id=%s AND kind='precommit'", (row['id'],))
        preflight = {'block_id': 'block-8', 'height': 8, 'seen_benchmarks': [], 'observed_at': int(time.time())}
        with self.assertRaises(Conflict):
            submissions.begin(self.db, intent['id'], preflight=preflight)
        submissions.prepare_archive(self.db, row['id'], MEDIA, b'fixture')
        self.assertEqual(submissions.begin(self.db, intent['id'], preflight=preflight)['state'], 'uncertain')

    def test_new_assignment_uses_the_direct_url_even_with_a_pool_artifact_origin(self):
        row = self.reserve_offer()
        payload = b'fixture'
        submissions.prepare_archive(self.db, row['id'], MEDIA, payload)
        intent = self.row("SELECT id FROM protocol_outbox WHERE reservation_id=%s AND kind='precommit'", (row['id'],))
        submissions.begin(self.db, intent['id'], preflight={
            'block_id': 'block-8', 'height': 8, 'seen_benchmarks': [], 'observed_at': int(time.time())})
        submissions.record_response(self.db, intent['id'], {'status': 200, 'body': {'benchmark_id': 'b' * 32}})
        assigned = submissions.publish_assignment(self.db, row['id'], self.precommit(row),
            evidence={'fixture': True}, artifact_origin='https://pool.example')
        self.assertEqual(assigned['assignment']['binary_url'], MEDIA)
        self.assertEqual(assigned['assignment']['binary_sha256'], hashlib.sha256(payload).hexdigest())
        self.assertEqual(self.row('SELECT count(*) AS n FROM algorithm_archives')['n'], 0)
        self.assertEqual(self.balance()['slots'], 1)

    def test_legacy_prepared_assignment_keeps_its_saved_archive_endpoint(self):
        row = self.reserve_offer()
        payload = b'legacy-fixture'
        digest = hashlib.sha256(payload).hexdigest()
        with self.db.transaction() as cursor:
            cursor.execute('INSERT INTO algorithm_archives(sha256,archive) VALUES (%s,%s)', (digest, payload))
            cursor.execute('INSERT INTO reservation_archives(reservation_id,sha256,source_url) VALUES (%s,%s,%s)',
                           (row['id'], digest, MEDIA))
        intent = self.row("SELECT id FROM protocol_outbox WHERE reservation_id=%s AND kind='precommit'", (row['id'],))
        submissions.begin(self.db, intent['id'], preflight={
            'block_id': 'block-8', 'height': 8, 'seen_benchmarks': [], 'observed_at': int(time.time())})
        submissions.record_response(self.db, intent['id'], {'status': 200, 'body': {'benchmark_id': 'b' * 32}})
        assigned = submissions.publish_assignment(self.db, row['id'], self.precommit(row),
            evidence={'fixture': True}, artifact_origin='https://pool.example')
        self.assertEqual(assigned['assignment']['binary_url'], 'https://pool.example/api/v2/artifacts/' + digest)
        self.assertEqual(read_archive(self.db, digest), payload)
        self.assertEqual(self.row('SELECT count(*) AS n FROM reservation_binaries')['n'], 0)
