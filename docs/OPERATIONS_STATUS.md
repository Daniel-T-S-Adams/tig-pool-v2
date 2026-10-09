# V2 operations and storage status

Checked against the running services on **9 October 2026**. This is the current
operations record. Older dated validation notes describe the state at the time
of their checks; they do not override this record.

## Deployment and controls

| Item | Current state |
|---|---|
| Primary | Helsinki, `46.62.249.188`; website, API, coordinator and four observers |
| Recovery | Nuremberg, `2.28.230.81`; database backups, WAL receiver and configuration copies |
| Pool application | `8477509afafe80b59f8fa7752b5faa0f537fedb2`, tag `mainnet-abandoned-benchmark-expiry-20261009` |
| Worker served by the API | `fd29279ce816c9f4e6e1416715971201c92775e0`, tag `mainnet-cpu-pilot-20261007` |
| Schema | 19 verified migrations; no migration required for this cleanup |
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
[PR #38](https://github.com/Daniel-T-S-Adams/tig-pool-v2/pull/38). The current
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

Custody observation last caught up successfully on 8 October at 15:12 UTC,
through Base height `52340909`. The public Base RPC rejects or rate-limits the
historical log requests needed to advance. A quiet, spaced request also failed;
other tested public endpoints rejected historical reads or returned service
errors. This is an RPC access failure, not evidence of a ledger balance mismatch.

The observer now spaces calls by two seconds, polls at sixty seconds and records
the failed RPC method plus numeric HTTP/RPC status without exposing provider
credentials. It still needs a usable Base endpoint for historical `eth_call`
and `eth_getLogs`. New spending remains subject to custody reconciliation.
No replacement provider or credential was configured during this cleanup.

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

The continuous WAL receiver and transfer jobs are active. The daily 9 October
base backup completed at 03:42 UTC with archive checksums and required WAL
verified. Three verified daily bases are retained with WAL back to the oldest
base. The actual point-in-time restore rehearsal passed on 4 October; today's
checksum verification is not a new restore rehearsal.

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

This proposal is not installed. Re-enabling the existing full copy would add
roughly 24 GiB of block/report files and consume space reserved for the next
base backup. No raw evidence was deleted to make the job pass. Storage policy,
RPC access and unresolved gaps remain open; keep new work paused while these
are addressed. Reward claim/unlock/receipt and expense integration, a funded
takeover, timely CPU completion and GPU execution also remain unvalidated.
