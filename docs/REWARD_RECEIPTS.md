# Observing mainnet reward distributions

`tools/inspect_rewards_v2.py` reads finalized Base evidence. It verifies the
network, TIG token and pinned deployed TokenLocker bytecode, then reports
claimable, locked and pending amounts separately. It can decode a finalized
reward transaction and compare every player amount against a saved TIG
round-emissions response. It cannot sign transactions, change member balances,
declare a final round or enable settlement.

## Verified 23 September 2026

| Item | Verified value |
|---|---|
| Chain | Base, 8453 |
| TIG | `0x0c03ce270b4826ec62e7dd007f0b716068639f7b` |
| TokenLocker | `0x9f6b29e498ef6bee4a050fa1f29c31dbe6c6aef4` |
| Deployed code SHA-256 | `a38dc562bbe1339d79fb13f088ffb4f27379c26e7e4c0d865ec0b5d99892a35e` |
| `pendingPeriod()` | 2,419,200 seconds: 28 days |

The [verified contract](https://base.blockscout.com/address/0x9f6b29e498ef6bee4a050fa1f29c31dbe6c6aef4?tab=contract)
and direct RPC reads establish this lifecycle:

1. `rewardTokens` credits the beneficiary's claimable balance inside TokenLocker.
2. `claim` moves that amount into its locked balance. It sends no TIG to the wallet.
3. `unlock` creates a pending withdrawal with the contract's delay.
4. `withdraw` after the deadline transfers TIG to the beneficiary's wallet.

Only the last step produces a possible custody receipt. A ready pending
withdrawal is still not cash in the wallet. Pending-array indices change when
a withdrawal or relock removes an entry; recheck the entry before signing.
This delay is separate from the pool's X+2 rule and does not extend otherwise
releasable member collateral.

Public distribution
[`0x8210…9598`](https://base.blockscout.com/tx/0x821062a5aa221846457667b15b5e05ade81e7024e0d1f9eb90b72c5255619598)
at Base block 51,476,049 contains 339 `TokensRewarded` events. All 338 positive
player totals in the saved round-132 emissions response match exactly. One
additional allocation equals the round's bootstrap total. That extra is
reported for review and cannot be attributed to pool members.

These events contain no TIG round number. A full distribution match supports
attribution review; it does not prove arbitration finality or a wallet receipt.
The round-132 response has no `totals.penalty` field despite the exact observed
distribution. That field cannot be the sole finality test. Do not infer zero
penalties from an omitted field or subtract an already applied penalty twice.

## Inspect without sending transactions

**Role: pool operator. Computer: remote server.** Use the current reviewed
network and contract pin. This example only reads public mainnet data:

```sh
python tools/inspect_rewards_v2.py \
  --rpc https://mainnet.base.org --chain-id 8453 \
  --token 0x0c03ce270b4826ec62e7dd007f0b716068639f7b \
  --wallet 0xd4a076b5335f0fb2da40158b52e0bb6d6ac59ebf \
  --locker 0x9f6b29e498ef6bee4a050fa1f29c31dbe6c6aef4 \
  --code-sha256 a38dc562bbe1339d79fb13f088ffb4f27379c26e7e4c0d865ec0b5d99892a35e \
  --output /protected/new-reward-observation.json
```

Optionally supply `--transaction`, `--round` and `--emissions` together for
distribution comparison. Emissions can be plain JSON or a gzip probe archive
containing `payload`. Each output path must be new. Provider errors, rate limits,
changed bytecode and unfinalized evidence stop the check; they are not zero
balances. The tool does not retry a rate limit.
Reads are paced two seconds apart by default; configure a slower interval if
the selected provider requires it.

## Remaining operational connection

Before a real reward becomes spendable, retain a reviewed mapping from final
round earnings through claim/unlock to an actual indexed wallet transfer.
Reconcile operator-paid contract-call gas with the shared custody nonce history.
Settlement still requires complete round credit, final arbitration, received
money and any funded operator reimbursement. A manual transfer or dashboard
earnings figure cannot substitute for those inputs.

The reader provides evidence only. It does not yet implement the operator
claim/unlock/send workflow or automatic round attribution. The public batch
and nine reader tests validate observation, not a live reward cycle for this pool.
