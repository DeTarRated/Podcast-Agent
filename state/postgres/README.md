# state/postgres — PostgreSQL 数据契约与持久化任务队列

负责当前业务表的 PostgreSQL 版本、版本迁移、SQLite 对账迁移及 `job`／`job_attempt` 队列。多用户表仍属后续阶段。

## 本地开发

```bash
docker compose up -d postgres redis
uv run python -m state.postgres.migrations
```

默认 DSN 仅用于本机开发：`postgresql://podcast_agent:podcast-dev@127.0.0.1:5432/podcast_agent`。生产必须使用独立强凭据，不复用开发密码。

## SQLite 迁移

```bash
uv run python -m state.postgres.migrate_from_sqlite --source var/podcast_agent.db
```

迁移保留原 id、artifact hash、call_ref、审批与当前发布指针；单事务写入并逐表对账，非空目标仅允许完全一致的幂等复查。

## 队列语义

`job_cli.py` 提供 `enqueue-episode`、`claim`、`heartbeat`、`complete`、`fail`、`stats`，供 `runner/src/drive-pending.ts` 使用；所有写事务完成后显式提交，关闭连接不丢任务。

- `enqueue_job`：idempotency_key 唯一，payload 为 JSONB
- `claim_job`：短事务 + `FOR UPDATE SKIP LOCKED`，支持多 worker
- `heartbeat_job`：续约；过期任务由其他 worker 接管
- `complete_job` / `fail_job`：校验当前 worker 与有效租约
- 可重试失败指数退避；非重试、unknown 或耗尽任务不自动重试
- 任务尝试历史独立保存，状态与业务结果不混入 Redis

## 真实测试

使用独立测试库，不运行业务库清表操作：

```bash
PODCAST_TEST_POSTGRES=1 \
PODCAST_POSTGRES_DSN=postgresql://podcast_agent:podcast-dev@127.0.0.1:5432/podcast_agent_test \
uv run pytest tests/test_postgres_job.py -q
```

迁移验收使用 `podcast_agent_migration_test` 与 `tests/test_postgres_migration.py`。默认测试列表会跳过这两组，避免无数据库环境时误报失败。

## 多用户与权限

- `user.py`：Argon2id 密码哈希、会话 token 哈希、resource_grant、audit_log、budget_limit；账号与授权存 PostgreSQL，Episode 可继续存 SQLite。
- `user_cli.py`：`create-user`、`grant`、`create-session`、`validate-session`、`set-budget`。密码只从 `PODCAST_NEW_USER_PASSWORD` 读取，不进入命令行。
- `review/auth_api.py`：登录、会话、退出、grant-check；登录经 Redis 独立限流。
- `state/compat.py`：SQLite `IN (?,...)` 与 PostgreSQL `ANY(array)` 兼容，避免把 PostgreSQL 写法直接发给 SQLite。

## 预算执行

- `budget.py` 在 `claim_job` 标记 running 前检查全局、用户、Episode 和单任务预算；支持 task/day/month 周期。
- 金额使用 Decimal，账本 amount_est 与 usage_basis 中的 cost_usd 均纳入已用额度。
- `max_running` 超限进入 retry 并延迟领取；金额超限或未知预估进入 failed，不消耗 attempts，也不创建 job_attempt。
- 测试：`tests/test_postgres_budget.py`，使用独立 PostgreSQL 测试库，零真实付费调用。
