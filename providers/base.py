from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable
import socket


# Controlled vocabulary for error_category. Providers should pick one of these
# rather than inventing new strings, so downstream consumers can rely on them.
#
#   network_error       — can't reach the provider API (reset, refused, DNS,
#                         timeout at TCP/HTTP layer). Usually firewall or proxy.
#   tls_error           — TLS handshake failed. Middlebox interception or cert
#                         issue.
#   auth_error          — provider rejected credentials.
#   invalid_from_number — from-number is not owned / not verified / not
#                         allowed to originate in the provider account.
#   invalid_to_number   — destination is malformed or provider refuses it.
#   rate_limited        — provider is throttling us.
#   provider_error      — other API-level failure from the provider.
#   call_setup_failed   — call was accepted by provider but never rang or
#                         completed setup.
#   internal_error      — unexpected exception in agent code.
ERROR_CATEGORIES = frozenset({
    "network_error",
    "tls_error",
    "auth_error",
    "invalid_from_number",
    "invalid_to_number",
    "rate_limited",
    "provider_error",
    "call_setup_failed",
    "internal_error",
})


@dataclass
class CallResult:
    status: str  # answered, no_answer, busy, failed
    duration: float | None = None
    provider_call_id: str | None = None
    error_message: str | None = None
    # A short controlled-vocab tag describing what class of failure occurred
    # (see ERROR_CATEGORIES). Only set when status == "failed" or "no_answer"
    # for reasons that carry diagnostic value.
    error_category: str | None = None


CallEventCallback = Callable[[str, dict], None]


def classify_generic(e: BaseException) -> tuple[str, str]:
    """Fallback exception -> (category, safe user-facing message) classifier.

    Providers should call this after their own SDK-specific checks. The
    returned message is safe to send to the Bullseye server — no traceback,
    no credentials, no raw exception text.
    """
    if isinstance(e, (ConnectionResetError, ConnectionAbortedError)):
        return ("network_error",
                "Connection to provider was reset — outbound to the provider "
                "API is likely blocked by a firewall, proxy, or security "
                "appliance.")
    if isinstance(e, ConnectionRefusedError):
        return ("network_error", "Provider API refused the connection.")
    if isinstance(e, TimeoutError):
        return ("network_error", "Timed out reaching the provider API.")
    if isinstance(e, socket.gaierror):
        return ("network_error", "DNS lookup for the provider host failed.")

    # Detect requests / urllib3 / SDK network errors by class name to avoid
    # a hard dependency on any one HTTP library at import time.
    name = type(e).__name__
    if name in ("SSLError", "SSLCertVerificationError", "SSLHandshakeError"):
        return ("tls_error",
                "TLS handshake with provider API failed — possible certificate "
                "problem or TLS-inspecting middlebox.")
    if name in ("ConnectionError", "NewConnectionError", "MaxRetryError",
                "ProxyError", "ProtocolError"):
        return ("network_error",
                "Cannot reach the provider API — check outbound network access "
                "and any proxy configuration.")
    if name in ("Timeout", "ReadTimeout", "ConnectTimeout"):
        return ("network_error", "Timed out reaching the provider API.")

    return ("provider_error", "Provider rejected the call attempt.")


class TelephonyProvider(ABC):
    @abstractmethod
    def place_call(
        self,
        from_number: str,
        to_number: str,
        on_event: CallEventCallback | None = None,
    ) -> CallResult:
        """Place a call and block until it completes.

        If on_event is provided, invoke it with real-time status updates:
          ("dialing",  {"provider_call_id": ...})
          ("answered", {"provider_call_id": ..., "duration": ...})
          ("done",     {"status": ..., "duration": ..., "provider_call_id": ...})
        """
        pass

    def preflight(self) -> None:
        """Optional startup connectivity + credentials check.

        Called after __init__ and before the agent connects to the Bullseye
        server. Should perform a cheap round-trip to the provider's API to
        surface network / firewall / credential problems at agent start
        instead of at first test-call time.

        Raise any exception on failure; the caller logs a warning and lets
        the agent continue (so it still shows as connected on the server
        for operator visibility). Default implementation is a no-op.
        """
        return
