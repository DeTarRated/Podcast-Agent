// 共享运行锁测试：握手成功才可调度，竞争者被拒，释放后可重新获取；子进程token识别父进程持锁。
import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import type { spawn } from "node:child_process";

import { acquireRunLock, ensureRunLock, runLockChildEnv } from "./run-lock.ts";

function heldFlock() {
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
}

function deniedFlock() {
  const child = new EventEmitter() as any;
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.kill = () => true;
  process.nextTick(() => child.emit("close", 1));
  return child;
}

test("持锁进程存活握手成功后锁可用，竞争者被拒，释放后可重新获取", async () => {
  const first = await acquireRunLock((() => heldFlock()) as typeof spawn);
  assert.notEqual(first, null);
  const denied = await acquireRunLock((() => deniedFlock()) as typeof spawn);
  assert.equal(denied, null);
  first!.release();
  const again = await acquireRunLock((() => heldFlock()) as typeof spawn);
  assert.notEqual(again, null);
  again!.release();
});

test("持锁进程无握手输出即拒绝，不视为持锁", async () => {
  const child = new EventEmitter() as any;
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.kill = () => true;
  // 异常路径：进程直接退出且没有握手输出。
  process.nextTick(() => child.emit("close", 0));
  const lock = await acquireRunLock((() => child) as typeof spawn);
  assert.equal(lock, null);
});

test("父进程token注入时ensureRunLock跳过申请；否则自行竞争", async () => {
  process.env.PODCAST_RUN_LOCK_TOKEN = "parent-holds-lock";
  try {
    const inherited = await ensureRunLock((() => {
      throw new Error("不得再申请锁");
    }) as typeof spawn);
    assert.notEqual(inherited, null);
    inherited!.release();
  } finally {
    delete process.env.PODCAST_RUN_LOCK_TOKEN;
  }
  const competed = await ensureRunLock((() => deniedFlock()) as typeof spawn);
  assert.equal(competed, null);
});

test("子进程环境注入持锁token", () => {
  const env = runLockChildEnv("token-1");
  assert.equal(env.PODCAST_RUN_LOCK_TOKEN, "token-1");
  assert.equal(typeof env.PATH, "string");
});

test("持锁命令包含父进程存活哨兵，父进程消失时子进程自行退出", async () => {
  let captured: string[] = [];
  await acquireRunLock(((command: string, args: string[]) => {
    captured = [command, ...args];
    const child = new EventEmitter() as any;
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.kill = () => true;
    process.nextTick(() => child.stdout.write("podcast-agent-run-lock\n"));
    return child;
  }) as typeof spawn);
  const hold = captured[captured.length - 1];
  assert.ok(hold.includes(`kill -0 ${process.pid}`));
  assert.ok(hold.includes("sleep 2"));
});
