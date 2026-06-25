"""FreeSWITCH telephony provider via ESL (Event Socket Library).

Connects to FreeSWITCH's inbound event socket, subscribes to channel
events, originates a call, and watches the event stream for ANSWER and
HANGUP_COMPLETE. Unlike the Asterisk V1 (polling-based), this is true
event-driven: cause codes from HANGUP_COMPLETE map cleanly to
`answered` / `no_answer` / `busy` / `failed`.

ESL is plain-text TCP. We speak it directly — no `python-ESL` /
`greenswitch` dependency.

FreeSWITCH-side prerequisites
-----------------------------

1. `mod_event_socket` must be loaded (it is by default in vanilla FS).

2. `conf/autoload_configs/event_socket.conf.xml` — bind on the interface
   the Bullseye agent can reach, and set a strong password. **Change the
   default `ClueCon`.**

       <configuration name="event_socket.conf" description="Socket Client">
         <settings>
           <param name="listen-ip" value="0.0.0.0"/>
           <param name="listen-port" value="8021"/>
           <param name="password" value="<strong-password>"/>
         </settings>
       </configuration>

3. A SIP profile or gateway configured for outbound calls. The endpoint
   template tells this provider how to format the dial string. Examples:

       sofia/external/{to_number}                — external profile
       sofia/gateway/my-provider/{to_number}     — a configured gateway

   Test it from the FreeSWITCH CLI first:
       originate sofia/gateway/my-provider/+15551234567 &sleep(10000)

4. Network: open TCP 8021 from the agent's host to the FreeSWITCH host.
   ESL is **not** encrypted — keep it on a private network or tunnel it.

Environment variables (see .env.example for the full template):

    FREESWITCH_HOST              ESL host (e.g. freeswitch.local)
    FREESWITCH_PORT              ESL port (default 8021)
    FREESWITCH_PASSWORD          ESL password from event_socket.conf.xml
    FREESWITCH_ENDPOINT_TEMPLATE Dial string template, with {to_number}
    FREESWITCH_DIAL_TIMEOUT      Total seconds to wait for terminal event
                                  (default 90 — gives time for ring + hold)
"""

import os
import time
import socket
import logging
import uuid as uuidlib
from typing import Optional
from providers.base import TelephonyProvider, CallResult, CallEventCallback

log = logging.getLogger(__name__)


# Map FreeSWITCH Hangup-Cause to Bullseye status. If we previously saw
# CHANNEL_ANSWER, NORMAL_CLEARING means the call was answered and then
# disconnected normally — that's the spam-tester happy path.
_CAUSE_BUSY = {"USER_BUSY"}
_CAUSE_NO_ANSWER = {
    "NO_USER_RESPONSE",
    "NO_ANSWER",
    "ORIGINATOR_CANCEL",
    "ALLOTTED_TIMEOUT",
}
# Anything else not matched as "answered" or busy/no_answer is "failed".


class _ESLClient:
    """Minimal blocking ESL client. Speaks the inbound text protocol."""

    def __init__(self, host: str, port: int, password: str, connect_timeout: float = 5.0):
        self.sock = socket.create_connection((host, port), timeout=connect_timeout)
        self.sock.settimeout(None)
        self.buf = b""
        # First message from FS on connect: "auth/request"
        self._read_block()
        self.send(f"auth {password}")
        reply = self._read_block()
        if reply.get("Reply-Text", "").startswith("+OK") is False:
            raise RuntimeError(f"ESL auth failed: {reply.get('Reply-Text')!r}")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def send(self, line: str):
        self.sock.sendall((line + "\n\n").encode("utf-8"))

    def set_timeout(self, t: Optional[float]):
        self.sock.settimeout(t)

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self.buf:
            chunk = self.sock.recv(8192)
            if not chunk:
                raise ConnectionError("ESL connection closed")
            self.buf += chunk
        idx = self.buf.index(marker)
        out = self.buf[:idx]
        self.buf = self.buf[idx + len(marker):]
        return out

    def _read_n(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(8192)
            if not chunk:
                raise ConnectionError("ESL connection closed")
            self.buf += chunk
        out = self.buf[:n]
        self.buf = self.buf[n:]
        return out

    def _read_block(self) -> dict:
        """Read one ESL block (headers, optional body) and return parsed headers.

        Body is parsed in-line when Content-Type is text/event-plain: body
        lines override/extend headers (this is how FS surfaces event detail).
        """
        raw_headers = self._read_until(b"\n\n").decode("utf-8", errors="replace")
        headers: dict = {}
        for line in raw_headers.split("\n"):
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip()] = v.strip()

        clen = int(headers.get("Content-Length", "0") or 0)
        if clen <= 0:
            return headers

        body = self._read_n(clen).decode("utf-8", errors="replace")
        if headers.get("Content-Type") == "text/event-plain":
            # Event body is also Header: Value lines, optionally followed by
            # another blank line + a sub-body (rare for our event set).
            for line in body.split("\n"):
                if ":" in line:
                    k, _, v = line.partition(":")
                    headers[k.strip()] = v.strip()
        else:
            headers["_body"] = body
        return headers

    def read_event(self, timeout: float) -> Optional[dict]:
        self.set_timeout(timeout)
        try:
            return self._read_block()
        except socket.timeout:
            return None


class FreeSwitchProvider(TelephonyProvider):
    def __init__(self):
        self.host = os.environ["FREESWITCH_HOST"]
        self.port = int(os.environ.get("FREESWITCH_PORT", "8021"))
        self.password = os.environ["FREESWITCH_PASSWORD"]
        self.endpoint_template = os.environ["FREESWITCH_ENDPOINT_TEMPLATE"]
        self.dial_timeout = int(os.environ.get("FREESWITCH_DIAL_TIMEOUT", "90"))

    def place_call(
        self,
        from_number: str,
        to_number: str,
        on_event: CallEventCallback | None = None,
    ) -> CallResult:
        endpoint = self.endpoint_template.format(to_number=to_number)
        call_uuid = str(uuidlib.uuid4())
        esl = None

        try:
            esl = _ESLClient(self.host, self.port, self.password)
            esl.send("event plain CHANNEL_CREATE CHANNEL_ANSWER CHANNEL_HANGUP_COMPLETE")
            # Drain the +OK reply to the event subscription
            esl.read_event(timeout=2)

            # bgapi so we don't block on the originate reply; the channel
            # lifecycle is observed via events filtered on our origination_uuid.
            dial_vars = (
                f"{{origination_uuid={call_uuid},"
                f"origination_caller_id_number={from_number}}}"
            )
            esl.send(f"bgapi originate {dial_vars}{endpoint} &sleep(10000)")
            # bgapi reply (Job-UUID) — we don't need it; we match on Unique-ID.
            esl.read_event(timeout=5)

            if on_event:
                on_event("dialing", {"provider_call_id": call_uuid})

            answered_at = None
            cause = None
            deadline = time.time() + self.dial_timeout

            while time.time() < deadline:
                ev = esl.read_event(timeout=max(0.5, deadline - time.time()))
                if ev is None:
                    continue
                if ev.get("Unique-ID") != call_uuid:
                    continue

                name = ev.get("Event-Name", "")
                if name == "CHANNEL_ANSWER" and answered_at is None:
                    answered_at = time.time()
                    if on_event:
                        on_event("answered", {"provider_call_id": call_uuid})
                elif name == "CHANNEL_HANGUP_COMPLETE":
                    cause = ev.get("Hangup-Cause", "")
                    break

            return self._build_result(call_uuid, answered_at, cause)

        except Exception:
            log.exception(
                "FreeSWITCH call failed: from=%s to=%s uuid=%s",
                from_number, to_number, call_uuid,
            )
            return CallResult(
                status="failed",
                provider_call_id=call_uuid,
                error_message="Call initiation failed",
            )
        finally:
            if esl is not None:
                esl.close()

    def _build_result(
        self,
        call_uuid: str,
        answered_at: Optional[float],
        cause: Optional[str],
    ) -> CallResult:
        # If we saw ANSWER, the call connected — report answered regardless
        # of how it ended. Duration counts from ANSWER to now.
        if answered_at is not None:
            return CallResult(
                status="answered",
                duration=time.time() - answered_at,
                provider_call_id=call_uuid,
            )

        if cause in _CAUSE_BUSY:
            return CallResult(status="busy", duration=0.0, provider_call_id=call_uuid)
        if cause in _CAUSE_NO_ANSWER:
            return CallResult(status="no_answer", duration=0.0, provider_call_id=call_uuid)
        # Includes the no-cause case (timed out waiting for HANGUP_COMPLETE)
        return CallResult(
            status="failed",
            duration=0.0,
            provider_call_id=call_uuid,
            error_message=f"Hangup cause: {cause or 'unknown/timeout'}",
        )
