# Arbitration evidence and round settlement

The isolated v2 package now implements the agreed collateral and reward rules
against PostgreSQL. It does not start a payment process or automatically turn
on live settlement. Migration `005_settlement.sql` adds append-only reporting
evidence, reporting-round associations, finalization records and round accounts.

Checked 9 October: settlement is disabled. Round 137 has unresolved block gaps
at `1375817` and `1376650`, after the one-time prelaunch waiver for `1372249`.
The expired CPU attempt's slot is free, with collateral still held for its
existing X+2 rules. See [operations status](OPERATIONS_STATUS.md). On
14 October the owner chose the
[bounded missing-block tolerance](#bounded-missing-block-tolerance) below;
once deployed, these three heights are within its limit and no longer hold
round 137 by themselves.

An acknowledged benchmark that expires without ever sending results uses the
same finalization path as an explicitly rejected late result: release its work
slot on evidenced expiry, keep collateral held until normal finalization after
the end of X+2, and forfeit the original hold once to its creation round's pot
because it never activated. Submission fees remain operator costs. This does
not change reporting evidence, finalization gates or settlement controls. See
[the unsent-results expiry evidence](SUBMISSION_RECOVERY.md#a-member-never-returns-results).

## Evidence collected before financial decisions

The deployed [storage cleanup](LEAN_STORAGE.md) fetches final responses for
open obligations after X+2 and the applicable reporting window close. It keeps
positive benchmark/reporting-round mappings while available, and saves relevant
final facts instead of every repeated global response. Pending arbitration and
failed requests hold final evidence. The following describes the legacy capture
path; financial ownership and credit rules remain the same.

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
fraud does not change recorded qualifying credit. Missing blocks beyond the
tolerated limit, a conflicting block or missing ownership data hold reward
settlement; independently eligible collateral can finalize.

## Bounded missing-block tolerance

Decided by the owner on 14 October 2026 and implemented in migration 023; not
yet deployed. A reward round may settle while a bounded number of its heights
have no complete snapshot. Each tolerated height counts as zero pool credit for
everyone: the round's actual received reward is split by the credit recorded
in the observed blocks. One block is about 0.01% of a round, so a few misses
barely move any member's share, and no block is fabricated or interpolated.

The limits live in the append-only `missing_block_policies` table. The
migration records the chosen defaults: at most **50** missing heights per round
settle, and more than **10** raise a warning in block status so a failing
collector is noticed long before settlement. A round is held when it has more
missing heights than the limit, when it has no observed block at all, when it
has not yet ended on chain, or when it holds an unresolved conflicting block.
Observed blocks still need their credit marker as before.

`BlockStore.round_gaps(round)` reports the missing, waived, uncredited and
conflicting heights of a round against the current limits; `round_coverage`
and `round_credits` follow it. Block status lists `missing_by_round`,
`held_rounds` and the current policy; the operator dashboard shows the missing
count and state per round. Every settlement records the tolerated heights, the
waived heights and the policy revision in its immutable inputs and digest. A
genuine block recorded later cannot reopen a settled round; it still needs its
own credit marker in any round that has not settled.

Change the limits with the owner database role. Each change appends a reviewed
revision with its actor and reason; identical limits add nothing:

```sh
python tools/set_missing_block_policy_v2.py            # show the current limits
python tools/set_missing_block_policy_v2.py --max-missing-per-round 50 \
  --warn-missing-per-round 10 --actor "pool operator" \
  --reason "reviewed: the collector outage on 20 October was unrecoverable"
```

A limit of 0 restores the earlier strict rule. The primary health script
deployed on the host is maintained outside this repository and still lists
every unresolved gap as an issue; when this release is deployed, change it to
flag `held_rounds` and conflicting heights instead.

## One-time prelaunch block-gap exception

If a missing block cannot be recovered before the pool has accepted any mainnet
work, the operator may record one explicit prelaunch exception. This is a
documented assumption, not a fabricated block. It leaves the missing height
visible in observation status and assumes zero pool-owned qualifying credit at
that height. The waiver is allowed only while new work is paused, before any
pool reservation exists, when the missing height is bracketed by complete,
nonconflicting blocks in the same round. It may be used once; later gaps fall
under the bounded tolerance above.

The immutable waiver records the operator, reason, evidence, adjacent block
identities and the missing block ID referenced by its successor. It does not
remove the gap from raw history or release/forfeit collateral. A later valid
backfill remains usable as actual evidence. The approximation is safe for the
current prelaunch gap because no pool benchmark was submitted before the waiver;
never use it to skip a height after members could have earned credit there.

After the reviewed migration is deployed, prepare a small evidence JSON file
stating the operator's no-prior-work check and the source attempts to recover
the block, then invoke the explicit admin tool with the owner database role:

```sh
python tools/waive_prelaunch_gap_v2.py --height 1372249 \
  --actor "pool operator" \
  --reason "unrecoverable prelaunch observation gap; no pool work in round 137" \
  --evidence-json /secure/path/gap-1372249-evidence.json
```

The command is deliberately not part of deployment or startup. Do not run it
against production until the operator has reviewed the evidence and approved
this one-time accounting assumption.

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
