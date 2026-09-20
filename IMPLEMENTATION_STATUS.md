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

## Next implementation stage

Stage 0 validates complete live TIG snapshots, benchmark lifecycle events,
public reports and arbitrations, qualifying-credit inputs, network identity,
and reward receipt reconciliation before dependent monetary operations are
enabled. The current baseline checks validate inherited behavior; they do not
yet establish v2 pool/worker compatibility or a deployable v2 release.
