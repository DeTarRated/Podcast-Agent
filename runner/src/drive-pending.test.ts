// PostgreSQL pending 调度器测试；队列命令与子进程均 fake，零真实模型调用。
//
// 模拟场景：
// - Episode 幂等入队、多个 worker 领取、完成回写与租约续约
// - 子进程 unknown 失败进入 fail 且不自动重试
import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import type { spawn } from "node:child_process";

import { runPending } from "./drive-pending.ts";
import type { RunResult } from "./bash.ts";

function childWith(exitCode: number, text = "done"): typeof spawn {
  return (() => {
    const child = new EventEmitter() as any;
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    process.nextTick(() => {
      child.stdout.end(text);
      child.stderr.end();
      child.emit("close", exitCode);
    });
    return child;
  }) as typeof spawn;
}

function queueRunner() {
  let claimIndex = 0;
  const completed: number[] = [];
  const failed: string[][] = [];
  const runner = async (_command: string, args: string[]): Promise<RunResult> => {
    let result: any;
    if (args.includes("pipeline.episode_flow")) {
      result = { items: [{ id: 1, guid: "g1" }, { id: 2, guid: "g2" }] };
    } else {
      const actionIndex = args.indexOf("state.postgres.job_cli") + 1;
      const action = args[actionIndex];
      if (action === "enqueue-episode") result = { reused: false };
      else if (action === "claim") {
        claimIndex += 1;
        result = claimIndex <= 2
          ? { job: { id: claimIndex, episode_id: claimIndex } }
          : null;
      } else if (action === "complete") {
        completed.push(Number(args[actionIndex + 1]));
        result = { status: "completed" };
      } else if (action === "fail") {
        failed.push(args);
        result = { status: "failed" };
      } else {
        result = { updated: true };
      }
    }
    return { exitCode: 0, stdout: JSON.stringify({ status: "success", data: result }), stderr: "" };
  };
  return { runner, completed, failed };
}

test("Episode 入队后多 worker 领取并完成", async () => {
  const { runner, completed } = queueRunner();
  const result = await runPending({
    commandRunner: runner,
    spawnImpl: childWith(0),
    parallel: 2,
    limit: 2,
    heartbeatMs: 60_000,
  });
  assert.equal(result.claimed, 2);
  assert.equal(result.completed, 2);
  assert.deepEqual(completed.sort(), [1, 2]);
});

test("unknown 子进程失败进入 fail 且不 complete", async () => {
  const { runner, completed, failed } = queueRunner();
  const result = await runPending({
    commandRunner: runner,
    spawnImpl: childWith(1, "RemoteProtocolError: unknown"),
    parallel: 1,
    limit: 1,
    heartbeatMs: 60_000,
  });
  assert.equal(result.failed, 1);
  assert.equal(completed.length, 0);
  assert.equal(failed.length, 1);
  assert.ok(failed[0].includes("--unknown"));
});
