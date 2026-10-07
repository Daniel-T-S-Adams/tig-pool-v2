# Preparing an isolated mainnet deployment

The templates in [deploy/v2-mainnet](../deploy/v2-mainnet) are a staging bundle,
not an enabled installation. They contain placeholders, no credentials, and
default to funds, work, settlement and TIG submissions being disabled. The
coordinator receives no TIG API key in this configuration.
Current setup progress is recorded in [Cloudflare setup](CLOUDFLARE_SETUP.md);
this document describes the full deployment and launch procedure.

## Inputs and roles

**Daniel as pool operator:** choose the hostname, server, independent archive
location and initial member count. The operator has authorized a maximum of
**10 TIG total** for the pool wallet's mainnet protocol-fee budget, plus the
previously stated **0.0005 Base ETH** native-transaction-fee ceiling per spending
wallet. This authorizes funding that fee balance; it does not enable benchmark
work or other pool spending. TIG top-ups consume TIG from the pool wallet.
Existing testnet collateral and observers remain on their own database.

**Funding status:** on 2026-10-06 the operator dashboard and independent
read-only mainnet TIG API captures agreed: 5 TIG total topped up, 5 TIG
available and 0 TIG deducted, against the authorized 10 TIG total budget. The
latest capture was at TIG height 1,372,652 and showed one confirmed top-up. Its
Base transaction was independently verified on chain 8453, finalized at Base
height 52,255,649, and transferred exactly 5 TIG from the pool wallet to TIG's
top-up address. A complete capture at TIG height 1,372,666 is now archived in
the mainnet funding spool and database (`21b0d47c…89acc5e`) and records the
confirmed top-up facts. The available 5 TIG has not yet been credited to the
pool ledger, so funding health correctly remains not-ready. No further top-up
is currently planned. The runtime role's missing INSERT permission on the
immutable protocol identity table was corrected narrowly and recorded in the
deployment SQL grant.

**Custody opening state:** the operator confirmed that all earlier wallet
transfers and the 10 TIG burn were setup for pre-launch mainnet testing. They
are not member deposits, collateral or pool rewards and will not be attributed
to individual pool activity. The mainnet database has no custody identity or
chain history, reservations or credited top-ups; it has one complete TIG
funding capture and its confirmed 5 TIG top-up fact. At finalized Base block
52,255,649 the wallet held 0 TIG and 0.000998944758821286 ETH, with outgoing
nonce 3. This historical value is a reference only; initialization must fetch a
fresh finalized opening snapshot.

Initialize custody with a named, immutable pre-launch baseline while new work
remains paused and before any pool reservations or withdrawals. The baseline
records the then-current TIG/native balances as operator opening funds and the
current outgoing nonce as the stream offset. It does not reconstruct or credit
the prior test transfers. The confirmed 5 TIG fee top-up is credited separately
to the operator's protocol-fee ledger only after a fresh TIG capture matches its
finalized Base transaction and exact transfer receipt. The custody collector is
still masked. The mainnet submission key is stored in the root-only
`/etc/innopool-v2-mainnet/submission.env`; submission and new work remain
disabled.

For [operator income payouts](OPERATOR_INCOME.md), the service template records
the operator's selected income wallet. The amount of operator TIG to retain for
running costs is still unset and must be chosen before using this configuration.
Financial operations remain disabled in the template. Migration 015 and the
matching API/website are required.

The **10 TIG** TIG protocol-fee budget and **0.0005 Base ETH** native-fee
ceiling are cumulative limits, not per-send limits. In the current design, the
pool's TIG submission account and Base custody wallet are the same address, so
the pool limits apply once to that shared account; member balances are internal
ledger entries and must not multiply the pool's spending allowance. These are
operator-monitored budgets; the software will not enforce them. Before and
during a pilot, the operator should check the TIG dashboard's total top-ups,
available fee balance and deductions, and the pool wallet's Base ETH balance
and transaction fees. Stop pool spending manually at the authorized budget.

**Codex assisting the pool operator, on the remote server:** complete the steps
below once those deployment details are known. The member browser runs on
Daniel's local computer. Worker execution belongs to the member role.

## Prepare the release and storage

1. Select pool/worker commits that passed local PostgreSQL/browser tests and
   hosted CI. Verify their tags and installer checksum using
   [the paired-release procedure](PAIRED_RELEASES.md). Record checksums of every
   applied migration, the installed dependencies, and the actual build commit.
   Extract the pinned Git revision into
   `/opt/innopool-v2-mainnet/releases/<commit>`; runtime users cannot write it.
2. Produce a **mainnet** runtime manifest for the actual worker architectures
   and current challenges. Do not copy the testnet ARM runtime manifest into
   production. The 23 September probe saw five CPU and three GPU challenges.
   Verify the actual digest and executable compatibility before a member joins.
3. Provision separate database and spool storage. The bounded mainnet probe
   captured three consecutive blocks with 2,223–2,233 active benchmarks, under
   a 384 MiB / 20%-of-one-CPU cap. The three compressed archives occupied about
   4.9 MiB: roughly 2.3 GiB/day per collector at one block/minute, before database
   expansion, reports, custody archives and backups. This short sample is an
   estimate, not a sustained capacity guarantee. Measure a longer run and size
   retention accordingly; the current server's approximately 26 GiB free is
   insufficient for a comfortable multi-round production archive.
4. Keep immutable raw evidence until all related collateral, disputes and
   settlement obligations are resolved and backed up. Do not delete old
   evidence merely to keep a service running. A second directory on the same
   host is not an independent archive.

The mainnet application slice has a separate 1.5-CPU/2-GiB ceiling, now used by
the preparation services on the dedicated VMs. Before sharing a host, review the
combined testnet, mainnet, database and worker limits; separate slice limits
alone do not impose a shared machine-wide ceiling. A worker belongs on its
member's machine for the final test.

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
   generate the key/CSR on Hetzner and obtain a Cloudflare Origin CA certificate
   covering both hosts. Keep Full (strict); verify the signed certificate and
   record/monitor expiry before activation. No HTTP/ACME challenge is needed.
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

The installed two-host backup schedule, retention limits and completed October
restore tests are described in [backup operations](BACKUP_OPERATIONS.md).
External alerts, sustained storage and funded takeover checks remain open.

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
- Rehearse worker interruption and restart on the separate member machine,
  then the bounded GPU path. Preserve worker SQLite and evidence files.
  A restarted worker must recover its existing benchmark before requesting more.

## Launch stages

Keep the existing testnet arbitration waiting in a separate checklist item.
Complete software tests, public historical replays, deposit withdrawals,
production storage/TLS setup and independent recovery now. Final reward
payment still requires the verified [TokenLocker lifecycle](REWARD_RECEIPTS.md)
and an actual custody receipt; its 28-day delay is not a reason to stop the
independent preparation work.

Before enabling a limited mainnet pilot, resolve the remaining authoritative
outcome/reporting and operator reward-call/correction integrations and confirm
the deployment inputs. The operator will monitor the authorized 10 TIG and
0.0005 Base ETH budgets manually; there is no software-enforced mainnet spending
cap. This repository's two-member pilot policy is **testnet-only** and separate
from the operator-monitored mainnet budget.

With the budget authorized and members ready, start a small monitored pilot.
Keep incomplete or unfunded reward
settlements held. Expand after its complete financial cycle is demonstrated.
