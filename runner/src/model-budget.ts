// 执行层·pi-ai 模型逐请求费用准入与账本核销；主 Agent 和子代理共用同一入口。
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { createAssistantMessageEventStream, type AssistantMessage, type AssistantMessageEvent, type Models, type Model } from "@earendil-works/pi-ai";
import { estimateContextTokens } from "@earendil-works/pi-ai/utils/estimate";

import { runCommand, type RunResult } from "./bash.ts";
import { assertBusinessRunning, businessPausePath } from "./pause.ts";
import { acquireProviderGate, releaseProviderGate } from "./provider-gate.ts";

const REPO_ROOT = fileURLToPath(new URL("../..", import.meta.url));
const BUDGET_CALL = Symbol.for("podcast-agent.budget-call");
// Audited provider/API pairs only: DeepSeek's OpenAI-compatible SSE path honors maxTokens and maxRetries.
const BUDGET_SAFE_ROUTES = new Set(["deepseek/openai-completions"]);

export interface ModelBudgetScope {
  episodeId: number;
  dbPath?: string;
}

export interface ModelBudgetCall {
  preauthorized?: boolean;
  episodeId: number;
  purpose: string;
  provider: string;
  model: string;
  callRef: string;
  dbPath?: string;
}

export function tagBudgetCall<T extends object>(context: T, call: ModelBudgetCall): T {
  Object.defineProperty(context, BUDGET_CALL, { value: call, enumerable: false });
  return context;
}

export interface BudgetDependencies {
  commandRunner?: typeof runCommand;
  acquire?: typeof acquireProviderGate;
  release?: typeof releaseProviderGate;
  now?: () => Date;
}

/** 单请求输出上限：默认值必须 ≤ 模型上限，避免把模型最大输出当作每次请求的预留。 */
function requestOutputLimit(model: Model<any>, requested?: number): number {
  const raw = process.env.PODCAST_MODEL_MAX_OUTPUT_TOKENS ?? "8192";
  const cap = Number(raw);
  if (!Number.isSafeInteger(cap) || cap <= 0) {
    throw new Error("PODCAST_MODEL_MAX_OUTPUT_TOKENS 非法");
  }
  const limit = Math.min(requested ?? cap, cap, model.maxTokens);
  if (!Number.isSafeInteger(limit) || limit <= 0) {
    throw new Error(`模型请求输出上限非法: ${model.provider}/${model.id}`);
  }
  return limit;
}

/** 上下文 token 上界估算：实际消息估算 + 安全余量，不超过上下文窗口。 */
function requestInputTokens(model: Model<any>, context: unknown): number {
  const safety = Number(process.env.PODCAST_MODEL_INPUT_SAFETY_TOKENS ?? "4096");
  if (!Number.isSafeInteger(safety) || safety < 0) {
    throw new Error("PODCAST_MODEL_INPUT_SAFETY_TOKENS 非法");
  }
  let estimate = model.contextWindow;
  try {
    const measured = estimateContextTokens(context as never).tokens;
    if (Number.isFinite(measured) && measured >= 0) {
      estimate = Math.min(model.contextWindow, Math.ceil(measured) + safety);
    }
  } catch {
    // 无法估算时退回上下文窗口上界（保守）。
  }
  return Math.max(1, estimate);
}

function priceConfig(
  model: Model<any>,
  now: Date,
  request: { inputTokens: number; outputTokens: number },
): { estimate: number; basis: string } {
  const modelKey = `${model.provider}/${model.id}`;
  if (!BUDGET_SAFE_ROUTES.has(`${model.provider}/${model.api}`)) {
    throw new Error(`模型API未完成单次请求与输出token能力验收: ${model.provider}/${model.api}`);
  }
  const allowlist = (process.env.PODCAST_MODEL_PRICE_MODELS ?? "").split(",").map((x) => x.trim()).filter(Boolean);
  if (!allowlist.includes(modelKey)) {
    throw new Error(`缺少当前模型的显式价格校准白名单: ${modelKey}`);
  }
  const source = process.env.PODCAST_MODEL_PRICE_SOURCE ?? "";
  const pricingDate = process.env.PODCAST_MODEL_PRICING_DATE ?? "";
  if (!source.startsWith("https://") || source.includes(";")) {
    throw new Error("PODCAST_MODEL_PRICE_SOURCE 必须为可审计 HTTPS URL");
  }
  const parsedDate = new Date(`${pricingDate}T00:00:00.000Z`);
  const ageDays = (now.getTime() - parsedDate.getTime()) / 86_400_000;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(pricingDate) || !Number.isFinite(ageDays) || ageDays < 0 || ageDays > 30 || parsedDate.toISOString().slice(0, 10) !== pricingDate) {
    throw new Error("PODCAST_MODEL_PRICING_DATE 缺失、未来日期或超过30日");
  }
  if (!Number.isSafeInteger(model.contextWindow) || model.contextWindow <= 0 ||
      !Number.isSafeInteger(model.maxTokens) || model.maxTokens <= 0) {
    throw new Error(`模型缺少可靠 contextWindow/maxTokens 上限: ${modelKey}`);
  }
  const rateSets = [model.cost, ...(model.cost.tiers ?? [])];
  let inputRate = 0;
  let outputRate = 0;
  for (const rates of rateSets) {
    const values = [rates.input, rates.output, rates.cacheRead, rates.cacheWrite];
    if (values.some((value) => !Number.isFinite(value) || value < 0)) {
      throw new Error(`模型价格目录含非法费率: ${modelKey}`);
    }
    inputRate = Math.max(inputRate, rates.input, rates.cacheRead, rates.cacheWrite);
    outputRate = Math.max(outputRate, rates.output);
  }
  const inputTokens = Math.min(model.contextWindow, Math.max(1, Math.floor(request.inputTokens)));
  const outputTokens = Math.min(model.maxTokens, Math.max(1, Math.floor(request.outputTokens)));
  // 预留按本次请求的实际上下文估算 + 显式输出上限计算，而非模型窗口最坏情形。
  const raw = (inputTokens * inputRate + outputTokens * outputRate) / 1_000_000;
  if (!Number.isFinite(raw) || raw <= 0 || !Number.isFinite(raw * 1_000_000_000_000)) {
    throw new Error(`模型最大费用估算为零或超出可表示范围: ${modelKey}`);
  }
  // 向上取整到 1e-12 USD，避免浮点乘除将上界向下舍入。
  const estimate = Math.ceil(raw * 1_000_000_000_000) / 1_000_000_000_000;
  const safeSource = source.replaceAll("=", "%3D");
  return {
    estimate,
    basis: `estimate=request_bound;pricing_date=${pricingDate};source=${safeSource};` +
      `input_tokens=${inputTokens};output_tokens=${outputTokens};` +
      `context_window=${model.contextWindow};model_max_output=${model.maxTokens};` +
      `max_input_usd_per_million=${inputRate};max_output_usd_per_million=${outputRate}`,
  };
}

/** 子代理预授权输入上界：提示由固定上下文文件构成，不用模型窗口最坏情形。 */
function subagentInputBound(model: Model<any>): number {
  const raw = process.env.PODCAST_SUBAGENT_INPUT_TOKENS ?? "40000";
  const bound = Number(raw);
  if (!Number.isSafeInteger(bound) || bound <= 0) {
    throw new Error("PODCAST_SUBAGENT_INPUT_TOKENS 非法");
  }
  return Math.min(model.contextWindow, bound);
}

export function estimateModelMaxCost(
  model: Model<any>,
  now: Date = new Date(),
  request?: { inputTokens?: number; outputTokens?: number },
  mode: "request" | "subagent" = "request",
): { amount: number; usageBasis: string } {
  const price = priceConfig(model, now, {
    inputTokens: request?.inputTokens
      ?? (mode === "subagent" ? subagentInputBound(model) : requestInputTokens(model, undefined)),
    outputTokens: request?.outputTokens ?? requestOutputLimit(model),
  });
  return { amount: price.estimate, usageBasis: price.basis };
}

function singleAttemptArgs(args: unknown[], model: Model<any>): unknown[] {
  const options = (args[2] ?? {}) as { maxTokens?: number; deferred?: unknown; [key: string]: unknown };
  if (options.deferred) throw new Error("deferred模型请求尚未接入预算与对账，拒绝调用");
  const maxTokens = requestOutputLimit(model, options.maxTokens);
  if (options.maxTokens !== undefined &&
      (!Number.isSafeInteger(options.maxTokens) || options.maxTokens <= 0 || options.maxTokens > model.maxTokens)) {
    throw new Error(`模型请求maxTokens超过已预算上限: ${model.provider}/${model.id}`);
  }
  return [args[0], args[1], { ...options, maxTokens, maxRetries: 0, maxRetryDelayMs: 0 }];
}

function callMetadata(
  model: Model<any>,
  context: unknown,
  scope: ModelBudgetScope,
): ModelBudgetCall {
  const tagged = context && typeof context === "object"
    ? (context as Record<PropertyKey, unknown>)[BUDGET_CALL] as ModelBudgetCall | undefined
    : undefined;
  if (tagged) {
    if (tagged.episodeId !== scope.episodeId || tagged.provider !== model.provider || tagged.model !== model.id ||
        tagged.dbPath !== scope.dbPath) {
      throw new Error("子代理账本归属与当前模型/Runner 费用范围不匹配");
    }
    return { ...tagged, preauthorized: true };
  }
  let serialized: string;
  const withoutMessageTimestamps = (value: unknown): unknown => {
    const normalizeMessage = (message: unknown): unknown => {
      if (!message || typeof message !== "object" || Array.isArray(message)) return message;
      const record = message as Record<string, unknown>;
      if (!["system", "user", "assistant", "toolResult"].includes(String(record.role))) return message;
      return Object.fromEntries(Object.entries(record).filter(([key]) => key !== "timestamp"));
    };
    if (Array.isArray(value)) return value.map(normalizeMessage);
    if (value && typeof value === "object") {
      const record = value as Record<string, unknown>;
      if (Array.isArray(record.messages)) return { ...record, messages: record.messages.map(normalizeMessage) };
    }
    return value;
  };
  try {
    // Durable compaction reconstructs message timestamps on resume; preserve all nested business fields.
    serialized = JSON.stringify({ provider: model.provider, model: model.id, context: withoutMessageTimestamps(context) });
  } catch {
    throw new Error("模型请求上下文不可稳定序列化，拒绝付费调用");
  }
  if (serialized === undefined) throw new Error("模型请求上下文为空，拒绝付费调用");
  const digest = createHash("sha256").update(serialized).digest("hex");
  return {
    preauthorized: false,
    episodeId: scope.episodeId,
    purpose: "agent",
    provider: model.provider,
    model: model.id,
    callRef: `agent:${scope.episodeId}:${digest}`,
    dbPath: scope.dbPath,
  };
}

/**
 * 在途子进程跨进程失租感知：队列启动的 Episode 子进程带有 job/worker 身份时，
 * 在每个付费边界续约复查；updated=false（失租或被接管）即写暂停标记并抛错，
 * 停止新增付费调用。环境身份缺失时跳过（人工入口由共享锁与暂停文件约束）。
 */
async function assertJobLeaseAlive(commandRunner: typeof runCommand): Promise<void> {
  const jobId = process.env.PODCAST_JOB_ID;
  const workerId = process.env.PODCAST_JOB_WORKER_ID;
  if (!jobId || !workerId) return;
  const result = await commandRunner("uv", [
    "run", "python", "-m", "state.postgres.job_cli", "heartbeat",
    jobId, "--worker-id", workerId,
  ], { cwd: REPO_ROOT, timeoutMs: 30_000 });
  let updated = false;
  try {
    const payload = JSON.parse(result.stdout.trim().split("\n").at(-1) ?? "{}") as {
      status?: string; data?: { updated?: boolean }; error?: string;
    };
    updated = payload.status === "success" && payload.data?.updated === true;
  } catch {
    updated = false;
  }
  if (result.exitCode !== 0 || !updated) {
    // 失租：尽力写暂停标记阻止同机其他付费入口，随后失败关闭。
    try {
      const { writeFileSync } = await import("node:fs");
      writeFileSync(businessPausePath(), `job_lease_lost job=${jobId} worker=${workerId}\n`);
    } catch { /* 进程内抛错仍然阻止本次调用。 */ }
    throw new Error(`job 租约失效（失租或被接管），停止新增付费调用: job=${jobId}`);
  }
}

async function ledgerCommand(
  command: "authorize" | "claim" | "outcome" | "get" | "reconcile",
  args: string[],
  commandRunner: typeof runCommand,
): Promise<Record<string, unknown>> {
  const result: RunResult = await commandRunner("uv", ["run", "python", "-m", "state.ledger_cli", command, ...args], {
    cwd: REPO_ROOT,
    timeoutMs: 30_000,
  });
  const lastLine = result.stdout.trim().split("\n").at(-1) ?? "{}";
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(lastLine) as Record<string, unknown>;
  } catch {
    throw new Error(`ledger ${command} 未返回 JSON`);
  }
  if (result.exitCode !== 0 || payload.status !== "success") {
    throw new Error(`ledger ${command} 失败: ${String(payload.error ?? result.stderr.trim())}`);
  }
  // ledger_cli 统一 envelope：{status:"success", data:{...}}
  const data = payload.data;
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new Error(`ledger ${command} 返回缺 data`);
  }
  return data as Record<string, unknown>;
}

function ledgerArgs(call: ModelBudgetCall, basis?: string): string[] {
  const args = ["--episode-id", String(call.episodeId), "--purpose", call.purpose,
    "--provider", call.provider, "--model", call.model, "--call-ref", call.callRef];
  if (basis !== undefined) args.push("--usage-basis", basis);
  if (call.dbPath) args.push("--db", call.dbPath);
  return args;
}

async function persistOutcome(
  call: ModelBudgetCall,
  status: "success" | "unknown",
  basis: string,
  amount: number,
  commandRunner: typeof runCommand,
): Promise<void> {
  const db = call.dbPath ? ["--db", call.dbPath] : [];
  const state = await ledgerCommand("get", ["--call-ref", call.callRef, ...db], commandRunner);
  if (state.status === "intent") {
    const updated = await ledgerCommand("outcome", ["--call-ref", call.callRef, "--status", status,
      "--usage-basis", basis, "--amount-est", String(amount), ...db], commandRunner);
    if (updated.status === status || (status === "unknown" && updated.status === "unknown")) return;
    if (status === "success" && updated.status === "unknown") {
      await ledgerCommand("reconcile", ["--call-ref", call.callRef, "--usage-basis", basis,
        "--amount-est", String(amount), ...db], commandRunner);
      return;
    }
    throw new Error(`ledger outcome 状态冲突: ${String(updated.status)}`);
  }
  if (status === "success" && state.status === "unknown") {
    await ledgerCommand("reconcile", ["--call-ref", call.callRef, "--usage-basis", basis,
      "--amount-est", String(amount), ...db], commandRunner);
    return;
  }
  if (state.status === status) return;
  throw new Error(`ledger 状态不允许核销: ${String(state.status)} -> ${status}`);
}

function usageBasis(message: AssistantMessage, estimateBasis: string): { basis: string; actualCost?: number } {
  const usage = message.usage;
  const values = [usage?.input, usage?.output, usage?.cacheRead, usage?.cacheWrite,
    usage?.totalTokens, usage?.cost?.total];
  const valid = values.every((value) => typeof value === "number" && Number.isFinite(value) && value >= 0);
  // pi-ai initializes absent provider usage to all-zero counters; that is not evidence of a free request.
  if (!valid || usage.totalTokens === 0 || (usage.cost.total === 0 && usage.cost.totalFromProvider !== true)) {
    return { basis: `${estimateBasis};usage_missing=true` };
  }
  return {
    actualCost: usage.cost.total,
    basis: `${estimateBasis};input_tokens=${usage.input};output_tokens=${usage.output};` +
      `cache_read_tokens=${usage.cacheRead};cache_write_tokens=${usage.cacheWrite};` +
      `total_tokens=${usage.totalTokens};cost_usd=${usage.cost.total};` +
      `cost_source=${usage.cost.totalFromProvider === true ? "provider" : "catalog"}`,
  };
}

function syntheticError(model: Model<any>, error: unknown): AssistantMessage {
  return {
    role: "assistant", content: [], api: model.api, provider: model.provider, model: model.id,
    usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    stopReason: "error", errorMessage: error instanceof Error ? error.message : String(error), timestamp: Date.now(),
  };
}

export function budgetModels(
  models: Models,
  scope: ModelBudgetScope,
  dependencies: BudgetDependencies = {},
): Models {
  if (!Number.isInteger(scope.episodeId) || scope.episodeId <= 0) throw new Error("Runner 费用归属必须提供正 Episode ID");
  const commandRunner = dependencies.commandRunner ?? runCommand;
  const acquire = dependencies.acquire ?? acquireProviderGate;
  const release = dependencies.release ?? releaseProviderGate;
  const now = dependencies.now ?? (() => new Date());

  const guarded = async (method: Function, args: unknown[]): Promise<AssistantMessage> => {
    const model = args[0] as Model<any>;
    const context = args[1];
    const metadata = callMetadata(model, context, scope);
    const safeArgs = singleAttemptArgs(args, model);
    const price = priceConfig(model, now(), {
      inputTokens: requestInputTokens(model, context),
      outputTokens: (safeArgs[2] as { maxTokens: number }).maxTokens,
    });
    // 付费边界复查暂停：取限流许可与写账本之前失败关闭。
    assertBusinessRunning();
    const lease = await acquire(model.provider, commandRunner);
    let authorized = false;
    let finished = false;
    try {
      // gate→intent 窗口复查：限流许可返回后、账本写入前暂停标记可能出现；
      // 置于 try 内确保 provider gate 一定释放。
      assertBusinessRunning();
      const basis = `${price.basis};reserved_usd=${price.estimate}`;
      if (metadata.preauthorized) {
        const intent = await ledgerCommand("get", ["--call-ref", metadata.callRef,
          ...(metadata.dbPath ? ["--db", metadata.dbPath] : [])], commandRunner);
        const reserved = Number(intent.amount_est);
        if (intent.status !== "intent" || !Number.isFinite(reserved) || reserved + 1e-12 < price.estimate || intent.request_started !== 0) {
          throw new Error("子代理预算intent缺失、已claim或低于当前模型上界");
        }
        await ledgerCommand("claim", ["--call-ref", metadata.callRef,
          ...(metadata.dbPath ? ["--db", metadata.dbPath] : [])], commandRunner);
      } else {
        await ledgerCommand("authorize", [
          ...ledgerArgs(metadata, basis), "--amount-est", String(price.estimate),
        ], commandRunner);
      }
      authorized = true;
      let message: AssistantMessage;
      try {
        // 账本写入与真实调用之间的 await 窗内可能出现暂停标记，发起供应商请求前复查；
        // 队列子进程同时复查 job 租约，失租即停止新增付费调用。
        assertBusinessRunning();
        await assertJobLeaseAlive(commandRunner);
        // heartbeat 的 await 窗内同样可能出现暂停标记，供应商调用前最终复查。
        assertBusinessRunning();
        message = await Reflect.apply(method, models, safeArgs) as AssistantMessage;
      } catch (error) {
        await ledgerCommand("outcome", [
          "--call-ref", metadata.callRef, "--status", "unknown",
          "--usage-basis", `${basis};request_error=${error instanceof Error ? error.name : "unknown"}`,
          ...(metadata.dbPath ? ["--db", metadata.dbPath] : []),
        ], commandRunner);
        finished = true;
        throw error;
      }
      const settled = usageBasis(message, price.basis);
      const usage = message.usage;
      const overReserved = settled.actualCost !== undefined && settled.actualCost > price.estimate;
      const tokenOverrun = usage.input + usage.cacheRead + usage.cacheWrite > model.contextWindow || usage.output > model.maxTokens;
      if (message.stopReason === "error" || message.stopReason === "aborted" || message.stopReason === "deferred" || overReserved || tokenOverrun) {
        const amount = settled.actualCost === undefined ? price.estimate : Math.max(price.estimate, settled.actualCost);
        const basisWithError = `${settled.basis};guard_stop=${message.stopReason};` +
          `estimate_overrun=${overReserved};token_overrun=${tokenOverrun}`;
        await persistOutcome(metadata, "unknown", basisWithError, amount, commandRunner);
        finished = true;
        throw new Error(`模型请求结果/费用超出预算契约，停止自动调用: ${message.stopReason}`);
      }
      const amount = settled.actualCost === undefined ? price.estimate : settled.actualCost;
      await persistOutcome(metadata, "success", settled.basis, amount, commandRunner);
      finished = true;
      return message;
    } catch (error) {
      if (authorized && !finished) {
        try {
          await ledgerCommand("outcome", ["--call-ref", metadata.callRef, "--status", "unknown",
            "--usage-basis", `estimate_usd=${price.estimate};request_error=${error instanceof Error ? error.name : "unknown"}`,
            ...(metadata.dbPath ? ["--db", metadata.dbPath] : [])], commandRunner);
        } catch {
          // 保留已提交 intent；不放行下一次付费请求。
        }
      }
      throw error;
    } finally {
      try { await release(model.provider, lease, commandRunner); } catch { /* Redis lease expires; ledger remains authoritative. */ }
    }
  };

  const guardedStream = (method: Function, args: unknown[]) => {
    const output = createAssistantMessageEventStream();
    void startStream(method, args, models, scope, dependencies, output, now);
    return output;
  };

  return new Proxy(models, {
    get(target, property) {
      const value = Reflect.get(target, property, target);
      if (property === "completeSimple" || property === "complete") {
        return (...args: unknown[]) => guarded(value as Function, args);
      }
      if (property === "streamSimple" || property === "stream") {
        return (...args: unknown[]) => guardedStream(value as Function, args);
      }
      if (["generateImages", "classify", "fetchDeferred", "streamDeferred", "cancelDeferred"].includes(String(property))) {
        return () => { throw new Error(`Runner 未实现该模型操作的费用上限，拒绝调用: ${String(property)}`); };
      }
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

async function startStream(
  method: Function,
  args: unknown[],
  models: Models,
  scope: ModelBudgetScope,
  dependencies: BudgetDependencies,
  output: ReturnType<typeof createAssistantMessageEventStream>,
  now: () => Date,
): Promise<void> {
  const model = args[0] as Model<any>;
  const context = args[1];
  const acquire = dependencies.acquire ?? acquireProviderGate;
  const release = dependencies.release ?? releaseProviderGate;
  const commandRunner = dependencies.commandRunner ?? runCommand;
  let metadata!: ModelBudgetCall;
  let price!: ReturnType<typeof priceConfig>;
  let lease: string | undefined;
  let authorized = false;
  let finished = false;
  let started = false;
  let terminalEvent: AssistantMessageEvent | undefined;
  let basis = "";
  const cli = (command: "authorize" | "claim" | "outcome" | "get" | "reconcile", tail: string[]) =>
    ledgerCommand(command, tail, commandRunner);
  let db: string[] = [];
  try {
    metadata = callMetadata(model, context, scope);
    const safeArgs = singleAttemptArgs(args, model);
    price = priceConfig(model, now(), {
      inputTokens: requestInputTokens(model, context),
      outputTokens: (safeArgs[2] as { maxTokens: number }).maxTokens,
    });
    // 付费边界复查暂停：取限流许可与写账本之前失败关闭。
    assertBusinessRunning();
    lease = await acquire(model.provider, commandRunner);
    // gate→intent 窗口复查：限流许可返回后、账本写入前暂停标记可能出现。
    assertBusinessRunning();
    basis = `${price.basis};reserved_usd=${price.estimate}`;
    db = metadata.dbPath ? ["--db", metadata.dbPath] : [];
    if (metadata.preauthorized) {
      const intent = await cli("get", ["--call-ref", metadata.callRef, ...db]);
      const reserved = Number(intent.amount_est);
      if (intent.status !== "intent" || !Number.isFinite(reserved) || reserved + 1e-12 < price.estimate || intent.request_started !== 0) {
        throw new Error("子代理预算intent缺失、已claim或低于当前模型上界");
      }
      await cli("claim", ["--call-ref", metadata.callRef, ...db]);
    } else {
      await cli("authorize", [...ledgerArgs(metadata, basis), "--amount-est", String(price.estimate)]);
    }
    authorized = true;
    let source;
    try {
      // 账本写入与真实调用之间的 await 窗内可能出现暂停标记，发起供应商请求前复查；
      // 队列子进程同时复查 job 租约，失租即停止新增付费调用。
      assertBusinessRunning();
      await assertJobLeaseAlive(commandRunner);
      // heartbeat 的 await 窗内同样可能出现暂停标记，供应商调用前最终复查。
      assertBusinessRunning();
      source = Reflect.apply(method, models, safeArgs) as ReturnType<Models["streamSimple"]>;
    } catch (error) {
      await cli("outcome", ["--call-ref", metadata.callRef, "--status", "unknown",
        "--usage-basis", `${basis};request_error=${error instanceof Error ? error.name : "unknown"}`, ...db]);
      finished = true;
      throw error;
    }
    for await (const event of source) {
      if (event.type === "done" || event.type === "error") {
        terminalEvent = event;
        continue;
      }
      if (event.type === "start") started = true;
      output.push(event);
    }
    const message = await source.result();
    const settled = usageBasis(message, price.basis);
    const usage = message.usage;
    const overReserved = settled.actualCost !== undefined && settled.actualCost > price.estimate;
    const tokenOverrun = usage.input + usage.cacheRead + usage.cacheWrite > model.contextWindow || usage.output > model.maxTokens;
    if (message.stopReason === "error" || message.stopReason === "aborted" || message.stopReason === "deferred" || overReserved || tokenOverrun) {
      const amount = settled.actualCost === undefined ? price.estimate : Math.max(price.estimate, settled.actualCost);
      const basisWithError = `${settled.basis};guard_stop=${message.stopReason};estimate_overrun=${overReserved};token_overrun=${tokenOverrun}`;
      await persistOutcome(metadata, "unknown", basisWithError, amount, commandRunner);
      finished = true;
      const errorMessage = syntheticError(model, `模型请求结果/费用超出预算契约，停止自动调用: ${message.stopReason}`);
      if (!started) output.push({ type: "start", partial: errorMessage });
      output.push({ type: "error", reason: "error", error: errorMessage });
      return;
    }
    const amount = settled.actualCost === undefined ? price.estimate : settled.actualCost;
    await persistOutcome(metadata, "success", settled.basis, amount, commandRunner);
    finished = true;
    if (terminalEvent) {
      if (!started) output.push({ type: "start", partial: message });
      output.push(terminalEvent);
    } else output.end(message);
  } catch (error) {
    if (authorized && !finished) {
      try {
        await cli("outcome", ["--call-ref", metadata.callRef, "--status", "unknown",
          "--usage-basis", `${basis};request_error=${error instanceof Error ? error.name : "unknown"}`, ...db]);
      } catch { /* 留下 reserved intent 继续失败关闭。 */ }
    }
    const message = syntheticError(model, error);
    if (!started) output.push({ type: "start", partial: message });
    output.push({ type: "error", reason: "error", error: message });
    output.end(message);
  } finally {
    if (lease !== undefined) {
      try { await release(model.provider, lease, commandRunner); } catch { /* Redis lease expires; ledger remains authoritative. */ }
    }
  }
}
