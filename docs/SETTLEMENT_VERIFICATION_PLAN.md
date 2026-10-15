# Reward settlement verification plan — from 15 October 2026

Issue [#54](https://github.com/Daniel-T-S-Adams/tig-pool-v2/issues/54) asks for
the pool's reward settlement to be demonstrated on the 14 and 15 October
mainnet benchmarks before settlement is enabled. This records what was verified
on 15 October, what the rounds can demonstrate, and the dated steps that follow.

## Finding: these benchmarks earned nothing

Both benchmarks activated on TIG but recorded zero qualifying bundles: their
knapsack qualities were below the network cutoff, so TIG attributes no
emissions to the pool. On 15 October TIG's public player data for the pool
listed `round_earnings` as null for all 138 rounds and an empty
`reward_by_type`, with 0.005 TIG of fees paid and 4.995 TIG of fee balance.
The pinned TokenLocker reader showed claimable 0, locked 0 and no pending
withdrawals at Base block 52,645,795 (evidence under
`validation/settlement-verification-20261015/`).

The cycle these rounds can therefore demonstrate is the zero-earnings path:
collateral returned for active work, forfeited for the failed and expired
attempts, declared earnings of zero, and the forfeiture pot going to the
operator because member credit is zero. Mapping an actual token receipt to a
reward round, the remaining live gate named in
[round settlement](ROUND_SETTLEMENT.md#validation-and-remaining-live-gates),
needs a benchmark that qualifies. That requires a competitive algorithm and
hardware for the chosen challenge, or a GPU workload, and is a separate
decision for the owner.

## Verified on 15 October

| Item | State |
|---|---|
| Round 137 (ended 14 October) | coverage complete under the missing-block tolerance, 10,077 of 10,080 blocks, zero credit |
| Round 138 (ends 19 October) | 4,687 blocks captured so far, zero credit, three pool benchmarks recorded in reporting round 138 |
| Reporting scopes, seals, finalizations, earnings, receipts, settlements | none yet, as expected before X+3 |
| Reservations | 8d216c4c expired (round 137, 0.0005 TIG), 61513d43 verification failed (138, 0.0004), 76046f2f active (138, 0.0004), 88453646 active (138, 0.0004) |
| Reports collector | healthy; final reports are fetched after creation round X+2 ends |

## Timeline

Block height advances about once a minute; rounds are 10,080 blocks.

| Event | Height | Expected |
|---|---|---|
| Round 138 ends | 1,391,039 | 19 October, about 10:20 UTC |
| Round 139 ends | 1,401,119 | 26 October, about 10:20 UTC |
| Round 137 can be scoped and sealed (X+3 begins) | 1,401,120 | 26 October |
| Round 140 ends | 1,411,199 | 2 November, about 10:20 UTC |
| Round 138 can be scoped and sealed (X+3 begins) | 1,411,200 | 2 November |

TIG's own reward distribution and the TokenLocker's 28-day pending period do
not apply here because no emissions are attributed; if a later benchmark
qualifies, add 28 days after the claim before a wallet transfer can be indexed.

## Steps when each window opens

For creation round X, with the normal pause and settlement controls unchanged
until the final step is reviewed:

1. Confirm the reports collector has complete final captures for the scoped
   reporting rounds, then record `reports.scope(X, rounds, adapter_version,
   evidence)` and `reports.seal(X, capture_ids, block_id)` with a complete block
   in X+3 or later.
2. `settlement.finalize_collateral(reservation, seal)` for each reservation of
   round X. Expected outcomes: 8d216c4c forfeited to round 137 (handed over,
   never active); 61513d43 forfeited to round 138; 76046f2f and 88453646
   returned to the member, since both activated with no upheld report.
3. `settlement.declare_earnings(X, expected_received_net=0,
   withheld_operating_cost=0, ...)` citing the TIG player data and the
   TokenLocker reading at that time as evidence; no receipt attribution exists.
4. `settlement.preview(X, seal)` must show a pot equal to the forfeitures,
   zero member credit and the whole pot to the operator; compare with the
   member balances and the ledger audit, which must stay empty.
5. Only after the preview is reviewed, enable settlement for that round and
   `settlement.allocate` once, then re-disable. Record the sealed inputs and
   outputs in the operations status.

Re-read the TokenLocker before step 3 with:

```sh
python tools/inspect_rewards_v2.py --rpc https://mainnet.base.org --chain-id 8453 \
  --token 0x0c03ce270b4826ec62e7dd007f0b716068639f7b \
  --wallet 0xd4a076b5335f0fb2da40158b52e0bb6d6ac59ebf \
  --locker 0x9f6b29e498ef6bee4a050fa1f29c31dbe6c6aef4 \
  --code-sha256 a38dc562bbe1339d79fb13f088ffb4f27379c26e7e4c0d865ec0b5d99892a35e \
  --output <evidence file>
```
