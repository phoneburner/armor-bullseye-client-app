"""Mock telephony provider — simulates calls without touching any telco.

For load/concurrency testing and demos: the full test lifecycle runs
(dialing -> answered -> result, with blocking sleeps on a worker thread
exactly like a real provider SDK), but no call is placed and nothing is
billed. Timings and outcomes are configurable so a load test can be fast
(seconds per call) or realistic (35-55s holds).

Environment variables (all optional):

    MOCK_DIAL_SECONDS     Seconds before the "dialing" event (default 0.5)
    MOCK_RING_SECONDS     Seconds of ringing before answer (default 3)
    MOCK_HOLD_SECONDS     Post-answer hold. A number ("5") or range
                          ("35-55"). Default "5" — set "35-55" to mimic
                          production call lengths.
    MOCK_ANSWER_RATE      Fraction of calls answered, 0.0-1.0 (default 1.0).
                          Unanswered calls ring for MOCK_RING_SECONDS + 10
                          and return no_answer.
    MOCK_FAIL_RATE        Fraction of calls that fail at dial time with
                          provider_error, 0.0-1.0 (default 0.0). Applied
                          before MOCK_ANSWER_RATE.

Use TELEPHONY_PROVIDER=mock. Never ship a customer .env with this set.
"""

import logging
import os
import random
import time
import uuid

from providers.base import TelephonyProvider, CallResult, CallEventCallback

log = logging.getLogger("bullseye.mock")


def _parse_hold(raw: str) -> tuple[float, float]:
    if "-" in raw:
        lo, hi = raw.split("-", 1)
        return float(lo), float(hi)
    v = float(raw)
    return v, v


class MockProvider(TelephonyProvider):
    def __init__(self):
        self.dial_seconds = float(os.environ.get("MOCK_DIAL_SECONDS", "0.5"))
        self.ring_seconds = float(os.environ.get("MOCK_RING_SECONDS", "3"))
        self.hold_range = _parse_hold(os.environ.get("MOCK_HOLD_SECONDS", "5"))
        self.answer_rate = float(os.environ.get("MOCK_ANSWER_RATE", "1.0"))
        self.fail_rate = float(os.environ.get("MOCK_FAIL_RATE", "0.0"))
        log.warning(
            "MOCK provider active — no real calls will be placed "
            "(dial=%.1fs ring=%.1fs hold=%.1f-%.1fs answer_rate=%.2f fail_rate=%.2f)",
            self.dial_seconds, self.ring_seconds, *self.hold_range,
            self.answer_rate, self.fail_rate,
        )

    def preflight(self) -> None:
        return  # nothing to check — there is no provider

    def place_call(
        self,
        from_number: str,
        to_number: str,
        on_event: CallEventCallback | None = None,
    ) -> CallResult:
        call_id = f"mock-{uuid.uuid4().hex[:12]}"
        start = time.time()
        time.sleep(self.dial_seconds)

        if random.random() < self.fail_rate:
            return CallResult(
                status="failed",
                duration=0.0,
                provider_call_id=call_id,
                error_message="Mock provider simulated failure",
                error_category="provider_error",
            )

        if on_event:
            on_event("dialing", {"provider_call_id": call_id})

        if random.random() >= self.answer_rate:
            time.sleep(self.ring_seconds + 10)
            duration = time.time() - start
            if on_event:
                on_event("done", {"status": "no_answer", "duration": duration,
                                  "provider_call_id": call_id})
            return CallResult(status="no_answer", duration=duration,
                              provider_call_id=call_id)

        time.sleep(self.ring_seconds)
        if on_event:
            on_event("answered", {"provider_call_id": call_id,
                                  "duration": time.time() - start})
        time.sleep(random.uniform(*self.hold_range))
        duration = time.time() - start
        if on_event:
            on_event("done", {"status": "answered", "duration": duration,
                              "provider_call_id": call_id})
        return CallResult(status="answered", duration=duration,
                          provider_call_id=call_id)
