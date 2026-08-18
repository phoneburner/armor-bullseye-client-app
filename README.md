# Bullseye Agent

The Bullseye agent runs on your machine and places test calls through your
own telecom provider when instructed by the Bullseye server. Calls originate
from your numbers, on your carrier — giving a realistic view of how your
numbers behave in production.

**Looking for the full install guide?** It ships in two formats:
- [`installation-guide.html`](installation-guide.html) — open in a browser
- [`installation-guide.md`](installation-guide.md) — Markdown

If you want to extend Bullseye with your own telephony backend (i.e.
you're not using one of the built-in providers), see
[`CUSTOM_PROVIDER_GUIDE.md`](CUSTOM_PROVIDER_GUIDE.md).

## Prerequisites

1. **Credentials from your Bullseye contact:**
   - `BULLSEYE_SERVER_URL` — the server your agent connects to
   - `BULLSEYE_API_KEY` — unique key for your agent (starts with `bse_`)

2. **Credentials for your telephony provider** (see [Provider Setup](#provider-setup) below)

3. **A machine that can run Docker** (recommended) **or Python 3.10+**

4. **Outbound network access** to the Bullseye server (port 443) and your
   provider's API

---

## Quick Start

### 1. Download and configure

```bash
# Extract the distribution archive (or clone the repo)
cd bullseye-agent
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set:
- `BULLSEYE_SERVER_URL`
- `BULLSEYE_API_KEY`
- `TELEPHONY_PROVIDER` (`bandwidth`, `twilio`, `telnyx`, or `ringcentral`)
- The credential variables for your chosen provider (see [Provider Setup](#provider-setup))

### 2. Start

**Docker (recommended):**

```bash
docker compose up -d --build
docker logs -f bullseye-agent
```

For RingCentral, start with the SIP sidecar profile:

```bash
docker compose --profile ringcentral up -d --build
```

**Python:**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

### 3. Verify

You should see:

```
12:00:01 INFO     Server:   configured
12:00:01 INFO     Provider: twilio
12:00:01 INFO     Connecting to server...
12:00:02 INFO     ============================================================
12:00:02 INFO     CONNECTED — agent is ready to receive tests
12:00:02 INFO     ============================================================
```

---

## Provider Setup

### Bandwidth

Bandwidth is a CPaaS platform with direct programmatic voice. Calls are
one-legged and fully automated.

**What you need:**
- A Bandwidth account with Voice API enabled (a free
  [Bandwidth Build](https://www.bandwidth.com/build-sign-up/) account works
  for evaluation, with the limitation that trial accounts can only call the
  verified mobile number on file until a credit card is added)
- **OAuth API credentials** (Client ID + Client Secret) created in the
  Bandwidth dashboard: **Account → API Credentials → Create**
- A Voice Application (provides the Application ID)
- A phone number assigned to that application

**Environment variables:**

| Variable | Description |
|----------|-------------|
| `BANDWIDTH_ACCOUNT_ID` | Your Bandwidth account ID (numeric) |
| `BANDWIDTH_CLIENT_ID` | OAuth Client ID |
| `BANDWIDTH_CLIENT_SECRET` | OAuth Client Secret |
| `BANDWIDTH_APPLICATION_ID` | Voice application ID |
| `BANDWIDTH_ANSWER_URL` | Pre-filled: `https://your-bullseye-server/bandwidth/answer` |

> **Legacy Basic Auth** (`BANDWIDTH_API_USERNAME` / `BANDWIDTH_API_PASSWORD`) is
> still accepted for backwards compatibility but is deprecated by Bandwidth
> and will be decommissioned on 2026-12-02. Use OAuth.

The `BANDWIDTH_ANSWER_URL` points to a Bullseye server endpoint that tells
Bandwidth what to do when the call is answered (hold the call open for a
randomized 35-55 seconds, then hang up — the same behavior as every other
provider). It's pre-configured — you don't need to change it.

**Additional requirements:** None. Works with Docker or Python out of the box.

---

### Twilio

Twilio is a CPaaS platform with direct programmatic voice. Calls are
one-legged and fully automated.

**What you need:**
- A Twilio account
- The primary Account SID and Auth Token from the
  [Twilio Console dashboard](https://console.twilio.com) (not an API key)
- A phone number with voice capability purchased in the account

**Environment variables:**

| Variable | Description |
|----------|-------------|
| `TWILIO_ACCOUNT_SID` | Account SID (starts with `AC`) |
| `TWILIO_AUTH_TOKEN` | Primary Auth Token from the Console dashboard |

> **Note:** Twilio API keys (`SK...`) have restricted permissions and will not
> work. Use the primary Auth Token.

**Additional requirements:** None. Works with Docker or Python out of the box.

---

### Telnyx

Telnyx is a CPaaS platform with Call Control APIs. Calls are one-legged and
fully automated, but call events have a 15–20 second delay in the Telnyx API.

**What you need:**
- A Telnyx account with a **Voice API (Call Control)** application — not
  TeXML, not SIP Connection
- An API key from the Telnyx portal
- A phone number assigned to the Call Control application
- An outbound voice profile assigned to the application
- A webhook URL configured on the application (can use `webhook.site` for
  testing — the agent doesn't receive webhooks, it polls the events API)

**Environment variables:**

| Variable | Description |
|----------|-------------|
| `TELNYX_API_KEY` | API key from the Telnyx Mission Control portal |
| `TELNYX_CONNECTION_ID` | Call Control application ID |

**Additional requirements:** None. Works with Docker or Python out of the box.

---

### RingCentral

RingCentral is a UCaaS platform. Unlike Twilio/Bandwidth/Telnyx, it does
not support simple REST-based outbound calling. The Bullseye agent uses a
**SIP sidecar** (a small Node.js service) to register as a softphone on
your RingCentral account and place calls directly via SIP.

**What you need:**
- A RingCentral account with a REST API application registered at
  [developers.ringcentral.com](https://developers.ringcentral.com)
- A JWT credential generated in the RingCentral admin portal
- An **"Other Phone" device** on the extension that the sidecar will
  register as. To create one: RingCentral Admin Portal → Phones & Devices →
  Add Device → Existing Phone.

**Environment variables:**

| Variable | Description |
|----------|-------------|
| `RINGCENTRAL_CLIENT_ID` | REST API app Client ID |
| `RINGCENTRAL_CLIENT_SECRET` | REST API app Client Secret |
| `RINGCENTRAL_JWT_TOKEN` | JWT credential from the admin portal |
| `RINGCENTRAL_SERVER_URL` | Optional. Defaults to `https://platform.ringcentral.com`. Use `https://platform.devtest.ringcentral.com` for sandbox. |

**Additional requirements:**

The SIP sidecar needs to run alongside the agent:

| Install method | What happens |
|----------------|-------------|
| **Docker** | Start with the `ringcentral` profile: `docker compose --profile ringcentral up -d --build`. The sidecar runs as a second container automatically. |
| **Python** | Requires **Node.js 18+** installed. The agent detects this and starts the sidecar as a subprocess automatically. First run will install npm dependencies. |

The sidecar registers as a SIP endpoint on your RingCentral account. No
public IP or inbound ports are needed — SIP runs over WebSocket (outbound
connection).

> **Note:** The "from" number is determined by which RingCentral device the
> sidecar registers as. You cannot specify a different caller ID per call.

### Asterisk

For customers running their own Asterisk PBX (13.1+). The agent talks to
Asterisk over ARI (REST + WebSocket) and originates calls into a landing
dialplan context that holds each call for a randomized 35-55 seconds.
See [`installation-guide.md`](installation-guide.md#asterisk) for env
vars and the sample dialplan.

For customers who want to dial through their own SBC, the
`sbc-asterisk/` directory ships a docker-compose stack that runs the
agent plus a preconfigured Asterisk sidecar. See
[`sbc-asterisk/README.md`](sbc-asterisk/README.md).

### Cisco Webex Calling

For customers whose phone system is Webex Calling. The `webex-calling/`
directory ships a docker-compose stack that runs the agent plus an
embedded Asterisk sidecar which registers to Webex as customer-managed
"Generic SIP Phone" devices (SIP-TLS + SRTP) — one per number you want
tested — and dials out through them. No Webex API integration is needed.
See [`webex-calling/README.md`](webex-calling/README.md) for setup and
[`webex-calling/DESIGN.md`](webex-calling/DESIGN.md) for the design.

> **Note:** Webex sets outbound caller ID from the user's/workspace's
> configuration, so each test number needs its own Webex device/line.

### FreeSWITCH

For customers running their own FreeSWITCH. The agent connects to ESL
(Event Socket) and originates calls with a randomized 35-55s hold. See
[`installation-guide.md`](installation-guide.md#freeswitch) for env
vars and setup.

### Proprietary

For customers who originate calls through their own network
infrastructure. Implement one Python function
(`providers/proprietary_provider.py::place_call`) and the agent handles
everything else. See
[`CUSTOM_PROVIDER_GUIDE.md`](CUSTOM_PROVIDER_GUIDE.md).

---

## Docker Commands

| Command | Description |
|---------|-------------|
| `docker compose up -d --build` | Start or rebuild |
| `docker compose --profile ringcentral up -d --build` | Start with RingCentral sidecar |
| `docker compose down` | Stop |
| `docker compose restart` | Restart (picks up `.env` changes) |
| `docker logs -f bullseye-agent` | Tail logs |
| `docker logs --tail 100 bullseye-agent` | Last 100 lines |

---

## Running from Source (Python)

```bash
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python3 main.py
```

For production use, run under a process supervisor so it restarts after
reboots. Example `systemd` unit (`/etc/systemd/system/bullseye-agent.service`):

```ini
[Unit]
Description=Bullseye Agent
After=network-online.target

[Service]
Type=simple
User=bullseye
WorkingDirectory=/opt/bullseye/agent
EnvironmentFile=/opt/bullseye/agent/.env
ExecStart=/opt/bullseye/agent/.venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bullseye-agent
sudo journalctl -u bullseye-agent -f
```

---

## Configuration

Set `LOG_LEVEL` in `.env` to control verbosity:

| Level | What you see |
|-------|-------------|
| `INFO` (default) | Connection status, test start/result |
| `DEBUG` | Per-poll status updates, event details |
| `WARNING` | Errors and reconnections only |

---

## Troubleshooting

**First stop for connectivity issues: `net-check`.** The image bundles a
canned diagnostic that probes the network path from the agent container
to the Bullseye server and to the configured telephony provider. Run it
inside a running container:

```bash
# Docker
docker exec bullseye-agent net-check

# Kubernetes / EKS
kubectl exec -it deployment/bullseye-agent -- net-check
```

Prints PASS/FAIL per stage (DNS → TCP → TLS → HTTP). Output has no
credentials; safe to paste into a support ticket. See the install guide
for a full breakdown of what each failure typically means.

Related: at startup, every provider runs a preflight check against its
API. If the startup banner shows `Provider: <name> (preflight FAILED)`,
the reason line explains what's wrong (usually a firewall, wrong
credentials, or a TLS-inspecting proxy).

**"Invalid API key" / connection closes with code 4003**
Double-check `BULLSEYE_API_KEY` in `.env` — it should start with `bse_`.

**Agent keeps reconnecting**
- Verify the server is reachable: `curl -v $BULLSEYE_SERVER_URL/docs`
- Confirm the URL uses `https://`
- Check that outbound port 443 is not blocked by a firewall

**"Error: Unsupported provider"**
`TELEPHONY_PROVIDER` must be `bandwidth`, `twilio`, `telnyx`, `ringcentral`,
`asterisk`, `webex`, `freeswitch`, or `proprietary` (lowercase).

**Twilio: authentication errors**
Use the primary Account SID + Auth Token from the Twilio Console, not an
API key (`SK...`).

**Bandwidth: calls fail immediately**
Verify your Voice Application has an `answerUrl` configured (or that
`BANDWIDTH_ANSWER_URL` in `.env` is set to the Bullseye server endpoint).

**Telnyx: events are delayed**
Expected. Telnyx call events have a 15–20 second delay before appearing in
the events API. The call still completes normally.

**RingCentral: sidecar won't start**
- Docker: make sure you used `--profile ringcentral`
- Python: verify Node.js 18+ is installed (`node --version`)
- Check that an "Other Phone" device exists on the extension

**RingCentral: "No OtherPhone device found"**
Create one in the RingCentral Admin Portal → Phones & Devices → Add Device →
Existing Phone.

**Calls aren't being placed**
Check the logs for errors after the test arrives. Common causes: the "from"
number isn't assigned to your provider account, or the account lacks outbound
voice capability.

---

## Security

Bullseye runs entirely on your infrastructure and uses your existing telecom
credentials to place test calls. Your API keys, tokens, and account IDs
never leave your environment. The only information sent to ARMOR is the
call status (e.g., spam label, blocked, delivered) and the phone number being
tested. No call content or recordings are ever collected. All connections are
encrypted with TLS and Armor Solutions is SOC 2 Type II compliant. You can
remove Bullseye from your environment at any time.

### Additional details

**Agent isolation.** Each agent authenticates with its own API key and only
receives tests addressed to its agent ID. Agents cannot see other agents'
tests.

**`.env` protection.** Your `.env` file contains secrets. Restrict access
with `chmod 600 .env`. The included `.gitignore` and `.dockerignore` prevent
it from being committed or baked into the Docker image.

**Log retention.** Logs include phone numbers, which may be PII. Docker log
rotation is configured at 10 MB x 5 files by default.

---

## Support

Contact your Bullseye representative with:
- Agent version (printed at startup)
- The last 50 lines of logs
- The first 12 characters of your API key (e.g. `bse_a1b2c3d4`)
