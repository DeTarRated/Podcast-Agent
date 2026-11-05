# 交接文档 — 播客翻译 Agent

- 日期：2026-11
- 定位：给下一个新会话的接手说明，覆盖「读什么、现状到哪、已定什么、待确认什么、下一步做什么、怎么自检」
- 生成方式：父代理与 worker 子代理两轮迭代（草稿 → 审阅 → 修订），父代理定稿

## 与既有交接文档的关系

- 本文档是实施现状交接，新会话以本文档为准。

## 阅读顺序（先读→后读）

- 先读 `AGENTS.md`：项目沟通规则（禁用编号风格、文档管理规则、测试规范）
- 再读 `docs/REQUIREMENTS.md`：需求与数据模型（权威文档，已定事项不重议）
- 然后读 `docs/ARCHITECTURE.md`：四层模块职责划分与文末「待验证事项」
- 再读 `docs/IMPLEMENTATION_PLAN.md`：阶段划分、任务依赖、产出与验收口径
- 再读 `docs/TECH_STACK.md`：技术选型、schema 草案、目录拓扑、选型决策记录
- 再读 `docs/NEXT_STEPS.md`：最小 MVP 的落地行动清单（当前推进所依据的清单）
- 再读 `.agent/pi-durable-vs-langgraph.md`：执行层骨架选型讨论与风险
- 再读 `.agent/knowledge-base.md`：知识库讨论结论（含未定字段级 gap）
- 再读 `docs/verification/translate-srt-reuse.md`：skill 复用边界自检结论
- 最后读代码：`state/` → `pipeline/` → `runner/src/` → `tests/`（顺序即数据契约→流水线工具→执行骨架→测试）

## 项目现状

### 四层架构（一句话）

发现层（cron 调度入队）→ 执行层（Agent 决策 + 工具循环 + 每步 checkpoint）→ 审核层（人工闸门 + override）→ 交付层（私有签名链接交付）；数据流为「发现层入队 → 执行层加工到待审核 → 审核层人工闸门 → 交付层私有链接」。

### 阶段术语

- 外层五大阶段（权威划分）：最小 MVP → 自动化主链 → 审核与交付 → 长程与沉淀 → 打磨
- 最小 MVP 内部五阶段（`docs/NEXT_STEPS.md` 执行序列）：基础架构 ✅ / 流程测试 / 真实转写 / 翻译与复核 / 收口与回填

当前进度：基础架构已完成；流程测试即将开始，尚未开展。

## 已完成内容（基础架构阶段，逐模块）

| 模块 | 文件 | 职责 | 测试数 | 运行命令 |
| --- | --- | --- | --- | --- |
| 状态存储 | `state/schema.sql`、`state/db.py`、`state/episode.py`、`state/manifest.py`、`state/ledger.py` | SQLite 三表（episode / artifact / ledger）+ WAL/外键/幂等建表；Episode 创建与状态推进；产物清单；intent→outcome 两段式账本 | 9 | `uv run pytest tests/test_state.py` |
| 下载固化 | `pipeline/download.py` | 跟随 302 下载、sha256 固化、时长读取、首版保护（hash 漂移告警） | 4 | `uv run pytest tests/test_download.py` |
| 词级重组 | `pipeline/wordrender.py` | Scribe 词级 JSON → 一句一条英文 SRT（`[S01] ` 前缀）+ provenance sidecar；时间戳缺失明确失败 | 10 | `uv run pytest tests/test_wordrender.py` |
| Runner 薄封装 | `runner/src/runner.ts` | 隔离 Pi Durable 实验 API，暴露 submit / resume / checkpoint | 1 | `cd runner && npm test` |
| 单期 conversation 骨架 | `runner/src/conversation.ts` | 占位工具（download 声明 safe、transcribe 默认 unsafe）+ registry 组装 | 2 | `cd runner && npm test` |
| skill 复用自检 | `docs/verification/translate-srt-reuse.md` | 两脚本 `--self-test` 通过，复用方式定为「经 bash 调子命令，不 import 为库」 | — | `python skills/translate-srt/scripts/srt_tools.py --self-test`、`python skills/translate-srt/scripts/kb_tools.py --self-test` |
| Python 环境 | `pyproject.toml`、`uv.lock` | uv + Python 3.12 + pytest + httpx + mutagen | — | `uv run pytest tests/` |

- Python 测试合计：23 个全部通过（9 + 4 + 10）
- Node 测试合计：3 个全部通过（1 + 2）
- 待建文件（基础架构已列出但未实现）：`pipeline/scribe.py`（Scribe 客户端，选型已定）、`pipeline/orchestrate.py`（translate-srt 全流程编排）

## 锁文件与依赖

- `uv.lock` 与 `runner/package-lock.json` 均已入库。
- 仓库根没有 `package-lock.json`（Node 项目在 `runner/` 子目录，锁文件随 Node 项目位于 `runner/package-lock.json`）。
- 两个锁文件都必须入库，凭据文件不入库（见 `.gitignore`）。

## git 提交状态

### 已提交（4 个 commit，`git log --oneline` 顺序）

| commit | 内容概览 |
| --- | --- |
| `6660e8a` | 仓库骨架：docs 文档、模块目录规划、kickoff 计划、skill 文件导入 |
| `87141d4` | Python 环境定稿：uv + pyproject + uv.lock（httpx / mutagen / pytest） |
| `9ba23bf` | state 模块、下载固化工具、转写客户端选型回填 |
| `561110f` | 最小 MVP：state、下载、词级重组、translate-srt 自检、Runner 薄封装 |

### 未提交（5 个文件，工作区修改，均未 staged）

| 文件 | 改动内容 |
| --- | --- |
| `AGENTS.md` | 新增「测试规范」章节（测试文件头场景注释规范） |
| `.agent/pi-durable-vs-langgraph.md`、`docs/NEXT_STEPS.md`、`docs/TECH_STACK.md`、`docs/verification/translate-srt-reuse.md` | 阶段术语更名收尾（垂直切片→最小 MVP、本地地基→本地代码→基础架构、骨架接通→流程测试） |

- 这批未提交改动本质是「最小 MVP 内部阶段术语统一」的收尾，与本文档的阶段术语一致，提交前无需再逐个拆解。

## 接手第一步

- 先确认并提交这 5 个未提交文件（一次 commit，术语统一收尾），再开始流程测试。
- 不要带着脏工作区继续开发；提交 message 可注明「阶段术语更名收尾 + AGENTS 测试规范」。

## 关键决策记录（本阶段新定，含来源文档）

| 决策 | 结论 | 来源文档 |
| --- | --- | --- |
| 转写客户端 | 直接 HTTP（httpx），不用 ElevenLabs 官方 SDK；依据：本项目只用 Scribe 一小面（POST + GET），便于携带幂等键头、原始响应落盘、复用已定 httpx、避免 SDK 版本 churn | `docs/TECH_STACK.md` 选型决策记录、`docs/NEXT_STEPS.md` 已定小决策 |
| 分层-目录映射 | `discover/`（发现层）、`runner/` + `pipeline/`（执行层）、`state/`（跨层数据契约，schema 归执行层维护）、`review/` + `eval/`（审核层）、`delivery/`（交付层）；层间只经数据契约与接口边界交互 | `docs/TECH_STACK.md` 目录与部署拓扑、`docs/NEXT_STEPS.md` 仓库骨架与归属 |
| Runner 薄封装职责 | 只暴露 submit / resume / checkpoint，隔离 Pi Durable 实验 API；业务工具不直接依赖实验接口细节 | `docs/ARCHITECTURE.md` 执行层、`docs/NEXT_STEPS.md`、`runner/src/runner.ts` |
| 测试文件头场景注释规范 | 每个测试文件开头用几行注释列出本文件模拟的场景清单（正常与异常路径） | `AGENTS.md` 测试规范、`docs/NEXT_STEPS.md` 执行约定 |
| 文档管理规则 | 需求文档为权威；不轻易新增文档，优先更新既有文档；多轮文档先确认再创建子代理；与子代理文档迭代最多 3 轮 | `AGENTS.md` 项目文档 |
| 模块级提交规则 | 一个模块的代码与测试全部通过后 commit 一次，message 注明模块与产出，不必每个任务一次 | `docs/NEXT_STEPS.md` 执行约定 |
| 本阶段其他已定技术项 | uv + pyproject + uv.lock + Python 3.12；pytest；httpx；mutagen；Pi Durable pin 1.5.0；Node LTS（engines >=22.19.0）；首期跳过联网调研、沉淀留到交付后 | `docs/NEXT_STEPS.md` 已定小决策、`runner/package.json`、`pyproject.toml` |

## 执行纪律（本阶段红线）

- 最小 MVP 完成前不扩展 feed 调度、不批量触发付费转写。
- 外部幂等或恢复语义不成立时，自动重试保持关闭并记录阻塞。
- 来源：`docs/IMPLEMENTATION_PLAN.md`「阶段推进与交接」章节。

## 待确认开放问题（阻塞型，每项带影响与推荐默认值）

| 问题 | 影响 | 推荐默认值 |
| --- | --- | --- |
| Scribe API key | 阻塞真实转写阶段全部任务；基础架构与流程测试不受影响 | 环境变量 `ELEVENLABS_API_KEY`，不入库、不写日志、不进 git |
| 模型凭据（翻译快模型、复核同档模型、调研快模型） | 阻塞翻译与复核阶段 | 环境变量；翻译与调研用快模型、复核同档强模型，具体供应商与型号待凭据确认 |
| 样本节目（一期真实 mp3 或 RSS 链接） | 阻塞真实转写与翻译复核；本地合成/固定响应测试不受阻 | 一期 Chelsea 相关比赛日节目，英文、多说话人、时长适中；未提供前先用合成词级 JSON 与已保存产物做可重复测试 |
| 模型路由（具体供应商与型号） | 影响翻译、复核执行与成本基线 | 遵循 skill 约定：翻译可用快模型、复核与主代理同档、调研用快模型 |
| 项目命名（Python 包名、Node 项目名、SQLite 文件名） | 三者连锁，改名是全局动作 | 按 `podcast_agent` 开工，用户定名后一次性全局重命名；成本低，建议在首次真实数据与多期 commit 前定名 |
| 知识库位置（`TRANSLATE_SRT_HOME` 或 config） | 阻塞 match / glossary 与全流程编排（可用默认位置先建空库） | 独立 git 仓 + 环境变量 `TRANSLATE_SRT_HOME`；缺省 `~/.translate-srt/`；迁移 = 改环境变量，不复制产物、不改 git 历史；缺库先 `kb_tools.py init`；不入本项目仓库 |
| 样本节目说话人身份 | 影响词级重组前缀、术语表、翻译人称 | 词级重组默认输出 `[S01] ` 占位；真名在 skill 的 speakers 步骤用 `--map` 落实；匿名素材按 pitfalls 给处置选项（全部 `--drop` / 换角色名 / 只给非旁白加角色名），不臆造真名 |

## 下一步行动（流程测试阶段，三个任务）

### 单期 conversation 验证

- 做什么：验证每步 checkpoint、transcript 持久化、恢复入口、safe / unsafe 工具边界、effect sandwich、memo、parked
- 验证点：mock 工具 + 固定响应；正常完成后重启从 checkpoint 继续，不重复已确认完成的付费步骤；区分框架提供能力与 Runner 仍需补齐能力
- 产出位置：`runner/` 内实现；实验记录写入 `docs/verification/`；结论回填 `docs/ARCHITECTURE.md` 文末待验证事项

### kill -9 崩溃恢复实验

- 做什么：在转写 POST 在途、翻译中途、子代理完成回写前、checkpoint 切换四个崩溃点注入 kill -9
- 验证点：safe 工具重跑、unsafe 工具不重跑、结果未知进入可对账状态而非再次扣费
- 产出位置：崩溃注入脚本；实验记录写入 `docs/verification/`；结论回填 `docs/ARCHITECTURE.md` 待验证事项

### 下载工具接入为真实 safe 工具

- 做什么：把 `pipeline/download.py` 封装为经 bash 调用的工具，落盘单期工作区与首版固化记录
- 验证点：本地起 http 源验证 302 重定向与 sha256；重跑安全（同 hash 复用）；动态广告 hash 漂移告警并保留首版
- 产出位置：`runner/` 工具封装、单期工作区产物

## 验收口径摘要（最小 MVP）

- 同一期音频产出英文 SRT、修正稿、中文译文、复核稿、重切分候选、术语表和检查记录；产物真实存在且关联正确
- 英文句子边界可回溯词级时间戳；译文与原文对齐，无未解释漏译、时间轴差异或越界插值
- 正常完成后重启从 checkpoint 继续，不重复已确认完成的付费步骤；调用已发出但 outcome 未落盘的窗口中断时进入可对账状态而非再次扣费
- 审核前没有任何上传或交付动作；人工修改保护要求已纳入后续数据契约
- 架构待验证事项有可复现实验结果；仍存在的外部阻塞写明影响，不以 mock 成功替代真实能力验收

## 当前被阻塞项清单

- 真实转写（阻塞源：Scribe API key + 样本节目）
- 翻译与复核（阻塞源：模型凭据 + 知识库位置 + 样本节目）
- 费用与耗时基线及最小 MVP 交接（阻塞源：依赖真实调用）
- 以上阻塞项在开放问题未确认前应跳过并在交接记录注明，不静默降级

## 接手自检命令

| 目的 | 命令 |
| --- | --- |
| Python 全部测试 | `uv run pytest tests/`（当前 23 个全过） |
| Node 类型检查 | `cd runner && npm run typecheck`（当前通过） |
| Node 全部测试 | `cd runner && npm test`（当前 3 个全过） |
| skill 未误改回归 | `python skills/translate-srt/scripts/srt_tools.py --self-test`、`python skills/translate-srt/scripts/kb_tools.py --self-test` |
