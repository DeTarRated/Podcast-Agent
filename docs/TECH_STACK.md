# 技术栈与工程设计

> 定位：记录本项目的技术选型与工程设计细节。模块归属以 `ARCHITECTURE.md` 为准，需求以 `REQUIREMENTS.md` 为准，阶段安排与验证时机以 `IMPLEMENTATION_PLAN.md` 为准。本文不重议已定事项；带「草案」标记的内容待对应实施阶段定稿，带「待验证」标记的内容由最小 MVP 验证后回填。
>
> 接口清单与代码入口见 `docs/API_REFERENCE.md`；逐表字段见 `docs/DATA_MODEL.md`；高并发技术实现、专项范围与验收指标见 `docs/HIGH_CONCURRENCY_REQUIREMENTS.md`。

## 状态标记约定

| 标记 | 含义 |
| --- | --- |
| 已定 | REQUIREMENTS / ARCHITECTURE 等权威文档既定 |
| 讨论结论 | `.agent/knowledge-base.md` 的讨论结论，需回填进需求数据模型 |
| 待验证 | 最小 MVP 等阶段实验后才能定，当前不当作已具备能力 |
| 草案 | 实施阶段定稿，当前仅作设计起点 |
| 待选 | 有候选清单，按「决策时机」列在对应阶段完成选型 |

## 已定技术项与讨论结论

| 模块 / 环节 | 技术 | 来源 |
| --- | --- | --- |
| 发现层·调度 | cron + Python discover（自研） | ARCHITECTURE |
| 执行层·骨架 | Pi Durable + 自研 Runner；流水线工具经 bash 调 Python | ARCHITECTURE |
| 执行层·转写 | ElevenLabs Scribe v2（说话人分离 + 词级时间戳）；Scribe 词级 JSON 重组为一句一条英文 SRT 是项目侧新组件，不在 translate-srt 脚本内 | REQUIREMENTS / IMPLEMENTATION_PLAN |
| 执行层·翻译 | translate-srt skill 全套流程（srt_tools.py / kb_tools.py、子代理角色定义复用） | REQUIREMENTS / IMPLEMENTATION_PLAN |
| 审核层 | 静态审核箱与审核页 + Fastify HTTP 适配 + Python 审核领域与 SQLite 事务 + 12 小时超时自动审批 + override 表；评测后置 | ARCHITECTURE / REQUIREMENTS / 代码实现规格 |
| 交付层 | 私人播客库 + 模板生成 + boto3 上传 R2 + 动态 S3 签名 URL（自研）；使用 Cloudflare R2，不用 WebDAV | ARCHITECTURE / REQUIREMENTS |
| 知识库 | 纯文本 + 独立 git 仓库，kb_tools.py 读写，项目代码之外 | REQUIREMENTS / skill |
| 流程状态存储 | 当前 SQLite；并发改造新增 PostgreSQL 持久化队列与业务表，Redis 只存短期协调数据 | REQUIREMENTS / ARCHITECTURE / `.agent/high-concurrency-architecture.md` |

## 语言与运行时（草案）

- Python：发现、流水线工具、Runner 周边脚本。版本与依赖管理在最小 MVP 定稿；依赖清单与锁文件进仓库，凭据不入库。
- Node：Fastify v5 审批 HTTP 适配、静态页与本地音频流；Python 经窄 CLI 统一执行审核与交付领域事务，Node 不直接连接 SQLite。
- 运行时约束：VPS 单机；FastAPI 领域服务与 Fastify 审核入口由 systemd 长驻，执行层按需唤醒、空闲退出；超时审批与交付由定时扫描触发，业务 cron 尚未开启。
- 凭据与环境变量：Scribe API key、模型凭据、R2 密钥（最小权限）、分类 LLM 凭据，统一走环境变量或受限配置文件，日志与仓库不出现密钥。

## 库与框架候选（待选）

| 用途 | 候选 | 状态 | 决策时机 |
| --- | --- | --- | --- |
| RSS 解析与条件请求 | 已定：feedparser + httpx | 已定 | 自动化主链 |
| HTTP 客户端（Lookup、下载） | requests / httpx | 待选 | 最小 MVP（下载工具） |
| 转写客户端 | ~~ElevenLabs 官方 SDK / 直接 HTTP~~ 已定：直接 HTTP（httpx） | 已定 | 已完成（最小 MVP·基础架构） |
| 分类 LLM 客户端 | 已定：DeepSeek Chat Completions 直接 HTTP（httpx） | 已定 | 自动化主链 |
| Node 审批服务框架 | ~~Express / Fastify~~ 已定：Fastify v5 | 已定 | 审核与私人播客库 MVP |
| 审核页前端 | 已定：静态 HTML + 原生 JavaScript，无构建步骤 | 已定 | 审核与私人播客库 MVP |
| SQLite 驱动 | 已定：Python stdlib sqlite3 统一领域事务；Node 经窄 CLI，不直接连接 | 已定 | 审核与私人播客库 MVP |
| 测试框架 | pytest / 内置 unittest | 待选 | 最小 MVP |
| Python 依赖与虚拟环境 | uv / pip + venv | 待选 | 最小 MVP |

## 存储与 schema（草案）

SQLite 单文件 + 备份；知识库仍是文本 + git（见上文「已定技术项与讨论结论」）。逐表字段、约束、职责与代码文件见 `docs/DATA_MODEL.md`；PostgreSQL 计划表与当前实现分开标记。

| 数据 | 关键字段 | 来源 |
| --- | --- | --- |
| feed 配置 | 名称、feedUrl、所属节目、ETag、Last-Modified、启用状态 | REQUIREMENTS / ARCHITECTURE |
| Episode | guid、feed、feed 快照、标题、发布时间、时长、enclosure、音频 hash、状态、产物清单引用、当前待审版本、auto_approve_at、自动审批暂停标记、熔断计数、审批通知 memo | REQUIREMENTS 数据模型 + IMPLEMENTATION_PLAN 补充；审核字段在审核与播客库 MVP 落迁移 |
| 产物清单 | 路径、版本、校验、生成步骤、状态 | IMPLEMENTATION_PLAN |
| override | 源稿版本、开始／结束时间、原文 hash、被修改产物版本、修改前后文本、请求幂等键、创建时间；历史追加保留 | REQUIREMENTS / IMPLEMENTATION_PLAN；SRT 条目号不作稳定主键 |
| 执行租约 | Episode、owner、获取时间、过期时间；默认六小时并续约，防 cron 重叠执行同一期 | IMPLEMENTATION_PLAN / 自动调度实现 |
| 审批记录 | 最终产物版本、SHA256、manual / auto_timeout、审批时间、请求标识 | REQUIREMENTS / ARCHITECTURE |
| 发布记录 | 当前上线版本、交付对象与校验信息、发布时间；修订时保留旧版直至新版完整交付 | REQUIREMENTS / ARCHITECTURE |
| 账本 | 所属期数、用途、触发时间、模型／服务、用量依据、金额估算、调用标识、结果状态 | IMPLEMENTATION_PLAN（schema 归执行层，发现层分类跨层写入） |
| 评测集 | 后续阶段建设；可从 override 历史补建样本，不把抽检未修改条目视为正确 | IMPLEMENTATION_PLAN |
| 幂等与对账 | 转写幂等键 = guid + 音频 hash；intent／outcome 记录支撑 unsafe 调用恢复 | REQUIREMENTS / IMPLEMENTATION_PLAN |

## 目录与部署拓扑（草案）

### 仓库目录

目录按 ARCHITECTURE 四层划分归属，层间只经数据契约与接口边界交互，不跨层直调：

| 目录 | 所属分层 | 职责 |
| --- | --- | --- |
| `docs/` | — | 需求与设计文档 |
| `skills/` | — | translate-srt（复用，不修改） |
| `discover/` | 发现层（调度模块） | feed 配置、轮询、分类、入队 |
| `runner/` | 执行层（Agent 执行模块） | Pi Durable 集成、Runner 接口、checkpoint、工具封装 |
| `pipeline/` | 执行层（Agent 执行模块） | 下载固化、词级重组、转写、翻译工作区编排（调用 translate-srt） |
| `state/` | 跨层数据契约（schema 归执行层维护） | schema、迁移、查询入口；Episode / 产物清单 / 账本 / override / 审批 / 发布记录 |
| `review/` | 审核层（审核模块） | Fastify HTTP 适配、Python 审核领域与 CLI、审核箱、静态页、超时自动审批、override |
| `eval/` | 审核层（后续评测模块） | 本轮不实现；后续评测样本与指标脚本 |
| `delivery/` | 交付层（私人播客库与交付模块） | 节目库与播放页、看点模板、R2 上传、动态签名 URL、发布指针 |
| `tests/` | — | 各层模块测试 |

层间边界（接口归属）：发现层 → 执行层 = Episode 记录与入队；执行层 ↔ 审核层 = 本地待审核产物目录 + 驳回原因与重跑范围；审核层 → 交付层 = 已批准版本；执行层 → 交付层 = approved 产物。发现层分类的付费调用经执行层维护的账本 schema 跨层写入。

实际布局实施时按需调整，本表是设计起点。

### 部署拓扑

- VPS 单机；PostgreSQL 16 与 Redis 7 使用 Docker Compose 持久化，端口只绑定 `127.0.0.1`。
- FastAPI 领域服务与 Fastify 审核入口由 systemd 长驻，分别监听 `127.0.0.1:8788` 与 `127.0.0.1:8787`。
- 当前公网只开放 SSH，通过 SSH 隧道访问审核入口；HTTPS 尚未配置。
- 执行进程按需启动：存在待处理期才运行，队列清空即退出；审核等待不占用模型会话运行。
- 人工或 12 小时超时审批后才上传 R2；自动审批与交付 cron 尚未开启。
- 私人播客库使用稳定私有节目地址；R2 私有桶 + 动态预签名 URL，过期重新签发。
- 发布后修订期间发布指针继续指向旧版，新版上传与校验完整后才切换。
- 知识库为独立 git 仓库，不随本仓库公开提交。
- 当前备份只保存到 VPS 独立目录，不包含 `.env` 或 SSH 私钥；异地副本与完整恢复演练待做。

当前 VPS 实施：

| 项目 | 当前值 |
| --- | --- |
| 系统与规格 | Ubuntu 24.04 LTS；2 核、4 GB、60 GB SSD；新加坡 |
| 代码目录 | `/home/ubuntu/podcast-agent` |
| 生产环境文件 | `/home/ubuntu/podcast-agent/.env`，权限 600，不入库 |
| 服务端运行配置 | `/home/ubuntu/podcast-agent-runtime/`，目前不随 GitHub 分发 |
| Python | uv 管理 3.12.16，避免 Ubuntu 自带 3.12.3 的 SRT 参数解析兼容问题 |
| Node | 22 LTS，使用内置 TypeScript strip-types |
| 领域服务 | `podcast-domain.service` |
| 审核服务 | `podcast-review.service` |
| 本机备份 | `podcast-backup.timer` / `podcast-backup.service` |
| 备份目录 | `/home/ubuntu/backups/podcast-agent/` |
| 备份策略 | 每天北京时间约 03:30；最近 7 天 + 更早 4 个每周备份 |

服务器部署回归：Python 166 passed／20 skipped、Runner 28 passed、Review 14 passed；两期节目、账本、审批和当前交付已迁移并对账。生产性能数字尚未测量，不能沿用本地压测结果。

### cron 唤醒顺序（待部署验收）

下列命令分别执行并退出，不启动常驻 worker；本地真实全链与 VPS 服务回归已通过，但业务 cron 尚未开启，实际安装前仍需明确真实付费调用边界：

```bash
# 每 15 分钟发现新节目
uv run python -m discover.cli poll --classify
# 有界处理 discovered 期数（持久化 execution_lock 互斥）
node --experimental-strip-types runner/src/drive-pending.ts
# 扫描到期的 12 小时审核窗口
uv run python -m review.cli auto-approve-due
# 补齐看点并发布已批准版本
node --experimental-strip-types runner/src/drive-delivery.ts
```

调度器不强杀在途子进程，避免制造未知付费结果；交付 worker 区分可重试上传失败与不自动重试的 highlights 失败。

## 待定项（最小 MVP 验证后回填）

- 子代理（翻译／复核）在 Pi Durable 中的接入方式：扩展任务 / 嵌套任务 / 独立 conversation。
- fork / steer 在本项目的实际用法与禁用场景。
- Durable 的 checkpoint、safe / unsafe、effect sandwich、memo、parked 实际行为，及 Runner 仍需补齐的边界。
- Scribe 幂等键的服务端支持、作用域、有效期与异常窗口语义：已查证（2026-11，文档 + 受控实测）——官方文档无幂等键机制（llms-full.txt 零命中）；实测同音频同自定义键重发两次产生两个不同 `transcription_id`，即无去重；对账手段为服务端 `transcription_id` + `GET /v1/speech-to-text/transcripts/{id}`（实测可用），旧路径 `/v1/speech-to-text/convert` 已 404；计费按发送音频时长秒级计量、无去重声明。证据与影响见 `docs/verification/scribe-idempotency-doc-review.md` 与 `docs/verification/scribe-idempotency-controlled-test.md`。
- 本节各项在最小 MVP 验证完成后回填；其余机制性验证按阶段归属执行（如按需启动见部署拓扑）。

## 选型决策记录

| 决策 | 候选 | 结论 | 依据 | 状态 |
| --- | --- | --- | --- | --- |
| 流程状态存储 | SQLite / 文本知识库 | 双存储分工 | 程序读写数据用 SQLite；知识库保持文本可读、可 git diff | 已定（已回填 REQUIREMENTS） |
| 词级重组组件 | 自研 / 复用上游 render 类脚本 | 待定 | 「项目侧新组件」为已定事实（计划已标注），自研 / 复用上游 render 脚本的选择属待验证 | 待验证 |
| 转写客户端 | ElevenLabs 官方 SDK / 直接 HTTP | 直接 HTTP（httpx） | 本项目仅用 Scribe 一小面（现行文档端点 `POST /v1/speech-to-text` + `GET /v1/speech-to-text/transcripts/{id}`，见幂等键查证；无幂等键可用），REST 与 SDK 均可获取 word 级时间戳与说话人分离；直接 HTTP 便于原始响应落盘、复用已定 httpx，避免大而全 SDK 的版本 churn 与无关升级牵连 | 已定 |
| RSS 解析 | feedparser / 标准库 XML | feedparser（当前锁文件版本） | feedparser 只解析响应 bytes，httpx 负责条件请求；条目 id/guid、enclosures、published_parsed 用法已经 Context7 核对 | 已定 |
| 分类客户端 | 供应商 SDK / 直接 HTTP | DeepSeek 直接 HTTP + JSON mode | 分类只发标题／简介；`response_format=json_object` 与 usage 字段按官方当前文档实现；call_ref 先落 intent，失败／unknown 不自动重发 | 已定 |
| PostgreSQL 队列 | psycopg 3 + 原生 SQL | `FOR UPDATE SKIP LOCKED` + JSONB + 版本迁移 | 支持幂等入队、多 worker、租约接管、尝试历史与 SQLite 对账迁移；`job_cli.py` 已接入实际 `drive-pending`，所有事务显式提交；事务和 Jsonb 用法已经 Context7 核对 | 已定（本地真实测试与队列接入通过） |
| 长驻领域服务 | 每请求 Python CLI / 长驻 FastAPI RPC | FastAPI + Uvicorn + 内部令牌 | 保留 Python 审核／交付领域规则，Fastify 改为内部 HTTP 调用；启动时一次迁移，同步 def 端点在线程池执行；lifespan/TestClient 用法已经 Context7 核对 | 已定（真实 RPC 联调通过） |
| Redis 分布式协调 | 进程内限流 / Redis Lua + Hash 租约 | 令牌桶 + owner/TTL 并发租约 | 使用 Redis TIME、原子 Lua、owner 释放与失败关闭；接入 Scribe、分类与真实 Runner 子代理；权威状态仍在数据库，文档见 `docs/REDIS_COORDINATION.md` | 已定（真实 Redis 测试通过） |
| 多用户认证与授权 | 共享 Basic Auth / PostgreSQL 账号会话 + Episode 授权 | Argon2id 密码哈希、随机会话 token 哈希、resource_grant、预算与审计 | 登录走 FastAPI，Fastify 处理 Cookie 与同源写保护；owner/admin 放行，普通用户按 Episode 授权；SQLite 业务与 PostgreSQL 授权分流，SQL 占位符与 IN 查询兼容 | 已定（真实端到端验证通过） |
| 压测与性能证据 | 本地脚本 + 固定负载 + JSON 结果 | `docs/PERFORMANCE_REPORT.md` | 队列 1000 任务／8 worker、管理 API 300 请求／16 并发、Redis 1000 次闸门调用／32 并发；全部零错误，报告明确区分本地 mock 与生产能力 | 已定（本地实测通过） |
| 审核服务接缝 | Node 直写 SQLite / Fastify + Python 领域 CLI | Fastify v5 + Python stdlib sqlite3 | 审核、override、审批与发布事务只在 Python 实现一次；Node 负责 HTTP、鉴权、静态页与音频流，避免原生 SQLite 依赖和双份状态机；Fastify v5 完整 JSON Schema 与 hooks 文档已经 Context7 核对 | 已定 |
| R2 客户端 | boto3 S3 兼容 API | boto3（当前锁文件版本） | put_object 流式 Body、HEAD ContentLength + sha256 metadata、get_object 预签名；使用内容寻址 key，HEAD 不一致拒绝覆盖；API 用法已经 Context7 核对，真实 R2 联调单独验收 | 已定 |
| 其余待选库 | 见候选表 | 未定 | 分别在标注的决策时机完成 | 待选 |

待选与待验证项在对应阶段决策后补录进本表，含结论与依据。
