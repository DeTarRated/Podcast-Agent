// 执行层·工具中立契约（相当于「database/sql」：业务只依赖这里，不依赖具体框架）
//
// 语义约定（换框架时新实现必须满足，验收用例见 docs/verification/flow-test.md）：
// - replay: "safe" 崩溃后自动重跑；省略 = unsafe，绝不自动重跑，模型收到 interrupted
// - execute 抛错 → 框架把错误结果交给模型
// - extras.memo：同名第一次写赢（防审批重复打扰）；自研框架不实现时可为 undefined
// - extras.dispatch（可选）：派发子代理任务并等待结果；返回框架的 TaskId 与结果。由适配层注入

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

export interface DispatchResult {
  /** 框架任务 ID（父子关联句柄） */
  taskId: string;
  /** 子任务结果（角色约定的 JSON） */
  result: JsonValue;
  /** 是否复用了同 callRef 的既有任务（去重命中） */
  reused: boolean;
}

export interface ToolExtras {
  /** 同名第一次写赢；返回已提交的值。框架不提供时为 undefined。 */
  memo?: (name: string, candidate: JsonValue) => Promise<JsonValue>;
  /** 派发子代理（嵌套任务）；同 callRef 去重。框架不提供时为 undefined。 */
  dispatch?: (input: {
    role: "translate" | "review" | "research";
    inputPath: string;
    outputPath: string;
    callRef: string;
    /** 账本元数据：Episode id、provider、model；缺省则不写账本（实验/测试场景） */
    ledger?: {
      episodeId: number;
      purpose: "translate" | "review" | "research";
      provider: string;
      model: string;
      dbPath?: string;
    };
  }) => Promise<DispatchResult>;
}

export interface ToolResult {
  /** 给模型看的文本 */
  text: string;
  /** 标记为错误结果（模型据此决定后续） */
  isError?: boolean;
}

export interface ToolSpec {
  name: string;
  description: string;
  /** JSON Schema，参数校验由框架执行 */
  parameters: object;
  /** 省略 = unsafe（付费调用绝不自动重跑） */
  replay?: "safe" | "unsafe";
  execute(args: unknown, extras?: ToolExtras): Promise<ToolResult>;
}
