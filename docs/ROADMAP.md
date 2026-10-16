# Known limitations and roadmap

This page is the single list of what the pool does not do yet, what has not
been validated end to end, and which decisions every operator has to make.
Other documents point here instead of carrying their own "remaining work"
notes.

## Not implemented

| Area | Limitation |
|---|---|
| Reward intake | The operator claims and unlocks TIG rewards outside the software and records the receipt by hand; there is no claim/unlock/send workflow and no automatic attribution of a receipt to a reward round. |
| Operator corrections | Reconciliation of unexpected custody movements is recorded through audited manual entries; there is no operator correction interface. |
| Member login | Only externally owned accounts can sign in. Contract-wallet signature verification and payments to smart accounts are not supported; relayed smart-account withdrawals are recovered but new ones are held. |
| Member administration | There is no per-member suspend or ban control. The operator can change a member's collateral multiplier and pause all new work. |
| Failover | The recovery host holds verified backups and a staged application, but takeover is a manual, rehearsed procedure, not automatic failover. |
| Worker deadlines | The worker's per-nonce timeout is not a whole-benchmark deadline. Operators must measure each challenge and hardware combination against TIG's benchmark lifetime before offering it to members. |
| Helper provisioning | The member master mode uses helper machines the member has already provisioned with SSH access, Docker and the pinned worker; the installer does not set helpers up. |
| Database space | Compaction frees rows, not disk; reclaiming space after large deletions needs a `VACUUM FULL` or `pg_repack` maintenance window. |

## Validated so far, and not yet

| Check | State |
|---|---|
| CPU benchmarks on mainnet | Completed end to end with the official pinned runtimes, on a single machine and on a master with one SSH helper, including TIG activation and credit recording. Rewards for those benchmarks were zero, so only the zero-earnings settlement path has been exercised in production. |
| GPU benchmarks | Execution timing on real GPU hardware is being measured; no GPU benchmark has been submitted through the pool. The pool offers CPU work only until this is done. |
| Challenge timing | Only the challenges an operator has timed on the members' hardware should be offered; selection chooses among all active challenges of a resource family at random. |
| Settlement with real rewards | Mapping an actual token receipt to a reward round, and the full reward cycle through the TokenLocker's pending period, have not been exercised with a qualifying benchmark. |
| Verified-binary reuse | The coordinator records checked archives for reuse; the reuse path has not yet served a production assignment. |
| Interruption during live compute | Worker restart and helper disconnect recovery passed rehearsals; interruption of a paid benchmark in flight has not been exercised in production. |
| Funded takeover | Restoring the recovery host's backup and starting the application there has been rehearsed in isolation, not as a funded takeover with members connected. |

## Decisions every operator makes

| Decision | Notes |
|---|---|
| Spending cap | Required before any fee or gas is committed. Choose the rolling window, the per-window ceilings for TIG submission fees and native gas, and the per-attempt ceilings. |
| Operator payouts | Optional. Configure an income wallet and a retained operating reserve to withdraw operator earnings. |
| Offered resources | CPU and/or GPU, after validating execution on the hardware members will use. |
| External alerts | The health check writes a status file; forwarding it to email, chat or a pager is the operator's choice. |
| Evidence copies | Whether the recovery host also keeps a raw copy of the primary's block and report spools, in addition to the database backups and pending-file snapshots. |
| Independent recovery collector | Whether the recovery host observes TIG independently so that a takeover has its own evidence stream. |
