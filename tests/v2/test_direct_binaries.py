import hashlib
import io
import tarfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request

from pool_manager.pool_v2 import submissions, work_requests
from pool_manager.pool_v2.artifacts import ArtifactRedirect, DEFAULT_HOSTS, check_url, read_archive
from pool_manager.pool_v2.block_observer import BlockStore
from pool_manager.pool_v2.coordinator import Coordinator, download_archive
from pool_manager.pool_v2.money import Conflict, FundsError, TIG
from funds_helpers import DatabaseCase
from observer_helpers import observation


MEDIA = 'https://media.githubusercontent.com/media/tig-foundation/tig-monorepo/main/algorithms/fixture.tar.gz'


def uuid_hex():
    import uuid
    return uuid.uuid4().hex


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
        # One compatible CPU challenge keeps the selected algorithm, and so the
        # archive identity, the same across the reservations in these tests.
        next(row for row in self.data['challenges']['challenges'] if row['id'] == 'c2')['config']['type'] = 'gpu'
        for row in self.data['algorithms']['binarys']:
            row['details']['download_url'] = MEDIA
        self.store = BlockStore(self.db)
        self.store.initialize(8)
        self.assertTrue(self.store.record(self.data, collector='fixture')['complete'])

    def reserve_offer(self, key='direct'):
        work_requests.create(self.db, self.member, key, resource='CPU', compute_type='aws_c7a',
                             capacity={'workers': 12})
        return work_requests.reserve_next(self.db, self.player, now=self.data['start']['block']['details']['timestamp'])

    @staticmethod
    def archive(algorithm_name, *, ptx=False):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as output:
            for name in [f'amd64/{algorithm_name}.so'] + ([f'ptx/{algorithm_name}.ptx'] if ptx else []):
                item = tarfile.TarInfo(name)
                item.size = 3
                output.addfile(item, io.BytesIO(b'bin'))
        return buffer.getvalue()

    def coordinator(self, download, **options):
        public = Mock()
        public.get.side_effect = lambda path, params=None: (
            {'block': {'id': 'block-8', 'details': {'height': 8, 'timestamp': int(time.time())}}}
            if path == '/get-block' else {'precommits': []})
        writer = Mock()
        writer.enabled = True
        writer.post.side_effect = lambda begun: {'status': 200, 'body': {'benchmark_id': uuid_hex()}}
        return Coordinator(self.db, self.player, public, writer, new_work=True, download=download, **options)

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

    def test_coordinator_downloads_once_and_reuses_the_checked_archive_for_the_next_reservation(self):
        first = self.reserve_offer('first')
        name = first['selection']['algorithm_name']
        payload = self.archive(name)
        digest = hashlib.sha256(payload).hexdigest()
        downloads = []

        def download(url):
            downloads.append(url)
            return payload
        coordinator = self.coordinator(download)
        self.assertIsNotNone(coordinator.dispatch_one())
        self.assertEqual(downloads, [MEDIA])
        verified = self.row('SELECT * FROM verified_binaries')
        self.assertEqual((verified['source_url'], verified['algorithm_name'], verified['compute_type'],
                          verified['sha256'], verified['size_bytes']), (MEDIA, name, 'aws_c7a', digest, len(payload)))
        second = self.reserve_offer('second')
        self.assertEqual(second['selection']['algorithm_name'], name)
        self.assertIsNotNone(coordinator.dispatch_one())
        self.assertEqual(downloads, [MEDIA])
        for row in (first, second):
            saved = self.row('SELECT sha256,source_url,size_bytes FROM reservation_binaries WHERE reservation_id=%s', (row['id'],))
            self.assertEqual((saved['sha256'], saved['source_url'], saved['size_bytes']), (digest, MEDIA, len(payload)))
        self.assertEqual(self.row('SELECT count(*) AS n FROM verified_binaries')['n'], 1)
        self.assertEqual(self.row('SELECT count(*) AS n FROM algorithm_archives')['n'], 0)

    def test_disabled_reuse_checks_every_reservation(self):
        first = self.reserve_offer('first')
        payload = self.archive(first['selection']['algorithm_name'])
        downloads = []

        def download(url):
            downloads.append(url)
            return payload
        coordinator = self.coordinator(download, binary_reuse_seconds=0)
        self.assertIsNotNone(coordinator.dispatch_one())
        self.reserve_offer('second')
        self.assertIsNotNone(coordinator.dispatch_one())
        self.assertEqual(downloads, [MEDIA, MEDIA])
        self.assertEqual(self.row('SELECT count(*) AS n FROM verified_binaries')['n'], 2)
        with self.assertRaises(ValueError):
            self.coordinator(download, binary_reuse_seconds=-1)

    def test_verified_archive_reuse_is_scoped_bounded_and_immutable(self):
        older, digest = 'a' * 64, 'b' * 64
        with self.db.transaction() as cursor:
            cursor.execute("""INSERT INTO verified_binaries(id,source_url,algorithm_name,compute_type,sha256,size_bytes,verified_at)
                VALUES (gen_random_uuid(),%s,'fixture_a1','aws_c7a',%s,5,clock_timestamp()-interval '2 days')""", (MEDIA, older))
        # A check older than the reuse age is not reused.
        self.assertIsNone(submissions.verified_binary(self.db, MEDIA, 'fixture_a1', 'aws_c7a', max_age_seconds=3600))
        self.assertEqual(submissions.verified_binary(self.db, MEDIA, 'fixture_a1', 'aws_c7a', max_age_seconds=3*86400)['sha256'], older)
        # A later actual check wins over an older one for the same key.
        submissions.remember_verified(self.db, MEDIA, 'fixture_a1', 'aws_c7a', digest, 6)
        found = submissions.verified_binary(self.db, MEDIA, 'fixture_a1', 'aws_c7a', max_age_seconds=3600)
        self.assertEqual((found['sha256'], found['size_bytes']), (digest, 6))
        # Another compute type, library name or URL was not checked by that download.
        for url, name, compute in ((MEDIA, 'fixture_a1', 'aws_c7g'), (MEDIA, 'fixture_a1', 'aws_g4dn'),
                                   (MEDIA, 'other_library', 'aws_c7a'), (MEDIA + '?v=2', 'fixture_a1', 'aws_c7a')):
            self.assertIsNone(submissions.verified_binary(self.db, url, name, compute, max_age_seconds=3600))
        self.assertIsNone(submissions.verified_binary(self.db, MEDIA, 'fixture_a1', 'aws_c7a', max_age_seconds=0))
        with self.assertRaises(FundsError):
            submissions.verified_binary(self.db, MEDIA, 'fixture_a1', 'aws_c7a', max_age_seconds=-1)
        for statement in ('DELETE FROM verified_binaries', "UPDATE verified_binaries SET sha256='c'||repeat('d',63)"):
            with self.assertRaises(Exception):
                with self.db.transaction() as cursor:
                    cursor.execute(statement)
        with self.assertRaises(FundsError):
            submissions.remember_verified(self.db, MEDIA, 'fixture_a1', 'aws_c7a', 'not-a-digest', 5)
