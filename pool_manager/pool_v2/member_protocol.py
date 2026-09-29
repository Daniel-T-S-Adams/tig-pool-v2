"""Authenticated whole-benchmark handover, immutable results and proof upload."""

import json

from blake3 import blake3
from psycopg2.extras import Json

from . import benchmarks, ledger
from .chain import hex_bytes
from .money import Conflict, FundsError
from .work_requests import enqueue


def owned(cursor, benchmark_id, member_id, *, for_update=False):
    cursor.execute("SELECT id FROM reservations WHERE benchmark_id=%s AND member_id=%s", (benchmark_id, member_id))
    row = cursor.fetchone()
    if not row:
        raise FundsError("unknown member benchmark")
    if for_update:
        return benchmarks._locked(cursor, row["id"])
    cursor.execute("SELECT * FROM reservations WHERE id=%s", (row["id"],))
    return dict(cursor.fetchone())


def get(database, benchmark_id, member_id):
    with database.transaction() as cursor:
        row = owned(cursor, benchmark_id, member_id)
        cursor.execute("SELECT sampled_nonces FROM benchmark_progress WHERE benchmark_id=%s", (benchmark_id,))
        progress = cursor.fetchone()
        return {"benchmark_id": benchmark_id, "state": row["state"],
                # These exact UTF-8 bytes avoid cross-language float/integer JSON
                # reserialization differences when verifying the handover digest.
                "assignment_payload": row["assignment_payload"] or ledger.canonical(row["assignment"]),
                "assignment_digest": row["assignment_digest"], "handed_over_at": row["handed_over_at"],
                "sampled_nonces": progress["sampled_nonces"] if progress else None}


def acknowledge(database, benchmark_id, member_id, digest):
    with database.transaction() as cursor:
        row = owned(cursor, benchmark_id, member_id)
    benchmarks.acknowledge(database, row["id"], member_id, digest)
    return get(database, benchmark_id, member_id)


def _store(cursor, row, kind, payload):
    if row["handed_over_at"] is None:
        raise Conflict("confirmed handover is required before results or proofs")
    digest = ledger.fingerprint(payload)
    cursor.execute("SELECT digest FROM benchmark_payloads WHERE benchmark_id=%s AND kind=%s", (row["benchmark_id"], kind))
    old = cursor.fetchone()
    if old:
        if old["digest"] != digest:
            raise Conflict("benchmark payload is immutable after its first upload")
        return {"stored": True, "digest": digest}
    if row["state"] != "accepted":
        raise Conflict("benchmark no longer accepts a first result/proof upload")
    cursor.execute("INSERT INTO benchmark_payloads(benchmark_id,kind,digest,payload,payload_text) VALUES (%s,%s,%s,%s,%s)",
                   (row["benchmark_id"], kind, digest, Json(payload), ledger.canonical(payload)))
    upstream = {"benchmark_id": row["benchmark_id"], **payload}
    if kind == "results":
        upstream["stopped"] = False
    enqueue(cursor, row["id"], kind, upstream)
    return {"stored": True, "digest": digest}


def results(database, benchmark_id, member_id, payload):
    if not isinstance(payload, dict) or set(payload) != {"merkle_root", "solution_quality"}:
        raise FundsError("result must contain a whole-benchmark Merkle root and nonce qualities")
    root = payload["merkle_root"]
    if not isinstance(root, str) or root != hex_bytes("0x"+root, 32)[2:]:
        raise FundsError("invalid Merkle root")
    qualities = payload["solution_quality"]
    if not isinstance(qualities, list) or any(type(v) is not int or not -(2**31) <= v < 2**31 for v in qualities):
        raise FundsError("nonce qualities must be protocol int32 integers")
    with database.transaction() as cursor:
        row = owned(cursor, benchmark_id, member_id, for_update=True)
        if len(qualities) != row["assignment"]["num_nonces"]:
            raise FundsError("result does not contain every benchmark nonce")
        return _store(cursor, row, "results", payload)


def sampled(database, benchmark_id, nonces, *, evidence):
    """Only the TIG observation adapter calls this; never a member endpoint."""
    if not evidence or not isinstance(nonces, list) or any(type(n) is not int or n < 0 for n in nonces) or len(set(nonces)) != len(nonces):
        raise FundsError("invalid authoritative sampled nonces or missing evidence")
    with database.transaction() as cursor:
        cursor.execute("SELECT id FROM reservations WHERE benchmark_id=%s", (benchmark_id,))
        identity = cursor.fetchone()
        if not identity:
            raise FundsError("unknown pool benchmark")
        row = benchmarks._locked(cursor, identity["id"])
        if any(n >= row["assignment"]["num_nonces"] for n in nonces):
            raise FundsError("sampled nonce is outside the assigned benchmark")
        cursor.execute("SELECT sampled_nonces FROM benchmark_progress WHERE benchmark_id=%s", (benchmark_id,))
        previous = cursor.fetchone()
        if previous and previous["sampled_nonces"] != sorted(nonces):
            raise Conflict("TIG sampled nonce set changed")
        cursor.execute("INSERT INTO benchmark_progress(benchmark_id,sampled_nonces,evidence) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                       (benchmark_id, Json(sorted(nonces)), Json(evidence)))


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def proof_root(proof, count):
    if not isinstance(proof, dict) or set(proof) != {"leaf", "branch"}:
        raise FundsError("invalid Merkle proof structure")
    leaf, branch = proof["leaf"], proof["branch"]
    if not isinstance(leaf, dict) or set(leaf) != {"nonce", "runtime_signature", "fuel_consumed", "solution", "cpu_arch"}:
        raise FundsError("invalid TIG output leaf")
    for field in ("nonce", "runtime_signature", "fuel_consumed"):
        if type(leaf[field]) is not int or not 0 <= leaf[field] < 2**64:
            raise FundsError("TIG output metadata must use uint64 integers")
    if leaf["nonce"] >= count or leaf["cpu_arch"] not in ("amd64", "arm64") or not isinstance(leaf["solution"], str):
        raise FundsError("TIG output does not match the benchmark")
    if not isinstance(branch, str) or len(branch) % 66:
        raise FundsError("invalid Merkle branch encoding")
    maximum = (count-1).bit_length()
    if len(branch)//66 > maximum:
        raise FundsError("Merkle branch exceeds benchmark depth")
    metadata = {key: leaf[key] for key in ("nonce", "runtime_signature", "fuel_consumed")}
    metadata["solution_signature"] = int.from_bytes(blake3(_json(leaf["solution"])).digest()[:8], "little")
    value, position, level = blake3(_json(metadata)).digest(), leaf["nonce"], 0
    try:
        for offset in range(0, len(branch), 66):
            depth, other = int(branch[offset:offset+2],16), bytes.fromhex(branch[offset+2:offset+66])
            if depth < level or depth >= maximum or len(other) != 32:
                raise FundsError("invalid Merkle branch depth")
            position >>= depth-level
            value = blake3(other+value if position & 1 else value+other).digest()
            position >>= 1
            level = depth+1
    except ValueError as exc:
        raise FundsError("invalid Merkle proof encoding") from exc
    return value.hex()


def proofs(database, benchmark_id, member_id, payload):
    if not isinstance(payload, dict) or set(payload) != {"merkle_proofs"} or not isinstance(payload["merkle_proofs"], list):
        raise FundsError("invalid proof payload")
    with database.transaction() as cursor:
        row = owned(cursor, benchmark_id, member_id, for_update=True)
        cursor.execute("SELECT sampled_nonces FROM benchmark_progress WHERE benchmark_id=%s", (benchmark_id,))
        progress = cursor.fetchone()
        cursor.execute("SELECT payload FROM benchmark_payloads WHERE benchmark_id=%s AND kind='results'", (benchmark_id,))
        result = cursor.fetchone()
        if not progress or not result:
            raise Conflict("results and authoritative sampled nonces must be stored first")
        nonces = []
        for proof in payload["merkle_proofs"]:
            if proof_root(proof, row["assignment"]["num_nonces"]) != result["payload"]["merkle_root"]:
                raise FundsError("proof does not match the committed benchmark root")
            nonces.append(proof["leaf"]["nonce"])
        if sorted(nonces) != progress["sampled_nonces"]:
            raise FundsError("proofs must cover the exact sampled nonce set once")
        ordered = {"merkle_proofs": sorted(payload["merkle_proofs"], key=lambda value: value["leaf"]["nonce"])}
        return _store(cursor, row, "proofs", ordered)
