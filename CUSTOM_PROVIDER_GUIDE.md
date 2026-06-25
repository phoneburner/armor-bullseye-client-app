# Bullseye Agent — Integrating Your Proprietary Telephony Network

This guide is for clients who originate calls through their own
telecom infrastructure (rather than a hosted CPaaS like Twilio, Telnyx,
or Bandwidth, and not a standard PBX like Asterisk or FreeSWITCH).

You implement **one Python function**. The agent handles everything
else — connecting to the Bullseye server, authenticating, receiving test
requests, reporting back results.

---

## How it fits together

```
Bullseye server  ◄── WebSocket ──►  Bullseye agent (on your hardware)
                                            │
                                            ▼
                                  place_call(from, to)        ← you implement
                                            │
                                            ▼
                                    Your network places
                                    a real call
                                            │
                                            ▼
                                    Status reported back
                                    over the WebSocket
```

The agent runs as a normal Python process (or Docker container) on a
machine you control. When the Bullseye server has a test for you, it
pushes a message containing a "from" number and a "to" number. The agent
calls your `place_call()` function. You originate the call however you
want. You return a result. The agent forwards it to the server.

That's the entire contract.

---

## What you need from us

1. **A Bullseye server URL** — typically `https://your-bullseye-server`.
2. **A Bullseye API key** — a token starting with `bse_...`. We generate this
   for your agent and send it to you securely.

That's it. No public IP, no inbound firewall holes, no webhooks. Your
agent makes one outbound WebSocket connection to our server.

---

## What we need to know from you

For sizing and operational coordination, helpful answers up front:

- **Roughly how many concurrent tests do you expect to run?** This drives
  whether one agent process is enough or whether you'll want to run a
  pool (each on its own API key).
- **What's the expected call duration?** The default agent holds calls
  ~90 seconds; if your network requires shorter or longer, that's easy
  to adjust.
- **Can your network give us intermediate status (dialing, answered) or
  only final status?** Both work; intermediate signals just make the
  product feel more real-time on our side.

---

## Setup, step by step

### 1. Get the agent code

We will give you a download (tarball or git repo URL). Unpack on the
machine that will run the agent.

```bash
tar xzf bullseye-agent-x.y.z.tar.gz
cd bullseye-agent-x.y.z
```

### 2. Make sure you have Python 3.10 or newer

```bash
python3 --version
```

If you want to run via Docker instead, that works too — skip ahead to
the Docker section.

### 3. Configure the agent

Copy the example env file and edit it:

```bash
cp .env.example .env
```

In `.env`, set:

```bash
BULLSEYE_SERVER_URL=https://your-bullseye-server
BULLSEYE_API_KEY=bse_<the key we gave you>
TELEPHONY_PROVIDER=proprietary
```

You can add any environment variables your own network code needs (API
tokens, endpoint URLs, certificate paths — whatever). The agent will
load `.env` into the environment before calling your code.

### 4. Implement your `place_call()`

Open `providers/proprietary_provider.py`. There's a clearly marked
`TODO` block inside the `place_call` method. That's the only place you
need to edit.

The function signature:

```python
def place_call(
    self,
    from_number: str,    # E.164 format, e.g. "+15551234567"
    to_number: str,      # E.164 format, e.g. "+19998887777"
    on_event: CallEventCallback | None = None,
) -> CallResult:
    ...
```

#### Step A — Originate the call on your network

Call into your internal API / library / queue / whatever you use to
place a call from `from_number` to `to_number`. Save the call ID your
network returns — we'll use it for tracking.

```python
provider_call_id = your_network.dial(from_number, to_number)
```

#### Step B — (Optional) report "dialing" as soon as the network accepts the request

If your network gives you a way to know "the request is in flight":

```python
if on_event:
    on_event("dialing", {"provider_call_id": provider_call_id})
```

This shows up on our dashboards in real time. If your network can't tell
you this, just skip it — return only the final result.

#### Step C — Wait for the call to complete

This is network-specific. Two common shapes:

**Pattern A — Polling**: every second or so, ask your network for the
current call state.

```python
import time
while True:
    state = your_network.get_state(provider_call_id)
    if state == "ringing":
        time.sleep(1); continue
    if state == "answered":
        if on_event:
            on_event("answered", {"provider_call_id": provider_call_id})
        # call is up — your network may auto-disconnect, or you might
        # need to send a hangup after a fixed hold period
        continue
    if state in ("completed", "disconnected"):
        # call ended — go to step D
        break
    if state == "busy":
        return CallResult(status="busy", provider_call_id=provider_call_id)
    if state in ("no-answer", "timeout"):
        return CallResult(status="no_answer", provider_call_id=provider_call_id)
    if state == "failed":
        return CallResult(status="failed", provider_call_id=provider_call_id)
    time.sleep(1)
```

**Pattern B — Event subscription**: your network pushes events to you
(webhook, message queue, event bus). Block until you receive the
terminal one.

Both patterns work. Pick whichever fits your stack.

#### Step D — Return a `CallResult`

The status must be one of these four strings (these map directly to
what our customers' systems consume):

| `status`     | Meaning |
|--------------|---------|
| `answered`   | Remote party picked up. **This is the most important signal — if the call is answered, the number is *not* being spam-blocked.** |
| `no_answer`  | Rang but no one picked up within the dial timeout |
| `busy`       | Got a busy signal |
| `failed`     | Provider couldn't place the call, or call failed for another reason |

If you can't tell `busy` from `no_answer` from `failed`, that's OK — use
`failed`. The critical signal for Bullseye is `answered` vs. anything
else.

```python
return CallResult(
    status="answered",
    duration=conversation_duration_in_seconds,   # float; from answer to hangup
    provider_call_id=provider_call_id,
)
```

That's the whole integration. Roughly 20-50 lines of code, depending
on whether you poll or use events.

---

## A reference implementation sketch

A complete (toy) example, assuming your network has a Python client
called `mynetwork`:

```python
import time
import logging
from providers.base import TelephonyProvider, CallResult, CallEventCallback
import mynetwork  # your internal library

log = logging.getLogger(__name__)


class ProprietaryProvider(TelephonyProvider):
    def __init__(self):
        self.client = mynetwork.Client(
            api_token=os.environ["MYNETWORK_API_TOKEN"],
        )

    def place_call(self, from_number, to_number, on_event=None):
        call_id = None
        try:
            call_id = self.client.dial(
                from_number=from_number,
                to_number=to_number,
                hold_seconds=10,
            )
            if on_event:
                on_event("dialing", {"provider_call_id": call_id})

            answered_at = None
            for _ in range(90):  # poll up to 90 seconds
                state = self.client.get_state(call_id)
                if state.is_answered and answered_at is None:
                    answered_at = time.time()
                    if on_event:
                        on_event("answered", {"provider_call_id": call_id})
                if state.is_terminal:
                    break
                time.sleep(1)

            if answered_at is not None:
                return CallResult(
                    status="answered",
                    duration=time.time() - answered_at,
                    provider_call_id=call_id,
                )
            if state.cause == "BUSY":
                return CallResult(status="busy", provider_call_id=call_id)
            if state.cause in ("NO_ANSWER", "TIMEOUT"):
                return CallResult(status="no_answer", provider_call_id=call_id)
            return CallResult(status="failed", provider_call_id=call_id)

        except Exception:
            log.exception("Call failed: from=%s to=%s", from_number, to_number)
            return CallResult(
                status="failed",
                provider_call_id=call_id,
                error_message="Call initiation failed",
            )
```

---

## Running the agent

### As a Python process

```bash
pip install -r requirements.txt
python3 main.py
```

You should see a banner, a version line, and `Connected to Bullseye
server`. If you see that, the agent is registered and waiting for tests.

### As a Docker container (recommended)

```bash
docker compose up -d --build
docker logs -f bullseye-agent
```

Same expected output. Restart policy is `unless-stopped` so it'll come
back automatically after a host reboot.

### Verifying it's working

1. Watch the logs while we send you a test from our side.
2. You should see something like:
   ```
   Test received: <test_id> from=+1... to=+1...
   Placing call via proprietary
   Result sent: status=answered duration=10.3
   ```

We will coordinate a smoke test with you the first time you bring the
agent up.

---

## Things we strongly recommend

- **Don't log credentials.** The agent already takes care of not sending
  raw exception text back to our server, but make sure your own
  `place_call()` doesn't print or log your network's API tokens.
- **Use TLS.** Connect the agent only to `https://...` server URLs —
  the agent will refuse `ws://` by default.
- **Run it under a process manager** (Docker's restart policy, systemd,
  whatever you use). The agent reconnects automatically on network
  blips, but the process itself needs to stay up.
- **Pin your dependencies.** If you use any external packages in your
  `place_call()`, add them to a separate `requirements.txt` and pin
  exact versions.
- **Test against your own number first.** Set the "to" number to a
  phone you control before pointing it at production destinations.

---

## What you *don't* have to think about

The agent handles all of this for you — none of it shows up in your code:

- WebSocket connection to the Bullseye server
- Authentication via your `BULLSEYE_API_KEY`
- Reconnection with exponential backoff if the connection drops
- Heartbeat / keepalive to detect dead connections
- Message protocol (test received, call event, result reported)
- Sanitizing exception text so internal errors don't leak to our server
- TLS enforcement on the server connection
- Concurrency control if multiple tests overlap

You only think about: "how do I place a call on my network?"

---