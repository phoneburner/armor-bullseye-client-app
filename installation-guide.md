# Bullseye Agent — Installation & Configuration Guide

**Version 1.0.2**

This is the Markdown version of the install guide. An HTML version
(`installation-guide.html`) covers the same material.

---

## Contents

1. [Overview](#1-overview)
2. [Prerequisites](#2-prerequisites)
3. [Download](#3-download)
4. [Configure](#4-configure)
5. [Provider Setup](#5-provider-setup)
6. [Install with Docker (Recommended)](#6-install-with-docker-recommended)
7. [Install with Python](#7-install-with-python-alternative)
8. [Verify the Connection](#8-verify-the-connection)
9. [Configuration Reference](#9-configuration-reference)
10. [Troubleshooting](#10-troubleshooting)
11. [Security](#11-security)
12. [Support](#12-support)

---

## 1. Overview

The Bullseye agent is a lightweight service that runs on your infrastructure.
When it receives a test instruction from the Bullseye server, it places a real
phone call through your telecom provider and reports the result.

Calls originate from **your own phone numbers** on **your own carrier**,
giving an accurate picture of whether those numbers are being flagged as spam.

**How it works:** the agent maintains a persistent, encrypted WebSocket
connection to the Bullseye server. When a test is queued, the server pushes
the instruction to the agent in real time. The agent dials the call, monitors
its progress, and reports the outcome (answered, no answer, busy, or failed)
back to the server. Your telecom credentials never leave your machine.

```
[ Your Environment ]                              [ Bullseye Environment ]
                                Encrypted
  Bullseye Agent   <-------- WebSocket --------->   Bullseye Server
        |                  (call status only)
        v
  Your Telecom Provider
  (Twilio / Bandwidth / Telnyx / etc.)
```

---

## 2. Prerequisites

Before you begin, make sure you have:

| Item | Details |
|------|---------|
| **Bullseye credentials** | Your Bullseye contact will provide a server URL and an API key (starts with `bse_`). |
| **Telecom provider account** | One of: Bandwidth, Twilio, Telnyx, RingCentral, Asterisk, FreeSWITCH, or your own (Proprietary). See [section 5](#5-provider-setup). |
| **Server / VM** | Any Linux, macOS, or Windows machine with outbound internet access. Docker recommended; Python 3.10+ is the alternative. |
| **Network access** | Outbound HTTPS (port 443) to the Bullseye server and your provider's API. No inbound ports required. |

---

## 3. Download

Download the latest agent release from GitHub:

- [bullseye-agent-1.0.2.tar.gz](https://github.com/phoneburner/armor-bullseye-client-app/archive/refs/tags/v1.0.2.tar.gz)
- [bullseye-agent-1.0.2.zip](https://github.com/phoneburner/armor-bullseye-client-app/archive/refs/tags/v1.0.2.zip)

Extract:

```bash
# Linux / macOS
tar xzf bullseye-agent-1.0.2.tar.gz
cd armor-bullseye-client-app-1.0.2

# Windows (PowerShell)
Expand-Archive bullseye-agent-1.0.2.zip -DestinationPath .
cd armor-bullseye-client-app-1.0.2
```

You should see:

```
main.py                # Agent entry point
requirements.txt       # Python dependencies
providers/             # Telephony provider integrations
Dockerfile             # Docker image definition
docker-compose.yml     # Docker Compose service
.env.example           # Configuration template
sbc-asterisk/          # Optional: SBC sidecar deployment (Asterisk + agent)
```

---

## 4. Configure

Create your configuration file from the template:

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` and fill in the required values:

| Variable | Required | Description |
|----------|----------|-------------|
| `BULLSEYE_SERVER_URL` | Yes | Server URL provided by Bullseye (e.g. `https://your-bullseye-server`) |
| `BULLSEYE_API_KEY` | Yes | Your agent's API key (starts with `bse_`) |
| `TELEPHONY_PROVIDER` | Yes | `bandwidth`, `twilio`, `telnyx`, `ringcentral`, `asterisk`, `freeswitch`, or `proprietary` |

Then set the credentials for your provider. See the next section.

---

## 5. Provider Setup

Only one provider is needed. Pick the one that matches your telecom setup.

### Bandwidth

CPaaS platform with direct programmatic voice. One-legged, fully automated.

**What you need:**
- A Bandwidth account with Voice API enabled.
- OAuth API credentials (Client ID + Client Secret) from the Bandwidth dashboard: Account → API Credentials → Create.
- A Voice Application (gives you the Application ID).
- A phone number assigned to that application.

| Variable | Description |
|----------|-------------|
| `BANDWIDTH_ACCOUNT_ID` | Your Bandwidth account ID (numeric) |
| `BANDWIDTH_CLIENT_ID` | OAuth Client ID |
| `BANDWIDTH_CLIENT_SECRET` | OAuth Client Secret |
| `BANDWIDTH_APPLICATION_ID` | Voice application ID |
| `BANDWIDTH_ANSWER_URL` | Pre-filled: `https://your-bullseye-server/bandwidth/answer` |

Bandwidth's legacy username/password auth (`BANDWIDTH_API_USERNAME` /
`BANDWIDTH_API_PASSWORD`) still works for backwards compatibility but is
being decommissioned by Bandwidth on **2026-12-02**. Use OAuth.

### Twilio

CPaaS platform with direct programmatic voice. One-legged, fully automated.

**What you need:**
- A Twilio account.
- The **primary Account SID and Auth Token** from the Twilio Console dashboard (not an API key — API keys `SK...` have restricted permissions and will not work).
- A phone number with voice capability purchased in the account.

| Variable | Description |
|----------|-------------|
| `TWILIO_ACCOUNT_SID` | Account SID (starts with `AC`) |
| `TWILIO_AUTH_TOKEN` | Primary Auth Token from the Console dashboard |

### Telnyx

CPaaS with Call Control APIs. One-legged, fully automated. Note that call
events have a 15–20 second delay in the Telnyx API; this does not affect
call completion.

**What you need:**
- A Telnyx account with a **Voice API (Call Control)** application — not TeXML, not SIP Connection.
- An API key from the Telnyx Mission Control portal.
- A phone number assigned to the Call Control application.
- An outbound voice profile assigned to the application.

| Variable | Description |
|----------|-------------|
| `TELNYX_API_KEY` | API key from the Telnyx portal |
| `TELNYX_CONNECTION_ID` | Call Control application ID |

### RingCentral

UCaaS platform. Unlike Bandwidth/Twilio/Telnyx, RingCentral doesn't support
simple REST-based outbound calling. The Bullseye agent uses a **SIP sidecar**
— a small Node.js service that registers as a softphone on your RingCentral
account and places calls via SIP.

**What you need:**
- A RingCentral account with a REST API application registered at [developers.ringcentral.com](https://developers.ringcentral.com).
- A JWT credential from the RingCentral admin portal.
- An **"Other Phone" device** on the extension. Create at: Admin Portal → Phones & Devices → Add Device → Existing Phone.

| Variable | Description |
|----------|-------------|
| `RINGCENTRAL_CLIENT_ID` | REST API app Client ID |
| `RINGCENTRAL_CLIENT_SECRET` | REST API app Client Secret |
| `RINGCENTRAL_JWT_TOKEN` | JWT credential from the admin portal |
| `RINGCENTRAL_SERVER_URL` | Optional. Defaults to `https://platform.ringcentral.com` |

**SIP sidecar:**
- **Docker:** start with the `ringcentral` profile: `docker compose --profile ringcentral up -d --build`. The sidecar runs as a second container automatically.
- **Python:** requires **Node.js 18+** installed. The agent starts the sidecar as a subprocess automatically; the first run installs npm dependencies.

**Caller ID limitation:** the "from" number is determined by which RingCentral
device the sidecar registers as. You cannot specify a different caller ID
per call.

### Asterisk

For customers running their own Asterisk PBX (13.1 or newer). The agent
talks to Asterisk via ARI (Asterisk REST Interface) over HTTP + WebSocket.

**What you need:**
- An Asterisk instance reachable from the agent host.
- ARI enabled (`ari.conf`) with a user the agent can authenticate as.
- HTTP server enabled (`http.conf`) — usually port 8088.
- A dialplan landing-pad context for the call to enter after the originate (a simple `Answer() / Wait(10) / Hangup()` context is enough).
- An outbound SIP trunk or endpoint configured for the destinations you want to dial.

| Variable | Description |
|----------|-------------|
| `ASTERISK_ARI_URL` | ARI base URL, e.g. `http://asterisk-host:8088/ari` |
| `ASTERISK_ARI_USERNAME` | ARI user from `ari.conf` |
| `ASTERISK_ARI_PASSWORD` | ARI password from `ari.conf` |
| `ASTERISK_ENDPOINT_TEMPLATE` | Dial endpoint template, e.g. `PJSIP/{to_number}@my-trunk` |
| `ASTERISK_CONTEXT` | Dialplan context the originated call enters (e.g. `bullseye-landing`) |
| `ASTERISK_EXTENSION` | Optional. Extension within that context (default `s`) |
| `ASTERISK_DIAL_TIMEOUT` | Optional. Dial timeout in seconds (default `30`) |

**SBC deployments:** if you want Bullseye to dial through your own SBC, the
`sbc-asterisk/` directory ships a docker-compose layout that runs the agent
plus an embedded Asterisk sidecar configured against your SBC. See
`sbc-asterisk/README.md`.

### FreeSWITCH

For customers running their own FreeSWITCH instance. The agent connects to
FreeSWITCH's Event Socket (ESL), originates the call, and watches the event
stream for the terminal hangup cause.

**What you need:**
- A FreeSWITCH instance with `mod_event_socket` loaded (loaded by default in vanilla FreeSWITCH).
- ESL bound on an interface reachable from the agent host, with a strong password configured in `event_socket.conf.xml` — **change the default `ClueCon`**.
- A SIP profile or gateway configured for outbound calls to the destinations you want to dial.

| Variable | Description |
|----------|-------------|
| `FREESWITCH_HOST` | ESL host or IP |
| `FREESWITCH_PORT` | Optional. ESL port (default `8021`) |
| `FREESWITCH_PASSWORD` | ESL password from `event_socket.conf.xml` |
| `FREESWITCH_ENDPOINT_TEMPLATE` | Dial string template, e.g. `sofia/gateway/my-provider/{to_number}` |
| `FREESWITCH_DIAL_TIMEOUT` | Optional. Total seconds to wait for the terminal event (default `90`) |

ESL is plain TCP, **no TLS**. Keep the agent and FreeSWITCH on a private
network, or tunnel the ESL connection (Tailscale, WireGuard, or SSH tunnel).

### Proprietary (your own network)

For customers who originate calls through their own telecom infrastructure
— anything that isn't a hosted CPaaS, Asterisk, or FreeSWITCH. You
implement a single Python function (`place_call`) that calls into your
internal system, and the agent handles everything else (WebSocket auth,
reconnect, event streaming).

**What you need:**
- A way to programmatically place a call on your network (an internal SDK, REST API, message queue, etc.).
- Whatever credentials / configuration your network needs — add them as environment variables that your `place_call` reads.

See **`CUSTOM_PROVIDER_GUIDE.md`** in the agent repo for the full
walkthrough: function signature, expected return values, status codes, and
a reference implementation sketch.

The stub lives at `providers/proprietary_provider.py` — open it, find the
`TODO` block inside `place_call`, and fill it in with your network call. No
other code changes required.

---

## 6. Install with Docker (Recommended)

Docker handles Python, dependencies, and automatic restarts.

**1. Install Docker.** Follow the official instructions for your OS at
[docs.docker.com/get-docker](https://docs.docker.com/get-docker/).

**2. Start the agent.**

```bash
docker compose up -d --build
```

**RingCentral users:** start with the SIP sidecar profile:

```bash
docker compose --profile ringcentral up -d --build
```

**3. Check the logs.**

```bash
docker logs -f bullseye-agent
```

You should see the ASCII bullseye banner followed by `CONNECTED — agent is
ready to receive tests`. Press Ctrl+C to stop tailing (the agent keeps running).

### Common Docker commands

| Command | Description |
|---------|-------------|
| `docker compose up -d --build` | Start or rebuild the agent |
| `docker compose down` | Stop the agent |
| `docker compose down && docker compose up -d --build` | Restart and pick up `.env` changes |
| `docker logs -f bullseye-agent` | Follow logs in real time |
| `docker logs --tail 100 bullseye-agent` | Show last 100 log lines |

`docker compose restart` does **not** re-read `.env`. Use `down && up`.

---

## 7. Install with Python (Alternative)

Use this if Docker is not available.

**1. Verify Python version.**

```bash
python3 --version    # must be 3.10 or newer
```

**2. Create a virtual environment and install dependencies.**

```bash
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**3. Start the agent.**

```bash
python3 main.py
```

**Keep it running.** When running from source, use a process supervisor
(`systemd`, `launchd`, `supervisord`) so the agent restarts automatically
after reboots or crashes. The README has a sample `systemd` unit.

---

## 8. Verify the Connection

After starting the agent, check the startup log:

```
B U L L S E Y E  v1.0.2

12:00:01 INFO     Server:   configured
12:00:01 INFO     Provider: twilio
12:00:01 INFO     Connecting to server...
12:00:02 INFO     ============================================================
12:00:02 INFO     CONNECTED — agent is ready to receive tests
12:00:02 INFO     ============================================================
```

If you see `CONNECTED`, the agent is registered and waiting for tests. Your
Bullseye contact can confirm from their side that your agent is visible.

---

## 9. Configuration Reference

All configuration is loaded from `.env` in the agent directory. The
`.env.example` file in the repo is the authoritative list with comments
for every option.

| Variable | Description |
|----------|-------------|
| `BULLSEYE_SERVER_URL` | Bullseye server URL. Use `https://` in production. |
| `BULLSEYE_API_KEY` | Your agent's API key, from your Bullseye contact |
| `TELEPHONY_PROVIDER` | Which provider implementation to load |
| `BULLSEYE_ALLOW_INSECURE` | Set to `1` to allow `http://` server URLs (local testing only) |
| `LOG_LEVEL` | `DEBUG`, `INFO` (default), `WARNING`, or `ERROR` |

Plus the provider-specific variables in [section 5](#5-provider-setup).

---

## 10. Troubleshooting

### "Error: Unsupported provider"

`TELEPHONY_PROVIDER` must be exactly one of `bandwidth`, `twilio`, `telnyx`,
`ringcentral`, `asterisk`, `freeswitch`, or `proprietary` (lowercase, no
quotes).

### "Connection refused" / "authentication failed" from the Bullseye server

- Confirm `BULLSEYE_SERVER_URL` is correct (typically `https://your-bullseye-server`).
- Confirm `BULLSEYE_API_KEY` is the value you were given, starting with `bse_`.
- Check outbound HTTPS (port 443) is open from your host.

### Twilio: "authentication error"

You're probably using a restricted API key (`SK...`). Use the primary Account
SID + Auth Token from the Twilio Console dashboard.

### Bandwidth: call dials but never hangs up

The BXML answer URL isn't reachable. From outside your environment:
`curl https://your-bullseye-server/bandwidth/answer` should return XML.

### Asterisk: `Call initiation failed`

- Check `ASTERISK_ARI_URL` is reachable: `curl -u user:pass http://asterisk-host:8088/ari/asterisk/info`
- Check the `ASTERISK_CONTEXT` and `ASTERISK_EXTENSION` exist in your Asterisk dialplan.
- Check `ASTERISK_ENDPOINT_TEMPLATE` matches your channel tech (e.g. `PJSIP/{to_number}@trunk-name`).

### FreeSWITCH: ESL connection refused

- Confirm `mod_event_socket` is loaded: `fs_cli -x "module_exists mod_event_socket"`.
- Confirm `event_socket.conf.xml` binds to an interface reachable from the agent.
- Test from the agent host: `nc -vz $FREESWITCH_HOST $FREESWITCH_PORT`.

### Updating the agent

```bash
docker compose down
git pull
docker compose up -d --build
```

---

## 11. Security

- **TLS enforced.** The agent refuses `ws://` server URLs by default.
- **Sanitized errors.** Provider exceptions are logged locally only; only generic strings are sent to the Bullseye server.
- **Pinned dependencies.** Exact versions in `requirements.txt`; Docker base image pinned by digest.
- **Max WebSocket message size.** 64 KiB — protects against memory exhaustion from a malicious server.
- **Non-root container.** Runs as UID 1000 inside Docker.
- **`.env` excluded** from the Docker image (`.dockerignore`) and from git (`.gitignore`).

Bullseye runs entirely on your infrastructure and uses your existing telecom
credentials to place test calls. Your API keys, tokens, and account IDs
never leave your environment. The only information sent to ARMOR is the
call status (e.g. spam label, blocked, delivered) and the phone number
being tested. No call content or recordings are ever collected. All
connections are encrypted with TLS and Armor Solutions is SOC 2 Type II
compliant. You can remove Bullseye from your environment at any time.

---

## 12. Support

For help, contact your Bullseye representative. Include in your message:

- Agent version (displayed at startup)
- Which provider you're using
- The log output around the issue (with sensitive values redacted)

---

*Bullseye Agent v1.0.2 — Installation Guide — © 2026 Armor Solutions, Inc.*
