// 执行层·Runner 薄封装：隔离 Pi Durable experimental API（submit / resume / checkpoint）
import type { Context } from "@earendil-works/chord";
import {
  createRegistry,
  Harness,
  LiveDoc,
  type Conversation,
  type Harness as HarnessInstance,
  type HarnessSettings,
  type LiveState,
  type ModelRef,
  type SettledSubmissionRecord,
} from "@earendil-works/pi-durable";
import { openNodeSqliteStorage } from "@earendil-works/pi-durable/storage/sqlite/node";
import type { Models } from "@earendil-works/pi-ai";

import { installTools } from "./durable-adapter.ts";
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
    const storage = await openNodeSqliteStorage(options.dbPath);
    const registry = createRegistry();
    if (options.tools && options.tools.length > 0) {
      installTools(registry, options.tools);
    }
    const harness = await Harness.open(
      storage,
      { models: options.models, registry, settings: options.settings },
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
