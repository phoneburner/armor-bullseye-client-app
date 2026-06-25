"""Proprietary telephony provider — fill in for clients with their own network.

The Bullseye agent calls `place_call(from_number, to_number, on_event)` when
the server dispatches a test. This file is the only thing a client with a
proprietary telecom backend needs to write — the WebSocket connection,
authentication, reconnection, and event streaming are handled upstream by
agent/main.py.

Quick start
-----------
1. Implement `_dispatch_call_to_your_network()` (search for "TODO" below).
2. Set `TELEPHONY_PROVIDER=proprietary` in `.env`.
3. Add any credentials you need as env vars (also documented in `.env.example`).
4. Run `python3 main.py` — the agent connects, authenticates with
   BULLSEYE_API_KEY, and your provider gets called whenever a test arrives.

Reporting progress
------------------
Call on_event() at each stage (all optional — skip stages your network can't
observe; just return the final CallResult and the server treats absence as
"unknown intermediate state"):

    on_event("dialing",  {"provider_call_id": "<your network's call id>"})
    on_event("answered", {"provider_call_id": "<...>"})

Final outcome travels in the returned CallResult, NOT via on_event.

Status values
-------------
CallResult.status must be one of: "answered", "no_answer", "busy", "failed".
If you can't tell which non-answered case applies, use "failed".
"""

import os
import logging
from providers.base import TelephonyProvider, CallResult, CallEventCallback

log = logging.getLogger(__name__)


class ProprietaryProvider(TelephonyProvider):
    def __init__(self):
        # TODO: read whatever env vars / credentials your network needs.
        # Example:
        # self.api_token = os.environ["PROPRIETARY_API_TOKEN"]
        # self.api_url   = os.environ.get("PROPRIETARY_API_URL", "https://...")
        # if not self.api_token:
        #     raise RuntimeError("PROPRIETARY_API_TOKEN is required")
        pass

    def place_call(
        self,
        from_number: str,
        to_number: str,
        on_event: CallEventCallback | None = None,
    ) -> CallResult:
        """Place a call on the proprietary network and block until it completes.

        Must return within a reasonable timeout (recommend 60-90s). Long-hung
        calls will pile up if your network occasionally drops disconnect
        notifications.
        """
        provider_call_id = None
        try:
            # ────────────────────────────────────────────────────────────────
            # TODO: Replace this block with the actual call to your network.
            #
            # Typical shape:
            #
            #   1. Hand off to your network — get back a call ID:
            #        provider_call_id = your_network.dial(from_number, to_number)
            #
            #   2. Signal "dialing" once the network has accepted the request:
            #        if on_event:
            #            on_event("dialing", {"provider_call_id": provider_call_id})
            #
            #   3. Poll or subscribe for state changes (this is network-specific):
            #        while True:
            #            state = your_network.get_state(provider_call_id)
            #            if state == "ringing":
            #                continue
            #            if state == "answered":
            #                if on_event:
            #                    on_event("answered", {"provider_call_id": provider_call_id})
            #                # let the call play out — the server's BXML / your
            #                # network's hangup logic handles teardown
            #            if state in ("disconnected", "completed"):
            #                duration = your_network.get_duration(provider_call_id)
            #                return CallResult(
            #                    status="answered",
            #                    duration=duration,
            #                    provider_call_id=provider_call_id,
            #                )
            #            if state == "busy":
            #                return CallResult(status="busy", provider_call_id=provider_call_id)
            #            if state in ("no-answer", "timeout"):
            #                return CallResult(status="no_answer", provider_call_id=provider_call_id)
            #            if state == "failed":
            #                return CallResult(status="failed", provider_call_id=provider_call_id)
            #            time.sleep(1)
            #
            # ────────────────────────────────────────────────────────────────
            raise NotImplementedError(
                "ProprietaryProvider.place_call is a stub. Implement the call "
                "to your network before running this in production."
            )

        except Exception as e:
            # Log full details locally — never send raw exception text to the
            # server (it can leak credentials). Return a generic message.
            log.exception("Proprietary call failed: from=%s to=%s", from_number, to_number)
            return CallResult(
                status="failed",
                provider_call_id=provider_call_id,
                error_message="Call initiation failed",
            )
