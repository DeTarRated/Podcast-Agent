// 崩溃注入子进程：被父测试 kill -9 的实验进程
//
// usage: node crash-child.ts <dbPath> <scenario> <markerDir>
// scenarios: unsafe-tool | safe-tool | model-midflight | parked-start | parked-resume
import { createModels, fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } from "@earendil-works/pi-ai";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";

import { Runner } from "./runner.ts";
import { setSlowMs, slowSafeTool, slowUnsafeTool } from "./test-tools.ts";

const [dbPath, scenario, markerDir] = process.argv.slice(2);
setSlowMs(Number(process.env.SLOW_MS ?? 60_000));

const MODEL = { provider: "mock", modelId: "mock-main" };
const faux = fauxProvider({
  provider: "mock",
  models: [{ id: "mock-main" }],
  tokensPerSecond: 5, // 模型在途场景需要慢速流式响应
});
const models = createModels();
models.setProvider(faux.provider);

async function main(): Promise<void> {
  if (scenario === "model-midflight") {
    faux.setResponses([fauxAssistantMessage("x".repeat(4000))]);
    const runner = await Runner.open(BACKGROUND_CONTEXT, { dbPath, models, model: MODEL });
    void runner.submit(BACKGROUND_CONTEXT, "慢速回答").then(() => console.log("SETTLED"));
    console.log("SUBMITTED");
    return;
  }

  const useUnsafe = scenario === "unsafe-tool";
  const parked = scenario === "parked-start" || scenario === "parked-resume";
  const tool = useUnsafe ? slowUnsafeTool({ markerDir }) : slowSafeTool({ markerDir });

  faux.setResponses([
    fauxAssistantMessage([fauxToolCall(tool.name, {})], { stopReason: "toolUse" }),
    fauxAssistantMessage([fauxText("final-after-tool")]),
  ]);
  const runner = await Runner.open(BACKGROUND_CONTEXT, {
    dbPath,
    models,
    model: MODEL,
    tools: [tool],
    settings: parked ? { restartLimit: 2 } : undefined,
  });
  runner.resume();
  if (scenario === "parked-resume") {
    // 只需续跑（safe 工具自动重跑并写标记），进程保持存活直到被杀
    return;
  }
  void runner.submit(BACKGROUND_CONTEXT, "开始流水线").then(() => console.log("SETTLED"));
  console.log("SUBMITTED");
}

void main();
// 保持进程存活，直到父测试 kill -9
await new Promise(() => {});
