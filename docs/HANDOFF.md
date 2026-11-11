# 项目交接与当前状态

- 日期：2026-11
- 定位：当前状态、验证证据、运行入口和剩余工作的唯一交接索引
- 历史整理：已完成的最小 MVP 清单、MVP 交接和 override 专题交接已合并到本文件；验证证据继续保留在 `docs/verification/`
- 当前阶段：本地真实自动全链已通过；高并发核心链路与本地压测已通过；VPS／生产部署尚未执行

## 当前已验证的系统闭环

```text
RSS 发现与可选 Chelsea 分类
→ Episode 去重入队
→ 下载与首次音频固化
→ Scribe 词级转写
→ 词级重组与英文 SRT
→ 翻译、复核、重切分
→ 审核箱 / 人工修改 / override
→ 人工通过或 12 小时超时审批
→ 模型看点与 R2 私有发布
→ 私人播客库、同步字幕、用量与费用
```

已实际完成并发布：

- Episode 1：《Boss with Jamie Carragher: Xabi Alonso》——审核、模型看点、R2 与本地节目库闭环
- Episode 2：《Spurs earn deserved win at Chelsea after Penders error and Palmer red》——真实分类、下载、转写、翻译、复核、看点、R2 和节目库全链验收；1904 条字幕、5 个有效看点

节目库、登录与权限已实际联调：

- 管理员可访问全部节目
- 普通用户按 Episode 授权，未授权资源返回 404
- 已交付节目不再残留在审核箱
- 发布后修订保留旧当前交付，新版本完整交付后才切换

## 当前代码分层

| 分层 | 当前目录 | 当前能力 |
| --- | --- | --- |
| 发现层 | `discover/` | feed 配置、ETag／304、历史基线、guid+feed 去重、可选 Chelsea 分类 |
| 执行层 | `pipeline/`、`runner/` | 下载、Scribe、词级重组、orchestrate、局部重跑、Runner 派发与调度 |
| 审核层 | `review/`、`state/` | 审核箱、override、审批、保留待审、发布后修订、用量与费用 |
| 交付层 | `delivery/` | 看点校验、R2 上传、动态签名、节目库与发布切换 |
| 数据契约 | `state/`、`state/postgres/` | SQLite 当前业务数据、PostgreSQL 队列／账号／授权、Redis 协调 |
| 学习资料 | `docs/learning/` | 独立后端连接管理学习模块 |

### 高并发专项范围

高并发只覆盖以下三条链路，范围与指标见 `docs/HIGH_CONCURRENCY_REQUIREMENTS.md`：

- 多期任务并发处理：PostgreSQL `job`、`job_attempt`、`SKIP LOCKED`、幂等、租约和恢复
- 供应商限流与预算：Redis 令牌桶、owner/TTL 租约、失败关闭、预算与最大 running
- 多用户只读访问：会话、授权过滤、延迟与状态码验证

实际 `drive-pending` 已接入 PostgreSQL 队列，不再把 SQLite execution_lock 当主队列。其他功能以正确性、安全、质量和恢复为主，不设置高 QPS 指标。

## 本地实测结果

详见 `docs/PERFORMANCE_REPORT.md`；这些是本地 mock／只读结果，不是生产 QPS 或供应商吞吐承诺。

| 对象 | 当前有效结果 |
| --- | --- |
| PostgreSQL 队列 | 1000 个 fake 任务、8 worker；1000.77 任务/秒；领取 p95 6.919ms；零错误 |
| 管理 API | 300 个只读请求、16 并发；256.13 QPS；全部 HTTP 200；p95 94.247ms |
| Redis 闸门 | 1000 次 acquire/release、32 并发；2520.70 次/秒；零错误 |

真实队列与权限测试另已验证：多 worker 不重复领取、预算超限不消耗 attempts、授权过滤生效、会话过期／撤销生效。

## 当前测试状态

- Python：**166 passed，20 skipped**（PostgreSQL／Redis 测试默认需要显式启用本地服务）
- Runner：**28 passed**
- Review：**14 passed**
- translate-srt 的两个 self-test 已通过
- `git diff --check` 已通过

真实 PostgreSQL／Redis 测试使用独立本地测试库或随机 key 前缀，不清空业务库，不发起真实付费调用。

## 运行入口

### 本地开发服务

```bash
docker compose up -d postgres redis
uv run python -m state.postgres.migrations
uv run python -m uvicorn review.api:app --host 127.0.0.1 --port 8788
cd review && npm start
```

Fastify 默认 `127.0.0.1:8787`。多用户模式需要 `PODCAST_MULTIUSER=1`、一致的 `PODCAST_DOMAIN_URL` 和 `PODCAST_DOMAIN_TOKEN`。生产 HTTPS 必须启用 `PODCAST_AUTH_SECURE_COOKIE=1`，不得沿用演示弱密码。

### 定时任务命令

```bash
uv run python -m discover.cli poll --classify
node --experimental-strip-types runner/src/drive-pending.ts
uv run python -m review.cli auto-approve-due
node --experimental-strip-types runner/src/drive-delivery.ts
```

这些命令已具备，但实际系统 cron 尚未安装。无任务时应退出，不启动常驻空转 worker。

### 常用自检

```bash
uv run pytest tests/
cd runner && npm run typecheck && npm test
cd review && npm run typecheck && npm test
uv run python skills/translate-srt/scripts/srt_tools.py --self-test
uv run python skills/translate-srt/scripts/kb_tools.py --self-test
```

## 文档索引

### 当前权威文档

- 需求：`docs/REQUIREMENTS.md`
- 架构：`docs/ARCHITECTURE.md`
- 实施计划：`docs/IMPLEMENTATION_PLAN.md`
- 技术栈：`docs/TECH_STACK.md`
- 当前全景：`docs/ROADMAP.md`
- 表结构与代码入口：`docs/DATA_MODEL.md`
- 接口清单：`docs/API_REFERENCE.md`
- 高并发范围：`docs/HIGH_CONCURRENCY_REQUIREMENTS.md`
- 压测结果：`docs/PERFORMANCE_REPORT.md`
- Redis 实现：`docs/REDIS_COORDINATION.md`

### 验证与实验记录

以下文档承载唯一实验或验收证据，不因清理历史交接而删除：

- `docs/verification/flow-test.md`：Durable、safe/unsafe、checkpoint、kill -9 与恢复
- `docs/verification/subagent-dispatch.md`：子代理嵌套任务、去重与账本接缝
- `docs/verification/scribe-idempotency-doc-review.md`：Scribe 无服务端幂等键的文档查证
- `docs/verification/scribe-idempotency-controlled-test.md`：Scribe 受控调用、计费与恢复证据
- `docs/verification/mvp-cost-baseline.md`：真实 MVP 成本基线
- `docs/verification/translate-srt-reuse.md`：skill 复用边界与自检

### 设计与学习

- `.agent/high-concurrency-architecture.md`：高并发完整设计，按用户决定暂不提交
- `docs/learning/connection-management.md`：独立后端连接管理学习模块

## 真实运行恢复记录

- Episode 2 的首次 Scribe 代理调用发生 RemoteProtocolError，保留 unknown 账本；用户授权后使用显式 `:rebill:2` 直连成功，原 unknown 不覆盖。
- 翻译、复核和看点的 unknown 均凭既有 terminal 子任务、真实产物和用量核销，未盲目重发。
- 模型原稿中超出音频范围的看点已保留原稿备份后人工移除。
- 样本数据库备份：`var/backups/podcast_agent_before_demo_20261109T092309Z.db`、`var/backups/podcast_agent_before_auto_20261109T165308Z.db`。
- 手写测试看点备份：`var/backups/highlights-demo-20261109T092506Z/`。
- 模型原稿备份：`var/backups/episode2-highlights-model-original.json`。

## 剩余工作与当前边界

- VPS、HTTPS、生产强密码、监控告警、备份策略与真实 cron 部署
- 生产环境下重新测量 API、队列、Redis、带宽和故障接管
- 按真实供应商账号配额校准限流，不用本地数字代替供应商能力
- 真实付费局部重跑需要受控验收，当前以 faux 测试为主
- 评测机制继续后置，不把未修改条目自动当成正确样本
- 不做公开再分发、公共注册或无限付费任务
- 高并发专项不扩展到所有页面或所有接口

## Git 纪律

- 代理不自行执行 git commit；用户自行提交或明确批准后由代理执行。
- 提交前实际读取 `git status` 与 `git log --oneline`，按模块内聚提交。
- 不提交 `.env`、`var/`、真实媒体、凭据或 `.agent/tmp/` 临时文件。
- `.agent/high-concurrency-architecture.md` 当前按用户决定暂不提交；`.vscode/` 为个人编辑器配置，不需要提交。
