import unittest

from eth_account import Account
from eth_account.messages import encode_defunct

from pool_manager.pool_v2.auth import Auth, AuthenticationError, token_digest
from pool_manager.pool_v2.chain import Network, Rpc
from pool_manager.pool_v2.money import FundsError
from funds_helpers import DatabaseCase, NETWORK, chain_fixture


class ChainTests(unittest.TestCase):
    def test_complete_confirmed_transfer(self):
        data, chain, tx_hash = chain_fixture()
        transfer = chain.transfer(tx_hash, 2)
        self.assertEqual(transfer.network.chain_id, 8453)
        self.assertEqual(transfer.block_number, 100)
        self.assertTrue(transfer.event_id.endswith(":2"))

    def test_wrong_network_decimals_failure_finality_and_event_rejected(self):
        mutations = [
            lambda d: d.update(chain_id="0x14a33"), lambda d: d.update(decimals="0x6"),
            lambda d: d["receipt"].update(status="0x0"), lambda d: d["latest"].update(number="0x65"),
            lambda d: d["finalized"].update(number="0x63"), lambda d: d["header"].update(hash="0x" + "b"*64),
            lambda d: d["receipt"]["logs"][0].update(removed=True),
            lambda d: d["receipt"]["logs"][0].update(address="0x" + "8"*40),
            lambda d: d["receipt"]["logs"][0].update(data="0x1"),
            lambda d: d["receipt"]["logs"][0].update(topics=[]),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                data, chain, tx_hash = chain_fixture()
                mutate(data)
                with self.assertRaises(FundsError): chain.transfer(tx_hash, 2)

    def test_read_only_rpc_rejects_transaction_sending(self):
        with self.assertRaises(FundsError):
            Rpc("https://example.invalid")("eth_sendRawTransaction", ["0x00"])
        with self.assertRaises(FundsError):
            Network(8453, NETWORK.token, NETWORK.custody, 0)


class AuthTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.auth = Auth(self.db, origin="https://pool.example", chain_id=8453)
        self.signer = Account.create()

    def login(self):
        challenge = self.auth.challenge(self.signer.address)
        signature = Account.sign_message(encode_defunct(text=challenge["message"]), self.signer.key).signature.hex()
        return challenge, signature, self.auth.verify(challenge["id"], signature)

    def test_signature_creates_stable_member_and_challenge_cannot_replay(self):
        challenge, signature, session = self.login()
        self.assertIn("Chain ID: 8453", challenge["message"])
        with self.assertRaises(AuthenticationError): self.auth.verify(challenge["id"], signature)
        _, _, second = self.login()
        self.assertEqual(session["member_id"], second["member_id"])
        with self.db.transaction() as cursor:
            self.assertEqual(str(self.auth.authenticate(cursor, session["token"], wallet=True)), session["member_id"])

    def test_wrong_signer_and_expired_challenge_fail(self):
        challenge = self.auth.challenge(self.signer.address)
        signature = Account.sign_message(encode_defunct(text=challenge["message"]), Account.create().key).signature.hex()
        with self.assertRaises(AuthenticationError): self.auth.verify(challenge["id"], signature)
        with self.db.transaction() as cursor:
            cursor.execute("UPDATE auth_challenges SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (challenge["id"],))
        correct = Account.sign_message(encode_defunct(text=challenge["message"]), self.signer.key).signature.hex()
        with self.assertRaises(AuthenticationError): self.auth.verify(challenge["id"], correct)

    def test_execution_token_cannot_manage_money_or_create_more_tokens(self):
        _, _, session = self.login()
        token = self.auth.issue_execution_token(session["token"])
        with self.db.transaction() as cursor:
            self.assertEqual(str(self.auth.authenticate(cursor, token)), session["member_id"])
        with self.assertRaises(AuthenticationError), self.db.transaction() as cursor:
            self.auth.authenticate(cursor, token, wallet=True)
        with self.assertRaises(AuthenticationError): self.auth.issue_execution_token(token)
        self.auth.revoke(session["token"], token)
        with self.assertRaises(AuthenticationError), self.db.transaction() as cursor:
            self.auth.authenticate(cursor, token)
        self.assertNotEqual(token_digest(token), token)

    def test_consuming_same_signature_concurrently_issues_one_session(self):
        challenge = self.auth.challenge(self.signer.address)
        signature = Account.sign_message(encode_defunct(text=challenge["message"]), self.signer.key).signature.hex()
        results = self.concurrent([lambda: self.auth.verify(challenge["id"], signature)] * 4)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1, results)
        self.assertTrue(all(isinstance(result, (dict, AuthenticationError)) for result in results), results)

    def test_challenge_bound_to_original_domain_and_chain(self):
        challenge = self.auth.challenge(self.signer.address)
        signature = Account.sign_message(encode_defunct(text=challenge["message"]), self.signer.key).signature.hex()
        other = Auth(self.db, origin="https://other.example", chain_id=8453)
        with self.assertRaises(AuthenticationError): other.verify(challenge["id"], signature)
        other = Auth(self.db, origin="https://pool.example", chain_id=1)
        with self.assertRaises(AuthenticationError): other.verify(challenge["id"], signature)
