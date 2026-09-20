# Operator submission fee funding

The pool uses its own operator TIG and native network-fee funds to top up the
TIG account's prepaid submission balance. Member deposits, collateral, round
pots and withdrawal reservations cannot fund this operation. Migration 009
adds durable top-up intents, positive protocol receipts and a shared custody
transaction registry. It preserves existing withdrawal attempts and outcomes.

## Observing the account

`tools/observe_funding_v2.py` brackets the public `/get-player-data` response
with two `/get-block` reads. The block and top-up policy must agree. The capture
includes the exact available fee balance and positive top-up records. A new
account may return `player: null` with no top-ups; that means zero fee credit.
Missing or inconsistent data cannot stand in for zero.

The protocol top-up owner is the source of the token transfer. This direct
funding adapter therefore requires the dedicated custody wallet and pool
benchmarker address to be the same. Both identities are frozen in the database
under the same lock, whichever monitor initializes first.

```sh
export POOL_V2_DATABASE_DSN='<isolated v2 runtime database connection>'
python3 tools/observe_funding_v2.py \
  --api-url https://mainnet-api.tig.foundation \
  --player-id '<dedicated custody and benchmarker address>' \
  --spool '<persistent v2 protocol funding archive>'
```

The collector makes public GET requests only. It fsyncs complete and failed
captures before database recording. Collection continues while a separate
recorder retries a database outage. Pending manifests and their chunks must be
backed up together. `--replay-only` imports pending captures without contacting
TIG. `--once` succeeds only after recording the capture and reconciling the
current fee balance. Replaying an older capture cannot replace a newer check.
Previously confirmed top-up identities and facts are immutable. Conflicting
positive history creates a persistent alert; a later balanced response cannot
clear it. Credit and new work remain held for audited reconciliation.

The observed balance is compared with all operator TIG accounts at the
protocol, including amounts committed to submissions. Checks expire after 120
seconds. Missing, stale or unexplained fee data pauses new benchmark
reservations and first precommit sends. Existing results, proofs and recovery
continue. The operator pause control cannot override this reconciliation gate.

## Preparing and reconciling a top-up

The operator dashboard shows protocol funding health and pending top-ups.
Preparing a top-up uses the most recent captured recipient and minimum amount,
plus fresh finalized wallet balances. It reserves the full amount from
available operator TIG and a network-fee budget from operator native funds.
The nonce is reserved in the same registry used by withdrawals, before the
manual transaction details are displayed. The browser and API never sign or
send the custody transaction.

The recorded route, amount, nonce, fee rule and original evidence are immutable.
Closing the dialog or losing an HTTP response does not release funds. Reusing
the same request key recovers the original intent without needing an available
RPC. A new intent cannot reuse a withdrawal or top-up nonce.

An operator then checks the transaction, supplying its hash or letting the
verifier recover it from the reserved nonce. A successful top-up requires a
finalized direct token transaction and an exact full-amount transfer event to
the frozen recipient. At this point the custody cash and actual operator gas
cost are recorded, and the top-up waits for TIG confirmation. The custody
observer recognizes this explained outgoing payment.

Submission funds are credited only when an archived TIG top-up positively
matches the same player, transaction hash, event index and full amount. Its
protocol identifier and transfer event can each be consumed once. The collector
automatically confirms already-paid top-ups when this evidence appears; the
operator page can request the same check. A token transfer alone cannot create
spendable protocol credit.

A finalized failed transaction, or an explicitly verified empty self-transfer
that consumes the reserved nonce, returns the operator TIG and charges its
actual native fee. An ambiguous send stays reserved. Unmatched successful
transactions require explicit reconciliation. If the actual gas fee exceeds
available operator funds, raw chain evidence is retained and the monetary
posting waits for additional operator funding. Members are never charged.

| Operator API | Purpose |
|---|---|
| `GET /api/v2/operator/funding` | Current fee check, policy and top-up status. |
| `POST /api/v2/operator/topups` | Reserve a manual send using `request_key`, integer-unit `amount` and `fee_limit`. |
| `GET /api/v2/operator/topups/{id}` | Recover the frozen manual instructions. |
| `POST /api/v2/operator/topups/{id}/reconcile` | Verify the chain transaction; optional `tx_hash` and `log_index`. |
| `POST /api/v2/operator/topups/{id}/confirm` | Match the latest captured positive TIG receipt. |

All routes require operator authentication. Preparing a new top-up also
requires enabled funds and matching configured custody/benchmarker addresses.
Reconciliation and existing-intent recovery remain available when new sends are
disabled. These routes do not accept an arbitrary protocol balance assertion.

## Evidence and rollout limits

The archived public fixture
[`protocol-topup.json.gz`](../tests/v2/fixtures/protocol-topup.json.gz) contains
a real TIG funding snapshot at height **1,351,384** and a verified **30 TIG**
Base transfer. TIG identifies its event as index **443** of transaction
`0x59afde3b553e55a3ede3d4a0133fa93c37dea707cd37f2c2cb821a2a65613c2b`.
The transfer's source matches the protocol player and its destination matches
the observed top-up address. Its actual Base fee is **279580507888 wei**.
The captured minimum is **5 TIG**; runtime instructions always read the current
policy. The public account was used only for schema verification and was not
imported into the deployment ledger.

The primary protocol interface is the
[TIG API schema](https://swagger.tig.foundation/swagger.yaml); the checked live
responses are retained because the running API may contain additional fields.
Database tests cover concurrent sends, duplicate credit, pending or mismatched
receipts, fee overruns, failed transactions, stale checks, identity binding,
outage replay and the withdrawal migration. Chromium exercises the complete
manual top-up and confirmation flow against the real API with simulated chain
and TIG observations.

This adapter does not classify unknown legacy protocol balances, automatically
reimburse arbitrary expenses, or create reward receipts. Those need explicit
audited evidence. Deployment must initialize both custody and funding monitors
before enabling live work. Intended-account submission and finalization checks,
isolated deployment and the paired worker installer remain rollout work.
