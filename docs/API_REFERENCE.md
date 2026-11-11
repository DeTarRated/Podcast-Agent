# 接口与功能参考

- 定位：记录当前已实现的 HTTP API、内部 RPC、CLI、Runner 工具和外部服务接口，以及对应代码文件
- 范围：只写当前代码已经实现的能力；计划中的接口不冒充现成接口
- 当前运行形态：Fastify 对外 Web／管理 API；FastAPI 提供内部领域 RPC；PostgreSQL 负责任务、账号和授权；当前 Episode 业务仍可使用 SQLite
- 数据表字段另见 `docs/DATA_MODEL.md`
- Redis 协调细节另见 `docs/REDIS_COORDINATION.md`
- 压测结果另见 `docs/PERFORMANCE_REPORT.md`
- 高并发只覆盖任务队列、供应商限流／预算和多用户只读访问，范围见 `docs/HIGH_CONCURRENCY_REQUIREMENTS.md`

## 服务与鉴权边界

| 服务 | 默认地址 | 用途 | 鉴权 |
| --- | --- | --- | --- |
| Fastify Web 服务 | `127.0.0.1:8787` | 页面、浏览器 API、本地音频 Range | 单用户 Basic Auth 或多用户会话 Cookie |
| FastAPI 领域服务 | `127.0.0.1:8788` | 审核、交付、账号与授权 RPC | 内部 `X-Domain-Token`；登录／会话接口由 Fastify 调用 |
| PostgreSQL | `127.0.0.1:5432` | 任务、账号、会话、授权、预算与迁移 | 数据库凭据，不暴露公网 |
| Redis | `127.0.0.1:6379` | 限流和并发租约 | 内部协调，不保存权威业务状态 |
| Cloudflare R2 | 配置的 S3 endpoint | 已批准音频、字幕和看点对象 | 私有桶 + 短期签名 URL |

多用户模式设置 `PODCAST_MULTIUSER=1`。生产 HTTPS 时设置 `PODCAST_AUTH_SECURE_COOKIE=1`。账号创建不开放公共注册，由管理员使用 `state.postgres.user_cli`。

## 对外 HTTP API（Fastify）

代码入口：`review/src/server.ts`；鉴权与登录：`review/src/auth.ts`；本地音频：`review/src/media.ts`。

### 页面与登录

| 方法与路径 | 功能 | 鉴权与说明 |
| --- | --- | --- |
| `GET /login.html` | 多用户登录页 | 公开；已有有效会话后可进入应用 |
| `GET /login` | 登录页别名 | 公开 |
| `POST /login` | 邮箱密码登录，创建会话 | 调用内部 `/auth/login`；写 `podcast_session` HttpOnly Cookie |
| `POST /logout` | 退出登录 | 删除服务端会话并清除 Cookie |
| `GET /`、`GET /index.html` | 主应用页面 | 需要有效会话；hash 路由继续使用前端页面 |
| `GET /app.js`、`/player.js`、`/styles.css` | 前端静态资源 | 按当前鉴权模式保护；登录页所需资源公开 |

前端 hash 页面：

- `#/review`：审核箱
- `#/library`：已发布节目库
- `#/usage`：用量与费用
- `#/episodes/{id}`：审核详情
- `#/library/episodes/{id}`：已发布节目详情

### 健康检查与用量

| 方法与路径 | 功能 | 主要参数 |
| --- | --- | --- |
| `GET /api/health` | Fastify 健康检查 | 无；公开 |
| `GET /api/usage` | 只读消费记录、汇总、筛选和异常状态 | `episodeId`、`purpose`、`status`、`provider`、`limit` |

用量用途包括 `transcribe`、`translate`、`review`、`research`、`highlights`、`classify`；状态包括 `intent`、`success`、`failed`、`unknown`。金额区分账本估算、模型返回成本和未对账，不把 NULL 显示为零。

实现文件：`state/ledger_report.py`、`review/cli.py`、`review/src/server.ts`。

### 审核箱与审核详情

| 方法与路径 | 功能 | 主要输入 |
| --- | --- | --- |
| `GET /api/review/episodes` | 当前待审核、驳回、待发布节目列表；当前已交付版本自动排除 | 无 |
| `GET /api/episodes/:episodeId/review` | 双语字幕、当前修订、候选版本、倒计时、冲突和 override 状态 | `episodeId` |
| `POST /api/episodes/:episodeId/overrides` | 保存人工修改并生成新审核修订 | `baseRevisionId`、`baseEntryNumber`、`sourceArtifactId`、`startMs`、`endMs`、`sourceText`、`afterText`；可选 `supersedesOverrideId`、`retracted` |
| `POST /api/episodes/:episodeId/variant` | 切换复核版／重切分版并重新应用人工修改 | `baseRevisionId`、`variant=review/split` |
| `POST /api/episodes/:episodeId/approve` | 人工通过当前确定版本，进入交付 | `baseRevisionId` + `Idempotency-Key` |
| `POST /api/episodes/:episodeId/hold` | 保留待审，暂停自动审批 | `baseRevisionId` + `Idempotency-Key` |
| `POST /api/episodes/:episodeId/resume` | 恢复完整 12 小时审核窗口 | `baseRevisionId` + `Idempotency-Key` |
| `POST /api/episodes/:episodeId/reject` | 按时间范围驳回，创建局部重跑请求 | `baseRevisionId`、`reason`、`ranges[{startMs,endMs}]` + `Idempotency-Key` |
| `POST /api/episodes/:episodeId/revisions` | 已批准／已发布节目再次编辑，创建新修订 | `baseRevisionId` + `Idempotency-Key` |

所有写请求要求 `Idempotency-Key`。旧 revision、重复请求、冲突和 hash 不一致不会覆盖新内容。

权限语义：

- `owner`／`admin` 可访问全部授权范围内资源
- 普通用户需 Episode 授权
- 读取使用 `read`
- 修改、切换、驳回和再次编辑使用 `review`
- 审批使用 `approve`
- 未授权资源按不存在处理，避免泄露内容

### 节目库与音频

| 方法与路径 | 功能 | 说明 |
| --- | --- | --- |
| `GET /api/library/episodes` | 当前用户可见的全部已发布节目 | 普通用户按 Episode 授权过滤 |
| `GET /api/library/episodes/:episodeId` | 已发布节目字幕、双语对照、看点、签名音频和下载地址 | 过期可重新签发，不重新翻译或上传 |
| `GET /api/episodes/:episodeId/audio` | 审核期本地固化音频播放 | 支持 HEAD、单段 Range、416 和路径白名单 |
| `HEAD /api/episodes/:episodeId/audio` | 获取音频元数据 | 保留 Content-Length，不返回音频体 |

节目库实现：`delivery/library.py`、`delivery/commands.py`、`review/src/server.ts`。签名只限定 bucket/key，不将 R2 桶改为公开。

## 内部 FastAPI RPC

代码入口：`review/api.py`。默认不对公网开放；Fastify 的 `review/src/python.ts` 负责调用并映射错误。

### 审核 RPC

```text
POST /rpc/review/{command}
X-Domain-Token: <内部令牌>
Content-Type: application/json
```

支持命令：

| command | 功能 |
| --- | --- |
| `usage-report` | 账本汇总、筛选和异常查询 |
| `inbox` | 审核箱查询 |
| `detail` | 当前审核修订、双语 cue 和冲突 |
| `media-info` | 审核期本地音频白名单路径与 hash |
| `create-revision` | 创建新审核修订与自动审批窗口 |
| `save-override` | 保存人工修改并渲染新版本 |
| `select-variant` | 选择复核版或重切分版 |
| `hold` / `resume` | 暂停／恢复自动审批 |
| `approve` | 人工或超时审批，绑定版本与 SHA256 |
| `reject` | 保存驳回原因与局部重跑范围 |
| `revise-published` | 创建发布后修订，保留旧当前交付 |
| `auto-approve-due` | 扫描到期待审版本并自动审批；系统 cron 无用户身份也可执行，浏览器普通用户不能调用 |

### 交付 RPC

```text
POST /rpc/delivery/{command}
X-Domain-Token: <内部令牌>
```

| command | 功能 |
| --- | --- |
| `publish-approved` | 已批准版本的看点检查、R2 上传、校验与当前发布切换 |
| `mark-highlights-failed` | 标记不可自动重试的看点失败，供交付 worker 使用 |
| `library` | 当前用户可见节目库列表 |
| `detail` | 当前发布详情、字幕、看点与动态签名 |

代码：`delivery/commands.py`、`delivery/publish.py`、`delivery/library.py`。

### 多用户认证 RPC

代码入口：`review/auth_api.py`；共享内部令牌校验：`review/domain_auth.py`。

| 方法与路径 | 功能 | 鉴权 |
| --- | --- | --- |
| `POST /auth/login` | 校验邮箱密码，创建随机会话 | Redis 登录限流；无需已登录会话 |
| `POST /auth/session` | 校验会话 token 是否有效、未过期且用户 active | 原始 token 只在登录时返回，数据库保存 hash |
| `POST /auth/logout` | 撤销会话 | 退出后原 token 不再有效 |
| `POST /auth/grant-check` | 检查用户对 Episode／资源的权限 | 需要内部 `X-Domain-Token` |

## CLI 接口

### 发现层

代码：`discover/cli.py`、`discover/poller.py`、`discover/classify.py`。

| 命令 | 功能 |
| --- | --- |
| `feed-add --name --url [--show] [--process-existing]` | 添加 feed；默认首轮只建立历史基线 |
| `feed-list` | 查询 feed 配置、条件请求和错误状态 |
| `feed-disable ID` | 禁用轮询，不影响在途 Episode |
| `poll [--feed-id ID] [--classify]` | 条件请求、候选去重和入队；`--classify` 才启用付费分类 |

### 审核与交付

代码：`review/cli.py`、`delivery/cli.py`。

| 命令 | 功能 |
| --- | --- |
| `review.cli inbox/detail/...` | 与内部审核 RPC 同义的本地命令 |
| `review.cli auto-approve-due` | 到期自动审批 |
| `delivery.cli publish-approved` | 发布已批准版本 |
| `delivery.cli mark-highlights-failed` | 标记不可自动重试的看点失败 |
| `delivery.cli library/detail` | 节目库查询和签名 |

### 执行流水线

| 命令 | 功能 | 代码 |
| --- | --- | --- |
| `pipeline.scribe transcribe` | Scribe 转写，默认 unsafe；显式 `--rebill-attempt N --direct` 才允许人工接受的新付费尝试 | `pipeline/scribe.py` |
| `pipeline.scribe fetch` | 凭 transcription_id 免费 GET 恢复，必要时 `--call-ref` 指定 re-bill 账本 | `pipeline/scribe.py` |
| `pipeline.scribe mark-unknown` | 显式补记结果未知，禁止自动重发 | `pipeline/scribe.py` |
| `pipeline.wordrender` | 词级 JSON → create-only SRT 与 provenance | `pipeline/wordrender.py` |
| `pipeline.orchestrate prepare/fix/apply/finalize` | 翻译主链确定性编排 | `pipeline/orchestrate.py` |
| `pipeline.rerun prepare/apply-translate/finalize` | 局部驳回重跑，不重新处理无关阶段 | `pipeline/rerun.py` |
| `pipeline.episode_flow download/status` | 音频固化、重试、状态推进与产物查询 | `pipeline/episode_flow.py` |
| `pipeline.episode_flow pending` | 当前 discovered 期数查询 | `pipeline/episode_flow.py` |
| `pipeline.episode_flow acquire/heartbeat/release` | 兼容旧执行租约的 CLI；主队列使用 PostgreSQL job | `pipeline/episode_flow.py`、`state/execution.py` |
| `pipeline.provider_gate acquire/release` | Redis 限流与并发租约 | `pipeline/provider_gate.py` |

### PostgreSQL 队列与多用户

| 命令 | 功能 | 代码 |
| --- | --- | --- |
| `state.postgres.migrations` | PostgreSQL schema 版本迁移 | `state/postgres/migrations.py` |
| `state.postgres.migrate_from_sqlite --source ...` | 保留 id、hash、call_ref 和当前发布版本的迁移与对账 | `state/postgres/migrate_from_sqlite.py` |
| `state.postgres.job_cli enqueue-episode` | Episode 幂等入队 | `state/postgres/job_cli.py` |
| `state.postgres.job_cli claim` | `SKIP LOCKED` 领取，含预算与租约检查 | `state/postgres/job_cli.py` |
| `state.postgres.job_cli heartbeat` | 续约 | 同上 |
| `state.postgres.job_cli complete` | 完成并写任务尝试结果／用量 | 同上 |
| `state.postgres.job_cli fail` | 失败、重试或 unknown 处理 | 同上 |
| `state.postgres.job_cli stats` | 队列状态统计 | 同上 |
| `state.postgres.user_cli create-user` | 创建账号；密码从 `PODCAST_NEW_USER_PASSWORD` 读取，不进命令行 | `state/postgres/user_cli.py` |
| `state.postgres.user_cli grant` | 授予 Episode read 等权限 | 同上 |
| `state.postgres.user_cli create-session / validate-session` | 创建／校验会话 | 同上 |
| `state.postgres.user_cli set-budget` | 设置全局／用户／Episode 预算 | 同上 |

## Runner 工具接口

### 单期 Agent 工具

代码：`runner/src/episode-agent.ts`。

| 工具 | 功能 | replay |
| --- | --- | --- |
| `episode_download` | 下载、固化、登记并推进 downloaded | safe |
| `episode_status` | Episode 状态与产物查询 | safe |
| `pipeline_transcribe` | Scribe 付费转写 | unsafe |
| `pipeline_wordrender` | 词级重组为 SRT 与 provenance | safe |
| `pipeline_orchestrate` | prepare／fix／apply／finalize | safe |
| `episode_read` | 工作区文本分段读取 | safe |
| `episode_write_context` | 原子写 brief/gaps/glossary/review_notes | safe |
| `episode_edit_transcript` | en_fix.srt 唯一定点替换 | safe |
| `episode_dispatch` | 翻译／复核／调研子代理派发 | unsafe |

路径只允许当前 Episode 工作区；只读参考文件额外允许 translate-srt skill 与知识库白名单。转写和模型派发失败或 unknown 时不自动重试。

### 派发与调度工具

| 文件／工具 | 功能 |
| --- | --- |
| `runner/src/review-tools.ts` / `review_dispatch` | 按 JSON dispatch 文件派发 rerun／highlights，付费 unsafe |
| `runner/src/drive-episode.ts` | 单期真实 Agent 执行入口 |
| `runner/src/drive-pending.ts` | Episode 幂等入队、多 worker 领取、预算、租约与失败回写 |
| `runner/src/drive-delivery.ts` | 补齐 highlights、再次发布、失败隔离 |
| `runner/src/drive-rerun.ts` | 局部重跑真实派发入口 |
| `runner/src/drive-highlights.ts` | 真实看点派发入口 |
| `runner/src/provider-gate.ts` | Runner 使用 Redis 闸门的窄包装 |

## 外部服务接口

| 服务 | 调用 | 功能与安全约束 |
| --- | --- | --- |
| RSS／Atom feed | HTTP GET，ETag／Last-Modified，304 | 只处理元数据；新 feed 首轮默认历史基线；不下载 enclosure |
| ElevenLabs Scribe | `POST /v1/speech-to-text` | 词级时间戳、说话人分离；付费 unsafe，结果未知不自动重发 |
| ElevenLabs Scribe | `GET /v1/speech-to-text/transcripts/{transcription_id}` | 凭 ID 免费恢复原始响应 |
| DeepSeek Chat Completions | `POST /chat/completions` | 分类、翻译、复核、调研、看点；使用 JSON mode 与 usage 记录 |
| Cloudflare R2 | `put_object`、`head_object`、`generate_presigned_url(get_object)` | 内容寻址对象、SHA256 metadata、HEAD 校验、短期私有签名 |
| Redis | `TIME`、Lua `EVAL`、Hash／TTL | 分布式限流和并发租约，不保存权威业务状态 |

## 通用错误码

| 错误码 | 含义 |
| --- | --- |
| `validation_error` | 参数或输入内容不合法 |
| `unauthorized` | 登录、会话或内部令牌无效 |
| `not_found` | 资源不存在或当前用户无权访问 |
| `stale_revision` | 页面基于旧审核修订提交 |
| `invalid_state` | 当前状态不允许该操作 |
| `override_conflict` | 人工修改无法唯一映射，阻止审批／发布 |
| `artifact_mismatch` | 文件、hash 或 Episode 关联不一致 |
| `delivery_not_ready` | 看点、上传或校验尚未完成 |
| `provider_rate_limited` | Redis 供应商速率或并发限制 |
| `budget_exceeded` | 预算上限阻止任务执行 |
| `cost_unknown` | 有预算限制但本次成本未知，失败关闭 |
| `internal_error` | 未预期内部错误；不回显秘密或完整敏感内容 |

## 当前不包含的接口

- 公共注册、公开分享和公开 RSS 发布接口
- 将 Redis 当作任务或账本权威存储的接口
- 绕过人工／超时审批直接上传的接口
- 让普通用户无限创建付费任务的接口
- 未经过实际实现与测试的 PostgreSQL 管理接口或生产 QPS 承诺
