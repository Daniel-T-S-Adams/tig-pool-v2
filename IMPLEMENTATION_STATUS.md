# InnoPool v2 implementation status

Updated 20 September 2026. The agreed plan is
[POOL_REDESIGN_PLAN.md](POOL_REDESIGN_PLAN.md). Repository preparation is
complete and both baseline CI suites pass. No v2 runtime has been deployed.

## Repositories and preserved baselines

| Repository | Provenance and visibility | Baseline |
|---|---|---|
| [tig-pool-v2](https://github.com/Daniel-T-S-Adams/tig-pool-v2) | Independent repository created from the intact local Git history with the user's approval. All 445 original commits are preserved. Initially private; the user subsequently made it public. | `19a7cafc135b4a1d3281d6acf48b5ebf13ed2b1a` |
| [innopool-slave-v2](https://github.com/Daniel-T-S-Adams/innopool-slave-v2) | Public native GitHub fork of `rootztigmod/innopool-slave`. | `14109c90b38ea342c8264e86ae122b6e9a0e49ea` |

The separate clones are `/root/mine-rootz/forks/tig-pool-v2` and
`/root/mine-rootz/forks/innopool-slave-v2`. Their `origin` and GitHub CLI default
repositories target the user's destinations. `upstream` pushes are disabled.
The pool clone does not share Git object hardlinks with the original checkout.
Both original checkouts remain unchanged.

Both repositories have an annotated `redesign-base` tag at their pinned
baseline. Integration and release branches are `redesign/v2` and `release/v2`.
Branch protection requires a pull request and the corresponding baseline CI
check, applies to administrators, and forbids force pushes and branch deletion.
No production service, wallet, or TIG submission has been changed.

## Foundation changes and validation

- [Pool foundation PR](https://github.com/Daniel-T-S-Adams/tig-pool-v2/pull/1):
  maintained plan, original questions, implementation status, and baseline CI.
- [Worker foundation PR](https://github.com/Daniel-T-S-Adams/innopool-slave-v2/pull/1):
  worker development notes and baseline CI.

| Check | Result |
|---|---|
| Worker regression suite | All 17 tests passed locally on Python 3.12.3 and in hosted CI. |
| Pool reward and work-credit checks | Passed locally, including without third-party packages, and in hosted CI. |
| Pool original Git history | Connectivity verified; the hosted baseline matches the original local commit. |
| Branch protection | Verified on both repositories' integration and release branches. |

The pool's initial private CI runs were blocked by an account billing or
spending-limit restriction before any tests started. After the user made the
repository public, both the
[push checks](https://github.com/Daniel-T-S-Adams/tig-pool-v2/actions/runs/35519030403)
and the
[pull-request checks](https://github.com/Daniel-T-S-Adams/tig-pool-v2/actions/runs/35519033126)
passed on rerun. That setup blocker is resolved.

## Protocol observation

Both foundation PRs are merged into `redesign/v2`; `release/v2` remains at its
original baseline. The [protocol probe PR](https://github.com/Daniel-T-S-Adams/tig-pool-v2/pull/2)
is also merged into `redesign/v2`.

The read-only probe captured consecutive live blocks 1,351,111 and 1,351,112,
covered every active benchmark, and reconciled exactly 4,000 qualifying places
per block. Recorded reports include 16 confirmed arbitrations for round 129
and 46 for round 130. Twenty-one local tests cover replay, exact equal sharing,
missing or inconsistent inputs, gaps, and report-result distinctions.

See [the protocol validation report](docs/PROTOCOL_VALIDATION.md) for commands,
recordings, the discovered chain-ID discrepancy, and the remaining Stage 0
integration checks. Those checks still gate the dependent live monetary adapters.

## Member funds implementation

The [member funds PR](https://github.com/Daniel-T-S-Adams/tig-pool-v2/pull/3), merged
into `redesign/v2`, implements the PostgreSQL journal,
wallet authentication and scoped tokens, confirmed-transfer verification and
deposit attribution, operator funding accounts, atomic benchmark collateral and
fee reservations, audited member multipliers, durable handover, withdrawal
reservations, and initial member/operator API routes. It includes exact reward
allocation arithmetic but does not yet post round settlements.

Real PostgreSQL tests cover simultaneous deposits, work/withdrawal contention,
shared operator funds and CPU/GPU slots, multiplier ordering, immutable journal
records, and replay. API tests enforce member/operator/token authority. See
[member funds implementation notes](docs/MEMBER_FUNDS.md) for the current API,
test commands, deployment boundaries, and remaining integrations.

All 56 tests in that increment passed locally and in hosted CI, along with the
legacy accounting checks.

## Block observer and selection

The [observer and selection PR](https://github.com/Daniel-T-S-Adams/tig-pool-v2/pull/4),
merged into `redesign/v2`, implements the independent
collector, local outage spool, compressed/deduplicated PostgreSQL history,
coverage cursors, gap/conflict detection, exact member credit attribution and
the agreed work selector. Recorded network fixtures exercise CPU and GPU
selection. Database and collector tests cover replica deduplication, recovery,
unavailable storage, missing ownership and independently complete reward rounds.

A finite read-only live run captured blocks 1,351,170 and 1,351,171. Replaying
them from the database again reconciled 4,000 qualifiers per block. The run
explicitly retained the missed launch height 1,351,169 as a gap. See
[observation and selection notes](docs/OBSERVATION_AND_SELECTION.md).

All 70 tests in that increment passed locally and in hosted CI.

## Member benchmark protocol and worker

The paired [pool PR](https://github.com/Daniel-T-S-Adams/tig-pool-v2/pull/5) and
[worker PR](https://github.com/Daniel-T-S-Adams/innopool-slave-v2/pull/2), both
merged into `redesign/v2`, implement expiring CPU/GPU compute offers,
atomic queue-to-reservation linkage, durable submission intents, exact-byte
assignment handover, full result upload and sampled Merkle proof validation.
The separate reference worker saves requests and every completed nonce, recovers
lost responses and interrupted compute, and retains evidence across restarts.
It isolates its own pinned challenge containers from existing deployments.

See [the versioned member API](docs/MEMBER_API_V2.md) and the worker's
`docs/WORKER_V2.md`. The paired test runs the actual worker against the pool API
with PostgreSQL for CPU and GPU assignments; it simulates TIG and compute.
Worker tests also reproduce the Merkle root from a recorded public TIG proof.

All 76 pool tests and the legacy accounting checks passed in hosted CI, along
with 12 new worker tests and all 17 inherited worker regressions for that pair.

## Submission recovery and saved algorithms

The merged [pool PR](https://github.com/Daniel-T-S-Adams/tig-pool-v2/pull/6)
and [worker PR](https://github.com/Daniel-T-S-Adams/innopool-slave-v2/pull/3)
connect the queue to a
separate, explicitly enabled TIG coordinator. It records potentially-sent
operations, persists positive benchmark IDs before fetching details, reconciles
lost responses without resending, and distinguishes confirmed proof receipt
from actual activation. It also captures the fresh pool's pending work,
replays stored blocks, preserves collateral after confirmed failures, and
prevents unresolved work from starving later eligible queue entries.

The pool serves its saved public algorithm archives by checksum. Real CPU and
GPU downloads verified the upstream redirect and actual named library layout;
the worker's artifact increment supports both. See
[submission recovery notes](docs/SUBMISSION_RECOVERY.md) for evidence and
remaining live-adapter checks. No actual algorithm or benchmark was run.

All 96 pool tests passed in hosted CI, with 13 new worker tests, the 17 inherited
worker tests and the legacy pool accounting checks. The pool CI uses the exact
worker artifact commit `4ff2cceed89b98ec65bc0a062ebcf391a8d23778`.

## Arbitration and round settlement

The merged [round settlement PR](https://github.com/Daniel-T-S-Adams/tig-pool-v2/pull/7)
adds independent public arbitration
capture with local outage recovery, immutable report and decision history,
positive protocol reporting-round associations, versioned post-X+2 evidence,
benchmark-specific collateral return/forfeiture, verified reward-receipt
attribution, actual operator reimbursement of withheld costs, and previewed or
posted round allocations. It keeps round credit separate from benchmark
creation rounds and requires complete recorded data before using the
zero-credit operator allocation. See [round settlement notes](docs/ROUND_SETTLEMENT.md).

The accelerated PostgreSQL simulations include concurrent/replayed payments,
multiple upheld nonce reports, multiplier snapshots, failed handover, pending
arbitration, missing block data, exact tied credit and unfunded earnings. The
deployment-specific reporting scope and final reward-receipt classification
remain explicit verified-adapter inputs. They are not inferred from a single
public sample or an estimated earnings response.

All 116 pool tests passed locally and in hosted CI, including the paired
worker/API tests and 20 new reporting/settlement tests.

## Operator-reviewed withdrawal implementation

The merged [withdrawal PR](https://github.com/Daniel-T-S-Adams/tig-pool-v2/pull/8)
adds operator review, frozen
payment routes, native operator fee reservations, durable manual-send attempts,
recovery from a lost transaction hash through its recorded nonce, verified full
payment and failed-transaction reconciliation. It also implements signed
withdrawal-wallet changes for future requests, custody-identity binding and
zero-valued token-event handling. See [withdrawal notes](docs/WITHDRAWALS_V2.md).

Targeted database and API tests cover successful/failed payments, replay,
concurrent nonce and fee-budget reservations, operator fee shortfalls, wallet
backing mismatches, authority, changed destinations and recovery. A public
finalized Base transaction verifies the current receipt shape; no member
withdrawal was sent.

All 134 pool tests passed locally and in hosted CI, including 18 new
withdrawal, chain and API tests.

## Member and operator screens

The merged [dashboard PR](https://github.com/Daniel-T-S-Adams/tig-pool-v2/pull/9)
adds the isolated v2
website, member funds and benchmark views, qualifying credit and settled
rewards, signed wallet login, execution-token management, reviewed withdrawals,
multiplier editing and audit history, finalization views and an audited pause
for new work. Round posting verifies the fingerprint of the operator's preview
inside its ledger transaction. See [dashboard notes](docs/DASHBOARD_V2.md).

Browser validation uses Chromium against the real HTTPS API and PostgreSQL,
generated wallets and simulated chain receipts. It covers exact 18-decimal
amounts, the complete withdrawal review flow, worker authority, multiplier
snapshots, pause/resume, token revocation and desktop/mobile layouts. It sends
no live transaction. All 143 pool tests passed locally and in hosted CI, including the browser
flow and paired worker/API tests. Both inherited pool accounting checks also
passed.

## Custody observation and incoming funding review

The merged [custody observer PR](https://github.com/Daniel-T-S-Adams/tig-pool-v2/pull/10)
adds continuous finalized TIG
transfer capture, local outage archives, offline replay, checksummed database
evidence, once-only deposit attribution and protected collection cursors. It
compares captured token-balance changes with verified receipts and checks all
custody funds and outgoing nonces before permitting new work or payment sends.
Catch-up, stale observations, unknown costs and canonical conflicts keep new
spending closed while existing work and reconciliation remain available.

The operator page now supports verified TIG receipt imports, direct native
funding and reviewed attribution of unknown deposits. A read-only public Base
capture reproduced a real 1.151417509939734543 TIG transfer and its balance
change from 22 archived RPC responses. See [custody observation notes](docs/CUSTODY_OBSERVER.md).

All 158 pool tests passed locally and in hosted CI, including 13 custody
capture/replay tests, funding API checks and the expanded browser flow.
No public probe was credited to a member
ledger and no live tokens were moved.

## Operator protocol fee funding

The merged [protocol funding PR](https://github.com/Daniel-T-S-Adams/tig-pool-v2/pull/11)
adds public fee-balance
collection and outage replay, operator-funded manual top-ups, shared withdrawal
and top-up nonce reservations, actual operator gas accounting, and once-only
protocol credit after both chain and TIG confirmation. New work pauses when
the observed prepaid fee balance is stale, incomplete or does not reconcile.
The operator dashboard supports the full review and recovery flow. See
[protocol funding notes](docs/PROTOCOL_FUNDING.md).

A public 30 TIG top-up matches its exact Base token event, player, destination
and amount. It is retained as an offline regression fixture; it does not fund
the test or deployment ledger. All 175 pool tests passed locally and in hosted CI, including 16
funding and observer integration tests, the withdrawal migration and the
expanded browser flow. The inherited accounting checks also passed.

## Paired installer and release metadata

The pool's paired-release increment and
[worker installer PR](https://github.com/Daniel-T-S-Adams/innopool-slave-v2/pull/4)
add checked release metadata,
installer downloads and hardware-specific Join-page instructions. The worker
installs a detached fork revision from a verified tag, preserves its private
configuration and evidence, refuses conflicting directories or modified code,
and requires a durable drain before upgrades. Routine restarts do not update
Git or rebuild legacy containers. See [paired release notes](docs/PAIRED_RELEASES.md).

The worker's 22 tests and 17 inherited regressions passed locally, including
real temporary Git checkouts, isolated environments, interrupted activation,
retained evidence and process-lock checks. All 180 pool tests passed locally,
including the expanded browser flow and paired installer/API check. Pool CI
pins worker commit `e6f4eadc24c81af4506d2080f795d00318911a2d`. Both increments
require passing hosted checks before merging into the protected integration
branches; neither is a production release.

Funds and work flags still default to disabled. No live service or wallet was
changed. Intended-account submission/rejection and expiry validation, live
finalization adapters, unconfirmed deposit views,
other operator expense/correction workflows, production release rehearsal and isolated
production deployment remain unfinished.
Real challenge execution and live protocol integration are not demonstrated by
these tests. There is no production v2 release yet. The remaining Stage 0 checks
still gate dependent live monetary operations.

## Testnet starter fee credit

Fresh testnet accounts can receive 10 TIG of prepaid fee credit without a token
top-up. Migration 010 and the explicit setup command now record that observed
balance once, solely as operator protocol credit. The setup requires the
matching testnet custody identity, fresh archived evidence from the official
testnet API, and no existing protocol funding or work. It cannot create member
cash or reset a balance after fees have been consumed. Ordinary funding
reconciliation continues after initialization.

Nine new PostgreSQL/setup-command checks cover duplicate and concurrent calls,
spending from protocol credit, separation from custody/member funds, wrong
network or evidence, prior activity, stale observations and immutable records.
The withdrawal upgrade rehearsal now restores both migrations 009 and 010 while
preserving real withdrawal fixtures. See [the setup procedure](docs/PROTOCOL_FUNDING.md#fresh-testnet-starter-credit).
This increment does not enable a live service, submit work or increase the
pilot's authorized spending budget.

## Bounded CPU testnet pilot

Migration 011 adds an immutable, optional policy for two CPU precommit attempts,
one per member in order. The second waits for the first to become active.
Potentially sent and rejected requests count; failure or uncertainty prevents
further work. Restart, setup replay and operator resume cannot reset the policy.
Reservations and first sends recheck the limit under the shared budget lock,
while recovery, results and proofs remain available. Fresh custody and fee
observers are required. Pilot state is available to the authenticated operator.

The initial allocation is 1 TIG per member, a `0.02` collateral multiplier and
a 0.01 TIG fee ceiling per attempt: at most 2.02 TIG authorized inside the 5 TIG
total cap. Extra attributed custody receipts pause spending, and token top-ups are disabled
in this mode. API/coordinator configuration must match the recorded testnet
identity. The new ASGI service factory keeps credentials separate from the
reviewable configuration and defaults funds, work and settlement to disabled.
See [local deployment and limit instructions](docs/LOCAL_CPU_PILOT.md).

Large unmatched receipts remain in the existing, non-spendable unattributed
account and do not increase pilot authorization. The funding guard counts
receipts only after attribution to a member or operator, and rechecks that
total before both reservation and first send. The operator status includes
attributed receipts, the unattributed balance and whether allocation is within
the recorded limit. Regression checks cover a 2,000 TIG unallocated receipt,
replay and restart, the unchanged two-attempt cap, and later attribution to
either a member or operator blocking a reserved first send.

## Native funding through contracts

Migration 012 and the operator receipt API now support finalized internal
native transfers identified by their exact call path. An explicitly configured
trace RPC must match the custody RPC's chain, canonical transaction and block.
The verifier checks the complete trace tree and rejects reverted ancestors,
non-transferring call types and custody-originated transactions. Immutable
receipt identities prevent concurrent imports or restarts from crediting the
same transfer twice. The outer sender pays its own gas; only the received
native amount credits the operator. Member TIG, collateral and pilot caps are
unchanged. The operator form accepts an optional call path, retaining its
existing direct-transfer behavior. See [native funding instructions](docs/CUSTODY_OBSERVER.md#native-funding-sent-through-a-contract).
