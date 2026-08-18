# Bullseye × Cisco Webex Calling — Design Spec

Status: **proposed / lab-untested** (2026-08-18)
Owner: Armor (Bullseye)
Scope: `armor-bullseye-client-app` (agent) only. No server changes.

## 1. Goal

Let a customer whose phone system is **Cisco Webex Calling** run Bullseye
spam tests through it, so the test calls originate from their real Webex
lines/numbers and traverse Webex's actual PSTN path — the thing carriers
and call-blocking apps are scoring.

Non-goals: Webex Contact Center, Webex Meetings dial-out, inbound call
handling, per-call caller-ID spoofing.

## 2. How Webex Calling can originate a call (options considered)

| # | Mechanism | Legs | Verdict |
|---|-----------|------|---------|
| 1 | REST Call Control `POST /v1/telephony/calls/dial` | **2** — "alerts on all the devices belonging to the user… when a user answers an alerting device, an outbound call is placed from that device to the destination" | Rejected. Same dead end as RingCentral RingOut: something must answer leg 1. Also needs a user-scoped OAuth token + refresh handling, and status via `telephony_calls` webhooks (person-level only, needs a public URL). |
| 2 | **Customer-Managed Device → "Generic SIP Phone"** | **1** — our SIP UA registers as one of the user's phones and sends the INVITE itself | **Chosen.** Control Hub hands out a SIP username, password and outbound proxy for any SIP-TLS-1.2-compliant UA. Real-time 180/200/486/487 signalling; no Webex API in the call path. |
| 3 | Webex Web Calling SDK (`@webex/calling`) | 1 | Rejected. WebRTC + `localAudioStream` ⇒ needs a (headless) browser; fragile, heavy. |
| 4 | Local Gateway registration-based trunk | 1 | Rejected. Site-wide trunk that participates in the location's routing; wrong blast radius for a test agent. |

Webex-side facts that shape the design (from Cisco docs, see §9):

- SIP **must** be over TLS 1.2+; media **must** be SRTP
  (`AES_CM_128_HMAC_SHA1_80`). Plain UDP/TCP SIP is refused.
- Endpoints discover the proxy via DNS SRV on `_sips._tcp.<host>`
  (Control Hub says "Port `0`, Transport `TLS`" for third-party devices).
- **Outbound caller ID is the Webex user's/workspace's configured caller
  ID** (Direct line / Location number / Custom), *not* the SIP `From`
  header. ⇒ Bullseye cannot vary `from_number` per call on one device.
- Cisco TAC explicitly does not troubleshoot generic customer-managed
  devices. We own support.

## 3. Architecture

```
Bullseye server ──WS──► bullseye-agent ──ARI (localhost:8088)──► bullseye-asterisk ──SIP-TLS/SRTP──► Webex Calling ──► PSTN
                        (WebexProvider)                          (one PJSIP registration per Webex line)
```

Reuses the existing `sbc-asterisk/` shape: the agent container plus an
Asterisk sidecar on `network_mode: host`, driven over ARI by the existing
event-driven `AsteriskProvider`. The Webex work is:

1. A new deployment directory `webex-calling/` (compose, `.env.example`,
   Asterisk templates, entrypoint, README) — TLS transport, SRTP, and an
   **outbound registration per configured line**.
2. A thin `WebexProvider(AsteriskProvider)` that maps the test's
   `from_number` to the matching registered line's PJSIP endpoint and
   refuses numbers that aren't configured (`invalid_from_number`).
3. Docs + `TELEPHONY_PROVIDER=webex`.

### 3.1 Line model

A **line** = one Webex Generic SIP Phone device = one caller-ID number.
The customer creates one device per number they want tested and gives us
its credentials. Configuration is positional env vars shared by both
containers via `.env`:

```
WEBEX_SIP_DOMAIN=<Server / registrar domain from Control Hub>
WEBEX_OUTBOUND_PROXY=<Outbound Proxy from Control Hub>

WEBEX_LINE_1_NUMBER=+15551234567
WEBEX_LINE_1_SIP_USERNAME=<from Control Hub>
WEBEX_LINE_1_SIP_PASSWORD=<from Control Hub>

WEBEX_LINE_2_NUMBER=+15557654321
WEBEX_LINE_2_SIP_USERNAME=…
WEBEX_LINE_2_SIP_PASSWORD=…
```

Both the sidecar entrypoint and the provider derive the same PJSIP object
name from the number: `webex-<E.164 digits>` (e.g. `webex-15551234567`).
That is the only coupling between the two containers.

### 3.2 Asterisk (sidecar) configuration

Rendered by `webex-calling/asterisk/entrypoint.sh` at container start:

- `[transport-tls]` — `protocol=tls`, `bind=0.0.0.0:5061`,
  `method=tlsv1_2`, `verify_server=yes` against the container's CA
  bundle (`WEBEX_TLS_VERIFY_SERVER=no` + `WEBEX_CA_BUNDLE` for lab
  troubleshooting). No client certificate — Webex generic devices use
  digest auth.
- Per line: `endpoint` (`media_encryption=sdes`, ulaw/alaw, `from_user`
  = SIP username, `outbound_proxy=sip:<proxy>\;transport=tls`, NAT
  helpers `rtp_symmetric/force_rport/rewrite_contact`), `auth`, `aor`
  (contact = `sip:<user>@<domain>\;transport=tls`, no qualify — Webex
  proxies don't need OPTIONS pings and some drop them), and
  `registration` (`server_uri=sip:<domain>\;transport=tls`,
  `client_uri=sip:<user>@<domain>`, `expiration=300` so NAT bindings
  stay warm, `line=yes` so inbound requests map to the endpoint,
  unlimited retries so a Webex maintenance blip self-heals).
- Optional `WEBEX_EXTERNAL_IP` → `external_media_address` /
  `external_signaling_address` for hosts behind NAT where symmetric RTP
  isn't enough.
- No port in URIs by default ⇒ PJSIP does the `_sips._tcp` SRV lookup
  Cisco requires. `WEBEX_SIP_PORT` overrides (e.g. `5061`) if a
  customer's DNS blocks SRV.
- Dialplan, ARI, HTTP, modules: identical to `sbc-asterisk/` (landing pad
  answers, holds `RAND(35,55)`, hangs up).

### 3.3 Provider (`providers/webex_provider.py`)

- Subclass of `AsteriskProvider`; all ARI/event logic is inherited.
- `__init__`: reads `WEBEX_LINE_N_NUMBER` for N = 1… until the first gap;
  normalizes to E.164; builds `{e164 → "webex-<digits>"}`. Fails fast on
  zero lines or duplicate numbers. ARI settings default to the sidecar's
  loopback values so the customer only fills in `ARI_PASSWORD`.
- `place_call`: normalize `from_number`; if it isn't a configured line →
  `CallResult(status="failed", error_category="invalid_from_number")`
  **without touching Asterisk**. Otherwise dial
  `PJSIP/<to>@webex-<digits>` via the parent.
- `to_number` is sent as `+E.164` (Webex accepts `+12223334444` per its
  own examples). `WEBEX_DIAL_PREFIX` / `WEBEX_STRIP_PLUS` exist for
  locations whose dial plan needs national or prefixed dialing.
- `preflight`: parent ARI check, then `GET /ari/endpoints/PJSIP/<name>`
  for every configured line — proves the sidecar rendered the same lines
  the agent thinks exist (catches `.env` drift between the containers).
  Registration *state* is not exposed via ARI; the README documents
  `pjsip show registrations` for that.

### 3.4 Result mapping

Unchanged from `AsteriskProvider`: `ChannelStateChange: Up` ⇒ answered;
Q.850 17 ⇒ busy; 16/18/19 without answer ⇒ no_answer; else failed.

## 4. Customer onboarding

Per number to test:

1. Control Hub → Management → Devices → Add device → *Existing user* (or a
   Workspace) → Cisco IP Phone → **Customer Managed Device** → vendor
   **Generic SIP Phone**. Any MAC is accepted for the generic profile.
2. Save the SIP username/password (shown once) and note the Outbound
   Proxy / Server domain.
3. Set that user's/workspace's **outgoing caller ID** to the number under
   test (Calling → user → Caller ID) and confirm outbound PSTN permissions
   for the location.
4. Put the values in `webex-calling/.env`; `docker compose up -d --build`.

We give them `BULLSEYE_SERVER_URL` + `BULLSEYE_API_KEY` as usual.

## 5. Security

- Credentials stay in the customer's `.env`; TLS to Webex is verified
  against public CAs by default; SRTP is mandatory (no
  `media_encryption_optimistic`).
- ARI stays bound to `127.0.0.1` inside host networking; agent →
  Asterisk never leaves the host.
- Same sanitized-error contract as every provider: raw SIP/ARI errors go
  to local logs, only `ERROR_CATEGORIES` strings go to the server.
- Webex holds the customer liable for fraud via leaked SIP credentials
  (their words). README calls this out and tells them to scope the device
  to a workspace/user with the narrowest calling permissions that still
  reach the numbers they test.

## 6. Open questions / risks (must be answered in the lab pass)

| # | Question | Plan |
|---|----------|------|
| R1 | Does Asterisk PJSIP register cleanly to Webex (SRV, TLS, SRTP offer)? | Lab against a customer-provided test user. Fallbacks already wired: `WEBEX_SIP_PORT=5061`, `WEBEX_TLS_VERIFY_SERVER=no` for diagnosis only. |
| R2 | How many concurrent calls does one registered generic device allow? Webex App is one call per line; hardware phones get multiple appearances. | Measure. If low, document `BULLSEYE_MAX_CONCURRENT_CALLS` ≈ lines × per-line limit; scale by adding devices (a Pro workspace allows up to 5). |
| R3 | Does Webex honour `+E.164` in the INVITE Request-URI for this location's dial plan? | Lab. `WEBEX_DIAL_PREFIX`/`WEBEX_STRIP_PLUS` cover national/prefixed plans. |
| R4 | Does the caller ID presented to the PSTN match `WEBEX_LINE_N_NUMBER`? | It's the user's configured CLI, so verify each line once at onboarding. Provider can't detect a mismatch. |
| R5 | Registration health isn't visible to the agent/server. | Follow-up: expose `pjsip show registrations` via a tiny health script or AMI, and surface it in preflight. |
| R6 | Cisco won't support this path. | Accepted; same posture as our SBC/FreeSWITCH/proprietary providers. |

## 7. Out of scope / follow-ups

- Registration-state health in preflight and in `/admin/status` (R5).
- Managing many lines: a `webex-lines.csv` mount instead of positional env
  vars once a customer needs > ~10 numbers.
- Digest-pinning the Asterisk image (tracked already for `sbc-asterisk/`).

## 8. Effort

~2–4 engineer-days including the lab pass, once we have a Webex Calling
test user. Code in this PR is the full first cut; what remains is
running it against a real org and closing R1–R4.

## 9. References

- Call Controls – Dial: https://developer.webex.com/docs/api/v1/call-controls/dial
- Add your customer managed device: https://help.webex.com/en-us/article/nemh93t/Add-your-customer-managed-device
- Security requirements for Webex Calling: https://help.webex.com/en-us/article/jwkbt2/Security-requirements-for-Webex-Calling
- Person caller-ID settings: https://developer.webex.com/docs/api/v1/user-call-settings/read-caller-id-settings-for-a-person
- Webex Web Calling SDK: https://developer.webex.com/docs/sdks/webex-calling-sdk-web-incoming-outgoing-calls
- Local Gateway trunks: https://help.webex.com/article/n0xb944/Configure-Trunks-Route-Groups-and-Dial-Plans-for-Cisco-Webex-Calling
