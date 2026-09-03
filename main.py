import os
import sys
import json
import logging
import asyncio
import time
import websockets
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

load_dotenv()

from providers.base import TelephonyProvider
from providers.bandwidth_provider import BandwidthProvider
from providers.telnyx_provider import TelnyxProvider
from providers.twilio_provider import TwilioProvider
from providers.ringcentral_provider import RingCentralProvider
from providers.asterisk_provider import AsteriskProvider
from providers.freeswitch_provider import FreeSwitchProvider
from providers.proprietary_provider import ProprietaryProvider

__version__ = "1.0.10"

log = logging.getLogger("bullseye")

PROVIDERS = {
    "bandwidth": BandwidthProvider,
    "telnyx": TelnyxProvider,
    "twilio": TwilioProvider,
    "ringcentral": RingCentralProvider,
    "asterisk": AsteriskProvider,
    "freeswitch": FreeSwitchProvider,
    "proprietary": ProprietaryProvider,
}

RECONNECT_DELAY = 3
MAX_RECONNECT_DELAY = 60
HEARTBEAT_INTERVAL = 30
MAX_WS_MESSAGE_SIZE = 64 * 1024

# Cap on how many calls can run in parallel. Provider SDKs vary in
# thread-safety; more importantly, most telco accounts throttle at some
# number of concurrent originates. The default (10) suits modest batch
# volume. High-volume customers whose upstream sends large bursts should
# raise BULLSEYE_MAX_CONCURRENT_CALLS to match their telco's concurrent-
# call limit — otherwise a burst backs up behind this cap and tests wait
# in the agent's queue before dialing. Must be a positive integer — 0
# would deadlock (no test could ever acquire the semaphore) and
# negative / non-numeric values are meaningless.
def _load_max_concurrent() -> int:
    raw = os.environ.get("BULLSEYE_MAX_CONCURRENT_CALLS", "10")
    try:
        value = int(raw)
    except ValueError:
        sys.exit(f"Error: BULLSEYE_MAX_CONCURRENT_CALLS must be an integer, got {raw!r}")
    if value < 1:
        sys.exit(f"Error: BULLSEYE_MAX_CONCURRENT_CALLS must be >= 1, got {value}")
    return value


MAX_CONCURRENT_CALLS = _load_max_concurrent()
_call_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)

# Dedicated executor for blocking provider calls, sized to the same cap.
# Without this, run_in_executor(None, ...) lands on asyncio's DEFAULT
# ThreadPoolExecutor — min(32, cpu_count + 4) threads — which silently
# becomes the real concurrency ceiling: the semaphore admits N tests but
# only that many can actually be dialing, and the rest wait for a thread
# while occupying a slot. (Observed in production 2026-09-02: cap 50,
# true active calls ~15-22, dial latency ~4 min.)
_call_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CALLS, thread_name_prefix="call")

# Test IDs currently in flight (or recently completed). Prevents the server
# from re-dispatching the same test — either due to a reconnect race or a
# retried delivery — while it's still executing.
_inflight_tests: set[str] = set()

# Results that could not be delivered because the WebSocket dropped
# mid-call. The provider call keeps running in its thread after the
# connection (and this test's coroutine) dies; when it finishes, the
# result lands here and is re-sent on the next successful connection.
# Without this, an in_progress test is orphaned server-side: the server
# only re-pushes *pending* tests on reconnect, so the result would be
# lost and the test eventually failed out by the server's reaper.
# In-memory on purpose — if the whole process dies, the in-flight calls
# and their results die with it and the server reaper is the remedy.
_unsent_results: dict[str, dict] = {}  # test_id -> {"msg": <result payload>, "ts": float}

# Drop spooled results older than this. The server reaper fails a test
# out after ~15 min and logs (but ignores) later results, so a very old
# result has diagnostic value only — not worth unbounded growth.
RESULT_SPOOL_TTL = 3600

# The `start` message is a claim handshake: servers ack it with status
# "in_progress" (claim accepted) or "completed" (test already terminal —
# e.g. reaped after waiting too long in our queue — do NOT place the
# call). Futures here connect the ack listener in connect_and_run to the
# test task awaiting its claim. If no ack arrives within the timeout we
# proceed with the call: an old server whose ack was lost must not stall
# testing, and duplicates are safe server-side.
START_ACK_TIMEOUT = 5
_start_acks: dict[str, asyncio.Future] = {}

BANNER = """
        ooooooooooo
      oo           oo
    oo   ooooooooo   oo
   oo  oo         oo  oo
  oo  oo   ooooo   oo  oo
  oo  oo  oo   oo  oo  oo
  oo  oo  oo * oo  oo  oo
  oo  oo  oo   oo  oo  oo
  oo  oo   ooooo   oo  oo
   oo  oo         oo  oo
    oo   ooooooooo   oo
      oo           oo
        ooooooooooo

      B U L L S E Y E  v{}
"""


def get_config():
    server_url = os.environ.get("BULLSEYE_SERVER_URL")
    api_key = os.environ.get("BULLSEYE_API_KEY")
    provider_name = os.environ.get("TELEPHONY_PROVIDER", "telnyx").lower()

    if not server_url:
        sys.exit("Error: BULLSEYE_SERVER_URL is required")
    if not api_key:
        sys.exit("Error: BULLSEYE_API_KEY is required")
    if provider_name not in PROVIDERS:
        sys.exit(f"Error: Unsupported provider '{provider_name}'. Supported: {', '.join(PROVIDERS.keys())}")

    ws_url = server_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
    ws_url += "/agent/ws"

    if ws_url.startswith("ws://") and os.environ.get("BULLSEYE_ALLOW_INSECURE") != "1":
        sys.exit("Error: BULLSEYE_SERVER_URL is insecure (http/ws). Use https:// in production.\n"
                 "To override for local testing, set BULLSEYE_ALLOW_INSECURE=1")

    return ws_url, api_key, provider_name


async def handle_test(provider: TelephonyProvider, ws: websockets.ClientConnection, test_msg: dict):
    """Run a single test: notify the server, place the call, stream events, report the result.

    Bounded by _call_semaphore so a backlog can't spawn N simultaneous provider
    calls, and deduped by _inflight_tests so a redelivered test doesn't dial
    twice.
    """
    test_id = test_msg["id"]
    from_number = test_msg["from_number"]
    to_number = test_msg["to_number"]

    if test_id in _inflight_tests:
        log.warning("Test %s already in flight; ignoring duplicate delivery", test_id)
        return
    _inflight_tests.add(test_id)

    try:
        async with _call_semaphore:
            await _handle_test_inner(provider, ws, test_id, from_number, to_number)
    finally:
        _inflight_tests.discard(test_id)


async def _handle_test_inner(provider, ws, test_id, from_number, to_number):
    claim = asyncio.get_event_loop().create_future()
    _start_acks[test_id] = claim
    try:
        await ws.send(json.dumps({"type": "start", "test_id": test_id}))
        try:
            ack_status = await asyncio.wait_for(claim, timeout=START_ACK_TIMEOUT)
        except (asyncio.TimeoutError, TimeoutError):
            ack_status = None  # no ack (older server / lost frame): proceed
    finally:
        _start_acks.pop(test_id, None)

    if ack_status == "completed":
        # The server already closed this test (reaped while it waited in
        # our queue). Placing the call now would bill the customer for a
        # result nobody will accept — abort before dialing.
        log.warning("Test %s: server refused start (already terminal); aborting without dialing", test_id)
        return

    event_queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def on_event(event_name: str, data: dict):
        loop.call_soon_threadsafe(event_queue.put_nowait, (event_name, data))

    def run_call():
        log.info("Test %s: %s -> %s", test_id, from_number, to_number)
        result = provider.place_call(from_number, to_number, on_event=on_event)
        log.info("Test %s: status=%s duration=%.1fs", test_id, result.status, result.duration or 0)
        return result

    call_future = loop.run_in_executor(_call_executor, run_call)

    # Spool the result the moment the provider thread finishes. If the
    # normal send below succeeds it is removed again; if this coroutine
    # was cancelled by a WebSocket drop, the entry survives and is
    # re-sent after reconnect (see flush_unsent_results).
    def _spool(fut):
        if fut.cancelled() or fut.exception() is not None:
            return
        r = fut.result()
        _unsent_results[test_id] = {
            "msg": {
                "type": "result",
                "test_id": test_id,
                "call_status": r.status,
                "call_duration": r.duration,
                "provider_call_id": r.provider_call_id,
                "error_message": r.error_message,
                "error_category": r.error_category,
            },
            "ts": time.time(),
        }

    call_future.add_done_callback(_spool)

    async def forward_events():
        """Forward intermediate call events to the server. Terminal 'done' events are
        skipped — the authoritative final state is sent in the result message."""
        while True:
            try:
                event_name, data = await asyncio.wait_for(event_queue.get(), timeout=1.0)
                if event_name == "done":
                    continue
                await ws.send(json.dumps({"type": "call_event", "test_id": test_id, "event": event_name, **data}))
                log.debug("Test %s: sent event %s", test_id, event_name)
            except asyncio.TimeoutError:
                if call_future.done():
                    while not event_queue.empty():
                        event_name, data = event_queue.get_nowait()
                        if event_name == "done":
                            continue
                        await ws.send(json.dumps({"type": "call_event", "test_id": test_id, "event": event_name, **data}))
                        log.debug("Test %s: sent event %s", test_id, event_name)
                    break

    await forward_events()
    result = await call_future

    await ws.send(json.dumps(_unsent_results[test_id]["msg"]))
    # NOT unspooled here: a successful ws.send() only proves the bytes left
    # this process, not that the server processed them. The entry is removed
    # when the server's "completed" ack arrives (see connect_and_run); until
    # then the heartbeat flusher may re-send it, which is safe — the server
    # drops duplicate results for completed tests.
    log.info("Test %s: result sent (%s), awaiting ack", test_id, result.status)


async def flush_unsent_results(ws: websockets.ClientConnection, min_age: float = 0.0):
    """Re-send results whose original delivery was lost to a dropped connection.

    Entries stay spooled until the server's "completed" ack removes them
    (a ws.send() alone proves nothing about server-side processing), so
    min_age keeps the flusher from re-sending a result whose first send or
    ack is still in flight. Re-sends are safe: the server drops duplicate
    results for completed tests and still acks them. Entries older than
    RESULT_SPOOL_TTL are dropped (the server reaper failed the test out
    long ago and would ignore the result anyway).
    """
    now = time.time()
    for test_id, entry in list(_unsent_results.items()):
        age = now - entry["ts"]
        if age > RESULT_SPOOL_TTL:
            log.warning("Dropping spooled result for test %s (age %.0fs > TTL)", test_id, age)
            _unsent_results.pop(test_id, None)
            continue
        if age < min_age:
            continue
        await ws.send(json.dumps(entry["msg"]))
        # Kept in the spool until the server's "completed" ack removes it —
        # if this send is lost, a later flush retries (the server drops
        # duplicates for already-completed tests, so retries are safe).
        log.info("Re-sent spooled result for test %s (%s) after %.0fs",
                 test_id, entry["msg"].get("call_status"), age)


async def heartbeat(ws: websockets.ClientConnection):
    """Send periodic pings to keep the WebSocket connection alive.

    Also flushes spooled results each tick: a call that was in flight when
    a previous connection dropped finishes *during* this connection, so its
    result lands in the spool with no reconnect coming to deliver it.
    """
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await ws.send(json.dumps({"type": "ping"}))
            await flush_unsent_results(ws, min_age=15.0)
    except Exception:
        pass


async def connect_and_run(ws_url: str, api_key: str, provider: TelephonyProvider):
    """Maintain a single WebSocket session: authenticate, dispatch tests, relay events."""
    async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10, max_size=MAX_WS_MESSAGE_SIZE) as ws:
        await ws.send(json.dumps({"type": "auth", "api_key": api_key, "version": __version__}))
        log.info("=" * 60)
        log.info("CONNECTED — agent is ready to receive tests")
        log.info("=" * 60)

        # Deliver any results stranded by the previous connection before
        # processing new work. Failures here drop us back to the reconnect
        # loop with the spool intact.
        await flush_unsent_results(ws)

        heartbeat_task = asyncio.create_task(heartbeat(ws))
        # Track the call tasks spawned on THIS connection so we can cancel
        # them when the socket drops. A task left running against a dead
        # socket would fail its final send, discard its in-flight ID, and
        # race the server's re-delivery on reconnect.
        call_tasks: set[asyncio.Task] = set()
        try:
            async for raw_msg in ws:
                msg = json.loads(raw_msg)
                msg_type = msg.get("type")

                if msg_type == "test":
                    task = asyncio.create_task(handle_test(provider, ws, msg))
                    call_tasks.add(task)
                    task.add_done_callback(call_tasks.discard)
                elif msg_type == "ack":
                    log.debug("Server ack: test %s -> %s", msg.get("test_id"), msg.get("status"))
                    # Resolve a pending start-claim, if this test is waiting
                    # on one (see _handle_test_inner).
                    claim = _start_acks.get(msg.get("test_id"))
                    if claim is not None and not claim.done():
                        claim.set_result(msg.get("status"))
                    # A "completed" ack is the server confirming it processed
                    # (or deliberately dropped a duplicate of) our result —
                    # only now is the spooled copy safe to discard.
                    if msg.get("status") == "completed":
                        if _unsent_results.pop(msg.get("test_id"), None) is not None:
                            log.debug("Result for test %s acknowledged; unspooled", msg.get("test_id"))
                elif msg_type == "pong":
                    pass
                else:
                    log.warning("Unknown message type: %s", msg_type)
        finally:
            heartbeat_task.cancel()
            # Cancel any call tasks still attached to this (now closing)
            # connection. Queued tasks unwind cleanly and release their
            # in-flight ID so the server's re-delivery on reconnect is
            # accepted rather than rejected as a duplicate. A task already
            # executing a provider call can't stop the underlying blocking
            # SDK thread — that call rides out — but its coroutine unwinds
            # and the test is re-dispatched on reconnect (at-least-once).
            for task in list(call_tasks):
                task.cancel()


async def main():
    ws_url, api_key, provider_name = get_config()

    provider = PROVIDERS[provider_name]()
    # Don't log the full server URL at INFO — keeps it out of pasted-log
    # leaks. Full URL is in .env for the operator, and shown at DEBUG for
    # support cases (LOG_LEVEL=DEBUG).
    log.info("Server:   configured")
    log.debug("Server:   %s", ws_url)

    try:
        provider.preflight()
        log.info("Provider: %s (preflight OK)", provider_name)
    except Exception as e:
        log.warning("Provider: %s (preflight FAILED)", provider_name)
        log.warning("  Reason: %s", e)
        log.warning("  The agent will still connect to the Bullseye server,")
        log.warning("  but call attempts will likely fail until this is resolved.")
        log.warning("  Common causes: firewall blocking outbound to the provider API,")
        log.warning("  wrong credentials, or a proxy interfering with TLS.")

    delay = RECONNECT_DELAY
    while True:
        try:
            log.info("Connecting to server...")
            await connect_and_run(ws_url, api_key, provider)
            log.info("Connection closed by server")
            delay = RECONNECT_DELAY
        except websockets.exceptions.ConnectionClosedError as e:
            log.warning("Connection lost: %s", e)
        except ConnectionRefusedError:
            log.warning("Connection refused — is the server running?")
        except Exception as e:
            log.error("Connection error: %s", e)

        log.info("Reconnecting in %ds...", delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, MAX_RECONNECT_DELAY)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("twilio.http_client").setLevel(logging.WARNING)
    print(BANNER.format(__version__), flush=True)
    asyncio.run(main())
