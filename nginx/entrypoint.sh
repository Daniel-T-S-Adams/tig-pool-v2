#!/bin/sh
# Wait for upstream services to be resolvable before starting nginx.
# This prevents "host not found in upstream" on cold start.

# Generate .htpasswd at runtime so the password is never baked into an image layer.
OPERATOR_USER="${OPERATOR_USER:-admin}"
OPERATOR_PASSWORD="${OPERATOR_PASSWORD:-changeme}"
printf "%s:%s\n" "${OPERATOR_USER}" "$(openssl passwd -apr1 "${OPERATOR_PASSWORD}")" \
    > /etc/nginx/.htpasswd
echo "htpasswd generated for user: ${OPERATOR_USER}"

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
