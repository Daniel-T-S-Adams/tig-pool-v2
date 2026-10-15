# Storage ordering fix — 14 October 2026

## Deployed state

Pool `59a3446add2804c7f4dc42309039d55f46bf1c96`, tag `mainnet-storage-ordering-20261014`, is deployed on the
primary and staged on recovery. The API still serves worker
`dddad9df9b0d051cf004a904fe7b7361f9816c43`, with the same official 0.0.8 CPU
runtime pins. Pair digest: `ad94bd7a729f71e8a6d7ab7bbc85699b98fbd61f6bed9a56f44b6464156012bf`.

Migrations through **022** were applied twice. The earlier 21 checksums match;
runtime/observer roles can only read the new resolution table and view.
All six primary services and the retention working directory use this release.
Recovery's pool services remain inactive. New work is durably paused,
settlement is disabled and all paid trial approvals remain consumed/disabled.

Compatible recovery source is `4eb113060c9d97735b810e5871ffc9b39cb0ee75`, tag
`mainnet-storage-ordering-compatible-recovery-20261014`. Its application and
migration files match the active release. Older readers cannot account for an
ordering resolution and must not be used after raw expiry.

## Cause and resolution

Height **1382587**, block `62291a09861055099b59a6b6663c5fac`, had two captures:

- Legacy: `a2db1e02-798b-4b86-b8cb-32ed8bb87401`.
- Compact v1: `a1d1ac30-febd-44b0-b705-4bf5e7070cc7`.

Legacy paged storage sorted record lists by digest. Compact v1 canonicalized the
pool feed but kept the API's reward-schedule order when computing its source
identity. This produced different hashes for unchanged records at the rollout
boundary. Binary metadata ordering also differed; record values and
multiplicities were identical.

The frozen code validated the entire retained network and reproduced the v1
compact capture after changing only the two header schedule orders and the
binary metadata order. The canonical full source hash reproduced was
`67e2906bea48c4e10cca764330d2c217c1eecf02455e7b99a8d3e10f6d36ffee`.
Original block digest
`6ce8ef62c06f5567c0d60ce643bb25c97cbc870818c548031cb0744e3682a3ed`
and its original attempt ID are unchanged.

The operator maintenance tool appended immutable resolution **alert 2** with
the original/conflicting capture IDs, actor, reason, payload hashes and full
network replay hash. The original conflict and incomplete-capture alerts remain.
Status and all read/work/report/coverage/pruning gates consult unresolved alerts
individually; later or genuine conflicts still block use. The operator dashboard
labels the original conflict as resolved.

Source identity version 2 canonicalizes header record lists for comparison and
preserves the original header in the payload. Legacy and compact v1 hashes
remain supported without rewriting existing records.

## Conversion and protection

All **26,050** legacy observed blocks now have compact replacements, including
the held boundary. The ledger audit is empty and the accounts/journals hash
remains `783afcc922243a792ede48895e9b8c3577acdb63a4104e040538c3ba65b9c186`.
Missing heights **1375817** and **1376650** remain unresolved; the earlier
prelaunch waiver at 1372249 is unchanged. This storage fix does not establish
complete settlement coverage or a successful reward cycle.

The new off-host base `2026-10-14T134951Z` passed data checksums and WAL
parsing. An actual isolated restore of the verified 03:13 UTC base plus 229
later WAL segments reached `innopool_storage_20261014_134952`. All **26,050**
compact payloads, projection hashes, the resolution, original alert and **47**
protected table fingerprints matched. All 22 migration checksums matched, with
migration replay applied twice. The disposable database was removed after
verification. No live database or recovery application was activated.

## Completed cleanup

Guarded cleanup completed on 14 October at **16:57 UTC**, after the verified
replacement backup and actual restore. It removed:

- **26,050** original full capture payloads, leaving identity/provenance tombstones.
- **26,066** redundant block archives and **5,371,460** unreferenced block-file chunks.
- **5,262,807** unused database chunks and the same number of reference rows.
- At least **585,950** backed report archives. Counts from the interrupted
  ordinary pass and an in-flight batch were not fabricated; this is a lower bound.

All old identities, semantic digests, attempt IDs, credit/reconciliation and
financial records passed final preservation checks. **17** non-original legacy
captures (16 duplicates and one failed capture) remain under their existing
expiry guards. The report scan preserved **32,857** ineligible files. Pending,
future and unmatched data stayed protected; report chunks still referenced by
surviving files were kept.

Block files matched the exact immutable original manifest checksum (inline or
manifest_ref encoding), except one separately replayed canonical-order duplicate
at height 1375464. Its entire canonical network identity and pool projection
matched. The saved proof and recovered admin audit explicitly state that its
compressed spool-file checksum was not retained. The original source identity
and financial evidence were preserved.

The one-off report job checks payload SHA, exact scope and backed capture time
in bounded batches, releasing its maintenance lock so pending backups continue.
The one-off database sweep checks every current surviving manifest under the
retention/writer locks, memoizes only verified immutable legacy reference sets,
and deletes at most 50,000 chunks per batch. Its forward cursor performs a full
current recheck before finishing. Reference entries are removed for exact deleted
chunks or bounded pages proven orphaned. Normal PostgreSQL vacuum/analyze ran;
no full table rewrite or role/trigger bypass was used.

### Storage measurements

After cleanup, block/report spool allocation fell from about **33.84 GiB** to
**0.85 GiB**, a reduction of about **33 GiB**. Primary free space rose from
about **58 GiB** to **90 GiB** during the operation. The 26,050 replacement
payloads occupy about **0.90 GiB compressed**; surviving chunk payloads occupy
about **0.11 GiB compressed**. Allocated database pages remain larger and are
reusable; retained physical bases do not instantly shrink with logical expiry.

The deletion generated required WAL, which briefly pushed recovery below its
reserve. [Base placement](BACKUP_BASE_ARCHIVES_20261014.md) now uses existing
spare root disk for two older verified bases while keeping the newest base,
all required WAL and pending snapshots on the attached volume. Three bases
remain, with independent filesystem reserve checks; no storage was purchased.
Recovery health and fresh pending copying passed after relocation. Both hosts'
updated settings were encrypted, privately checked and copied off-host.

## Verification

The frozen release passed **390 tests** with PostgreSQL, the paired worker and
Chromium enabled, without skips, in
[hosted CI](https://github.com/Daniel-T-S-Adams/tig-pool-v2/actions/runs/38573915156).
The preceding local full suite passed 389 tests; the final local ordering suite
passed 13 tests, including the additional resolution-cutoff regression.
Both inherited accounting checks also passed in hosted CI. The archive/retention
and recovery-health changes separately passed 17 targeted local tests; the
one-off pruning helpers passed their isolated manifest, report-lock and chunk
retention checks. No new paid mainnet
benchmark was launched for this change.

Evidence: `/root/mine-rootz/validation/storage-ordering-20261014/`.
