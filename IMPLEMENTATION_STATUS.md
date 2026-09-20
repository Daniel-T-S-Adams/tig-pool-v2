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

The increment on `feature/reviewed-withdrawals` adds operator review, frozen
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

All 134 pool tests pass locally, including 18 new withdrawal, chain and API
tests. Hosted checks are required before this increment is merged.

Funds and work flags still default to disabled. No live service or wallet was
changed. Intended-account submission/rejection and expiry validation, live
finalization adapters, continuous chain indexing, product screens and isolated
production deployment remain unfinished.
Real challenge execution and live protocol integration are not demonstrated by
these tests. There is no production v2 release yet. The remaining Stage 0 checks
still gate dependent live monetary operations.
