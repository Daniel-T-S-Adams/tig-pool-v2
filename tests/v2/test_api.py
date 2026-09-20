import hashlib

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from pool_manager.pool_v2.api import Settings, create_app
from pool_manager.pool_v2 import benchmarks, controls, deposits, members, withdrawals
from pool_manager.pool_v2.money import TIG
from funds_helpers import DatabaseCase, transfer


class ApiTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.operator = "isolated-test-operator-secret"
        self.settings = Settings(self.db.dsn, "https://pool.example", 8453,
                                 hashlib.sha256(self.operator.encode()).hexdigest(), funds_enabled=True)
        self.client = TestClient(create_app(self.settings))
        self.signer = Account.create()
        challenge = self.client.post("/api/v2/auth/challenges", json={"wallet": self.signer.address}).json()
        signature = Account.sign_message(encode_defunct(text=challenge["message"]), self.signer.key).signature.hex()
        result = self.client.post("/api/v2/auth/sessions", json={"challenge_id": challenge["id"], "signature": signature})
        self.assertEqual(result.status_code, 200, result.text)
        self.member_id = result.json()["member_id"]
        self.headers = {"Authorization": "Bearer " + result.json()["token"]}
        self.execution = self.client.post("/api/v2/auth/execution-tokens", headers=self.headers).json()["token"]

    def test_funds_and_ledger_views_preserve_large_integer_units(self):
        deposits.receive(self.db, transfer(sender=self.signer.address.lower(), amount=101*TIG+1))
        result = self.client.get("/api/v2/member/balance", headers=self.headers)
        self.assertEqual(result.json()["available"], str(101*TIG+1))
        self.assertEqual(result.json()["multiplier"], "1")
        self.assertEqual(result.headers["cache-control"], "no-store")
        journal = self.client.get("/api/v2/member/journal", headers=self.headers).json()["entries"]
        self.assertEqual(len(journal), 1)
        self.assertEqual(journal[0]["amount"], str(101*TIG+1))
        self.assertEqual(self.client.get("/api/v2/member/balance").status_code, 401)

    def test_only_operator_changes_multiplier_and_numbers_are_rejected(self):
        path = f"/api/v2/operator/members/{self.member_id}/multiplier"
        body = {"multiplier": "0.4", "reason": "trust", "event_key": "trust-1"}
        self.assertEqual(self.client.post(path, json=body, headers=self.headers).status_code, 403)
        operator_headers = {"Authorization": "Bearer " + self.operator}
        response = self.client.post(path, json=body, headers=operator_headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["new_value"], "0.4")
        self.assertEqual(self.client.post(path, json={**body, "multiplier": 0.4}, headers=operator_headers).status_code, 422)

    def test_execution_token_cannot_withdraw_but_wallet_session_can(self):
        deposits.receive(self.db, transfer(sender=self.signer.address.lower(), amount=10*TIG))
        body = {"amount": str(7*TIG), "request_key": "withdraw-1"}
        self.assertEqual(self.client.post("/api/v2/withdrawals", json=body,
            headers={"Authorization": "Bearer " + self.execution}).status_code, 401)
        result = self.client.post("/api/v2/withdrawals", json=body, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["amount"], str(7*TIG))
        balance = self.client.get("/api/v2/member/balance", headers=self.headers).json()
        self.assertEqual(balance["available"], str(3*TIG))
        self.assertEqual(balance["pending_withdrawals"], str(7*TIG))

    def test_work_and_funds_remain_closed_by_default(self):
        settings = Settings(self.db.dsn, "https://pool.example", 8453, self.settings.operator_token_sha256)
        client = TestClient(create_app(settings))
        self.assertFalse(client.get("/api/v2/capabilities").json()["work_enabled"])
        self.assertEqual(client.post("/api/v2/withdrawals", json={"amount": "1", "request_key": "test"}, headers=self.headers).status_code, 503)

    def test_dashboards_keep_exact_funds_and_member_records_separate(self):
        deposits.receive(self.db, transfer(sender=self.signer.address.lower(), amount=101*TIG+1))
        self.fund()
        own = self.reserve('own', member=self.member_id)
        hidden = self.reserve('hidden')
        withdrawal = withdrawals.request(self.db, self.member_id, 'own-payment', 3*TIG+1)
        withdrawals.request(self.db, self.member, 'hidden-payment', TIG)
        members.set_multiplier(self.db, self.member_id, '0.2', actor='operator', reason='trust', event_key='trust')
        result = self.client.get('/api/v2/member/dashboard', headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        data = result.json()
        self.assertEqual(data['balance']['available'], str(48*TIG))
        self.assertEqual(data['balance']['pending_withdrawals'], str(3*TIG+1))
        self.assertEqual([row['id'] for row in data['assignments']], [str(own['id'])])
        self.assertEqual(data['assignments'][0]['multiplier'], '1')
        self.assertEqual(data['assignments'][0]['held'], str(50*TIG))
        self.assertEqual([row['id'] for row in data['withdrawals']], [str(withdrawal['id'])])
        self.assertNotIn(str(hidden['id']), result.text)
        self.assertEqual(self.client.get('/api/v2/operator/dashboard', headers=self.headers).status_code, 403)
        result = self.client.get('/api/v2/operator/dashboard', headers={'Authorization':'Bearer '+self.operator})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(result.json()['collateral']), 2)
        self.assertEqual(len(result.json()['multiplier_changes']), 1)
        self.assertEqual(self.client.get('/api/v2/member/dashboard?limit=0', headers=self.headers).status_code, 400)
        self.assertEqual(self.client.get('/api/v2/member/dashboard?offset=-1', headers=self.headers).status_code, 400)

    def test_operator_pause_is_audited_and_cannot_enable_disabled_capabilities(self):
        path='/api/v2/operator/controls/new-work'
        body={'paused':True,'reason':'maintenance','event_key':'pause'}
        operator={'Authorization':'Bearer '+self.operator}
        self.assertEqual(self.client.post(path,json=body,headers=self.headers).status_code,403)
        self.assertEqual(self.client.post(path,json={**body,'paused':'true'},headers=operator).status_code,422)
        self.assertEqual(self.client.post(path,json=body,headers=operator).status_code,200)
        capabilities=self.client.get('/api/v2/capabilities').json()
        self.assertTrue(capabilities['new_work_paused'])
        self.assertFalse(capabilities['work_enabled'])
        self.assertEqual(self.client.post(path,json={**body,'paused':False,'event_key':'resume'},headers=operator).status_code,200)
        # Replaying the older event is idempotent; it must not undo the later resume.
        self.assertEqual(self.client.post(path,json=body,headers=operator).status_code,200)
        self.assertFalse(controls.paused(self.db))
        self.assertFalse(self.client.get('/api/v2/capabilities').json()['work_enabled'])
        self.assertEqual(self.row('SELECT count(*) AS n FROM runtime_control_events')['n'],2)

    def test_static_screens_are_public_but_actions_require_their_own_authority(self):
        for path in ('/','/join','/operator'):
            result=self.client.get(path)
            self.assertEqual(result.status_code,200)
            self.assertIn('frame-ancestors \'none\'',result.headers['content-security-policy'])
            self.assertIn('no-store',result.headers['cache-control'])
        self.assertEqual(self.client.get('/assets/app.js').status_code,200)
        self.assertEqual(self.client.get('/assets/%2e%2e/api.py').status_code,404)
        self.assertEqual(self.client.get('/api/v2/member/dashboard').status_code,401)
        path='/api/v2/operator/rounds/3/settle'
        self.assertEqual(self.client.post(path,json={'input_digest':'0'*64},headers=self.headers).status_code,403)
        self.assertEqual(self.client.post(path,json={'input_digest':'0'*64},
            headers={'Authorization':'Bearer '+self.operator}).status_code,503)

    def test_member_can_revoke_execution_access_without_retaining_the_original_secret(self):
        path='/api/v2/auth/execution-tokens'
        worker={'Authorization':'Bearer '+self.execution}
        self.assertEqual(self.client.get(path,headers=worker).status_code,401)
        listed=self.client.get(path,headers=self.headers).json()['tokens']
        self.assertEqual(len(listed),1)
        self.assertNotIn(self.execution,str(listed))
        identity=listed[0]['id']
        self.assertEqual(self.client.post(path+'/'+identity+'/revoke',headers=worker).status_code,401)
        self.assertEqual(self.client.post(path+'/'+('f'*64)+'/revoke',headers=self.headers).status_code,404)
        self.assertEqual(self.client.post(path+'/'+identity+'/revoke',headers=self.headers).status_code,200)
        self.assertEqual(self.client.get('/api/v2/member/balance',headers=worker).status_code,401)
        self.assertEqual(self.client.get(path,headers=self.headers).json()['tokens'],[])
        self.assertEqual(self.client.get('/api/v2/member/balance',headers=self.headers).status_code,200)
