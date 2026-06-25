import logging
import os
import time
import bandwidth
from .base import TelephonyProvider, CallResult, CallEventCallback

log = logging.getLogger("bullseye.bandwidth")

DISCONNECT_STATUS_MAP = {
    "busy": "busy",
    "rejected": "busy",
    "timeout": "no_answer",
    "cancel": "no_answer",
    "hangup": "answered",
    "callback-error": "failed",
    "invalid-bxml": "failed",
    "application-error": "failed",
    "account-limit": "failed",
    "node-capacity-exceeded": "failed",
    "error": "failed",
    "unknown": "failed",
}


class BandwidthProvider(TelephonyProvider):
    def __init__(self):
        self.account_id = os.environ.get("BANDWIDTH_ACCOUNT_ID")
        self.application_id = os.environ.get("BANDWIDTH_APPLICATION_ID")
        self.answer_url = os.environ.get("BANDWIDTH_ANSWER_URL")

        client_id = os.environ.get("BANDWIDTH_CLIENT_ID")
        client_secret = os.environ.get("BANDWIDTH_CLIENT_SECRET")
        username = os.environ.get("BANDWIDTH_API_USERNAME")
        password = os.environ.get("BANDWIDTH_API_PASSWORD")

        if not all([self.account_id, self.application_id, self.answer_url]):
            raise ValueError(
                "BANDWIDTH_ACCOUNT_ID, BANDWIDTH_APPLICATION_ID, "
                "and BANDWIDTH_ANSWER_URL are required"
            )

        # Prefer OAuth 2.0 client credentials (current). Fall back to Basic Auth
        # (deprecated by Bandwidth — legacy API users decommissioned 2026-12-02).
        if client_id and client_secret:
            self.config = bandwidth.Configuration(client_id=client_id, client_secret=client_secret)
        elif username and password:
            log.warning("Using deprecated Basic Auth — migrate to OAuth (BANDWIDTH_CLIENT_ID/SECRET) before 2026-12-02")
            self.config = bandwidth.Configuration(username=username, password=password)
        else:
            raise ValueError(
                "Bandwidth credentials required: set either "
                "BANDWIDTH_CLIENT_ID + BANDWIDTH_CLIENT_SECRET (recommended) or "
                "BANDWIDTH_API_USERNAME + BANDWIDTH_API_PASSWORD (deprecated)"
            )

    def place_call(self, from_number: str, to_number: str, on_event: CallEventCallback | None = None) -> CallResult:
        log.info("Dialing %s -> %s", from_number, to_number)

        with bandwidth.ApiClient(self.config) as api_client:
            calls_api = bandwidth.CallsApi(api_client)

            try:
                create_call = bandwidth.CreateCall(
                    to=to_number,
                    var_from=from_number,
                    application_id=self.application_id,
                    answer_url=self.answer_url,
                    call_timeout=30,
                )
                response = calls_api.create_call(self.account_id, create_call)
                call_id = response.call_id
                log.info("Call initiated: call_id=%s", call_id)
            except Exception as e:
                log.error("Dial failed: %s", e)
                return CallResult(status="failed", error_message="Call initiation failed")

            if on_event:
                on_event("dialing", {"provider_call_id": call_id})

            start_time = time.time()
            max_wait = 120
            poll_interval = 2
            answered = False

            while time.time() - start_time < max_wait:
                time.sleep(poll_interval)
                elapsed = time.time() - start_time
                try:
                    state = calls_api.get_call_state(self.account_id, call_id)
                    log.debug("[%.1fs] state=%s", elapsed, state.state)

                    if state.state == "answered" and not answered:
                        answered = True
                        if on_event:
                            on_event("answered", {"provider_call_id": call_id, "duration": elapsed})

                    if state.state == "disconnected":
                        duration = time.time() - start_time
                        cause = state.disconnect_cause or "unknown"
                        log.info("Call disconnected: cause=%s", cause)

                        if answered or state.answer_time:
                            final_status = "answered"
                        else:
                            final_status = DISCONNECT_STATUS_MAP.get(cause, "failed")

                        if on_event:
                            on_event("done", {"status": final_status, "duration": duration, "provider_call_id": call_id})
                        return CallResult(status=final_status, duration=duration, provider_call_id=call_id)

                except Exception as e:
                    log.warning("[%.1fs] Status check error: %s", elapsed, e)
                    continue

            duration = time.time() - start_time
            log.info("Call timed out")
            if on_event:
                on_event("done", {"status": "no_answer", "duration": duration, "provider_call_id": call_id})
            return CallResult(status="no_answer", duration=duration, provider_call_id=call_id,
                              error_message="Call timed out waiting for completion")
