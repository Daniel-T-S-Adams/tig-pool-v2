# Operator-reviewed withdrawals

V2 keeps the full requested TIG amount reserved while the operator reviews and
sends a withdrawal using their own wallet software. The service holds no signing
key and its RPC adapter cannot send transactions. Native transaction fees use
separate operator funds, including fees on a failed or cancelled transaction.

## State and recovery

1. A wallet-authenticated member requests an amount within their available
   balance. One request may be pending at a time. Seven days must have elapsed
   since the last successful payment's chain block timestamp.
2. Operator approval freezes the configured chain, TIG token, custody sender,
   confirmation policy and fee model. The amount and recipient were already
   frozen by the member's request. Approval records the operator's review.
3. Before using their wallet, the operator starts a payment attempt. The service
   verifies finalized wallet balances against **all** custody accounts, checks
   the pending transaction nonce, and atomically reserves that nonce and an
   operator-native fee budget. The withdrawal becomes `uncertain` before the
   response is returned. Its instructions include the exact token contract,
   recipient, amount and nonce for the manual send. Reading or replaying those
   instructions is not evidence that another transfer is needed.
4. The operator supplies a transaction hash, or the service recovers the
   finalized transaction that consumed the recorded nonce. Recovery binary
   searches historical nonce counts and then reads the relevant block; it does
   not scan every intervening Base block. RPC providers must support those
   reads. An unavailable or still-pending transaction leaves the request held.
5. The verifier checks the configured chain, token, custody source, full amount,
   frozen destination, nonce, successful canonical receipt, confirmation count
   and finality. One matching transfer event pays one withdrawal. Actual native
   fees are charged to the operator in the same balanced transaction that
   records payment. The member's seven-day interval starts at the successful
   transfer's block timestamp. Replay never resets that timestamp.

A confirmed failed transaction consumes its nonce and charges the operator's
actual fee. It returns the same withdrawal to `approved`, retaining the member's
full reservation. A new attempt requires a new nonce and a fresh operator fee
reservation. An ordinary finalized, empty self-transfer with no logs can also
prove nonce cancellation. Other successful unmatched transactions require
explicit wallet reconciliation; their disappearance or a supplied hash cannot
release funds. Rejection or member cancellation releases the request only while
it has no unresolved send attempt.

If an actual fee exceeds its reservation and the operator's remaining native
funds are insufficient, the confirmed transaction evidence is retained while
the withdrawal remains uncertain. Operator funding must be replenished before
reconciliation can complete. Member funds cannot cover that difference.

## API

Existing member authentication and execution-token boundaries still apply.
Amounts are integer token-unit strings in requests and monetary API fields.

| Route | Authority and purpose |
|---|---|
| `POST /api/v2/withdrawals` | Wallet session; reserve a positive amount with a member request key. |
| `GET /api/v2/member/withdrawals` | Member; show only that member's requests and outcomes. |
| `POST /api/v2/withdrawals/{id}/cancel` | Owner's wallet session; reason and event key, before an unresolved send. |
| `POST /api/v2/member/withdrawal-wallet/challenges` | Existing member wallet session; request a challenge for a new destination. |
| `POST /api/v2/member/withdrawal-wallet` | Same member session plus the new wallet's challenge signature; changes future requests only. |
| `GET /api/v2/operator/withdrawals` | Operator; requests and attempt history. |
| `POST /api/v2/operator/withdrawals/{id}/approve` | Operator; review reason and frozen configured route. |
| `POST /api/v2/operator/withdrawals/{id}/reject` | Operator; reason and event key, only before an unresolved send. |
| `POST /api/v2/operator/withdrawals/{id}/begin` | Operator; request key and native `fee_limit`, then durable manual-send instructions. |
| `POST /api/v2/operator/withdrawal-attempts/{id}/reconcile` | Operator; optional `tx_hash` and exact `log_index`, or recover from the frozen nonce. |
| `POST /api/v2/operator/withdrawal-attempts/{id}/recover-sponsored` | Operator; exact `tx_hash`, `log_index` and review `reason` for the bounded sponsored recovery below. |

Beginning or approving a new payment requires `funds_enabled`. Reconciliation
and cancellation remain available for existing liabilities while new monetary
activity is paused. Payment routes also require explicit `custody_network`,
`custody_rpc_url` and `withdrawal_fee_model` settings. Authentication and custody
must use the same verified chain ID. No payment RPC is enabled by default.

The first verified receipt binds the database's custody identity. Subsequent
receipts and reviews must use that same chain, token and wallet. Existing
transfer history is checked before binding an upgraded database. Ledger
reconciliation includes member balances, collateral, pending withdrawals,
round accounts, operator funds and unattributed receipts.

The verified adapter now accepts zero-valued ERC-20 events. They are retained
exactly once without manufacturing a monetary journal or blocking an indexer.
Direct, confirmed native transfers into custody fund operator-native accounts;
replay cannot credit them twice. Native deposits are not member TIG deposits.

## Fee verification and current limits

The fee model is configured explicitly and frozen per withdrawal. Supported
rules are `ethereum`, `op-isthmus` and `op-jovian`. OP receipts require actual
L1 fee evidence in addition to execution fees. The operator fee calculation
changed with Jovian, and its `blobGasUsed` field describes DA footprint rather
than an additional blob payment. These distinctions follow the primary
[Base fee documentation](https://docs.base.org/specifications/transactions/network-fees),
[Isthmus specification](https://specs.optimism.io/protocol/isthmus/exec-engine.html#fees)
and [Jovian specification](https://specs.optimism.io/protocol/jovian/exec-engine.html#operator-fee).

The recorded Base receipt in `tests/v2/fixtures/base-transaction.json` was read
from `https://mainnet.base.org` at finalized block 51,570,607 on 20 September
2026. It exercises the actual Jovian receipt shape, L1 fee and DA footprint
fields. It is public test evidence and is unrelated to any member payment.
The new transaction verifier also checked that same transaction directly
against the public RPC, including canonical inclusion and finality; its
verified fee was 1,160,679,890,131 native units (wei).

New payments support ordinary direct EOA custody transactions (types 0, 1 and 2).
Custody preflight rejects contract or delegated wallets. Wallet login and
destination verification support EOA signatures. Continuous custody indexing,
member deposits, operator funding and member/operator screens are implemented;
live validation and production rollout remain separately recorded checks.

## Already sent sponsored payments

Migration 014 adds immutable authorization-payment evidence and observed custody
code. An operator's **Check transfer** can recover an initial EIP-7702 payment
already sent through a relayer. A single previously supplied hash is remembered;
checking again need not rediscover it through the direct sender's nonce.
The explicit recovery endpoint also accepts a review reason. Both require the
configured trace RPC, exact final receipt and full verification:

- The authorization signature belongs to custody, binds this chain and consumes
  the reserved nonce. The complete canonical block excludes other custody
  transactions or authorizations that could explain the change. Custody moves
  from empty code and nonce N to the signed delegation and nonce N+1.
- Exactly one token event pays the frozen recipient and amount. The custody
  balance change agrees. A complete canonical call trace proves zero native
  transfers and only the exact token call from custody. Extra effects,
  incomplete traces and reverted frames are rejected.
- The relayer's real sender, nonce and fee are retained separately from custody's
  authorization. Relayer gas is not a pool expense: the unused operator gas
  reserve is returned. The paid timestamp comes from the confirmed transfer.
  Repeated or concurrent recovery cannot pay twice.

This is recovery of an initial sponsored authorization, not general smart-account
custody. Arbitrary batching, token-paid gas and subsequent relayed payments
without a fresh consumed custody authorization remain unsupported and held.
New payments remain disabled for a delegated wallet, in preflight and the
operator page. Observation can continue after verified recovery. Restoring
direct-wallet mode needs its own recorded wallet action, nonce and cost
accounting; delegation history must never be deleted.

Deploy migration 014 before the application. Grant the restricted runtime
`SELECT, INSERT` on `pool_v2.custody_authorization_payments`; existing custody-check
permissions cover its new column. Recovery remains available while new funds
actions are paused. A prior application will fail closed on delegated custody;
do not downgrade or delete its recorded financial history to bypass verification.

The recorded Base Sepolia fixture replays a finalized 0.05 TIG test withdrawal
with its public signature, block, balances and trace; see
[fixture provenance](../tests/v2/fixtures/README.md). Authorization processing
follows [EIP-7702](https://eips.ethereum.org/EIPS/eip-7702). The service still holds
no signing key and has no transaction-sending RPC.
