import RingCentral from "@rc-ex/core";
import Softphone from "ringcentral-softphone";
import express from "express";

const PORT = parseInt(process.env.SIDECAR_PORT || "3000");
// Bind to loopback by default. If someone genuinely needs to reach the
// sidecar from another host (e.g., Docker service-to-service on a private
// network), they must explicitly set SIDECAR_BIND=0.0.0.0 AND
// SIDECAR_AUTH_TOKEN.
const BIND = process.env.SIDECAR_BIND || "127.0.0.1";
const AUTH_TOKEN = process.env.SIDECAR_AUTH_TOKEN || "";

// Fallback if the caller doesn't specify hold_seconds in the POST body.
// The agent picks a random value per call to avoid a fixed-duration
// signature; this default only applies to direct sidecar callers.
const CALL_HOLD_SECONDS_DEFAULT = parseInt(process.env.CALL_HOLD_SECONDS || "45");
// Hard bounds — protect against a bug or malicious caller asking for a
// 24-hour hold.
const CALL_HOLD_MIN_SECONDS = 1;
const CALL_HOLD_MAX_SECONDS = 120;

const RC_SERVER = process.env.RINGCENTRAL_SERVER_URL || "https://platform.ringcentral.com";
const RC_CLIENT_ID = process.env.RINGCENTRAL_CLIENT_ID;
const RC_CLIENT_SECRET = process.env.RINGCENTRAL_CLIENT_SECRET;
const RC_JWT = process.env.RINGCENTRAL_JWT_TOKEN;

if (!RC_CLIENT_ID || !RC_CLIENT_SECRET || !RC_JWT) {
  console.error("Error: RINGCENTRAL_CLIENT_ID, RINGCENTRAL_CLIENT_SECRET, and RINGCENTRAL_JWT_TOKEN are required");
  process.exit(1);
}

if (BIND !== "127.0.0.1" && BIND !== "localhost" && !AUTH_TOKEN) {
  console.error(
    `Refusing to bind on ${BIND} without SIDECAR_AUTH_TOKEN. ` +
    "The sidecar can place chargeable calls; requiring a token when it's " +
    "not loopback-only prevents anyone with network access from placing calls. " +
    "Either set SIDECAR_BIND=127.0.0.1 (default) or set a strong SIDECAR_AUTH_TOKEN."
  );
  process.exit(1);
}

// --- State ---

let softphone = null;
let registeredNumber = null;
const calls = new Map(); // callId -> { status, startTime, answerTime, endTime, error }
let callCounter = 0;

// --- SIP Registration ---

async function initSoftphone() {
  console.log(`Authenticating with RingCentral at ${RC_SERVER}...`);

  const rc = new RingCentral({ server: RC_SERVER, clientId: RC_CLIENT_ID, clientSecret: RC_CLIENT_SECRET });
  await rc.authorize({ jwt: RC_JWT });

  // Find an OtherPhone device to register as
  const deviceList = await rc.restapi().account().extension().device().get();
  const devices = deviceList.records.filter((d) => d.type === "OtherPhone");

  if (devices.length === 0) {
    throw new Error(
      'No "OtherPhone" device found on this extension. ' +
      "Create one in the RingCentral admin portal (Phones & Devices > Add Device > Existing Phone)."
    );
  }

  const device = devices[0];
  console.log(`Using device: ${device.name} (${device.id})`);

  const sipInfo = await rc.restapi().account().device(device.id).sipInfo().get();
  await rc.revoke();

  registeredNumber = sipInfo.userName;
  console.log(`SIP user: ${registeredNumber}`);

  softphone = new Softphone({
    domain: sipInfo.domain,
    outboundProxy: sipInfo.outboundProxies[0].proxyTLS,
    username: sipInfo.userName,
    password: sipInfo.password,
    authorizationId: sipInfo.authorizationId,
  });

  await softphone.register();
  console.log("SIP registered — ready to place calls");
}

// --- Call Management ---

function clampHold(requested) {
  const n = Number.isFinite(requested) && requested > 0
    ? requested
    : CALL_HOLD_SECONDS_DEFAULT;
  return Math.max(CALL_HOLD_MIN_SECONDS, Math.min(CALL_HOLD_MAX_SECONDS, n));
}

function placeCall(toNumber, holdSecondsRequested) {
  const callId = `rc-${++callCounter}-${Date.now()}`;
  const hold = clampHold(holdSecondsRequested);

  calls.set(callId, {
    status: "dialing",
    to: toNumber,
    from: registeredNumber,
    startTime: Date.now(),
    answerTime: null,
    endTime: null,
    error: null,
  });

  // Run async call logic without blocking the HTTP response
  (async () => {
    try {
      const session = await softphone.call(toNumber);
      console.log(`[${callId}] Ringing ${toNumber} (hold ${hold}s)`);

      let hangupTimer = null;

      session.once("answered", () => {
        const call = calls.get(callId);
        call.status = "answered";
        call.answerTime = Date.now();
        console.log(`[${callId}] Answered`);

        hangupTimer = setTimeout(() => {
          console.log(`[${callId}] Hold time elapsed, hanging up`);
          try { session.hangup(); } catch (_) {}
        }, hold * 1000);
      });

      session.once("busy", () => {
        const call = calls.get(callId);
        if (call.status === "dialing") {
          call.status = "busy";
          call.endTime = Date.now();
          console.log(`[${callId}] Busy / unreachable`);
        }
      });

      session.once("disposed", () => {
        if (hangupTimer) clearTimeout(hangupTimer);
        const call = calls.get(callId);
        if (!call.endTime) {
          call.endTime = Date.now();
          if (call.status === "dialing") call.status = "no_answer";
        }
        console.log(`[${callId}] Disposed — final status: ${call.status}`);
      });

      // Safety timeout: ring budget + hold + slack.
      const safetyTimeoutMs = (hold + 45) * 1000;
      setTimeout(() => {
        const call = calls.get(callId);
        if (call && !call.endTime) {
          call.status = call.answerTime ? "answered" : "no_answer";
          call.endTime = Date.now();
          try { session.cancel(); } catch (_) {}
          try { session.hangup(); } catch (_) {}
          console.log(`[${callId}] Safety timeout — final status: ${call.status}`);
        }
      }, safetyTimeoutMs);

    } catch (err) {
      const call = calls.get(callId);
      call.status = "failed";
      call.endTime = Date.now();
      call.error = "Call placement failed";
      console.error(`[${callId}] Error: ${err.message}`);
    }
  })();

  return callId;
}

// --- HTTP API ---

const app = express();
app.use(express.json());

// Optional shared-secret auth. If SIDECAR_AUTH_TOKEN is set, every
// non-health request must send Authorization: Bearer <token>.  Health
// is unauthenticated so ops probes work; it exposes no chargeable action.
app.use((req, res, next) => {
  if (req.path === "/health") return next();
  if (!AUTH_TOKEN) return next();
  const header = req.get("Authorization") || "";
  const expected = `Bearer ${AUTH_TOKEN}`;
  if (header !== expected) {
    return res.status(401).json({ error: "Unauthorized" });
  }
  next();
});

app.get("/health", (_req, res) => {
  res.json({
    status: softphone ? "ready" : "initializing",
    registeredNumber,
  });
});

app.post("/call", (req, res) => {
  if (!softphone) {
    return res.status(503).json({ error: "Softphone not yet registered" });
  }

  const { to, hold_seconds } = req.body;
  if (!to) {
    return res.status(400).json({ error: "Missing 'to' field" });
  }

  // Strip leading + for SIP dialing (RC expects country code without +)
  const toClean = to.startsWith("+") ? to.slice(1) : to;
  const callId = placeCall(toClean, hold_seconds);

  res.status(201).json({
    call_id: callId,
    from: registeredNumber,
    to: toClean,
    status: "dialing",
  });
});

app.get("/call/:callId", (req, res) => {
  const call = calls.get(req.params.callId);
  if (!call) {
    return res.status(404).json({ error: "Call not found" });
  }

  const duration = call.endTime
    ? (call.endTime - call.startTime) / 1000
    : (Date.now() - call.startTime) / 1000;

  res.json({
    call_id: req.params.callId,
    status: call.status,
    from: call.from,
    to: call.to,
    duration,
    completed: call.endTime !== null,
    error: call.error,
  });
});

// --- Start ---

async function main() {
  try {
    await initSoftphone();
  } catch (err) {
    console.error(`Failed to initialize softphone: ${err.message}`);
    process.exit(1);
  }

  app.listen(PORT, BIND, () => {
    console.log(`Sidecar listening on http://${BIND}:${PORT}` +
      (AUTH_TOKEN ? " (auth required)" : " (loopback-only, no auth)"));
  });
}

main();
