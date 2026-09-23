# Custody transfer observation and reconciliation

`tools/observe_custody_v2.py` continuously captures finalized ERC-20 transfers
for the configured TIG token and custody wallet. It uses read-only JSON-RPC
methods; it cannot sign or send a transaction. Migration 008 adds the immutable
response archive, processed batches, custody checks and alerts, plus a protected
forward-only observation cursor.

The observer verifies chain ID, token decimals, confirmation depth and finality.
It queries incoming and outgoing logs, verifies each event against its receipt
and canonical block, and checks that the complete range explains the token
balance change. Self-transfers are recorded without manufacturing income. It
also captures native balance and the wallet's finalized outgoing nonce.
New payments require an undelegated EOA on the configured network. Version-2
captures also retain EIP-7702 delegation indicators so observation can continue
after an already sent sponsored payment. Unknown delegation keeps custody
unhealthy until verified authorization-payment recovery explains it. Arbitrary
contract code is rejected. Version-1 archives keep their original undelegated
wallet requirement and remain replayable.

Migration 014 records observed code and immutable authorization evidence.
Nonce reconciliation counts verified custody sends, including the separately
proved custody authorization in a relayer transaction. The relayer's sender,
nonce and gas bill are not recorded as custody's. Unexplained transfers,
balances, nonces or delegation still block readiness. See the
[bounded recovery procedure](WITHDRAWALS_V2.md#already-sent-sponsored-payments).

## Starting and recovering the collector

Start at or before the new custody wallet's first funding or transaction. The
preceding block must show zero TIG, zero native balance and zero outgoing nonce,
and the custody ledger must still be empty. Do this before accepting member or
operator funds. An existing wallet requires starting earlier and replaying its
actual history; a balance snapshot cannot silently become operator funding.

```sh
# Use the isolated v2 environment and database, after explicit migration.
export POOL_V2_DATABASE_DSN='<v2 runtime database connection>'
export POOL_V2_CUSTODY_RPC_URL='<verified read-only RPC endpoint>'
python3 tools/observe_custody_v2.py \
  --chain-id 8453 --token '<verified TIG token>' --custody '<new custody wallet>' \
  --confirmations 12 --start-height '<first funding block or earlier>' \
  --spool '<persistent v2 custody archive directory>'
```

The example confirmation depth is an explicit deployment choice, not a default
inferred from TIG's advertised network metadata. The recorded stream freezes
its network and finality policy. The RPC must provide the required historical
logs, receipts and balance state. On restart the collector resumes after the
last completely recorded batch. `--batch-size` bounds a query to 1–1000 blocks;
reduce it for a provider with tighter limits. `--once` returns success only when
collection is current and custody reconciles. `--replay-only` imports pending
local captures without making network calls.

Every complete or failed attempt is fsynced to the local spool before database
recording. The database also preserves the raw RPC responses with a checksum.
An interrupted deposit credit can be replayed without crediting it twice; the
cursor advances only after the whole batch has been processed. Incomplete data
does not mean there were no deposits. Invalid attempts are also recorded; they
do not prevent the collector from obtaining fresh data. An RPC/database failure
can be retried, while a canonical conflict keeps financial operations held.
Keep the entire spool, including recorded
manifests and chunks, in the backup policy. The RPC URL is represented by an
opaque source identifier in captures, rather than persisting provider secrets.

## Funds and work controls

Verified receipts from a registered member's source wallet are credited once.
Unknown sources remain in the unattributed account for operator review. The
operator page can import a specific verified TIG receipt, record a verified
direct or verified internal native funding transfer, and assign an unattributed deposit to a
verified member wallet or operator funds with an ownership-review reason.
Public knowledge of a transaction hash is not ownership evidence.

Once custody collection is initialized, fresh, complete wallet reconciliation
is required before new benchmark reservations, first precommit sends or another
withdrawal send attempt. Catching up through an old range cannot enable work,
even if that historical balance reconciles. Checks expire after 120 seconds,
and a change in recorded custody backing invalidates a prior check. Resuming the
operator pause cannot override this condition. Existing benchmark handover,
results, proof submission, withdrawal reconciliation and receipt recovery stay
available.

The check compares all custody ledger funds with actual TIG and native balances
and accounts for finalized wallet nonces. Recognized withdrawals and
[protocol top-ups](PROTOCOL_FUNDING.md) explain their full transfer and
operator-paid fee through one shared transaction registry. Unexplained outgoing transfers,
unrecorded native funding or unknown costs hold new spending; they never trigger
a member haircut. A change to a previously recorded finalized anchor creates a
persistent conflict requiring explicit investigation and correction.

## Native funding sent through a contract

**Role: pool operator.** Migration 012 records each internal native receipt by
chain, transaction hash and exact call path, separately from the outer
transaction. Configure `custody_trace_rpc_url` in the API service with a trusted
read-only RPC that supports the [Parity-format `trace_transaction` method](https://docs.erigon.tech/interacting-with-erigon/trace).
The custody RPC still verifies the network, receipt, canonical block, finality
and fee evidence. The trace RPC must identify the same chain and block.

On the operator page, choose **Record native funding**, enter the transaction
hash and the verified internal call path, such as `5.0`. Leave the path empty
for a direct transfer. The equivalent operator API request is:

```json
{"tx_hash":"<full transaction hash>","trace_address":[5,0]}
```

Send it to `POST /api/v2/operator/custody/receive-native`. The path comes from
the verified trace; it is not the transaction's log index or an explorer's
flattened row number. No amount or sender can be supplied by the caller.

Only a positive, successful `CALL` into undelegated pool custody is accepted.
Failed calls and descendants of reverted calls, incomplete trees, mismatched
blocks, and `DELEGATECALL`/`CALLCODE` values cannot create funding. Outer
transactions sent by custody are rejected from this incoming-funding path.
Raw receipt and trace evidence is retained in immutable records. Repeated or
concurrent imports credit the operator's native-fee account once; the external
sender's gas is not charged to the pool. Member TIG and collateral are unchanged.
The observer must subsequently reconcile the wallet before new spending resumes.

The optional trace endpoint has the same trust requirement as a configured
custody RPC. Missing or unsupported tracing keeps the receipt unresolved. No
balance snapshot, transaction input or explorer display substitutes for verified
transfer evidence. Automatic native-transfer discovery remains a separate task.

## Evidence and remaining adapters

A read-only public Base capture from block **51,572,936** includes a real TIG
transfer of **1.151417509939734543 TIG** and 22 RPC responses. It is preserved in
[`custody-block-51572936.json.gz`](../tests/v2/fixtures/custody-block-51572936.json.gz).
Offline verification reproduces the receipt and token-balance change; altering
the closing balance is rejected. This sample was not imported into any member
ledger. No live payment or benchmark was made.

The JSON-RPC interfaces are specified in the primary
[execution API reference](https://ethereum.github.io/execution-apis/api/methods/eth_getLogs/)
and [Ethereum JSON-RPC documentation](https://ethereum.org/developers/docs/apis/json-rpc/).
PostgreSQL tests cover missing logs, RPC failure, replay, a crash after credit,
concurrent collectors, backfill, stale evidence, canonical conflicts, exact
attribution, and source/network mismatches. Browser tests exercise operator
deposit review and native funding against the real API with generated fixtures.

Unconfirmed-deposit display, other operator
expense reconciliation, automatic native/internal-transfer indexing, and the
audited correction workflow remain separate adapters. A pending native funding
or protocol top-up cannot be assumed to explain a discrepancy. Deployment must
initialize and verify the custody monitor before enabling its live work flags;
the low-level API factory remains usable without a monitor in isolated tests.
This increment is not a production deployment or a complete live finalization
adapter.
