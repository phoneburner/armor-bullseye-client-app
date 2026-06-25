import logging
import os
import time
from telnyx import Client
from .base import TelephonyProvider, CallResult, CallEventCallback

log = logging.getLogger("bullseye.telnyx")


class TelnyxProvider(TelephonyProvider):
    def __init__(self):
        api_key = os.environ.get("TELNYX_API_KEY")
        if not api_key:
            raise ValueError("TELNYX_API_KEY is required")
        self.connection_id = os.environ.get("TELNYX_CONNECTION_ID")
        if not self.connection_id:
            raise ValueError("TELNYX_CONNECTION_ID is required")
        self.client = Client(api_key=api_key)

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
            return CallResult(status="failed", error_message="Call initiation failed")

        start_time = time.time()
        max_wait = 60
        poll_interval = 3
        was_answered = False
        call_ended = False

        while time.time() - start_time < max_wait:
            time.sleep(poll_interval)
            elapsed = time.time() - start_time
            try:
                events = self.client.call_events.list(filter={"leg_id": call_leg_id}, page_size=50)
                event_names = [e.name for e in events.data]
                log.debug("[%.1fs] events=%s", elapsed, event_names)

                for event in events.data:
                    if event.name == "call.answered":
                        was_answered = True
                    if event.name in ("call.hangup", "call.machine.detection.ended"):
                        call_ended = True

                if was_answered:
                    log.info("Call answered, hanging up")
                    time.sleep(2)
                    try:
                        self.client.calls.actions.hangup(call_control_id)
                    except Exception:
                        pass
                    break

                if call_ended:
                    log.info("Call ended without answer")
                    break

            except Exception as e:
                log.warning("[%.1fs] Event check error: %s", elapsed, e)
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
