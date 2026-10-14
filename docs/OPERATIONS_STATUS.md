# V2 operations and storage status

**14 October update:** the corrected two-server mainnet CPU benchmark activated
successfully at height **1383916**, around **11:35 UTC**. Results/proofs matched
TIG exactly; no verification failure was recorded. Master/helper saved 4,000
outputs (1,968/2,032), with results uploaded 12m40s after handover. Actual fee
0.001 TIG and collateral hold 0.0004 TIG stayed within the additional approval.
The earlier attempt failed because its runtime pin was outdated; both outcomes
are preserved in [the trial record](MAINNET_CPU_TRIAL_20261014.md). The corrected
pair remains a65b3a8/dddad9 with official 0.0.8 CPU runtimes. All trial approvals
are consumed/disabled; ordinary work is paused and settlement disabled.
Historical conversion finished except for the ordering conflict at 1382587;
complete replay now proves the cause, but its alert and raw originals remain.

Checked against the running services on **14 October 2026**. This is the current
operations record. Wallet RPC recovery was checked on **12 October**; backup
relocation, recovery verification and paired application activation passed
on **13 October**, as recorded below. Older dated validation notes
describe their own check state.

## Deployment and controls

The [lean storage implementation](LEAN_STORAGE.md) was activated at 13:28 UTC
on 13 October. It adds direct worker binary downloads,
compact pool evidence, selective final report fetching and guarded legacy
conversion. Production migrations are verified through 021. The matched worker,
public installer, compatible readers and unchanged balances were checked.
Historical conversion completed except for one held boundary block; corrected CPU timing passed; no primary raw
archive has been deleted by this rollout. See [the rollout record](LEAN_ROLLOUT_20261013.md).

The corrected 14 October two-host `c003_a154` test passed all five possible tracks,
including three 4,000-nonce assignments. The worst took 12m29s; all actual uploads and
sampled proofs passed an isolated API replay. This remains unpaid verification.
The subsequent [RPC routing repair](RPC_ROUTING_20261013.md) separates Base's
finality/basic reads from Pocket's log queries and recovered custody freshness.

On 11 October, local verification passed all 336 pool tests (paired worker and
Chromium enabled), 33 worker v2 tests, 17 inherited worker tests and both pool
accounting checks. A disposable upgrade, physical backup/restore and guarded
cleanup rehearsal also passed. See [verification details](LEAN_STORAGE_VERIFICATION.md).
These results do not change the last measured production state below.

The 12 October [release candidate](RELEASE_CANDIDATE_20261012.md) finishes member
onboarding and adds pending-file recovery/health tools. Its local suite passed
370 pool tests without skips, the exact paired worker's 46 v2/17 inherited tests,
and both accounting checks. Recovery capacity and combined production-copy
backup/restore and paired application activation passed on 13 October.
The backup health extension in `dfcfbf6` passed all 371 hosted tests.
See [the recovery activation record](RECOVERY_VOLUME_20261013.md).

| Item | Current state |
|---|---|
| Primary | Helsinki, `46.62.249.188`; website, API, coordinator and four observers |
| Recovery | Nuremberg, `2.28.230.81`; database backups, WAL receiver and configuration copies |
| Pool application | `a65b3a85d7d53285915ba6f75ac67df4cda503c5`, tag `mainnet-runtime08-member-master-20261014` |
| Worker served by the API | `dddad9df9b0d051cf004a904fe7b7361f9816c43`, tag `mainnet-runtime-release-guard-20261014` |
| Schema | 21 verified migrations deployed; 020/021 applied twice with earlier checksums unchanged |
| Member funds | Enabled, with custody freshness checks still applying |
| New work | Operator pause is recorded; effective `work_enabled=false`, configured `work_enabled=true` |
| TIG submission adapter | Configured for existing work; this does not bypass the pause on new precommits |
| Settlement | Disabled |
| Recovery pool | Application staged and collector units updated; API and collectors inactive |

`main` is the protected integration branch. `release/v2` follows the deployed
application tag. Documentation commits can advance `main` without changing the
running application or the worker served by the API. The worker's development
head may also differ from the pinned worker above.

The preceding operational cleanup passed all **299 pool tests**, including the
paired worker and Chromium flows, and the hosted `pool-baseline` check. See
[PR #38](https://github.com/Daniel-T-S-Adams/tig-pool-v2/pull/38). The earlier
abandoned-results expiry change passed the existing required hosted checks in
[PR #40](https://github.com/Daniel-T-S-Adams/tig-pool-v2/pull/40). It requires no
migration, dependency or worker change. Activated at 14:28 UTC on 9 October,
it preserved all ledger balances and journals with all nineteen stored
migration checksums verified; recovery staging completed at 14:27 UTC.

## CPU benchmark and protocol evidence

The mainnet CPU attempt `290447ad305de92a644ccb03fc4b967e` was handed over on
7 October at 17:18 UTC. Its complete results reached the pool on 9 October at
09:13 UTC. TIG rejected the result POST at 09:14 UTC with HTTP 400 and an
explicit response identifying the missing precommit. The original challenge
configuration allowed **120 blocks** of lifetime, approximately two hours at
one block per minute. This attempt did not activate and is not a successful
end-to-end benchmark validation.

The cleanup reconciles this particular rejection as `expired` only when its
immutable response matches the configured TIG origin, benchmark ID and raw
response hash, a complete later pool feed lacks the benchmark, and the archived
challenge lifetime has elapsed. The reservation slot is freed and the result
intent becomes `rejected`. The result POST is not retried. Collateral and
financial history remain intact for the existing end-of-X+2 finalization rules;
an expired work slot does not make collateral spendable immediately.

An acknowledged benchmark whose member never returns results now reaches the
same expiry and financial outcome automatically. Reconciliation requires the
archived creation block, a complete later pool feed with no records for the
benchmark, a strictly elapsed and unchanged challenge lifetime, and durable
proof that no results could have been sent. It frees the slot and cancels any
queued unsent result. Possibly sent or accepted results, proof intents and
authoritative sampling remain pending for their own reconciliation.

Both cases retain the original collateral until normal finalization after the
end of X+2. A handed-over benchmark that never activated forfeits that hold once
to its creation round's pot; it earns no qualifying credit and adds no member
submission-fee charge. See [the recovery rule](SUBMISSION_RECOVERY.md#a-member-never-returns-results).

Before another paid attempt, measure actual nonce running time on the intended
hardware and resource limits, and check that the whole protocol minimum fits
inside the lifetime with time for result/proof exchange. The completed pilot
does not establish that this CPU/algorithm combination can meet that deadline.

The isolated [11 October CPU hardware check](CPU_HARDWARE_VERIFICATION.md)
found that `poolminer1` had configured only one worker and one CPU on its
eight-CPU ARM64 host. With an independent six-worker budget, a real 200-nonce
knapsack benchmark completed in 20 minutes 35 seconds including setup and the
probe/resume pause. Its results and three proofs passed the isolated pool API.
The original job-scheduling algorithm did not finish a single nonce in a
480-second probe. Production controls and the existing worker installation
were unchanged; no real TIG submission was made.

A **block gap** means that no complete, validated snapshot exists for a required
TIG height. The snapshot supplies network qualifiers and the pool's protocol
feed used to assign exact member credit and reconcile outcomes. It does not
mean that PostgreSQL itself lost a transaction. Health currently lists:

- `1372249`: the recorded, one-time prelaunch waiver; the missing height stays
  visible and contributes zero pool credit under that restricted waiver.
- `1375817` and `1376650`: unresolved gaps in round 137, with no complete
  recoverable captures found. A later head or neighbouring snapshot cannot
  establish the missing block's qualifiers. Exact settlement for this round
  remains held. Do not interpolate credit or apply another prelaunch waiver
  after reservations exist.

**Report captures** are saved responses from TIG's fraud/arbitration reports and
its reportable-benchmark indices. Collectors write immutable manifests and
compressed content into a local **spool** before importing them into PostgreSQL.
`pending` means the files still await database import; `recorded` means that
import completed. A pending report is not necessarily a fraud against this pool.
Report evidence is needed later to establish final collateral outcomes.

The old importer replayed an entire network block for each report just to find
its height. PR #38 uses the validated immutable block header for that lookup;
conflicting or missing anchors remain errors. Full block replay is still used
where qualifier calculations or protocol deadlines require it. The archived
report backlog was cleared by 13:46 UTC on 9 October using two bounded passes
alongside the normal recorder. The pending queue reached zero; neither pass
had import errors. Incomplete upstream responses were preserved as incomplete
captures, not accepted as full report evidence. A few fresh captures can still
be pending briefly while the regular recorder imports them.

## Wallet observation

Wallet observation was repaired on **12 October 2026**. The primary API and
observer now use the verified public Pocket Base endpoint. At 13:11 UTC the
observer was reconciled and ready at Base **52510065**, recovering **169,056
blocks** from the previously stuck cursor. Account/journal fingerprints and the
member's 0.9995 TIG available balance were unchanged; every sampled audit passed.

The previous `mainnet.base.org` connection had rejected historical reads since
the earlier 9 October record. Candidate checks verified a known deposit/receipt,
complete historical ranges and canonical anchors before the switch. Catch-up
used 1,000-block batches with 0.25-second request spacing and one-second polls;
steady polling is now 30 seconds. Older supplier targets and rate limits are
still rejected under the existing freshness/reconciliation rules.

Encrypted settings, the exact observer unit and protected rollback files were
copied to Germany and verified. No new provider account, deposit, paid benchmark,
application release or schema change was made. New work remains paused for the
remaining paired-release and paid-test checks. See
[the recovery record](RPC_RECOVERY_20261012.md).

## What is stored, and why

| Store | Purpose | Protection / retention |
|---|---|---|
| PostgreSQL | Member ownership, ledger, holds, outcomes, credit and parsed protocol evidence | Daily verified base backups plus continuous WAL; financial/credit/settlement records are permanent |
| Protocol spool | Raw responses saved before database import, plus recorded archives | Local immutable manifests and compressed content; raw evidence follows the approved policy |
| Worker SQLite and nonce files | Member-owned assignment and saved computation evidence | Retained by that worker; server backups do not back up a member's machine |
| Release and configuration | Exact application, settings, credentials and TLS | Encrypted daily exports copied to Germany; exact deployed release checked |

**WAL** means PostgreSQL's *write-ahead log*: database changes are logged before
their data pages are written. Restoring a base backup and replaying the later
WAL reconstructs later committed database state. The German receiver saves WAL
files; it does not continuously apply them to a running replacement pool.
See [PostgreSQL's WAL introduction](https://www.postgresql.org/docs/18/wal-intro.html)
and [receiver documentation](https://www.postgresql.org/docs/18/app-pgreceivewal.html).

Normal runtime and migration-owner commits require the German receiver's disk
acknowledgment. The 14:55 storage trace confirmed that all six running primary
services, including the four collectors, use `innopool_runtime` with
`synchronous_commit=on`. The separately configured `innopool_observer` role uses
local commits, but these services do not currently use it. Collector files can
still be saved locally while database import waits for the receiver.
Database WAL does **not** contain spool files that have not
yet entered the database, or worker files on another machine. A backup is not
an independent TIG collector: it cannot reconstruct an observation never made.
See [the storage map](STORAGE_MAP.md) for data sources, paths, transfer schedules
and the measured database breakdown.

The approved evidence policy keeps raw blocks and reports for four rounds and
wallet/fee captures for thirty days, with obligation, credit-coverage and backup
guards. It expires database payloads and prunes eligible local **block** spool
files. Report, wallet and funding spool files do not yet have equivalent file
pruning. Expired database rows free space for PostgreSQL reuse; returning that
space to the operating system requires a separately planned table rewrite.
See [evidence retention](EVIDENCE_RETENTION.md).

### Measured footprint

Approximate binary GiB, measured 9 October before the report catch-up:

| Host / data | Size |
|---|---:|
| Primary database | 26.4 GiB |
| Primary block spool | 21.2 GiB |
| Primary report spool | 2.1 GiB |
| Primary custody + funding spools | 2.1 GiB |
| Primary free disk | 83.9 GiB |
| Recovery retained base backups | 38.6 GiB |
| Recovery WAL files | 49.6 GiB |
| Recovery configuration/manual exports | 2.9 GiB |
| Recovery free disk | 46.6 GiB |

These are observations, not fixed capacity budgets. The primary collects
network-wide data even when new work is paused or membership is small. Database
import and subsequent backups can change the footprint during catch-up.

## Recovery protection and remaining cleanup

The continuous WAL receiver and transfer jobs are active. The latest successful
base completed on **10 October at 03:46 UTC**, with archive checksums and required
WAL verified. Later daily attempts failed the disk reserve check. Three verified
bases remain with WAL back to the oldest base. The actual point-in-time restore
rehearsal passed on 4 October; these backup records are not a new restore check.

The earlier cleanup application and encrypted configuration arrived on Germany
at 13:24 UTC. The current expiry release and encrypted settings were exported
at 14:29 UTC and copied there, with all exported-file checksums verified. This configuration copy
does not advance the database recovery point. The primary ledger audit passed
after the update and catch-up; new work remained paused throughout.

The former **full raw-evidence copy is disabled**, following its 8 October
disk-reserve failure. Germany's independent block/report collectors have been
inactive since 7 October. The old recovery health helper still expects those
collectors and a full evidence copy; its overall result does not accurately
describe the current backup-only host. Treat each backup job's evidence
separately until that health profile is corrected.

The proposed storage setup for discussion is:

1. Keep verified database bases plus WAL, and encrypted release/configuration
   copies.
2. Copy raw protocol manifests and content that are **still pending import**
   separately, since they are outside the database recovery stream.
3. Define the required freshness and disk reserve for that copy, and rehearse
   restoring the database plus pending files together.
4. Choose whether an independent TIG collector is also required. It protects
   missed observations and needs its own storage; it is a separate decision
   from copying the primary's files.

On **12 October**, the pending-file copy and matching recovery health profile
were implemented for the release candidate. They cover all four protocol spools,
retain two verified snapshots, require a durable WAL boundary and enforce five
minutes of freshness. They were **activated and production-copy verified on
13 October**; see
[pending evidence protection](PENDING_EVIDENCE_BACKUP.md).

The historical 12 October survey found about 15 GiB free, with 44 GiB of bases
and 76 GiB of required WAL; new daily backups could not start. On 13 October the
owner attached a 200 GiB EXT4 volume. Relocation, a fresh base and combined
production-copy restore passed, and normal backup retention resumed. The
obsolete original copy was removed afterward. Root had about 135 GiB free and
the volume about 82 GiB free. Recovery health was healthy at 12:18 UTC.
Production still uses the old larger application storage design.

Re-enabling the existing full copy would add
roughly 24 GiB of block/report files and consume space reserved for the next
base backup. No raw evidence was deleted to make the job pass. Storage policy,
unresolved gaps remain open; keep new work paused while these are addressed.
The repaired public RPC remains subject to ordinary custody availability/freshness gates. Reward claim/unlock/receipt and expense integration, a funded
takeover, real TIG acceptance of timely CPU results and GPU execution also
remain unvalidated. The isolated knapsack measurement above covers one CPU
workload only.
