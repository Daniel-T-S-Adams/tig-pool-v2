# Preparing an isolated mainnet deployment

The templates in [deploy/v2-mainnet](../deploy/v2-mainnet) are a staging bundle,
not an enabled installation. They contain placeholders, no credentials, and
default to funds, work, settlement and TIG submissions being disabled. The
coordinator receives no TIG API key in this configuration.
The templates describe safe starting defaults; a live deployment enables the
controls (funds, submission recovery, new work, settlement) one at a time as
the checks below pass. This document describes the procedure; the
funding/opening notes below describe what to establish before initialization.

## Inputs and roles

**The pool operator:** choose the hostname, server, independent archive
location and initial member count, and authorize the mainnet budgets: a
maximum total for the pool wallet's mainnet protocol-fee top-ups (for example
**10 TIG**) and a native-transaction-fee ceiling per spending wallet (for
example **0.0005 Base ETH**). This authorizes funding that fee balance; it does
not enable benchmark work or other pool spending. TIG top-ups consume TIG from
the pool wallet. Existing testnet collateral and observers remain on their own
database.

**Funding check:** before initialization, confirm that the operator dashboard
and an independent read-only mainnet TIG API capture agree on the total topped
up, available and deducted protocol fee against the authorized budget, and
that each top-up's Base transaction is independently verified on chain 8453,
finalized, and transferred exactly the expected amount from the pool wallet to
TIG's top-up address. A complete funding capture archived in the mainnet
funding spool and database records the confirmed top-up facts. Until the
available fee balance is credited to the pool ledger, funding health correctly
remains not-ready. The runtime role needs INSERT permission on the immutable
protocol identity table; this is recorded in the deployment SQL grant.

**Custody opening state:** any earlier wallet transfers and TIG burn made for
pre-launch mainnet testing are setup. They are not member deposits, collateral
or pool rewards and are not attributed to individual pool activity. A fresh
mainnet database has no custody identity or chain history, reservations or
credited top-ups; it may hold a complete TIG funding capture and its confirmed
top-up fact. Any balance noted during preparation is a reference only;
initialization must fetch a fresh finalized opening snapshot.

Initialize custody with a named, immutable pre-launch baseline while new work
remains paused and before any pool reservations or withdrawals. The baseline
records the then-current TIG/native balances as operator opening funds and the
current outgoing nonce as the stream offset. It does not reconstruct or credit
the prior test transfers. A confirmed fee top-up is credited separately to the
operator's protocol-fee ledger only after a fresh TIG capture matches its
finalized Base transaction and exact transfer receipt. The mainnet submission
key is stored in the root-only `/etc/innopool-v2-mainnet/submission.env`;
keep submission and new work disabled until the launch stages below.

For [operator income payouts](OPERATOR_INCOME.md), the service template records
the operator's selected income wallet (`<income wallet>`). The amount of
operator TIG to retain for running costs must be chosen before using this
configuration. Financial operations remain disabled in the template. Migration
015 and the matching API/website are required.

The protocol-fee budget and the native-fee ceiling are cumulative limits, not
per-send limits. In the current design, the pool's TIG submission account and
Base custody wallet are the same address (`<pool custody wallet>`), so the pool
limits apply once to that shared account; member balances are internal ledger
entries and must not multiply the pool's spending allowance. These are
operator-monitored budgets; the software will not enforce them by itself (see
the spending cap under [launch stages](#launch-stages)). Before and during a
pilot, the operator should check the TIG dashboard's total top-ups, available
fee balance and deductions, and the pool wallet's Base ETH balance and
transaction fees. Stop pool spending manually at the authorized budget.

**On the remote server:** complete the steps below once those deployment
details are known. The member browser runs on the member's own computer.
Worker execution belongs to the member role.

## Initialization status

Once the named prelaunch custody baseline and confirmed protocol-fee credit
have been initialized, the custody and funding observers are running, and
member registrations plus benchmark reservations exist, do not rerun the
prelaunch baseline or assume that no pool activity has occurred. Public RPC
failures hold the ordinary custody gates until wallet catch-up completes.
Recovery is a backup host with inactive independent collectors and an
optional, disabled-by-default raw-file copy; both choices are listed in
[the roadmap](ROADMAP.md). Monetary budgets remain operator-monitored.

## Prepare the release and storage

1. Select pool/worker commits that passed local PostgreSQL/browser tests and
   hosted CI. Verify their tags and installer checksum using
   [the paired-release procedure](PAIRED_RELEASES.md). Record checksums of every
   applied migration, the installed dependencies, and the actual build commit.
   Extract the pinned Git revision into
   `/opt/innopool-v2-mainnet/releases/<commit>`; runtime users cannot write it.
2. Produce a **mainnet** runtime manifest for the actual worker architectures
   and current challenges. Do not copy the testnet ARM runtime manifest into
   production. Probe the mainnet API for the current set of CPU and GPU
   challenges. Verify the actual digest and executable compatibility before a
   member joins.
3. Provision separate database and spool storage. A bounded probe capturing
   three consecutive full-network blocks with about 2,200 active benchmarks,
   under a 384 MiB / 20%-of-one-CPU cap, produced about 4.9 MiB of compressed
   archives: roughly 2.3 GiB/day per collector at one block/minute in the
   full-network format, before database expansion, reports, custody archives
   and backups. This short sample is an estimate, not a sustained capacity
   guarantee; the pool-scoped capture format ([lean storage](LEAN_STORAGE.md))
   writes less per block. A longer-running full-network deployment measured a
   primary database of about 26 GiB, a block spool of about 21 GiB, and
   recovery bases plus WAL of about 88 GiB. Size retention and copy staging
   from measured growth.
4. Keep immutable raw evidence until all related collateral, disputes and
   settlement obligations are resolved and backed up. Do not delete old
   evidence merely to keep a service running. A second directory on the same
   host is not an independent archive.

The mainnet application slice has a separate 1.5-CPU/2-GiB ceiling. Before
sharing a host, review the combined testnet, mainnet, database and worker
limits; separate slice limits alone do not impose a shared machine-wide
ceiling. A worker belongs on its member's machine for the final test.

## Isolate database and credentials

1. Create a separate PostgreSQL database `innopool_v2_mainnet`, distinct from
   the testnet database and from every name ending in `_test`. Use the pinned,
   tested PostgreSQL version. If containerized, give it an explicit CPU/RAM
   limit, loopback-only port, unique volume and a supervised startup order.
2. Use a migration owner and a separate runtime role. The runtime role must
   not be superuser, create roles/databases/schemas, own the schema or alter
   tables/triggers. Migrate with the owner, then grant only runtime operations.
   Give the runtime SELECT on migration history and pilot policy/phase tables,
   not policy mutation rights. Public fee collection also needs INSERT on the
   immutable `protocol_identity` row; apply
   [the observer grant](../deploy/v2-mainnet/grant-runtime-observer-privileges.sql).
   Verify privileges before enabling any API action.
3. Create a dedicated `innopoolmainnet` Unix account. Keep reviewed settings in
   `/etc/innopool-v2-mainnet` and persistent spools in
   `/var/lib/innopool-v2-mainnet/spool`. Keep credentials outside repositories,
   root-owned and mode 0600; systemd reads its EnvironmentFile before changing
   user. Do not print or paste environment files into chat.
4. Copy and fill the example JSON and environment files, removing the
   `.example` suffix. Generate a fresh operator token. Do not copy the testnet
   DSN, token, member sessions or fee grant. Leave all enable flags false.
   `/etc/innopool-v2-mainnet/deployment.json` carries a `hosts` object with
   the `primary` and `recovery` addresses; the backup, restore and
   pending-evidence tools read it, and the pending-evidence receiver refuses a
   source that is not the recorded primary.
5. Verify the selected pool wallet's actual mainnet history and starting
   balances before initializing custody. The same address can have different
   state on Base and Base Sepolia. Record the first Base block before its
   mainnet funding/use; do not invent an empty opening state.
6. Replace every `REPLACE_` value in the staged files. The actual Base network
   must be 8453 with the configured mainnet TIG token; current TIG metadata has
   advertised a mismatched chain ID and cannot be the sole source of truth.
   Verify the RPC and token through direct read-only calls.

## HTTPS and services

1. Point the proxied website and API hostnames to the server. For this setup,
   generate the key/CSR on the primary server and obtain a Cloudflare Origin CA
   certificate covering both hosts. Keep Full (strict); verify the signed
   certificate and record/monitor expiry before activation. No HTTP/ACME
   challenge is needed.
   Configure `origin` as the website address and
   `api_origin` as the worker/dashboard API address; omitting `api_origin`
   preserves a same-origin deployment. Set the coordinator's `public_origin`
   to the API address so benchmark artifact URLs use it too. Wallet signatures
   stay bound to the website origin. Route public website files and API paths
   to `127.0.0.1:18080`; keep that listener private. The API permits browser
   access only from the configured website and requires existing bearer
   permissions. Trust forwarded headers only from the configured proxy.
   See [Cloudflare setup](CLOUDFLARE_SETUP.md) for the two-host deployment.
   HTML and all API responses use `no-store`; only content-hashed public
   website assets use long-lived immutable caching. Keep previous release
   assets available across a rollout, and bypass API caching at the proxy.
2. Install the six filled service files and slice, after checking executable
   paths and the pinned commit. Establish startup ordering after the database
   and network. The examples deliberately do not assume how PostgreSQL is hosted.
3. Initialize accounts and the durable pause control in the fresh database.
   Record the TIG start height and intended first **complete** reward round.
   Start collection before that round. Never pretend that its earlier blocks
   were captured.
4. Start API, custody, fee, block and report observation. The paused coordinator
   may run to recover known work, but has no submission capability. Confirm
   schema checksums, chain identity, freshness, ledger audit and balance
   reconciliation. Start the independent collector before allowing paid work.
5. Recheck every active challenge for reporting-index coverage. Pin older
   reporting rounds while they have unresolved obligations; polling only the
   latest four rounds is insufficient after a prolonged outage.
6. Enable boot startup only after this ordering has been rehearsed. Test a
   service restart with work disabled and confirm that the same build,
   collector cursors and paused flags return.

A mainnet observation deployment does not authorize member deposits, benchmark
submissions, reward contract calls or withdrawals.

## Backups, recovery and monitoring

The two-host backup schedule, retention limits and restore rehearsal are
described in [backup operations](BACKUP_OPERATIONS.md). External alerts,
sustained storage and funded takeover are listed in [the roadmap](ROADMAP.md).

- Archive immutable spools on another host as they arrive; monitor both
  collectors for missed/conflicting blocks. Store database backups and the
  release/configuration manifest off-host. Protect a separate encrypted copy
  of credentials; do not include credentials in public validation records.
- Record database dump checksums, creation time and the matching spool
  manifest. Verify the dump's archive list before reporting backup success.
  Choose a retention policy after measuring daily volume; no automatic
  deletion of unresolved evidence is authorized by this template.
- Rehearse restore to a new database named `innopool_v2_mainnet_recovery_test`
  on the separate host. Check the target name before restoring. Attach no
  live API or submission credentials. Migrate only to the pinned release,
  replay retained observations, audit the journal and compare all balances,
  collateral holds, pending withdrawals and potentially sent attempts.
- Do not restore an old database over a live pool as a routine rollback:
  that could erase transactions accepted since the backup. Pause new work,
  keep collecting, reconcile ambiguous sends and use a forward fix or a
  compatible prior application revision without resetting financial history.
- Monitor database/disk free space, observer freshness/gaps, spool backlog,
  custody and protocol-fee reconciliation, ledger audit, active service build,
  pending send ambiguity and backup age. Monitor from another host too;
  alerts must be arranged with an explicitly chosen destination.
  The primary's five-minute check is `tools/mainnet_health_check_v2.py`,
  installed as `operations/mainnet_health_check.py`. It judges block history
  by the configured missing-block tolerance: held rounds, a current round
  already over the limit and unresolved conflicting heights are issues;
  tolerated and waived gaps are recorded only. Release activations must not
  overwrite it with an older copy.
- Rehearse worker interruption and restart on the separate member machine,
  then the bounded GPU path. Preserve worker SQLite and evidence files.
  A restarted worker must recover its existing benchmark before requesting more.

## Launch stages

Keep any outstanding testnet arbitration in a separate checklist item.
Complete software tests, public historical replays, deposit withdrawals,
production storage/TLS setup and independent recovery now. Final reward
payment still requires the verified [TokenLocker lifecycle](REWARD_RECEIPTS.md)
and an actual custody receipt; its 28-day delay is not a reason to stop the
independent preparation work.

Before enabling a limited mainnet pilot, resolve the remaining authoritative
outcome/reporting and operator reward-call/correction integrations and confirm
the deployment inputs. The authorized budgets are enforced in software by the
operator's recorded spending cap (migration 027): without a cap row the pool
commits no submission fee and reserves no custody gas, and every committed
ceiling counts in full inside the rolling window. The operator records the cap
through `POST /api/v2/operator/spending-cap` or the operator screen; the
health check reports a missing cap or a window more than 90% committed. This
repository's two-member pilot policy is **testnet-only** and separate from the
mainnet cap.

With the budget authorized and members ready, start a small monitored pilot.
Keep incomplete or unfunded reward
settlements held. Expand after its complete financial cycle is demonstrated.
