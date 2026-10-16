# Community Pool

A member-run mining pool for [The Innovation Game](https://tig.foundation) (TIG).
Members connect their own machines and receive whole benchmarks; the pool
submits the work under its own TIG account, watches TIG's blocks to credit
the bundles that qualify, holds member balances and collateral in a custody
wallet on Base, settles each round's rewards with equal credit per qualifying
bundle, and pays operator-reviewed withdrawals. Every movement of money is a
balanced, append-only journal, and nothing is spent beyond an operator-recorded
cap.

The pool is one repository (this one) and a separate member worker that members
install from the pool's Join page.

## How it works

- **Whole benchmarks.** A member offers CPU or GPU capacity with a TIG
  verification compute type. The coordinator selects a challenge and algorithm
  from the live network, reserves a fee ceiling and the member's collateral,
  sends the precommit, and hands the complete benchmark to the member. The
  member computes every nonce, keeps durable evidence, and returns the result
  and TIG's sampled proofs through the member API.
- **Observation and credit.** Collectors record the pool's own feed from every
  TIG block and fetch whole-network references on demand. Qualifying bundles
  earn equal credit; conflicts between captures are resolved with appended
  proofs, never by editing history.
- **Funds.** Deposits are matched to verified member wallets, collateral is
  held per benchmark and returned or forfeited on the recorded outcome, and
  withdrawals are reviewed by the operator and reconciled against the chain.
  The software never holds a signing key; the operator signs sends outside it.
- **Settlement.** Reporting rounds are scoped and sealed after TIG's reporting
  delay, earnings are declared with evidence, and each round's pot is allocated
  to members by credit with the remainder to the operator.
- **Safety.** A recorded spending cap bounds submission fees and custody gas
  per rolling window and per attempt. New work can be paused durably. The
  recovery host keeps verified database backups and pending-file snapshots.

## Documentation

For operators:
[owner's guide](docs/OWNER_GUIDE.md),
[deployment](docs/MAINNET_DEPLOYMENT.md),
[paired releases and the worker installer](docs/PAIRED_RELEASES.md),
[backups and recovery](docs/BACKUP_OPERATIONS.md),
[pending-evidence snapshots](docs/PENDING_EVIDENCE_BACKUP.md),
[storage map](docs/STORAGE_MAP.md),
[evidence retention](docs/EVIDENCE_RETENTION.md),
[lean storage](docs/LEAN_STORAGE.md),
[Cloudflare in front of the pool](docs/CLOUDFLARE_SETUP.md),
[pool structure](docs/POOL_STRUCTURE.md),
[operator income](docs/OPERATOR_INCOME.md),
[protocol fee funding](docs/PROTOCOL_FUNDING.md),
[custody observer](docs/CUSTODY_OBSERVER.md),
[protocol validation](docs/PROTOCOL_VALIDATION.md),
[submission recovery](docs/SUBMISSION_RECOVERY.md),
[dashboard](docs/DASHBOARD_V2.md),
[branding](docs/BRANDING.md).

For members:
[member setup](docs/MEMBER_SETUP_V2.md),
[member API](docs/MEMBER_API_V2.md),
[member master contract](docs/MEMBER_MASTER_V2.md),
[one bounded worker on a shared machine](docs/LOCAL_CPU_PILOT.md).

Design and money:
[design](docs/DESIGN.md),
[observation and selection](docs/OBSERVATION_AND_SELECTION.md),
[member funds](docs/MEMBER_FUNDS.md),
[withdrawals](docs/WITHDRAWALS_V2.md),
[round settlement](docs/ROUND_SETTLEMENT.md),
[reward receipts](docs/REWARD_RECEIPTS.md).

What is not implemented or validated yet, and the decisions every operator
makes, are listed in the [roadmap](docs/ROADMAP.md).

## Repository layout

| Path | Contents |
|---|---|
| `pool_manager/pool_v2/` | The pool: API and website, coordinator, collectors, ledger, settlement, controls, migrations |
| `tools/` | Operations scripts run by the systemd units: collectors, coordinator, backups, health checks, retention, conflict resolution |
| `deploy/v2-mainnet/` | systemd unit, nginx and configuration templates for a two-host deployment |
| `tests/v2/` | The test suite, which is the specification for the money and protocol behaviour |
| `docs/` | Operator, member and design documentation |

## Development

Python 3.12 and PostgreSQL. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
test setup, the branch policy and the rules for changes that touch money or
the member protocol. Security reports follow [SECURITY.md](SECURITY.md).
Releases are fixed tags; `release/v2` points at the pool commit production
serves and `main` is the integration branch.

## Status

The pool has run on TIG mainnet with CPU benchmarks end to end: assignment,
member computation across two machines, TIG activation, credit recording and
collateral handling. GPU execution and a reward cycle with non-zero earnings
are still being validated; the [roadmap](docs/ROADMAP.md) has the details.

## License

[MIT](LICENSE).
