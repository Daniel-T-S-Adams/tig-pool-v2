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

`base-transaction.json` records a public finalized Base transaction at block
51,570,607, captured on 20 September 2026 from `https://mainnet.base.org`.
It includes the raw transaction, receipt and header for exact fee-model tests.
The v2 transaction verifier independently confirmed its canonical inclusion and
finality. No test withdrawal was submitted. See
[withdrawal implementation notes](../../../docs/WITHDRAWALS_V2.md).
# Custody observation

`custody-block-51572936.json.gz` contains 22 read-only public Base RPC responses
captured on 20 September 2026. It verifies a TIG transfer of
1.151417509939734543 TIG to an undelegated public EOA and the corresponding
balance change. It is an external protocol fixture, not a member deposit in
the test ledger. The gzip JSON includes the capture time, explicit network,
request parameters and responses, and a non-secret source label.
