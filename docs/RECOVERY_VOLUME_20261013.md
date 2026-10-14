# Recovery volume and combined restore — 13 October 2026

**14 October update:** the [storage ordering fix](STORAGE_ORDERING_FIX_20261014.md)
uses this recovery path for schema 022, final compact conversion and guarded
archive cleanup. The [14 October base placement](BACKUP_BASE_ARCHIVES_20261014.md) keeps the newest base on this volume and two older bases on spare root disk. The measurements below describe the 13 October migration.

The owner-attached 200 GiB EXT4 volume **107125099** is mounted at
`/mnt/HC_Volume_107125099` on Germany `2.28.230.81`. Database bases/WAL and
pending-file snapshots use directories there, bound to their original
`/var/lib/innopool-v2-mainnet/primary-continuous` and `primary-pending-evidence`
paths. Persistent mounts and backup service dependencies prevent fallback writes
to an unmounted directory. No device was reformatted by the agent.

## Verified operations

- All three copied base archives and **5,366 completed WAL segments** passed
  checksum comparison. Cutover at 10:42 UTC took about 1.5 seconds. The same
  replication slot resumed, source identity matched, lag reached zero and
  balances/journals were unchanged.
- Replacement base `2026-10-13T104305Z` completed at 11:26 UTC, followed by
  retained-archive checks and normal retention. Three verified bases remain
  from **9, 10 and 13 October**, with WAL since the oldest. Daily backups are
  scheduled again. Compressed replacement archives total 23,000,053,734 bytes.
- A dedicated restricted SSH key on recovery copies only pending files from
  blocks, reports, custody and funding. A shell command with that key was
  refused. The one-minute timer and the matching recovery health profile are
  active; full raw-file copying and independent collectors remain inactive.
- The production-copy restore passed at **12:09 UTC** with no network or
  published ports. Native checksum/WAL checks passed; a post-backup committed
  probe and exact fingerprints of **44 tables** were recovered. Migrations
  020/021 applied twice preserved all existing protected state.
- Three actual current report-index captures and one actual 12 October block
  capture were replayed twice. Financial state and unresolved gaps stayed
  unchanged. The disposable database and container were removed.
- Only after that proof, the obsolete original filesystem copy was removed.
  At 12:13 UTC root had about **134.6 GiB free** and the volume **82.3 GiB free**.
  No primary protocol archive was deleted.
- Primary encrypted export `configuration-daily-2026-10-13T121202Z` was checked
  and privately decrypted on recovery. Recovery settings, units, mount setup,
  pending key and host keys were encrypted and checked, then copied to primary
  as `recovery-operations-2026-10-13T121448Z`. Temporary plaintext was removed.
- Recovery health at 12:18 UTC was healthy, with no issues. Both actual archive
  destinations exceeded the 35 GiB reserve. Its pending snapshot was eleven
  seconds old and had a verified durable WAL boundary.

Backup tooling is pinned to candidate `dfcfbf6`; seven targeted health tests and
hosted CI **38381742884** passed all **371 pool tests** without skips. Full local
evidence is in `/root/mine-rootz/validation/recovery-volume-20261013/`.

## Scope and next work

After this storage verification, the production pair was activated at 13:28 UTC:
pool `f5f1028`, worker `a715041`, migrations 001–021. See
[the subsequent rollout](LEAN_ROLLOUT_20261013.md). New work remains
paused, settlement disabled, and no paid TIG trial was submitted. This validates
database and file recovery; a funded API takeover remains separate work.

The running pool now writes compact captures. Future capacity must be
measured after guarded legacy conversion/pruning, which remains in progress. Expiry
makes PostgreSQL pages reusable; it does not automatically shrink database files
or retained physical backups. The added capacity provided migration and restore
room, rather than establishing the steady footprint of the lean design.

The rehearsal's first WAL extraction failed with direct gzip/tar streaming;
automatic archive detection used by the installed backup verifier succeeded.
Native verification then passed on the reused isolated base. File import used
the project virtual environment, since system Python lacks psycopg2. Originals
were kept until all final checks succeeded.
