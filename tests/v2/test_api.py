import hashlib

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from pool_manager.pool_v2.api import Settings, create_app
from pool_manager.pool_v2 import deposits
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
