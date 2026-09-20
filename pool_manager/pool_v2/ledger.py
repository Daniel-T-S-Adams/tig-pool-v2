"""Append-only, balanced journal. All public methods use the caller's transaction."""

from collections import defaultdict
import hashlib
import json
import uuid

from psycopg2.extras import Json

from .database import lock
from .money import Conflict, FundsError, InsufficientFunds


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def account(cursor, account_id, kind, asset="TIG", location="custody"):
    cursor.execute("INSERT INTO accounts(id, kind, asset, location) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                   (account_id, kind, asset, location))
    cursor.execute("SELECT kind, asset, location FROM accounts WHERE id=%s", (account_id,))
    if dict(cursor.fetchone()) != {"kind": kind, "asset": asset, "location": location}:
        raise Conflict("account identity has a different kind or asset")
    return account_id


def post(cursor, event_key, kind, movements, details=None, reverses=None):
    """Lock balances in stable order and post one immutable, idempotent journal.

    A retry must describe the exact same movement and metadata. Corrections are
    new reversing journals, never edits. Zero-money lifecycle events live in
    their domain tables instead of manufacturing zero-value journal entries.
    """
    if not event_key or not kind:
        raise FundsError("journal requires an event key and kind")
    amounts = defaultdict(int)
    for account_id, amount in movements:
        if type(amount) is not int:
            raise FundsError("journal amounts must be integer token units")
        amounts[account_id] += amount
    amounts = {key: value for key, value in amounts.items() if value}
    if not amounts:
        raise FundsError("a monetary journal requires nonzero entries")
    details = {} if details is None else details
    digest = fingerprint({"kind": kind, "amounts": amounts, "details": details,
                          "reverses": str(reverses) if reverses else None})
    lock(cursor, "journal:" + event_key)
    cursor.execute("SELECT id, fingerprint FROM journals WHERE event_key=%s", (event_key,))
    existing = cursor.fetchone()
    if existing:
        if existing["fingerprint"] != digest:
            raise Conflict("journal event key was reused with different inputs")
        return existing["id"]
    if reverses:
        lock(cursor, "reversal:" + str(reverses))
        cursor.execute("SELECT id FROM journals WHERE reverses=%s", (reverses,))
        if cursor.fetchone():
            raise Conflict("journal has already been reversed")
    cursor.execute("SELECT id, asset, kind, balance FROM accounts WHERE id=ANY(%s) ORDER BY id FOR UPDATE",
                   (sorted(amounts),))
    accounts = {row["id"]: row for row in cursor.fetchall()}
    if accounts.keys() != amounts.keys():
        raise FundsError("unknown journal account")
    totals = defaultdict(int)
    for key, amount in amounts.items():
        row = accounts[key]
        totals[row["asset"]] += amount
        if row["kind"] != "external" and int(row["balance"]) + amount < 0:
            raise InsufficientFunds(f"insufficient available {row['asset']} funds")
    if any(totals.values()):
        raise FundsError("journal must balance for each asset")
    identity = uuid.uuid4()
    cursor.execute("""INSERT INTO journals(id,event_key,kind,fingerprint,details,reverses)
        VALUES (%s,%s,%s,%s,%s,%s)""", (identity, event_key, kind, digest, Json(details), reverses))
    for key in sorted(amounts):
        cursor.execute("INSERT INTO entries(journal_id,account_id,asset,amount) VALUES (%s,%s,%s,%s)",
                       (identity, key, accounts[key]["asset"], amounts[key]))
    return identity


def reverse(cursor, journal_id, event_key, actor, reason):
    if not actor or not reason:
        raise FundsError("a correction requires an actor and reason")
    cursor.execute("SELECT account_id, amount FROM entries WHERE journal_id=%s", (journal_id,))
    movements = [(row["account_id"], -int(row["amount"])) for row in cursor.fetchall()]
    if not movements:
        raise FundsError("unknown journal to reverse")
    return post(cursor, event_key, "correction", movements,
                {"actor": actor, "reason": reason}, reverses=journal_id)


def audit(cursor):
    cursor.execute("""SELECT a.id, a.balance, coalesce(sum(e.amount),0) AS reconstructed
        FROM accounts a LEFT JOIN entries e ON e.account_id=a.id
        GROUP BY a.id HAVING a.balance <> coalesce(sum(e.amount),0)""")
    return [dict(row) for row in cursor.fetchall()]


def backing(cursor, asset="TIG", location="custody"):
    cursor.execute("""SELECT coalesce(sum(balance),0) AS amount FROM accounts
        WHERE asset=%s AND location=%s AND kind <> 'external'""", (asset, location))
    return int(cursor.fetchone()["amount"])
