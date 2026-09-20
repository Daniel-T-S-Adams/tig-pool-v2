# Recorded public protocol inputs

Captured directly from `https://mainnet-api.tig.foundation` on 20 September
2026 using `tools/probe_tig_v2.py`. No credentials or member data were supplied.
The block archives retain the public API response objects and per-request
provenance, including canonical response digests. `summary.json.gz` records
the consecutive-block validation result and report counts.

`token-network.json` is a separate read-only JSON-RPC observation of the RPC
and token advertised in the first captured block. Its mismatched advertised
and actual chain IDs are intentional evidence, not a fixture transcription
error. See [the validation report](../../../docs/PROTOCOL_VALIDATION.md).

Fixtures exercise the observed deployment's response shapes. They do not
provide historical inputs for a subsequently launched pool, establish
internal member ownership, or authorize payouts.
