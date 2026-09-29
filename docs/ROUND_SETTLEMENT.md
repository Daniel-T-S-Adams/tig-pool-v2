# Arbitration evidence and round settlement

The isolated v2 package now implements the agreed collateral and reward rules
against PostgreSQL. It does not start a payment process or automatically turn
on live settlement. Migration `005_settlement.sql` adds append-only reporting
evidence, reporting-round associations, finalization records and round accounts.

## Evidence collected before financial decisions

`tools/observe_reports_v2.py` independently polls public reports/arbitrations.
It captures the current and preceding three protocol rounds by default. Add
`--reporting-round` for an older unresolved round. Each request is bracketed by
current block reads, saved to a local durable spool before database access, and
linked to a complete block in the independent block archive when replayed.
Failed fetches, stale anchors and block transitions remain incomplete evidence;
they cannot stand in for an empty report list. Use a separate spool directory
and collector identity for each independent replica.

```sh
python tools/observe_reports_v2.py --collector reports-a \
  --spool /var/lib/innopool-v2/reports-a --recent-rounds 4
```

Supply the dedicated v2 database through `POOL_V2_DATABASE_DSN`. Schema
migration is a separate operation. This public GET-only collector has no TIG
API key, wallet key, settlement authority or transfer capability. Its finite
`--once` mode returns nonzero if recording remains pending or evidence is
incomplete. It requires the ordinary v2 block observer to archive its block
anchors; missing blocks keep report evidence in the local spool.

Optional `--player-id` and repeated `--challenge-id` arguments also capture
`/get-reportable-benchmark-ids`. Positive membership associates an owned
benchmark with its protocol reporting round, separately from its recorded
creation round. An empty index cannot establish that a benchmark has no
reports. Unowned public benchmark IDs never create internal ownership.

A read-only probe on 20 September found benchmark
`0017d114eead779d2a14567929f45c69`, created in round 135, in the round-135 index
for player `0xb9a66a0f6c1aedadfd7d03e9d9b2388da67300f5` and challenge `c008`.
It was absent from the adjacent round-134 and round-136 responses. This is one
verified association, not proof that creation and reporting rounds always
coincide. The probe's request provenance and counts are retained in
`tests/v2/fixtures/reporting-round-manifest.json`.

Confirmed report identities, nonces and arbitration decisions are immutable.
Omission or contradiction of a previously confirmed fact invalidates a capture.
Out-of-order replay can reveal an earlier confirmed report; existing seals are
checked against those newly learned facts before any new financial movement.

## Settlement interfaces

These are internal functions for a verified deployment adapter, not public
member APIs:

1. `reports.scope` freezes the explicitly verified protocol reporting rounds
   relevant to creation round X, with adapter version and evidence. It rejects
   a scope that excludes an already known benchmark/report association.
2. `reports.seal` requires a complete block in X+3 or later and complete latest
   captures for every scoped reporting round. It retains pending decisions;
   per-benchmark finalization holds only the affected benchmark. A new capture
   requires a new seal version before further financial actions.
3. `settlement.finalize_collateral` requires a definitive activation, verification
   failure or expiry and a freed slot. It returns the stored hold for active
   work without upheld reports, and for definitively expired work that never
   completed handover. Handed-over work that never became active, or has any
   final `nonreproducible` nonce report, forfeits its stored hold to creation
   round X. `reproducible` and final `inconclusive` decisions are not upheld
   reports. A pending decision keeps that benchmark's hold in place.
4. `settlement.declare_earnings` records final expected net receipts and any
   operating charges withheld by TIG. This declaration creates no spendable
   funds. A chain adapter must first verify and record actual incoming token
   transfers; `attribute_receipt` then assigns an unmatched receipt to its
   evidenced reward round. Previously attributed member deposits cannot be
   reused as rewards, and one event cannot fund two rounds.
5. `settlement.reimburse_operating_cost` moves the declared withheld operating
   charge from actual available operator funds into the round account. An
   unfunded operator promise cannot satisfy this requirement.
6. `settlement.preview` calculates a complete, funded round without changing
   balances. `settlement.allocate` repeats the same checks under database locks
   and posts the final allocation once. Recorded inputs include exact credit
   fractions, receipt events, forfeitures, reimbursement, rule version and seal.

All balances use integer token units. With member credit, the operator receives
5% of the complete pot, rounded down once, and members share the remainder by
exact cumulative credit. Largest remainders with stable member-ID ordering
allocate indivisible units. With proven zero member credit, the whole pot goes
to the operator once. A zero-value pot records completion without a monetary
journal. A member's current multiplier never reprices an existing hold.

For example, 98 TIG actually received after a 2 TIG operating charge, plus
2 TIG actually reimbursed by the operator, gives a 100 TIG pot. Credit in a 3:2
ratio produces 5 TIG for the operator and 57/38 TIG for the two members. No
submission or withdrawal charge is taken from the member allocations.

Credit follows the reward round of each recorded block, even for benchmarks
created earlier. Forfeitures follow the benchmark's creation round. Later
fraud does not change recorded qualifying credit. Missing block or ownership
data holds reward settlement; independently eligible collateral can finalize.

## Validation and remaining live gates

Real PostgreSQL tests simulate X through X+2, concurrent finalization and
settlement, restart/replay, several upheld nonces on one benchmark, handover
failure, multiplier changes and zero holds, independent collateral release,
missing blocks, exact equal credit, zero-credit pots and actual operator
reimbursement. Recorded public arbitration fixtures are replayed through the
new evidence store. These tests contain no real token transfers.

Live monetary operation still requires verification of reporting-round scope
at benchmark boundaries, authoritative expiry, intended-account precommit
rejection, final emissions/penalty classification and the mapping from actual
token receipts to reward rounds. The public earnings response alone does not
prove a token receipt. A reporting index alone does not establish scope for
failed or not-yet-reportable work. Neither assumption is automated here.

The [reward receipt investigation](REWARD_RECEIPTS.md) includes a finalized
mainnet distribution and a pinned TokenLocker reader. Its 28-day withdrawal
delay is separate from collateral finalization. Claimable/locked balances are
not spendable custody money; the reader does not yet automate attribution.

Finalized journal entries and allocations cannot be edited. A later externally
discovered inconsistency requires an explicit evidenced correction through
new journals and reconciliation; replay never silently reprices or debits a
member. The operator correction interface and production reconciliation gates
remain part of the remaining deployment work.
