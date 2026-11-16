# runner — 执行层骨架

所属分层：执行层。职责：封装 Pi Durable 主 Agent 与子代理任务执行。

付费模型入口由 `src/model-budget.ts` 统一保护：当前仅验收 `deepseek/openai-completions`，其他 Provider/API 组合失败关闭；使用模型目录的 context/output token 上限及费率最高档估算，原始和 simple 请求均显式设置 maxTokens。要求 `PODCAST_MODEL_PRICE_MODELS` 精确列出允许模型，且 `PODCAST_MODEL_PRICE_SOURCE` 为 HTTPS、`PODCAST_MODEL_PRICING_DATE` 在 30 日内；缺失或模型/价格不匹配时不请求供应商。每次请求先预算写 intent；子代理另在请求前原子单次 claim；响应后核销 usage，SDK 缺失 usage 时保留全部预留，不以默认零值核销；结果未知禁止同一 call_ref 重发。provider 与 agent 内部自动重试关闭。预算模式下关闭自动 compaction，手动 compaction 的 call_ref 忽略重建时生成的 message timestamp。

drive-pending 在实际调度前检查业务暂停标记（`PODCAST_BUSINESS_PAUSE_FILE`，默认 `var/business.pause`）并竞争共享 flock（`PODCAST_BUSINESS_LOCK_FILE`，默认 `var/business.lock`），暂停或锁竞争失败时不触碰队列；心跳 `updated=false` 视为失租，在途任务自然结束后停止新增领取。子代理派发同样受暂停标记约束。过期 unsafe 任务由队列侧转 parked/unknown，不被其他 worker 重放。

`scripts/business-cycle.sh`（源 `src/business-cycle.ts`）是 cron 业务循环入口：前置健康/暂停/预算一致性检查（PG、SQLite 账本、Redis 闸门）失败关闭；poll --classify → drive-pending → auto-approve-due → delivery 顺序执行；阶段失败记录后继续独立阶段，feed 失败、分类失败、worker 失败、审批 skipped、发布失败全部计入最终非零退出；`--dry-run` 只打印计划；单轮 limit≤20、并行≤4；日志与汇总经 redactText 去除签名查询串。本入口持有共享锁，下游驱动经 token 复用。

Pi Durable 集成（submit / resume / checkpoint）、Runner 接口、工具封装、checkpoint 与恢复入口。
