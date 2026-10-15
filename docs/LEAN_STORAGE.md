# Lean pool storage

Implemented on the 9 October 2026 storage cleanup branches and activated on
13 October as pool `f5f1028`, worker `a715041`, with migrations through 021.
See [rollout and remaining maintenance](LEAN_ROLLOUT_20261013.md) and
[verification details](LEAN_STORAGE_VERIFICATION.md). The [14 October ordering fix](STORAGE_ORDERING_FIX_20261014.md) adds migration 022 and a verified resolution
for the single held rollout-boundary capture. Historical conversion, backup/restore
verification and guarded redundant raw/file/chunk cleanup are complete. See
[operations status](OPERATIONS_STATUS.md) for live measurements and controls.

## Binary downloads

Before a paid precommit, the coordinator downloads and checks the selected
public archive. It retains only the URL, SHA-256 and byte count in
`reservation_binaries`; it does not save those archive bytes. Each actual
check is also appended to `verified_binaries` by URL, library name and compute
type. A later reservation of the same algorithm and compute type reuses that
hash and size without downloading again while the newest check is younger
than `binary_reuse_seconds` in the coordinator configuration (default 86400;
0 checks every reservation). A check for another compute type is never
reused, because that architecture's library or PTX was not inspected. The
worker still verifies the assigned hash against the bytes it downloads, so a
replaced public archive fails before any computation. New assignments
give the worker the public URL. Workers download from TIG or TIG's GitHub media
host, verify the hash and safely extract the required library/PTX. Public GET
redirects are restricted to allowed HTTPS sources and do not forward credentials.
Pool API requests still refuse redirects.

Legacy archive rows and the old artifact endpoint remain for previously prepared
reservations and issued assignments. Update the paired worker before activating
direct assignments.
An existing explicit `binary_hosts` override must include
`media.githubusercontent.com` as well as the configured TIG API host.

## Block capture and credit

With `--pool-player-id`, the collector reads six public responses per block
and saves `pool-scoped-v2`: the block header with its active algorithm,
challenge and OPoW ID lists, the full algorithm, challenge and OPoW tables,
and only the pool's own benchmark feed. Other players' feeds are never
requested. The header's active benchmark list is reduced to the pool's own
IDs, keeping the original count and a digest of the full list so independent
collectors can be compared. The capture is validated before it is saved: the
two block reads must match, every pool benchmark listed active must have its
precommit, benchmark and proof records, the pool's qualifier counts may not
exceed its own bundles, and every player's counts inside the algorithm
records must reconcile with the OPoW and challenge totals. Record lists are
stored in digest order, so collectors that received the same data in a
different order record the same block identity. Failed collection saves a
header and bounded diagnostic.

The approved credit rule groups bundles by **player**, challenge, algorithm and
track. The pool's own eligible bundles plus its own qualifier counts therefore
reproduce the exact fractional credit; member credit needs nothing else.
Ownership, per-block credit, coverage and financial records remain permanent.
Pool-scoped and compact captures are one compressed, checksummed blob and use
the existing bounded retention floors. Open selection anchors remain until
collateral is finalized. The separate capture/DB-recorder threads and legacy
readers remain.

`--full-network` restores the previous behaviour: every active player's feed
is read, the whole network is validated, and the compact `pool-network-v1`
projection is saved with the original header, the pool's feed, its qualifier
totals, algorithm and challenge config, one best network reference per
algorithm/track with its parameters, and source identity digests. The guarded
conversion of historical full captures to that format remains.

Deploy every collector with this change at once: a legacy full capture and a
pool-scoped capture of the same block carry different block identities and
would be reported as a conflicting observation at that height.

## Selection references

Choosing hyperparameters copies the best bundle per algorithm and track from
the whole network. The coordinator reads every active player's feed for one
current block only when a member is waiting for work and the newest stored
reference is older than `reference_max_age_seconds` (default 1800). It
validates the whole network and keeps only the best bundle per algorithm and
track, with its parameters, in `reference_captures`. Selection then uses that
index: a reference from after the credited block, or older than the configured
age, is refused, and the reservation records the reference capture's identity,
block and digest beside the credited block. Nothing is read while new work is
paused or no offer is queued. Reference payloads expire under the raw block
floor; a reference a reservation cites, and the newest one, stay readable.
Compact `pool-network-v1` captures keep their embedded references and need no
separate read.

## Reports

Collection follows open collateral obligations, declared scopes and pending
reward rounds. Final reports are fetched after creation round X+2 and the
actual protocol reporting window close. Confirmation across a round boundary
can require a later fetch.

The pinned TIG adapter records reporting membership from an actual archived
benchmark-confirmation header. Selective public index queries supply missing
mappings while available. Empty indices establish nothing and are not archived;
positive membership is saved once, and unchanged responses are deduplicated.

Final captures keep reports/arbitrations for pool-owned benchmarks, together
with previously confirmed facts needed by existing seals. They retain the full
source response hash and the filtered benchmark scope. Successful final
evidence is reused. API failures, open reporting windows and pending arbitration
cause retries, never an empty final decision. Scope/seal and settlement gates
remain; collection does not move money.
The scheduled final fetch waits 65 seconds after an archived final-window
boundary, covering the pinned backend's 60-second report cache. HTTP cache age
must also leave the response after that boundary/grace; an older cached result
is retried. This prevents a last-block report being missed by an immediate
cached fetch at the round transition.

## Existing archive cleanup

Apply the additive migrations through 025 and the updated runtime/observer
grants; 020 to 022 are already deployed.
Install all collector changes before pruning so writing-chunk pins protect
captures during maintenance. The old deployed reader cannot read the new
compact format; rollback must retain a reader that understands it.

`tools/compact_evidence_v2.py` uses the admin DSN for bounded maintenance:

1. Run with `--player-id` and `--limit` to add compact replacements for legacy
   snapshots. Original immutable block identities stay unchanged. Held blocks
   retain raw evidence. Review held reasons; `--after-height` continues past a
   reviewed batch without declaring held blocks converted.
2. Wait for a verified base backup containing those replacements.
3. Run with that `--backup-proof` and `--prune`. A raw copy expires only after
   the compact replacement is backed, the block is credited/reconciled, and no
   unresolved conflict exists. Any ordering resolution must also be backed.
   Identity rows stay; payload/provenance become tombstones.
4. Add `--block-spool` or `--report-spool` to remove redundant recorded files
   in bounded batches. Report files require matching backed DB payload hashes.
   Pending files remain. After backfill/pruning, `--collect-unused-chunks`
   removes chunks that surviving captures no longer reference. Database deletion
   is limited by `--chunk-limit` (default 5,000, maximum 50,000) and recorded in
   `retention_runs`. Repeat while the result reports `more_chunks: true` or
   `more_reference_rows: true`.

File maintenance pins in-progress writes and briefly locks deletion batches,
without holding capture behind the full directory scan. DB chunk cleanup uses
the observation writer lock and defers with more than 100 legacy captures.
Corrupt writing pins or digest pages stop cleanup; the temporary cleanup marker
is cleared even on failure.
Missing heights remain visible and continue to hold exact settlement.

WAL, bases and configuration transfers keep their current transport and
retention. Smaller writes reduce future backup volume. Expiry makes PostgreSQL
space reusable; it does not shrink database files or purge retained backups.
Local API, worker, browser and database checks and fixture-only pruning have
been performed. Production migrations and paired activation are complete.
Historical conversion and guarded redundant archive/chunk cleanup completed on
14 October. Allocated-table rewrites were not performed. Normal backup
retirement still retains three verified bases and their required WAL; the
[archive placement](BACKUP_BASE_ARCHIVES_20261014.md) uses existing server disks.

## Legacy ordering conflicts

Source identity version 2 compares record lists in the header using the legacy
canonical encoding while retaining the original header in the compact payload.
Existing full and v1 compact source hashes remain readable; algorithm binary
metadata ordering does not change bundle credit or selection references.

Migration 022 adds immutable `observation_conflict_resolutions` and the
`unresolved_observation_conflicts` view. Runtime and observer roles have read
access only. The admin tool `tools/resolve_ordering_conflict_v2.py` requires
an explicit alert ID, conflicting capture ID, actor and reason. It validates
both retained captures, checks record values and multiplicities, and rebuilds
the conflicting v1 projection from the entire original network after permuting
only reward schedules and binary metadata lists. The canonical source hash and
complete projection must match. The resolution retains payload hashes and the
full replay hash; original block identities, captures and alerts stay unchanged.

Resolution applies to one alert. Different block IDs, broken neighbor links,
changed records, missing network evidence and later conflicts continue to
block reads, coverage and pruning. Missing block heights remain unresolved.
Pruning checks the backup cutoff against both the compact replacement and
its resolution. Rollback must use a release with migration 022 and this reader;
pre-resolution releases are insufficient after raw copies expire.
