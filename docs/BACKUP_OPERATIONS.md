# Mainnet backup and recovery operations

The pool runs on two hosts. The primary host serves members and runs the
collectors; the independent recovery host is a backup host, with its API and
independent block/report collectors inactive. Which live controls are enabled
(member funds, new work, settlement) is the operator's decision; the open
decisions are listed in [the roadmap](ROADMAP.md).

## Installed protection

| Protection | Where / cadence | What it preserves |
|---|---|---|
| PostgreSQL WAL receiver | Recovery server, continuously | Changes after a database backup, including ledger records and migrations. |
| Compressed database backup | Recovery server, daily around 03:10 UTC | Entire primary PostgreSQL cluster; data checksums and required WAL are checked before publication. |
| Encrypted configuration export | Primary, daily around 02:30 UTC | Credentials, TLS key/certificate, service configuration, operational helpers, exact application and retained website assets. |
| Configuration/archive pull | Recovery server, every five minutes | Published primary exports over restricted read-only SSH. |
| Raw evidence copy | Optional; disabled by default | A full copy of the primary block/report spool. Block evidence lives in the database as compact copies, protected by the WAL receiver and daily backups, and files not yet recorded are covered by the pending-file snapshot below. With lean storage the primary's block and report spool is small (about 1 GiB), so the copy can be enabled cheaply if the operator wants a second raw-file copy; the recovery health profile then switches from `pending` to `full`. |
| Pending evidence snapshot | Recovery server, one minute after the previous copy finishes | Copies only pending block/report/custody/funding manifests and their referenced chunks, with a durable WAL boundary; two verified snapshots are retained. |
| Health checks | Installed helpers and timers | Checks database/WAL/settings/pending-file protection, actual destination reserves and inactive recovery pool services. |

## Capacity and base placement

The newest verified base, its required WAL and the pending-file snapshots stay
on a dedicated EXT4 volume mounted at `/mnt/<volume>`. Older retained bases use
the recovery server's spare root disk; the catalogue, the three-base retention
and the filesystem reserve checks cover both locations. Moving archives to a
new volume is complete only after a fresh verified base and a combined
production-copy restore pass; the obsolete original copy is removed only
afterward.

When daily base backups fail the disk reserve check, the WAL stream stays
active, but a stale base and a low reserve are release blockers. Run the
read-only retention plan first; when it finds no eligible bases and no
eligible WAL segments, attach more storage. Do not delete required recovery
data or reduce the three-base policy to make the job pass.

The optional raw evidence copy runs its interval after each completion and
does not guarantee five-minute freshness. Database WAL is a separate stream
and cannot protect protocol files that have not entered the database, or
supply a TIG block never observed. The pending-file snapshot covers files
awaiting import; whether to run an independent collector on the recovery host
is a separate storage and coverage decision ([roadmap](ROADMAP.md)).

The backup connection uses a dedicated replication-only PostgreSQL role and an
SSH account restricted to forwarding the primary's loopback database port.
The evidence key can read only the spool directory. Neither connection can
activate the recovery pool or send tokens. Secrets remain outside Git.

Normal runtime and migration-owner database commits require acknowledgment from
the WAL receiver on the recovery server. The receiver uses `--synchronous`,
flushing WAL to disk before acknowledging it. The API, coordinator, blocks,
reports, custody and funding services all use `innopool_runtime` with
`synchronous_commit=on`. The separate observer role is configured for local
commits but is not used by those live services. Database imports therefore
also wait for the receiver; the collectors' separate capture threads can
continue saving local spool files. This protects commit acknowledgment; it is
**not automatic failover** or proof that an in-flight request cannot be
visible before its original caller receives acknowledgment. A funded takeover
must still test concurrent/retried requests and reconcile potentially sent
payments before work resumes. Enabling the member-funds capability does not
complete a funded takeover rehearsal.

The replication slot may retain at most 4 GiB on the primary. A sufficiently
long interruption can invalidate it: investigate and establish a new verified
base/stream if necessary. Do not silently recreate the slot and claim the
missing WAL was recovered. Do not disable synchronous protection simply to
clear a waiting transaction.

## Retention and resource limits

The backup tool retains at least **three complete verified daily base backups**.
The newest stays on the volume and older retained bases use the configured root
archive. Archive copies are checksum verified before redundant sources are
removed; the catalogue and WAL floor include both locations.
Only after checking the retained archives may it remove older redundant bases
and WAL preceding the oldest retained base on the same timeline. Different
cluster identities or timelines stop automatic pruning. Partial WAL, timeline
history, original manual snapshots, raw protocol evidence and unresolved
financial records are not deleted by this policy.

A base backup or checksum verifier is limited to two CPUs and 768 MiB RAM.
The WAL receiver uses at most a quarter CPU and 128 MiB. Evidence copying and
configuration exports have separate systemd limits. Full evidence verification
uses two CPUs and 1.5 GiB. These run on the dedicated recovery VM; no local
benchmarking worker runs there. The primary application slice uses 1.5 CPUs
with a 2 GiB memory limit (raised from one CPU after measured report
processing fell behind); the database retains its separate two-CPU/two-GiB
ceiling.

Backup/export jobs refuse new copies below their configured disk reserve;
the preparation health guard also stops collectors below 25 GiB. This is an
unfunded preparation guard, not a substitute for a funded-pool shutdown policy.
Before lean storage, a full-network deployment measured roughly 26 GiB for the
primary database, 21 GiB for its block spool and 4 GiB for other spools, with
about 39 GiB of retained bases and 50 GiB of WAL on the recovery host; adding
a full block/report copy of that size would consume space needed to stage the
next base. Small membership and paused work do not reduce network-wide
observation. The database/block-spool retention is described in
[evidence retention](EVIDENCE_RETENTION.md); the [storage map](STORAGE_MAP.md)
explains the lean path.

## Inspect without moving funds

**Role: pool operator. Computer: remote VM via SSH.**

On the recovery server:

```sh
systemctl list-timers 'innopoolv2mainnet-*'
systemctl status innopoolv2mainnet-wal.service
cat /var/lib/innopool-v2-mainnet/primary-continuous/latest.json
cat /var/lib/innopool-v2-mainnet/primary-continuous/restore-rehearsal.json
cat /var/lib/innopool-v2-mainnet/health/status.json
```

Check each job's `Result`, receiver status and the manifest's verified timestamp.
A recovery overall health record is not proof that full raw-file coverage
exists. Configuration copy timestamps do not advance the database recovery point.

On either VM, inspect the protected journal for a failed named service with
`journalctl -u SERVICE_NAME --since today`. Do not print credential, environment
or DSN files. The health JSON contains no credentials. External alerting is an
operator decision ([roadmap](ROADMAP.md)); until a destination is configured,
manual inspection is the process.

To request a new off-host base backup, run on the recovery server:

```sh
systemctl start --no-block innopoolv2mainnet-basebackup.service
```

Successful completion requires a new `latest.json` **and** a successful service
result. A directory under `.preparing-*` is not a completed backup. The exact
scripts are [database backup](../tools/mainnet_backup_v2.py),
[configuration export](../tools/mainnet_config_backup_v2.py),
[pending evidence snapshot](../tools/mainnet_pending_evidence_v2.py),
[evidence verification](../tools/mainnet_verify_evidence_v2.py) for the
optional raw copy, and [backup health](../tools/mainnet_backup_health_v2.py).

## Bounded reporting replay

If reports are safely archived but await database replay, the operator can run
[the bounded replay tool](../tools/replay_reports_v2.py) with the observer DSN
in `POOL_V2_DATABASE_DSN`, an explicit spool path and its default one-hour/
10,000-record bounds. It refuses to run unless work is paused. The regular
drainer can remain active: duplicate recording is idempotent. Missing chunks
remain pending and cause failure; they are not treated as successfully recorded.
A catch-up run uses a one-CPU/one-GiB transient service on the primary and
cannot submit work, settle rewards or send tokens. The importer uses validated
immutable block headers for report height lookup, avoiding a full
network-block replay per capture. Incomplete upstream captures are recorded as
incomplete; missing/conflicting anchors remain failures.

## Restore rehearsal

**Role: pool operator. Computer: the recovery server.**

1. Select a complete verified base backup and preserve its immutable manifest.
   Record a harmless probe and named recovery point on the primary **after**
   that backup finishes, alongside a read-only financial-state snapshot.
   Force a WAL segment switch and confirm the segment arrived on the recovery
   server.
2. Save the reviewed seal as `primary-continuous/rehearsal-seal.json`. It must
   contain the selected snapshot name, restore point, probe payload, financial
   comparison query/result, database size and final WAL segment. This is a
   protected operator-generated file, not an API input.
3. Run [the rehearsal helper](../tools/mainnet_restore_backup_v2.py) on the
   recovery server. It verifies archive checksums, extracts to a new disposable
   directory, uses PostgreSQL 18 to validate data/WAL and starts a container
   with **no network and no published port**. It replays to the named point,
   pauses, checks the post-backup probe, financial snapshot and ledger, then
   removes its own disposable database. It does not replace either running
   database.
4. Retain the result and verify the service completed successfully. A failed
   rehearsal retains its evidence for inspection; do not overwrite a live
   database or attach live credentials to the test copy.

This rehearsal has been performed against a real primary backup, replaying its
post-backup transaction successfully. The full evidence audit has also verified
every chunk digest and manifest reference of a raw evidence copy on the
recovery server. Separately, a migration has been applied and replayed against
a restored **funded testnet pilot** with member balances, both collateral
holds, pending withdrawals and journal history unchanged. These are distinct
tests; none of them is a funded mainnet takeover or a token transfer.

## Launch checks

Before enabling live work, the operator performs these checks:

- Decide on external alerting and, if chosen, configure its destination;
  otherwise commit to manual inspection.
- Confirm pending-file protection and the recovery health profile match the
  agreed backup/collector profile; rehearse the database-plus-file restore.
- Check recovery capacity, verified base relocation, the three-base/WAL
  retention plan and both filesystem reserves.
- Test funded takeover, uncertain-send recovery and concurrent/retried
  requests.
- Sign in a real member through the proxy and complete a mainnet CPU
  assignment/result upload. This does not prove timely TIG completion.
- Confirm the master/helper benchmark path activates on TIG.
- Validate GPU execution, job-scheduling timing and live
  interruption/restart.
- Integrate reward claim/unlock/withdraw, final round attribution and
  expense/correction operations; backup completion does not complete these.
- Record the operator-approved mainnet budgets for protocol-fee top-ups and
  pool-wallet transaction fees, and monitor them.
- Confirm custody catch-up and RPC freshness; ordinary availability/freshness
  checks remain. Consume or disable any scoped paid trial approvals.

The backup mechanisms follow the PostgreSQL 18 documentation for
[base backups](https://www.postgresql.org/docs/18/app-pgbasebackup.html),
[WAL reception](https://www.postgresql.org/docs/18/app-pgreceivewal.html), and
[backup verification](https://www.postgresql.org/docs/18/app-pgverifybackup.html).
Checksum verification is supplemented by the actual isolated restore described
above; neither alone demonstrates a funded production takeover.
