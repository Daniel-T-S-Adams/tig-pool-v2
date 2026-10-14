# Two-server mainnet CPU trials — 14 October 2026

The owner separately authorized the original CPU benchmark and one corrected
attempt, each capped at 0.0004 TIG member collateral and a 0.001 TIG pool fee.
The original attempt failed as recorded below. The corrected attempt activated successfully on TIG;
its approval was consumed by one fenced precommit. All trial plans are now
disabled. Ordinary pool work remains paused and settlement disabled.

## First attempt: verification failure

Benchmark `0fd6714756833d70378a1b630f93c126` used `c003_a154` on `aws_c7g`,
track `n_items=1000,budget=10`, four bundles and 4,000 nonces. Master
`204.168.170.205` and helper `77.42.14.166` each had six workers inside their
persistent six-CPU/10-GiB limits. The master saved all 4,000 outputs; 2,034
were computed by the helper and 1,966 locally.

| Event | UTC time |
|---|---|
| Precommit sent and accepted | 08:35:29 |
| Member handover | 08:36:02 |
| Results accepted by TIG | 08:46:33 |
| Structural Merkle proof response | 08:46:56 |
| Confirmed verification failure | 08:47:53, height 1383748 |

Computation and result upload took about **10 minutes 31 seconds** after
handover, within the nominal two-hour lifetime. This establishes the intended
two-server timing and upload path. It does **not** establish successful mainnet
activation: TIG's subsequent solution verification failed.

The actual fee was **0.001 TIG** and collateral hold **0.0004 TIG**. The work
slot was freed normally. Collateral remains held for the normal X+2 forfeiture;
it was not refunded or settled early. Ledger audit remained empty.

## Cause established by replay

The release manifest pinned knapsack runtime **0.0.7**, digest
`fb040e7eca936f08b6808136b26027d54b997f82eb02718e310124012909a840`.
The current official **0.0.8** digest is
`b86289d33a78688b561ffbade7f256dc2e7d289815a53192061512472c0b6014`.
The algorithm archive, benchmark settings and random hash matched TIG.

All three submitted sampled solutions passed the pinned 0.0.7 verifier but
failed 0.0.8. For nonce 129, 0.0.8 reproduced TIG's exact error: selected weight
853 exceeded the maximum 548. Recomputing the same real assignment/nonce with
0.0.8 produced a solution its verifier accepted. This identifies the runtime
mismatch that the earlier internally consistent unpaid checks did not catch.

The `/submit-proof` response `verified: {"Ok": null}` establishes the initial
Merkle verification step, not successful later solution verification. The
summary `/get-benchmarks` endpoint always redacts fraud allegation text; the
detailed `/get-benchmark-data` endpoint supplied the actual failure reason.
See [TIG's official API specification](https://github.com/tig-foundation/tig-monorepo/blob/main/swagger.yaml)
and [proof submission contract](https://github.com/tig-foundation/tig-monorepo/blob/main/tig-protocol/src/contracts/benchmarks.rs).
The existing pool failure classification was correct. An initially proposed
null-allegation decoder change was discarded before commit or deployment.

## Correction work

The corrected pair was activated on primary at **10:18 UTC** and staged on
recovery with its pool services inactive:

- Pool `a65b3a85d7d53285915ba6f75ac67df4cda503c5`, tag
  `mainnet-runtime08-member-master-20261014`.
- Worker `dddad9df9b0d051cf004a904fe7b7361f9816c43`, tag
  `mainnet-runtime-release-guard-20261014`.
- All five configured CPU runtime images now pin official **0.0.8** digests.
- Public and loopback manifests, installer checksum, all six service paths,
  retention, 21 migrations and compatible readers passed live checks.
  Deployment preserved balances/journals and the durable work pause.

The unpaid two-host rerun completed all **12,400 outputs** and **15 sampled
proofs** using the corrected runtime and simulated pool/TIG replies, without
pool credentials. All actual result/proof sets passed an isolated pool API:
retries were idempotent, changed roots were refused, accounting was unchanged,
and only binary metadata was stored.

| Current c003_a154 track | Nonces | Complete compute/proof time |
|---|---:|---:|
| n_items=1000,budget=10 | 4,000 | 10m40s |
| n_items=1000,budget=25 | 4,000 | 12m29s |
| n_items=1000,budget=5 | 4,000 | 9m13s |
| n_items=5000,budget=10 | 200 | 11m06s |
| n_items=5000,budget=25 | 200 | 10m37s |

The worker checks the release digest and every backend's runtime map before
new offers, unlinked saved offers and queued-offer refresh. Existing
handed-over work can drain. All **53 worker v2 tests and 17 inherited tests**
passed locally and in hosted run `38545322562`; the exact paired pool passed
**377 hosted tests** in run `38545838913`. The hardware rerun used the prior
worker's unchanged compute/proof methods; source comparison confirms that only
the offer guard changed. Both task-owned test installations now use the
served guarded worker and preserve their prior source/configuration.

The owner subsequently approved one corrected mainnet attempt, recorded below.
Unpaid results alone do not prove successful paid activation. Hardware checks cover c003 on these two
ARM64 servers; other workloads, GPU and the reward/expense flow remain separate
launch gates.

## Corrected paid attempt — successful activation

The owner's additional **YES** authorized exactly one further attempt with the
same caps. A first launch stopped before reservation/binding because TIG's
current track-25 reference changed from `ils_rounds=135` to default parameters.
It incurred no fee or collateral hold; that unused plan was disabled. A complete
unpaid rerun of the changed 4,000-nonce default track passed in **10m54s**, with
three verified proofs and an isolated API replay. Eleven bounds tests and six
disposable driver scenarios passed, including refusal of unvalidated parameters
and another paid send. The plan accepts only these measured track-25 variants;
all other algorithm, workload, member and cost checks remain fixed.

The same unconsumed approval then funded benchmark
`8c7c7bc72f7769d9c65a562b20ca62d2`, reservation
`76046f2f-c1b6-4355-9983-18c48417c96e`, in namespace
`20261014-runtime08-v2`. One precommit was sent at **11:02:41 UTC**, confirmed at
height **1383884**, and handed over at **11:03:08**. TIG selected
`n_items=1000,budget=25`, 4,000 nonces and `ils_rounds=135`. Actual fee is
**0.001 TIG** and collateral hold **0.0004 TIG**. Both servers finished all 4,000 outputs: master **1,968**, helper **2,032**.
Results were accepted at **11:15:48 UTC**, **12m40s** after handover. Proofs were
accepted at **11:16:11** and confirmed at height **1383898**, with no fraud.
TIG activated the benchmark at height **1383916**, approximately **11:35 UTC**.
The independent current-head check found both the benchmark and pool active;
the detailed record had four active bundles and no verification failure.

All 4,000 qualities, the Merkle root and all three sampled proofs exactly match
the pool's stored payloads. The pool recorded the reservation `active` and freed
its work slot. There is exactly one accepted intent each for precommit, results
and proofs, and one durable potentially-sent event. The member worker completed
normally. Ledger audit remained empty and all trial approvals are disabled.
Collateral remains held for normal X+2 settlement; it was not refunded early.
This establishes the two-host CPU path on mainnet. Full reward/expense settlement,
funded takeover and GPU checks remain separate launch gates.

Both protected approvals are false, the send binding is durable, and normal
new work is paused. Native WAL remains synchronous. Encrypted export
`configuration-daily-2026-10-14T110548Z` passed all hashes/private decryption on
recovery at **11:07:32**, including the consumed plan, installer, router and
service/exit controls. Temporary plaintext was removed. The original member
installation and all prior trial data remain preserved.

## Separate storage hold

Historical conversion completed overnight except for one held block at
`1382587`, the legacy/compact rollout boundary. The two captures differ in
reward-schedule and binary-list ordering. A bounded read-only comparison on
14 October validated both snapshots and proved identical records and
multiplicities in all three differing lists; their pool projections match after
only those ordering changes. This is diagnostic evidence, not an alert waiver. Originals and the conflict alert are
retained; no primary raw archive was pruned. The automatic watcher stopped
before requesting a replacement backup. Validate the storage correction and
its backup/restore evidence before guarded deletion.

Detailed artifacts are in the shared workspace at
`/root/mine-rootz/validation/mainnet-cpu-trial-20261014/`.
