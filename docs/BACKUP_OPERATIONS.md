# Mainnet backup and recovery operations

The primary is **46.62.249.188** in Helsinki; the independent recovery host is
**2.28.230.81** in Nuremberg. Checked **9 October 2026**: recovery is currently
a backup host, with its API and independent block/report collectors inactive.
The primary has member funds enabled, new work paused and settlement disabled.
See [operations status](OPERATIONS_STATUS.md) for the exact release and controls.

## Installed protection

| Protection | Where / cadence | What it preserves |
|---|---|---|
| PostgreSQL WAL receiver | Germany, continuously | Changes after a database backup, including ledger records and migrations. |
| Compressed database backup | Germany, daily around 03:10 UTC | Entire primary PostgreSQL cluster; data checksums and required WAL are checked before publication. |
| Encrypted configuration export | Primary, daily around 02:30 UTC | Credentials, TLS key/certificate, service configuration, operational helpers, exact application and retained website assets. |
| Configuration/archive pull | Germany, every five minutes | Published primary exports over restricted read-only SSH. |
| Raw evidence copy | **Disabled** following the 8 October disk-reserve failure | The installed job would copy the full primary block/report spool; current pending files have no separate file-copy protection. |
| Health checks | Installed helpers and timers | Primary checks and individual backup-job records remain useful; the old recovery overall check expects inactive collectors/full evidence copy and needs a corrected profile. |

The full evidence copy is not running. Its former interval was measured after
completion, with no guarantee of five-minute freshness. Germany's collectors
have been inactive since 7 October. Database WAL is a separate stream and
cannot protect protocol files that have not entered the database, or supply a
TIG block never observed. Pending-file protection and whether to run an
independent collector are storage decisions still under discussion.

The backup connection uses a dedicated replication-only PostgreSQL role and an
SSH account restricted to forwarding the primary's loopback database port.
The evidence key can read only the spool directory. Neither connection can
activate the recovery pool or send tokens. Secrets remain outside Git.

Normal runtime and migration-owner database commits require acknowledgment from
the WAL receiver in Germany. The receiver uses `--synchronous`, flushing WAL to
disk before acknowledging it. A 9 October read of the actual service DSNs and
connection settings confirmed that API, coordinator, blocks, reports, custody
and funding all use `innopool_runtime` with `synchronous_commit=on`.
The separate observer role is configured for local commits but is not used by
those live services. Database imports therefore also wait for the receiver;
the collectors' separate capture threads can continue saving local spool files.
This protects commit acknowledgment; it is **not automatic
failover** or proof that an in-flight request cannot be visible before its
original caller receives acknowledgment. A funded takeover must still test
concurrent/retried requests and reconcile potentially sent payments before work
resumes. New work remains paused; the enabled member-funds capability does not
complete a funded takeover rehearsal.

The replication slot may retain at most 4 GiB on the primary. A sufficiently
long interruption can invalidate it: investigate and establish a new verified
base/stream if necessary. Do not silently recreate the slot and claim the
missing WAL was recovered. Do not disable synchronous protection simply to
clear a waiting transaction.

## Retention and resource limits

The backup tool retains at least **three complete verified daily base backups**.
Only after checking the retained archives may it remove older redundant bases
and WAL preceding the oldest retained base on the same timeline. Different
cluster identities or timelines stop automatic pruning. Partial WAL, timeline
history, original manual snapshots, raw protocol evidence and unresolved
financial records are not deleted by this policy.

A base backup or checksum verifier is limited to two CPUs and 768 MiB RAM.
The WAL receiver uses at most a quarter CPU and 128 MiB. Evidence copying and
configuration exports have separate systemd limits. Full evidence verification
uses two CPUs and 1.5 GiB. These are on the dedicated recovery VM; no local
benchmarking worker was started. The primary application slice was increased
from one CPU to 1.5 CPUs, retaining its 2 GiB memory limit, after measured report
processing fell behind. The dedicated primary has eight CPUs; its database
retains its separate two-CPU/two-GiB ceiling.

Backup/export jobs refuse new copies below their configured disk reserve;
the preparation health guard also stops collectors below 25 GiB. This is an
unfunded preparation guard, not a substitute for a funded-pool shutdown policy.
The measured 9 October footprint is approximately 26.4 GiB for the primary
database, 21.2 GiB for its block spool and 4.2 GiB for other spools. Recovery has
38.6 GiB of retained bases, 49.6 GiB of WAL and 46.6 GiB free. Adding the full
24 GiB block/report copy would consume space needed to stage the next base.
Small membership and paused work do not reduce network-wide observation.
The approved database/block-spool retention is described in
[evidence retention](EVIDENCE_RETENTION.md). No new deletion or storage-policy
change was made in the 9 October cleanup.
The later 14:55 read-only [storage map](STORAGE_MAP.md) records the current sizes
and explains the complete data path. All four pending import queues were empty
in that sample; the configured recovery raw-copy destination was absent.

## Inspect without moving funds

**Role: pool operator. Computer: remote VM via SSH.**

On Germany:

```sh
systemctl list-timers 'innopoolv2mainnet-*'
systemctl status innopoolv2mainnet-wal.service
cat /var/lib/innopool-v2-mainnet/primary-continuous/latest.json
cat /var/lib/innopool-v2-mainnet/primary-continuous/restore-rehearsal.json
cat /var/lib/innopool-v2-mainnet/health/status.json
```

Check each job's `Result`, receiver status and the manifest's verified timestamp.
The old recovery overall health record is not proof that full raw-file coverage
exists. Configuration copy timestamps do not advance the database recovery point.

On either VM, inspect the protected journal for a failed named service with
`journalctl -u SERVICE_NAME --since today`. Do not print credential, environment
or DSN files. The health JSON contains no credentials. External alerts were intentionally
deferred by the operator on 6 October; manual inspection is the chosen process.
No external destination is configured.

To request a new off-host base backup, run on Germany:

```sh
systemctl start --no-block innopoolv2mainnet-basebackup.service
```

Successful completion requires a new `latest.json` **and** a successful service
result. A directory under `.preparing-*` is not a completed backup. The exact
scripts are [database backup](../tools/mainnet_backup_v2.py),
[configuration export](../tools/mainnet_config_backup_v2.py),
[evidence copy](../tools/mainnet_evidence_backup_v2.py),
[evidence verification](../tools/mainnet_verify_evidence_v2.py), and
[backup health](../tools/mainnet_backup_health_v2.py).

## Bounded reporting replay

If reports are safely archived but await database replay, the operator can run
[the bounded replay tool](../tools/replay_reports_v2.py) with the observer DSN
in `POOL_V2_DATABASE_DSN`, an explicit spool path and its default one-hour/
10,000-record bounds. It refuses to run unless work is paused. The regular
drainer can remain active: duplicate recording is idempotent. Missing chunks
remain pending and cause failure; they are not treated as successfully recorded.
The 9 October catch-up uses a one-CPU/one-GiB transient service on the primary
and cannot submit work, settle rewards or send tokens. The current importer
uses validated immutable block headers for report height lookup, avoiding a
full network-block replay per capture. Incomplete upstream captures are
recorded as incomplete; missing/conflicting anchors remain failures.

## Restore rehearsal

**Role: Codex assisting the pool operator. Computer: Germany.**

1. Select a complete verified base backup and preserve its immutable manifest.
   Record a harmless probe and named recovery point on the primary **after**
   that backup finishes, alongside a read-only financial-state snapshot.
   Force a WAL segment switch and confirm the segment arrived in Germany.
2. Save the reviewed seal as `primary-continuous/rehearsal-seal.json`. It must
   contain the selected snapshot name, restore point, probe payload, financial
   comparison query/result, database size and final WAL segment. This is a
   protected operator-generated file, not an API input.
3. Run [the rehearsal helper](../tools/mainnet_restore_backup_v2.py) on Germany.
   It verifies archive checksums, extracts to a new disposable directory, uses
   PostgreSQL 18 to validate data/WAL and starts a container with **no network
   and no published port**. It replays to the named point, pauses, checks the
   post-backup probe, financial snapshot and ledger, then removes its own
   disposable database. It does not replace either running database.
4. Retain the result and verify the service completed successfully. A failed
   rehearsal retains its evidence for inspection; do not overwrite a live
   database or attach live credentials to the test copy.

The 4 October rehearsal restored the actual primary backup and replayed its
post-backup transaction successfully. A full evidence audit also verified
1,649,396 chunk digests and 376,604 manifest references on Germany. Separately, migration 015 was applied
and replayed against a restored **funded testnet pilot**: member balances,
both collateral holds, pending withdrawals and journal history were unchanged.
These are distinct tests. No funded mainnet takeover or token transfer occurred.

## Remaining launch checks

- [x] Operator chose manual inspection on 6 October; external alerts remain
  disabled. No external destination has been configured.
- [ ] Restore pending-file protection and correct recovery health to the
  agreed backup/collector profile; rehearse the database-plus-file restore.
- [ ] Sufficient archive storage and a measured, obligation-aware retention plan.
- [ ] Funded takeover, uncertain-send recovery and concurrent/retried-request tests.
- [x] Real member sign-in through Cloudflare on 6 October and the later mainnet
  CPU assignment/result upload. This does not prove timely TIG completion.
- [ ] Timely mainnet CPU/GPU execution and live interruption/restart validation.
- [ ] Reward claim/unlock/withdraw integration, final round attribution and
  expense/correction operations; backup completion does not complete these.
- [x] Operator-approved mainnet budgets: 10 TIG for protocol-fee top-ups and
  0.0005 Base ETH for pool-wallet transaction fees, monitored manually.
- [ ] Restore current mainnet custody reconciliation with a usable historical
  Base RPC. Custody baseline and fee funding were initialized before the pilot.

The backup mechanisms follow the PostgreSQL 18 documentation for
[base backups](https://www.postgresql.org/docs/18/app-pgbasebackup.html),
[WAL reception](https://www.postgresql.org/docs/18/app-pgreceivewal.html), and
[backup verification](https://www.postgresql.org/docs/18/app-pgverifybackup.html).
Checksum verification is supplemented by the actual isolated restore described
above; neither alone demonstrates a funded production takeover.
