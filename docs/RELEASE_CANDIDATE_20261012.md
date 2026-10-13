# Release candidate — 12 October 2026

The pool candidate pairs with worker
`a715041901e865d136138cff0017f46b90b3d5ce`. The production pool remains `8477509`
with served worker `fd29279`, migrations 001–019, paused new work and disabled
settlement. The application candidate is not activated. Pending-file protection
and recovery health were activated and verified on 13 October. See
[the completed storage/recovery record](RECOVERY_VOLUME_20261013.md).

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
- Passed **370 pool tests**, including both real HTTPS browser flows, paired
  worker/API checks, actual restored spool imports into PostgreSQL and all
  backup/health regressions, without skips. Both accounting scripts passed.
- Rechecked the exact paired worker: **46 v2 tests and 17 inherited tests** passed.
- Checked deployment unit syntax, example JSON and relative guide links.
- Verified collector ownership for root-created spool locks, initialization
  directories and garbage-collection journals. Tests drop privileges to the
  collector UID; unsafe linked control files are refused. Primary lock access
  was also checked as the actual `innopoolmainnet` service user.
- Fixed sign-in before the initial pool settings response arrives. The
  onboarding browser regression deliberately holds that first response and
  verifies successful sign-in, guide visibility and sign-out.

The prior actual two-server computation and restart rehearsal remains recorded
in [SSH member master verification](SSH_MEMBER_MASTER_VERIFICATION.md). Its
15-minute measurement covers `c003_a153`, not the newer selected algorithm or
the original slow job-scheduling workload. Actual TIG acceptance is still open.

## Release gates still open

**13 October:** capacity, fresh backup, restricted pending-file transfer/timer,
recovery health and combined production-copy restoration are complete. Exact
44-table fingerprints and actual block/report replay passed; the health extension
passed all **371 hosted tests**. Items 1/2 and the restore part of item 3 below
record the original 12 October gates. Paired activation, compatible rollback and
the exact capped paid trial remain open.

1. **Recovery capacity and fresh backup — complete 13 October.** Verified
   relocation, a fresh base and normal three-base/WAL retention passed.
2. **Pending-file protection — complete 13 October.** Restricted SSH, the timer,
   corrected health profile and encrypted configuration copies are verified.
3. **Combined restore complete; paired activation pending.** Finance, ownership,
   holds, gaps and replay checks passed on the isolated production copy. Prepare
   a compact-format-compatible rollback reader before pool/worker activation
   or production pruning.
4. **Exact workload and paid trial.** Verify timing for the selected live
   algorithm/track, install one-attempt/workload/fee/collateral bounds and obtain
   the owner's paid-test approval and required signing. Watch actual acceptance
   and subsequent reports/finalization.

See [pending evidence installation](PENDING_EVIDENCE_BACKUP.md) and
[current operations](OPERATIONS_STATUS.md). Development logs, screenshots,
capacity evidence and the storage relocation plan are in
`/root/mine-rootz/validation/release-candidate-20261012/`.
