# 本地压测与故障验收报告

- 日期：2026-11
- 范围：本地开发机 + Docker PostgreSQL／Redis；Scribe、模型和 R2 外部调用均未参与
- 目的：验证并发队列、管理 API 和 Redis 闸门的实际行为，形成可复现的工程证据
- 高并发专项范围：`docs/HIGH_CONCURRENCY_REQUIREMENTS.md`
- 重要边界：以下数据是本地 mock／只读测试结果，不是生产环境 QPS、真实供应商吞吐或 SLA

## 环境与限制

- PostgreSQL 16（Docker）
- Redis 7（Docker）
- Fastify + 长驻 FastAPI 领域服务
- 本地 SQLite 业务数据与 PostgreSQL 队列／授权数据
- 所有任务使用 fake payload；不发起 Scribe、DeepSeek 或 R2 写调用
- 压测脚本位于 `.agent/tmp/`，该目录按项目规则不入库

## 结果汇总

| 压测对象 | 负载 | 实测吞吐 | 延迟 | 错误 |
| --- | --- | --- | --- | --- |
| PostgreSQL 任务队列 | 1000 个任务、8 个 worker | **1000.77 任务/秒** | 领取 p95 **6.919ms**，平均 **4.735ms** | **0** |
| 管理 API 只读请求 | 300 个请求、16 并发 | **256.13 QPS** | p50 **58.061ms**，p95 **94.247ms**，p99 **114.28ms** | **0** |
| Redis 限流与并发闸门 | 1000 次 acquire/release、32 并发 | **2520.70 次/秒** | p95 **19.454ms**，平均 **12.528ms** | **0** |

## PostgreSQL 队列详情

- 1000 个任务全部 `completed`
- 创建 1000 条 job_attempt，未出现重复领取
- 8 个 worker 分别完成 122–127 个任务，分布有界且没有单 worker 独占
- 入队耗时 1.350 秒
- 领取、完成及状态提交总耗时 0.999 秒
- 显式提交事务后，关闭连接再查询仍保留任务与尝试记录
- 错误列表为空

这组数据证明的是本地 PostgreSQL 队列的并发领取和状态提交行为，不代表外部模型处理速度。

## 管理 API 详情

请求覆盖：

- `/api/library/episodes`
- `/api/usage`
- `/api/review/episodes`

结果：

- 300 个请求全部 HTTP 200
- 使用一次登录后复用会话 Cookie
- 没有写入审核、审批、发布或付费账本
- 使用 Fastify → 长驻 FastAPI 的实际 RPC 路径，不再每次启动 Python CLI

调试过程中发现旧 Fastify／领域进程使用不同内部令牌，导致 RPC 401；清理本项目残留进程并统一令牌后重新压测。上表只记录修正后的有效结果。

## Redis 闸门详情

- 使用真实本地 Redis 的 Lua 令牌桶和 Hash owner/TTL 租约
- 1000 次 acquire/release 均成功，没有错误
- 并发上限设置为 32，未出现超发或租约泄漏
- 不产生真实付费调用

这组数据只反映 Redis 协调开销，不证明外部 API 在相同并发下允许或能够完成调用。

## 复现命令

### PostgreSQL 队列

```bash
docker compose up -d postgres redis
uv run python -m state.postgres.migrations
PODCAST_POSTGRES_DSN=postgresql://podcast_agent:podcast-dev@127.0.0.1:5432/podcast_agent_load_test \
  uv run python .agent/tmp/load-queue.py
```

脚本会清空 `podcast_agent_load_test` 的 job／job_attempt，禁止将 DSN 指向业务库。

### 管理 API

```bash
# 先启动 Fastify 与长驻 FastAPI，并保证 PODCAST_DOMAIN_TOKEN 一致
uv run python .agent/tmp/load-api.py
```

脚本只发只读请求，登录使用本地演示账号 `owner@example.com`，不写业务数据。

### Redis 闸门

```bash
uv run python .agent/tmp/load-redis.py
```

使用随机 key 前缀，测试结束后可删除该前缀对应的测试 key；不 flush 整个 Redis。

## 仍需验收

- 真实 VPS 的 CPU、内存、带宽、磁盘 IO 和外部网络
- 真实 Scribe／模型的限流阈值与失败率
- 多用户权限下的混合读写压测和预算超限行为
- worker 崩溃、租约接管、Redis／PostgreSQL 短暂不可用后的恢复
- 生产域名、HTTPS、强密码、备份和监控告警

在取得这些证据前，不把本报告中的本地数字写成生产高并发承诺。
