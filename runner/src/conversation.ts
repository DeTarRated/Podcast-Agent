// 执行层·最小单期 conversation 骨架：流水线工具（只依赖 ToolSpec，不依赖框架）
//
// pipeline.download / pipeline.transcribe 均为真实工具（经 bash 调 pipeline/ 下 Python CLI）；
// transcribe 为 unsafe：付费调用绝不自动重跑。
import { resolve } from "node:path";

import { runCommand } from "./bash.ts";
import type { ToolSpec } from "./tool-spec.ts";

const REPO_ROOT = resolve(import.meta.dirname, "../..");

export interface EpisodeRef {
  guid: string;
  feed: string;
}

export function episodeInput(ref: EpisodeRef): string {
  return `处理新一期节目：guid=${ref.guid}，feed=${ref.feed}。请依次执行流水线步骤。`;
}

export interface DownloadInfo {
  path: string;
  sha256: string;
  size: number;
  duration_secs: number | null;
  warning: string | null;
}

/** 经 bash 调 pipeline/download.py；返回解析后的 JSON 结果。 */
export async function runDownload(url: string, dest: string): Promise<DownloadInfo> {
  const r = await runCommand(
    "uv",
    ["run", "python", "-m", "pipeline.download", url, dest],
    { cwd: REPO_ROOT, timeoutMs: 120_000 },
  );
  if (r.exitCode !== 0) {
    throw new Error(`download 退出码 ${r.exitCode}: ${r.stderr.trim()}`);
  }
  const lastLine = r.stdout.trim().split("\n").at(-1) ?? "{}";
  return JSON.parse(lastLine) as DownloadInfo;
}

/** 经 bash 调 pipeline/scribe.py；返回解析后的 JSON 结果（status 非 success 时退出码为 1，由工具层转为错误）。 */
export interface ScribeInfo {
  status: string;
  call_ref: string;
  episode_id: number;
  transcription_id: string | null;
  raw_path: string | null;
  error: string | null;
}

export async function runScribe(
  command: "transcribe" | "fetch" | "mark-unknown",
  args: string[],
): Promise<{ info: ScribeInfo; exitCode: number; stderr: string }> {
  const r = await runCommand(
    "uv",
    ["run", "python", "-m", "pipeline.scribe", command, ...args],
    { cwd: REPO_ROOT, timeoutMs: 300_000 },
  );
  const lastLine = r.stdout.trim().split("\n").at(-1) ?? "{}";
  return { info: JSON.parse(lastLine) as ScribeInfo, exitCode: r.exitCode, stderr: r.stderr };
}

/** 流水线工具集：download 真实（safe）、transcribe 真实（unsafe，付费调用绝不自动重跑）。 */
export function pipelineTools(): ToolSpec[] {
  return [
    {
      name: "pipeline.download",
      description: "下载音频并 sha256 固化到工作区（重跑安全；同 hash 复用，hash 漂移保留首版）",
      parameters: {
        type: "object",
        properties: {
          url: { type: "string", description: "音频直链（自动跟随重定向）" },
          dest: { type: "string", description: "固化目标路径" },
        },
        required: ["url", "dest"],
      },
      replay: "safe",
      async execute(args) {
        const { url, dest } = args as { url: string; dest: string };
        const info = await runDownload(url, dest);
        const warning = info.warning ? `，warning=${info.warning}` : "";
        return {
          text: `downloaded ${url} → ${dest}（sha256=${info.sha256}，size=${info.size}，duration=${info.duration_secs}s${warning}）`,
        };
      },
    },
    {
      name: "pipeline.transcribe",
      description:
        "调用 ElevenLabs Scribe 转写（付费，unsafe：崩溃后绝不自动重跑）；" +
        "本地幂等键由 scribe.py 从 Episode guid + 音频 hash 构造，无需传入；" +
        "结果未知返回 unknown 时绝不重发，走 fetch / 人工对账",
      parameters: {
        type: "object",
        properties: {
          episode_id: { type: "integer", description: "state 库中的 Episode id（须已下载固化）" },
          audio: { type: "string", description: "固化音频路径" },
          workspace: { type: "string", description: "单期工作区（缺省 var/episodes/{episode_id}）" },
        },
        required: ["episode_id", "audio"],
      },
      // replay 省略 = unsafe
      async execute(args) {
        const { episode_id, audio, workspace } = args as {
          episode_id: number;
          audio: string;
          workspace?: string;
        };
        const cliArgs = [String(episode_id), audio];
        if (workspace) cliArgs.push("--workspace", workspace);
        const { info, exitCode, stderr } = await runScribe("transcribe", cliArgs);
        const detail =
          `status=${info.status}，call_ref=${info.call_ref}` +
          (info.transcription_id ? `，transcription_id=${info.transcription_id}` : "") +
          (info.raw_path ? `，raw=${info.raw_path}` : "") +
          (info.error ? `，error=${info.error}` : "");
        if (exitCode !== 0) {
          throw new Error(`transcribe 退出码 ${exitCode}：${detail}${stderr ? `（${stderr.trim()}）` : ""}`);
        }
        return { text: detail };
      },
    },
  ];
}
