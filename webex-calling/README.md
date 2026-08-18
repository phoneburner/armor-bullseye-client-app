# Bullseye Agent — Cisco Webex Calling Deployment

Use this deployment shape when your phone system is **Cisco Webex
Calling** and you want Bullseye's test calls to originate from your real
Webex numbers. The agent is bundled with a small embedded Asterisk
container that registers to Webex Calling as one or more
**customer-managed "Generic SIP Phone" devices** — the same mechanism
Webex uses for third-party desk phones, ATAs and paging adapters. No
Twilio/Bandwidth/Telnyx account is needed, and no Webex API integration
or OAuth app is involved.

```
Bullseye agent ──(ARI: localhost:8088)──► Asterisk sidecar ──(SIP-TLS + SRTP)──► Webex Calling ──► PSTN
                                          one registration per Webex line
```

> **Status:** first release — validated in design against Cisco's
> published requirements for third-party SIP devices, and pending a live
> lab pass against a Webex Calling org. See `DESIGN.md` for the full
> spec and the open questions we will close during that pass.

## How it works, in one paragraph

Webex Calling has no API to "place a call from number X" in one step (its
REST *dial* endpoint rings your phones first and then dials out — click-to-dial).
It does, however, let an admin add a generic SIP device to a user or
workspace and hands back a SIP username/password/outbound proxy. Our
Asterisk registers with those credentials, so to Webex it *is* one of
that user's phones. When Bullseye sends a test, the agent tells Asterisk
to dial the destination through the registration that belongs to the
test's "from" number; Webex places the call to the PSTN with that user's
configured caller ID. Answered / busy / no-answer come straight back over
SIP in real time.

## What you need

From your Webex administrator (Control Hub), **per phone number you want
tested**:

1. A user (Webex Calling Professional license) or a workspace whose
   **outgoing caller ID** is set to that number
   (Calling → user → Caller ID → *Direct line* or *Custom*).
2. A customer-managed device on that user/workspace:
   **Management → Devices → Add device → Existing user (or Workspace) →
   Cisco IP Phone → Customer Managed Device → vendor: *Generic SIP Phone*.**
   Any MAC address is accepted for the generic profile.
3. From the confirmation screen (shown once — export the CSV):
   - **SIP Username**
   - **SIP Password**
   - **Outbound Proxy** and the SIP **Server/domain**
     (these two are the same for every device in your org)
4. Outbound PSTN calling permitted for that user's location, for the
   destinations you will test.

Network:
- Outbound **TCP 5061** (SIP over TLS) from this host to the Webex proxy,
  plus DNS SRV lookups (`_sips._tcp.<proxy>`).
- Outbound **UDP 10000-10999** for SRTP media, and return traffic.
- No inbound port openings are required.

From Armor:
- `BULLSEYE_SERVER_URL`
- `BULLSEYE_API_KEY` (starts with `bse_`)

A **Linux host with Docker** (Compose v2). `network_mode: host` is
required — SIP/RTP do not survive Docker's NAT — so Docker Desktop on
macOS/Windows will not work for this stack.

## Setup

1. **Get the files.** This directory (`webex-calling/`) sits next to the
   regular agent code at `agent/`; both must be present.

2. **Configure `.env`:**
   ```bash
   cp .env.example .env
   nano .env
   ```
   Fill in the Bullseye section, the Webex SIP domain/proxy, one
   `WEBEX_LINE_<N>_*` block per number, and a strong `ARI_PASSWORD`.
   Line numbering must be consecutive from 1.

3. **Bring up both containers:**
   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```
   On startup you should see:
   - `bullseye-asterisk`: `Configured Webex line 1: +1555... -> webex-1555...`
     for each line, then `Asterisk Ready`
   - `bullseye-agent`: the ASCII bullseye banner, `Provider: webex (preflight OK)`,
     then `Connected to Bullseye server`

4. **Confirm the registrations to Webex:**
   ```bash
   docker exec bullseye-asterisk asterisk -rx "pjsip show registrations"
   ```
   Every line should show `Registered`. If a line shows `Rejected` or
   `Unregistered`, see Troubleshooting below.

5. **Smoke test** from the Asterisk container before running real
   Bullseye tests (replace with a number you can answer, and with your
   line's endpoint name from step 3):
   ```bash
   docker exec bullseye-asterisk asterisk -rx \
     "channel originate PJSIP/+15551234567@webex-15559876543 application Wait 10"
   ```
   If your phone rings and shows the Webex line's caller ID, you're done.

6. **Tell Armor** the agent is up — we'll send a Bullseye test through
   and confirm the round trip.

## Concurrency

Set `BULLSEYE_MAX_CONCURRENT_CALLS` to at most
*(number of lines) × (calls a Webex generic device may carry at once)*.
Cisco doesn't publish the second number for generic devices; start with
`2` per line and raise it once you've watched a batch. If tests queue up
behind the limit you'll see the server report rising dial latency for
this agent. To scale, add more devices (a Professional workspace can hold
up to five customer-managed devices) rather than pushing one line harder.

## Operations

| Action | Command |
|--------|---------|
| Tail agent logs | `docker compose logs -f bullseye-agent` |
| Tail Asterisk logs | `docker compose logs -f bullseye-asterisk` |
| Registration status | `docker exec bullseye-asterisk asterisk -rx "pjsip show registrations"` |
| Endpoints / lines | `docker exec bullseye-asterisk asterisk -rx "pjsip show endpoints"` |
| Asterisk CLI | `docker exec -it bullseye-asterisk asterisk -r` |
| Reload config after `.env` change | `docker compose down && docker compose up -d --build` (both containers must restart — they read the same `.env`) |
| Add a line | Append `WEBEX_LINE_<N+1>_*` to `.env`, then the reload above |
| Rotate a SIP password | Control Hub → device → *Reset password*; update `.env`; reload |

## Troubleshooting

| Symptom | Likely cause |
|---------|-------------|
| Asterisk: `ERROR: WEBEX_SIP_DOMAIN is required` / `no Webex lines configured` | `.env` incomplete — see `.env.example` |
| Registration `Rejected` (401/403) | SIP username/password wrong, or the device was deleted/reset in Control Hub |
| Registration `Unregistered`, log shows DNS/`Unable to resolve` | Host can't resolve the proxy or its `_sips._tcp` SRV record. Try `WEBEX_SIP_PORT=5061` to bypass SRV; check outbound DNS |
| Registration fails with TLS/certificate errors | Corporate TLS inspection between this host and Webex. Exempt the host; do **not** run with `WEBEX_TLS_VERIFY_SERVER=no` in production |
| Agent: `preflight FAILED … absent in Asterisk` | The two containers read different `WEBEX_LINE_*` sets — restart both |
| Agent result `invalid_from_number` | The test's from-number isn't one of `WEBEX_LINE_<N>_NUMBER`. Add the line (and its Webex device) or fix the number in Bullseye |
| Call gets `403`/`404` from Webex | Destination not permitted by the location's outbound calling settings, or dial format — try `WEBEX_STRIP_PLUS=yes` or a `WEBEX_DIAL_PREFIX` |
| Call is answered but the caller ID shown is wrong | Caller ID is the Webex user's setting, not ours — fix it in Control Hub for that user/workspace |
| Answered state never reached; one-way/no audio | SRTP media blocked. Allow UDP 10000-10999 out and back; if the host is behind strict NAT set `WEBEX_EXTERNAL_IP` |
| Bursty tests wait minutes before dialing | Concurrency cap — see the section above |
| `connected: 0` in `/admin/status` | Agent can't reach the Bullseye server. Check `BULLSEYE_SERVER_URL` and outbound 443 |

Cisco's own note on this provisioning path: *"Cisco Technical Support
doesn't investigate issues with devices connecting through this
provisioning option."* Armor supports the sidecar; Webex-side settings
(licences, caller ID, calling permissions) remain with your Webex admin.

## Security notes

- SIP credentials live only in your `.env` (mode 600 recommended) and in
  the generated `pjsip.conf` inside the Asterisk container. Webex holds
  the customer responsible for fraud committed with leaked SIP
  credentials — scope the device's user/workspace to the narrowest
  outbound calling permissions that still reach the numbers you test.
- TLS to Webex is verified against public CAs; SRTP is mandatory (calls
  fail rather than fall back to clear RTP).
- ARI is bound to `127.0.0.1` inside host networking; nothing on the
  Asterisk side is exposed off-host except the outbound SIP/RTP flows.
- Inbound calls to a test line are answered with an immediate hangup — the
  sidecar is not a phone.
