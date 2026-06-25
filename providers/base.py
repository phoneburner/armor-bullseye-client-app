from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable


@dataclass
class CallResult:
    status: str  # answered, no_answer, busy, failed
    duration: float | None = None
    provider_call_id: str | None = None
    error_message: str | None = None


CallEventCallback = Callable[[str, dict], None]


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
