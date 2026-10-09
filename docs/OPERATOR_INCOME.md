# Operator income withdrawals

The pool custody wallet remains the TIG benchmarker identity and holds the
recorded member and operator funds. A separate operator income wallet receives
reviewed payouts from the operator's available custody TIG. Your personal
member wallet continues to use the ordinary member balance and withdrawal flow.

The operator balance includes settled reward allocations **and operator capital
contributions**. It is not a profit calculation. Member deposits, collateral,
pending member withdrawals, unsettled round pots, prepaid protocol fees and
money already committed to top-ups are not available for operator withdrawal.

## Configuration

Migration `015_operator_withdrawals.sql` extends the existing withdrawal records
with an immutable owner kind and operating reserve. Existing member records
retain their member identity, amounts, destinations, outcomes and cooldowns.
Deploy the migrated API and its matching website assets together. Older API
versions do not support the new operator records.

Configure both fields in the protected service JSON:

- `operator_income_wallet`: the operator's chosen income address, different from
  the pool custody address. API callers cannot supply or override a recipient.
- `operator_tig_reserve_units`: the minimum available operator TIG to retain for
  running costs, as an integer string in 18-decimal units. For example,
  `"10000000000000000000"` means 10 TIG. This is an example, not a required budget.
  Zero is allowed only when explicitly configured as `"0"`.

Both API settings default to `null`, disabling new operator payouts. Existing
custody configuration and `funds_enabled: true` are also required.

The operator selected income wallet
`0x03f540Af6aAB40Bbf86D94584D4712541FbD6c4a` on 28 September 2026. Its address
format and checksum were checked locally; this does not verify ownership or
on-chain balances. The mainnet deployment template records this address and
leaves financial operations disabled. The retained TIG budget is still awaiting
the operator's choice. Complete `operator_tig_reserve_units` before loading the
template: the API requires the income address and reserve to be configured
together. Setting these fields does not send tokens or enable reward settlement.

Changing the configured address never changes an existing request. An unsent
request for the old address must be rejected and recreated before a new payment
attempt can begin. Already prepared attempts retain their original instructions
and can still be reconciled. Raising the reserve applies to new attempts too;
lowering it cannot weaken a pending request's original reserve.

## Dashboard flow

1. Open **Operator income** on the operator dashboard. Check the full destination,
   withdrawable amount, retained operating budget and pending payout amount.
2. Choose **Withdraw operator funds**, enter the amount and a reason. The pool
   reserves only operator funds. One pending operator withdrawal is allowed.
3. Find **Operator income** in **Withdrawal review** and approve the destination
   and amount. Rejecting an unsent request returns its funds to the operator.
4. Choose **Prepare payment** and enter the native transaction-fee budget. The
   server checks custody balances, observation status and the retained reserve,
   then records the nonce and reserves operator gas before showing instructions.
5. Send the exact transfer manually from the pool wallet using those instructions.
   The browser and server do not hold a wallet key or sign/broadcast payments.
6. Choose **Check transfer**. Supply the hash, or leave it empty to recover the
   finalized transaction from its recorded nonce. An exact confirmed transfer
   records the payout once and charges gas only to operator native funds.

Closing the dialog does not cancel a prepared payment. An uncertain attempt
keeps its funds reserved until finalized evidence proves payment, failure or
nonce cancellation. After a proven failure, review the request for another
attempt or reject it. A failed or cancelled transaction still incurs its actual
gas cost. Payment recovery remains available when new payouts are disabled.

Operator payouts share transaction/nonce and receipt uniqueness with member
withdrawals and fee top-ups. They do not start or alter a member's seven-day
withdrawal cooldown. The existing verification path also handles previously
sent supported sponsored withdrawals; creating new sponsored payments is not
part of this flow.

The retained amount limits payouts; it is still available for legitimate
operator expenses. If expenses consume it after a withdrawal request, the
reserve must be restored before preparing the payout. A reserve is not an
estimate of future expenses.

## API and verification

- `GET /api/v2/operator/income` returns the configured address, reserve,
  available operator balance, pending payouts, withdrawable amount and enabled
  state. Amounts are decimal integer strings.
- `POST /api/v2/operator/income/withdrawals` accepts `amount`, `request_key` and
  `reason`. All calls require the operator credential. Retrying identical inputs
  returns the original request; a changed request with the same key conflicts.
- Review, rejection, preparation, instruction recovery and reconciliation use
  the existing `/api/v2/operator/withdrawals` and `withdrawal-attempts` routes.
  Results identify `kind: "operator"`; member lists exclude these records.

Database and API tests cover reserve enforcement, concurrent requests/top-ups,
member isolation, immutable recipients, shared nonces, failed transactions,
duplicate confirmations and recovery after configuration changes. The HTTPS
browser test completes member payment, fee funding and operator payment in one
flow using simulated chain evidence and exact integer amounts. These tests send
no real funds and do not establish a completed mainnet reward cycle.

## Current deployment — 9 October 2026

The payout implementation is in the current cleanup release `8477509`, schema
through 019. Member funds are enabled, new work paused and settlement disabled.
The operator reserve is still unset, so operator payout configuration remains
disabled. Custody RPC access currently prevents a fresh wallet check. See
[operations status](OPERATIONS_STATUS.md).

## Historical deployment verification — 4 October 2026

Release `994739d` and migration 015 are installed on the primary and German
recovery servers. The matching website assets are deployed; previous asset
hashes remain available. A restored funded testnet pilot preserved both member
balances, collateral holds, withdrawal records and journals through the upgrade
and a second migration run. All 270 current pool tests passed, including the
browser flow; public authenticated operator reads passed after deployment.

Financial operations remain disabled. The operator income wallet/reserve pair
is unset in the active service configuration until the operator chooses the
reserve. The chosen destination remains recorded above. No mainnet payment was
sent, and the pool has not completed a live mainnet reward cycle.
