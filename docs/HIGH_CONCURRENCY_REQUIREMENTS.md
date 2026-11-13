# 高并发技术实现与验收边界

- 状态：三条核心链路已实现，本地指标通过；VPS 基础部署与回归通过，生产压测和长期验收中
- 原则：只选择有真实并发价值、可测试、可写进简历的链路；不是所有功能都需要高并发
- 关联设计：`.agent/high-concurrency-architecture.md`（原始设计稿与实施差异；实际实现以本文为准）
- 实测报告：`docs/PERFORMANCE_REPORT.md`
- 数据表与字段：`docs/DATA_MODEL.md`
- 接口清单：`docs/API_REFERENCE.md`
- 初学者学习入口：`docs/learning/high-concurrency-foundations.md`

## 总目标

> 面向多期内容处理的异步任务系统，支持持久化队列、多 worker 领取、租约恢复、供应商限流、预算控制和用户级资源授权，并用本地基准测试证明任务吞吐、API 延迟和并发安全性。

当前系统仍需区分：

- 本地 fake／只读基准结果
- 真实供应商受控验收
- 生产 VPS 的长期运行能力

本地数字不能直接写成生产 QPS 或供应商吞吐承诺。

## 技术实现总览

本项目没有用“增加一台配置更高的服务器”来定义高并发，而是把并发问题拆成三个边界：

- **执行并发**：多期长任务如何互斥领取、并行执行、限流和恢复
- **付费资源并发**：多个 worker 如何共享供应商配额、运行上限和预算
- **访问并发**：多个用户如何同时读取私有内容，同时保持会话与 Episode 权限隔离

实际运行链路：

```text
RSS / 手动发现
  → Episode 业务记录
  → PostgreSQL job 幂等入队
  → 有界 worker 组领取与心跳
  → Redis 供应商闸门与 PostgreSQL 预算检查
  → 下载 / Scribe / 模型 / 发布
  → 任务结果、尝试历史与业务产物持久化

Browser
  → Fastify 鉴权、Cookie、同源写保护、静态页
  → 长驻 FastAPI 内部 RPC
  → SQLite 业务查询 + PostgreSQL 会话／授权查询
  → R2 私有对象动态签名
```

技术选型：

| 问题 | 实际方案 | 为什么适合当前规模 |
| --- | --- | --- |
| 持久化任务队列 | PostgreSQL `job` / `job_attempt` | 已有事务、唯一约束和恢复语义，不需要额外消息队列 |
| 多 worker 互斥领取 | `FOR UPDATE SKIP LOCKED` | 在数据库内解决竞争，避免两个 worker 执行同一任务 |
| 供应商协调 | Redis TIME + Lua + Hash owner/TTL | 跨进程共享速率与并发，同时保持协调数据可重建 |
| Web 与领域逻辑 | Fastify + 长驻 FastAPI | 避免每个管理请求启动 Python 进程，领域规则仍只实现一次 |
| 多用户 | PostgreSQL 账号、会话、授权、预算和审计 | 权限事实持久化，Redis 故障不会丢失账号与授权 |
| 部署 | 单 VPS、systemd、Docker Compose | 当前负载可解释、可备份、可恢复，不引入 Kubernetes |

### 当前存储边界

当前是刻意保留的混合架构：

- SQLite `var/podcast_agent.db`：Episode、artifact、账本、审核、审批和交付的当前业务主存储
- PostgreSQL：任务队列、任务尝试、账号、会话、授权、预算、审计，以及 SQLite 业务表的对账迁移副本
- Redis：令牌桶、并发租约和登录限流等短期协调状态

因此本文不宣称“全部业务已经迁到 PostgreSQL”。`state/compat.py` 处理 SQLite 的 `IN (?,…)` 与 PostgreSQL 的 `ANY` 差异，授权查询通过独立 PostgreSQL 连接完成。

## 多期任务并发处理的实际实现

### 幂等入队

入口：`state/postgres/job.py`、`state/postgres/job_cli.py`、`runner/src/drive-pending.ts`。

任务写入使用：

```sql
INSERT INTO job (...)
VALUES (...)
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING *;
```

实际自动 Episode 的幂等键为 `episode:{episode_id}:auto:v1`。重复发现或重复入队会读取已有任务，而不是新增一条可执行任务。`payload` 使用 JSONB 保存 Episode、guid 和可选成本预估。

### 原子领取与任务尝试

worker 在一个短事务内完成：

```text
处理已耗尽次数的过期 running
→ 选择 ready／到期 retry／租约已过期的 running
→ ORDER BY priority DESC, id
→ FOR UPDATE SKIP LOCKED LIMIT 1
→ 预算与 running 上限检查
→ 更新 running、locked_by、locked_until、attempts
→ 插入 job_attempt
→ 提交事务
```

`SKIP LOCKED` 让并发 worker 跳过已被其他数据库事务锁住的行。最终完成和失败还会校验 `job_id + worker_id + running + 未过期租约`，防止过期 worker 覆盖新 owner 的结果。

每个任务都有独立的 `job_attempt`，记录 attempt_no、worker、开始／结束时间、结果、usage 和错误。队列不是只更新一个状态字段，而是保留可追踪的执行历史。

### 租约、心跳与崩溃接管

- 默认任务租约 300 秒，worker 每 60 秒调用一次 heartbeat
- heartbeat 只能更新同 job、同 worker、未过期的 running 任务
- worker 崩溃后不会立刻被其他 worker 重入，而是等待租约到期
- 到期任务可以被重新领取；旧 running attempt 被标记 `lease_expired`
- 已耗尽 attempts 的过期任务进入 `parked`，不会无限重启

租约解决的是执行互斥与接管，不等于付费调用可以安全重放。unsafe 结果未知时仍必须查询账本、产物和供应商记录。

### 有界并行与按需退出

实际 `drive-pending` 不是无限创建进程：

- `PODCAST_RUNNER_PARALLEL` 默认 2，代码限制为 1–4
- 每次最多领取 `limit` 个任务，limit 限制为 1–100
- 每个 worker 对应一个 Episode 子进程，执行结束后回收
- 多 worker 使用共享计数限制本轮领取总量
- 队列清空后 worker 组退出，不常驻空转

这形成了第一道背压：任务可以积压在 PostgreSQL，但本机只启动有限数量的执行者。第二道背压来自供应商 Redis 闸门和预算 `max_running`。

### 重试与 unsafe 边界

队列原语支持 retry、指数退避、failed 和 parked，但实际 Episode 子进程失败默认不自动标记为可重试。输出中出现 unknown、RemoteProtocolError 或状态不明时：

- `job_attempt` 标记 `unknown`
- job 进入失败状态，而不是自动重发付费请求
- 后续凭 call_ref、transcription_id、terminal 子任务或已有产物恢复

因此系统采用的是**至少一次调度 + 幂等约束 + 结果对账**，不是宣称分布式 Exactly Once。

## 供应商限流与预算的实际实现

### Redis 原子令牌桶

实现：`pipeline/provider_gate.py`；Runner 接缝：`runner/src/provider-gate.ts`。

每个供应商有独立的 tokens Hash：

```text
<prefix>:<provider>:tokens = { tokens, updated }
```

Lua 在 Redis 内一次完成读取、基于 Redis TIME 的 refill、扣减、retry_after_ms 计算和写回。这样避免多个 worker 分别 GET／SET 时发生竞态。速率控制的是单位时间允许发起多少次调用。

### owner/TTL 并发租约

每个供应商另有 leases Hash：

```text
<prefix>:<provider>:leases = { owner: expires_at_ms }
```

同一 Lua 协调过程清理过期 owner、检查当前并发、写入新 owner。每次调用使用随机 owner，结束时只 HDEL 自己的 owner；进程崩溃后的残留由 TTL 回收。

速率与并发必须同时限制：低 QPS 的长任务仍可能长期占满连接、内存或供应商并发名额。

### 付费调用顺序

真实 Scribe、分类和 Runner 模型调用遵循：

```text
本地重复结果检查
→ Redis acquire
→ 账本 record_intent
→ 外部付费调用
→ record_outcome(success / failed / unknown)
→ release(owner)
```

限流或 Redis 故障发生在 intent 和外部 POST 之前。Redis 不可用时选择失败关闭，不降级为无限制直连。release 失败不会篡改已经确定的业务结果，而是依赖 TTL 清理。

### 数据库预算闸门

实现：`state/postgres/budget.py`，在 `claim_job` 将任务改为 running 之前执行。

预算可以来自全局、用户、Episode 和单任务：

- 金额检查同时读取 `ledger.amount_est` 与 `usage_basis` 中的模型 `cost_usd`
- 有金额上限但缺少成本预估时，以 `cost_unknown` 失败关闭
- 超过金额上限时以 `budget_exceeded` 拒绝
- 达到 `max_running` 时改为 30 秒后可领取的 retry，形成背压
- 预算拒绝发生在 attempts 增加和 job_attempt 创建之前，不把“等待配额”算作一次失败执行

Redis 控制供应商速率与并发，PostgreSQL 控制持久化预算与任务状态，两者不能互相替代。

## 多用户只读访问的实际实现

### 长驻领域服务

`review/api.py` 提供 FastAPI 内部 RPC；`review/src/python.ts` 让 Fastify 通过 `PODCAST_DOMAIN_URL` 和内部令牌调用。

改造前每个管理请求都可能启动一次 Python CLI；改造后：

- FastAPI 启动时执行一次数据库迁移
- 请求路径复用长驻进程与已加载模块
- Fastify 不再为每个请求支付 Python 解释器启动成本
- 同步 `def` 端点由 Starlette 在线程池执行
- SQLite／PostgreSQL 连接仍在请求或任务结束后关闭

当前没有声称使用 PostgreSQL 连接池。并发通过 worker 上限和服务规模受控；若生产连接数成为瓶颈，再评估连接池或 PgBouncer。

### 会话与权限隔离

实现：`state/postgres/user.py`、`review/auth_api.py`、`review/src/auth.ts`、`review/domain_auth.py`。

- 密码使用 Argon2id 哈希，数据库不保存明文密码
- 原始 session token 只出现在登录响应和 HttpOnly Cookie，数据库保存 SHA-256 token hash
- Cookie 使用 HttpOnly、SameSite=Lax；HTTPS 部署时启用 Secure
- Fastify 对每个受保护请求调用 FastAPI 验证会话，再注入 `_user_id` 和 `_user_role`
- owner／admin 可以访问全部 Episode；普通用户通过 `resource_grant` 过滤允许访问的 Episode
- 未授权详情按 404 处理，避免泄露资源是否存在
- 写请求校验 Origin host 与 Sec-Fetch-Site，拒绝跨站写入
- 登录复用 Redis 闸门进行速率限制，会话撤销和过期由 PostgreSQL 判断

权限过滤在领域查询中执行，不只是前端隐藏按钮。R2 签名 URL 也只有通过 Episode 授权后才生成。

### 只读并发为什么不是单纯加缓存

当前没有为了压测加入权限缓存或节目列表缓存。这样避免缓存失效导致越权或旧发布版本残留。当前优化来自：

- 长驻 FastAPI 替代每请求 Python 启动
- 只读 SQL 查询与明确连接关闭
- 已发布音频和字幕由 R2 承载，减少 VPS 带宽压力
- Fastify 负责 HTTP 与静态资源，Python 负责领域事务

生产并发提高后，可以考虑短 TTL 只读缓存，但权限和当前发布指针仍必须以数据库为权威。

## 一致性与故障恢复设计

| 故障 | 实际处理 |
| --- | --- |
| 重复入队 | `idempotency_key` 唯一约束复用已有 job |
| 多 worker 竞争 | 数据库行锁 + `SKIP LOCKED` |
| worker 崩溃 | 租约到期接管，attempt 留痕；unsafe 不盲目重放 |
| 过期 worker 回写 | complete／fail 校验 worker 与未过期租约 |
| Redis 故障 | 付费调用失败关闭，不绕过闸门 |
| owner 释放失败 | 业务结果不变，等待 TTL 回收 |
| 超过 running 配额 | retry 背压，不增加 attempts |
| 成本未知或超预算 | 付费执行前失败关闭 |
| 供应商结果未知 | unknown 账本／attempt，凭既有结果对账 |
| 普通用户越权 | 会话验证 + Episode 授权过滤 + 404 |
| 已发布音频访问增加 | R2 私有对象与动态签名，不占 VPS 长连接带宽 |

## VPS 上的实际运行方式

- 单 VPS：2 核、4 GB、60 GB SSD
- PostgreSQL／Redis 使用 Docker Compose，仅监听本机
- FastAPI／Fastify 使用 systemd 长驻，仅监听本机
- 当前通过 SSH 隧道内测，公网未开放管理端口
- 业务 cron 尚未开启，因此 VPS 当前没有自动发起新付费任务
- 本机自动备份已配置；完整恢复演练和生产故障注入仍待做
- 服务器已复跑 Python、Runner、Review 回归，但尚未取得生产 QPS 数字

## 多期任务并发处理验收

### 业务需求

大量 Episode 同时进入系统时，多个 worker 可以并发处理下载、转写、翻译、复核、看点和发布任务；任务不重复领取、不丢失，失败可以恢复。

### 必须具备的机制

- PostgreSQL 持久化任务队列与任务尝试历史
- Episode 任务幂等键，业务阶段与输入版本绑定
- `SELECT ... FOR UPDATE SKIP LOCKED` 多 worker 领取
- 有界并行和队列背压
- 任务租约、心跳、过期接管
- 可重试与不可重试失败分离
- 付费 unsafe 调用结果未知时不自动重发
- 任务状态与业务状态使用一致的数据库事务

### 本地验收指标

| 指标 | 本轮要求 |
| --- | --- |
| 负载 | 1000 个 fake 任务、8 个 worker |
| 完成性 | 1000 个任务全部完成，无丢失 |
| 唯一性 | 同一任务不被多个 worker 重复领取 |
| 领取延迟 | p95 小于 20ms |
| 错误 | 压测错误列表为空 |
| 持久性 | 关闭并重新连接后任务与尝试记录仍存在 |

### 当前证据

`docs/PERFORMANCE_REPORT.md` 记录的实际结果：

- 1000 个任务全部完成
- 1000 条任务尝试记录
- 吞吐 **1000.77 任务/秒**
- 领取 p95 **6.919ms**，平均 **4.735ms**
- 零错误
- 显式事务提交后，关闭连接再查询仍保留记录

这些是 fake 任务的队列处理结果，不是真实 Scribe 或模型的端到端吞吐。

## 供应商限流与预算验收

### 业务需求

大量任务同时就绪时，不得超过 Scribe、模型供应商、单用户和单期的速率、并发与费用限制；协调服务故障不能导致无限付费调用。

### 必须具备的机制

- Redis Lua 原子令牌桶，按供应商限制速率
- Redis Hash + owner + TTL 管理长任务并发租约
- owner 只能释放自己的租约，崩溃残留由 TTL 回收
- 获取许可后才写付费 intent，结束后释放 owner
- Redis 不可用时付费调用失败关闭，不降级为无限制调用
- 全局／用户／Episode／单任务预算检查在付费调用之前执行
- 账本同时考虑 `amount_est` 与模型 usage 中的 `cost_usd`
- 最大 running 数量产生可重试背压
- 金额超限或未知预估失败关闭，不消耗任务 attempts

### 本地验收指标

| 指标 | 本轮要求 |
| --- | --- |
| 原子性 | 多线程竞争不超过配置的并发上限 |
| 限流 | burst 用完后返回 retry_after_ms |
| 租约安全 | owner 只能释放自己；过期后允许接管 |
| 故障安全 | Redis 故障时付费调用停止，不绕过 |
| 预算安全 | 超限任务不进入 running、不增加 attempts、不创建 job_attempt |
| 背压恢复 | running 释放后，retry 任务可在到期后领取 |

### 当前证据

- 真实 Redis 测试 **4 项通过**
- 20 个线程竞争、并发上限为 5 时，只允许 5 个进入
- 真实 `from_env` acquire／release 成功
- 1000 次 acquire/release、32 并发，**2520.70 次/秒**，p95 **19.454ms**，零错误
- 真实 PostgreSQL 预算测试覆盖超预算、最大 running 背压、未知成本和单任务预算

Redis 协调吞吐不代表外部供应商允许相同并发或能以相同速度完成调用。

## 多用户只读访问与权限验收

### 业务需求

多个用户同时查看审核箱、节目库、用量和节目详情时，接口保持可用；普通用户只能访问被授权的 Episode，不能读取其他用户的私有音频、字幕、override 或费用。

### 必须具备的机制

- Argon2id 密码哈希与随机会话 token 哈希
- 安全 Cookie：HttpOnly、SameSite；生产 HTTPS 使用 Secure
- owner／admin 与普通用户角色
- Episode 资源授权与权限过滤
- 未授权资源按不存在处理
- 登录和写请求限流／同源保护
- 会话过期、撤销与退出
- 只读查询延迟与状态码统计

### 本地验收指标

| 指标 | 本轮要求 |
| --- | --- |
| 负载 | 300 个只读请求、16 并发 |
| 正确性 | 全部 HTTP 200 |
| 延迟 | p95 小于 150ms |
| 权限 | 普通用户只能访问被授权 Episode |
| 会话 | 错误 token、过期 token、退出后的 token 均不可用 |

### 当前证据

当前实测覆盖 `/api/library/episodes`、`/api/usage`、`/api/review/episodes`：

- **256.13 QPS**
- 全部 HTTP 200
- p50 **58.061ms**，p95 **94.247ms**，p99 **114.28ms**
- 零错误

多用户真实联调另已验证：管理员全量访问，普通用户只读授权 Episode；未授权 Episode 返回 404。

详情、登录、音频 Range 和写接口的全量压测仍属补充验收，不把当前列表接口结果冒充全接口结果。

## 明确不需要高并发的功能

以下功能以正确性、安全、质量和可恢复性为主，不设置高 QPS 指标：

- override 文本修改与冲突处理
- 人工审批、驳回与发布后修订
- R2 上传、对象校验和私有签名
- 登录密码安全和普通低频账号管理
- 音频播放器、字幕同步与页面交互
- 知识库沉淀和模型翻译质量
- 单期正常执行的内容质量

它们仍需通过功能、权限、幂等和故障测试，但不为了简历硬加并发指标。

## 非目标与禁止宣称

- 不宣称所有接口都支持高并发
- 不把队列 fake 吞吐写成真实节目处理吞吐
- 不把本地 API 结果写成生产 QPS
- 不把 Redis 闸门吞吐写成供应商模型吞吐
- 不因高并发取消人工审核、override 保护、unknown 对账或私有交付
- 不为了术语引入 Kafka／Kubernetes／服务网格，除非出现对应实际需求与验收
- 不把模拟用户、模拟供应商或本地结果包装成真实生产流量

## 后续验收清单

- 扩展详情、登录会话、音频 HEAD／Range 和写接口的压测矩阵
- 覆盖重复保存、重复审批、旧版本、驳回和权限拒绝场景
- 注入 worker 崩溃、租约过期、Redis／PostgreSQL 短暂不可用
- 在 VPS 上重新测量 CPU、内存、网络、队列积压和 p95／p99
- 做小流量真实供应商调用，校准速率、预算和错误率
- 将生产结果与本地基准分开报告，不混写数字

## 技术总结（简历表述边界）

可以使用以下事实描述：

> 针对多期播客长任务与多用户私有访问，设计并实现了 PostgreSQL 持久化任务队列、Redis 分布式供应商闸门和多用户权限体系。通过 `SKIP LOCKED`、租约心跳和任务尝试历史支持多 worker 并发与故障接管；通过 Lua 令牌桶、owner/TTL 并发租约、预算检查和失败关闭控制付费资源；通过 Fastify + 长驻 FastAPI、Argon2id、会话哈希与 Episode 授权实现私有只读访问。使用 fake provider 完成本地可复现压测，并在单 VPS 完成部署与数据迁移。

必须同时保留边界：生产 QPS、真实供应商吞吐和长期稳定性仍需 VPS 压测与受控调用验收。

## 完成口径

只有同时满足以下条件，才可称为本轮高并发专项完成：

- 三条链路的本地指标和功能测试通过
- 权限、幂等、预算和付费恢复没有退化
- 压测报告可复现，且明确负载是否真实
- VPS 部署后重新测量并记录生产环境差异
- 文档中的每个数字都有实际运行依据
