# Base image pinned by digest. Re-pin it whenever you cut a release: a digest
# never moves, so a stale pin keeps shipping the OS packages that were current
# the day it was taken. Look up the current one with:
#   docker pull python:3.12-slim && docker image inspect python:3.12-slim \
#     | jq -r '.[0].RepoDigests[0]'
#
# Re-pinned 2026-11-05. The previous pin (804ddf32) had accumulated 6 critical
# and 71 high fixable CVEs across 19 OS packages, including openssl/libssl3 and
# libsqlite3. This digest scans clean at high/critical.
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Diagnostic tools bundled in the image so customers running behind
# firewalls / EKS NetworkPolicies / corporate proxies can debug
# connectivity without needing to install anything.  Kept small:
# curl+ca-certs for HTTPS, dnsutils for dig/nslookup, iputils-ping for
# ping, netcat-openbsd for raw TCP port checks.  Combined ~10 MB.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        dnsutils \
        iputils-ping \
        netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# Bundled diagnostic: net-check runs a full DNS / TCP / TLS / HTTP probe
# against the Bullseye server and the configured telephony provider.
# Available inside the container as `net-check`.
RUN install -m 0755 /app/bin/net-check /usr/local/bin/net-check

RUN useradd --create-home --shell /bin/bash bullseye \
    && chown -R bullseye:bullseye /app
USER bullseye

CMD ["python", "main.py"]
