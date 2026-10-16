# Community Pool

The [14 October mainnet CPU trial](docs/MAINNET_CPU_TRIAL_20261014.md) established
an initial runtime-pin failure followed by successful corrected activation.
The master and helper completed 4,000 nonces and uploaded results in 12m40s;
TIG confirmed the proof and activated the benchmark. New work remains paused
while the remaining storage and settlement launch checks are completed.

Community Pool is the member-run pool for The Innovation Game that grew out of
the InnoPool v2 redesign. This independent repository preserves the original
pool's complete local Git history. The pool is deployed on the mainnet servers
with new work and settlement still disabled; the legacy instructions below are
kept for reference only. The system uses whole-benchmark
member work, custodial balances and collateral, equal qualifying-bundle credit,
round settlement and operator-reviewed withdrawals. See the
[redesign plan](docs/DESIGN.md) and
[implementation status](IMPLEMENTATION_STATUS.md) and
[checked operations status](docs/OPERATIONS_STATUS.md) before using this repository.
The new API serves its own member/operator screens and
[paired worker installer](docs/PAIRED_RELEASES.md). Releases are deployed as
fixed tags; `release/v2` points at the currently deployed one and `main` is the
integration branch. The old `/static/install.sh` installer is retired in this fork; use the checked download from a configured v2 pool's Join page.

For a high-level view of hosting, connections and deployments, see the
[pool structure diagram](docs/POOL_STRUCTURE.md) and
[Cloudflare setup steps](docs/CLOUDFLARE_SETUP.md). The name, mark and
colours used by the website are recorded in the [branding guide](docs/BRANDING.md).

The [13 October recovery update](docs/RECOVERY_VOLUME_20261013.md) records the new
volume, verified database/WAL/pending-file restore and active protection.
The [lean application and paired member master worker](docs/LEAN_ROLLOUT_20261013.md)
were activated on 13 October with new work paused. Historical conversion and the corrected mainnet CPU test have passed. See
[the storage ordering fix](docs/STORAGE_ORDERING_FIX_20261014.md) for verified backup, restore and
completed redundant archive cleanup.

For day-to-day operation, use the [owner's guide](docs/OWNER_GUIDE.md).
It explains the website and MetaMask steps, server configuration, software
updates, and the remaining deployment requirements.

Members building a master to use several compatible machines should follow the
[member setup guide](docs/MEMBER_SETUP_V2.md) and
[member master contract](docs/MEMBER_MASTER_V2.md). The pool assigns a complete
benchmark; members coordinate their own machines and return its combined result
and proofs through the existing API.
