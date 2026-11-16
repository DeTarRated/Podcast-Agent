// 执行层·Runner 薄封装：隔离 Pi Durable experimental API（submit / resume / checkpoint）
import type { Context } from "@earendil-works/chord";
import { createRegistry, Harness, LiveDoc, defineExtension, type Conversation, type Harness as HarnessInstance, type HarnessSettings, type LiveState, type ModelRef, type SettledSubmissionRecord } from "@earendil-works/pi-durable";
import { openNodeSqliteStorage } from "@earendil-works/pi-durable/storage/sqlite/node";
import type { Models } from "@earendil-works/pi-ai";

import { statSync } from "node:fs";

import { installTools } from "./durable-adapter.ts";
import { budgetModels, estimateModelMaxCost, type ModelBudgetScope } from "./model-budget.ts";
import type { SubagentInput } from "./experiment-subagent.ts";
import { defineSubagentTask } from "./experiment-subagent.ts";
import type { ToolSpec } from "./tool-spec.ts";

export interface RunnerOpenOptions {
  /** SQLite 存储文件（单期一个 conversation，文件随单期走） */
  dbPath: string;
  /** pi-ai 模型目录 */
  models: Models;
  /** 主代理模型 */
  model: ModelRef;
  /** 工具（中立 ToolSpec 契约，不依赖框架类型） */
  tools?: readonly ToolSpec[];
  /** 运行设置（如 restartLimit 熔断阈值） */
  settings?: Partial<HarnessSettings>;
  /** 付费模型请求的Episode费用归属；真实provider必填，fake/mock tests可省略。 */
  budget?: ModelBudgetScope;
}

export function budgetSafeHarnessSettings(settings: Partial<HarnessSettings> = {}): Partial<HarnessSettings> {
  return {
    ...settings,
    retry: { ...settings.retry, enabled: false, maxRetries: 0 },
    stream: { ...settings.stream, maxRetries: 0, maxRetryDelayMs: 0 },
    // Durable compaction regenerates timestamps after resume. Until its task ID is part of the paid call key,
    // keep automatic summarization off; manual compaction remains available and uses timestamp-stable callRefs.
    compaction: { ...settings.compaction, enabled: false, modelOverrides: {} },
  };
}

export interface SubmitOutcome {
  id: string;
  status: string;
  type: string;
  answer?: string;
  reason?: string;
}

export interface Checkpoint {
  rootId: string;
  live: LiveState | undefined;
}

export class Runner {
  readonly harness: HarnessInstance;
  readonly root: Conversation;
  readonly dbPath: string;

  private constructor(harness: HarnessInstance, root: Conversation, dbPath: string) {
    this.harness = harness;
    this.root = root;
    this.dbPath = dbPath;
  }

  /** 打开（或重开）存储；root conversation 不存在则创建。 */
  static async open(ctx: Context, options: RunnerOpenOptions): Promise<Runner> {
    const hasRealProvider = options.models.getProviders().some((provider) => provider.id !== "mock");
    if (hasRealProvider && !options.budget) {
      throw new Error("真实模型 Runner 必须配置费用预算归属");
    }
    const runtimeModels = options.budget ? budgetModels(options.models, options.budget) : options.models;
    const settings: Partial<HarnessSettings> = options.budget
      ? budgetSafeHarnessSettings(options.settings)
      : options.settings ?? {};
    const storage = await openNodeSqliteStorage(options.dbPath);
    const registry = createRegistry();
    if (options.tools && options.tools.length > 0) {
      const estimateSubagent = options.budget ? (input: SubagentInput) => {
        if (!input.model || !input.ledger || input.ledger.episodeId !== options.budget!.episodeId ||
            input.ledger.dbPath !== options.budget!.dbPath || input.ledger.provider !== input.model.provider ||
            input.ledger.model !== input.model.modelId || input.ledger.purpose !== input.role) {
          throw new Error("子代理预算元数据与Runner费用归属不匹配");
        }
        const model = runtimeModels.getModel(input.model.provider, input.model.modelId);
        if (!model) throw new Error(`子代理模型未在Runner目录中配置: ${input.model.provider}/${input.model.modelId}`);
        // 子代理提示由固定上下文文件组成：按真实文件规模估算输入 token，
        // 与配置下限取较大者，避免「预留恒小于实际」。
        const files = [...(input.contextFiles ?? []), ...(input.referenceFiles ?? [])];
        let bytes = 0;
        for (const file of files) {
          try {
            bytes += statSync(file).size;
          } catch {
            throw new Error(`子代理上下文文件不可读，无法估算费用: ${file}`);
          }
        }
        // 字节→token 估算留 30% 余量：tokenizer 对中文/代码的估算可能高于 chars/3.5。
        const promptTokens = bytes > 0 ? Math.ceil((bytes / 3.5) * 1.3) : undefined;
        return estimateModelMaxCost(
          model, new Date(),
          promptTokens === undefined ? undefined : { inputTokens: promptTokens },
          "subagent",
        );
      } : undefined;
      installTools(registry, options.tools, estimateSubagent);
    }
    // 子代理任务定义注册（嵌套任务形态；由 dispatch extras 创建）
    registry.install(defineExtension({ name: "experiment-tasks", tasks: [defineSubagentTask()] }));
    const harness = await Harness.open(
      storage,
      { models: runtimeModels, registry, settings },
      ctx,
    );
    const root = await harness.root(ctx, { agent: { model: options.model } });
    return new Runner(harness, root, options.dbPath);
  }

  /** 继续任何上次进程留下的未完成运行。幂等。 */
  resume(): void {
    this.harness.resume();
  }

  /** 提交一条输入并等待 settle（done / unanswered）。 */
  async submit(ctx: Context, content: string): Promise<SubmitOutcome> {
    this.resume();
    const submission = await this.root.submit({ type: "input", content }, ctx);
    const settled = await submission.wait(ctx);
    return this.#outcome(settled);
  }

  /** 让一次未完成运行继续（不追加新消息）。 */
  async continue(ctx: Context): Promise<SubmitOutcome> {
    this.resume();
    const submission = await this.root.submit({ type: "continue" }, ctx);
    const settled = await submission.wait(ctx);
    return this.#outcome(settled);
  }

  /** 等待对话普通作用域空闲（无在跑任务）。 */
  async waitForIdle(ctx: Context): Promise<void> {
    await this.root.waitForIdle(ctx);
  }

  #outcome(settled: SettledSubmissionRecord): SubmitOutcome {
    return {
      id: String(settled.id),
      status: settled.status,
      type: settled.type,
      answer: "answer" in settled && settled.answer !== undefined ? String(settled.answer) : undefined,
      reason: "reason" in settled && settled.reason !== undefined ? String(settled.reason) : undefined,
    };
  }

  /** 当前对话状态快照（run / generation / 失败原因）。 */
  async checkpoint(ctx: Context): Promise<Checkpoint> {
    const live = await this.harness.snapshot(LiveDoc, this.root.id, ctx);
    return { rootId: String(this.root.id), live };
  }

  async close(ctx: Context): Promise<void> {
    await this.harness.close(ctx);
  }
}
