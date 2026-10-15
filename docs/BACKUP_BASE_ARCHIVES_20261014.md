# Recovery base placement — 14 October 2026

## Current layout

The recovery server retains **three verified database bases**:

| Snapshot | Location |
|---|---|
| `2026-10-14T134951Z` (newest) | Volume: `/var/lib/innopool-v2-mainnet/primary-continuous/base/` |
| `2026-10-14T031353Z` | Existing root disk: `/var/lib/innopool-v2-mainnet/primary-base-archives/` |
| `2026-10-13T104305Z` | Existing root disk: `/var/lib/innopool-v2-mainnet/primary-base-archives/` |

The attached **200 GiB EXT4 volume 107125099** continues to hold the newest
base, continuous WAL and pending-file snapshots. Existing mount guards remain
required. `continuous-backup.json` identifies the older-base directory with
`base_archive_directory`. Both locations are on the off-host recovery server;
its pool application remains inactive.

No additional disk was provisioned. Immediately after relocation the volume
had **64.59 GiB free** and root had **89.91 GiB free**. Both also met the next
backup's forecast reserve: 35 GiB plus 115% of the largest retained archive
(about **62 GiB** at this check). These are measurements, not fixed capacities.

## Why placement changed

The historical cleanup removed millions of database rows and made their pages
reusable. Those changes generated recovery WAL. The older bases still require
that WAL, so cleanup temporarily left about **97.6 GiB** of required WAL and
**67.95 GiB** of bases on the volume. Free space fell to **20.15 GiB**, below
the 35 GiB reserve; pending-file protection correctly refused a new copy.

The server's root disk had about 134 GiB free. The two older bases were copied
there, with every source/destination archive SHA checked against its immutable
verification record and flushed to disk. Only then were their redundant volume
copies removed. Snapshot identities, archive bytes and `latest.json` are
unchanged. **No required WAL was removed.** Pending copying and recovery health
passed again afterward.

The first copy stopped at its memory cap while originals remained intact.
The resumed operation used bounded writes, reused only reverified files and
completed both required copies. Normal archive copying now flushes in bounded
chunks as well.

## Backup and retention behavior

`mainnet_backup_v2.py` provides:

- `base_catalogue`: finds both locations, verifies archive hashes when pruning,
  and deduplicates only identical snapshot copies. Conflicting or corrupt
  copies stop maintenance.
- `maintain_bases`: keeps the newest verified base on the volume and the two
  older retained bases on root. Relocation verifies the destination before
  removing its redundant source.
- The existing three-base retention rule, with the WAL floor calculated from
  the oldest retained base **across both locations**. Required WAL cannot be
  pruned merely because an older base moved to root.
- Reserve checks for both filesystems before a new backup, including room for
  the largest expected archive. Low space preserves existing bases and WAL.

Recovery health checks the older archive, its reserve, cluster/timeline
consistency, the published newest volume copy and the existing WAL/pending
protection. A temporary fourth verified base during normal rotation is allowed;
completed retention returns to three.

The standard latest-base restore path stays on the volume. For an older
snapshot, `locate_base(volume_base_directory, snapshot, older_base_directory)`
returns its verified catalogue location. Verify its archive hashes and the
required WAL before restoring. The 14 October isolated restore used the
03:13 UTC base; its relocated archive has exactly the same verified bytes.

## Verification and configuration protection

Eight archive/retention tests and nine recovery-health tests passed locally.
They cover identical/conflicting copies, corruption, older-snapshot lookup,
three-base/WAL retention, insufficient root reserve, newest-base placement and
independent reserve checks. The frozen application release separately passed
390 hosted tests; its deployed pool/worker pair remains recorded in
[the storage fix](STORAGE_ORDERING_FIX_20261014.md).

The cutover record is
`/etc/innopool-v2-mainnet/base-storage-layout-20261014.json`, including snapshot
IDs, verified-copy proof hash, unchanged latest-record confirmation and exact
backup/health source hashes. Updated recovery settings and operation scripts
were encrypted, privately verified and mirrored to the primary. Primary
settings are independently exported and verified on recovery. Temporary
plaintext is removed after verification.
