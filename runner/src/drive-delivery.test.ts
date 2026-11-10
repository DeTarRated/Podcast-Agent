// 审批后交付调度测试；命令和 highlights 子进程均 fake，零真实模型/R2。
//
// 模拟场景：
// - waiting_highlights 派发、租约获取与释放、补齐后再次发布
// - highlights 子进程失败显式标记，不阻塞调度返回，不自动重试
import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import type { spawn } from "node:child_process";

import { runDelivery } from "./drive-delivery.ts";
import type { RunResult } from "./bash.ts";

function fakeChild(exitCode: number): typeof spawn {
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

function fakeRunner() {
  let publishCalls = 0;
  const released: number[] = [];
  const marked: unknown[] = [];
  const runner = async (_command: string, args: string[], options: { input?: string }): Promise<RunResult> => {
    const moduleIndex = args.findIndex((item) => item.endsWith(".cli") || item === "pipeline.episode_flow");
    const action = args[moduleIndex + 1];
    if (action === "publish-approved") {
      publishCalls += 1;
      const value = publishCalls === 1
        ? { published: 0, items: [{ episode_id: 1, approval_id: 9, status: "waiting_highlights", dispatchFile: "/tmp/dispatch.json" }] }
        : { published: 1, items: [] };
      return { exitCode: 0, stdout: JSON.stringify(value), stderr: "" };
    }
    if (action === "acquire") return { exitCode: 0, stdout: '{"acquired":true}', stderr: "" };
    if (action === "release") {
      released.push(Number(args[moduleIndex + 2]));
      return { exitCode: 0, stdout: '{"released":true}', stderr: "" };
    }
    if (action === "mark-highlights-failed") {
      marked.push(JSON.parse(options.input ?? "{}"));
      return { exitCode: 0, stdout: '{"status":"success"}', stderr: "" };
    }
    throw new Error(`unexpected action ${action}`);
  };
  return { runner, released, marked };
}

test("看点补齐后再次发布并释放租约", async () => {
  const { runner, released } = fakeRunner();
  const result = await runDelivery({ commandRunner: runner, spawnImpl: fakeChild(0) });
  assert.equal(result.published, 1);
  assert.equal(result.highlights_completed, 1);
  assert.deepEqual(released, [1]);
});

test("看点子进程失败被显式标记且释放租约", async () => {
  const { runner, released, marked } = fakeRunner();
  const result = await runDelivery({ commandRunner: runner, spawnImpl: fakeChild(1) });
  assert.equal(result.failed, 1);
  assert.equal(marked.length, 1);
  assert.deepEqual(released, [1]);
});
