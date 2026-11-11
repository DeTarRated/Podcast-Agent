// 执行层·真实模型调用的 Redis 闸门窄包装；使用 Python provider_gate CLI。
import { resolve } from "node:path";

import { runCommand } from "./bash.ts";

const REPO_ROOT = resolve(import.meta.dirname, "../..");

export async function acquireProviderGate(
  provider: string,
  commandRunner: typeof runCommand = runCommand,
): Promise<string> {
  const result = await commandRunner("uv", [
    "run", "python", "-m", "pipeline.provider_gate", "acquire", "--provider", provider,
  ], { cwd: REPO_ROOT, timeoutMs: 30_000 });
  const payload = JSON.parse(result.stdout.trim().split("\n").at(-1) ?? "{}");
  if (result.exitCode !== 0 || payload.status !== "success" || !payload.data?.allowed) {
    throw new Error(`provider_rate_limited:${provider};retry_after_ms=${payload.data?.retry_after_ms ?? "unknown"}`);
  }
  return String(payload.data.owner);
}

export async function releaseProviderGate(
  provider: string,
  owner: string,
  commandRunner: typeof runCommand = runCommand,
): Promise<void> {
  await commandRunner("uv", [
    "run", "python", "-m", "pipeline.provider_gate", "release", "--provider", provider, "--owner", owner,
  ], { cwd: REPO_ROOT, timeoutMs: 30_000 });
}
