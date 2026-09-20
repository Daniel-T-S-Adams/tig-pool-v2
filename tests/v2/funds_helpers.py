"""Generated chain fixtures and explicitly isolated PostgreSQL test setup."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
import threading
import unittest
import uuid

from psycopg2.extensions import parse_dsn

from pool_manager.pool_v2 import benchmarks, deposits, ledger, members
from pool_manager.pool_v2.chain import Chain, Network, TRANSFER_TOPIC
from pool_manager.pool_v2.database import Database
from pool_manager.pool_v2.money import TIG


WALLET = "0x" + "1" * 40
OTHER = "0x" + "2" * 40
TOKEN = "0x" + "3" * 40
CUSTODY = "0x" + "4" * 40
NETWORK = Network(8453, TOKEN, CUSTODY, 12)


def chain_fixture(sender=WALLET, recipient=CUSTODY, amount=100*TIG):
    tx_hash = "0x" + uuid.uuid4().hex * 2
    block_hash = "0x" + "a" * 64
    log = {"address": TOKEN, "transactionHash": tx_hash, "logIndex": "0x2", "blockNumber": "0x64",
           "blockHash": block_hash, "removed": False, "data": f"0x{amount:064x}",
           "topics": [TRANSFER_TOPIC, "0x" + "0" * 24 + sender[2:], "0x" + "0" * 24 + recipient[2:]]}
    data = {
        "receipt": {"transactionHash": tx_hash, "status": "0x1", "blockNumber": "0x64", "blockHash": block_hash, "logs": [log]},
        "header": {"number": "0x64", "hash": block_hash, "timestamp": hex(int(datetime.now(timezone.utc).timestamp()))},
        "latest": {"number": "0x80"}, "finalized": {"number": "0x70"}, "chain_id": "0x2105", "decimals": "0x12",
    }
    def rpc(method, params):
        if method == "eth_chainId": return data["chain_id"]
        if method == "eth_call": return data["decimals"]
        if method == "eth_getTransactionReceipt": return deepcopy(data["receipt"])
        if method == "eth_getBlockByNumber": return deepcopy(data[params[0] if params[0] in ("latest", "finalized") else "header"])
        raise AssertionError((method, params))
    return data, Chain(NETWORK, rpc), tx_hash


def transfer(sender=WALLET, recipient=CUSTODY, amount=100*TIG):
    _, chain, tx_hash = chain_fixture(sender, recipient, amount)
    return chain.transfer(tx_hash, 2)


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        dsn = os.environ.get("POOL_V2_TEST_DSN")
        if not dsn:
            self.skipTest("set POOL_V2_TEST_DSN to an isolated PostgreSQL test database")
        if not parse_dsn(dsn).get("dbname", "").endswith("_test"):
            raise RuntimeError("test database name must end in _test; only isolated test databases are allowed")
        self.db = Database(dsn)
        with self.db.transaction() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS pool_v2 CASCADE")
        self.db.migrate()
        benchmarks.initialize_accounts(self.db)
        with self.db.transaction() as cursor:
            self.member = members.register_verified(cursor, WALLET)["id"]
            self.other = members.register_verified(cursor, OTHER)["id"]
        self.expiry = datetime.now(timezone.utc) + timedelta(minutes=10)

    def tearDown(self):
        if hasattr(self, "db"):
            with self.db.transaction() as cursor:
                self.assertEqual(ledger.audit(cursor), [])

    def fund(self, member_wallet=WALLET, amount=100*TIG):
        value = transfer(member_wallet, amount=amount)
        deposits.receive(self.db, value)
        return value

    def fees(self, amount=10*TIG):
        # Test-only bootstrap for prepaid protocol funds. The live top-up
        # adapter must verify both the outgoing token transfer and TIG credit.
        with self.db.transaction() as cursor:
            ledger.post(cursor, "test-protocol-funding:" + str(uuid.uuid4()), "fixture",
                        [("external:protocol:TIG", -amount), (benchmarks.OPERATOR_FEES, amount)])

    def reserve(self, key="one", member=None, resource="CPU", fee=0):
        return benchmarks.reserve(self.db, member or self.member, key, creation_round=10,
            resource=resource, selection={"block_id": "fixture-block"},
            payload={"track_settings": {"a": {"num_bundles": 5}, "b": {"num_bundles": 3}}},
            fee_limit=fee, offer_expires_at=self.expiry)

    def row(self, sql, args=()):
        with self.db.transaction() as cursor:
            cursor.execute(sql, args)
            return cursor.fetchone()

    def balance(self, member=None):
        return members.balances(self.db, member or self.member)

    def concurrent(self, functions):
        barrier = threading.Barrier(len(functions))
        def call(function):
            barrier.wait(timeout=15)
            try:
                return function()
            except Exception as exc:
                return exc
        with ThreadPoolExecutor(max_workers=len(functions)) as executor:
            return list(executor.map(call, functions))
