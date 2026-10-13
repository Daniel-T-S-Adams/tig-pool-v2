# Lean storage and member master rollout — 13 October 2026

The paired application was activated on Helsinki at **13:28 UTC**. Germany has
the same application and worker artifacts staged, with pool services inactive.

| Item | Verified state |
|---|---|
| Pool | `f5f1028ec3e8e2a0ee875e8cff4c0cb0a6d449e5`, tag `mainnet-lean-storage-member-master-20261013` |
| Worker | `a715041901e865d136138cff0017f46b90b3d5ce`, tag `mainnet-direct-binaries-member-master-20261013` |
| Pair digest | `c81b1914a7c1d192d3420324324ce2b93fa4ea28ca21c6e36f3a6397dae4816f` |
| Schema | Migrations 001–021; 020/021 applied twice, earlier checksums unchanged |
| Services | API, coordinator, blocks, reports, custody and funding use the new application |
| Maintenance | Retention uses the new readers; health and report replay follow the running application |
| Controls | Member funds enabled, new work paused, settlement disabled |
| Finance | Ledger audit empty; account/journal hash unchanged at `95f77c34…784e80` |

The final pool candidate passed **371 hosted tests**. The paired worker passed
its 46 v2 and 17 inherited tests. Compatible recovery commit
`086dee600bc77a4c2088aaa426f7a651ca6fc053` passed all **370 tests** again locally,
including PostgreSQL, worker/API and browser checks. Its application readers,
writers and migrations are byte-identical to the candidate. It is staged under
tag `mainnet-compact-compatible-recovery-20261013`; current backup operations
remain installed separately. It preserves format compatibility, but a fault in
shared application logic requires a forward fix. Returning to `8477509` after
compact writes would leave evidence unreadable. Do not rewind the live database
to roll back an application.

## Live checks

At 13:32 UTC both loopback and public API responses matched the recorded pair,
installer checksum and disabled effective work flag. Both pinned application
versions read the same actual compact block and produced the same checksum.
The first four new block payloads occupied **38,019–38,092 compressed bytes each**.
This is a short sample, not a steady storage or backup capacity measurement.
Round-137 gaps at `1375817` and `1376650` remain visible; exact settlement is held.
Existing grants remain narrow; the runtime role can read compact replacements
and insert binary metadata, without schema or metadata-deletion authority.

The encrypted configuration export `configuration-daily-2026-10-13T133507Z`
contains the new pair, installer, units, migration evidence and operational
scripts. Off-host checks and private decryption are recorded with the rollout.
The earlier [database/WAL/pending-file restore](RECOVERY_VOLUME_20261013.md)
remains separate evidence from a funded API takeover.

## Historical storage conversion

A first additive batch converted **20 legacy blocks**, preserving their raw
copies and identities. Three bounded processes then started converting the
remaining historical blocks, each with a one-CPU/1-GiB limit and a checkpoint.
This is running maintenance, not completed archive cleanup. All observed
historical blocks had credit and reconciliation records at the initial check.

Raw deletion requires a verified later base backup containing the replacement,
completed credit/reconciliation and the existing conflict/obligation guards.
The old scheduled retention job had been held by a stale backup proof while
backup capacity was unavailable. Its restricted SSH source already points to
the correct continuous-backup record; fetching the new proof now succeeds.
The preview retains the block floor at `-1`. No gap was waived and no primary
raw archive has been deleted by this rollout. Database pages and old backups
will not shrink merely because compact captures are enabled.

## Current CPU validation and remaining input

The 13:31 UTC public quote selected **c003_a154, satchel** on `aws_c7g`. TIG
selects **one** track from the proposed track settings. The three 1,000-item
tracks each require **4,000 nonces**; the two 5,000-item tracks each require
**200 nonces**. The 12 October 200-nonce timing for `c003_a153` does not establish
timing for this current algorithm and every possible assigned track.

The unpaid test of all five possible tracks passed on master
`204.168.170.205` and helper `77.42.14.166`, with six workers on each. Both have
persistent six-CPU/10-GiB aggregate slice limits. Their runtime source matches
the paired worker. The test used official runtime/verifier code but simulated
pool replies and activation; it had no pool execution token or TIG API key.
All **12,400 outputs** and **15 sampled proofs** were verified. The five tracks
took 10m33s, 12m33s, 9m18s, 11m04s and 10m32s respectively. The slowest fits the
nominal two-hour lifetime with substantial time for exchange and confirmation,
for this measured algorithm and configuration. All five actual result/proof
sets then passed an isolated pool API/PostgreSQL replay, including retries,
changed-root rejection, one intent per upload kind and unchanged finance.

The subsequent [RPC routing repair](RPC_ROUTING_20261013.md) uses Base for basic
finality/balance reads and Pocket only for log queries. It recovered current
custody reconciliation without changing balances or the work pause.

The quote proposes at most **0.0004 TIG collateral** and **0.001 TIG submission
fee** for one attempt. It is not spending authorization or an installed trial
policy. Workload timing is complete. A scoped trial driver and member launcher
are staged with approval false and both units inactive. Proposal/send limit
checks and an isolated real-input reservation passed; a competing member's
older offer remained untouched. Both entrypoints refuse unapproved execution.
The driver's immutable reservation binding and normal potentially-sent events
prevent a second attempt; its supervisor restores the pause on exit. The
existing member token was verified with GET only and was not exported. Obtain
the owner's capped-test approval before starting either unit; no new wallet
signing is currently needed while that token remains valid.
Actual TIG acceptance, later report/finalization, GPU execution and funded
takeover remain separate checks. Operator payout reserve remains unset.

Operational scripts, logs, checksums and current progress are in
`/root/mine-rootz/validation/lean-rollout-20261013/`.
