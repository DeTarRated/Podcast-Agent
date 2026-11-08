# 最小 MVP 交接文档

- 日期：2026-11
- 阶段：最小 MVP 收口（五阶段全部完成：基础架构 / 流程测试 / 真实转写 / 翻译与复核 / 收口与回填）
- 状态：最小 MVP 完成，单期真实节目全链路产物落盘，Episode = reviewing（本地待审核）
- 全景与分层索引见 `docs/ROADMAP.md`；本文是实现变更与验收的交接记录

## 实现变更摘要

最小 MVP 建立了「单期节目从固化音频到本地待审核」的执行骨架：

- **执行层 pipeline/**：download.py（下载固化）、scribe.py（Scribe 真实转写）、wordrender.py（词级重组）、orchestrate.py（translate-srt 确定性步骤编排，主代理驱动）
- **执行层 runner/**：Runner 薄封装（隔离 Pi Durable）、ToolSpec 中立契约、durable-adapter（工具转换 + dispatch 子代理派发 + 账本闭环）、experiment-subagent.ts（真实子代理执行体，DeepSeek 经 pi-ai）
- **state/ 数据契约**：schema.sql（episode/artifact/ledger 三表）、episode/manifest/ledger 读写封装、ledger_cli.py（Node 经 bash 调用的窄账本入口）

## 通过验收项（对照实施计划验收口径）

- **产物真实存在且关联正确**：样本期产出英文 SRT、修正稿、中文译文、复核稿、重切分候选、术语表与检查记录，全部落盘并登记 manifest
- **句子边界可回溯词级时间戳**：en.provenance.json 保存每条 cue 的 word_indices 指向原始 token
- **译文与原文对齐**：apply/check 校验通过；复核删除停顿条目（30 条）为合法编辑，provenance 无越界插值（219 个插值点全部落在原条目区间内）
- **崩溃恢复不重复付费**：转写 unsafe 不自动重跑；子代理失败即终态不自动重派；账本 intent→outcome 两段式 + unknown 对账态 + reconcile 核销
- **审核前无上传/交付**：Episode 仅推进到 reviewing，未上传 R2、未标记交付
- **架构待验证事项有可复现实验**：子代理接入定为嵌套任务（`docs/verification/subagent-dispatch.md`）；Scribe 幂等键查证（服务端无幂等键，凭 transcription_id 恢复）与计费口径结案（PAYG $0.22/h）

## 未通过 / 留待后续阶段的验收项

- 人工审核页、override 持久化、审批/驳回：审核与交付阶段
- R2 上传、S3 签名 URL、看点页、用户选交付版本：审核与交付阶段
- 知识库沉淀（skill 第 8 步）、评测自动生长：审核与交付 + 长程阶段
- 自动发现（feed 轮询/分类过滤）、多期并行、跨天恢复：自动化主链 + 长程阶段

## 复现入口

- 全量测试：`uv run pytest tests/`（72 passed）；`cd runner && npm test`（16 passed）
- 单期流程（样本期已跑通，可参考命令序列）：
  - `uv run python -m pipeline.orchestrate prepare 1`
  - 主代理补 `_context/brief.md` / `gaps.md` / `glossary.md`
  - `uv run python -m pipeline.orchestrate fix 1 --map S01=Carragher --map S02=Alonso`
  - 主代理定点修正 en_fix.srt 后 `fix 1 --manual-done --allow-unmatched`
  - 翻译派发：`cd runner && node --experimental-strip-types src/drive-translate.ts`
  - `uv run python -m pipeline.orchestrate apply 1`
  - 复核派发：`node --experimental-strip-types src/drive-review.ts`
  - `uv run python -m pipeline.orchestrate finalize 1`

## 样本与产物位置

- 样本：《Boss with Jamie Carragher: Xabi Alonso》（Sky Sports PL Podcast，2026-10-26，37:29，36MB）
- 工作区：`var/episodes/1/`（gitignore 内，不入库）
- 关键产物：`en.srt`（英文 709 条）→ `en_fix.srt`（修正）→ `en_zh.srt`（译文）→ `en_zh_fix.srt`（复核 679 条）→ `en_zh_split.srt`（重切分 898 条）；`_context/`（brief/glossary/review_notes/orchestrate_state）；`_context/logs/`（各命令日志与 provenance）

## 费用（真实实测，详见 docs/verification/mvp-cost-baseline.md）

- 单期合计 ≈ **$0.36**（转写 $0.137 + 翻译 $0.0175 + 复核 $0.205），在需求 $0.5 目标线内
- 成本结构：复核 > 转写 > 翻译；复核是质量优先的刻意代价

## 恢复说明

- 转写中断：凭 `scribe_transcription_id.txt` 的 ID 走 `pipeline/scribe.py fetch` 免费恢复；ID 未拿的窗口记 unknown 走人工对账
- 子代理（翻译/复核）失败：任务失败即终态，不自动重跑；主代理判断是否重派（新 call_ref 版本）
- 账本卡在 intent/unknown：经 `state/ledger_cli.py reconcile` 凭真实结果核销为 success 并回填 usage
- Runner 状态：SQLite transcript 持久化，重开 `resume()` 继续；restartLimit 熔断 parked

## 剩余阻塞 / 待确认

- 需求修订（幂等键条目）：修订提案在 `docs/verification/scribe-idempotency-controlled-test.md`，待用户确认后改 `docs/REQUIREMENTS.md`
- 项目命名：按 `podcast_agent` 默认开工，用户定名后一次性全局重命名
- 模型路由：DeepSeek 先行（flash 翻译/调研 + v4-pro 复核），换 provider 改 `.env` 路由行 + 对应 key
- 知识库沉淀：本期专名自拟（未联网调研），沉淀留交付后演练

## 下一阶段

进入「审核与交付」：审核页（双语对照/定点修改/审批驳回）、override 机制、交付层（看点页/R2/签名 URL）。人工修改保护已纳入数据契约（orchestrate 的 phase 保护 + 版本关联）。
