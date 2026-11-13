import logging
import os
import time
from twilio.rest import Client
from .base import TelephonyProvider, CallResult, CallEventCallback, classify_generic, random_hold_seconds

log = logging.getLogger("bullseye.twilio")

TERMINAL_STATUSES = {"completed", "busy", "no-answer", "canceled", "failed"}

STATUS_MAP = {
    "completed": "answered",
    "busy": "busy",
    "no-answer": "no_answer",
    "canceled": "failed",
    "failed": "failed",
}


def _classify(e: BaseException) -> tuple[str, str]:
    """Classify a Twilio exception into (category, safe message)."""
    try:
        from twilio.base.exceptions import TwilioRestException
    except ImportError:
        TwilioRestException = ()  # type: ignore[assignment]

    if isinstance(e, TwilioRestException):
        code = getattr(e, "code", None)
        status = getattr(e, "status", None)
        if status in (401, 403) or code in (20003, 20004):
            return ("auth_error",
                    "Twilio credentials rejected — check TWILIO_ACCOUNT_SID and "
                    "either TWILIO_API_KEY_SID/TWILIO_API_KEY_SECRET or "
                    "TWILIO_AUTH_TOKEN.")
        # From-number problems: not owned by this account, not voice-capable,
        # or SIP-domain-mismatched.
        if code in (21212, 21606, 21210):
            return ("invalid_from_number",
                    "From-number is not a valid Twilio number in this account, "
                    "or is not permitted to originate calls.")
        # To-number problems: malformed, unverified trial destination, etc.
        if code in (21211, 21217, 21219):
            return ("invalid_to_number",
                    "Destination number rejected by Twilio (bad format or, on "
                    "trial accounts, not on the verified-callers list).")
        # Geographic-permissions / carrier-reachability: not the from-number.
        if code in (21214, 21215):
            return ("provider_error",
                    "Twilio cannot reach this destination — geographic calling "
                    "permissions may not be enabled for the destination "
                    "country/region on this account.")
        # Misconfigured Voice Application SID.
        if code == 21218:
            return ("provider_error",
                    "Twilio ApplicationSid is invalid — check the Voice "
                    "Application configuration in the Twilio Console.")
        if status == 429 or code == 20429:
            return ("rate_limited", "Twilio is rate-limiting this account.")
        return ("provider_error",
                f"Twilio rejected the call (HTTP {status}, code {code}).")

    return classify_generic(e)


class TwilioProvider(TelephonyProvider):
    def __init__(self):
        account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        api_key_sid = os.environ.get("TWILIO_API_KEY_SID")
        api_key_secret = os.environ.get("TWILIO_API_KEY_SECRET")

        if not account_sid:
            raise ValueError("TWILIO_ACCOUNT_SID is required")
        if not account_sid.startswith("AC"):
            # Every Twilio REST path embeds the Account SID, so an API Key SID
            # here produces a bare 404 for an account that does not exist.
            raise ValueError(
                f"TWILIO_ACCOUNT_SID must be the Account SID beginning with 'AC' "
                f"(got one beginning with {account_sid[:2]!r}). An API Key SID "
                f"('SK...') belongs in TWILIO_API_KEY_SID instead."
            )

        if api_key_sid or api_key_secret:
            # API key auth. The key authenticates the caller; account_sid still
            # selects the account it acts on. Preferred for customer-supplied
            # credentials: the customer can revoke the key in their console
            # without rotating the account-wide auth token.
            if not (api_key_sid and api_key_secret):
                raise ValueError(
                    "TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET must be set together"
                )
            if not api_key_sid.startswith("SK"):
                raise ValueError(
                    f"TWILIO_API_KEY_SID must begin with 'SK' (got one beginning "
                    f"with {api_key_sid[:2]!r})."
                )
            self.client = Client(api_key_sid, api_key_secret, account_sid)
            self.auth_mode = "api_key"
        elif auth_token:
            self.client = Client(account_sid, auth_token)
            self.auth_mode = "auth_token"
        else:
            raise ValueError(
                "Twilio credentials are required: either TWILIO_API_KEY_SID with "
                "TWILIO_API_KEY_SECRET, or TWILIO_AUTH_TOKEN"
            )
        log.info("Twilio auth mode: %s", self.auth_mode)
        # The SDK's shared requests.Session defaults to a pool of 10
        # connections; with more concurrent calls than that, poll requests
        # queue for a socket and stretch every call's wall time. Size the
        # pool to the agent's concurrency cap (plus slack for preflight
        # and stragglers).
        try:
            from requests.adapters import HTTPAdapter
            pool = int(os.environ.get("BULLSEYE_MAX_CONCURRENT_CALLS", "10")) + 5
            adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool)
            self.client.http_client.session.mount("https://", adapter)
        except Exception:
            # Cosmetic optimisation — never fail startup over it.
            log.debug("Could not resize Twilio HTTP connection pool", exc_info=True)

    def preflight(self) -> None:
        # Cheap, no side effects, exercises the full path — DNS to
        # api.twilio.com, TCP + TLS, HTTP request/response, and Basic Auth
        # credentials. If this succeeds, real call attempts should reach
        # Twilio too.
        #
        # The probe differs by auth mode. API keys are not permitted to read
        # the Account resource at all (401, code 70004) however broad their
        # permissions, so fetching it would fail preflight for every API-key
        # deployment while calls themselves worked perfectly. Listing one call
        # is the equivalent check that a key is allowed to make.
        if self.auth_mode == "api_key":
            self.client.calls.list(limit=1)
        else:
            self.client.api.account.fetch()

    def place_call(self, from_number: str, to_number: str, on_event: CallEventCallback | None = None) -> CallResult:
        hold = random_hold_seconds()
        log.info("Dialing %s -> %s (hold %ds)", from_number, to_number, hold)
        try:
            call = self.client.calls.create(
                to=to_number,
                from_=from_number,
                twiml=f'<Response><Pause length="{hold}"/><Hangup/></Response>',
            )
            call_sid = call.sid
            log.info("Call initiated: sid=%s", call_sid)
        except Exception as e:
            log.error("Dial failed: %s", e)
            category, msg = _classify(e)
            return CallResult(status="failed", error_message=msg, error_category=category)

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
                category, msg = _classify(e)
                if on_event:
                    on_event("done", {"status": "failed", "duration": duration, "provider_call_id": call_sid})
                return CallResult(status="failed", duration=duration, provider_call_id=call_sid,
                                  error_message=msg, error_category=category)

        duration = time.time() - start_time
        if on_event:
            on_event("done", {"status": "no_answer", "duration": duration, "provider_call_id": call_sid})
        return CallResult(status="no_answer", duration=duration, provider_call_id=call_sid,
                          error_message="Call timed out waiting for completion")
