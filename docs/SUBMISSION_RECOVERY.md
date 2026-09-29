# TIG submission and recovery service

The v2 coordinator links the member queue to TIG using durable submission
intents. It runs independently of the API and block collector. This increment
has been tested with simulated writes and real public reads; no TIG key was
configured and no live benchmark or token transfer was submitted.

## Durable submission sequence

Before a precommit, the pool downloads and saves the chosen binary, verifies
that it contains the needed architecture library/PTX, and checks that TIG's
current block still matches the immutable selection. It captures existing
benchmark IDs between two reads of that same head. A changed head cancels the
proven-unsent reservation and returns its collateral/fee commitment. The next
request receives a new selection and multiplier snapshot.

The database commits the potentially-sent marker before the HTTP call. Only one
process can begin that intent. Pending precommits with the same settings and
compute type are serialized even if their unselected track parameters differ:
TIG's random track choice can otherwise make their eventual records
indistinguishable. A timeout does not clear this fence or permit a resend.

A successful precommit response stores the returned benchmark ID immediately
with its immutable reservation owner. Metadata fetches happen afterwards. If
the response was lost, recovery searches for exactly one matching precommit
that was absent from the captured baseline and is not already claimed by
another reservation. Zero or multiple matches remain unresolved.

Publication waits for confirmed details, validates the chosen track, nonce
count, random seed, exact fee and creation height against the saved block, and
then publishes the assignment. Its `binary_url` points at the pool's saved,
checksum-addressed archive when the configured member origin is supplied.
Algorithm names inside TIG archives differ from their IDs; both are preserved.

Member result/proof uploads create immutable intents in the same transaction
as their stored payloads. The sender again commits uncertainty before writing.
Confirmed owned benchmark/proof records reconcile lost responses. A successful
proof POST is not an activation signal: a confirmed verification failure or
presence in the actual active benchmark set determines slot release.

## Recovery and held evidence

Every collector must be configured with the same pool player ID, so its pending
feed is saved even before the pool enters OPoW. Complete observations replay in
height order after service downtime; qualifying credits are recorded once.
Missing or inconsistent blocks remain held, and failed attempts rotate through
the queue so later complete blocks can progress. Ineligible compute offers also
rotate rather than permanently occupying the first queue page.

Activations and confirmed verification failures free slots while keeping all
collateral held for X+2 finalization. Disappearing records, elapsed wall time,
ambiguous HTTP failures and missing reports do not release funds. Explicit
definitive-rejection handling exists for a trusted protocol adapter; generic
HTTP 4xx responses are deliberately insufficient evidence. The live rejection
classification and definitive-expiry rule remain Stage 0 integration checks.

Pausing new work cancels unsent precommits and permits existing payload recovery.
An uncertain operation is retained. Unused, unsent result/proof intents can be
retired only after a definitive benchmark outcome. A service lease never
reclassifies an external write as unsent.

## Development entry points

The database must first be migrated using the separate migration privilege.
Provide `POOL_V2_DATABASE_DSN` only for the isolated v2 database. The coordinator
entry point is:

```sh
python3 tools/run_tig_v2.py --config /absolute/path/coordinator.json
```

The JSON file includes `tig_api_url`, `pool_player_id`, `public_origin` (the
member API's HTTPS origin), `submissions_enabled` and `new_work_enabled`.
Both boolean flags default to false. Enabling new work requires enabled
submissions and a public artifact origin. Only the submission service receives
`POOL_V2_TIG_API_KEY` through its environment. The key is never included in
response evidence, logs, member assignments or runtime containers. `--once`
performs one cycle for development checks; it does not change the flags.

The observer remains `tools/observe_tig_v2.py`, now with
`--pool-player-id <configured-address>`. It needs no TIG API key. Neither entry
point changes legacy deployments or performs startup migrations.

The writer supports only `/submit-precommit`, `/submit-benchmark` and
`/submit-proof`. Authenticated writes never follow redirects. Public artifact
GETs permit configured HTTPS hosts; the observed default redirects are limited
to TIG's own monorepo under `media.githubusercontent.com`. Credential headers
are never forwarded. The worker reads the pool's saved copy directly and
checks its SHA-256 before using it.

## Validation

Database tests exercise two simultaneous senders, delayed acceptance metadata,
lost responses, ambiguous matches, stale choices, queue fairness, pause,
recorded verification failures, delayed activation and persistent collateral.
They also cover artifact integrity and serving, restricted public redirects,
and replay that continues past held blocks. The paired worker commit is pinned
in pool CI. No test writes to TIG or Ethereum.

Read-only downloads on 20 September 2026 verified these live artifacts:

| Algorithm | Library name | Compressed bytes | SHA-256 |
|---|---|---:|---|
| `c008_a050` (CPU) | `titan_killer` | 4,804,855 | `efc1913b60cd83908e1132fd2bc58ee9282bff2f3840c71a715fe6d0f971b967` |
| `c006_a047` (GPU) | `parallax_vega` | 2,754,088 | `88c59421861e560194003e8ca6e80beee00d173406ec01c83c258dda52931253` |

Both include AMD64 and ARM64 libraries; the GPU archive also includes PTX.
The sources were TIG's public `get-binary-blob` endpoint, with the 307 redirect
into the corresponding TIG GitHub artifact. The saved manifest is
`tests/v2/fixtures/artifact-manifest.json`; the actual archives are temporary
local verification files, not committed binaries.

`confirmed-verification-failure.json.gz` retains the complete compact records
for public benchmark `df559c2aa698bfb4d5aa04df761a3263`, captured at height
1,351,102. Proof and fraud confirmation at 1,351,066 precede its planned
activation at 1,351,079, demonstrating why proof receipt alone is insufficient.
Its owner and public source are retained in the fixture. This failure feed is
separate from later per-nonce arbitrations.

Real CPU/GPU benchmark execution, intended-account submission/rejection and
expiry validation, final reward receipt mapping, deployment configuration and
the remaining implementation stages still gate a production release.
