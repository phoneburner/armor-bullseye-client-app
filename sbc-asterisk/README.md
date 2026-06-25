# Bullseye Agent — SBC (Session Border Controller) Deployment

Use this deployment shape when your telecom infrastructure originates
calls through your own SBC and you want Bullseye's agent to send SIP
INVITEs directly to it. The agent is bundled with a small embedded
Asterisk container that holds the SIP trunk to your SBC; you don't need
Twilio/Bandwidth/Telnyx accounts.

```
Bullseye agent  ──(ARI: localhost:8088)──►  Asterisk sidecar  ──(SIP)──►  Your SBC  ──►  PSTN
```

## What you need

From your telecom / SBC team:

- **SBC hostname or IP** (`SBC_HOST`)
- **Signaling port and transport** — UDP/TCP/TLS (`SBC_PORT`, `SBC_TRANSPORT`)
- **Authentication method** — IP whitelist (just our host's IP) or SIP
  digest (username + password)
- **Identity** — what From-user/From-domain the SBC expects
  (`SBC_FROM_USER`, `SBC_FROM_DOMAIN`)
- **Firewall**: whitelist this host's IP for SIP signaling on the
  configured port, plus the RTP range UDP 10000-10999

From Armor:

- `BULLSEYE_SERVER_URL` — typically `https://your-bullseye-server`
- `BULLSEYE_API_KEY` — your agent's API key, starts with `bse_`

## Setup

1. **Get the files.** This directory (`sbc-asterisk/`) contains
   everything you need: compose file, Asterisk config templates, env
   example. Sits next to the regular agent code at `agent/`.

2. **Configure your `.env`:**
   ```bash
   cp .env.example .env
   nano .env
   ```
   Fill in the SBC section + the BULLSEYE_API_KEY. Pick a strong
   `ARI_PASSWORD` (it only needs to be reachable on localhost).

3. **Bring up both containers:**
   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```

   On startup you should see:
   - `bullseye-asterisk`: `Asterisk Ready` and the rendered configs
     listed
   - `bullseye-agent`: ASCII bullseye banner, then `Connected to
     Bullseye server`

4. **Smoke test the SBC trunk** from the Asterisk container before
   running real Bullseye tests:
   ```bash
   docker exec bullseye-asterisk asterisk -rx "pjsip show endpoints"
   ```
   You should see `sbc` listed with state `Unavailable` (if the SBC
   doesn't respond to OPTIONS pings) or `Not in use` (if it does).
   Either is fine for outbound calls.

   Then try an originate:
   ```bash
   docker exec bullseye-asterisk asterisk -rx \
     "originate PJSIP/+15551234567@sbc application Wait 10"
   ```
   Replace the number with one you can answer. If your phone rings,
   you're done.

5. **Tell Armor** the agent is up — we'll send a real Bullseye test
   through and confirm the round trip.

## Operations

| Action | Command |
|--------|---------|
| Tail agent logs | `docker compose logs -f bullseye-agent` |
| Tail Asterisk logs | `docker compose logs -f bullseye-asterisk` |
| Asterisk CLI | `docker exec -it bullseye-asterisk asterisk -r` |
| Restart Asterisk only | `docker compose restart bullseye-asterisk` |
| Restart agent only | `docker compose restart bullseye-agent` |
| Reload config (after .env change) | `docker compose down && docker compose up -d --build` |
| Check current SBC reachability | `docker exec bullseye-asterisk asterisk -rx "pjsip show endpoints"` |

## Notes & caveats

- **`network_mode: host` is required on Linux.** SIP and RTP do not
  play well with Docker's NAT, especially when the SBC needs to send
  RTP back to a specific IP. Host networking sidesteps the entire
  problem.
- **macOS / Windows hosts**: `network_mode: host` is broken on
  Docker Desktop. This compose file as-is won't work — you'd need to
  expose ports explicitly and configure NAT in Asterisk. For
  production, deploy on Linux.
- **RTP port range**: hardcoded to UDP 10000-10999. Your firewall must
  allow these from this host out to the SBC (and back).
- **TLS to the SBC**: the template has TLS commented out. If your SBC
  requires SIPS, uncomment the `transport-tls` section in
  `asterisk/templates/pjsip.conf.tmpl`, mount your cert/key files into
  the container, and set `SBC_TRANSPORT=tls` + `SBC_PORT=5061`.
- **What this is not**: not a generic carrier-grade SIP setup. We've
  picked specific tradeoffs (ulaw/alaw codecs only, no media
  transcoding, fixed RTP range, no NAT handling). Works well for the
  testing use case; not designed for production call volume.

## Troubleshooting

| Symptom | Likely cause |
|---------|-------------|
| `bullseye-agent` logs `Call initiation failed` | Look at Asterisk logs — usually a SIP-level issue (rejected by SBC, auth failure, codec mismatch) |
| Asterisk: `403 Forbidden` from SBC | Your IP isn't whitelisted, or digest credentials wrong |
| Asterisk: `408 Request Timeout` | SBC not reachable. Check `SBC_HOST`/`SBC_PORT` and firewall |
| Asterisk: `488 Not Acceptable Here` | Codec mismatch. Most SBCs require ulaw or alaw; both are configured by default |
| Phone rings but never gets answered state | RTP not flowing. Check firewall allows UDP 10000-10999 |
| `connected: 0` in `/admin/status` | Agent can't reach Bullseye server. Check `BULLSEYE_SERVER_URL` and outbound 443 |
