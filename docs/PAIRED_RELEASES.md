# Serving the recorded pool and worker release

The API publishes a paired release and its checked worker installer. The
served pair names the tested pool and worker commits, pins the official
challenge runtime images by digest and offers only the resources listed in
the service configuration's `offered_resources` (CPU by default; GPU once
validated). A CPU pair has been validated on a private testnet, and a
two-server CPU benchmark has activated on TIG through a mainnet pair.
See the [deployment procedure](MAINNET_DEPLOYMENT.md); the checks that remain
open, including the reward cycle, are listed in [ROADMAP.md](ROADMAP.md).

Supply the API factory with `release_manifest`, the actual running
`build_commit`, and the paired `worker_installer` bytes. Defaults leave these
unset. The manifest is a separate deployment record created after the tested
pool and worker commits; publishing metadata must not alter those commits.
The build process must obtain its revision from the application artifact and
extract the installer from the recorded worker checkout. It must not infer a
running revision from an arbitrary value in the deployment manifest.

Startup verifies the expected repository URLs, full commits, release tags,
worker evidence format, pool/build match, installer checksum and official TIG
runtime digests. The API keeps its own snapshot of the validated metadata.
Changing the caller's configuration object cannot change the served pair.

| Route | Behavior |
|---|---|
| `GET /api/v2/capabilities` | Includes the canonical manifest SHA-256 as `release_digest` and the recorded `pool_commit`. |
| `GET /api/v2/release` | Returns the validated manifest. |
| `GET /api/v2/install-worker` | Downloads the checked installer bytes as `install_worker_v2.py`. |
| `GET /api/v2/worker-installation` | Generates instructions for one compatible `resource`, `compute_type` and `workers` capacity. |

These are public setup resources and do not grant member or operator authority.
For a separate API hostname, set `api_origin` independently of the wallet-login
website `origin`. Installer instructions and downloads use that API address;
the member protocol remains `/api/v2`. The default keeps both addresses equal.
Downloads and metadata use `Cache-Control: no-store`. No installer is served if
the release is absent or its supplied artifacts do not validate. Merely
configuring release metadata does not enable member funds, work or settlement.

## Manifest fields

```json
{
  "manifest_version": 1,
  "api_version": "2.0",
  "pool": {"repository": "https://github.com/Daniel-T-S-Adams/community-pool.git", "commit": "<40 hex characters>", "tag": "<tested pool tag>"},
  "worker": {"repository": "https://github.com/Daniel-T-S-Adams/community-pool-worker.git", "commit": "<40 hex characters>", "tag": "<tested worker tag>", "state_version": 1, "installer_sha256": "<64 hex characters>"},
  "runtime_images": {"c001": "ghcr.io/tig-foundation/tig-monorepo/satisfiability/runtime@sha256:<64 hex characters>"}
}
```

This is an interface example. A release producer must also record the tested
database migrations, application images, TIG/runtime versions and validation
evidence required by the plan. Those rollout artifacts and their verification
are not established by this minimal API schema.

## Member installation and updates

The Join page lets members choose CPU on AMD64, CPU on ARM64, or GPU on AMD64,
and concurrent nonce capacity. Only resources listed in the service
configuration's `offered_resources` are shown and served; by default that is
CPU only, until GPU execution has been validated end to end. It provides the checked download and commands
using a new `innopool-v2-member` directory. The command contains no execution
token; the installer prompts privately for it. Hardware compatibility is
checked before an installed worker can request work.

The worker installer verifies that its own bytes match the current manifest,
and that the fetched worker tag resolves to the full recorded commit. It also
checks the installer inside that checkout. Restarts use the installed detached
revision and make no Git network requests. Explicit updates preserve the
member's configuration, token and all saved benchmark evidence. They require
the worker to stop and SQLite requests to finish; they do not discard unresolved
work. A durable drain request lets a running worker finish and exit.

Download the current installer again before updating, since its checksum is
part of the release. The previous file can still request a drain. See the
worker repository's installation procedure (`docs/INSTALL_V2.md`) for recovery
details. The worker's former startup script invokes only the pinned v2
launcher; its previous Git-reset, Compose and service-installation behavior is
retired.

## Validation and remaining rollout

Pool tests check mismatched builds, installer tampering, unexpected forks,
moving references, unavailable releases and the unchanged funds/work flags.
The actual paired worker bootstrap reads the pool API's metadata and accepts
the served bytes. Chromium exercises the installation instructions alongside
member balances, withdrawals and operator top-ups. Worker installer tests use
real temporary Git repositories, tags, Python environments and subprocesses,
including lost activation, changed checkouts and unfinished evidence.

Automated tests simulate runtime execution and do not establish CPU/GPU
performance. The deployment procedure installs fixed tags, the release
manifest, restricted database roles and backup/restore protection. Measure
each challenge and hardware combination against TIG's benchmark lifetime
before offering it to members: a benchmark that does not complete within that
lifetime expires. GPU execution and a complete funded recovery/reward cycle
are among the checks listed in [ROADMAP.md](ROADMAP.md).
