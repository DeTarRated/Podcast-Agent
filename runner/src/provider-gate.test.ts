// Runner Redis 闸门包装测试；命令 fake，零真实 Redis／模型调用。
//
// 模拟场景：
// - acquire 成功返回 owner，release 携带同一 provider/owner
// - 限流或闸门失败在真实模型调用前抛错
import { test } from "node:test";
import assert from "node:assert/strict";

import { acquireProviderGate, releaseProviderGate } from "./provider-gate.ts";
import type { RunResult } from "./bash.ts";

test("acquire／release 使用同一 provider 与 owner", async () => {
  const calls: string[][] = [];
  const runner = async (_command: string, args: string[]): Promise<RunResult> => {
    calls.push(args);
    const output = args.includes("acquire")
      ? { status: "success", data: { allowed: true, owner: "owner-1" } }
      : { status: "success", data: { released: true } };
    return { exitCode: 0, stdout: JSON.stringify(output), stderr: "" };
  };
  const owner = await acquireProviderGate("deepseek", runner);
  assert.equal(owner, "owner-1");
  await releaseProviderGate("deepseek", owner, runner);
  assert.ok(calls[1].includes("release"));
  assert.ok(calls[1].includes("owner-1"));
});

test("限流或 Redis 失败在模型调用前抛错", async () => {
  const runner = async (): Promise<RunResult> => ({
    exitCode: 2,
    stdout: JSON.stringify({ status: "success", data: { allowed: false, retry_after_ms: 500 } }),
    stderr: "",
  });
  await assert.rejects(acquireProviderGate("deepseek", runner), /provider_rate_limited/);
});
