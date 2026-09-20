"""Real database simulation of X through the end of X+2; no live money."""

from copy import deepcopy
from fractions import Fraction
import uuid

from pool_manager.pool_v2 import benchmarks, dashboard, deposits, members, qualifiers, reports, settlement
from pool_manager.pool_v2.block_observer import BlockStore
from pool_manager.pool_v2.money import Conflict, InsufficientFunds, TIG
from pool_manager.pool_v2.protocol import ProtocolDataError
from funds_helpers import DatabaseCase, WALLET, OTHER, transfer
from observer_helpers import observation


POOL = WALLET
EMPTY_POOL = "0x" + "8" * 40
OPERATOR = "0x" + "7" * 40
MINT = "0x" + "0" * 40


def report_payload(*values, reporting_round=3):
    result = {"reports": [], "arbitrations": []}
    for identity, benchmark_id, nonce, decision in values:
        result["reports"].append({"id": identity, "state": {"block_confirmed": 13},
            "details": {"benchmark_id": benchmark_id, "benchmarker": POOL,
                        "nonce": nonce, "round": reporting_round}})
        if decision is not None:
            result["arbitrations"].append({"report_id": identity, "state": {"block_confirmed": 19},
                                           "details": {"result": decision}})
    return result


class SettlementTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.store = BlockStore(self.db)
        self.store.initialize(8)
        self.fund(amount=300*TIG)
        self.fund(OTHER, amount=300*TIG)

    def block(self, height, data=None):
        result = self.store.record(data or observation(height), collector="round-simulation")
        self.assertTrue(result["complete"], result)
        return result["block_id"]

    def benchmark(self, identity=None, *, member=None, created=3, bundles=5,
                  handed=True, outcome="active"):
        identity = identity or uuid.uuid4().hex
        owner = member or self.member
        row = benchmarks.reserve(self.db, owner, "work-"+identity, creation_round=created,
            resource="CPU", selection={"block_id": "block-"+str((created-1)*4)},
            payload={"settings": {"player_id": POOL}, "track_settings": {"t": {"num_bundles": bundles}}},
            fee_limit=0, offer_expires_at=self.expiry)
        benchmarks.mark_submitting(self.db, row["id"])
        row = benchmarks.accept(self.db, row["id"], identity,
            {"benchmark_id": identity, "settings": {"player_id": POOL, "challenge_id": "c1"}, "num_nonces": bundles*2},
            actual_fee=0, evidence={"source": "confirmed-fixture-precommit"})
        if handed:
            row = benchmarks.acknowledge(self.db, row["id"], owner, row["assignment_digest"])
        if outcome:
            row = benchmarks.record_outcome(self.db, row["id"], outcome,
                height=(created-1)*4+3, evidence={"source": "definitive-fixture-outcome"})
        return row

    def seal(self, payload=None, *, height=20, created=3, reporting_round=3):
        block_id = self.block(height)
        reports.scope(self.db, created, [reporting_round], adapter_version="fixture-round-mapping-v1",
                      evidence={"mapping": "explicit test fixture, not a live protocol assumption"})
        capture = reports.record(self.db, reporting_round, block_id, payload or report_payload())
        self.assertTrue(capture["complete"], capture)
        return reports.seal(self.db, created, [capture["id"]], block_id)

    def credits(self, *, zero=False, heights=range(8, 12)):
        if not zero:
            # Both benchmarks were made in round 1 and earn credit during round 3.
            self.benchmark("b", created=1, bundles=3)
            self.benchmark("second", member=self.other, created=1, bundles=2)
        for height in heights:
            data = observation(height)
            if not zero:
                for block in (data["start"]["block"], data["end"]["block"]):
                    block["details"]["num_active"]["benchmark"] = 2
                    block["data"]["active_ids"]["benchmark"] = ["b", "second"]
                player = data["players"][POOL]
                precommit = deepcopy(player["precommits"][0]); precommit["benchmark_id"] = "second"
                player["precommits"][0]["details"]["num_bundles"] = 3
                player["precommits"].append(precommit)
                benchmark = deepcopy(player["benchmarks"][0]); benchmark["id"] = "second"
                player["benchmarks"][0]["details"].update(num_active_bundles=3, average_quality_by_bundle=[9, 9, 9])
                player["benchmarks"].append(benchmark)
                proof = deepcopy(player["proofs"][0]); proof["benchmark_id"] = "second"
                player["proofs"].append(proof)
                data["opow"]["opow"][0]["block_data"]["num_qualifiers_by_challenge_by_track"]["c1"]["t"] = 3
                data["algorithms"]["codes"][0]["block_data"]["num_qualifiers_by_track_by_player"]["t"][POOL] = 3
                data["challenges"]["challenges"][0]["block_data"]["num_qualifiers_by_track"]["t"] = 3
            self.block(height, data)
            qualifiers.credit_block(self.db, "block-"+str(height), EMPTY_POOL if zero else POOL)

    def earnings(self, amount, *, withheld=0, received=True):
        settlement.declare_earnings(self.db, 3, expected_received_net=amount,
            withheld_operating_cost=withheld, block_id="block-20",
            evidence={"final_emissions": "recorded fixture", "penalties_already_deducted": True})
        if received and amount:
            receipt = transfer(MINT, amount=amount)
            deposits.receive(self.db, receipt)
            settlement.attribute_receipt(self.db, 3, receipt, evidence={"protocol_round": 3})
            return receipt

    def operator_fund(self, amount):
        receipt = transfer(OPERATOR, amount=amount)
        deposits.receive(self.db, receipt)
        deposits.attribute_reviewed(self.db, receipt, operator=True, actor="operator",
                                    evidence={"verified_sender": OPERATOR})

    def test_deadline_and_definitive_outcome_are_required(self):
        row = self.benchmark(outcome=None)
        self.block(19)
        reports.scope(self.db, 3, [3], adapter_version="fixture", evidence={"fixture": True})
        capture = reports.record(self.db, 3, "block-19", report_payload())
        with self.assertRaisesRegex(Conflict, "X\\+2"):
            reports.seal(self.db, 3, [capture["id"]], "block-19")
        self.block(20)
        with self.assertRaisesRegex(Conflict, "predates"):
            reports.seal(self.db, 3, [capture["id"]], "block-20")
        capture = reports.record(self.db, 3, "block-20", report_payload())
        seal = reports.seal(self.db, 3, [capture["id"]], "block-20")
        with self.assertRaisesRegex(Conflict, "definitive"):
            settlement.finalize_collateral(self.db, row["id"], seal["id"])
        self.assertEqual(self.balance()["collateral"], 50*TIG)

    def test_reviewed_preview_must_still_match_when_the_operator_posts(self):
        self.credits()
        seal=self.seal()
        self.earnings(100*TIG+1)
        preview=settlement.preview(self.db,3,seal['id'])
        newer=self.seal(height=21)
        with self.assertRaisesRegex(Conflict,'fresh preview'):
            settlement.allocate(self.db,3,newer['id'],expected_digest=preview['input_digest'])
        self.assertEqual(self.row('SELECT count(*) AS n FROM round_settlements')['n'],0)
        updated=settlement.preview(self.db,3,newer['id'])
        result=settlement.allocate(self.db,3,newer['id'],expected_digest=updated['input_digest'])
        self.assertEqual(result['input_digest'],updated['input_digest'])
        self.assertEqual(settlement.allocate(self.db,3,newer['id'],expected_digest=updated['input_digest']),result)
        with self.assertRaisesRegex(Conflict,'reviewed preview'):
            settlement.allocate(self.db,3,newer['id'],expected_digest=preview['input_digest'])

    def test_dashboard_shows_exact_fractional_credit_and_only_own_reward(self):
        self.credits()
        seal=self.seal()
        self.earnings(100*TIG+1)
        before=dashboard.member(self.db,self.member)['rounds']
        self.assertEqual(before[0]['allocation'],None)
        self.assertEqual(Fraction(int(before[0]['credit_numerator']),int(before[0]['credit_denominator'])),Fraction(36,5))
        import hashlib
        from fastapi.testclient import TestClient
        from pool_manager.pool_v2.api import Settings,create_app
        operator='fixture-reviewer'
        client=TestClient(create_app(Settings(self.db.dsn,'https://pool.example',8453,
            hashlib.sha256(operator.encode()).hexdigest(),settlement_enabled=True)))
        headers={'Authorization':'Bearer '+operator}
        preview=client.get('/api/v2/operator/rounds/3/preview',headers=headers)
        self.assertEqual(preview.status_code,200,preview.text)
        self.assertEqual(preview.json()['pot'],str(100*TIG+1))
        self.assertEqual(preview.json()['member_wallets'][str(self.member)],WALLET)
        posted=client.post('/api/v2/operator/rounds/3/settle',headers=headers,
            json={'input_digest':preview.json()['input_digest']})
        self.assertEqual(posted.status_code,200,posted.text)
        result=settlement.allocate(self.db,3,seal['id'])
        after=dashboard.member(self.db,self.member)['rounds']
        self.assertEqual(after[0]['allocation'],str(result['member_allocations'][str(self.member)]))
        self.assertIsNotNone(after[0]['settled_at'])
        shown=next(row for row in dashboard.operator(self.db)['rounds'] if row['round']==3)
        self.assertEqual(shown['credited_blocks'],4)
        self.assertEqual(shown['pot'],100*TIG+1)

    def test_reproducible_and_inconclusive_release_the_recorded_hold(self):
        first = self.benchmark("first")
        second = self.benchmark("second")
        seal = self.seal(report_payload(("r1", "first", 0, "reproducible"), ("r2", "second", 1, "inconclusive")))
        for row in (first, second):
            result = settlement.finalize_collateral(self.db, row["id"], seal["id"])
            self.assertEqual((result["outcome"], int(result["amount"])), ("returned", 50*TIG))
        self.assertEqual(self.balance()["available"], 300*TIG)
        self.assertEqual(self.row("SELECT count(*) AS n FROM round_settlements")["n"], 0)

    def test_two_upheld_nonces_forfeit_once_without_repricing(self):
        members.set_multiplier(self.db, self.member, "0.2", actor="operator", reason="test trust", event_key="trust")
        row = self.benchmark("fraud")
        members.set_multiplier(self.db, self.member, "1", actor="operator", reason="changed later", event_key="reset")
        seal = self.seal(report_payload(("r1", "fraud", 0, "nonreproducible"), ("r2", "fraud", 1, "nonreproducible")))
        results = self.concurrent([lambda: settlement.finalize_collateral(self.db, row["id"], seal["id"]) for _ in range(3)])
        self.assertTrue(all(isinstance(value, dict) and value["outcome"] == "forfeited" for value in results), results)
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='round:3'")["balance"], 10*TIG)
        self.assertEqual(self.balance()["available"], 290*TIG)
        self.assertEqual(self.row("SELECT count(*) AS n FROM collateral_finalizations")["n"], 1)

    def test_handover_decides_expired_work_and_zero_hold_still_finalizes(self):
        handed = self.benchmark("handed", outcome="expired")
        unhanded = self.benchmark("unhanded", handed=False, outcome="expired")
        members.set_multiplier(self.db, self.other, "0", actor="operator", reason="test", event_key="zero")
        zero = self.benchmark("zero", member=self.other, outcome="verification_failed")
        seal = self.seal()
        for row, outcome in ((handed, "forfeited"), (unhanded, "returned"), (zero, "forfeited")):
            self.assertEqual(settlement.finalize_collateral(self.db, row["id"], seal["id"])["outcome"], outcome)
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='round:3'")["balance"], 50*TIG)
        self.assertIsNone(self.row("SELECT journal_id FROM collateral_finalizations WHERE reservation_id=%s", (zero["id"],))["journal_id"])
        self.assertEqual(self.balance()["available"], 250*TIG)

    def test_pending_report_holds_only_its_benchmark_and_can_finish_after_restart(self):
        pending = self.benchmark("pending")
        clear = self.benchmark("clear")
        seal = self.seal(report_payload(("r", "pending", 0, None)))
        with self.assertRaisesRegex(Conflict, "unresolved arbitration"):
            settlement.finalize_collateral(self.db, pending["id"], seal["id"])
        first = settlement.finalize_collateral(self.db, clear["id"], seal["id"])
        updated = self.seal(report_payload(("r", "pending", 0, "inconclusive")), height=21)
        # Fresh Database instances model restart; there is no in-memory settlement state.
        from pool_manager.pool_v2.database import Database
        resumed = Database(self.db.dsn)
        self.assertEqual(settlement.finalize_collateral(resumed, clear["id"], seal["id"]), first)
        with self.assertRaisesRegex(Conflict, "changed after"):
            settlement.finalize_collateral(resumed, pending["id"], seal["id"])
        self.assertEqual(settlement.finalize_collateral(resumed, pending["id"], updated["id"])["outcome"], "returned")

    def test_failed_fetch_or_omitted_confirmed_fact_cannot_mean_no_reports(self):
        row = self.benchmark("reported")
        seal = self.seal(report_payload(("r", "reported", 0, "nonreproducible")))
        self.block(21)
        malformed = reports.record(self.db, 3, "block-21", {"error": "unavailable"})
        omitted = reports.record(self.db, 3, "block-21", report_payload())
        self.assertFalse(malformed["complete"])
        self.assertFalse(omitted["complete"])
        with self.assertRaises(Conflict):
            settlement.finalize_collateral(self.db, row["id"], seal["id"])
        with self.assertRaises(Conflict):
            reports.seal(self.db, 3, [omitted["id"]], "block-21")

    def test_replay_of_older_evidence_invalidates_an_apparently_empty_seal(self):
        row = self.benchmark("reported")
        seal = self.seal()
        self.block(19)
        result = reports.record(self.db, 3, "block-19", report_payload(("r", "reported", 0, "nonreproducible")))
        self.assertTrue(result["complete"])
        with self.assertRaisesRegex(Conflict, "replay"):
            settlement.finalize_collateral(self.db, row["id"], seal["id"])
        self.assertEqual(self.balance()["collateral"], 50*TIG)

    def test_confirmed_report_and_arbitration_identity_cannot_change(self):
        self.block(20)
        original = report_payload(("r", "benchmark", 0, "nonreproducible"))
        self.assertTrue(reports.record(self.db, 3, "block-20", original)["complete"])
        changed = deepcopy(original); changed["reports"][0]["details"]["nonce"] = 1
        self.assertFalse(reports.record(self.db, 3, "block-20", changed)["complete"])
        moved = report_payload(("r", "benchmark", 0, "nonreproducible"), reporting_round=4)
        self.assertFalse(reports.record(self.db, 4, "block-20", moved)["complete"])
        reversed_result = report_payload(("r", "benchmark", 0, "reproducible"))
        self.assertFalse(reports.record(self.db, 3, "block-20", reversed_result)["complete"])

    def test_incomplete_scope_and_report_nonce_mismatch_hold_collateral(self):
        row = self.benchmark("b")
        self.block(20)
        reports.scope(self.db, 3, [3, 4], adapter_version="boundary-fixture", evidence={"boundary": True})
        one = reports.record(self.db, 3, "block-20", report_payload(("r", "b", 10, "nonreproducible")))
        two = reports.record(self.db, 4, "block-20", report_payload())
        with self.assertRaisesRegex(Conflict, "every required"):
            reports.seal(self.db, 3, [one["id"]], "block-20")
        seal = reports.seal(self.db, 3, [one["id"], two["id"]], "block-20")
        with self.assertRaisesRegex(Conflict, "nonce"):
            settlement.finalize_collateral(self.db, row["id"], seal["id"])

    def test_reporting_index_records_positive_membership_without_assuming_creation_round(self):
        identity = "a"*32
        self.benchmark(identity)
        self.block(20)
        payload = {"benchmark_ids": [identity, "b"*32]}
        result = reports.record_index(self.db, 4, "block-20", POOL, "c1", payload)
        self.assertTrue(result["complete"], result)
        mapping = self.row("SELECT * FROM benchmark_reporting_rounds WHERE benchmark_id=%s", (identity,))
        self.assertEqual((mapping["creation_round"], mapping["reporting_round"]), (3, 4))
        self.assertEqual(self.row("SELECT count(*) AS n FROM round_report_scopes")["n"], 0)
        self.assertEqual(reports.record_index(self.db, 4, "block-20", POOL, "c1", payload)["id"], result["id"])
        # Disappearance is not a different mapping or proof of no reports.
        self.assertTrue(reports.record_index(self.db, 4, "block-20", POOL, "c1", {"benchmark_ids": []})["complete"])
        self.assertFalse(reports.record_index(self.db, 3, "block-20", POOL, "c1", payload)["complete"])
        self.assertEqual(self.row("SELECT count(*) AS n FROM benchmark_reporting_rounds")["n"], 1)
        with self.assertRaisesRegex(Conflict, "omits"):
            reports.scope(self.db, 3, [3], adapter_version="wrong-fixture", evidence={"guess": True})

    def test_source_failure_with_identical_payload_is_distinct_from_a_successful_capture(self):
        row = self.benchmark()
        seal = self.seal()
        failed = reports.record(self.db, 3, "block-20", report_payload(), error="stale source anchor")
        self.assertFalse(failed["complete"])
        with self.assertRaises(Conflict):
            settlement.finalize_collateral(self.db, row["id"], seal["id"])
        recovered = reports.record(self.db, 3, "block-20", report_payload(), metadata={"request": "fresh retry"})
        self.assertTrue(recovered["complete"])
        newest = reports.seal(self.db, 3, [recovered["id"]], "block-20")
        self.assertEqual(settlement.finalize_collateral(self.db, row["id"], newest["id"])["outcome"], "returned")

    def test_net_receipts_plus_actual_operator_reimbursement_allocate_exactly_once(self):
        self.credits()
        credits = qualifiers.round_credits(self.db, 3)
        self.assertEqual(credits, {str(self.member): Fraction(36, 5), str(self.other): Fraction(24, 5)})
        seal = self.seal()
        self.earnings(98*TIG, withheld=2*TIG)
        with self.assertRaisesRegex(Conflict, "operator funding"):
            settlement.allocate(self.db, 3, seal["id"])
        with self.assertRaises(InsufficientFunds):
            settlement.reimburse_operating_cost(self.db, 3)
        self.operator_fund(2*TIG)
        settlement.reimburse_operating_cost(self.db, 3)
        before = (self.balance()["available"], self.balance(self.other)["available"])
        preview = settlement.preview(self.db, 3, seal["id"])
        self.assertTrue(preview["preview"])
        self.assertEqual(self.row("SELECT count(*) AS n FROM round_settlements")["n"], 0)
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='round:3'")["balance"], 100*TIG)
        results = self.concurrent([lambda: settlement.allocate(self.db, 3, seal["id"]) for _ in range(3)])
        self.assertTrue(all(isinstance(row, dict) and int(row["pot"]) == 100*TIG for row in results), results)
        result = results[0]
        self.assertEqual(preview["input_digest"], result["input_digest"])
        self.assertEqual(result["operator_allocation"], 5*TIG)
        self.assertEqual(result["member_allocations"], {str(self.member): 57*TIG, str(self.other): 38*TIG})
        self.assertEqual(self.balance()["available"]-before[0], 57*TIG)
        self.assertEqual(self.balance(self.other)["available"]-before[1], 38*TIG)
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='round:3'")["balance"], 0)
        self.assertEqual(self.row("SELECT count(*) AS n FROM round_settlements")["n"], 1)
        # Older benchmark holds are not part of this reward round's pot.
        self.assertEqual(self.balance()["collateral"], 30*TIG)
        self.seal(height=21)
        self.assertEqual(settlement.allocate(self.db, 3, seal["id"]), result)

    def test_forfeiture_funds_its_creation_round_and_keeps_earlier_qualifying_credit(self):
        self.credits()
        original = qualifiers.round_credits(self.db, 3)
        failed = self.benchmark("new-failure", member=self.other, outcome="verification_failed")
        seal = self.seal(report_payload(("r", "b", 0, "nonreproducible")))
        self.earnings(100*TIG)
        with self.assertRaisesRegex(Conflict, "collateral outcomes"):
            settlement.allocate(self.db, 3, seal["id"])
        settlement.finalize_collateral(self.db, failed["id"], seal["id"])
        result = settlement.allocate(self.db, 3, seal["id"])
        self.assertEqual(result["pot"], 150*TIG)
        self.assertEqual(result["operator_allocation"], 75*TIG//10)
        self.assertEqual(result["member_allocations"], {str(self.member): 855*TIG//10, str(self.other): 57*TIG})
        self.assertEqual(qualifiers.round_credits(self.db, 3), original)
        self.assertEqual(self.row("SELECT count(*) AS n FROM collateral_finalizations")["n"], 1)

    def test_unreceived_reward_and_member_deposit_cannot_back_a_settlement(self):
        self.credits(zero=True)
        seal = self.seal()
        self.earnings(10*TIG, received=False)
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='round:3'")["balance"], 0)
        with self.assertRaisesRegex(Conflict, "not all been received"):
            settlement.allocate(self.db, 3, seal["id"])
        deposit = self.fund(amount=10*TIG)
        with self.assertRaisesRegex(Conflict, "already assigned"):
            settlement.attribute_receipt(self.db, 3, deposit, evidence={"claim": "not a reward"})
        unseen = transfer(MINT, amount=10*TIG)
        with self.assertRaisesRegex(Conflict, "observed"):
            settlement.attribute_receipt(self.db, 3, unseen, evidence={"round": 3})
        deposits.receive(self.db, unseen)
        outcomes = self.concurrent([lambda: settlement.attribute_receipt(self.db, 3, unseen, evidence={"round": 3}) for _ in range(2)])
        self.assertTrue(all(isinstance(value, dict) for value in outcomes), outcomes)
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='round:3'")["balance"], 10*TIG)
        with self.assertRaises(Conflict):
            settlement.attribute_receipt(self.db, 4, unseen, evidence={"round": 4})

    def test_complete_zero_credit_and_forfeiture_pay_the_operator_once(self):
        self.credits(zero=True)
        failed = self.benchmark(outcome="expired")
        seal = self.seal()
        self.earnings(0)
        settlement.finalize_collateral(self.db, failed["id"], seal["id"])
        result = settlement.allocate(self.db, 3, seal["id"])
        self.assertEqual((result["pot"], result["operator_allocation"], result["member_allocations"]), (50*TIG, 50*TIG, {}))
        settlement.allocate(self.db, 3, seal["id"])
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='operator:custody:TIG'")["balance"], 50*TIG)

    def test_zero_value_pot_records_completion_without_a_money_journal(self):
        self.credits(zero=True)
        seal = self.seal()
        self.earnings(0)
        result = settlement.allocate(self.db, 3, seal["id"])
        self.assertEqual((result["pot"], result["operator_allocation"]), (0, 0))
        self.assertIsNone(result["journal_id"])

    def test_missing_credit_block_holds_rewards_but_not_independent_collateral(self):
        self.credits(zero=True, heights=(8, 10, 11))
        row = self.benchmark()
        seal = self.seal()
        self.earnings(10*TIG)
        self.assertEqual(settlement.finalize_collateral(self.db, row["id"], seal["id"])["outcome"], "returned")
        with self.assertRaisesRegex(ProtocolDataError, "incomplete"):
            settlement.allocate(self.db, 3, seal["id"])
        self.block(9)
        with self.assertRaises(ProtocolDataError):
            settlement.allocate(self.db, 3, seal["id"])
        qualifiers.credit_block(self.db, "block-9", EMPTY_POOL)
        self.assertEqual(settlement.allocate(self.db, 3, seal["id"])["operator_allocation"], 10*TIG)
