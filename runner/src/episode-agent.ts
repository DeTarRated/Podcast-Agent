// 执行层·单期自动 Agent 工具：下载、转写、重组、orchestrate、受控文件编辑与 dispatch。
//
// 工具名只用字母/数字/下划线；转写和模型 dispatch 省略 replay = unsafe。
// 文件路径限定在该 Episode 工作区；审核、上传与交付不在本工具集中。
import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { dirname, resolve, sep } from "node:path";
import { homedir } from "node:os";

import { runCommand, type RunResult } from "./bash.ts";
import type { ToolSpec } from "./tool-spec.ts";
import type { SubagentInput } from "./experiment-subagent.ts";

const REPO_ROOT = resolve(import.meta.dirname, "../..");

export interface EpisodeToolOptions {
  dbPath?: string;
  workspaceRoot?: string;
  commandRunner?: typeof runCommand;
}

function modelRoute(role: string): { provider: string; modelId: string } {
  const variable = role === "translate" || role === "research"
    ? role === "translate" ? "PODCAST_MODEL_TRANSLATE" : "PODCAST_MODEL_RESEARCH"
    : "PODCAST_MODEL_REVIEW";
  const fallback = variable === "PODCAST_MODEL_REVIEW" ? "deepseek/deepseek-v4-pro" : "deepseek/deepseek-flash";
  const route = process.env[variable] ?? fallback;
  const separator = route.indexOf("/");
  if (separator <= 0) throw new Error(`模型路由无效: ${route}`);
  return { provider: route.slice(0, separator), modelId: route.slice(separator + 1) };
}

function atomicWrite(path: string, content: string): void {
  mkdirSync(dirname(path), { recursive: true });
  const temporary = `${path}.tmp-${process.pid}-${Date.now()}`;
  writeFileSync(temporary, content, "utf8");
  renameSync(temporary, path);
}

export function episodeTools(episodeId: number, options: EpisodeToolOptions = {}): ToolSpec[] {
  if (!Number.isInteger(episodeId) || episodeId <= 0) throw new Error("episodeId 必须为正整数");
  const workspaceRoot = options.workspaceRoot ?? resolve(REPO_ROOT, "var/episodes");
  const workspace = resolve(workspaceRoot, String(episodeId));
  const runner = options.commandRunner ?? runCommand;
  const audio = resolve(workspace, "audio.mp3");

  async function commandJson(command: string, args: string[], timeoutMs = 120_000): Promise<unknown> {
    const result: RunResult = await runner(command, args, { cwd: REPO_ROOT, timeoutMs });
    const lastLine = result.stdout.trim().split("\n").at(-1) ?? "{}";
    let payload: unknown;
    try {
      payload = JSON.parse(lastLine);
    } catch {
      throw new Error("本地命令未返回 JSON");
    }
    if (result.exitCode !== 0) {
      const message = (payload as { error?: string }).error ?? `退出码 ${result.exitCode}`;
      throw new Error(message);
    }
    return payload;
  }

  function flow(command: "download" | "status"): Promise<unknown> {
    const args = ["run", "python", "-m", "pipeline.episode_flow", command, String(episodeId),
      "--workspace", workspace];
    if (options.dbPath) args.push("--db", options.dbPath);
    return commandJson("uv", args, command === "download" ? 600_000 : 120_000);
  }

  function resolveWorkspace(path: string): string {
    if (!path || path.includes("\0")) throw new Error("非法文件路径");
    const absolute = path.startsWith("/") ? resolve(path) : resolve(workspace, path);
    if (absolute !== workspace && !absolute.startsWith(workspace + sep)) {
      throw new Error("路径超出当前 Episode 工作区");
    }
    return absolute;
  }

  function resolveReadOnly(path: string): string {
    const absolute = path.startsWith("/") ? resolve(path) : resolve(workspace, path);
    if (absolute === workspace || absolute.startsWith(workspace + sep)) return absolute;
    const skillRoot = resolve(REPO_ROOT, "skills/translate-srt");
    const knowledgeRoot = resolve(process.env.TRANSLATE_SRT_HOME ?? resolve(homedir(), ".translate-srt"));
    if (absolute.startsWith(skillRoot + sep) || absolute.startsWith(knowledgeRoot + sep)) return absolute;
    throw new Error("只读参考文件超出工作区、skill 或知识库白名单");
  }

  return [
    {
      name: "episode_download",
      description: "下载并固化当前 Episode 音频，登记 hash/artifact，推进 downloaded；可安全重跑",
      parameters: { type: "object", properties: {}, required: [] },
      replay: "safe",
      async execute() { return { text: JSON.stringify(await flow("download")) }; },
    },
    {
      name: "episode_status",
      description: "查询当前 Episode 状态与已登记产物",
      parameters: { type: "object", properties: {}, required: [] },
      replay: "safe",
      async execute() { return { text: JSON.stringify(await flow("status")) }; },
    },
    {
      name: "pipeline_transcribe",
      description: "Scribe 付费转写当前固化音频；unsafe，unknown/failed 不得自动重试",
      parameters: { type: "object", properties: {}, required: [] },
      async execute() {
        const args = ["run", "python", "-m", "pipeline.scribe", "transcribe", String(episodeId), audio,
          "--workspace", workspace];
        if (options.dbPath) args.push("--db", options.dbPath);
        return { text: JSON.stringify(await commandJson("uv", args, 900_000)) };
      },
    },
    {
      name: "pipeline_wordrender",
      description: "把 Scribe 词级 JSON 确定性重组为 en.srt 与 provenance；create-only",
      parameters: { type: "object", properties: {}, required: [] },
      replay: "safe",
      async execute() {
        const args = ["run", "python", "-m", "pipeline.wordrender",
          resolve(workspace, "scribe_raw.json"), resolve(workspace, "en.srt"),
          "--provenance", resolve(workspace, "en.provenance.json")];
        return { text: JSON.stringify(await commandJson("uv", args)) };
      },
    },
    {
      name: "pipeline_orchestrate",
      description: "执行 orchestrate 的 prepare/fix/apply/finalize 本地确定性阶段",
      parameters: {
        type: "object",
        properties: {
          command: { type: "string", enum: ["prepare", "fix", "apply", "finalize"] },
          videoDate: { type: "string" },
          domains: { type: "array", items: { type: "string" } },
          maps: { type: "array", items: { type: "string" } },
          drop: { type: "boolean" },
          manualDone: { type: "boolean" },
          allowUnmatched: { type: "boolean" },
        },
        required: ["command"],
      },
      replay: "safe",
      async execute(args) {
        const input = args as Record<string, any>;
        const cli = ["run", "python", "-m", "pipeline.orchestrate", input.command, String(episodeId),
          "--workspace", workspace];
        if (options.dbPath) cli.push("--db", options.dbPath);
        if (input.videoDate) cli.push("--video-date", input.videoDate);
        for (const domain of input.domains ?? []) cli.push("--domain", domain);
        for (const map of input.maps ?? []) cli.push("--map", map);
        if (input.drop) cli.push("--drop");
        if (input.manualDone) cli.push("--manual-done");
        if (input.allowUnmatched) cli.push("--allow-unmatched");
        return { text: JSON.stringify(await commandJson("uv", cli, 300_000)) };
      },
    },
    {
      name: "episode_read",
      description: "读取当前工作区文本；path 可分段读取，禁止越界",
      parameters: {
        type: "object",
        properties: {
          path: { type: "string" },
          offset: { type: "integer", minimum: 0 },
          maxChars: { type: "integer", minimum: 1, maximum: 200000 },
        },
        required: ["path"],
      },
      replay: "safe",
      async execute(args) {
        const input = args as { path: string; offset?: number; maxChars?: number };
        const path = resolveWorkspace(input.path);
        const content = readFileSync(path, "utf8");
        const offset = input.offset ?? 0;
        const maxChars = input.maxChars ?? 60_000;
        return { text: JSON.stringify({ path, offset, nextOffset: Math.min(content.length, offset + maxChars),
          totalChars: content.length, content: content.slice(offset, offset + maxChars) }) };
      },
    },
    {
      name: "episode_write_context",
      description: "原子写 _context/brief.md、gaps.md 或 glossary.md；同内容重跑复用",
      parameters: {
        type: "object",
        properties: { path: { type: "string" }, content: { type: "string" } },
        required: ["path", "content"],
      },
      replay: "safe",
      async execute(args) {
        const input = args as { path: string; content: string };
        const allowed = new Set(["_context/brief.md", "_context/gaps.md", "_context/glossary.md", "_context/review_notes.md"]);
        if (!allowed.has(input.path)) throw new Error("只允许写 brief/gaps/glossary/review_notes 上下文");
        const path = resolveWorkspace(input.path);
        if (existsSync(path) && readFileSync(path, "utf8") === input.content) {
          return { text: JSON.stringify({ path, reused: true }) };
        }
        atomicWrite(path, input.content);
        return { text: JSON.stringify({ path, reused: false }) };
      },
    },
    {
      name: "episode_edit_transcript",
      description: "对 en_fix.srt 做唯一定点替换；禁止整份重写",
      parameters: {
        type: "object",
        properties: { oldText: { type: "string", minLength: 1 }, newText: { type: "string" } },
        required: ["oldText", "newText"],
      },
      replay: "safe",
      async execute(args) {
        const input = args as { oldText: string; newText: string };
        const path = resolveWorkspace("en_fix.srt");
        const content = readFileSync(path, "utf8");
        const occurrences = content.split(input.oldText).length - 1;
        if (occurrences === 0 && content.includes(input.newText)) {
          return { text: JSON.stringify({ path, reused: true }) };
        }
        if (occurrences !== 1) throw new Error(`定点替换要求唯一匹配，实际 ${occurrences}`);
        atomicWrite(path, content.replace(input.oldText, input.newText));
        return { text: JSON.stringify({ path, reused: false }) };
      },
    },
    {
      name: "episode_dispatch",
      description: "按 orchestrate 返回的 callRef 派发翻译/复核/调研；付费 unsafe，不自动重试",
      parameters: {
        type: "object",
        properties: {
          role: { type: "string", enum: ["translate", "review", "research"] },
          inputPath: { type: "string" },
          outputPath: { type: "string" },
          callRef: { type: "string", minLength: 1 },
          contextFiles: { type: "array", items: { type: "string" } },
          referenceFiles: { type: "array", items: { type: "string" } },
        },
        required: ["role", "inputPath", "outputPath", "callRef"],
      },
      async execute(args, extras) {
        if (!extras?.dispatch) throw new Error("dispatch extras 不可用");
        const input = args as {
          role: "translate" | "review" | "research";
          inputPath: string; outputPath: string; callRef: string;
          contextFiles?: string[]; referenceFiles?: string[];
        };
        const model = modelRoute(input.role);
        const dispatchInput: SubagentInput = {
          role: input.role,
          inputPath: resolveWorkspace(input.inputPath),
          outputPath: resolveWorkspace(input.outputPath),
          callRef: input.callRef,
          contextFiles: (input.contextFiles ?? []).map(resolveReadOnly),
          referenceFiles: (input.referenceFiles ?? []).map(resolveReadOnly),
          targetLang: "zh",
          model,
          ledger: {
            episodeId,
            purpose: input.role,
            provider: model.provider,
            model: model.modelId,
            dbPath: options.dbPath,
          },
        };
        const result = await extras.dispatch(dispatchInput);
        return { text: JSON.stringify(result) };
      },
    },
  ];
}

export function episodePrompt(input: { episodeId: number; guid: string; title?: string; status?: string }): string {
  const resume = input.status === "translated"
    ? "当前翻译与 en_zh.srt 已完成。严禁重复下载、转写或翻译；按当前 apply 的 dispatch 调用 episode_dispatch 完成复核，读取复核稿并写 _context/review_notes.md，然后 finalize。若已有成功复核任务，复用同 callRef，不新建付费任务。\n"
    : input.status === "transcribed"
    ? "当前转写与 scribe_raw.json 已完成。严禁再次下载或转写，从 pipeline_wordrender 开始。\n"
    : input.status === "downloaded"
      ? "当前音频已下载。从 pipeline_transcribe 开始，不重复 episode_download。\n"
      : "";
  return `${resume}只处理 Episode ${input.episodeId}（guid=${input.guid}，标题=${input.title ?? "未知"}）。按顺序调用工具：episode_download → pipeline_transcribe → pipeline_wordrender → pipeline_orchestrate prepare。
随后读取必要源稿与 hits，填写 _context/brief.md、gaps.md、glossary.md；执行 fix，定点阅读/修正 en_fix.srt，再 fix manualDone=true。复核完成后必须读取复核稿并写 _context/review_notes.md，再 finalize。
严格按 prepare/apply 返回的 dispatch 与 callRef 调用 episode_dispatch 完成翻译和复核，再分别 apply、finalize。说话人身份无法确认时使用匿名标签，不臆造真名。
付费工具返回 unknown/failed 或调用状态不明时立即停止并报告，不自动重试，不更换 callRef 重发。不得审批、上传或交付，最终只推进到 reviewing。每一步都必须实际调用工具；不要只回复“继续执行”等文字，除非已到达 reviewing 或遇到需要人工处理的失败。`;
}
