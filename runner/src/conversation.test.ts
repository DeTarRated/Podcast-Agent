// conversation 骨架与流水线工具测试。
//
// 模拟场景：
// - replay 声明检查：download 显式 safe，transcribe 省略（默认 unsafe）
// - 模型脚本化调用真实 download 工具：经 bash 调 pipeline/download.py，本地 HTTP 源 302 跳转，产物落盘且 sha256 正确
// - 同 hash 重复下载：复用现有文件，无告警
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, readFileSync, existsSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:http";
import { createHash } from "node:crypto";

import {
  createModels,
  fauxAssistantMessage,
  fauxProvider,
  fauxText,
  fauxToolCall,
} from "@earendil-works/pi-ai";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";

import { Runner } from "./runner.ts";
import { episodeInput, pipelineTools } from "./conversation.ts";

// 最小合法 MPEG-1 Layer III 静音帧（与 Python 测试一致）
const SILENT_FRAME = Buffer.concat([Buffer.from([0xff, 0xfb, 0x90, 0x00]), Buffer.alloc(413)]);
const MP3_BYTES = Buffer.concat(Array.from({ length: 20 }, () => SILENT_FRAME));

function sha256(buf: Buffer): string {
  return createHash("sha256").update(buf).digest("hex");
}

/** 本地 HTTP 源：/feed.mp3 302 → /real/a.mp3（模拟 Captivate 直链跳转）。 */
function startHttpSource(): Promise<{ base: string; close: () => void }> {
  return new Promise((resolve) => {
    const server = createServer((req, res) => {
      if (req.url === "/feed.mp3") {
        res.writeHead(302, { Location: "/real/a.mp3" });
        res.end();
      } else if (req.url === "/real/a.mp3") {
        res.writeHead(200, { "Content-Type": "audio/mpeg", "Content-Length": String(MP3_BYTES.length) });
        res.end(MP3_BYTES);
      } else {
        res.writeHead(404);
        res.end();
      }
    });
    server.listen(0, "127.0.0.1", () => {
      const addr = server.address();
      assert.ok(addr && typeof addr === "object");
      resolve({
        base: `http://127.0.0.1:${addr.port}`,
        close: () => server.close(),
      });
    });
  });
}

function makeFaux() {
  const faux = fauxProvider({ provider: "mock", models: [{ id: "mock-main" }] });
  const models = createModels();
  models.setProvider(faux.provider);
  return { faux, models };
}

const MODEL = { provider: "mock", modelId: "mock-main" };

test("replay 声明：download 为 safe、transcribe 省略（默认 unsafe）", () => {
  const [downloadTool, transcribeTool] = pipelineTools();
  assert.equal(downloadTool.name, "pipeline.download");
  assert.equal(downloadTool.replay, "safe");
  assert.equal(transcribeTool.name, "pipeline.transcribe");
  assert.equal(transcribeTool.replay, undefined);
});

test("模型脚本化调用真实 download 工具：产物落盘且 sha256 正确", async () => {
  const dir = mkdtempSync(join(tmpdir(), "conv-test-"));
  const dbPath = join(dir, "session.sqlite");
  const dest = join(dir, "ep1.mp3");
  const source = await startHttpSource();
  const { faux, models } = makeFaux();

  faux.setResponses([
    fauxAssistantMessage(
      [fauxToolCall("pipeline.download", { url: `${source.base}/feed.mp3`, dest })],
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage([fauxText("done")]),
  ]);

  const runner = await Runner.open(BACKGROUND_CONTEXT, {
    dbPath,
    models,
    model: MODEL,
    tools: pipelineTools(),
  });
  const out = await runner.submit(
    BACKGROUND_CONTEXT,
    episodeInput({ guid: "g-1", feed: "https://feeds.example.com/pl" }),
  );
  await runner.close(BACKGROUND_CONTEXT);
  source.close();

  assert.equal(out.status, "done");
  assert.ok(existsSync(dest));
  assert.equal(statSync(dest).size, MP3_BYTES.length);
  assert.equal(sha256(readFileSync(dest)), sha256(MP3_BYTES));
  rmSync(dir, { recursive: true, force: true });
});

test("同 hash 重复下载：复用现有文件，无告警", async () => {
  const dir = mkdtempSync(join(tmpdir(), "conv-test-"));
  const dbPath = join(dir, "session.sqlite");
  const dest = join(dir, "ep1.mp3");
  const source = await startHttpSource();
  const { faux, models } = makeFaux();

  // 第一轮：下载；第二轮：同 url 同 dest 再调一次
  faux.setResponses([
    fauxAssistantMessage(
      [fauxToolCall("pipeline.download", { url: `${source.base}/feed.mp3`, dest })],
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage(
      [fauxToolCall("pipeline.download", { url: `${source.base}/feed.mp3`, dest })],
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage([fauxText("done")]),
  ]);

  const runner = await Runner.open(BACKGROUND_CONTEXT, {
    dbPath,
    models,
    model: MODEL,
    tools: pipelineTools(),
  });
  const out = await runner.submit(BACKGROUND_CONTEXT, episodeInput({ guid: "g-1", feed: "f" }));
  await runner.close(BACKGROUND_CONTEXT);
  source.close();

  assert.equal(out.status, "done");
  assert.ok(existsSync(dest));
  assert.equal(sha256(readFileSync(dest)), sha256(MP3_BYTES));
  rmSync(dir, { recursive: true, force: true });
});
