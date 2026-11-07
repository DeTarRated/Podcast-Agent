# 下一步行动：最小 MVP kickoff 计划

- 日期：2026-11
- 定位：最小 MVP 阶段的落地行动清单
- 依据：`docs/IMPLEMENTATION_PLAN.md`「最小 MVP 」章节；执行层选型讨论见 `.agent/pi-durable-vs-langgraph.md`
- 生成方式：父代理与 worker 子代理两轮迭代（草稿 → 审阅 → 修订）

背景一句话：用一期真实节目打通「固化音频 → 词级转写 → 英文 SRT → 翻译与复核 → 本地待审核产物」，证明执行骨架（Pi Durable + 自研 Runner）与既有 translate-srt skill 能接通，为后续自动发现、审核与交付扫清执行层不确定性。

## 目标与范围

### 本阶段做什么

- 以人工提供的一期节目元数据为入口，建最小 Episode 持久化、单期工作区、产物清单与账本。
- 建 Runner 薄封装与单期 Pi Durable conversation，验证 checkpoint、transcript 持久化、恢复入口、safe / unsafe 边界。
- 实现最小下载工具：跟随重定向、记录大小、时长与 sha256，后续步骤只读首次固化版本。
- 接通 ElevenLabs Scribe v2 的说话人分离与词级时间戳，保留原始响应；把词级 JSON 重组为一句一条、带说话人前缀的英文 SRT。
- 以最小英超知识库接通 translate-srt 全流程：输入体检、工作区初始化、命中与缺口、简报与术语表、转录修正、翻译、复核、重切分、provenance。
- 翻译与调研用快模型，复核与主代理同档；验证翻译、复核子代理的文件输入输出与恢复边界。
- 产出复核版与重切分版候选及原始／插值时间点统计，仅进入本地待审核状态。

### 本阶段不做什么

- 不做 feed 发现、轮询、ETag/304、guid 去重入队、Apple Lookup、便宜标题分类（均属自动化主链）。
- 不做审核页、Node 审批服务、override 表、评测样本写入（均属审核与交付）。
- 不做看点页、R2 上传、S3 预签名 URL、交付标记（均属交付层）。
- 不做 VPS 部署、cron、按需唤醒的进程开关演练（均属打磨）。
- 不做知识库沉淀演练（沉淀角色留到交付后），不演练多期并行与长程故障注入。

## 仓库骨架与归属（快速对照）

| 目录 | 本阶段是否动 | 用途 |
| --- | --- | --- |
| `state/` | 动 | Episode 持久化、产物清单、账本（SQLite schema + 读写封装） |
| `pipeline/` | 动 | 下载固化、词级重组、Scribe 转写、翻译工作区编排 |
| `runner/` | 动 | Pi Durable 集成、Runner 接口、工具封装、checkpoint 与恢复入口 |
| `tests/` | 动 | 各模块测试 |
| `discover/` | 不动 | 留待自动化主链 |
| `review/` | 不动 | 留待审核与交付 |
| `delivery/` | 不动 | 留待审核与交付 |
| `eval/` | 不动 | 留待审核与交付及长程阶段 |
| `skills/translate-srt/` | 只读复用 | 脚本与角色定义复用，不修改 |

## 执行序列（按依赖排序）

### 基础架构（零外部凭据，立即开始）

| 任务 | 做什么 | 产出文件 | 依赖前置 | 阻塞状态 |
| --- | --- | --- | --- | --- |
| 仓库初始化（git） | `git init` + 建 `.gitignore` + 首次 commit。忽略凭据文件（`.env`、`*.pem`、`*.key`、`secrets.*`）、单期工作区与音频/SRT 产物目录（本阶段约定为仓库根 `var/`）、`node_modules/`、`__pycache__/`、`.pytest_cache/`、`.venv/`、`.DS_Store`；依赖锁文件 `uv.lock`、`package-lock.json` 必须入库 | `.gitignore`、首次 commit | 无 | 立即 |
| Python 环境与依赖定稿 | 定死 uv + `pyproject.toml` + `uv.lock` + Python 3.12，测试框架 pytest，HTTP 客户端 httpx，时长读取库 mutagen；依赖清单与锁文件进仓库，凭据不入库 | `pyproject.toml`、`uv.lock`；选型结论回填 `docs/TECH_STACK.md` 选型决策记录 | 仓库初始化 | 立即 |
| 转写客户端选型 | 在 ElevenLabs 官方 SDK 与直接 HTTP 之间定稿，依据词级时间戳字段可获取性与维护成本选择；这是 `docs/TECH_STACK.md` 候选表中决策时机为最小 MVP 的待选项 | 选型结论回填 `docs/TECH_STACK.md` 选型决策记录；`pipeline/scribe.py` 按选型实现（在真实转写阶段） | Python 环境 | 立即（查文档即可，不需凭据） |
| 最小 Episode 持久化 | 建 SQLite schema：Episode（guid、feed、feed 快照、标题、发布时间、时长、enclosure、音频 hash、状态、产物清单引用）、产物清单、账本最小表；提供读写封装 | `state/schema.sql`、`state/episode.py`、`state/manifest.py`、`state/ledger.py`、`tests/test_state.py` | Python 环境 | 立即 |
| 最小下载工具 | 跟随重定向下载 mp3，记录 sha256、大小、时长，落首次固化版本；后续步骤只读首版 | `pipeline/download.py`、`tests/test_download.py` | Python 环境 | 立即（本地 mock 音频即可验证） |
| 词级重组新组件 | 把词级时间戳 JSON 重组为一句一条英文 SRT；说话人前缀默认输出 `[S01] ` 占位形态（skill 对 `名字: ` 与 `[S01] ` 两种形态都认）；保留词级时间戳供 provenance 溯源 | `pipeline/wordrender.py`、`tests/test_wordrender.py` | Python 环境 | 立即（合成词级 JSON 可测） |
| translate-srt 复用边界自检 | 跑 `srt_tools.py --self-test` 与 `kb_tools.py --self-test`；确认从仓库内调用脚本的路径约定与输入输出边界，不修改 skill | 复用边界说明写入 `docs/verification/` 验证记录 | 无 | 立即 |
| Pi Durable 安装与 Runner 薄封装 | 先做 Node LTS 环境检查；npm 安装并 pin 小版本 1.5.0；建 Runner 接口（submit / resume / checkpoint）隔离实验 API；建最小单期 conversation 骨架 | `runner/package.json` + `package-lock.json`、`runner/src/runner.ts`、`runner/src/conversation.ts`、mock 对话测试 | 仓库初始化 | 立即 |

### 流程测试（Pi Durable 能力验证）

| 任务 | 做什么 | 产出文件 | 依赖前置 | 阻塞状态 |
| --- | --- | --- | --- | --- | --- |
| 单期 conversation 验证 | 验证每步 checkpoint、transcript 持久化、恢复入口、safe / unsafe 工具边界、effect sandwich、memo、parked | `runner/` 内实现；实验记录写入 `docs/verification/` | 基础架构 | 立即（用 mock 工具 + 固定响应） |
| kill -9 崩溃恢复实验 | 构造崩溃点（转写 POST 在途、翻译中途、子代理完成回写前、checkpoint 切换）注入崩溃，观察 safe 重跑、unsafe 不重跑、结果未知进可对账状态 | 崩溃注入脚本；实验记录写入 `docs/verification/`；结论回填 `docs/ARCHITECTURE.md` 待验证事项 | 单期 conversation | 立即（mock 付费工具即可） |
| 下载工具接入为 safe 工具 | 把 `pipeline/download.py` 封装为经 bash 调用的工具，落盘单期工作区与首版固化记录 | `runner/` 工具封装、单期工作区产物 | 下载工具 + Runner | 立即（本地起一个 http 源即可验证重定向与 hash） |

### 真实转写（Scribe）

| 任务 | 做什么 | 产出文件 | 依赖前置 | 阻塞状态 |
| --- | --- | --- | --- | --- |
| Scribe 客户端接通 | 接通 Scribe v2 说话人分离与词级时间戳；原始响应落盘到每期工作区且不覆盖 | `pipeline/scribe.py`、每期工作区原始响应文件 | 转写客户端选型、Scribe API key、样本节目 | ✅ 已完成：样本期真实转写成功（transcription_id `rBnXktBF69KA1s8RQkeu`，raw + ID sidecar 落盘，Episode → transcribed，账本 success $0.13744 与 usage 实扣分毫不差） |
| Scribe 幂等键服务端语义查证 | 查官方文档并做受控验证：请求键支持、作用域、有效期、超时后查询或对账手段、计费语义 | 查证与受控验证记录；结论回填 `docs/TECH_STACK.md` 待验证项 | Scribe API key | ✅ 已完成：服务端无幂等键（文档 + 实测），计费口径结案（PAYG $0.22/h），记录见 `docs/verification/scribe-idempotency-*.md` |
| 词级重组接真实响应 | 用真实 Scribe 词级 JSON 跑通重组，输出 `[S01] ` 占位前缀；处理缺失词时间戳、未知说话人、异常时间区间，无法生成可信输入时明确失败或等待，不伪造时间点 | `pipeline/wordrender.py` 异常路径 + 对应测试 | 真实转写 | ✅ 已完成：真实响应 14061 词零缺失时间戳 → 709 条 `en.srt` + provenance（S01/S02，audio_event 33 个隔离出文本），产物已登记 manifest |
| 英文 SRT 输入体检 | 对重组产物跑 `srt_tools.py stats` 体检，判定 merge / split 是否需要；记录说话人前缀形态 | 英文 SRT（一句一条 + `[S01] ` 占位前缀）、体检记录、产物清单与账本写入 | 词级重组接真实响应 | ✅ 已完成：709 条体检——merge 不需要；split 仅 #264 需转录修正补句读；87 条长单句留 resplit；speakers 真名映射（S01→Carragher、S02→Alonso）留翻译阶段 |

### 翻译与复核接通（translate-srt）

| 任务 | 做什么 | 产出文件 | 依赖前置 | 阻塞状态 |
| --- | --- | --- | --- | --- |
| 最小英超知识库 init | 用 `kb_tools.py init` 建空库或最小英超底座；`kb_tools.py status` 通过 | 独立 git 知识库（位置由 `TRANSLATE_SRT_HOME` 或 config 决定） | 知识库位置 | 可用默认位置先做 |
| skill 全流程编排 | 编排输入体检、工作区初始化、命中与缺口、简报与术语表、转录修正、翻译、复核、重切分、provenance；merge / split 只在 stats 判定需要时运行 | `pipeline/orchestrate.py`、单期工作区全套产物 | 英文 SRT、知识库 | 前置凭据与样本均已就绪，依赖上游产物 |
| 子代理接入方式实验 | 实验扩展任务、嵌套任务或独立 conversation 中适合本项目的方式；记录模型路由、产物位置、父子完成关联、崩溃恢复、结果去重、成本归属 | 实验记录；结论回填 `docs/ARCHITECTURE.md` 待验证事项 | 流程测试 | 模型凭据已就绪（DeepSeek），可开工 |
| 翻译与复核执行 | 快模型翻译、同档模型复核；保留复核说明、别名替换日志、检查结果；复核修改异常少时主代理补查 | 翻译稿、复核稿、`review_notes.md`、`alias_log.tsv`、check 记录 | 子代理接入方式 | 凭据已就绪，依赖上游产物 |
| 产出候选与 provenance | 产出复核版与重切分版候选，跑 provenance 统计原始／插值时间点；状态推进到 reviewing，不上传、不标记交付 | 复核版、重切分版候选、provenance 统计、本地待审核状态 | 翻译与复核执行 | 凭据已就绪，依赖上游产物 |

### 收口与回填

| 任务 | 做什么 | 产出文件 | 依赖前置 | 阻塞状态 |
| --- | --- | --- | --- | --- |
| 真实费用与耗时基线 | 账本记录转写、翻译、复核、调研各次调用的金额估算、触发时间、所属期数；用真实用量对比需求典型成本 | `state/ledger.py` 记录、单期成本与耗时基线报告 | 真实转写 + 翻译复核 | 被真实调用阻塞 |
| 最小 MVP 交接 | 回填 `docs/ARCHITECTURE.md` 待验证事项结论；产出交接记录（实现变更摘要、通过／未通过验收项、复现入口、样本与产物位置、费用、恢复说明、剩余阻塞） | `docs/ARCHITECTURE.md` 更新、最小 MVP 交接文档 | 全部前序 | 被前序阻塞 |

## 待确认开放问题

### 需要用户作答（阻塞型）

| 问题 | 影响 | 推荐默认值 |
| --- | --- | --- |
| Scribe API key | 阻塞真实转写阶段的全部任务；基础架构与流程测试不受影响 | 环境变量 `ELEVENLABS_API_KEY`；不入库、不写日志、不进 git |
| 模型凭据（翻译快模型、复核同档模型、调研快模型） | ~~阻塞翻译与复核阶段~~ 已解决（2026-11-06）：`.env` 中 `DEEPSEEK_API_KEY` 已填并实测可用 | DeepSeek 先行；翻译 `deepseek-flash`、复核 `deepseek-v4-pro`、调研 `deepseek-flash`；换 provider 只改 `.env` 路由行 + 对应 key |
| 样本节目（一期真实 mp3 或 RSS 链接） | 已选定（2026-11-06）：《Boss with Jamie Carragher: Xabi Alonso》，Sky Sports PL Podcast，2026-10-26，37:29，36MB，卡拉格专访切尔西主帅阿隆索 | enclosure `https://episodes.captivate.fm/episode/8db0bde6-c9cb-43be-92ab-b18987fbdc58.mp3`（feed `https://feeds.captivate.fm/sky-sports-premierleague/`，已实测 200）；转写成本预估 ≈$0.14（37.5min × $0.22/h）；双人访谈形态，与比赛日多人圆桌的形态差异仅影响发现层分类样本，不影响下游测试 |
| 模型路由（具体供应商与型号） | 影响翻译、复核执行与成本基线 | 已定（2026-11-06）：`.env` 路由变量 `PODCAST_MODEL_TRANSLATE/REVIEW/RESEARCH` = `deepseek/deepseek-flash`、`deepseek/deepseek-v4-pro`、`deepseek/deepseek-flash`；均为推理模型，`effort` 支持 low/high/max（默认 high），批量任务建议 low，接线时定 |
| 项目命名（Python 包名、Node 项目名、SQLite 文件名） | 三者连锁：import 路径、DB 文件名、`package.json` name、目录名相互引用，改名是全局动作 | 默认按 `podcast_agent` 开工；用户定名后一次性全局重命名，成本边界见下 |
| 知识库位置（`TRANSLATE_SRT_HOME` 或 config） | 阻塞 match / glossary 与全流程编排（但可用默认位置先建空库） | 独立 git 仓 + 环境变量 `TRANSLATE_SRT_HOME` 指向；迁移方式 = 改环境变量，不复制产物、不改 git 历史；缺库先 `kb_tools.py init`；不入本项目仓库 |
| 样本节目说话人身份 | 影响词级重组前缀处理、术语表、翻译人称 | 词级重组默认输出 `[S01] ` 占位形态；真名在 skill 的 speakers 步骤用 `speakers --map` 落实；匿名素材按 pitfalls 给处置选项（全部 `--drop` / 换成角色名 / 只给非旁白加角色名），不臆造真名 |

命名重命名的成本边界：按 `podcast_agent` 开工后若用户另定名，重命名是一次性全局动作——改 Python 顶层包名与 import 路径、SQLite 文件名（仅改文件，schema 与数据不受影响）、`package.json` name、相关目录与文档引用；不改知识库（独立 git 仓）、不改 skill 文件、不改 SQLite schema。成本低，但建议在首次真实数据与多期 commit 之前定名，减少后续 diff 噪声。

### 已定小决策（开工即按此执行，不需等用户）

| 问题 | 已定写法 |
| --- | --- |
| Python 版本与依赖管理 | uv + `pyproject.toml` + `uv.lock` + Python 3.12 |
| 测试框架 | pytest |
| HTTP 客户端（下载工具） | httpx |
| 时长读取库 | mutagen |
| 转写客户端 | ElevenLabs 官方 SDK 与直接 HTTP 二选一，在基础架构「转写客户端选型」任务定稿 |
| Pi Durable 版本 | pin 小版本 1.5.0，Runner 接口隔离实验 API |
| Node 运行时 | Node LTS |
| 首期调研策略 | 跳过联网补缺调研（gaps 少时自拟译名），沉淀留到交付后，本阶段不演练沉淀 |

## 执行约定

- 以模块为单位提交：一个模块（如 state、pipeline/download、runner 骨架）的代码与测试全部通过后 commit 一次，commit message 注明模块与产出；不必每个任务一次。仓库初始化后先 commit 骨架，后续逐模块提交。
- 完成记录去向：待验证项结论回填 `docs/ARCHITECTURE.md` 文末待验证事项；选型结论回填 `docs/TECH_STACK.md` 选型决策记录；实验与验证过程记录统一写入 `docs/verification/`。
- 测试运行方式：Python 用 `uv run pytest tests/`；skill 脚本自检用 `python srt_tools.py --self-test` 与 `python kb_tools.py --self-test`；Node 用 `runner/package.json` 内约定的 test script。测试通过后再 commit。
- 测试文件开头用几行注释列出本文件模拟的场景清单，便于人工核对覆盖范围。
- 产物与凭据边界：凭据只走环境变量，不入库、不写日志；音频、单期工作区、原始 ASR 响应等产物进 `.gitignore` 的 `var/`，不进 git；依赖锁文件 `uv.lock` 与 `package-lock.json` 必须入库。
- 推进方式：实施会话按执行序列顺序推进，不跳步；被开放问题阻塞的任务先跳过并在交接记录注明阻塞原因，不静默降级。

## 前置风险与验证项

| 验证项 | 风险 | 验证方法 | 通过判据 |
| --- | --- | --- | --- |
| Pi Durable 安装与 kill -9 实验 | 包龄短、API 可无通知变动、单进程独占存储 | pin 小版本；在转写 POST 在途、翻译中途、子代理完成回写前、checkpoint 切换等崩溃点注入 kill -9 | 不重复已确认完成的付费步骤；unsafe 调用在途时进入可对账状态而非再次扣费 |
| Scribe 幂等键服务端语义 | 本地幂等键去重不等于服务端不重复计费 | 查官方文档 + 受控验证请求键支持、作用域、有效期、超时后查询／对账、计费语义 | 能证明结果未知时安全恢复；否则关闭付费调用自动重试并记录阻塞 |
| 子代理接入方式 | 翻译／复核子代理在 Pi Durable 中如何接入是最大不确定性 | 实验扩展任务、嵌套任务、独立 conversation；记录模型路由、产物位置、父子完成关联、崩溃恢复、结果去重、成本归属 | 有可复现结论并回填架构待验证事项 |
| 词级重组新组件 | translate-srt 脚本不覆盖词级 JSON → SRT；缺失时间戳、未知说话人、异常区间可能伪造时间点 | 合成词级 JSON 单测覆盖正常与异常，再接真实 Scribe 响应 | 英文句子边界可回溯词级时间戳；异常明确失败或等待，不伪造时间点 |
| fork / steer 实际用法 | 翻译策略调整与驳回范围变更场景下上下文、checkpoint、产物版本、付费记录可能混淆 | 用翻译策略调整和驳回范围变更场景验证是否需要 fork / steer | 记录采用场景与禁止场景；不适用也保留结论，不强行为主链引入分叉 |
| Durable 能力边界 | 框架提供能力与 Runner 仍需补齐的能力边界不清 | 逐项验证 checkpoint、safe / unsafe、effect sandwich、memo、parked 实际行为 | 明确哪些是框架保证、哪些由 Runner 补齐 |
| 费用与耗时基线 | 需求典型成本不等于供应商现价 | 用实际节目与实际用量记录转写、翻译、复核、调研开销和耗时 | 真实单期成本与耗时基线可对照需求目标线 |

## 验收口径摘要

- 同一期音频可产出英文 SRT、修正稿、中文译文、复核稿、重切分候选、术语表和检查记录；产物真实存在且关联正确。
- 英文句子边界可回溯词级时间戳；译文与原文对齐，无未解释漏译、时间轴差异或越界插值。
- 正常完成后重启从 checkpoint 继续，不重复已确认完成的付费步骤；调用已发出但 outcome 未落盘的窗口中断时，进入可对账状态而不是再次扣费调用。
- 审核前没有任何上传或交付动作；人工修改保护要求已纳入后续数据契约。
- 架构待验证事项有可复现实验结果；仍存在的外部阻塞写明影响，不以 mock 成功替代真实能力验收。

## 立即可以开始的步骤清单

- 仓库初始化：`git init` + `.gitignore` + 首次 commit。
- 定稿 Python 环境与依赖清单：uv + `pyproject.toml` + `uv.lock`，提交锁文件。
- 完成转写客户端选型，结论回填 `docs/TECH_STACK.md` 选型决策记录。
- 建 `state/schema.sql` 与 Episode、产物清单、账本的最小读写封装，补 `tests/test_state.py`。
- 实现 `pipeline/download.py`，用本地 http 源做重定向与 sha256 验证，补 `tests/test_download.py`。
- 实现 `pipeline/wordrender.py`，默认输出 `[S01] ` 占位前缀，用合成词级 JSON 覆盖正常、缺失时间戳、未知说话人、异常区间，补 `tests/test_wordrender.py`。
- 跑 translate-srt 两个脚本的 `--self-test`，复用边界说明写入 `docs/verification/`。
- 检查 Node LTS 环境，npm 安装并 pin Pi Durable 1.5.0，建 Runner 薄封装与最小 conversation 骨架。
- 用 mock 工具 + 固定响应做单期 conversation 验证与 kill -9 崩溃实验，实验记录写入 `docs/verification/`，结论回填 Durable 能力边界。
- 用默认位置 `kb_tools.py init` 建空知识库（用户确认位置后改 `TRANSLATE_SRT_HOME` 环境变量迁移，不复制产物、不改 git 历史）。

被开放问题阻塞、暂不能开始的步骤：真实转写阶段已完成（样本期全链路产物与账本落盘，`var/episodes/1/`）；翻译与复核无外部阻塞，进入子代理接入方式实验与 skill 全流程编排。知识库沉淀留待交付后演练；项目命名未定前按 `podcast_agent` 默认开工，用户定名后一次性全局重命名。
