// 执行层·discovered 期数的有界调度器：租约互斥、续约、失败隔离、完成释放。
import { randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { runCommand, type RunResult } from "./bash.ts";

const REPO_ROOT = resolve(import.meta.dirname, "../..");
const DRIVE_EPISODE = resolve(import.meta.dirname, "drive-episode.ts");

export interface PendingOptions {
  commandRunner?: typeof runCommand;
  spawnImpl?: typeof spawn;
  parallel?: number;
  limit?: number;
  heartbeatMs?: number;
}

async function commandJson(
  runner: typeof runCommand,
  command: string,
  args: string[],
): Promise<any> {
  const result: RunResult = await runner(command, args, { cwd: REPO_ROOT, timeoutMs: 30_000 });
  const payload = JSON.parse(result.stdout.trim().split("\n").at(-1) ?? "{}");
  if (result.exitCode !== 0) throw new Error(payload.error ?? `命令失败 ${result.exitCode}`);
  return payload;
}

export async function runPending(options: PendingOptions = {}): Promise<Record<string, unknown>> {
  const runner = options.commandRunner ?? runCommand;
  const spawnImpl = options.spawnImpl ?? spawn;
  const parallel = options.parallel ?? Number(process.env.PODCAST_RUNNER_PARALLEL ?? 2);
  if (!Number.isInteger(parallel) || parallel < 1 || parallel > 4) {
    throw new Error("并行度必须在 1..4");
  }
  const pending = await commandJson(runner, "uv", [
    "run", "python", "-m", "pipeline.episode_flow", "pending",
    "--limit", String(options.limit ?? 10),
  ]);
  const items = pending.items as Array<{ id: number }>;
  const runId = randomUUID();
  const results: Array<Record<string, unknown>> = [];
  let next = 0;

  async function executeEpisode(episodeId: number): Promise<void> {
    const owner = `pending-${process.pid}-${runId}-${episodeId}`;
    const acquired = await commandJson(runner, "uv", [
      "run", "python", "-m", "pipeline.episode_flow", "acquire", String(episodeId), "--owner", owner,
    ]);
    if (!acquired.acquired) {
      results.push({ episode_id: episodeId, status: "skipped_locked" });
      return;
    }
    let heartbeatFailed: string | undefined;
    const heartbeat = setInterval(() => {
      void commandJson(runner, "uv", [
        "run", "python", "-m", "pipeline.episode_flow", "heartbeat", String(episodeId), "--owner", owner,
      ]).catch((error) => { heartbeatFailed = String(error); });
    }, options.heartbeatMs ?? 60_000);
    try {
      const result = await new Promise<Record<string, unknown>>((resolveResult) => {
        const child = spawnImpl(process.execPath, ["--experimental-strip-types", DRIVE_EPISODE, String(episodeId)], {
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
      if (heartbeatFailed) result.heartbeat_error = heartbeatFailed;
      results.push({ episode_id: episodeId, ...result });
    } finally {
      clearInterval(heartbeat);
      await commandJson(runner, "uv", [
        "run", "python", "-m", "pipeline.episode_flow", "release", String(episodeId), "--owner", owner,
      ]);
    }
  }

  async function worker(): Promise<void> {
    while (next < items.length) {
      const item = items[next];
      next += 1;
      await executeEpisode(item.id);
    }
  }
  await Promise.all(Array.from({ length: Math.min(parallel, items.length) }, () => worker()));
  return {
    status: "success",
    discovered: items.length,
    completed: results.filter((item) => item.status === "completed").length,
    failed: results.filter((item) => item.status === "failed").length,
    skipped: results.filter((item) => item.status === "skipped_locked").length,
    items: results,
  };
}

async function main(): Promise<void> {
  const result = await runPending();
  console.log(JSON.stringify(result));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : "unknown");
    process.exitCode = 1;
  });
}
