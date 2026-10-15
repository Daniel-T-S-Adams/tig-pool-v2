"""Expiring member offers and a queue that atomically creates one reservation."""

from datetime import timedelta
import uuid

from psycopg2.extras import Json

from . import benchmarks, ledger, references as network_references
from .block_observer import BlockStore
from .database import lock
from .members import member_lock
from .money import Conflict, FundsError, InsufficientFunds
from .protocol import ProtocolDataError
from .selection import COMPUTE_FAMILIES, NoCompatibleWork, choose, references


def _offer(resource, compute_type, capacity):
    """Validate one compute offer; capacity may span member-managed machines."""

    if resource not in ("CPU", "GPU") or COMPUTE_FAMILIES.get(compute_type) != resource:
        raise FundsError("offer must specify one compatible CPU or GPU verification compute type")
    if not isinstance(capacity, dict) or set(capacity) != {"workers"} or type(capacity["workers"]) is not int or not 1 <= capacity["workers"] <= 4096:
        raise FundsError("capacity requires a positive worker count up to 4096")
    return {"resource": resource, "compute_type": compute_type, "capacity": capacity}


def create(database, member_id, request_key, *, resource, compute_type, capacity, ttl_seconds=60):
    offer = _offer(resource, compute_type, capacity)
    if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128 or not 5 <= ttl_seconds <= 600:
        raise FundsError("invalid request key or configured offer lifetime")
    with database.transaction() as cursor:
        member_lock(cursor, member_id)
        cursor.execute("SELECT * FROM work_requests WHERE member_id=%s AND request_key=%s", (member_id, request_key))
        row = cursor.fetchone()
        digest = ledger.fingerprint(offer)
        if row:
            if row["offer_hash"] != digest:
                raise Conflict("work request key was reused for a different compute offer")
            return dict(row)
        cursor.execute("""INSERT INTO work_requests(id,member_id,request_key,offer_hash,offer,expires_at)
            VALUES (%s,%s,%s,%s,%s,clock_timestamp()+%s) RETURNING *""",
            (uuid.uuid4(), member_id, request_key, digest, Json(offer), timedelta(seconds=ttl_seconds)))
        return dict(cursor.fetchone())


def get(database, identity, member_id):
    with database.transaction() as cursor:
        cursor.execute("""UPDATE work_requests SET state='expired' WHERE id=%s AND member_id=%s
            AND state='queued' AND expires_at<=clock_timestamp()""", (identity, member_id))
        cursor.execute("""SELECT w.*,r.benchmark_id,r.state AS benchmark_state FROM work_requests w
            LEFT JOIN reservations r ON r.id=w.reservation_id WHERE w.id=%s AND w.member_id=%s""", (identity, member_id))
        row = cursor.fetchone()
        if not row:
            raise FundsError("unknown member work request")
        result = dict(row)
        result["request_state"] = result["state"]
        if result["benchmark_state"] in ("active", "expired", "verification_failed", "rejected", "cancelled"):
            result["state"] = result["benchmark_state"]
        elif result["benchmark_id"]:
            result["state"] = "assigned"
        return result


def refresh(database, identity, member_id, ttl_seconds=60):
    with database.transaction() as cursor:
        cursor.execute("SELECT * FROM work_requests WHERE id=%s AND member_id=%s FOR UPDATE", (identity, member_id))
        row = cursor.fetchone()
        if not row or row["state"] != "queued":
            raise Conflict("only a queued compute offer can be refreshed")
        cursor.execute("SELECT clock_timestamp() AS now")
        if row["expires_at"] <= cursor.fetchone()["now"]:
            raise Conflict("compute offer expired; submit a new request key")
        cursor.execute("UPDATE work_requests SET expires_at=clock_timestamp()+%s WHERE id=%s RETURNING *",
                       (timedelta(seconds=ttl_seconds), identity))
        return dict(cursor.fetchone())


def enqueue(cursor, reservation_id, kind, payload):
    identity = uuid.uuid4()
    cursor.execute("""INSERT INTO protocol_outbox(id,reservation_id,kind,payload,payload_text,digest)
        VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(reservation_id,kind) DO NOTHING""",
        (identity, reservation_id, kind, Json(payload), ledger.canonical(payload), ledger.fingerprint(payload)))
    cursor.execute("SELECT digest FROM protocol_outbox WHERE reservation_id=%s AND kind=%s", (reservation_id,kind))
    if cursor.fetchone()["digest"] != ledger.fingerprint(payload):
        raise Conflict("existing submission intent has different immutable inputs")


def has_queued(database):
    """Whether any member is waiting for work; offers past their expiry are retired first."""
    with database.transaction() as cursor:
        cursor.execute("UPDATE work_requests SET state='expired' WHERE state='queued' AND expires_at<=clock_timestamp()")
        cursor.execute("SELECT 1 FROM work_requests WHERE state='queued' LIMIT 1")
        return cursor.fetchone() is not None


def network_reference(database, *, now, max_age, refresh=None):
    """A fresh whole-network reference for selection, reading one if the caller allows."""
    index = network_references.latest(database, now=now, max_age_seconds=max_age)
    if index is None and refresh is not None:
        refresh()
        index = network_references.latest(database, now=now, max_age_seconds=max_age)
    if index is None:
        raise ProtocolDataError("no fresh whole-network reference is available for selection")
    return index


def reserve_next(database, player_id, *, now, max_age=120, reference_max_age=1800, refresh_references=None):
    store = BlockStore(database)
    with database.transaction() as cursor:
        cursor.execute("SELECT id FROM observed_blocks ORDER BY height DESC LIMIT 1")
        row = cursor.fetchone()
        if not row:
            raise ProtocolDataError("no complete current snapshot")
        block_id = row["id"]
    observation, snapshot = store.read(block_id)
    if snapshot.scoped:
        # The credited block holds only the pool's own feed. Parameters are
        # copied from a whole-network reference read on demand, so no read
        # happens while nobody is waiting for work.
        if not has_queued(database):
            return None
        reference_index = network_reference(database, now=now, max_age=reference_max_age, refresh=refresh_references)
    else:
        reference_index = references(snapshot)
    with database.transaction() as cursor:
        # Fence updates to the selected block/coverage while making the decision.
        lock(cursor, "observation-stream")
        lock(cursor, "operator:protocol-budget")
        from .controls import blocked
        if blocked(database,cursor=cursor):return None
        cursor.execute("SELECT id FROM observed_blocks ORDER BY height DESC LIMIT 1")
        if cursor.fetchone()["id"] != block_id:
            raise ProtocolDataError("new block arrived before work reservation")
        cursor.execute("SELECT 1 FROM unresolved_observation_conflicts WHERE kind='conflicting-block' AND height=%s", (snapshot.height,))
        if cursor.fetchone():
            raise ProtocolDataError("current snapshot has conflicting evidence")
        cursor.execute("UPDATE work_requests SET state='expired' WHERE state='queued' AND expires_at<=clock_timestamp()")
        cursor.execute("""SELECT * FROM work_requests WHERE state='queued'
            ORDER BY coalesce(last_attempted_at,created_at),created_at,id LIMIT 100 FOR UPDATE SKIP LOCKED""")
        requests = cursor.fetchall()
        for request in requests:
            cursor.execute("UPDATE work_requests SET last_attempted_at=clock_timestamp() WHERE id=%s", (request["id"],))
            offer = request["offer"]
            try:
                selection = choose(snapshot, observation["algorithms"]["binarys"], player_id=player_id,
                    resource=offer["resource"], compute_type=offer["compute_type"], now=now, max_age=max_age,
                    reference_index=reference_index, reference_max_age=reference_max_age)
            except NoCompatibleWork:
                continue
            # An unavailable member cannot prevent the next queued member from
            # using their own funds. Roll back only that reservation attempt.
            cursor.execute("SAVEPOINT member_reservation")
            try:
                reservation = benchmarks.reserve(database, request["member_id"], str(request["id"]),
                    creation_round=selection.creation_round, resource=offer["resource"], selection=selection.evidence,
                    payload=selection.payload, fee_limit=selection.max_submission_fee,
                    offer_expires_at=request["expires_at"], _cursor=cursor)
            except (InsufficientFunds, Conflict):
                cursor.execute("ROLLBACK TO SAVEPOINT member_reservation")
                cursor.execute("RELEASE SAVEPOINT member_reservation")
                continue
            cursor.execute("RELEASE SAVEPOINT member_reservation")
            enqueue(cursor, reservation["id"], "precommit", selection.payload)
            cursor.execute("UPDATE work_requests SET state='reserved',reservation_id=%s WHERE id=%s",
                           (reservation["id"], request["id"]))
            return reservation
    return None
