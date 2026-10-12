# Lean storage and member master verification

Local checks on **11 October 2026** use a dedicated PostgreSQL 18.4 container,
generated wallets and disposable databases. The paired worker is from
`cleanup/direct-algorithm-downloads`. No production configuration is read by
the rehearsal and no live transfer or TIG benchmark is submitted.

## Coverage

All **336 pool tests** passed, with PostgreSQL, the paired worker and Chromium
enabled and no skips. The worker passed **33 v2** and **17 inherited** tests.
Both inherited pool accounting checks (`test_revenue_split.py` and
`test_work_credits.py`) passed. The 11 October pool CI used worker
`4bba91962db48903326d18d6e00f6ed2a4d4ff84`.

- Direct public archive downloads: matching hashes, AMD64/ARM extraction,
  restricted HTTPS sources, safe paths and redirects without credentials.
  Legacy issued assignments still use their saved archive endpoint.
- Compact projections: exact fractional pool credit and algorithm selection
  match full observations, including an external best reference. Invalid or
  incomplete sources cannot become compact accounting evidence.
- Additive conversion preserves original block identity. Raw expiry waits for
  completed credit/reconciliation and a verified backup of its replacement.
  Live references, open anchors, pending files and unresolved gaps survive.
- Database chunk deletion is bounded and audited. Concurrent file writers are
  protected both before manifest publication and during a cleanup scan.
  Corrupt pins/pages abort cleanup without discarding referenced data.
- Final report collection: owned scopes, actual confirmation-round mapping,
  X+2/report-window closure, cache grace, pending arbitration and reuse of final
  evidence. An empty pool does not poll the network's reports.
- A simulated CPU/GPU member master combines three helper partitions. One
  helper disappears; restart recovers completed work and finishes the missing
  nonces. Partial or changed uploads fail; identical retries succeed. The master
  returns one complete Merkle tree and the authoritative sampled proofs.

Runtime computations and TIG responses in the master test are simulated.
The 12 October [SSH member implementation](SSH_MEMBER_MASTER_VERIFICATION.md)
adds optional helper transport to the reference worker. A subsequent complete
real two-server CPU rehearsal passed; GPU execution remains unverified.

The subsequent [CPU hardware check](CPU_HARDWARE_VERIFICATION.md) on
`poolminer1` ran the real pinned TIG runtime and verifier. All 200 knapsack
nonces and three sampled Merkle branches passed. Cold-start wall time,
including the pause before resuming, was 20 minutes 35 seconds against a
nominal two-hour lifetime. The isolated pool API accepted these real results
and proofs, accepted identical retries, rejected a changed root and preserved
financial state. Actual public binary verification retained metadata only.
All 33 worker v2 tests also passed on the host's Python 3.14.4.

The original job-scheduling algorithm still failed a 480-second single-nonce
probe. Its old installation used only one worker and one CPU, but historical
timing does not support assuming that eight cores suffice for the full minimum.
Timely execution is established only for the measured knapsack workload; real
TIG acceptance and confirmation were not exercised.

On 12 October, the optional SSH master/helper implementation passed **46 worker
v2 tests** and **17 inherited tests** locally. A workspace coordinator and an
isolated helper on `poolminer1` computed and verified eight real knapsack nonces
over SSH, then recovered a saved output after reconnecting. Pre-handover
assignment validation also passed on that real helper. The later full two-server
rehearsal resolved helper access: `poolminer1` used six local and six remote
workers, verified all 200 knapsack nonces and three proofs, and recovered all
105 crash-saved outputs unchanged. Wall time including the deliberate fault and
restart pause was 15 minutes 8 seconds. Complete isolated API uploads, retries
and financial preservation passed. See the SSH verification note for exact
source revisions, resource limits, retained setup and remaining live checks.

## Physical upgrade and restore rehearsal

The fixture was created using deployed pool source `8477509` and migrations
001–019, with a funded member, an acknowledged benchmark and collateral,
qualifying credit, recorded raw/report files, a pending file and a missing
block. The current source applied migrations 020/021 twice without changing
the original migration checksums. Conversion ran in single-block batches.
Runtime and observer roles could read compact evidence using the updated grants;
neither received permission to change compact evidence.

`pg_basebackup` produced a **165,569,320-byte** fixture-cluster backup with a
SHA-256 manifest and streamed WAL. `pg_verifybackup` passed, including WAL
parsing. After that verified backup, bounded cleanup removed one eligible raw
database copy, its recorded spool file, a backed report file and one unused
database chunk. Financial records, credits, identities, the open anchor,
pending evidence and the missing height were unchanged.

The verified base was restored into a second isolated PostgreSQL container.
The restored database contained both original raw captures and compact
replacements, and reproduced the same financial/credit/identity state and gap.
Migration replay and compact reads passed after recovery.

This is a fixture restore, not a rehearsal of the full production database,
later off-host WAL replay or funded recovery-server takeover. It does not
measure production backfill time or storage savings.

Development evidence is retained under
`/root/mine-rootz/validation/lean-storage-20261011/`: suite logs,
`rehearse.py`, `rehearsal-result.json`, before-upgrade hashes,
`backup_manifest.json`, `pg-verifybackup.log` and the backup proof.

## Release steps

1. Pass paired pool/worker PR CI with an exact worker commit.
2. Verify timely execution on the intended compatible CPU/GPU hardware.
3. Prepare a paired release and fresh off-host backup with a compatible rollback
   reader. Deploy the worker and all pool collectors before enabling direct
   assignments or pruning.
4. Apply migrations 020 to 022 and grants, backfill bounded reviewed batches, then
   obtain a verified backup containing their replacements before production
   pruning. Existing monetary controls and gap/settlement gates still apply.
