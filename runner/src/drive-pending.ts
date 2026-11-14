// 执行层·PostgreSQL job 队列调度器：幂等入队、SKIP LOCKED 领取、预算闸门与失败回写。
import { randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { runCommand, type RunResult } from "./bash.ts";

const REPO_ROOT = resolve(import.meta.dirname, "../..");
const DRIVE_EPISODE = resolve(import.meta.dirname, "drive-episode.ts");
const JOB_CLI = ["uv", "run", "python", "-m", "state.postgres.job_cli"];

export interface PendingOptions {
  commandRunner?: typeof runCommand;
  spawnImpl?: typeof spawn;
  parallel?: number;
  limit?: number;
  heartbeatMs?: number;
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

  const pending = await commandJson(runner, [
    "uv", "run", "python", "-m", "pipeline.episode_flow", "pending", "--limit", String(limit),
  ]);
  for (const episode of pending.items ?? []) {
    await commandJson(runner, jobArgs("enqueue-episode", [String(episode.id), episode.guid]));
  }

  const runId = randomUUID();
  const results: Array<Record<string, unknown>> = [];
  let claimedCount = 0;

  async function executeClaimed(claimed: any, workerId: string): Promise<void> {
    const current = claimed.job;
    let heartbeatFailed: string | undefined;
    const heartbeat = setInterval(() => {
      void commandJson(runner, jobArgs("heartbeat", [
        String(current.id), "--worker-id", workerId,
      ])).catch((error) => { heartbeatFailed = String(error); });
    }, options.heartbeatMs ?? 60_000);
    let childResult: Record<string, unknown>;
    try {
      childResult = await new Promise<Record<string, unknown>>((resolveResult) => {
        const child = spawnImpl(process.execPath, ["--experimental-strip-types", DRIVE_EPISODE, String(current.episode_id)], {
          cwd: REPO_ROOT,
          stdio: ["ignore", "pipe", "pipe"],
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
      if (heartbeatFailed) childResult.heartbeat_error = heartbeatFailed;
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
    while (claimedCount < limit) {
      const claimed = await commandJson(runner, jobArgs("claim", ["--worker-id", workerId]));
      if (!claimed) break;
      claimedCount += 1;
      await executeClaimed(claimed, workerId);
    }
  }

  await Promise.all(Array.from({ length: Math.min(parallel, limit) }, (_, index) => worker(index)));
  return {
    status: "success",
    discovered: pending.items?.length ?? 0,
    claimed: claimedCount,
    completed: results.filter((item) => item.status === "completed").length,
    failed: results.filter((item) => item.status === "failed").length,
    items: results,
  };
}

async function main(): Promise<void> {
  console.log(JSON.stringify(await runPending()));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : "unknown");
    process.exitCode = 1;
  });
}
