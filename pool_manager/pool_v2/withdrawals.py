"""Member withdrawal reservations. Transfer review/sending is a separate process."""

from datetime import timedelta
import uuid

from . import ledger
from .members import available, member_lock
from .money import Conflict, FundsError, units


def pending(identity):
    return f"withdrawal:{identity}"


def request(database, member_id, request_key, amount):
    units(amount, positive=True)
    if not request_key or len(request_key) > 128:
        raise FundsError("withdrawal requires a bounded idempotency key")
    with database.transaction() as cursor:
        member = member_lock(cursor, member_id)
        cursor.execute("SELECT * FROM withdrawals WHERE member_id=%s AND request_key=%s", (member_id, request_key))
        existing = cursor.fetchone()
        if existing:
            if existing["amount"] != amount:
                raise Conflict("withdrawal key was reused with a different amount")
            return dict(existing)
        cursor.execute("SELECT clock_timestamp() AS now")
        now = cursor.fetchone()["now"]
        if member["last_paid_at"] and now < member["last_paid_at"] + timedelta(days=7):
            raise Conflict("seven days must pass after the previous successful withdrawal")
        cursor.execute("SELECT 1 FROM withdrawals WHERE member_id=%s AND state IN ('requested','approved','uncertain')",
                       (member_id,))
        if cursor.fetchone():
            raise Conflict("member already has a pending withdrawal")
        identity = uuid.uuid4()
        ledger.account(cursor, pending(identity), "withdrawal")
        ledger.post(cursor, f"withdrawal:{identity}:reserve", "withdrawal_reservation",
                    [(available(member_id), -amount), (pending(identity), amount)])
        cursor.execute("""INSERT INTO withdrawals(id,member_id,request_key,amount,recipient)
            VALUES (%s,%s,%s,%s,%s) RETURNING *""", (identity, member_id, request_key, amount, member["withdrawal_wallet"]))
        return dict(cursor.fetchone())
