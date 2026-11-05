// 执行层·最小单期 conversation 骨架：占位工具 + registry 组装
//
// 占位工具仅验证框架语义（下一步 kill -9 实验用），真实实现随后接 pipeline/ 的 bash 调用。
import { defineExtension, defineTool, type Registry, type ToolRegistration } from "@earendil-works/pi-durable";

export interface EpisodeRef {
  guid: string;
  feed: string;
}

export function episodeInput(ref: EpisodeRef): string {
  return `处理新一期节目：guid=${ref.guid}，feed=${ref.feed}。请依次执行流水线步骤。`;
}

/** 占位工具：download 声明 safe（重跑安全）、transcribe 省略 replay（默认 unsafe，付费调用绝不自动重跑）。 */
export function placeholderTools(counts?: Map<string, number>): ToolRegistration[] {
  const record = (name: string): void => {
    if (counts) counts.set(name, (counts.get(name) ?? 0) + 1);
  };
  return [
    defineTool({
      name: "pipeline.download",
      description: "下载音频并 sha256 固化到工作区（重跑安全）",
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
        record("pipeline.download");
        return { output: [{ type: "text", text: `downloaded ${url} -> ${dest}` }] };
      },
    }),
    defineTool({
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
        record("pipeline.transcribe");
        return {
          output: [{ type: "text", text: `transcribed ${audio} (key=${idempotency_key})` }],
        };
      },
    }),
  ];
}

export function installPipelineTools(registry: Registry, counts?: Map<string, number>): void {
  registry.install(defineExtension({ name: "podcast-pipeline", tools: placeholderTools(counts) }));
}
