#!/usr/bin/env bash
# The v2 pool serves its checked, paired worker installer through its own API.
set -euo pipefail
printf '%s\n' 'This legacy batch-worker installer is retired in the v2 fork.' 'Open /join on the v2 pool and download its verified paired worker installer.' >&2
exit 2
