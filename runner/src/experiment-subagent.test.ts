// 子代理执行体测试（真实任务定义，模拟模型行为经 behaviors 注入）。
//
// 模拟场景：
// - 派发翻译子任务：mockOutput 落盘、pi-ai usage（input/output/cacheRead/cost）随结果回传、父子关联（taskId）
// - 同 callRef 重复派发：scanTasks 去重，复用既有 taskId，不新建任务
// - 复核角色子任务同路径跑通（注入不同 usage）
// - 子任务 call 崩溃后重开存储 resume：失败即终态不自动重跑、产物未写
// - 生产路径无 model 时明确报错（不静默模拟）
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, existsSync, readFileSync } from "node:fs";
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
import { behaviors, type SubagentResult } from "./experiment-subagent.ts";
import type { ToolSpec } from "./tool-spec.ts";

const MODEL = { provider: "mock", modelId: "mock-main" };

function makeFaux() {
  const faux = fauxProvider({ provider: "mock", models: [{ id: "mock-main" }] });
  const models = createModels();
  models.setProvider(faux.provider);
  return { faux, models };
}

function mockUsage(input: number, output: number): SubagentResult["usage"] {
  return { input, output, cacheRead: 0, totalTokens: input + output, costTotal: 0.001, costFromProvider: false };
}

function dispatchTool(log: Array<{ callRef: string; taskId: string; reused: boolean }>): ToolSpec {
  return {
    name: "experiment.dispatch",
    description: "派发翻译/复核子代理",
    parameters: {
      type: "object",
      properties: {
        role: { type: "string" },
        inputPath: { type: "string" },
        outputPath: { type: "string" },
        callRef: { type: "string" },
      },
      required: ["role", "inputPath", "outputPath", "callRef"],
    },
    replay: "safe",
    async execute(args, extras) {
      const input = args as { role: "translate" | "review"; inputPath: string; outputPath: string; callRef: string };
      assert.ok(extras?.dispatch, "dispatch extras 必须由适配层注入");
      const r = await extras.dispatch(input);
      log.push({ callRef: input.callRef, taskId: r.taskId, reused: r.reused });
      const result = r.result as unknown as SubagentResult;
      return {
        text: `${input.role} done → ${input.outputPath}（usage=${result.usage.input}+${result.usage.output}）`,
      };
    },
  };
}

function dispatchArgs(dir: string, role: string, callRef: string) {
  return {
    role,
    inputPath: join(dir, "en.srt"),
    outputPath: join(dir, `${role}.srt`),
    callRef,
  };
}

test("派发翻译子任务：mockOutput 落盘 + pi-ai usage 回传 + 父子关联", async () => {
  const dir = mkdtempSync(join(tmpdir(), "sub-test-"));
  const { faux, models } = makeFaux();
  const log: Array<{ callRef: string; taskId: string; reused: boolean }> = [];
  behaviors.set("ref-t1", { mockOutput: "[translate] 译文内容\n", mockUsage: mockUsage(100, 50), attempts: 0 });

  faux.setResponses([
    fauxAssistantMessage(
      [fauxToolCall("experiment.dispatch", dispatchArgs(dir, "translate", "ref-t1"))],
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage([fauxText("done")]),
  ]);

  const runner = await Runner.open(BACKGROUND_CONTEXT, {
    dbPath: join(dir, "session.sqlite"),
    models,
    model: MODEL,
    tools: [dispatchTool(log)],
  });
  const out = await runner.submit(BACKGROUND_CONTEXT, "翻译这一期");
  await runner.close(BACKGROUND_CONTEXT);

  assert.equal(out.status, "done");
  assert.equal(log.length, 1);
  assert.equal(log[0].reused, false);
  assert.ok(log[0].taskId.length > 0);
  assert.ok(existsSync(join(dir, "translate.srt")), "产物已落盘");
  assert.match(readFileSync(join(dir, "translate.srt"), "utf8"), /译文内容/);
  behaviors.delete("ref-t1");
  rmSync(dir, { recursive: true, force: true });
});

test("同 callRef 重复派发：scanTasks 去重复用 taskId", async () => {
  const dir = mkdtempSync(join(tmpdir(), "sub-test-"));
  const { faux, models } = makeFaux();
  const log: Array<{ callRef: string; taskId: string; reused: boolean }> = [];
  behaviors.set("ref-dup", { mockOutput: "x\n", mockUsage: mockUsage(10, 5), attempts: 0 });

  faux.setResponses([
    fauxAssistantMessage(
      [fauxToolCall("experiment.dispatch", dispatchArgs(dir, "translate", "ref-dup"))],
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage(
      [fauxToolCall("experiment.dispatch", dispatchArgs(dir, "translate", "ref-dup"))],
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage([fauxText("done")]),
  ]);

  const runner = await Runner.open(BACKGROUND_CONTEXT, {
    dbPath: join(dir, "session.sqlite"),
    models,
    model: MODEL,
    tools: [dispatchTool(log)],
  });
  const out = await runner.submit(BACKGROUND_CONTEXT, "翻译后再重复翻译");
  await runner.close(BACKGROUND_CONTEXT);

  assert.equal(out.status, "done");
  assert.equal(log.length, 2);
  assert.equal(log[0].reused, false);
  assert.equal(log[1].reused, true, "第二次应命中去重");
  assert.equal(log[0].taskId, log[1].taskId, "复用同一 taskId");
  assert.equal(behaviors.get("ref-dup")?.attempts, 1, "只执行一次");
  behaviors.delete("ref-dup");
  rmSync(dir, { recursive: true, force: true });
});

test("复核角色子任务同路径跑通（注入不同 usage）", async () => {
  const dir = mkdtempSync(join(tmpdir(), "sub-test-"));
  const { faux, models } = makeFaux();
  const log: Array<{ callRef: string; taskId: string; reused: boolean }> = [];
  behaviors.set("ref-r1", { mockOutput: "[review] 复核稿\n", mockUsage: mockUsage(800, 200), attempts: 0 });

  faux.setResponses([
    fauxAssistantMessage(
      [fauxToolCall("experiment.dispatch", dispatchArgs(dir, "review", "ref-r1"))],
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage([fauxText("done")]),
  ]);

  const runner = await Runner.open(BACKGROUND_CONTEXT, {
    dbPath: join(dir, "session.sqlite"),
    models,
    model: MODEL,
    tools: [dispatchTool(log)],
  });
  const out = await runner.submit(BACKGROUND_CONTEXT, "复核译文");
  await runner.close(BACKGROUND_CONTEXT);

  assert.equal(out.status, "done");
  assert.ok(existsSync(join(dir, "review.srt")));
  assert.equal(behaviors.get("ref-r1")?.attempts, 1);
  behaviors.delete("ref-r1");
  rmSync(dir, { recursive: true, force: true });
});

test("子任务崩溃后重开存储 resume：失败即终态不自动重跑", async () => {
  const dir = mkdtempSync(join(tmpdir(), "sub-test-"));
  const dbPath = join(dir, "session.sqlite");
  const log: Array<{ callRef: string; taskId: string; reused: boolean }> = [];
  // 第一次 call 直接 throw（模拟进程级崩溃前的执行中断）
  behaviors.set("ref-crash", { crashOnFirstCall: true, attempts: 0 });

  {
    const { faux, models } = makeFaux();
    faux.setResponses([
      fauxAssistantMessage(
        [fauxToolCall("experiment.dispatch", dispatchArgs(dir, "translate", "ref-crash"))],
        { stopReason: "toolUse" },
      ),
      fauxAssistantMessage([fauxText("done")]),
    ]);
    const runner = await Runner.open(BACKGROUND_CONTEXT, {
      dbPath,
      models,
      model: MODEL,
      tools: [dispatchTool(log)],
    });
    runner.root.submit({ type: "input", content: "翻译" }, BACKGROUND_CONTEXT).catch(() => {});
    await new Promise((r) => setTimeout(r, 800));
    await runner.close(BACKGROUND_CONTEXT).catch(() => {});
  }

  assert.equal(behaviors.get("ref-crash")?.attempts, 1, "第一次 call 已执行");
  assert.equal(existsSync(join(dir, "translate.srt")), false, "崩溃时产物未写");

  {
    const { faux, models } = makeFaux();
    faux.setResponses([fauxAssistantMessage([fauxText("recovered")])]);
    const runner = await Runner.open(BACKGROUND_CONTEXT, {
      dbPath,
      models,
      model: MODEL,
      tools: [dispatchTool(log)],
    });
    runner.resume();
    await runner.root.waitForIdle(BACKGROUND_CONTEXT);
    await runner.close(BACKGROUND_CONTEXT);
  }

  const attempts = behaviors.get("ref-crash")?.attempts;
  const produced = existsSync(join(dir, "translate.srt"));
  console.log("崩溃恢复结果: attempts =", attempts, "产物 =", produced, "（失败即终态，不自动重跑）");
  assert.equal(attempts, 1, "失败后不自动重跑");
  behaviors.delete("ref-crash");
  rmSync(dir, { recursive: true, force: true });
});

test("生产路径无 model 时明确报错（不静默模拟）", async () => {
  const dir = mkdtempSync(join(tmpdir(), "sub-test-"));
  // 不注入 behavior、不给 model → 真实执行体应抛「须指定 model」
  const { faux, models } = makeFaux();
  const log: Array<{ callRef: string; taskId: string; reused: boolean }> = [];
  faux.setResponses([
    fauxAssistantMessage(
      [fauxToolCall("experiment.dispatch", dispatchArgs(dir, "translate", "ref-nomodel"))],
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage([fauxText("done")]),
  ]);
  const runner = await Runner.open(BACKGROUND_CONTEXT, {
    dbPath: join(dir, "session.sqlite"),
    models,
    model: MODEL,
    tools: [dispatchTool(log)],
  });
  const out = await runner.submit(BACKGROUND_CONTEXT, "翻译（无 model）");
  await runner.close(BACKGROUND_CONTEXT);
  // 子任务失败（无 model 报错），产物未写
  assert.equal(existsSync(join(dir, "translate.srt")), false);
  rmSync(dir, { recursive: true, force: true });
});
