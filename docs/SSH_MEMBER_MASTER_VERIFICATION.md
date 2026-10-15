# SSH member master — 12 October 2026

The paired worker has optional member-owned SSH helpers. The master computes
locally while assigning other nonces from the **same benchmark** to explicitly
configured helper servers. The pool API, assignment format, collateral and
member slot rules are unchanged. Only the master uses a pool execution token.

## Implemented checks

- Verify every helper's architecture, configured verification compute type,
  runtime image mapping, timeout, master identity and concurrent capacity.
- Check ready capacity before a new offer or queued-offer refresh.
- Validate the exact saved assignment before handover and send its digest and
  confirmed handover timestamp before computation. Helpers save complete outputs
  before replying; the master saves them before assembling results and proofs.
- Recover missing nonces after master restart. A disconnected helper can be
  omitted from already handed-over work while compatible compute finishes it.
- Use strict SSH host-key checks, bounded messages/RPC waits and a helper lock
  that prevents competing sessions from restarting the same runtime.
- Retain output evidence through settlement; release only owned runtime containers.

All **46 worker v2 tests** and **17 inherited tests** passed locally and in hosted
CI. The paired pool hosted suite passed **336 tests**, with no skips. Unit tests
use simulated computation/SSH; the hardware checks below use actual SSH and TIG
runtime/verifier containers.

## Two-server hardware rehearsal

After the member installed the workspace public key, root SSH access to
`77.42.14.166` passed. Both intended servers are ARM64 with eight CPUs and about
16 GB RAM. `poolminer1` ran the master with six local workers and six SSH workers
on the helper. Both used worker `a296a76c2773ed8a800a1978a83f53625d14f8fc`, identical
pinned runtime images and configured `aws_c7g` verification type. The architecture
and actual verification path were checked; this does not certify a cloud SKU.

Isolated setup included a dedicated key generated on the master, strict known
hosts and helper authorization restricted to that master address and helper
command. Each host's slice enforced six CPUs, 10 GiB memory, no swap and 1,024
tasks; each runtime container had an 8 GiB memory limit and 512-process limit.
Docker and Python virtual-environment support were installed on the helper.

The fresh public knapsack input used `c003_a153 knap_exact16_fast3`, the
5,000-item/budget-25 track, four bundles and **200 nonces**. The actual TIG runtime
and verifier passed every nonce. The master returned **99** results locally and
received **101** from the helper, combining them into one result and three
verified sampled branches at nonces 0, 100 and 199.

At 105 durably saved outputs, the rehearsal disconnected the helper SSH session
and killed the master process with SIGKILL. After the helper finished bounded
in-flight work, the master restarted from its saved assignment and outputs,
reconnected the helper and finished the missing work. All **105 saved leaf
encodings and qualities were unchanged and were reused without recomputation**.

Wall time from the first coordinator start to completion was **908.33 seconds
(15 minutes 8 seconds)**, including preparation, the intentional fault and the
pause before restarting. This fits the assignment's nominal 120-block/two-hour
lifetime; actual block timing, network submission and acceptance were not tested.

The real combined result/proofs were replayed through the isolated pool API at
`8dc9b9dcdaac02d0750cc843855f9d108f62cafd`. Complete uploads and identical retries
passed, a changed root returned 409, and each result/proof outbox intent existed
once. Balances and journals were unchanged. Actual public archive verification
retained metadata only, with zero algorithm archive rows.

Protocol precommit receipts, sampled selection and activation were simulated.
No paid TIG submission, production execution token or member funds were used.
This proves real two-server computation, persistence and the isolated API
contract; it does not prove mainnet acceptance or final settlement.

## Cleanup and retained setup

Owned runtime containers were released and rehearsal units stopped. The labelled
disposable PostgreSQL container and volume were removed after API verification.
Both servers retain isolated source/configuration, cached images and evidence
under `/opt/innopool-member-fleet-validation-20261012`. Dedicated helper
credentials remain restricted. No autostart worker service was created; the
master points to `https://offline.invalid`. Hard slice definitions are temporary
in `/run/systemd/system` and disappear on reboot. A persistent release must
install those limits deliberately. Original master worker configuration,
installation marker and database hashes remain unchanged.

Development evidence is under `/root/mine-rootz/validation/member-fleet-20261012/`.
`verification-summary.json`, runtime results/proofs, per-node events,
recovery fingerprints, API replay and cleanup records capture the full scope.
The earlier eight-nonce workspace-to-master SSH subset check is retained under
`validation/member-master-20261012/`; its second-host authentication blocker
has been resolved by this follow-up.

## Remaining validation

1. Complete the paired release and production/backup checks before live work.
2. Run a separately capped mainnet member test; observe actual TIG acceptance,
   outcomes and finalization. The pool release and live controls are unchanged.
3. Establish timely execution for the original job-scheduling workload and
   validate GPU execution before offering either. This knapsack measurement
   is not a guarantee for other algorithms or machine configurations.

The setup guide is in worker PR
[#10](https://github.com/Daniel-T-S-Adams/innopool-slave-v2/pull/10),
`docs/SSH_MEMBER_MASTER_V2.md`. The paired work remains in draft pool PR
[#43](https://github.com/Daniel-T-S-Adams/tig-pool-v2/pull/43), without production
pool or served-worker activation.
