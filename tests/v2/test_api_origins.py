from dataclasses import replace
import hashlib
import re
from pathlib import Path
import tempfile
import unittest

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from pool_manager.pool_v2.api import Settings, create_app
from pool_manager.pool_v2.frontend import Frontend
from funds_helpers import DatabaseCase
from test_releases import INSTALLER, release_fixture


class ApiOriginTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.website = 'https://pool.example'
        self.api = 'https://pool-api.example'
        self.settings = Settings(self.db.dsn, self.website, 8453, '0'*64,
            api_origin=self.api, release_manifest=release_fixture(),
            build_commit='a'*40, worker_installer=INSTALLER)
        self.client = TestClient(create_app(self.settings), base_url=self.api)

    def test_website_origin_can_sign_in_and_workers_keep_their_own_permissions(self):
        preflight = self.client.options('/api/v2/auth/sessions', headers={
            'Origin': self.website, 'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'authorization,content-type,x-innopool-version'})
        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(preflight.headers['access-control-allow-origin'], self.website)
        self.assertNotIn('access-control-allow-credentials', preflight.headers)
        self.assertEqual(preflight.headers['cache-control'], 'no-store')
        signer = Account.create()
        challenge = self.client.post('/api/v2/auth/challenges',
            headers={'Origin': self.website}, json={'wallet': signer.address})
        self.assertEqual(challenge.headers['access-control-allow-origin'], self.website)
        message = challenge.json()['message']
        self.assertIn('URI: '+self.website+'\n', message)
        self.assertNotIn(self.api, message)
        signature = Account.sign_message(encode_defunct(text=message), signer.key).signature.hex()
        login = self.client.post('/api/v2/auth/sessions', headers={'Origin': self.website},
            json={'challenge_id': challenge.json()['id'], 'signature': signature})
        self.assertEqual(login.status_code, 200, login.text)
        headers = {'Origin': self.website, 'Authorization': 'Bearer '+login.json()['token']}
        token = self.client.post('/api/v2/auth/execution-tokens', headers=headers).json()['token']
        worker = {'Authorization': 'Bearer '+token}
        self.assertEqual(self.client.get('/api/v2/member/balance', headers=worker).status_code, 200)
        self.assertEqual(self.client.get('/api/v2/operator/dashboard', headers=worker).status_code, 403)
        self.assertEqual(self.client.post('/api/v2/withdrawals', headers=worker,
            json={'amount': '1', 'request_key': 'worker-cannot-withdraw'}).status_code, 401)
        capabilities = self.client.get('/api/v2/capabilities').json()
        self.assertEqual((capabilities['origin'], capabilities['api_origin']), (self.website, self.api))
        self.assertFalse(capabilities['funds_enabled'])
        self.assertFalse(capabilities['work_enabled'])

    def test_unapproved_browser_origins_methods_and_headers_are_rejected(self):
        for origin in ('https://evil.example', 'null', self.website+'.evil', self.api):
            with self.subTest(origin=origin):
                for method, kwargs in [('OPTIONS', {'headers': {'Origin': origin,
                        'Access-Control-Request-Method': 'POST'}}),
                        ('POST', {'headers': {'Origin': origin}, 'json': {'wallet': '0x'+'9'*40}})]:
                    result = self.client.request(method, '/api/v2/auth/challenges', **kwargs)
                    self.assertEqual(result.status_code, 403)
                    self.assertNotIn('access-control-allow-origin', result.headers)
                    self.assertEqual(result.headers['cache-control'], 'no-store')
        for method, header in [('DELETE', 'authorization'), ('POST', 'x-unapproved')]:
            result = self.client.options('/api/v2/member/balance', headers={
                'Origin': self.website, 'Access-Control-Request-Method': method,
                'Access-Control-Request-Headers': header})
            self.assertEqual(result.status_code, 400)
        self.assertEqual(self.client.get('/api/v2/member/balance',
            headers={'Origin': self.website}).status_code, 401)

    def test_installer_uses_api_origin_and_api_errors_are_never_cached(self):
        guide = self.client.get('/api/v2/worker-installation?resource=CPU&compute_type=aws_c7g').json()
        self.assertEqual(guide['installer_url'], self.api+'/api/v2/install-worker')
        self.assertIn('--pool '+self.api, guide['command'])
        for path, status in [('member/balance', 401), ('missing', 404), ('install-worker', 200)]:
            result = self.client.get('/api/v2/'+path, headers={'Origin': self.website})
            self.assertEqual(result.status_code, status)
            self.assertEqual(result.headers['cache-control'], 'no-store')
            self.assertEqual(result.headers['access-control-allow-origin'], self.website)

    def test_same_origin_configuration_remains_supported(self):
        client = TestClient(create_app(replace(self.settings, api_origin=None)))
        self.assertEqual(client.get('/api/v2/capabilities').json()['api_origin'], self.website)
        self.assertIn('content="'+self.website+'"', client.get('/').text)


class WebsiteAssetTests(unittest.TestCase):
    def test_only_matching_content_hashes_are_cacheable_and_html_stays_fresh(self):
        client = TestClient(create_app(Settings('dbname=unused_test', 'https://pool.example',
            8453, '0'*64, api_origin='https://pool-api.example')))
        page = client.get('/')
        self.assertEqual(page.headers['cache-control'], 'no-store')
        self.assertIn('content="https://pool-api.example"', page.text)
        self.assertIn("connect-src 'self' https://pool-api.example;", page.headers['content-security-policy'])
        paths = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', page.text)
        self.assertEqual(len(paths), 2)
        for path in paths:
            asset = client.get(path)
            self.assertEqual(asset.status_code, 200)
            self.assertEqual(path.split('/')[2], hashlib.sha256(asset.content).hexdigest())
            self.assertEqual(asset.headers['cache-control'], 'public, max-age=31536000, immutable')
            missing = client.get(path.replace(path.split('/')[2], '0'*64))
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(missing.headers['cache-control'], 'no-store')
        self.assertEqual(client.get('/assets/app.js').headers['cache-control'], 'no-store')
        self.assertEqual(client.get('/assets/'+'0'*64+'/index.html').status_code, 404)

    def test_a_changed_asset_gets_a_new_url_and_does_not_change_loaded_release_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory/'index.html').write_text('<script src="/assets/app.js"></script>')
            (directory/'app.js').write_bytes(b'old release')
            (directory/'style.css').write_bytes(b'body {}')
            old = Frontend('https://api.example', directory)
            (directory/'app.js').write_bytes(b'new release')
            new = Frontend('https://api.example', directory)
            self.assertNotEqual(old.html, new.html)
            self.assertEqual(old.asset('app.js', old.assets['app.js'][0]).body, b'old release')

    def test_unsafe_origins_fail_at_startup(self):
        settings = Settings('dbname=unused_test', 'https://pool.example', 8453, '0'*64)
        for origin in ('http://api.example', 'https://api.example/path', 'https://user:pw@api.example',
                'https://api.example?x=1', 'https://api.example/#x', "https://api.example;script-src *",
                'https://api.example\r\nX-Evil: true', 'https://api.example:0', 'https://api.example:99999', ''):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                create_app(replace(settings, api_origin=origin))
