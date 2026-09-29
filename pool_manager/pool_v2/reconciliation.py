"""Recover owned precommits, sampled nonces and confirmed TIG outcomes.

Only stored complete block observations drive this adapter. A disappeared
record is not an expiry signal, and a proof POST is not an activation signal.
"""

from . import benchmarks, member_protocol, submissions, qualifiers
from .block_observer import BlockStore
from .money import Conflict
from .protocol import ProtocolDataError, _index, _integer


def player_feed(observation, player_id):
    if player_id in observation["players"]:
        feed = observation["players"][player_id]
    elif observation.get("pool_player_id") == player_id:
        feed = observation.get("pool_pending")
    else:
        feed = None
    if not isinstance(feed, dict):
        raise ProtocolDataError("this block did not capture the pool's pending/active benchmark feed")
    try:
        indexed = {name: _index(feed[name], key, name) for name,key in
                   (("precommits","benchmark_id"),("benchmarks","id"),("proofs","benchmark_id"),("frauds","benchmark_id"))}
        for value in indexed["precommits"].values():
            if value["settings"]["player_id"] != player_id:
                raise ProtocolDataError("pool precommit feed contains another owner")
        return indexed
    except (KeyError,TypeError,AttributeError) as exc:
        raise ProtocolDataError("pool benchmark feed is incomplete") from exc


def confirmed(record, height):
    if not record or record.get("state") is None:
        return False
    value = _integer(record["state"]["block_confirmed"], "protocol confirmation", 1)
    if value > height:
        raise ProtocolDataError("protocol record was confirmed after the observed block")
    return True


def reconcile_block(database, block_id, player_id, *, artifact_origin=None):
    observation,snapshot = BlockStore(database).read(block_id)
    feed = player_feed(observation,player_id)
    evidence = {"block_id":snapshot.block_id,"height":snapshot.height,"source":"archived-pool-feed"}
    with database.transaction() as cursor:
        cursor.execute("SELECT id FROM protocol_outbox WHERE kind='precommit' AND state='uncertain' ORDER BY created_at,id")
        uncertain = cursor.fetchall()
    for intent in uncertain:
        submissions.recover_precommit(database,intent["id"],list(feed["precommits"].values()),evidence=evidence)
    with database.transaction() as cursor:
        cursor.execute("""SELECT r.id,p.benchmark_id FROM reservations r JOIN precommit_receipts p ON p.reservation_id=r.id
            WHERE r.state='uncertain' ORDER BY r.id""")
        unpublished = cursor.fetchall()
    for row in unpublished:
        precommit = feed["precommits"].get(row["benchmark_id"])
        if confirmed(precommit,snapshot.height):
            submissions.publish_assignment(database,row["id"],precommit,evidence=evidence,artifact_origin=artifact_origin)
    with database.transaction() as cursor:
        cursor.execute("SELECT * FROM reservations WHERE state='accepted' ORDER BY id")
        owned = cursor.fetchall()
    for row in owned:
        identity=row["benchmark_id"]
        benchmark,proof,fraud=(feed[name].get(identity) for name in ("benchmarks","proofs","frauds"))
        if confirmed(benchmark,snapshot.height):
            with database.transaction() as cursor:
                cursor.execute("SELECT payload FROM benchmark_payloads WHERE benchmark_id=%s AND kind='results'",(identity,))
                result=cursor.fetchone()
            if not result or not row["handed_over_at"] or benchmark["details"].get("merkle_root") != result["payload"]["merkle_root"]:
                raise ProtocolDataError("TIG benchmark has no matching member-owned root and handover")
            submissions.confirm_payload(database,row["id"],"results",evidence=evidence)
            if benchmark["details"].get("stopped") is False:
                member_protocol.sampled(database,identity,benchmark["details"].get("sampled_nonces"),evidence=evidence)
        if confirmed(fraud,snapshot.height):
            # This is the confirmed pre-activation verification failure feed.
            # Later nonce arbitration is a separate X+2 settlement input.
            submissions.confirm_payload(database,row["id"],"proofs",evidence=evidence)
            benchmarks.record_outcome(database,row["id"],"verification_failed",height=fraud["state"]["block_confirmed"],evidence=evidence)
        elif confirmed(proof,snapshot.height):
            submissions.confirm_payload(database,row["id"],"proofs",evidence=evidence)
            if identity in snapshot.precommits:
                benchmarks.record_outcome(database,row["id"],"active",height=proof["details"]["block_active"],evidence=evidence)
    return evidence


def reconcile_pending(database, player_id, *, limit=100, artifact_origin=None):
    """Replay missed blocks in height order, without letting one hold stop others."""
    with database.transaction() as cursor:
        cursor.execute("""SELECT b.id,b.height FROM observed_blocks b LEFT JOIN reconciled_blocks r ON r.block_id=b.id
            LEFT JOIN reconciliation_queue q ON q.block_id=b.id WHERE r.block_id IS NULL
            ORDER BY coalesce(q.last_attempted_at,to_timestamp(0)),b.height LIMIT %s""", (limit,))
        blocks=cursor.fetchall()
    completed,held=[],[]
    for block in blocks:
        try:
            reconcile_block(database,block["id"],player_id,artifact_origin=artifact_origin)
            qualifiers.credit_block(database,block["id"],player_id)
        except (ProtocolDataError,Conflict) as error:
            held.append({"block_id":block["id"],"height":block["height"],"reason":str(error)})
            with database.transaction() as cursor:
                cursor.execute("""INSERT INTO reconciliation_queue(block_id,last_error) VALUES (%s,%s)
                    ON CONFLICT(block_id) DO UPDATE SET last_attempted_at=clock_timestamp(),last_error=excluded.last_error""",
                    (block["id"],str(error)))
            continue
        with database.transaction() as cursor:
            cursor.execute("INSERT INTO reconciled_blocks(block_id,player_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                           (block["id"],player_id))
        completed.append(block["id"])
    return {"completed":completed,"held":held}
