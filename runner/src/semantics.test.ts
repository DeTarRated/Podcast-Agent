// 流程测试·单期 conversation 验证（进程内语义，不注入崩溃）。
//
// 模拟场景：
// - memo 第一次写赢（同一工具调用内两次写入同名 memo）
// - transcript 跨进程持久化：重开存储后模型可见前一轮的 user/assistant/toolResult 条目
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, readFileSync, writeFileSync, existsSync } from "node:fs";
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
import { memoProbeTool } from "./test-tools.ts";

const MODEL = { provider: "mock", modelId: "mock-main" };

function makeFaux() {
  const faux = fauxProvider({ provider: "mock", models: [{ id: "mock-main" }] });
  const models = createModels();
  models.setProvider(faux.provider);
  return { faux, models };
}

test("memo 第一次写赢：第二次写入同名 memo 返回第一次的值", async () => {
  const dir = mkdtempSync(join(tmpdir(), "sem-test-"));
  const dbPath = join(dir, "session.sqlite");
  const markerDir = join(dir, "markers");
  const { faux, models } = makeFaux();
  const fs = await import("node:fs");
  fs.mkdirSync(markerDir, { recursive: true });

  faux.setResponses([
    fauxAssistantMessage([fauxToolCall("test.memo_probe", { name: "k1" })], {
      stopReason: "toolUse",
    }),
    fauxAssistantMessage([fauxText("done")]),
  ]);

  const runner = await Runner.open(BACKGROUND_CONTEXT, {
    dbPath,
    models,
    model: MODEL,
    tools: [memoProbeTool({ markerDir })],
  });
  const out = await runner.submit(BACKGROUND_CONTEXT, "跑 memo 探针");
  assert.equal(out.status, "done");
  await runner.close(BACKGROUND_CONTEXT);

  const result = JSON.parse(readFileSync(join(markerDir, "memo-result.json"), "utf8"));
  assert.equal(result.first, "first");
  assert.equal(result.second, "first"); // 第二次写不覆盖
  rmSync(dir, { recursive: true, force: true });
});

test("transcript 跨进程持久化：重开后模型可见前一轮条目", async () => {
  const dir = mkdtempSync(join(tmpdir(), "sem-test-"));
  const dbPath = join(dir, "session.sqlite");
  const markerDir = join(dir, "markers");
  const fs = await import("node:fs");
  fs.mkdirSync(markerDir, { recursive: true });

  // 第一轮：工具调用 + 收尾（进程 A）
  {
    const { faux, models } = makeFaux();
    faux.setResponses([
      fauxAssistantMessage([fauxToolCall("test.memo_probe", { name: "k1" })], {
        stopReason: "toolUse",
      }),
      fauxAssistantMessage([fauxText("round1 done")]),
    ]);
    const runner = await Runner.open(BACKGROUND_CONTEXT, {
      dbPath,
      models,
      model: MODEL,
      tools: [memoProbeTool({ markerDir })],
    });
    const out = await runner.submit(BACKGROUND_CONTEXT, "第一轮输入");
    assert.equal(out.status, "done");
    await runner.close(BACKGROUND_CONTEXT);
  }

  // 第二轮（进程 B 模拟）：全新 faux，用 factory 检查模型看到的 transcript
  {
    const { faux, models } = makeFaux();
    faux.setResponses([
      (ctx) => {
        const record = {
          messageCount: ctx.messages.length,
          roles: ctx.messages.map((m) => m.role),
          hasToolResult: ctx.messages.some((m) => m.role === "toolResult"),
        };
        writeFileSync(join(markerDir, "transcript.json"), JSON.stringify(record));
        return fauxAssistantMessage([fauxText("round2 done")]);
      },
    ]);
    const runner = await Runner.open(BACKGROUND_CONTEXT, {
      dbPath,
      models,
      model: MODEL,
      tools: [memoProbeTool({ markerDir })],
    });
    const out = await runner.submit(BACKGROUND_CONTEXT, "第二轮输入");
    assert.equal(out.status, "done");
    await runner.close(BACKGROUND_CONTEXT);
  }

  assert.ok(existsSync(join(markerDir, "transcript.json")));
  const record = JSON.parse(readFileSync(join(markerDir, "transcript.json"), "utf8"));
  assert.ok(record.messageCount >= 4, `条目数 ${record.messageCount}`);
  assert.ok(record.roles.includes("toolResult"), "模型应看到前一轮的工具结果");
  assert.ok(record.roles.includes("user"), "模型应看到用户消息");
  rmSync(dir, { recursive: true, force: true });
});
