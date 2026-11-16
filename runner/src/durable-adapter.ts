// 执行层·pi-durable 适配层：ToolSpec → ToolRegistration 转换
//
// 换框架时只有本文件与 runner.ts 需要重写；业务工具与 ToolSpec 契约不变。
import { defineExtension, defineTool, type Registry, type ToolRegistration, type ToolExecutionApi, type TaskId } from "@earendil-works/pi-durable";
import type { Context } from "@earendil-works/chord";
import { resolve } from "node:path";

import { runCommand } from "./bash.ts";
import { assertBusinessRunning } from "./pause.ts";
import type { JsonValue, ToolSpec } from "./tool-spec.ts";
import { taskWaitTimeout, waitForTerminalTask } from "./task-wait.ts";
import { defineSubagentTask, type SubagentInput, type SubagentResult } from "./experiment-subagent.ts";

const subagentTask = defineSubagentTask();
const REPO_ROOT = resolve(import.meta.dirname, "../..");

/** 经 Python 窄 CLI 写账本（Node 不拼 SQL）。失败抛错。 */
async function ledgerCli(command: "authorize" | "outcome" | "reconcile", args: string[]): Promise<void> {
  const r = await runCommand("uv", ["run", "python", "-m", "state.ledger_cli", command, ...args], {
    cwd: REPO_ROOT,
    timeoutMs: 30_000,
  });
  if (r.exitCode !== 0) {
    throw new Error(`ledger ${command} 失败: ${r.stdout.trim()} ${r.stderr.trim()}`);
  }
}

async function writeIntent(
  meta: NonNullable<SubagentInput["ledger"]> & object,
  callRef: string,
  estimate: { amount: number; usageBasis: string },
): Promise<void> {
  const args = [
    "--episode-id", String(meta.episodeId), "--purpose", meta.purpose,
    "--provider", meta.provider, "--model", meta.model, "--call-ref", callRef,
    "--usage-basis", estimate.usageBasis, "--amount-est", String(estimate.amount), "--defer-start",
  ];
  if (meta.dbPath) args.push("--db", meta.dbPath);
  await ledgerCli("authorize", args);
}

async function writeOutcome(
  meta: { dbPath?: string },
  callRef: string,
  status: "success" | "failed" | "unknown",
  usageBasis?: string,
): Promise<void> {
  const args = ["--call-ref", callRef, "--status", status];
  if (usageBasis) args.push("--usage-basis", usageBasis);
  if (meta.dbPath) args.push("--db", meta.dbPath);
  await ledgerCli("outcome", args);
}

async function getLedgerStatus(callRef: string, dbPath?: string): Promise<string> {
  const args = ["--call-ref", callRef];
  if (dbPath) args.push("--db", dbPath);
  const r = await runCommand("uv", ["run", "python", "-m", "state.ledger_cli", "get", ...args], {
    cwd: REPO_ROOT, timeoutMs: 30_000,
  });
  const lastLine = r.stdout.trim().split("\n").at(-1) ?? "{}";
  // ledger_cli 统一 envelope：{status:"success", data:{status: ...}}
  const payload = JSON.parse(lastLine) as { status?: string; data?: { status?: string } };
  if (payload.status !== "success" || !payload.data?.status) {
    throw new Error(`ledger get 未返回有效状态: ${lastLine.slice(0, 200)}`);
  }
  return payload.data.status;
}

async function reconcileLedger(callRef: string, usageBasis: string | undefined, dbPath?: string): Promise<void> {
  const args = ["--call-ref", callRef];
  if (usageBasis) args.push("--usage-basis", usageBasis);
  if (dbPath) args.push("--db", dbPath);
  await ledgerCli("reconcile", args);
}

export interface DispatchLedger {
  writeIntent: typeof writeIntent;
  writeOutcome: typeof writeOutcome;
  getStatus: typeof getLedgerStatus;
  reconcile: typeof reconcileLedger;
}
const defaultLedger: DispatchLedger = {
  writeIntent, writeOutcome, getStatus: getLedgerStatus, reconcile: reconcileLedger,
};

/** 同一实现供真实工具与 fake API/账本契约测试使用，不另建调度状态机。 */
export type SubagentEstimate = (input: SubagentInput) => { amount: number; usageBasis: string };

export async function dispatchSubagent(
  input: SubagentInput,
  api: Pick<ToolExecutionApi, "commit" | "conversationId" | "createTask" | "waitForTask">,
  context: Context,
  ledger: DispatchLedger = defaultLedger,
  estimateSubagent?: SubagentEstimate,
) {
  // 配置在任何 intent 或 createTask 之前校验，非法配置不能触发付费；暂停标记存在时拒绝派发。
  assertBusinessRunning();
  const waitTimeoutMs = taskWaitTimeout(process.env.PODCAST_SUBAGENT_WAIT_TIMEOUT_MS);
  if (input.model && input.model.provider !== "mock" && !input.ledger) {
    throw new Error("真实子代理派发必须提供费用账本归属");
  }
  if (input.ledger && !input.model) throw new Error("子代理账本归属缺少模型路由");
  const kind = subagentTask.definition.name;
  // 跨调用去重：扫描本 conversation 下同 kind 任务的 input.callRef，命中则复用既有任务
  //（memo 存在工具任务上、不跨调用，不能用于跨调用去重）
  let taskId: TaskId | undefined;
  let cursor: Record<string, JsonValue> | undefined;
  do {
    const page = await api.commit(
      (tx) => tx.scanTasks({ conversationId: api.conversationId, kind }, 100, cursor),
      context,
    );
    for (const record of page.items) {
      const rec = record as { id: TaskId; input?: { callRef?: string } };
      if (rec.input?.callRef === input.callRef) {
        taskId = rec.id;
        break;
      }
    }
    cursor = page.next;
  } while (cursor !== undefined && taskId === undefined);
  const reused = taskId !== undefined;
  if (taskId === undefined) {
    // 先按目标模型最大上下文费用预算并落 intent，再创建任务；子代理实际请求时一次性claim。
    // 扫描与 intent 之间可能出现暂停标记，写 intent 与创建任务前复查。
    assertBusinessRunning();
    if (input.ledger) {
      if (!input.model || !estimateSubagent) throw new Error("子代理缺少可核实的模型费用估算配置");
      await ledger.writeIntent(input.ledger, input.callRef, estimateSubagent(input));
    }
    try {
      // intent 落库与创建任务之间的 await 窗内可能出现暂停标记，创建前复查。
      assertBusinessRunning();
      taskId = await api.createTask(subagentTask, input, { ownership: { kind: "conversation" } }, context);
    } catch (error) {
      if (input.ledger) await ledger.writeOutcome(input.ledger, input.callRef, "failed");
      throw error;
    }
  }
  let settled;
  try {
    settled = await waitForTerminalTask(
      () => api.waitForTask(taskId!, context), waitTimeoutMs,
    );
  } catch (error) {
    // 停止等待不代表外部调用失败；包括 reused intent 在内均保留 unknown。
    if (input.ledger && await ledger.getStatus(input.callRef, input.ledger.dbPath) === "intent") {
      await ledger.writeOutcome(input.ledger, input.callRef, "unknown");
    }
    throw error;
  }
  const outcome = (settled as { state?: { outcome?: { status: string; result?: SubagentResult } } }).state?.outcome;
  // 账本 outcome：新任务直接写；reused 但账本非 success 时补账（unknown→success reconcile）
  if (input.ledger) {
    const usageOf = (result: unknown): string | undefined => {
      const u = (result as { usage?: Record<string, number | boolean | undefined> })?.usage;
      if (!u) return undefined;
      return u.input !== undefined
        ? `input_tokens=${u.input};output_tokens=${u.output};cache_read=${u.cacheRead ?? 0}` +
          (u.costTotal !== undefined ? `;cost_usd=${u.costTotal}` : "")
        : `prompt_tokens=${u.promptTokens};completion_tokens=${u.completionTokens}`;
    };
    const currentStatus = await ledger.getStatus(input.callRef, input.ledger.dbPath);
    if (outcome?.status === "completed" && outcome.result) {
      const usageBasis = usageOf(outcome.result);
      if (currentStatus === "intent") {
        await ledger.writeOutcome(input.ledger, input.callRef, "success", usageBasis);
      } else if (currentStatus === "unknown") {
        await ledger.reconcile(input.callRef, usageBasis, input.ledger.dbPath);
      }
      // 已 success/failed 不改写
    } else {
      if (outcome?.status === "failed" || outcome?.status === "aborted") {
        if (currentStatus === "intent") await ledger.writeOutcome(input.ledger, input.callRef, "failed");
      } else if (currentStatus === "intent") {
        await ledger.writeOutcome(input.ledger, input.callRef, "unknown");
      }
    }
  }
  if (!outcome || outcome.status !== "completed" || !outcome.result) {
    throw new Error(`子任务未成功完成: ${outcome?.status ?? "unknown"}`);
  }
  return { taskId: String(taskId), result: outcome.result as unknown as JsonValue, reused };
}

export function toDurableTools(specs: readonly ToolSpec[], estimateSubagent?: SubagentEstimate): ToolRegistration[] {
  return specs.map((spec) =>
    defineTool({
      name: spec.name,
      description: spec.description,
      parameters: spec.parameters,
      replay: spec.replay,
      async execute(args, api, context) {
        const extras = {
          memo: (name: string, candidate: JsonValue) => api.memo(name, candidate, context),
          dispatch: (input: SubagentInput) => dispatchSubagent(input, api, context, defaultLedger, estimateSubagent),
        };
        const result = await spec.execute(args, extras);
        return {
          output: [{ type: "text" as const, text: result.text }],
          isError: result.isError,
        };
      },
    }),
  );
}

export function installTools(registry: Registry, specs: readonly ToolSpec[], estimateSubagent?: SubagentEstimate): void {
  registry.install(defineExtension({ name: "runner-tools", tools: toDurableTools(specs, estimateSubagent) }));
}
