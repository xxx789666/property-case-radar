#!/usr/bin/env node

import http from "node:http";
import path from "node:path";
import {
  appendFileSync,
  readFileSync,
  readdirSync,
  realpathSync,
  statSync,
} from "node:fs";

const HOST = "127.0.0.1";
const PORT = Number(process.env.RADAR_PDF_BROKER_PORT || "18766");
const TOKEN = process.env.OPENAB_DISCORD_BOT_TOKEN || "";
const DOWNLOAD_ROOT_RAW = process.env.RADAR_AUCTION_DOWNLOAD_DIR || "";
const ALLOWED_PARENT_IDS = new Set(
  (process.env.RADAR_PDF_ALLOWED_PARENT_IDS || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean),
);
const LOG_PATH = process.env.RADAR_PDF_BROKER_LOG || "";
const PARENT_PID = Number(process.env.RADAR_PDF_BROKER_PARENT_PID || "0");
const API_BASE = process.env.RADAR_DISCORD_API_BASE_URL || "https://discord.com/api/v10";
const MAX_BODY_BYTES = 16 * 1024;
const MAX_FILE_BYTES = 25 * 1024 * 1024;
const MAX_FILES = 10;

function audit(event, detail = {}) {
  const record = JSON.stringify({
    ts: new Date().toISOString(),
    component: "pdf-upload-broker",
    event,
    ...detail,
  });
  if (LOG_PATH) {
    try {
      appendFileSync(LOG_PATH, record + "\n", "utf8");
    } catch {
      // Operational logging must not reveal the token or crash mid-response.
    }
  }
  process.stderr.write(record + "\n");
}

function fatal(reason) {
  audit("fatal", { reason });
  process.exit(2);
}

if (!TOKEN) fatal("OPENAB_DISCORD_BOT_TOKEN is required");
if (!DOWNLOAD_ROOT_RAW) fatal("RADAR_AUCTION_DOWNLOAD_DIR is required");
if (ALLOWED_PARENT_IDS.size === 0) fatal("RADAR_PDF_ALLOWED_PARENT_IDS is required");
if (![...ALLOWED_PARENT_IDS].every((value) => /^[0-9]+$/.test(value))) {
  fatal("RADAR_PDF_ALLOWED_PARENT_IDS contains an invalid channel id");
}
if (!Number.isInteger(PORT) || PORT <= 0 || PORT > 65535) fatal("invalid broker port");

let downloadRoot;
try {
  downloadRoot = realpathSync(DOWNLOAD_ROOT_RAW);
} catch (err) {
  fatal(`download root is unavailable: ${err.message}`);
}

function isSafeComponent(value, maxLength = 80) {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= maxLength &&
    value !== "." &&
    value !== ".." &&
    !value.includes("\0") &&
    !value.includes("/") &&
    !value.includes("\\")
  );
}

function isWithinRoot(candidate) {
  const relative = path.relative(downloadRoot, candidate);
  return relative !== "" && !relative.startsWith("..") && !path.isAbsolute(relative);
}

function findOriginalPdfs({ city, district, case_number: caseNumber }) {
  const directories = [
    path.join(downloadRoot, city, district, caseNumber),
    path.join(downloadRoot, city, caseNumber),
    path.join(downloadRoot, caseNumber),
  ];
  const filenamePattern = new RegExp(`^${caseNumber}_[0-9]+_[0-9]+\\.pdf$`, "i");
  const found = new Map();
  for (const directory of directories) {
    let resolved;
    try {
      resolved = realpathSync(directory);
    } catch {
      continue;
    }
    if (!isWithinRoot(resolved)) continue;
    for (const entry of readdirSync(resolved, { withFileTypes: true })) {
      if (!entry.isFile() || !filenamePattern.test(entry.name)) continue;
      const filePath = realpathSync(path.join(resolved, entry.name));
      if (!isWithinRoot(filePath)) continue;
      const size = statSync(filePath).size;
      if (size > MAX_FILE_BYTES) {
        throw new Error(`${entry.name} 超過 Discord 25 MB 附件上限`);
      }
      found.set(entry.name, { filePath, size });
    }
  }
  return [...found.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .slice(0, MAX_FILES)
    .map(([filename, info]) => ({ filename, ...info }));
}

async function discordRequest(route, options = {}) {
  const response = await fetch(`${API_BASE}${route}`, {
    ...options,
    signal: AbortSignal.timeout(15000),
    headers: {
      Authorization: `Bot ${TOKEN}`,
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  if (!response.ok) {
    throw new Error(`Discord API ${response.status}: ${payload?.message || "request failed"}`);
  }
  return payload;
}

async function verifyAllowedThread(threadId) {
  const channel = await discordRequest(`/channels/${threadId}`);
  const parentId = channel?.parent_id;
  if (!parentId || !ALLOWED_PARENT_IDS.has(String(parentId))) {
    throw new Error("指定頻道不屬於允許的法拍案件頻道");
  }
  return channel;
}

async function uploadPdfs(threadId, files, caseNumber) {
  const form = new FormData();
  const attachments = files.map((file, index) => {
    form.append(`files[${index}]`, new Blob([readFileSync(file.filePath)], {
      type: "application/pdf",
    }), file.filename);
    return { id: String(index), filename: file.filename };
  });
  form.append("payload_json", JSON.stringify({
    content: `法院原始 PDF｜案號 ${caseNumber}`,
    attachments,
    allowed_mentions: { parse: [] },
  }));
  return discordRequest(`/channels/${threadId}/messages`, {
    method: "POST",
    body: form,
  });
}

function sendJson(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store",
  });
  response.end(body);
}

async function handleUpload(request, response) {
  let body = "";
  for await (const chunk of request) {
    body += chunk;
    if (Buffer.byteLength(body) > MAX_BODY_BYTES) {
      sendJson(response, 413, { error: "request body too large" });
      return;
    }
  }
  let input;
  try {
    input = JSON.parse(body);
  } catch {
    sendJson(response, 400, { error: "invalid JSON" });
    return;
  }
  const { thread_id: threadId, city, district, case_number: caseNumber } = input || {};
  if (!/^[0-9]+$/.test(threadId || "") || !/^[0-9]+$/.test(caseNumber || "")) {
    sendJson(response, 400, { error: "thread_id and case_number must contain digits only" });
    return;
  }
  if (!isSafeComponent(city) || !isSafeComponent(district)) {
    sendJson(response, 400, { error: "invalid city or district" });
    return;
  }

  try {
    const channel = await verifyAllowedThread(threadId);
    const files = findOriginalPdfs({ city, district, case_number: caseNumber });
    if (files.length === 0) {
      sendJson(response, 404, { error: "找不到法院原始 PDF" });
      return;
    }
    const message = await uploadPdfs(threadId, files, caseNumber);
    audit("upload_ok", {
      thread_id: threadId,
      case_number: caseNumber,
      message_id: message?.id || null,
      filenames: files.map((file) => file.filename),
    });
    sendJson(response, 200, {
      ok: true,
      message_id: message?.id || null,
      message_url: (
        channel?.guild_id && message?.id
          ? `https://discord.com/channels/${channel.guild_id}/${threadId}/${message.id}`
          : null
      ),
      filenames: files.map((file) => file.filename),
      attachments: (message?.attachments || []).map((attachment) => ({
        filename: attachment.filename,
        url: attachment.url,
      })),
    });
  } catch (err) {
    audit("upload_failed", {
      thread_id: threadId,
      case_number: caseNumber,
      reason: err.message,
    });
    sendJson(response, 502, { error: err.message });
  }
}

const server = http.createServer(async (request, response) => {
  if (request.method === "GET" && request.url === "/health") {
    sendJson(response, 200, { ok: true });
    return;
  }
  if (request.method === "POST" && request.url === "/upload-auction-pdf") {
    await handleUpload(request, response);
    return;
  }
  sendJson(response, 404, { error: "not found" });
});

server.on("error", (err) => fatal(`listen failed: ${err.message}`));
server.listen(PORT, HOST, () => audit("listening", { host: HOST, port: PORT }));

if (Number.isInteger(PARENT_PID) && PARENT_PID > 0) {
  setInterval(() => {
    try {
      process.kill(PARENT_PID, 0);
    } catch {
      audit("parent_gone", { parent_pid: PARENT_PID });
      server.close(() => process.exit(0));
    }
  }, 5000).unref();
}

for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, () => server.close(() => process.exit(0)));
}
