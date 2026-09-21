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

Before funded execution, confirm an actual CPU algorithm fits the memory/time
budget without paid submission, finish the isolated service deployment and
monitoring, and record the financial ceilings and benchmark-count cap. A single
worker can otherwise continue requesting benchmarks. Hitting a resource limit
can fail an acknowledged assignment and forfeit its collateral under the agreed
rules; do not silently raise the host resource ceiling to avoid that failure.

Capture protocol, custody, fee and arbitration data from before funding/work.
Local archive replay is in scope; copies on this same host cannot demonstrate
recovery from losing the host. Independent recovery coverage remains a final
validation requirement. Missing observations still hold the affected settlement.
CPU-only activation also does not prove GPU execution or eligibility for a
positive protocol reward; reward-payment validation requires an actual receipt.

The working checklist and host evidence remain in the shared workspace at
`LIVE_VALIDATION_CHECKLIST.md` and `validation/2026-09-21-local-cpu-pilot/`.
