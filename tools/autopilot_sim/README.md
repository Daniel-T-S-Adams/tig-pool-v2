# Autopilot Simulator

Offline simulator for InnoPool autopilot decisions.

This tool is intentionally separated from the live pool:

- reads only JSON files from disk
- does not connect to Postgres
- does not call master `/update-config`
- does not call TIG APIs
- does not run Docker
- refuses DB/write operations if a live path is accidentally called

## Export A Live Report

On the VPS:

```bash
cd ~/tig-pool
python3 admin.py autopilot --json > autopilot_report.json
```

Copy `autopilot_report.json` into `tools/autopilot_sim/reports/` locally, then run:

```bash
cd /home/kevin/tig-pool
python3 tools/autopilot_sim/simulator.py --report tools/autopilot_sim/reports/autopilot_report.json
```

## Run A Synthetic Scenario

```bash
cd /home/kevin/tig-pool
python3 tools/autopilot_sim/simulator.py --scenario tools/autopilot_sim/scenarios/one_cpu_warmup.json
python3 tools/autopilot_sim/simulator.py --scenario tools/autopilot_sim/scenarios/unhealthy_funnel.json
```

## Run Every Scenario

```bash
cd /home/kevin/tig-pool
python3 tools/autopilot_sim/simulator.py --all
```

The command exits non-zero if any assertion fails. This is the main safety
check before moving an autopilot rule into the live pool.

## Run Reward What-If Worlds

The reward simulator compares policies across synthetic pool worlds. It does
not try to predict exact TIG payouts. It estimates which policy should perform
better under controlled assumptions about fleet size, runtime, bundle count,
proof conversion, stopped work, and reward value.

```bash
cd /home/kevin/tig-pool
python3 tools/autopilot_sim/reward_simulator.py --all
```

Run one world:

```bash
python3 tools/autopilot_sim/reward_simulator.py --world tools/autopilot_sim/reward_worlds/c3_gpu_failure.json
```

The reward output includes:

- `reward`: net expected reward after stale/stopped/precommit penalties
- `gross`: reward before penalties
- `penalty`: stale, stopped, and precommit-overhang costs
- `completed`: proof-converted benchmark equivalents
- `stale`: benchmark work that did not convert cleanly
- `util`: average use of the policy's configured concurrent benchmark budget
- `final_max`: final simulated `max_concurrent_benchmarks`
- `bundles`: final per-track bundle targets

Use this layer to answer policy questions, for example:

- does a large fleet justify more aggressive scaling?
- when do more bundles stop improving net reward?
- how badly does a failing GPU/C3 fleet poison reward?
- should a low-compute pool stay conservative?
- which tracks deserve more weight under clean proof conversion?

## Convert Live Reports To Reward Worlds

Export on the VPS:

```bash
cd ~/tig-pool
mkdir -p autopilot_exports
python3 admin.py autopilot --json > autopilot_exports/autopilot_$(date -u +%Y%m%d_%H%M%S).json
ls -lh autopilot_exports
```

Copy reports to your local machine:

```bash
mkdir -p ~/tig-pool/tools/autopilot_sim/reports
scp kevin@YOUR_VPS_IP:~/tig-pool/autopilot_exports/*.json ~/tig-pool/tools/autopilot_sim/reports/
```

Generate a scaled reward world from one report:

```bash
cd ~/tig-pool
python3 tools/autopilot_sim/report_to_world.py \
  --report tools/autopilot_sim/reports/autopilot_20260626_223000.json \
  --out tools/autopilot_sim/reward_worlds/from_live_cpu10_gpu3.json \
  --name from_live_cpu10_gpu3 \
  --cpu-scale 10 \
  --gpu-scale 3
```

Then run it:

```bash
python3 tools/autopilot_sim/reward_simulator.py \
  --world tools/autopilot_sim/reward_worlds/from_live_cpu10_gpu3.json
```

This lets a small live pool calibrate synthetic larger worlds. The live report
provides observed timings, proof conversion, stopped rate, configured bundles,
batch sizes, track weights, and active fleet shape; the converter scales the
fleet without touching the live pool.

## Output

The simulator prints:

- health summary
- reward-funnel safety
- decision reason
- proposed changes
- guardrails
- scenario assertions
- full decision JSON when `--json` is used

It does not apply anything.
