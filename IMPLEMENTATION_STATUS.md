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

The next increment on `feature/observer-selection` implements the independent
collector, local outage spool, compressed/deduplicated PostgreSQL history,
coverage cursors, gap/conflict detection, exact member credit attribution and
the agreed work selector. Recorded network fixtures exercise CPU and GPU
selection. Database and collector tests cover replica deduplication, recovery,
unavailable storage, missing ownership and independently complete reward rounds.

A finite read-only live run captured blocks 1,351,170 and 1,351,171. Replaying
them from the database again reconciled 4,000 qualifiers per block. The run
explicitly retained the missed launch height 1,351,169 as a gap. See
[observation and selection notes](docs/OBSERVATION_AND_SELECTION.md).

Work assignment remains disabled in the API, and funds operations default to
disabled. No live service or wallet was changed. Live submission/outcome adapters,
the whole-benchmark worker, finalization, completed withdrawal payments, product
screens and isolated production deployment remain unfinished. There is no paired
v2 release yet. The remaining Stage 0 checks still gate live monetary operations.
