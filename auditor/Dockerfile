FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# docker CLI only — talks to the host daemon over the mounted socket to
# `docker exec <challenge> tig-verifier ...`. Never runs tig-runtime.
# Debian 13 (trixie, current python:3.12-slim) splits the client into
# `docker-cli`; `docker.io` there is daemon-only. Older bases only have
# `docker.io`, which still ships the binary.
RUN apt-get update \
    && (apt-get install -y --no-install-recommends docker-cli \
        || apt-get install -y --no-install-recommends docker.io) \
    && command -v docker \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY audit_core.py main.py ./
CMD ["python", "-u", "main.py"]
