FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# docker CLI only — talks to the host daemon over the mounted socket to
# `docker exec <challenge> tig-verifier ...`. Never runs tig-runtime.
RUN apt-get update && apt-get install -y --no-install-recommends docker.io \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY audit_core.py main.py ./
CMD ["python", "-u", "main.py"]
