# CPU pilot: observation and recovery follow-up

The two testnet CPU benchmarks completed and became active on 23 September
2026. This follow-up keeps new submissions and settlement disabled. It changes
public observation freshness and checks recovery using the completed pilot's
backup; it makes no token transfer or new benchmark submission.

## Mutable block-head caching

The public testnet `/get-block` response advertises `Cache-Control: max-age=15`.
A live probe received `CF-Cache-Status: HIT` and `Age: 14` despite sending
`Cache-Control: no-cache`. Requests with and without `include_data` have
different cache entries, so two head reads can disagree near publication.
The original collector logs showed these disagreements and no capture of
height 1,342,117. This proves a cache hazard, not the exact cause of that
particular missing block.

`PublicTigClient` now gives each `/get-block` read a unique query value and
retains response cache headers and request timing as provenance. Block-specific
feeds keep their normal cache keys. Accounting still requires a complete,
consistent snapshot between matching block heads; the fix does not waive any
coverage check. A read-only live probe captured blocks 1,342,172–1,342,174
consecutively, with all active benchmarks and reconciled qualifier totals.

The published backend's [get-block implementation](https://github.com/tig-foundation/tig-backend/blob/2740d32e2ab42cd2c1c6559ed8f49b389e1e0754/src/api/get_block.rs)
returns only the latest cached block and does not accept a block selector.
The client now rejects historical selectors locally, avoiding an apparent
successful backfill that actually returns today's head. The backend also
[prunes algorithm, OPoW and benchmark response caches after two blocks](https://github.com/tig-foundation/tig-backend/blob/2740d32e2ab42cd2c1c6559ed8f49b389e1e0754/src/context/mod.rs#L2372).

No complete header for the missing block was found in the independent funding
captures from that time, and its old algorithm query returned HTTP 400. It
remains missing. Round 134 also began before observation started, so recovering
that single block would not make it a complete reward round. Round 135 remains
the first intended complete reward round. Independent host-loss coverage is
still a separate final validation task.

## Completed-state restore and replay

The actual post-benchmark PostgreSQL backup was restored to an isolated
database with no attached payment or submission service. Both member SQLite
backups were copied into separate rehearsal directories. The rehearsal:

- rebuilt all 50 nonce results for each member and exactly reproduced the
  saved Merkle roots and five requested proofs;
- replayed handover, results and proofs twice without changing their contents,
  ownership, balances, collateral, slots, journals or submission states;
- replayed six actual protocol blocks and restarted a coordinator twice with
  no callable network transport;
- rejected changed results, another member's access, duplicate precommit
  sends, a rejection contradicting accepted work, early finalization and an
  incomplete reward round;
- changed one member's multiplier only in the copy and preserved both existing
  1 TIG holds and their original multiplier snapshots.

Live monetary state was compared before and after and stayed unchanged. The
existing failure/recovery suite additionally exercises lost responses, failed
handover, rejected precommits, expiry, pending arbitration and later-reservation
multiplier changes. Those injected outcomes are simulations; they are not
new intended-account live rejection, expiry or arbitration evidence.

## Reporting scope and finality

The public reporting index places both pilot benchmark IDs in round 134.
The reporting collector must run with the pool player ID and relevant
challenge IDs to retain these positive associations, as well as report lists.
Keep round 134 explicitly configured while any of its obligations remain open.

The backend [assigns a reporting round when benchmark results are confirmed](https://github.com/tig-foundation/tig-backend/blob/2740d32e2ab42cd2c1c6559ed8f49b389e1e0754/src/context/mod.rs#L2116),
not when the precommit was created. A benchmark crossing a round boundary can
therefore report under a different round. Finalization must use verified
per-benchmark associations and a complete reporting scope; a matching example
inside round 134 is not proof that the two round identifiers always coincide.

The observed testnet reporting `submission_period` is 0; the recorded mainnet
fixture uses 1. The [pinned protocol reporting rule](https://github.com/tig-foundation/tig-monorepo/blob/466d1409754ed459eac6f976c0c5d68f8679526e/tig-protocol/src/contracts/players.rs#L205)
closes new reports after the reporting round plus that configured period.
Our agreed hold still lasts through creation round X+2 and until the relevant
outcomes and arbitration evidence are final. Do not copy a testnet period into
a production adapter or interpret an empty response as proof of finality.

## Reward receipts and remaining live evidence

`get-round-emissions` exposes total, shared, penalty and destination amounts.
The backend's [penalty finalizer](https://github.com/tig-foundation/tig-backend/blob/2740d32e2ab42cd2c1c6559ed8f49b389e1e0754/src/context/mod.rs#L869)
reduces destination amounts and adds a `penalty` total. Absence of that field
does not prove a finalized zero penalty; the examined testnet round 131
response had no such marker.

The published [testnet mint preparation script](https://github.com/tig-foundation/tig-backend/blob/2740d32e2ab42cd2c1c6559ed8f49b389e1e0754/scripts/mint.py)
describes an operator-prepared batch mint. It contains old network metadata
and is not a current wallet instruction. Neither that source nor a public
earnings amount proves a transfer to this pool or a verified reward-round
attribution. Actual reward receipt/mint evidence and any current claim step
remain live settlement prerequisites. No claim or finalizer was called here.

The current testnet active challenges are c001, c002, c003 and c008, all CPU.
The current zero cutoff reflects missing challenge coverage, so a later
positive-reward test may fit the existing CPU machine. It must be sized inside
the unchanged 5 TIG ceiling and a new bounded test configuration. It does not
need GPU work merely to cover today's testnet challenges; GPU validation
remains a separate deferred execution test.

Definitive intended-account rejection and expiry still need live evidence.
Published code excludes result/proof confirmation after the 120-block window,
but disappearance from a current API feed cannot prove that a benchmark never
activated. Keep unresolved outcomes held rather than deriving a forfeiture
from a missing record. X+2 collateral release, positive reward allocation,
withdrawals, seven-day live re-eligibility and independent-host recovery remain
unticked until actually observed.
