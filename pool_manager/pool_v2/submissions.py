"""Durable TIG submission fences and evidence-driven acceptance recovery.

This module owns no network client and never retries an uncertain POST.
Its caller records preflight reads, commits begin(), then performs one write.
"""

import hashlib
import json
import re
import uuid
from urllib.parse import urlsplit

from psycopg2 import IntegrityError
from psycopg2.extras import Json

from . import benchmarks, ledger
from .block_observer import BlockStore
from .database import lock
from .money import Conflict, FundsError
from .protocol import ProtocolDataError, _integer
from .selection import _precise


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ProtocolDataError("invalid TIG benchmark ID")
    return value


def _locked(cursor, identity):
    cursor.execute("SELECT reservation_id FROM protocol_outbox WHERE id=%s", (identity,))
    found = cursor.fetchone()
    if not found:
        raise FundsError("unknown protocol intent")
    row = benchmarks._locked(cursor, found["reservation_id"], budget=True)
    cursor.execute("SELECT * FROM protocol_outbox WHERE id=%s FOR UPDATE", (identity,))
    return row, dict(cursor.fetchone())


def prepare_archive(database, reservation_id, source_url, archive):
    if not isinstance(archive, bytes) or not 0 < len(archive) <= 64*1024*1024:
        raise FundsError("invalid algorithm archive size")
    digest = hashlib.sha256(archive).hexdigest()
    with database.transaction() as cursor:
        row = benchmarks._locked(cursor, reservation_id)
        expected = row["selection"]["binary"]["details"]["download_url"]
        if source_url != expected:
            raise Conflict("algorithm archive URL differs from reserved selection")
        cursor.execute("SELECT sha256,source_url FROM reservation_archives WHERE reservation_id=%s", (reservation_id,))
        previous = cursor.fetchone()
        if previous:
            if (previous["sha256"], previous["source_url"]) != (digest, source_url):
                raise Conflict("reserved algorithm archive changed")
            return digest
        if row["state"] != "reserved":
            raise Conflict("algorithm archive must be saved before precommit submission")
        cursor.execute("INSERT INTO algorithm_archives(sha256,archive) VALUES (%s,%s) ON CONFLICT DO NOTHING", (digest, archive))
        cursor.execute("INSERT INTO reservation_archives(reservation_id,sha256,source_url) VALUES (%s,%s,%s)",
                       (reservation_id, digest, source_url))
    return digest


def begin(database, identity, *, preflight=None):
    """Commit before the first HTTP POST; duplicate workers cannot both send."""
    try:
        with database.transaction() as cursor:
            row, intent = _locked(cursor, identity)
            if intent["state"] != "ready":
                raise Conflict("intent is not unsent; reconcile instead of retrying")
            if intent["kind"] == "precommit":
                from .controls import paused
                if paused(database,cursor=cursor):raise Conflict('new precommit submissions are paused')
                payload = json.loads(intent["payload_text"])
                if not isinstance(preflight, dict) or preflight.get("block_id") != payload["settings"]["block_id"]:
                    raise Conflict("current TIG block differs from immutable precommit; cancel unsent work")
                if preflight.get("height") != row["selection"]["height"] or not isinstance(preflight.get("seen_benchmarks"), list):
                    raise ProtocolDataError("complete preflight block and benchmark identities required")
                _integer(preflight.get("observed_at"), "preflight timestamp")
                cursor.execute("SELECT extract(epoch FROM clock_timestamp()) AS now")
                if not -5 <= float(cursor.fetchone()["now"])-preflight["observed_at"] <= 20:
                    raise Conflict("preflight is too old to authorize a first submission")
                if len(set(preflight["seen_benchmarks"])) != len(preflight["seen_benchmarks"]):
                    raise ProtocolDataError("duplicate preflight identities")
                for value in preflight["seen_benchmarks"]:
                    _identity(value)
                cursor.execute("SELECT 1 FROM reservation_archives WHERE reservation_id=%s", (row["id"],))
                if not cursor.fetchone():
                    raise Conflict("algorithm archive is not durable")
                # TIG exposes only the randomly chosen track. Different full
                # track proposals can still become indistinguishable later.
                match_key = ledger.fingerprint({"settings": payload["settings"], "compute_type": payload["compute_type"]})
                lock(cursor, "precommit-match:"+match_key)
                cursor.execute("SELECT 1 FROM protocol_outbox WHERE match_key=%s AND state='uncertain'", (match_key,))
                if cursor.fetchone():
                    raise Conflict("indistinguishable precommit is still unresolved")
                benchmarks.mark_submitting(database, row["id"], _cursor=cursor)
                cursor.execute("UPDATE protocol_outbox SET match_key=%s,preflight=%s WHERE id=%s",
                               (match_key, Json(preflight), identity))
            else:
                if row["state"] != "accepted" or row["handed_over_at"] is None:
                    raise Conflict("benchmark is not available for a first result/proof submission")
                if intent["kind"] == "proofs":
                    cursor.execute("SELECT state FROM protocol_outbox WHERE reservation_id=%s AND kind='results'", (row["id"],))
                    results = cursor.fetchone()
                    if not results or results["state"] != "accepted":
                        raise Conflict("TIG has not accepted this benchmark's results")
            cursor.execute("UPDATE protocol_outbox SET state='uncertain',sent_at=clock_timestamp() WHERE id=%s RETURNING *", (identity,))
            return dict(cursor.fetchone())
    except IntegrityError as exc:
        raise Conflict("another submission holds this immutable protocol fence") from exc


def cancel_unsent(database, identity, *, evidence):
    if not evidence:
        raise FundsError("unsent cancellation requires a reason")
    with database.transaction() as cursor:
        row, intent = _locked(cursor, identity)
        if intent["state"] == "cancelled":
            return
        if intent["state"] != "ready" or intent["kind"] != "precommit":
            raise Conflict("only an unsent precommit can cancel and return collateral")
        benchmarks.release_unstarted(database, row["id"], evidence=evidence, _cursor=cursor)
        cursor.execute("UPDATE protocol_outbox SET state='cancelled',evidence=%s WHERE id=%s", (Json(evidence), identity))


def _receipt(cursor, row, benchmark_id, evidence):
    _identity(benchmark_id)
    cursor.execute("SELECT benchmark_id FROM precommit_receipts WHERE reservation_id=%s", (row["id"],))
    existing = cursor.fetchone()
    if existing:
        if existing["benchmark_id"] != benchmark_id:
            raise Conflict("precommit response contradicts its recorded identity")
        return
    cursor.execute("SELECT id FROM reservations WHERE benchmark_id=%s AND id<>%s", (benchmark_id, row["id"]))
    if cursor.fetchone():
        raise Conflict("benchmark already belongs to another reservation")
    cursor.execute("INSERT INTO precommit_receipts(reservation_id,benchmark_id,evidence) VALUES (%s,%s,%s)",
                   (row["id"], benchmark_id, Json(evidence)))


def record_response(database, identity, response):
    """Persist raw safe response metadata; unknown/error responses stay uncertain.

    A positive precommit ID is stored before any subsequent network reads. A
    successful proof POST means transport acceptance, not TIG activation.
    """
    if not isinstance(response, dict):
        raise FundsError("submission response evidence must be an object")
    with database.transaction() as cursor:
        row, intent = _locked(cursor, identity)
        if intent["state"] not in ("uncertain", "accepted"):
            raise Conflict("no potentially sent operation matches this response")
        response_id = uuid.uuid4()
        cursor.execute("INSERT INTO submission_responses(id,outbox_id,response) VALUES (%s,%s,%s)",
                       (response_id, identity, Json(response)))
        payload = response.get("body")
        success = response.get("status") == 200 and isinstance(payload, dict)
        evidence = {"response_id": str(response_id)}
        if success and intent["kind"] == "precommit" and "benchmark_id" in payload:
            try:
                benchmark_id = _identity(payload["benchmark_id"])
            except ProtocolDataError:
                success = False
            else:
                _receipt(cursor, row, benchmark_id, evidence)
        elif success and intent["kind"] == "results":
            success = payload.get("ok") is True
        elif success and intent["kind"] == "proofs":
            success = "verified" in payload
        else:
            success = False
        if success and intent["state"] == "uncertain":
            cursor.execute("UPDATE protocol_outbox SET state='accepted',evidence=%s WHERE id=%s", (Json(evidence), identity))
        return success


def definitive_rejection(database, identity, *, evidence, actual_fee=0):
    """Trusted adapter only: HTTP timeout, absence or generic 4xx is insufficient."""
    if not evidence or evidence.get("definitive_no_precommit") is not True:
        raise FundsError("explicit evidence of definitive rejection is required")
    with database.transaction() as cursor:
        row, intent = _locked(cursor, identity)
        if intent["kind"] != "precommit" or intent["state"] != "uncertain":
            raise Conflict("rejection conflicts with the stored submission")
        cursor.execute("SELECT 1 FROM precommit_receipts WHERE reservation_id=%s", (row["id"],))
        if cursor.fetchone():
            raise Conflict("positive acceptance cannot be replaced by rejection")
        benchmarks.release_unstarted(database, row["id"], rejected=True, actual_fee=actual_fee,
                                     evidence=evidence, _cursor=cursor)
        cursor.execute("UPDATE protocol_outbox SET state='rejected',evidence=%s WHERE id=%s", (Json(evidence), identity))


def matches(payload, precommit):
    try:
        settings, details = precommit["settings"], precommit["details"]
        expected = {**payload["settings"], "track_id": settings["track_id"]}
        track = payload["track_settings"][settings["track_id"]]
        return (settings == expected and details["compute_type"] == payload["compute_type"]
                and all(details[key] == track[key] for key in ("num_bundles", "fuel_budget", "hyperparameters")))
    except (KeyError, TypeError):
        return False


def recover_precommit(database, identity, precommits, *, evidence):
    """Recover exactly one new match. Zero/multiple matches remain unresolved."""
    if not isinstance(precommits, list) or not evidence:
        raise ProtocolDataError("precommit reconciliation requires observed protocol records")
    with database.transaction() as cursor:
        row, intent = _locked(cursor, identity)
        if intent["kind"] != "precommit" or intent["state"] != "uncertain" or not intent["preflight"]:
            raise Conflict("precommit is not awaiting identity reconciliation")
        excluded = set(intent["preflight"]["seen_benchmarks"])
        cursor.execute("SELECT benchmark_id FROM precommit_receipts UNION SELECT benchmark_id FROM reservations WHERE benchmark_id IS NOT NULL")
        excluded.update(item["benchmark_id"] for item in cursor.fetchall())
        payload = json.loads(intent["payload_text"])
        candidates = {value["benchmark_id"]: value for value in precommits
                      if isinstance(value, dict) and value.get("benchmark_id") not in excluded and matches(payload, value)}
        if len(candidates) != 1:
            return None
        benchmark_id = next(iter(candidates))
        _receipt(cursor, row, benchmark_id, evidence)
        cursor.execute("UPDATE protocol_outbox SET state='accepted',evidence=%s WHERE id=%s", (Json(evidence), identity))
        return benchmark_id


def publish_assignment(database, reservation_id, precommit, *, evidence, artifact_origin=None):
    """Publish confirmed TIG details only after validating the immutable choice."""
    if artifact_origin:
        origin=urlsplit(artifact_origin)
        if origin.scheme != "https" or not origin.netloc or origin.username or origin.password or origin.path not in ("","/") or origin.query or origin.fragment:
            raise FundsError("artifact serving requires the configured HTTPS pool origin")
    with database.transaction() as cursor:
        cursor.execute("SELECT payload_text FROM reservations WHERE id=%s", (reservation_id,))
        row = cursor.fetchone()
        if not row or not row["payload_text"]:
            raise FundsError("unknown serialized reservation")
        payload = json.loads(row["payload_text"])
    _, snapshot = BlockStore(database).read(payload["settings"]["block_id"])
    try:
        if not matches(payload, precommit):
            raise ProtocolDataError("accepted benchmark does not match the reserved precommit")
        benchmark_id = _identity(precommit["benchmark_id"])
        details = precommit["details"]
        confirmed = _integer(precommit["state"]["block_confirmed"], "precommit confirmation", snapshot.height)
        if confirmed <= snapshot.height:
            raise ProtocolDataError("precommit confirmation must follow the referenced block")
        if details["block_started"] != snapshot.height:
            raise ProtocolDataError("accepted creation height differs from the collateral reservation")
        config = snapshot.challenges[payload["settings"]["challenge_id"]]["config"]
        track = config["active_tracks"][precommit["settings"]["track_id"]]
        if details["num_nonces"] != details["num_bundles"] * track["num_nonces_per_bundle"]:
            raise ProtocolDataError("accepted nonce count differs from the selected track")
        if not isinstance(details["rand_hash"], str) or not re.fullmatch(r"[0-9a-f]{32}", details["rand_hash"]):
            raise ProtocolDataError("accepted random seed is invalid")
        fee = _precise(details["fee_paid"], "accepted submission fee")
        expected_fee = _precise(config["base_fee"], "base fee") + _precise(config["per_nonce_fee"], "per-bundle fee") * details["num_bundles"]
        if fee != expected_fee:
            raise ProtocolDataError("charged fee differs from the captured protocol configuration")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ProtocolDataError("accepted precommit metadata is incomplete") from exc
    with database.transaction() as cursor:
        row = benchmarks._locked(cursor, reservation_id, budget=True)
        cursor.execute("SELECT benchmark_id FROM precommit_receipts WHERE reservation_id=%s", (reservation_id,))
        receipt = cursor.fetchone()
        if not receipt or receipt["benchmark_id"] != benchmark_id:
            raise Conflict("confirmed metadata has no matching durable acceptance receipt")
        cursor.execute("SELECT * FROM reservation_archives WHERE reservation_id=%s", (reservation_id,))
        archive = cursor.fetchone()
        if not archive:
            raise Conflict("reserved binary archive is missing")
        assignment = {"api_version": "2.0", "benchmark_id": benchmark_id, "settings": precommit["settings"],
            "algorithm_name": snapshot.algorithms[payload["settings"]["algorithm_id"]]["details"]["name"],
            **{key: details[key] for key in ("rand_hash", "num_nonces", "num_bundles", "fuel_budget", "hyperparameters", "compute_type")},
            "binary_url": artifact_origin.rstrip('/')+'/api/v2/artifacts/'+archive["sha256"] if artifact_origin else archive["source_url"],
            "binary_sha256": archive["sha256"]}
        return benchmarks.accept(database, reservation_id, benchmark_id, assignment, actual_fee=fee,
                                 evidence=evidence, _cursor=cursor)


def confirm_payload(database, reservation_id, kind, *, evidence):
    """A confirmed owned benchmark/proof resolves an uncertain payload POST."""
    if kind not in ("results", "proofs") or not evidence:
        raise FundsError("confirmed result/proof evidence required")
    with database.transaction() as cursor:
        benchmarks._locked(cursor, reservation_id, budget=True)
        cursor.execute("SELECT * FROM protocol_outbox WHERE reservation_id=%s AND kind=%s FOR UPDATE", (reservation_id,kind))
        row = cursor.fetchone()
        if not row or row["state"] not in ("uncertain", "accepted"):
            raise Conflict("confirmed protocol payload has no matching potentially-sent intent")
        if row["state"] == "uncertain":
            cursor.execute("UPDATE protocol_outbox SET state='accepted',evidence=%s WHERE id=%s", (Json(evidence),row["id"]))


def retire_unsent_payload(database, identity):
    with database.transaction() as cursor:
        row,intent=_locked(cursor,identity)
        if intent["kind"] == "precommit" or intent["state"] != "ready" or row["state"] not in ("active","verification_failed","expired"):
            return False
        cursor.execute("UPDATE protocol_outbox SET state='cancelled',evidence=%s WHERE id=%s",
                       (Json({"reason":"definitive-benchmark-outcome","state":row["state"]}),identity))
        return True
