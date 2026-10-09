"""Atomic collateral/slot ownership and recoverable benchmark handover.

No function here calls TIG. The submission worker must commit mark_submitting
before its first network write. An uncertain operation can only be resolved
with protocol evidence, never by expiration of a worker lease.
"""

from datetime import datetime
from contextlib import nullcontext
import json
import uuid

from psycopg2.extras import Json

from . import ledger
from .database import lock
from .members import available, member_lock
from .money import Conflict, FundsError, collateral, units


OPERATOR_FEES = "operator:protocol:TIG"


def initialize_accounts(database):
    with database.transaction() as cursor:
        for asset in ("TIG", "NATIVE"):
            ledger.account(cursor, f"operator:custody:{asset}", "operator", asset)
            ledger.account(cursor, f"external:custody:{asset}", "external", asset)
        ledger.account(cursor, OPERATOR_FEES, "operator", location="protocol")
        ledger.account(cursor, "external:protocol:TIG", "external", location="protocol")
        ledger.account(cursor, "unattributed:TIG", "unattributed")


def held(reservation_id):
    return f"collateral:{reservation_id}"


def committed_fee(reservation_id):
    return f"operator:commitment:{reservation_id}"


def event(cursor, identity, kind, details):
    key = f"reservation:{identity}:{kind}"
    cursor.execute("SELECT details FROM reservation_events WHERE event_key=%s", (key,))
    existing = cursor.fetchone()
    if existing:
        if ledger.fingerprint(existing["details"]) != ledger.fingerprint(details):
            raise Conflict("reservation event was repeated with different evidence")
        return
    cursor.execute("INSERT INTO reservation_events(event_key,reservation_id,kind,details) VALUES (%s,%s,%s,%s)",
                   (key, identity, kind, Json(details)))


def _locked(cursor, identity, *, budget=False):
    if budget:
        lock(cursor, "operator:protocol-budget")
    cursor.execute("SELECT member_id FROM reservations WHERE id=%s", (identity,))
    row = cursor.fetchone()
    if not row:
        raise FundsError("unknown reservation")
    member_lock(cursor, row["member_id"])
    cursor.execute("SELECT * FROM reservations WHERE id=%s FOR UPDATE", (identity,))
    return dict(cursor.fetchone())


def _row(cursor, identity):
    cursor.execute("SELECT * FROM reservations WHERE id=%s", (identity,))
    return dict(cursor.fetchone())


def reserve(database, member_id, request_key, *, creation_round, resource, selection, payload,
            fee_limit, offer_expires_at, _cursor=None):
    if resource not in ("CPU", "GPU") or not request_key or len(request_key) > 128:
        raise FundsError("request requires a key and exactly one CPU or GPU offer")
    units(creation_round, positive=True)
    units(fee_limit)
    if not isinstance(offer_expires_at, datetime) or offer_expires_at.utcoffset() is None:
        raise FundsError("offer expiry must be an absolute timestamp")
    try:
        counts = [track["num_bundles"] for track in payload["track_settings"].values()]
    except (KeyError, TypeError, AttributeError) as exc:
        raise FundsError("precommit requires all proposed track settings") from exc
    request_hash = ledger.fingerprint({"round": creation_round, "resource": resource,
        "selection": selection, "payload": payload, "fee_limit": fee_limit,
        "offer_expires_at": offer_expires_at.isoformat()})
    with (database.transaction() if _cursor is None else nullcontext(_cursor)) as cursor:
        # All budget-using paths use budget -> member -> reservation -> accounts.
        lock(cursor, "operator:protocol-budget")
        from .controls import blocked
        member = member_lock(cursor, member_id)
        cursor.execute("SELECT * FROM reservations WHERE member_id=%s AND request_key=%s", (member_id, request_key))
        existing = cursor.fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise Conflict("work request key was reused with different inputs")
            return dict(existing)
        if blocked(database,cursor=cursor):
            raise Conflict('new benchmark reservations are paused or awaiting custody reconciliation')
        cursor.execute("SELECT clock_timestamp() AS now")
        if offer_expires_at <= cursor.fetchone()["now"]:
            raise Conflict("compute offer has expired")
        cursor.execute("SELECT count(*) AS slots FROM reservations WHERE member_id=%s AND slot_held", (member_id,))
        if cursor.fetchone()["slots"] >= 2:
            raise Conflict("member already occupies both benchmark slots")
        base, amount = collateral(counts, member["multiplier"])
        from . import pilot
        pilot.check(database, cursor, member=member, resource=resource, amount=amount,
                    fee_limit=fee_limit, payload=payload)
        identity = uuid.uuid4()
        ledger.account(cursor, held(identity), "collateral")
        ledger.account(cursor, committed_fee(identity), "operator_commitment", location="protocol")
        movements = [(available(member_id), -amount), (held(identity), amount),
                     (OPERATOR_FEES, -fee_limit), (committed_fee(identity), fee_limit)]
        if amount or fee_limit:
            ledger.post(cursor, f"reservation:{identity}:reserve", "benchmark_reservation", movements,
                        {"member_id": str(member_id), "multiplier_revision": member["multiplier_revision"]})
        cursor.execute("""INSERT INTO reservations
            (id,member_id,request_key,request_hash,creation_round,resource,base_amount,multiplier,
             multiplier_revision,amount,selection,payload,offer_expires_at,fee_limit)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (identity, member_id, request_key, request_hash, creation_round, resource, base,
             member["multiplier"], member["multiplier_revision"], amount, Json(selection), Json(payload),
             offer_expires_at, fee_limit))
        result = dict(cursor.fetchone())
        cursor.execute("UPDATE reservations SET payload_text=%s WHERE id=%s", (ledger.canonical(payload), identity))
        result["payload_text"] = ledger.canonical(payload)
        event(cursor, identity, "reserved", {"request_hash": request_hash})
        return result


def mark_submitting(database, identity, *, _cursor=None):
    """Commit uncertainty before an external call; this is not a retry lease."""
    with (database.transaction() if _cursor is None else nullcontext(_cursor)) as cursor:
        row = _locked(cursor, identity, budget=True)
        if row["state"] != "reserved":
            raise Conflict("precommit may already have been sent; reconcile before proceeding")
        cursor.execute("SELECT clock_timestamp() AS now")
        if row["offer_expires_at"] <= cursor.fetchone()["now"]:
            raise Conflict("offer expired before submission")
        from . import pilot
        member = member_lock(cursor, row['member_id'])
        pilot.check(database, cursor, member=member, resource=row['resource'], amount=int(row['amount']),
                    fee_limit=int(row['fee_limit']), payload=row['payload'], reservation_id=identity)
        cursor.execute("UPDATE reservations SET state='uncertain' WHERE id=%s", (identity,))
        payload = json.loads(row["payload_text"]) if row["payload_text"] else row["payload"]
        event(cursor, identity, "potentially_sent", {"payload_hash": ledger.fingerprint(payload)})
        return payload


def _fees(cursor, row, actual):
    units(actual)
    limit = int(row["fee_limit"])
    if actual > limit:
        raise Conflict("actual protocol fee exceeds its committed budget; reconcile explicitly")
    if limit:
        ledger.post(cursor, f"reservation:{row['id']}:fee", "operator_submission_cost",
                    [(committed_fee(row["id"]), -limit), (OPERATOR_FEES, limit - actual),
                     ("external:protocol:TIG", actual)], {"actual": actual})
    cursor.execute("UPDATE reservations SET fee_actual=%s WHERE id=%s", (actual, row["id"]))


def accept(database, identity, benchmark_id, assignment, *, actual_fee, evidence, _cursor=None):
    if not benchmark_id or not isinstance(assignment, dict) or not assignment or not evidence:
        raise FundsError("acceptance requires benchmark identity, complete assignment and evidence")
    digest = ledger.fingerprint(assignment)
    with (database.transaction() if _cursor is None else nullcontext(_cursor)) as cursor:
        row = _locked(cursor, identity, budget=True)
        if row["benchmark_id"]:
            if (row["benchmark_id"], row["assignment_digest"], row["fee_actual"]) != (benchmark_id, digest, actual_fee):
                raise Conflict("benchmark acceptance differs from recorded assignment")
            return row
        if row["state"] != "uncertain":
            raise Conflict("acceptance requires a potentially sent submission")
        _fees(cursor, row, actual_fee)
        cursor.execute("""UPDATE reservations SET state='accepted', benchmark_id=%s, assignment=%s,
            assignment_digest=%s, assignment_payload=%s WHERE id=%s""",
            (benchmark_id, Json(assignment), digest, ledger.canonical(assignment), identity))
        event(cursor, identity, "accepted", {"benchmark_id": benchmark_id, "digest": digest, "evidence": evidence})
        return _row(cursor, identity)


def release_unstarted(database, identity, *, rejected=False, actual_fee=0, evidence, _cursor=None):
    if not evidence:
        raise FundsError("cancellation/rejection requires durable evidence")
    state = "rejected" if rejected else "cancelled"
    with (database.transaction() if _cursor is None else nullcontext(_cursor)) as cursor:
        row = _locked(cursor, identity, budget=True)
        if row["state"] == state:
            if row["fee_actual"] != actual_fee:
                raise Conflict("repeated release has a different actual fee")
            return row
        if row["state"] != ("uncertain" if rejected else "reserved"):
            raise Conflict("only definitive rejection or a proven-unsent intent can release immediately")
        if not rejected and actual_fee:
            raise Conflict("an unsent cancellation cannot have a submission charge")
        _fees(cursor, row, actual_fee)
        amount = int(row["amount"])
        if amount:
            ledger.post(cursor, f"reservation:{identity}:return", "collateral_return",
                        [(held(identity), -amount), (available(row["member_id"]), amount)])
        cursor.execute("UPDATE reservations SET state=%s, slot_held=false, collateral_outcome='returned' WHERE id=%s",
                       (state, identity))
        event(cursor, identity, state, evidence)
        return _row(cursor, identity)


def release_unhanded(database, identity, *, actor, reason, event_key):
    """Return member collateral when an accepted assignment was never handed over."""
    if not actor or not reason or not event_key:
        raise FundsError("unhanded collateral return requires an operator, reason and event key")
    with database.transaction() as cursor:
        row = _locked(cursor, identity, budget=True)
        evidence = {"actor": actor, "reason": reason, "event_key": event_key,
                    "benchmark_id": row["benchmark_id"], "offer_expires_at": row["offer_expires_at"].isoformat()}
        if row["state"] == "cancelled" and row["collateral_outcome"] == "returned":
            event(cursor, identity, "unhanded_returned", evidence)
            return row
        if row["state"] != "accepted" or not row["benchmark_id"] or not row["slot_held"]:
            raise Conflict("only an accepted, collateral-backed assignment can be returned")
        if row["handed_over_at"] is not None:
            raise Conflict("member confirmed handover; collateral cannot be returned as unhanded")
        from .controls import paused
        if not paused(database, cursor=cursor):
            raise Conflict("pause new work before returning unhanded collateral")
        cursor.execute("SELECT clock_timestamp() AS now")
        if row["offer_expires_at"] > cursor.fetchone()["now"]:
            raise Conflict("member offer has not expired")
        cursor.execute("""SELECT 1 FROM benchmark_payloads WHERE benchmark_id=%s
            UNION ALL SELECT 1 FROM benchmark_progress WHERE benchmark_id=%s LIMIT 1""",
            (row["benchmark_id"], row["benchmark_id"]))
        if cursor.fetchone():
            raise Conflict("benchmark already has member results or progress evidence")
        amount = int(row["amount"])
        if amount:
            ledger.post(cursor, f"reservation:{identity}:unhanded-return", "collateral_return",
                        [(held(identity), -amount), (available(row["member_id"]), amount)], evidence)
        cursor.execute("""UPDATE reservations SET state='cancelled',slot_held=false,
            collateral_outcome='returned' WHERE id=%s""", (identity,))
        event(cursor, identity, "unhanded_returned", evidence)
        return _row(cursor, identity)


def acknowledge(database, identity, member_id, digest):
    with database.transaction() as cursor:
        row = _locked(cursor, identity)
        if str(row["member_id"]) != str(member_id) or digest != row["assignment_digest"]:
            raise Conflict("assignment owner or digest does not match")
        if row["handed_over_at"]:
            return row  # A lost response never erases an already committed handover.
        if row["state"] != "accepted":
            raise Conflict("assignment is not available for first handover")
        cursor.execute("UPDATE reservations SET handed_over_at=clock_timestamp() WHERE id=%s", (identity,))
        event(cursor, identity, "handed_over", {"member_id": str(member_id), "digest": digest})
        return _row(cursor, identity)


def record_outcome(database, identity, state, *, height, evidence, _cursor=None):
    if state not in ("active", "verification_failed", "expired") or not evidence:
        raise FundsError("a definitive protocol outcome and evidence are required")
    units(height, positive=True)
    with (nullcontext(_cursor) if _cursor is not None else database.transaction()) as cursor:
        row = _locked(cursor, identity)
        if row["state"] == state:
            return row
        if row["state"] != "accepted":
            raise Conflict("outcome conflicts with benchmark state")
        if state != "expired" and row["handed_over_at"] is None:
            raise Conflict("results cannot be submitted without confirmed handover")
        cursor.execute("""UPDATE reservations SET state=%s, slot_held=false,
            active_at_height=%s WHERE id=%s""", (state, height if state == "active" else None, identity))
        event(cursor, identity, state, {"height": height, "evidence": evidence})
        return _row(cursor, identity)
