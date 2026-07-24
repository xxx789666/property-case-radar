#!/usr/bin/env node
// Minimal ACP stdio<->TCP bridge server (sidecar side).
//
// Listens on a TCP port reachable only from the internal-only Compose
// network shared with the gateway container (see docker-compose.yml --
// that network has no route to anything else, and no other container can
// join it). On each connection, spawns the real `codex-acp` process and
// relays raw bytes bidirectionally between the socket and that child's
// stdio -- the same byte-for-byte relay as the gateway's bridge-client.mjs,
// so newline-delimited ACP message framing is preserved automatically and
// backpressure is handled by Node's own `pipe()` flow control.
//
// This process is what actually has Codex auth + the read-only database
// secret; it never touches the Discord token (which the gateway container
// never even hands to the network in any form -- the bridge carries only
// ACP JSON-RPC bytes, never a credential of any kind). Bounded disconnect:
// every terminal event (socket end/error/close, child exit/error) drives a
// single shutdown path with a hard timeout, so a wedged child or a half-
// closed socket can never leave this process hanging indefinitely.

import net from "node:net";
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";

const PORT = Number(process.env.ACP_SIDECAR_LISTEN_PORT || "8765");
const HOST = process.env.ACP_SIDECAR_LISTEN_HOST || "0.0.0.0";
// A single executable name/path, never a shell string -- spawn() below
// never runs through a shell, so there is no command-injection surface
// from this env var even though it's operator-controlled configuration,
// not attacker-controlled input.
const AGENT_COMMAND = process.env.ACP_AGENT_COMMAND || "codex-acp";
const AGENT_ARGS = (process.env.ACP_AGENT_ARGS || "").split(" ").filter((part) => part.length > 0);
// codex-acp discovers AGENTS.md from its own working directory (it does
// not traverse above it) -- this must match where AGENTS.md is actually
// COPYed to in Dockerfile.sidecar.
const AGENT_CWD = process.env.ACP_AGENT_CWD || "/workspace/radar-agent";
const SHUTDOWN_TIMEOUT_MS = Number(process.env.ACP_BRIDGE_SHUTDOWN_TIMEOUT_MS || "5000");
const DB_SECRET_PATH = process.env.RADAR_AGENT_DATABASE_URL_SECRET_FILE || "/run/secrets/radar_agent_database_url";

function auditLog(event, detail) {
  const line = JSON.stringify({ ts: new Date().toISOString(), component: "bridge-server", event, ...detail });
  process.stderr.write(line + "\n");
}

// Read once at startup, not per-connection: fail closed immediately
// rather than accepting connections that would just fail later. Read
// directly into a local variable, never assigned to this process's own
// process.env -- it is only ever placed into the *spawned codex-acp
// child's* explicit env object below, so this parent process's own
// /proc/<pid>/environ never contains it (the same reasoning as
// openab/gateway/container-entrypoint.sh's token handling: an immutable
// exec-time environment snapshot is the wrong place to keep a secret this
// process itself doesn't need).
let databaseUrl;
try {
  databaseUrl = readFileSync(DB_SECRET_PATH, "utf8").trim();
} catch (err) {
  auditLog("fatal", { reason: `cannot read ${DB_SECRET_PATH}: ${err.message}` });
  process.exit(1);
}
if (!databaseUrl) {
  auditLog("fatal", { reason: `${DB_SECRET_PATH} is empty` });
  process.exit(1);
}

let connectionCounter = 0;

function handleConnection(socket) {
  const connectionId = ++connectionCounter;
  auditLog("connection_open", { connectionId, remote: `${socket.remoteAddress}:${socket.remotePort}` });

  const child = spawn(AGENT_COMMAND, AGENT_ARGS, {
    stdio: ["pipe", "pipe", "inherit"],
    cwd: AGENT_CWD,
    env: { ...process.env, DATABASE_URL: databaseUrl },
  });
  let bytesToAgent = 0;
  let bytesFromAgent = 0;
  let shuttingDown = false;
  let halfCloseTimer = null;

  // crashed=true drives an ABORTIVE close (TCP RST via resetAndDestroy(),
  // Node >=16.17) instead of a graceful FIN (socket.end()) -- this is what
  // lets bridge-client.mjs's own socket "error" handler (already wired to
  // a nonzero exit) distinguish "the agent crashed/hung" from "the session
  // ended normally", without inventing an out-of-band signal inside the
  // raw ACP byte stream itself. A graceful client-initiated disconnect (the
  // client's own stdin ending) is never treated as a crash.
  function shutdown(reason, { crashed = false } = {}) {
    if (shuttingDown) return;
    shuttingDown = true;
    if (halfCloseTimer) {
      clearTimeout(halfCloseTimer);
      halfCloseTimer = null;
    }
    auditLog("connection_close", { connectionId, reason, crashed, bytesToAgent, bytesFromAgent });
    const forceTimer = setTimeout(() => {
      auditLog("connection_close_forced", { connectionId, reason: "graceful shutdown exceeded bound" });
      try {
        child.kill("SIGKILL");
      } catch {
        // already gone
      }
      try {
        socket.destroy();
      } catch {
        // already gone
      }
    }, SHUTDOWN_TIMEOUT_MS);
    forceTimer.unref();
    try {
      child.kill("SIGTERM");
    } catch {
      // already gone
    }
    try {
      if (crashed) {
        socket.resetAndDestroy();
      } else {
        socket.end();
      }
    } catch {
      // already gone
    }
  }

  socket.on("data", (chunk) => {
    bytesToAgent += chunk.length;
  });
  child.stdout.on("data", (chunk) => {
    bytesFromAgent += chunk.length;
  });

  // { end: false } on both: pipe()'s default behavior of automatically
  // calling .end() on the destination the instant the source ends would
  // otherwise race with -- and silently win over -- our own explicit,
  // crash-aware shutdown() logic below (an automatic graceful socket.end()
  // triggered the moment child.stdout ends always beat our own child
  // "close" handler's resetAndDestroy() call in practice, since the
  // automatic end's own "close" event grabbed the shuttingDown guard
  // first). Ending both stdin/the socket is handled entirely explicitly
  // here instead: child.stdin.end() in the socket "end" handler, and
  // socket.end()/resetAndDestroy() in shutdown().
  socket.pipe(child.stdin, { end: false });
  child.stdout.pipe(socket, { end: false });

  socket.on("error", (err) => shutdown(`socket_error: ${err.message}`, { crashed: true }));
  socket.on("close", () => shutdown("socket_closed"));
  socket.on("end", () => {
    // Client half-closed (its own stdin ended) -- half-close towards the
    // agent so it can finish, without forcing an immediate kill. Bounded:
    // if the agent doesn't actually exit within SHUTDOWN_TIMEOUT_MS of
    // this (e.g. it ignores EOF on its stdin and hangs), treat that as a
    // crash-equivalent and force the connection closed rather than
    // leaving it open indefinitely.
    try {
      child.stdin.end();
    } catch {
      // already gone
    }
    halfCloseTimer = setTimeout(() => {
      shutdown("half_close_timeout: agent did not exit after client stdin EOF", { crashed: true });
    }, SHUTDOWN_TIMEOUT_MS);
    halfCloseTimer.unref();
  });
  child.on("error", (err) => shutdown(`agent_spawn_error: ${err.message}`, { crashed: true }));
  // "close" (not "exit"): fires only after the child's stdio streams have
  // themselves been fully drained/closed -- "exit" can fire while
  // child.stdout still has buffered, unread data in flight through the
  // pipe() to the socket, which previously truncated large (2MB+)
  // responses by ending the socket before the last chunk(s) had been
  // written out. See tests/test_openab_bridge.py's large-payload test.
  child.on("close", (code, signal) => {
    const crashed = signal !== null || (typeof code === "number" && code !== 0);
    shutdown(`agent_exited: code=${code} signal=${signal}`, { crashed });
  });
}

// allowHalfOpen: true is required for correctness, not just an option --
// net.createServer()'s default (false) makes Node auto-end this socket's
// OWN write side the instant the client's FIN arrives (readable end),
// regardless of how much of child.stdout's response has actually been
// piped through yet. Since the client (bridge-client.mjs) always finishes
// sending its request and half-closes before the response is fully
// received (that's the normal request/response shape of one ACP
// round-trip), the auto-end raced with -- and silently truncated -- any
// response still streaming from the agent at that moment (reproducibly
// cut large multi-hundred-KB+ payloads short at whatever had been queued
// so far). With allowHalfOpen: true, ending the read side no longer
// implicitly ends the write side; this code already ends the write side
// itself, deliberately, once the agent has actually finished (see the
// child "close" handler below).
const server = net.createServer({ allowHalfOpen: true }, handleConnection);
server.on("error", (err) => {
  auditLog("fatal", { reason: `listen_error: ${err.message}` });
  process.exit(1);
});
server.listen(PORT, HOST, () => {
  auditLog("listening", { host: HOST, port: PORT, agentCommand: AGENT_COMMAND });
});

for (const sig of ["SIGTERM", "SIGINT"]) {
  process.on(sig, () => {
    auditLog("server_shutdown", { signal: sig });
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), SHUTDOWN_TIMEOUT_MS).unref();
  });
}
