// discovered 调度器测试；命令与子进程均 fake，零真实 Episode 执行。
//
// 模拟场景：
// - 有界调度、锁占用跳过、完成释放
// - 单期子进程失败不阻塞其他期，所有已获取租约均释放
import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import type { spawn } from "node:child_process";

import { runPending } from "./drive-pending.ts";
import type { RunResult } from "./bash.ts";

function childWith(exitCode: number): typeof spawn {
  return (() => {
    const child = new EventEmitter() as any;
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    process.nextTick(() => {
      child.stdout.end("done");
      child.stderr.end();
      child.emit("close", exitCode);
    });
    return child;
  }) as typeof spawn;
}

function runnerFor(locks: Set<number>, episodes: number[]) {
  const releases: number[] = [];
  const runner = async (_command: string, args: string[]): Promise<RunResult> => {
    const actionIndex = args.indexOf("pipeline.episode_flow") + 1;
    const action = args[actionIndex];
    const episodeId = Number(args[actionIndex + 1]);
    if (action === "pending") {
      return { exitCode: 0, stdout: JSON.stringify({ items: episodes.map((id) => ({ id })) }), stderr: "" };
    }
    if (action === "acquire") {
      const acquired = !locks.has(episodeId);
      return { exitCode: 0, stdout: JSON.stringify({ acquired }), stderr: "" };
    }
    if (action === "release") {
      releases.push(episodeId);
      return { exitCode: 0, stdout: JSON.stringify({ released: true }), stderr: "" };
    }
    return { exitCode: 0, stdout: JSON.stringify({ updated: true }), stderr: "" };
  };
  return { runner, releases };
}

test("锁占用的期数跳过，完成期释放", async () => {
  const { runner, releases } = runnerFor(new Set([2]), [1, 2]);
  const result = await runPending({
    commandRunner: runner,
    spawnImpl: childWith(0),
    parallel: 1,
    heartbeatMs: 60_000,
  });
  assert.equal(result.completed, 1);
  assert.equal(result.skipped, 1);
  assert.deepEqual(releases, [1]);
});

test("单期失败不阻塞其他期且均释放", async () => {
  const { runner, releases } = runnerFor(new Set(), [1, 2]);
  let calls = 0;
  const spawnImpl = (() => {
    calls += 1;
    return childWith(calls === 1 ? 1 : 0)(process.execPath, [], {} as any);
  }) as unknown as typeof spawn;
  const result = await runPending({
    commandRunner: runner,
    spawnImpl,
    parallel: 1,
    heartbeatMs: 60_000,
  });
  assert.equal(result.completed, 1);
  assert.equal(result.failed, 1);
  assert.deepEqual(releases.sort(), [1, 2]);
});
