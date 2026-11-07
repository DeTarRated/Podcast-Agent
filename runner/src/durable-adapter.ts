// 执行层·pi-durable 适配层：ToolSpec → ToolRegistration 转换
//
// 换框架时只有本文件与 runner.ts 需要重写；业务工具与 ToolSpec 契约不变。
import { defineExtension, defineTool, type Registry, type ToolRegistration } from "@earendil-works/pi-durable";
import { resolve } from "node:path";

import { runCommand } from "./bash.ts";
import type { JsonValue, ToolSpec } from "./tool-spec.ts";
import { defineSubagentTask, type SubagentInput, type SubagentResult } from "./experiment-subagent.ts";

const subagentTask = defineSubagentTask();
const REPO_ROOT = resolve(import.meta.dirname, "../..");

/** 经 Python 窄 CLI 写账本（Node 不拼 SQL）。失败抛错。 */
async function ledgerCli(command: "intent" | "outcome" | "reconcile", args: string[]): Promise<void> {
  const r = await runCommand("uv", ["run", "python", "-m", "state.ledger_cli", command, ...args], {
    cwd: REPO_ROOT,
    timeoutMs: 30_000,
  });
  if (r.exitCode !== 0) {
    throw new Error(`ledger ${command} 失败: ${r.stdout.trim()} ${r.stderr.trim()}`);
  }
}

async function writeIntent(meta: NonNullable<SubagentInput["ledger"]> & object, callRef: string): Promise<void> {
  const args = [
    "--episode-id", String(meta.episodeId),
    "--purpose", meta.purpose,
    "--provider", meta.provider,
    "--model", meta.model,
    "--call-ref", callRef,
    "--usage-basis", "phase=intent",
  ];
  if (meta.dbPath) args.push("--db", meta.dbPath);
  await ledgerCli("intent", args);
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
  return (JSON.parse(lastLine) as { status: string }).status;
}

async function reconcileLedger(callRef: string, usageBasis: string | undefined, dbPath?: string): Promise<void> {
  const args = ["--call-ref", callRef];
  if (usageBasis) args.push("--usage-basis", usageBasis);
  if (dbPath) args.push("--db", dbPath);
  await ledgerCli("reconcile", args);
}

export function toDurableTools(specs: readonly ToolSpec[]): ToolRegistration[] {
  return specs.map((spec) =>
    defineTool({
      name: spec.name,
      description: spec.description,
      parameters: spec.parameters,
      replay: spec.replay,
      async execute(args, api, context) {
        const extras = {
          memo: (name: string, candidate: JsonValue) => api.memo(name, candidate, context),
          dispatch: async (input: SubagentInput) => {
            const kind = subagentTask.definition.name;
            // 跨调用去重：扫描本 conversation 下同 kind 任务的 input.callRef，命中则复用既有任务
            //（memo 存在工具任务上、不跨调用，不能用于跨调用去重）
            let taskId: string | undefined;
            let cursor: Record<string, JsonValue> | undefined;
            do {
              const page = await api.commit(
                (tx) => tx.scanTasks({ conversationId: api.conversationId, kind }, 100, cursor),
                context,
              );
              for (const record of page.items) {
                const rec = record as { id: unknown; input?: { callRef?: string } };
                if (rec.input?.callRef === input.callRef) {
                  taskId = String(rec.id);
                  break;
                }
              }
              cursor = page.next;
            } while (cursor !== undefined && taskId === undefined);
            const reused = taskId !== undefined;
            if (taskId === undefined) {
              // 新付费任务：intent 先于 createTask；intent 失败则不创建任务
              if (input.ledger) {
                await writeIntent(input.ledger, input.callRef);
              }
              try {
                taskId = String(
                  await api.createTask(subagentTask, input, { ownership: { kind: "conversation" } }, context),
                );
              } catch (err) {
                // createTask 失败：付费请求未发出，记 failed
                if (input.ledger) await writeOutcome(input.ledger, input.callRef, "failed");
                throw err;
              }
            }
            let settled = await api.waitForTask(taskId as never, context);
            // 防御：waitForTask 在竞态下可能返回 running 快照（resume 场景），循环等 terminal
            for (let i = 0; i < 100 && (settled as { state?: { status?: string } }).state?.status !== "terminal"; i++) {
              await new Promise((r) => setTimeout(r, 50));
              settled = await api.waitForTask(taskId as never, context);
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
              const currentStatus = await getLedgerStatus(input.callRef, input.ledger.dbPath);
              if (outcome?.status === "completed" && outcome.result) {
                const usageBasis = usageOf(outcome.result);
                if (currentStatus === "intent") {
                  await writeOutcome(input.ledger, input.callRef, "success", usageBasis);
                } else if (currentStatus === "unknown") {
                  await reconcileLedger(input.callRef, usageBasis, input.ledger.dbPath);
                }
                // 已 success/failed 不改写
              } else if (!reused) {
                if (outcome?.status === "failed" || outcome?.status === "aborted") {
                  if (currentStatus === "intent") await writeOutcome(input.ledger, input.callRef, "failed");
                } else if (currentStatus === "intent") {
                  await writeOutcome(input.ledger, input.callRef, "unknown");
                }
              }
            }
            if (!outcome || outcome.status !== "completed" || !outcome.result) {
              throw new Error(`子任务未成功完成: ${outcome?.status ?? "unknown"}`);
            }
            return { taskId: String(taskId), result: outcome.result as unknown as JsonValue, reused };
          },
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

export function installTools(registry: Registry, specs: readonly ToolSpec[]): void {
  registry.install(defineExtension({ name: "runner-tools", tools: toDurableTools(specs) }));
}
