// 流程测试·kill -9 崩溃恢复实验。
//
// 模拟场景：
// - unsafe 工具在途被 kill -9：绝不自动重跑，模型收到 interrupted 错误结果
// - safe 工具在途被 kill -9：重开后自动重跑并完成
// - 模型请求在途被 kill -9：重开后请求重发并完成
// - 连续崩溃触发 restart_limit 熔断（restartLimit=2）：工具不再重跑，模型收到错误结果
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { createModels, fauxAssistantMessage, fauxProvider, fauxText } from "@earendil-works/pi-ai";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";

import { Runner } from "./runner.ts";
import { setSlowMs, slowSafeTool, slowUnsafeTool } from "./test-tools.ts";

const CHILD = join(import.meta.dirname, "crash-child.ts");
const MODEL = { provider: "mock", modelId: "mock-main" };

function makeFaux() {
  const faux = fauxProvider({ provider: "mock", models: [{ id: "mock-main" }] });
  const models = createModels();
  models.setProvider(faux.provider);
  return { faux, models };
}

interface ChildProc {
  pid: number;
  exited: Promise<void>;
}

function spawnChild(args: string[], env: Record<string, string> = {}): ChildProc {
  const child = spawn(process.execPath, [CHILD, ...args], {
    env: { ...process.env, ...env },
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.resume();
  child.stderr.resume();
  const exited = new Promise<void>((resolve) => child.on("exit", () => resolve()));
  return { pid: child.pid!, exited };
}

async function waitForFile(path: string, timeoutMs: number): Promise<void> {
  const start = Date.now();
  while (!existsSync(path)) {
    if (Date.now() - start > timeoutMs) {
      throw new Error(`等待文件超时: ${path}`);
    }
    await new Promise((r) => setTimeout(r, 50));
  }
}

function kill9(pid: number): void {
  process.kill(pid, "SIGKILL");
}

function setupDir(): { dir: string; dbPath: string; markerDir: string } {
  const dir = mkdtempSync(join(tmpdir(), "kill9-"));
  const markerDir = join(dir, "markers");
  mkdirSync(markerDir, { recursive: true });
  return { dir, dbPath: join(dir, "session.sqlite"), markerDir };
}

function execCount(markerDir: string): number {
  return Number(readFileSync(join(markerDir, "exec-count.txt"), "utf8"));
}

/** 记录模型看到的最后一条 toolResult 的 factory。 */
function recordingFactory(markerDir: string, text: string) {
  return (ctx: { messages: { role: string; isError?: boolean; toolName?: string }[] }) => {
    const last = ctx.messages.at(-1);
    writeFileSync(
      join(markerDir, "last-result.json"),
      JSON.stringify({
        role: last?.role,
        isError: last?.role === "toolResult" ? last.isError : null,
        toolName: last?.role === "toolResult" ? last.toolName : null,
      }),
    );
    return fauxAssistantMessage([fauxText(text)]);
  };
}

test("unsafe 工具在途被 kill -9：绝不自动重跑，模型收到 interrupted", async () => {
  const { dir, dbPath, markerDir } = setupDir();
  const child = spawnChild([dbPath, "unsafe-tool", markerDir], { SLOW_MS: "60000" });
  await waitForFile(join(markerDir, "tool-started"), 15_000);
  kill9(child.pid);
  await child.exited;

  // 父进程重开：模型应收到 interrupted 的 toolResult
  const { faux, models } = makeFaux();
  faux.setResponses([recordingFactory(markerDir, "recovered")]);
  const runner = await Runner.open(BACKGROUND_CONTEXT, {
    dbPath,
    models,
    model: MODEL,
    tools: [slowUnsafeTool({ markerDir })],
  });
  runner.resume();
  await runner.waitForIdle(BACKGROUND_CONTEXT);
  const cp = await runner.checkpoint(BACKGROUND_CONTEXT);
  assert.equal(cp.live?.runFailure, undefined);
  await runner.close(BACKGROUND_CONTEXT);

  assert.equal(execCount(markerDir), 1, "unsafe 工具不得重跑");
  const last = JSON.parse(readFileSync(join(markerDir, "last-result.json"), "utf8"));
  assert.equal(last.role, "toolResult");
  assert.equal(last.isError, true, "模型应收到 interrupted 错误结果");
  assert.equal(last.toolName, "test.slow_unsafe");
  rmSync(dir, { recursive: true, force: true });
});

test("safe 工具在途被 kill -9：重开后自动重跑并完成", async () => {
  const { dir, dbPath, markerDir } = setupDir();
  const child = spawnChild([dbPath, "safe-tool", markerDir], { SLOW_MS: "60000" });
  await waitForFile(join(markerDir, "tool-started"), 15_000);
  kill9(child.pid);
  await child.exited;

  // 父进程重开：把慢时长调短，让重跑快速完成
  setSlowMs(50);
  const { faux, models } = makeFaux();
  faux.setResponses([fauxAssistantMessage([fauxText("done-after-rerun")])]);
  const runner = await Runner.open(BACKGROUND_CONTEXT, {
    dbPath,
    models,
    model: MODEL,
    tools: [slowSafeTool({ markerDir })],
  });
  runner.resume();
  await runner.waitForIdle(BACKGROUND_CONTEXT);
  const cp = await runner.checkpoint(BACKGROUND_CONTEXT);
  assert.equal(cp.live?.runFailure, undefined);
  await runner.close(BACKGROUND_CONTEXT);

  assert.equal(execCount(markerDir), 2, "safe 工具应自动重跑一次");
  rmSync(dir, { recursive: true, force: true });
});

test("模型请求在途被 kill -9：重开后请求重发并完成", async () => {
  const { dir, dbPath, markerDir } = setupDir();
  const child = spawnChild([dbPath, "model-midflight", markerDir]);
  await new Promise((r) => setTimeout(r, 1200)); // 子进程慢速流式中
  kill9(child.pid);
  await child.exited;

  const { faux, models } = makeFaux();
  faux.setResponses([fauxAssistantMessage([fauxText("recovered-model")])]);
  const runner = await Runner.open(BACKGROUND_CONTEXT, { dbPath, models, model: MODEL });
  runner.resume();
  await runner.waitForIdle(BACKGROUND_CONTEXT);
  const cp = await runner.checkpoint(BACKGROUND_CONTEXT);
  assert.equal(cp.live?.runFailure, undefined, "模型请求应重发并完成");
  assert.equal(faux.state.callCount, 1, "父进程内模型请求只发一次");
  await runner.close(BACKGROUND_CONTEXT);
  rmSync(dir, { recursive: true, force: true });
});

test("连续崩溃触发 restart_limit 熔断（restartLimit=2）：工具不再重跑", async () => {
  const { dir, dbPath, markerDir } = setupDir();

  // 第 1 轮：启动 → 工具执行中 → kill
  const c1 = spawnChild([dbPath, "parked-start", markerDir], { SLOW_MS: "60000" });
  await waitForFile(join(markerDir, "tool-started"), 15_000);
  kill9(c1.pid);
  await c1.exited;
  rmSync(join(markerDir, "tool-started"));

  // 第 2 轮：重开续跑 → safe 工具自动重跑 → 再次 kill
  const c2 = spawnChild([dbPath, "parked-resume", markerDir], { SLOW_MS: "60000" });
  await waitForFile(join(markerDir, "tool-started"), 15_000);
  kill9(c2.pid);
  await c2.exited;

  // 第 3 轮（父进程）：不应再重跑，工具以 restart_limit 错误结果结束，模型收尾
  setSlowMs(50);
  const { faux, models } = makeFaux();
  faux.setResponses([recordingFactory(markerDir, "after-limit")]);
  const runner = await Runner.open(BACKGROUND_CONTEXT, {
    dbPath,
    models,
    model: MODEL,
    tools: [slowSafeTool({ markerDir })],
    settings: { restartLimit: 2 },
  });
  runner.resume();
  await runner.waitForIdle(BACKGROUND_CONTEXT);
  await runner.close(BACKGROUND_CONTEXT);

  assert.equal(execCount(markerDir), 2, "熔断后不得第三次执行工具");
  const last = JSON.parse(readFileSync(join(markerDir, "last-result.json"), "utf8"));
  assert.equal(last.role, "toolResult");
  assert.equal(last.isError, true, "熔断以错误结果形式交给模型");
  assert.equal(last.toolName, "test.slow_safe");
  rmSync(dir, { recursive: true, force: true });
});
