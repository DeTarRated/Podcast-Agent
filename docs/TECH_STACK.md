# 技术栈与工程设计

> 定位：记录本项目的技术选型与工程设计细节。模块归属以 `ARCHITECTURE.md` 为准，需求以 `REQUIREMENTS.md` 为准，阶段安排与验证时机以 `IMPLEMENTATION_PLAN.md` 为准。本文不重议已定事项；带「草案」标记的内容待对应实施阶段定稿，带「待验证」标记的内容由垂直切片验证后回填。

## 状态标记约定

| 标记 | 含义 |
| --- | --- |
| 已定 | REQUIREMENTS / ARCHITECTURE 等权威文档既定 |
| 讨论结论 | `.agent/knowledge-base.md` 的讨论结论，需回填进需求数据模型 |
| 待验证 | 垂直切片等阶段实验后才能定，当前不当作已具备能力 |
| 草案 | 实施阶段定稿，当前仅作设计起点 |
| 待选 | 有候选清单，按「决策时机」列在对应阶段完成选型 |

## 已定技术项与讨论结论

| 模块 / 环节 | 技术 | 来源 |
| --- | --- | --- |
| 发现层·调度 | cron + Python discover（自研） | ARCHITECTURE |
| 执行层·骨架 | Pi Durable + 自研 Runner；流水线工具经 bash 调 Python | ARCHITECTURE |
| 执行层·转写 | ElevenLabs Scribe v2（说话人分离 + 词级时间戳）；Scribe 词级 JSON 重组为一句一条英文 SRT 是项目侧新组件，不在 translate-srt 脚本内 | REQUIREMENTS / IMPLEMENTATION_PLAN |
| 执行层·翻译 | translate-srt skill 全套流程（srt_tools.py / kb_tools.py、子代理角色定义复用） | REQUIREMENTS / IMPLEMENTATION_PLAN |
| 审核层 | 静态审核页 + Node 审批服务 + override 表 + 评测集（自研） | ARCHITECTURE |
| 交付层 | 模板生成 + boto3 上传 R2 + S3 签名 URL（自研）；使用 Cloudflare R2，不用 WebDAV | ARCHITECTURE / REQUIREMENTS |
| 知识库 | 纯文本 + 独立 git 仓库，kb_tools.py 读写，项目代码之外 | REQUIREMENTS / skill |
| 流程状态存储 | SQLite 存程序读写数据（Episode、override、账本、评测集等），知识库不存流程状态 | 讨论结论（待回填需求） |

末行「流程状态存储」为讨论结论（出处 `.agent/knowledge-base.md`），不属权威已定，待回填需求数据模型。

## 语言与运行时（草案）

- Python：发现、流水线工具、Runner 周边脚本。版本与依赖管理在垂直切片定稿；依赖清单与锁文件进仓库，凭据不入库。
- Node：审批服务。版本与框架见「库与框架候选」。
- 运行时约束：VPS 单机；无任务时除 cron 外无常驻进程，执行层按需唤醒、空闲退出。
- 凭据与环境变量：Scribe API key、模型凭据、R2 密钥（最小权限）、分类 LLM 凭据，统一走环境变量或受限配置文件，日志与仓库不出现密钥。

## 库与框架候选（待选）

| 用途 | 候选 | 状态 | 决策时机 |
| --- | --- | --- | --- |
| RSS 解析与条件请求 | feedparser / 标准库解析 | 待选 | 自动化主链 |
| HTTP 客户端（Lookup、下载） | requests / httpx | 待选 | 垂直切片（下载工具） |
| 转写客户端 | ~~ElevenLabs 官方 SDK / 直接 HTTP~~ 已定：直接 HTTP（httpx） | 已定 | 已完成（垂直切片·本地地基） |
| 分类 LLM 客户端 | 按所选便宜模型供应商 SDK | 待选 | 自动化主链 |
| Node 审批服务框架 | Express / Fastify | 待选 | 审核与交付 |
| 审核页前端 | 静态 HTML + JS，原生或轻量框架 | 待选 | 审核与交付 |
| SQLite 驱动 | Python stdlib sqlite3 / SQLAlchemy；Node 侧按所选框架配套 | 待选 | 自动化主链 |
| 测试框架 | pytest / 内置 unittest | 待选 | 垂直切片 |
| Python 依赖与虚拟环境 | uv / pip + venv | 待选 | 垂直切片 |

## 存储与 schema（草案）

SQLite 单文件 + 备份；知识库仍是文本 + git（见上文「已定技术项与讨论结论」）。表结构与字段在自动化主链阶段与状态机、账本汇总口径一起评审定稿。

| 数据 | 关键字段 | 来源 |
| --- | --- | --- |
| feed 配置 | 名称、feedUrl、所属节目、ETag、Last-Modified、启用状态 | REQUIREMENTS / ARCHITECTURE |
| Episode | guid、feed、feed 快照、标题、发布时间、时长、enclosure、音频 hash、状态、产物清单引用、熔断计数、审批通知 memo | REQUIREMENTS 数据模型 + IMPLEMENTATION_PLAN 补充（feed 快照需回填需求）；熔断计数、审批通知 memo 为本草案新增（依据 ARCHITECTURE 熔断/memo 机制），待自动化主链与状态机一起评审定稿 |
| 产物清单 | 路径、版本、校验、生成步骤、状态 | IMPLEMENTATION_PLAN |
| override | 源句／时间范围、版本、修改前后文本、修订号、冲突标记 | IMPLEMENTATION_PLAN（唯一映射契约在审核与交付定稿） |
| 账本 | 所属期数、用途、触发时间、模型／服务、用量依据、金额估算、调用标识、结果状态 | IMPLEMENTATION_PLAN（schema 归执行层，发现层分类跨层写入） |
| 评测集 | 原文、模型版本、修改前后文本、时间范围、问题类别；唯一键 = 源句 + 时间范围 + 修改前后文本 hash | IMPLEMENTATION_PLAN |
| 幂等与对账 | 转写幂等键 = guid + 音频 hash；intent／outcome 记录支撑 unsafe 调用恢复 | REQUIREMENTS / IMPLEMENTATION_PLAN |

## 目录与部署拓扑（草案）

### 仓库目录

- `docs/`：REQUIREMENTS、ARCHITECTURE、IMPLEMENTATION_PLAN、TECH_STACK（当前在根目录，迁入 docs/ 为草案目标）
- `skills/`：translate-srt（当前在仓库内，保留现状或按引入方式调整）
- `discover/`：feed 配置、轮询、分类、入队
- `runner/`：Pi Durable 集成、checkpoint、工具封装
- `pipeline/`：下载固化、词级重组、转写、翻译工作区编排（调用 translate-srt）
- `review/`：静态审核页、审批服务、override
- `delivery/`：看点页模板、R2 上传、签名 URL
- `state/`：schema、迁移、查询入口
- `eval/`：评测样本与指标脚本
- `tests/`

实际布局实施时按需调整，本表是设计起点。

### 部署拓扑

- VPS 单机；cron 每 15 分钟触发 discover，条件请求 304 时零后续动作。
- 执行进程按需启动：存在待处理期才运行，队列清空即退出；审核等待不占用模型会话运行。
- 审核页与审批服务按需启动，审核期直接读本地产物；R2 仅在批准后上传。
- R2 私有桶 + 预签名 URL；备份目的地独立于工作数据。
- 知识库为独立 git 仓库，不随本仓库公开提交。
- 审核入口、续签入口等按需启动方式待目标 VPS 环境实测（打磨阶段部署验收）。

## 待定项（垂直切片验证后回填）

- 子代理（翻译／复核）在 Pi Durable 中的接入方式：扩展任务 / 嵌套任务 / 独立 conversation。
- fork / steer 在本项目的实际用法与禁用场景。
- Durable 的 checkpoint、safe / unsafe、effect sandwich、memo、parked 实际行为，及 Runner 仍需补齐的边界。
- Scribe 幂等键的服务端支持、作用域、有效期与异常窗口语义。
- 本节各项在垂直切片验证完成后回填；其余机制性验证按阶段归属执行（如按需启动见部署拓扑）。

## 选型决策记录

| 决策 | 候选 | 结论 | 依据 | 状态 |
| --- | --- | --- | --- | --- |
| 流程状态存储 | SQLite / 文本知识库 | 双存储分工 | 程序读写数据用 SQLite；知识库保持文本可读、可 git diff | 讨论结论（待回填需求） |
| 词级重组组件 | 自研 / 复用上游 render 类脚本 | 待定 | 「项目侧新组件」为已定事实（计划已标注），自研 / 复用上游 render 脚本的选择属待验证 | 待验证 |
| 转写客户端 | ElevenLabs 官方 SDK / 直接 HTTP | 直接 HTTP（httpx） | 本项目仅用 Scribe 一小面（POST /speech-to-text/convert + GET /transcripts/{id}），REST 与 SDK 均可获取 word 级时间戳与说话人分离；直接 HTTP 便于携带幂等键头做服务端语义验证、原始响应落盘、复用已定 httpx，避免大而全 SDK 的版本 churn 与无关升级牵连 | 已定 |
| 其余待选库 | 见候选表 | 未定 | 分别在标注的决策时机完成 | 待选 |

待选与待验证项在对应阶段决策后补录进本表，含结论与依据。
