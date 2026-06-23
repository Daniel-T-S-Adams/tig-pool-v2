#!/usr/bin/env bash
set -euo pipefail

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

echo "InnoPool preflight"
echo "  slave: ${SLAVE_NAME}"
echo "  master: ${MASTER_IP}:${MASTER_PORT}"
echo "  algorithms: ${ALGORITHMS_DIR}"
echo "  results: ${RESULTS_DIR}"

docker compose -f slave.yml config >/dev/null

services=("$@")
if [ "${#services[@]}" -eq 0 ]; then
  services=(slave satisfiability vehicle_routing knapsack job_scheduling energy_arbitrage)
fi

docker compose -f slave.yml up -d --force-recreate "${services[@]}"
docker compose -f slave.yml exec slave sh -lc 'test -d algorithms && test -d results && echo "slave mounts ok"'

for service in "${services[@]}"; do
  if [ "$service" = "slave" ]; then
    continue
  fi
  docker compose -f slave.yml exec "$service" sh -lc 'test -d algorithms && test -d results && echo "'"$service"' mounts ok"'
done

echo "Preflight passed. Watching slave logs."
docker compose -f slave.yml logs -f slave
