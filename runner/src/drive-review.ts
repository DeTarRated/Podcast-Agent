// 主线驱动：派发复核子代理（真实 DeepSeek v4-pro，同档强模型）。
//
// 主 conversation 用 deepseek-v4-pro；复核子代理对照 en_fix.srt 与 en_zh.srt，
// 产出 en_zh_fix.srt + review_notes.md。账本 intent/outcome 由 dispatch 写入。
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

const [rProvider, rModel] = (env.PODCAST_MODEL_REVIEW ?? "deepseek/deepseek-v4-pro").split("/");
const ws = resolve(REPO_ROOT, "var/episodes/1");

const reviewTool: ToolSpec = {
  name: "pipeline_review",
  description: "派发复核子代理，对照原文与译文产出复核稿与复核说明（真实付费调用）",
  parameters: { type: "object", properties: {}, required: [] },
  replay: "safe",
  async execute(_args, extras) {
    const r = await extras!.dispatch!({
      role: "review",
      inputPath: resolve(ws, "en_zh.srt"),
      outputPath: resolve(ws, "en_zh_fix.srt"),
      callRef: "review:1:v1",
      model: { provider: rProvider, modelId: rModel },
      contextFiles: [
        resolve(REPO_ROOT, "skills/translate-srt/references/subtitle-rules.md"),
        resolve(REPO_ROOT, "skills/translate-srt/references/translation-style.md"),
        resolve(ws, "_context/brief.md"),
        resolve(ws, "_context/glossary.md"),
      ],
      referenceFiles: [
        resolve(ws, "en_fix.srt"),
        resolve(ws, "en.srt"),
        resolve(ws, "_context/alias_log.tsv"),
      ],
      ledger: { episodeId: 1, purpose: "review", provider: rProvider, model: rModel },
    });
    const result = r.result as { usage: { input: number; output: number; costTotal: number } };
    return {
      text: `复核完成 → en_zh_fix.srt（reused=${r.reused}，input=${result.usage.input}，output=${result.usage.output}，cost=$${result.usage.costTotal}）`,
    };
  },
};

const models = builtinModels();
const runner = await Runner.open(BACKGROUND_CONTEXT, {
  dbPath: resolve(REPO_ROOT, "var/episodes/1/runner-review.sqlite"),
  models,
  model: { provider: rProvider, modelId: rModel },
  tools: [reviewTool],
});

console.log("派发复核子代理（", rModel, "）...");
const out = await runner.submit(
  BACKGROUND_CONTEXT,
  "请调用 pipeline_review 工具，派发复核子代理处理这一期节目的译文。",
);
console.log("结果:", JSON.stringify(out));
await runner.close(BACKGROUND_CONTEXT);
