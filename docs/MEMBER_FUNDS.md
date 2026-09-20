# Member funds foundation

This increment implements the internal v2 funds services and their first API
routes. It does not run a live pool, accept work, send transactions, or finish
round settlement. Existing deployment entrypoints do not import it. The
remaining live protocol checks in [PROTOCOL_VALIDATION.md](PROTOCOL_VALIDATION.md)
still gate dependent monetary integration.

## Storage and accounting

Use a **separate PostgreSQL database and credentials** for v2. Pass its DSN
explicitly to `pool_manager.pool_v2.database.Database`; call `migrate()` during
controlled deployment, then `benchmarks.initialize_accounts()`. No legacy
environment variable, connection, table, scheduler, or retention task is used.
Ordered migrations carry checksums and refuse changed or unknown versions.

All token values are integer units (18 decimals for TIG). The append-only
journal balances independently for TIG and the native gas asset. Database
constraints/triggers reject unbalanced entries, negative internal balances,
changes to posted records, extension of a committed journal, direct balance
edits, and financial-table truncation. Cached balances reconstruct from entries.
Corrections use a new reversing journal, once per original journal.

Member available funds, per-benchmark collateral, and pending withdrawals are
separate accounts. Operator custody funds and **prepaid protocol fee funds** are
also separate: spending an already prepaid TIG fee is not a second outgoing
custody transfer. `ledger.backing()` reports custody liabilities by default and
can separately reconcile the protocol fee balance. Native gas is a different
asset. No submission or withdrawal cost draws on a member account.

The application database role must not own the schema or have DDL/TRUNCATE
permissions. Administrative access can change database enforcement; these
triggers do not attempt to defeat a database administrator. Deployment role
provisioning, continuously observed wallet reconciliation, and the live
protocol-fee top-up adapter remain to be wired into the isolated deployment.

## Identity, deposits and API

Wallet sign-in uses a server-created
[ERC-4361 message](https://eips.ethereum.org/EIPS/eip-4361), EIP-191 signature
recovery, the configured HTTPS origin and verified chain ID, a random one-use
nonce, and five-minute expiry. This first verifier supports externally owned
wallets; contract-wallet signature verification is not implemented. A stable
member ID is created only after signature verification. Wallet sessions expire
after 15 minutes. Separately issued execution tokens expire after 30 days, are
revocable, and cannot authorize withdrawals, multiplier changes, or new tokens.
Only hashes of bearer tokens are stored. API responses carrying member data
are marked `no-store`.

The read-only chain adapter verifies the configured chain ID/token decimals,
successful receipt, ERC-20 event identity, canonical block hash, configured
confirmation count, and finalized block (enabled by default). It has no sending
or signing method. The generated receipt tests exercise both acceptance and
rejection; the production network and custody still require deployment checks.

Transfers are unique by chain, token, transaction and log index. A repeated
observation cannot credit again. Only a verified source wallet auto-attributes a
deposit. Other receipts remain unattributed until operator review records its
evidence; a public transaction hash does not authorize a member claim. Operator
funding is attributed to its own account. Reward-receipt attribution remains
separate work pending the live emissions-to-receipt mapping.

`api.create_app(Settings(...))` provides the current API:

| Route | Authority / behavior |
|---|---|
| `GET /api/v2/capabilities` | Public; advertises v2, whole benchmarks, and work disabled. |
| `POST /api/v2/auth/challenges`, `/sessions` | Wallet challenge and one-use signature verification. |
| `POST /api/v2/auth/execution-tokens`, `/revoke` | Wallet session only. |
| `GET /api/v2/member/balance`, `/journal` | Member token; exact amounts encoded as decimal strings. |
| `POST /api/v2/operator/members/{id}/multiplier` | Separate operator credential; actor, reason and immutable revision recorded. |
| `POST /api/v2/withdrawals` | Wallet session, available funds, one pending request, seven days since previous payment; funds actions disabled by default. |

The HTTP deployment still needs TLS, ingress rate limits (especially sign-in),
operator access controls, secret configuration, and the remaining lifecycle
routes. Do not expose this factory as a finished production application.

## Reservations and handover

`benchmarks.reserve()` atomically locks the common operator fee budget and the
member, captures the multiplier revision, reserves the largest track's base
collateral times that multiplier, rounds up once, occupies one of two shared
CPU/GPU slots, and saves immutable selection/payload data. Zero collateral still
occupies a slot. Request retries recover the same reservation; changed inputs
under the same key conflict. Operator multiplier changes take the same member
lock. Existing holds cannot be repriced.

Before the future submission worker makes a network write, it must commit
`mark_submitting()`. Uncertain submissions cannot be sent twice or cancelled as
unsent. Definitive rejection returns the member hold and unused operator fee
capacity; actual costs debit only the protocol operator budget. The live
adapter must establish that evidence before calling these internal services.

Acceptance fixes one benchmark ID and assignment digest to one member. The
member's acknowledgement is durable and idempotent. Publication alone does not
establish handover. Activation/failure/definitive expiry frees the slot while
retaining collateral. A first acknowledgement after expiry is rejected; a retry
of a previously committed acknowledgement returns the original timestamp.
Actual X+2 collateral finalization belongs to the settlement increment.

Withdrawal requests reserve their full amount and freeze the destination;
execution tokens cannot create them. Operator review, potentially-sent attempts,
outgoing verification, payment/cancellation and the dashboard remain to be
implemented. No application signer is planned.

## Validation

Install into an isolated environment:

```sh
python3 -m venv /tmp/innopool-v2-test-env
/tmp/innopool-v2-test-env/bin/pip install -r tests/v2/requirements.txt
POOL_V2_TEST_DSN='host=127.0.0.1 port=5432 user=postgres dbname=innopool_v2_test' \
  /tmp/innopool-v2-test-env/bin/python -m unittest discover -s tests/v2 -v
```

Use a disposable database whose name ends in `_test`. The tests **drop and
recreate only its `pool_v2` schema** between scenarios. Never point this variable
at a deployed database. Hosted CI provisions its own PostgreSQL service and
sets the DSN, so funds tests cannot silently skip there.

Tests include simultaneous duplicate deposits, competing work/withdrawal
requests, cross-member operator budget contention, the shared CPU/GPU slot cap,
multiplier update ordering and retry preservation, zero-amount holds, uncertain
submission/rejection, handover recovery, frozen withdrawal terms, journal
enforcement/reversal, signature replay, revoked/scoped tokens, wrong transfer
network/token/finality, and exact rounding/reward allocation mathematics. The
allocation function alone does not post or settle a reward round.
