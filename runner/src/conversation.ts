// 执行层·最小单期 conversation 骨架：流水线工具（只依赖 ToolSpec，不依赖框架）
//
// pipeline.download 为真实工具（经 bash 调 pipeline/download.py）；
// pipeline.transcribe 仍是占位（真实实现等 Scribe 凭据）。
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
    ["run", "python", "pipeline/download.py", url, dest],
    { cwd: REPO_ROOT, timeoutMs: 120_000 },
  );
  if (r.exitCode !== 0) {
    throw new Error(`download 退出码 ${r.exitCode}: ${r.stderr.trim()}`);
  }
  const lastLine = r.stdout.trim().split("\n").at(-1) ?? "{}";
  return JSON.parse(lastLine) as DownloadInfo;
}

/** 流水线工具集：download 真实（safe）、transcribe 占位（unsafe，付费调用绝不自动重跑）。 */
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
      description: "调用 ElevenLabs Scribe 转写（付费调用，unsafe：崩溃后绝不自动重跑）",
      parameters: {
        type: "object",
        properties: {
          audio: { type: "string", description: "固化音频路径" },
          idempotency_key: { type: "string", description: "guid + 音频 hash" },
        },
        required: ["audio", "idempotency_key"],
      },
      // replay 省略 = unsafe
      async execute(args) {
        const { audio, idempotency_key } = args as { audio: string; idempotency_key: string };
        return { text: `transcribed ${audio} (key=${idempotency_key})` };
      },
    },
  ];
}
