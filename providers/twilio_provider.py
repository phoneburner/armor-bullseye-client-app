import logging
import os
import time
from twilio.rest import Client
from .base import TelephonyProvider, CallResult, CallEventCallback

log = logging.getLogger("bullseye.twilio")

TERMINAL_STATUSES = {"completed", "busy", "no-answer", "canceled", "failed"}

STATUS_MAP = {
    "completed": "answered",
    "busy": "busy",
    "no-answer": "no_answer",
    "canceled": "failed",
    "failed": "failed",
}


class TwilioProvider(TelephonyProvider):
    def __init__(self):
        account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        if not account_sid or not auth_token:
            raise ValueError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN are required")
        self.client = Client(account_sid, auth_token)

    def place_call(self, from_number: str, to_number: str, on_event: CallEventCallback | None = None) -> CallResult:
        log.info("Dialing %s -> %s", from_number, to_number)
        try:
            call = self.client.calls.create(
                to=to_number,
                from_=from_number,
                twiml='<Response><Pause length="10"/><Hangup/></Response>',
            )
            call_sid = call.sid
            log.info("Call initiated: sid=%s", call_sid)
        except Exception as e:
            log.error("Dial failed: %s", e)
            return CallResult(status="failed", error_message="Call initiation failed")

        if on_event:
            on_event("dialing", {"provider_call_id": call_sid})

        start_time = time.time()
        max_wait = 120
        poll_interval = 2
        answered = False

        while time.time() - start_time < max_wait:
            time.sleep(poll_interval)
            elapsed = time.time() - start_time
            try:
                call = self.client.calls(call_sid).fetch()
                log.debug("[%.1fs] status=%s", elapsed, call.status)

                if call.status == "in-progress" and not answered:
                    answered = True
                    if on_event:
                        on_event("answered", {"provider_call_id": call_sid, "duration": elapsed})

                if call.status in TERMINAL_STATUSES:
                    duration = time.time() - start_time
                    final_status = STATUS_MAP.get(call.status, "failed")
                    if on_event:
                        on_event("done", {"status": final_status, "duration": duration, "provider_call_id": call_sid})
                    return CallResult(status=final_status, duration=duration, provider_call_id=call_sid)
            except Exception as e:
                log.error("Poll error: %s", e)
                duration = time.time() - start_time
                if on_event:
                    on_event("done", {"status": "failed", "duration": duration, "provider_call_id": call_sid})
                return CallResult(status="failed", duration=duration, provider_call_id=call_sid,
                                  error_message="Error polling call status")

        duration = time.time() - start_time
        if on_event:
            on_event("done", {"status": "no_answer", "duration": duration, "provider_call_id": call_sid})
        return CallResult(status="no_answer", duration=duration, provider_call_id=call_sid,
                          error_message="Call timed out waiting for completion")
