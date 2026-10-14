# Evidence retention

The storage cleanup activated on 13 October adds compact pool captures, direct
binary downloads, final report fetching and guarded legacy conversion.
[Lean storage](LEAN_STORAGE.md) specifies migrations 020–022 and the cleanup
sequence. The policy below applies to retained legacy and new compact captures.
Historical conversion is complete; see [the ordering fix and cleanup](STORAGE_ORDERING_FIX_20261014.md).

Decided by the operator on 8 October 2026. The pool reads the whole TIG network
every block, but it only has to **keep** what an open obligation can still need.
Ledger, credit, benchmark, collateral and settlement records are permanent.
Raw captures are bounded-life evidence and expire under this policy.
The lean format retains compact pool evidence under these floors. A redundant
full-network copy can expire sooner when its validated compact replacement,
credit, reconciliation and any conflict resolution are verified off-host;
the original identity and accounting records remain.

| Evidence | Expires when | Cap |
|---|---|---|
| Raw block captures (database manifests and chunks, spool archives) | Round X is settled, and never before round X+2 has ended | 4 rounds, even while settlement is disabled |
| Fraud-report captures and report indices | Every pool benchmark created up to that round has its collateral outcome, and the round is at least three rounds old | 4 rounds |
| Wallet (custody) and fee-balance captures | Older than 30 days | 30 days |
| Database backups | Unchanged: three verified bases plus the WAL since the oldest | about 3 days |

What never expires, whatever the cap:

- A round whose blocks are not all captured and credited. The run stops at
  that round and says so. Recover and validate the actual missing capture
  before crediting it. The one-time prelaunch waiver requires a recorded pause
  and no existing reservations; it is not a general way to fill later gaps.
- Anything captured after the most recent verified base backup started.
- A capture cited by a confirmed report, arbitration, round seal, benchmark
  reporting round, fee top-up, funding alert, opening credit or custody
  baseline. The most recent capture per key is also kept so sealing and
  status checks keep working.
- The capture rows themselves. Expiry blanks a payload and stamps
  `expired_at` (or replaces a block manifest with an `expired` tombstone
  carrying the manifest's checksum); an expired report capture also replaces
  its provenance metadata with the tombstone, keeping only identifiers and
  digests. Foreign keys stay valid and readers fail with "expired under the
  retention policy" instead of returning data.

## How a run works

`tools/retain_evidence_v2.py` runs daily after the verified backup, with the
admin DSN. It:

1. Reads the recovery host's `latest.json` as the backup proof; the backup
   must be checksum and WAL verified and recent.
2. Computes target floors: the last block height, the last reporting round and
   the capture cutoff that the policy, settlement, credit coverage and backup
   coverage all allow. Floors are stored in `retention_floors` and can only
   advance.
3. Inside a session flagged `pool_v2.retention`, tombstones block manifests
   under the floor, removes chunks that only expired captures referenced,
   blanks report, funding and custody payloads under their floors, and prunes
   the local block spool (archives under the floor, chunk files the database
   removed, never a chunk a pending archive still needs).
4. Appends a `retention_runs` row with the policy, proof, floors, counts and
   notes.

Outside such a session every update or delete on these tables is still
refused, and inside one only the policy-shaped change under the recorded floor
is accepted; see migration `018_evidence_retention.sql`.

Chunk removal uses `chunk_last_ref`, maintained by the block observer: a row
holds the last block height that referenced a chunk the newest block no longer
references. A chunk with a row at or below the block floor is referenced only
by expired captures. On a database recorded before migration 018, run
`tools/retain_evidence_v2.py --initialize-chunk-index` once; it bounds every
chunk the newest block does not reference by that block's height, so such
chunks expire only once every block up to it has.

## Deployment

1. Apply migrations 018 and 019 with the admin role, then apply
   `deploy/v2-mainnet/grant-runtime-observer-privileges.sql`: the observer and
   runtime roles need the chunk index and may read the floors and runs. On
   8 October the grants were applied a minute after the migration; in that
   minute the recorder refused every block and the spool replayed them.
2. Run `tools/retain_evidence_v2.py --initialize-chunk-index` once, before any
   retention run, while the observer is the only writer.
3. Install `innopoolv2mainnet-retention.service` and `.timer` with
   `retention.env` (admin DSN) and a backup proof. The primary fetches the
   recovery host's `latest.json` through a dedicated key whose only permitted
   command on that host is printing the file.

## Operating notes

- `--dry-run` prints the floors a run would set without changing anything.
- Expired block manifests free their space for reuse after PostgreSQL's
  autovacuum; returning it to the operating system needs a `VACUUM FULL` or
  `pg_repack` of `capture_attempts` during a maintenance window.
- New report index collection retains positive membership and deduplicates
  unchanged responses; empty indices are not archived. Final reports are
  fetched for open obligations after the X+2 reporting window closes.
- Both block and report spools have guarded file pruning. Recorded report
  files require a matching backed database payload and scope; pending files
  remain. Custody/funding spool cleanup retains its existing controls.
- Recovery collectors are inactive and the full primary-evidence copy is
  disabled after a disk-reserve failure. At 14:55 UTC on 9 October, the configured
  recovery raw-copy directory was absent and its local spool measured only
  36 KiB; do not assume that a separate raw archive exists there.
  The replacement bounded pending-file snapshots were activated on 13 October
  and include all four spools. See [operations status](OPERATIONS_STATUS.md)
  for the current recovery path and measured footprint.
