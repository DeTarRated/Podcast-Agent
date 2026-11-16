// 执行层·模型预算包装器测试。
// 模拟场景：逐请求准入与claim、异常不重发、缺失usage保留预留、SDK真实HTTP输出上限、API fail-closed与稳定callRef边界。
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createAssistantMessageEventStream, type AssistantMessage, type Model, type Models } from "@earendil-works/pi-ai";
import { stream as openAiCompletionsStream } from "@earendil-works/pi-ai/api/openai-completions";

import { budgetModels, tagBudgetCall, type BudgetDependencies } from "./model-budget.ts";

const model = {
  id: "mock-main", provider: "deepseek", api: "openai-completions", name: "mock",
  input: ["text"], baseUrl: "https://mock.invalid", reasoning: false,
  contextWindow: 1_000, maxTokens: 100,
  compat: { supportsStore: false, supportsDeveloperRole: false, maxTokensField: "max_tokens" },
  cost: { input: 0.3, output: 0.6, cacheRead: 0.1, cacheWrite: 0.4 },
} as unknown as Model<any>;

function message(stopReason: AssistantMessage["stopReason"] = "stop"): AssistantMessage {
  return {
    role: "assistant", content: [], api: model.api, provider: model.provider, model: model.id,
    usage: { input: 10, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: 15,
      cost: { input: 0.000003, output: 0.000003, cacheRead: 0, cacheWrite: 0, total: 0.000006 } },
    stopReason, timestamp: Date.now(),
  };
}

function fixture(options: { authorizeFails?: boolean; providerFails?: boolean; preauthorized?: boolean; usageMissing?: boolean } = {}) {
  const calls: string[] = [];
  const makeMessage = () => {
    if (!options.usageMissing) return message();
    const result = message();
    result.content = [{ type: "text", text: "finished" }];
    result.usage = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
    return result;
  };
  const authorizedRefs = new Set<string>();
  const reservations: number[] = [];
  const settlements: { amount?: number; basis?: string }[] = [];
  let status = options.preauthorized ? "intent" : "missing";
  const commandRunner: BudgetDependencies["commandRunner"] = async (_command, args) => {
    const cli = args[4];
    calls.push(`ledger:${cli}`);
    let payload: Record<string, unknown>;
    if (cli === "authorize") {
      const ref = args[args.indexOf("--call-ref") + 1];
      if (options.authorizeFails) payload = { status: "error", error: "budget exceeded" };
      else if (authorizedRefs.has(ref)) payload = { status: "error", error: "duplicate callRef" };
      else {
        authorizedRefs.add(ref);
        reservations.push(Number(args[args.indexOf("--amount-est") + 1]));
        status = "intent";
        payload = { status };
      }
    } else if (cli === "get") {
      payload = { status, amount_est: options.preauthorized ? 0.001 : null,
        request_started: options.preauthorized ? 0 : 1 };
    } else if (cli === "claim") {
      assert.equal(status, "intent");
      payload = { status: "claimed" };
    } else if (cli === "outcome") {
      const next = args[args.indexOf("--status") + 1];
      const amountIndex = args.indexOf("--amount-est");
      const basisIndex = args.indexOf("--usage-basis");
      settlements.push({ amount: amountIndex < 0 ? undefined : Number(args[amountIndex + 1]),
        basis: basisIndex < 0 ? undefined : args[basisIndex + 1] });
      status = next;
      payload = { status };
    } else if (cli === "reconcile") {
      status = "success";
      payload = { status };
    } else {
      payload = { status: "error", error: `unexpected cli ${cli}` };
    }
    // 与账本 CLI 一致：统一 envelope {status:"success", data:{...}}；错误为非零退出。
    const failed = payload.status === "error";
    return {
      stdout: `${JSON.stringify(failed ? payload : { status: "success", data: payload })}\n`,
      stderr: "",
      exitCode: failed ? 1 : 0,
    };
  };
  const models = {
    getProviders: () => [{ id: "mock" }],
    completeSimple: async (_model: unknown, _context: unknown, optionsArg: unknown) => {
      calls.push("provider:complete");
      assert.equal((optionsArg as { maxRetries: number }).maxRetries, 0);
      assert.equal((optionsArg as { maxTokens: number }).maxTokens, model.maxTokens);
      if (options.providerFails) throw new Error("simulated network failure");
      return makeMessage();
    },
    streamSimple: (_model: unknown, _context: unknown) => {
      calls.push("provider:stream");
      const stream = createAssistantMessageEventStream();
      const result = makeMessage();
      stream.push({ type: "start", partial: { ...result, stopReason: "pending" } });
      stream.push({ type: "done", reason: "stop", message: result });
      stream.end(result);
      return stream;
    },
  } as unknown as Models;
  const deps: BudgetDependencies = {
    commandRunner,
    acquire: async () => { calls.push("gate:acquire"); return "owner"; },
    release: async () => { calls.push("gate:release"); },
    now: () => new Date("2026-11-15T00:00:00Z"),
  };
  return { calls, models, deps, status: () => status, reservations, settlements };
}

function setPriceEnv() {
  process.env.PODCAST_MODEL_PRICE_MODELS = "deepseek/mock-main";
  process.env.PODCAST_MODEL_PRICE_SOURCE = "https://prices.example.test/model";
  process.env.PODCAST_MODEL_PRICING_DATE = "2026-11-15";
}

test("主Agent先预算提交，再单次调用并按usage核销", async () => {
  setPriceEnv();
  const f = fixture();
  const guarded = budgetModels(f.models, { episodeId: 17 }, f.deps);
  const result = await guarded.completeSimple(model, { messages: [{ role: "user", content: "hello", timestamp: 1 }] }, { maxRetries: 5 });
  assert.equal(result.stopReason, "stop");
  assert.ok(f.calls.indexOf("ledger:authorize") < f.calls.indexOf("provider:complete"));
  assert.ok(f.calls.indexOf("provider:complete") < f.calls.indexOf("ledger:outcome"));
  assert.equal(f.status(), "success");
  assert.equal(f.calls.filter((call) => call === "provider:complete").length, 1);
});

test("SDK默认零usage不作为免费依据；complete和stream均保留整个预留", async () => {
  setPriceEnv();
  for (const operation of ["complete", "stream"] as const) {
    const f = fixture({ usageMissing: true });
    const guarded = budgetModels(f.models, { episodeId: 17 }, f.deps);
    if (operation === "complete") {
      await guarded.completeSimple(model, { messages: [{ role: "user", content: "complete", timestamp: 1 }] }, {});
    } else {
      await guarded.streamSimple(model, { messages: [{ role: "user", content: "stream", timestamp: 1 }] }, {}).result();
    }
    assert.equal(f.status(), "success");
    assert.equal(f.settlements.at(-1)?.amount, f.reservations[0]);
    assert.match(f.settlements.at(-1)?.basis ?? "", /usage_missing=true/);
  }
});

test("超限准入失败不调用模型且释放provider gate", async () => {
  setPriceEnv();
  const f = fixture({ authorizeFails: true });
  const guarded = budgetModels(f.models, { episodeId: 17 }, f.deps);
  await assert.rejects(guarded.completeSimple(model, { messages: [] }, {}), /budget exceeded/);
  assert.equal(f.calls.some((call) => call.startsWith("provider:")), false);
  assert.equal(f.calls.at(-1), "gate:release");
});

test("结果未知保留预留并禁用pi-ai内部重试", async () => {
  setPriceEnv();
  const f = fixture({ providerFails: true });
  const guarded = budgetModels(f.models, { episodeId: 17 }, f.deps);
  await assert.rejects(guarded.completeSimple(model, { messages: [] }, {}), /simulated network failure/);
  assert.equal(f.status(), "unknown");
  assert.equal(f.calls.filter((call) => call === "provider:complete").length, 1);
  await assert.rejects(guarded.completeSimple(model, { messages: [] }, {}), /duplicate callRef/);
  assert.equal(f.calls.filter((call) => call === "provider:complete").length, 1);
});

test("stream请求先预算，终态usage核销后才结束输出流", async () => {
  setPriceEnv();
  const f = fixture();
  const guarded = budgetModels(f.models, { episodeId: 17 }, f.deps);
  const stream = guarded.streamSimple(model, { messages: [] }, {});
  const settled = await stream.result();
  assert.equal(settled.stopReason, "stop");
  assert.equal(f.status(), "success", JSON.stringify(f.calls));
  assert.ok(f.calls.indexOf("ledger:authorize") < f.calls.indexOf("provider:stream"));
  assert.ok(f.calls.indexOf("provider:stream") < f.calls.indexOf("ledger:outcome"));
});

test("实际pi-ai OpenAI兼容传输设置maxTokens且缺失usage保留预留", async () => {
  setPriceEnv();
  const sse = [
    `data: ${JSON.stringify({ id: "chatcmpl-test", object: "chat.completion.chunk", created: 1, model: model.id,
      choices: [{ index: 0, delta: { content: "ok" }, finish_reason: null }] })}`,
    `data: ${JSON.stringify({ id: "chatcmpl-test", object: "chat.completion.chunk", created: 1, model: model.id,
      choices: [{ index: 0, delta: {}, finish_reason: "stop" }] })}`,
    "data: [DONE]", "",
  ].join("\n\n") + "\n\n";
  for (const operation of ["complete", "stream"] as const) {
    const f = fixture();
    let requestCount = 0;
    let requestBody: Record<string, unknown> = {};
    const fetcher: typeof fetch = async (_input, init) => {
      requestCount++;
      requestBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
      return new Response(sse, { status: 200, headers: { "content-type": "text/event-stream" } });
    };
    const rawStream = (requestModel: Model<any>, context: unknown, options: unknown) =>
      openAiCompletionsStream(requestModel as Model<"openai-completions">, context as never, options as never);
    const sdkModels = {
      getProviders: () => [{ id: "deepseek" }],
      stream: rawStream,
      complete: async (requestModel: Model<any>, context: unknown, options: unknown) =>
        rawStream(requestModel, context, options).result(),
    } as unknown as Models;
    const guarded = budgetModels(sdkModels, { episodeId: 17 }, f.deps);
    const requestOptions = { apiKey: "test-key", fetch: fetcher };
    const context = { messages: [{ role: "user" as const, content: operation, timestamp: 1 }] } as never;
    const result = operation === "complete"
      ? await guarded.complete(model, context, requestOptions)
      : await guarded.stream(model, context, requestOptions).result();
    assert.equal(result.stopReason, "stop");
    assert.equal(requestCount, 1);
    assert.equal(requestBody.max_tokens, model.maxTokens);
    assert.equal(f.settlements.at(-1)?.amount, f.reservations[0]);
    assert.match(f.settlements.at(-1)?.basis ?? "", /usage_missing=true/);
  }
});

test("实际pi-ai传输遇到可重试HTTP状态也只发送一次", async () => {
  setPriceEnv();
  const f = fixture();
  let requestCount = 0;
  const fetcher: typeof fetch = async () => {
    requestCount++;
    return new Response(JSON.stringify({ error: { message: "temporary" } }), {
      status: 500, headers: { "content-type": "application/json" },
    });
  };
  const rawStream = (requestModel: Model<any>, context: unknown, options: unknown) =>
    openAiCompletionsStream(requestModel as Model<"openai-completions">, context as never, options as never);
  const sdkModels = { getProviders: () => [{ id: "deepseek" }], stream: rawStream } as unknown as Models;
  const guarded = budgetModels(sdkModels, { episodeId: 17 }, f.deps);
  const result = await guarded.stream(model, { messages: [{ role: "user", content: "retry?", timestamp: 1 }] } as never,
    { apiKey: "test-key", fetch: fetcher, maxRetries: 8 }).result();
  assert.equal(result.stopReason, "error");
  assert.equal(requestCount, 1);
  assert.equal(f.status(), "unknown");
});

test("超出模型maxTokens时在限流器和供应商调用前拒绝", async () => {
  setPriceEnv();
  const f = fixture();
  const guarded = budgetModels(f.models, { episodeId: 17 }, f.deps);
  await assert.rejects(guarded.completeSimple(model, { messages: [] }, { maxTokens: 101 }), /超过已预算上限/);
  assert.deepEqual(f.calls, []);
});

test("缺少价格校准白名单时stream不触发gate或provider", async () => {
  setPriceEnv();
  delete process.env.PODCAST_MODEL_PRICE_MODELS;
  const f = fixture();
  const guarded = budgetModels(f.models, { episodeId: 17 }, f.deps);
  const settled = await guarded.streamSimple(model, { messages: [] }, {}).result();
  assert.equal(settled.stopReason, "error");
  assert.deepEqual(f.calls, []);
});

test("恢复重建message timestamp不改变相同请求的callRef", async () => {
  setPriceEnv();
  const f = fixture();
  const guarded = budgetModels(f.models, { episodeId: 17 }, f.deps);
  await guarded.completeSimple(model, { messages: [{ role: "user", content: "same summary", timestamp: 10 }] }, {});
  await assert.rejects(
    guarded.completeSimple(model, { messages: [{ role: "user", content: "same summary", timestamp: 99 }] }, {}),
    /duplicate callRef/,
  );
  assert.equal(f.calls.filter((call) => call === "provider:complete").length, 1);
});

test("工具参数内的timestamp仍参与callRef，避免不同请求误判重复", async () => {
  setPriceEnv();
  const f = fixture();
  const guarded = budgetModels(f.models, { episodeId: 17 }, f.deps);
  const context = (timestamp: number) => ({ messages: [{ role: "assistant", timestamp: 1,
    content: [{ type: "toolCall", id: "tool-call", name: "run", arguments: { timestamp } }] }] } as never);
  await guarded.completeSimple(model, context(10), {});
  await guarded.completeSimple(model, context(20), {});
  assert.equal(f.calls.filter((call) => call === "provider:complete").length, 2);
  assert.equal(f.reservations.length, 2);
});

test("未验收的模型API组合在限流器和供应商调用前拒绝", async () => {
  setPriceEnv();
  const f = fixture();
  const guarded = budgetModels(f.models, { episodeId: 17 }, f.deps);
  const unsupported = { ...model, api: "openai-codex-responses" } as Model<any>;
  const result = await guarded.streamSimple(unsupported, { messages: [] }, {}).result();
  assert.equal(result.stopReason, "error");
  assert.equal(result.errorMessage?.includes("API未完成"), true);
  assert.deepEqual(f.calls, []);
});

test("暂停标记存在时complete与stream在账本写入前失败，零gate外副作用", async () => {
  setPriceEnv();
  const dir = mkdtempSync(join(tmpdir(), "budget-pause-test-"));
  process.env.PODCAST_BUSINESS_PAUSE_FILE = join(dir, "business.pause");
  try {
    writeFileSync(process.env.PODCAST_BUSINESS_PAUSE_FILE, "paused\n");
    for (const operation of ["complete", "stream"] as const) {
      const f = fixture();
      const guarded = budgetModels(f.models, { episodeId: 17 }, f.deps);
      const context = { messages: [{ role: "user", content: operation, timestamp: 1 }] } as never;
      if (operation === "complete") {
        await assert.rejects(guarded.completeSimple(model, context, {}), /业务暂停标记存在/);
      } else {
        const settled = await guarded.streamSimple(model, context, {}).result();
        assert.equal(settled.stopReason, "error");
      }
      // 暂停在账本写入前触发：无 authorize、无 outcome。
      assert.deepEqual(f.calls, []);
    }
  } finally {
    delete process.env.PODCAST_BUSINESS_PAUSE_FILE;
    rmSync(dir, { recursive: true, force: true });
  }
});

test("账本写入与真实调用之间的窗口出现暂停：complete与stream均不发起供应商请求", async () => {
  setPriceEnv();
  for (const operation of ["complete", "stream"] as const) {
    const dir = mkdtempSync(join(tmpdir(), "budget-race-test-"));
    const pausePath = join(dir, "business.pause");
    process.env.PODCAST_BUSINESS_PAUSE_FILE = pausePath;
    try {
      const f = fixture();
      const pausingDeps: BudgetDependencies = {
        ...f.deps,
        commandRunner: (async (command: string, args: string[], opts?: unknown) => {
          const result = await f.deps.commandRunner!(command, args, opts as never);
          if (args[4] === "authorize") writeFileSync(pausePath, "paused after authorize\n");
          return result;
        }) as BudgetDependencies["commandRunner"],
      };
      const guarded = budgetModels(f.models, { episodeId: 17 }, pausingDeps);
      const context = { messages: [{ role: "user", content: operation, timestamp: 1 }] } as never;
      if (operation === "complete") {
        await assert.rejects(guarded.completeSimple(model, context, {}), /业务暂停标记存在/);
        assert.equal(f.calls.includes("provider:complete"), false);
      } else {
        const settled = await guarded.streamSimple(model, context, {}).result();
        assert.equal(settled.stopReason, "error");
        assert.equal(f.calls.includes("provider:stream"), false);
      }
      // 已写入的 intent 被保留为 unknown 占用。
      assert.equal(f.status(), "unknown");
    } finally {
      delete process.env.PODCAST_BUSINESS_PAUSE_FILE;
      rmSync(dir, { recursive: true, force: true });
    }
  }
});

test("队列子进程失租：complete与stream在heartbeat updated=false时拒绝付费调用", async () => {
  setPriceEnv();
  for (const operation of ["complete", "stream"] as const) {
    const dir = mkdtempSync(join(tmpdir(), "budget-lease-test-"));
    const pausePath = join(dir, "business.pause");
    process.env.PODCAST_BUSINESS_PAUSE_FILE = pausePath;
    process.env.PODCAST_JOB_ID = "42";
    process.env.PODCAST_JOB_WORKER_ID = "worker-x";
    try {
      const f = fixture();
      const leaseDeps: BudgetDependencies = {
        ...f.deps,
        commandRunner: (async (command: string, args: string[], opts?: unknown) => {
          if (args[4] === "heartbeat") {
            return {
              stdout: JSON.stringify({ status: "success", data: { updated: false } }),
              stderr: "",
              exitCode: 0,
            };
          }
          return f.deps.commandRunner!(command, args, opts as never);
        }) as BudgetDependencies["commandRunner"],
      };
      const guarded = budgetModels(f.models, { episodeId: 17 }, leaseDeps);
      const context = { messages: [{ role: "user", content: operation, timestamp: 1 }] } as never;
      if (operation === "complete") {
        await assert.rejects(guarded.completeSimple(model, context, {}), /租约失效/);
        assert.equal(f.calls.includes("provider:complete"), false);
      } else {
        const settled = await guarded.streamSimple(model, context, {}).result();
        assert.equal(settled.stopReason, "error");
        assert.equal(f.calls.includes("provider:stream"), false);
      }
      // 供应商请求未发起；失租写入暂停标记阻止其他付费入口。
      assert.match(readFileSync(pausePath, "utf8"), /job_lease_lost/);
      assert.equal(f.status(), "unknown");
    } finally {
      delete process.env.PODCAST_BUSINESS_PAUSE_FILE;
      delete process.env.PODCAST_JOB_ID;
      delete process.env.PODCAST_JOB_WORKER_ID;
      rmSync(dir, { recursive: true, force: true });
    }
  }
});

test("heartbeat await 窗口内出现暂停：updated=true 也不发起供应商请求（complete与stream）", async () => {
  setPriceEnv();
  for (const operation of ["complete", "stream"] as const) {
    const dir = mkdtempSync(join(tmpdir(), "budget-hb-race-test-"));
    const pausePath = join(dir, "business.pause");
    process.env.PODCAST_BUSINESS_PAUSE_FILE = pausePath;
    process.env.PODCAST_JOB_ID = "42";
    process.env.PODCAST_JOB_WORKER_ID = "worker-x";
    try {
      const f = fixture();
      const racingDeps: BudgetDependencies = {
        ...f.deps,
        commandRunner: (async (command: string, args: string[], opts?: unknown) => {
          if (args[4] === "heartbeat") {
            writeFileSync(pausePath, "paused during heartbeat\n");
            return {
              stdout: JSON.stringify({ status: "success", data: { updated: true } }),
              stderr: "",
              exitCode: 0,
            };
          }
          return f.deps.commandRunner!(command, args, opts as never);
        }) as BudgetDependencies["commandRunner"],
      };
      const guarded = budgetModels(f.models, { episodeId: 17 }, racingDeps);
      const context = { messages: [{ role: "user", content: `hb-race-${operation}`, timestamp: 1 }] } as never;
      if (operation === "complete") {
        await assert.rejects(guarded.completeSimple(model, context, {}), /业务暂停标记存在/);
        assert.equal(f.calls.includes("provider:complete"), false);
      } else {
        const settled = await guarded.streamSimple(model, context, {}).result();
        assert.equal(settled.stopReason, "error");
        assert.equal(f.calls.includes("provider:stream"), false);
      }
      assert.equal(f.status(), "unknown");
    } finally {
      delete process.env.PODCAST_BUSINESS_PAUSE_FILE;
      delete process.env.PODCAST_JOB_ID;
      delete process.env.PODCAST_JOB_WORKER_ID;
      rmSync(dir, { recursive: true, force: true });
    }
  }
});

test("子代理标记使用稳定callRef与角色账目", async () => {
  setPriceEnv();
  const f = fixture({ preauthorized: true });
  const guarded = budgetModels(f.models, { episodeId: 17 }, f.deps);
  const context = tagBudgetCall({ messages: [] }, {
    episodeId: 17, purpose: "translate", provider: "deepseek", model: "mock-main",
    callRef: "translate:17:v1",
  });
  await guarded.completeSimple(model, context, {});
  assert.ok(f.calls.includes("ledger:get"));
  assert.ok(f.calls.includes("ledger:claim"));
  assert.ok(f.calls.indexOf("ledger:claim") < f.calls.indexOf("provider:complete"));
  assert.equal(f.calls.includes("ledger:authorize"), false);
  assert.equal(f.status(), "success");
  await assert.rejects(guarded.completeSimple(model, context, {}), /子代理预算intent缺失/);
  assert.equal(f.calls.filter((call) => call === "provider:complete").length, 1);
});
