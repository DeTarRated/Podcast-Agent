// 执行层·审批后交付调度：补齐 highlights、再发布；失败逐期隔离且不自动重试模型。
import { randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { runCommand, type RunResult } from "./bash.ts";
import { loadRepoEnv } from "./env.ts";
import { assertBusinessRunning, BusinessPausedError } from "./pause.ts";
import { acquireRunLock, runLockChildEnv, type RunLock } from "./run-lock.ts";

const REPO_ROOT = resolve(import.meta.dirname, "../..");
const DRIVE_HIGHLIGHTS = resolve(import.meta.dirname, "drive-highlights.ts");

export interface DeliveryOptions {
  commandRunner?: typeof runCommand;
  spawnImpl?: typeof spawn;
  /** 派发边界预算门：首次发布前与每条 highlights 前调用；返回 false 即停止新增派发。 */
  beforeDispatch?: () => Promise<boolean>;
}

async function commandJson(
  runner: typeof runCommand,
  args: string[],
  input?: string,
): Promise<any> {
  // 长期任务不强杀：交付命令不设 kill 超时，超时语义=停止新增+告警。
  const result: RunResult = await runner("uv", args, { cwd: REPO_ROOT, input });
  const payload = JSON.parse(result.stdout.trim().split("\n").at(-1) ?? "{}");
  if (result.exitCode !== 0) throw new Error(payload.error?.message ?? payload.error ?? "命令失败");
  return payload.status === "success" && "data" in payload ? payload.data : payload;
}

function flowArgs(command: string, episodeId: number, owner: string): string[] {
  return ["run", "python", "-m", "pipeline.episode_flow", command, String(episodeId), "--owner", owner];
}

async function publish(runner: typeof runCommand): Promise<any> {
  return commandJson(runner, ["run", "python", "-m", "delivery.cli", "publish-approved"]);
}

export async function runDelivery(
  options: DeliveryOptions = {},
  lock: RunLock | null = null,
): Promise<Record<string, unknown>> {
  const runner = options.commandRunner ?? runCommand;
  const spawnImpl = options.spawnImpl ?? spawn;
  // 与所有驱动入口一致：任何锁/暂停判断之前完成统一 .env 加载。
  loadRepoEnv();
  assertBusinessRunning();
  // 未由调用方持锁时（人工直接启动）竞争共享锁；由业务循环持锁调用时透传。
  let held = lock;
  let token = process.env.PODCAST_RUN_LOCK_TOKEN;
  if (held === null && !token) {
    held = await acquireRunLock(spawnImpl);
    if (held === null) {
      return { status: "locked", published: 0, highlights_completed: 0, failed: 0, items: [] };
    }
    token = `delivery-${process.pid}-${randomUUID()}`;
  }
  if (held === null) {
    // 已由环境 token 识别父进程持锁，无需本进程再加锁。
    held = { release: () => {} };
  }
  try {
    return await runDeliveryLocked(
      runner, spawnImpl, held, token ?? "parent-holds-lock", options.beforeDispatch,
    );
  } finally {
    held.release();
  }
}

async function runDeliveryLocked(
  runner: typeof runCommand,
  spawnImpl: typeof spawn,
  _lock: RunLock,
  lockToken: string,
  beforeDispatch?: () => Promise<boolean>,
): Promise<Record<string, unknown>> {
  if (beforeDispatch && !(await beforeDispatch())) {
    return { status: "budget_blocked", published: 0, highlights_completed: 0, failed: 0, items: [] };
  }
  const first = await publish(runner);
  const results: Array<Record<string, unknown>> = [];
  for (const item of first.items ?? []) {
    if (item.status !== "waiting_highlights" || !item.dispatchFile) continue;
    try {
      assertBusinessRunning();
    } catch (error) {
      if (error instanceof BusinessPausedError) break;
      throw error;
    }
    // 每条 highlights 派发前的预算门。
    if (beforeDispatch && !(await beforeDispatch())) break;
    const episodeId = Number(item.episode_id);
    const approvalId = Number(item.approval_id);
    const owner = `delivery-${process.pid}-${randomUUID()}-${episodeId}`;
    const lock = await commandJson(runner, flowArgs("acquire", episodeId, owner));
    if (!lock.acquired) {
      results.push({ episode_id: episodeId, status: "skipped_locked" });
      continue;
    }
    try {
      const childResult = await new Promise<Record<string, unknown>>((resolveResult) => {
        const child = spawnImpl(process.execPath, ["--experimental-strip-types", DRIVE_HIGHLIGHTS, item.dispatchFile], {
          cwd: REPO_ROOT,
          stdio: ["ignore", "pipe", "pipe"],
          env: runLockChildEnv(lockToken),
        });
        let stdout = "";
        let stderr = "";
        child.stdout.on("data", (chunk: Buffer) => { stdout += chunk.toString(); });
        child.stderr.on("data", (chunk: Buffer) => { stderr += chunk.toString(); });
        child.on("error", (error) => resolveResult({ status: "failed", error: String(error) }));
        child.on("close", (exitCode) => resolveResult({
          status: exitCode === 0 ? "completed" : "failed",
          exitCode,
          stdout: stdout.trim().slice(-1000),
          stderr: stderr.trim().slice(-1000),
        }));
      });
      if (childResult.status === "failed") {
        await commandJson(
          runner,
          ["run", "python", "-m", "delivery.cli", "mark-highlights-failed"],
          JSON.stringify({ approval_id: approvalId, error: childResult.error ?? "highlights 子进程失败" }),
        );
      }
      results.push({ episode_id: episodeId, approval_id: approvalId, ...childResult });
    } finally {
      await commandJson(runner, flowArgs("release", episodeId, owner));
    }
  }
  const second = results.length > 0 ? await publish(runner) : { published: 0, items: [], failed: 0 };
  const highlightsFailed = results.filter((item) => item.status === "failed").length;
  const firstFailed = Number(first.failed ?? 0);
  const secondFailed = Number((second as Record<string, unknown>).failed ?? 0);
  return {
    status: "success",
    published: (first.published ?? 0) + (second.published ?? 0),
    highlights_completed: results.filter((item) => item.status === "completed").length,
    // 两轮 publish 与 highlights 的失败项全部计入失败判据，不得丢失。
    failed: firstFailed + secondFailed + highlightsFailed,
    items: results,
  };
}

async function main(): Promise<void> {
  console.log(JSON.stringify(await runDelivery()));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : "unknown");
    process.exitCode = 1;
  });
}
