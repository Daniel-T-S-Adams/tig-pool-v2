# Preparing an isolated mainnet deployment

The templates in [deploy/v2-mainnet](../deploy/v2-mainnet) are a staging bundle,
not an enabled installation. They contain placeholders, no credentials, and
default to funds, work, settlement and TIG submissions being disabled. The
coordinator receives no TIG API key in this configuration.
Current setup progress is recorded in [Cloudflare setup](CLOUDFLARE_SETUP.md);
this document describes the full deployment and launch procedure.

## Inputs and roles

**Daniel as pool operator:** choose the hostname, server, independent archive
location and initial member count. Provide a separate mainnet TIG/ETH budget
before any transaction. The 5 TIG testnet limit does not authorize mainnet
spending. Existing testnet collateral and observers remain on their own database.

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

The mainnet application slice has a separate 1-CPU/2-GiB ceiling, now used by
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
   not policy mutation rights. Verify privileges before enabling any API action.
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
outcome/reporting and operator reward-call/correction integrations, confirm
the deployment inputs, and install an explicit mainnet attempt/funding cap.
This repository's two-member pilot policy is **testnet-only**. Do not disable
it and assume the 5 TIG allowance has become a mainnet spending limit.

After the operator authorizes the concrete mainnet budget and members are
ready, start a small monitored pilot. Keep incomplete or unfunded reward
settlements held. Expand after its complete financial cycle is demonstrated.
