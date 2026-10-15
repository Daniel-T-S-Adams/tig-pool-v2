# Wallet RPC recovery — 12 October 2026

The primary's API and wallet observer previously used `https://mainnet.base.org`.
That public endpoint was selected by the installation template; no separate RPC
provider account was configured. Historical reads were already failing in the
9 October operations record. The unpaid CPU/master/helper rehearsal did not
exercise this wallet-history gate.

## Connection repair

At 12:47 UTC the API and wallet observer switched to
`https://base.api.pocket.network`. Pocket publishes this as a free public Base
endpoint. [Provider documentation](https://docs.pocket.network/developers/supported-chains/).

Before applying the change, the actual deployed read-only adapter and custody
verifier checked chain/token identity, a known credited deposit and receipt,
complete 1,000-block ranges, historical opening/closing balances, nonce/code and
canonical anchors. Repeated probes passed. Latest/finalized block headers were
also compared with Base's own endpoint. Some supplier responses had older
finalized targets; the existing verifier bounds every captured range by its
own recorded target and preserves the stream's canonical anchor.

Catch-up used 1,000-block batches, 0.25-second request spacing and
one-second polls; steady polling is now 30 seconds. This pacing passed a read-only probe before application.
Both affected services restarted successfully. Pacing is stored in the existing
systemd unit file that the encrypted configuration export already includes.

Previous settings and the original unit remain in the protected primary directory
`/etc/innopool-v2-mainnet/rollback/rpc-pocket-20261012`. Application `8477509`,
worker `fd29279`, migration history and monetary flags are unchanged. The
operator pause remains recorded. No new provider account, deposit, transfer,
collateral release or paid TIG benchmark was created.

## Completed verification

At **13:11 UTC**, the observer was reconciled and ready at Base **52510065**,
recovering **169,056 blocks** from its original cursor at 52341009. Readiness
passed repeated checks, including the steady polling profile. The recovered
canonical anchor was independently checked against Base's own endpoint.

Every sampled ledger audit passed. Account/journal fingerprints remained unchanged
with 12 journals; the member retained **0.9995 TIG available** and the original
collateral hold. No manual financial adjustment or freshness override was used.

After catch-up, polling was reduced to **30 seconds**, retaining 1,000-block
batches and 0.25-second request spacing. Some public supplier responses reported
older finalized targets or rate limits; these failed captures never advanced the
cursor and were rejected under the existing custody rules. Availability remains
subject to the same gates. New paid work is still paused for the remaining checks.

## Configuration protection

The encrypted export `configuration-daily-2026-10-12T130941Z` was copied to Germany.
All manifest hashes passed. Decryption in a temporary private directory verified
the API/observer endpoint, exact pacing unit and protected rollback files.
Temporary plaintext was removed; no secrets or standby services were activated.
This supplements configuration protection and does not itself advance a database
backup or demonstrate funded takeover.

Development evidence is retained under
`/root/mine-rootz/validation/member-mainnet-20261012/`, including candidate probes,
`rpc-production-change.json`, `rpc-unit-persistence.json`, progress snapshots,
observer logs and encrypted-backup verification. Existing custody freshness and
reconciliation gates remain applicable if this public endpoint becomes unavailable.
The paired storage/worker release and capped paid-test prerequisites are separate.
