# 交接文档 — 播客翻译 Agent

- 日期：2026-11
- 定位：给下一个新会话的接手说明，覆盖「读什么、现状到哪、已定什么、待确认什么、本会话已完成什么、需要用户配合什么、怎么自检」
- 生成方式：父代理与 worker 子代理迭代（草稿 → 审阅 → 修订），父代理定稿

## 与既有交接文档的关系

- 本文档是实施现状交接，新会话以本文档为准。

## 阅读顺序

- 先读 `AGENTS.md`：项目沟通规则（禁用编号风格、文档管理规则、测试规范、Git 规范）
- 再读 `docs/REQUIREMENTS.md`：需求与数据模型（权威文档，已定事项不重议）
- 然后读 `docs/ARCHITECTURE.md`：四层模块职责划分与文末「待验证事项」
- 再读 `docs/IMPLEMENTATION_PLAN.md`：阶段划分、任务依赖、产出与验收口径
- 再读 `docs/TECH_STACK.md`：技术选型、schema 草案、目录拓扑、选型决策记录、待验证项
- 再读 `docs/NEXT_STEPS.md`：最小 MVP 的落地行动清单（当前推进所依据的清单）
- 再读 `.agent/pi-durable-vs-langgraph.md`：执行层骨架选型讨论与风险
- 再读 `.agent/knowledge-base.md`：知识库讨论结论（含未定字段级 gap）
- 再读 `docs/verification/flow-test.md`：Pi Durable 语义验证与 kill -9 崩溃恢复实验结论
- 再读 `docs/verification/translate-srt-reuse.md`：skill 复用边界自检结论
- 最后读代码：`state/` → `pipeline/` → `runner/src/` → `tests/`（顺序即数据契约→流水线工具→执行骨架→测试）

## 项目现状

### 阶段进度

- 外层五大阶段：最小 MVP → 自动化主链 → 审核与交付 → 长程与沉淀 → 打磨
- 最小 MVP 内部五阶段：基础架构 ✅ / 流程测试 ✅ / 真实转写 / 翻译与复核 / 收口与回填
- 当前进度：基础架构、流程测试、ToolSpec 隔离层已完成并提交；**真实转写阶段已完成**——样本期《Boss with Jamie Carragher: Xabi Alonso》全链路跑通：下载固化（Episode id=1，36MB/2249s）→ 真实 Scribe 转写（一次 POST $0.13744，与 usage 实扣一致）→ 词级重组（709 条 en.srt + provenance）→ 体检（merge 不需要，split 仅 #264）。产物在 `var/episodes/1/`。下一步进入「翻译与复核」：子代理接入方式实验 + skill 全流程编排（DeepSeek 凭据已就绪），本仓无未决外部阻塞。

### 已完成内容（逐模块）

| 模块 | 文件 | 职责 | 测试数 | 运行命令 |
| --- | --- | --- | --- | --- |
| 状态存储 | `state/schema.sql`、`state/db.py`、`state/episode.py`、`state/manifest.py`、`state/ledger.py` | SQLite 三表（episode / artifact / ledger）+ WAL/外键/幂等建表；Episode 创建与状态推进；产物清单；intent→outcome 两段式账本（call_ref 唯一，unknown 态供对账不自动重跑） | 9 | `uv run pytest tests/test_state.py` |
| 下载固化 | `pipeline/download.py` | 跟随 302 下载、sha256 固化、时长读取、首版保护（hash 漂移告警）；含 CLI 端到端 | 5 | `uv run pytest tests/test_download.py` |
| 词级重组 | `pipeline/wordrender.py` | Scribe 词级 JSON → 一句一条英文 SRT（`[S01] ` 前缀）+ provenance sidecar；时间戳缺失明确失败 | 10 | `uv run pytest tests/test_wordrender.py` |
| Runner 薄封装 | `runner/src/runner.ts` | 隔离 Pi Durable 实验 API，暴露 submit / resume / continue / checkpoint | 1 | `cd runner && npm test` |
| ToolSpec 工具契约 | `runner/src/tool-spec.ts` | 工具中立契约（`ToolSpec`）：replay safe/unsafe、execute、extras.memo，业务工具只依赖它 | — | — |
| pi-durable 适配层 | `runner/src/durable-adapter.ts` | ToolSpec → ToolRegistration 转换；换框架时只重写本文件与 runner.ts | — | — |
| 单期 conversation | `runner/src/conversation.ts` | download 真实（safe）、transcribe 真实（unsafe，经 `python -m pipeline.scribe` 调 CLI）的流水线工具集 | 4 | `cd runner && npm test` |
| 流程测试 | `runner/src/semantics.test.ts`、`runner/src/kill9.test.ts`、`runner/src/crash-child.ts` | 进程内语义验证 + kill -9 四崩溃点恢复实验 | 6 | `cd runner && npm test` |
| skill 复用自检 | `docs/verification/translate-srt-reuse.md` | 两脚本 `--self-test` 通过，复用方式定为「经 bash 调子命令，不 import 为库」 | — | 见常用命令 |
| Python 环境 | `pyproject.toml`、`uv.lock` | uv + Python 3.12 + pytest + httpx + mutagen | — | `uv run pytest tests/` |

- Python 测试合计：60 个全部通过（state 10 + download 5 + wordrender 10 + scribe 35）
- Node 测试合计：11 个全部通过（runner 1 + conversation 4 + semantics 2 + kill9 4）
- 待建文件（已列出但未实现）：`pipeline/orchestrate.py`（translate-srt 全流程编排）

### ToolSpec 工具契约隔离层（已提交 d1438bd）

- 业务工具（`conversation.ts` 的 download/transcribe、`test-tools.ts` 的测试工具）只依赖 `tool-spec.ts` 的中立 `ToolSpec` 契约，零 pi-durable 框架依赖。
- pi-durable 的 import 收拢到两个文件：`runner.ts` 与 `durable-adapter.ts`。
- 契约语义（换框架时新实现必须满足）：`replay: "safe"` 崩溃后自动重跑；省略 = unsafe，绝不自动重跑，模型收到 interrupted；`execute` 抛错由框架把错误结果交给模型；`extras.memo` 同名第一次写赢。
- 验收用例见 `docs/verification/flow-test.md`。

### git 提交状态（已实际读取）

- 历史提交至 `d1438bd`（ToolSpec 隔离层），共 7 个 commit；本会话全部改动已按用户要求撤销提交，保留在工作区待用户审阅，未提交。
- 待审阅改动：`AGENTS.md`（Git 规范 + 新增「代理不得自行 commit」规则）、`docs/TECH_STACK.md`（待验证项回填 + 转写端点路径修正）、`docs/HANDOFF.md`（本会话任务收官）、新增 `docs/verification/scribe-idempotency-doc-review.md` 与 `docs/verification/scribe-idempotency-controlled-test.md`。
- 知识库仓（`~/.translate-srt/knowledge`）：init 脚本自带一次提交；我的「移除示例域 seiyuu」提交已撤销，删除暂存中待审阅。

## 本会话完成的任务（两个，均已收官）

### Scribe 幂等键服务端语义查证（已完成：文档 + 受控实测）

背景：`docs/REQUIREMENTS.md` 要求转写调用携带幂等键（episode guid + 音频 hash），重试不产生二次计费。但 `docs/IMPLEMENTATION_PLAN.md` 明确：服务端是否支持、有效期和异常语义必须先查证，不能把本地去重当作服务端不重复计费的证明。

完成状态：两半均已做完。结论——官方文档与受控实测均证实 Scribe 无幂等键机制，「同键重试不重复计费」不成立；凭 `transcription_id` 的 GET 取回路径可用；付费调用自动重试保持关闭。证据与影响见 `docs/verification/scribe-idempotency-doc-review.md`（文档查证）与 `docs/verification/scribe-idempotency-controlled-test.md`（受控实测）。遗留：usage 面板读数待用户核对后回填实测记录。

分两半推进（已归档为验收依据）：

- 文档查证（无需 key，可立即做，结论需用户审阅）
  - 查官方文档，确认 Scribe 转写接口对幂等键的支持情况与语义边界。
  - 查证项：请求键是否支持、参数名（header 或 body 字段）、作用域（账号级 / 端点级 / 请求级）、有效期（多久内重发同键视为同一次）、超时后的查询或对账手段（GET transcript by id 能否查到、能否以同键续查）、计费语义（重试同键是否只计一次、键过期后重发是否另计）。
  - 产出写入 `docs/verification/`，并回填 `docs/TECH_STACK.md` 待验证项。
  - 文档查证结论是「供应商声称的语义」，仍需用户审阅确认是否接受作为依据，不能替代受控验证。

- 受控验证（需用户配合：Scribe key + 一段短音频）
  - 用一段短音频做受控调用，验证文档声称的幂等语义在实际行为上成立。
  - 价格口径：Scribe v2 按音频时长计费（$0.22/小时，2026-11 查证口径），30 秒级短音频成本约 $0.002，可忽略；若需要精确成本，受控验证前按官方定价复核一次。
  - 重点验证「结果未知时的安全恢复路径」：发出调用后中断，凭同键重发或查询是否不重复计费、能否拿到已有结果。
  - 短音频不要求是正式样本节目，只要能触发真实转写即可；正式样本节目另属「真实转写」阶段，不在本次范围。

验收口径：

- 不能把本地去重当作服务端不重复计费证明；必须落到「服务端对同键重试/查询的实际行为」。
- 能证明结果未知时安全恢复，则付费调用自动重试语义成立；否则关闭付费调用自动重试并记录阻塞，不以 mock 成功替代真实能力验收。

### 最小英超知识库 init（已完成）

- 已用 `kb_tools.py init` 建库：默认位置 `~/.translate-srt/`，`status` 通过，独立 git 仓（未落入本项目仓库）；已移除 skill 示例域 seiyuu（提交于知识库仓）。
- 领域包命名与粒度仍按 `.agent/knowledge-base.md` 未决项处理，本会话只建空骨架。
- 迁移方式：改 `TRANSLATE_SRT_HOME` 环境变量即可，不复制产物、不改 git 历史。
- 位置约定与复用边界见 `docs/verification/translate-srt-reuse.md`。

### 需要用户配合 / 决策的点（本次会话范围内，详情见下方「待确认开放问题」）

| 决策点 | 简短说明 |
| --- | --- |
| Scribe key 提供时机 | 已解决：已写入 `.env`（`ELEVENLABS_API_KEY`，已 gitignore） |
| 受控验证是否做 | 已做：短音频由本地 `say` 合成（38.4 秒 WAV），实测完成 |
| 知识库位置是否用默认 | 已定：默认 `~/.translate-srt/`，init 完成 |
| usage 面板读数核对 | 受控验证计费口径（约 $0.009）待用户在 ElevenLabs 后台核对后回填实测记录 |

## 关键决策记录（含来源文档）

| 决策 | 结论 | 来源文档 |
| --- | --- | --- |
| 转写客户端 | 直接 HTTP（httpx），不用 ElevenLabs 官方 SDK；便于携带幂等键头做服务端语义验证、原始响应落盘、复用已定 httpx | `docs/TECH_STACK.md` 选型决策记录 |
| ToolSpec 工具契约隔离层 | 业务工具零框架依赖，pi-durable import 收拢到 runner.ts 与 durable-adapter.ts；换框架只重写这两个文件 | `runner/src/tool-spec.ts`、`runner/src/durable-adapter.ts`、commit d1438bd |
| 分层-目录映射 | discover=发现层、runner+pipeline=执行层、state=跨层数据契约（schema 归执行层维护）、review+eval=审核层、delivery=交付层；层间只经数据契约与接口边界 | `docs/TECH_STACK.md`、`docs/NEXT_STEPS.md` |
| Runner 薄封装职责 | 暴露 submit / resume / continue / checkpoint，隔离 Pi Durable 实验 API；业务工具不直接依赖实验接口 | `docs/ARCHITECTURE.md`、`runner/src/runner.ts` |
| safe / unsafe 工具语义 | safe 崩溃后自动重跑、unsafe 绝不自动重跑且模型收到 interrupted；已由流程测试验证 | `docs/verification/flow-test.md` |
| 测试文件头场景注释规范 | 每个测试文件开头用几行注释列出模拟场景清单 | `AGENTS.md` 测试规范 |
| Git 提交前读取规范 | 每次准备提交前先 `git log --oneline` 与 `git status` 实际读取，再决定提交内容与 message | `AGENTS.md` Git 规范 |
| 文档管理规则 | 需求文档为权威；不轻易新增文档，优先更新既有文档；多轮文档先确认再创建子代理，迭代最多 3 轮 | `AGENTS.md` 项目文档 |
| 模块级提交规则 | 一个模块代码与测试全过后 commit 一次，message 注明模块与产出 | `docs/NEXT_STEPS.md` 执行约定 |

## 执行纪律（红线）

- 最小 MVP 完成前不扩展 feed 调度、不批量触发付费转写。
- 外部幂等或恢复语义不成立时，自动重试保持关闭并记录阻塞。
- 提交前先 `git log --oneline` 与 `git status` 实际读取，不凭记忆推断 git 状态。
- 来源：`docs/IMPLEMENTATION_PLAN.md`「阶段推进与交接」、`AGENTS.md` Git 规范。

## 待确认开放问题（阻塞型，长期清单）

| 问题 | 影响 | 推荐默认值 |
| --- | --- | --- |
| Scribe API key | 已解决：`.env` 中 `ELEVENLABS_API_KEY`（不入库、不写日志、不进 git） | 环境变量 `ELEVENLABS_API_KEY`，不入库、不写日志、不进 git |
| 受控验证短音频 | 已解决：本会话用本地 `say` 合成 38.4 秒语音完成实测；正式样本节目另属真实转写阶段 | 一段短音频即可（30 秒级，成本约 $0.002 可忽略）；正式样本节目另属真实转写阶段 |
| Scribe 计费实测读数 | 已结案（2026-11-06 充值后重测）：PAYG 期 $0.2200/h 零残差吻合官方定价，此前 $0.40/h 系免费计划 credit 折算口径，费用基线维持需求 $0.18/49 分钟，无需重估 | 结案节见 `docs/verification/scribe-idempotency-controlled-test.md` |
| 需求修订（幂等键不成立） | 查证证实服务端无幂等键，`docs/REQUIREMENTS.md`「重试不产生二次计费」需修订为「本地防重复触发 + 凭 transcription_id 恢复」，待用户确认后改 | 修订提案见 `docs/verification/scribe-idempotency-controlled-test.md`「对项目的影响」 |
| 模型凭据（翻译快模型、复核同档模型、调研快模型） | 已解决（2026-11-06）：`.env` 中 `DEEPSEEK_API_KEY` 已填并实测可用 | DeepSeek 先行；翻译 `deepseek-flash`、复核 `deepseek-v4-pro`、调研 `deepseek-flash` |
| 样本节目（一期真实 mp3 或 RSS 链接） | 已选定（2026-11-06）：《Boss with Jamie Carragher: Xabi Alonso》，Sky Sports PL Podcast，2026-10-26，37:29，36MB，卡拉格专访切尔西主帅阿隆索 | enclosure `https://episodes.captivate.fm/episode/8db0bde6-c9cb-43be-92ab-b18987fbdc58.mp3`（feed `https://feeds.captivate.fm/sky-sports-premierleague/`，已实测 200）；转写成本预估 ≈$0.14（$0.22/h）；双人访谈形态，与比赛日多人圆桌的差异不影响下游测试 |
| 模型路由（具体供应商与型号） | 已定（2026-11-06）：`.env` 路由变量 `PODCAST_MODEL_TRANSLATE/REVIEW/RESEARCH` = `deepseek/deepseek-flash`、`deepseek/deepseek-v4-pro`、`deepseek/deepseek-flash`；换 provider 只改路由行 + 对应 key | 均为推理模型，`effort` 支持 low/high/max（默认 high），批量任务建议 low，接线时定 |
| 项目命名 | 三者连锁，改名是全局动作 | 按 `podcast_agent` 开工，用户定名后一次性全局重命名；建议在首次真实数据与多期 commit 前定名 |
| 知识库位置（`TRANSLATE_SRT_HOME`） | 阻塞 match / glossary 与全流程编排（本次只 init，可用默认位置先做） | 独立 git 仓 + 环境变量；缺省 `~/.translate-srt/`；迁移 = 改环境变量，不复制产物、不改 git 历史 |
| 样本节目说话人身份 | 影响词级重组前缀、术语表、翻译人称 | 词级重组默认输出 `[S01] ` 占位；真名用 `speakers --map` 落实；匿名素材给处置选项，不臆造真名 |

## 验收口径

### 本次会话（已完成，验收依据留档）

- Scribe 幂等键查证结论已落「服务端对同键重试/查询的实际行为」：请求键不支持（文档零命中 + 实测同键不同 ID）、参数名不适用、作用域/有效期不适用、超时后对账手段 = `transcription_id` + GET（实测可用）、计费语义 = 每请求独立计费无去重；文档查证经用户审阅，受控验证有实测证据。
- 结论已回填 `docs/TECH_STACK.md` 待验证项，证据位置在 `docs/verification/scribe-idempotency-doc-review.md` 与 `docs/verification/scribe-idempotency-controlled-test.md`。
- 知识库 init 完成且 `kb_tools.py status` 通过；产物为独立 git 知识库（`~/.translate-srt/`），未落入本项目仓库。

### 最小 MVP 总体（背景口径，非本次全部完成）

- 同一期音频产出英文 SRT、修正稿、中文译文、复核稿、重切分候选、术语表和检查记录；产物真实存在且关联正确。
- 英文句子边界可回溯词级时间戳；译文与原文对齐，无未解释漏译、时间轴差异或越界插值。
- 正常完成后重启从 checkpoint 继续，不重复已确认完成的付费步骤；调用已发出但 outcome 未落盘的窗口中断时进入可对账状态而非再次扣费。
- 审核前没有任何上传或交付动作；人工修改保护要求已纳入后续数据契约。
- 架构待验证事项有可复现实验结果；仍存在的外部阻塞写明影响，不以 mock 成功替代真实能力验收。

## 接手第一步

- 先 `git log --oneline` + `git status` 实际读取状态：本会话改动全部未提交（待用户审阅），审阅批准后才提交。
- 计费已核对（日账单）；如需最终归属确认：Request Log 逐请求导出 + 核对账号套餐费率（实测有效单价 ≈ $0.40/h，与文档 $0.22/h 不一致）。
- 待用户确认需求修订提案（幂等键不成立），改 `docs/REQUIREMENTS.md`。
- 然后推进「翻译与复核」：真实转写已完成，英文 SRT（709 条）就绪；做子代理接入方式实验（崩溃恢复、成本归属、产物落盘与账本衔接）+ `pipeline/orchestrate.py` skill 全流程编排，speakers 真名映射（S01→Carragher、S02→Alonso）在 skill 的 speakers 步骤落实。

## 接手常用命令

| 用途 | 命令 |
| --- | --- |
| Python 全部测试（自检） | `uv run pytest tests/`（当前 24 个全过） |
| Node 类型检查（自检） | `cd runner && npm run typecheck`（当前通过） |
| Node 全部测试（自检） | `cd runner && npm test`（当前 10 个全过） |
| skill 未误改回归（自检） | `uv run python skills/translate-srt/scripts/srt_tools.py --self-test`、`uv run python skills/translate-srt/scripts/kb_tools.py --self-test` |
| 知识库 init（任务命令） | `uv run python skills/translate-srt/scripts/kb_tools.py init`（建空库骨架，默认 `~/.translate-srt/` 或 `--home DIR`） |
| 知识库状态（任务命令） | `uv run python skills/translate-srt/scripts/kb_tools.py status`（确认路径解析、领域条目数与 git 状态） |
