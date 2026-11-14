# 播客翻译 Agent — 主线与支线全景图

- 日期：2026-11
- 定位：项目全景索引。主线只列节目数据流，支线列横切机制；当前状态、运行入口与恢复证据统一见 `docs/HANDOFF.md`
- 需求 `docs/REQUIREMENTS.md`；架构 `docs/ARCHITECTURE.md`；实施计划 `docs/IMPLEMENTATION_PLAN.md`；表结构 `docs/DATA_MODEL.md`；接口 `docs/API_REFERENCE.md`
- 高并发专项范围：`docs/HIGH_CONCURRENCY_REQUIREMENTS.md`；实测：`docs/PERFORMANCE_REPORT.md`
- 状态变化时更新主线／支线当前状态与阻塞；历史交接不再另建多份文档

## 状态标记约定

| 标记 | 含义 |
| --- | --- |
| ✅ | 完成，有测试或实验证据 |
| 🟡 | 部分就绪，仍有剩余项 |
| ⛔ | 被外部条件阻塞 |
| ⬜ | 未开始，按阶段留待 |

## 主线

主线 = 发现 → 下载 → 转写 → 词级重组 → 翻译与复核 → 审核箱 → 私人播客库。

| 环节 | 职责 | 归属分层与目录 | 当前状态 | 验收入口 |
| --- | --- | --- | --- | --- |
| 发现播客 | 条件轮询、历史基线、guid+feed 去重、分类过滤、新期入队 | `discover/` + `runner/` | ✅ Episode 2 真实自动全链通过；VPS 已部署，业务 cron 待授权开启 | `docs/HANDOFF.md`、`tests/test_discover.py` |
| 下载 | 302、hash／大小／时长固化、首版保护、重试 | `pipeline/download.py`、`pipeline/episode_flow.py` | ✅ | `tests/test_download.py`、`tests/test_episode_flow.py` |
| 转写 | Scribe v2 说话人分离、词级时间戳、原始响应落盘与恢复 | `pipeline/scribe.py` + Runner unsafe | ✅ 真实转写与显式 re-bill 恢复通过 | `docs/verification/scribe-idempotency-doc-review.md`、`docs/verification/scribe-idempotency-controlled-test.md` |
| 词级重组 | 词级 JSON → 英文 SRT + provenance | `pipeline/wordrender.py` | ✅ | `tests/test_wordrender.py` |
| 翻译与复核 | skill 全流程、术语、翻译、复核、重切分、provenance | `pipeline/orchestrate.py` + `runner/` + `skills/translate-srt/` | ✅ 样本期与 Episode 2 均已验证 | `docs/verification/mvp-cost-baseline.md`、`docs/HANDOFF.md` |
| 审核箱 | 多期待审、双语对照、override、人工／超时审批、驳回与保留待审 | `review/` | ✅ 审核、修订、权限与已交付过滤均通过 | `docs/HANDOFF.md`、`tests/test_review_service.py` |
| 私人播客库 | 已发布节目、音频、同步字幕、看点、SRT 下载、发布后修订、R2 动态签名 | `delivery/` | ✅ Episode 1 与 Episode 2 已实际发布 | `docs/HANDOFF.md`、`tests/test_delivery.py` |

## 支线

支线保障正确性、成本、质量和长期运行，不是所有支线都需要高并发指标。

| 支线 | 职责 | 当前状态 | 证据位置 |
| --- | --- | --- | --- |
| 不重复计费 | call_ref、intent→outcome、unknown 对账，禁止盲目重发 | ✅ 已查证、实测并在真实恢复中使用 | `docs/verification/scribe-idempotency-doc-review.md`、`docs/verification/scribe-idempotency-controlled-test.md`、`state/ledger.py` |
| 崩溃恢复与调度 | checkpoint、safe/unsafe、parked、租约、心跳、按需唤醒 | ✅ 本地队列与恢复通过；长期生产故障待验收 | `docs/verification/flow-test.md`、`state/postgres/job.py`、`runner/src/drive-pending.ts` |
| 成本账本 | 用量、金额估算、期数归属、unknown 与只读管理页 | ✅ 账本与管理页完成；R2 费用由 Cloudflare 独立核对 | `state/ledger.py`、`state/ledger_report.py`、`docs/API_REFERENCE.md` |
| 领域知识库 | 英超译名、术语、ASR 变体、volatile 重查、人工沉淀 | 🟡 最小底座已 init，沉淀仍待演练 | `docs/verification/translate-srt-reuse.md`、`.agent/knowledge-base.md` |
| override 机制 | 人工修改持久化、重跑人工优先、冲突阻断 | ✅ 保存、合并、驳回与发布后修订通过；真实付费局部重跑待受控验收 | `docs/HANDOFF.md`、`pipeline/override.py`、`review/service.py` |
| 评测 | 后续从 override 历史生成样本，追踪质量与工作量 | ⬜ 明确后置，不阻塞当前版本 | `docs/REQUIREMENTS.md`·评测 |
| 框架隔离 | ToolSpec 中立契约与 Runner 薄封装，隔离实验 API | ✅ | `runner/src/tool-spec.ts`、`runner/src/durable-adapter.ts`、`docs/verification/flow-test.md` |
| 部署运维 | VPS、cron、凭据、私有访问、备份、磁盘与操作手册 | 🟡 VPS 基础部署、systemd 长驻服务、数据迁移、SSH 隧道、本机备份与同机恢复演练已完成；监控、HTTPS、异地副本和业务 cron 待做 | `docs/TECH_STACK.md`、`docs/HANDOFF.md` |
| 分类过滤 | 只按标题／简介判断 Chelsea 相关性，独立账本，入队前过滤 | ✅ mock 与真实分类入队通过；价格／命中率继续观察 | `discover/classify.py`、`tests/test_classify.py` |
| 多用户与高并发 | PostgreSQL 队列、多 worker、Redis 闸门、授权、预算与本地压测 | ✅ 核心链路已实现并有本地证据；VPS 回归通过，生产压测与真实供应商校准待做 | `docs/HIGH_CONCURRENCY_REQUIREMENTS.md`、`docs/PERFORMANCE_REPORT.md`、`state/postgres/` |

## 投入原则

- 主线是业务数据流，支线决定系统能否长期正确、可控成本和可追踪质量。
- 防重复付费、崩溃恢复、override 和私有交付优先级高于继续打磨页面。
- 高并发只投入任务队列、供应商限流／预算和多用户只读访问，不为所有功能设置 QPS 指标。
- 本地 mock 结果与真实供应商／生产结果必须分开记录。
- 历史交接合并到 `docs/HANDOFF.md`，实验证据留在 `docs/verification/`，不重复创建过期交接文档。

## 当前阻塞清单

| 阻塞项 | 状态 | 影响 | 处理入口 |
| --- | --- | --- | --- |
| 生产运维收尾 | 🟡 基础部署与同机恢复已完成 | 监控、HTTPS、强认证、异地副本、生产压测与业务 cron | `docs/TECH_STACK.md`、`docs/HANDOFF.md` |
| 备份恢复 | ✅ 本机自动备份与同机恢复演练已通过 | 当前无异地副本，不覆盖整机丢失恢复或全时点事务快照 | `docs/HANDOFF.md`、`docs/TECH_STACK.md` |
| 需求修订（Scribe 无服务端幂等键） | 待确认是否回填既有需求 | 防止把本地 call_ref 误认为供应商幂等 | `docs/verification/scribe-idempotency-controlled-test.md` |
| 真实供应商长期限流校准 | ⬜ 待生产小流量验收 | 本地速率不代表账号真实配额 | `docs/REDIS_COORDINATION.md`、`docs/PERFORMANCE_REPORT.md` |
