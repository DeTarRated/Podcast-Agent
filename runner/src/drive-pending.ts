// 执行层·PostgreSQL job 队列调度器：幂等入队、SKIP LOCKED 领取、预算闸门与失败回写。
import { randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { runCommand, type RunResult } from "./bash.ts";
import { loadRepoEnv } from "./env.ts";
import { assertBusinessRunning, BusinessPausedError, businessPausePath } from "./pause.ts";
import { ensureRunLock, runLockChildEnv } from "./run-lock.ts";

const REPO_ROOT = resolve(import.meta.dirname, "../..");
const DRIVE_EPISODE = resolve(import.meta.dirname, "drive-episode.ts");
const JOB_CLI = ["uv", "run", "python", "-m", "state.postgres.job_cli"];

export { acquireRunLock } from "./run-lock.ts";

export interface PendingOptions {
  commandRunner?: typeof runCommand;
  spawnImpl?: typeof spawn;
  parallel?: number;
  limit?: number;
  heartbeatMs?: number;
  /** 派发边界预算门：每次 claim 前调用；返回 false 表示停止新增派发。 */
  beforeClaim?: () => Promise<boolean>;
}

async function commandJson(
  runner: typeof runCommand,
  args: string[],
  timeoutMs = 30_000,
): Promise<any> {
  const result: RunResult = await runner(args[0] === "uv" ? "uv" : args[0], args.slice(1), {
    cwd: REPO_ROOT,
    timeoutMs,
  });
  const payload = JSON.parse(result.stdout.trim().split("\n").at(-1) ?? "{}");
  if (result.exitCode !== 0) throw new Error(payload.error?.message ?? payload.error ?? "队列命令失败");
  return payload.status === "success" && "data" in payload ? payload.data : payload;
}

function jobArgs(command: string, extra: string[] = []): string[] {
  return [...JOB_CLI, command, ...extra];
}

export async function runPending(options: PendingOptions = {}): Promise<Record<string, unknown>> {
  const runner = options.commandRunner ?? runCommand;
  const spawnImpl = options.spawnImpl ?? spawn;
  const parallel = options.parallel ?? Number(process.env.PODCAST_RUNNER_PARALLEL ?? 2);
  const limit = options.limit ?? 10;
  if (!Number.isInteger(parallel) || parallel < 1 || parallel > 4) throw new Error("并行度必须在 1..4");
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw new Error("limit 必须在 1..100");

  // 与所有驱动入口一致：任何锁/暂停判断之前完成统一 .env 加载（格式错误失败关闭）。
  loadRepoEnv();
  assertBusinessRunning();
  // 业务循环等上层已持锁时经 token 跳过申请，避免与自身竞争死锁。
  const lock = await ensureRunLock(spawnImpl);
  if (lock === null) {
    return { status: "locked", claimed: 0, completed: 0, failed: 0, items: [] };
  }
  try {
    return await runPendingLocked(
      runner, spawnImpl, parallel, limit, options.heartbeatMs ?? 60_000, options.beforeClaim,
    );
  } finally {
    lock.release();
  }
}

async function runPendingLocked(
  runner: typeof runCommand,
  spawnImpl: typeof spawn,
  parallel: number,
  limit: number,
  heartbeatMs: number,
  beforeClaim?: () => Promise<boolean>,
): Promise<Record<string, unknown>> {
  assertBusinessRunning();
  const estimateFailures: Array<{ episode_id: number; error: string }> = [];
  // 在途期数的估算冲突属良性跳过，不计入阶段失败。
  const skippedInFlight: Array<{ episode_id: number; reason: string }> = [];
  const pending = await commandJson(runner, [
    "uv", "run", "python", "-m", "pipeline.episode_flow", "pending", "--limit", String(limit),
  ]);
  for (const episode of pending.items ?? []) {
    assertBusinessRunning();
    // 金额限制下入队必须带可审计估算；缺估算的 Episode 直接跳过，
    // 不创建无估算 job（否则之后会因幂等冲突无法用正确估算重入队）。
    if (episode.estimate_error) {
      estimateFailures.push({ episode_id: episode.id, error: String(episode.estimate_error) });
      continue;
    }
    if (typeof episode.estimated_cost !== "string" || typeof episode.estimate_basis !== "string") {
      estimateFailures.push({ episode_id: episode.id, error: "缺少估算或估算依据" });
      continue;
    }
    try {
      const enqueued = await commandJson(runner, jobArgs("enqueue-episode", [
        String(episode.id), episode.guid,
        "--estimated-cost", episode.estimated_cost,
        "--estimate-basis", episode.estimate_basis,
      ]));
      // 白名单范围外的 Episode 由队列 CLI 显式跳过，不中断整个调度轮次。
      if (enqueued?.skipped === "not_in_allowlist") continue;
    } catch (error) {
      // 单期入队失败不得中断整轮；区分「该期仍在跑」与真实估算错误。
      const message = error instanceof Error ? error.message : String(error);
      if (message.includes("ENQUEUE_IN_FLIGHT")) {
        skippedInFlight.push({ episode_id: episode.id, reason: "running" });
      } else {
        estimateFailures.push({ episode_id: episode.id, error: `enqueue_failed: ${message}` });
      }
      continue;
    }
  }

  const runId = randomUUID();
  const results: Array<Record<string, unknown>> = [];
  let claimedCount = 0;

  let leaseLost = false;

  /** 失租立即全局限流：写暂停标记让所有付费入口（含在途 Episode 的后续请求）停止新增。
   *  写盘失败时抛出并在结果中显式报告；进程内停止信号仍然生效。 */
  function stopDispatchGlobally(reason: string): void {
    leaseLost = true;
    try {
      writeFileSync(businessPausePath(), `${reason}\n`);
    } catch (error) {
      pauseWriteFailed = `${reason}; write_error=${error instanceof Error ? error.message : String(error)}`;
    }
  }
  let pauseWriteFailed: string | undefined;

  async function executeClaimed(claimed: any, workerId: string): Promise<void> {
    const current = claimed.job;
    let heartbeatFailed: string | undefined;
    const heartbeat = setInterval(() => {
      void commandJson(runner, jobArgs("heartbeat", [
        String(current.id), "--worker-id", workerId,
      ])).then((result) => {
        // updated=false 视为失去租约：立即全局停止新增派发，在途任务自然结束。
        if (result && result.updated === false) {
          heartbeatFailed = "lease_lost";
          stopDispatchGlobally(`lease_lost job=${current.id} worker=${workerId}`);
        }
      }).catch((error) => {
        heartbeatFailed = String(error);
        stopDispatchGlobally(`heartbeat_error job=${current.id} worker=${workerId}`);
      });
    }, heartbeatMs);
    let childResult: Record<string, unknown>;
    try {
      // claim 返回与 spawn 之间暂停标记或失租信号可能出现，必须复查后才允许启动真实模型子进程。
      assertBusinessRunning();
      if (leaseLost) {
        throw new BusinessPausedError(`失租停止派发: job=${current.id}`);
      }
      childResult = await new Promise<Record<string, unknown>>((resolveResult) => {
        const child = spawnImpl(process.execPath, ["--experimental-strip-types", DRIVE_EPISODE, String(current.episode_id)], {
          cwd: REPO_ROOT,
          stdio: ["ignore", "pipe", "pipe"],
          // 锁身份 + 租约身份一并注入：子进程在每个付费边界可感知失租并停止新增调用。
          env: { ...runLockChildEnv(runId), PODCAST_JOB_ID: String(current.id), PODCAST_JOB_WORKER_ID: workerId },
        });
        let stdout = "";
        let stderr = "";
        child.stdout.on("data", (chunk: Buffer) => { stdout += chunk.toString(); });
        child.stderr.on("data", (chunk: Buffer) => { stderr += chunk.toString(); });
        child.on("error", (error) => resolveResult({ status: "failed", error: String(error) }));
        child.on("close", (exitCode) => resolveResult({
          status: exitCode === 0 ? "completed" : "failed",
          exitCode,
          stdout: stdout.trim().slice(-2000),
          stderr: stderr.trim().slice(-2000),
        }));
      });
      if (heartbeatFailed) {
        childResult.heartbeat_error = heartbeatFailed;
        leaseLost = true;
      }
      if (pauseWriteFailed) childResult.pause_write_failed = pauseWriteFailed;
      if (childResult.status === "completed") {
        await commandJson(runner, jobArgs("complete", [
          String(current.id), "--worker-id", workerId,
          "--result", JSON.stringify({ child: childResult }),
        ]));
      } else {
        const text = `${childResult.stdout ?? ""} ${childResult.stderr ?? ""}`.toLowerCase();
        const unknown = text.includes("unknown") || text.includes("remoteprotocolerror") || text.includes("状态不明");
        await commandJson(runner, jobArgs("fail", [
          String(current.id), "--worker-id", workerId,
          "--error", String(childResult.error ?? childResult.stderr ?? "子进程失败").slice(0, 1000),
          ...(unknown ? ["--unknown"] : []),
          // 默认不自动重放可能已付费的子进程；显式 retryable 需另行验收。
        ]));
      }
      results.push({ episode_id: current.episode_id, job_id: current.id, ...childResult });
    } finally {
      clearInterval(heartbeat);
    }
  }

  async function worker(index: number): Promise<void> {
    const workerId = `worker-${process.pid}-${runId}-${index}`;
    while (claimedCount < limit && !leaseLost) {
      try {
        assertBusinessRunning();
      } catch (error) {
        if (error instanceof BusinessPausedError) return;
        throw error;
      }
      // 派发边界预算门：领任务前检查本轮额度，超限不再新增工作。
      if (beforeClaim && !(await beforeClaim())) break;
      const claimed = await commandJson(runner, jobArgs("claim", ["--worker-id", workerId]));
      if (!claimed) break;
      claimedCount += 1;
      try {
        await executeClaimed(claimed, workerId);
      } catch (error) {
        // claim 与 spawn 之间出现暂停：不启动子进程，任务留给租约过期与人工恢复。
        if (error instanceof BusinessPausedError) return;
        throw error;
      }
    }
  }

  await Promise.all(Array.from({ length: Math.min(parallel, limit) }, (_, index) => worker(index)));
  const summary = {
    discovered: pending.items?.length ?? 0,
    claimed: claimedCount,
    completed: results.filter((item) => item.status === "completed").length,
    failed: results.filter((item) => item.status === "failed").length,
    lease_lost: leaseLost,
    pause_write_failed: pauseWriteFailed,
    estimate_failures: estimateFailures,
    skipped_in_flight: skippedInFlight,
    items: results,
    job_ids: results.map((item) => item.job_id),
  };
  // 失租全局限流落盘失败必须显式失败，上层编排不得按成功继续。
  if (pauseWriteFailed) return { status: "error_pause_write_failed", ...summary };
  return { status: "success", ...summary };
}

async function main(): Promise<void> {
  const result = await runPending();
  console.log(JSON.stringify(result));
  // 失租全局限流落盘失败必须非零退出，shell/cron 编排不得按成功继续。
  if (result.status === "error_pause_write_failed") process.exitCode = 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : "unknown");
    process.exitCode = 1;
  });
}
