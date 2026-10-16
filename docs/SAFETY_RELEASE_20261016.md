# Safety release — 16 October 2026

Pool `ea4c830` (tag `mainnet-safety-release-20261016`) replaced `e3cf0d2` on both
mainnet hosts at **14:04 UTC**, at captured boundary 1386945 and behind restore
point `innopool_safety_release_20261016`. The worker pair stays `c6eccfb` and
the runtime image map is unchanged, so installed member workers keep working.
The financial digest was identical before migration, after migration and after
activation; new work stays paused and settlement disabled.

## What changed

| Item | Before | After |
|---|---|---|
| Schema | 26 migrations | 27: `spending_caps` (append-only, immutable) |
| Spending control | operator-monitored budgets | recorded cap enforced on every fee commitment and custody send |
| Offered resources | CPU and GPU on the Join page | CPU only (`offered_resources`) until GPU execution is validated |
| Web framework | FastAPI 0.115.12, Starlette 0.46.2 (seven advisories) | FastAPI 0.143.2, Starlette 1.8.0, installed offline from a shipped wheelhouse |
| Health check | block, backup and ledger rules | plus a missing cap or a window more than 90% committed |

## Recorded cap

Recorded at 14:04 UTC as `mainnet-spending-cap-20261016-v1` by the owner's
approval: 10 TIG of submission fees per rolling 30 days with at most 0.01 TIG
per attempt, and 0.0005 Base ETH of custody gas per rolling 30 days with at
most 0.0001 ETH per send. The window already counts the five 0.001 TIG
ceilings committed by the October benchmarks, so 9.995 TIG remain. The
operator reserve is not set because no operator income wallet is configured;
configuring operator payouts is a separate owner decision.

## Sequence

| Step | UTC | Result |
|---|---|---|
| Stage both hosts, upgrade both virtual environments from the wheelhouse | 14:00 | FastAPI 0.143.2, Starlette 1.8.0, pydantic 2.13.5 kept |
| Migrate on the primary, grants applied, digest unchanged | 14:00 | 27 migrations, zero cap rows |
| First activation attempt | 14:01 | stopped before any edit: the script required an income wallet for the reserve |
| Second activation | 14:02 | units switched; the API crash-looped because the upgraded packages were mode 600/700 |
| Permissions corrected (`chmod -R a+rX` on both venvs) | 14:03 | API served capabilities for `ea4c830` |
| Final activation on both hosts, records written | 14:04 | boundary 1386945, recovery collectors restarted |
| Cap recorded, health check run | 14:04 | no issues |

The deployment script now installs packages under umask 022 and re-reads
permissions afterwards. Rollback material: the configuration backup directory
`safety-release-pair-20261016T…` under `/etc/innopool-v2-mainnet/rollback/`
and the `wheels-rollback` directory of the staged wheelhouse (FastAPI
0.115.12, Starlette 0.46.2, pydantic 2.13.5). Migration 027 only adds a table.
