# AGENTS.md — Guidance for AI Coding Assistants

This file is for AI coding agents (Claude Code, Cursor, Aider, Codex CLI,
GitHub Copilot Workspace, etc.) working in this repository. Read this first
before making changes.

## What this repository is

The Bullseye agent — a small Python service that runs on a customer's machine,
maintains an authenticated WebSocket connection to a Bullseye server, receives
test-call instructions, places real phone calls through a configured telephony
provider, and reports the outcomes back.

The customer-facing entry points are:

| File | Purpose |
|------|---------|
| `main.py` | Agent entry point. WebSocket loop, auth, reconnect with backoff, dispatch to provider. |
| `providers/base.py` | The `TelephonyProvider` ABC. Every provider implements `place_call(from_number, to_number, on_event)`. |
| `providers/*.py` | One file per telephony backend (twilio, bandwidth, telnyx, ringcentral, asterisk, freeswitch, proprietary). |
| `.env.example` | Authoritative list of configuration variables with comments. |
| `Dockerfile`, `docker-compose.yml` | Containerized deployment. |
| `ringcentral-sidecar/` | Node.js SIP softphone — only used when `TELEPHONY_PROVIDER=ringcentral`. |
| `sbc-asterisk/` | Optional Docker-compose stack: the agent + an embedded Asterisk container, for customers who dial through their own SBC. |

Customer-facing docs:

- `README.md` — quick start and operations.
- `installation-guide.md` / `installation-guide.html` — full setup walkthrough.
- `CUSTOM_PROVIDER_GUIDE.md` — for customers writing their own provider.
- `LICENSE`, `SECURITY.md`.

## What you (the agent) are likely being asked to do

The most common reasons a customer points an AI assistant at this repo:

1. **Fill in the `proprietary` provider.** Open
   `providers/proprietary_provider.py`, find the `TODO` block inside
   `place_call`, and replace it with code that calls the customer's
   internal telephony system. Return a `CallResult` with `status` set
   to one of `answered`, `no_answer`, `busy`, or `failed`. See
   `CUSTOM_PROVIDER_GUIDE.md` for a worked example.

2. **Configure or debug an existing provider.** Look at
   `providers/<name>_provider.py`, then `.env.example` for the relevant
   env vars, then the matching section of `installation-guide.md`. Most
   issues are credentials, network reachability, or the dial endpoint
   template being wrong for the customer's setup.

3. **Run the agent locally to see if a change works.** Use
   `python3 main.py` from a virtualenv, or `docker compose up -d --build`
   from the repo root. The startup banner prints `Server: configured`
   (the URL is suppressed at INFO level by design — use
   `LOG_LEVEL=DEBUG` to see the full WebSocket URL).

4. **Add or modify a docker-compose layout** (e.g., the SBC deployment in
   `sbc-asterisk/`). Read that directory's own README first.

## Conventions

- **Python 3.10+** — type hints use the union pipe (`X | Y`) syntax.
- **Dependencies are pinned** in `requirements.txt`. Don't relax pins
  without a reason; don't add new deps without a reason.
- **Docker base image is pinned by digest.** Don't replace it with a
  floating tag.
- **The agent runs as a non-root user (`bullseye`) inside Docker.** Any
  file changes should preserve that.
- **No print statements**, use the `logging` module (`log = logging.getLogger(...)`).
- **Error messages sent over the wire to the Bullseye server are
  sanitized** (generic strings like `"Call initiation failed"`).
  Provider exceptions are logged locally only — see how the existing
  providers do this and follow the same pattern. **Never** pass raw
  exception text to the server; it can contain credentials.

## Hard rules — do not do these things

- **Never commit `.env`.** It contains real credentials. It's already
  in `.gitignore` and `.dockerignore`; don't move it or override that.
- **Never log credentials.** Don't print API keys, OAuth tokens, JWTs,
  passwords, or anything that looks secret. The Twilio SDK has been
  silenced specifically because it would log HTTP requests at INFO; if
  you add a new SDK, check whether it leaks.
- **Never weaken the TLS check.** The agent refuses `ws://` server URLs
  unless `BULLSEYE_ALLOW_INSECURE=1`. That gate exists for local
  development only; don't lower it.
- **Never hardcode server URLs, account IDs, ARNs, or hostnames.** All
  of those come from environment variables. The repo is intentionally
  free of production-server identifiers — keep it that way.
- **Don't add files that look like screenshots, decks, or internal
  documents.** This repo is customer-facing and may be public.

## Testing changes

There is no test suite in this repository (the agent's correctness is
exercised by real phone calls). To verify a change works:

1. Set up a `.env` with valid Bullseye credentials and a real telephony
   provider account (Twilio is fastest for development).
2. Use a phone you can answer as the destination number.
3. Run the agent: `python3 main.py` or `docker compose up -d --build`.
4. Ask whoever provided the Bullseye credentials to dispatch a test
   call to your agent, and watch the logs.

For provider-level changes (e.g., the `proprietary` stub), you can also
construct a small driver script that imports your provider class and
calls `place_call(from_number, to_number, on_event=print)` directly.

## Areas that look simple but aren't

- **The WebSocket reconnect logic.** Don't add retry-storm behavior. The
  current exponential-backoff with a cap is intentional.
- **The `on_event` callback contract.** Events are streamed in real time
  to the Bullseye server. Calling `on_event("answered")` more than once
  per call, or calling it after `place_call` returns, can produce
  duplicate or out-of-order events downstream. Look at how the existing
  providers gate this (`if answered_at is None: ...`).
- **The Bandwidth provider's `answer_url`.** Bandwidth requires a public
  webhook for call control; the URL is on the Bullseye server side, not
  the agent's. Customers fill it in from the value they were given out
  of band.

## Where the source of truth lives

- For env vars and their meaning: **`.env.example`**.
- For the customer setup flow: **`installation-guide.md`**.
- For extending the agent with a new backend: **`CUSTOM_PROVIDER_GUIDE.md`**.
- For everything else: ask the human who pointed you at this repo.
