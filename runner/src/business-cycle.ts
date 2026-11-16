// 执行层·业务循环编排：poll --classify → 入队/处理 → 自动审批 → 交付发布，JSON 汇总。
// 前置健康/暂停/预算一致性检查失败关闭；阶段失败记录后继续独立阶段；失败计数汇总为非零退出。
// dry run 只打印计划，不请求 feed、不查询业务写接口、不触发供应商。
// 长期任务不强杀：业务命令不设 kill 超时，超时语义由上层停止新增工作与告警承担。
import { randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { runCommand, type RunResult } from "./bash.ts";
import { loadRepoEnv } from "./env.ts";
import { assertBusinessRunning } from "./pause.ts";
import { acquireRunLock, type RunLock } from "./run-lock.ts";
import { runPending } from "./drive-pending.ts";
import { runDelivery } from "./drive-delivery.ts";

const REPO_ROOT = resolve(import.meta.dirname, "../..");

export interface CycleOptions {
  commandRunner?: typeof runCommand;
  spawnImpl?: typeof spawn;
  dryRun?: boolean;
  feedId?: number;
  limit?: number;
  parallel?: number;
  runId?: string;
}

export interface StageResult {
  stage: string;
  ok: boolean;
  error?: string;
  summary?: Record<string, unknown>;
}

/** 日志/汇总脱敏：整段 URL 查询串（含 S3v4 签名）、Cookie/Authorization 头、长 token；截断长度。 */
export function redactText(text: string, max = 300): string {
  return text
    .replace(/(https?:\/\/[^\s"'?]+)\?[^\s"']*/g, "$1?<redacted>")
    .replace(/(cookie:)[^\n]*/gi, "$1 <redacted>")
    .replace(/(authorization:\s*(?:bearer|basic)\s+)[^\s]+/gi, "$1<redacted>")
    .replace(/(bearer\s+)[A-Za-z0-9._\-]+/gi, "$1<redacted>")
    .replace(/(api[_-]?key=)[^\s&"']+/gi, "$1<redacted>")
    .replace(/[A-Za-z0-9_\-]{24,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{20,}/g, "<token-redacted>")
    .slice(0, max);
}

function lastJson(stdout: string): Record<string, unknown> {
  const line = stdout.trim().split("\n").at(-1) ?? "";
  const payload = JSON.parse(line) as Record<string, unknown>;
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("输出不是 JSON object");
  }
  return payload;
}

/** 严格字段校验：缺字段/类型非法一律失败关闭，不用默认值替代安全关键字段。 */
function needNumber(payload: Record<string, unknown>, key: string, stage: string): number {
  const value = payload[key];
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${stage} 输出缺字段或类型非法: ${key}`);
  }
  return value;
}

function needBoolean(payload: Record<string, unknown>, key: string, stage: string): boolean {
  const value = payload[key];
  if (typeof value !== "boolean") {
    throw new Error(`${stage} 输出缺字段或类型非法: ${key}`);
  }
  return value;
}

function planStages(): string[] {
  return ["preflight", "poll", "drive-pending", "auto-approve", "delivery"];
}

export async function runBusinessCycle(options: CycleOptions = {}): Promise<Record<string, unknown>> {
  const runner = options.commandRunner ?? runCommand;
  const spawnImpl = options.spawnImpl ?? spawn;
  const runId = options.runId ?? randomUUID();
  const limit = options.limit ?? 5;
  const parallel = options.parallel ?? 2;
  const feedId = options.feedId;
  if (!Number.isInteger(limit) || limit < 1 || limit > 20) throw new Error("单轮任务数 limit 必须在 1..20");
  if (!Number.isInteger(parallel) || parallel < 1 || parallel > 4) throw new Error("并行度必须在 1..4");

  const log = (stage: string, message: string) => {
    console.log(JSON.stringify({ run_id: runId, stage, message: redactText(message, 500) }));
  };

  // 统一 .env 加载（格式错误失败关闭）；dry run 也要加载以预览真实额度。
  loadRepoEnv();
  const maxCalls = Number(process.env.PODCAST_CYCLE_MAX_CALLS ?? 50);
  const maxUsd = Number(process.env.PODCAST_CYCLE_MAX_USD ?? 2.0);
  if (!Number.isFinite(maxCalls) || maxCalls <= 0) throw new Error("PODCAST_CYCLE_MAX_CALLS 非法");
  if (!Number.isFinite(maxUsd) || maxUsd <= 0) throw new Error("PODCAST_CYCLE_MAX_USD 非法");

  if (options.dryRun) {
    return {
      status: "dry_run",
      run_id: runId,
      plan: planStages().map((stage) => ({
        stage,
        feed_id: feedId ?? "all",
        limit,
        parallel,
        max_calls: maxCalls,
        max_usd: maxUsd,
      })),
    };
  }

  assertBusinessRunning();
  const lock: RunLock | null = await acquireRunLock(spawnImpl);
  if (lock === null) {
    return { status: "locked", run_id: runId, failures: ["lock"], stages: [] };
  }
  // 下游 drive-pending/drive-delivery/子进程经 token 识别本进程持锁。
  process.env.PODCAST_RUN_LOCK_TOKEN = runId;

  const stages: StageResult[] = [];
  const fail = (stage: string, error: string, summary?: Record<string, unknown>): StageResult => {
    const result: StageResult = { stage, ok: false, error: redactText(error, 500), summary };
    stages.push(result);
    log(stage, `FAILED: ${error}`);
    return result;
  };
  const ok = (stage: string, summary: Record<string, unknown>): StageResult => {
    const result: StageResult = { stage, ok: true, summary };
    stages.push(result);
    log(stage, `OK ${JSON.stringify(summary)}`);
    return result;
  };

  // 业务命令不设 kill 超时：在途工作保留，超时语义=停止新增+告警，由上层承担。
  async function uvJson(args: string[], input?: string): Promise<Record<string, unknown>> {
    const result: RunResult = await runner("uv", ["run", "python", ...args], {
      cwd: REPO_ROOT, input,
    });
    if (result.exitCode !== 0) throw new Error(`退出码 ${result.exitCode}: ${redactText(result.stderr, 300)}`);
    const payload = lastJson(result.stdout);
    // 严格 envelope：成功必须显式 status=success 且 data 为 object，不用缺省值兼容未知形态。
    if (payload.status !== "success") {
      throw new Error(redactText(JSON.stringify(payload.error ?? payload), 300));
    }
    const data = payload.data;
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      throw new Error("输出 envelope.data 不是 JSON object");
    }
    return data as Record<string, unknown>;
  }

  interface Totals {
    calls: number;
    usd: number;
  }

  async function ledgerTotals(): Promise<Totals> {
    const data = await uvJson(["-m", "state.ledger_cli", "totals"]);
    return {
      // 在途/未对账预留与成功调用合计计入单轮额度，unknown/intent 不被漏算。
      calls: needNumber(data, "success_calls", "ledger.totals") + needNumber(data, "open_calls", "ledger.totals"),
      usd: needNumber(data, "success_total_usd", "ledger.totals") + needNumber(data, "open_total_usd", "ledger.totals"),
    };
  }

  let totalsStart: Totals | undefined;
  let budgetBreached = false;

  /**
   * 额度门：在阶段间与派发边界（每次 claim / 每条 highlights）调用。
   * 超限或账本读取失败均记为 budget 失败并只记录一次；返回 true 表示必须停止新增派发。
   */
  async function enforceBudget(): Promise<boolean> {
    if (budgetBreached) return true;
    if (!totalsStart) return false;
    try {
      const current = await ledgerTotals();
      const callsDelta = current.calls - totalsStart.calls;
      const usdDelta = current.usd - totalsStart.usd;
      if (callsDelta >= maxCalls || usdDelta >= maxUsd) {
        budgetBreached = true;
        fail("budget", `单轮额度超限: calls=${callsDelta}/${maxCalls} usd=${usdDelta.toFixed(6)}/${maxUsd}`);
        return true;
      }
      return false;
    } catch (error) {
      // 账本不可读同样失败关闭，且不得以未捕获异常中断汇总。
      budgetBreached = true;
      fail("budget", `额度检查失败: ${error instanceof Error ? error.message : String(error)}`);
      return true;
    }
  }

  /** 统一早退结构：failures 一律由 stages 推导，不硬编码。 */
  const summarize = (status: string): Record<string, unknown> => ({
    status,
    run_id: runId,
    failures: stages.filter((stage) => !stage.ok).map((stage) => stage.stage),
    stages: stages.map((stage) => ({
      stage: stage.stage,
      ok: stage.ok,
      ...(stage.error ? { error: stage.error } : {}),
      ...(stage.summary ? { summary: stage.summary } : {}),
    })),
  });

  try {
    // 前置健康／暂停／预算一致性检查：任一失败即整轮失败关闭。
    try {
      assertBusinessRunning();
      await uvJson(["-m", "state.postgres.job_cli", "stats"]);
      await uvJson(["-m", "state.ledger_cli", "get", "--call-ref", "__preflight_probe__"]);
      // 原子化探针：CLI 内部 acquire+release，调用方无需保存 owner，杜绝许可泄漏。
      await uvJson(["-m", "pipeline.provider_gate", "probe", "--provider", "deepseek"]);
      totalsStart = await ledgerTotals();
      ok("preflight", { pg: "ok", sqlite_ledger: "ok", redis_gate: "ok", pause: "absent" });
    } catch (error) {
      fail("preflight", error instanceof Error ? error.message : String(error));
      return summarize("preflight_failed");
    }

    // poll --classify：feed 失败与分类失败（含逐项聚合）计入最终失败判据。
    try {
      // 分类次数上限即派发边界额度：剩余可用调用数作为本轮 max-classify，
      // poller 在每次付费分类前检查，耗尽后候选留 pending 供下轮。
      const remainingBeforePoll = totalsStart
        ? Math.max(0, maxCalls - ((await ledgerTotals()).calls - totalsStart.calls))
        : maxCalls;
      const poll = await uvJson(
        ["-m", "discover.cli", "poll", "--classify",
         "--max-classify", String(remainingBeforePoll),
         ...(feedId !== undefined ? ["--feed-id", String(feedId)] : [])],
      );
      const feeds = needNumber(poll, "feeds", "poll");
      const newCandidates = needNumber(poll, "new_candidates", "poll");
      const enqueued = needNumber(poll, "enqueued", "poll");
      const topFailed = needNumber(poll, "failed", "poll");
      if (!Array.isArray(poll.items)) throw new Error("poll 输出缺字段或类型非法: items");
      const items = poll.items as Record<string, unknown>[];
      // 条目缺 classification_failed 判失败关闭，不用缺省零兼容未知形态。
      const perItemFailed = items.reduce((acc, item) => (
        acc + needNumber(item ?? {}, "classification_failed", "poll.items")
      ), 0);
      const topClassified = needNumber(poll, "classification_failed", "poll");
      const classificationFailed = topClassified + perItemFailed;
      // 被额度延后的候选数量必须可见，运维才能判断限流是否生效。
      const deferred = needNumber(poll, "classify_deferred", "poll");
      const summary = {
        feeds, new_candidates: newCandidates, enqueued,
        failed: topFailed, classification_failed: classificationFailed,
        max_classify: remainingBeforePoll, classify_deferred: deferred,
      };
      if (topFailed > 0 || classificationFailed > 0) {
        fail("poll", `feed_failed=${topFailed} classification_failed=${classificationFailed}`, summary);
      } else {
        ok("poll", summary);
      }
      // 额度硬门：poll 的分类消费已发生，超限时停止后续阶段新增派发。
      if (await enforceBudget()) return summarize("failed");
    } catch (error) {
      // 本轮 poll 失败不阻断处理既有队列（独立工作），但计入失败。
      fail("poll", error instanceof Error ? error.message : String(error));
      if (await enforceBudget()) return summarize("failed");
    }
    if (budgetBreached) return summarize("failed");

    // drive-pending：处理既有队列；worker 失败与失租计入失败判据。
    try {
      const pending = await runPending({
        commandRunner: runner, spawnImpl, parallel, limit,
        // 派发边界额度门：每次 claim 前检查，超限不再新增工作。
        beforeClaim: async () => !(await enforceBudget()),
      }) as Record<string, unknown>;
      if (pending.status === "locked") {
        fail("drive-pending", "共享运行锁竞争失败");
      } else if (pending.status === "error_pause_write_failed") {
        fail("drive-pending", String(pending.pause_write_failed));
      } else if (pending.status !== "success") {
        fail("drive-pending", `调度器状态异常: ${String(pending.status)}`);
      } else {
        if (!Array.isArray(pending.job_ids)) throw new Error("drive-pending 输出缺字段或类型非法: job_ids");
        if (!Array.isArray(pending.estimate_failures)) throw new Error("drive-pending 输出缺字段或类型非法: estimate_failures");
        if (!Array.isArray(pending.skipped_in_flight)) throw new Error("drive-pending 输出缺字段或类型非法: skipped_in_flight");
        const summary = {
          claimed: needNumber(pending, "claimed", "drive-pending"),
          completed: needNumber(pending, "completed", "drive-pending"),
          failed: needNumber(pending, "failed", "drive-pending"),
          lease_lost: needBoolean(pending, "lease_lost", "drive-pending"),
          job_ids: pending.job_ids as number[],
          estimate_failures: (pending.estimate_failures as Array<Record<string, unknown>>).length,
          skipped_in_flight: (pending.skipped_in_flight as Array<Record<string, unknown>>).length,
        };
        if (summary.estimate_failures > 0) {
          fail("drive-pending", `estimate_failed=${summary.estimate_failures}`, summary);
        } else if (summary.failed > 0 || summary.lease_lost) {
          fail("drive-pending", `worker_failed=${summary.failed} lease_lost=${summary.lease_lost}`, summary);
        } else {
          ok("drive-pending", summary);
        }
      }
      if (await enforceBudget()) return summarize("failed");
    } catch (error) {
      fail("drive-pending", error instanceof Error ? error.message : String(error));
      if (await enforceBudget()) return summarize("failed");
    }
    if (budgetBreached) return summarize("failed");

    // auto-approve-due：审批 error/skipped 计入失败判据。
    try {
      const approve = await uvJson(["-m", "review.cli", "auto-approve-due"], "{}");
      const summary = {
        approved: needNumber(approve, "approved", "auto-approve"),
        skipped: needNumber(approve, "skipped", "auto-approve"),
      };
      if (summary.skipped > 0) fail("auto-approve", `approval_skipped=${summary.skipped}`, summary);
      else ok("auto-approve", summary);
    } catch (error) {
      fail("auto-approve", error instanceof Error ? error.message : String(error));
    }

    if (budgetBreached) return summarize("failed");

    // delivery：发布 + highlights 补齐；两轮 publish 的失败项都计入判据。
    try {
      const delivery = await runDelivery({
        commandRunner: runner, spawnImpl,
        // 首次发布前与每条 highlights 派发前的额度门。
        beforeDispatch: async () => !(await enforceBudget()),
      }, lock) as Record<string, unknown>;
      if (delivery.status === "locked") {
        fail("delivery", "共享运行锁竞争失败");
      } else if (delivery.status === "budget_blocked") {
        // 额度门已记录 budget 失败，这里不重复记录，交由最终汇总体现。
        if (!budgetBreached) fail("delivery", "额度门阻断派发");
      } else if (delivery.status !== "success") {
        fail("delivery", `交付调度状态异常: ${String(delivery.status)}`);
      } else {
        const summary = {
          published: needNumber(delivery, "published", "delivery"),
          highlights_completed: needNumber(delivery, "highlights_completed", "delivery"),
          failed: needNumber(delivery, "failed", "delivery"),
        };
        if (summary.failed > 0) fail("delivery", `publish_failed=${summary.failed}`, summary);
        else ok("delivery", summary);
      }
    } catch (error) {
      fail("delivery", error instanceof Error ? error.message : String(error));
    }

    // 收尾额度核对：在途预留口径的最终兜底。
    if (totalsStart) await enforceBudget();
  } finally {
    lock.release();
    delete process.env.PODCAST_RUN_LOCK_TOKEN;
  }

  const failures = stages.filter((stage) => !stage.ok).map((stage) => stage.stage);
  return summarize(failures.length === 0 ? "success" : "failed");
}

function readIntFlag(args: string[], name: string): number | undefined {
  const index = args.indexOf(name);
  if (index < 0) return undefined;
  const raw = args[index + 1];
  const value = Number(raw);
  if (raw === undefined || !Number.isInteger(value)) {
    throw new Error(`参数 ${name} 缺失或不是整数`);
  }
  return value;
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  const dryRun = args.includes("--dry-run");
  const feedId = readIntFlag(args, "--feed-id");
  const limit = readIntFlag(args, "--limit");
  const parallel = readIntFlag(args, "--parallel");
  const result = await runBusinessCycle({ dryRun, feedId, limit, parallel });
  console.log(JSON.stringify(result));
  if (result.status !== "success" && result.status !== "dry_run") process.exitCode = 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(JSON.stringify({ status: "failed", error: redactText(error instanceof Error ? error.message : String(error)) }));
    process.exitCode = 1;
  });
}
