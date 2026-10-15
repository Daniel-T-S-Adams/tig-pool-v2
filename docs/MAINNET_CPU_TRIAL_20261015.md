# Single mainnet CPU benchmark on the scoped-capture release — 15 October 2026

The owner authorized exactly one paid benchmark for member
`cc713240-95f2-4596-baa7-8f89900e049a` using both of the member's machines,
capped at a 0.001 TIG pool fee and 0.0004 TIG member collateral, to exercise
the release deployed earlier the same day: pool `e3cf0d2`, worker `c6eccfb`,
26 migrations. The approval was consumed by one fenced precommit; the plan is
disabled on both hosts, ordinary work remains paused and settlement disabled.

## Preparation

Selection on the deployed release can assign any of the five CPU challenges at
random. Only knapsack (`c003_a154`, `satchel`) had been timed on the member's
two ARM64 servers, and one of its five tracks, `n_items=5000,budget=10`, now
carries the reference parameter `ils_rounds=226` instead of the default. That
track was timed unpaid on the fleet with the official 0.0.8 runtime: 200 nonces
in **14m40s**, 100 on each machine, three proofs verified. The other four
tracks keep the 13 and 14 October timings with the identical binary. An unpaid
timing of vehicle routing was started and stopped unfinished; satisfiability,
energy arbitrage and job scheduling remain untimed, so the bounds accepted only
knapsack with exactly the track settings quoted from the pool on 15 October.

The drivers are the 14 October scoped pair adapted for this release, under
`validation/cpu-trial-20261015-scoped/`: the pool driver stops the ordinary
coordinator, lifts the durable pause for the one authenticated offer, forces the
timed challenge among tied candidates, checks the proposal against the plan
before any download, fences a single send, and re-pauses on every exit; the
member driver makes one offer and drains that benchmark across master and helper.
The member ran the new worker `c6eccfb`; the helper kept `dddad9d`.

## Sequence

| Event | UTC time |
|---|---|
| Window opened, offer reserved, reference captured on demand | 15:21:03 to 15:21:04 |
| Precommit sent and accepted | 15:21:05 |
| TIG confirmed the precommit | height 1385580 |
| Member handover | 15:21:23 |
| Results accepted | 15:33:49 |
| Proofs accepted | 15:34:09 |
| TIG confirmed benchmark and proof | heights 1385594, 1385595 |
| Pool recorded activation, slot freed | 15:52:38 |

Benchmark `3685bada1781eb7dd0f366daf81a8884`, reservation
`88453646-8951-4591-9ed9-52c81fa50106`, TIG-selected track
`n_items=1000,budget=25`, 4,000 nonces: **1,987** computed on the master and
**2,013** on the helper, results uploaded **12m26s** after handover. TIG's
record shows four active bundles with average qualities 51,404 to 53,360, the
Merkle root matching the member's upload and no fraud. Actual fee **0.001 TIG**,
collateral hold **0.0004 TIG**, held for normal X+2 finalization. Ledger audit
remained empty; the coordinator logged no errors through the window.

## What the new release did

- The coordinator read the whole network once for selection, 23 requests in a
  few seconds, and stored reference capture `f3d5aec2` at height 1385580; the
  reservation cites it. No reference had been read before this offer.
- The scoped per-block capture recorded the pool's own benchmark from the first
  block after activation; its credit rows at heights 1385613 and 1385614 are
  zero, as on 14 October, because the pool's bundles did not reach the network
  cutoff. The whole-network reconciliation continued to pass every block.
- Verified-binary reuse was not exercised: the driver prepared the archive
  itself, so `verified_binaries` stays empty until an ordinary coordinator
  reservation runs. The member worker's library cache held the extracted
  knapsack library after verification.

## Open after this trial

Reward-cycle verification of this and the 14 October benchmark remains issue
#54. Member `b97529ca` holds no balance and cannot post collateral until it
deposits. The second machine's worker is still `dddad9d`; both installations
keep working across pool releases while the runtime map is unchanged.
