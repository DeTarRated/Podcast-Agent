# Redis 分布式限流与并发协调实现记录

- 状态：已实现并通过本地真实 Redis 测试
- 目标：在多 worker／多期节目并发处理时，对外部付费供应商进行速率与并发控制
- 核心代码：`pipeline/provider_gate.py`
- Runner 接缝：`runner/src/provider-gate.ts`
- 测试：`tests/test_provider_gate.py`、`runner/src/provider-gate.test.ts`

## 为什么使用 Redis

下载、转写、翻译、复核和看点可能由不同 worker 并发执行。单个进程内的信号量无法限制其他进程，因此需要所有执行者共享的协调点。

Redis 在本项目只保存：

- 供应商令牌桶
- 当前长任务的并发租约
- 可过期、可重建的协调状态

PostgreSQL／当前业务 SQLite 仍是任务、Episode、产物、审批、交付和账本的权威存储。Redis 故障不能导致这些事实丢失。

## 实现文件与调用链

| 文件 | 职责 |
| --- | --- |
| `pipeline/provider_gate.py` | Redis TIME、Lua 令牌桶、并发租约、owner 释放、CLI |
| `pipeline/scribe.py` | Scribe POST 前获取许可，结束后释放租约 |
| `discover/classify.py` | DeepSeek 分类 POST 前获取许可，结束后释放 |
| `runner/src/provider-gate.ts` | Runner 经窄 Python CLI 使用同一闸门 |
| `runner/src/experiment-subagent.ts` | 真实翻译／复核／调研／看点模型调用前后使用闸门 |

真实子代理的 `behaviors` mock 在模型调用前返回，因此单元测试不依赖 Redis。只有真实付费路径才要求 Redis 可用。

## 数据结构与 key

按供应商分别使用两个 key：

```text
<prefix>:<provider>:tokens   # Hash：tokens、updated
<prefix>:<provider>:leases   # Hash：owner -> 到期毫秒时间戳
```

`prefix` 默认 `podcast-agent`，可通过 `PODCAST_REDIS_PREFIX` 修改。测试使用随机前缀，不 flush 整个 Redis 数据库。

## 令牌桶限流

Lua 脚本在 Redis 内原子完成：

```text
读取 tokens / updated
→ 根据 Redis TIME 计算时间差
→ tokens = min(burst, tokens + elapsed_seconds × rate)
→ 有足够令牌则扣减，否则计算 retry_after_ms
→ 写回并设置过期时间
```

速率与并发是两个不同概念：

- 速率限制控制单位时间允许多少次调用
- 并发限制控制当前仍在执行的调用数量
- 即使平均速率较低，也必须限制长任务的并发占用

配置：

```bash
PODCAST_REDIS_URL=redis://127.0.0.1:6379/0
PODCAST_RATE_SCRIBE=0.2
PODCAST_BURST_SCRIBE=1
PODCAST_CONCURRENT_SCRIBE=2
PODCAST_RATE_DEEPSEEK=5
PODCAST_BURST_DEEPSEEK=5
PODCAST_CONCURRENT_DEEPSEEK=2
```

通用形式为 `PODCAST_RATE_<PROVIDER>`、`PODCAST_BURST_<PROVIDER>`、`PODCAST_CONCURRENT_<PROVIDER>`。Scribe 默认约 0.2 次／秒、burst 1；DeepSeek 默认 5 次／秒、burst 5；并发默认均为 2。

## 并发租约

每次真实调用生成随机 owner，并在 leases Hash 中写入：

```text
owner → 当前 Redis 时间 + lease TTL
```

Lua 脚本原子执行：

- 清理已经过期的 owner
- 同一 owner 重复进入时只续约，不新增占用
- 当前数量达到上限时拒绝新调用
- 未达到上限时写入 owner 并设置 Hash 过期时间

调用完成后执行 `HDEL provider owner`，只释放本次调用自己的租约。释放失败时不把已确定的业务结果改成 unknown，而是等待 TTL 自动清理。

租约默认六小时，是崩溃后的协调回收时间，不是正常业务超时。任务租约与 PostgreSQL 队列租约是两层不同机制，不能互相替代。

## 与付费账本的顺序

真实调用顺序为：

```text
本地状态与重复检查
→ Redis acquire
→ 付费账本 record_intent
→ 外部 POST／模型调用
→ record_outcome(success / failed / unknown)
→ Redis release(owner)
```

被限流或 Redis 不可用时，不会写入新的付费 intent，也不会发外部请求。已有 call_ref 的重复任务在 acquire 前就被拒绝，不会重复占用配额。

如果账本 intent 写入失败，会释放已取得的租约。若调用完成但 release 失败，业务结果保持不变，Redis owner 等待 TTL 回收。

## 失败关闭而不是绕过

`acquire` 遇到 Redis 连接失败、Lua 错误或非法配置时，直接返回 `ProviderGateError`，停止付费调用。

这是刻意选择的 fail-closed 策略：

- 本项目的首要约束是防止高并发下重复消费和预算失控
- 不能因为协调服务故障就无限调用付费服务
- 恢复 Redis 后，任务可以从持久化状态继续
- 不允许把 Redis 故障自动降级成无限制直连

## CLI 与调试入口

```bash
# 获取一次许可；被限流时退出码为 2，并返回 retry_after_ms
uv run python -m pipeline.provider_gate acquire --provider deepseek

# 使用返回的 owner 释放
uv run python -m pipeline.provider_gate release --provider deepseek --owner <owner>
```

Runner 使用 `runner/src/provider-gate.ts` 调用同一 CLI，不另写一套 Redis 算法。

## 真实测试证据

```bash
PODCAST_TEST_REDIS=1 uv run pytest tests/test_provider_gate.py -q
```

当前真实 Redis 测试覆盖并通过：

- burst 内允许、超出速率时返回 retry_after_ms
- 并发达到上限后拒绝
- owner 只能释放自己的租约
- TTL 到期后允许接管
- 20 个线程竞争、并发上限为 5 时，只允许 5 个进入
- Redis 故障时 acquire 失败关闭，release 不篡改业务结果

Runner 的 `provider-gate.test.ts` 另验证 provider／owner 参数映射和限流错误。真实测试使用本地 Docker Redis，不产生 Scribe 或模型费用。

## 面试可讲的问题

### 为什么限流和并发控制都要做？

速率是时间维度，控制平均流量；并发是资源占用维度，控制正在执行的请求。长任务即使 QPS 很低，也可能因为持续占用而造成资源耗尽。

### 为什么使用 Lua？

读取、 refill、扣减、写回若分成多个命令，会被其他 worker 穿插执行。Lua 在 Redis 内原子执行，避免竞态导致超发令牌或超额并发。

### 为什么使用 Redis TIME？

不同 worker 的本地时钟可能有偏差。使用 Redis 时间让所有参与者使用同一时间源；测试可注入毫秒时间验证边界。

### 为什么释放需要 owner？

直接删除整个并发键会误删其他 worker 的租约。owner 保证只释放当前调用，TTL 处理进程崩溃后的残留。

### Redis 挂了为什么不能继续调用？

本项目把付费安全和预算控制放在可用性之前。故障时继续无限制调用会造成费用和供应商压力，因此选择失败关闭，由持久化任务在恢复后继续。

### 是否因此实现 Exactly Once？

不是。Redis 闸门只负责速率与并发协调。业务使用持久化任务、call_ref 唯一约束、intent／outcome 和 unknown 对账实现安全的至少一次执行与结果恢复，不宣称分布式 Exactly Once。

### 为什么没有使用 Redlock 替代数据库约束？

当前需求不是用一把分布式锁证明所有业务正确性。任务幂等、版本和当前发布指针仍由数据库唯一约束与事务兜底；Redis 只负责可重建的限流和并发协调。

## 当前边界

- 当前验证为本地单 Redis 实例，不代表 Redis Cluster／Sentinel 已完成验收
- 尚未实现动态限流配置管理页；当前通过环境变量配置
- 长任务租约依赖 TTL，尚未做独立心跳压测；PostgreSQL 任务租约仍有自己的恢复语义
- 未把本地测试吞吐写成生产 QPS
- 多用户配额与权限在后续阶段实施
- 真实供应商的限流阈值仍需按账号配额与受控调用校准
