"""Stable wallet identities and audited collateral settings.

These are internal services. The API must verify the corresponding wallet or
operator authority before calling a mutation; machine names never identify a
member. Registering a wallet is only called after signature verification.
"""

import re
import uuid

from . import ledger
from .database import lock
from .money import Conflict, FundsError, multiplier


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise FundsError("invalid Ethereum wallet address")
    if int(value, 16) == 0:
        raise FundsError("zero address cannot identify a member or custody wallet")
    return value.lower()


def available(member_id):
    return f"member:{member_id}:available"


def register_verified(cursor, wallet):
    wallet = address(wallet)
    lock(cursor, "wallet:" + wallet)
    cursor.execute("SELECT * FROM members WHERE wallet=%s", (wallet,))
    row = cursor.fetchone()
    if row:
        return dict(row)
    identity = uuid.uuid4()
    cursor.execute("INSERT INTO members(id,wallet,withdrawal_wallet) VALUES (%s,%s,%s) RETURNING *",
                   (identity, wallet, wallet))
    row = dict(cursor.fetchone())
    ledger.account(cursor, available(identity), "member")
    return row


def member_lock(cursor, member_id):
    cursor.execute("SELECT * FROM members WHERE id=%s FOR UPDATE", (member_id,))
    row = cursor.fetchone()
    if not row:
        raise FundsError("unknown member")
    return dict(row)


def set_multiplier(database, member_id, value, *, actor, reason, event_key):
    value = multiplier(value)
    if not actor or not reason or not event_key:
        raise FundsError("multiplier changes require an actor, reason and event key")
    with database.transaction() as cursor:
        lock(cursor, "multiplier-event:" + event_key)
        member = member_lock(cursor, member_id)
        cursor.execute("SELECT * FROM multiplier_changes WHERE event_key=%s", (event_key,))
        previous = cursor.fetchone()
        if previous:
            if (str(previous["member_id"]), previous["new_value"], previous["actor"], previous["reason"]) != (
                    str(member_id), value, actor, reason):
                raise Conflict("multiplier event key was reused")
            return dict(previous)
        revision = member["multiplier_revision"] + 1
        cursor.execute("""INSERT INTO multiplier_changes
            (member_id,revision,old_value,new_value,actor,reason,event_key)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (member_id, revision, member["multiplier"], value, actor, reason, event_key))
        result = dict(cursor.fetchone())
        cursor.execute("UPDATE members SET multiplier=%s, multiplier_revision=%s WHERE id=%s",
                       (value, revision, member_id))
        return result


def balances(database, member_id):
    with database.transaction() as cursor:
        cursor.execute("SELECT id,wallet,withdrawal_wallet,multiplier,multiplier_revision,last_paid_at FROM members WHERE id=%s",
                       (member_id,))
        result = cursor.fetchone()
        if not result:
            raise FundsError("unknown member")
        cursor.execute("SELECT balance FROM accounts WHERE id=%s", (available(member_id),))
        result["available"] = int(cursor.fetchone()["balance"])
        cursor.execute("""SELECT coalesce(sum(amount),0) AS collateral, count(*) FILTER (WHERE slot_held) AS slots
            FROM reservations WHERE member_id=%s AND collateral_outcome IS NULL""", (member_id,))
        result.update({key: int(value) for key, value in cursor.fetchone().items()})
        cursor.execute("""SELECT coalesce(sum(amount),0) AS pending FROM withdrawals
            WHERE member_id=%s AND state IN ('requested','approved','uncertain')""", (member_id,))
        result["pending_withdrawals"] = int(cursor.fetchone()["pending"])
        return dict(result)
