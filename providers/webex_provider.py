"""Cisco Webex Calling provider.

Webex Calling has no one-legged "place a call from this number" API — its
REST dial endpoint is click-to-dial (rings the user's phones first). What
Webex *does* offer is customer-managed generic SIP devices: Control Hub
hands out a SIP username/password/outbound-proxy for any SIP-TLS UA, and
that UA becomes one of the user's phones. So we register the bundled
Asterisk sidecar (see webex-calling/) as one such device per number the
customer wants tested, and dial straight out over SIP.

This class is a thin layer over AsteriskProvider (all ARI / event logic is
inherited). It adds:

  * Line mapping — the test's from_number selects which registered Webex
    line (PJSIP endpoint) to dial from. Webex sets outbound caller ID from
    the *user's* configuration, not from our SIP From header, so a number
    can only be tested through the device that belongs to it.
  * A hard refusal (invalid_from_number) for from_numbers that aren't
    configured, without touching Asterisk.
  * Sidecar-friendly defaults for the ASTERISK_* settings so the customer
    only has to fill in the WEBEX_* variables and ARI_PASSWORD.
  * Preflight that checks every configured line exists on the Asterisk
    side, catching .env drift between the two containers.

Environment variables (see webex-calling/.env.example):

    WEBEX_LINE_<N>_NUMBER         E.164 number of line N (N = 1, 2, ...)
    WEBEX_LINE_<N>_SIP_USERNAME   Control Hub SIP username (used by the
    WEBEX_LINE_<N>_SIP_PASSWORD   sidecar only; the agent ignores them)
    WEBEX_STRIP_PLUS              yes/no — send 1222... instead of +1222...
    WEBEX_DIAL_PREFIX             optional digits prepended to the destination
    ARI_PASSWORD                  shared with the sidecar's ari.conf
    ASTERISK_ARI_URL / _USERNAME / _PASSWORD / _CONTEXT / _EXTENSION /
    ASTERISK_DIAL_TIMEOUT         optional overrides (sidecar defaults apply)
"""

import logging
import os
import re

import requests

from providers.asterisk_provider import AsteriskProvider
from providers.base import CallResult, CallEventCallback

log = logging.getLogger(__name__)

# Keep in sync with webex-calling/asterisk/entrypoint.sh, which derives the
# same PJSIP object names from the same WEBEX_LINE_<N>_NUMBER values.
ENDPOINT_PREFIX = "webex-"


def normalize_e164(number: str) -> str | None:
    """Reduce a phone number to '+<digits>'; 10 digits are assumed NANP.

    Mirrors the sidecar's shell normalisation exactly (tr -cd '0-9', then
    prepend 1 for a 10-digit number). Returns None if there are no digits.
    """
    digits = re.sub(r"\D", "", number or "")
    if not digits:
        return None
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits


def endpoint_name_for(number: str) -> str:
    return ENDPOINT_PREFIX + normalize_e164(number)[1:]


def load_lines(env: dict | None = None) -> dict[str, str]:
    """Return {e164_number: pjsip_endpoint_name} from WEBEX_LINE_<N>_NUMBER.

    Numbering must be consecutive from 1; the first missing N ends the
    list (same rule the sidecar uses, so both sides see the same lines).
    """
    env = os.environ if env is None else env
    lines: dict[str, str] = {}
    n = 1
    while True:
        raw = env.get(f"WEBEX_LINE_{n}_NUMBER", "").strip()
        if not raw:
            break
        e164 = normalize_e164(raw)
        if e164 is None:
            raise ValueError(f"WEBEX_LINE_{n}_NUMBER={raw!r} contains no digits")
        if e164 in lines:
            raise ValueError(f"WEBEX_LINE_{n}_NUMBER duplicates an earlier line ({e164})")
        lines[e164] = ENDPOINT_PREFIX + e164[1:]
        n += 1
    return lines


class WebexProvider(AsteriskProvider):
    def __init__(self):
        # Sidecar defaults. Explicit ASTERISK_* values in the environment
        # still win, so a customer with an external Asterisk can point at it.
        os.environ.setdefault("ASTERISK_ARI_URL", "http://127.0.0.1:8088/ari")
        os.environ.setdefault("ASTERISK_ARI_USERNAME", "bullseye")
        if not os.environ.get("ASTERISK_ARI_PASSWORD"):
            ari_pw = os.environ.get("ARI_PASSWORD")
            if not ari_pw:
                raise ValueError("ARI_PASSWORD (or ASTERISK_ARI_PASSWORD) is required for the Webex provider")
            os.environ["ASTERISK_ARI_PASSWORD"] = ari_pw
        os.environ.setdefault("ASTERISK_CONTEXT", "bullseye-landing")
        # Not used (we override _build_endpoint) but AsteriskProvider requires it.
        os.environ.setdefault("ASTERISK_ENDPOINT_TEMPLATE", "PJSIP/{to_number}@webex")
        super().__init__()

        self.lines = load_lines()
        if not self.lines:
            raise ValueError(
                "No Webex lines configured — set WEBEX_LINE_1_NUMBER / "
                "WEBEX_LINE_1_SIP_USERNAME / WEBEX_LINE_1_SIP_PASSWORD"
            )
        self.strip_plus = os.environ.get("WEBEX_STRIP_PLUS", "no").strip().lower() in ("1", "yes", "true")
        self.dial_prefix = os.environ.get("WEBEX_DIAL_PREFIX", "").strip()
        log.info("Webex lines configured: %s", ", ".join(self.lines))

    # ── line selection ────────────────────────────────────────────────

    def _format_destination(self, to_number: str) -> str:
        e164 = normalize_e164(to_number)
        if e164 is None:
            raise ValueError("destination has no digits")
        dest = e164[1:] if self.strip_plus else e164
        return f"{self.dial_prefix}{dest}"

    def _build_endpoint(self, from_number: str, to_number: str) -> str:
        e164_from = normalize_e164(from_number)
        endpoint = self.lines.get(e164_from) if e164_from else None
        if endpoint is None:
            # place_call() checks this first and never gets here; guard anyway.
            raise KeyError(from_number)
        return f"PJSIP/{self._format_destination(to_number)}@{endpoint}"

    def place_call(
        self,
        from_number: str,
        to_number: str,
        on_event: CallEventCallback | None = None,
    ) -> CallResult:
        e164_from = normalize_e164(from_number)
        if e164_from is None or e164_from not in self.lines:
            log.warning(
                "Refusing test: from_number %s is not a configured Webex line (have: %s)",
                from_number, ", ".join(self.lines),
            )
            return CallResult(
                status="failed",
                duration=0.0,
                error_category="invalid_from_number",
                error_message=(
                    "From number is not one of the Webex lines configured on "
                    "this agent (WEBEX_LINE_<N>_NUMBER)."
                ),
            )
        return super().place_call(from_number, to_number, on_event)

    # ── preflight ─────────────────────────────────────────────────────

    def preflight(self) -> None:
        # ARI reachability + credentials.
        super().preflight()
        # Every line the agent knows about must exist as a PJSIP endpoint in
        # the sidecar; otherwise the two containers rendered different
        # WEBEX_LINE_* sets (stale .env, typo, restart of only one side).
        # ARI cannot tell us whether the *registration* to Webex succeeded —
        # use `pjsip show registrations` in the Asterisk container for that.
        missing = []
        for number, endpoint in self.lines.items():
            r = requests.get(
                f"{self.ari_url}/endpoints/PJSIP/{endpoint}",
                auth=self.auth, timeout=5,
            )
            if r.status_code == 404:
                missing.append(f"{number} ({endpoint})")
            else:
                r.raise_for_status()
        if missing:
            raise RuntimeError(
                "Webex lines configured on the agent but absent in Asterisk: "
                + ", ".join(missing)
                + " — restart both containers so they read the same .env"
            )
