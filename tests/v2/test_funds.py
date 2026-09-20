from datetime import datetime, timedelta, timezone
import uuid

import psycopg2

from pool_manager.pool_v2 import benchmarks, deposits, ledger, members, withdrawals
from pool_manager.pool_v2.database import Database
from pool_manager.pool_v2.money import Conflict, FundsError, InsufficientFunds, TIG
from funds_helpers import DatabaseCase, OTHER, WALLET, transfer


class FundsTests(DatabaseCase):
    def test_duplicate_concurrent_deposit_is_credited_once_and_replays_after_restart(self):
        received = transfer(amount=71*TIG)
        results = self.concurrent([lambda: deposits.receive(self.db, received)] * 8)
        self.assertTrue(all(result == members.available(self.member) for result in results), results)
        self.assertEqual(self.balance()["available"], 71*TIG)
        deposits.receive(Database(self.db.dsn), received)
        self.assertEqual(self.balance()["available"], 71*TIG)
        with self.db.transaction() as cursor:
            self.assertEqual(ledger.backing(cursor), 71*TIG)

    def test_unknown_sender_requires_review_and_cannot_be_claimed_twice(self):
        received = transfer(sender="0x" + "9" * 40, amount=7*TIG)
        self.assertEqual(deposits.receive(self.db, received), "unattributed:TIG")
        self.assertEqual(self.balance()["available"], 0)
        deposits.attribute_reviewed(self.db, received, actor="operator", evidence={"case": "verified exchange withdrawal"}, member_id=self.member)
        self.assertEqual(self.balance()["available"], 7*TIG)
        with self.assertRaises(Conflict):
            deposits.attribute_reviewed(self.db, received, actor="operator", evidence={"case": "duplicate claim"}, member_id=self.other)

    def test_operator_funding_is_separate_from_member_balances(self):
        received = transfer(sender="0x" + "9" * 40, amount=9*TIG)
        deposits.receive(self.db, received)
        deposits.attribute_reviewed(self.db, received, actor="operator", evidence={"source": "own wallet"}, operator=True)
        self.assertEqual(self.balance()["available"], 0)
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id='operator:custody:TIG'")["balance"], 9*TIG)

    def test_work_and_withdrawal_race_cannot_spend_same_funds(self):
        self.fund(amount=70*TIG)
        results = self.concurrent([lambda: self.reserve(), lambda: withdrawals.request(self.db, self.member, "pay", 40*TIG)])
        self.assertEqual(sum(isinstance(result, InsufficientFunds) for result in results), 1, results)
        balances = self.balance()
        self.assertEqual(balances["available"] + balances["collateral"] + balances["pending_withdrawals"], 70*TIG)
        self.assertGreaterEqual(balances["available"], 0)

    def test_two_slots_shared_by_cpu_and_gpu_under_concurrency(self):
        self.fund(amount=1000*TIG)
        results = self.concurrent([lambda i=i: self.reserve(str(i), resource="CPU" if i % 2 else "GPU") for i in range(8)])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 2, results)
        self.assertTrue(all(isinstance(result, (dict, Conflict)) for result in results), results)
        self.assertEqual(self.balance()["slots"], 2)
        self.assertEqual(self.balance()["collateral"], 100*TIG)

    def test_operator_fee_budget_is_shared_between_members(self):
        self.fund(amount=100*TIG)
        self.fund(OTHER, amount=100*TIG)
        self.fees(3*TIG)
        results = self.concurrent([lambda: self.reserve(fee=2*TIG), lambda: self.reserve(member=self.other, fee=2*TIG)])
        self.assertEqual(sum(isinstance(result, InsufficientFunds) for result in results), 1, results)
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id=%s", (benchmarks.OPERATOR_FEES,))["balance"], TIG)
        self.assertEqual(self.balance()["collateral"] + self.balance(self.other)["collateral"], 50*TIG)

    def test_multiplier_update_and_reservation_have_one_consistent_order(self):
        self.fund(amount=100*TIG)
        results = self.concurrent([lambda: self.reserve(), lambda: members.set_multiplier(
            self.db, self.member, "0.4", actor="operator", reason="trusted", event_key="trust")])
        self.assertTrue(all(isinstance(result, dict) for result in results), results)
        reserved = results[0]
        self.assertIn((reserved["multiplier_revision"], reserved["amount"]), ((0, 50*TIG), (1, 20*TIG)))
        second = self.reserve("two")
        self.assertEqual(second["amount"], 20*TIG)
        self.assertEqual(self.reserve()["amount"], reserved["amount"])
        members.set_multiplier(self.db, self.member, "1", actor="operator", reason="reset", event_key="reset")
        self.assertEqual(self.reserve("two")["amount"], 20*TIG)
        with self.assertRaises(Conflict):
            members.set_multiplier(self.db, self.member, "0", actor="operator", reason="changed", event_key="trust")

    def test_zero_multiplier_still_reserves_slots_and_keeps_lifecycle(self):
        members.set_multiplier(self.db, self.member, "0", actor="operator", reason="trusted", event_key="zero")
        first, second = self.reserve(), self.reserve("two", resource="GPU")
        self.assertEqual(first["amount"], 0)
        self.assertEqual(first["base_amount"], 50*TIG)
        self.assertEqual(self.balance()["slots"], 2)
        with self.assertRaises(Conflict): self.reserve("third")
        benchmarks.release_unstarted(self.db, second["id"], evidence={"unsent": True})
        self.assertEqual(self.balance()["slots"], 1)
        self.assertEqual(self.row("SELECT count(*) AS n FROM journals")["n"], 0)

    def test_ambiguous_submission_cannot_refund_or_resubmit(self):
        self.fund()
        self.fees()
        reserved = self.reserve(fee=2*TIG)
        benchmarks.mark_submitting(self.db, reserved["id"])
        with self.assertRaises(Conflict): benchmarks.mark_submitting(self.db, reserved["id"])
        with self.assertRaises(Conflict): benchmarks.release_unstarted(self.db, reserved["id"], evidence={"timeout": True})
        self.assertEqual(self.balance()["collateral"], 50*TIG)
        self.assertEqual(self.balance()["slots"], 1)
        benchmarks.release_unstarted(self.db, reserved["id"], rejected=True, actual_fee=1*TIG, evidence={"definitive_rejection": "fixture"})
        benchmarks.release_unstarted(self.db, reserved["id"], rejected=True, actual_fee=1*TIG, evidence={"definitive_rejection": "fixture"})
        self.assertEqual(self.balance()["available"], 100*TIG)
        self.assertEqual(self.balance()["slots"], 0)
        self.assertEqual(self.row("SELECT balance FROM accounts WHERE id=%s", (benchmarks.OPERATOR_FEES,))["balance"], 9*TIG)

    def test_acknowledgement_is_durable_and_cannot_be_faked_by_another_member(self):
        self.fund()
        row = self.reserve()
        benchmarks.mark_submitting(self.db, row["id"])
        accepted = benchmarks.accept(self.db, row["id"], "benchmark-one", {"whole": "assignment"}, actual_fee=0, evidence={"TIG": "accepted"})
        self.assertIsNone(accepted["handed_over_at"])
        with self.assertRaises(Conflict): benchmarks.acknowledge(self.db, row["id"], self.other, accepted["assignment_digest"])
        with self.assertRaises(Conflict): benchmarks.acknowledge(self.db, row["id"], self.member, "wrong")
        handed_over = benchmarks.acknowledge(self.db, row["id"], self.member, accepted["assignment_digest"])
        benchmarks.record_outcome(self.db, row["id"], "active", height=100, evidence={"proof_block_active": 100})
        retried = benchmarks.acknowledge(Database(self.db.dsn), row["id"], self.member, accepted["assignment_digest"])
        self.assertEqual(retried["handed_over_at"], handed_over["handed_over_at"])
        self.assertEqual(self.balance()["slots"], 0)
        self.assertEqual(self.balance()["collateral"], 50*TIG)

    def test_expiry_without_handover_frees_slot_but_holds_collateral(self):
        self.fund()
        row = self.reserve()
        benchmarks.mark_submitting(self.db, row["id"])
        accepted = benchmarks.accept(self.db, row["id"], "benchmark-one", {"whole": "assignment"}, actual_fee=0, evidence={"TIG": "accepted"})
        benchmarks.record_outcome(self.db, row["id"], "expired", height=100, evidence={"definitive_expiry": "fixture"})
        with self.assertRaises(Conflict): benchmarks.acknowledge(self.db, row["id"], self.member, accepted["assignment_digest"])
        self.assertEqual(self.balance()["slots"], 0)
        self.assertEqual(self.balance()["collateral"], 50*TIG)

    def test_duplicate_work_and_withdrawal_keys_conflict_on_changed_input(self):
        self.fund(amount=1000*TIG)
        one = self.reserve()
        self.assertEqual(self.reserve()["id"], one["id"])
        with self.assertRaises(Conflict): self.reserve(resource="GPU")
        withdrawal = withdrawals.request(self.db, self.member, "pay", TIG)
        self.assertEqual(withdrawals.request(self.db, self.member, "pay", TIG)["id"], withdrawal["id"])
        with self.assertRaises(Conflict): withdrawals.request(self.db, self.member, "pay", 2*TIG)
        with self.assertRaises(Conflict): withdrawals.request(self.db, self.member, "pay-two", TIG)

    def test_rolling_seven_days_enforced_from_paid_timestamp(self):
        self.fund()
        with self.db.transaction() as cursor:
            cursor.execute("UPDATE members SET last_paid_at=clock_timestamp()-interval '6 days 23 hours' WHERE id=%s", (self.member,))
        with self.assertRaises(Conflict): withdrawals.request(self.db, self.member, "pay", TIG)
        with self.db.transaction() as cursor:
            cursor.execute("UPDATE members SET last_paid_at=clock_timestamp()-interval '7 days' WHERE id=%s", (self.member,))
        withdrawals.request(self.db, self.member, "pay", TIG)

    def test_expired_offer_does_not_reserve_money(self):
        self.fund()
        self.expiry = datetime.now(timezone.utc) - timedelta(seconds=1)
        with self.assertRaises(Conflict): self.reserve()
        self.assertEqual(self.balance()["available"], 100*TIG)

    def test_database_enforces_append_only_and_balanced_entries(self):
        self.fund()
        for sql in ("UPDATE entries SET amount=amount+1", "DELETE FROM entries", "DELETE FROM journals",
                    "UPDATE accounts SET balance=balance+1", "DELETE FROM accounts"):
            with self.subTest(sql=sql), self.assertRaises(psycopg2.Error), self.db.transaction() as cursor:
                cursor.execute(sql)
        with self.assertRaises(psycopg2.Error), self.db.transaction() as cursor:
            identity = uuid.uuid4()
            cursor.execute("INSERT INTO journals(id,event_key,kind,fingerprint,details) VALUES (%s,'bad','bad','bad','{}')", (identity,))
            cursor.execute("INSERT INTO entries VALUES (%s,%s,'TIG',1)", (identity, members.available(self.member)))
        with self.assertRaises(psycopg2.Error), self.db.transaction() as cursor:
            identity = uuid.uuid4()
            cursor.execute("INSERT INTO journals(id,event_key,kind,fingerprint,details) VALUES (%s,'cross','bad','bad','{}')", (identity,))
            cursor.execute("INSERT INTO entries VALUES (%s,'external:custody:TIG','TIG',-1)", (identity,))
            cursor.execute("INSERT INTO entries VALUES (%s,'operator:custody:NATIVE','NATIVE',1)", (identity,))
        self.assertEqual(self.balance()["available"], 100*TIG)

    def test_cannot_extend_old_journal_or_reprice_reservation(self):
        self.fund()
        row = self.reserve()
        with self.assertRaises(psycopg2.Error), self.db.transaction() as cursor:
            cursor.execute("SELECT id FROM journals ORDER BY created_at LIMIT 1")
            identity = cursor.fetchone()["id"]
            cursor.execute("INSERT INTO entries VALUES (%s,'operator:custody:TIG','TIG',1)", (identity,))
        with self.assertRaises(psycopg2.Error), self.db.transaction() as cursor:
            cursor.execute("UPDATE reservations SET amount=1 WHERE id=%s", (row["id"],))

    def test_switching_constraints_to_immediate_cannot_leave_unbalanced_journal(self):
        self.fund()
        with self.assertRaises(psycopg2.Error), self.db.transaction() as cursor:
            identity = ledger.post(cursor, "balanced", "test", [(members.available(self.member), -1), (members.available(self.other), 1)])
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute("INSERT INTO entries VALUES (%s,'operator:custody:TIG','TIG',1)", (identity,))

    def test_withdrawal_destination_and_amount_are_frozen(self):
        self.fund()
        row = withdrawals.request(self.db, self.member, "pay", TIG)
        with self.assertRaises(psycopg2.Error), self.db.transaction() as cursor:
            cursor.execute("UPDATE withdrawals SET recipient=%s WHERE id=%s", (OTHER, row["id"]))
        with self.assertRaises(psycopg2.Error), self.db.transaction() as cursor:
            cursor.execute("UPDATE withdrawals SET amount=1 WHERE id=%s", (row["id"],))

    def test_correction_is_new_balanced_reversal_and_cannot_repeat(self):
        self.fund()
        with self.db.transaction() as cursor:
            identity = ledger.post(cursor, "manual", "test", [(members.available(self.member), -3), (members.available(self.other), 3)])
        with self.db.transaction() as cursor:
            reversal = ledger.reverse(cursor, identity, "reverse", "operator", "test correction")
        with self.db.transaction() as cursor:
            self.assertEqual(ledger.reverse(cursor, identity, "reverse", "operator", "test correction"), reversal)
        self.assertEqual(self.balance()["available"], 100*TIG)
        with self.assertRaises(Conflict), self.db.transaction() as cursor:
            ledger.reverse(cursor, identity, "another-reversal", "operator", "duplicate")

    def test_migration_replay_preserves_existing_funds(self):
        self.fund(amount=5*TIG)
        Database(self.db.dsn).migrate()
        self.assertEqual(self.balance()["available"], 5*TIG)
