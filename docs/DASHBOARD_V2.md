# Member and operator screens

The v2 API serves its own static website at `/`, `/join` and `/operator`.
It does not load the inherited dashboard, scheduler or payout processes.
Deploy it behind HTTPS at the exact origin configured for wallet signatures.
Assets are local; no third-party scripts or analytics are required.

## Member account

Members sign a one-time message with their wallet. The page shows available
TIG, collateral, pending withdrawals, the current multiplier, benchmark holds
and slots, exact qualifying credit, and settled rewards. TIG amounts use integer
base units through the API and browser; they never pass through floating-point
currency arithmetic. Pending credit is shown separately from spendable funds.
Benchmark rows retain their original multiplier after an operator changes it.

Members can request a full-amount withdrawal, cancel a request that has not
entered a send attempt, verify a new destination with that wallet's signature,
and issue or revoke execution tokens for workers. Revocation does not require
retaining a token's original secret; a wallet session can list and revoke its
own active token identifiers. The server enforces ownership,
available funds, one pending withdrawal, and the seven-day interval after a
successful payment. Execution tokens cannot authorize financial actions.

Session, operator and newly issued execution tokens stay in page memory. They
are not written to browser storage. Leaving the page clears them, and restoring
a page from the browser's back/forward cache requires a fresh login. Financial
responses and the HTML shell use `Cache-Control: no-store`. The API remains the
authority for every action; hiding a button does not grant or remove access.

## Operator controls

The operator page uses the configured bearer token and shows aggregate ledger
accounts, members, collateral, withdrawal reviews, round settlement status,
uncertain protocol submissions, collection alerts and multiplier changes.
Member/hold activity is paginated. Recent round and alert summaries are bounded.
Member API projections only contain the authenticated member's activity.

An operator can change a member's multiplier with an audit reason, review or
reject withdrawals, prepare a manual payment and reconcile its confirmed token
event. Preparing a payment durably reserves its nonce and fee budget before
showing the custody route. The page does not send or sign custody transactions.
An uncertain attempt can be reopened and recovered from its recorded nonce;
closing a dialog does not release funds or permit a second payment attempt.

Round posting requires a calculated preview. Its fingerprint is checked again
inside the posting transaction. Changed evidence requires a fresh preview;
replaying the same approved allocation cannot credit the round twice. Wallet
addresses identify the member allocations displayed for review. Finalizing a
benchmark's collateral still requires authoritative outcome and post-X+2
arbitration evidence. The operator cannot bypass these checks from the screen.

## Pausing new work

`POST /api/v2/operator/controls/new-work` takes `paused`, `reason` and an
`event_key`. The change is audited and idempotent. Replaying an old pause event
does not undo a later resume. Migration 007 adds the control and immutable
history.

New collateral reservations and first precommit sends check the control under
the shared protocol-budget lock. An already committed send can still finish;
the pause is not a cancellation of in-flight requests. Unsent precommits can be
cancelled safely. Existing handover, result/proof upload, protocol recovery,
collateral accounting and withdrawal reconciliation remain available. Resuming
this control cannot enable work disabled by deployment configuration.

## Validation and remaining rollout work

The browser suite uses a real Chromium browser, isolated HTTPS FastAPI service,
PostgreSQL, generated signing wallets and simulated canonical chain receipts.
It checks exact 18-decimal amounts, wallet login, withdrawal review and payment,
worker authority, multiplier snapshots, work pause/resume, audit text escaping,
session storage, and desktop/mobile layouts. CI installs the pinned browser and
requires this test. Local invocation:

```sh
python3 -m pip install -r tests/v2/requirements.txt
python3 -m playwright install --with-deps --only-shell chromium
POOL_V2_BROWSER_TESTS=1 POOL_V2_TEST_DSN='<isolated database ending in _test>' \
  python3 -m unittest discover -s tests/v2 -p test_dashboard_browser.py -v
```

Set `POOL_V2_BROWSER_ARTIFACTS` to a local directory to save desktop and mobile
screenshots using fixture accounts only. Never run this test against a deployed
pool database: it deliberately recreates its isolated test schema.

Funds, work and settlement capability flags default to disabled. This screen
does not establish live reporting scope, final reward-receipt attribution or
definitive benchmark expiry. Those verified adapters, continuous deposit
indexing, operator funding/review workflows and the pinned worker installer
remain rollout work. The worker connection page labels the installer as
unreleased until the paired deployment is verified.
