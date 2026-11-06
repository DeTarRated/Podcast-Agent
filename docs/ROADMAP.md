# 播客翻译 Agent — 主线与支线全景图

- 日期：2026-11
- 定位：项目全景索引。只给「主线有哪些环节、支线有哪些机制、当前状态与阻塞」的一页总览；细节指向各权威文档（需求 `docs/REQUIREMENTS.md`、架构 `docs/ARCHITECTURE.md`、实施计划 `docs/IMPLEMENTATION_PLAN.md`、最小 MVP 清单 `docs/NEXT_STEPS.md`、最新现状 `docs/HANDOFF.md`）。
- 维护约定：状态变化时更新——主线表「所处阶段」列（阶段推进时）与「当前状态」列（环节完成/阻塞变化时）、支线表「当前状态」列、「当前阻塞清单」节（凭据/样本就绪或解除阻塞时增删行），并更新本头部日期。

## 状态标记约定

| 标记 | 含义 |
| --- | --- |
| ✅ | 完成（有测试或实验证据） |
| 🟡 | 部分就绪（部分组件或前置已完成，仍有剩余项） |
| ⛔ | 被外部阻塞（缺凭据/样本，无法推进） |
| ⬜ | 未开始（按阶段留待，尚未轮到） |

## 主线（七个环节）

主线 = 单期节目从头到尾的数据流：发现 → 下载 → 转写 → 词级重组 → 翻译与复核 → 人工审核 → 私有交付。

| 环节 | 职责 | 归属分层与目录 | 所处阶段 | 当前状态 | 验收归属文档 |
| --- | --- | --- | --- | --- | --- |
| 发现播客 | cron 轮询 + ETag/304 + guid 去重 + 分类过滤，新期入队 | 发现层 `discover/` | 自动化主链 | ⬜ | REQUIREMENTS·节目源管理/增量发现、IMPLEMENTATION_PLAN·自动化主链 |
| 下载 | 跟随 302 下载 mp3，sha256/大小/时长固化，首版保护，重试 | 执行层 `pipeline/download.py` | 最小 MVP·基础架构 | ✅ | NEXT_STEPS·最小下载工具 |
| 转写 | Scribe v2 说话人分离 + 词级时间戳，原始响应落盘 | 执行层 `pipeline/scribe.py`（待建）+ runner transcribe 占位 | 最小 MVP·真实转写 | 🟡 前置全部就绪（key、计费口径 $0.22/h、样本节目已选定）；scribe.py 实现正在另一会话讨论 | NEXT_STEPS·真实转写、flow-test |
| 词级重组 | 词级 JSON → 一句一条英文 SRT + 说话人前缀 + provenance sidecar | 执行层 `pipeline/wordrender.py` | 最小 MVP·基础架构 | ✅（合成词级 JSON 已验；接真实响应依赖真实转写产物） | NEXT_STEPS·词级重组新组件 |
| 翻译与复核 | translate-srt 全流程：体检、知识库、术语表、翻译、复核、重切分、provenance | 执行层 `pipeline/orchestrate.py`（待建）+ `skills/translate-srt/` | 最小 MVP·翻译与复核 | 🟡 凭据已就绪（DeepSeek flash 翻译/调研 + v4-pro 复核）；依赖上游真实转写产物，`pipeline/orchestrate.py` 未实现 | NEXT_STEPS·翻译与复核接通 |
| 人工审核 | 审核页同步对照、定点修改、问题标注、审批/驳回 | 审核层 `review/` + `eval/` | 审核与交付 | ⬜ | IMPLEMENTATION_PLAN·审核与交付、REQUIREMENTS·人工审核 |
| 私有交付 | 看点页模板、R2 上传、S3 签名 URL、交付清单 | 交付层 `delivery/` | 审核与交付 | ⬜ | IMPLEMENTATION_PLAN·审核与交付、REQUIREMENTS·看点页/交付 |

## 支线（九条）

支线 = 不直接产出主线产物、但保障正确性/成本/质量的横切机制。用户明确：支线（尤其「禁止二次消费」）可能比主线更重要。

| 支线 | 职责 | 为什么重要 | 与主线的关系 | 当前状态 | 证据位置 |
| --- | --- | --- | --- | --- | --- |
| 不重复计费 | 保证付费调用（转写/翻译/复核/调研/分类）不因重试、崩溃、重复入队而二次计费 | 头号横切项；服务端无幂等键已实测证实，自动重试即二次计费 | 横切下载→转写→翻译全程，尤其转写与翻译 | 🟡 已查证+实测（Scribe 无幂等键）；防重复计费设计已定稿；需求修订待用户确认 | `docs/verification/scribe-idempotency-doc-review.md`、`scribe-idempotency-controlled-test.md`、`.agent/scribe-idempotency-design.md`、`state/ledger.py` |
| 崩溃恢复与长程调度 | 每步 checkpoint、transcript 持久化、safe/unsafe 重放、parked 熔断、memo、按需唤醒、跨天恢复 | 长程系统正确性根基；重启不得丢产物、回退状态、重复付费 | 包裹执行层全部环节 | 🟡 框架能力已验；Runner 需补齐边界已明确；长程故障注入留待长程阶段 | `docs/verification/flow-test.md`（含 kill -9 四崩溃点实验）、`runner/src/*.test.ts` |
| 成本账本 | 记录每次付费调用金额估算、触发时间、期数归属；intent→outcome 两段式 + unknown 态对账 | 单期 <$0.5、空闲零付费可验证；是「禁止二次消费」的可观测载体 | 覆盖转写/翻译/复核/调研/分类所有付费调用 | ✅ 最小账本已建（`state/ledger.py`）；真实金额基线待重估 | `state/ledger.py`、`docs/verification/scribe-idempotency-controlled-test.md` |
| 领域知识库 | 英超领域包：译名、术语、ASR 错听变体、volatile 重查；纯文本 + 独立 git + 人工确认沉淀 | 跨期降低人工校对量与调研项数；需求文档花篇幅的部分 | 服务翻译与复核；沉淀在交付后 | 🟡 最小底座已 init（`~/.translate-srt/`，status 通过）；领域包命名粒度待定、沉淀未演练 | `docs/HANDOFF.md`、`docs/verification/translate-srt-reuse.md`、`.agent/knowledge-base.md` |
| override 机制 | 人工修改持久化保留，任何重跑优先于模型产物、不可覆盖 | 人工审核是唯一必经闸门，人工修改是最高优先级产物 | 服务人工审核，横切驳回重跑 | ⬜ 未开始（留待审核与交付）；契约已写入计划 | `docs/IMPLEMENTATION_PLAN.md`·审核与交付 |
| 评测 | 每次人工改动自动生成标注样本；指标跨期追踪质量与调研递减 | 零额外标注成本自动生长，验证「调研项数递减」目标 | 依赖人工审核的修改动作 | ⬜ 未开始（留待审核与交付 + 长程） | `docs/IMPLEMENTATION_PLAN.md`、`docs/REQUIREMENTS.md`·评测 |
| 框架隔离 | ToolSpec 中立契约 + Runner 薄封装 + durable-adapter；业务工具零 pi-durable 依赖 | Pi Durable 包龄短、API 可无通知变动；隔离实验 API 保护业务与测试 | 包裹执行层全部工具与 conversation | ✅ 已完成（commit d1438bd） | `runner/src/tool-spec.ts`、`durable-adapter.ts`、`runner.ts`、`docs/HANDOFF.md` |
| 部署运维 | VPS 部署、cron 按需唤醒、凭据/密钥保护、私有访问、备份恢复、磁盘保留、操作手册 | 长程稳定与隐私边界 | 发现层与交付层的运行时底座 | ⬜ 未开始（留待打磨）；拓扑已设计 | `docs/TECH_STACK.md`·部署拓扑、`docs/IMPLEMENTATION_PLAN.md`·打磨 |
| 分类过滤 | 「只关心 Chelsea」过滤，转录前用便宜 LLM 对标题/简介分类（< $0.005/期） | 成本闸门，避免无关节目进入付费转写；纯元数据会漏男足讨论 | 发现层，入队前过滤候选 | ⬜ 未开始（留待自动化主链）；策略已定 | `docs/REQUIREMENTS.md`·节目源管理、`docs/IMPLEMENTATION_PLAN.md`·自动化主链 |

## 主线 vs 支线的投入哲学

- 主线只是数据流骨架：把「发现→下载→转写→重组→翻译→审核→交付」串起来，本身不构成项目壁垒。
- 支线才是项目价值所在。需求文档真正花篇幅的是：禁止二次消费（幂等、成本与计费透明）、崩溃恢复（调度与长程运行）、override（人工修改永不被覆盖）、评测自动生长（审核修改即标注）。这些横切机制决定系统能否长程正确、成本可控、质量可追踪。
- 当前投入分布印证这一点：已完成的工作几乎全部落在支线——ToolSpec 框架隔离、账本两段式、kill -9 崩溃实验、Scribe 幂等查证与防重复计费设计、知识库 init；主线仅完成下载与词级重组两个环节。执行骨架与横切保障先于主线铺开，符合计划「状态与产物先于自动化规模」的贯穿约束。
- 「禁止二次消费」是支线的支点：服务端无幂等键已实测证实，因此它决定转写/翻译的恢复策略（凭 ID 查询而非重发）、账本语义（intent→outcome、unknown 态）、以及自动重试永远关闭这一红线。

## 当前阻塞清单

| 阻塞项 | 状态 | 卡住的主线环节 | 说明 |
| --- | --- | --- | --- |
| Scribe key | 已解决（`.env` 中 `ELEVENLABS_API_KEY`，不入库） | 转写（此项阻塞已解除） | 转写仍被样本节目阻塞；`pipeline/scribe.py` 未实现 |
| 模型凭据（翻译快模型、复核同档模型、调研快模型） | 已解决（2026-11-06）：DeepSeek key 已填 `.env` 并实测可用 | 翻译与复核（此项阻塞已解除） | 路由已定：翻译/调研 `deepseek/deepseek-flash`、复核 `deepseek/deepseek-v4-pro`；换 provider 只改 `.env` 路由行 + 对应 key |
| 样本节目（一期真实 mp3 或 RSS 链接） | 已选定（2026-11-06）：《Boss with Jamie Carragher: Xabi Alonso》37:29 | 转写 + 翻译与复核（此项阻塞已解除） | enclosure `https://episodes.captivate.fm/episode/8db0bde6-c9cb-43be-92ab-b18987fbdc58.mp3`，feed `https://feeds.captivate.fm/sky-sports-premierleague/`（已实测 200）；转写预估 ≈$0.14 |
| Scribe 计费单价差异 | 已解决（充值后重测 $0.22/h 零残差吻合，$0.40/h 系免费计划折算口径） | 转写成本基线 | 详见 `docs/verification/scribe-idempotency-controlled-test.md` 结案节 |
| 需求修订（幂等键不成立） | 待用户确认 | 转写（防重复计费条目） | 修订提案见 `docs/verification/scribe-idempotency-controlled-test.md`；改 `docs/REQUIREMENTS.md` 对应条目 |
