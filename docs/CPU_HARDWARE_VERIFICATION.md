# CPU hardware verification — 11 October 2026

## Scope and machine

The member supplied `poolminer1` for an isolated CPU check. It has eight ARM64
Neoverse-N1 virtual CPUs and 15,568 MiB RAM. The test runs worker
`4bba91962db48903326d18d6e00f6ed2a4d4ff84`; its **33 v2 tests passed** on the
host's Python 3.14.4. Pool API replay uses
`63f8ceb9ca5e3dc49ea03106e7e9fb73883a2c7e` and a disposable PostgreSQL 18.4
database.

An independent worker directory and temporary systemd slice give the test six
concurrent nonces, an aggregate six-CPU ceiling and a 10 GiB parent memory cap.
The runtime container is limited to 8 GiB. The existing worker installation,
credentials, drain marker and pool production services are not activated or
changed. No precommit, paid TIG submission or real wallet transfer is made.
The assignment uses CPU verification type `aws_c7g`, matching the existing
offer; this check establishes execution with its ARM64 runtime on this host.

Both workloads use current public selection inputs, official public ARM64
algorithm archives with verified SHA-256 hashes, and official runtime images
pinned by digest. The worker downloads the binaries directly. Original network
snapshots are not retained by the test.

## Original job-scheduling workload

The old installation configured **one worker and one CPU**, leaving most of
this eight-CPU machine unused. Its saved 200-nonce benchmark
`290447ad305de92a644ccb03fc4b967e` used `c007_a042`, track
`n=50,s=hybrid_flow_shop`, and a fuel budget of 5,000,000,000,000 per nonce.
Saved timing shows 143,492 seconds (39.86 hours) from handover to the last
result, with a median of 721 seconds between results.

A new single-nonce probe of the same algorithm and track did not finish within
**480 seconds**, using the current public reference and pinned runtime. It was
stopped and its runtime container removed. The runtime used roughly one core;
allowing more aggregate CPU does not make a single-threaded nonce use all cores.

The current protocol minimum for this track is 160 nonces. Its lifetime is
120 blocks, nominally 7,200 seconds at a 60-second block target. Based on the
old time to nonce 159, ideal eightfold scaling would still take about four
hours. That is an estimate from historical timing, not a measured complete
eight-core run. This algorithm/track/fuel configuration is **not validated for
timely execution on this machine**.

## Knapsack workload

The compatible CPU workload selected from public head 1380042 is `c003_a153`
(`knap_exact16_fast3`), track `n_items=5000,budget=25`, four bundles and
200 nonces. Its quality threshold is 15,000 and nominal lifetime is also
7,200 seconds. Hyperparameters are the reference's null default.

The archive SHA-256 is
`37f7050d94a2be76a3df7d12f821621bd13884a3278ea36976c4e5e8d8920cd5`.
The runtime image is
`ghcr.io/tig-foundation/tig-monorepo/knapsack/runtime@sha256:fb040e7eca936f08b6808136b26027d54b997f82eb02718e310124012909a840`.

Cold preparation took 13.66 seconds. The first nonce took 31.18 seconds,
passed the actual TIG verifier and was saved durably before the full worker
resumed the remaining 199 nonces.

The full worker completed at 19:24:07 UTC. **All 200 nonces passed TIG's
verifier**, with qualities from 30,286 to 66,586. The combined Merkle root is
`3ee2d51772624af200b8b9b30de827ebf8670518ab19814b53306996cce161ec`;
sampled branches for nonces 0, 100 and 199 independently reproduced that root.

The resumed full run took **1,126.36 seconds**. Cold preparation, the first
nonce and the full run total **1,171.21 seconds (19 minutes 31 seconds)**.
Elapsed wall time from the cold probe's start to completion, including the
manual pause before resuming, was **1,235.14 seconds (20 minutes 35 seconds)**.
Both are within the nominal two-hour lifetime. These times include runtime
verification and local result/proof creation, but exclude a real protocol
submission and confirmation.

The actual pool API accepted these real results and three proofs in the
isolated database. Identical retries returned the original response; changing
the root returned HTTP 409. Only one results intent and one proofs intent
were queued. Ledger balances and journal fingerprints stayed unchanged. The
pool verified the actual public archive but retained only its source, hash
and size metadata: **zero rows in `algorithm_archives`**.

All test runtime containers and the temporary slice were removed. Hashes of
the original worker configuration, installation marker and SQLite database
still matched their pre-test values. The disposable pool database was removed
after its evidence was saved.

## Evidence and limits

Development evidence is retained under
`/root/mine-rootz/validation/cpu-poolminer1-20261011/`; the independent remote
directory is `/opt/innopool-cpu-validation-20261011`. These contain public
workload hashes, timing, verifier output, result/proof payloads and replay logs.

The fixture simulates precommit acceptance and authoritative sampling. Real
TIG acceptance, network/confirmation delays, GPU execution and coordination
between distinct member machines are outside this check. The measured time
for one CPU workload does not establish timely execution for every CPU
challenge. Before another paid attempt, check the intended algorithm, track,
fuel and minimum nonce count against measured compatible compute capacity,
with time left for protocol exchange.

Protocol inputs: [current public configuration](https://mainnet-api.tig.foundation/get-block),
[TIG slave setup](https://docs.tig.foundation/benchmarking/setting-up-slaves)
and [benchmarking guidance](https://docs.tig.foundation/benchmarking/advanced-tips).
