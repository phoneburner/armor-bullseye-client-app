import json
import logging
import os
import shutil
import subprocess
import sys
import time
import requests
from .base import TelephonyProvider, CallResult, CallEventCallback, classify_generic


def _classify_http(status: int | None, body: str | None) -> tuple[str, str]:
    """Classify a sidecar HTTP failure into (category, safe message)."""
    if status in (401, 403):
        return ("auth_error",
                "RingCentral credentials rejected by the sidecar — check "
                "RINGCENTRAL_CLIENT_ID / SECRET and RINGCENTRAL_JWT_TOKEN.")
    if status == 429:
        return ("rate_limited", "RingCentral is rate-limiting this account.")
    if status == 503:
        return ("call_setup_failed",
                "RingCentral sidecar is not ready — SIP registration may be down.")
    if status is not None:
        return ("provider_error", f"RingCentral sidecar returned HTTP {status}.")
    return ("provider_error", "RingCentral sidecar request failed.")

log = logging.getLogger("bullseye.ringcentral")

DEFAULT_SIDECAR_URL = "http://127.0.0.1:3000"
SIDECAR_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ringcentral-sidecar")


class RingCentralProvider(TelephonyProvider):
    def __init__(self):
        self.base_url = os.environ.get("RINGCENTRAL_SIDECAR_URL", DEFAULT_SIDECAR_URL).rstrip("/")
        self.port = int(self.base_url.rsplit(":", 1)[-1].split("/")[0]) if ":" in self.base_url else 3000
        self._sidecar_process = None

        # Check if required RC env vars are set
        for var in ("RINGCENTRAL_CLIENT_ID", "RINGCENTRAL_CLIENT_SECRET", "RINGCENTRAL_JWT_TOKEN"):
            if not os.environ.get(var):
                raise ValueError(f"{var} is required for the RingCentral provider")

        # If sidecar is already running (Docker mode), just verify health
        if self._sidecar_healthy():
            log.info("Connected to existing RingCentral sidecar at %s", self.base_url)
            return

        # Not running — try to start it (Python-from-source mode)
        self._start_sidecar()

    def _sidecar_healthy(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/health", timeout=2)
            return resp.ok and resp.json().get("status") == "ready"
        except Exception:
            return False

    def _start_sidecar(self):
        if not os.path.isdir(SIDECAR_DIR):
            raise RuntimeError(
                f"RingCentral sidecar not found at {SIDECAR_DIR}. "
                "Ensure the ringcentral-sidecar/ directory is present."
            )

        node = shutil.which("node")
        npm = shutil.which("npm")
        if not node or not npm:
            raise RuntimeError(
                "Node.js is required for the RingCentral provider. "
                "Install Node.js 18+ from https://nodejs.org"
            )

        # Install dependencies if needed
        node_modules = os.path.join(SIDECAR_DIR, "node_modules")
        if not os.path.isdir(node_modules):
            log.info("Installing RingCentral sidecar dependencies...")
            subprocess.run(
                [npm, "install", "--omit=dev"],
                cwd=SIDECAR_DIR,
                check=True,
                capture_output=True,
            )

        log.info("Starting RingCentral sidecar...")
        self._sidecar_process = subprocess.Popen(
            [node, "index.js"],
            cwd=SIDECAR_DIR,
            env={**os.environ, "SIDECAR_PORT": str(self.port)},
            stdout=sys.stdout,
            stderr=sys.stderr,
        )

        # Wait for it to become healthy
        for i in range(30):
            time.sleep(1)
            if self._sidecar_process.poll() is not None:
                raise RuntimeError("RingCentral sidecar exited during startup. Check logs above.")
            if self._sidecar_healthy():
                log.info("RingCentral sidecar ready at %s", self.base_url)
                return

        self._sidecar_process.kill()
        raise RuntimeError("RingCentral sidecar failed to become ready within 30 seconds")

    def preflight(self) -> None:
        # The sidecar's /health endpoint confirms it's up and SIP-registered.
        # If the sidecar isn't running, this raises via requests.
        resp = requests.get(f"{self.base_url}/health", timeout=5)
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "ready":
            raise RuntimeError(f"RingCentral sidecar reports status={body.get('status')!r}")

    def place_call(self, from_number: str, to_number: str, on_event: CallEventCallback | None = None) -> CallResult:
        log.info("Dialing %s -> %s via RingCentral", from_number, to_number)

        try:
            resp = requests.post(
                f"{self.base_url}/call",
                json={"to": to_number},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            call_id = data["call_id"]
            log.info("Call initiated: call_id=%s", call_id)
        except requests.HTTPError as e:
            log.error("Dial failed: %s", e)
            category, msg = _classify_http(e.response.status_code if e.response is not None else None, None)
            return CallResult(status="failed", error_message=msg, error_category=category)
        except Exception as e:
            log.error("Dial failed: %s", e)
            category, msg = classify_generic(e)
            return CallResult(status="failed", error_message=msg, error_category=category)

        if on_event:
            on_event("dialing", {"provider_call_id": call_id})

        start_time = time.time()
        max_wait = 120
        poll_interval = 1
        answered = False

        while time.time() - start_time < max_wait:
            time.sleep(poll_interval)
            try:
                resp = requests.get(f"{self.base_url}/call/{call_id}", timeout=5)
                data = resp.json()
                status = data["status"]
                duration = data["duration"]

                if status == "answered" and not answered:
                    answered = True
                    if on_event:
                        on_event("answered", {"provider_call_id": call_id, "duration": duration})

                if data.get("completed"):
                    if on_event:
                        on_event("done", {"status": status, "duration": duration, "provider_call_id": call_id})
                    return CallResult(status=status, duration=duration, provider_call_id=call_id)

            except Exception as e:
                log.warning("Poll error: %s", e)
                continue

        duration = time.time() - start_time
        final_status = "answered" if answered else "no_answer"
        if on_event:
            on_event("done", {"status": final_status, "duration": duration, "provider_call_id": call_id})
        return CallResult(status=final_status, duration=duration, provider_call_id=call_id)
