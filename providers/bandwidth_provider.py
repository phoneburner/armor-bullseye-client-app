import logging
import os
import time
import bandwidth
from .base import TelephonyProvider, CallResult, CallEventCallback, classify_generic

log = logging.getLogger("bullseye.bandwidth")


def _classify(e: BaseException) -> tuple[str, str]:
    """Classify a Bandwidth exception into (category, safe message)."""
    # Bandwidth SDK raises subclasses of ApiException with a status attribute.
    name = type(e).__name__
    status = getattr(e, "status", None) or getattr(e, "status_code", None)

    if name in ("UnauthorizedException", "ForbiddenException") or status in (401, 403):
        return ("auth_error",
                "Bandwidth credentials rejected — check BANDWIDTH_CLIENT_ID / "
                "BANDWIDTH_CLIENT_SECRET (or the legacy username/password pair).")
    if name == "BadRequestException" or status == 400:
        return ("provider_error",
                "Bandwidth rejected the call request (bad from/to number, "
                "wrong application, or missing configuration).")
    if status == 429:
        return ("rate_limited", "Bandwidth is rate-limiting this account.")
    if name in ("ApiException", "NotFoundException") and status == 404:
        return ("provider_error",
                "Bandwidth resource not found — check BANDWIDTH_APPLICATION_ID.")
    if name == "ApiException" and status is not None:
        return ("provider_error", f"Bandwidth API returned HTTP {status}.")

    return classify_generic(e)

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

    def preflight(self) -> None:
        # Cheapest authenticated round-trip we can make against the account.
        # Listing calls (page size 1) exercises DNS, TCP, TLS, HTTP, and creds
        # without side effects. If Bandwidth is unreachable or creds are bad,
        # this raises.
        with bandwidth.ApiClient(self.config) as api_client:
            calls_api = bandwidth.CallsApi(api_client)
            calls_api.list_calls(self.account_id, size=1)

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
                category, msg = _classify(e)
                return CallResult(status="failed", error_message=msg, error_category=category)

            if on_event:
                on_event("dialing", {"provider_call_id": call_id})

            start_time = time.time()
            max_wait = 180
            poll_interval = 2
            answered = False
            # Consecutive poll errors: if we hit this many in a row we treat
            # the call as failed (network outage) rather than let the loop
            # fall through to a "no_answer" that would mislead ARMOR into
            # believing the number is being blocked.
            consecutive_poll_errors = 0
            poll_error_threshold = 5

            while time.time() - start_time < max_wait:
                time.sleep(poll_interval)
                elapsed = time.time() - start_time
                try:
                    state = calls_api.get_call_state(self.account_id, call_id)
                    log.debug("[%.1fs] state=%s", elapsed, state.state)
                    consecutive_poll_errors = 0

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
                    consecutive_poll_errors += 1
                    log.warning("[%.1fs] Status check error (%d/%d): %s",
                                elapsed, consecutive_poll_errors, poll_error_threshold, e)
                    if consecutive_poll_errors >= poll_error_threshold:
                        duration = time.time() - start_time
                        category, msg = _classify(e)
                        # Even if the classifier says "provider_error", if the
                        # SDK never got a response we're really talking about
                        # a reachability problem.
                        return CallResult(
                            status="failed",
                            duration=duration,
                            provider_call_id=call_id,
                            error_message=msg,
                            error_category=category,
                        )
                    continue

            duration = time.time() - start_time
            log.info("Call timed out")
            if on_event:
                on_event("done", {"status": "no_answer", "duration": duration, "provider_call_id": call_id})
            return CallResult(status="no_answer", duration=duration, provider_call_id=call_id,
                              error_message="Call timed out waiting for completion")
