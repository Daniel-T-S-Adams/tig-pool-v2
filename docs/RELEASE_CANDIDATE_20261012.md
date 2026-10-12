# Release candidate — 12 October 2026

The pool candidate pairs with worker
`a715041901e865d136138cff0017f46b90b3d5ce`. The production pool remains `8477509`
with served worker `fd29279`, migrations 001–019, paused new work and disabled
settlement. Neither this candidate nor its pending-file protection is activated.

## Completed preparation

- Reviewed the pending **Connect your master** onboarding page and member/owner
  guides. They now distinguish the default single-machine installer from the
  optional SSH mode and describe the completed two-server rehearsal accurately.
- Corrected release instructions to migrations **020/021**, the two actual
  additive storage migrations. No migration 022 is included.
- Implemented bounded snapshots of pending block/report/custody/funding evidence,
  including only referenced chunks. Verified content, a matching database
  cluster and durable recovery WAL acknowledgment are required for publication.
- Added a recovery health profile for database/WAL/settings/pending-file
  protection with inactive pool services and independent collectors.
- Passed **361 pool tests**, including both real HTTPS browser flows, paired
  worker/API checks, actual restored spool imports into PostgreSQL and all
  backup/health regressions, without skips. Both accounting scripts passed.
- Rechecked the exact paired worker: **46 v2 tests and 17 inherited tests** passed.
- Checked deployment unit syntax, example JSON and relative guide links.

The prior actual two-server computation and restart rehearsal remains recorded
in [SSH member master verification](SSH_MEMBER_MASTER_VERIFICATION.md). Its
15-minute measurement covers `c003_a153`, not the newer selected algorithm or
the original slow job-scheduling workload. Actual TIG acceptance is still open.

## Release gates still open

1. **Recovery capacity and fresh backup.** At 17:11 UTC Germany had about 15.0 GiB
   free. The last successful daily base is from 10 October; subsequent attempts
   failed the reserve. Required WAL is still streaming. No bases/WAL qualify
   for deletion under the existing policy. A new 200 GB volume is requested
   from the owner for verified relocation and the full restore rehearsal.
2. **Install and verify pending-file protection.** Run a real restricted SSH
   export/copy, enable its timer only after verification, and switch recovery
   health to the prepared profile. Back up the updated configuration/units.
3. **Combined production restore and paired activation.** Restore a fresh base,
   later WAL and pending evidence in isolation, then compare finance, ownership,
   holds, gaps and idempotent replay. Prepare a compact-format-compatible
   rollback reader before pool/worker activation or production pruning.
4. **Exact workload and paid trial.** Verify timing for the selected live
   algorithm/track, install one-attempt/workload/fee/collateral bounds and obtain
   the owner's paid-test approval and required signing. Watch actual acceptance
   and subsequent reports/finalization.

See [pending evidence installation](PENDING_EVIDENCE_BACKUP.md) and
[current operations](OPERATIONS_STATUS.md). Development logs, screenshots,
capacity evidence and the storage relocation plan are in
`/root/mine-rootz/validation/release-candidate-20261012/`.
