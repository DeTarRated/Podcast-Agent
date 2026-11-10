// 单期自动 Agent 工具测试；命令与 dispatch 均 fake，零真实下载、转写或模型调用。
//
// 模拟场景：
// - 工具名兼容 DeepSeek，download/wordrender/orchestrate 为 safe，transcribe/dispatch 为 unsafe
// - 工作区读取、上下文原子写、en_fix 唯一定点编辑与重试幂等
// - 命令行参数映射和 Episode 固定归属
// - dispatch 工作区／只读白名单、模型路由、账本元数据与越界拒绝
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { episodeTools } from "./episode-agent.ts";
import type { RunResult } from "./bash.ts";
import type { ToolSpec } from "./tool-spec.ts";

const REPO_ROOT = resolve(import.meta.dirname, "../..");

function tool(tools: ToolSpec[], name: string): ToolSpec {
  const found = tools.find((item) => item.name === name);
  assert.ok(found, `缺少工具 ${name}`);
  return found;
}

test("工具名合法且付费调用保持 unsafe", () => {
  const tools = episodeTools(1);
  for (const item of tools) assert.match(item.name, /^[a-zA-Z0-9_-]+$/);
  assert.equal(tool(tools, "episode_download").replay, "safe");
  assert.equal(tool(tools, "pipeline_wordrender").replay, "safe");
  assert.equal(tool(tools, "pipeline_orchestrate").replay, "safe");
  assert.equal(tool(tools, "pipeline_transcribe").replay, undefined);
  assert.equal(tool(tools, "episode_dispatch").replay, undefined);
});

test("工作区读写、路径保护与定点编辑幂等", async () => {
  const root = mkdtempSync(join(tmpdir(), "episode-tools-"));
  const workspace = join(root, "1");
  mkdirSync(workspace, { recursive: true });
  writeFileSync(join(workspace, "en_fix.srt"), "Hello world.\n");
  const tools = episodeTools(1, { workspaceRoot: root });

  const read = await tool(tools, "episode_read").execute({ path: "en_fix.srt" });
  assert.ok(read.text.includes("Hello world"));
  await assert.rejects(
    tool(tools, "episode_read").execute({ path: "../outside.txt" }),
    /超出当前 Episode/,
  );

  const write = tool(tools, "episode_write_context");
  assert.equal(JSON.parse((await write.execute({ path: "_context/brief.md", content: "简报" })).text).reused, false);
  assert.equal(JSON.parse((await write.execute({ path: "_context/brief.md", content: "简报" })).text).reused, true);
  assert.equal(readFileSync(join(workspace, "_context/brief.md"), "utf8"), "简报");
  await assert.rejects(write.execute({ path: "en.srt", content: "不允许" }), /只允许/);

  const edit = tool(tools, "episode_edit_transcript");
  assert.equal(JSON.parse((await edit.execute({ oldText: "world", newText: "Chelsea" })).text).reused, false);
  assert.equal(readFileSync(join(workspace, "en_fix.srt"), "utf8"), "Hello Chelsea.\n");
  assert.equal(JSON.parse((await edit.execute({ oldText: "world", newText: "Chelsea" })).text).reused, true);
  rmSync(root, { recursive: true, force: true });
});

test("Episode 下载与 orchestrate 命令参数固定归属", async () => {
  const calls: string[][] = [];
  const commandRunner = async (_command: string, args: string[]): Promise<RunResult> => {
    calls.push(args);
    return { exitCode: 0, stdout: '{"status":"success"}\n', stderr: "" };
  };
  const tools = episodeTools(7, { commandRunner });
  await tool(tools, "episode_download").execute({});
  await tool(tools, "pipeline_orchestrate").execute({
    command: "fix", maps: ["S01=Anonymous"], manualDone: true, allowUnmatched: true,
  });
  assert.ok(calls[0].includes("pipeline.episode_flow"));
  assert.ok(calls[0].includes("7"));
  assert.ok(calls[1].includes("pipeline.orchestrate"));
  assert.ok(calls[1].includes("--map"));
  assert.ok(calls[1].includes("S01=Anonymous"));
  assert.ok(calls[1].includes("--manual-done"));
});

test("dispatch 设置账本与路由并拒绝工作区外输出", async () => {
  const root = mkdtempSync(join(tmpdir(), "episode-dispatch-"));
  const workspace = join(root, "3");
  mkdirSync(workspace, { recursive: true });
  writeFileSync(join(workspace, "en_fix.srt"), "input");
  const tools = episodeTools(3, { workspaceRoot: root, dbPath: "/tmp/test-state.db" });
  const dispatchTool = tool(tools, "episode_dispatch");
  let captured: any;
  const extras = {
    dispatch: async (input: any) => {
      captured = input;
      return { taskId: "task", result: {}, reused: false };
    },
  };
  const result = await dispatchTool.execute({
    role: "translate",
    inputPath: "en_fix.srt",
    outputPath: "en_zh.txt",
    callRef: "translate:3:v1",
    contextFiles: [resolve(REPO_ROOT, "skills/translate-srt/references/subtitle-rules.md")],
  }, extras as any);
  assert.ok(result.text.includes("task"));
  assert.equal(captured.ledger.episodeId, 3);
  assert.equal(captured.ledger.purpose, "translate");
  assert.equal(captured.ledger.dbPath, "/tmp/test-state.db");
  assert.ok(captured.inputPath.startsWith(workspace));
  await assert.rejects(
    dispatchTool.execute({
      role: "translate", inputPath: "en_fix.srt", outputPath: "/tmp/outside.txt", callRef: "translate:3:v1",
    }, extras as any),
    /超出当前 Episode/,
  );
  rmSync(root, { recursive: true, force: true });
});
