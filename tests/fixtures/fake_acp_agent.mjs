// Minimal stand-in for codex-acp used only by tests/test_openab_bridge.py:
// echoes each newline-delimited line back prefixed with "echo:", proving a
// full request/response roundtrip through both bridge hops without
// needing real Codex auth or the actual ACP JSON-RPC schema. The literal
// line "CRASH_NOW" makes it exit immediately (simulating a crashed agent)
// -- a cross-platform way to test the bridge's crash handling without
// needing an OS-specific "find and kill a child process" mechanism.
import { createInterface } from "node:readline";
const rl = createInterface({ input: process.stdin, terminal: false, crlfDelay: Infinity });
rl.on("line", (line) => {
  if (line === "CRASH_NOW") {
    process.exit(1);
  }
  process.stdout.write(`echo:${line}\n`);
});
