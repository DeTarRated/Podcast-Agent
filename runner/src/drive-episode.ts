// 单期自动执行入口：node --experimental-strip-types src/drive-episode.ts <episode_id>
// 真实转写与模型调用均为 unsafe；样本或批量历史不得用本入口无控制触发。
// 人工直接启动同样竞争共享运行锁；由队列启动时经 PODCAST_RUN_LOCK_TOKEN 识别父进程持锁。
import { spawn } from "node:child_process";

import { resolve } from "node:path";

const episodeId = Number(process.argv[2]);
if (!Number.isInteger(episodeId) || episodeId <= 0) throw new Error("用法: drive-episode.ts <episode_id>");

const REPO_ROOT = resolve(import.meta.dirname, "../..");
// 与所有驱动入口一致：任何锁/暂停判断之前完成统一 .env 加载（格式错误失败关闭）。
const { loadRepoEnv } = await import("./env.ts");
loadRepoEnv();
const env = process.env;

const { ensureRunLock } = await import("./run-lock.ts");
const { assertBusinessRunning } = await import("./pause.ts");
const runLock = await ensureRunLock(spawn);
if (runLock === null) throw new Error("共享运行锁被其他调度入口持有，拒绝启动真实模型调用");
// 阶段级暂停检查：持锁后、打开 Runner 之前。
assertBusinessRunning();

const [{ builtinModels }, { BACKGROUND_CONTEXT }, { Runner }, { episodeTools, episodePrompt }, { runCommand }] =
  await Promise.all([
    import("@earendil-works/pi-ai/providers/all"),
    import("@earendil-works/chord/context"),
    import("./runner.ts"),
    import("./episode-agent.ts"),
    import("./bash.ts"),
  ]);
const statusResult = await runCommand(
  "uv", ["run", "python", "-m", "pipeline.episode_flow", "status", String(episodeId)],
  { cwd: REPO_ROOT, timeoutMs: 30_000 },
);
if (statusResult.exitCode !== 0) throw new Error("Episode 状态读取失败");
const status = JSON.parse(statusResult.stdout.trim().split("\n").at(-1) ?? "{}");
const workspace = resolve(REPO_ROOT, "var/episodes", String(episodeId));
const [provider, modelId] = (process.env.PODCAST_MODEL_REVIEW ?? "deepseek/deepseek-v4-pro").split("/");
const runner = await Runner.open(BACKGROUND_CONTEXT, {
  dbPath: resolve(workspace, "runner-auto.sqlite"),
  models: builtinModels(),
  model: { provider, modelId },
  budget: { episodeId, dbPath: process.env.PODCAST_AGENT_DB },
  tools: episodeTools(episodeId, { dbPath: process.env.PODCAST_AGENT_DB }),
});
try {
  const outcome = await runner.submit(BACKGROUND_CONTEXT, episodePrompt({
    episodeId,
    guid: status.episode.guid,
    title: status.episode.title,
    status: status.episode.status,
  }));
  console.log(JSON.stringify(outcome));
  if (outcome.status !== "done") {
    throw new Error(`单期 Agent 未完成: ${outcome.status}:${outcome.reason ?? "unknown"}`);
  }
  const finalResult = await runCommand(
    "uv", ["run", "python", "-m", "pipeline.episode_flow", "status", String(episodeId)],
    { cwd: REPO_ROOT, timeoutMs: 30_000 },
  );
  const finalStatus = JSON.parse(finalResult.stdout.trim().split("\n").at(-1) ?? "{}");
  if (finalResult.exitCode !== 0 || finalStatus.episode?.status !== "reviewing") {
    throw new Error(`单期执行未到达 reviewing: ${finalStatus.episode?.status ?? "unknown"}`);
  }
} finally {
  await runner.close(BACKGROUND_CONTEXT);
  runLock.release();
}
