#!/usr/bin/env node
// End-to-end smoke for the running Windows sidecar: TCP bridge ->
// codex-acp -> Codex -> read-only radar query tool. It never touches the
// Discord token or Discord API.

import net from "node:net";
import { Readable, Writable } from "node:stream";
import * as acp from "../openab/.runtime/npm/node_modules/@agentclientprotocol/sdk/dist/acp.js";

const repoRoot = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1").replace(/\//g, "\\").replace(/\\$/, "");
const agentWorkingDir = `${repoRoot}\\openab\\sidecar`;
const socket = net.createConnection({ host: "127.0.0.1", port: 18765 });
await new Promise((resolve, reject) => {
  socket.once("connect", resolve);
  socket.once("error", reject);
});

const stream = acp.ndJsonStream(Writable.toWeb(socket), Readable.toWeb(socket));
let answer = "";
try {
  const result = await acp
    .client({ name: "radar-native-smoke", version: "1" })
    .onRequest(acp.methods.client.session.requestPermission, ({ params }) => ({
      outcome: {
        outcome: "selected",
        optionId: params.options.find((option) => option.kind === "allow_always")?.optionId ?? params.options[0].optionId,
      },
    }))
    .connectWith(stream, async (ctx) => {
      await ctx.request(acp.methods.agent.initialize, {
        protocolVersion: acp.PROTOCOL_VERSION,
        clientCapabilities: {},
      });
      return ctx.buildSession(agentWorkingDir).withSession(async (session) => {
        await ctx.request(acp.methods.agent.session.setConfigOption, {
          sessionId: session.sessionId,
          configId: "mode",
          value: "agent-full-access",
        });
        session.prompt(
          "依照 AGENTS.md，只用唯讀工具查詢最新 1 筆法拍案件。" +
          "若工具成功且有資料，最後一行只輸出 RADAR_DB_SMOKE_OK。"
        );
        for (;;) {
          const message = await session.nextUpdate();
          if (message.kind === "stop") return message.response;
          const update = message.notification.update;
          if (update.sessionUpdate === "agent_message_chunk" && update.content.type === "text") {
            answer += update.content.text;
          }
        }
      });
    });
  if (!answer.includes("RADAR_DB_SMOKE_OK")) {
    throw new Error(`agent did not confirm the read-only query; stopReason=${result.stopReason}; answer=${answer}`);
  }
  console.log("RADAR_DB_SMOKE_OK");
} finally {
  socket.destroy();
}
