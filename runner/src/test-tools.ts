// 流程测试专用工具：可控的慢工具/计数器/memo 探针（仅供实验与测试，不进流水线）
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { defineTool, type ToolRegistration } from "@earendil-works/pi-durable";

export interface TestToolEnv {
  markerDir: string;
}

let slowMs = 60_000;

/** 设置慢工具睡眠时长（父进程重开后设短值让重跑快速完成）。 */
export function setSlowMs(ms: number): void {
  slowMs = ms;
}

const sleepMs = () => slowMs;

/** 执行计数：写在 markerDir/exec-count.txt。 */
function bumpCount(env: TestToolEnv): number {
  const f = join(env.markerDir, "exec-count.txt");
  const n = existsSync(f) ? Number(readFileSync(f, "utf8")) : 0;
  writeFileSync(f, String(n + 1));
  return n + 1;
}

/** safe 慢工具：写 tool-started 标记后按 SLOW_MS 环境变量睡眠（kill -9 实验用）。 */
export function slowSafeTool(env: TestToolEnv): ToolRegistration {
  return defineTool({
    name: "test.slow_safe",
    description: "实验用：重跑安全慢工具",
    parameters: { type: "object", properties: {} },
    replay: "safe",
    async execute() {
      const n = bumpCount(env);
      writeFileSync(join(env.markerDir, "tool-started"), String(Date.now()));
      await new Promise((r) => setTimeout(r, sleepMs()));
      return { output: [{ type: "text", text: `slow_safe done (exec #${n})` }] };
    },
  });
}

/** unsafe 慢工具：同上，但省略 replay = unsafe（付费调用语义的实验替身）。 */
export function slowUnsafeTool(env: TestToolEnv): ToolRegistration {
  return defineTool({
    name: "test.slow_unsafe",
    description: "实验用：绝不自动重跑的慢工具",
    parameters: { type: "object", properties: {} },
    async execute() {
      const n = bumpCount(env);
      writeFileSync(join(env.markerDir, "tool-started"), String(Date.now()));
      await new Promise((r) => setTimeout(r, sleepMs()));
      return { output: [{ type: "text", text: `slow_unsafe done (exec #${n})` }] };
    },
  });
}

/** memo 第一次写赢探针：两次 api.memo 写同名，结果写入 memo-result.json。 */
export function memoProbeTool(env: TestToolEnv): ToolRegistration {
  return defineTool({
    name: "test.memo_probe",
    description: "实验用：memo 第一次写赢探针",
    parameters: {
      type: "object",
      properties: { name: { type: "string" } },
      required: ["name"],
    },
    replay: "safe",
    async execute(args, api, context) {
      const { name } = args as { name: string };
      const first = await api.memo(name, "first", context);
      const second = await api.memo(name, "second", context);
      writeFileSync(
        join(env.markerDir, "memo-result.json"),
        JSON.stringify({ name, first, second }),
      );
      return { output: [{ type: "text", text: `memo(${name}): first=${first} second=${second}` }] };
    },
  });
}
