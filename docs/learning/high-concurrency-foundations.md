# 本项目高并发基础：从一次 Episode 任务开始

- 定位：给没有任务调度型高并发经验的读者，解释本项目为什么这样设计
- 前置要求：只需要基本的 HTTP、SQL 和进程概念，不要求先学 Kafka、Kubernetes 或 Redis Cluster
- 阅读方法：先看一次任务的完整过程，再回到每项技术的优缺点
- 实现与验收细节：`docs/HIGH_CONCURRENCY_REQUIREMENTS.md`
- 重要边界：本文解释架构与学习实验，不代表生产 QPS、供应商吞吐或长期稳定性已经验收

## 先理解本项目的高并发类型

电商秒杀更常见的是：

```text
大量用户
→ 同时读取商品
→ 同时下单
→ 同时竞争库存
```

本项目更常见的是：

```text
多期节目同时待处理
→ 多个 worker 同时领取任务
→ 同时调用有速率、并发和费用限制的外部服务
→ 某些 worker 可能中途崩溃
→ 系统必须知道哪些任务能恢复、哪些付费结果不能重发
```

所以本项目的主要问题不是“热点商品如何缓存”，而是：

- 同一任务会不会被两个 worker 同时执行
- worker 崩溃后任务会不会永远卡住
- 外部付费调用是否会超过速率、并发或预算
- 请求已经发出但结果未知时，能否安全恢复
- 多个用户同时读取时，会不会看到别人的私有内容

可以把本项目称为**任务调度型、资源约束型并发**。

## 并发、并行、QPS 和吞吐不是一回事

| 概念 | 含义 | 本项目的例子 |
| --- | --- | --- |
| 并发 | 同一时间段内有多个工作进行 | 多个 Episode 同时处于执行中 |
| 并行 | 多个工作真正同时占用计算资源 | 两个 Episode 子进程同时运行 |
| QPS | 每秒完成或接收的请求数 | 管理 API 每秒响应多少次只读请求 |
| 任务吞吐 | 单位时间完成多少任务 | 每秒完成多少 fake 队列任务 |
| 延迟 | 一次操作等待多久 | 领取一个任务的 p95 延迟 |
| 在途数量 | 当前尚未结束的工作数量 | 当前正在运行的 Scribe 调用数 |

一次 Scribe 调用可能持续很久。即使 QPS 很低，也可能积累很多在途任务，因此不能只看 QPS。

## 一次 Episode 的完整并发链路

```text
发现 Episode
→ PostgreSQL job 幂等入队
→ worker 原子领取，领取前检查预算与 running 上限
→ 写入 running、租约和 job_attempt
→ 有界 Episode 子进程开始执行
→ 定期 heartbeat 续租
→ 付费调用前获取 Redis 速率与并发许可
→ 写入账本 intent
→ 调用 Scribe／模型
→ 写入 success／failed／unknown
→ 释放 Redis owner
→ 完成 job，或对失败任务进行有界处理
```

这条链路里有多层保护，每层解决的问题不同：

| 层次 | 主要保护对象 |
| --- | --- |
| 幂等键 | 防止重复入队 |
| PostgreSQL 行锁 | 防止重复领取 |
| 有界 worker | 防止本机进程、内存和连接无限增加 |
| 任务租约 | 防止 worker 崩溃后任务永久卡住 |
| Redis 闸门 | 防止供应商速率与并发超限 |
| PostgreSQL 预算 | 防止金额和用户／Episode running 超限 |
| 账本与 unknown | 防止结果不明时盲目重复付费 |
| owner 校验 | 防止过期 worker 覆盖新 worker 的结果 |

## 为什么使用 PostgreSQL 做任务队列

核心代码：`state/postgres/job.py`、`state/postgres/job_cli.py`。

任务不是只放在内存里，而是写入 `job` 表。每次执行另写 `job_attempt`。

优点：

- 服务重启后任务仍存在
- 可以使用事务、唯一约束、行锁和 SQL 查询
- 任务状态、预算、用户授权和尝试历史可以使用一致的数据契约
- 当前单机规模不需要再维护一个消息队列服务

代价：

- 极高频任务事件会增加数据库写入与锁竞争
- 领取通常需要主动查询，不是专业消息队列的完整推送生态
- 如果未来达到更大规模，需要测量索引、连接数、表膨胀和数据库瓶颈

### 与其他方案的差别

| 方案 | 更适合什么 | 当前为什么不选 |
| --- | --- | --- |
| 内存队列 | 单进程、允许重启丢任务 | 无法满足持久化与多进程恢复 |
| Redis 列表／Stream | 较轻量的任务分发与事件 | 当前预算、授权和任务事务已经在数据库，Redis 不作为权威状态 |
| RabbitMQ | 通用消息路由、确认和消费组 | 当前规模不需要额外 broker 与路由体系 |
| Kafka | 高吞吐事件日志、流处理和多订阅者 | 本项目不是事件流分析，引入它会增加理解与运维成本 |
| PostgreSQL | 有事务要求的持久化任务 | 与当前规模和数据约束匹配 |

不是 PostgreSQL 永远更好，而是它适合当前“任务量有界、状态正确性优先”的阶段。

## 幂等入队为什么需要唯一约束

自动 Episode 使用类似下面的幂等键：

```text
episode:{episode_id}:auto:v1
```

重复发现同一期时，数据库执行：

```sql
INSERT ... ON CONFLICT (idempotency_key) DO NOTHING
```

这意味着重复请求不会创建第二条有效任务。

### 幂等不等于只执行一次

系统仍可能出现：

- worker 领取后崩溃
- 外部请求已经发出但客户端没收到响应
- 结果写盘后数据库提交失败
- 同一任务到期后被重新领取

所以更准确的描述是：

> 至少一次调度，加上唯一约束、版本校验、账本和结果对账，尽量避免重复业务效果。

不能直接宣称分布式 Exactly Once。

## `SKIP LOCKED` 如何避免重复领取

假设有两个 worker 同时寻找任务：

```text
worker A：选中 job 10，并锁住这一行
worker B：也扫描到 job 10，但发现它被锁住，跳过并寻找下一项
```

核心 SQL：

```sql
SELECT * FROM job
WHERE ...
ORDER BY priority DESC, id
FOR UPDATE SKIP LOCKED
LIMIT 1
```

优点：

- 锁竞争处理由 PostgreSQL 完成
- 不需要自己维护“哪个任务正在被谁领取”的内存表
- 多个 worker 可以共享同一个持久化队列

代价：

- 数据库成为协调点
- 极高并发下需要关注锁、索引、事务长度和连接数
- 只解决领取互斥，不解决外部付费结果是否已经产生

领取事务必须短，不能在整个 Scribe 调用期间一直持有数据库行锁。

## 为什么还需要租约和心跳

如果 worker 领取任务后进程消失，数据库里的 running 不能永远保留。

当前任务记录：

```text
locked_by      当前 worker
locked_until   租约到期时间
attempts       已执行次数
```

worker 定期 heartbeat 延长 `locked_until`。如果 worker 崩溃，其他 worker 等租约到期后才能接管。

### 为什么不能崩溃后立即重试

因为“进程没有回应”不等于“外部调用没有发生”。

例如：

```text
Scribe 已收到音频
→ 网络响应中断
→ 本地 worker 崩溃
```

立即重发可能产生第二次费用。因此租约接管只恢复调度能力，是否重新调用供应商还要查询账本、transcription_id、既有子任务和产物。

### 为什么完成时还要校验 owner

旧 worker 可能在租约到期后仍然活着：

```text
worker A 租约过期
→ worker B 接管
→ worker A 又从停顿中恢复并尝试提交
```

complete／fail 会校验任务仍是 running、worker 匹配且租约未过期，防止旧 worker 覆盖新结果。

## 有界 worker 和背压

实际入口：`runner/src/drive-pending.ts`。

当前不是常驻无限扩容的 worker 集群，而是按需启动的一组有界执行者：

- 默认并行度为 2，代码限制为 1–4
- 每个 worker 执行一个 Episode 子进程
- 每轮领取数量有上限
- 队列清空后退出

如果任务增加而 worker 数量不变，结果是任务留在 PostgreSQL 等待，而不是本机无限创建进程。

这就是背压：

> 下游处理能力不足时，把压力保留在可持久化的队列里，而不是继续向下游发送。

### 背压与降级不同

- 背压：暂缓接收或执行，保留任务，等待恢复
- 降级：减少功能、返回简化结果或拒绝部分请求

本项目任务执行主要使用背压；付费协调故障使用失败关闭，不能绕过限制继续调用。

## Redis 为什么既限速率又限并发

核心代码：`pipeline/provider_gate.py`。

### 令牌桶限制速率

桶里按时间补充令牌：

```text
经过时间 × rate = 新增令牌
令牌总数最多为 burst
调用一次消耗一个令牌
没有令牌则返回 retry_after_ms
```

例如 rate 很低、burst 为 1，可以防止多个 worker 在同一时刻同时发起调用。

### 并发租约限制在途长任务

即使速率很低，长任务也可能积累。因此另有：

```text
owner → 租约到期时间
```

只有当前活跃 owner 数低于上限，才能进入。

### 两类租约不能混淆

| 类型 | 存在哪里 | 保护什么 |
| --- | --- | --- |
| PostgreSQL 任务租约 | `job.locked_by / locked_until` | 哪个 worker 可以执行和提交这个 job |
| Redis 供应商租约 | `<provider>:leases` | 当前有多少个外部供应商调用仍在执行 |

任务完成不代表可以删除其他 worker 的 Redis 租约；Redis 释放成功也不代表数据库任务已经完成。

## 为什么使用 Lua 和 Redis TIME

如果限流由多条命令组成：

```text
GET tokens
计算新 tokens
SET tokens
```

两个 worker 可能同时读取相同值，然后都扣减，造成超发。

Lua 把读取、补充、扣减和写回放在 Redis 内原子执行。

Redis TIME 提供统一时间源，避免不同 worker 本机时钟略有差异。

优点：

- 算法跨进程共享
- 原子操作减少竞态
- 令牌桶和并发租约可以统一协调

代价：

- Redis 成为付费调用的前置依赖
- Lua 脚本需要测试边界与错误处理
- 当前是单 Redis 实例，不代表集群容灾已经完成

## 为什么 Redis 故障要失败关闭

本项目不能采用下面的“降级”：

```text
Redis 不可用 → 暂时不限制 → 直接调用付费供应商
```

这样可能超预算或超过供应商配额。

当前选择：

```text
Redis 不可用
→ acquire 失败
→ 不创建新的付费 intent
→ 不发外部请求
→ 等 Redis 恢复后从持久化任务继续
```

这是付费安全优先于可用性的刻意取舍。

## 预算为什么在领取任务时检查

核心代码：`state/postgres/budget.py`。

如果先执行任务再检查费用，超限已经发生。当前在任务进入 running 前检查：

- 全局预算
- 用户周期预算
- Episode 预算
- 单任务预算
- 对应范围的 max_running

超过金额或成本未知时失败关闭。达到 running 上限时改为稍后 retry。

预算拦截不会增加 attempts，也不会创建 job_attempt，因为任务并未真正执行。

### 金额为什么有两个来源

账本可能记录：

- `amount_est`：按供应商单价和用量估算
- `usage_basis` 中的 `cost_usd`：模型返回的成本字段

预算读取会考虑两者。没有可靠预估时，不能把成本当成 0。

## Fastify + 长驻 FastAPI 解决什么问题

早期方式可能是：

```text
每个管理请求
→ 启动一个 Python 进程
→ 加载依赖
→ 执行 CLI
→ 退出
```

请求增加时，进程启动成本和内存占用会明显增加。

当前方式：

```text
Fastify 接收请求
→ 内部 HTTP RPC
→ 长驻 FastAPI
→ Python 领域逻辑
```

优点：

- Python 解释器和依赖只加载一次
- Fastify 继续负责 HTTP、Cookie、静态页和同源保护
- 审核与交付状态机仍只在 Python 领域层实现一次

代价：

- 多一个长驻进程和内部 RPC
- 需要内部令牌、超时、健康检查和 systemd 运维
- 两个服务需要一致的环境配置

长驻进程不等于长连接，也不等于使用数据库连接池。当前数据库连接仍按请求或任务关闭。

## 为什么当前保留 SQLite + PostgreSQL

当前实际边界：

| 存储 | 当前职责 |
| --- | --- |
| SQLite | Episode、产物、账本、审核、审批、交付的当前业务主存储 |
| PostgreSQL | 队列、尝试、账号、会话、授权、预算、审计和业务对账副本 |
| Redis | 令牌桶、并发租约、登录限流等短期协调数据 |

优点：

- 保留已经真实验收的 Episode 业务链
- 队列和多用户可以先独立落地
- 避免一次性重写全部业务带来的风险

代价：

- SQLite 与 PostgreSQL 的 SQL 写法存在差异
- 一次业务操作不一定能跨两个数据库共享同一个事务
- 需要兼容层、迁移对账和明确的权威数据边界

这是当前工程取舍，不是最终必须永远保持的形态。如果真实生产证明 SQLite 写竞争成为瓶颈，再考虑完整迁移，而不是预先假设瓶颈存在。

## 多用户高并发为什么还要讲权限

多用户同时读取时，性能正确但越权仍然是失败。

当前机制：

- Argon2id 保存密码哈希
- 数据库保存 session token 的 SHA-256，而不是原始 token
- Cookie 使用 HttpOnly 和 SameSite
- 普通用户通过 resource_grant 过滤 Episode
- 未授权详情返回 404
- 写请求检查同源信息
- R2 签名 URL 在授权成功后生成

当前没有为了列表压测加入权限缓存。权限数据变化快、错误缓存可能导致越权，必须优先保证正确性。

## 为什么暂时不做多级缓存

本项目当前没有电商式热点商品读：

- 私有节目访问人数有限
- 每个用户授权不同
- 当前审核版本与发布指针需要准确
- 已发布媒体由 R2 承载，减少 VPS 带宽压力

缓存不是错误，而是需要证据。只有生产测量显示某个只读查询确实是瓶颈，才值得评估短 TTL 缓存，并同时设计权限与版本失效策略。

## 常见误解

### “使用了 Redis，所以就是高并发”

不成立。组件名称不构成能力，必须有实际竞争场景、原子性、故障行为和压测证据。

### “有队列就能随便重试”

不成立。safe 下载和 unsafe 付费调用的重试语义不同，结果未知必须对账。

### “worker 越多，吞吐越高”

不成立。worker 增多会增加数据库连接、内存、供应商并发和费用压力，超过下游能力后只会增加排队与失败。

### “租约过期说明旧任务已经停止”

不成立。旧进程可能只是停顿，完成时仍要校验 owner 和租约。

### “本地 1000 个 fake 任务很快，生产就能处理 1000 期真实节目”

不成立。fake 压测只证明队列和状态提交能力，真实 Scribe、模型、网络和费用需要独立验收。

### “加了缓存一定更快”

不成立。缓存还有命中率、失效、权限和一致性问题。没有瓶颈证据时，缓存可能只是增加复杂度。

## 安全的本地学习实验

以下命令只在本地开发环境执行，不要在 VPS 生产环境运行。

PostgreSQL 集成测试会清表，DSN 必须指向独立的 `podcast_agent_test`，绝不能指向业务库。

```bash
docker compose up -d postgres redis

# 如果本地还没有测试数据库，先创建一次
# docker compose exec -T postgres psql -U podcast_agent -d podcast_agent \
#   -c 'CREATE DATABASE podcast_agent_test'

PODCAST_TEST_POSTGRES=1 \
PODCAST_POSTGRES_DSN=postgresql://podcast_agent:podcast-dev@127.0.0.1:5432/podcast_agent_test \
uv run pytest tests/test_postgres_job.py -q

PODCAST_TEST_POSTGRES=1 \
PODCAST_POSTGRES_DSN=postgresql://podcast_agent:podcast-dev@127.0.0.1:5432/podcast_agent_test \
uv run pytest tests/test_postgres_budget.py -q

PODCAST_TEST_REDIS=1 \
uv run pytest tests/test_provider_gate.py -q
```

Redis 测试使用随机 key 前缀，不 flush 整个 Redis。测试均不发起真实 Scribe 或模型调用。

## 建议的代码阅读顺序

| 顺序主题 | 代码入口 | 阅读时重点 |
| --- | --- | --- |
| 任务入队与领取 | `state/postgres/job.py` | 唯一约束、事务、行锁、状态变化 |
| worker 实际执行 | `runner/src/drive-pending.ts` | 并行上限、子进程、heartbeat、unknown |
| Redis 闸门 | `pipeline/provider_gate.py` | Lua、TIME、owner、TTL、失败关闭 |
| 预算 | `state/postgres/budget.py` | 检查时机、金额来源、max_running |
| Runner 供应商接缝 | `runner/src/provider-gate.ts` | 真实模型调用如何复用同一闸门 |
| 长驻领域服务 | `review/api.py`、`review/src/python.ts` | 内部 RPC、迁移时机、错误映射 |
| 多用户 | `state/postgres/user.py`、`review/auth_api.py`、`review/src/auth.ts` | 密码、会话、授权、同源保护 |
| 混合存储兼容 | `state/compat.py` | SQLite 与 PostgreSQL 查询差异 |

## 自测问题

读完代码后，可以尝试不看文档回答：

- 为什么唯一约束不能代替租约？
- 为什么 `SKIP LOCKED` 不能防止供应商重复扣费？
- 为什么 Redis 令牌桶之外还需要并发租约？
- 为什么 Redis 故障时不能绕过闸门？
- 为什么预算超限时不能先执行再记录失败？
- 为什么旧 worker 在租约过期后不能直接提交结果？
- 为什么本项目当前不把权限和发布版本放进长期缓存？
- 为什么 fake 队列吞吐不能写成真实节目处理吞吐？

能把这些问题解释清楚，就已经掌握了本项目高并发实现的主要思想。

## 继续阅读

- 完整实现与验收：`docs/HIGH_CONCURRENCY_REQUIREMENTS.md`
- Redis 细节：`docs/REDIS_COORDINATION.md`
- 本地实测：`docs/PERFORMANCE_REPORT.md`
- 数据表：`docs/DATA_MODEL.md`
- 连接、超时、Keep-Alive 与背压：`docs/learning/connection-management.md`
