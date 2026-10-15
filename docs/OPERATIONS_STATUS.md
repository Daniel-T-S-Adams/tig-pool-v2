# V2 operations and storage status

**15 October update:** `main` now carries every ready feature, and the pair
pool `4e64bf3` (tag `mainnet-pool-scoped-capture-20261015`) with worker
`c6eccfb` (tag `mainnet-library-cache-20261015`) was deployed at **11:02 UTC**
with additive migrations 023 to 025 behind restore point
`innopool_pool_scoped_20261015`. The financial digest was unchanged before and
after. The collector now records only the pool's own feed per block; the first
scoped captures at heights 1385324 onward were complete and credited. The
restart re-captured height **1385323**, already recorded compact, which the
store flagged as alert 5. Two follow-up releases the same day added the
collector resume guard and migration 026 (pool `be87fdc`, 11:51 UTC) and the
corrected boundary proof (pool `e3cf0d2`, tag `mainnet-boundary-proof-20261015`,
12:06 UTC); alert 5 was resolved at **12:07 UTC** under
`format-boundary-projection-v1` with every alert retained, no conflicting
heights remain, and each later restart captured every height exactly once.
New work stays paused and settlement disabled. See
[the rollout record](POOL_SCOPED_ROLLOUT_20261015.md). At **15:21 UTC** one
owner-approved benchmark ran on this release through a scoped window for member
`cc713240` on both of its machines: TIG activated benchmark `3685bada…` with no
fraud, results were uploaded 12m26s after handover, the coordinator read its
first on-demand selection reference, and the fee and collateral stayed at the
0.001 and 0.0004 TIG caps. See [the trial record](MAINNET_CPU_TRIAL_20261015.md).
The recovery base backup failure, the retired evidence copy's stale state and
the primary health check were fixed the same day (issues #50 to #52).

**14 October update:** the corrected two-server mainnet CPU benchmark activated
successfully at height **1383916**, around **11:35 UTC**. Results/proofs matched
TIG exactly; no verification failure was recorded. Master/helper saved 4,000
outputs (1,968/2,032), with results uploaded 12m40s after handover. Actual fee
0.001 TIG and collateral hold 0.0004 TIG stayed within the additional approval.
The earlier attempt failed because its runtime pin was outdated; both outcomes
are preserved in [the trial record](MAINNET_CPU_TRIAL_20261014.md). The worker/runtime pair remains unchanged; the pool now runs the storage fix
with official 0.0.8 CPU runtimes. All trial approvals
are consumed/disabled; ordinary work is paused and settlement disabled.
The [storage ordering fix](STORAGE_ORDERING_FIX_20261014.md) is deployed.
All 26,050 historical blocks are converted; alert 2 has an immutable replay
resolution. Fresh backup, actual restore and guarded redundant raw cleanup passed.

Checked against the running services on **14 October 2026**. This is the current
operations record. Wallet RPC recovery was checked on **12 October**; backup
relocation, recovery verification and paired application activation passed
on **13 October**, as recorded below. Older dated validation notes
describe their own check state.

## Open items

Tracked as GitHub issues. Open:
[#54](https://github.com/Daniel-T-S-Adams/tig-pool-v2/issues/54) verifying the
reward settlement of the 14 and 15 October benchmarks before settlement is
enabled; both earned zero emissions, so the rounds demonstrate the
zero-earnings path only, with round 137 sealable from 26 October and round 138
from 2 November. See [the verification plan](SETTLEMENT_VERIFICATION_PLAN.md).
Closed on 15 October:
[#50](https://github.com/Daniel-T-S-Adams/tig-pool-v2/issues/50) recovery base
backup fixed and verified;
[#51](https://github.com/Daniel-T-S-Adams/tig-pool-v2/issues/51) retired raw
evidence copy, stale unit state cleared;
[#52](https://github.com/Daniel-T-S-Adams/tig-pool-v2/issues/52) primary health
check now judges held rounds and conflicts;
[#53](https://github.com/Daniel-T-S-Adams/tig-pool-v2/issues/53) one bounded
benchmark completed on the deployed release, ordinary work still paused.

## Deployment and controls

The [lean storage implementation](LEAN_STORAGE.md) was activated at 13:28 UTC
on 13 October. It adds direct worker binary downloads,
compact pool evidence, selective final report fetching and guarded legacy
conversion. Production migrations are verified through 026 since 15 October. The matched worker,
public installer, compatible readers and unchanged balances were checked.
Historical conversion, backup/restore verification, guarded redundant cleanup
and corrected CPU timing passed. See [the rollout record](LEAN_ROLLOUT_20261013.md).

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
| Pool application | `e3cf0d2dda69a6bfe393716b0f5eabc7b16e6f65`, tag `mainnet-boundary-proof-20261015` |
| Worker served by the API | `c6eccfb4f7bf4fc1ceb448f80983dd954bfc99ef`, tag `mainnet-library-cache-20261015`; installer bytes unchanged |
| Schema | 26 verified migrations deployed; 023 to 026 applied twice on 15 October with earlier checksums unchanged |
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
  establish the missing block's qualifiers, and TIG's public API serves only
  the latest block. On 14 October the owner decided not to pursue recovery and
  chose the
  [bounded missing-block tolerance](ROUND_SETTLEMENT.md#bounded-missing-block-tolerance):
  once deployed, up to 50 missing heights per round count as zero pool credit,
  so the three round-137 heights no longer hold that round. Rounds missing more
  are held for review. Credit is never interpolated and the prelaunch waiver is
  not reused.

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
application release or schema change was made during that repair. New work
stayed paused; the later corrected CPU activation is recorded above. See
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
files. Report spool files now have backed-payload pruning. Wallet/funding files
continue to use their existing evidence controls. Expired database rows free space for PostgreSQL reuse; returning that
space to the operating system requires a separately planned table rewrite.
See [evidence retention](EVIDENCE_RETENTION.md).

### Current footprint — 14 October, 17:31 UTC

Binary GiB after completed cleanup and archive relocation:

| Host / data | Size |
|---|---:|
| Primary allocated database | 35.77 GiB |
| Primary block spool | 0.67 GiB |
| Primary report spool | 0.18 GiB |
| Primary custody spool | 0.31 GiB |
| Primary funding spool | 6.98 GiB |
| Primary free disk | 90.34 GiB |
| Recovery newest base on volume | 23.48 GiB |
| Recovery two older bases on root | 44.47 GiB |
| Recovery required WAL | 97.63 GiB |
| Recovery volume free disk | 64.59 GiB |
| Recovery root free disk | 89.91 GiB |

Block/report spool allocation fell by about **33 GiB**. Allocated database
space includes reusable pages; its logical replacement payloads are much
smaller. Required WAL includes the cleanup activity until the relevant older
bases retire under normal retention. The [base placement record](BACKUP_BASE_ARCHIVES_20261014.md)
explains the two filesystem reserves and unchanged three-base/WAL policy.

### Historical footprint — 9 October


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

The current recovery path is verified database bases plus continuous WAL,
encrypted release/configuration copies and bounded snapshots of the four
pending protocol spools. The 200 GiB EXT4 volume **107125099** holds the newest base,
WAL and pending snapshots through persistent guarded mounts. Two older verified
bases use the recovery server's existing root disk. The catalogue/retention and
per-filesystem reserve checks cover both locations; see
[the 14 October placement record](BACKUP_BASE_ARCHIVES_20261014.md). Three verified
bases and WAL back to the oldest required base remain under the existing
retention policy. Recovery's pool API and independent TIG collectors remain
inactive; backup protection does not recreate a block never captured.

On **14 October**, base `2026-10-14T134951Z` passed data checksums and WAL
parsing. A separate actual restore used the verified 03:13 UTC base plus 229
later WAL segments to reach `innopool_storage_20261014_134952`. All 26,050
compact copies, all 47 protected table fingerprints, the ordering resolution,
retained alert and 22 migration checksums passed. The disposable database was
removed afterward. The encrypted configuration export
`configuration-daily-2026-10-14T134952Z` was verified on Germany, including
private decryption, all file hashes, the active pair, consumed trial approvals,
RPC router and service/retention directories; temporary plaintext was removed.

The old full raw-spool mirror stays disabled. The replacement pending-file
protection covers blocks, reports, custody and funding, retains two verified
snapshots and requires a durable WAL boundary plus five minutes of freshness.
It was activated and verified on 13 October. See
[pending evidence protection](PENDING_EVIDENCE_BACKUP.md) and
[the recovery-volume record](RECOVERY_VOLUME_20261013.md).

Historical compact conversion is complete. Guarded redundant raw/file/chunk
cleanup completed after the new backup and actual restore checks. See
[the storage ordering fix](STORAGE_ORDERING_FIX_20261014.md) for final counts
and its immutable replay proof. Ordinary PostgreSQL expiry makes pages
reusable; allocated database files and retained recovery bases do not
immediately shrink to the live logical data size.

New work remains paused and settlement disabled. The round-137 gaps at
**1375817** and **1376650** fall within the bounded missing-block tolerance
implemented on 14 October, which is not yet deployed. Reward claim/unlock/receipt and
expense integration, a funded takeover and real GPU execution remain
unverified. The corrected two-server CPU benchmark **did** activate on TIG;
point checks showed no pool qualifiers, so it does not demonstrate a reward
cycle or profitability. The RPC router remains subject to custody freshness
and ordinary availability gates.
