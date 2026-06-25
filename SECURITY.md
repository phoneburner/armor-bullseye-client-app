# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in the Bullseye agent, please
**do not** open a public issue. Email a description of the issue to:

**paul@phoneburner.com**

Include:

- A clear description of the vulnerability and its impact.
- Steps to reproduce, ideally with a minimal proof of concept.
- The agent version (`__version__` in `main.py`, or shown in the startup banner).
- Your name and how you would like to be credited (optional).

## What to Expect

- **Acknowledgment** within 2 business days.
- **Initial assessment** with severity and rough remediation timeline within 7
  business days.
- **Coordinated disclosure** once a fix is available. We will credit reporters
  who request it.

## Scope

In scope:

- The Bullseye agent code in this repository (`main.py`, the `providers/`
  directory, the `ringcentral-sidecar/` and `sbc-asterisk/` deployments).
- The Docker and Python distributions of the agent.

Out of scope (please don't test against these):

- The Bullseye server itself — we manage that.
- Third-party telephony provider APIs (Twilio, Bandwidth, Telnyx, RingCentral) —
  please report findings to those providers directly.
- Issues that require local code execution as the agent's user (e.g., reading
  `.env` from disk) are not vulnerabilities; protect that file with normal OS
  permissions.

## Responsible Disclosure

Please give us a reasonable window (typically 90 days) to investigate and ship
a fix before publicly disclosing. We will work with you in good faith.
