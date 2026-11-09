// 审核重跑与看点 dispatch 工具测试；模型由 behaviors mock，零真实付费调用。
//
// 模拟场景：
// - highlights JSON dispatch 经真实 Runner/ToolSpec 调用子任务并落盘
// - 付费工具 replay 省略（unsafe）
// - dispatch 文件缺 role 或必要路径时拒绝
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import {
  createModels,
  fauxAssistantMessage,
  fauxProvider,
  fauxText,
  fauxToolCall,
} from "@earendil-works/pi-ai";

import { Runner } from "./runner.ts";
import { behaviors } from "./experiment-subagent.ts";
import { readDispatchFile, reviewTools } from "./review-tools.ts";

const MODEL = { provider: "mock", modelId: "mock-main" };

test("highlights dispatch 落盘且付费工具为 unsafe", async () => {
  const directory = mkdtempSync(join(tmpdir(), "review-dispatch-"));
  const inputPath = join(directory, "approved.srt");
  const outputPath = join(directory, "highlights.json");
  const dispatchFile = join(directory, "dispatch.json");
  writeFileSync(inputPath, "1\n00:00:01,000 --> 00:00:03,000\n字幕\n");
  writeFileSync(dispatchFile, JSON.stringify({
    dispatch: {
      role: "highlights",
      inputPath,
      outputPath,
      callRef: "highlights:test:v1",
      model: MODEL,
    },
  }));
  behaviors.set("highlights:test:v1", {
    mockOutput: '{"summary":"摘要","items":[{"text":"看点","timestampMs":1000}]}',
    attempts: 0,
  });
  const faux = fauxProvider({ provider: "mock", models: [{ id: "mock-main" }] });
  const models = createModels();
  models.setProvider(faux.provider);
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall("review_dispatch", { dispatchFile })], { stopReason: "toolUse" }),
    fauxAssistantMessage([fauxText("done")]),
  ]);
  const tools = reviewTools();
  assert.equal(tools[0].replay, undefined);
  const runner = await Runner.open(BACKGROUND_CONTEXT, {
    dbPath: join(directory, "runner.sqlite"), models, model: MODEL, tools,
  });
  const outcome = await runner.submit(BACKGROUND_CONTEXT, "生成看点");
  await runner.close(BACKGROUND_CONTEXT);
  assert.equal(outcome.status, "done");
  assert.equal(JSON.parse(readFileSync(outputPath, "utf8")).summary, "摘要");
  assert.equal(behaviors.get("highlights:test:v1")?.attempts, 1);
  behaviors.delete("highlights:test:v1");
  rmSync(directory, { recursive: true, force: true });
});

test("dispatch 文件缺少必要字段时拒绝", () => {
  const directory = mkdtempSync(join(tmpdir(), "review-dispatch-invalid-"));
  const path = join(directory, "dispatch.json");
  writeFileSync(path, JSON.stringify({ dispatch: { role: "review" } }));
  assert.throws(() => readDispatchFile(path), /inputPath\/outputPath\/callRef/);
  rmSync(directory, { recursive: true, force: true });
});
