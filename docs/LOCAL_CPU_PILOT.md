# Local CPU validation scope

Daniel selected CPU-only testing on the existing workspace host on 21 September
2026. Both member identities use separate worker installations on that machine,
one at a time. GPU execution and validation with separate machines are deferred
to the final test.

The worker now supplies a local pilot launcher and systemd slice. Follow the
[recorded worker procedure](https://github.com/Daniel-T-S-Adams/innopool-slave-v2/blob/ded5f4b4fe07de0cfeb1845cb50f9ef97ae65c36/docs/LOCAL_CPU_PILOT.md).
CI pins that same worker revision for the paired API tests; this is an
integration pair, not a published production release.

All new pilot services must share `innopoolv2pilot.slice`: one CPU's worth of
processing, 2 GiB total memory, no swap and at most 256 processes/threads. Native
systemd services use `Slice=innopoolv2pilot.slice`; Docker services additionally
need `--cgroup-parent=innopoolv2pilot.slice`. Benchmark containers have their own
1280-MiB limit, leaving the balance for the worker and pool services. Reusing an
unrestricted development database would not meet this aggregate budget; the
pilot needs a separate, bounded database.

Worker startup checks host headroom, rejects GPU or parallel worker configuration,
and requires explicit limits. The fixed service name prevents simultaneous member
workers. Container recovery preserves both resource limits and saved evidence.
Drained updates retain the configuration. An offline Docker test demonstrated
CPU throttling and a container-local out-of-memory termination under these controls.
It did not submit TIG work or exercise a member's collateral.

Before funded execution, finish the isolated service deployment and monitoring,
and install the financial ceilings and benchmark-count cap below. Daniel expects
the selected resources to fit a real benchmark; the first bounded live attempt
will confirm its memory use and completion time. Separate unpaid sizing is
optional for this pilot. A single
worker can otherwise continue requesting benchmarks. Hitting a resource limit
can fail an acknowledged assignment and forfeit its collateral under the agreed
rules; do not silently raise the host resource ceiling to avoid that failure.

## Initial testnet budget and attempt limits

The pilot uses TIG testnet and Base Sepolia, with **5 TIG maximum total** for
member funding/collateral and submission charges. The account's free 10 TIG fee
credit is protocol-only funding, not permission to spend 10 TIG. Use 1 TIG per
member and a recorded member multiplier of `0.02`: with the observed 2- or
5-bundle proposals, a benchmark holds 0.4 or 1 TIG. New work still requires the
actual member balance and positive collateral; parameter changes that exceed
the allowance stop the pilot. Set the multiplier only after each member has
registered through wallet authentication. Existing holds remain unchanged.

Migration 011 installs an optional immutable pilot policy. The setup command
requires a fresh work/top-up history and the matching custody identity:

```sh
python3 tools/configure_pilot_v2.py --config '<pilot-limits.json>' --actor '<operator>'
```

The JSON contains `version: 1`, the explicit `api_origin`, `chain_id`, `token`,
`pool_wallet`, `maximum_total_tig_units: "5000000000000000000"`,
`max_fee_per_attempt_units: "10000000000000000"`, and an ordered `members` list
of two objects containing `wallet` and `funding_units: "1000000000000000000"`.
There are two precommit attempts total, one per member, CPU only, in that order.
The second cannot start until the first becomes active. A potentially sent or
rejected attempt counts permanently; failure or uncertainty stops later work.
Proven-unsent cancellations can be replaced. Recovery, results and proofs remain
available after reaching the cap. Restarting, repeating setup or toggling the
operator pause cannot remove the limit. Changing this pilot requires an explicit
review of a new validation scope; its immutable record has no reset API.

The fee ceiling is 0.01 TIG per attempt, so the configured authorization is at
most **2.02 TIG**, below the overall 5 TIG cap. Current expected fees are lower,
but must be checked when selecting work. Charges paid from starter credit count
the same way. Custody receipts attributed to member or operator balances above
the 2 TIG funding allocation pause new work, and new token top-ups are disabled.
An unmatched receipt remains in the separate `unattributed:TIG` account: it
reconciles with the wallet but cannot fund collateral, withdrawals or submission
fees. Such a receipt is outside the pilot allocation until an operator reviews
and attributes it. If attributed later, its full original amount counts, and
the next reservation/first send rechecks the cap. Returns and restarts do not
reset the receipt total or either attempt. The pilot status endpoint reports
both attributed receipts and the remaining unattributed balance. The pool cannot
prevent an external wallet from making an unsolicited transfer; follow the exact
funding instructions and reconcile any unexpected receipt. Native gas uses separately
funded Base Sepolia test ETH only. No mainnet spending is authorized.

Set `require_pilot_limits: true` in both API and coordinator service configs.
The coordinator also checks its actual transport origin before every dispatch;
a pilot database cannot submit to mainnet. Fresh reconciled custody and fee
observers are mandatory for both reservations and first sends. Operators can
inspect the durable limit and consumed attempts at `GET /api/v2/operator/pilot`.

The ASGI factory is `pool_manager.pool_v2.service:application` with `--factory`.
It reads the reviewable JSON path from `POOL_V2_SERVICE_CONFIG`, and database and
operator secrets from protected `POOL_V2_DATABASE_DSN` and
`POOL_V2_OPERATOR_TOKEN` environment configuration. Funds, work and settlement
default to disabled. Use loopback HTTPS plus an SSH tunnel for initial browser
access, keeping API and database listeners private. The selected API origin must
match the browser's origin and worker settings.

Capture protocol, custody, fee and arbitration data from before funding/work.
Local archive replay is in scope; copies on this same host cannot demonstrate
recovery from losing the host. Independent recovery coverage remains a final
validation requirement. Missing observations still hold the affected settlement.
CPU-only activation also does not prove GPU execution or eligibility for a
positive protocol reward; reward-payment validation requires an actual receipt.

The working checklist and host evidence remain in the shared workspace at
`LIVE_VALIDATION_CHECKLIST.md` and `validation/2026-09-21-local-cpu-pilot/`.
