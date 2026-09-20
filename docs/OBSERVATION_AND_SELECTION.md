# Block history and work selection

The Stage 2 increment adds independent collection, durable replay, exact member
credit attribution and a pure selector. It does not enable benchmark submission
or settle rewards. The coordinator and worker integration are still required.

## Collection and recovery

`tools/observe_tig_v2.py` runs separately from the API and scheduler. Its capture
thread saves raw evidence to a local spool before a separate recorder thread
touches PostgreSQL. A database outage, lock or recovery backlog cannot stop the
capture thread. Immutable response records are content-addressed and compressed
once across repeated blocks, both in the spool and in PostgreSQL. Manifests retain
array order and all original fields; checksums are verified when replaying.

Use a separate spool per collector and run the redundant collector in a different
failure domain. Both can record the same block into the database: block identity
and credit keys deduplicate it. Neither has a TIG API key or token signing key.
Back up the database **and** spool chunks/manifests. A backup cannot recover an
unobserved block. Do not delete these records using legacy retention scripts.

After separately applying the v2 migrations, set `POOL_V2_DATABASE_DSN` in the
service environment and run, for example:

```sh
python3 tools/observe_tig_v2.py --collector primary \
  --spool /var/lib/innopool-v2/observations --launch-height YOUR_START_HEIGHT
```

`--captures 2` performs a finite validation run. Exit 0 means all pending evidence
was recorded and no known coverage gap/conflict remains; exit 1 means local
evidence awaits recording, and exit 2 means a gap or conflict remains. Continuous
mode logs lag, missing heights, failed capture and conflicting observations.
It retries incomplete captures while the block remains observable. Startup
requires an explicit, permanent collection start; it cannot silently move that
start past a missing block.

The database tracks latest observed height separately from the highest contiguous
complete height. Mixed, truncated and failed responses retain their partial raw
evidence without becoming complete blocks. Another collector can fill a gap and
advance the contiguous cursor. A conflicting block identity or accounting input
stops use of that height pending investigation; it cannot overwrite an accepted
record or silently replace already recorded credit. Conflict resolution needs
explicit reviewed recovery, not automatic selection of whichever source arrived
last.

Stale timestamps generate alerts but do not prevent preserving otherwise complete
historical observations. The work selector separately refuses stale snapshots.
In the live validation here, normal and cache-busted reads returned the same head
even when its protocol timestamp lagged wall time; `Cache-Control: max-age=15`
alone did not explain that lag. Configure and monitor freshness based on the
chosen deployment, without treating an older timestamp as proof of a missing or
expired benchmark.

## Credit and coverage

`qualifiers.credit_block()` replays a validated block, reconstructs D4 exact
fractional bundle credit and joins every pool benchmark to its immutable member
owner and confirmed handover. Missing ownership is an accounting gap. Fractions
are stored per benchmark/block; later fraud does not rewrite them. Reprocessing
the same block checks the prior rule and ownership digest and cannot credit twice.

The block's reward round owns its credit. A benchmark's creation round stays on
its separate reservation. The adapter checks the deployed block-round mapping
against configured blocks per round; this does **not** establish the separate
reporting-round mapping still listed in the Stage 0 integration checks.

`round_credits()` requires every expected block and its credit marker in the
round. An entirely observed, attributed zero-credit round is distinct from a
missing round. A gap in one round does not prevent use of a different fully
covered round. This check will gate settlement; it does not delay independently
eligible collateral returns.

## Selector

`selection.choose()` uses exactly one validated snapshot and matching reference
index. The pinned compute-type adapter supports the nine CPU verification types
and `aws_g4dn` for GPU currently enumerated by TIG. Unknown or incompatible offers
fail before reservation.

It selects the compatible challenge with the fewest pool qualifying bundles,
breaks ties uniformly, then selects its usable algorithm with the highest exact
overall adoption. For each active track it copies the selected algorithm's
highest-scoring active bundle's hyperparameters. Equal reference scores use
benchmark ID and active-array index. A complete absence produces JSON `null`;
missing binary/reference data is never silently treated as an empty reference.

Bundle counts start at each applicable minimum plus one. Fuel defaults to the
protocol maximum, with a validated operator override. All tracks are included;
`settings.track_id` is an empty placeholder because TIG chooses the actual track.
The selector records candidates, counts, adoption units, draws, reference
identities, parameters and binary metadata. Collateral uses the largest proposed
track; the funds transaction applies the member's current multiplier afterwards.
Fee capacity likewise covers the largest proposed track. The deployed Rust
contract multiplies the field named `per_nonce_fee` by **bundle count**, which is
the calculation used here.

## Validation and remaining integration

Tests replay the existing live fixtures and exercise CPU/GPU filtering, exact
adoption comparisons, random-tie candidates, best references, `null` defaults,
stale/missing inputs, archive deduplication, replica capture, database-outage
spooling, unknown ownership, gap recovery, conflicting data and complete rounds
on either side of a gap. Run the full suite using the isolated test instructions
in [MEMBER_FUNDS.md](MEMBER_FUNDS.md).

A finite live run on 20 September 2026 recorded complete consecutive blocks
1,351,170 and 1,351,171 into the new PostgreSQL/spool implementation. The chosen
start height 1,351,169 had advanced before the first complete capture; the
collector correctly retained that missing height and did not advance its
contiguous cursor past it. This was a read-only test, with no member liabilities.

Production deployment still needs external alert delivery, redundant hosts,
backup/restore rehearsal, storage monitoring, and the coordinator's current-head
checks immediately before precommit submission. The collector is not deployed
as an ongoing service by this change.
