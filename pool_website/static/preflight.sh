#!/usr/bin/env bash
set -euo pipefail

ALLOW_LOW_SPEC="${INNOPOOL_ALLOW_LOW_SPEC:-0}"
BASE_URL="${INNOPOOL_URL:-https://www.innopool.co.uk}"
MIN_CPU_THREADS="${INNOPOOL_MIN_CPU_THREADS:-24}"
# 28 GB so nominal 32 GB boxes (MemTotal often ~30 GB) are not false low-spec.
MIN_RAM_GB="${INNOPOOL_MIN_RAM_GB:-28}"
MIN_DISK_GB="${INNOPOOL_MIN_DISK_GB:-100}"
# Optional only. Default 0 = no VRAM floor (any working NVIDIA GPU is accepted).
MIN_GPU_VRAM_GB="${INNOPOOL_MIN_GPU_VRAM_GB:-0}"
services=()
warnings=()
worker_type="cpu"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --allow-low-spec)
      ALLOW_LOW_SPEC=1
      shift
      ;;
    --worker-type)
      worker_type="${2:-cpu}"
      shift 2
      ;;
    --base-url)
      BASE_URL="${2:-$BASE_URL}"
      shift 2
      ;;
    --)
      shift
      while [ "$#" -gt 0 ]; do
        services+=("$1")
        shift
      done
      ;;
    *)
      services+=("$1")
      shift
      ;;
  esac
done

if [ ! -f slave.yml ]; then
  echo "Run this from tig-monorepo/tig-benchmarker, where slave.yml exists." >&2
  exit 1
fi

if [ ! -f .env ]; then
  echo "Missing .env. Run the InnoPool setup command first." >&2
  exit 1
fi

required_vars="VERSION SLAVE_NAME MASTER_IP MASTER_PORT NUM_WORKERS ALGORITHMS_DIR RESULTS_DIR TTL"
for name in $required_vars; do
  if ! grep -Eq "^${name}=.+" .env; then
    echo "Missing or empty ${name} in .env" >&2
    exit 1
  fi
done

set -a
# shellcheck disable=SC1091
. ./.env
set +a

mkdir -p "${ALGORITHMS_DIR}" "${RESULTS_DIR}"

if [[ "${SLAVE_NAME}" == pool-gpu-* ]] || printf '%s\n' "${services[@]}" | grep -Eq '^(vector_search|hypergraph|neuralnet_optimizer)$'; then
  worker_type="gpu"
fi

fail_or_warn() {
  local message="$1"
  warnings+=("$message")
  if [ "${ALLOW_LOW_SPEC}" = "1" ]; then
    echo "WARNING: ${message}" >&2
    return 0
  fi
  echo "ERROR: ${message}" >&2
  echo "To run anyway, rerun preflight with --allow-low-spec or set INNOPOOL_ALLOW_LOW_SPEC=1." >&2
  exit 1
}

detect_threads() {
  if command -v nproc >/dev/null 2>&1; then
    nproc
  else
    getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1
  fi
}

detect_ram_gb() {
  awk '/MemTotal/ { printf "%.0f\n", $2 / 1024 / 1024 }' /proc/meminfo 2>/dev/null || echo 0
}

detect_disk_gb() {
  df -BG . | awk 'NR==2 { gsub(/G/, "", $4); print $4 }'
}

threads="$(detect_threads)"
ram_gb="$(detect_ram_gb)"
disk_gb="$(detect_disk_gb)"

echo "InnoPool preflight"
echo "  slave: ${SLAVE_NAME}"
echo "  master: ${MASTER_IP}:${MASTER_PORT}"
echo "  algorithms: ${ALGORITHMS_DIR}"
echo "  results: ${RESULTS_DIR}"
echo "  worker type: ${worker_type}"
echo "  logical threads: ${threads}"
echo "  ram: ${ram_gb} GB"
echo "  free disk: ${disk_gb} GB"

if [ "${worker_type}" = "cpu" ] && [ "${threads}" -lt "${MIN_CPU_THREADS}" ]; then
  fail_or_warn "CPU workers require at least ${MIN_CPU_THREADS} logical threads; detected ${threads}."
fi

if [ "${ram_gb}" -lt "${MIN_RAM_GB}" ]; then
  fail_or_warn "Workers require at least ${MIN_RAM_GB} GB RAM; detected ${ram_gb} GB."
fi

if [ "${disk_gb}" -lt "${MIN_DISK_GB}" ]; then
  fail_or_warn "Workers require at least ${MIN_DISK_GB} GB free disk; detected ${disk_gb} GB."
fi

gpu_count=0
gpu_name_first=""
gpu_vram_mb_max=0
gpu_names_csv=""
if [ "${worker_type}" = "gpu" ]; then
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    fail_or_warn "GPU workers require nvidia-smi and a working NVIDIA driver."
  else
    gpu_count="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | grep -c . || true)"
    if [ "${gpu_count}" -lt 1 ]; then
      fail_or_warn "GPU workers require at least one visible NVIDIA GPU."
    fi
    # Print GPU inventory for the report; VRAM is informational unless an
    # operator explicitly sets INNOPOOL_MIN_GPU_VRAM_GB > 0.
    while IFS=, read -r gpu_name vram_mb; do
      gpu_name="$(echo "${gpu_name}" | xargs)"
      vram_mb="$(echo "${vram_mb}" | xargs)"
      echo "  gpu: ${gpu_name} (${vram_mb:-?} MiB)"
      if [ -z "${gpu_name_first}" ]; then
        gpu_name_first="${gpu_name}"
      fi
      if [ -n "${gpu_names_csv}" ]; then
        gpu_names_csv="${gpu_names_csv}; ${gpu_name}"
      else
        gpu_names_csv="${gpu_name}"
      fi
      if [ -n "${vram_mb}" ] && [ "${vram_mb}" -gt "${gpu_vram_mb_max}" ] 2>/dev/null; then
        gpu_vram_mb_max="${vram_mb}"
      fi
      if [ "${MIN_GPU_VRAM_GB}" -gt 0 ] && [ -n "${vram_mb}" ]; then
        min_vram_mb=$((MIN_GPU_VRAM_GB * 1024))
        if [ "${vram_mb}" -lt "${min_vram_mb}" ]; then
          fail_or_warn "GPU ${gpu_name} has ${vram_mb} MiB VRAM; configured minimum is ${MIN_GPU_VRAM_GB} GB."
        fi
      fi
    done < <(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null || true)
    echo "  gpu count: ${gpu_count}"
  fi
fi

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker is not running or this user cannot access Docker." >&2
  exit 1
fi

docker compose -f slave.yml config >/dev/null

if [ "${#services[@]}" -eq 0 ]; then
  services=(slave satisfiability vehicle_routing knapsack job_scheduling energy_arbitrage)
fi

docker compose -f slave.yml up -d --force-recreate "${services[@]}"
docker compose -f slave.yml exec -T slave sh -lc 'test -d algorithms && test -d results && echo "slave mounts ok"'

for service in "${services[@]}"; do
  if [ "$service" = "slave" ]; then
    continue
  fi
  docker compose -f slave.yml exec -T "$service" sh -lc 'test -d algorithms && test -d results && echo "'"$service"' mounts ok"'
done

if [ "${ALLOW_LOW_SPEC}" = "1" ] && [ "${#warnings[@]}" -gt 0 ]; then
  echo
  echo "Low-spec override enabled. This machine is unsupported and may be disabled if it harms InnoPool health." >&2
fi

preflight_status="passed"
if [ "${ALLOW_LOW_SPEC}" = "1" ] && [ "${#warnings[@]}" -gt 0 ]; then
  preflight_status="low_spec_override"
fi

if command -v python3 >/dev/null 2>&1; then
  WARNINGS_TEXT="$(printf '%s\n' "${warnings[@]}")" \
  BASE_URL="${BASE_URL}" \
  PREFLIGHT_STATUS="${preflight_status}" \
  PREFLIGHT_WORKER_TYPE="${worker_type}" \
  PREFLIGHT_THREADS="${threads}" \
  PREFLIGHT_RAM_GB="${ram_gb}" \
  PREFLIGHT_DISK_GB="${disk_gb}" \
  PREFLIGHT_GPU_COUNT="${gpu_count}" \
  PREFLIGHT_GPU_NAME="${gpu_name_first}" \
  PREFLIGHT_GPU_NAMES="${gpu_names_csv}" \
  PREFLIGHT_GPU_VRAM_MB="${gpu_vram_mb_max}" \
  PREFLIGHT_SLAVE_NAME="${SLAVE_NAME}" \
  python3 - <<'PY' || true
import json
import os
import urllib.request

base_url = os.environ["BASE_URL"].rstrip("/")
payload = {
    "slave_name": os.environ["PREFLIGHT_SLAVE_NAME"],
    "worker_type": os.environ["PREFLIGHT_WORKER_TYPE"],
    "status": os.environ["PREFLIGHT_STATUS"],
    "report": {
        "threads": int(os.environ["PREFLIGHT_THREADS"]),
        "ram_gb": int(os.environ["PREFLIGHT_RAM_GB"]),
        "disk_gb": int(os.environ["PREFLIGHT_DISK_GB"]),
        "gpu_count": int(os.environ.get("PREFLIGHT_GPU_COUNT") or 0),
        "gpu_name": os.environ.get("PREFLIGHT_GPU_NAME") or "",
        "gpu_names": os.environ.get("PREFLIGHT_GPU_NAMES") or "",
        "gpu_vram_mb": int(os.environ.get("PREFLIGHT_GPU_VRAM_MB") or 0),
        "warnings": [line for line in os.environ.get("WARNINGS_TEXT", "").splitlines() if line],
    },
}
data = json.dumps(payload).encode()
req = urllib.request.Request(
    f"{base_url}/api/slave/preflight",
    data=data,
    headers={"Content-Type": "application/json"},
    method="POST",
)
urllib.request.urlopen(req, timeout=5).read()
PY
fi

echo "Preflight passed. Watching slave logs."
docker compose -f slave.yml logs -f slave
