# Two-server mainnet CPU trial — 14 October 2026

The owner authorized one CPU benchmark, with at most 0.0004 TIG member
collateral and a 0.001 TIG pool submission fee. That attempt is complete;
another paid attempt requires separate authorization. Both trial plans are
consumed and disabled. Ordinary pool work remains paused and settlement disabled.

## Result

Benchmark `0fd6714756833d70378a1b630f93c126` used `c003_a154` on `aws_c7g`,
track `n_items=1000,budget=10`, four bundles and 4,000 nonces. Master
`204.168.170.205` and helper `77.42.14.166` each had six workers inside their
persistent six-CPU/10-GiB limits. The master saved all 4,000 outputs; 2,034
were computed by the helper and 1,966 locally.

| Event | UTC time |
|---|---|
| Precommit sent and accepted | 08:35:29 |
| Member handover | 08:36:02 |
| Results accepted by TIG | 08:46:33 |
| Structural Merkle proof response | 08:46:56 |
| Confirmed verification failure | 08:47:53, height 1383748 |

Computation and result upload took about **10 minutes 31 seconds** after
handover, within the nominal two-hour lifetime. This establishes the intended
two-server timing and upload path. It does **not** establish successful mainnet
activation: TIG's subsequent solution verification failed.

The actual fee was **0.001 TIG** and collateral hold **0.0004 TIG**. The work
slot was freed normally. Collateral remains held for the normal X+2 forfeiture;
it was not refunded or settled early. Ledger audit remained empty.

## Cause established by replay

The release manifest pinned knapsack runtime **0.0.7**, digest
`fb040e7eca936f08b6808136b26027d54b997f82eb02718e310124012909a840`.
The current official **0.0.8** digest is
`b86289d33a78688b561ffbade7f256dc2e7d289815a53192061512472c0b6014`.
The algorithm archive, benchmark settings and random hash matched TIG.

All three submitted sampled solutions passed the pinned 0.0.7 verifier but
failed 0.0.8. For nonce 129, 0.0.8 reproduced TIG's exact error: selected weight
853 exceeded the maximum 548. Recomputing the same real assignment/nonce with
0.0.8 produced a solution its verifier accepted. This identifies the runtime
mismatch that the earlier internally consistent unpaid checks did not catch.

The `/submit-proof` response `verified: {"Ok": null}` establishes the initial
Merkle verification step, not successful later solution verification. The
summary `/get-benchmarks` endpoint always redacts fraud allegation text; the
detailed `/get-benchmark-data` endpoint supplied the actual failure reason.
See [TIG's official API specification](https://github.com/tig-foundation/tig-monorepo/blob/main/swagger.yaml)
and [proof submission contract](https://github.com/tig-foundation/tig-monorepo/blob/main/tig-protocol/src/contracts/benchmarks.rs).
The existing pool failure classification was correct. An initially proposed
null-allegation decoder change was discarded before commit or deployment.

## Correction work

A separate unpaid master and the drained task-owned helper now use the exact
0.0.8 digest. Original helper configuration and all prior results are preserved.
All five current workload tracks are being rerun with real runtime/verifier
code and simulated pool/TIG replies, without a pool execution token or TIG API
key. The pool's served runtime manifest has not yet been changed. Finish the
corrected workload checks, update and verify the paired runtime manifest and
documentation, then obtain approval before another paid attempt.

Worker candidate `dddad9df9b0d051cf004a904fe7b7361f9816c43` adds a runtime
release check before new offers, unlinked saved offers and queued-offer refresh.
It verifies the pool's release digest and requires every configured backend's
runtime map to match. Existing handed-over work can still drain. All 53 worker
v2 tests and 17 inherited tests passed locally and in hosted run `38545322562`.
The pool CI now pins this candidate for actual API interoperability checks.

All five configured CPU challenge images have a different official 0.0.8
digest from the 13 October pair. The corrected paired runtime manifest must
pin all five current images. Hardware execution in this correction is limited
to c003; other workload viability and GPU checks remain separate launch gates.

## Separate storage hold

Historical conversion completed overnight except for one held block at
`1382587`, the legacy/compact rollout boundary. The two captures differ in
reward-schedule and binary-list ordering. Originals and the conflict alert are
retained; no primary raw archive was pruned. The automatic watcher stopped
before requesting a replacement backup. Validate the storage correction and
its backup/restore evidence before guarded deletion.

Detailed artifacts are in the shared workspace at
`/root/mine-rootz/validation/mainnet-cpu-trial-20261014/`.
