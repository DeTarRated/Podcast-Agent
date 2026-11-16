// 执行层·dispatch 适配器契约测试，fake API/账本，零数据库和供应商调用。
// 场景：数字 taskId、新建/复用超时、晚完成不再次等待、unknown 核销 usage、终态保留、非法配置零副作用。
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import { dispatchSubagent, type DispatchLedger } from "./durable-adapter.ts";
import type { SubagentInput } from "./experiment-subagent.ts";

type Api = Parameters<typeof dispatchSubagent>[1];
const input: SubagentInput = {
  role: "translate", inputPath: "input.srt", outputPath: "output.txt", callRef: "test-call",
  model: { provider: "fake", modelId: "fake" },
  ledger: { episodeId: 1, purpose: "translate", provider: "fake", model: "fake" },
};
const completed = { state: { status: "terminal", outcome: { status: "completed", result: {
  role: "translate", outputPath: "output.txt",
  usage: { input: 20, output: 10, cacheRead: 2, totalTokens: 30, costTotal: 0.01 },
} } } };

function fixture(options: { reused?: boolean; status?: string; wait?: () => Promise<unknown>; createFails?: boolean; requestStarted?: boolean } = {}) {
  const calls: string[] = [];
  let status = options.status ?? "missing";
  let usage: string | undefined;
  const ledger: DispatchLedger = {
    async writeIntent() { calls.push("intent"); assert.equal(status, "missing"); status = "intent"; },
    async writeOutcome(_meta, _ref, next, basis) {
      calls.push("outcome:" + next); assert.equal(status, "intent"); status = next; usage = basis;
    },
    async getStatus() { return status; },
    async getRequestStarted() { return options.requestStarted ?? true; },
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
      // Fake child starts a metered model call before its wait becomes observable.
      if (status === "missing") status = "intent";
      const result = options.wait ? await options.wait() : completed;
      if ((result as any)?.state?.outcome?.status === "completed" && status === "intent") {
        status = "success"; usage = "input_tokens=20;output_tokens=10;cache_read=2;cost_usd=0.01";
      }
      return result;
    },
  } as unknown as Api;
  return { api, ledger, calls, status: () => status, usage: () => usage };
}

const estimateSubagent = () => ({ amount: 0.01, usageBasis: "estimate=max_context_tokens" });
async function dispatch(f: ReturnType<typeof fixture>) {
  return dispatchSubagent(input, f.api, BACKGROUND_CONTEXT, f.ledger, estimateSubagent);
}

async function withTimeout(raw: string, fn: () => Promise<void>) {
  const old = process.env.PODCAST_SUBAGENT_WAIT_TIMEOUT_MS;
  process.env.PODCAST_SUBAGENT_WAIT_TIMEOUT_MS = raw;
  try { await fn(); } finally {
    if (old === undefined) delete process.env.PODCAST_SUBAGENT_WAIT_TIMEOUT_MS;
    else process.env.PODCAST_SUBAGENT_WAIT_TIMEOUT_MS = old;
  }
}

test("子代理预算intent先于task创建；worker请求自行claim并核销", async () => {
  const f = fixture();
  const result = await dispatch(f);
  assert.equal(result.taskId, "42"); assert.equal(result.reused, false);
  assert.deepEqual(f.calls, ["scan", "intent", "create", "wait"]);
  assert.match(f.usage()!, /input_tokens=20;output_tokens=10;cache_read=2;cost_usd=0.01/);
});

test("未知任务复用原 id 核销成功，不创建新 intent 或 task", async () => {
  const f = fixture({ reused: true, status: "unknown" });
  assert.equal((await dispatch(f)).reused, true);
  assert.deepEqual(f.calls, ["scan", "wait", "reconcile"]);
  assert.equal(f.status(), "success"); assert.match(f.usage()!, /cost_usd=0.01/);
});

test("新建和复用 intent 的永不返回等待到期后记 unknown，不重复 wait", async () => {
  await withTimeout("20", async () => {
    for (const reused of [false, true]) {
      const f = fixture({ reused, status: reused ? "intent" : "missing", wait: () => new Promise(() => {}) });
      await assert.rejects(dispatch(f), /超时/);
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
    await assert.rejects(dispatch(f), /超时/);
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
      await assert.rejects(dispatch(f));
      assert.equal(f.status(), status);
      assert.deepEqual(f.calls, ["scan", "wait"]);
    }
  });
});

test("失败终态仅更新 intent，复用时也不创建新任务", async () => {
  for (const reused of [false, true]) {
    const f = fixture({ reused, status: reused ? "intent" : "missing", wait: async () => ({ state: { status: "terminal", outcome: { status: "failed" } } }) });
    await assert.rejects(dispatch(f), /failed/);
    assert.equal(f.status(), "failed");
    assert.equal(f.calls.filter(x => x === "wait").length, 1);
  }
});

test("非法配置在任何 scan/intent/create 之前拒绝", async () => {
  await withTimeout("Infinity", async () => {
    const f = fixture();
    await assert.rejects(dispatch(f), /正整数/);
    assert.deepEqual(f.calls, []); assert.equal(f.status(), "missing");
  });
});

test("扫描与 createTask 之间出现暂停：拒绝、不写账本不建任务", async () => {
  const dir = mkdtempSync(join(tmpdir(), "dispatch-pause-test-"));
  const pausePath = join(dir, "business.pause");
  process.env.PODCAST_BUSINESS_PAUSE_FILE = pausePath;
  try {
    const f = fixture();
    const pausingApi = {
      ...f.api,
      async commit(fn: (tx: unknown) => unknown) {
        // 扫描完成、intent/createTask 之前出现暂停标记。
        writeFileSync(pausePath, "paused before createTask\n");
        return (f.api as any).commit(fn);
      },
    } as unknown as Api;
    await assert.rejects(
      dispatchSubagent(input, pausingApi, BACKGROUND_CONTEXT, f.ledger, estimateSubagent),
      /业务暂停标记存在/,
    );
    assert.equal(f.calls.includes("intent"), false);
    assert.equal(f.calls.includes("create"), false);
  } finally {
    delete process.env.PODCAST_BUSINESS_PAUSE_FILE;
    rmSync(dir, { recursive: true, force: true });
  }
});

test("intent 落库与 createTask 之间出现暂停：intent释放为failed且不建任务", async () => {
  const dir = mkdtempSync(join(tmpdir(), "dispatch-pause2-test-"));
  const pausePath = join(dir, "business.pause");
  process.env.PODCAST_BUSINESS_PAUSE_FILE = pausePath;
  try {
    const f = fixture();
    const pausingLedger: DispatchLedger = {
      async writeIntent(...params: Parameters<DispatchLedger["writeIntent"]>) {
        writeFileSync(pausePath, "paused after intent\n");
        await f.ledger.writeIntent(...params);
      },
      writeOutcome: f.ledger.writeOutcome,
      getStatus: f.ledger.getStatus,
      getRequestStarted: f.ledger.getRequestStarted,
      reconcile: f.ledger.reconcile,
    };
    await assert.rejects(
      dispatchSubagent(input, f.api, BACKGROUND_CONTEXT, pausingLedger, estimateSubagent),
      /业务暂停标记存在/,
    );
    assert.equal(f.calls.includes("create"), false);
    assert.equal(f.calls.includes("outcome:failed"), true);
  } finally {
    delete process.env.PODCAST_BUSINESS_PAUSE_FILE;
    rmSync(dir, { recursive: true, force: true });
  }
});

test("create 失败未触发模型调用，不写费用账本且不进入 wait", async () => {
  const f = fixture({ createFails: true });
  await assert.rejects(dispatch(f), /create failed/);
  assert.deepEqual(f.calls, ["scan", "intent", "create", "outcome:failed"]);
  assert.equal(f.status(), "failed");
});

test("既有失败子任务但请求从未发出：允许重建，不当作重复付费", async () => {
  const f = fixture({ reused: true, requestStarted: false });
  const result = await dispatchSubagent(input, f.api, BACKGROUND_CONTEXT, f.ledger, estimateSubagent);
  assert.ok(result);
  // 账本 record_started=false → 重建任务（create 被调用），而不是复用失败任务。
  assert.equal(f.calls.includes("create"), true);
});

test("既有失败子任务且请求已发出：保持终态，不重建不重发", async () => {
  const f = fixture({ reused: true, requestStarted: true });
  await dispatchSubagent(input, f.api, BACKGROUND_CONTEXT, f.ledger, estimateSubagent);
  assert.equal(f.calls.includes("create"), false);
});
