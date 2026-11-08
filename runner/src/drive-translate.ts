// 主线驱动：在真实样本期上派发翻译子代理（真实 DeepSeek）。
//
// 主 conversation 用 deepseek-v4-pro，加载样本工作区上下文，
// 经 dispatch 派发整份翻译（deepseek-flash，709 条 en_fix.srt → en_zh.txt）。
// 付费调用：翻译一次（约 3.4 万 tokens）；账本 intent/outcome 由 dispatch 写入。
//
// .env 必须在任何 pi-ai import 之前加载（envApiKeyAuth 在 createModels 时读环境）。
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const REPO_ROOT = resolve(import.meta.dirname, "../..");
const env = Object.fromEntries(
  readFileSync(resolve(REPO_ROOT, ".env"), "utf8")
    .split("\n")
    .filter((l) => /^[A-Z_]+=/.test(l))
    .map((l) => { const i = l.indexOf("="); return [l.slice(0, i).trim(), l.slice(i + 1).trim()] as [string, string]; }),
);
for (const [k, v] of Object.entries(env)) {
  if (!(k in process.env)) process.env[k] = v;
}

const { builtinModels } = await import("@earendil-works/pi-ai/providers/all");
const { BACKGROUND_CONTEXT } = await import("@earendil-works/chord/context");
const { Runner } = await import("./runner.ts");
import type { ToolSpec } from "./tool-spec.ts";
const [tProvider, tModel] = (env.PODCAST_MODEL_TRANSLATE ?? "deepseek/deepseek-flash").split("/");
const [mProvider, mModel] = (env.PODCAST_MODEL_REVIEW ?? "deepseek/deepseek-v4-pro").split("/");

const ws = resolve(REPO_ROOT, "var/episodes/1");
const enFix = resolve(ws, "en_fix.srt");
const enZhTxt = resolve(ws, "en_zh.txt");

const translateTool: ToolSpec = {
  name: "pipeline_translate",
  description: "派发翻译子代理，把整份 en_fix.srt 翻译成中文 en_zh.txt（真实付费调用）",
  parameters: { type: "object", properties: {}, required: [] },
  replay: "safe",
  async execute(_args, extras) {
    const r = await extras!.dispatch!({
      role: "translate",
      inputPath: enFix,
      outputPath: enZhTxt,
      callRef: "translate:1:v2",
      model: { provider: tProvider, modelId: tModel },
      contextFiles: [
        resolve(REPO_ROOT, "skills/translate-srt/references/subtitle-rules.md"),
        resolve(REPO_ROOT, "skills/translate-srt/references/translation-style.md"),
        resolve(ws, "_context/brief.md"),
        resolve(ws, "_context/glossary.md"),
      ],
      ledger: {
        episodeId: 1, purpose: "translate", provider: tProvider, model: tModel,
      },
    });
    const result = r.result as { usage: { input: number; output: number; costTotal: number } };
    return {
      text: `翻译完成 → ${enZhTxt}（reused=${r.reused}，input=${result.usage.input}，output=${result.usage.output}，cost=$${result.usage.costTotal}）`,
    };
  },
};

const models = builtinModels();
const runner = await Runner.open(BACKGROUND_CONTEXT, {
  dbPath: resolve(REPO_ROOT, "var/episodes/1/runner.sqlite"),
  models,
  model: { provider: mProvider, modelId: mModel },
  tools: [translateTool],
});

console.log("主 conversation 启动（模型:", mModel, "）；派发翻译（", tModel, "）...");
const out = await runner.submit(
  BACKGROUND_CONTEXT,
  "请调用 pipeline_translate 工具，派发翻译子代理处理这一期节目。",
);
console.log("结果:", JSON.stringify(out));
await runner.close(BACKGROUND_CONTEXT);
