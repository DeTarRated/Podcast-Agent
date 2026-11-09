// 执行层·子代理执行体（真实模型调用，经 pi-ai）
//
// dispatch 创建的子任务在此执行：读规范与上下文文件 → 调模型（DeepSeek 等）→
// 写产物（create-only）→ 回传真实 usage（pi-ai input/output/cacheRead/cost）。
// 翻译/复核角色对应 skill roles.md 约束；调研角色查 gaps.md。
// behaviors 表仅供测试注入模拟行为（崩溃/用量），生产路径为空即真实调用。
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { defineTask } from "@earendil-works/pi-durable";
import type { Model } from "@earendil-works/pi-ai";

const REPO_ROOT = resolve(import.meta.dirname, "../..");

export interface SubagentInput {
  role: "translate" | "review" | "research" | "highlights";
  /** 主输入文件（en_fix.srt / en_zh.srt / gaps.md） */
  inputPath: string;
  /** 产物输出路径（en_zh.txt / en_zh_fix.srt / research/01-gaps.md） */
  outputPath: string;
  /** 幂等键：同 role + inputPath 只允许一个活任务 */
  callRef: string;
  /** 账本元数据（可选）：提供时 dispatch 适配层写 intent/outcome */
  ledger?: {
    episodeId: number;
    purpose: "translate" | "review" | "research" | "highlights";
    provider: string;
    model: string;
    dbPath?: string;
  };
  /** 模型路由（deepseek/deepseek-flash 等）；生产必填 */
  model?: { provider: string; modelId: string };
  /** 子代理须先读的规范与上下文文件（绝对路径） */
  contextFiles?: string[];
  /** 复核角色的原文基准与日志（en_fix.srt、en.srt、alias_log.tsv） */
  referenceFiles?: string[];
  targetLang?: string;
}

export interface SubagentCheckpoint {
  phase: "call";
}

export interface SubagentResult {
  role: string;
  outputPath: string;
  /** 真实 token 用量（pi-ai usage） */
  usage: {
    input: number;
    output: number;
    cacheRead: number;
    totalTokens: number;
    costTotal: number;
    costFromProvider: boolean;
  };
}

const ROLE_PROMPTS: Record<string, string> = {
  translate:
    "你是足球播客字幕翻译。把给定 SRT 字幕条目翻译成简体中文。\n" +
    "【输出格式硬性要求】每行一条，严格为「编号<TAB>译文」——编号后紧跟一个制表符(TAB)，再跟译文。" +
    "禁止三段式（编号单独一行、译文另一行、空行分隔）。\n" +
    "示例输入：\n1\t[S01] Hello there.\n\n示例输出：\n1\t[卡拉格] 你好。\n" +
    "一一对应、不合并不拆条、不增删、不写时间轴、译文内不换行不空行。" +
    "说话人前缀按术语表译，专名遵循术语表，语域遵循简报风格基调，长句在语义停顿处加逗号，数字不用千位分隔符。",
  review:
    "你是字幕复核。对照原文基准与译文，检出并修正漏译、错译、术语不一致、说话人前缀丢失或译名不一致、语体错位。" +
    "输出修正后的完整译文（保持编号与时间轴），并附复核说明。",
  research:
    "你是补缺调研。只查 gaps.md 里列出的缺口，每项给出译名建议与来源 URL，不发散。",
  highlights:
    "你是播客看点编辑。根据已批准字幕生成简洁中文看点。" +
    "只输出一个 JSON object，不要 Markdown 或解释：{\"summary\":\"本期摘要\",\"items\":[{\"text\":\"看点内容\",\"timestampMs\":123456}]}。" +
    "timestampMs 必须使用字幕中实际存在的毫秒时间点，不得编造；summary 为字符串，items 为数组。",
};

// ---- 测试注入的模拟行为（生产路径为空）----

export interface SimulatedBehavior {
  /** call 阶段第一次执行时抛错（模拟崩溃/超时），用于验证恢复 */
  crashOnFirstCall?: boolean;
  /** 注入模拟结果（跳过真实模型调用） */
  mockOutput?: string;
  mockUsage?: SubagentResult["usage"];
}

export const behaviors = new Map<string, SimulatedBehavior & { attempts: number }>();

async function runSubagent(
  input: SubagentInput,
  models: {
    getModel: (p: string, m: string) => Model<never> | undefined;
    completeSimple: (m: unknown, c: unknown, o?: unknown) => Promise<unknown>;
  },
): Promise<SubagentResult> {
  // 测试注入：mockOutput 直接落盘，跳过真实调用
  const behavior = behaviors.get(input.callRef);
  if (behavior) {
    behavior.attempts += 1;
    if (behavior.crashOnFirstCall && behavior.attempts === 1) {
      throw new Error("simulated crash before output");
    }
    if (behavior.mockOutput !== undefined) {
      if (!existsSync(input.outputPath)) {
        mkdirSync(dirname(input.outputPath), { recursive: true });
        writeFileSync(input.outputPath, behavior.mockOutput, { flag: "wx" });
      }
      return {
        role: input.role,
        outputPath: input.outputPath,
        usage: behavior.mockUsage ?? { input: 100, output: 50, cacheRead: 0, totalTokens: 150, costTotal: 0, costFromProvider: false },
      };
    }
  }

  if (!input.model) {
    throw new Error("真实子代理须指定 model（provider/modelId）");
  }
  const model = models.getModel(input.model.provider, input.model.modelId);
  if (!model) {
    throw new Error(`模型未配置: ${input.model.provider}/${input.model.modelId}`);
  }

  // 读规范与上下文（skill 约定：子代理先 Read 再动手）
  const contextParts: string[] = [];
  for (const f of input.contextFiles ?? []) {
    if (existsSync(f)) {
      contextParts.push(`=== ${f} ===\n${readFileSync(f, "utf8")}`);
    }
  }
  const sourceText = readFileSync(input.inputPath, "utf8");
  const references = (input.referenceFiles ?? [])
    .filter((f) => existsSync(f))
    .map((f) => `=== 参考 ${f} ===\n${readFileSync(f, "utf8")}`)
    .join("\n\n");

  const systemPrompt = ROLE_PROMPTS[input.role] + (contextParts.length
    ? "\n\n以下是你必须先读的规范与上下文：\n" + contextParts.join("\n\n")
    : "");

  // 翻译角色：把 SRT 转成「编号<TAB>原文」（剥离时间轴），模型只输出译文
  const toTabbed = (srt: string): string => {
    const blocks = srt.trim().split(/\n\s*\n/);
    const lines: string[] = [];
    for (const b of blocks) {
      const rows = b.split("\n");
      if (rows.length >= 3 && /^\d+$/.test(rows[0].trim())) {
        const text = rows.slice(2).join(" ").trim();
        lines.push(`${rows[0].trim()}\t${text}`);
      }
    }
    return lines.join("\n");
  };

  const userContent = input.role === "translate"
    ? `翻译以下条目为简体中文（输入为「编号<TAB>原文」，输出「编号<TAB>译文」）：\n\n${toTabbed(sourceText)}`
    : input.role === "review"
      ? `原文基准与参考：\n${references}\n\n待复核译文：\n${sourceText}`
      : input.role === "highlights"
        ? `节目元数据与参考：\n${references}\n\n已批准字幕：\n${sourceText}`
        : `调研以下缺口：\n${sourceText}`;

  const message = (await models.completeSimple(
    model,
    { systemPrompt, messages: [{ role: "user", content: userContent, timestamp: Date.now() }] },
    {},
  )) as {
    content: Array<{ type: string; text?: string }>;
    usage: { input: number; output: number; cacheRead: number; totalTokens: number; cost: { total: number; totalFromProvider?: boolean } };
  };

  const output = message.content
    .filter((c) => c.type === "text")
    .map((c) => c.text ?? "")
    .join("");

  // 产物落盘：create-only（已存在则校验不覆盖）
  if (!existsSync(input.outputPath)) {
    mkdirSync(dirname(input.outputPath), { recursive: true });
    writeFileSync(input.outputPath, output, { flag: "wx" });
  }

  const u = message.usage;
  return {
    role: input.role,
    outputPath: input.outputPath,
    usage: {
      input: u.input, output: u.output, cacheRead: u.cacheRead,
      totalTokens: u.totalTokens, costTotal: u.cost.total,
      costFromProvider: u.cost.totalFromProvider ?? false,
    },
  };
}

export function defineSubagentTask() {
  return defineTask<SubagentInput, SubagentCheckpoint, SubagentResult>({
    name: "subagent",
    version: 1,
    initial: () => ({ phase: "call" }),
    phases: {
      call: async (task, runtime, ctx) => {
        const result = await runSubagent(task.input, runtime.models as never);
        await runtime.commit(
          () => ({ status: "terminal", outcome: { status: "completed", result } }),
          ctx,
        );
      },
    },
    abort: async (task, runtime, ctx) => {
      await runtime.commit(
        () => ({ status: "terminal", outcome: { status: "aborted", reason: "aborted" } }),
        ctx,
      );
    },
  });
}
