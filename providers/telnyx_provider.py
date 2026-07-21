import logging
import os
import time
from telnyx import Client
from .base import TelephonyProvider, CallResult, CallEventCallback, classify_generic, random_hold_seconds

log = logging.getLogger("bullseye.telnyx")


def _classify(e: BaseException) -> tuple[str, str]:
    """Classify a Telnyx exception into (category, safe message)."""
    name = type(e).__name__
    status = getattr(e, "http_status", None) or getattr(e, "status", None) \
        or getattr(e, "status_code", None)

    if status in (401, 403) or name in ("AuthenticationError", "PermissionError"):
        return ("auth_error",
                "Telnyx credentials rejected — check TELNYX_API_KEY.")
    if status == 422 or name == "InvalidRequestError":
        return ("provider_error",
                "Telnyx rejected the call parameters (from/to number or "
                "connection ID). Check TELNYX_CONNECTION_ID and that the "
                "from-number is assigned to that Call Control app.")
    if status == 429:
        return ("rate_limited", "Telnyx is rate-limiting this account.")
    if status is not None:
        return ("provider_error", f"Telnyx API returned HTTP {status}.")

    return classify_generic(e)


class TelnyxProvider(TelephonyProvider):
    def __init__(self):
        api_key = os.environ.get("TELNYX_API_KEY")
        if not api_key:
            raise ValueError("TELNYX_API_KEY is required")
        self.connection_id = os.environ.get("TELNYX_CONNECTION_ID")
        if not self.connection_id:
            raise ValueError("TELNYX_CONNECTION_ID is required")
        self.client = Client(api_key=api_key)

    def preflight(self) -> None:
        # List phone numbers with the smallest page — cheapest authenticated
        # round-trip that exercises DNS, TCP, TLS, HTTP, and API key auth.
        self.client.phone_numbers.list(page_size=1)

    def place_call(self, from_number: str, to_number: str, on_event: CallEventCallback | None = None) -> CallResult:
        log.info("Dialing %s -> %s via connection %s", from_number, to_number, self.connection_id)
        try:
            result = self.client.calls.dial(
                connection_id=self.connection_id,
                to=to_number,
                from_=from_number,
            )
            call_control_id = result.data.call_control_id
            call_leg_id = result.data.call_leg_id
            log.info("Call initiated: leg_id=%s", call_leg_id)
        except Exception as e:
            log.error("Dial failed: %s", e)
            category, msg = _classify(e)
            return CallResult(status="failed", error_message=msg, error_category=category)

        # Per the TelephonyProvider contract, notify the caller that the call
        # is in flight so the server can stream a "dialing" event to ARMOR.
        if on_event:
            on_event("dialing", {"provider_call_id": call_leg_id})

        start_time = time.time()
        # Telnyx events have a ~15-20s delay; hold is up to 55s; total budget
        # needs to cover both plus some ring time.
        max_wait = 150
        poll_interval = 3
        was_answered = False
        call_ended = False
        # Bounded polling errors: a firewall/API outage should surface as
        # failed/network_error, not silently ride out the deadline and
        # report no_answer (which would falsely tell ARMOR the number is
        # being blocked).
        consecutive_poll_errors = 0
        poll_error_threshold = 5

        while time.time() - start_time < max_wait:
            time.sleep(poll_interval)
            elapsed = time.time() - start_time
            try:
                events = self.client.call_events.list(filter={"leg_id": call_leg_id}, page_size=50)
                consecutive_poll_errors = 0
                event_names = {e.name for e in events.data}
                log.debug("[%.1fs] events=%s", elapsed, sorted(event_names))

                if "call.answered" in event_names and not was_answered:
                    was_answered = True
                    if on_event:
                        on_event("answered", {
                            "provider_call_id": call_leg_id,
                            "duration": elapsed,
                        })
                if event_names & {"call.hangup", "call.machine.detection.ended"}:
                    call_ended = True

                # Terminal event wins. Because Telnyx batches delayed events,
                # a single poll can surface both call.answered and call.hangup
                # together — in that case the call is already over and we must
                # NOT start a 35-55s hold on a dead call.
                if call_ended:
                    log.info("Call ended (answered=%s)", was_answered)
                    break

                # Answered and still live → hold, then hang up.
                if was_answered:
                    hold = random_hold_seconds()
                    log.info("Call answered, holding for %ds", hold)
                    time.sleep(hold)
                    try:
                        self.client.calls.actions.hangup(call_control_id)
                    except Exception:
                        pass
                    call_ended = True
                    break

            except Exception as e:
                consecutive_poll_errors += 1
                log.warning("[%.1fs] Event check error (%d/%d): %s",
                            elapsed, consecutive_poll_errors, poll_error_threshold, e)
                if consecutive_poll_errors >= poll_error_threshold:
                    duration = time.time() - start_time
                    category, msg = _classify(e)
                    # The dial already succeeded, so the leg may still be
                    # ringing or connected (and chargeable). Best-effort
                    # hangup before we give up — otherwise we report the
                    # call finished while it keeps running.
                    try:
                        self.client.calls.actions.hangup(call_control_id)
                        log.info("Best-effort hangup sent after poll failures")
                    except Exception as he:
                        log.warning("Best-effort hangup failed: %s", he)
                    return CallResult(status="failed", duration=duration,
                                      provider_call_id=call_leg_id,
                                      error_message=msg, error_category=category)
                continue

        duration = time.time() - start_time

        if was_answered:
            return CallResult(status="answered", duration=duration, provider_call_id=call_leg_id)

        if not call_ended:
            log.info("Call timed out, hanging up")
            try:
                self.client.calls.actions.hangup(call_control_id)
            except Exception:
                pass

        return CallResult(status="no_answer", duration=duration, provider_call_id=call_leg_id)
