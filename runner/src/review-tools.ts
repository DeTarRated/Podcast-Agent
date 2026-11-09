// 执行层·审核重跑与看点派发工具：从确定性 JSON 描述调用 dispatch，付费调用 unsafe。
import { readFileSync } from "node:fs";

import type { ToolSpec } from "./tool-spec.ts";
import type { SubagentInput } from "./experiment-subagent.ts";

function modelFor(role: string): { provider: string; modelId: string } {
  const route = role === "translate"
    ? process.env.PODCAST_MODEL_TRANSLATE ?? "deepseek/deepseek-flash"
    : role === "research"
      ? process.env.PODCAST_MODEL_RESEARCH ?? "deepseek/deepseek-flash"
      : process.env.PODCAST_MODEL_REVIEW ?? "deepseek/deepseek-v4-pro";
  const separator = route.indexOf("/");
  if (separator <= 0) throw new Error(`模型路由无效: ${route}`);
  return { provider: route.slice(0, separator), modelId: route.slice(separator + 1) };
}

export function readDispatchFile(path: string): SubagentInput {
  const value = JSON.parse(readFileSync(path, "utf8")) as { dispatch?: SubagentInput };
  const input = value.dispatch;
  if (!input || !["translate", "review", "research", "highlights"].includes(input.role)) {
    throw new Error("dispatch 文件缺少有效 role");
  }
  if (!input.inputPath || !input.outputPath || !input.callRef) {
    throw new Error("dispatch 文件缺少 inputPath/outputPath/callRef");
  }
  const model = input.model ?? modelFor(input.role);
  return {
    ...input,
    model,
    ledger: input.ledger
      ? { ...input.ledger, provider: model.provider, model: model.modelId }
      : undefined,
  };
}

export function reviewTools(): ToolSpec[] {
  return [
    {
      name: "review_dispatch",
      description: "按 rerun/highlights JSON 描述派发子代理；付费 unsafe，失败不自动重派",
      parameters: {
        type: "object",
        properties: { dispatchFile: { type: "string", description: "含 dispatch 字段的 JSON 文件" } },
        required: ["dispatchFile"],
      },
      // 省略 replay = unsafe：付费派发绝不因框架恢复自动重放。
      async execute(args, extras) {
        if (!extras?.dispatch) throw new Error("dispatch extras 不可用");
        const { dispatchFile } = args as { dispatchFile: string };
        const input = readDispatchFile(dispatchFile);
        const result = await extras.dispatch(input);
        return {
          text: `${input.role} 完成 → ${input.outputPath}（taskId=${result.taskId}，reused=${result.reused}）`,
        };
      },
    },
  ];
}
