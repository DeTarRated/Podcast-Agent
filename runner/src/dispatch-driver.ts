// 真实 Runner 驱动：加载环境后按 JSON dispatch 文件派发；调用方负责提供明确文件。
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { runCommand } from "./bash.ts";
import { readDispatchFile } from "./review-tools.ts";

export async function driveDispatchFile(dispatchFile: string): Promise<void> {
  const absolute = resolve(dispatchFile);
  const repoRoot = resolve(import.meta.dirname, "../..");
  const env = Object.fromEntries(
    readFileSync(resolve(repoRoot, ".env"), "utf8")
      .split("\n")
      .filter((line) => /^[A-Z_]+=/.test(line))
      .map((line) => {
        const index = line.indexOf("=");
        return [line.slice(0, index).trim(), line.slice(index + 1).trim()] as [string, string];
      }),
  );
  for (const [key, value] of Object.entries(env)) {
    if (!(key in process.env)) process.env[key] = value;
  }
  const [{ builtinModels }, { BACKGROUND_CONTEXT }, { Runner }, { reviewTools }] = await Promise.all([
    import("@earendil-works/pi-ai/providers/all"),
    import("@earendil-works/chord/context"),
    import("./runner.ts"),
    import("./review-tools.ts"),
  ]);
  const [provider, modelId] = (process.env.PODCAST_MODEL_REVIEW ?? "deepseek/deepseek-v4-pro").split("/");
  const runner = await Runner.open(BACKGROUND_CONTEXT, {
    dbPath: resolve(dirname(absolute), "runner-dispatch.sqlite"),
    models: builtinModels(),
    model: { provider, modelId },
    tools: reviewTools(),
  });
  try {
    const outcome = await runner.submit(
      BACKGROUND_CONTEXT,
      `请调用 review_dispatch 工具，参数 dispatchFile=${absolute}。只执行该派发，不重试失败任务。`,
    );
    console.log(JSON.stringify(outcome));
    if (outcome.status !== "done") {
      throw new Error(`派发未完成: ${outcome.status}:${outcome.reason ?? "unknown"}`);
    }
    const input = readDispatchFile(absolute);
    if (!existsSync(input.outputPath)) {
      throw new Error("派发结束但产物未落盘");
    }
    if (input.ledger) {
      const args = ["run", "python", "-m", "state.ledger_cli", "get", "--call-ref", input.callRef];
      if (input.ledger.dbPath) args.push("--db", input.ledger.dbPath);
      const result = await runCommand("uv", args, { cwd: repoRoot, timeoutMs: 30_000 });
      const ledger = JSON.parse(result.stdout.trim().split("\n").at(-1) ?? "{}");
      if (result.exitCode !== 0 || ledger.status !== "success") {
        throw new Error(`派发账本未核销: ${ledger.status ?? "unknown"}`);
      }
    }
  } finally {
    await runner.close(BACKGROUND_CONTEXT);
  }
}
