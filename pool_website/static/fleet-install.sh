#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${INNOPOOL_URL:-https://www.innopool.co.uk}"
FLEET_TOKEN="${FLEET_TOKEN:-}"
WORKER_TYPE="${WORKER_TYPE:-cpu}"
MACHINE_INDEX="${MACHINE_INDEX:-}"
FROM_HOSTNAME=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fleet-token) FLEET_TOKEN="${2:-}"; shift 2 ;;
    --worker-type) WORKER_TYPE="${2:-cpu}"; shift 2 ;;
    --machine-index) MACHINE_INDEX="${2:-}"; shift 2 ;;
    --machine-index-from-hostname) FROM_HOSTNAME=1; shift ;;
    --base-url) BASE_URL="${2:-$BASE_URL}"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$FLEET_TOKEN" ]]; then
  echo "Missing --fleet-token" >&2
  exit 2
fi

if [[ "$FROM_HOSTNAME" == "1" || -z "$MACHINE_INDEX" || "$MACHINE_INDEX" == "AUTO" ]]; then
  MACHINE_INDEX="$(hostname | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9-]+/-/g; s/^-+|-+$//g')"
fi

if [[ -z "$MACHINE_INDEX" ]]; then
  echo "Could not determine machine index. Pass --machine-index 001." >&2
  exit 2
fi

if [[ ! -f "slave.yml" ]]; then
  echo "Run this from a tig-benchmarker directory containing slave.yml." >&2
  echo "Example:"
  echo "  git clone https://github.com/tig-foundation/tig-monorepo.git"
  echo "  cd tig-monorepo/tig-benchmarker"
  echo "  curl -fsSL ${BASE_URL}/static/fleet-install.sh | bash -s -- --fleet-token TOKEN --worker-type ${WORKER_TYPE} --machine-index ${MACHINE_INDEX}"
  exit 2
fi

TMP_JSON="$(mktemp)"
cleanup() { rm -f "$TMP_JSON"; }
trap cleanup EXIT

CONFIG_URL="${BASE_URL%/}/api/fleet/config?token=${FLEET_TOKEN}&worker_type=${WORKER_TYPE}&machine_index=${MACHINE_INDEX}"
curl -fsSL "$CONFIG_URL" > "$TMP_JSON"

python3 - "$TMP_JSON" <<'PY'
import json
import os
import pathlib
import re
import sys

data = json.loads(pathlib.Path(sys.argv[1]).read_text())
if not data.get("success"):
    raise SystemExit("Fleet config request failed")

def detect_cpu_workers():
    override = os.environ.get("NUM_WORKERS")
    if override and override.isdigit() and int(override) > 0:
        return int(override)

    return max(1, os.cpu_count() or 1)

pathlib.Path("algorithms").mkdir(exist_ok=True)
pathlib.Path("results").mkdir(exist_ok=True)
env_text = data["setup_command"].split("cat > .env <<EOF\n", 1)[1].split("\nEOF", 1)[0]
env_text = env_text.replace("$(pwd)", str(pathlib.Path.cwd()))
if data.get("worker_type") == "gpu":
    workers = int(os.environ.get("NUM_WORKERS") or "1")
else:
    workers = detect_cpu_workers()
env_text = re.sub(r"^NUM_WORKERS=.*$", f"NUM_WORKERS={workers}", env_text, flags=re.MULTILINE)
pathlib.Path(".env").write_text(env_text + "\n")

print("InnoPool fleet slave configured")
print(f"  slave_name   : {data['slave_name']}")
print(f"  fleet_id     : {data['fleet_id']}")
print(f"  worker_type  : {data['worker_type']}")
print(f"  machine_index: {data['machine_index']}")
print(f"  num_workers  : {workers}")
print()
print("Next commands:")
print(data.get("preflight_command") or "")
print(data.get("start_command") or "")
print()
print("If the preflight blocks a low-spec machine and the operator has approved it, rerun preflight with --allow-low-spec.")
PY
