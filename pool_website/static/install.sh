#!/usr/bin/env bash
# InnoPool one-command miner install — custom innopool-slave + telemetry.
# Honors register-page worker choice: cpu | gpu | both (never auto-starts GPU).
set -euo pipefail

BASE_URL="${INNOPOOL_URL:-https://www.innopool.co.uk}"
FLEET_TOKEN="${FLEET_TOKEN:-}"
WORKER_TYPE="${WORKER_TYPE:-cpu}"
MACHINE_INDEX="${MACHINE_INDEX:-}"
INSTALL_ROOT="${INNOPOOL_INSTALL_ROOT:-$HOME}"
SLAVE_REPO="${INNOPOOL_SLAVE_REPO:-https://github.com/rootztigmod/innopool-slave.git}"
SLAVE_REF="${INNOPOOL_SLAVE_REF:-main}"
SKIP_DOCKER_INSTALL=0
SKIP_NVIDIA_INSTALL=0
START=1

usage() {
  cat <<'EOF'
Usage:
  curl -fsSL https://www.innopool.co.uk/static/install.sh | bash -s -- \
    --fleet-token TOKEN --worker-type cpu|gpu|both

Options:
  --fleet-token TOKEN     Required. From the Join page after register.
  --worker-type TYPE      cpu | gpu | both  (must match what you registered)
  --machine-index NAME    Default: EC2 instance-id, else hostname
  --install-root DIR      Default: $HOME  (creates innopool-slave-cpu / -gpu)
  --base-url URL          Default: https://www.innopool.co.uk
  --skip-docker-install   Do not apt-install Docker
  --skip-nvidia-install   Do not auto-install NVIDIA drivers / toolkit
  --no-start              Configure .env only; do not compose up
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fleet-token) FLEET_TOKEN="${2:-}"; shift 2 ;;
    --worker-type) WORKER_TYPE="${2:-cpu}"; shift 2 ;;
    --machine-index) MACHINE_INDEX="${2:-}"; shift 2 ;;
    --install-root) INSTALL_ROOT="${2:-$HOME}"; shift 2 ;;
    --base-url) BASE_URL="${2:-$BASE_URL}"; shift 2 ;;
    --skip-docker-install) SKIP_DOCKER_INSTALL=1; shift ;;
    --skip-nvidia-install) SKIP_NVIDIA_INSTALL=1; shift ;;
    --no-start) START=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

WORKER_TYPE="$(echo "$WORKER_TYPE" | tr '[:upper:]' '[:lower:]')"
if [[ -z "$FLEET_TOKEN" ]]; then
  echo "Missing --fleet-token" >&2
  exit 2
fi
if [[ "$WORKER_TYPE" != "cpu" && "$WORKER_TYPE" != "gpu" && "$WORKER_TYPE" != "both" ]]; then
  echo "worker_type must be cpu, gpu, or both (got: $WORKER_TYPE)" >&2
  exit 2
fi

if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=""
else
  SUDO="sudo"
fi

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

install_docker_if_needed() {
  if need_cmd docker && docker compose version >/dev/null 2>&1; then
    return 0
  fi
  if [[ "$SKIP_DOCKER_INSTALL" == "1" ]]; then
    echo "Docker / compose plugin missing and --skip-docker-install was set." >&2
    exit 1
  fi
  if ! need_cmd apt-get; then
    echo "Please install Docker + docker compose plugin, then re-run." >&2
    exit 1
  fi
  echo "Installing Docker..."
  $SUDO apt-get update
  $SUDO apt-get install -y curl git ca-certificates python3
  $SUDO apt-get remove -y docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc || true
  curl -fsSL https://get.docker.com | $SUDO sh
  $SUDO apt-get install -y docker-compose-plugin
  $SUDO systemctl enable --now docker || true
  if [[ -n "${SUDO}" ]]; then
    $SUDO usermod -aG docker "$USER" || true
  fi
}

resolve_machine_index() {
  if [[ -n "$MACHINE_INDEX" && "$MACHINE_INDEX" != "AUTO" ]]; then
    echo "$MACHINE_INDEX"
    return
  fi
  local imds_token instance_id
  imds_token="$(curl -s -m 2 -X PUT http://169.254.169.254/latest/api/token \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 21600' || true)"
  if [[ -n "$imds_token" ]]; then
    instance_id="$(curl -s -m 2 -H "X-aws-ec2-metadata-token: $imds_token" \
      http://169.254.169.254/latest/meta-data/instance-id || true)"
    if [[ -n "$instance_id" ]]; then
      echo "$instance_id"
      return
    fi
  fi
  hostname | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9-]+/-/g; s/^-+|-+$//g'
}

cpu_services() {
  echo "slave satisfiability vehicle_routing knapsack job_scheduling energy_arbitrage"
}

gpu_services() {
  echo "slave vector_search hypergraph neuralnet_optimizer"
}

nvidia_smi_ok() {
  if need_cmd nvidia-smi && nvidia-smi >/dev/null 2>&1; then
    return 0
  fi
  if [[ -n "${SUDO}" ]] && $SUDO nvidia-smi >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

has_nvidia_pci() {
  if need_cmd lspci && lspci -nn 2>/dev/null | grep -qiE 'NVIDIA|10de:'; then
    return 0
  fi
  return 1
}

install_nvidia_drivers() {
  if ! need_cmd apt-get; then
    echo "apt-get not available; install NVIDIA drivers manually, then re-run." >&2
    exit 1
  fi
  echo "Installing NVIDIA drivers (this can take several minutes)..."
  export DEBIAN_FRONTEND=noninteractive
  $SUDO apt-get update
  $SUDO apt-get install -y \
    "linux-headers-$(uname -r)" \
    ubuntu-drivers-common \
    dkms \
    build-essential \
    curl \
    gnupg \
    ca-certificates
  $SUDO apt-get install -y "linux-modules-extra-$(uname -r)" || true
  $SUDO ubuntu-drivers devices || true
  # Prefer recent open/server packages; fall back to ubuntu-drivers.
  $SUDO apt-get install -y nvidia-driver-595-open nvidia-utils-595 \
    || $SUDO apt-get install -y nvidia-driver-580-open nvidia-utils-580 \
    || $SUDO apt-get install -y nvidia-driver-580-server nvidia-utils-580-server nvidia-dkms-580-server \
    || $SUDO ubuntu-drivers install
  $SUDO dkms autoinstall || true
  $SUDO depmod -a || true
  $SUDO modprobe nvidia || true
  if ! nvidia_smi_ok; then
    echo "NVIDIA drivers installed but nvidia-smi still failed." >&2
    echo "Try: sudo modprobe nvidia && sudo nvidia-smi" >&2
    exit 1
  fi
  echo "nvidia-smi OK."
}

install_nvidia_container_toolkit() {
  if ! need_cmd apt-get; then
    echo "apt-get not available; install nvidia-container-toolkit manually." >&2
    exit 1
  fi
  echo "Installing NVIDIA Container Toolkit..."
  export DEBIAN_FRONTEND=noninteractive
  $SUDO mkdir -p /usr/share/keyrings
  $SUDO rm -f /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | $SUDO gpg --batch --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | $SUDO tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  $SUDO apt-get update
  $SUDO apt-get install -y nvidia-container-toolkit
  $SUDO nvidia-ctk runtime configure --runtime=docker
  $SUDO systemctl restart docker || true
  echo "NVIDIA Container Toolkit configured for Docker."
}

ensure_nvidia_for_gpu() {
  if nvidia_smi_ok; then
    echo "NVIDIA driver present (nvidia-smi OK)."
  else
    if [[ "$SKIP_NVIDIA_INSTALL" == "1" ]]; then
      echo "GPU install selected, but nvidia-smi was not found and --skip-nvidia-install was set." >&2
      exit 1
    fi
    if need_cmd apt-get && ! need_cmd lspci; then
      $SUDO apt-get update >/dev/null 2>&1 || true
      $SUDO apt-get install -y pciutils >/dev/null 2>&1 || true
    fi
    if need_cmd lspci && ! has_nvidia_pci; then
      echo "GPU install selected, but no NVIDIA GPU was detected (lspci) and nvidia-smi is missing." >&2
      echo "Use a GPU instance, or register as CPU only." >&2
      exit 1
    fi
    if has_nvidia_pci; then
      echo "NVIDIA GPU detected, but drivers are missing — installing..."
    else
      echo "nvidia-smi missing (could not confirm PCI device) — installing NVIDIA stack for GPU worker..."
    fi
    install_nvidia_drivers
  fi

  # Toolkit is required for compose services with runtime: nvidia.
  if need_cmd nvidia-ctk && [[ -f /etc/docker/daemon.json ]] \
    && grep -q 'nvidia' /etc/docker/daemon.json 2>/dev/null; then
    echo "NVIDIA Container Toolkit already configured."
  else
    if [[ "$SKIP_NVIDIA_INSTALL" == "1" ]]; then
      echo "nvidia-container-toolkit / Docker nvidia runtime missing and --skip-nvidia-install was set." >&2
      exit 1
    fi
    install_nvidia_container_toolkit
  fi

  # Final sanity: host + container GPU path.
  if ! nvidia_smi_ok; then
    echo "nvidia-smi failed after NVIDIA setup." >&2
    exit 1
  fi
  local dcmd
  dcmd="$(docker_bin)"
  if ! $dcmd run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi >/dev/null 2>&1; then
    echo "Warning: docker --gpus all test failed; retrying after docker restart..."
    $SUDO systemctl restart docker || true
    sleep 2
    if ! $dcmd run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi; then
      echo "Docker cannot see the GPU. Check nvidia-container-toolkit and 'sudo docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi'." >&2
      exit 1
    fi
  fi
  echo "Docker GPU runtime OK."
}

clone_or_update_slave() {
  local dest="$1"
  if [[ -d "$dest/.git" ]]; then
    git -C "$dest" fetch --depth 1 origin "$SLAVE_REF" || true
    git -C "$dest" checkout "$SLAVE_REF" || true
    git -C "$dest" pull --ff-only || true
  else
    mkdir -p "$(dirname "$dest")"
    git clone --branch "$SLAVE_REF" --depth 1 "$SLAVE_REPO" "$dest"
  fi
}

docker_bin() {
  if docker info >/dev/null 2>&1; then
    echo docker
  else
    echo "$SUDO docker"
  fi
}

fetch_and_write_env() {
  local dest="$1"
  local wtype="$2"
  local dash_port="$3"
  local tmp_json workers
  tmp_json="$(mktemp)"

  local config_url
  config_url="${BASE_URL%/}/api/fleet/config?token=${FLEET_TOKEN}&worker_type=${wtype}&machine_index=${MACHINE_INDEX}"
  curl -fsSL "$config_url" > "$tmp_json"

  if [[ "$wtype" == "gpu" ]]; then
    workers="${NUM_WORKERS:-1}"
  else
    workers="${NUM_WORKERS:-}"
    if [[ -z "$workers" ]]; then
      # ~80% of cores (10–20% headroom) so telemetry can earn concurrent>1
      # when master cpu_headroom_ratio≈1.25. Override with NUM_WORKERS=...
      workers="$(python3 - <<'PY'
import os
cores = max(1, os.cpu_count() or 1)
print(max(1, (cores * 4) // 5))
PY
)"
    fi
  fi

  mkdir -p "$dest/data/algorithms" "$dest/data/results"

  python3 - "$tmp_json" "$dest" "$workers" "$dash_port" <<'PY'
import json, pathlib, re, sys

data = json.loads(pathlib.Path(sys.argv[1]).read_text())
dest = pathlib.Path(sys.argv[2])
workers = sys.argv[3]
dash_port = sys.argv[4]

if not data.get("success"):
    raise SystemExit(f"Fleet config failed: {data}")

# Prefer structured fields; fall back to parsing legacy setup_command.
slave_name = data.get("slave_name") or ""
master_ip = data.get("master_ip") or ""
master_port = str(data.get("master_port") or "")
tig_version = str(data.get("tig_version") or data.get("version") or "0.0.7")

setup = data.get("setup_command") or ""
if "cat > .env <<EOF" in setup:
    body = setup.split("cat > .env <<EOF\n", 1)[1].split("\nEOF", 1)[0]
    def grab(key, default=""):
        m = re.search(rf"^{key}=(.*)$", body, flags=re.M)
        return (m.group(1).strip() if m else default)
    slave_name = slave_name or grab("SLAVE_NAME")
    master_ip = master_ip or grab("MASTER_IP")
    master_port = master_port or grab("MASTER_PORT")
    tig_version = grab("TIG_VERSION") or grab("VERSION") or tig_version

if not slave_name or not master_ip or not master_port:
    raise SystemExit("Fleet config missing slave_name / master_ip / master_port")

env = f"""# Generated by InnoPool install.sh
SLAVE_NAME={slave_name}
MASTER_IP={master_ip}
MASTER_PORT={master_port}
NUM_WORKERS={workers}
TTL=3600
TIG_VERSION={tig_version}
ALGORITHMS_DIR={dest / 'data' / 'algorithms'}
RESULTS_DIR={dest / 'data' / 'results'}
DASHBOARD_HOST_PORT={dash_port}
VERBOSE=
INNOPOOL_SLAVE_VERSION=innopool-slave/0.1.4
"""
(dest / ".env").write_text(env)
print(f"Configured {slave_name} ({data.get('worker_type')}) in {dest}")
print(f"  NUM_WORKERS={workers}  dashboard=:{dash_port}")
PY
  rm -f "$tmp_json"
}

start_stack() {
  local dest="$1"
  local wtype="$2"
  local services
  local dcmd
  dcmd="$(docker_bin)"
  if [[ "$wtype" == "gpu" ]]; then
    services="$(gpu_services)"
  else
    services="$(cpu_services)"
  fi
  (
    cd "$dest"
    # shellcheck disable=SC2086
    $dcmd compose up -d --build --force-recreate $services
    # shellcheck disable=SC2086
    $dcmd compose ps -q $services | xargs -r $dcmd update --restart unless-stopped || true
    echo
    echo "Dashboard: http://127.0.0.1:$(grep -E '^DASHBOARD_HOST_PORT=' .env | cut -d= -f2-)"
    echo "Logs:      cd $dest && $dcmd compose logs -f slave"
  )
}

install_one() {
  local wtype="$1"
  local dash_port="$2"
  local dest="${INSTALL_ROOT%/}/innopool-slave-${wtype}"

  if [[ "$wtype" == "gpu" ]]; then
    ensure_nvidia_for_gpu
  fi

  echo "==> Installing InnoPool ${wtype} slave into ${dest}"
  clone_or_update_slave "$dest"
  fetch_and_write_env "$dest" "$wtype" "$dash_port"
  if [[ "$START" == "1" ]]; then
    start_stack "$dest" "$wtype"
  else
    echo "Configured only (--no-start). Next:"
    echo "  cd $dest && docker compose up -d --build"
  fi
}

install_docker_if_needed
MACHINE_INDEX="$(resolve_machine_index)"
echo "Using machine_index=${MACHINE_INDEX}"
echo "Worker type (from Join page choice): ${WORKER_TYPE}"

case "$WORKER_TYPE" in
  cpu)  install_one cpu 8787 ;;
  gpu)  install_one gpu 8787 ;;
  both)
    install_one cpu 8787
    install_one gpu 8788
    ;;
esac

echo
echo "InnoPool install finished."
echo "Confirm the slave is online on the pool dashboard after the first batch."
