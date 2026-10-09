# Lean pool storage

Implemented on the 9 October 2026 storage cleanup branches. **Not deployed.**
The primary remains pool `8477509` with worker `fd29279` and migrations through
019. No production archives were deleted by this change. See
[operations status](OPERATIONS_STATUS.md) for live measurements and controls.

## Binary downloads

Before a paid precommit, the coordinator downloads and checks the selected
public archive. It retains only the URL, SHA-256 and byte count in
`reservation_binaries`; it does not save those archive bytes. New assignments
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

The collector validates the complete current network, then saves
`pool-network-v1` with:

- The original block ID, height, predecessor, timestamp, round and config.
- Our pending/active feed, active benchmark IDs and authoritative qualifier
  totals per algorithm and track.
- Active algorithm adoption, state and binary metadata, and challenge config.
- One best network reference per algorithm/track, with benchmark ID, original
  active-array index, quality and hyperparameters.
- Source identity digests and a summary/hash of the request log.

Other miners' full feeds and global active benchmark/player ID lists are
discarded before spool/database writes. Failed collection saves a header and
bounded diagnostic. Scoped replay checks pool bundles, confirmations and totals;
it does not recreate the discarded network.

The approved credit rule groups bundles by **player**, challenge, algorithm and
track. Complete eligible pool bundles plus pool totals therefore reproduce the
exact fractional credit. Selection uses the separate best-reference summary.
Ownership, per-block credit, coverage and financial records remain permanent.
Compact captures are one compressed, checksummed blob and use the existing
bounded retention floors. Open selection anchors remain until collateral is
finalized. The separate capture/DB-recorder threads and legacy readers remain.

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

Apply additive migrations 020/021 and the updated runtime/observer grants.
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
   conflict exists. Identity rows stay; payload/provenance become tombstones.
4. Add `--block-spool` or `--report-spool` to remove redundant recorded files
   in bounded batches. Report files require matching backed DB payload hashes.
   Pending files remain. After backfill/pruning, `--collect-unused-chunks`
   removes chunks that surviving captures no longer reference.

File maintenance pins in-progress writes and briefly locks deletion batches,
without holding capture behind the full directory scan. DB chunk cleanup uses
the observation writer lock and defers with more than 100 legacy captures.
Missing heights remain visible and continue to hold exact settlement.

WAL, bases and configuration transfers keep their current transport and
retention. Smaller writes reduce future backup volume. Expiry makes PostgreSQL
space reusable; it does not shrink database files or purge retained backups.
No runtime tests, production migration, release activation, archive deletion,
table rewrite or backup purge was performed while implementing this change.
