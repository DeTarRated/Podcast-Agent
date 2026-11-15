// 执行层·终态等待测试，零数据库、零供应商调用。
// 场景：晚完成、持续 pending、永不返回、非法配置与状态、失败终态、超时不重发。
import { test } from "node:test";
import assert from "node:assert/strict";
import { taskWaitTimeout, waitForTerminalTask, TaskWaitTimeout } from "./task-wait.ts";

test("等待 pending/running 后的终态", async () => {
  const states = ["pending", "running", "terminal"];
  let calls = 0;
  const r = await waitForTerminalTask(async () => ({ state: { status: states[calls++] } }), 500, 1);
  assert.equal(r.state.status, "terminal");
  assert.equal(calls, 3);
});

test("持续 pending 使用同一总期限", async () => {
  const start = performance.now();
  await assert.rejects(waitForTerminalTask(async () => ({ state: { status: "pending" } }), 30, 5), TaskWaitTimeout);
  assert.ok(performance.now() - start < 500);
});

test("单次等待永不返回也会到期，且不再调用", async () => {
  let calls = 0;
  await assert.rejects(waitForTerminalTask(() => {
    calls++;
    return new Promise<{ state: { status: string } }>(() => {});
  }, 20), TaskWaitTimeout);
  assert.equal(calls, 1);
});

test("非法配置在 wait 调用前拒绝", async () => {
  for (const raw of ["", "NaN", "Infinity", "-1", "0", "0.5", "2147483648"]) {
    assert.throws(() => taskWaitTimeout(raw));
  }
  let calls = 0;
  await assert.rejects(waitForTerminalTask(async () => { calls++; return { state: { status: "terminal" } }; }, NaN));
  assert.equal(calls, 0);
});

test("缺失或非法状态不能当成成功", async () => {
  await assert.rejects(waitForTerminalTask(async () => ({}), 100), /非法状态/);
  await assert.rejects(waitForTerminalTask(async () => ({ state: { status: "unexpected" } }), 100), /非法状态/);
});

test("失败终态原样返回，不重试", async () => {
  let calls = 0;
  const result = await waitForTerminalTask(async () => {
    calls++;
    return { state: { status: "terminal", outcome: { status: "failed" } } };
  }, 100);
  assert.equal(result.state.outcome.status, "failed");
  assert.equal(calls, 1);
});
