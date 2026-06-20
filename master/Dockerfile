ARG TIG_VERSION=0.0.6
FROM ghcr.io/tig-foundation/tig-monorepo/benchmarker/master:${TIG_VERSION}

# Patch slave_manager.py to support per_challenge_time_before_batch_retry.
# All other master files remain from the official image.
COPY slave_manager.py /app/master/slave_manager.py
