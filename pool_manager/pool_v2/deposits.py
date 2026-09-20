"""Confirmed incoming transfers, attribution and separate operator funding."""

from psycopg2.extras import Json

from . import ledger
from .chain import ConfirmedTransfer
from .database import lock
from .members import available, member_lock
from .money import Conflict, FundsError


def save_transfer(cursor, transfer):
    if not isinstance(transfer, ConfirmedTransfer):
        raise FundsError("only the chain adapter's verified transfer can be recorded")
    lock(cursor, "transfer:" + transfer.event_id)
    values = dict(event_id=transfer.event_id, chain_id=transfer.network.chain_id, token=transfer.network.token,
        tx_hash=transfer.tx_hash, log_index=transfer.log_index, block_number=transfer.block_number,
        block_hash=transfer.block_hash, block_timestamp=transfer.block_timestamp, sender=transfer.sender,
        recipient=transfer.recipient, amount=transfer.amount)
    cursor.execute("SELECT * FROM transfers WHERE event_id=%s", (transfer.event_id,))
    previous = cursor.fetchone()
    if previous:
        if any(previous[key] != value for key, value in values.items()):
            raise Conflict("a previously recorded transfer changed; reconciliation required")
        return False
    cursor.execute("""INSERT INTO transfers(event_id,chain_id,token,tx_hash,log_index,block_number,
        block_hash,block_timestamp,sender,recipient,amount,evidence)
        VALUES (%(event_id)s,%(chain_id)s,%(token)s,%(tx_hash)s,%(log_index)s,%(block_number)s,
        %(block_hash)s,%(block_timestamp)s,%(sender)s,%(recipient)s,%(amount)s,%(evidence)s)""",
        {**values, "evidence": Json(transfer.evidence)})
    return True


def _attribute(cursor, transfer, destination, actor, evidence):
    cursor.execute("SELECT destination FROM transfer_attributions WHERE event_id=%s", (transfer.event_id,))
    previous = cursor.fetchone()
    if previous:
        if previous["destination"] != destination:
            raise Conflict("deposit has already been attributed elsewhere")
        return destination
    ledger.post(cursor, "attribute:" + transfer.event_id, "deposit_attribution",
        [("unattributed:TIG", -transfer.amount), (destination, transfer.amount)],
        {"actor": actor, "evidence": evidence})
    cursor.execute("INSERT INTO transfer_attributions(event_id,destination,actor,evidence) VALUES (%s,%s,%s,%s)",
                   (transfer.event_id, destination, actor, Json(evidence)))
    return destination


def receive(database, transfer):
    if transfer.recipient != transfer.network.custody or transfer.sender == transfer.network.custody:
        raise FundsError("expected an external incoming transfer to custody")
    with database.transaction() as cursor:
        if save_transfer(cursor, transfer):
            ledger.post(cursor, "receipt:" + transfer.event_id, "custody_receipt",
                [("external:custody:TIG", -transfer.amount), ("unattributed:TIG", transfer.amount)])
        cursor.execute("SELECT destination FROM transfer_attributions WHERE event_id=%s", (transfer.event_id,))
        attributed = cursor.fetchone()
        if attributed:
            return attributed["destination"]
        cursor.execute("SELECT id FROM members WHERE wallet=%s", (transfer.sender,))
        member = cursor.fetchone()
        if member:
            member_lock(cursor, member["id"])
            return _attribute(cursor, transfer, available(member["id"]), "verified-source",
                              {"wallet": transfer.sender})
        return "unattributed:TIG"


def attribute_reviewed(database, transfer, *, actor, evidence, member_id=None, operator=False):
    """Operator-only resolution of an already observed, unattributed receipt.

    The HTTP layer must not accept a public transaction hash as ownership proof.
    evidence records the operator's independent attribution investigation.
    """
    if not actor or not evidence or (bool(member_id) == bool(operator)):
        raise FundsError("choose one verified attribution destination and supply operator evidence")
    if transfer.recipient != transfer.network.custody or transfer.sender == transfer.network.custody:
        raise FundsError("expected incoming custody transfer")
    with database.transaction() as cursor:
        lock(cursor, "transfer:" + transfer.event_id)
        cursor.execute("SELECT 1 FROM transfers WHERE event_id=%s", (transfer.event_id,))
        if not cursor.fetchone():
            raise FundsError("receipt must be observed before operator attribution")
        # Verify supplied immutable facts too; this is a replay, not a new deposit.
        save_transfer(cursor, transfer)
        if member_id:
            member_lock(cursor, member_id)
        destination = available(member_id) if member_id else "operator:custody:TIG"
        return _attribute(cursor, transfer, destination, actor, evidence)
