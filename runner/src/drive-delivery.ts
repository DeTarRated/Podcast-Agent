// 执行层·审批后交付调度：补齐 highlights、再发布；失败逐期隔离且不自动重试模型。
import { randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { runCommand, type RunResult } from "./bash.ts";

const REPO_ROOT = resolve(import.meta.dirname, "../..");
const DRIVE_HIGHLIGHTS = resolve(import.meta.dirname, "drive-highlights.ts");

export interface DeliveryOptions {
  commandRunner?: typeof runCommand;
  spawnImpl?: typeof spawn;
}

async function commandJson(
  runner: typeof runCommand,
  args: string[],
  input?: string,
): Promise<any> {
  const result: RunResult = await runner("uv", args, { cwd: REPO_ROOT, timeoutMs: 120_000, input });
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

export async function runDelivery(options: DeliveryOptions = {}): Promise<Record<string, unknown>> {
  const runner = options.commandRunner ?? runCommand;
  const spawnImpl = options.spawnImpl ?? spawn;
  const first = await publish(runner);
  const results: Array<Record<string, unknown>> = [];
  for (const item of first.items ?? []) {
    if (item.status !== "waiting_highlights" || !item.dispatchFile) continue;
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
  const second = results.length > 0 ? await publish(runner) : { published: 0, items: [] };
  return {
    status: "success",
    published: (first.published ?? 0) + (second.published ?? 0),
    highlights_completed: results.filter((item) => item.status === "completed").length,
    failed: results.filter((item) => item.status === "failed").length,
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
