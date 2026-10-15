# Custody RPC routing — 13 October 2026

The catch-up repair on 12 October remains valid: no cursor, balances or journals
were reset. Subsequent read-only captures identified inconsistent Pocket
`finalized` replies. At 14:34 UTC its backends returned heights **52,543,862** and
**52,554,988**, below the stored cursor **52,555,758**. Base's own endpoint
returned that exact cursor as finalized, with the matching canonical hash.
The existing behind-cursor guard correctly held these inconsistent captures.

Paced direct Base reads passed a complete recent capture, but transfer-log
queries still received HTTP 429. Base's published status records lower public
read limits starting **8 October**, which helps explain the original throttling;
it does not establish a guaranteed request allowance.
[Base's official incident record](https://status.base.org/).

## Installed routing

At **15:01 UTC**, a private read-only router was activated at
`127.0.0.1:18545` on primary. Basic reads, including `latest`, `finalized`,
balances, receipts and block hashes, use **mainnet.base.org**. Only
`eth_getLogs` uses **base.api.pocket.network**. Pocket's inconsistent finalized
tags therefore cannot advance or regress the custody cursor.

The existing collector still checks configured chain/token, finalized range,
previous/end hashes, receipts, transfers, balances and outgoing nonce. A router
or provider failure holds these checks; it cannot bypass them. It accepts only
listed read methods, rejects broadcasts/signing/admin methods, binds to loopback,
limits body sizes and allows two simultaneous reads. It forwards no incoming
authorization headers. It stores no ledger or benchmark data.

`innopoolv2mainnet-rpc-router.service` starts before API/custody through ordinary
systemd dependencies. It runs as `innopoolmainnet` with a 256-MiB/0.25-CPU ceiling
inside the existing application slice. Custody remains at a 1,000-block maximum
batch, a 60-second poll and two-second call spacing. Other pool controls and the
paired application/schema remain unchanged.

## Verification

- Six router regression tests passed: method separation, refusal of writes,
  envelope validation, bounded responses and no forwarded credentials.
- A complete staged custody capture reached finalized height **52,556,535**,
  exercised the transfer-log route, matched ledger balances and the direct
  Base block hash. An attempted broadcast method was refused locally.
- After activation, the collector reconciled at **52,556,712** at 15:03 UTC;
  ledger audit was empty and financial hash remained `95f77c34…784e80`.
- At 16:11 UTC, reconciliation was current at **52,558,823**, with the same
  empty audit, balances and financial fingerprint. The router's source,
  configuration and unit were staged on Germany without starting it or changing
  its inactive pool configuration; takeover must explicitly select loopback RPC.
- Export the source, configuration, regular units and protected rollback records
  in encrypted settings. Keep the router with compatible application
  rollback/recovery. No public firewall port was opened.
- The 16:21 UTC primary configuration export passed off-host checksum and
  private-decryption checks at 16:24, including router source/configuration/unit
  and the unapproved trial plan. Temporary plaintext was removed.

These endpoints remain public services. Sustained reconciliation must be
measured; successful snapshots do not establish provider availability for wider
pool use. No provider account or payment is currently requested from the owner.

Evidence and installed-source checksums are in
`/root/mine-rootz/validation/lean-rollout-20261013/`.
