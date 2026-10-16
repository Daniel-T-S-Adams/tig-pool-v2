# Stage 0 protocol validation

Live block capture, qualifying-credit inputs, and public report parsing are
implemented and tested. This probe does not submit benchmarks, change a reward
destination, authenticate a member, or make any ledger or token movement.
A narrow explicit late-result rejection rule exists in the coordinator (see
[submission recovery](SUBMISSION_RECOVERY.md)); disappearance alone is
insufficient evidence of expiry. The recorded probes below are integration
inputs, not health checks of a running deployment. [ROADMAP.md](ROADMAP.md)
lists the checks that have not been validated live.

## Run and replay

From this repository, using Python 3.12 and its standard library:

```sh
python3 tools/probe_tig_v2.py \
  --output /tmp/innopool-v2-observations-new \
  --snapshots 2 --max-wait 240 \
  --reports-round 129 --reports-round 130

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/v2 -p 'test_protocol.py' -v
```

Use an empty output directory. The probe queries the latest block, its
algorithms/challenges/OPoW data, and the compact `/get-benchmarks` response for
every active OPoW player. A second block read must match the first. All active
benchmark IDs must have their precommit, bundle scores, and proof metadata.
The collected qualifier totals must agree at algorithm, player, and challenge
levels. Missing records, unknown fields needed for accounting, inconsistent
counts, stale blocks, and a block change prevent that observation from passing.

The compact player endpoint avoids fetching full nonce solutions and Merkle
proofs for every network benchmark. It covers the last 120 blocks;
the probe checks actual coverage against the active IDs on every capture,
instead of assuming that window will always cover every eligible benchmark.

Each compressed JSON archive contains the observed responses, retrieval
provenance, and a validation marker. Files are atomically replaced and synced
to disk. Failed attempts retain available partial evidence and are marked
incomplete. A missed height or inconsistent parent link prevents the run from
claiming consecutive coverage, even when both individual snapshots validate.
Source data is never filled with zeros or copied forward from an older block.

These are finite integration probes, not the production observer. Persistent
coverage cursors, database storage, backup/restore, redundancy, retention, and
efficient storage of recurring benchmark data belong to Stage 2.

## Recorded live evidence

The public responses were captured from
[TIG's mainnet API](https://mainnet-api.tig.foundation/get-block?include_data=true).
The complete compressed recordings and response provenance are in
[`tests/v2/fixtures`](../tests/v2/fixtures).

| Block | Active benchmarks fully covered | Eligible bundles | Reconciled qualifying credit |
|---|---:|---:|---:|
| 1,351,111 | 2,464 | 394,477 | 4,000 |
| 1,351,112 | 2,475 | 401,453 | 4,000 |

The second block references the first as its parent. Both are in round 135.
The validator reconstructs exact fractional bundle credit using the approved
equal-sharing rule within each player/algorithm/track group. Higher scores
receive credit first; bundles tied at the boundary share the remaining places
equally. No attempt is made to recover TIG's randomly selected winning bundle
identities. The active-score array index is only a local credit identifier,
not an original nonce or bundle index for fraud evidence.

This demonstrates complete protocol inputs and arithmetic. Assigning credit
to pool members still requires the new pool's immutable ownership records.
These recordings cannot substitute for observations of future reward rounds.

Round 129 returned 16 reports and 16 confirmed `nonreproducible` arbitrations;
round 130 returned 46 of each. Reports and decisions are joined by report ID.
Pending decisions, final `inconclusive`, final `reproducible`, and upheld
`nonreproducible` remain distinct. Unknown results or missing response fields
are errors. This parser does not itself decide that an arbitration period has
ended or release/forfeit collateral.

## Network metadata discrepancy

The captured TIG block advertises chain ID `0x14a33` (84531), RPC URL
`https://mainnet.base.org`, and token address
`0x0c03ce270b4826ec62e7dd007f0b716068639f7b`.

A separate read-only RPC check returned chain ID `0x2105` (8453), and the
token's `decimals()` returned 18. Its requests and responses are preserved in
[`token-network.json`](../tests/v2/fixtures/token-network.json). Base's
[official chain ID documentation](https://docs.base.org/base-chain/api-reference/ethereum-json-rpc-api/eth_chainId)
also identifies 8453 as Base Mainnet.

The funds adapter must use explicit deployment configuration, verify it
against the RPC and token contract, and surface disagreement with TIG's
advertised metadata. Do not copy that advertised chain ID into wallet-signing
messages, deposit attribution, or withdrawal checks. This probe changes no
deployment network configuration or wallet.

The custody observer also reproduces a complete public token transfer and
its wallet balance change from archived RPC responses. See the
[custody observation evidence](CUSTODY_OBSERVER.md). This validates the read-only
adapter against real Base responses; it does not select or fund a deployment
wallet.

## Remaining Stage 0 work

The probe establishes complete protocol inputs and arithmetic for recorded
blocks; it proves nothing about a particular deployment. A record disappearing
from a current feed is not proof of expiry (the confirmed verification-failure
feed has a recorded public fixture and adapter tests; see
[submission recovery](SUBMISSION_RECOVERY.md)), an elapsed date or an
unsuccessful reports fetch is not a final-outcome signal, and both the
creation and the reporting round identifiers are preserved. Every deployment
must configure and verify its own pool account, custody wallet, token network
and confirmation policy before enabling monetary operations. Precommit
acceptance and rejection against a deployment, definitive expiry, the
creation/reporting-round boundary mapping and the path from final round
emissions to actual pool receipts (claim, protocol penalties and operator-paid
cost reimbursement) are the checks listed in [ROADMAP.md](ROADMAP.md). There
is no new business-policy question in them; they are technical integration
and deployment requirements.
