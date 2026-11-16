// 真实 Runner 驱动：加载环境后按 JSON dispatch 文件派发；调用方负责提供明确文件。
// 人工直接启动同样竞争共享运行锁；由队列启动时经 PODCAST_RUN_LOCK_TOKEN 识别父进程持锁。
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { runCommand } from "./bash.ts";
import { loadRepoEnv } from "./env.ts";
import { assertBusinessRunning } from "./pause.ts";
import { ensureRunLock } from "./run-lock.ts";
import { readDispatchFile } from "./review-tools.ts";

export async function driveDispatchFile(dispatchFile: string): Promise<void> {
  const absolute = resolve(dispatchFile);
  const dispatchInput = readDispatchFile(absolute);
  if (!dispatchInput.ledger) throw new Error("dispatch 文件缺少费用账本归属");
  // 与所有驱动入口一致：任何锁/暂停判断之前完成统一 .env 加载（格式错误失败关闭）。
  loadRepoEnv();
  const runLock = await ensureRunLock(spawn);
  if (runLock === null) throw new Error("共享运行锁被其他调度入口持有，拒绝启动真实模型调用");
  // 阶段级暂停检查：持锁后、打开 Runner 之前。
  assertBusinessRunning();
  const repoRoot = resolve(import.meta.dirname, "../..");
  const env = process.env;
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
    budget: { episodeId: dispatchInput.ledger.episodeId, dbPath: dispatchInput.ledger.dbPath },
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
    const input = dispatchInput;
    if (!existsSync(input.outputPath)) {
      throw new Error("派发结束但产物未落盘");
    }
    if (input.ledger) {
      const args = ["run", "python", "-m", "state.ledger_cli", "get", "--call-ref", input.callRef];
      if (input.ledger.dbPath) args.push("--db", input.ledger.dbPath);
      const result = await runCommand("uv", args, { cwd: repoRoot, timeoutMs: 30_000 });
      const envelope = JSON.parse(result.stdout.trim().split("\n").at(-1) ?? "{}") as {
        status?: string; data?: { status?: string };
      };
      const ledgerStatus = envelope.data?.status;
      if (result.exitCode !== 0 || envelope.status !== "success" || ledgerStatus !== "success") {
        throw new Error(`派发账本未核销: ${ledgerStatus ?? envelope.status ?? "unknown"}`);
      }
    }
  } finally {
    await runner.close(BACKGROUND_CONTEXT);
    runLock.release();
  }
}
