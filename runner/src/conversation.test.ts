// conversation 骨架与占位工具测试。
//
// 模拟场景：
// - replay 声明检查：download 显式 safe，transcribe 省略（默认 unsafe）
// - 模型脚本化调用工具：工具被执行、调用计数正确（mock 固定响应模型）
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  createModels,
  fauxAssistantMessage,
  fauxProvider,
  fauxText,
  fauxToolCall,
} from "@earendil-works/pi-ai";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";

import { Runner } from "./runner.ts";
import { episodeInput, placeholderTools } from "./conversation.ts";

test("replay 声明：download 为 safe、transcribe 省略（默认 unsafe）", () => {
  const [downloadTool, transcribeTool] = placeholderTools();
  assert.equal(downloadTool.name, "pipeline.download");
  assert.equal(downloadTool.replay, "safe");
  assert.equal(transcribeTool.name, "pipeline.transcribe");
  assert.equal(transcribeTool.replay, undefined);
});

test("模型脚本化调用工具：工具被执行且计数正确", async () => {
  const dir = mkdtempSync(join(tmpdir(), "conv-test-"));
  const dbPath = join(dir, "session.sqlite");
  const faux = fauxProvider({ provider: "mock", models: [{ id: "mock-main" }] });
  const models = createModels();
  models.setProvider(faux.provider);

  // 第一轮：模型发起工具调用；第二轮：收尾
  faux.setResponses([
    fauxAssistantMessage(
      [fauxToolCall("pipeline.download", { url: "http://127.0.0.1/a.mp3", dest: "/tmp/a.mp3" })],
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage([fauxText("done")]),
  ]);

  const counts = new Map<string, number>();
  const runner = await Runner.open(BACKGROUND_CONTEXT, {
    dbPath,
    models,
    model: { provider: "mock", modelId: "mock-main" },
    tools: placeholderTools(counts),
  });
  const out = await runner.submit(
    BACKGROUND_CONTEXT,
    episodeInput({ guid: "g-1", feed: "https://feeds.example.com/pl" }),
  );
  assert.equal(out.status, "done");
  assert.equal(counts.get("pipeline.download"), 1);
  assert.equal(counts.get("pipeline.transcribe"), undefined);
  await runner.close(BACKGROUND_CONTEXT);

  rmSync(dir, { recursive: true, force: true });
});
