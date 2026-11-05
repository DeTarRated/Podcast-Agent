// 执行层·pi-durable 适配层：ToolSpec → ToolRegistration 转换
//
// 换框架时只有本文件与 runner.ts 需要重写；业务工具与 ToolSpec 契约不变。
import { defineExtension, defineTool, type Registry, type ToolRegistration } from "@earendil-works/pi-durable";

import type { JsonValue, ToolSpec } from "./tool-spec.ts";

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
