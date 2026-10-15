# Connect your master to Community Pool

Use this guide to receive a complete benchmark in your own master. Your master
is the software that connects to the pool, saves assignments and submits your
combined results and proofs. You decide how it distributes computation to your
other machines. The pool requires no helper-machine registration.

The updated **Connect your master** page at `/join` gives the same connection
steps. It was deployed with the paired worker on 13 October; see
[rollout status](LEAN_ROLLOUT_20261013.md).
The supplied reference installer configures one machine. You can implement a
custom master using the member API described here, or configure the paired
reference worker's optional SSH helper mode. See the
[setup diagram and master contract](MEMBER_MASTER_V2.md) and
[SSH implementation and verification](SSH_MEMBER_MASTER_VERIFICATION.md).

## 1. Register and prepare your account

Open the pool website, select **Connect wallet**, and sign its login message.
This registers your member account. Under **Member funds**, use the displayed
network, TIG contract and custody address if you need to deposit collateral.
Send from your registered wallet and wait for the pool to show the finalized
credit in **Available funds**. Your operator can set a reduced collateral
multiplier; the amount required depends on the selected benchmark and that
multiplier.

One benchmark consumes one execution slot and one collateral reservation even
when many helper machines compute it. The limit is two unfinished benchmarks
across all your CPU/GPU masters and worker installations. Released slots do not
immediately release collateral; final accounting follows the creation round's
X+2 rules.

## 2. Create the credential for your master

Select **Create execution token** on your member account and save the token in
your master's private configuration. It authenticates the master as your member
account. Helpers need only the jobs supplied by your master, not this token.
You do not need the pool's TIG API key or a wallet private key in mining software.

Execution tokens currently expire after 30 days. The member account lists their
expiry and allows revocation and replacement. Replace the token in your master
before it expires; revocation immediately removes that token's API access.
Changing the execution token does not transfer or discard existing benchmarks.

## 3. Configure and check the pool connection

For the recorded mainnet deployment:

| Setting | Value |
|---|---|
| Website | `https://pool.tig.foundation` |
| API origin | `https://pool-api.tig.foundation` |
| Member API base | `https://pool-api.tig.foundation/api/v2` |
| Authorization header | `Authorization: Bearer <execution-token>` |
| Protocol header | `X-InnoPool-Version: 2.0` |
| JSON request header | `Content-Type: application/json` |

Use the API origin shown on your own pool's `/join` page. The browser address
and worker API address can differ. The reference client's `pool_origin` takes
only the HTTPS origin, without `/api/v2`; your custom client appends the routes.
Do not follow redirects on authenticated pool requests.

Before requesting paid work, check these routes:

- `GET /api/v2/capabilities` is public. Check `api_version: "2.0"`,
  `assignment_unit: "whole-benchmark"` and `work_enabled: true`. If new work is
  paused or disabled, wait for the operator; receiving a token does not enable it.
- `GET /api/v2/member/balance` with both headers confirms that the execution
  token belongs to your account and reports available funds, collateral and slots.
- `GET /api/v2/release` is public and supplies the paired release and official
  challenge runtime images pinned by digest. Check the runtime configuration
  your machines will use before offering capacity.

Choose one resource and a compatible TIG verification compute type for each
request. CPU types are `aws_t3`, `aws_t3a`, `aws_t4g`, `aws_c7i`, `aws_c7a`,
`aws_c7g`, `aws_m7i`, `aws_m7a` and `aws_m7g`. The t4g/c7g/m7g types require ARM64;
the other CPU types require AMD64. GPU requests use `aws_g4dn` on AMD64 with the
appropriate NVIDIA runtime and PTX. Every participating machine must execute the
exact assigned binary and runtime compatibly; CPU/GPU family alone is not enough.

Your master supplies `capacity.workers`: concurrent nonce computations available
for this offer, between 1 and 4096. This can span compatible machines. It is not
machine count, CPU-core count or benchmark count. The current pool records this
declaration but does not use the number to size the selected benchmark. Configure
or discover concurrency in your own master/helpers; no website concurrency choice
is required to use a custom master. Divide available capacity between concurrent
benchmarks.

## 4. Request, receive and acknowledge a benchmark

Persist a unique request key and the exact offer before sending this JSON:

```http
POST /api/v2/work-requests
Authorization: Bearer <execution-token>
X-InnoPool-Version: 2.0
Content-Type: application/json

{
  "request_key": "<your-persisted-unique-request-key>",
  "resource": "CPU",
  "compute_type": "aws_c7a",
  "capacity": {"workers": 12}
}
```

This example could describe three compatible machines offering four simultaneous
nonce computations each. The POST returns a request `id`. Save it and poll
`GET /api/v2/work-requests/{id}` with the same headers.

| Request state | Your master does this |
|---|---|
| `queued` | Wait. Refresh using `POST /api/v2/work-requests/{id}/refresh` with `{}` before `expires_at`, while that capacity remains available. The default offer lifetime is 60 seconds. |
| `reserved` | Wait for TIG acceptance; a slot and collateral have been reserved. Do not compute yet. |
| `assigned` | Read `benchmark_id` and fetch `GET /api/v2/benchmarks/{benchmark_id}`. |
| `active`, `verification_failed`, `expired`, `rejected`, `cancelled` | Record the terminal outcome. Reconcile any saved benchmark before offering replacement work. |

After an uncertain POST response, resend the same saved key and offer. Never
change its inputs or create another key just to work around a lost response.
An expired queued offer requires a new key; repeating the old key still returns
the original request.

The benchmark GET returns `assignment_payload`, `assignment_digest`,
`handed_over_at` and `sampled_nonces`. To complete receipt:

1. Save the **exact** `assignment_payload` string durably. Its UTF-8 SHA-256 must
   equal `assignment_digest`; parsing and reserializing the JSON is not equivalent.
2. Validate the offered compute type, assignment settings, nonce count, fuel
   budget, hyperparameters, algorithm archive SHA-256 and pinned runtime.
3. Send `POST /api/v2/benchmarks/{benchmark_id}/acknowledge` with
   `{"assignment_digest":"<the-returned-digest>"}` and the authentication/version
   headers.
4. Save the confirmed `handed_over_at` timestamp. If the response is lost, repeat
   the identical acknowledgement or GET the benchmark to recover confirmation.
   Start computation only after confirmed handover.

**A saved assignment and confirmed handover mean your master has received the
benchmark and your member account is responsible for finishing it.** Your master
can now dispatch nonce computations to your compatible machines using your own
scheduling and transport. A GET alone does not establish handover.

## Return the completed benchmark

Your master retains the complete assignment and each output leaf and verified
quality, covering nonce `0` through `num_nonces - 1`. When all outputs are saved:

1. Upload `POST /api/v2/benchmarks/{benchmark_id}/results` containing one
   `merkle_root` and the int32 `solution_quality` array for every nonce in order.
2. Poll the benchmark GET for authoritative `sampled_nonces`. Upload exactly those
   proofs to `POST /api/v2/benchmarks/{benchmark_id}/proofs` as `merkle_proofs` in
   TIG's leaf/encoded-branch format against the committed root.
3. Continue polling until TIG activation, verification failure or definitive
   expiry. `stored: true` confirms pool storage, not TIG acceptance.

Use one uploader per benchmark. Retry identical uploads after uncertain
responses; the first stored result and proof payloads are immutable. Complete
the work within TIG's challenge lifetime and retain evidence through collateral
settlement and pending evidence requests. The reference worker's
`worker_v2/proofs.py` demonstrates the required tree and proof format. Full
payload requirements are in [member API 2.0](MEMBER_API_V2.md).

## If you receive no benchmark

| Observation | What to check |
|---|---|
| `401` | Execution token is missing, expired or revoked. Replace it through your member account. |
| `426` | Send `X-InnoPool-Version: 2.0`. |
| `400` / `422` | Check JSON fields, CPU/GPU compute compatibility and capacity from 1 to 4096. |
| `409` | Read the response detail and reconcile saved state: conflicting request inputs, expired offers, changed assignment digests and changed uploads are refused. |
| `503` / `work_enabled: false` | The pool is paused or disabled. Existing benchmark recovery and uploads remain available. |
| Offer stays `queued` | Refresh before expiry. Check available funds and slots; the operator may need to resolve compatible-work availability, protocol funding or observation issues. |
| Results stored but no activation | Continue following the authoritative outcome; local computation and pool storage do not establish TIG acceptance. |

The current operational pause and deployment limits are recorded in
[operations status](OPERATIONS_STATUS.md). The API contract supports a
member-managed master. Real two-server computation, restart recovery and
combined uploads passed on 12 October; actual mainnet acceptance and final
settlement remain unverified. See the
[hardware verification record](SSH_MEMBER_MASTER_VERIFICATION.md). This guide
covers your connection to the pool; configuring helper machines and their
communication remains your responsibility.
