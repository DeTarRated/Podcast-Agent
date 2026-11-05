// Runner 薄封装测试。
//
// 模拟场景：
// - 打开存储 → 提交输入 → settle 完成（mock 固定响应模型）
// - 关闭后重开同一 SQLite 存储：root 一致、checkpoint 可读、继续提交可用
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { createModels, fauxAssistantMessage, fauxProvider, fauxText } from "@earendil-works/pi-ai";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";

import { Runner } from "./runner.ts";
import { placeholderTools } from "./conversation.ts";

function makeFaux() {
  const faux = fauxProvider({ provider: "mock", models: [{ id: "mock-main" }] });
  const models = createModels();
  models.setProvider(faux.provider);
  return { faux, models };
}

const MODEL = { provider: "mock", modelId: "mock-main" };

test("打开→提交→完成；重开→checkpoint 可读→继续提交", async () => {
  const dir = mkdtempSync(join(tmpdir(), "runner-test-"));
  const dbPath = join(dir, "session.sqlite");
  const { faux, models } = makeFaux();

  faux.setResponses([fauxAssistantMessage([fauxText("done: ok")])]);
  const runner = await Runner.open(BACKGROUND_CONTEXT, {
    dbPath,
    models,
    model: MODEL,
    tools: placeholderTools(),
  });
  const first = await runner.submit(BACKGROUND_CONTEXT, "处理一期节目");
  assert.equal(first.status, "done");
  assert.equal(first.type, "input");
  await runner.close(BACKGROUND_CONTEXT);

  // 重开同一存储：root 一致，checkpoint 可读
  faux.setResponses([fauxAssistantMessage([fauxText("resumed: ok")])]);
  const runner2 = await Runner.open(BACKGROUND_CONTEXT, {
    dbPath,
    models,
    model: MODEL,
    tools: placeholderTools(),
  });
  runner2.resume();
  const cp = await runner2.checkpoint(BACKGROUND_CONTEXT);
  assert.equal(cp.rootId, String(runner2.root.id));
  const second = await runner2.submit(BACKGROUND_CONTEXT, "继续");
  assert.equal(second.status, "done");
  await runner2.close(BACKGROUND_CONTEXT);

  rmSync(dir, { recursive: true, force: true });
});
