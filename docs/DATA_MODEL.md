# 数据模型与表结构索引

- 定位：记录当前 SQLite 数据表、字段、约束、职责及对应代码文件；同时列出多用户／高并发阶段计划新增的 PostgreSQL 表
- 当前权威 DDL：`state/schema.sql`
- 当前迁移：`state/migrations.py`，使用 `PRAGMA user_version`，当前版本为 v3
- 高并发设计：`.agent/high-concurrency-architecture.md`（设计稿，未提交）
- HTTP、RPC、CLI、Runner 与外部服务接口见 `docs/API_REFERENCE.md`
- 状态标记：「当前」表示代码和测试已实现；「计划」表示尚未建表，字段可在实施评审时细化

## 全局约定

- 当前时间字段为 ISO8601 UTC 文本；PostgreSQL 目标使用 `TIMESTAMPTZ`。
- SHA256 使用小写十六进制；音频、字幕和渲染产物按不可变版本关联。
- 金额未知为 `NULL`，不记为零；SQLite 当前为 `REAL`，PostgreSQL 目标金额使用 `NUMERIC`。
- `request_id`／`call_ref`／任务幂等键用于防重复写入或重复付费，不代表外部供应商一定支持服务端幂等。
- 当前布尔字段使用 `INTEGER 0/1` + CHECK；PostgreSQL 目标使用 `BOOLEAN`。
- override、审核修订、任务尝试和审计记录采用追加历史，不覆盖事实记录。
- 密码、API key、Cookie、R2 秘密和完整签名 URL 不进入数据表。

## 当前表总览

| 表 | 职责 | 主要代码文件 |
| --- | --- | --- |
| `feed` | RSS feed 配置、条件请求状态、历史基线 | `state/feed.py`、`discover/cli.py`、`discover/poller.py` |
| `discovery_candidate` | 候选去重、分类结果、Episode 入队关联 | `state/discovery.py`、`discover/poller.py`、`discover/classify.py` |
| `episode` | 一期节目的主状态、元数据、首次音频 hash | `state/episode.py`、`pipeline/episode_flow.py` |
| `artifact` | 不可变产物路径、版本、SHA256、完成状态 | `state/manifest.py`、`pipeline/orchestrate.py`、`pipeline/episode_flow.py` |
| `ledger` | 付费调用 intent→outcome、用量、估算与 unknown | `state/ledger.py`、`state/ledger_cli.py`、`state/ledger_report.py` |
| `execution_lock` | 单期执行租约、续约、过期接管与释放 | `state/execution.py`、`runner/src/drive-pending.ts`、`runner/src/drive-delivery.ts` |
| `review_revision` | 审核版本、当前修订、分条选择与自动审批窗口 | `state/review_state.py`、`review/service.py`、`pipeline/orchestrate.py` |
| `override` | 人工修改的追加历史与修改链 | `review/service.py`、`pipeline/override.py` |
| `override_application` | 修改应用到候选稿的结果及冲突 | `review/service.py`、`pipeline/rerun.py` |
| `approval` | 人工／超时审批，绑定确定版本和 SHA256 | `review/service.py` |
| `rerun_request` | 驳回原因、时间范围、override 快照与重跑状态 | `review/service.py`、`pipeline/rerun.py` |
| `delivery` | R2 对象、交付状态、当前发布版本与错误 | `delivery/publish.py`、`delivery/library.py`、`state/delivery_state.py` |

## 发现层表

### `feed`

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `name` | TEXT | 必填，feed 显示名称 |
| `feed_url` | TEXT | 必填、唯一，RSS／Atom URL |
| `show_name` | TEXT | 可选，所属节目 |
| `etag` | TEXT | 上次成功响应的 ETag |
| `last_modified` | TEXT | 上次成功响应的 Last-Modified |
| `enabled` | INTEGER | 默认 1；禁用只停止轮询，不影响在途期数 |
| `bootstrap_pending` | INTEGER | 默认 1；首轮只建立历史基线，完成后为 0 |
| `last_checked_at` | TEXT | 最近检查时间 |
| `last_error` | TEXT | 最近一次安全错误摘要 |
| `created_at` / `updated_at` | TEXT | 创建与更新时间 |

代码：`state/feed.py` 负责配置读写；`discover/poller.py` 负责条件请求和轮询结果；`discover/cli.py` 提供 `feed-add/feed-list/feed-disable`。

### `discovery_candidate`

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `feed_id` | INTEGER | 外键 → feed；与 guid 联合唯一 |
| `guid` | TEXT | 必填，节目去重标识 |
| `title` / `summary` | TEXT | 标题与简介；分类只使用这些元数据 |
| `published_at` | TEXT | 发布时间 |
| `enclosure` | TEXT | 音频直链，缺失时候选失败 |
| `status` | TEXT | pending / enqueued / skipped / failed |
| `reason` | TEXT | 基线、跳过或失败原因 |
| `classification_relevant` | INTEGER | NULL 未分类，0 不相关，1 相关 |
| `classification_call_ref` | TEXT | 对应分类账本，不自动重发 |
| `episode_id` | INTEGER | 入队后外键 → episode；删除 Episode 时置 NULL |
| `created_at` / `updated_at` | TEXT | 创建与更新时间 |

代码：`state/discovery.py` 负责候选与 Episode 原子入队；`discover/classify.py` 负责可选付费分类；`discover/poller.py` 负责去重及历史基线。

## 执行与产物表

### `episode`

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `guid` | TEXT | 必填；与 feed 联合唯一 |
| `feed` | TEXT | 发现时 feedUrl；配置变化不回写在途期数 |
| `feed_snapshot` | TEXT | feed 配置 JSON 快照 |
| `title` | TEXT | 节目标题 |
| `published_at` | TEXT | 发布时间 |
| `duration_secs` | REAL | 音频秒数 |
| `enclosure` | TEXT | 音频直链 |
| `audio_sha256` | TEXT | 首次固化音频 hash |
| `status` | TEXT | discovered / downloaded / transcribed / translated / reviewing / rejected / approved / delivered / failed / parked |
| `crash_count` | INTEGER | 连续崩溃计数，默认 0 |
| `created_at` / `updated_at` | TEXT | 创建与更新时间 |

代码：`state/episode.py`；下载推进位于 `pipeline/episode_flow.py`；发现入队位于 `state/discovery.py`。

### `artifact`

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `episode_id` | INTEGER | 外键 → episode，级联删除 |
| `step` | TEXT | download / scribe_raw / en_srt / review / split / review_render 等 |
| `kind` | TEXT | audio / json / srt / md / tsv / html 等 |
| `path` | TEXT | 工作区产物路径 |
| `version` | INTEGER | 默认 1；episode + step + path + version 唯一 |
| `sha256` | TEXT | 完成产物的内容 hash |
| `status` | TEXT | pending / done / failed |
| `created_at` | TEXT | 登记时间 |

代码：`state/manifest.py`；`commit=False` 支持审核和交付外层事务。各流水线模块只读或登记自己负责的产物。

### `ledger`

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `episode_id` | INTEGER | 可空外键；发现层分类可不绑期数 |
| `purpose` | TEXT | transcribe / translate / review / research / highlights / classify 等 |
| `triggered_at` | TEXT | intent 落库时间 |
| `provider` / `model` | TEXT | 供应商与模型 |
| `usage_basis` | TEXT | 音频秒数、token、缓存和成本依据 |
| `amount_est` | REAL | 金额估算；未知为 NULL |
| `call_ref` | TEXT | 唯一本地调用标识；不代表供应商幂等 |
| `status` | TEXT | intent / success / failed / unknown |

代码：`state/ledger.py` 实现两段式记账；`state/ledger_cli.py` 供 Runner 使用；`state/ledger_report.py` 提供只读管理报表。

### `execution_lock`

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| `episode_id` | INTEGER | 主键、外键 → episode；每期最多一个当前执行租约 |
| `owner` | TEXT | 当前执行者标识 |
| `acquired_at` | TEXT | 获取时间 |
| `expires_at` | TEXT | 过期时间；过期后可被其他执行者接管 |

代码：`state/execution.py`；CLI 接缝在 `pipeline/episode_flow.py`；调度使用 `runner/src/drive-pending.ts` 与 `drive-delivery.ts`。

## 审核与人工修改表

### `review_revision`

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `episode_id` | INTEGER | 外键 → episode |
| `base_artifact_id` | INTEGER | 模型／既有发布基准 artifact |
| `rendered_artifact_id` | INTEGER | 应用 override 后的实际审核 artifact |
| `variant` | TEXT | review / split |
| `status` | TEXT | reviewing / rejected / approved / delivered / superseded |
| `is_current` | INTEGER | 每期最多一个当前修订，使用部分唯一索引 |
| `reviewing_since` | TEXT | 当前审核窗口开始时间 |
| `auto_approve_at` | TEXT | 默认 12 小时后的自动审批时间 |
| `auto_approve_paused` | INTEGER | 保留待审时暂停自动审批 |
| `supersedes_revision_id` | INTEGER | 被本修订取代的上一修订 |
| `created_at` | TEXT | 创建时间 |

代码：`state/review_state.py` 提供当前修订持久化；`review/service.py` 实现保存、切换、审批、驳回和再次编辑；`pipeline/orchestrate.py` 在新期完成时创建修订。

### `override`

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `episode_id` | INTEGER | 外键 → episode |
| `base_revision_id` | INTEGER | 被修改的审核修订 |
| `base_artifact_id` | INTEGER | 修改前的当前 rendered artifact |
| `source_artifact_id` | INTEGER | 对应 transcript_fix 源稿 |
| `start_ms` / `end_ms` | INTEGER | 源时间锚点，要求 end > start |
| `source_text_sha256` | TEXT | 规范化原文 hash |
| `before_text` / `after_text` | TEXT | 修改前后文本 |
| `supersedes_override_id` | INTEGER | 修改链前驱；每个修改最多一个后继 |
| `retracted` | INTEGER | 是否为撤销记录 |
| `request_id` | TEXT | 唯一保存请求幂等键 |
| `result_revision_id` | INTEGER | 本次修改产生的审核修订，供重试返回首次结果 |
| `created_at` | TEXT | 创建时间 |

代码：`review/service.py` 负责校验与持久化；`pipeline/override.py` 负责确定性 SRT 合并，不直接访问数据库。

### `override_application`

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `override_id` | INTEGER | 外键 → override |
| `candidate_artifact_id` | INTEGER | 外键 → artifact；与 override 联合唯一 |
| `status` | TEXT | applied / conflict |
| `target_start_ms` / `target_end_ms` | INTEGER | 应用到目标稿的时间范围 |
| `reason` | TEXT | 冲突稳定原因与安全说明 |
| `created_at` | TEXT | 创建时间 |

代码：`review/service.py` 和 `pipeline/rerun.py` 写入；当前 rendered artifact 存在 conflict 时审批被拒绝。

### `approval`

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `review_revision_id` | INTEGER | 唯一外键；同一修订只能有一个审批 |
| `episode_id` | INTEGER | 外键 → episode |
| `artifact_id` | INTEGER | 被批准的确定 rendered artifact |
| `sha256` | TEXT | 被批准内容 hash |
| `method` | TEXT | manual / auto_timeout |
| `request_id` | TEXT | 唯一审批请求幂等键 |
| `created_at` | TEXT | 审批时间 |

代码：`review/service.py`。人工与自动审批共用版本、hash、冲突和唯一约束校验。

## 驳回与交付表

### `rerun_request`

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `episode_id` | INTEGER | 外键 → episode |
| `base_revision_id` | INTEGER | 被驳回的审核修订 |
| `reason` | TEXT | 必填驳回原因 |
| `ranges_json` | TEXT | 合并后的源时间范围快照 |
| `override_ids_json` | TEXT | 驳回时有效 override 快照 |
| `status` | TEXT | pending / running / completed / failed / unknown |
| `result_revision_id` | INTEGER | 重跑后产生的新审核修订 |
| `request_id` | TEXT | 唯一驳回请求幂等键 |
| `error` | TEXT | 安全错误摘要 |
| `created_at` / `updated_at` | TEXT | 创建与更新时间 |

代码：`review/service.py` 写请求；`pipeline/rerun.py` 实现 prepare → apply-translate → finalize，付费 dispatch 仍由 Runner 执行。

### `delivery`

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `approval_id` | INTEGER | 唯一外键 → approval；同一审批只交付一次 |
| `episode_id` | INTEGER | 外键 → episode |
| `artifact_id` | INTEGER | 被交付的确定字幕 artifact |
| `status` | TEXT | pending / waiting_highlights / uploading / delivered / failed / superseded |
| `is_current` | INTEGER | 每期最多一个当前交付；仅 delivered 可为 1 |
| `audio_key` | TEXT | R2 音频对象 key |
| `subtitle_key` | TEXT | R2 字幕对象 key |
| `highlights_key` | TEXT | R2 看点 HTML 对象 key，包含内容 hash |
| `highlights_json` | TEXT | 已校验看点结构化快照 |
| `sha256` | TEXT | 交付字幕 hash |
| `error` | TEXT | 安全错误摘要；上传失败可重试，highlights 失败不自动重试 |
| `created_at` / `updated_at` | TEXT | 创建与更新时间 |

代码：`delivery/publish.py` 负责上传、校验和发布指针；`delivery/library.py` 负责节目库只读查询与动态签名；`delivery/r2.py` 负责 S3 适配。

## PostgreSQL 当前队列表

以下表已在本地 PostgreSQL 实现并通过真实测试，DDL 为 `state/postgres/schema.sql`，版本迁移为 `state/postgres/migrations.py`。它们不写入 SQLite `schema.sql`。`job.user_id` 在多用户表接入前暂不设置外键。

### `schema_migration`

| 字段 | 目标类型 | 含义 |
| --- | --- | --- |
| `version` | TEXT | 唯一迁移版本 |
| `applied_at` | TIMESTAMPTZ | 应用时间 |

代码：`state/postgres/migrations.py`；每次迁移可重复执行并保留版本记录。SQLite 对账迁移位于 `state/postgres/migrate_from_sqlite.py`。

### `job`

| 字段 | 目标类型 | 约束与含义 |
| --- | --- | --- |
| `id` | BIGINT | 主键 |
| `episode_id` | BIGINT | 可空外键；系统级任务可不绑期数 |
| `user_id` | BIGINT | 可空外键，用于用户配额与权限 |
| `kind` | TEXT | episode / download / transcribe / translate / review / highlights / publish / rerun |
| `status` | TEXT | ready / running / retry / failed / parked / completed |
| `payload` | JSONB | 不可变输入版本、hash 与必要参数，不含凭据 |
| `idempotency_key` | TEXT | 唯一；业务阶段 + 输入 hash + 版本 |
| `priority` | INTEGER | 调度优先级 |
| `available_at` | TIMESTAMPTZ | 延迟重试／可领取时间 |
| `locked_by` | TEXT | 当前 worker |
| `locked_until` | TIMESTAMPTZ | 租约到期时间 |
| `attempts` / `max_attempts` | INTEGER | 当前与最大尝试次数 |
| `budget` | NUMERIC | 单任务预算上限，可为空 |
| `last_error` | TEXT | 稳定错误码与安全摘要 |
| `created_at` / `updated_at` | TIMESTAMPTZ | 创建与更新时间 |

代码：`state/postgres/job.py`、`state/postgres/job_cli.py`、`runner/src/drive-pending.ts`。领取使用短事务 + `FOR UPDATE SKIP LOCKED`；unsafe 付费任务恢复前必须查账本。所有写事务完成后显式提交，关闭连接不丢任务。

### `job_attempt`

| 字段 | 目标类型 | 约束与含义 |
| --- | --- | --- |
| `id` | BIGINT | 主键 |
| `job_id` | BIGINT | 外键 → job，级联删除 |
| `attempt_no` | INTEGER | 与 job 联合唯一 |
| `worker_id` | TEXT | 执行 worker |
| `status` | TEXT | running / succeeded / failed / unknown |
| `started_at` / `finished_at` | TIMESTAMPTZ | 尝试起止时间 |
| `result` | JSONB | 产物与结果引用，不存秘密 |
| `usage` | JSONB | token／音频时长等用量依据 |
| `error` | TEXT | 安全错误摘要 |

代码：`state/postgres/job.py` 在领取、完成和失败时写入；Worker 编排接缝仍由 Runner 后续接入。

## 多用户与权限当前表

以下表已在本地 PostgreSQL 实现并通过真实测试，业务 Episode 当前仍可使用 SQLite，授权映射保留原 id。密码和会话只存哈希；资源授权在签发 R2 URL 或读写 API 前检查。

### `user_account`

| 字段 | 目标类型 | 约束与含义 |
| --- | --- | --- |
| `id` | BIGINT | 主键 |
| `email` | TEXT | 规范化后唯一 |
| `password_hash` | TEXT | Argon2id 或等效安全哈希，不存密码明文 |
| `role` | TEXT | owner / admin / reviewer / viewer |
| `status` | TEXT | active / disabled |
| `created_at` / `updated_at` | TIMESTAMPTZ | 创建与更新时间 |

代码：`state/postgres/user.py`、`review/src/auth.ts`、`review/auth_api.py`、`state/postgres/user_cli.py`。

### `session`

| 字段 | 目标类型 | 约束与含义 |
| --- | --- | --- |
| `id` | BIGINT | 主键 |
| `user_id` | BIGINT | 外键 → user_account |
| `token_hash` | TEXT | 唯一会话 token 哈希，不存原始 token |
| `expires_at` | TIMESTAMPTZ | 会话过期时间 |
| `created_at` / `last_used_at` | TIMESTAMPTZ | 创建与最近使用时间 |

代码：`state/postgres/user.py`、`review/src/auth.ts`。Cookie 必须 HttpOnly、Secure（生产 HTTPS）、SameSite，并支持过期与轮换；本地演示不强行设置 Secure。

### `resource_grant`

| 字段 | 目标类型 | 约束与含义 |
| --- | --- | --- |
| `id` | BIGINT | 主键 |
| `user_id` | BIGINT | 外键 → user_account |
| `resource_type` | TEXT | feed / episode / delivery / ledger 等 |
| `resource_id` | BIGINT | 资源 id |
| `permission` | TEXT | read / review / approve / publish / admin |
| `created_at` | TIMESTAMPTZ | 授权时间 |

`user_id + resource_type + resource_id + permission` 唯一。代码：`state/postgres/user.py`、`review/auth_api.py`、`review/cli.py`、`delivery/commands.py`、`delivery/library.py`。

### `audit_log`

| 字段 | 目标类型 | 约束与含义 |
| --- | --- | --- |
| `id` | BIGINT | 主键 |
| `user_id` | BIGINT | 可空外键；系统任务可为空 |
| `action` | TEXT | 审核、审批、驳回、发布、授权等动作 |
| `resource_type` / `resource_id` | TEXT / BIGINT | 资源定位 |
| `version` | TEXT | 被操作版本或 hash |
| `result` | TEXT | success / failed / rejected |
| `details` | JSONB | 安全摘要，不含秘密 |
| `created_at` | TIMESTAMPTZ | 操作时间，追加保存 |

代码：`state/postgres/user.py::audit`；审核与审批操作可复用该追加审计接口。

### `budget_limit`

| 字段 | 目标类型 | 约束与含义 |
| --- | --- | --- |
| `id` | BIGINT | 主键 |
| `scope` | TEXT | global / user / episode |
| `user_id` / `episode_id` | BIGINT | 按 scope 使用的可空外键 |
| `period` | TEXT | task / day / month |
| `max_amount` | NUMERIC | 对应周期费用上限 |
| `max_running` | INTEGER | 最大 running 任务数 |
| `enabled` | BOOLEAN | 是否启用 |
| `created_at` / `updated_at` | TIMESTAMPTZ | 创建与更新时间 |

代码：`state/postgres/user.py::set_budget/budget_for`、`state/postgres/budget.py`、`state/postgres/job.py`。预算检查发生在领取付费任务之前；max_running 超限进入可重试 retry，金额超限或未知预估失败关闭。

## 迁移与测试索引

- 当前 schema 测试：`tests/test_review_state.py`、`tests/test_discover.py`、`tests/test_execution_lock.py`
- 审核与交付写入测试：`tests/test_review_service.py`、`tests/test_delivery.py`
- 账本与报表测试：`tests/test_state.py`、`tests/test_ledger_report.py`
- PostgreSQL 阶段需新增迁移对账、多 worker 领取、租约接管和多用户权限测试，不用 SQLite 测试冒充 PostgreSQL 验收
