"""Asterisk telephony provider via ARI (event-driven via WebSocket).

Originates a channel via ARI REST, then watches Asterisk's ARI WebSocket
event stream for ChannelStateChange, ChannelHangupRequest, and
ChannelDestroyed events filtered by the originated channel ID. Maps
Q.850 hangup cause codes to busy / no_answer / failed.

Requires Asterisk 13.1+ (the `subscribeAll` parameter on the ARI
WebSocket subscription was added in 13.1). ARI must be enabled and
HTTP must be bound; see the docstring below for the Asterisk-side
prerequisites.

Asterisk-side prerequisites
---------------------------

1. Enable ARI and HTTP. In `ari.conf`:

       [general]
       enabled = yes

       [bullseye]
       type = user
       read_only = no
       password = <strong-password>
       password_format = plain

   And in `http.conf`:

       [general]
       enabled = yes
       bindaddr = 0.0.0.0
       bindport = 8088

2. Add a landing-pad context in `extensions.conf`:

       [bullseye-landing]
       exten => s,1,Answer()
        same => n,Wait(10)
        same => n,Hangup()

   Then set `ASTERISK_CONTEXT=bullseye-landing` and
   `ASTERISK_EXTENSION=s`.

3. Reload Asterisk: `core reload`.

Environment variables (see .env.example):

    ASTERISK_ARI_URL           e.g. http://asterisk-host:8088/ari
    ASTERISK_ARI_USERNAME      ARI user from ari.conf
    ASTERISK_ARI_PASSWORD      ARI password from ari.conf
    ASTERISK_ENDPOINT_TEMPLATE Dial endpoint template, e.g.:
                                  PJSIP/{to_number}@my-trunk
                                  SIP/my-trunk/{to_number}
                                  Local/{to_number}@outbound-context
    ASTERISK_CONTEXT           Dialplan context for the call to land in
    ASTERISK_EXTENSION         Extension in that context (default: s)
    ASTERISK_DIAL_TIMEOUT      Dial timeout in seconds (default: 30)
"""

import os
import json
import time
import logging
from urllib.parse import urlparse, urlunparse, quote

import requests
from requests.auth import HTTPBasicAuth
from websockets.sync.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from providers.base import TelephonyProvider, CallResult, CallEventCallback

log = logging.getLogger(__name__)

# Q.850 cause codes. We mainly care about distinguishing busy vs no-answer
# vs failed when the channel was never answered. NORMAL_CLEARING (16) without
# a prior ANSWER is treated as no_answer.
_CAUSE_BUSY = {17}                      # USER_BUSY
_CAUSE_NO_ANSWER = {18, 19}             # NO_USER_RESPONSE, NO_ANSWER
_CAUSE_NORMAL = {16}                    # NORMAL_CLEARING


def _derive_ws_url(http_url: str) -> str:
    """Convert http://host:port/ari → ws://host:port/ari/events (preserves https→wss)."""
    p = urlparse(http_url.rstrip("/"))
    scheme = "wss" if p.scheme == "https" else "ws"
    return urlunparse((scheme, p.netloc, p.path + "/events", "", "", ""))


class AsteriskProvider(TelephonyProvider):
    def __init__(self):
        self.ari_url = os.environ["ASTERISK_ARI_URL"].rstrip("/")
        self.username = os.environ["ASTERISK_ARI_USERNAME"]
        self.password = os.environ["ASTERISK_ARI_PASSWORD"]
        self.endpoint_template = os.environ["ASTERISK_ENDPOINT_TEMPLATE"]
        self.context = os.environ["ASTERISK_CONTEXT"]
        self.extension = os.environ.get("ASTERISK_EXTENSION", "s")
        self.dial_timeout = int(os.environ.get("ASTERISK_DIAL_TIMEOUT", "30"))
        self.auth = HTTPBasicAuth(self.username, self.password)
        self.ws_url_base = _derive_ws_url(self.ari_url)

    def place_call(
        self,
        from_number: str,
        to_number: str,
        on_event: CallEventCallback | None = None,
    ) -> CallResult:
        endpoint = self.endpoint_template.format(to_number=to_number)
        channel_id = None
        ws = None
        try:
            # Open the WebSocket BEFORE originating so we don't miss the
            # early ChannelStateChange events (Ringing → Up can happen
            # very fast on local dialplan paths).
            api_key = quote(f"{self.username}:{self.password}", safe="")
            ws_url = (
                f"{self.ws_url_base}"
                f"?api_key={api_key}"
                f"&app=bullseye-monitor"
                f"&subscribeAll=true"
            )
            ws = ws_connect(ws_url, open_timeout=10, close_timeout=5)

            # Originate the channel
            resp = requests.post(
                f"{self.ari_url}/channels",
                auth=self.auth,
                params={
                    "endpoint": endpoint,
                    "extension": self.extension,
                    "context": self.context,
                    "callerId": from_number,
                    "timeout": self.dial_timeout,
                },
                timeout=10,
            )
            resp.raise_for_status()
            channel_id = resp.json()["id"]
            log.info("Asterisk channel originated: %s", channel_id)

            if on_event:
                on_event("dialing", {"provider_call_id": channel_id})

            answered_at = None
            cause = None
            deadline = time.time() + self.dial_timeout + 60

            while time.time() < deadline:
                remaining = max(0.5, deadline - time.time())
                try:
                    raw = ws.recv(timeout=remaining)
                except TimeoutError:
                    continue
                except WebSocketException:
                    log.warning("ARI WebSocket closed unexpectedly")
                    break

                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                # All channel events nest the channel under ev["channel"].
                ch = ev.get("channel") or {}
                if ch.get("id") != channel_id:
                    continue

                ev_type = ev.get("type", "")
                if ev_type == "ChannelStateChange":
                    state = (ch.get("state") or "").lower()
                    if state == "up" and answered_at is None:
                        answered_at = time.time()
                        if on_event:
                            on_event("answered", {"provider_call_id": channel_id})

                elif ev_type == "ChannelHangupRequest":
                    # cause may be int or absent. Asterisk sometimes emits
                    # Hangup before Destroyed; capture it either way.
                    if ev.get("cause") is not None:
                        cause = ev["cause"]

                elif ev_type == "ChannelDestroyed":
                    if cause is None:
                        cause = ev.get("cause")
                    break

            return self._build_result(channel_id, answered_at, cause)

        except Exception:
            log.exception(
                "Asterisk call failed: from=%s to=%s channel=%s",
                from_number, to_number, channel_id,
            )
            return CallResult(
                status="failed",
                provider_call_id=channel_id,
                error_message="Call initiation failed",
            )
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    def _build_result(
        self,
        channel_id: str | None,
        answered_at: float | None,
        cause: int | None,
    ) -> CallResult:
        # If we saw the channel reach Up state, the remote answered. The
        # cause code at hangup tells us how it ended, but for spam-testing
        # what matters is whether it was answered at all.
        if answered_at is not None:
            return CallResult(
                status="answered",
                duration=time.time() - answered_at,
                provider_call_id=channel_id,
            )

        if cause in _CAUSE_BUSY:
            return CallResult(status="busy", duration=0.0, provider_call_id=channel_id)
        if cause in _CAUSE_NO_ANSWER or cause in _CAUSE_NORMAL:
            # NORMAL_CLEARING without prior ANSWER means the call rang
            # and ended cleanly without being picked up.
            return CallResult(status="no_answer", duration=0.0, provider_call_id=channel_id)

        return CallResult(
            status="failed",
            duration=0.0,
            provider_call_id=channel_id,
            error_message=f"Hangup cause: {cause if cause is not None else 'unknown/timeout'}",
        )
