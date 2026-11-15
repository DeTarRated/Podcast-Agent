// 执行层·dispatch 适配器契约测试，fake API/账本，零数据库和供应商调用。
// 场景：数字 taskId、新建/复用超时、晚完成不再次等待、unknown 核销 usage、终态保留、非法配置零副作用。
import { test } from "node:test";
import assert from "node:assert/strict";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import { dispatchSubagent, type DispatchLedger } from "./durable-adapter.ts";
import type { SubagentInput } from "./experiment-subagent.ts";

type Api = Parameters<typeof dispatchSubagent>[1];
const input: SubagentInput = {
  role: "translate", inputPath: "input.srt", outputPath: "output.txt", callRef: "test-call",
  ledger: { episodeId: 1, purpose: "translate", provider: "fake", model: "fake" },
};
const completed = { state: { status: "terminal", outcome: { status: "completed", result: {
  role: "translate", outputPath: "output.txt",
  usage: { input: 20, output: 10, cacheRead: 2, totalTokens: 30, costTotal: 0.01 },
} } } };

function fixture(options: { reused?: boolean; status?: string; wait?: () => Promise<unknown>; createFails?: boolean } = {}) {
  const calls: string[] = [];
  let status = options.status ?? "missing";
  let usage: string | undefined;
  const ledger: DispatchLedger = {
    async writeIntent() { calls.push("intent"); assert.equal(status, "missing"); status = "intent"; },
    async writeOutcome(_meta, _ref, next, basis) {
      calls.push("outcome:" + next); assert.equal(status, "intent"); status = next; usage = basis;
    },
    async getStatus() { return status; },
    async reconcile(_ref, basis) { calls.push("reconcile"); assert.equal(status, "unknown"); status = "success"; usage = basis; },
  };
  // 只有 fake SDK 边界使用类型断言；被测适配器内部不将数字 id 转为字符串。
  const api = {
    conversationId: 1,
    async commit(fn: (tx: unknown) => unknown) {
      calls.push("scan");
      return fn({ scanTasks: () => ({ items: options.reused ? [{ id: 42, input }] : [] }) });
    },
    async createTask() {
      calls.push("create");
      if (options.createFails) throw new Error("create failed");
      return 42;
    },
    async waitForTask(id: unknown) {
      calls.push("wait"); assert.equal(id, 42); assert.equal(typeof id, "number");
      return options.wait ? options.wait() : completed;
    },
  } as unknown as Api;
  return { api, ledger, calls, status: () => status, usage: () => usage };
}

async function withTimeout(raw: string, fn: () => Promise<void>) {
  const old = process.env.PODCAST_SUBAGENT_WAIT_TIMEOUT_MS;
  process.env.PODCAST_SUBAGENT_WAIT_TIMEOUT_MS = raw;
  try { await fn(); } finally {
    if (old === undefined) delete process.env.PODCAST_SUBAGENT_WAIT_TIMEOUT_MS;
    else process.env.PODCAST_SUBAGENT_WAIT_TIMEOUT_MS = old;
  }
}

test("新任务顺序 intent→create→wait→success，数字 id 与 usage 保留", async () => {
  const f = fixture();
  const result = await dispatchSubagent(input, f.api, BACKGROUND_CONTEXT, f.ledger);
  assert.equal(result.taskId, "42"); assert.equal(result.reused, false);
  assert.deepEqual(f.calls, ["scan", "intent", "create", "wait", "outcome:success"]);
  assert.match(f.usage()!, /input_tokens=20;output_tokens=10;cache_read=2;cost_usd=0.01/);
});

test("未知任务复用原 id 核销成功，不创建新 intent 或 task", async () => {
  const f = fixture({ reused: true, status: "unknown" });
  assert.equal((await dispatchSubagent(input, f.api, BACKGROUND_CONTEXT, f.ledger)).reused, true);
  assert.deepEqual(f.calls, ["scan", "wait", "reconcile"]);
  assert.equal(f.status(), "success"); assert.match(f.usage()!, /cost_usd=0.01/);
});

test("新建和复用 intent 的永不返回等待到期后记 unknown，不重复 wait", async () => {
  await withTimeout("20", async () => {
    for (const reused of [false, true]) {
      const f = fixture({ reused, status: reused ? "intent" : "missing", wait: () => new Promise(() => {}) });
      await assert.rejects(dispatchSubagent(input, f.api, BACKGROUND_CONTEXT, f.ledger), /超时/);
      assert.equal(f.status(), "unknown");
      assert.equal(f.calls.filter(x => x === "wait").length, 1);
      assert.equal(f.calls.filter(x => x === "create").length, reused ? 0 : 1);
    }
  });
});

test("等待到期后原请求晚完成，保持 unknown 且不再 wait/create", async () => {
  await withTimeout("20", async () => {
    let finish!: (r: unknown) => void;
    const f = fixture({ wait: () => new Promise(resolve => { finish = resolve; }) });
    await assert.rejects(dispatchSubagent(input, f.api, BACKGROUND_CONTEXT, f.ledger), /超时/);
    const calls = [...f.calls];
    finish(completed);
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal(f.status(), "unknown"); assert.deepEqual(f.calls, calls);
  });
});

test("超时不覆盖已有 unknown/success/failed", async () => {
  await withTimeout("15", async () => {
    for (const status of ["unknown", "success", "failed"]) {
      const f = fixture({ reused: true, status, wait: () => new Promise(() => {}) });
      await assert.rejects(dispatchSubagent(input, f.api, BACKGROUND_CONTEXT, f.ledger));
      assert.equal(f.status(), status);
      assert.deepEqual(f.calls, ["scan", "wait"]);
    }
  });
});

test("失败终态仅更新 intent，复用时也不创建新任务", async () => {
  for (const reused of [false, true]) {
    const f = fixture({ reused, status: reused ? "intent" : "missing", wait: async () => ({ state: { status: "terminal", outcome: { status: "failed" } } }) });
    await assert.rejects(dispatchSubagent(input, f.api, BACKGROUND_CONTEXT, f.ledger), /failed/);
    assert.equal(f.status(), "failed");
    assert.equal(f.calls.filter(x => x === "wait").length, 1);
  }
});

test("非法配置在任何 scan/intent/create 之前拒绝", async () => {
  await withTimeout("Infinity", async () => {
    const f = fixture();
    await assert.rejects(dispatchSubagent(input, f.api, BACKGROUND_CONTEXT, f.ledger), /正整数/);
    assert.deepEqual(f.calls, []); assert.equal(f.status(), "missing");
  });
});

test("create 失败记录 failed，不进入 wait", async () => {
  const f = fixture({ createFails: true });
  await assert.rejects(dispatchSubagent(input, f.api, BACKGROUND_CONTEXT, f.ledger), /create failed/);
  assert.deepEqual(f.calls, ["scan", "intent", "create", "outcome:failed"]);
});
