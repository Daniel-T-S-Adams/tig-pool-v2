ARG TIG_VERSION=0.0.7
FROM ghcr.io/tig-foundation/tig-monorepo/benchmarker/master:${TIG_VERSION}

# Patch slave_manager.py to support per_challenge_time_before_batch_retry.
# All other master files remain from the official image.
COPY slave_manager.py /app/master/slave_manager.py
COPY precommit_manager.py /app/master/precommit_manager.py
COPY job_manager.py /app/master/job_manager.py
COPY data_fetcher.py /app/master/data_fetcher.py
COPY proof_affinity.py /app/master/proof_affinity.py
COPY capability_scheduler.py /app/master/capability_scheduler.py
COPY cpu_tier_caps.py /app/master/cpu_tier_caps.py
