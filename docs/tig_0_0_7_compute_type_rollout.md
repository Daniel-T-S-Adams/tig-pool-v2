# TIG 0.0.7 Compute Type Rollout

TIG 0.0.7 requires every `algo_selection` entry to include `compute_type`.
Benchmarkers are responsible for choosing an AWS instance type that reproduces
the same solution qualities as their benchmarking hardware.

## Defaults Used By InnoPool

The config tooling defaults to:

- CPU challenges (`c001`, `c002`, `c003`, `c007`, `c008`): `aws_c7a`
- GPU challenges (`c004`, `c005`, `c006`): `aws_g4dn`

These are defaults only. Change them if reproducibility testing shows a different
AWS instance type better matches the hardware used for a specific algorithm.

Allowed values:

- `aws_t3`
- `aws_t3a`
- `aws_t4g`
- `aws_c7i`
- `aws_c7a`
- `aws_c7g`
- `aws_m7i`
- `aws_m7a`
- `aws_m7g`
- `aws_g4dn`

## Override Options

Set global defaults in `.env`:

```bash
CPU_COMPUTE_TYPE=aws_c7a
GPU_COMPUTE_TYPE=aws_g4dn
```

Override by challenge prefix:

```bash
COMPUTE_TYPE_C001=aws_c7a
COMPUTE_TYPE_C002=aws_c7a
COMPUTE_TYPE_C004=aws_g4dn
```

Override by challenge prefix or exact algorithm ID using JSON:

```bash
COMPUTE_TYPE_OVERRIDES={"c001":"aws_c7a","c004":"aws_g4dn","c005_a025":"aws_g4dn"}
```

## Patch Current Live Config

Dry run:

```bash
cd ~/tig-pool
python3 admin.py compute-types
```

Apply:

```bash
python3 admin.py compute-types --apply
```

Force replacement of existing values:

```bash
python3 admin.py compute-types --force --apply
```

Verify:

```bash
curl -s http://127.0.0.1:3336/get-config > /tmp/innopool-config.json
python3 - <<'PY'
import json
cfg=json.load(open("/tmp/innopool-config.json"))
for sel in cfg.get("algo_selection", []):
    print(sel["algorithm_id"], sel.get("compute_type"))
PY
```

## Update Images

Set:

```bash
TIG_VERSION=0.0.7
```

Then rebuild:

```bash
docker compose pull benchmarker_ui
docker compose up -d --build master benchmarker_ui
docker compose restart nginx
```

## Important Notes

- Do not choose compute types blindly for long-term production. Compare solution
  qualities on your actual hardware versus the selected AWS instance type.
- `neuralnet_optimizer` reports are initially disabled by TIG, but the config
  still needs `compute_type`.
- For GPU algorithms, `aws_g4dn` is currently the only whitelisted GPU compute
  type.
- If an algorithm is nondeterministic across same seed and same hardware, it can
  be penalized under the new protocol.
