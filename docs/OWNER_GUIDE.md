# InnoPool v2 owner's guide

**Most routine owner actions belong on the pool website. MetaMask signs money
movements. Hetzner hosts the services and their configuration. Software changes
are reviewed and tested before a fixed release is deployed.**

Revised **9 October 2026**, checked against release
`mainnet-operational-cleanup-20261009` (`c954172`, schema through 019).
Member funds are enabled; new work is paused and settlement is disabled.
The API serves pinned worker `fd29279`. Recovery preserves base backups, WAL
and encrypted settings; its API and collectors are inactive and the full raw
file-copy job is disabled. A usable Base RPC, unresolved round-137 block gaps
and storage protection remain cleanup items. See [operations status](OPERATIONS_STATUS.md)
for the current findings, what the evidence means and the storage discussion.

This guide describes implemented controls. A control still depends on its
deployed capability, custody checks and complete evidence; its presence on the
website does not establish that a full mainnet reward cycle has passed.

The website address is `https://pool.tig.foundation`. Open `/operator` for owner controls, `/` for a member account, and
`/join` for worker installation instructions. The separate
`pool-api.tig.foundation` hostname serves browser and worker requests.
Always identify which deployment you are using before a financial action.

Use this table to decide where a task belongs:

| Owner task | Where you do it | What you use |
|---|---|---|
| Check balances, deposits, work and alerts | Pool website | Operator dashboard → Refresh |
| Change a member's collateral requirement | Pool website | Members & collateral → Edit multiplier |
| Pause or resume new pool work | Pool website | Pause new work / Resume new work |
| Register a member | Member's browser and MetaMask | Member account → Connect wallet → sign in |
| Connect, drain or update a worker | Member website, then that worker's machine | Execution token and verified installer |
| Review a withdrawal | Pool website | Withdrawal review → Approve or Reject |
| Pay a withdrawal | Pool website → MetaMask → pool website | Prepare payment → manual transfer → Check transfer |
| Add operator running funds | Funding wallet in MetaMask, then pool website | Wallet reconciliation and receipt/ownership checks |
| Refill TIG submission credit | Pool website → MetaMask → pool website | Prepare fee top-up → manual transfer → verify transaction and TIG credit |
| Withdraw your available operator funds | Pool website → MetaMask → pool website | Operator income → Withdraw operator funds, then the normal payment procedure |
| Change your income destination or retained operating budget | Hetzner service configuration | Update the configured values and reload the API |
| Finalize eligible collateral or credit a completed round | Pool website, after backend evidence is ready | Finalize; Preview → Credit this round |
| Claim/unlock TIG protocol rewards | Wallet actions plus backend integration | The complete pool workflow remains unfinished |
| Rotate the operator token, TIG API key or database credentials | Protected server configuration | Coordinate service updates; keep secrets outside Git |
| Enable mainnet funds, submissions or settlement | Server configuration and launch procedure | Explicit launch settings, evidence and budget |
| Change the 5% fee, reward rules, work selection or two-slot limit | Development code, tests, GitHub and deployment | A reviewed software release |
| Deploy an existing tested software update | Hetzner administration | Install the recorded release and required database migrations |
| Maintain backups, inspect services or recover a server | Hetzner administration | Service checks, backups and rehearsed restore/takeover |
| Change DNS, certificates or hosting resources | Cloudflare/Hetzner web consoles and server administration | Infrastructure settings; usually no application code change |

**To open the owner controls, use the operator access token.**

1. Open the pool website and select **Operator**.
2. Enter the deployment's **Operator token**, then **Open operator dashboard**.
3. Use **Refresh** to reload balances and status; the current screen does not
   continuously refresh itself.
4. Use **Sign out** when finished. The page keeps the token in memory; leaving
   or reloading the page clears it.

The operator token is issued during server setup. It is separate from the TIG
API key, worker execution tokens and wallet keys. It lets you administer the
pool's records; the pool custody wallet must separately sign outgoing transfers.
If you are also a member, use a separate member tab and the member wallet.

**For the daily check, start with the operator dashboard.** Review the latest
observed block and listed gaps, **Observation alerts**, **Protocol
confirmations**, **Wallet reconciliation**, and **Submission fee funding**.
Both funding panels should reconcile before new financial work proceeds.
Check available **Operator funds**, **Network fee funds**, and the prepaid
**Submission balance**, then inspect pending withdrawals and held collateral.

The overall custody balance includes money belonging to members. Use the
operator balance and **Operator income** allowance for your own spending.
Server disk space, backup freshness and failed services require the server's
monitoring/admin tools; the pool page is not a complete server-health console.

**To change a member's collateral multiplier, use the website only.**

1. In **Members & collateral**, find the intended wallet and click
   **Edit multiplier**.
2. Enter a decimal from `0` to `1`, explain the change in **Reason**, and choose
   **Save multiplier**.
3. Check the updated row and **Multiplier history**.

`1` means the full base collateral, `0.5` means half, and `0` means no collateral
for new reservations. For a 50 TIG base requirement, `0.02` holds 1 TIG. The
setting applies across that member's CPU/GPU workers. Already-reserved
benchmarks retain their original multiplier and held amount. Changing it needs
no wallet transaction. Zero collateral does not suspend the member or remove
their work limits.

**To pause the pool's new work, click Pause new work.** Existing handovers,
computation results, proofs and recovery remain available; an already committed
submission can still finish. Payments and observation are separate controls.
Use **Resume new work** once the cause is resolved. This button cannot override
disabled server capabilities or unresolved funding/evidence checks. The current
owner screen has no separate per-member suspend/ban control.

**To onboard a member, the member signs in and installs their own worker.**

1. The member opens **Member account**, selects **Connect wallet**, and signs
   the pool login message in MetaMask. This verifies and registers the account;
   it is a message signature, not a payment.
2. If needed, set their multiplier before their first work reservation.
3. Once deposits are enabled, the member sends TIG from their verified member
   wallet to the address and network shown under **Member funds**. The pool
   credits a verified, finalized direct-source deposit to that member.
4. The member selects **Create execution token** and opens **Connect a worker**.
   They select their hardware/capacity, choose **Show installation steps**, and
   download the verified installer.
5. They run the displayed commands on their worker machine, supply the execution
   token privately, and start that installation. This is worker administration;
   it does not require editing the pool server's code.

Installer availability depends on a configured, tested pool/worker release.
Members can **Revoke** execution tokens on their member page and create
replacements, which must also be installed in their workers. Plan ordinary token
changes around active work because revocation removes that worker's API access.
Worker updates require draining and preserving saved evidence; use the
[paired release instructions](PAIRED_RELEASES.md).

**To handle incoming funds, distinguish member deposits from operator capital.**

Verified direct transfers from registered member wallets are automatically
credited to those members. Other TIG receipts appear under **Wallet
reconciliation** for review. Establish the actual owner, choose **Credit
member** or **Operator funding**, record the ownership evidence, and select
**Credit funds**. A transaction hash by itself does not establish ownership.

For additional operator capital, agree the funding source before sending:
a transfer from a registered member wallet will normally become that member's
balance, even if you also own the wallet. The funding wallet sends the agreed
amount to the configured custody address using MetaMask. After finality, verify
the appropriate operator credit and reconciliation on the website.

**Record TIG receipt** verifies a particular incoming transaction and transfer
event index if it needs explicit processing. **Record native funding** verifies
incoming ETH used for operator network fees. Its call-path field is for a
verified transfer through a contract; a direct ETH transfer leaves it empty.
Have the maintainer identify an event index or contract call path if needed.
These buttons verify real receipts; they do not create money or send a transfer.
See [custody observation](CUSTODY_OBSERVER.md).

**To pay a member withdrawal, use website → MetaMask → website.**

1. The member first requests an amount from their available funds on the member
   page. Existing collateral is not available to withdraw.
2. In **Withdrawal review**, check the member, destination and full amount.
   Select **Approve**, provide review notes and confirm. Use **Reject** with
   a reason if an unsent request should be declined.
3. Select **Prepare payment** and enter the operator's network-fee allowance
   in native currency (ETH on Base). This reserves a payment attempt, its
   transaction number (nonce), and fee budget. It does not send money.
4. Keep the displayed instructions open. In MetaMask select the displayed
   **From** account, which must be the pool custody wallet, and the displayed
   network. Choose **Send**, select the exact TIG token, and enter the displayed
   recipient and amount.
5. Review the token, recipient, amount, network, fee and nonce against the
   prepared instructions before confirming. The current pool supports a direct
   token transfer with fees paid in native ETH. A swap, sponsored payment or
   smart-account upgrade is not an interchangeable payment route.
6. Send once. Copy the transaction hash from MetaMask's transaction details.
7. Return to **Check transfer** for that same attempt, enter the hash and submit.
   If finality is pending, wait and check that same transfer again. Leave the
   event-index field empty unless the verifier needs a specific event.
8. Confirm that the pool records the withdrawal as paid. Verification records
   the operator's actual fee and starts the member's seven-day withdrawal interval.

MetaMask's [send instructions](https://support.metamask.io/manage-crypto/move-crypto/send/how-to-send-tokens-from-your-metamask-wallet)
explain its account, network and transaction-review screens. Its
[nonce guide](https://support.metamask.io/configure/transactions/how-to-customize-a-transaction-nonce)
describes **Show advanced details** and **Custom nonce** in Extension. If your
wallet cannot produce the required direct payment or its nonce differs, resolve
that with the maintainer before sending. Changing an on-chain smart-account
delegation is itself a separate transaction requiring accounting.

An `uncertain` request means the pool has reserved a potentially sent attempt;
it does not mean that payment failed. Closing the dialog does not cancel it.
Reopen **Check transfer** and reconcile the original attempt. Avoid unrelated
outgoing custody transactions while an attempt is pending because withdrawals
and fee top-ups share the same nonce sequence. The recorded sponsored-testnet
recovery supports one existing payment pattern; it is not general support for
new sponsored payments. See [withdrawal behavior](WITHDRAWALS_V2.md).

**To refill submission credit, prepare a fee top-up on the pool website.**

1. Check that **Submission fee funding** is reconciled and operator TIG/ETH
   funds are available.
2. Choose **Prepare fee top-up**, enter the TIG amount and network-fee allowance,
   then **Prepare top-up**. The form reads the current minimum from TIG.
3. Use the recorded sender, recipient, amount and nonce to make the direct TIG
   transfer in MetaMask, following the prepared-payment procedure above.
4. Use **Check transfer**, then wait for TIG to acknowledge the top-up. If the
   row says it is awaiting protocol confirmation, use **Check TIG credit**.
5. Confirm that the pool's prepaid **Submission balance** is credited and
   reconciled before expecting new work to use it.

This spends operator funds. A protocol top-up burns TIG for fee credit; it is
different from a member collateral deposit or simply adding TIG to the pool
wallet. Preserve the prepared pool record even when using external wallet
software. [TIG's explanation](https://docs.tig.foundation/deposits/make-topups)
and [the pool's funding workflow](PROTOCOL_FUNDING.md) describe the distinction.

**To withdraw your own operator funds, use Operator income.**

1. Check the configured income address, withdrawable TIG and retained operating
   budget shown in **Operator income**.
2. Choose **Withdraw operator funds**, enter the amount and reason, then
   **Request withdrawal**.
3. Find the resulting **Operator income** row in **Withdrawal review**, approve
   it, and select **Prepare payment**.
4. Send from the pool custody wallet to the displayed income address in
   MetaMask, then return to **Check transfer**.

Your available operator funds include settled pool fees and your capital
contributions; the number is not a profit calculation. The retained amount
limits withdrawals while leaving money for operations. Member funds and
prepaid protocol credit are excluded. The income destination and retained
budget are server settings, not editable fields on this screen. See
[operator income configuration and behavior](OPERATOR_INCOME.md).

**To finalize collateral and allocate rewards, use the recorded evidence.**

Once settlement is enabled and the required final protocol outcomes are
available, **Collateral awaiting finalization → Finalize** resolves an
eligible benchmark's hold. Its creation-round arbitration period must be over;
successful computation alone does not release collateral.

For a complete, funded reward round, use **Round settlement → Preview**, check
the member allocations, then **Credit this round**. These actions update the
pool ledger. They do not distribute individual on-chain payments; members
subsequently use the withdrawal process.

The backend must first establish complete credit, final outcomes and actual
reward receipts. The current implementation still needs the complete operator
reward claim/unlock/receipt and round-attribution connection. Treat that as
remaining engineering and wallet-validation work, rather than a ready-to-use
sequence of owner clicks. See [settlement](ROUND_SETTLEMENT.md) and
[reward receipts](REWARD_RECEIPTS.md).

**Server settings can change without editing application code.** The mainnet
deployment keeps service settings under `/etc/innopool-v2-mainnet/`, with secrets
in protected service configuration. The installed application is a fixed
release under `/opt/innopool-v2-mainnet/releases/`. For example, changing the
income wallet or operating reserve consists of choosing the new values,
updating `service.json`, validating them, restarting the API and checking the
website. The source code does not change. The retained budget is stored in
token base units, so have the maintainer convert your human TIG amount exactly.

Other server work includes credential rotation, API/coordinator capability
settings, collector/RPC configuration, TLS installation, service restarts,
resource limits, backup scheduling and restores. Funds/work/settlement flags
and the coordinator's submission settings must agree with the launch plan;
the website's **Resume new work** button is not the mainnet enable switch.

The owner provides decisions such as the operating reserve, destination wallet
and launch budget. The server maintainer applies and verifies the configuration.
For a changed income address, existing withdrawal requests keep their original
destination; the operator handles any affected unsent requests explicitly.

**Application behavior changes follow the development and release workflow.**
The current fee rule is 5%, with a two-slot member limit and automatic work
selection. Changing these rules, adding a per-member suspension control or
changing the website's behavior requires source changes and relevant testing.
Work is prepared in the development checkout, reviewed through GitHub, then a
tested pool/worker release is installed on Hetzner with any required migrations.
Editing GitHub alone does not update the running site. Routine restarts use
the installed release.

The pool's v2 code, including operator income, was promoted to `main` in
[PR 22](https://github.com/Daniel-T-S-Adams/tig-pool-v2/pull/22) on 29 September
and deployed with migration 015 on 4 October. Confirm the configured operating
reserve in `service.json` before relying on operator withdrawals. Since
[PR 28](https://github.com/Daniel-T-S-Adams/tig-pool-v2/pull/28), deployed on
8 October, new benchmarks copy the hyperparameters of the selected algorithm's
best active bundle per track. Source
availability is separate from production readiness. The inherited `admin.py`,
legacy `.env` settings and original Compose instructions do not administer these
v2 ledger controls.

**If something goes wrong, use the existing record before retrying an action.**

| What you see | First owner action | When server work is needed |
|---|---|---|
| Withdrawal/top-up is pending or uncertain | Check the original MetaMask transaction and use the same attempt's Check transfer | Conflicting nonce, unsupported payment route, missing finality or receipt verification failure |
| Missing member deposit | Refresh after finality; review unattributed receipts and verify ownership | Missing chain observations, incorrect network/token, or a deposit already credited to a different account |
| Workers receive no new work | Check pause status, member funds/slots and fee reconciliation | Collector, coordinator, credentials, release or runtime failures |
| Observation gaps or custody mismatch | Pause new work and retain the recorded evidence | Investigate/recover observations and reconcile balances before resuming |
| Website unavailable | Check the configured hostname and whether the API is reachable | Inspect nginx, TLS, API services, Cloudflare routing and host health |
| Disk/backup/server failure | Ask the maintainer to follow the documented recovery procedure | Restore and compare the ledger; stop the old primary before a takeover |

The separate German host provides recovery data and independent observation;
takeover is manual. Restoring an old database over the active pool is not a
routine software rollback because it could erase newer financial records.
Use the [deployment/recovery procedure](MAINNET_DEPLOYMENT.md) and
[hosting diagram](POOL_STRUCTURE.md) for these server tasks.
