// PostgreSQL pending 调度器测试；队列命令与子进程均 fake，零真实模型调用。
//
// 模拟场景：
// - Episode 幂等入队、多个 worker 领取、完成回写与租约续约
// - 子进程 unknown 失败进入 fail 且不自动重试
// - 队列命令始终经 uv 启动，不把 run 误当可执行文件
// - 共享运行锁竞争失败直接返回 locked；暂停标记在入队与领取前阻止
// - 心跳 updated=false 视为失租：在途任务结束但停止新增领取
import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { writeFileSync, readFileSync, existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough } from "node:stream";
import type { spawn } from "node:child_process";

import { runPending } from "./drive-pending.ts";
import type { RunResult } from "./bash.ts";

function childWith(exitCode: number, text = "done"): typeof spawn {
  return (() => {
    const child = new EventEmitter() as any;
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.kill = () => true;
    process.nextTick(() => {
      child.stdout.end(text);
      child.stderr.end();
      child.emit("close", exitCode);
    });
    return child;
  }) as typeof spawn;
}

/** 锁进程 fake：成功路径输出握手信号后保持存活，失败路径以非零码退出且无输出。 */
function flockChild(acquire: boolean): ReturnType<typeof childWith> {
  if (!acquire) return childWith(1, "");
  return (() => {
    const child = new EventEmitter() as any;
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.killed = false;
    child.kill = () => {
      child.killed = true;
      return true;
    };
    process.nextTick(() => child.stdout.write("podcast-agent-run-lock\n"));
    return child;
  }) as ReturnType<typeof childWith>;
}

/** 区分锁进程（flock）与任务子进程：flock 握手成功后保持存活直到 kill。 */
function spawnFor(
  lockAcquired: boolean,
  taskExitCode = 0,
  taskDelayMs = 0,
  taskText = "done",
): typeof spawn {
  return ((command: string) => {
    if (command === "flock") return flockChild(lockAcquired)(command);
    if (taskDelayMs > 0) return childDelayed(taskExitCode, taskDelayMs, taskText)(command);
    return childWith(taskExitCode, taskText)(command);
  }) as typeof spawn;
}

function childDelayed(exitCode: number, delayMs: number, text = "done"): typeof spawn {
  return (() => {
    const child = new EventEmitter() as any;
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.kill = () => true;
    setTimeout(() => {
      child.stdout.end(text);
      child.stderr.end();
      child.emit("close", exitCode);
    }, delayMs);
    return child;
  }) as typeof spawn;
}

function queueRunner(options: { heartbeatUpdated?: boolean; claimJobs?: number } = {}) {
  let claimIndex = 0;
  const completed: number[] = [];
  const failed: string[][] = [];
  const commands: string[] = [];
  const actions: string[] = [];
  const runner = async (_command: string, args: string[]): Promise<RunResult> => {
    commands.push(_command);
    let result: any;
    if (args.includes("pipeline.episode_flow")) {
      result = { items: [{ id: 1, guid: "g1" }, { id: 2, guid: "g2" }] };
    } else {
      const actionIndex = args.indexOf("state.postgres.job_cli") + 1;
      const action = args[actionIndex];
      actions.push(action);
      if (action === "enqueue-episode") result = { reused: false };
      else if (action === "claim") {
        claimIndex += 1;
        result = claimIndex <= (options.claimJobs ?? 2)
          ? { job: { id: claimIndex, episode_id: claimIndex } }
          : null;
      } else if (action === "complete") {
        completed.push(Number(args[actionIndex + 1]));
        result = { status: "completed" };
      } else if (action === "fail") {
        failed.push(args);
        result = { status: "failed" };
      } else if (action === "heartbeat") {
        result = { updated: options.heartbeatUpdated ?? true };
      } else {
        result = { updated: true };
      }
    }
    return { exitCode: 0, stdout: JSON.stringify({ status: "success", data: result }), stderr: "" };
  };
  return { runner, completed, failed, commands, actions };
}

test("Episode 入队后多 worker 领取并完成", async () => {
  const { runner, completed, commands } = queueRunner();
  const result = await runPending({
    commandRunner: runner,
    spawnImpl: spawnFor(true),
    parallel: 2,
    limit: 2,
    heartbeatMs: 60_000,
  });
  assert.equal(result.claimed, 2);
  assert.equal(result.completed, 2);
  assert.deepEqual(completed.sort(), [1, 2]);
  assert.ok(commands.length > 0);
  assert.ok(commands.every((command) => command === "uv"));
});

test("unknown 子进程失败进入 fail 且不 complete", async () => {
  const { runner, completed, failed } = queueRunner();
  const result = await runPending({
    commandRunner: runner,
    spawnImpl: spawnFor(true, 1, 0, "RemoteProtocolError: unknown"),
    parallel: 1,
    limit: 1,
    heartbeatMs: 60_000,
  });
  assert.equal(result.failed, 1);
  assert.equal(completed.length, 0);
  assert.equal(failed.length, 1);
  assert.ok(failed[0].includes("--unknown"));
});

test("共享运行锁竞争失败直接返回 locked，不触碰队列", async () => {
  const { runner, commands } = queueRunner();
  const result = await runPending({
    commandRunner: runner,
    spawnImpl: spawnFor(false),
    parallel: 1,
    limit: 1,
    heartbeatMs: 60_000,
  });
  assert.equal(result.status, "locked");
  assert.equal(result.claimed, 0);
  assert.deepEqual(commands, []);
});

test("暂停标记存在时不入队不领取", async () => {
  const dir = mkdtempSync(join(tmpdir(), "pause-test-"));
  writeFileSync(join(dir, "business.pause"), "paused");
  process.env.PODCAST_BUSINESS_PAUSE_FILE = join(dir, "business.pause");
  try {
    const { runner, commands } = queueRunner();
    await assert.rejects(runPending({
      commandRunner: runner,
      spawnImpl: spawnFor(true),
      parallel: 1,
      limit: 1,
      heartbeatMs: 60_000,
    }), /业务暂停标记存在/);
    assert.deepEqual(commands, []);
  } finally {
    delete process.env.PODCAST_BUSINESS_PAUSE_FILE;
    rmSync(dir, { recursive: true, force: true });
  }
});

test("心跳 updated=false 视为失租：写暂停标记、当前任务结束、停止新增领取", async () => {
  const dir = mkdtempSync(join(tmpdir(), "lease-test-"));
  const pausePath = join(dir, "business.pause");
  process.env.PODCAST_BUSINESS_PAUSE_FILE = pausePath;
  try {
    const { runner, completed, actions } = queueRunner({ heartbeatUpdated: false, claimJobs: 5 });
    const result = await runPending({
      commandRunner: runner,
      spawnImpl: spawnFor(true, 0, 25),
      parallel: 1,
      limit: 5,
      heartbeatMs: 1,
    });
    assert.equal(result.lease_lost, true);
    assert.equal(result.claimed, 1);
    assert.equal(completed.length, 1);
    // 失租后 worker 在途任务结束，不再发起新的 claim。
    assert.equal(actions.filter((action) => action === "claim").length, 1);
    // 失租立即写暂停标记：在途 Episode 的后续付费入口同样被阻止。
    assert.match(readFileSync(pausePath, "utf8"), /lease_lost/);
  } finally {
    delete process.env.PODCAST_BUSINESS_PAUSE_FILE;
    rmSync(dir, { recursive: true, force: true });
  }
});

test("失租写暂停标记失败时显式报告且仍停止派发", async () => {
  // 暂停路径指向不存在目录的深处：writeFileSync 必然失败。
  const dir = mkdtempSync(join(tmpdir(), "lease-write-fail-"));
  const badPath = join(dir, "no-such-dir", "business.pause");
  process.env.PODCAST_BUSINESS_PAUSE_FILE = badPath;
  try {
    const { runner, actions } = queueRunner({ heartbeatUpdated: false, claimJobs: 5 });
    const result = await runPending({
      commandRunner: runner,
      spawnImpl: spawnFor(true, 0, 25),
      parallel: 1,
      limit: 5,
      heartbeatMs: 1,
    }) as Record<string, unknown>;
    assert.equal(result.lease_lost, true);
    assert.match(String(result.pause_write_failed), /write_error=/);
    // 写盘失败必须显式返回非成功状态，上层编排不得按成功继续。
    assert.equal(result.status, "error_pause_write_failed");
    // 进程内停止信号仍然生效：不再领取新任务。
    assert.equal(actions.filter((action) => action === "claim").length, 1);
    assert.equal(existsSync(badPath), false);
  } finally {
    delete process.env.PODCAST_BUSINESS_PAUSE_FILE;
    rmSync(dir, { recursive: true, force: true });
  }
});

test("claim 返回后 spawn 前出现暂停标记则不启动子进程", async () => {
  const dir = mkdtempSync(join(tmpdir(), "pause-claim-test-"));
  const pausePath = join(dir, "business.pause");
  let spawned = 0;
  const spawnCounting = ((command: string) => {
    if (command !== "flock") spawned += 1;
    return spawnFor(true)(command);
  }) as typeof spawn;
  process.env.PODCAST_BUSINESS_PAUSE_FILE = pausePath;
  try {
    const { runner, completed, actions } = queueRunner({ claimJobs: 3 });
    const pausingRunner: typeof runner = async (command, args) => {
      const result = await runner(command, args);
      if (args.includes("claim")) writeFileSync(pausePath, "paused during claim\n");
      return result;
    };
    const result = await runPending({
      commandRunner: pausingRunner,
      spawnImpl: spawnCounting,
      parallel: 1,
      limit: 3,
      heartbeatMs: 60_000,
    });
    assert.equal(result.claimed, 1);
    assert.equal(spawned, 0);
    assert.equal(completed.length, 0);
    assert.equal(actions.includes("complete"), false);
  } finally {
    delete process.env.PODCAST_BUSINESS_PAUSE_FILE;
    rmSync(dir, { recursive: true, force: true });
  }
});
