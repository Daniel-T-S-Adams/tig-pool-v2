# Pending protocol file protection

Prepared on 12 October and **activated/verified on 13 October 2026**. The German
host now has the new volume, a fresh base and a successful combined production-copy
restore. See [the activation record](RECOVERY_VOLUME_20261013.md).
This supplements the existing three verified database bases, continuous WAL and
encrypted settings/release exports. It does not activate a second pool or
independent protocol collection.

## What is copied

The dedicated source command selects only manifests currently awaiting import
from all four spools: **blocks, reports, custody and funding**. It includes only
their referenced chunks, including digest pages and compact blobs. Recorded
archives, unreferenced chunks and algorithm binaries are excluded.

Lock files follow the spool directory's owner even when the exporter or
maintenance runs as root, so the collector can reopen them. Symlinked or
hardlinked lock files are refused. Publication locks make one bounded selection;
maintenance locks then protect
the selected chunks while copying. A manifest moved to `recorded` after selection
still has its complete saved bytes in the snapshot. The updated collector and
maintenance locking implementation must be installed before production pruning.

The export records a database WAL boundary after selecting the files and waits
up to 15 seconds for Germany's existing receiver to acknowledge it. This binds
imports that have left the pending queues to durable WAL. The receiver verifies
the source cluster identity against its base backup, every compressed file hash,
every content digest and the complete set of manifest references before
publishing an atomic snapshot pointer. Failed exports retain the previous copy
and fail the service/health check.

## Limits and retention

- At most 1 GiB of compressed content, 10,000 manifests and 100,000 files per
  snapshot; per-file expansion and manifest nesting are also bounded.
- Keep the latest **two verified pending snapshots**. An older tool-owned
  snapshot is removed only after its replacement and durable WAL barrier pass.
  Primary evidence, database bases and WAL are never removed by this tool.
- Retain at least 35 GiB free plus the maximum incoming content, index and
  filesystem allocation/metadata allowance before starting a copy. Small
  compressed chunks occupy whole disk blocks. A low reserve causes failure,
  not weaker protection.
- Run every minute after the previous attempt finishes, with a four-minute
  service timeout. Health requires both the selected source files and the
  completed copy to be no more than five minutes old. It checks the published
  index hash, its coverage facts and WAL acknowledgment.

This is periodic file protection: files created after a successful selection
wait for the next copy. It does not claim zero loss for files never imported or
copied. Database financial commits retain the existing synchronous WAL policy.

## Installation sequence

1. Verify disk capacity, the current WAL receiver and a fresh verified base.
   Keep new work paused. Do not delete older required recovery data for space.
2. Install `mainnet_pending_evidence_v2.py` and its `mainnet_backup_v2.py` dependency
   in the operations directory on both hosts. The primary exporter uses the
   existing virtual environment for its read-only PostgreSQL query.
3. Generate a dedicated key **on Germany**. Add its public key on the primary
   with `from="2.28.230.81",restrict` and a forced command:
   `/opt/innopool-v2-mainnet/venv/bin/python /opt/innopool-v2-mainnet/operations/mainnet_pending_evidence_v2.py export`.
   The exporter also requires `SSH_ORIGINAL_COMMAND=export-pending-v1`; the key
   permits neither arbitrary commands nor forwarding. Use existing verified
   known hosts, never automatic host-key acceptance. Keep private keys local.
4. Install root-owned 0600 copies of
   [`pending-evidence.json.example`](../deploy/v2-mainnet/pending-evidence.json.example)
   and [`recovery-protection.json.example`](../deploy/v2-mainnet/recovery-protection.json.example).
   Install the pending-evidence service/timer examples, run one copy manually,
   and verify the snapshot before enabling its timer.
5. Install the updated backup health module and recovery health tool. Point the
   existing recovery health service at `mainnet_recovery_health_v2.py` using the
   supplied recovery health service example. The recovery profile expects pool
   services and independent collectors to remain inactive. Database bases, WAL,
   settings and pending-file protection must all be healthy. The reserve check
   covers the root disk and the actual database/pending-file destinations,
   including separate mounted volumes. The primary health
   service remains responsible for its actual pool and collectors.
6. Export encrypted settings, operations files and unit files again and verify
   their off-host copy. Rehearse restoring the database, later WAL and pending
   files together into isolation, and verify balances and idempotent import.
   Never install rehearsal credentials into a live API or enable takeover.

## Restore files into isolation

Choose the snapshot named in `primary-pending-evidence/status.json`. Run:

```sh
python3 /opt/innopool-v2-mainnet/operations/mainnet_pending_evidence_v2.py restore \
  --snapshot /var/lib/innopool-v2-mainnet/primary-pending-evidence/snapshots/SNAPSHOT_ID \
  --target /a/new/isolated/spool
```

The target must not exist. The restored spool keeps all manifests pending until
the isolated database importer verifies them. Replay may encounter evidence
already present through WAL; imports must remain idempotent and leave financial
records unchanged. File round trips and actual PostgreSQL block/report imports
are covered by the release tests. These fixture checks do not replace the full
production database-plus-WAL restore rehearsal.
