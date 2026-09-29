from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
import uuid

from fastapi.testclient import TestClient

from pool_manager.pool_v2 import benchmarks, ledger, member_protocol, work_requests
from pool_manager.pool_v2.api import Settings, create_app
from pool_manager.pool_v2.auth import Auth
from pool_manager.pool_v2.block_observer import BlockStore
from pool_manager.pool_v2.money import Conflict, TIG
from funds_helpers import DatabaseCase, WALLET
from observer_helpers import observation


WORKER = os.environ.get("POOL_V2_WORKER_CHECKOUT")
if WORKER:
    sys.path.insert(0, str(Path(WORKER).resolve()))
    from worker_v2.client import Client
    from worker_v2.runner import Runner
    from worker_v2.state import Store


class MemberProtocolTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.player = "0x" + "6"*40
        self.settings = Settings(self.db.dsn, "https://pool.example", 8453, "a"*64,
                                 funds_enabled=True, work_enabled=True, pool_player_id=self.player)
        self.api = TestClient(create_app(self.settings))
        auth = Auth(self.db, origin=self.settings.origin, chain_id=8453)
        with self.db.transaction() as cursor:
            self.token = auth._issue(cursor, self.member, "execution", timedelta(days=1))
            self.other_token = auth._issue(cursor, self.other, "execution", timedelta(days=1))
        self.headers = {"Authorization": "Bearer " + self.token, "X-InnoPool-Version": "2.0"}
        self.fund(amount=200*TIG)
        self.fees()
        store = BlockStore(self.db)
        store.initialize(8)
        self.observation = observation(8)
        store.record(self.observation, collector="fixture")

    def offer(self, resource="CPU", key="request"):
        return {"request_key": key, "resource": resource, "compute_type": "aws_c7a" if resource == "CPU" else "aws_g4dn", "capacity": {"workers": 2}}

    def coordinate(self):
        row = work_requests.reserve_next(self.db, self.player, now=self.observation["start"]["block"]["details"]["timestamp"])
        if row is None:
            return None
        benchmarks.mark_submitting(self.db, row["id"])
        track = sorted(row["payload"]["track_settings"])[0]
        chosen = row["payload"]["track_settings"][track]
        self.assignment = {"api_version": "2.0", "benchmark_id": "assigned-"+str(row["id"]),
            "settings": {**row["payload"]["settings"], "track_id": track}, "rand_hash": "0"*32,
            "num_nonces": chosen["num_bundles"]*2, "num_bundles": chosen["num_bundles"],
            "fuel_budget": chosen["fuel_budget"], "hyperparameters": chosen["hyperparameters"],
            "compute_type": row["payload"]["compute_type"],
            "binary_url": row["selection"]["binary"]["details"]["download_url"], "binary_sha256": "a"*64}
        accepted = benchmarks.accept(self.db, row["id"], self.assignment["benchmark_id"], self.assignment,
                                     actual_fee=int(row["fee_limit"]), evidence={"TIG_fixture": "accepted"})
        with self.db.transaction() as cursor:
            cursor.execute("UPDATE protocol_outbox SET state='accepted',evidence=%s WHERE reservation_id=%s AND kind='precommit'",
                           ('{"TIG_fixture":"accepted"}', row["id"]))
        return accepted

    def test_incompatible_version_and_resource_are_rejected_before_reservation(self):
        headers = {**self.headers, "X-InnoPool-Version": "1.0"}
        self.assertEqual(self.api.post("/api/v2/work-requests", json=self.offer(), headers=headers).status_code, 426)
        bad = {**self.offer(), "resource": "GPU"}
        self.assertEqual(self.api.post("/api/v2/work-requests", json=bad, headers=self.headers).status_code, 400)
        self.assertEqual(self.row("SELECT count(*) AS n FROM work_requests")["n"], 0)
        self.assertEqual(self.balance()["collateral"], 0)

    def test_queue_links_slot_money_and_durable_submission_in_one_transaction(self):
        request = self.api.post("/api/v2/work-requests", json=self.offer(), headers=self.headers).json()
        same = self.api.post("/api/v2/work-requests", json=self.offer(), headers=self.headers).json()
        self.assertEqual(request["id"], same["id"])
        results = self.concurrent([lambda: work_requests.reserve_next(self.db, self.player,
                                   now=self.observation["start"]["block"]["details"]["timestamp"])]*2)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1, results)
        self.assertEqual(sum(result is None for result in results), 1, results)
        self.assertEqual(self.balance()["collateral"], 50*TIG)
        self.assertEqual(self.row("SELECT count(*) AS n FROM protocol_outbox")["n"], 1)
        self.assertIsNotNone(work_requests.get(self.db, request["id"], self.member)["reservation_id"])

    def test_unhanded_work_cannot_upload_results_and_other_member_cannot_read_it(self):
        self.api.post("/api/v2/work-requests", json=self.offer(), headers=self.headers)
        row = self.coordinate()
        path = "/api/v2/benchmarks/"+row["benchmark_id"]
        result = {"merkle_root": "a"*64, "solution_quality": [1]*self.assignment["num_nonces"]}
        self.assertEqual(self.api.post(path+"/results", json=result, headers=self.headers).status_code, 409)
        self.assertEqual(self.api.get(path, headers={**self.headers,"Authorization":"Bearer "+self.other_token}).status_code, 400)
        data = self.api.get(path, headers=self.headers).json()
        self.assertEqual(hashlib.sha256(data["assignment_payload"].encode()).hexdigest(), data["assignment_digest"])
        self.assertEqual(json.loads(data["assignment_payload"]), self.assignment)

    def test_original_assignment_bytes_survive_postgres_numeric_normalization(self):
        row = self.reserve()
        benchmarks.mark_submitting(self.db, row["id"])
        value = {"api_version":"2.0","benchmark_id":"negative-zero","hyperparameters":{"value":-0.0}}
        accepted = benchmarks.accept(self.db, row["id"], "negative-zero", value, actual_fee=0, evidence={"fixture":True})
        reply = member_protocol.get(self.db, "negative-zero", self.member)
        self.assertIn('-0.0',reply["assignment_payload"])
        self.assertEqual(hashlib.sha256(reply["assignment_payload"].encode()).hexdigest(),accepted["assignment_digest"])

    def test_expired_queued_offer_can_never_reserve_or_refresh(self):
        identity = uuid.uuid4()
        offer = {k:v for k,v in self.offer().items() if k != "request_key"}
        # Seed an already aged offer; production cannot shorten a saved TTL.
        with self.db.transaction() as cursor:
            cursor.execute("""INSERT INTO work_requests(id,member_id,request_key,offer_hash,offer,expires_at)
                VALUES (%s,%s,'expired-fixture',%s,%s,clock_timestamp()-interval '1 second')""",
                (identity,self.member,ledger.fingerprint(offer),json.dumps(offer)))
        self.assertEqual(work_requests.get(self.db,identity,self.member)["state"],"expired")
        with self.assertRaises(Conflict): work_requests.refresh(self.db,identity,self.member)
        self.assertIsNone(work_requests.reserve_next(self.db,self.player,now=self.observation["start"]["block"]["details"]["timestamp"]))
        self.assertEqual(self.balance()["collateral"],0)

    @unittest.skipUnless(WORKER, "set POOL_V2_WORKER_CHECKOUT to the paired worker commit")
    def test_actual_worker_and_pool_api_complete_cpu_and_gpu_benchmarks(self):
        import tempfile
        case = self
        class HttpClient(Client):
            def __init__(self): self.origin="https://pool.example"
            def call(self, method, path, body=None):
                reply=case.api.request(method,path,json=body,headers=case.headers)
                case.assertEqual(reply.status_code,200,reply.text)
                result=reply.json()
                if method=="POST" and path=="/api/v2/work-requests": case.coordinate()
                if path.endswith("/results"):
                    member_protocol.sampled(case.db,case.assignment["benchmark_id"],[0,case.assignment["num_nonces"]-1],evidence={"TIG_fixture":"sampled"})
                if path.endswith("/proofs"):
                    row=case.row("SELECT id FROM reservations WHERE benchmark_id=%s",(case.assignment["benchmark_id"],))
                    benchmarks.record_outcome(case.db,row["id"],"active",height=10,evidence={"TIG_fixture":"verified"})
                return result
        class Runtime:
            def __init__(self): self.calls=[]
            def validate(self, assignment, offer): case.assertEqual(assignment["compute_type"],offer["compute_type"])
            def prepare(self, assignment): pass
            def release(self, assignment): pass
            def compute(self, assignment, nonce):
                self.calls.append(nonce)
                return {"nonce":nonce,"runtime_signature":123,"fuel_consumed":42,"solution":"fixture","cpu_arch":"amd64"},nonce
        for resource,compute in (("CPU","aws_c7a"),("GPU","aws_g4dn")):
            with tempfile.TemporaryDirectory() as directory:
                store,runtime=Store(directory),Runtime()
                try:
                    runner=Runner(HttpClient(),store,runtime,resource=resource,compute_type=compute,workers=2)
                    self.assertEqual(runner.step(),"awaiting_verification")
                    self.assertEqual(sorted(runtime.calls),list(range(self.assignment["num_nonces"])))
                    self.assertEqual(runner.step(),"active")
                    self.assertEqual(self.balance()["slots"],0)
                    # Active benchmarks free execution slots, not their collateral.
                    self.assertEqual(self.balance()["collateral"],50*TIG if resource=="CPU" else 100*TIG)
                finally: store.close()
