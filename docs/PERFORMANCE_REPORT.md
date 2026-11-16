# 本地与 VPS 压测及故障验收报告

- 日期：2026-11；VPS 首轮基准执行于 2026-11-13
- 范围：本地开发机与实际部署 VPS 上的 Docker PostgreSQL／Redis、Fastify 和长驻 FastAPI；Scribe、模型和 R2 外部调用均未参与
- 目的：验证并发队列、管理 API 和 Redis 闸门的实际行为，形成可复现的工程证据
- 高并发专项范围：`docs/HIGH_CONCURRENCY_REQUIREMENTS.md`
- 重要边界：所有数据都是 fake 任务或只读请求结果，不是实际节目处理吞吐、真实供应商能力或生产 SLA；VPS 数字也不得替代公网、混合负载和长期运行验收

## 本地环境与限制

- PostgreSQL 16（Docker）
- Redis 7（Docker）
- Fastify + 长驻 FastAPI 领域服务
- 本地 SQLite 业务数据与 PostgreSQL 队列／授权数据
- 所有任务使用 fake payload；不发起 Scribe、DeepSeek 或 R2 写调用
- 压测脚本位于 `.agent/tmp/`，该目录按项目规则不入库

## 本地结果汇总

| 压测对象 | 负载 | 实测吞吐 | 延迟 | 错误 |
| --- | --- | --- | --- | --- |
| PostgreSQL 任务队列 | 1000 个任务、8 个 worker | **1000.77 任务/秒** | 领取 p95 **6.919ms**，平均 **4.735ms** | **0** |
| 管理 API 只读请求 | 300 个请求、16 并发 | **256.13 QPS** | p50 **58.061ms**，p95 **94.247ms**，p99 **114.28ms** | **0** |
| Redis 限流与并发闸门 | 1000 次 acquire/release、32 并发 | **2520.70 次/秒** | p95 **19.454ms**，平均 **12.528ms** | **0** |

## 本地 PostgreSQL 队列详情

- 1000 个任务全部 `completed`
- 创建 1000 条 job_attempt，未出现重复领取
- 8 个 worker 分别完成 122–127 个任务，分布有界且没有单 worker 独占
- 入队耗时 1.350 秒
- 领取、完成及状态提交总耗时 0.999 秒
- 显式提交事务后，关闭连接再查询仍保留任务与尝试记录
- 错误列表为空

这组数据证明的是本地 PostgreSQL 队列的并发领取和状态提交行为，不代表外部模型处理速度。

## 本地管理 API 详情

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

## 本地 Redis 闸门详情

- 使用真实本地 Redis 的 Lua 令牌桶和 Hash owner/TTL 租约
- 1000 次 acquire/release 均成功，没有错误
- 并发上限设置为 32，未出现超发或租约泄漏
- 不产生真实付费调用

这组数据只反映 Redis 协调开销，不证明外部 API 在相同并发下允许或能够完成调用。

## 本地复现命令

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

## VPS 首轮实测

### 环境与口径

- 实际部署实例：腾讯云新加坡轻量服务器，2 核、4 GB、60 GB SSD，Ubuntu 24.04 LTS
- PostgreSQL 16／Redis 7 使用 Docker Compose，Fastify 与 FastAPI 使用 systemd；数据库和管理服务均只监听本机
- 三项测试**顺序执行**，不是混合负载；压测客户端也在 VPS 上，经 `127.0.0.1` 调用，不包含公网、SSH 隧道或跨地域网络延迟
- PostgreSQL 使用独立 `podcast_agent_load_test` 数据库；不触碰生产业务库
- 本轮没有同步采集 CPU、内存、磁盘 IO 或网络曲线，因此不将结果解释为容量上限或资源瓶颈结论
- 队列压测脚本与本地脚本并非完全同一份实现；Redis 有效轮次参数也与本地不同，以下数据不能用于简单同比或线性外推

### 结果汇总

| 压测对象 | 负载 | VPS 实测吞吐 | 延迟 | 错误 |
| --- | --- | --- | --- | --- |
| PostgreSQL 任务队列 | 1000 个 fake 任务、8 个 worker | **43.896 任务/秒** | 领取 p50 **54.164ms**，p95 **77.769ms**，p99 **90.190ms** | **0** |
| 管理 API 只读请求 | 300 个请求、16 并发 | **48.128 QPS** | p50 **325.760ms**，p95 **408.849ms**，p99 **445.707ms** | **0** |
| Redis 限流与并发闸门 | 1000 次 acquire/release、32 并发 | **1512.636 次/秒** | p50 **18.403ms**，p95 **34.754ms**，p99 **78.176ms** | **0** |

### VPS PostgreSQL 队列详情

- 1000 个任务全部 `completed`，1000 条 `job_attempt`，领取计数 1000，错误列表为空
- 入队耗时 **10.453233 秒**，worker 领取／完成耗时 **12.327843 秒**，总计 **22.781076 秒**
- 吞吐 **43.896 任务/秒**包含逐任务入队时间，不是只计算 worker 阶段
- 领取平均延迟 **53.235ms**；测试使用每 worker 的数据库连接和真实 `claim_job`／`complete_job` 路径
- 首个启动尝试因测试数据库尚不存在而停止，未产生有效压测；创建独立测试库后的上述轮次才是有效结果

这组数据证明当前 VPS 上 fake 队列的持久化领取和状态提交可以完成，但不代表真实 Scribe／模型任务的速度。

### VPS 管理 API 详情

请求覆盖：

- `/api/library/episodes`
- `/api/usage`
- `/api/review/episodes`

结果：

- 300 个请求全部 HTTP 200，状态码计数为 `{"200": 300}`
- 平均延迟 **328.583ms**；总耗时 **6.233422 秒**
- 使用一次管理员登录后复用会话 Cookie，登录不在 300 个请求的计时负载内
- 节目库 count 保持 2；没有写入审核、审批、发布或付费账本
- 使用生产部署的 Fastify → 长驻 FastAPI 实际 RPC 路径

p95 与 p99 明显高于本地只读基准，但本轮没有资源采样、分接口拆分或公网请求，不能据此断言单一根因，也不能将 HTTP 200 等同于生产延迟目标已满足。

### VPS Redis 闸门详情

有效轮次使用独立随机 key 前缀，参数明确为：

- `rate_per_second=1000`
- `burst=1000`
- `max_concurrent=64`
- 32 个客户端并发，1000 次 acquire/release 均成功，错误数为 0
- 总耗时 **0.661098 秒**，平均延迟 **20.814ms**
- 测试结束后只删除随机前缀对应 key，不 flush Redis

首次尝试使用较小 burst／并发参数，触发了令牌和并发租约的预期拒绝；该轮不能作为吞吐结果，已废弃。有效轮次只证明协调调用开销，不证明外部供应商允许相同调用速率。

### 与本地结果的关系

| 对象 | 本地结果 | VPS 首轮结果 | 解释边界 |
| --- | --- | --- | --- |
| PostgreSQL 队列 | 1000.77 任务/秒；领取 p95 6.919ms | 43.896 任务/秒；领取 p95 77.769ms | 硬件、连接与脚本实现不同，不做同条件降幅结论 |
| 管理 API | 256.13 QPS；p95 94.247ms | 48.128 QPS；p95 408.849ms | 均为本机回环只读测试，但机器与运行环境不同，需进一步剖析延迟 |
| Redis 闸门 | 2520.70 次/秒；p95 19.454ms | 1512.636 次/秒；p95 34.754ms | 参数不同，只分别记录各自结果 |

VPS 首轮结果提供了真实部署实例上的基线，不是生产 SLA、公网容量或真实业务端到端吞吐。报告同时保留本地和 VPS 数字，不混写成单一性能承诺。

## 仍需验收

- 压测期间同步采集真实 VPS 的 CPU、内存、带宽、磁盘 IO 和连接数
- 真实 Scribe／模型的限流阈值与失败率
- 多用户权限下的混合读写压测、分接口延迟拆解和预算超限行为
- 公网／SSH 隧道访问下的延迟，以及更大请求量和更长持续时间的容量测试
- worker 崩溃、租约接管、Redis／PostgreSQL 短暂不可用后的恢复
- HTTPS、生产强密码和监控告警；本机备份与同机恢复演练已完成，异地副本仍未覆盖

即使已有 VPS 首轮数字，也不把 fake／只读结果写成真实供应商吞吐、生产 SLA 或长期高并发承诺。
