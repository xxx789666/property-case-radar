#!/usr/bin/env node
// Minimal ACP stdio<->TCP bridge client.
//
// OpenAB has no built-in support for a remote ACP agent -- its [agent]
// config always spawns a local subprocess and talks ACP (newline-delimited
// JSON-RPC, confirmed directly from the ACP TypeScript SDK's
// dist/line-buffer.js, which is what codex-acp itself is built on) over
// that subprocess's stdin/stdout. This script IS that subprocess from
// OpenAB's point of view -- but instead of running codex-acp locally, it
// opens exactly one TCP connection to the codex-acp sidecar (a *different*
// container, reachable only over an internal-only Compose network) and
// relays raw bytes in both directions.
//
// This has no shell, no arbitrary exec, and no other capability: it is a
// pure byte relay between two file descriptors (stdio) and one socket.
// A gateway process compromised via a malicious Discord message cannot use
// this bridge to do anything beyond what a normal ACP stdio subprocess
// could already do -- there is no command channel, no second protocol, and
// nothing here parses or acts on message content.
//
// Framing: relayed via `stream.pipe()` in both directions, which forwards
// bytes verbatim regardless of how the source chunks them -- newline
// message boundaries are therefore preserved automatically, they are
// never re-parsed or re-serialized by this script.
// Backpressure: `pipe()` is Node's own flow-controlled relay -- it pauses
// the source when the destination's internal buffer is full and resumes
// it on 'drain', so a slow sidecar (or a slow OpenAB) cannot make this
// process buffer unboundedly in memory.
// Bounded disconnect: every terminal event on either side (stdin end,
// stdout error, socket close/error/timeout) triggers a single shutdown
// path that closes the other side and exits within SHUTDOWN_TIMEOUT_MS,
// never hanging indefinitely.

import net from "node:net";

const HOST = process.env.ACP_SIDECAR_HOST;
const PORT_RAW = process.env.ACP_SIDECAR_PORT;
const CONNECT_TIMEOUT_MS = Number(process.env.ACP_BRIDGE_CONNECT_TIMEOUT_MS || "5000");
const SHUTDOWN_TIMEOUT_MS = Number(process.env.ACP_BRIDGE_SHUTDOWN_TIMEOUT_MS || "5000");

function auditLog(event, detail) {
  const line = JSON.stringify({ ts: new Date().toISOString(), component: "bridge-client", event, ...detail });
  process.stderr.write(line + "\n");
}

if (!HOST || !PORT_RAW) {
  auditLog("fatal", { reason: "ACP_SIDECAR_HOST/ACP_SIDECAR_PORT must both be set" });
  process.exit(2);
}
const PORT = Number(PORT_RAW);
if (!Number.isInteger(PORT) || PORT <= 0 || PORT > 65535) {
  auditLog("fatal", { reason: `ACP_SIDECAR_PORT must be a valid port number, got ${JSON.stringify(PORT_RAW)}` });
  process.exit(2);
}

let shuttingDown = false;
let bytesToSidecar = 0;
let bytesFromSidecar = 0;
let halfCloseTimer = null;

function shutdown(exitCode, reason) {
  if (shuttingDown) return;
  shuttingDown = true;
  if (halfCloseTimer) {
    clearTimeout(halfCloseTimer);
    halfCloseTimer = null;
  }
  auditLog("shutdown", { reason, bytesToSidecar, bytesFromSidecar });
  const forceTimer = setTimeout(() => {
    auditLog("shutdown_forced", { reason: "graceful shutdown exceeded bound" });
    process.exit(exitCode);
  }, SHUTDOWN_TIMEOUT_MS);
  forceTimer.unref();
  try {
    socket.destroy();
  } catch {
    // already gone
  }
  process.exitCode = exitCode;
  // Let the event loop drain naturally (stdout flush etc.); the timer
  // above is the bound in case something doesn't finish on its own.
  setImmediate(() => process.exit(exitCode));
}

const socket = net.createConnection({ host: HOST, port: PORT });
socket.setTimeout(CONNECT_TIMEOUT_MS);

socket.on("connect", () => {
  socket.setTimeout(0); // bound only applies to establishing the connection
  auditLog("connected", { host: HOST, port: PORT });
});

socket.on("timeout", () => {
  shutdown(1, "connect_timeout");
});

socket.on("error", (err) => {
  shutdown(1, `socket_error: ${err.message}`);
});

socket.on("close", () => {
  shutdown(0, "socket_closed");
});

process.stdin.on("error", (err) => shutdown(1, `stdin_error: ${err.message}`));
process.stdout.on("error", (err) => shutdown(1, `stdout_error: ${err.message}`));

process.stdin.on("data", (chunk) => {
  bytesToSidecar += chunk.length;
});
socket.on("data", (chunk) => {
  bytesFromSidecar += chunk.length;
});

// The actual relay: two independent, backpressure-aware pipes.
process.stdin.pipe(socket);
socket.pipe(process.stdout);

process.stdin.on("end", () => {
  // OpenAB closed its write side -- half-close towards the sidecar so it
  // can finish responding to anything already in flight, then let the
  // socket's own 'close' handler above drive the final exit. Bounded: if
  // the sidecar never actually closes its end within SHUTDOWN_TIMEOUT_MS
  // of this half-close (e.g. it's wedged), force the connection closed
  // rather than hanging indefinitely waiting for a 'close' that may never
  // come.
  socket.end();
  halfCloseTimer = setTimeout(() => {
    shutdown(1, "half_close_timeout: sidecar did not close after our stdin EOF");
  }, SHUTDOWN_TIMEOUT_MS);
  halfCloseTimer.unref();
});

for (const sig of ["SIGTERM", "SIGINT", "SIGHUP"]) {
  process.on(sig, () => shutdown(0, `signal_${sig}`));
}
