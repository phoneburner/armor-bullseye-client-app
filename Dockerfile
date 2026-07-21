FROM python:3.12-slim@sha256:804ddf3251a60bbf9c92e73b7566c40428d54d0e79d3428194edf40da6521286

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
