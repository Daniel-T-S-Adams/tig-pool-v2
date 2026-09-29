# Extending the CPU testnet pilot

Migration 013 adds an append-only phase record. The initial network, custody
wallet, two member identities, maximum fee per attempt and **5 TIG maximum**
remain immutable. Later phases hold cumulative allowances, including every
earlier potentially sent attempt and every attributed incoming receipt. A
restart, withdrawal, refund or collateral release cannot reset that history.

The initial phase still permits A then B, once each. Later phases assign each
member a cumulative attempt limit, retaining one unresolved pilot submission
at a time. Earlier attempts must have confirmed activation before more work
can start. Failure or ambiguity still stops the pilot for reconciliation.
This is validation policy, not the production scheduler.

## Prepare a withdrawal before arbitration ends

**Role: pool operator. Computer: remote server.** Pause new work and reconcile
in-flight reservations. Apply migration 013 using the separate migration role.
Grant the API runtime SELECT on `pool_v2.pilot_phases`; only the setup operator
needs INSERT. Preserve the existing limits and reservations.

Prepare a JSON file, replacing these example addresses with the original
registered members. Amounts are integer token units:

```json
{
  "version": 2,
  "phase_key": "early-withdrawal-a",
  "attempt_limit": 2,
  "members": [
    {"wallet": "<Member A>", "funding_units": "1100000000000000000", "attempt_limit": 1},
    {"wallet": "<Member B>", "funding_units": "1000000000000000000", "attempt_limit": 1}
  ]
}
```

With the initial 0.01 TIG fee ceiling per attempt, this allocates at most
2.12 TIG, including both earlier attempts. It allows a further 0.1 TIG deposit
for A and **zero additional benchmark attempts**. Neither existing 1 TIG
collateral hold changes.

Run with the protected database credential in `POOL_V2_DATABASE_DSN`, without
putting it in shell history or printing it:

```sh
python tools/extend_pilot_v2.py --config /protected/reviewed-phase.json \
  --expected-phase 0 --actor pool-operator \
  --reason 'Test withdrawal from an additional deposit; preserve both holds'
```

Review the returned phase, cumulative receipts and attempts. This does not
enable work or settlement. Identical retries return the existing record;
concurrent or changed reviews are refused. New phase keys cannot change
identities, reduce counters or exceed the original ceiling.

**Role: Member A. Computer: local wallet browser.** After the operator confirms
the phase is installed, deposit the instructed 0.1 testnet TIG to the same pool
custody account on Base Sepolia. Wait until A's dashboard shows 0.1 TIG
available and the old 1 TIG collateral still held. Request withdrawal of
0.1 TIG. Use wallet login; an execution token cannot request withdrawals.

**Role: pool operator. Computer: local wallet browser.** Follow the existing
[withdrawal review and send procedure](WITHDRAWALS_V2.md). Send the full
prepared amount; the operator pays gas. Reconcile the actual finalized receipt.

**Role: Member A. Computer: local wallet browser.** Confirm receipt and the
seven-day restriction. Record later live eligibility separately. This tests
withdrawals from deposits, not receipt of TIG rewards.

For subsequent benchmark phases, increase only reviewed cumulative allowances.
Recalculate funding plus **all** attempt fee ceilings first. Multipliers affect
new reservations only. Never remove the original policy, erase attempts or
reuse the unattributed testnet grant as member funds.
