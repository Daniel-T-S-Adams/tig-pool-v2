# Pool-scoped capture rollout — 15 October 2026

## Deployed state

Pool `4e64bf32cfb124bef8ea1c526eb6ed3f9ede45bc`, tag
`mainnet-pool-scoped-capture-20261015`, is the head of `main` after the seven
integration merges of 15 October (pool #43, #44, #45, #46; worker #10, #11,
#12). It is deployed on the primary and staged on recovery with the paired
worker `c6eccfb4f7bf4fc1ceb448f80983dd954bfc99ef`, tag
`mainnet-library-cache-20261015`. The installer bytes are unchanged from the
previous pair, so the served installer checksum is the same; the runtime map
remains the official 0.0.8 CPU images. Pair digest:
`432aaaa64d61225231fe20e1ed21e47accdd35688b8497128e232806c0ead336`. Hosted CI
passed on both commits (pool run 38721882771, worker run 38720603560).

The release adds the pool-scoped block capture with on-demand selection
references, verified-binary reuse, the bounded missing-block tolerance and the
Community Pool screens. Coordinator settings keep their defaults:
`binary_reuse_seconds` 86400 and `reference_max_age_seconds` 1800.

## Sequence

1. `stage`: archives of both commits were uploaded and extracted under
   `/opt/innopool-v2-mainnet/releases` and `worker-releases` on both hosts;
   `requirements-v2.txt` is unchanged.
2. `migrate` on the primary at **11:02:19 UTC**: restore point
   `innopool_pool_scoped_20261015` (LSN `3F/A32BD418`), then migrations
   `023_missing_block_tolerance.sql`, `024_verified_binaries.sql` and
   `025_reference_captures.sql` applied twice with the schema owner; the 22
   earlier checksums were unchanged. The runtime role gained SELECT and INSERT
   on `verified_binaries` and `reference_captures` and no UPDATE, DELETE or
   TRUNCATE; both service roles read `missing_block_policies` only. One seeded
   policy row exists. Synchronous WAL streaming to recovery was confirmed.
3. `activate` on the primary at **11:02:42 UTC**: the API stopped at an
   already-captured boundary (height 1385323, 23 s old), the six service units
   and the retention unit moved to the new release directory, `service.json`
   records the build, the release manifest and installer were replaced, and
   the services restarted. `/api/v2/capabilities` reported the new commit and
   pair digest with funds enabled, new work paused and settlement disabled.
   Recovery followed at 11:02:46 with its collector units updated and inactive.
4. The financial digest
   `783afcc922243a792ede48895e9b8c3577acdb63a4104e040538c3ba65b9c186` was equal
   before migration, after migration and after activation; the ledger audit is
   empty; no slots, intents or queued offers existed.

## Verification after the switch

Heights 1385324 and 1385325 were captured in the `pool-scoped-v2` format with
3,655 and 3,660 active benchmarks recorded in scope, validated complete and
credited. The public release endpoint serves the new pair. The coordinator
logged three `ProtocolDataError` interruptions in the first seconds while the
latest block carried the conflict below, then resumed normal cycles. No
reference capture or verified binary row exists yet, because no offer is
queued. Members' installed workers keep working: the worker guard compares
runtime images only, and those are unchanged; the installer serves the new
worker commit to new installations.

## Boundary conflict at height 1385323

The previous collector recorded 1385323 as a complete compact capture at
11:02:27. The restarted collector, which starts without memory of recorded
blocks, captured the same head again at 11:02:43 in the scoped format. The
two formats carry different identities by construction, so the store
recorded alert 5 (`conflicting-block`) and alert 6 (`incomplete-capture`,
attempt `4edb06ac-1efb-4d53-b794-7947542dda26`) and kept the original block
(attempt `904799b3-620c-423c-8ed1-31f8a04e7558`, digest
`563bd357…067420`, round 138, pool credit 0). Later heights were unaffected.
New work and reports consult only the block they use, so neither gate depends
on this height; round 138 would be held for settlement while the alert stays
unresolved, and the collector logs the conflict every cycle.

Two changes are prepared on `operations/format-boundary-resolution`:

- The collector resumes after the newest recorded block on start, so a
  restart never captures the current head twice.
- Migration 026 admits the policy `format-boundary-projection-v1` for
  `observation_conflict_resolutions`. Its proof is accepted only when the
  block identity, configuration and details, the pool's feed, precommits,
  bundles, qualifier counts and credit, the pool's own algorithm counts, the
  algorithm, binary and challenge records, the pool's OPoW row and the active
  benchmark list agree between the compact original and the scoped recapture,
  and the scope count matches the block's active benchmark count. Alerts,
  captures and the original identity stay immutable, and a later genuine
  conflict at the same height still blocks use.

The resolution of alert 5 is applied after that change is reviewed, merged,
and deployed as a schema-only release.

## Open items

- Recovery's daily base backup failed on 15 October inside the base-archive
  catalogue step of `operations/mainnet_backup_v2.py` (a `TarFile` passed as a
  path); the base `2026-10-15T031353Z` exists and WAL streaming is healthy,
  so verified backup age stays within its limit for now. Recovery's
  evidence-pull refuses to copy because its disk reserve is below the
  configured minimum. Both predate this rollout and need operator attention.
- The primary health check still flags the three historical missing heights;
  it should consult `held_rounds` now that the tolerance is deployed.
