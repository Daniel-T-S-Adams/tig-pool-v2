# SSH member master — 12 October 2026

The paired worker now has optional member-owned SSH helpers. The master may
compute locally while assigning other nonces from the **same benchmark** to
explicit helper servers. The pool API, assignment format, collateral and member
slot rules are unchanged. Only the master uses a pool execution token.

The intended member setup is `poolminer1` as master with local compute and
`77.42.14.166` as its helper. SSH to `poolminer1` works. The second host responds
but rejects the available workspace key for root login. Its architecture,
resources and ability to join the benchmark are therefore not yet verified.

## Implemented checks

- Verify every helper's architecture, verification compute type, runtime image
  mapping, timeout, master identity and configured concurrent capacity.
- Check ready capacity before a new offer or queued-offer refresh. A helper
  disconnection does not keep refreshing unavailable capacity.
- Send the exact saved assignment and digest after confirmed handover.
  The helper saves full outputs before replying; the master saves them before
  assembling the whole result and authoritative sampled proofs.
- Recover missing nonces after master restart. A disconnected helper can be
  omitted from already handed-over work while compatible compute finishes it.
- Use strict SSH host-key checks and bounded message/RPC sizes and waits.
  A helper lock prevents concurrent sessions restarting the same runtime.
- Retain assignment/output evidence. Session cleanup removes only owned runtime
  containers; it does not discard settlement evidence or change pool funds.

The worker tests include real subprocess pipes for concurrent helper messages;
their computation and SSH executable are simulated. The existing pool tests
exercise complete results/proofs, member ownership, retries and financial rules.

All **45 worker v2 tests** and **17 inherited tests** passed locally. A real
SSH/Docker check then used a workspace coordinator and an isolated two-worker
helper on `poolminer1`. Eight nonces from the archived public knapsack assignment
passed the actual TIG runtime/verifier in 129.45 seconds including preparation.
A new SSH session recovered a saved nonce from the helper cache in 0.004 seconds.
This was a subset check with no pool calls, complete benchmark or paid TIG
submission. It did not use `77.42.14.166`. Owned test containers and the temporary
slice were removed; original worker configuration, installation marker and
database hashes remained unchanged.

## Remaining validation

1. Resolve SSH authentication on the second member host and verify compatibility.
2. Install isolated master/helper configurations on the two intended servers,
   with dedicated SSH authorization, known hosts and hard resource limits.
3. Complete a real minimum-sized benchmark across both machines without paid
   TIG submission. Check both contributed, interrupt a helper and restart the
   master, then verify the combined result/proofs through the isolated pool API.
4. Measure the selected workload against TIG's lifetime before paid precommit.
   Extra compute is not a guarantee; the original job-scheduling workload still
   lacks timely-completion evidence on these machines.
5. Complete the paired release, production/backup checks and a separately capped
   mainnet test. Observe actual TIG acceptance, outcomes and finalization.

The setup guide is in worker PR
[#10](https://github.com/Daniel-T-S-Adams/innopool-slave-v2/pull/10),
`docs/SSH_MEMBER_MASTER_V2.md`. Development evidence is retained under
`/root/mine-rootz/validation/member-master-20261012/`. These changes have not
been deployed to the production pool or its served worker.
