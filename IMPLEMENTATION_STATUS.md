# InnoPool v2 implementation status

Updated 20 September 2026. The agreed plan is
[POOL_REDESIGN_PLAN.md](POOL_REDESIGN_PLAN.md). Implementation has started with
the fork prerequisite. No v2 runtime has been implemented or deployed.

## Fork setup

| Repository | Status |
|---|---|
| Pool | Created as the private [Daniel-T-S-Adams/tig-pool-v2](https://github.com/Daniel-T-S-Adams/tig-pool-v2) repository with the user’s explicit approval. All 445 commits reachable from the original baseline are preserved. This is an independent GitHub repository, not a native fork. |
| Worker | Native GitHub fork created at [Daniel-T-S-Adams/innopool-slave-v2](https://github.com/Daniel-T-S-Adams/innopool-slave-v2), verified as a fork of `rootztigmod/innopool-slave`. |

The worker clone is `/root/mine-rootz/forks/innopool-slave-v2`. Its `origin`
targets the new fork, `upstream` pushes are disabled, and the GitHub CLI default
repository targets the fork. Hosted branches `redesign/v2` and `release/v2`
and the annotated `redesign-base` tag start at
`14109c90b38ea342c8264e86ae122b6e9a0e49ea`.

Worker CI and development notes are committed on `feature/fork-foundation` at
`871f736d749af1d565dcd0828da2198c2cc751f9`. The
[draft foundation PR](https://github.com/Daniel-T-S-Adams/innopool-slave-v2/pull/1)
targets the fork's `redesign/v2` branch. Both GitHub Actions runs passed the
`worker-baseline` check. Branch protection on `redesign/v2` and `release/v2`
requires that check and a pull request, applies to administrators, and forbids
force pushes and branch deletion. The draft PR has not been merged.
Paired pool/worker compatibility checks belong to the later implementation
stages.

The pool baseline is `19a7cafc135b4a1d3281d6acf48b5ebf13ed2b1a` in
`/root/mine-rootz/tig-pool`. Neither original checkout has been modified.

The separate pool clone is `/root/mine-rootz/forks/tig-pool-v2`, prepared from
the original local Git history without shared object hardlinks. Its local
`redesign/v2`, `release/v2`, and `redesign-base` references use the pinned
baseline; the checked-out branch is `feature/fork-foundation`. The `origin`
URL targets the verified private GitHub repository; all baseline references
have been pushed and verified remotely. `upstream` pushes are disabled, and
the GitHub CLI default targets the private repository. A Git connectivity
check passed. Both pool branches have the same protection as the worker,
requiring the `pool-baseline` check. Pool CI and the maintained plan are
committed on `feature/fork-foundation`; the
[draft pool foundation PR](https://github.com/Daniel-T-S-Adams/tig-pool-v2/pull/1)
targets `redesign/v2`. It has not been merged.

## Existing checks

These establish the inherited baseline; they do not validate the new design.

| Check | Result |
|---|---|
| Worker: `python3 -m unittest discover -s tests -p 'test_*.py' -v` | 17 passed with Python 3.12.3. |
| Worker: hosted `worker-baseline` check | Passed for both the feature-branch push and draft pull request. |
| Pool: `python3 tools/test_revenue_split.py` | Passed. |
| Pool: `python3 tools/test_work_credits.py` | Passed. |
| Pool: private GitHub Actions `pool-baseline` | Did not start: GitHub reports an account billing or spending-limit restriction. This is not a failing test result. |

All local checks ran with `PYTHONDONTWRITEBYTECODE=1`. The pool checks also
passed in the new clone with `python3 -S`, confirming they need no installed
third-party packages. No production service, database, wallet, or TIG
submission was used.

GitHub’s [private CI run](https://github.com/Daniel-T-S-Adams/tig-pool-v2/actions/runs/35518923394)
reports: “The job was not started because recent account payments have failed
or your spending limit needs to be increased.” GitHub’s account billing
settings must be resolved before hosted checks can run. Repository privacy
and branch protections remain as configured; no billing setting was changed.

## Next step

The requested private repository is created and verified. The source-access
blocker is resolved, and no further repository permission is required. Both
foundation pull requests are ready for review; completing the private hosted
CI check is blocked by the GitHub account’s billing restriction. Once that
check can run, finish the foundation pull requests and proceed to Stage 0
protocol probes before implementing dependent accounting.
