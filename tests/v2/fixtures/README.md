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

`base-sepolia-internal-native.json.gz` records 13 read-only RPC responses on
23 September 2026. The custody endpoint was `https://sepolia.base.org` and the
trace endpoint was `https://base-sepolia-rpc.publicnode.com`. A finalized
contract call at block 47,196,244 delivered 0.1 test ETH to the pilot custody
wallet at call path `[5,0]`; the outer transaction sent zero ETH to another
contract. Offline replay verifies every recorded request and the resulting
receipt. No credentials are present and the capture itself posts no ledger entry.

`mainnet-reward-distribution.json.gz` records 17 public Base RPC responses and
the public round-132 emissions response on 23 September 2026. Replay verifies
the deployed TokenLocker code, its token and 28-day pending period, our empty
reward balances, and the canonical finalized distribution at block 51,476,049.
All 338 positive player totals match; the extra bootstrap allocation remains
explicit. The contract events contain no round ID and prove no receipt in our
custody wallet. The source URLs and reviewed code checksum are in the fixture.
