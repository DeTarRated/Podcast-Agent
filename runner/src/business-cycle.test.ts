// 业务循环编排测试；所有外部命令与 spawn 均 fake，零 feed 请求与真实模型调用。
//
// 模拟场景：
// - dry run 只打印计划、零命令
// - 完整成功路径的阶段顺序与汇总；preflight 探针 acquire/release 协议（owner 由闸门生成）
// - preflight 失败整轮失败关闭；poll/worker/审批/发布失败记录后继续独立阶段且最终非零
// - 分类失败按真实 per-item 形状聚合；两轮 publish 失败全部计入；缺字段失败关闭
// - 暂停标记与锁竞争失败关闭；单轮调用/金额上限超限判失败
// - redactText 去除签名查询串、Cookie、Bearer 并截断
import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough } from "node:stream";
import type { spawn } from "node:child_process";

import { redactText, runBusinessCycle } from "./business-cycle.ts";
import type { RunResult } from "./bash.ts";

function flockHeld() {
  const child = new EventEmitter() as any;
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.kill = () => true;
  process.nextTick(() => child.stdout.write("podcast-agent-run-lock\n"));
  return child;
}

function taskChild(exitCode = 0, text = "done") {
  const child = new EventEmitter() as any;
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.kill = () => true;
  process.nextTick(() => {
    child.stdout.end(text);
    child.stderr.end();
    child.emit("close", exitCode);
  });
  return child;
}

function spawnFake(options: { lock?: boolean; taskExit?: number; taskText?: string } = {}): typeof spawn {
  const { lock = true, taskExit = 0, taskText = "done" } = options;
  return ((command: string, args: string[]) => {
    if (command === "flock") return lock ? flockHeld() : taskChild(1, "");
    // 只让单期执行子进程失败；highlights 等其他子进程保持成功。
    const isEpisodeChild = (args ?? []).some((arg) => arg.includes("drive-episode"));
    return isEpisodeChild ? taskChild(taskExit, taskText) : taskChild(0);
  }) as typeof spawn;
}

interface RouterOptions {
  pollFailed?: number;
  pollClassificationFailed?: number;
  pollMalformed?: boolean;
  pollMissingField?: boolean;
  pendingMissingLeaseLost?: boolean;
  approveSkipped?: number;
  firstPublishFailed?: number;
  secondPublishFailed?: number;
  preflightFail?: "pg" | "malformed";
  episodes?: Array<{ id: number; guid: string }>;
}

let routerTotals: (() => Record<string, unknown>) | undefined;
let onPollInvoked: (() => void) | undefined;

function router(options: RouterOptions = {}) {
  const uvCalls: string[][] = [];
  let publishCalls = 0;
  let claimCalls = 0;
  const runner = async (_command: string, args: string[], opts?: { input?: string }): Promise<RunResult> => {
    uvCalls.push(args);
    const module = args[3];
    const action = args[4];
    let data: unknown;
    if (options.preflightFail === "pg" && module === "state.postgres.job_cli" && action === "stats") {
      return { exitCode: 1, stdout: "", stderr: "pg down" };
    }
    if (options.preflightFail === "malformed" && module === "state.postgres.job_cli" && action === "stats") {
      return { exitCode: 0, stdout: "not-json", stderr: "" };
    }
    switch (module) {
      case "state.postgres.job_cli":
        if (action === "stats") data = { ready: 0 };
        else if (action === "enqueue-episode") data = { reused: false };
        else if (action === "claim") {
          claimCalls += 1;
          data = (options.episodes?.length && claimCalls <= options.episodes.length)
            ? { job: { id: claimCalls, episode_id: options.episodes[claimCalls - 1].id } }
            : null;
        }
        else if (action === "heartbeat") data = { updated: true };
        else if (action === "complete") data = { status: "completed" };
        else if (action === "fail") data = { status: "failed" };
        break;
      case "state.ledger_cli":
        if (action === "totals") data = routerTotals?.() ?? { success_calls: 0, success_total_usd: 0, open_calls: 0, open_total_usd: 0, by_status: {} };
        else data = { status: "missing" };
        break;
      case "pipeline.provider_gate":
        // 真实 CLI：probe 原子化 acquire+release，调用方不保存 owner。
        if (action === "probe") data = { allowed: true, released: true };
        else if (action === "acquire") data = { allowed: true, provider: "deepseek", owner: "gate-owner-1", retry_after_ms: 0, remaining_tokens: 10, active: 0 };
        else data = { released: true };
        break;
      case "pipeline.episode_flow":
        if (action === "pending") data = {
          items: (options.episodes ?? []).map((item) => ({
            ...item, estimated_cost: "0.900000", estimate_basis: "estimate_version=v1;max_duration_secs=5400",
          })),
        };
        else if (action === "acquire") data = { acquired: true };
        else data = { released: true };
        break;
      case "discover.cli": {
        onPollInvoked?.();
        if (options.pollMalformed) return { exitCode: 0, stdout: "???", stderr: "" };
        if (options.pollMissingField) {
          data = { feeds: 1, new_candidates: 0, items: [] }; // 缺 enqueued/failed
          break;
        }
        // 真实 poll_all 形状：classification_failed 在 items 内逐项给出。
        // 真实 poll_all 形状：顶层 failed/classification_failed + items 逐项统计。
        data = {
          feeds: 1,
          new_candidates: 1,
          enqueued: 1,
          failed: options.pollFailed ?? 0,
          classification_failed: 0,
          classify_deferred: 0,
          items: [
            { feed_id: 1, status: "ok", new_candidates: 1, enqueued: 1, failed: 0, classification_failed: options.pollClassificationFailed ?? 0 },
          ],
        };
        break;
      }
      case "review.cli":
        data = { approved: 0, skipped: options.approveSkipped ?? 0 };
        break;
      case "delivery.cli": {
        publishCalls += 1;
        if (action === "publish-approved") {
          data = publishCalls === 1
            ? { published: 0, items: [{ episode_id: 1, approval_id: 9, status: "waiting_highlights", dispatchFile: "/tmp/dispatch.json" }], failed: options.firstPublishFailed ?? 0 }
            : { published: 0, items: [], failed: options.secondPublishFailed ?? 0 };
        } else {
          data = { status: "failed" };
        }
        break;
      }
      default:
        throw new Error(`unexpected module ${module}`);
    }
    return { exitCode: 0, stdout: JSON.stringify({ status: "success", data }), stderr: "" };
  };
  return { runner, uvCalls };
}

const stageNames = (result: Record<string, unknown>) =>
  ((result.stages as Array<{ stage: string }>) ?? []).map((stage) => stage.stage);

test("dry run 只打印计划，零外部命令", async () => {
  const { runner, uvCalls } = router();
  const result = await runBusinessCycle({
    commandRunner: runner, spawnImpl: spawnFake(), dryRun: true, feedId: 3, limit: 2, parallel: 1, runId: "dry-1",
  });
  assert.equal(result.status, "dry_run");
  assert.deepEqual(
    (result.plan as Array<{ stage: string }>).map((stage) => stage.stage),
    ["preflight", "poll", "drive-pending", "auto-approve", "delivery"],
  );
  assert.deepEqual(uvCalls, []);
});

test("完整成功路径：阶段顺序、preflight探针协议与成功汇总", async () => {
  const { runner, uvCalls } = router();
  const result = await runBusinessCycle({
    commandRunner: runner, spawnImpl: spawnFake(), limit: 2, parallel: 1, runId: "ok-1",
  });
  assert.equal(result.status, "success");
  assert.deepEqual(stageNames(result), ["preflight", "poll", "drive-pending", "auto-approve", "delivery"]);
  assert.ok(uvCalls.some((args) => args[3] === "pipeline.provider_gate" && args[4] === "probe"));
  assert.ok(uvCalls.some((args) => args[3] === "discover.cli" && args[4] === "poll" && args.includes("--classify")));
  assert.ok(uvCalls.some((args) => args[3] === "review.cli"));
});

test("preflight 失败整轮失败关闭，不执行后续阶段", async () => {
  const { runner, uvCalls } = router({ preflightFail: "pg" });
  const result = await runBusinessCycle({
    commandRunner: runner, spawnImpl: spawnFake(), runId: "pre-1",
  });
  assert.equal(result.status, "preflight_failed");
  assert.deepEqual(stageNames(result), ["preflight"]);
  assert.equal(uvCalls.some((args) => args[3] === "discover.cli"), false);
});

test("preflight 输出畸形 JSON 失败关闭", async () => {
  const { runner } = router({ preflightFail: "malformed" });
  const result = await runBusinessCycle({
    commandRunner: runner, spawnImpl: spawnFake(), runId: "pre-2",
  });
  assert.equal(result.status, "preflight_failed");
});

test("poll feed 失败记录后继续独立阶段且最终非零", async () => {
  const { runner, uvCalls } = router({ pollFailed: 1 });
  const result = await runBusinessCycle({
    commandRunner: runner, spawnImpl: spawnFake(), runId: "poll-1",
  });
  assert.equal(result.status, "failed");
  assert.deepEqual(result.failures, ["poll"]);
  assert.deepEqual(stageNames(result), ["preflight", "poll", "drive-pending", "auto-approve", "delivery"]);
  assert.ok(uvCalls.some((args) => args[3] === "state.postgres.job_cli" && args[4] === "claim"));
});

test("分类失败按 items 逐项聚合计入判据", async () => {
  const { runner } = router({ pollClassificationFailed: 2 });
  const result = await runBusinessCycle({
    commandRunner: runner, spawnImpl: spawnFake(), runId: "poll-2",
  });
  assert.equal(result.status, "failed");
  assert.deepEqual(result.failures, ["poll"]);
});

test("poll 输出缺字段失败关闭且不阻断后续", async () => {
  const { runner } = router({ pollMissingField: true });
  const result = await runBusinessCycle({
    commandRunner: runner, spawnImpl: spawnFake(), runId: "poll-3",
  });
  assert.equal(result.status, "failed");
  assert.deepEqual(result.failures, ["poll"]);
  assert.equal(stageNames(result).length, 5);
});

test("worker 子进程失败计入判据", async () => {
  const { runner } = router({ episodes: [{ id: 1, guid: "g1" }] });
  const result = await runBusinessCycle({
    commandRunner: runner,
    spawnImpl: spawnFake({ taskExit: 1, taskText: "boom" }),
    limit: 2, parallel: 1, runId: "worker-1",
  });
  assert.equal(result.status, "failed");
  assert.deepEqual(result.failures, ["drive-pending"]);
});

test("第二轮 publish 失败计入判据", async () => {
  const { runner } = router({ secondPublishFailed: 1 });
  const result = await runBusinessCycle({
    commandRunner: runner, spawnImpl: spawnFake(), runId: "pub-1",
  });
  assert.equal(result.status, "failed");
  assert.deepEqual(result.failures, ["delivery"]);
});

test("审批 skipped 计入失败判据", async () => {
  const { runner } = router({ approveSkipped: 2 });
  const result = await runBusinessCycle({
    commandRunner: runner, spawnImpl: spawnFake(), runId: "mix-1",
  });
  assert.equal(result.status, "failed");
  assert.deepEqual(result.failures, ["auto-approve"]);
});

test("单轮金额上限超限：阶段间硬门判失败并停止后续阶段", async () => {
  process.env.PODCAST_CYCLE_MAX_USD = "0.000001";
  try {
    let polled = false;
    const { runner, uvCalls } = router();
    const countingRunner: typeof runner = async (command, args, opts) => {
      if (args[3] === "discover.cli") polled = true;
      if (args[3] === "state.ledger_cli" && args[4] === "totals") {
        const usd = polled ? 1 : 0; // poll 之后新增 1 USD
        return {
          exitCode: 0,
          stdout: JSON.stringify({ status: "success", data: {
            success_calls: usd, success_total_usd: usd, open_calls: 0, open_total_usd: 0, by_status: {},
          } }),
          stderr: "",
        };
      }
      return runner(command, args, opts);
    };
    const result = await runBusinessCycle({
      commandRunner: countingRunner, spawnImpl: spawnFake(), runId: "budget-1",
    });
    assert.equal(result.status, "failed");
    assert.deepEqual(result.failures, ["budget"]);
    // 硬门生效：poll 之后不再派发新工作。
    assert.equal(uvCalls.some((args) => args[3] === "state.postgres.job_cli" && args[4] === "claim"), false);
  } finally {
    delete process.env.PODCAST_CYCLE_MAX_USD;
  }
});

test("在途预留计入单轮额度：open_total 超限同样触发硬门", async () => {
  process.env.PODCAST_CYCLE_MAX_USD = "0.5";
  try {
    let polled = false;
    onPollInvoked = () => { polled = true; };
    routerTotals = () => (
      // preflight 快照为零；poll 之后出现 3 笔 unknown 在途预留共 0.9 USD。
      polled
        ? { success_calls: 0, success_total_usd: 0, open_calls: 3, open_total_usd: 0.9, by_status: { unknown: 3 } }
        : { success_calls: 0, success_total_usd: 0, open_calls: 0, open_total_usd: 0, by_status: {} }
    );
    const { runner } = router();
    const result = await runBusinessCycle({
      commandRunner: runner, spawnImpl: spawnFake(), runId: "budget-2",
    });
    assert.equal(result.status, "failed");
    assert.deepEqual(result.failures, ["budget"]);
  } finally {
    routerTotals = undefined;
    onPollInvoked = undefined;
    delete process.env.PODCAST_CYCLE_MAX_USD;
  }
});

test("暂停标记存在时整轮拒绝", async () => {
  const dir = mkdtempSync(join(tmpdir(), "cycle-pause-"));
  writeFileSync(join(dir, "business.pause"), "paused");
  process.env.PODCAST_BUSINESS_PAUSE_FILE = join(dir, "business.pause");
  try {
    const { runner, uvCalls } = router();
    await assert.rejects(runBusinessCycle({
      commandRunner: runner, spawnImpl: spawnFake(), runId: "pause-1",
    }), /业务暂停标记存在/);
    assert.deepEqual(uvCalls, []);
  } finally {
    delete process.env.PODCAST_BUSINESS_PAUSE_FILE;
    rmSync(dir, { recursive: true, force: true });
  }
});

test("共享锁竞争失败返回 locked，零命令", async () => {
  const { runner, uvCalls } = router();
  const result = await runBusinessCycle({
    commandRunner: runner, spawnImpl: spawnFake({ lock: false }), runId: "lock-1",
  });
  assert.equal(result.status, "locked");
  assert.deepEqual(uvCalls, []);
});

test("redactText 遮蔽整段查询串、S3v4 签名、多 Cookie 与 Bearer", () => {
  const s3 = "GET https://bucket.r2.cloudflarestorage.com/episodes/a.mp3?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=deadbeef&X-Amz-Credential=AKIA 失败";
  const redactedS3 = redactText(s3);
  assert.equal(redactedS3.includes("deadbeef"), false);
  assert.equal(redactedS3.includes("AKIA"), false);
  assert.equal(redactedS3.includes("?<redacted>"), true);
  const multiCookie = "cookie: session=secret123; csrf=token456; theme=dark";
  const redactedCookie = redactText(multiCookie);
  assert.equal(redactedCookie.includes("secret123"), false);
  assert.equal(redactedCookie.includes("token456"), false);
  assert.equal(redactedCookie.includes("theme=dark"), false);
  assert.equal(redactText("Authorization: Bearer abc.def.ghijklmno").includes("abc.def"), false);
  const long = "x".repeat(500);
  assert.ok(redactText(long, 200).length <= 200);
});

test("派发边界额度门：claim 前超限即停止新增派发", async () => {
  process.env.PODCAST_CYCLE_MAX_CALLS = "1";
  try {
    let polled = false;
    const { runner, uvCalls } = router({ episodes: [{ id: 1, guid: "g1" }, { id: 2, guid: "g2" }] });
    const countingRunner: typeof runner = async (command, args, opts) => {
      if (args[3] === "discover.cli") polled = true;
      if (args[3] === "state.ledger_cli" && args[4] === "totals") {
        // poll 之后累计 2 次调用，超过 max_calls=1。
        const calls = polled ? 2 : 0;
        return {
          exitCode: 0,
          stdout: JSON.stringify({ status: "success", data: {
            success_calls: calls, success_total_usd: 0, open_calls: 0, open_total_usd: 0, by_status: {},
          } }),
          stderr: "",
        };
      }
      return runner(command, args, opts);
    };
    const result = await runBusinessCycle({
      commandRunner: countingRunner, spawnImpl: spawnFake(), limit: 5, parallel: 1, runId: "gate-claim",
    });
    assert.equal(result.status, "failed");
    assert.ok((result.failures as string[]).includes("budget"));
    // 派发边界门生效：claim 一次都不会发生。
    assert.equal(uvCalls.some((args) => args[3] === "state.postgres.job_cli" && args[4] === "claim"), false);
  } finally {
    delete process.env.PODCAST_CYCLE_MAX_CALLS;
  }
});

test("describes 分类上限作为派发边界传递给 poll", async () => {
  process.env.PODCAST_CYCLE_MAX_CALLS = "7";
  try {
    const { runner, uvCalls } = router();
    await runBusinessCycle({ commandRunner: runner, spawnImpl: spawnFake(), runId: "cap-1" });
    const pollCall = uvCalls.find((args) => args[3] === "discover.cli" && args[4] === "poll");
    assert.ok(pollCall);
    const index = pollCall!.indexOf("--max-classify");
    assert.ok(index >= 0);
    assert.equal(pollCall![index + 1], "7");
  } finally {
    delete process.env.PODCAST_CYCLE_MAX_CALLS;
  }
});

test("locked 与 preflight_failed 早退也带 failures 列表", async () => {
  const locked = await runBusinessCycle({
    commandRunner: router().runner, spawnImpl: spawnFake({ lock: false }), runId: "lock-2",
  });
  assert.equal(locked.status, "locked");
  assert.ok(Array.isArray(locked.failures));
  const pre = await runBusinessCycle({
    commandRunner: router({ preflightFail: "pg" }).runner, spawnImpl: spawnFake(), runId: "pre-3",
  });
  assert.equal(pre.status, "preflight_failed");
  assert.deepEqual(pre.failures, ["preflight"]);
});

test("uvJson 拒绝成功但 data 非 object 的 envelope", async () => {
  const base = router();
  const badEnvelope: typeof base.runner = async (command, args, opts) => {
    if (args[3] === "state.postgres.job_cli" && args[4] === "stats") {
      return { exitCode: 0, stdout: JSON.stringify({ status: "success", data: [] }), stderr: "" };
    }
    return base.runner(command, args, opts);
  };
  const result = await runBusinessCycle({
    commandRunner: badEnvelope, spawnImpl: spawnFake(), runId: "env-1",
  });
  assert.equal(result.status, "preflight_failed");
});

test("poll 条目缺 classification_failed 失败关闭", async () => {
  const base = router();
  const badItems: typeof base.runner = async (command, args, opts) => {
    if (args[3] === "discover.cli") {
      return {
        exitCode: 0,
        stdout: JSON.stringify({ status: "success", data: {
          feeds: 1, new_candidates: 1, enqueued: 1, failed: 0, classification_failed: 0,
          classify_deferred: 0,
          items: [{ feed_id: 1, status: "ok" }],
        } }),
        stderr: "",
      };
    }
    return base.runner(command, args, opts);
  };
  const result = await runBusinessCycle({
    commandRunner: badItems, spawnImpl: spawnFake(), runId: "poll-4",
  });
  assert.equal(result.status, "failed");
  assert.deepEqual(result.failures, ["poll"]);
});

test("dry run 计划仍展示单轮额度上限", async () => {
  const result = await runBusinessCycle({
    commandRunner: router().runner, spawnImpl: spawnFake(), dryRun: true, runId: "dry-2",
  });
  const plan = result.plan as Array<Record<string, unknown>>;
  assert.equal(typeof plan[0].max_calls, "number");
  assert.equal(typeof plan[0].max_usd, "number");
});

test("估算失败的 Episode 不入队，并计入 drive-pending 失败", async () => {
  const base = router({ episodes: [{ id: 7, guid: "g7" }] });
  const noEstimate: typeof base.runner = async (command, args, opts) => {
    if (args[3] === "pipeline.episode_flow" && args[4] === "pending") {
      return {
        exitCode: 0,
        stdout: JSON.stringify({ status: "success", data: {
          items: [{ id: 7, guid: "g7", estimate_error: "缺少估算配置: PODCAST_ESTIMATE_VERSION" }],
        } }),
        stderr: "",
      };
    }
    return base.runner(command, args, opts);
  };
  const result = await runBusinessCycle({
    commandRunner: noEstimate, spawnImpl: spawnFake(), limit: 2, parallel: 1, runId: "est-1",
  });
  assert.equal(result.status, "failed");
  assert.deepEqual(result.failures, ["drive-pending"]);
  const stages = result.stages as Array<{ stage: string; summary?: Record<string, unknown> }>;
  const pendingStage = stages.find((stage) => stage.stage === "drive-pending");
  assert.equal(pendingStage?.summary?.estimate_failures, 1);
  // 无估算时不得创建 job（否则幂等冲突会把 Episode 卡死）。
  assert.equal(base.uvCalls.some((args) => args[4] === "enqueue-episode"), false);
});
