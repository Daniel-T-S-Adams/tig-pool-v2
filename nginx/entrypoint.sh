#!/bin/sh
# Wait for upstream services to be resolvable before starting nginx.
# This prevents "host not found in upstream" on cold start.

wait_for() {
    host=$1
    port=$2
    echo "Waiting for $host:$port..."
    until nc -z "$host" "$port" 2>/dev/null; do
        sleep 1
    done
    echo "$host:$port is ready."
}

wait_for master         3336
wait_for pool_manager   8080
wait_for benchmarker_ui 80

exec nginx -g 'daemon off;'
