import os
import sys
import json
import logging
import asyncio
import websockets
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

__version__ = "1.0.1"

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
    """Run a single test: notify the server, place the call, stream events, report the result."""
    test_id = test_msg["id"]
    from_number = test_msg["from_number"]
    to_number = test_msg["to_number"]

    await ws.send(json.dumps({"type": "start", "test_id": test_id}))

    event_queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def on_event(event_name: str, data: dict):
        loop.call_soon_threadsafe(event_queue.put_nowait, (event_name, data))

    def run_call():
        log.info("Test %s: %s -> %s", test_id, from_number, to_number)
        result = provider.place_call(from_number, to_number, on_event=on_event)
        log.info("Test %s: status=%s duration=%.1fs", test_id, result.status, result.duration or 0)
        return result

    call_future = loop.run_in_executor(None, run_call)

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

    await ws.send(json.dumps({
        "type": "result",
        "test_id": test_id,
        "call_status": result.status,
        "call_duration": result.duration,
        "provider_call_id": result.provider_call_id,
        "error_message": result.error_message,
    }))
    log.info("Test %s: result sent (%s)", test_id, result.status)


async def heartbeat(ws: websockets.ClientConnection):
    """Send periodic pings to keep the WebSocket connection alive."""
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await ws.send(json.dumps({"type": "ping"}))
    except Exception:
        pass


async def connect_and_run(ws_url: str, api_key: str, provider: TelephonyProvider):
    """Maintain a single WebSocket session: authenticate, dispatch tests, relay events."""
    async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10, max_size=MAX_WS_MESSAGE_SIZE) as ws:
        await ws.send(json.dumps({"type": "auth", "api_key": api_key}))
        log.info("=" * 60)
        log.info("CONNECTED — agent is ready to receive tests")
        log.info("=" * 60)

        heartbeat_task = asyncio.create_task(heartbeat(ws))
        try:
            async for raw_msg in ws:
                msg = json.loads(raw_msg)
                msg_type = msg.get("type")

                if msg_type == "test":
                    asyncio.create_task(handle_test(provider, ws, msg))
                elif msg_type == "ack":
                    log.debug("Server ack: test %s -> %s", msg.get("test_id"), msg.get("status"))
                elif msg_type == "pong":
                    pass
                else:
                    log.warning("Unknown message type: %s", msg_type)
        finally:
            heartbeat_task.cancel()


async def main():
    ws_url, api_key, provider_name = get_config()

    provider = PROVIDERS[provider_name]()
    # Don't log the full server URL at INFO — keeps it out of pasted-log
    # leaks. Full URL is in .env for the operator, and shown at DEBUG for
    # support cases (LOG_LEVEL=DEBUG).
    log.info("Server:   configured")
    log.debug("Server:   %s", ws_url)
    log.info("Provider: %s", provider_name)

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
