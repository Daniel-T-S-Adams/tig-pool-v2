# Member benchmark protocol 2.0

This contract pairs `pool_manager.pool_v2` with the separate `worker_v2`
package in the user's `innopool-slave-v2` repository. All work routes require
`Authorization: Bearer <member-token>` and `X-InnoPool-Version: 2.0`. A wrong or
missing work-protocol version returns 426 before reserving funds. The pool's
capabilities route reports `api_version`, `assignment_unit: whole-benchmark`
and whether new work is enabled. Amounts exposed to clients use decimal strings
of token base units, with 18 decimal places per TIG.

## Work request

`POST /api/v2/work-requests` accepts:

```json
{
  "request_key": "client-persisted-unique-key",
  "resource": "CPU",
  "compute_type": "aws_c7a",
  "capacity": {"workers": 4}
}
```

Each request offers only CPU or GPU. Verification compute type must match:
`aws_g4dn` is GPU, while TIG's supported t3/t3a/t4g/c7i/c7a/c7g/m7i/m7a/m7g
types are CPU. Capacity is 1–4096 workers. Persist the key before sending; reuse
with different inputs returns 409. Repeating the same offer returns its original
request, even after expiry. The default offer lifetime is 60 seconds.

`GET /api/v2/work-requests/{id}` returns only the authenticated member's
request. `POST /api/v2/work-requests/{id}/refresh` extends an unexpired queued
offer. Expired offers require a new key. The coordinator atomically links the
immutable reservation, collateral, shared operator fee commitment, member slot
and durable precommit intent using one complete current block's selection.

Member slots are shared across CPU and GPU. A queued request has not consumed
a slot or funds. Returned state becomes `reserved` while acceptance is pending
and `assigned` when complete benchmark data is available. `active`,
`verification_failed`, `expired`, `rejected` and `cancelled` end that work
request. An uncertain submission never becomes eligible for a resend or refund
because a worker lease or HTTP timeout elapsed.

## Handover

`GET /api/v2/benchmarks/{benchmark_id}` returns:

- `state`, `benchmark_id` and `handed_over_at` (null until confirmed).
- `assignment_payload`: the **exact JSON string**, containing API version,
  benchmark ID, all TIG settings, random hash, nonce/bundle counts, fuel budget,
  compute type, hyperparameters (including explicit null), algorithm archive
  URL and its SHA-256.
- `assignment_digest`: SHA-256 of `assignment_payload.encode('utf-8')`.
- `sampled_nonces`: null until TIG supplies the authoritative list.

Store the exact string and verify its digest before acknowledging. Hashing a
parsed/re-serialized JSON object is not equivalent: languages and database JSON
storage can normalize numbers differently. The pool preserves original bytes
separately from its queryable JSON representation.

`POST /api/v2/benchmarks/{benchmark_id}/acknowledge` takes
`{"assignment_digest":"<64 lowercase hex characters>"}`. The pool verifies
ownership, commits its first handover timestamp, then responds. Computation
starts only after confirmation. A lost acknowledgement response can be recovered
by repeating the same acknowledgement or reading the stored timestamp. GET alone
does not transfer responsibility; a changed digest is refused.

## Whole-benchmark outputs

`POST /api/v2/benchmarks/{benchmark_id}/results` takes `merkle_root` and
`solution_quality`, an int32 array covering every nonce in order starting at
zero. The root is 64 lowercase hex characters. Results require confirmed
handover. The pool stores the immutable payload and its TIG submission intent
in one transaction, then returns `stored: true` and its digest. This response
acknowledges pool storage, not TIG activation.

After `sampled_nonces` is available, `POST .../proofs` takes `merkle_proofs` in
TIG's output-leaf/encoded-branch format. Proofs must cover each sampled nonce
exactly once and reconstruct the committed root. These local Merkle checks do
not replace TIG solution verification. Repeating an identical stored result or
proof succeeds; changing it returns 409. The worker keeps its complete evidence
and polls for a definitive outcome.

Activation, verification failure and expiry free the slot, but they do not
release collateral early. Final collateral accounting follows the creation
round's X+2 rule. Missing or ambiguous external evidence leaves the operation
pending. Pausing new work keeps authenticated handover/recovery/upload routes
available for existing work.

## Implementation boundary and tests

The API, transactional queue, handover and upload storage are implemented.
The live TIG dispatcher and outcome reconciler are a separate integration
increment. Their tests currently supply explicit simulated acceptance,
sampling and activation evidence; no API module starts a legacy scheduler or
submits anything on import. Both funds and work flags default to false.

The pool CI checks out an exact worker commit and runs its real client/runner
against the actual FastAPI application with PostgreSQL. CPU and GPU fixtures
exercise requests, full assignments, acknowledgements, all nonce results,
proofs and slot release while collateral remains held. Runtime execution and
TIG responses in that test are simulated. Separate worker tests check durable
restart recovery and reproduce a recorded public TIG Merkle root.

Current worker pin: `6ff56f2833191e6d9885f1bdefe4ed1e3d268d6f`. This is an
integration-test pairing, not a production release manifest.
